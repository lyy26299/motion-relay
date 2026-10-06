"""Opt-in P0 product evaluation for Motion Relay V2.2.

These tests encode NEW product/evaluation contracts. Some are expected to fail
against the current V2.2 implementation because they expose known gaps rather
than regressions. They use no network, camera, microphone, paid API, or human
participant.

Run explicitly from the repository root:
    uv run python -m unittest discover -s evals -p 'test_eval_p0_now.py' -v
"""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from coach.arbiter import FeedbackArbiter, FeedbackKind
from coach.memory.retrieval import RetrievalService, ScopeChangedError, _finite_time
from coach.memory.store import MemoryStore
from coach.models import MotionSnapshot, PoseSnapshot, ProjectedAngles
from coach.runtime import MotionRuntime
from coach.session_agent import SessionAgentBridge


def pose(frame: int, at: float, angle: float, *, epoch: int = 1, status: str = "observable"):
    return PoseSnapshot(
        "s", epoch, frame, at, at, at, 640, 480, "fixture", .5, (),
        ProjectedAngles(angle, angle, 160, 160), status, 0,
    )


def add_rep(
    store: MemoryStore,
    user: str,
    session: str,
    index: int,
    *,
    valid: bool = True,
    completed_at: float | None = None,
):
    t = float(completed_at if completed_at is not None else index)
    store.record_rep(
        user,
        session,
        rep_id=f"{session}-rep-{index}",
        rep_index=index,
        valid=valid,
        started_at=t - .5,
        completed_at=t,
        duration_ms=500.0,
        min_knee_angle_deg=95.0,
        max_knee_angle_deg=175.0,
        usable_sample_ratio=1.0,
        reason_codes=(),
        rule_version="squat-v1",
    )


class FakeVoice:
    connected = True

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def inject_text(self, text: str, **kwargs):
        if not kwargs.get("is_current", lambda: True)():
            return False
        self.calls.append((text, kwargs))
        return True


class EvalBridgeCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MemoryStore(":memory:")
        self.store.ensure_user("u")
        self.store.create_session("u", session_id="s", exercise="squat", started_at=100.0)
        self.runtime = MotionRuntime(session_id="s")
        self.voice = FakeVoice()
        self.bridge = SessionAgentBridge(
            user_id="u",
            session_id="s",
            memory=self.runtime.memory,
            store=self.store,
            session_epoch=0,
            motion_runtime=self.runtime,
            qwen=self.voice,
        )

    async def asyncTearDown(self):
        await self.bridge.close()
        self.store.close()

    async def drain(self):
        for _ in range(20):
            tasks = tuple(self.bridge._tasks)
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
        self.fail("bridge did not drain")

    def complete_one_rep(self):
        now = time.monotonic() - 1.3
        emitted = ()
        for i, at, angle in ((1, 0, 175), (2, .3, 145), (3, .7, 110), (4, 1, 130), (5, 1.3, 170)):
            _, events, _ = self.runtime.ingest(pose(i, now + at, angle))
            if events:
                emitted = events
        return emitted

    async def test_C01_idle_required_rep_feedback_is_covered_once(self):
        events = self.complete_one_rep()
        self.assertTrue(any(e.kind == "rep_completed" for e in events))
        self.bridge.on_motion_events(events)
        await self.drain()
        self.assertEqual(len(self.voice.calls), 1)
        self.assertIn("第 1 次", self.voice.calls[0][0])

    async def test_Q01_user_speaking_suppresses_routine_rep_feedback(self):
        self.bridge.on_user_speech_started()
        events = self.complete_one_rep()
        self.bridge.on_motion_events(events)
        await self.drain()
        self.assertEqual(self.voice.calls, [], "routine speech leaked into user-speaking window")

    async def test_Q03_duplicate_motion_event_is_not_spoken_twice(self):
        events = self.complete_one_rep()
        self.bridge.on_motion_events(events)
        await self.drain()
        first_count = len(self.voice.calls)
        self.bridge.on_motion_events(events)
        await self.drain()
        self.assertEqual(first_count, 1)
        self.assertEqual(len(self.voice.calls), 1)

    async def test_I03_local_discomfort_pause_does_not_wait_for_sqlite(self):
        with patch.object(self.store, "record_feedback", side_effect=AssertionError("disk path")):
            self.bridge.on_user_transcript("我现在不舒服")
            self.assertTrue(self.runtime.fsm_snapshot().paused)
            await self.drain()
        self.assertEqual(len(self.voice.calls), 1)

    async def test_I09_negated_discomfort_is_not_current_discomfort_report(self):
        self.bridge.on_user_transcript("我现在没有不舒服")
        await self.drain()
        self.assertFalse(
            self.runtime.fsm_snapshot().paused,
            "negated discomfort was treated as an affirmative current report",
        )

    async def test_I10_historical_discomfort_does_not_pause_current_session(self):
        self.bridge.on_user_transcript("昨天不舒服，现在已经没有了")
        await self.drain()
        self.assertFalse(
            self.runtime.fsm_snapshot().paused,
            "historical/negated report paused the current session",
        )

    async def test_I11_generic_pause_command_changes_runtime_state(self):
        self.bridge.on_user_transcript("暂停一下")
        await self.drain()
        self.assertTrue(
            self.runtime.fsm_snapshot().paused,
            "generic pause intent produced no application pause",
        )

    async def test_I12_resume_command_releases_application_pause(self):
        self.runtime.pause("operator")
        self.bridge.on_user_transcript("继续训练")
        await self.drain()
        self.assertFalse(
            self.runtime.fsm_snapshot().paused,
            "resume intent did not release application pause",
        )

    async def test_C02_T01_current_count_query_uses_live_working_memory(self):
        self.runtime.memory.add_motion(
            MotionSnapshot(
                session_id="s",
                frame_id=99,
                observed_at=time.monotonic(),
                phase="standing",
                completed_reps=5,
                valid_reps=4,
                visible=True,
                paused=False,
                knee_angle_deg=175.0,
                hip_angle_deg=160.0,
                rule_version="squat-v1",
            )
        )
        for i, valid in ((1, True), (2, True), (3, False)):
            add_rep(self.store, "u", "s", i, valid=valid, completed_at=100 + i)

        self.bridge.on_user_transcript("我现在完成多少次了")
        await self.drain()

        self.assertEqual(len(self.voice.calls), 1)
        prompt = self.voice.calls[0][0]
        self.assertIn(
            '"completed_reps":5',
            prompt,
            "current-status query did not use the live authoritative count",
        )
        self.assertIn(
            '"valid_reps":4',
            prompt,
            "current-status query did not use the live authoritative valid count",
        )


