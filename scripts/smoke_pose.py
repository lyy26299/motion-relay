"""Offline real-model smoke using a local still image, never camera/microphone/cloud.

Usage: .venv/bin/python scripts/smoke_pose.py --device cpu
Add --ui to check the desktop at normal/minimum size; --output-dir saves test artifacts.
This checks plumbing and projection availability, NOT human exercise accuracy.
"""

import argparse
import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import av
import numpy as np
from aiortc import VideoStreamTrack
from PIL import Image
from vision_agents.core.utils.video_forwarder import VideoForwarder

from coach.pose_adapter import StructuredPoseProcessor


class StillImageTrack(VideoStreamTrack):
    def __init__(self, image):
        super().__init__()
        self.image = image

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        frame = av.VideoFrame.from_image(self.image)
        frame.pts, frame.time_base = pts, time_base
        return frame


async def main(args):
    if args.samples < 2:
        raise ValueError("At least two samples are required")
    model_path = Path("yolo11n-pose.pt").resolve()
    if not model_path.is_file():
        raise FileNotFoundError(
            "Local yolo11n-pose.pt required; this smoke never downloads weights"
        )
    with Image.open(args.image) as source:
        image = source.convert("RGB")
    image.thumbnail((960, 720))
    snapshots = []
    latest_frame = None
    root = ui = None
    processor = forwarder = receiver = track = None
    artifact_dir = args.output_dir
    if artifact_dir:
        artifact_dir.mkdir(parents=True, exist_ok=True)

    def receive_snapshot(snapshot):
        snapshots.append(snapshot)
        if ui:
            ui.submit_pose(snapshot)

    try:
        if args.ui:
            import tkinter as tk

            from agent_local import FitnessCoachUI

            root = tk.Tk()
            ui = FitnessCoachUI(root)
            ui.set_state("starting", "本地样本测试")
            ui.begin_pose_session("offline-pose-smoke")
            root.update()
        processor = await asyncio.to_thread(
            StructuredPoseProcessor,
            session_id="offline-pose-smoke",
            snapshot_sink=receive_snapshot,
            model_path=str(model_path),
            device=args.device,
        )
        track = StillImageTrack(image)
        forwarder = VideoForwarder(track, max_buffer=1, fps=30)
        await processor.process_video(track, "offline", forwarder)
        if ui:
            ui.set_state("running", "本地样本测试")

        async def receive_frames():
            nonlocal latest_frame
            while True:
                latest_frame = await processor.publish_video_track().recv()
                if ui:
                    ui.submit_frame(latest_frame)

        receiver = asyncio.create_task(receive_frames())
        async with asyncio.timeout(30):
            while len(snapshots) < args.samples:
                if root:
                    root.update()
                    ui.update()
                await asyncio.sleep(1 / 60)
        assert all(s.status != "inference_error" for s in snapshots)
        assert all(b.frame_id > a.frame_id for a, b in zip(snapshots, snapshots[1:]))
        assert latest_frame is not None
        assert (
            len(snapshots[-1].detections) > 0
        ), "No person in sample; choose an image with visible people"
        assert all(len(p.keypoints) == 17 for s in snapshots for p in s.detections)
        json.dumps(snapshots[-1].to_dict(), allow_nan=False)
        if artifact_dir:
            latest_frame.to_image().save(artifact_dir / "pose-annotated.png")
        layouts = []
        if ui:
            from tkinter import font as tkfont

            from PIL import ImageGrab

            for size in ("1180x780", "980x680"):
                root.geometry(size)
                for _ in range(10):
                    root.update()
                    ui.update()
                    await asyncio.sleep(0.02)
                for label in ui.pose_angle_labels:
                    assert label.winfo_x() + label.winfo_width() <= label.master.winfo_width()
                    font = tkfont.Font(root=root, font=label.cget("font"))
                    assert font.measure("180.0°") + 4 <= label.winfo_width()
                label = ui.pose_status_label
                assert label.winfo_y() + label.winfo_height() <= label.master.winfo_height()
                if artifact_dir:
                    bounds = (
                        root.winfo_rootx(),
                        root.winfo_rooty(),
                        root.winfo_rootx() + root.winfo_width(),
                        root.winfo_rooty() + root.winfo_height(),
                    )
                    ImageGrab.grab(bbox=bounds).save(artifact_dir / f"desktop-{size}.png")
                layouts.append(size)
            await processor.stop_processing()
            await asyncio.sleep(0.1)
            with contextlib.suppress(asyncio.CancelledError):
                receiver.cancel()
                await receiver
            # No new snapshots: the UI must invalidate the observation using its input age.
            while not snapshots[-1].is_stale(time.monotonic()):
                root.update()
                ui.update()
                await asyncio.sleep(0.02)
            ui.update()
            assert all(label.cget("text") == "--" for label in ui.pose_angle_labels)
            ui.set_state("idle")
            assert ui._latest_pose is None
        steady = snapshots[1 : args.samples]
        report = {
            "device": args.device,
            "image": str(args.image),
            "samples": args.samples,
            "size": [snapshots[0].width, snapshots[0].height],
            "people": len(snapshots[-1].detections),
            "status": snapshots[-1].status,
            "angles": snapshots[-1].to_dict()["angles"],
            "cold_processing_ms": round(snapshots[0].processing_ms, 1),
            "steady_processing_p95_ms": round(
                float(np.percentile([s.processing_ms for s in steady], 95)), 1
            ),
            "input_to_result_p95_ms": round(
                float(np.percentile([(s.processed_at - s.observed_at) * 1000 for s in steady], 95)),
                1,
            ),
            "dropped_frames": processor.dropped_frames,
            "checked_layouts": layouts,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        if receiver:
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receiver
        if processor:
            await processor.close()
        if forwarder:
            await forwarder.stop()
        if track:
            track.stop()
        if root:
            root.destroy()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--image", type=Path, default=Path("docs/images/demo-preview.png"))
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--ui", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    asyncio.run(main(parser.parse_args()))
