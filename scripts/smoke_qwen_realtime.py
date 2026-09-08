#!/usr/bin/env python3
"""Opt-in live smoke test for the verified Qwen Realtime text contract.

The call is disabled by default because it uses a real API key and may incur
cost. Run with ``RUN_QWEN_LIVE_TEST=1`` after the offline unit tests pass.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
import time
from pathlib import Path
from typing import Any

import certifi
import websockets
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coach.qwen_contract import (
    CHINA_BASE_URL,
    QWEN_REALTIME_MODEL,
    QwenRuntimeContract,
    build_response_create_event,
    build_session_update_event,
    build_text_input_event,
)

EXPECTED = "TEXT_OK"


def event_id(prefix: str) -> str:
    return f"{prefix}_{time.time_ns()}"


async def send(ws: Any, event: dict[str, Any]) -> None:
    payload = {"event_id": event_id(event["type"].replace(".", "_")), **event}
    await ws.send(json.dumps(payload, ensure_ascii=False))


async def receive_until(ws: Any, terminal: set[str], timeout: float) -> tuple[list[str], str]:
    seen: list[str] = []
    text_parts: list[str] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"Qwen event timeout; seen={seen}")
        event = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        event_type = event.get("type", "<missing>")
        seen.append(event_type)
        if event_type == "error":
            error = event.get("error", {})
            raise RuntimeError(
                f"Qwen error {error.get('code', '<unknown>')}: "
                f"{error.get('message', '<no message>')}"
            )
        if event_type == "response.text.delta":
            text_parts.append(event.get("delta", ""))
        if event_type in terminal:
            return seen, "".join(text_parts)


async def run() -> None:
    load_dotenv(ROOT / ".env")
    if os.getenv("RUN_QWEN_LIVE_TEST") != "1":
        print("SKIP: set RUN_QWEN_LIVE_TEST=1 to enable the live API test")
        return

    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key or api_key == "your_dashscope_api_key_here":
        raise SystemExit("DASHSCOPE_API_KEY is not configured")

    contract = QwenRuntimeContract(
        model=os.getenv("QWEN_REALTIME_MODEL", QWEN_REALTIME_MODEL),
        base_url=os.getenv("DASHSCOPE_BASE_URL", CHINA_BASE_URL),
    )
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    async with websockets.connect(
        contract.websocket_url(),
        additional_headers={"Authorization": f"Bearer {api_key}"},
        ssl=ssl_context,
        open_timeout=15,
    ) as ws:
        await receive_until(ws, {"session.created"}, 15)
        await send(
            ws,
            build_session_update_event(
                "Reply exactly as requested. This is a protocol smoke test.",
                modalities=("text",),
                turn_detection=None,
            ),
        )
        await receive_until(ws, {"session.updated"}, 15)
        await send(ws, build_text_input_event(f"Reply only {EXPECTED}"))
        await receive_until(ws, {"conversation.item.created"}, 15)
        await send(ws, build_response_create_event())
        seen, response_text = await receive_until(ws, {"response.done"}, 30)

    if response_text.strip() != EXPECTED:
        raise RuntimeError(f"unexpected response: {response_text!r}; events={seen}")
    print(
        json.dumps(
            {
                "status": "PASS",
                "model": contract.model,
                "endpoint": contract.base_url,
                "text": response_text.strip(),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(run())
