"""Offline Qwen event races, run against the pinned SDK in full CI."""
import asyncio
import base64
import unittest
from unittest.mock import AsyncMock, Mock

from coach.qwen_duplex import DuplexQwenRealtime

DELTA = base64.b64encode(bytes(960)).decode()


def created(identity):
    return {"type": "response.created", "response": {"id": identity}}


def audio(identity):
    return {"type": "response.audio.delta", "response_id": identity, "delta": DELTA}


def finished(identity, status="completed"):
    return {"type": "response.done", "response": {"id": identity, "status": status}}


class VoiceOutputBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def make_llm(self, events=()):
        llm = DuplexQwenRealtime(api_key="offline-test")
        self.addCleanup(llm._executor.shutdown, wait=False)

        async def read():
            for event in events:
                if callable(event):
                    event()
                else:
                    yield event

        llm._real_client = Mock(
            read=read, cancel_response=AsyncMock(), send_event=AsyncMock(), close=AsyncMock(),
        )
        llm.connected = True
        llm._emit_audio_output_event = Mock()
        llm._emit_audio_output_done_event = Mock()
        llm._emit_agent_speech_transcription = Mock()
        llm._emit_user_speech_transcription = Mock()
        llm._emit_error_event = Mock()
        llm.feedback_sink = Mock()
        return llm

    async def test_duplicate_created_emits_no_duplicate_feedback(self):
        llm = self.make_llm([created("one"), created("one"), audio("one")])
        llm._active_feedback_id = "f"
        await llm._process_events()
        llm.feedback_sink.assert_called_once_with("generated", "f", "one")
        llm._emit_audio_output_event.assert_called_once()

    async def test_late_created_cannot_replace_current_response(self):
        llm = self.make_llm([
            created("current"), created("late"), audio("late"), finished("late"), audio("current"),
        ])
        await llm._process_events()
        self.assertEqual(llm._current_response_id, "current")
        llm._emit_audio_output_event.assert_called_once()
        self.assertEqual(llm._emit_audio_output_event.call_args.kwargs["response_id"], "current")

    async def test_completed_response_cannot_resurrect(self):
        llm = self.make_llm([created("one"), finished("one"), created("one"), audio("one")])
        await llm._process_events()
        llm._emit_audio_output_event.assert_not_called()
        self.assertIsNone(llm._current_response_id)

    async def test_missing_ids_never_mutate_current_response(self):
        llm = self.make_llm([
            created("one"), audio(None),
            {"type": "response.audio.done"}, {"type": "response.done", "response": {}},
            {"type": "response.audio_transcript.delta", "delta": "unscoped"}, audio("one"),
        ])
        await llm._process_events()
        self.assertEqual(llm._current_response_id, "one")
        llm._emit_audio_output_event.assert_called_once()
        llm._emit_audio_output_done_event.assert_not_called()
        llm._emit_agent_speech_transcription.assert_not_called()

    async def test_foreign_item_does_not_overwrite_current_item(self):
        llm = self.make_llm([
            created("one"),
            {"type": "response.output_item.added", "response_id": "one", "item": {"id": "good"}},
            {"type": "response.output_item.added", "response_id": "old", "item": {"id": "bad"}},
        ])
        await llm._process_events()
        self.assertEqual(llm._current_item_id, "good")

    async def test_expired_guard_filters_transcript_and_flushes_audio(self):
        llm = self.make_llm([
            created("one"),
            {"type": "response.audio_transcript.delta", "response_id": "one", "delta": "stale"},
            audio("one"),
        ])
        llm._playback_guard = lambda: False
        await llm._process_events()
        llm._emit_agent_speech_transcription.assert_not_called()
        llm._emit_audio_output_event.assert_not_called()
        llm._emit_audio_output_done_event.assert_called_once_with(interrupted=True)

    async def test_guard_exception_fails_closed_not_reader_crash(self):
        llm = self.make_llm([created("one"), audio("one")])
        llm._playback_guard = Mock(side_effect=ValueError("bad observer"))
        await llm._process_events()
        llm._emit_audio_output_event.assert_not_called()

    async def test_pending_injection_cannot_be_overwritten(self):
        llm = self.make_llm()
        self.assertTrue(await llm.inject_text("first", feedback_id="first"))
        self.assertFalse(await llm.inject_text("second", feedback_id="second"))
        self.assertEqual(llm._real_client.send_event.await_count, 2)
        self.assertEqual(llm._pending_injection.feedback_id, "first")

    async def test_interrupted_unbound_request_quarantines_late_created(self):
        events = [{"type": "input_audio_buffer.speech_started"}, created("late"), audio("late")]
        llm = self.make_llm(events)
        self.assertTrue(await llm.inject_text("first", feedback_id="first"))
        await llm._process_events()
        self.assertEqual(llm.output_blocked_reason, "unresolved_response_after_interruption")
        self.assertFalse(await llm.inject_text("new", feedback_id="new"))
        llm._emit_audio_output_event.assert_not_called()
        self.assertNotIn("generated", [c.args[0] for c in llm.feedback_sink.call_args_list])

    async def test_expired_pending_does_not_become_a_new_generation(self):
        from dataclasses import replace
        llm = self.make_llm([created("late"), audio("late")])
        await llm.inject_text("first")
        llm._pending_injection = replace(llm._pending_injection, deadline=0.0)
        await llm._process_events()
        self.assertIsNotNone(llm.output_blocked_reason)
        llm._emit_audio_output_event.assert_not_called()

    async def test_response_send_error_is_uncertain_not_automatic_retry(self):
        llm = self.make_llm()
        llm._real_client.send_event.side_effect = [None, OSError("connection lost")]
        with self.assertRaises(OSError):
            await llm.inject_text("first")
        self.assertEqual(llm.output_blocked_reason, "response_send_uncertain")
        self.assertFalse(await llm.inject_text("retry"))

    async def test_native_rejection_blocks_audio_text_and_feedback(self):
        llm = self.make_llm([
            created("native"), audio("native"),
            {"type": "response.audio_transcript.delta", "response_id": "native", "delta": "no"},
            finished("native"),
        ])
        llm.native_response_gate = Mock(return_value=None)
        await llm._process_events()
        llm.native_response_gate.assert_called_once_with("native")
        llm._emit_audio_output_event.assert_not_called()
        llm._emit_agent_speech_transcription.assert_not_called()
        llm.feedback_sink.assert_not_called()

    async def test_native_guard_revocation_blocks_following_packets(self):
        allowed = [True]
        llm = self.make_llm([
            created("native"), audio("native"), lambda: allowed.__setitem__(0, False), audio("native"),
        ])
        llm.native_response_gate = lambda _: lambda: allowed[0]
        await llm._process_events()
        llm._emit_audio_output_event.assert_called_once()
        llm._emit_audio_output_done_event.assert_called_once_with(interrupted=True)

    async def test_failed_and_incomplete_generation_are_not_completed(self):
        for status, expected in (("failed", "generation_failed"),
                                 ("incomplete", "generation_incomplete"),
                                 ("cancelled", "interrupted"), ("unknown", "generation_unknown")):
            with self.subTest(status=status):
                llm = self.make_llm([created("one"), finished("one", status)])
                llm._active_feedback_id = "f"
                await llm._process_events()
                self.assertEqual(llm.feedback_sink.call_args.args[0], expected)
                self.assertIsNone(llm._current_response_id)

    async def test_missing_status_does_not_claim_success(self):
        llm = self.make_llm([created("one"), {"type": "response.done", "response": {"id": "one"}}])
        llm._active_feedback_id = "f"
        await llm._process_events()
        self.assertEqual(llm.feedback_sink.call_args.args[0], "generation_unknown")

    async def test_malformed_audio_and_response_envelopes_do_not_kill_reader(self):
        llm = self.make_llm([
            None, {"type": "response.created", "response": None}, created("one"),
            {"type": "response.audio.delta", "response_id": "one", "delta": "@@@"},
            audio("one"), finished("one"),
        ])
        await llm._process_events()
        llm._emit_audio_output_event.assert_called_once()
        self.assertEqual(llm.responses.metrics["malformed_audio"], 1)

    async def test_observer_exception_does_not_skip_interruption(self):
        llm = self.make_llm([created("one"), {"type": "input_audio_buffer.speech_started"}, audio("one")])
        llm.user_speech_started_sink = Mock(side_effect=RuntimeError("observer failure"))
        await llm._process_events()
        llm._emit_audio_output_event.assert_not_called()
        llm._real_client.cancel_response.assert_awaited_once()
        self.assertIsNone(llm._current_response_id)

    async def test_uncooperative_cancel_has_bounded_wait_and_owned_task(self):
        llm = self.make_llm()
        llm._begin_response("one")
        entered, release = asyncio.Event(), asyncio.Event()
        async def cancel():
            entered.set()
            await release.wait()
        llm._real_client.cancel_response.side_effect = cancel
        task = asyncio.create_task(llm._on_interruption())
        try:
            await entered.wait()
            self.assertIsNone(llm._current_response_id)
            done, _ = await asyncio.wait({task}, timeout=1.0)
            self.assertIn(task, done)
            self.assertFalse(llm._cancel_task.done())
            self.assertEqual(llm.output_blocked_reason, "provider_cancel_unconfirmed")
            self.assertFalse(await llm.inject_text("new"))
        finally:
            release.set()
            await llm._cancel_task
            await task

    async def test_reader_from_old_connection_cannot_adopt_new_response(self):
        llm = self.make_llm()
        entered, release = asyncio.Event(), asyncio.Event()
        async def read():
            entered.set()
            await release.wait()
            yield created("late")
        llm._real_client.read = read
        task = asyncio.create_task(llm._process_events())
        await entered.wait()
        llm._reader_epoch += 1
        llm._begin_response("current")
        release.set()
        await task
        self.assertEqual(llm._current_response_id, "current")

    async def test_close_while_text_send_waits_prevents_response_create(self):
        llm = self.make_llm()
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(event):
            entered.set()
            await release.wait()
        client = llm._real_client
        client.send_event.side_effect = send
        task = asyncio.create_task(llm.inject_text("first"))
        await entered.wait()
        try:
            await llm.close()
        finally:
            release.set()
        self.assertFalse(await task)
        # The real SDK clears _real_client during close; retain the fake
        # handle to assert on the actual side effects, not an obsolete owner.
        self.assertEqual(client.send_event.await_count, 1)
        client.close.assert_awaited_once()
