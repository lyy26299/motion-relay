"""Offline lifecycle tests for the SessionController memory integration."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_local_agent import SessionController
from coach.memory.store import MemoryStore
from coach.models import PoseSnapshot, ProjectedAngles


def pose(frame_id: int, observed_at: float, knee: float, *, session_id: str) -> PoseSnapshot:
    return PoseSnapshot(
        session_id=session_id,
        stream_epoch=1,
        frame_id=frame_id,
        observed_at=observed_at,
        processed_at=observed_at,
        media_time_s=observed_at,
        width=640,
        height=480,
        model="test",
        keypoint_threshold=0.5,
        detections=(),
        angles=ProjectedAngles(knee, knee, 160, 160),
        status="observable",
        processing_ms=1.0,
    )


class FakeUI:
    alive = True

    def __init__(self) -> None:
        self._state = "idle"
        self.poses = []
        self.motions = []
        self.memory_statuses = []
        self.logs = []

    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str, detail: str = "") -> None:
        self._state = state

    def submit_frame(self, frame) -> None:
        pass

    def begin_pose_session(self, session_id: str) -> None:
        pass

    def submit_pose(self, snapshot: PoseSnapshot) -> None:
        self.poses.append(snapshot)

    def submit_motion(self, motion) -> None:
        self.motions.append(motion)

    def submit_memory_status(self, status: str, detail: str = "") -> None:
        self.memory_statuses.append((status, detail))

    def append_log(self, text: str, tag: str = "normal") -> None:
        self.logs.append((text, tag))


class SessionControllerMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_pose_sink_updates_ui_runtime_and_durable_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "coach.sqlite3")
            ui = FakeUI()
            controller = SessionController(ui)
            controller.memory_db = db_path
            settings = SimpleNamespace(exercise="深蹲", target_reps=5)
            await asyncio.to_thread(
                controller._prepare_memory, session_id="session-test", settings=settings
            )

            for frame_id, at, knee in (
                (1, 0.0, 175),
                (2, 0.3, 145),
                (3, 0.7, 110),
                (4, 1.0, 130),
                (5, 1.3, 170),
            ):
                controller._submit_pose(pose(frame_id, at, knee, session_id="session-test"))

            self.assertEqual([snapshot.frame_id for snapshot in ui.poses], [1, 2, 3, 4, 5])
            self.assertEqual(ui.motions[-1].completed_reps, 1)
            self.assertEqual(ui.motions[-1].valid_reps, 1)
            self.assertIsNotNone(controller.working_memory)
            self.assertEqual(controller.working_memory.view().reps[0].rep_id, "rep-session-test-1")

            await controller._finish_memory(status="completed")

            with MemoryStore(db_path) as store:
                session = store.get_session("local-user", "session-test")
                self.assertEqual(session["status"], "completed")
                events = store.list_events("local-user", session_id="session-test")
                self.assertEqual(len(events), 6)
                self.assertEqual(events[-1]["kind"], "rep_completed")
                reps = store.list_reps("local-user", session_id="session-test")
                self.assertEqual(len(reps), 1)
                self.assertEqual(reps[0]["rep_id"], "rep-session-test-1")
                summary = store.get_summary("local-user", "summary-session-test")
                self.assertIsNotNone(summary)
                self.assertIn("完成 1 次，有效 1 次", summary["summary"])
                self.assertIn("rep-session-test-1", summary["source_ids"])
            self.assertIn("ready", {status for status, _ in ui.memory_statuses})
            self.assertIn("closed", {status for status, _ in ui.memory_statuses})

    async def test_watchdog_is_independent_of_pose_callbacks_and_is_drained(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "coach.sqlite3")
            ui = FakeUI()
            controller = SessionController(ui)
            controller.memory_db = db_path
            controller.stop_event = asyncio.Event()
            await asyncio.to_thread(
                controller._prepare_memory,
                session_id="session-watchdog",
                settings=SimpleNamespace(exercise="squat", target_reps=5),
            )
            controller._watchdog_task = asyncio.create_task(controller._watchdog_loop())
            old = time.monotonic() - 2.0
            controller._submit_pose(pose(1, old, 175, session_id="session-watchdog"))
            await asyncio.sleep(0.35)

            self.assertTrue(controller.motion_runtime.memory.view().pause_reason == "frame_timeout")
            self.assertEqual(ui.motions[-1].phase, "paused")
            await controller._finish_memory(status="interrupted")

            with MemoryStore(db_path) as store:
                events = store.list_events("local-user", session_id="session-watchdog")
                self.assertTrue(any(row["kind"] == "visibility_lost" for row in events))
                self.assertEqual(
                    store.get_session("local-user", "session-watchdog")["status"],
                    "interrupted",
                )
                summary = store.get_summary("local-user", "summary-session-watchdog")
                self.assertIn("interrupted", summary["summary"])


if __name__ == "__main__":
    unittest.main()
