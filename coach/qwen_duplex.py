"""Qwen adapter completion/barge-in fixes for the pinned vision-agents 0.6.9."""

import asyncio
import base64
import contextlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable

from getstream.video.rtc import PcmData
from vision_agents.plugins.qwen import Realtime
from vision_agents.plugins.qwen.client import Qwen3RealtimeClient

from coach.qwen_contract import build_response_create_event, build_text_input_event

LOGGER = logging.getLogger("vision_coach")


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
        self._cancelled_response_ids: set[str] = set()

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

        await self._stop_processing_task()
        session_config = self._build_session_config()
        self._real_client = Qwen3RealtimeClient(
            api_key=self._api_key,
            base_url=self._base_url,
            model=self.model,
            config=session_config,
        )
        await self._real_client.connect()
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
    ) -> bool:
        """Ask the active Qwen session to speak a validated text instruction.

        This is an application-to-model control message, not a user utterance
        and not an authoritative training fact.  The caller is responsible
        for bounding and validating ``text`` before it reaches this method.
        """

        text = str(text).strip()
        if not text or not self.connected:
            return False
        if interrupt:
            await self._on_interruption()
        self._active_feedback_id = feedback_id
        self._active_feedback_response_id = None
        await self._client.send_event(build_text_input_event(text))
        await self._client.send_event(build_response_create_event())
        self._schedule_feedback_sink("queued", feedback_id, None)
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
        task = asyncio.create_task(result)
        self._transcript_tasks.add(task)
        task.add_done_callback(self._transcript_tasks.discard)

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
        task = asyncio.create_task(result)
        self._feedback_tasks.add(task)
        task.add_done_callback(self._feedback_tasks.discard)

    async def close(self):
        # Upstream 0.6.9 lets CancelledError skip websocket/executor cleanup.
        if self._processing_task is not None:
            self._processing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._processing_task
            self._processing_task = None
        if self._transcript_tasks:
            await asyncio.gather(*self._transcript_tasks, return_exceptions=True)
            self._transcript_tasks.clear()
        if self._feedback_tasks:
            await asyncio.gather(*self._feedback_tasks, return_exceptions=True)
            self._feedback_tasks.clear()
        self._response_stats.clear()
        self._cancelled_response_ids.clear()
        await super().close()

    async def _process_events(self):
        # Generation IDs prevent late packets from a cancelled response restarting playback.
        audio_done = set()
        async for event in self._client.read():
            kind = event.get("type")
            response_id = event.get("response_id")
            if kind == "error":
                self._emit_error_event(
                    error=Exception(str(event.get("error"))), context="qwen_realtime_api"
                )
            elif kind == "response.created":
                created_id = event.get("response", {}).get("id")
                if self._is_responding and created_id != self._current_response_id:
                    LOGGER.warning(
                        "同一时刻存在多个 Qwen response：previous=%s current=%s",
                        self._current_response_id,
                        created_id,
                    )
                self._current_response_id = created_id
                self._is_responding = True
                if created_id:
                    self._response_stats[created_id] = {
                        "created_at": time.monotonic(),
                        "audio_chunks": 0,
                        "audio_ms": 0.0,
                        "max_delta_gap_ms": 0.0,
                    }
                LOGGER.debug("Qwen response.created id=%s", created_id)
                self._active_feedback_response_id = self._current_response_id
                self._schedule_feedback_sink(
                    "generated", self._active_feedback_id, self._active_feedback_response_id
                )
            elif kind == "response.output_item.added":
                self._current_item_id = event.get("item", {}).get("id")
            elif kind == "input_audio_buffer.speech_started":
                # Also flush when server generation has ended but local playout has not.
                LOGGER.debug(
                    "Qwen speech_started during response=%s responding=%s",
                    self._current_response_id,
                    self._is_responding,
                )
                self._emit_audio_output_done_event(interrupted=True)
                self._emit_user_speech_started()
                await self._on_interruption()
            elif kind == "input_audio_buffer.speech_stopped":
                LOGGER.debug("Qwen speech_stopped")
                self._emit_user_speech_ended()
            elif kind == "response.audio.done":
                done_id = response_id or self._current_response_id
                if done_id not in self._cancelled_response_ids and done_id not in audio_done:
                    self._emit_audio_output_done_event(response_id=done_id)
                    audio_done.add(done_id)
                    LOGGER.debug("Qwen response.audio.done id=%s", done_id)
            elif kind == "response.done":
                done_id = event.get("response", {}).get("id")
                if done_id not in self._cancelled_response_ids:
                    # Older event sequences may omit response.audio.done.
                    if done_id not in audio_done:
                        self._emit_audio_output_done_event(response_id=done_id)
                    self._emit_agent_speech_transcription(text="", mode="final")
                    self._schedule_feedback_sink("completed", self._active_feedback_id, done_id)
                stats = self._response_stats.pop(done_id, None) if done_id else None
                if stats is not None:
                    LOGGER.debug(
                        "Qwen response.done id=%s chunks=%d audio=%.0fms max_delta_gap=%.0fms",
                        done_id,
                        stats["audio_chunks"],
                        stats["audio_ms"],
                        stats["max_delta_gap_ms"],
                    )
                if done_id == self._current_response_id or done_id is None:
                    self._is_responding = False
                    self._current_response_id = None
                    self._current_item_id = None
                    self._active_feedback_id = None
                    self._active_feedback_response_id = None
                self._cancelled_response_ids.discard(done_id)
                audio_done.discard(done_id)
            elif kind == "response.audio.delta":
                if (
                    self._is_responding
                    and response_id not in self._cancelled_response_ids
                    and response_id not in audio_done
                    and response_id in (None, self._current_response_id)
                ):
                    pcm = PcmData.from_bytes(base64.b64decode(event["delta"]), 24000)
                    stats_id = response_id or self._current_response_id
                    if stats_id and (stats := self._response_stats.get(stats_id)) is not None:
                        now = time.monotonic()
                        previous = stats.get("last_delta_at")
                        if isinstance(previous, float):
                            gap_ms = (now - previous) * 1000
                            stats["max_delta_gap_ms"] = max(
                                float(stats["max_delta_gap_ms"]), gap_ms
                            )
                        stats["last_delta_at"] = now
                        stats["audio_chunks"] = int(stats["audio_chunks"]) + 1
                        stats["audio_ms"] = float(stats["audio_ms"]) + pcm.duration_ms
                    self._emit_audio_output_event(
                        pcm=pcm,
                        response_id=response_id,
                    )
            elif kind == "conversation.item.input_audio_transcription.completed":
                if text := event.get("transcript", ""):
                    self._emit_user_speech_transcription(text=text, mode="final")
                    self._schedule_transcript_sink(text)
            elif (
                kind == "response.audio_transcript.delta"
                and response_id not in self._cancelled_response_ids
                and (text := event.get("delta", ""))
            ):
                self._emit_agent_speech_transcription(text=text, mode="delta")

    async def _on_interruption(self):
        if not self._is_responding:
            return
        response_id = self._current_response_id
        feedback_id = self._active_feedback_id
        self._schedule_feedback_sink(
            "interrupted", feedback_id, response_id
        )
        if response_id:
            self._cancelled_response_ids.add(response_id)
        self._is_responding = False
        self._current_response_id = None
        self._current_item_id = None
        self._active_feedback_id = None
        self._active_feedback_response_id = None
        if response_id:
            await self._client.cancel_response()
