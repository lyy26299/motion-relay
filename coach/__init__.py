"""Deterministic runtime contracts for the local coaching agent."""

from .qwen_contract import (
    CHINA_BASE_URL,
    QWEN_REALTIME_MODEL,
    VISION_AGENTS_VERSION,
    build_response_create_event,
    build_session_update_event,
    build_text_input_event,
)

__all__ = [
    "CHINA_BASE_URL",
    "QWEN_REALTIME_MODEL",
    "VISION_AGENTS_VERSION",
    "build_response_create_event",
    "build_session_update_event",
    "build_text_input_event",
]
