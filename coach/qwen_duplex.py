"""Qwen adapter completion/barge-in fixes for the pinned vision-agents 0.6.9."""

import asyncio
import base64
import binascii
from collections import OrderedDict
from dataclasses import dataclass
import contextlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable

from getstream.video.rtc import PcmData
from vision_agents.plugins.qwen import Realtime
from vision_agents.plugins.qwen.client import Qwen3RealtimeClient

from coach.qwen_contract import build_response_create_event, build_text_input_event
from coach.voice_state import ResponseWindow

LOGGER = logging.getLogger("vision_coach")


@dataclass(frozen=True, slots=True)
class _PendingInjection:
    feedback_id: str | None
    guard: Callable[[], bool] | None
    deadline: float


class DuplexQwenRealtime(Realtime):
    """Qwen Realtime adapter with interruption-safe text injection.

    The pinned Vision-Agents adapter intentionally leaves ``simple_response``
    empty because Qwen Realtime does not expose that older helper contract.
    The DashScope websocket does accept a conversation ``input_text`` item,
    so the memory/agent layer uses :meth:`inject_text` instead.  User
    transcription callbacks are scheduled away from the websocket reader so
    a slow retrieval operation cannot stall audio/video ingestion.
    """

    def __init__(
        self,
        *args,
        user_transcript_sink: Callable[[str], Awaitable[None] | None] | None = None,
        feedback_sink: Callable[[str, str | None, str | None], Awaitable[None] | None]
        | None = None,
        vad_type: str = "semantic_vad",
        **kwargs,
    ):
        vad_type = str(vad_type).strip()
        if vad_type not in {"server_vad", "semantic_vad"}:
            raise ValueError("vad_type must be server_vad or semantic_vad")
        super().__init__(*args, **kwargs)
        self.vad_type = vad_type
        self.user_transcript_sink = user_transcript_sink
        self.feedback_sink = feedback_sink
        self._transcript_tasks: set[asyncio.Task] = set()
        self._feedback_tasks: set[asyncio.Task] = set()
        self._active_feedback_id: str | None = None
        self._active_feedback_response_id: str | None = None
        self._response_stats: dict[str, dict[str, float | int]] = {}
        self._cancelled_response_ids: OrderedDict[str, None] = OrderedDict()
        self.responses = ResponseWindow()
        self.native_response_gate: Callable[[str], Callable[[], bool] | None] | None = None
        self._pending_injection: _PendingInjection | None = None
        self._request_sent = False
        self._voice_generation = 0
        self._reader_epoch = 0
        self._closing = False
        self.output_blocked_reason: str | None = None
        self._cancel_task: asyncio.Task | None = None
        self._injection_lock = asyncio.Lock()
        self.user_speech_started_sink: Callable[[], None] | None = None
        self._playback_guard: Callable[[], bool] | None = None

    def _build_session_config(self) -> dict:
        """Build the pinned Qwen session payload with the selected VAD mode."""

        return {
            "modalities": ["text", "audio"],
            "voice": self.voice,
            "instructions": self._instructions,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm24",
            "input_audio_transcription": {"model": self._audio_transcription_model},
            "turn_detection": {
                "type": self.vad_type,
                "threshold": self._vad_threshold,
                "prefix_padding_ms": self._vad_prefix_padding_ms,
                "silence_duration_ms": self._vad_silence_duration_ms,
            },
        }

    async def connect(self):
        """Connect using semantic VAD support absent from the pinned adapter."""

        # Fence the old reader and every old guard before opening a new socket.
        self._reader_epoch += 1
        self._voice_generation += 1
        self.responses.invalidate()
        self._closing = True
        await self._stop_processing_task()
        session_config = self._build_session_config()
        self._real_client = Qwen3RealtimeClient(
            api_key=self._api_key,
            base_url=self._base_url,
            model=self.model,
            config=session_config,
        )
        await self._real_client.connect()
        self.responses = ResponseWindow()
        self._pending_injection = None
        self._request_sent = False
        self._playback_guard = None
        self._active_feedback_id = None
        self._active_feedback_response_id = None
        self._current_response_id = None
        self._current_item_id = None
        self._is_responding = False
        self._response_stats.clear()
        self._cancelled_response_ids.clear()
        self.output_blocked_reason = None
        self._closing = False
        self._on_connected(session_config=session_config)
        LOGGER.info(
            "Qwen Realtime 已连接：vad=%s threshold=%.2f silence=%dms",
            self.vad_type,
            self._vad_threshold,
            self._vad_silence_duration_ms,
        )
        self._start_processing_task()

    async def inject_text(
        self,
        text: str,
        *,
        interrupt: bool = True,
        feedback_id: str | None = None,
        is_current: Callable[[], bool] | None = None,
        playback_guard: Callable[[], bool] | None = None,
    ) -> bool:
        """Ask the active Qwen session to speak a validated text instruction.

        This is an application-to-model control message, not a user utterance
        and not an authoritative training fact.  The caller is responsible
        for bounding and validating ``text`` before it reaches this method.
        """

        text = str(text).strip()
        if not text or not self.connected or self._closing or self.output_blocked_reason:
            return False
        generation = self._voice_generation
        async with self._injection_lock:
            if (generation != self._voice_generation or self._closing
                    or self.output_blocked_reason or not self._guard_ok(is_current)):
                return False
            if self._pending_injection is not None:
                if time.monotonic() >= self._pending_injection.deadline:
                    self._block_output("response_creation_timeout")
                self.responses.metrics["pending_injection_rejected"] += 1
                return False
            if interrupt:
                generation += 1
                await self._on_interruption()
                if is_current is not None:
                    self._emit_audio_output_done_event(interrupted=True)
            elif self._is_responding:
                return False
            if (generation != self._voice_generation or self.output_blocked_reason
                    or self._closing or not self._guard_ok(is_current)):
                return False
            generation = self._voice_generation
            pending = _PendingInjection(feedback_id, playback_guard, time.monotonic() + 4.0)
            self._pending_injection = pending
            self._request_sent = False
            client = self._client
            try:
                await client.send_event(build_text_input_event(text))
                if (generation != self._voice_generation or self.output_blocked_reason
                        or self._closing or not self._guard_ok(is_current)):
                    return False
                # Set before awaiting: a send failure/cancellation may be an
                # uncertain write, not evidence that the server received nothing.
                self._request_sent = True
                await client.send_event(build_response_create_event())
                if generation != self._voice_generation or not self._guard_ok(is_current):
                    self._block_output("injection_invalidated_during_send")
                    return False
                self._schedule_feedback_sink("queued", feedback_id, None)
                return True
            except BaseException:
                if self._request_sent:
                    self._block_output("response_send_uncertain")
                raise
            finally:
                if self._pending_injection is pending and not self._request_sent:
                    self._pending_injection = None

    @staticmethod
    def _guard_ok(guard: Callable[[], bool] | None) -> bool:
        if guard is None:
            return True
        try:
            return guard() is True
        except Exception:
            LOGGER.warning("voice_guard_failed")
            return False

    def _clear_active(self) -> None:
        if self.responses.active_id is not None:
            self.responses.invalidate()
        self._is_responding = False
        self._current_response_id = None
        self._current_item_id = None
        self._active_feedback_id = None
        self._active_feedback_response_id = None
        self._playback_guard = None
        self._response_stats.clear()

    def _block_output(self, reason: str) -> None:
        if self.output_blocked_reason is None:
            LOGGER.warning("voice_output_blocked: %s", reason)
            self._emit_audio_output_done_event(interrupted=True)
        self.output_blocked_reason = reason
        self._voice_generation += 1
        self._pending_injection = None
        self._request_sent = False
        self._clear_active()

    def _accept_output(self, response_id: object, *, audio: bool = False) -> bool:
        if self._closing or self.output_blocked_reason or not self.responses.accepts(
                response_id, audio=audio):
            return False
        if self._guard_ok(self._playback_guard):
            return True
        self.responses.metrics["guard_rejected"] += 1
        self._schedule_feedback_sink("interrupted", self._active_feedback_id,
                                     self._current_response_id)
        self._emit_audio_output_done_event(interrupted=True)
        self._clear_active()
        return False

    def _begin_response(self, response_id: object) -> bool:
        if self._closing or self.output_blocked_reason:
            self.responses.retire(response_id)
            return False
        if self._cancel_task is not None and not self._cancel_task.done():
            self._block_output("response_created_during_cancel")
            self.responses.retire(response_id)
            return False
        if self.responses.begin(response_id) != "started":
            return False
        pending = self._pending_injection
        if pending is not None:
            if (not self._request_sent or time.monotonic() >= pending.deadline
                    or not self._guard_ok(pending.guard)):
                self._block_output("response_creation_ambiguous_or_expired")
                return False
            # Legacy ordered candidate only: this protocol does not echo a
            # client request ID. Native/manual concurrency is NOT proven away.
            self._active_feedback_id = pending.feedback_id
            self._playback_guard = pending.guard
            self._pending_injection = None
            self._request_sent = False
            self.responses.metrics["ordered_injection_candidate"] += 1
        elif self.native_response_gate is not None:
            try:
                guard = self.native_response_gate(response_id)
            except Exception:
                guard = None
                LOGGER.warning("native_response_admission_failed")
            if guard is None or not callable(guard) or not self._guard_ok(guard):
                self.responses.metrics["native_rejected"] += 1
                self.responses.finish(response_id)
                return False
            self._playback_guard = guard
            self._active_feedback_id = None
            self.responses.metrics["native_admitted"] += 1
        self._current_response_id = response_id
        self._current_item_id = None
        self._is_responding = True
        self._active_feedback_response_id = response_id
        self._response_stats.clear()
        self._response_stats[response_id] = {
            "created_at": time.monotonic(), "audio_chunks": 0,
            "audio_ms": 0.0, "max_delta_gap_ms": 0.0,
        }
        self._schedule_feedback_sink("generated", self._active_feedback_id, response_id)
        return True

    def _schedule_transcript_sink(self, text: str) -> None:
        sink = self.user_transcript_sink
        if sink is None:
            return
        try:
            result = sink(text)
        except Exception:
            # A transcript observer must never terminate the Qwen reader.
            return
        if not inspect.isawaitable(result):
            return
        if len(self._transcript_tasks) >= 4:
            if inspect.iscoroutine(result):
                result.close()
            LOGGER.warning("transcript_observer_capacity_exhausted")
            return
        task = asyncio.create_task(result)
        self._transcript_tasks.add(task)
        task.add_done_callback(lambda done: self._observer_done(done, self._transcript_tasks))

    def _schedule_feedback_sink(
        self, state: str, feedback_id: str | None, response_id: str | None
    ) -> None:
        sink = self.feedback_sink
        if sink is None or not feedback_id:
            return
        try:
            result = sink(state, feedback_id, response_id)
        except Exception:
            return
        if not inspect.isawaitable(result):
            return
        if len(self._feedback_tasks) >= 16:
            if inspect.iscoroutine(result):
                result.close()
            LOGGER.warning("feedback_observer_capacity_exhausted")
            return
        task = asyncio.create_task(result)
        self._feedback_tasks.add(task)
        task.add_done_callback(lambda done: self._observer_done(done, self._feedback_tasks))

    @staticmethod
    def _observer_done(task, tasks):
        tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            LOGGER.warning("qwen_observer_failed")

    async def close(self):
        self._closing = True
        self._reader_epoch += 1
        self._voice_generation += 1
        self._pending_injection = None
        self._request_sent = False
        self._clear_active()
        if self._cancel_task is not None and not self._cancel_task.done():
            self._cancel_task.cancel()
            await asyncio.wait({self._cancel_task}, timeout=0.1)
        # Upstream 0.6.9 lets CancelledError skip websocket/executor cleanup.
        if self._processing_task is not None:
            self._processing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._processing_task
            self._processing_task = None
        observers = self._transcript_tasks | self._feedback_tasks
        for task in observers:
            task.cancel()
        if observers:
            _, pending = await asyncio.wait(observers, timeout=0.1)
            if pending:
                LOGGER.warning("qwen_observers_pending_on_close")
        self._response_stats.clear()
        self._cancelled_response_ids.clear()
        await super().close()

    async def _process_events(self):
        # Pin both client and connection epoch; a late old reader must not
        # consume/mutate the newly connected session through self._client.
        client, epoch = self._client, self._reader_epoch
        async for event in client.read():
            if self._closing or epoch != self._reader_epoch:
                return
            if not isinstance(event, dict):
                self.responses.metrics["malformed_event"] += 1
                continue
            kind = event.get("type")
            response_id = event.get("response_id")
            response = event.get("response")
            response = response if isinstance(response, dict) else {}
            if kind == "error":
                if self._pending_injection is not None:
                    self._block_output("provider_error_with_pending_injection")
                self._emit_error_event(
                    error=Exception("Qwen realtime provider error"), context="qwen_realtime_api"
                )
            elif kind == "response.created":
                self._begin_response(response.get("id"))
            elif kind == "response.output_item.added":
                if self._accept_output(response_id):
                    item = event.get("item")
                    if isinstance(item, dict) and ResponseWindow.valid_id(item.get("id")):
                        self._current_item_id = item["id"]
            elif kind == "input_audio_buffer.speech_started":
                # Observer failure must not skip the local playback fence.
                try:
                    if self.user_speech_started_sink is not None:
                        self.user_speech_started_sink()
                except Exception:
                    LOGGER.warning("speech_started_observer_failed")
                self._emit_audio_output_done_event(interrupted=True)
                self._emit_user_speech_started()
                await self._on_interruption()
            elif kind == "input_audio_buffer.speech_stopped":
                self._emit_user_speech_ended()
            elif kind == "response.audio.done":
                if self._accept_output(response_id, audio=True):
                    self.responses.finish_audio(response_id)
                    self._emit_audio_output_done_event(response_id=response_id)
            elif kind == "response.done":
                done_id = response.get("id")
                if self._accept_output(done_id):
                    status = response.get("status")
                    if status is not None and not isinstance(status, str):
                        status = "unknown"
                    completed = status in (None, "completed")  # finalize legacy audio, not success
                    if not completed:
                        self._emit_audio_output_done_event(response_id=done_id, interrupted=True)
                    elif not self.responses.audio_closed:
                        self._emit_audio_output_done_event(response_id=done_id)
                    self._emit_agent_speech_transcription(text="", mode="final")
                    state = {
                        "failed": "generation_failed", "incomplete": "generation_incomplete",
                        "cancelled": "interrupted",
                        "completed": "generation_completed",
                    }.get(status, "generation_unknown")
                    self._schedule_feedback_sink(state, self._active_feedback_id, done_id)
                    stats = self._response_stats.get(done_id)
                    if stats is not None:
                        LOGGER.debug(
                            "Qwen response.done id=%s chunks=%d audio=%.0fms max_delta_gap=%.0fms",
                            done_id, stats["audio_chunks"], stats["audio_ms"],
                            stats["max_delta_gap_ms"],
                        )
                    self.responses.finish(done_id)
                    self._clear_active()
                else:
                    self.responses.retire(done_id)
                if ResponseWindow.valid_id(done_id):
                    self._cancelled_response_ids.pop(done_id, None)
            elif kind == "response.audio.delta":
                if not self._accept_output(response_id, audio=True):
                    continue
                try:
                    delta = event["delta"]
                    if not isinstance(delta, str) or len(delta) > 262144:
                        raise ValueError("audio delta exceeds byte budget")
                    pcm = PcmData.from_bytes(base64.b64decode(delta, validate=True), 24000)
                except (ValueError, TypeError, KeyError, binascii.Error):
                    self.responses.metrics["malformed_audio"] += 1
                    continue
                stats = self._response_stats.get(response_id)
                if stats is not None:
                    now = time.monotonic()
                    previous = stats.get("last_delta_at")
                    if isinstance(previous, float):
                        stats["max_delta_gap_ms"] = max(
                            float(stats["max_delta_gap_ms"]), (now - previous) * 1000
                        )
                    stats["last_delta_at"] = now
                    stats["audio_chunks"] = int(stats["audio_chunks"]) + 1
                    stats["audio_ms"] = float(stats["audio_ms"]) + pcm.duration_ms
                self._emit_audio_output_event(pcm=pcm, response_id=response_id)
            elif kind == "conversation.item.input_audio_transcription.completed":
                if text := event.get("transcript", ""):
                    self._emit_user_speech_transcription(text=text, mode="final")
                    self._schedule_transcript_sink(text)
            elif kind == "response.audio_transcript.delta":
                if self._accept_output(response_id) and (text := event.get("delta", "")):
                    self._emit_agent_speech_transcription(text=text, mode="delta")

    async def _on_interruption(self):
        self._voice_generation += 1
        pending = self._pending_injection
        if pending is not None:
            self._schedule_feedback_sink("interrupted", pending.feedback_id, None)
            if self._request_sent:
                # Unknown response identity cannot safely be reassigned to a
                # newer request. Reconnect is the explicit recovery boundary.
                self._block_output("unresolved_response_after_interruption")
            self._pending_injection = None
        response_id = self._current_response_id
        feedback_id = self._active_feedback_id
        if response_id:
            self._schedule_feedback_sink("interrupted", feedback_id, response_id)
            self.responses.retire(response_id)
            self._cancelled_response_ids[response_id] = None
            while len(self._cancelled_response_ids) > self.responses.history_size:
                self._cancelled_response_ids.popitem(last=False)
        self._clear_active()
        if response_id:
            if self._cancel_task is None or self._cancel_task.done():
                self._cancel_task = asyncio.create_task(self._client.cancel_response())
                self._cancel_task.add_done_callback(self._cancel_done)
            task = self._cancel_task
            try:
                done, _ = await asyncio.wait({task}, timeout=0.2)
            except asyncio.CancelledError:
                self._block_output("cancel_wait_abandoned")
                raise
            if not done or task.cancelled() or task.exception() is not None:
                self._block_output("provider_cancel_unconfirmed")

    @staticmethod
    def _cancel_done(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()  # A timed-out cancel still owns and drains its exception.
