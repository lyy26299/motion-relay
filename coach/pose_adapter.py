"""One YOLO inference produces an annotated frame and a structured observation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
import numpy as np
from aiortc import VideoStreamTrack
from vision_agents.core.processors.base_processor import VideoProcessorPublisher
from vision_agents.core.utils.video_forwarder import VideoForwarder
from vision_agents.core.utils.video_track import QueuedVideoTrack

from coach.geometry import single_person_angles
from coach.models import Keypoint, PersonPose, PoseSnapshot, PoseStatus, ProjectedAngles

LOGGER = logging.getLogger(__name__)


def _finite(value: Any) -> float | None:
    result = float(value)
    return result if math.isfinite(result) else None


def _confidence(value: Any) -> float | None:
    result = _finite(value)
    return result if result is not None and 0 <= result <= 1 else None


def detections_from_result(result: Any, width: int, height: int) -> tuple[PersonPose, ...]:
    """Copy ALL detections off the model tensors; coordinates and boxes are normalized."""
    if width <= 0 or height <= 0 or tuple(result.orig_shape) != (height, width):
        raise ValueError("Pose result dimensions do not match the input frame")
    boxes = result.boxes
    box_data = boxes.xyxy.cpu().numpy().copy() if boxes is not None else None
    box_conf = boxes.conf.cpu().numpy().copy() if boxes is not None else None
    points = result.keypoints
    point_data = points.data.cpu().numpy().copy() if points is not None else None
    count = (
        len(box_data) if box_data is not None else len(point_data) if point_data is not None else 0
    )
    if point_data is not None and point_data.shape != (count, 17, 3):
        raise ValueError("Expected COCO pose data shaped (persons, 17, 3)")
    people = []
    for index in range(count):
        keypoints = (
            tuple(
                Keypoint(_finite(x / width), _finite(y / height), _confidence(conf))
                for x, y, conf in point_data[index]
            )
            if point_data is not None
            else tuple(Keypoint(None, None, None) for _ in range(17))
        )
        bbox = None
        if box_data is not None:
            coordinates = tuple(float(v) for v in box_data[index] / [width, height, width, height])
            if all(math.isfinite(v) for v in coordinates):
                bbox = coordinates
        people.append(
            PersonPose(
                index,
                keypoints,
                bbox,
                _confidence(box_conf[index]) if box_conf is not None else None,
            )
        )
    return tuple(people)


@dataclass(frozen=True, slots=True)
class _FrameInput:
    frame: av.VideoFrame
    epoch: int
    frame_id: int
    observed_at: float
    pts: int | None
    time_base: Fraction | None


class StructuredPoseProcessor(VideoProcessorPublisher):
    """Single worker + one pending frame; callbacks run on the event loop, not the GPU thread."""

    name = "structured_yolo_pose"

    def __init__(
        self,
        *,
        session_id: str,
        snapshot_sink: Callable[[PoseSnapshot], None],
        model_path: str = "yolo11n-pose.pt",
        device: str = "cpu",
        fps: int = 10,
        imgsz: int = 512,
        detection_threshold: float = 0.5,
        keypoint_threshold: float = 0.5,
        model: Any = None,
    ) -> None:
        super().__init__()
        if fps <= 0 or imgsz <= 0:
            raise ValueError("fps and imgsz must be positive")
        if not 0 < detection_threshold <= 1 or not 0 < keypoint_threshold <= 1:
            raise ValueError("Confidence thresholds must be in (0, 1]")
        if model is None:
            from ultralytics import YOLO

            model = YOLO(model_path)
            model.to(device)
        self._model = model
        self._model_name = Path(model_path).name
        self.session_id = session_id
        self.snapshot_sink = snapshot_sink
        self.device = device
        self.fps = fps
        self.imgsz = imgsz
        self.detection_threshold = detection_threshold
        self.keypoint_threshold = keypoint_threshold
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="coach-pose")
        self._pending: asyncio.Queue[_FrameInput] = asyncio.Queue(maxsize=1)
        self._worker: asyncio.Task[None] | None = None
        self._forwarder: VideoForwarder | None = None
        self._handler: Callable[[av.VideoFrame], None] | None = None
        self._video_track = QueuedVideoTrack(fps=fps, max_queue_size=1)
        self._lifecycle_lock = asyncio.Lock()
        self._epoch = 0
        self._frame_id = 0
        self._active = False
        self._closed = False
        self.dropped_frames = 0
        self.discarded_results = 0

    async def process_video(
        self,
        track: VideoStreamTrack,
        participant_id: str | None,
        shared_forwarder: VideoForwarder | None = None,
    ) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("Pose processor is closed")
            await self._detach()
            self._active = True
            epoch = self._epoch
            self._forwarder = shared_forwarder or VideoForwarder(
                track, max_buffer=1, fps=self.fps, name="coach-pose-input"
            )
            self._handler = lambda frame: self._enqueue(frame, epoch)
            forwarder_fps = self._forwarder.fps
            handler_fps = min(self.fps, forwarder_fps) if forwarder_fps else self.fps
            self._forwarder.add_frame_handler(self._handler, fps=handler_fps, name=self.name)
            if self._worker is None:
                self._worker = asyncio.create_task(self._consume(), name="coach-pose-worker")

    def _enqueue(self, frame: av.VideoFrame, epoch: int) -> None:
        if not self._active or self._closed or epoch != self._epoch:
            return
        self._frame_id += 1
        item = _FrameInput(
            frame, epoch, self._frame_id, time.monotonic(), frame.pts, frame.time_base
        )
        if self._pending.full():
            self._pending.get_nowait()
            self.dropped_frames += 1
        self._pending.put_nowait(item)

    async def _consume(self) -> None:
        while True:
            item = await self._pending.get()
            started_at = time.monotonic()
            try:
                frame, snapshot = await asyncio.get_running_loop().run_in_executor(
                    self._executor, self._infer, item
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Pose inference failed")
                snapshot = self._snapshot(
                    item,
                    (),
                    ProjectedAngles(),
                    "inference_error",
                    (time.monotonic() - started_at) * 1000,
                )
                # Do not publish the shared input object: output tracks rewrite its PTS.
                frame = av.VideoFrame.from_ndarray(
                    item.frame.to_ndarray(format="bgr24"), format="bgr24"
                )
            if not self._active or self._closed or item.epoch != self._epoch:
                self.discarded_results += 1
                continue
            frame.pts = item.pts
            if item.time_base is not None:
                frame.time_base = item.time_base
            await self._video_track.add_frame(frame)
            try:
                self.snapshot_sink(snapshot)
            except Exception:
                LOGGER.exception("Pose snapshot consumer failed")

    def _infer(self, item: _FrameInput) -> tuple[av.VideoFrame, PoseSnapshot]:
        started_at = time.monotonic()
        bgr = item.frame.to_ndarray(format="bgr24")
        results = self._model(
            bgr, verbose=False, conf=self.detection_threshold, device=self.device, imgsz=self.imgsz
        )
        if len(results) != 1:
            raise ValueError("Expected one Results object per input image")
        result = results[0]
        detections = detections_from_result(result, item.frame.width, item.frame.height)
        status, angles = single_person_angles(
            detections,
            width=item.frame.width,
            height=item.frame.height,
            min_confidence=self.keypoint_threshold,
        )
        annotated = result.plot(pil=False, labels=False, boxes=False, conf=False)
        frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(annotated), format="bgr24")
        snapshot = self._snapshot(
            item, detections, angles, status, (time.monotonic() - started_at) * 1000
        )
        return frame, snapshot

    def _snapshot(
        self,
        item: _FrameInput,
        detections: tuple[PersonPose, ...],
        angles: ProjectedAngles,
        status: PoseStatus,
        processing_ms: float,
    ) -> PoseSnapshot:
        media_time = (
            float(item.pts * item.time_base)
            if item.pts is not None and item.time_base is not None
            else None
        )
        return PoseSnapshot(
            session_id=self.session_id,
            stream_epoch=item.epoch,
            frame_id=item.frame_id,
            observed_at=item.observed_at,
            processed_at=time.monotonic(),
            media_time_s=media_time,
            width=item.frame.width,
            height=item.frame.height,
            model=self._model_name,
            keypoint_threshold=self.keypoint_threshold,
            detections=detections,
            angles=angles,
            status=status,
            processing_ms=processing_ms,
        )

    def publish_video_track(self) -> VideoStreamTrack:
        return self._video_track

    async def _detach(self) -> None:
        self._active = False
        self._epoch += 1
        while not self._pending.empty():
            self._pending.get_nowait()
        if self._forwarder is not None and self._handler is not None:
            await self._forwarder.remove_frame_handler(self._handler)
        self._forwarder = None
        self._handler = None

    async def stop_processing(self) -> None:
        async with self._lifecycle_lock:
            await self._detach()

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            await self._detach()
            if self._worker is not None:
                self._worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._worker
            self._video_track.stop()
            # Cancelling an asyncio waiter cannot kill native inference. Join off the UI loop.
            await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)
