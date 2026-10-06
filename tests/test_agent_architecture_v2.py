"""No network, model, camera or microphone is required by this suite."""
import asyncio
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from coach.agent_loop import AgentLoop, AgentTrigger, DecisionDraft
from coach.arbiter import FeedbackArbiter, FeedbackKind
from coach.memory.store import MemoryStore
from coach.operations import OperationPool, OperationCapacityError
from coach.runtime import MotionRuntime
from coach.session_agent import SessionAgentBridge
from coach.working_memory import WorkingMemory
from test_runtime_boundaries import pose


class ArbiterTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.arbiter = FeedbackArbiter(clock=lambda: self.now, history_size=4)

    def admit(self, kind=FeedbackKind.REP, key="rep", deadline=20):
        return self.arbiter.admit(kind, key, deadline=deadline)

    def test_safety_preempts_encouragement(self):
        first = self.admit(FeedbackKind.ENCOURAGEMENT, "enc")
        second = self.admit(FeedbackKind.SAFETY, "safety")
        self.assertTrue(second.preempted)
        self.assertFalse(self.arbiter.current(first.lease))
        self.assertTrue(self.arbiter.current(second.lease))

    def test_low_priority_is_rejected_not_queued(self):
        self.admit(FeedbackKind.SAFETY, "safety")
        self.assertEqual(self.admit().reason, "busy")

    def test_duplicate_and_cooldown_are_distinct(self):
        first = self.admit()
        self.arbiter.finish(first.lease)
        self.assertEqual(self.admit().reason, "duplicate")
        self.assertEqual(self.admit(key="next").reason, "cooldown")
        self.now += 1
        self.assertIsNotNone(self.admit(key="next").lease)

    def test_stale_and_silence_do_not_acquire_output(self):
        self.assertEqual(self.admit(deadline=10).reason, "stale")
        self.assertEqual(self.admit(FeedbackKind.SILENCE).reason, "silence")

    def test_late_completion_cannot_clear_successor(self):
        first = self.admit()
        second = self.admit(FeedbackKind.SAFETY, "safety")
        self.assertFalse(self.arbiter.finish(first.lease))
        self.assertTrue(self.arbiter.current(second.lease))

    def test_interruption_and_expiry_invalidate_leases(self):
        lease = self.admit().lease
        self.arbiter.interrupt()
        self.assertFalse(self.arbiter.current(lease))
        self.now += 1
        lease = self.admit(key="new").lease
        self.now = 20
        self.assertFalse(self.arbiter.current(lease))

    def test_deduplication_memory_is_bounded(self):
        for i in range(20):
            self.arbiter.interrupt()
            self.admit(FeedbackKind.ANSWER, str(i))
        self.assertLessEqual(len(self.arbiter._seen), 4)

    def test_invalid_policy_inputs_fail_closed(self):
        with self.assertRaises(ValueError):
            self.admit(deadline=float("nan"))
        with self.assertRaises(ValueError):
            self.admit(kind=999)


class OperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stubborn_async_decider_does_not_hold_turn_open(self):
        release, entered = asyncio.Event(), asyncio.Event()
        async def stubborn(context):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return DecisionDraft(action="answer", utterance_intent="late")
        actor = AsyncMock(return_value={"status": "queued"})
        loop = AgentLoop(user_id="u", session_epoch=0, memory=WorkingMemory(session_id="s"),
                         tools={}, decider=stubborn, actor=actor)
        task = asyncio.create_task(loop.run(AgentTrigger("question"), timeout_s=.02))
        try:
            await entered.wait()
            done, _ = await asyncio.wait({task}, timeout=.2)
            completed_in_budget = task in done
            self.assertEqual(loop.operations.pending, 1)
        finally:
            release.set()
            await task
            await loop.close()
        self.assertTrue(completed_in_budget)
        self.assertEqual(task.result().status, "timeout")
        actor.assert_not_awaited()

    async def test_sync_operation_keeps_capacity_after_caller_cancellation(self):
        pool = OperationPool(1)
        entered, release = threading.Event(), threading.Event()
        def blocking():
            entered.set()
            release.wait(2)
            return 7
        operation = pool.start(blocking)
        try:
            await asyncio.to_thread(entered.wait, 1)
            pool.cancel(operation)
            self.assertEqual(pool.pending, 1)
            with self.assertRaises(OperationCapacityError):
                pool.start(lambda: 8)
        finally:
            release.set()
            self.assertEqual(await operation, 7)
            await pool.close()

    async def test_close_reports_unstopped_work_without_hanging(self):
        pool = OperationPool(1)
        entered, release = asyncio.Event(), asyncio.Event()
        async def stubborn():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        task = pool.start(stubborn)
        await entered.wait()
        try:
            self.assertEqual(await pool.close(.01), 1)
            with self.assertRaises(OperationCapacityError):
                pool.start(stubborn)
        finally:
            release.set()
            await task