class EvalRuntimeAndArbiter(unittest.TestCase):
    def test_F06_deadline_equal_to_now_is_stale(self):
        arbiter = FeedbackArbiter(clock=lambda: 10.0)
        result = arbiter.admit(FeedbackKind.REP, "k", deadline=10.0)
        self.assertIsNone(result.lease)
        self.assertEqual(result.reason, "stale")

    def test_F10_new_camera_epoch_cannot_complete_old_partial_rep(self):
        runtime = MotionRuntime(session_id="s")
        runtime.ingest(pose(1, 0.0, 175))
        runtime.ingest(pose(2, 0.3, 110))
        _, _, reps = runtime.ingest(pose(1, 0.7, 175, epoch=2))
        self.assertEqual(reps, ())
        self.assertEqual(runtime.fsm.completed_reps, 0)

    def test_I11_explicit_pause_is_latched_across_new_frames(self):
        runtime = MotionRuntime(session_id="s")
        runtime.ingest(pose(1, 10.0, 175))
        runtime.pause("operator")
        for i, angle in enumerate((175, 110, 175, 110, 175), 2):
            motion, _, reps = runtime.ingest(pose(i, 10 + i * .3, angle))
            self.assertTrue(motion.paused)
            self.assertEqual(reps, ())
        self.assertEqual(runtime.fsm.completed_reps, 0)

    def test_M12_multiple_people_cannot_create_person_owned_rep(self):
        runtime = MotionRuntime(session_id="s")
        runtime.ingest(pose(1, 0.0, 175))
        _, events, reps = runtime.ingest(pose(2, 0.3, 110, status="multiple_people"))
        self.assertEqual(reps, ())
        self.assertTrue(any(e.kind == "multiple_people" for e in events))


