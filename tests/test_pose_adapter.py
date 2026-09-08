"""Real Ultralytics Results + fake inference, exercised through the async video pipeline."""

import asyncio
import inspect
import json
import threading
import time
import unittest
from dataclasses import FrozenInstanceError
from fractions import Fraction
from unittest.mock import Mock

import av
import numpy as np
import torch
from ultralytics.engine.results import Results

from coach.pose_adapter import StructuredPoseProcessor, detections_from_result


def pose_result(image, count=1):
    height, width = image.shape[:2]
    points = np.zeros((count, 17, 3), dtype=np.float32)
    for index in range(count):
        for indices, x in (((5, 11, 13, 15), 0.3), ((6, 12, 14, 16), 0.7)):
            for point, y in zip(indices, (0.2, 0.4, 0.6, 0.8)):
                points[index, point] = (x * width, y * height, 0.9)
    boxes = np.array([[0, 0, width, height, 0.9, 0]] * count, dtype=np.float32).reshape(count, 6)
    return Results(
        image.copy(),
        path="synthetic",
        names={0: "person"},
        boxes=torch.from_numpy(boxes),
        keypoints=torch.from_numpy(points),
    )


def video_frame(pts=0):
    rgb = np.zeros((240, 320, 3), dtype=np.uint8)
    rgb[:, :, 0] = 200
    frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
    frame.pts, frame.time_base = pts, Fraction(1, 90000)
    return frame


class FakeModel:
    def __init__(self, count=1):
        self.count = count
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.fail = False

    def __call__(self, image, **kwargs):
        self.calls.append((image.copy(), kwargs, threading.get_ident()))
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError("Test did not release inference")
        if self.fail:
            raise RuntimeError("synthetic inference failure")
        return [pose_result(image, self.count)]


class SharedForwarder:
    fps = 30

    def __init__(self):
        self.handlers = [lambda frame: None]

    def add_frame_handler(self, handler, **kwargs):
        self.handlers.append(handler)

    async def remove_frame_handler(self, handler):
        self.handlers.remove(handler)

    def emit(self, frame):
        for handler in self.handlers:
            handler(frame)


