"""Regression tests also runnable against the unmodified main snapshot."""
import tempfile
import queue
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from coach.memory.writer import LedgerWriter
from coach.models import CoachEvent, PoseSnapshot, ProjectedAngles
from coach.runtime import MotionRuntime


def pose(frame, at, angle, epoch=1):
    return PoseSnapshot("s", epoch, frame, at, at, at, 640, 480, "fixture", .5,
                        (), ProjectedAngles(angle, angle, 160, 160), "observable", 0)


def event(n=1):
    return CoachEvent(f"event-{n}", "s", "phase_changed", float(n), n, "test", None, n,
                      {"nested": {"phase": "standing"}})


class RuntimeBoundaryTests(unittest.TestCase):
    def test_explicit_pause_cannot_resume_on_standing_frames(self):
        runtime = MotionRuntime(session_id="s")
        runtime.ingest(pose(1, 10, 175))
        runtime.pause("operator")
        for i, angle in enumerate((175, 110, 175, 110, 175), 2):
            motion, _, reps = runtime.ingest(pose(i, 10 + i * .3, angle))
            self.assertTrue(motion.paused)
            self.assertEqual(reps, ())
        self.assertEqual(runtime.fsm.completed_reps, 0)

    def test_new_camera_epoch_discards_partial_rep(self):
        runtime = MotionRuntime(session_id="s")
        runtime.ingest(pose(1, 0, 175))
        runtime.ingest(pose(2, .3, 110))
        _, _, reps = runtime.ingest(pose(1, .7, 175, epoch=2))
        self.assertEqual(reps, ())
        self.assertEqual(runtime.fsm.completed_reps, 0)

    def test_old_epoch_cannot_replace_new_pose(self):
        runtime = MotionRuntime(session_id="s")
        runtime.ingest(pose(1, 1, 175, epoch=2))
        runtime.ingest(pose(999, 2, 110, epoch=1))
        self.assertEqual(runtime.memory.view().current_pose.stream_epoch, 2)

    def test_rep_starting_at_zero_keeps_its_duration(self):
        runtime = MotionRuntime(session_id="s")
        runtime.ingest(pose(1, 0, 110))
        _, _, reps = runtime.ingest(pose(2, .5, 175))
        self.assertEqual(reps[0].duration_ms, 500)

    def test_memory_view_cannot_mutate_owned_event_facts(self):
        runtime = MotionRuntime(session_id="s")
        motion, _, _ = runtime.ingest(pose(1, 0, 175))
        source = event()
        runtime.memory.add_motion(motion, (source,))
        source.facts["nested"]["phase"] = "source mutation"
        view = runtime.memory.view()
        with self.assertRaises(TypeError):
            view.events[-1].facts["nested"]["phase"] = "view mutation"
        self.assertEqual(runtime.memory.view().events[-1].facts["nested"]["phase"], "standing")

    def test_dialogue_timestamp_zero_is_not_replaced_with_now(self):
        runtime = MotionRuntime(session_id="s")
        runtime.memory.add_dialogue("user", "test", occurred_at=0.0)
        self.assertEqual(runtime.memory.view().dialogue[0].occurred_at, 0.0)


class WriterBoundaryTests(unittest.TestCase):
    def writer(self, directory, size=256):
        writer = LedgerWriter(path=Path(directory)/"test.sqlite3", user_id="u", session_id="s",
                              queue_size=size)
        writer.start_and_wait()
        return writer

    def test_overflow_cannot_later_be_reported_as_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = self.writer(directory, 1)
            entered, release = threading.Event(), threading.Event()
            original = writer._write_batch
            def slow(store, batch):
                entered.set()
                release.wait(2)
                original(store, batch)
            try:
                with patch.object(writer, "_write_batch", side_effect=slow):
                    self.assertTrue(writer.submit((event(1),)))
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(writer.submit((event(2),)))
                    self.assertFalse(writer.submit((event(3),)))
                    release.set()
                    writer.close()
                self.assertEqual(writer.status, "failed")
                self.assertIsNotNone(writer.error)
            finally:
                release.set()
                writer.close()

    def test_close_stops_admission_before_draining(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = self.writer(directory)
            entered, release, joining = threading.Event(), threading.Event(), threading.Event()
            original_write, original_join = writer._write_batch, writer._thread.join
            def slow(store, batch):
                entered.set()
                release.wait(2)
                original_write(store, batch)
            def join(timeout=None):
                joining.set()
                return original_join(timeout)
            closer = None
            try:
                with patch.object(writer, "_write_batch", side_effect=slow), patch.object(
                    writer._thread, "join", side_effect=join
                ):
                    writer.submit((event(1),))
                    self.assertTrue(entered.wait(1))
                    closer = threading.Thread(target=writer.close)
                    closer.start()
                    self.assertTrue(joining.wait(1))
                    accepted_after_close = writer.submit((event(2),))
                    release.set()
                    closer.join(2)
                self.assertFalse(accepted_after_close)
                self.assertFalse(closer.is_alive())
            finally:
                release.set()
                if closer:
                    closer.join(2)
                writer.close()


class UIQueueTests(unittest.TestCase):
    def test_stop_replaces_a_full_control_queue_without_blocking(self):
        from agent_local import FitnessCoachUI
        ui = FitnessCoachUI.__new__(FitnessCoachUI)
        ui.actions = queue.Queue(maxsize=2)
        ui._queue_action("start", 1)
        ui._queue_action("start", 2)
        ui._queue_action("start", 3)
        self.assertEqual(ui.actions.qsize(), 2)
        ui._queue_action("stop")
        self.assertEqual(ui.actions.get_nowait(), ("stop", None))
        self.assertTrue(ui.actions.empty())
