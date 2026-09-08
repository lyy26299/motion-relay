"""Run real headless Chrome against BrowserEdge using fake media, never real devices.

Usage: .venv/bin/python scripts/smoke_browser_aec.py
Set CHROME_BINARY if Chrome is not installed at the macOS default location.
This verifies signaling and browser AEC settings, NOT acoustic suppression quality.
"""

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiohttp
import numpy as np
from getstream.video.rtc import PcmData
from vision_agents.core import Agent, User
from vision_agents.core.edge.events import AudioReceivedEvent

from coach.browser_edge import BrowserEdge
from coach.qwen_duplex import DuplexQwenRealtime


class OfflineClient:
    def __init__(self):
        self.audio_frames = 0
        self.video_frames = 0

    async def send_audio(self, pcm):
        self.audio_frames += 1

    async def send_frame(self, jpg_bytes):
        self.video_frames += 1

    async def close(self):
        pass


class OfflineQwen(DuplexQwenRealtime):
    async def connect(self):
        self._real_client = OfflineClient()
        self._on_connected()


async def main(with_pose=False, pose_device="cpu"):
    displayed_frames = 0

    def display(frame):
        nonlocal displayed_frames
        displayed_frames += 1

    edge = BrowserEdge(frame_sink=display)
    uploads = []
    poses = []
    processors = []
    if with_pose:
        from coach.pose_adapter import StructuredPoseProcessor

        processors.append(
            await asyncio.to_thread(
                StructuredPoseProcessor,
                session_id="chrome-smoke",
                snapshot_sink=poses.append,
                device=pose_device,
            )
        )

    @edge.events.subscribe
    async def receive(event: AudioReceivedEvent):
        uploads.append(event.pcm_data.duration)

    llm = OfflineQwen(api_key="offline-test", include_video=True)
    agent = Agent(
        edge=edge,
        llm=llm,
        agent_user=User(name="Smoke"),
        instructions="Offline smoke",
        processors=processors,
    )
    call = await agent.create_call("browser", "chrome-smoke")
    session_context = contextlib.AsyncExitStack()
    await session_context.enter_async_context(agent.join(call, participant_wait_timeout=0))
    chrome = os.getenv(
        "CHROME_BINARY", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    process = None
    producer = None
    with tempfile.TemporaryDirectory(prefix="coach-chrome-") as profile:
        try:
            process = await asyncio.create_subprocess_exec(
                chrome,
                "--headless=new",
                "--no-first-run",
                "--no-default-browser-check",
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile}",
                "about:blank",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            async with asyncio.timeout(20):
                while True:
                    line = await process.stderr.readline()
                    if not line:
                        raise RuntimeError("Chrome exited before opening DevTools")
                    if b"DevTools listening on " in line:
                        websocket_url = line.decode().strip().split(" on ", 1)[1]
                        break
            async with (
                aiohttp.ClientSession() as http,
                http.ws_connect(websocket_url) as ws,
            ):
                serial = 0

                async def command(method, params=None, session=None):
                    nonlocal serial
                    serial += 1
                    message = {"id": serial, "method": method, "params": params or {}}
                    if session:
                        message["sessionId"] = session
                    await ws.send_json(message)
                    async with asyncio.timeout(30):
                        while True:
                            response = await ws.receive_json()
                            if response.get("id") == serial:
                                if "error" in response:
                                    raise RuntimeError(response["error"])
                                return response["result"]

                target = await command("Target.createTarget", {"url": edge.url})
                attached = await command(
                    "Target.attachToTarget",
                    {
                        "targetId": target["targetId"],
                        "flatten": True,
                    },
                )
                session = attached["sessionId"]

                async def evaluate(expression):
                    result = await command(
                        "Runtime.evaluate",
                        {
                            "expression": expression,
                            "awaitPromise": True,
                            "returnByValue": True,
                        },
                        session,
                    )
                    if "exceptionDetails" in result:
                        raise RuntimeError(result["exceptionDetails"])
                    return result["result"].get("value")

                for _ in range(100):
                    if await evaluate("Boolean(document.querySelector('#start'))"):
                        break
                    await asyncio.sleep(0.05)
                await evaluate("document.querySelector('#start').click()")
                await asyncio.wait_for(edge.connected.wait(), 20)

                async def produce():
                    samples = (np.sin(np.arange(480) * 2 * np.pi * 440 / 24000) * 8000).astype(
                        np.int16
                    )
                    while True:
                        llm._emit_audio_output_event(PcmData(24000, "s16", samples))
                        await asyncio.sleep(0.02)

                producer = asyncio.create_task(produce())
                await asyncio.sleep(2)
                if with_pose:
                    async with asyncio.timeout(20):
                        while not poses or llm._client.video_frames < 2:
                            await asyncio.sleep(0.05)
                report = await evaluate("""(async () => {
                        const stats = [...(await pc.getStats()).values()];
                        return {aec:stream.getAudioTracks()[0].getSettings().echoCancellation,
                            connected:pc.connectionState, speakerPlaying:!speaker.paused,
                            previewAudioTracks:preview.srcObject.getAudioTracks().length,
                            uploaded:stats.filter(s=>s.type==='outbound-rtp').map(s=>({kind:s.kind,bytes:s.bytesSent})),
                            downloaded:stats.filter(s=>s.type==='inbound-rtp').map(s=>({kind:s.kind,bytes:s.bytesReceived}))};
                    })()""")
                assert report["aec"] is True, report
                assert report["speakerPlaying"], report
                assert report["previewAudioTracks"] == 0, report
                assert any(
                    s["kind"] == "audio" and s["bytes"] > 0 for s in report["downloaded"]
                ), report
                assert {s["kind"] for s in report["uploaded"] if s["bytes"] > 0} == {
                    "audio",
                    "video",
                }, report
                assert len(uploads) > 10, "No uplink PCM received"
                report["uplink_pcm_frames"] = len(uploads)
                assert llm._client.audio_frames > 0, "Agent did not deliver microphone PCM to Qwen"
                assert llm._client.video_frames > 0, "Agent did not deliver camera frames to Qwen"
                report["agent_audio_frames"] = llm._client.audio_frames
                report["agent_video_frames"] = llm._client.video_frames
                if with_pose:
                    assert displayed_frames > 0, "No processed video reached the dashboard sink"
                    assert poses[-1].status != "inference_error", poses[-1].to_dict()
                    assert all(p.session_id == "chrome-smoke" for p in poses)
                    report["pose_snapshots"] = len(poses)
                    report["pose_status"] = poses[-1].status
                    report["dashboard_frames"] = displayed_frames
                    report["pose_device"] = pose_device
                await evaluate("document.querySelector('#stop').click()")
                await asyncio.wait_for(edge.finished.wait(), 5)
                assert await evaluate("stream === null && pc === null")
                report["stop_cleanup"] = True
                print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            if producer:
                producer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await producer
            await session_context.aclose()
            await agent.close()
            await edge.close()
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pose", action="store_true", help="Include the real local YOLO adapter")
    parser.add_argument("--pose-device", default="cpu")
    args = parser.parse_args()
    asyncio.run(main(args.pose, args.pose_device))
