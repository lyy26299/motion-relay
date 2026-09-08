"""Pinned Qwen Realtime protocol contract used by Scheme A.

This module intentionally has no network or Vision-Agents imports. It gives
the deterministic coaching runtime a testable boundary while the upstream
Vision-Agents Qwen adapter still lacks ``input_text`` support.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

VISION_AGENTS_VERSION = "0.6.9"
QWEN_REALTIME_MODEL = "qwen3.5-omni-plus-realtime"
CHINA_BASE_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"

SUPPORTED_REALTIME_INPUTS = frozenset({"audio", "image", "text"})
UNSUPPORTED_NATIVE_INPUTS = frozenset({"pdf", "docx"})


class QwenContractError(ValueError):
    """Raised before an invalid event can be sent to Qwen Realtime."""


@dataclass(frozen=True, slots=True)
class QwenRuntimeContract:
    """The exact backend/adapter combination verified by this project."""

    model: str = QWEN_REALTIME_MODEL
    base_url: str = CHINA_BASE_URL
    vision_agents_version: str = VISION_AGENTS_VERSION

    def websocket_url(self) -> str:
        separator = "&" if "?" in self.base_url else "?"
        return f"{self.base_url}{separator}model={self.model}"


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise QwenContractError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise QwenContractError(f"{field} must not be empty")
    return normalized


def build_text_input_event(text: str) -> dict[str, Any]:
    """Build the verified ``message + input_text`` client event."""

    return {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": _required_text(text, "text")}],
        },
    }


def build_response_create_event() -> dict[str, str]:
    """Request one response after the current turn inputs are complete."""

    return {"type": "response.create"}


def build_session_update_event(
    instructions: str,
    *,
    voice: str = "Ethan",
    modalities: Sequence[str] = ("text", "audio"),
    turn_detection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the session envelope shared by the adapter and live smoke test."""

    normalized_modalities = list(dict.fromkeys(modalities))
    if not normalized_modalities or not set(normalized_modalities) <= {"text", "audio"}:
        raise QwenContractError("modalities must contain only 'text' and/or 'audio'")

    session: dict[str, Any] = {
        "modalities": normalized_modalities,
        "instructions": _required_text(instructions, "instructions"),
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm24",
        "turn_detection": dict(turn_detection) if turn_detection is not None else None,
    }
    if "audio" in normalized_modalities:
        session["voice"] = _required_text(voice, "voice")
    return {"type": "session.update", "session": session}