class FakeVoice:
    connected = True
    def __init__(self):
        self.calls = []
    async def inject_text(self, text, **kwargs):
        if not kwargs.get("is_current", lambda: True)():
            return False
        self.calls.append((text, kwargs))
        return True


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MemoryStore(":memory:")
        self.store.ensure_user("u")
        self.store.create_session("u", session_id="s")
        self.runtime = MotionRuntime(session_id="s")
        self.voice = FakeVoice()
        self.bridge = SessionAgentBridge(
            user_id="u", session_id="s", memory=self.runtime.memory, store=self.store,
            session_epoch=0, motion_runtime=self.runtime, qwen=self.voice,
        )

    async def asyncTearDown(self):
        await self.bridge.close()
        self.store.close()

    async def drain(self):
        for _ in range(10):
            tasks = tuple(self.bridge._tasks)
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
        self.fail("bridge did not drain")

    async def test_pose_to_agent_to_arbiter_to_mock_voice(self):
        now = time.monotonic() - 1.3
        for i, at, angle in ((1,0,175),(2,.3,145),(3,.7,110),(4,1,130),(5,1.3,170)):
            _, events, _ = self.runtime.ingest(pose(i, now+at, angle))
            self.bridge.on_motion_events(events)
        await self.drain()
        self.assertEqual(self.runtime.fsm.completed_reps, 1)
        self.assertEqual(len(self.voice.calls), 1)
        self.assertIn("第 1 次", self.voice.calls[0][0])
        self.assertTrue(self.voice.calls[0][1]["playback_guard"]())
        self.assertEqual(self.bridge.arbiter.metrics["admitted"], 1)

    async def test_late_storage_result_after_interrupt_does_not_inject(self):
        entered, release = threading.Event(), threading.Event()
        original = self.store.record_feedback
        def slow(*args, **kwargs):
            entered.set()
            release.wait(2)
            return original(*args, **kwargs)
        with patch.object(self.store, "record_feedback", side_effect=slow):
            task = asyncio.create_task(self.bridge._inject("old", event_id=None, facts={}))
            try:
                await asyncio.to_thread(entered.wait, 1)
                self.bridge.on_user_speech_started()
            finally:
                release.set()
            result = await task
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.voice.calls, [])
        await self.drain()

    async def test_safety_pause_and_voice_do_not_wait_for_sqlite(self):
        with patch.object(self.store, "record_feedback", side_effect=AssertionError("disk path")):
            self.bridge.on_user_transcript("我不舒服")
            self.assertTrue(self.runtime.fsm_snapshot().paused)
            await self.drain()
        self.assertEqual(len(self.voice.calls), 1)
        self.assertIsNone(self.voice.calls[0][1]["feedback_id"])

    async def test_generation_callback_is_not_marked_as_played(self):
        row = self.store.record_feedback("u", "s", cue_text="fixture")
        await self.bridge.feedback_state("generation_completed", row["feedback_id"], "response")
        row = self.store.record_feedback(
            "u", "s", feedback_id=row["feedback_id"], cue_text="fixture"
        )
        self.assertIsNone(row["played_at"])
        self.assertEqual(row["playback_state"], "unknown")

    async def test_old_memory_epoch_is_rejected_before_output(self):
        with patch.object(self.store, "get_memory_epoch", return_value=1):
            result = await self.bridge._inject("old history", event_id=None, facts={})
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.voice.calls, [])

    async def test_burst_has_bounded_tasks_and_close_clears_state(self):
        for _ in range(100):
            self.bridge.on_user_transcript("上次训练怎么样")
        self.assertLessEqual(len(self.bridge._tasks), 4)
        await self.bridge.close()
        self.assertFalse(self.bridge._results_by_turn)
        self.assertFalse(self.bridge._trigger_text_by_turn)
