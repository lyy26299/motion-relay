"""Application bridge between user transcripts, memory tools and Qwen audio.

The realtime model still owns ordinary conversational turns.  This bridge is
used for the narrow set of turns that require durable memory: it asks the
bounded AgentLoop to retrieve exact local facts, then injects an evidence-
scoped prompt into the active Qwen websocket.  The model never receives a
database handle or a user-scope argument.
"""

from __future__ import annotations

import asyncio
import logging
import json
import time
from collections import Counter
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
from coach.memory.feedback import FeedbackRecorder
from coach.memory.retrieval import RetrievalService
from coach.memory.store import MemoryStore, NotFoundError, ScopeError
from coach.mcp_server import MCPMemoryDispatcher
from coach.arbiter import FeedbackArbiter, FeedbackKind
from coach.operations import OperationCapacityError, OperationPool
from coach.models import CoachEvent
from coach.runtime import MotionRuntime
from coach.working_memory import WorkingMemory


LOGGER = logging.getLogger(__name__)

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
        self._closed = False
        self.task_rejections = 0
        self._pending_work: Awaitable[Any] | None = None
        self._output_generation = 0
        self._awaiting_user_transcript = False
        self.arbiter = FeedbackArbiter()
        self.io = OperationPool(max_pending=4)
        # Snapshot epoch for pure in-loop checkpoints. Actual storage epochs
        # are checked off-loop before an answer is sent; tools also carry one.
        self._memory_epoch = store.get_memory_epoch(self.user_id)
        self.feedback_recorder = FeedbackRecorder(
            store, self.user_id, self.session_id, self._memory_epoch,
        )
        self.feedback_metrics: Counter[str] = Counter()
        self._results_by_turn: dict[str, tuple[ToolResult, ...]] = {}
        self._trigger_text_by_turn: dict[str, str] = {}
        retrieval = RetrievalService(store, self.user_id)
        self.dispatcher = MCPMemoryDispatcher(retrieval)
        self.loop = AgentLoop(
            user_id=self.user_id,
            memory=memory,
            session_epoch=lambda: self._session_epoch,
            memory_epoch=lambda: self._memory_epoch,
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
        if not normalized or self._closed:
            return
        had_vad = self._awaiting_user_transcript
        self._awaiting_user_transcript = False
        # VAD already invalidated the previous turn. A normal final transcript
        # must not cancel the native answer that has just started. Routed turns
        # still revoke that answer before accessing memory or issuing a warning.
        if not had_vad or _is_discomfort_report(normalized) or _is_history_question(normalized):
            self._invalidate_output()
        self.memory.add_dialogue("user", normalized)
        if _is_discomfort_report(normalized):
            self.motion_runtime.pause("user_reported_discomfort")
            self.log("已根据用户不适描述暂停动作事实采集。")
            self._spawn(self._safety_prompt(normalized))
            return
        if _is_history_question(normalized):
            self._spawn(self._run_history_turn(normalized))

    def on_user_speech_started(self) -> None:
        """Synchronous invalidation at VAD, not after slow transcription/retrieval."""
        self._awaiting_user_transcript = True
        self._invalidate_output()

    def _invalidate_output(self) -> None:
        if self._pending_work is not None:
            self._pending_work.close()
            self._pending_work = None
        self._output_generation += 1
        self.arbiter.interrupt()
        self.memory.cancel_task()
        for task in tuple(self._tasks):
            task.cancel()

    def admit_native_response(self, response_id: str) -> Callable[[], bool] | None:
        """Grant output permission, NOT evidence validation or request attribution.

        The provider calls this synchronously before emitting a native response.
        No database or model calls are allowed on this admission path.
        """
        if (self._closed or not isinstance(response_id, str) or not response_id.strip()
                or len(response_id) > 249 or self._tasks or self._pending_work is not None
                or self.loop.active_turn_id is not None):
            return None
        generation = self._output_generation
        admission = self.arbiter.admit(
            FeedbackKind.ANSWER, f"native:{response_id}", deadline=time.monotonic() + 12.0,
        )
        lease = admission.lease
        if lease is None:
            return None

        def current() -> bool:
            return (not self._closed and generation == self._output_generation
                    and self.arbiter.current(lease))

        return current

    def on_motion_events(self, events: tuple[CoachEvent, ...]) -> None:
        """Bounded trigger bridge; never wait in the pose callback."""
        if self._closed:
            return
        for event in reversed(events):
            if event.kind not in {"rep_completed", "visibility_lost", "multiple_people"}:
                continue
            if event.kind == "rep_completed":
                if self._tasks or self._pending_work or self.loop.active_turn_id is not None:
                    return  # Do not supersede a user question with routine counting.
                text = f"本地动作记录：已完成第 {event.facts['rep_index']} 次。"
                kind = FeedbackKind.REP
            else:
                # Visibility safety preempts history, but repeated reports do
                # not continually restart a warning already being delivered.
                active = self.arbiter.active_kind
                if active is not None and active >= FeedbackKind.SAFETY:
                    return
                self._invalidate_output()
                text = "当前画面不足以继续判断动作，请先暂停并确认摄像头。"
                kind = FeedbackKind.SAFETY
            self._spawn(self._run_motion_turn(event, text, kind))
            break

    async def _run_motion_turn(self, event: CoachEvent, text: str, kind: FeedbackKind) -> None:
        await self.loop.run(AgentTrigger(
            kind="motion_event", text=text, event_id=event.event_id,
            occurred_at_mono=event.occurred_at, ttl_s=2.5,
            metadata={"feedback_kind": int(kind)},
        ))

    def _spawn(self, coroutine: Awaitable[Any]) -> None:
        if self._closed:
            coroutine.close()
            return
        if len(self._tasks) >= 4:
            if self._pending_work is not None:
                self._pending_work.close()
            self._pending_work = coroutine  # One latest-work slot, not an unbounded queue.
            self.task_rejections += 1
            if self.task_rejections & (self.task_rejections - 1) == 0:
                LOGGER.warning("bridge_task_rejected", extra={
                    "component": "session_bridge", "rejections": self.task_rejections,
                })
            return
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            LOGGER.warning("bridge_task_failed", extra={"component": "session_bridge"})
        active = self.loop.active_turn_id
        for mapping in (self._results_by_turn, self._trigger_text_by_turn):
            for key in tuple(mapping):
                if key != active:
                    mapping.pop(key, None)
        if self._pending_work is not None and not self._closed:
            pending, self._pending_work = self._pending_work, None
            self._spawn(pending)

    async def _io(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        operation = self.io.start(callback, *args, **kwargs)
        done, _ = await asyncio.wait({operation}, timeout=0.5)
        if not done:
            raise TimeoutError("storage_operation_timeout")
        return operation.result()

    async def close(self) -> None:
        self._closed = True
        self._awaiting_user_transcript = False
        self._invalidate_output()
        if self._tasks:
            await asyncio.wait(tuple(self._tasks), timeout=0.1)
        pending = await self.loop.close(timeout_s=0.05)
        pending += await self.io.close(timeout_s=0.05)
        if pending:
            LOGGER.warning("bridge_operations_pending_on_close", extra={"pending": pending})
        self._results_by_turn.clear()
        self._trigger_text_by_turn.clear()

    async def _safety_prompt(self, text: str) -> None:
        await self._inject(
            "用户报告了不适："
            + text[:160]
            + "。请用一句中文明确要求立即停止训练，不要诊断原因；等待用户确认后再继续。",
            event_id=None,
            facts={"safety_pause": True},
            kind=FeedbackKind.SAFETY,
        )

    async def _run_history_turn(self, text: str) -> None:
        result = await self.loop.run(
            AgentTrigger(kind="history_query", text=text, ttl_s=4.0),
            timeout_s=3.5,
        )
        if result.status != "completed" or result.decision is None:
            self.log(f"历史检索未执行：{result.reason or result.status}")

    def _tool(self, request: Any, _context: ToolContext) -> dict[str, Any]:
        return self.dispatcher.safe_dispatch(request.name, request.arguments)

    async def _decide(self, context: DecisionContext) -> DecisionDraft:
        trigger = context.observation.basis.trigger
        if trigger.kind == "motion_event":
            refs = tuple(ref for ref in context.observation.evidence_refs
                         if ref.source_type == "event" and ref.source_id == trigger.event_id)
            if not refs:
                return DecisionDraft(action="abstain")
            return DecisionDraft(action="cue", evidence_refs=refs, utterance_intent=trigger.text)
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
        if decision.action == "cue":
            return await self._inject(
                decision.utterance_intent, event_id=_context.basis.trigger.event_id,
                facts={"turn_id": decision.turn_id},
                kind=FeedbackKind(_context.basis.trigger.metadata["feedback_kind"]),
                is_current=_context.is_current, deadline=decision.deadline_mono,
            )
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
            is_current=_context.is_current, deadline=decision.deadline_mono,
        )

    async def _inject(
        self,
        prompt: str,
        *,
        event_id: str | None,
        facts: dict[str, Any],
        kind: FeedbackKind = FeedbackKind.ANSWER,
        is_current: Callable[[], bool] = lambda: True,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        generation = self._output_generation
        admission = self.arbiter.admit(
            kind, event_id or f"turn-{generation}-{kind.name}",
            deadline=deadline if deadline is not None else time.monotonic() + 4.0,
        )
        lease = admission.lease
        LOGGER.info("feedback_admission", extra={
            "component": "arbiter", "status": admission.reason,
            "session_id": self.session_id, "event_id": event_id,
        })
        if lease is None:
            return {"status": "rejected", "details": {"reason": admission.reason}}

        def playback_current() -> bool:
            return (not self._closed and generation == self._output_generation
                    and self.arbiter.current(lease))

        def current() -> bool:
            return playback_current() and is_current()

        feedback_id = None
        accepted = False
        try:
            if not current() or self.qwen is None or not getattr(self.qwen, "connected", False):
                return {"status": "rejected", "details": {"reason": "stale_or_disconnected"}}
            # Hard local pause precedes this method. Its voice request does not
            # wait on SQLite; no durable receipt is falsely claimed for it.
            if kind < FeedbackKind.SAFETY:
                epoch = await self._io(self.store.get_memory_epoch, self.user_id)
                if not current() or epoch != self._memory_epoch:
                    return {"status": "rejected", "details": {"reason": "scope_or_turn_changed"}}
                feedback = await self._io(
                    self.store.record_feedback, self.user_id, self.session_id,
                    cue_text=prompt[:800], event_id=None, status="accepted",
                    playback_state="unknown", facts={**facts, "event_id": event_id,
                                                     "response_correlation": "unverified"},
                    idempotency_key=f"agent-{self.session_id}-{lease.generation}",
                )
                feedback_id = str(feedback["feedback_id"])
                epoch = await self._io(self.store.get_memory_epoch, self.user_id)
                if epoch != self._memory_epoch:
                    return {"status": "rejected", "details": {"reason": "scope_changed"}}
            if not current():
                return {"status": "rejected", "details": {"reason": "stale"}}
            accepted = await self.qwen.inject_text(
                prompt[:6000], interrupt=True, feedback_id=feedback_id,
                is_current=current, playback_guard=playback_current,
            )
            if not accepted:
                self.arbiter.finish(lease)
            return {"status": "queued" if accepted else "rejected", "feedback_id": feedback_id,
                    "details": {"response_correlation": "unverified"}}
        except asyncio.CancelledError:
            self.arbiter.finish(lease)
            raise
        except Exception:
            self.arbiter.finish(lease)
            LOGGER.warning("feedback_injection_failed", extra={"component": "session_bridge"})
            return {"status": "rejected", "feedback_id": feedback_id}
        finally:
            if not accepted:
                self.arbiter.finish(lease)
                if feedback_id is not None and not self._closed:
                    self._spawn(self.feedback_state("rejected", feedback_id, None))

    async def feedback_state(
        self, state: str, feedback_id: str | None, response_id: str | None
    ) -> None:
        if not feedback_id or self._closed:
            return
        # Observers can race after cancellation or finish out of order. Merge
        # inside the storage transaction; a lock around asyncio callbacks would
        # not fence an already-running worker or another SQLite connection.
        try:
            update = await self._io(self.feedback_recorder.apply, feedback_id, state, response_id)
        except asyncio.CancelledError:
            self.feedback_metrics["wait_cancelled"] += 1
            raise  # The owned worker may still commit; do not claim rollback.
        except ScopeError:
            self.feedback_metrics["scope_rejected"] += 1
            LOGGER.warning("feedback_scope_rejected", extra={"component": "feedback"})
        except (ValueError, NotFoundError):
            self.feedback_metrics["invalid_observation"] += 1
            LOGGER.warning("feedback_observation_invalid", extra={"component": "feedback"})
        except OperationCapacityError:
            self.feedback_metrics["capacity_rejected"] += 1
            LOGGER.warning("feedback_capacity_rejected", extra={"component": "feedback"})
        except TimeoutError:
            self.feedback_metrics["commit_wait_timeout"] += 1
            LOGGER.warning("feedback_commit_unconfirmed", extra={"component": "feedback"})
        except Exception:
            self.feedback_metrics["storage_error"] += 1
            raise
        else:
            self.feedback_metrics[update.reason] += 1
            if update.reason in {"response_id_conflict", "outcome_conflict", "unmanaged_existing_status"}:
                LOGGER.warning("feedback_observation_conflict", extra={
                    "component": "feedback", "status": update.reason,
                })


__all__ = ["SessionAgentBridge"]
