"""Synthetic loopback tests; no microphone, camera, YOLO or cloud credentials."""

import asyncio
import base64
import unittest
from unittest.mock import AsyncMock, Mock

import av
import numpy as np
from aiohttp import ClientSession
from aiortc import (
    AudioStreamTrack,
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)
from getstream.video.rtc import PcmData
from vision_agents.core.edge.events import AudioReceivedEvent, TrackAddedEvent

from coach.browser_edge import BrowserAudioTrack, BrowserEdge
from coach.qwen_duplex import DuplexQwenRealtime


def tone(rate=24000, count=480):
    samples = (np.sin(np.arange(count) * 2 * np.pi * 440 / rate) * 8000).astype(np.int16)
    return PcmData(rate, "s16", samples)


class ToneTrack(AudioStreamTrack):
    async def recv(self):
        frame = await super().recv()
        replacement = av.AudioFrame.from_ndarray(
            tone(frame.sample_rate, frame.samples).samples.reshape(1, -1),
            format="s16",
            layout="mono",
        )
        replacement.sample_rate = frame.sample_rate
        replacement.pts, replacement.time_base = frame.pts, frame.time_base
        return replacement


class BrowserAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_flush_unblocks_backpressure_without_replaying_old_frames(self):
        track = BrowserAudioTrack()
        track.active = True
        writer = asyncio.create_task(track.write(tone(count=48000), final=True))
        await asyncio.sleep(0)
        self.assertFalse(writer.done())
        self.assertEqual(len(track.frames), track.max_frames)
        await track.flush()
        await asyncio.wait_for(writer, 1)
        self.assertEqual(len(track.frames), 0)
        self.assertEqual(np.abs((await track.recv()).to_ndarray()).max(), 0)
        writer = asyncio.create_task(track.write(tone(count=48000), final=True))
        await asyncio.sleep(0)
        track.stop()
        await asyncio.wait_for(writer, 1)

    async def test_resample_tail_and_monotonic_clock(self):
        track = BrowserAudioTrack(start_buffer_ms=20, resume_buffer_ms=20)
        track.active = True
        await track.write(tone(), final=True)
        frames = [await track.recv() for _ in range(len(track.frames))]
        self.assertEqual(sum(f.samples for f in frames), 960)
        self.assertTrue(all(f.sample_rate == 48000 for f in frames))
        self.assertGreater(np.abs(frames[0].to_ndarray()).max(), 100)
        silence = await track.recv()
        self.assertEqual(silence.pts, 960)
        self.assertEqual(np.abs(silence.to_ndarray()).max(), 0)
        track.stop()

    async def test_flush_removes_queue_and_resampler_tail(self):
        track = BrowserAudioTrack(start_buffer_ms=20, resume_buffer_ms=20)
        track.active = True
        await track.write(tone(count=2400))
        await track.flush()
        await track.write(PcmData(24000, "s16"), final=True)
        frame = await track.recv()
        self.assertEqual(np.abs(frame.to_ndarray()).max(), 0)
        track.stop()

    async def test_prebuffer_and_underrun_recovery_smooth_bursty_deltas(self):
        track = BrowserAudioTrack(
            start_buffer_ms=60,
            resume_buffer_ms=40,
            max_buffer_ms=200,
        )
        track.active = True

        # Two frames are below the start threshold and therefore do not
        # response; the first render tick must remain silent.
        await track.write(tone(count=960))
        first = await track.recv()
        self.assertEqual(np.abs(first.to_ndarray()).max(), 0)
        self.assertTrue(track.is_buffering)

        # Two more frames complete the start buffer.  Playback then begins
        # with audio rather than exposing the burst boundary as a gap.
        await track.write(tone(count=960))
        second = await track.recv()
        self.assertGreater(np.abs(second.to_ndarray()).max(), 100)
        self.assertFalse(track.is_buffering)

        # Consume the remaining frames and force exactly one underrun.
        while track.frames:
            await track.recv()
        underrun = await track.recv()
        self.assertEqual(np.abs(underrun.to_ndarray()).max(), 0)
        self.assertEqual(track.underrun_count, 1)
        self.assertTrue(track.is_buffering)

        # Resume requires the smaller recovery buffer.
        await track.write(tone(count=960))
        resumed = await track.recv()
        self.assertGreater(np.abs(resumed.to_ndarray()).max(), 100)
        self.assertFalse(track.is_buffering)
        track.stop()

    async def test_initial_playout_uses_start_threshold_not_resume_threshold(self):
        track = BrowserAudioTrack(
            start_buffer_ms=60,
            resume_buffer_ms=20,
            max_buffer_ms=200,
        )
        track.active = True

        await track.write(tone(count=1440))
        self.assertEqual(len(track.frames), 2)
        first = await track.recv()
        self.assertEqual(np.abs(first.to_ndarray()).max(), 0)
        self.assertEqual(track.playout_state, "priming")

        await track.write(tone(count=480))
        second = await track.recv()
        self.assertGreater(np.abs(second.to_ndarray()).max(), 100)
        self.assertEqual(track.playout_state, "playing")
        track.stop()

    async def test_final_short_utterance_bypasses_prebuffer_without_underrun(self):
        track = BrowserAudioTrack(
            start_buffer_ms=200,
            resume_buffer_ms=240,
            max_buffer_ms=400,
        )
        track.active = True

        await track.write(tone(count=480), final=True)
        self.assertEqual(len(track.frames), 1)
        audio = await track.recv()
        self.assertGreater(np.abs(audio.to_ndarray()).max(), 100)
        silence = await track.recv()
        self.assertEqual(np.abs(silence.to_ndarray()).max(), 0)
        self.assertEqual(track.playout_state, "idle")
        self.assertEqual(track.underrun_count, 0)
        track.stop()

    async def test_rebuffer_can_require_more_audio_than_initial_playout(self):
        track = BrowserAudioTrack(
            start_buffer_ms=20,
            resume_buffer_ms=60,
            max_buffer_ms=200,
        )
        track.active = True

        await track.write(tone(count=960))
        self.assertGreater(np.abs((await track.recv()).to_ndarray()).max(), 100)
        self.assertEqual(np.abs((await track.recv()).to_ndarray()).max(), 0)
        self.assertEqual(track.playout_state, "rebuffering")
        await track.write(tone(count=960))
        self.assertEqual(np.abs((await track.recv()).to_ndarray()).max(), 0)
        await track.write(tone(count=480))
        self.assertGreater(np.abs((await track.recv()).to_ndarray()).max(), 100)
        track.stop()

    async def test_flush_resets_prebuffer_and_discards_late_audio(self):
        track = BrowserAudioTrack(start_buffer_ms=60, resume_buffer_ms=40)
        track.active = True
        await track.write(tone(count=1440))
        await track.flush()
        await track.write(tone(count=480))
        frame = await track.recv()
        self.assertEqual(np.abs(frame.to_ndarray()).max(), 0)
        self.assertEqual(track.underrun_count, 0)
        track.stop()


class BrowserLoopbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.edge = BrowserEdge()
        self.events = []
        self.edge.events.send = self.events.append
        self.call = await self.edge.create_call("test")
        await self.edge.join(None, self.call)
        await self.edge.publish_tracks(self.edge.audio, None)
        self.http = ClientSession()
        self.peer = None

    async def asyncTearDown(self):
        if self.peer:
            await self.peer.close()
        await self.edge.close()
        await self.edge.close()  # Idempotent shutdown is required by Agent.
        await self.http.close()
        self.assertFalse(self.edge.tasks)
        self.assertEqual(self.edge.audio.readyState, "ended")

    async def test_invalid_token_origin_aec_and_offer(self):
        async with self.http.get(self.edge.origin + "/") as r:
            self.assertEqual(r.status, 403)
        async with self.http.get(self.edge.url, headers={"Origin": "https://example.com"}) as r:
            self.assertEqual(r.status, 403)
        url = self.edge.origin + "/offer?token=" + self.edge.token
        for payload in ({}, {"type": "offer", "sdp": "bad", "aec": False}, []):
            async with self.http.post(url, json=payload) as r:
                self.assertEqual(r.status, 400)
        self.assertIsNone(self.edge.pc)

    async def test_simultaneous_audio_and_video_over_real_webrtc(self):
        self.peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        incoming = asyncio.Queue()
        self.peer.on("track", incoming.put_nowait)
        self.peer.addTrack(ToneTrack())
        self.peer.addTrack(VideoStreamTrack())
        await self.peer.setLocalDescription(await self.peer.createOffer())
        async with self.http.post(
            self.edge.origin + "/offer?token=" + self.edge.token,
            json={
                "type": "offer",
                "sdp": self.peer.localDescription.sdp,
                "aec": True,
            },
        ) as r:
            self.assertEqual(r.status, 200, await r.text())
            answer = await r.json()
        await self.peer.setRemoteDescription(RTCSessionDescription(**answer))
        await asyncio.wait_for(self.edge.connected.wait(), 10)
        remote_audio = await asyncio.wait_for(incoming.get(), 3)
        # Fill the server render path while the browser-side tone is also uploading.
        await self.edge.audio.write(tone(count=12000), final=True)
        heard = False
        for _ in range(40):
            frame = await asyncio.wait_for(remote_audio.recv(), 3)
            if np.abs(frame.to_ndarray()).max() > 100:
                heard = True
                break
        self.assertTrue(heard, "downlink lost server audio")
        for _ in range(100):
            uploads = [e for e in self.events if isinstance(e, AudioReceivedEvent)]
            if uploads:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(uploads, "uplink stopped during server playback")
        self.assertGreater(np.abs(uploads[-1].pcm_data.samples).max(), 100)
        video_event = next(e for e in self.events if isinstance(e, TrackAddedEvent))
        video = self.edge.add_track_subscriber(video_event.track_id)
        frame = await asyncio.wait_for(video.recv(), 5)
        self.assertEqual((frame.width, frame.height), (640, 480))
        async with self.http.post(
            self.edge.origin + "/offer?token=" + self.edge.token,
            json={
                "type": "offer",
                "sdp": self.peer.localDescription.sdp,
                "aec": True,
            },
        ) as r:
            self.assertEqual(r.status, 409)
        async with self.http.post(self.edge.origin + "/stop?token=" + self.edge.token) as r:
            self.assertEqual(r.status, 200)
        self.assertTrue(self.edge.finished.is_set())


