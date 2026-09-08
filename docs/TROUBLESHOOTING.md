# Troubleshooting Guide

The current desktop entry point is `agent_local.py`, not the historical
`vision_agent_demo.py` Stream/Gemini example.

macOS/Linux:

```bash
./run.sh
```

Windows: `scripts\\run.bat`.

Check the local environment without exposing secrets:

```bash
.venv/bin/python scripts/check_local_setup.py
```

There is no Docker stack, separate frontend build, or always-on web service.
The desktop controller is Tkinter; BrowserEdge serves a temporary local browser
page for each session. The MCP server is an optional, separate stdio diagnostic
process.

Offline checks:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/smoke_pose.py --device cpu
.venv/bin/python scripts/smoke_browser_aec.py --pose --pose-device cpu
```

The active runtime uses BrowserEdge WebRTC for browser camera/microphone/AEC,
YOLO Pose for local observations, and Qwen Realtime through DashScope. The
desktop session wires YOLO/FSM/working memory to the asynchronous SQLite ledger.
MCP client calls, Qwen tool invocation, and cross-session RAG answers are still
staged work; the offline MCP/retrieval/agent-loop contracts do not mean those
features are active in the desktop path.

## Common Issues

### `DASHSCOPE_API_KEY` is missing

Create `.env` from `.env.example` and set the key. Do not print or commit the
value. The setup checker only reports whether it is configured.

### The browser does not open

Copy the one-time local URL printed in the terminal into Chromium. It normally
looks like `http://127.0.0.1:PORT/?token=...`; do not share it.

### Camera or microphone access is denied

Allow both permissions for the local BrowserEdge page, close other applications
using the devices, refresh the page, and retry. The server rejects an offer if
the browser does not confirm AEC.

### YOLO model download or detection fails

Run `.venv/bin/python scripts/smoke_pose.py --device cpu`. If download fails,
place `yolo11n-pose.pt` in the repository root and retry. Set `YOLO_DEVICE=cpu`
in `.env` for compatibility.

### No voice feedback or Qwen returns 401/403/429

Confirm that the DashScope account has Qwen Realtime access and that
`DASHSCOPE_BASE_URL`, `QWEN_REALTIME_MODEL`, and `QWEN_VOICE` are correct. Check
browser volume and playback permissions. The offline media smoke test can
separate local WebRTC problems from cloud configuration problems.

### The coach cannot see a movement

Keep one person fully in frame with adequate light; a side or three-quarter view
is best for squat knee angles. Multiple people, low confidence, missing points,
or stale frames intentionally pause observation and counting. Authoritative
counts come from the local YOLO/FSM path, not from Qwen prose.

### The process is stuck or does not close cleanly

Use the desktop Stop control first. If needed, press `Ctrl+C`, wait for cleanup,
and restart `./run.sh`. Do not run multiple instances against one camera.

### CPU usage is high

Use the nano model and set `YOLO_DEVICE=cpu` or `mps` in `.env`. Raising the Qwen
video FPS does not improve authoritative counting.

### Inspect the memory ledger or MCP tools

Set `COACH_USER_ID` and `COACH_MEMORY_DB` in `.env` when you need an isolated
profile or a different database location. The optional stdio server waits for an
MCP client and is not required for the desktop UI:

```bash
.venv/bin/python -m coach.mcp_server --db coach_memory.sqlite3 --user-id local-user
```

## Debug Logging

```bash
LOG_LEVEL=DEBUG .venv/bin/python agent_local.py
```

Logs should contain session state and errors only; never redirect `.env` into a
log file.
