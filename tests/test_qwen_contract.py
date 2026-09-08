from __future__ import annotations

import unittest
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vision_agents.core.instructions import Instructions

from agent_local_agent import qwen_vad_settings, session_instructions
from coach.qwen_contract import (
    CHINA_BASE_URL,
    QWEN_REALTIME_MODEL,
    SUPPORTED_REALTIME_INPUTS,
    UNSUPPORTED_NATIVE_INPUTS,
    VISION_AGENTS_VERSION,
    QwenContractError,
    QwenRuntimeContract,
    build_response_create_event,
    build_session_update_event,
    build_text_input_event,
)

ROOT = Path(__file__).resolve().parents[1]


class QwenContractTests(unittest.TestCase):
    def test_verified_runtime_version_is_installed(self) -> None:
        self.assertEqual(version("vision-agents"), VISION_AGENTS_VERSION)

    def test_runtime_contract_builds_china_websocket_url(self) -> None:
        contract = QwenRuntimeContract()
        self.assertEqual(contract.model, QWEN_REALTIME_MODEL)
        self.assertEqual(contract.base_url, CHINA_BASE_URL)
        self.assertEqual(
            contract.websocket_url(),
            f"{CHINA_BASE_URL}?model={QWEN_REALTIME_MODEL}",
        )

    def test_verified_input_capabilities_are_explicit(self) -> None:
        self.assertEqual(SUPPORTED_REALTIME_INPUTS, {"audio", "image", "text"})
        self.assertEqual(UNSUPPORTED_NATIVE_INPUTS, {"pdf", "docx"})
        self.assertTrue(SUPPORTED_REALTIME_INPUTS.isdisjoint(UNSUPPORTED_NATIVE_INPUTS))

    def test_text_event_matches_verified_envelope(self) -> None:
        self.assertEqual(
            build_text_input_event("  REP_COMPLETED: 3  "),
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "REP_COMPLETED: 3"}],
                },
            },
        )

    def test_empty_text_is_rejected_before_network_io(self) -> None:
        with self.assertRaises(QwenContractError):
            build_text_input_event("  ")

    def test_session_event_and_response_event_are_stable(self) -> None:
        event = build_session_update_event(
            "coach contract test",
            modalities=("text",),
            turn_detection=None,
        )
        self.assertEqual(event["type"], "session.update")
        self.assertEqual(event["session"]["modalities"], ["text"])
        self.assertNotIn("voice", event["session"])
        self.assertIsNone(event["session"]["turn_detection"])
        self.assertEqual(build_response_create_event(), {"type": "response.create"})

    def test_instruction_reference_exists_and_contains_runtime_rules(self) -> None:
        instruction_file = ROOT / "docs" / "COACHING_INSTRUCTIONS.md"
        self.assertTrue(instruction_file.is_file())
        text = session_instructions(SimpleNamespace(exercise="深蹲", target_reps=10))
        self.assertIn("@docs/COACHING_INSTRUCTIONS.md", text)
        self.assertIn("深蹲", text)
        self.assertIn("10 次", text)
        resolved = Instructions(input_text=text, base_dir=ROOT).full_reference
        self.assertIn("# AI 健身教练 & 交互感知助手指令", resolved)
        self.assertIn("只反馈你确定看到的", resolved)

    def test_vad_environment_is_validated(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "QWEN_VAD_TYPE": "semantic_vad",
                "QWEN_VAD_THRESHOLD": "0.2",
                "QWEN_VAD_SILENCE_MS": "1200",
            },
        ):
            self.assertEqual(qwen_vad_settings(), ("semantic_vad", 0.2, 1200))

        for name, value in (
            ("QWEN_VAD_TYPE", "manual"),
            ("QWEN_VAD_THRESHOLD", "nan"),
            ("QWEN_VAD_SILENCE_MS", "199"),
            ("QWEN_VAD_SILENCE_MS", "900.5"),
        ):
            with self.subTest(name=name, value=value):
                with patch.dict("os.environ", {name: value}, clear=False):
                    with self.assertRaises(ValueError):
                        qwen_vad_settings()


if __name__ == "__main__":
    unittest.main()