class QwenDuplexTests(unittest.IsolatedAsyncioTestCase):
    def test_semantic_vad_session_config(self):
        llm = DuplexQwenRealtime(
            api_key="offline-test",
            vad_type="semantic_vad",
            vad_threshold=0.2,
            vad_silence_duration_ms=900,
        )
        self.addCleanup(llm._executor.shutdown, wait=False)
        llm._instructions = "test"
        turn_detection = llm._build_session_config()["turn_detection"]
        self.assertEqual(
            turn_detection,
            {
                "type": "semantic_vad",
                "threshold": 0.2,
                "prefix_padding_ms": 500,
                "silence_duration_ms": 900,
            },
        )

    def test_invalid_vad_type_is_rejected(self):
        with self.assertRaises(ValueError):
            DuplexQwenRealtime(api_key="offline-test", vad_type="manual")

    async def test_close_cancels_reader_and_closes_websocket(self):
        llm = DuplexQwenRealtime(api_key="offline-test")
        client = Mock(close=AsyncMock())
        llm._real_client = client
        llm._processing_task = asyncio.create_task(asyncio.sleep(100))
        await llm.close()
        client.close.assert_awaited_once()
        self.assertIsNone(llm._processing_task)

    async def test_barge_in_clears_playout_and_discards_cancelled_packets(self):
        llm = DuplexQwenRealtime(api_key="offline-test")
        self.addCleanup(llm._executor.shutdown, wait=False)
        delta = base64.b64encode(tone().to_bytes()).decode()
        events = [
            {"type": "response.created", "response": {"id": "one"}},
            {"type": "response.audio.delta", "response_id": "one", "delta": delta},
            {"type": "input_audio_buffer.speech_started"},
            {"type": "response.audio.delta", "response_id": "one", "delta": delta},
            {"type": "response.done", "response": {"id": "one"}},
            {"type": "response.created", "response": {"id": "two"}},
            {"type": "response.audio.delta", "response_id": "two", "delta": delta},
            {"type": "response.done", "response": {"id": "two"}},
            {"type": "input_audio_buffer.speech_started"},
        ]

        async def read():
            for event in events:
                yield event

        llm._real_client = Mock(read=read, cancel_response=AsyncMock())
        llm._emit_audio_output_event = Mock()
        llm._emit_audio_output_done_event = Mock()
        llm._emit_agent_speech_transcription = Mock()
        await llm._process_events()
        self.assertEqual(llm._emit_audio_output_event.call_count, 2)
        llm._real_client.cancel_response.assert_awaited_once()
        calls = llm._emit_audio_output_done_event.call_args_list
        self.assertEqual(sum(c.kwargs.get("interrupted", False) for c in calls), 2)
        self.assertTrue(any(c.kwargs.get("response_id") == "two" for c in calls))

    async def test_audio_done_finalizes_playout_once_and_rejects_late_delta(self):
        llm = DuplexQwenRealtime(api_key="offline-test")
        self.addCleanup(llm._executor.shutdown, wait=False)
        delta = base64.b64encode(tone().to_bytes()).decode()
        events = [
            {"type": "response.created", "response": {"id": "one"}},
            {"type": "response.audio.delta", "response_id": "one", "delta": delta},
            {"type": "response.audio.done", "response_id": "one"},
            {"type": "response.audio.delta", "response_id": "one", "delta": delta},
            {"type": "response.done", "response": {"id": "one", "status": "completed"}},
        ]

        async def read():
            for event in events:
                yield event

        llm._real_client = Mock(read=read)
        llm._emit_audio_output_event = Mock()
        llm._emit_audio_output_done_event = Mock()
        llm._emit_agent_speech_transcription = Mock()
        await llm._process_events()
        llm._emit_audio_output_event.assert_called_once()
        llm._emit_audio_output_done_event.assert_called_once_with(response_id="one")
        self.assertFalse(llm._response_stats)

    async def test_injected_response_rejects_old_cancelled_audio_done(self):
        llm = DuplexQwenRealtime(api_key="offline-test")
        self.addCleanup(llm._executor.shutdown, wait=False)
        delta = base64.b64encode(tone().to_bytes()).decode()
        events = [
            {"type": "response.audio.done", "response_id": "old"},
            {"type": "response.done", "response": {"id": "old", "status": "cancelled"}},
            {"type": "response.created", "response": {"id": "new"}},
            {"type": "response.audio.delta", "response_id": "new", "delta": delta},
            {"type": "response.audio.done", "response_id": "new"},
            {"type": "response.done", "response": {"id": "new", "status": "completed"}},
        ]

        async def read():
            for event in events:
                yield event

        client = Mock(read=read, cancel_response=AsyncMock(), send_event=AsyncMock())
        llm._real_client = client
        llm.connected = True
        llm._is_responding = True
        llm._current_response_id = "old"
        llm._emit_audio_output_event = Mock()
        llm._emit_audio_output_done_event = Mock()
        llm._emit_agent_speech_transcription = Mock()

        self.assertTrue(await llm.inject_text("new response", feedback_id="feedback-new"))
        self.assertIn("old", llm._cancelled_response_ids)
        client.cancel_response.assert_awaited_once()
        await llm._process_events()

        llm._emit_audio_output_event.assert_called_once()
        llm._emit_audio_output_done_event.assert_called_once_with(response_id="new")
        self.assertNotIn("old", llm._cancelled_response_ids)


if __name__ == "__main__":
    unittest.main()
