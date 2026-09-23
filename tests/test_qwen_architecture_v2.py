"""Offline protocol regression tests for the pinned Qwen adapter."""
import asyncio
import base64
import unittest
from unittest.mock import AsyncMock, Mock

from coach.qwen_duplex import DuplexQwenRealtime


class QwenArchitectureTests(unittest.IsolatedAsyncioTestCase):
    def make_llm(self, events=()):
        llm = DuplexQwenRealtime(api_key="offline-test")
        self.addCleanup(llm._executor.shutdown, wait=False)
        async def read():
            for event in events:
                yield event
        llm._real_client = Mock(read=read, cancel_response=AsyncMock(), send_event=AsyncMock())
        llm.connected = True
        llm._emit_audio_output_event = Mock()
        llm._emit_audio_output_done_event = Mock()
        llm._emit_agent_speech_transcription = Mock()
        return llm

    async def test_invalidated_injection_never_sends_request(self):
        llm = self.make_llm()
        accepted = await llm.inject_text("stale", is_current=lambda: False)
        self.assertFalse(accepted)
        llm._real_client.send_event.assert_not_awaited()

    async def test_invalidation_during_cancel_blocks_new_request(self):
        llm = self.make_llm()
        valid = [True]
        async def cancel():
            valid[0] = False
        llm._real_client.cancel_response = AsyncMock(side_effect=cancel)
        llm._is_responding = True
        llm._current_response_id = "old"
        self.assertFalse(await llm.inject_text("new", is_current=lambda: valid[0]))
        llm._real_client.send_event.assert_not_awaited()

    async def test_old_done_does_not_complete_new_feedback_or_audio(self):
        events = [
            {"type": "response.created", "response": {"id": "new"}},
            {"type": "response.done", "response": {"id": "old"}},
        ]
        llm = self.make_llm(events)
        llm.feedback_sink = Mock()
        llm._active_feedback_id = "feedback-new"
        await llm._process_events()
        self.assertEqual(llm.feedback_sink.call_count, 1)
        self.assertEqual(llm.feedback_sink.call_args.args[0], "generated")
        llm._emit_audio_output_done_event.assert_not_called()
        self.assertEqual(llm._current_response_id, "new")

    async def test_generation_done_is_not_playback_done(self):
        llm = self.make_llm([
            {"type": "response.created", "response": {"id": "one"}},
            {"type": "response.done", "response": {"id": "one"}},
        ])
        llm._active_feedback_id = "feedback"
        llm.feedback_sink = Mock()
        await llm._process_events()
        states = [call.args[0] for call in llm.feedback_sink.call_args_list]
        self.assertEqual(states, ["generated", "generation_completed"])

    async def test_guard_drops_stale_audio_and_resets_for_next_response(self):
        delta = base64.b64encode(bytes(960)).decode()
        llm = self.make_llm([
            {"type": "response.created", "response": {"id": "old"}},
            {"type": "response.audio.delta", "response_id": "old", "delta": delta},
            {"type": "response.done", "response": {"id": "old"}},
            {"type": "response.created", "response": {"id": "new"}},
            {"type": "response.audio.delta", "response_id": "new", "delta": delta},
        ])
        llm._playback_guard = lambda: False
        await llm._process_events()
        llm._emit_audio_output_event.assert_called_once()
        self.assertEqual(llm._emit_audio_output_event.call_args.kwargs["response_id"], "new")

    async def test_vad_invalidates_application_before_reader_continues(self):
        llm = self.make_llm([{"type": "input_audio_buffer.speech_started"}])
        llm.user_speech_started_sink = Mock()
        await llm._process_events()
        llm.user_speech_started_sink.assert_called_once()
        llm._emit_audio_output_done_event.assert_called_once_with(interrupted=True)

    async def test_feedback_observer_fanout_is_bounded(self):
        llm = self.make_llm()
        release = asyncio.Event()
        async def observer(*args):
            await release.wait()
        llm.feedback_sink = observer
        for i in range(40):
            llm._schedule_feedback_sink("generated", str(i), None)
        self.assertLessEqual(len(llm._feedback_tasks), 16)
        release.set()
        await asyncio.gather(*tuple(llm._feedback_tasks))
