"""Exercise the real UI pose methods without requiring a display server."""

import queue
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from agent_local import FitnessCoachUI
from coach.models import PoseSnapshot, ProjectedAngles


def snapshot(**changes):
    value = PoseSnapshot(
        session_id="current",
        stream_epoch=1,
        frame_id=1,
        observed_at=100,
        processed_at=100.1,
        media_time_s=None,
        width=640,
        height=480,
        model="test",
        keypoint_threshold=0.5,
        detections=(),
        angles=ProjectedAngles(180, None, 90, None),
        status="partial",
        processing_ms=100,
    )
    return replace(value, **changes)


class PoseUiTests(unittest.TestCase):
    def setUp(self):
        self.ui = FitnessCoachUI.__new__(FitnessCoachUI)
        self.ui._state = "running"
        self.ui._pose_session_id = None
        self.ui._latest_pose = None
        self.ui.poses = queue.Queue(maxsize=1)
        self.ui.pose_angle_labels = [Mock() for _ in range(4)]
        self.ui.pose_status_label = Mock()
        self.clock = patch("agent_local.time.monotonic", return_value=100.2)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.ui.begin_pose_session("current")

    def values(self):
        return [label.configure.call_args.kwargs["text"] for label in self.ui.pose_angle_labels]

    def test_displays_available_sides_and_marks_missing_values(self):
        self.ui.submit_pose(snapshot())
        self.ui._draw_pose()
        self.assertEqual(self.values(), ["180.0°", "--", "90.0°", "--"])

    def test_ttl_clears_angles_even_when_no_new_frames_arrive(self):
        self.ui.submit_pose(snapshot())
        self.ui._draw_pose()
        with patch("agent_local.time.monotonic", return_value=102):
            self.ui._draw_pose()
        self.assertEqual(self.values(), ["--"] * 4)
        self.assertIn("过期", self.ui.pose_status_label.configure.call_args.kwargs["text"])

    def test_queue_is_bounded_and_new_session_rejects_old_data(self):
        for frame_id in range(1, 100):
            self.ui.submit_pose(snapshot(frame_id=frame_id))
        self.assertEqual(self.ui.poses.qsize(), 1)
        self.ui._draw_pose()
        self.assertEqual(self.ui._latest_pose.frame_id, 99)
        self.ui.begin_pose_session("next")
        self.ui.submit_pose(snapshot(frame_id=100))
        self.ui._draw_pose()
        self.assertIsNone(self.ui._latest_pose)
        self.assertEqual(self.values(), ["--"] * 4)

    def test_stopped_ui_rejects_late_callbacks(self):
        for state in ("stopping", "idle", "error"):
            self.ui._state = state
            self.ui.submit_pose(snapshot())
            self.assertTrue(self.ui.poses.empty())

    def test_out_of_order_snapshot_does_not_replace_displayed_value(self):
        self.ui.submit_pose(snapshot(frame_id=2))
        self.ui._draw_pose()
        self.ui.submit_pose(snapshot(frame_id=1))
        self.ui._draw_pose()
        self.assertEqual(self.ui._latest_pose.frame_id, 2)
        self.ui.submit_pose(snapshot(stream_epoch=2, frame_id=3))
        self.ui._draw_pose()
        self.ui.submit_pose(snapshot(stream_epoch=1, frame_id=50))
        self.ui._draw_pose()
        self.assertEqual(self.ui._latest_pose.stream_epoch, 2)

    def test_out_of_order_snapshot_does_not_evict_pending_newer_value(self):
        self.ui.submit_pose(snapshot(frame_id=2))
        self.ui.submit_pose(snapshot(frame_id=1))
        self.ui._draw_pose()
        self.assertEqual(self.ui._latest_pose.frame_id, 2)


if __name__ == "__main__":
    unittest.main()
