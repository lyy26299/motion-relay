"""Replay tests for deterministic squat facts and bounded working memory."""

import unittest
from dataclasses import replace

from coach.exercises.squat import SquatFSM, SquatThresholds
from coach.models import PoseSnapshot, ProjectedAngles
from coach.runtime import MotionRuntime
from coach.working_memory import WorkingMemory


def pose(
    frame_id: int, at: float, knee: float, *, status: str = "observable", session_id: str = "s"
):
    return PoseSnapshot(
        session_id=session_id,
        stream_epoch=1,
        frame_id=frame_id,
        observed_at=at,
        processed_at=at,
        media_time_s=at,
        width=640,
        height=480,
        model="test",
        keypoint_threshold=0.5,
        detections=(),
        angles=ProjectedAngles(knee, knee, 160, 160),
        status=status,
        processing_ms=1,
    )


class SquatReplayTests(unittest.TestCase):
    def test_complete_rep_is_counted_once_and_has_evidence(self):
        fsm = SquatFSM(session_id="s")
        records = []
        events = []
        sequence = ((1, 0.0, 175), (2, 0.3, 145), (3, 0.7, 110), (4, 1.0, 130), (5, 1.3, 170))
        for frame_id, at, angle in sequence:
            _, frame_events, frame_records = fsm.update(pose(frame_id, at, angle))
            events.extend(frame_events)
            records.extend(frame_records)
        self.assertEqual(fsm.completed_reps, 1)
        self.assertEqual(fsm.valid_reps, 1)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0].valid)
        self.assertEqual(records[0].evidence_start_frame, 2)
        self.assertEqual(records[0].evidence_end_frame, 5)
        self.assertEqual(len({event.event_id for event in events}), len(events))
        self.assertEqual(records[0].rep_id, "rep-s-1")

    def test_replaying_same_sequence_produces_same_rep_id(self):
        sequence = ((1, 0.0, 175), (2, 0.3, 145), (3, 0.7, 110), (4, 1.0, 130), (5, 1.3, 170))
        ids = []
        for _ in range(2):
            fsm = SquatFSM(session_id="replay")
            records = []
            for frame_id, at, angle in sequence:
                records.extend(fsm.update(pose(frame_id, at, angle, session_id="replay"))[2])
            ids.append(records[0].rep_id)
        self.assertEqual(ids, ["rep-replay-1", "rep-replay-1"])

    def test_shallow_rep_is_counted_but_invalid(self):
        fsm = SquatFSM(session_id="s")
        records = []
        for frame_id, at, angle in ((1, 0.0, 175), (2, 0.3, 145), (3, 0.7, 130), (4, 1.0, 170)):
            records.extend(fsm.update(pose(frame_id, at, angle))[2])
        self.assertEqual(fsm.completed_reps, 1)
        self.assertEqual(fsm.valid_reps, 0)
        self.assertFalse(records[0].valid)
        self.assertIn("insufficient_depth", records[0].reason_codes)

    def test_multiple_people_and_gap_abort_in_progress_rep(self):
        fsm = SquatFSM(session_id="s", thresholds=SquatThresholds(max_gap_ms=500))
        fsm.update(pose(1, 0.0, 175))
        fsm.update(pose(2, 0.3, 110))
        _, events, records = fsm.update(pose(3, 1.0, 170, status="multiple_people"))
        self.assertEqual(records, ())
        self.assertEqual(fsm.completed_reps, 0)
        self.assertEqual(fsm.phase, "paused")
        self.assertTrue(any(event.kind == "multiple_people" for event in events))
        _, events, _ = fsm.update(pose(4, 1.1, 175))
        self.assertEqual(fsm.phase, "standing")
        self.assertTrue(any(event.kind == "phase_changed" for event in events))

    def test_runtime_watchdog_latches_pause_and_duplicate_frames_are_ignored(self):
        runtime = MotionRuntime(session_id="s")
        snapshot = pose(1, 10.0, 175)
        runtime.ingest(snapshot)
        runtime.ingest(snapshot)
        self.assertEqual(runtime.memory.view().state_version, 2)
        events = runtime.watchdog(now=11.6, timeout_s=1.5)
        self.assertEqual(len(events), 1)
        self.assertEqual(runtime.memory.view().pause_reason, "frame_timeout")
        self.assertEqual(runtime.watchdog(now=12.0, timeout_s=1.5), ())
        runtime.resume()
        self.assertIsNone(runtime.memory.view().pause_reason)

    def test_watchdog_requires_standing_recovery_before_counting(self):
        runtime = MotionRuntime(session_id="s")
        runtime.ingest(pose(1, 10.0, 175))
        runtime.ingest(pose(2, 10.3, 110))
        runtime.watchdog(now=12.0, timeout_s=1.5)
        runtime.resume()
        motion, _, records = runtime.ingest(pose(3, 12.1, 110))
        self.assertEqual(records, ())
        self.assertEqual(motion.completed_reps, 0)
        self.assertEqual(motion.phase, "paused")
        motion, _, records = runtime.ingest(pose(4, 12.2, 175))
        self.assertEqual(records, ())
        self.assertEqual(motion.phase, "standing")
        runtime.ingest(pose(5, 12.5, 110))
        _, _, records = runtime.ingest(pose(6, 13.5, 175))
        self.assertEqual(len(records), 1)

    def test_application_pause_latches_reason_and_discards_partial_rep(self):
        runtime = MotionRuntime(session_id="s")
        runtime.ingest(pose(1, 10.0, 175))
        runtime.ingest(pose(2, 10.3, 110))

        paused = runtime.pause("user_reported_discomfort")

        self.assertTrue(paused.paused)
        self.assertEqual(paused.phase, "paused")
        self.assertEqual(runtime.memory.view().pause_reason, "user_reported_discomfort")
        runtime.resume()
        _, _, records = runtime.ingest(pose(3, 10.6, 175))
        self.assertEqual(records, ())
        self.assertEqual(runtime.fsm.completed_reps, 0)