async def until(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(0.005)


class ResultConversionTests(unittest.TestCase):
    def test_extracts_all_people_as_immutable_cpu_values(self):
        result = pose_result(np.zeros((240, 320, 3), dtype=np.uint8), count=2)
        people = detections_from_result(result, 320, 240)
        self.assertEqual([p.detection_index for p in people], [0, 1])
        self.assertTrue(all(len(p.keypoints) == 17 for p in people))
        self.assertEqual(people[0].bbox_xyxy, (0, 0, 1, 1))
        self.assertAlmostEqual(people[1].keypoints[11].x, 0.3)
        result.keypoints.data[0, 11, 0] = 0
        self.assertAlmostEqual(people[0].keypoints[11].x, 0.3)
        with self.assertRaises(FrozenInstanceError):
            people[0].detection_index = 99

    def test_rejects_wrong_shape_and_coordinate_space(self):
        result = pose_result(np.zeros((240, 320, 3), dtype=np.uint8))
        with self.assertRaises(ValueError):
            detections_from_result(result, 320, 480)
        result.keypoints.data = result.keypoints.data[:, :16]
        with self.assertRaises(ValueError):
            detections_from_result(result, 320, 240)

    def test_missing_pose_keeps_person_detection(self):
        result = pose_result(np.zeros((240, 320, 3), dtype=np.uint8))
        result.keypoints = None
        people = detections_from_result(result, 320, 240)
        self.assertEqual(len(people), 1)
        self.assertEqual(len(people[0].keypoints), 17)
        self.assertTrue(all(p.confidence is None for p in people[0].keypoints))

    def test_nonfinite_values_are_not_serialized_as_nan(self):
        result = pose_result(np.zeros((240, 320, 3), dtype=np.uint8))
        result.keypoints.data[0, 11] = torch.tensor([float("nan"), float("inf"), float("nan")])
        point = detections_from_result(result, 320, 240)[0].keypoints[11]
        self.assertEqual((point.x, point.y, point.confidence), (None, None, None))


class PosePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.snapshots = []
        self.model = FakeModel()
        self.processor = StructuredPoseProcessor(
            session_id="test-session", snapshot_sink=self.snapshots.append, model=self.model
        )
        self.forwarder = SharedForwarder()
        await self.processor.process_video(Mock(), "test", self.forwarder)

    async def asyncTearDown(self):
        self.model.release.set()
        await self.processor.close()
        await self.processor.close()
        self.assertTrue(self.processor.publish_video_track().stopped)
        self.assertTrue(self.processor._worker.done())
        self.assertEqual(len(self.forwarder.handlers), 1)

    async def test_one_inference_publishes_frame_and_snapshot_with_input_metadata(self):
        frame = video_frame(90000)
        before = time.monotonic()
        self.forwarder.emit(frame)
        # The shared output path may rewrite PTS after our synchronous enqueue.
        frame.pts = 180000
        await until(lambda: self.snapshots)
        snapshot = self.snapshots[0]
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(snapshot.session_id, "test-session")
        self.assertEqual(snapshot.frame_id, 1)
        self.assertEqual(snapshot.media_time_s, 1.0)
        self.assertGreaterEqual(snapshot.observed_at, before)
        self.assertGreaterEqual(snapshot.processed_at, snapshot.observed_at)
        self.assertGreater(snapshot.processing_ms, 0)
        self.assertEqual(snapshot.status, "observable")
        self.assertEqual(snapshot.angles.values(), (180, 180, 180, 180))
        self.assertFalse(snapshot.is_stale(snapshot.observed_at + 1))
        self.assertTrue(snapshot.is_stale(snapshot.observed_at + 2))
        json.dumps(snapshot.to_dict(), allow_nan=False)
        self.assertEqual(snapshot.to_dict()["schema_version"], "coach.pose.v1")
        self.assertNotEqual(self.model.calls[0][2], threading.get_ident())
        annotated = await self.processor.publish_video_track().recv()
        self.assertEqual((annotated.width, annotated.height), (320, 240))
        self.assertGreater(
            np.count_nonzero(
                annotated.to_ndarray(format="rgb24") != frame.to_ndarray(format="rgb24")
            ),
            100,
        )

    async def test_bgr_model_and_rgb_display_do_not_swap_channels(self):
        self.model.count = 0
        self.forwarder.emit(video_frame())
        await until(lambda: self.snapshots)
        np.testing.assert_array_equal(self.model.calls[0][0][0, 0], [0, 0, 200])
        frame = await self.processor.publish_video_track().recv()
        np.testing.assert_array_equal(frame.to_ndarray(format="rgb24")[0, 0], [200, 0, 0])
        self.assertEqual(self.snapshots[0].status, "no_person")
        self.assertEqual(self.model.calls[0][1]["imgsz"], 512)

    async def test_multi_person_frame_has_all_points_but_no_selected_angles(self):
        self.model.count = 2
        self.forwarder.emit(video_frame())
        await until(lambda: self.snapshots)
        self.assertEqual(len(self.snapshots[0].detections), 2)
        self.assertEqual(self.snapshots[0].status, "multiple_people")
        self.assertEqual(self.snapshots[0].angles.values(), (None,) * 4)

    async def test_slow_inference_drops_backlog_without_blocking_dispatch(self):
        self.model.release.clear()
        self.forwarder.emit(video_frame())
        await until(self.model.started.is_set)
        self.assertFalse(inspect.iscoroutinefunction(self.processor._handler))
        for index in range(2, 22):
            self.forwarder.emit(video_frame(index))
        self.assertEqual(self.processor._pending.qsize(), 1)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.processor.dropped_frames, 19)
        self.model.release.set()
        await until(lambda: len(self.snapshots) == 2)
        self.assertEqual([s.frame_id for s in self.snapshots], [1, 21])
        self.assertEqual(len(self.model.calls), 2)

    async def test_stop_and_restart_reject_inflight_and_old_handler(self):
        old_handler = self.processor._handler
        self.model.release.clear()
        self.forwarder.emit(video_frame())
        await until(self.model.started.is_set)
        await self.processor.stop_processing()
        self.assertEqual(len(self.forwarder.handlers), 1)
        await self.processor.process_video(Mock(), "test", self.forwarder)
        old_handler(video_frame())
        self.forwarder.emit(video_frame(90000))
        self.model.release.set()
        await until(lambda: self.snapshots)
        self.assertEqual(len(self.snapshots), 1)
        self.assertEqual(self.snapshots[0].frame_id, 2)
        self.assertEqual(self.snapshots[0].media_time_s, 1)
        self.assertEqual(self.processor.discarded_results, 1)

    async def test_failure_emits_unknown_and_recovers(self):
        self.model.fail = True
        with self.assertLogs("coach.pose_adapter", level="ERROR"):
            self.forwarder.emit(video_frame())
            await until(lambda: self.snapshots)
        self.assertEqual(self.snapshots[0].status, "inference_error")
        self.assertEqual(self.snapshots[0].angles.values(), (None,) * 4)
        frame = await self.processor.publish_video_track().recv()
        np.testing.assert_array_equal(frame.to_ndarray(format="rgb24")[0, 0], [200, 0, 0])
        self.model.fail = False
        self.forwarder.emit(video_frame())
        await until(lambda: len(self.snapshots) == 2)
        self.assertEqual(self.snapshots[1].status, "observable")

    async def test_failing_sink_does_not_kill_processing(self):
        self.processor.snapshot_sink = Mock(side_effect=RuntimeError("consumer failed"))
        with self.assertLogs("coach.pose_adapter", level="ERROR"):
            self.forwarder.emit(video_frame())
            await until(lambda: self.processor.snapshot_sink.called)
        self.processor.snapshot_sink = self.snapshots.append
        self.forwarder.emit(video_frame())
        await until(lambda: self.snapshots)
        self.assertFalse(self.processor._worker.done())

    async def test_close_waits_off_loop_and_drops_late_native_result(self):
        self.model.release.clear()
        self.forwarder.emit(video_frame())
        await until(self.model.started.is_set)
        closing = asyncio.create_task(self.processor.close())
        await asyncio.sleep(0.02)
        self.assertFalse(closing.done())
        self.model.release.set()
        await asyncio.wait_for(closing, 2)
        self.assertEqual(self.snapshots, [])
        with self.assertRaises(RuntimeError):
            await self.processor.process_video(Mock(), "test", self.forwarder)


if __name__ == "__main__":
    unittest.main()
