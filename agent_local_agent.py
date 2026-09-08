"""Agent runtime for the local Vision Coach desktop application."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import ssl
import uuid
from collections.abc import Callable
from typing import Any, Protocol

import certifi
from dotenv import load_dotenv

from coach.memory.consolidate import consolidate_session
from coach.memory.store import MemoryStore
from coach.memory.writer import LedgerWriter
from coach.models import MotionSnapshot, PoseSnapshot
from coach.qwen_contract import CHINA_BASE_URL, QWEN_REALTIME_MODEL
from coach.runtime import MotionRuntime
from coach.session_agent import SessionAgentBridge
from coach.working_memory import WorkingMemory

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
load_dotenv()

try:
    import aiohttp.connector

    cached_context = aiohttp.connector._SSL_CONTEXT_VERIFIED
    if not cached_context.get_ca_certs():
        aiohttp.connector._SSL_CONTEXT_VERIFIED = ssl.create_default_context(
            cafile=os.environ["SSL_CERT_FILE"]
        )
except (ImportError, AttributeError, OSError):
    pass


LOGGER = logging.getLogger("vision_coach")


class SessionSettings(Protocol):
    exercise: str
    target_reps: int


class CoachUI(Protocol):
    alive: bool

    @property
    def state(self) -> str: ...

    def set_state(self, state: str, detail: str = "") -> None: ...

    def submit_frame(self, frame: Any) -> None: ...

    def begin_pose_session(self, session_id: str) -> None: ...

    def submit_pose(self, snapshot: PoseSnapshot) -> None: ...

    def submit_motion(self, motion: MotionSnapshot) -> None: ...

    def submit_memory_status(self, status: str, detail: str = "") -> None: ...

    def append_log(self, text: str, tag: str = "normal") -> None: ...


def make_dashboard_edge(settings: SessionSettings, frame_sink: Callable[[Any], None]) -> Any:
    """Browser owns device I/O; processed video stays in the dashboard."""
    from coach.browser_edge import BrowserEdge

    return BrowserEdge(
        frame_sink=frame_sink,
        audio_start_buffer_ms=_env_float(
            "COACH_AUDIO_START_BUFFER_MS", 160.0, minimum=20.0, maximum=2000.0
        ),
        audio_resume_buffer_ms=_env_float(
            "COACH_AUDIO_REBUFFER_MS", 200.0, minimum=20.0, maximum=2000.0
        ),
        audio_max_buffer_ms=_env_float(
            "COACH_AUDIO_MAX_BUFFER_MS", 1000.0, minimum=20.0, maximum=5000.0
        ),
    )


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum:g}-{maximum:g} 之间")
    return value


def qwen_vad_settings() -> tuple[str, float, int]:
    """Return a validated Qwen 3.5 turn-detection configuration."""

    vad_type = os.getenv("QWEN_VAD_TYPE", "semantic_vad").strip()
    if vad_type not in {"server_vad", "semantic_vad"}:
        raise ValueError("QWEN_VAD_TYPE 必须是 server_vad 或 semantic_vad")
    threshold = _env_float("QWEN_VAD_THRESHOLD", 0.2, minimum=-1.0, maximum=1.0)
    silence_ms = _env_float("QWEN_VAD_SILENCE_MS", 900.0, minimum=200.0, maximum=6000.0)
    if not silence_ms.is_integer():
        raise ValueError("QWEN_VAD_SILENCE_MS 必须是整数")
    return vad_type, threshold, int(silence_ms)


def session_instructions(settings: SessionSettings) -> str:
    return (
        "Read @docs/COACHING_INSTRUCTIONS.md\n\n"
        f"本次训练项目：{settings.exercise}。目标：{settings.target_reps} 次。"
        "优先观察这个动作，清晰计数，并只在必要时给出一句纠正。"
        "实时动作反馈严格限制为一个不超过 20 个汉字的短句，不要列点或连续补充。"
    )


def valid_dashscope_key() -> bool:
    value = os.getenv("DASHSCOPE_API_KEY", "").strip()
    return bool(value and value != "your_dashscope_api_key_here")


class SessionController:
    """Own the Vision Agent lifecycle without depending on Tkinter."""

    def __init__(self, ui: CoachUI) -> None:
        self.ui = ui
        self.task: asyncio.Task[None] | None = None
        self.stop_event: asyncio.Event | None = None
        self.user_id = os.getenv("COACH_USER_ID", "local-user").strip() or "local-user"
        self.memory_db = (
            os.getenv("COACH_MEMORY_DB", "coach_memory.sqlite3").strip() or "coach_memory.sqlite3"
        )
        self.session_id: str | None = None
        self.session_epoch: int | None = None
        self.working_memory: WorkingMemory | None = None
        self.memory_store: MemoryStore | None = None
        self.ledger_writer: LedgerWriter | None = None
        self.motion_runtime: MotionRuntime | None = None
        self.session_agent: SessionAgentBridge | None = None
        self._last_pose_at: float | None = None
        self._watchdog_task: asyncio.Task[None] | None = None

    def start(self, settings: SessionSettings) -> None:
        if self.task is not None and not self.task.done():
            return
        self.stop_event = asyncio.Event()
        self.task = asyncio.create_task(self._run(settings), name="local-fitness-session")

    def stop(self) -> None:
        if self.stop_event is not None:
            self.ui.set_state("stopping")
            self.stop_event.set()

    async def shutdown(self) -> None:
        if self.task is not None and not self.task.done():
            self.stop()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self.task, timeout=8)

    def _memory_status(self, status: str, detail: str = "") -> None:
        callback = getattr(self.ui, "submit_memory_status", None)
        if callable(callback):
            with contextlib.suppress(Exception):
                callback(status, detail)

    def _submit_motion(self, motion: MotionSnapshot) -> None:
        callback = getattr(self.ui, "submit_motion", None)
        if callable(callback):
            with contextlib.suppress(Exception):
                callback(motion)

    def _prepare_memory(self, *, session_id: str, settings: SessionSettings) -> None:
        """Create the per-session local state before opening media input."""

        self.session_id = session_id
        self.memory_store = MemoryStore(self.memory_db)
        self.memory_store.ensure_user(self.user_id)
        self.session_epoch = self.memory_store.get_memory_epoch(self.user_id)
        self.working_memory = WorkingMemory(session_id=session_id)
        exercise = "squat" if settings.exercise in {"深蹲", "squat"} else settings.exercise
        self.ledger_writer = LedgerWriter(
            path=self.memory_db,
            user_id=self.user_id,
            session_id=session_id,
            exercise=exercise,
            status_sink=self._memory_status,
        )
        self.ledger_writer.start_and_wait()
        self.motion_runtime = MotionRuntime(
            session_id=session_id,
            memory=self.working_memory,
        )
        self._last_pose_at = None

    def _submit_pose(self, snapshot: PoseSnapshot) -> None:
        """Route one immutable pose snapshot to UI, local facts and the ledger."""

        if self.session_id != snapshot.session_id:
            return
        self._last_pose_at = snapshot.observed_at
        with contextlib.suppress(Exception):
            self.ui.submit_pose(snapshot)
        runtime = self.motion_runtime
        if runtime is None:
            return
        try:
            motion, events, reps = runtime.ingest(snapshot)
            self._submit_motion(motion)
            writer = self.ledger_writer
            if writer is not None and not writer.submit(events=events, reps=reps):
                LOGGER.error("训练事实未能进入账本队列")
        except Exception:
            LOGGER.exception("处理姿态事实失败")

    async def _watchdog_loop(self) -> None:
        """Pause the local FSM when frames stop, independent of new callbacks."""

        while self.stop_event is not None and not self.stop_event.is_set():
            await asyncio.sleep(0.25)
            runtime = self.motion_runtime
            if runtime is None:
                continue
            events = runtime.watchdog(timeout_s=1.5)
            if not events:
                continue
            self._submit_motion(runtime.fsm_snapshot())
            writer = self.ledger_writer
            if writer is not None and not writer.submit(events=events):
                LOGGER.error("watchdog 事实未能进入账本队列")

    async def _finish_memory(self, *, status: str) -> None:
        session_agent = self.session_agent
        self.session_agent = None
        if session_agent is not None:
            with contextlib.suppress(Exception):
                await session_agent.close()
        watchdog = self._watchdog_task
        self._watchdog_task = None
        if watchdog is not None:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
        writer = self.ledger_writer
        self.ledger_writer = None
        if writer is not None:
            try:
                await asyncio.to_thread(writer.close, status=status, timeout=8)
                if writer.status != "closed" or writer.error is not None:
                    raise RuntimeError("账本未确认排空，未生成训练摘要")
                summary = await asyncio.to_thread(
                    consolidate_session,
                    writer.path,
                    writer.user_id,
                    writer.session_id,
                    exercise=writer.exercise,
                )
                if summary is None:
                    raise RuntimeError("未找到已结束训练，未生成训练摘要")
            except Exception:
                LOGGER.exception("保存训练摘要失败")
                self._memory_status("failed", "训练摘要未保存，请检查账本状态")
        store = self.memory_store
        self.memory_store = None
        if store is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(store.close)
        self.motion_runtime = None
        self.working_memory = None
        self.session_id = None
        self.session_epoch = None
        self._last_pose_at = None

    async def _run(self, settings: SessionSettings) -> None:
        agent = None
        processor = None
        edge = None
        waiters = []
        session_status = "completed"
        memory_ready = False
        try:
            if not valid_dashscope_key():
                raise RuntimeError("请先在 .env 中配置 DASHSCOPE_API_KEY")

            self.ui.set_state("starting", "正在加载姿态模型")
            LOGGER.info("正在加载 YOLO 姿态模型...")

            from vision_agents.core import Agent, User

            from coach.pose_adapter import StructuredPoseProcessor
            from coach.qwen_duplex import DuplexQwenRealtime

            call_id = f"local-{uuid.uuid4().hex[:8]}"
            await asyncio.to_thread(self._prepare_memory, session_id=call_id, settings=settings)
            memory_ready = True
            self.ui.begin_pose_session(call_id)
            processor = await asyncio.to_thread(
                StructuredPoseProcessor,
                session_id=call_id,
                snapshot_sink=self._submit_pose,
                model_path="yolo11n-pose.pt",
                device=os.getenv("YOLO_DEVICE", "mps"),
                fps=10,
            )
            if self.stop_event is None or self.stop_event.is_set():
                return

            self._watchdog_task = asyncio.create_task(
                self._watchdog_loop(), name="coach-motion-watchdog"
            )

            self.ui.set_state("starting", "正在连接实时教练")
            edge = make_dashboard_edge(settings, self.ui.submit_frame)
            vad_type, vad_threshold, vad_silence_ms = qwen_vad_settings()
            qwen = DuplexQwenRealtime(
                model=os.getenv("QWEN_REALTIME_MODEL", QWEN_REALTIME_MODEL),
                fps=1,
                include_video=True,
                base_url=os.getenv(
                    "DASHSCOPE_BASE_URL",
                    CHINA_BASE_URL,
                ),
                voice=os.getenv("QWEN_VOICE", "Ethan"),
                vad_type=vad_type,
                vad_threshold=vad_threshold,
                vad_silence_duration_ms=vad_silence_ms,
            )
            if self.memory_store is None or self.working_memory is None or self.motion_runtime is None:
                raise RuntimeError("训练记忆尚未准备好")
            self.session_agent = SessionAgentBridge(
                user_id=self.user_id,
                session_id=call_id,
                memory=self.working_memory,
                store=self.memory_store,
                session_epoch=self.session_epoch or 0,
                motion_runtime=self.motion_runtime,
                qwen=qwen,
                log=lambda message: LOGGER.info(message),
            )
            qwen.user_transcript_sink = self.session_agent.on_user_transcript
            qwen.feedback_sink = self.session_agent.feedback_state
            agent = Agent(
                edge=edge,
                agent_user=User(name="AI 健身教练"),
                instructions=session_instructions(settings),
                llm=qwen,
                processors=[processor],
            )

            call = await agent.create_call("local", call_id)
            async with agent.join(call, participant_wait_timeout=0):
                self.ui.set_state("starting", "等待浏览器授权")
                LOGGER.info("请打开浏览器链接并连接音视频设备：%s", edge.url)
                await asyncio.to_thread(edge.open_demo)
                stop_wait = asyncio.create_task(self.stop_event.wait())
                end_wait = asyncio.create_task(edge.finished.wait())
                ready_wait = asyncio.create_task(edge.connected.wait())
                waiters = [stop_wait, end_wait, ready_wait]
                done, _ = await asyncio.wait(
                    waiters, timeout=120, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    raise RuntimeError("等待浏览器授权超时，请重新开始训练")
                if stop_wait in done or end_wait in done:
                    if edge.failure:
                        raise RuntimeError(edge.failure)
                    return
                self.ui.set_state("running", f"{settings.exercise} · {settings.target_reps} 次")
                LOGGER.info("训练已开始：浏览器全双工 AEC，请保持全身位于画面内。")
                await asyncio.wait([stop_wait, end_wait], return_when=asyncio.FIRST_COMPLETED)
                if edge.failure:
                    raise RuntimeError(edge.failure)
        except asyncio.CancelledError:
            session_status = "interrupted"
            raise
        except Exception as exc:
            session_status = "interrupted"
            LOGGER.exception("训练启动失败")
            self.ui.set_state("error", "启动失败")
            self.ui.append_log(str(exc), "error")
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            if agent is not None:
                with contextlib.suppress(Exception):
                    await agent.close()
            elif processor is not None:
                with contextlib.suppress(Exception):
                    await processor.close()
            if edge is not None:
                with contextlib.suppress(Exception):
                    await edge.close()
            if memory_ready or self.memory_store is not None or self.ledger_writer is not None:
                await self._finish_memory(status=session_status)
            if self.ui.alive and self.ui.state != "error":
                self.ui.set_state("idle", "待命")
                LOGGER.info("训练已停止。")
