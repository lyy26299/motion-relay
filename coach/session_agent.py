"""Application bridge between user transcripts, memory tools and Qwen audio.

The realtime model still owns ordinary conversational turns.  This bridge is
used for the narrow set of turns that require durable memory: it asks the
bounded AgentLoop to retrieve exact local facts, then injects an evidence-
scoped prompt into the active Qwen websocket.  The model never receives a
database handle or a user-scope argument.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from coach.agent_loop import (
    ActionContext,
    AgentLoop,
    AgentLoopConfig,
    AgentTrigger,
    CoachDecision,
    DecisionContext,
    DecisionDraft,
    ToolCall,
    ToolContext,
    ToolResult,
)
from coach.memory.retrieval import RetrievalService
from coach.memory.store import MemoryStore
from coach.mcp_server import MCPMemoryDispatcher
from coach.runtime import MotionRuntime
from coach.working_memory import WorkingMemory


_HISTORY_TERMS = (
    "上次",
    "之前",
    "历史",
    "记录",
    "进步",
    "比较",
    "多少次",
    "有效",
    "膝盖角度",
    "训练总结",
)
_PAIN_TERMS = ("疼", "痛", "不舒服", "受伤", "不适")


def _is_history_question(text: str) -> bool:
    return any(term in text for term in _HISTORY_TERMS)


def _is_discomfort_report(text: str) -> bool:
    return any(term in text for term in _PAIN_TERMS)


class SessionAgentBridge:
    """Own one session's bounded retrieval loop and Qwen actor."""

    def __init__(
        self,
        *,
        user_id: str,
        session_id: str,
        memory: WorkingMemory,
        store: MemoryStore,
        session_epoch: int,
        motion_runtime: MotionRuntime,
        qwen: Any | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.user_id = str(user_id)
        self.session_id = str(session_id)
        self.memory = memory
        self.store = store
        self.motion_runtime = motion_runtime
        self._session_epoch = int(session_epoch)
        self.qwen = qwen
        self.log = log or (lambda _message: None)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._results_by_turn: dict[str, tuple[ToolResult, ...]] = {}
        self._trigger_text_by_turn: dict[str, str] = {}
        retrieval = RetrievalService(store, self.user_id)
        self.dispatcher = MCPMemoryDispatcher(retrieval)
        self.loop = AgentLoop(
            user_id=self.user_id,
            memory=memory,
            session_epoch=lambda: self._session_epoch,
            memory_epoch=lambda: store.get_memory_epoch(self.user_id),
            tools={name: self._tool for name in self.dispatcher.TOOL_NAMES},
            decider=self._decide,
            actor=self._act,
            config=AgentLoopConfig(
                max_decision_rounds=2,
                max_tools_per_round=1,
                max_total_tools=2,
                default_deadline_s=3.5,
                max_deadline_s=4.0,
                tool_timeout_s=0.5,
            ),
        )

    def set_qwen(self, qwen: Any) -> None:
        self.qwen = qwen

    def on_user_transcript(self, text: str) -> None:
        """Schedule handling without blocking the Qwen websocket reader."""

        normalized = str(text).strip()
        if not normalized:
            return
        self.memory.add_dialogue("user", normalized)
        if _is_discomfort_report(normalized):
            self.motion_runtime.pause("user_reported_discomfort")
            self.log("已根据用户不适描述暂停动作事实采集。")
            self._spawn(self._safety_prompt(normalized))
            return
        if _is_history_question(normalized):
            self._spawn(self._run_history_turn(normalized))

    def _spawn(self, coroutine: Awaitable[Any]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        if self.loop.active_turn_id is not None:
            await self.loop.cancel_active("session_closed")
        if self._tasks:
            for task in tuple(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

    async def _safety_prompt(self, text: str) -> None:
        await self._inject(
            "用户报告了不适："
            + text[:160]
            + "。请用一句中文明确要求立即停止训练，不要诊断原因；等待用户确认后再继续。",
            event_id=None,
            facts={"safety_pause": True, "user_report": text[:160]},
        )

    async def _run_history_turn(self, text: str) -> None:
        result = await self.loop.run(
            AgentTrigger(kind="history_query", text=text, ttl_s=4.0),
            timeout_s=3.5,
        )
        if result.status != "completed" or result.decision is None:
            self.log(f"历史检索未执行：{result.reason or result.status}")

    async def _tool(self, request: Any, _context: ToolContext) -> dict[str, Any]:
        return self.dispatcher.safe_dispatch(request.name, request.arguments)

    def _decide(self, context: DecisionContext) -> DecisionDraft:
        trigger = context.observation.basis.trigger
        if not context.tool_results:
            self._trigger_text_by_turn[context.observation.basis.turn_id] = trigger.text
            return DecisionDraft(
                tool_calls=(
                    ToolCall(
                        "memory.query_training",
                        {"exercise": "squat", "metric": "all", "limit": 5},
                    ),
                )
            )
        self._results_by_turn[context.observation.basis.turn_id] = context.tool_results
        refs = tuple(
            ref
            for result in context.tool_results
            for ref in result.evidence_refs
        )
        return DecisionDraft(
            action="answer",
            evidence_refs=refs,
            utterance_intent=(
                "请只依据下面本地训练事实回答用户，不要补造没有记录的数字；"
                "如果没有匹配记录，明确说没有足够历史依据。"
            ),
        )

    async def _act(self, decision: CoachDecision, _context: ActionContext) -> dict[str, Any]:
        results = self._results_by_turn.pop(decision.turn_id, ())
        payload = [
            {
                "status": result.status,
                "items": list(result.items),
                "evidence_refs": [
                    {
                        "source_type": ref.source_type,
                        "source_id": ref.source_id,
                        "revision": ref.revision,
                    }
                    for ref in result.evidence_refs
                ],
                "error": result.error_message,
            }
            for result in results
        ]
        prompt = (
            "用户问题："
            + decision.utterance_intent
            + "\n原始问题："
            + self._trigger_text_by_turn.pop(decision.turn_id, "")
            + "\n本地记忆检索结果（JSON，仅作事实依据）："
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            + "\n请直接用中文简短回答，并说明依据来自训练记录。"
        )
        return await self._inject(
            prompt,
            event_id=None,
            facts={
                "action": decision.action,
                "evidence_ids": [ref.evidence_id for ref in decision.evidence_refs],
                "turn_id": decision.turn_id,
            },
        )

    async def _inject(
        self,
        prompt: str,
        *,
        event_id: str | None,
        facts: dict[str, Any],
    ) -> dict[str, Any]:
        if self.qwen is None or not getattr(self.qwen, "connected", False):
            return {"status": "rejected", "details": {"reason": "qwen_not_connected"}}
        feedback = await asyncio.to_thread(
            self.store.record_feedback,
            self.user_id,
            self.session_id,
            cue_text=prompt[:800],
            event_id=event_id,
            status="generated",
            playback_state="accepted",
            facts=facts,
            idempotency_key=f"agent-{self.session_id}-{time.time_ns()}",
        )
        feedback_id = str(feedback["feedback_id"])
        try:
            accepted = await self.qwen.inject_text(
                prompt[:6000], interrupt=True, feedback_id=feedback_id
            )
        except Exception as exc:
            await asyncio.to_thread(
                self.store.update_feedback,
                self.user_id,
                feedback_id,
                status="rejected",
                playback_state="rejected",
            )
            return {"status": "rejected", "feedback_id": feedback_id, "details": {"error": str(exc)}}
        if not accepted:
            await asyncio.to_thread(
                self.store.update_feedback,
                self.user_id,
                feedback_id,
                status="rejected",
                playback_state="rejected",
            )
            return {"status": "rejected", "feedback_id": feedback_id}
        return {"status": "queued", "feedback_id": feedback_id}

    async def feedback_state(
        self, state: str, feedback_id: str | None, response_id: str | None
    ) -> None:
        if not feedback_id:
            return
        await asyncio.to_thread(
            self.store.update_feedback,
            self.user_id,
            feedback_id,
            status=state,
            playback_state=state,
            response_id=response_id,
            played_at=time.time() if state == "generated" else None,
        )


__all__ = ["SessionAgentBridge"]