class WorkingMemoryTests(unittest.TestCase):
    def test_pose_and_event_ttl_are_bounded_but_rep_state_is_retained(self):
        memory = WorkingMemory(
            session_id="s", pose_window_s=2, pose_max=3, event_window_s=2, event_max=2
        )
        runtime = MotionRuntime(session_id="s", memory=memory)
        for frame_id, at, angle in (
            (1, 0, 175),
            (2, 0.3, 145),
            (3, 0.7, 110),
            (4, 1, 130),
            (5, 1.3, 170),
        ):
            runtime.ingest(pose(frame_id, at, angle))
        runtime.ingest(pose(6, 4.0, 175))
        view = memory.view()
        self.assertLessEqual(len(view.poses), 3)
        self.assertLessEqual(len(view.events), 2)
        self.assertEqual(len(view.reps), 1)
        self.assertEqual(view.current_motion.completed_reps, 1)

    def test_dialogue_task_and_scope(self):
        memory = WorkingMemory(session_id="s", dialogue_max=2)
        memory.add_dialogue("user", "上次膝盖角度怎样？")
        memory.add_dialogue("coach", "我会先查询已保存事实。")
        memory.add_dialogue("system", "第三条会淘汰最旧对话。")
        generation = memory.begin_task("q1", kind="history_query", deadline=10.0)
        self.assertFalse(memory.task_is_current("q1", generation - 1))
        self.assertTrue(memory.task_is_current("q1", generation))
        self.assertEqual(len(memory.view().dialogue), 2)
        memory.cancel_task()
        self.assertFalse(memory.task_is_current("q1", generation))
        with self.assertRaises(ValueError):
            memory.add_pose(replace(pose(1, 0, 175), session_id="other"))


if __name__ == "__main__":
    unittest.main()