class EvalMemoryIsolation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name) / "eval.sqlite3")
        for user in ("A", "B"):
            self.store.ensure_user(user)
        self.store.create_session("A", session_id="A_PREV", exercise="squat", started_at=100.0, status="completed")
        self.store.create_session("B", session_id="B_PREV", exercise="squat", started_at=100.0, status="completed")
        for i in range(1, 12):
            add_rep(self.store, "A", "A_PREV", i, valid=i <= 9, completed_at=100 + i)
        for i in range(1, 22):
            add_rep(self.store, "B", "B_PREV", i, valid=i <= 19, completed_at=100 + i)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_M01_same_query_text_is_scoped_to_each_user(self):
        a = RetrievalService(self.store, "A").query_training(session_id="A_PREV", metric="valid_reps")
        b = RetrievalService(self.store, "B").query_training(session_id="B_PREV", metric="valid_reps")
        self.assertEqual(a["items"][0]["valid_reps"], 9)
        self.assertEqual(b["items"][0]["valid_reps"], 19)
        self.assertIsNotNone(a["scope_epoch"])
        self.assertFalse(any(item["session_id"] == "B_PREV" for item in a["items"]))

    def test_M03_cross_user_session_id_returns_no_private_rows(self):
        a = RetrievalService(self.store, "A").query_training(session_id="B_PREV")
        self.assertEqual(a["items"], [])

    def test_M08_old_retrieval_service_dies_after_delete_recreate(self):
        service = RetrievalService(self.store, "A")
        self.store.delete_user("A")
        self.store.ensure_user("A")
        self.store.create_session("A", session_id="A_NEW", exercise="squat", started_at=200.0)
        with self.assertRaises(ScopeChangedError):
            service.query_training()


class EvalTemporalRouting(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MemoryStore(":memory:")
        self.store.ensure_user("A")
        self.day5 = 1791129600.0  # 2026-10-05T00:00:00+08:00
        self.day6 = 1791216000.0  # 2026-10-06T00:00:00+08:00

        self.store.create_session(
            "A",
            session_id="A_PREV",
            exercise="squat",
            started_at=self.day5 + 18 * 3600,
            status="completed",
        )
        self.store.create_session(
            "A",
            session_id="A_NOW",
            exercise="squat",
            started_at=self.day6 + 60,
            status="active",
        )
        for i in range(1, 4):
            add_rep(
                self.store,
                "A",
                "A_PREV",
                i,
                valid=i <= 2,
                completed_at=self.day5 + 18 * 3600 + i,
            )
        add_rep(self.store, "A", "A_NOW", 1, valid=True, completed_at=self.day6 + 120)

        self.runtime = MotionRuntime(session_id="A_NOW")
        self.voice = FakeVoice()
        self.bridge = SessionAgentBridge(
            user_id="A",
            session_id="A_NOW",
            memory=self.runtime.memory,
            store=self.store,
            session_epoch=0,
            motion_runtime=self.runtime,
            qwen=self.voice,
        )

    async def asyncTearDown(self):
        await self.bridge.close()
        self.store.close()

    async def drain(self):
        for _ in range(20):
            tasks = tuple(self.bridge._tasks)
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
        self.fail("bridge did not drain")

    async def test_T02_last_session_query_excludes_current_active_session(self):
        self.bridge.on_user_transcript("上一次训练有效几次")
        await self.drain()
        self.assertEqual(len(self.voice.calls), 1)
        prompt = self.voice.calls[0][0]
        self.assertIn("A_PREV", prompt)
        self.assertNotIn(
            "A_NOW",
            prompt,
            "last-session query leaked current active session into model evidence",
        )

    async def test_T03_yesterday_query_excludes_today_session(self):
        self.bridge.on_user_transcript("昨天有效几次")
        await self.drain()
        self.assertEqual(len(self.voice.calls), 1)
        prompt = self.voice.calls[0][0]
        self.assertIn("A_PREV", prompt)
        self.assertNotIn(
            "A_NOW",
            prompt,
            "yesterday route supplied today's active session as evidence",
        )

    def test_T08_yesterday_completed_reps_use_rep_completion_time(self):
        self.store.delete_session("A", "A_PREV")
        self.store.delete_session("A", "A_NOW")
        self.store.create_session(
            "A",
            session_id="CROSS",
            exercise="squat",
            started_at=self.day6 - 60,
            status="completed",
        )
        for i, completed in enumerate(
            (self.day6 - 50, self.day6 - 10, self.day6 + 10, self.day6 + 70),
            1,
        ):
            add_rep(self.store, "A", "CROSS", i, valid=True, completed_at=completed)

        rows = self.store.query_training(
            "A",
            exercise="squat",
            since=self.day5,
            until=self.day6,
        )
        self.assertEqual(
            sum(row["completed_reps"] for row in rows),
            2,
            "yesterday-completed semantic counted post-midnight reps from a session started yesterday",
        )

    def test_T17_naive_iso_datetime_is_rejected_without_explicit_timezone(self):
        with self.assertRaises(ValueError):
            _finite_time("2026-10-05T00:00:00", name="since")


if __name__ == "__main__":
    unittest.main(verbosity=2)
