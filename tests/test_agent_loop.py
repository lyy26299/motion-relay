"""Contract tests for the bounded, user-scoped coach agent loop."""

from __future__ import annotations

import asyncio
import unittest

from coach.agent_loop import (
    ActionReceipt,
    AgentLoop,
    AgentLoopConfig,
    AgentTrigger,
    ClaimRef,
    DecisionDraft,
    EvidenceRef,
    ProposedPlan,
    ToolCall,
)
from coach.models import PoseSnapshot, ProjectedAngles
from coach.working_memory import WorkingMemory


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.memory = WorkingMemory(session_id="session-1")
        self.epoch = {"value": 1}
        self.actor_calls = []

    def make_loop(self, *, decider, tools=None, actor=None, **kwargs) -> AgentLoop:
        if actor is None:

            async def actor(decision, context):
                self.actor_calls.append(decision)
                self.assertTrue(context.is_current())
                return ActionReceipt(status="queued", response_id="response-1")

        return AgentLoop(
            user_id="user-1",
            memory=self.memory,
            session_epoch=lambda: self.epoch["value"],
            tools=tools or {},
            decider=decider,
            actor=actor,
            **kwargs,
        )

    async def test_retrieve_decide_act_carries_only_admitted_evidence(self):
        reference = EvidenceRef(
            source_type="session_summary",
            source_id="summary-old",
            revision=2,
            user_id="user-1",
            session_id="session-1",
        )
        seen_requests = []

        async def history_tool(request, context):
            seen_requests.append(request)
            self.assertEqual(request.arguments, {"exercise": "squat", "limit": 1})
            self.assertEqual(context.basis.user_id, "user-1")
            self.assertTrue(context.is_current())
            return {
                "schema_version": "coach.retrieval.v1",
                "request_id": request.call_id,
                "items": [{"completed_reps": 8, "valid_reps": 7}],
                "evidence_refs": [
                    {
                        "source_type": reference.source_type,
                        "source_id": reference.source_id,
                        "revision": reference.revision,
                        "user_id": reference.user_id,
                        "session_id": reference.session_id,
                    }
                ],
                "truncated": False,
                "error": None,
            }

        async def decider(context):
            if context.round_index == 1:
                return DecisionDraft(
                    tool_calls=(
                        ToolCall(
                            name="memory.query_training",
                            arguments={"exercise": "squat", "limit": 1},
                        ),
                    )
                )
            self.assertEqual(context.tool_results[0].status, "ok")
            # Normal pose updates do not invalidate a historical answer merely
            # because WorkingMemory.state_version advanced.
            self.memory.add_dialogue("system", "new live observation")
            return DecisionDraft(
                action="answer",
                evidence_refs=(reference,),
                claim_refs=(
                    ClaimRef(
                        claim_key="last_session_reps",
                        evidence_ids=(reference.evidence_id,),
                    ),
                ),
                utterance_intent="上次完成 8 次，其中 7 次有效。",
            )

        loop = self.make_loop(decider=decider, tools={"memory.query_training": history_tool})
        result = await loop.run(AgentTrigger(kind="user_question", text="上次做了几次？"))

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(seen_requests), 1)
        self.assertEqual(result.tool_results[0].items[0]["completed_reps"], 8)
        self.assertEqual(result.decision.evidence_refs, (reference,))
        self.assertEqual(result.receipt.status, "queued")
        self.assertGreater(
            result.post_observation.memory.state_version,
            result.basis.basis_state_version,
        )
        self.assertEqual(len(self.actor_calls), 1)
        self.assertIsNone(self.memory.view().active_task)

    async def test_sync_decider_and_actor_are_supported(self):
        actor_contexts = []

        def decider(context):
            return DecisionDraft(action="ask_clarification", utterance_intent="你希望做几次？")

        def actor(decision, context):
            actor_contexts.append(context)
            return {"status": "generated", "response_id": "sync-response"}

        loop = self.make_loop(decider=decider, actor=actor)
        result = await loop.run(AgentTrigger(kind="user_question"))

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.receipt.response_id, "sync-response")
        self.assertEqual(len(actor_contexts), 1)

    async def test_tool_timeout_is_typed_and_not_treated_as_empty_history(self):
        async def slow_tool(request, context):
            await asyncio.sleep(1)
            return {"items": [{"should_not": "arrive"}]}

        async def decider(context):
            if context.round_index == 1:
                return DecisionDraft(tool_calls=(ToolCall("memory.get_profile"),))
            self.assertEqual(context.tool_results[0].status, "timeout")
            self.assertEqual(context.tool_results[0].error_code, "tool_timeout")
            return DecisionDraft(action="abstain")

        loop = self.make_loop(
            decider=decider,
            tools={"memory.get_profile": slow_tool},
            config=AgentLoopConfig(tool_timeout_s=0.01),
        )
        result = await loop.run(AgentTrigger(kind="user_question"), timeout_s=1)

        self.assertEqual(result.status, "abstained")
        self.assertEqual(result.tool_results[0].status, "timeout")
        self.assertEqual(self.actor_calls, [])

    async def test_retrieval_envelope_preserves_null_revision_and_memory_epoch(self):
        memory_epoch = {"value": 7}
        reference = EvidenceRef("profile_fact", "fact-goal", revision=None)

        async def tool(request, context):
            return {
                "items": [{"fact_key": "goal", "value": "mobility"}],
                "evidence_refs": [
                    {
                        "source_type": "profile_fact",
                        "source_id": "fact-goal",
                        "revision": None,
                    }
                ],
                "scope_epoch": 7,
                "error": None,
            }

        async def decider(context):
            if context.round_index == 1:
                return DecisionDraft(tool_calls=(ToolCall("memory.get_profile"),))
            return DecisionDraft(
                action="answer",
                evidence_refs=(reference,),
                utterance_intent="你的当前目标是提升活动能力。",
            )

        loop = self.make_loop(
            decider=decider,
            tools={"memory.get_profile": tool},
            memory_epoch=lambda: memory_epoch["value"],
        )
        result = await loop.run(AgentTrigger(kind="user_question"))

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.basis.memory_epoch, 7)
        self.assertEqual(result.tool_results[0].scope_epoch, 7)
        self.assertIsNone(result.decision.evidence_refs[0].revision)

    async def test_mismatched_tool_scope_epoch_is_rejected_as_stale(self):
        async def tool(request, context):
            return {"items": [{"private": "old"}], "scope_epoch": 6}

        async def decider(context):
            return DecisionDraft(tool_calls=(ToolCall("memory.get_profile"),))

        loop = self.make_loop(
            decider=decider,
            tools={"memory.get_profile": tool},
            memory_epoch=lambda: 7,
        )
        result = await loop.run(AgentTrigger(kind="user_question"))

        self.assertEqual(result.status, "stale")
        self.assertEqual(result.reason, "tool_scope_epoch_mismatch")
        self.assertEqual(result.tool_results, ())
        self.assertEqual(self.actor_calls, [])

    async def test_missing_tool_scope_epoch_is_rejected_when_bound(self):
        async def tool(request, context):
            return {"items": [{"private": "unscoped"}]}

        async def decider(context):
            return DecisionDraft(tool_calls=(ToolCall("memory.get_profile"),))

        loop = self.make_loop(
            decider=decider,
            tools={"memory.get_profile": tool},
            memory_epoch=lambda: 7,
        )
        result = await loop.run(AgentTrigger(kind="user_question"))

        self.assertEqual(result.status, "stale")
        self.assertEqual(result.reason, "tool_scope_epoch_missing")
        self.assertEqual(self.actor_calls, [])

    async def test_cancellation_drops_a_late_tool_result_and_never_acts(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def tool(request, context):
            started.set()
            await release.wait()
            return {"items": [{"late": True}]}

        async def decider(context):
            if context.round_index == 1:
                return DecisionDraft(tool_calls=(ToolCall("memory.get_profile"),))
            return DecisionDraft(action="answer", utterance_intent="late")

        loop = self.make_loop(
            decider=decider,
            tools={"memory.get_profile": tool},
            config=AgentLoopConfig(tool_timeout_s=1),
        )
        task = asyncio.create_task(loop.run(AgentTrigger(kind="user_question"), timeout_s=2))
        await asyncio.wait_for(started.wait(), timeout=0.5)
        self.assertTrue(await loop.cancel_active("user_interrupted"))
        release.set()
        result = await asyncio.wait_for(task, timeout=0.5)

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.reason, "user_interrupted")
        self.assertEqual(result.tool_results, ())
        self.assertEqual(self.actor_calls, [])

    async def test_cancellation_while_actor_waits_drops_its_receipt(self):
        actor_started = asyncio.Event()

        async def decider(context):
            return DecisionDraft(action="answer", utterance_intent="pending")

        async def actor(decision, context):
            actor_started.set()
            await asyncio.sleep(1)
            return ActionReceipt(status="queued", response_id="late-response")

        loop = self.make_loop(decider=decider, actor=actor)
        task = asyncio.create_task(loop.run(AgentTrigger(kind="user_question")))
        await asyncio.wait_for(actor_started.wait(), timeout=0.5)
        await loop.cancel_active("user_interrupted")
        result = await asyncio.wait_for(task, timeout=0.5)

        self.assertEqual(result.status, "cancelled")
        self.assertIsNotNone(result.decision)
        self.assertIsNone(result.receipt)

    async def test_changed_session_epoch_rejects_returned_tool_data(self):
        async def tool(request, context):
            self.epoch["value"] += 1
            return {"items": [{"completed_reps": 99}]}

        async def decider(context):
            return DecisionDraft(tool_calls=(ToolCall("memory.query_training"),))

        loop = self.make_loop(decider=decider, tools={"memory.query_training": tool})
        result = await loop.run(AgentTrigger(kind="user_question"))

        self.assertEqual(result.status, "stale")
        self.assertEqual(result.reason, "session_epoch_changed")
        self.assertEqual(result.tool_results, ())
        self.assertEqual(self.actor_calls, [])

    async def test_new_generation_supersedes_pending_turn(self):
        first_started = asyncio.Event()

        async def tool(request, context):
            first_started.set()
            await asyncio.sleep(1)
            return {"items": [{"late": True}]}

        async def decider(context):
            if context.observation.basis.trigger.text == "old":
                return DecisionDraft(tool_calls=(ToolCall("memory.get_profile"),))
            return DecisionDraft(action="answer", utterance_intent="new answer")

        loop = self.make_loop(
            decider=decider,
            tools={"memory.get_profile": tool},
            config=AgentLoopConfig(tool_timeout_s=2),
        )
        old_task = asyncio.create_task(
            loop.run(AgentTrigger(kind="user_question", text="old"), timeout_s=3)
        )
        await asyncio.wait_for(first_started.wait(), timeout=0.5)
        new_result = await loop.run(AgentTrigger(kind="user_question", text="new"), timeout_s=1)
        old_result = await asyncio.wait_for(old_task, timeout=0.5)

        self.assertEqual(new_result.status, "completed")
        self.assertEqual(old_result.status, "stale")
        self.assertEqual(old_result.reason, "superseded_by_new_turn")
        self.assertEqual(old_result.tool_results, ())
        self.assertEqual(len(self.actor_calls), 1)

    async def test_relevance_guard_rechecks_live_fact_before_action(self):
        guard_calls = []

        async def decider(context):
            return DecisionDraft(action="cue", utterance_intent="膝盖方向保持稳定。")

        def relevance_guard(basis, decision, latest):
            guard_calls.append((basis, decision, latest))
            return "form_issue_cleared"

        loop = self.make_loop(decider=decider, relevance_guard=relevance_guard)
        result = await loop.run(AgentTrigger(kind="form_issue", event_id="event-now", ttl_s=2))

        self.assertEqual(result.status, "stale")
        self.assertEqual(result.reason, "form_issue_cleared")
        self.assertEqual(len(guard_calls), 1)
        self.assertEqual(self.actor_calls, [])

    async def test_evidence_expiry_is_rechecked_after_relevance_guard(self):
        now = {"value": 10.0}
        self.memory.add_pose(
            PoseSnapshot(
                session_id="session-1",
                stream_epoch=1,
                frame_id=1,
                observed_at=10.0,
                processed_at=10.0,
                media_time_s=None,
                width=640,
                height=480,
                model="test",
                keypoint_threshold=0.5,
                detections=(),
                angles=ProjectedAngles(),
                status="no_person",
                processing_ms=0.0,
            )
        )

        async def decider(context):
            return DecisionDraft(
                action="cue",
                evidence_refs=(context.observation.evidence_refs[-1],),
                utterance_intent="保持膝盖方向。",
            )

        def guard(basis, decision, latest):
            now["value"] = 50.0
            return True

        loop = self.make_loop(
            decider=decider,
            relevance_guard=guard,
            clock=lambda: now["value"],
            config=AgentLoopConfig(default_deadline_s=60, max_deadline_s=60),
        )
        result = await loop.run(AgentTrigger(kind="form_issue"), timeout_s=60)

        self.assertEqual(result.status, "stale")
        self.assertEqual(result.reason, "decision_evidence_expired")
        self.assertEqual(self.actor_calls, [])

    async def test_fabricated_evidence_and_scope_arguments_are_rejected(self):
        cases = []

        async def unused_tool(request, context):
            cases.append(request)
            return {"items": []}

        async def scoped_decider(context):
            return DecisionDraft(
                tool_calls=(
                    ToolCall(
                        "memory.query_training",
                        {"filters": {"user_id": "another-user"}},
                    ),
                )
            )

        scoped_loop = self.make_loop(
            decider=scoped_decider, tools={"memory.query_training": unused_tool}
        )
        scoped = await scoped_loop.run(AgentTrigger(kind="user_question"))
        self.assertEqual(scoped.status, "invalid_decision")
        self.assertIn("cannot select scope", scoped.reason)
        self.assertEqual(cases, [])

        fabricated = EvidenceRef("rep", "not-observed")

        async def fabricated_decider(context):
            return DecisionDraft(
                action="answer",
                evidence_refs=(fabricated,),
                utterance_intent="没有事实依据的回答",
            )

        fabricated_loop = self.make_loop(decider=fabricated_decider)
        result = await fabricated_loop.run(AgentTrigger(kind="user_question"))
        self.assertEqual(result.status, "invalid_decision")
        self.assertIn("unavailable evidence", result.reason)
        self.assertEqual(self.actor_calls, [])

    async def test_round_and_tool_budgets_cannot_be_bypassed(self):
        calls = []

        async def tool(request, context):
            calls.append(request)
            return {"items": []}

        async def too_many(context):
            return DecisionDraft(tool_calls=(ToolCall("a"), ToolCall("a"), ToolCall("a")))

        loop = self.make_loop(decider=too_many, tools={"a": tool})
        result = await loop.run(AgentTrigger(kind="user_question"))
        self.assertEqual(result.status, "invalid_decision")
        self.assertIn("per-round budget", result.reason)
        self.assertEqual(calls, [])

        async def never_finishes(context):
            return DecisionDraft(tool_calls=(ToolCall("a"),))

        exhausted_loop = self.make_loop(decider=never_finishes, tools={"a": tool})
        exhausted = await exhausted_loop.run(AgentTrigger(kind="user_question"))
        self.assertEqual(exhausted.status, "budget_exhausted")
        self.assertEqual(exhausted.reason, "no_decision_round_remains_after_retrieval")
        self.assertEqual(len(exhausted.tool_results), 1)
        self.assertEqual(self.actor_calls, [])

    async def test_total_tool_budget_can_be_lower_than_round_budget(self):
        calls = []

        async def tool(request, context):
            calls.append(request)
            return {"items": []}

        async def decider(context):
            if context.round_index == 1:
                return DecisionDraft(tool_calls=(ToolCall("a"), ToolCall("a")))
            return DecisionDraft(action="abstain")

        loop = self.make_loop(
            decider=decider,
            tools={"a": tool},
            config=AgentLoopConfig(max_tools_per_round=2, max_total_tools=1),
        )
        result = await loop.run(AgentTrigger(kind="user_question"))

        self.assertEqual(result.status, "invalid_decision")
        self.assertIn("total turn budget", result.reason)
        self.assertEqual(calls, [])

    async def test_expired_trigger_does_not_supersede_an_active_turn(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def tool(request, context):
            started.set()
            await release.wait()
            return {"items": []}

        async def decider(context):
            if context.observation.basis.trigger.text == "active" and context.round_index == 1:
                return DecisionDraft(tool_calls=(ToolCall("memory.get_profile"),))
            return DecisionDraft(action="abstain")

        loop = self.make_loop(
            decider=decider,
            tools={"memory.get_profile": tool},
            config=AgentLoopConfig(tool_timeout_s=1),
        )
        active = asyncio.create_task(
            loop.run(AgentTrigger(kind="user_question", text="active"), timeout_s=2)
        )
        await asyncio.wait_for(started.wait(), timeout=0.5)
        active_turn_id = loop.active_turn_id
        expired = await loop.run(
            AgentTrigger(
                kind="form_issue",
                text="old event",
                occurred_at_mono=0.0,
                ttl_s=0.1,
            )
        )

        self.assertEqual(expired.status, "timeout")
        self.assertEqual(expired.reason, "trigger_expired")
        self.assertEqual(loop.active_turn_id, active_turn_id)
        release.set()
        active_result = await asyncio.wait_for(active, timeout=0.5)
        self.assertEqual(active_result.status, "abstained")

    async def test_plan_change_is_only_a_versioned_proposal(self):
        captured = []

        async def decider(context):
            return DecisionDraft(
                action="propose_plan",
                proposed_plan=ProposedPlan(
                    exercise="squat",
                    target_reps=6,
                    reason="最近一组有效率下降",
                    expected_plan_version=3,
                ),
                utterance_intent="建议下一组调整为 6 次，请你确认。",
            )

        async def actor(decision, context):
            captured.append(decision.proposed_plan)
            return ActionReceipt(status="accepted")

        loop = self.make_loop(decider=decider, actor=actor)
        result = await loop.run(AgentTrigger(kind="set_review"))

        self.assertEqual(result.status, "completed")
        self.assertEqual(captured[0].expected_plan_version, 3)
        self.assertNotIn("plan", self.memory.view().active_task or {})


if __name__ == "__main__":
    unittest.main()
