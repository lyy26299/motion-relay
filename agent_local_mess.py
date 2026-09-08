"""Local desktop interface for the real-time Vision Agent fitness coach."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import queue
import shutil
import ssl
import sys
import tkinter as tk
import uuid
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any, Callable

import certifi
from dotenv import load_dotenv


# Configure certificate discovery before any SDK networking modules are loaded.
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


@dataclass(frozen=True)
class DeviceCatalog:
    audio_inputs: list[Any]
    audio_outputs: list[Any]
    cameras: list[Any]


@dataclass(frozen=True)
class SessionSettings:
    audio_input: Any
    audio_output: Any
    camera: Any
    exercise: str
    target_reps: int


class UiLogHandler(logging.Handler):
    """Move log records into Tk's main-thread update loop."""

    def __init__(self, target: queue.Queue[str]) -> None:
        super().__init__()
        self.target = target

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.target.put_nowait(self.format(record))
        except (Exception, queue.Full):
            self.handleError(record)


class FitnessCoachUI:
    BG = "#0C0E10"
    PANEL = "#15191D"
    PANEL_ALT = "#1C2126"
    BORDER = "#2C333A"
    TEXT = "#F4F6F8"
    MUTED = "#919AA3"
    ACCENT = "#FF6B35"
    ACCENT_ACTIVE = "#E85A28"
    GREEN = "#57D39B"
    BLUE = "#63A7FF"
    DANGER = "#FF6B6B"

    EXERCISES = ("深蹲", "俯卧撑", "平板支撑", "自由训练")

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.alive = True
        self.devices: DeviceCatalog | None = None
        self.actions: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.logs: queue.Queue[str] = queue.Queue(maxsize=400)
        self.frames: queue.Queue[Any] = queue.Queue(maxsize=2)
        self._photo: tk.PhotoImage | None = None
        self._exercise = self.EXERCISES[0]
        self._session_started_at: float | None = None
        self._state = "idle"

        self._configure_window()
        self._build_styles()
        self._build_layout()
        self._bind_events()

    def _configure_window(self) -> None:
        self.root.title("Vision Coach | 本地训练台")
        self.root.geometry("1180x780")
        self.root.minsize(980, 680)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        available = set(tkfont.families(self.root))
        self.font_family = "PingFang SC" if "PingFang SC" in available else "Arial"

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        with contextlib.suppress(tk.TclError):
            style.theme_use("clam")
        style.configure(
            "Coach.TCombobox",
            fieldbackground=self.PANEL_ALT,
            background=self.PANEL_ALT,
            foreground=self.TEXT,
            arrowcolor=self.MUTED,
            bordercolor=self.BORDER,
            lightcolor=self.BORDER,
            darkcolor=self.BORDER,
            padding=8,
            font=(self.font_family, 13),
        )
        style.map(
            "Coach.TCombobox",
            fieldbackground=[("readonly", self.PANEL_ALT)],
            foreground=[("readonly", self.TEXT)],
            selectbackground=[("readonly", self.PANEL_ALT)],
            selectforeground=[("readonly", self.TEXT)],
        )
        self.root.option_add("*TCombobox*Listbox.background", self.PANEL_ALT)
        self.root.option_add("*TCombobox*Listbox.foreground", self.TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", self.ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", self.TEXT)

    def _build_layout(self) -> None:
        shell = tk.Frame(self.root, bg=self.BG)
        shell.pack(fill="both", expand=True, padx=24, pady=(20, 22))
        shell.grid_columnconfigure(0, weight=1, minsize=580)
        shell.grid_columnconfigure(1, weight=0, minsize=332)
        shell.grid_rowconfigure(1, weight=1)

        self._build_header(shell)
        self._build_camera_panel(shell)
        self._build_control_panel(shell)

    def _build_header(self, parent: tk.Widget) -> None:
        header = tk.Frame(parent, bg=self.BG, height=58)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 18))
        header.grid_columnconfigure(0, weight=1)

        brand = tk.Frame(header, bg=self.BG)
        brand.grid(row=0, column=0, sticky="w")
        tk.Label(
            brand,
            text="VISION COACH",
            bg=self.BG,
            fg=self.TEXT,
            font=(self.font_family, 20, "bold"),
        ).pack(anchor="w")
        tk.Label(
            brand,
            text="本地实时姿态训练",
            bg=self.BG,
            fg=self.MUTED,
            font=(self.font_family, 13),
        ).pack(anchor="w", pady=(2, 0))

        status_box = tk.Frame(header, bg=self.PANEL_ALT, padx=14, pady=8)
        status_box.grid(row=0, column=1, sticky="e")
        self.status_dot = tk.Label(
            status_box,
            text="●",
            bg=self.PANEL_ALT,
            fg=self.MUTED,
            font=(self.font_family, 12),
        )
        self.status_dot.pack(side="left", padx=(0, 8))
        self.status_label = tk.Label(
            status_box,
            text="正在检测设备",
            bg=self.PANEL_ALT,
            fg=self.TEXT,
            font=(self.font_family, 13, "bold"),
        )
        self.status_label.pack(side="left")

    def _build_camera_panel(self, parent: tk.Widget) -> None:
        left = tk.Frame(
            parent,
            bg=self.PANEL,
            highlightthickness=1,
            highlightbackground=self.BORDER,
        )
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 16))
        left.grid_rowconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=0)
        left.grid_columnconfigure(0, weight=1)

        self.video_canvas = tk.Canvas(
            left,
            bg="#070809",
            bd=0,
            highlightthickness=0,
            cursor="crosshair",
        )
        self.video_canvas.grid(row=0, column=0, sticky="nsew")
        self.video_image_id = self.video_canvas.create_image(0, 0, anchor="center")
        self.video_title_id = self.video_canvas.create_text(
            0,
            0,
            text="准备开始训练",
            fill=self.TEXT,
            font=(self.font_family, 18, "bold"),
        )
        self.video_hint_id = self.video_canvas.create_text(
            0,
            0,
            text="选择设备后，点击右侧开始训练",
            fill=self.MUTED,
            font=(self.font_family, 13),
        )
        self.video_canvas.bind("<Configure>", self._position_video_placeholder)

        footer = tk.Frame(left, bg=self.PANEL, height=54)
        footer.grid(row=1, column=0, sticky="ew")
        footer.pack_propagate(False)
        self.elapsed_label = tk.Label(
            footer,
            text="00:00",
            bg=self.PANEL,
            fg=self.TEXT,
            font=("Menlo", 14, "bold"),
        )
        self.elapsed_label.pack(side="left", padx=16)
        self.frame_status_label = tk.Label(
            footer,
            text="等待视频流",
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 13),
        )
        self.frame_status_label.pack(side="left", padx=(6, 0))
        tk.Label(
            footer,
            text="YOLO POSE · 17 KEYPOINTS",
            bg=self.PANEL,
            fg=self.GREEN,
            font=(self.font_family, 12, "bold"),
        ).pack(side="right", padx=16)

    def _build_control_panel(self, parent: tk.Widget) -> None:
        side = tk.Frame(parent, bg=self.BG, width=332)
        side.grid(row=1, column=1, sticky="nsew")
        side.grid_propagate(False)
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(2, weight=1)

        settings = tk.Frame(
            side,
            bg=self.PANEL,
            highlightthickness=1,
            highlightbackground=self.BORDER,
            padx=16,
            pady=16,
        )
        settings.grid(row=0, column=0, sticky="ew")
        settings.grid_columnconfigure(0, weight=1)

        self._section_title(settings, "训练项目").grid(row=0, column=0, sticky="w")
        modes = tk.Frame(settings, bg=self.PANEL)
        modes.grid(row=1, column=0, sticky="ew", pady=(10, 16))
        modes.grid_columnconfigure((0, 1), weight=1, uniform="mode")
        self.exercise_buttons: dict[str, tk.Button] = {}
        for index, exercise in enumerate(self.EXERCISES):
            button = tk.Button(
                modes,
                text=exercise,
                command=lambda value=exercise: self._select_exercise(value),
                bd=0,
                relief="flat",
                cursor="hand2",
                padx=8,
                pady=9,
                font=(self.font_family, 13, "bold"),
                activeforeground=self.TEXT,
            )
            button.grid(
                row=index // 2,
                column=index % 2,
                sticky="ew",
                padx=(0 if index % 2 == 0 else 4, 4 if index % 2 == 0 else 0),
                pady=(0, 6),
            )
            self.exercise_buttons[exercise] = button
        self._refresh_exercise_buttons()

        target_row = tk.Frame(settings, bg=self.PANEL)
        target_row.grid(row=2, column=0, sticky="ew", pady=(0, 16))
        target_row.grid_columnconfigure(0, weight=1)
        tk.Label(
            target_row,
            text="目标次数",
            bg=self.PANEL,
            fg=self.TEXT,
            font=(self.font_family, 13),
        ).grid(row=0, column=0, sticky="w")
        self.target_var = tk.IntVar(value=12)
        self.target_spinbox = tk.Spinbox(
            target_row,
            from_=1,
            to=99,
            width=5,
            justify="center",
            textvariable=self.target_var,
            bg=self.PANEL_ALT,
            fg=self.TEXT,
            buttonbackground=self.PANEL_ALT,
            insertbackground=self.TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor=self.ACCENT,
            font=(self.font_family, 13, "bold"),
        )
        self.target_spinbox.grid(row=0, column=1, sticky="e", ipady=6)

        self._section_title(settings, "本地设备").grid(row=3, column=0, sticky="w")
        self.input_combo = self._device_combo(settings, 4, "麦克风")
        self.output_combo = self._device_combo(settings, 5, "扬声器")
        self.camera_combo = self._device_combo(settings, 6, "摄像头")

        self.refresh_button = tk.Button(
            settings,
            text="重新检测设备",
            command=lambda: self.actions.put(("refresh", None)),
            bg=self.PANEL,
            fg=self.BLUE,
            activebackground=self.PANEL,
            activeforeground=self.TEXT,
            bd=0,
            relief="flat",
            cursor="hand2",
            anchor="w",
            font=(self.font_family, 12, "bold"),
        )
        self.refresh_button.grid(row=7, column=0, sticky="w", pady=(6, 0))

        self.primary_button = tk.Button(
            side,
            text="正在检测设备...",
            command=self._primary_action,
            bg=self.PANEL_ALT,
            fg=self.MUTED,
            activebackground=self.ACCENT_ACTIVE,
            activeforeground=self.TEXT,
            disabledforeground=self.MUTED,
            bd=0,
            relief="flat",
            cursor="arrow",
            pady=13,
            state="disabled",
            font=(self.font_family, 14, "bold"),
        )
        self.primary_button.grid(row=1, column=0, sticky="ew", pady=12)

        activity = tk.Frame(
            side,
            bg=self.PANEL,
            highlightthickness=1,
            highlightbackground=self.BORDER,
            padx=14,
            pady=14,
        )
        activity.grid(row=2, column=0, sticky="nsew")
        activity.grid_rowconfigure(1, weight=1)
        activity.grid_columnconfigure(0, weight=1)
        self._section_title(activity, "运行记录").grid(row=0, column=0, sticky="w", pady=(0, 8))

        self.log_text = tk.Text(
            activity,
            bg=self.PANEL,
            fg=self.MUTED,
            insertbackground=self.TEXT,
            selectbackground=self.BORDER,
            bd=0,
            highlightthickness=0,
            wrap="word",
            state="disabled",
            font=(self.font_family, 12),
            spacing1=2,
            spacing3=5,
        )
        self.log_text.grid(row=1, column=0, sticky="nsew")
        self.log_text.tag_configure("error", foreground=self.DANGER)
        self.log_text.tag_configure("success", foreground=self.GREEN)
        self.log_text.tag_configure("normal", foreground=self.MUTED)

    def _section_title(self, parent: tk.Widget, text: str) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            bg=self.PANEL,
            fg=self.TEXT,
            font=(self.font_family, 14, "bold"),
        )

    def _device_combo(self, parent: tk.Widget, row: int, label: str) -> ttk.Combobox:
        wrapper = tk.Frame(parent, bg=self.PANEL)
        wrapper.grid(row=row, column=0, sticky="ew", pady=(9, 0))
        wrapper.grid_columnconfigure(0, weight=1)
        tk.Label(
            wrapper,
            text=label,
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 12),
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        combo = ttk.Combobox(wrapper, state="disabled", style="Coach.TCombobox")
        combo.grid(row=1, column=0, sticky="ew")
        return combo

    def _bind_events(self) -> None:
        self.root.bind("<Command-Return>", lambda _event: self._primary_action())
        self.root.bind("<Control-Return>", lambda _event: self._primary_action())
        self.root.bind("<Escape>", lambda _event: self._request_stop())

    def _position_video_placeholder(self, event: tk.Event[Any]) -> None:
        center_x = event.width / 2
        center_y = event.height / 2
        self.video_canvas.coords(self.video_image_id, center_x, center_y)
        self.video_canvas.coords(self.video_title_id, center_x, center_y - 12)
        self.video_canvas.coords(self.video_hint_id, center_x, center_y + 22)

    def _select_exercise(self, exercise: str) -> None:
        if self._state not in {"idle", "error"}:
            return
        self._exercise = exercise
        self._refresh_exercise_buttons()

    def _refresh_exercise_buttons(self) -> None:
        for exercise, button in self.exercise_buttons.items():
            selected = exercise == self._exercise
            button.configure(
                bg=self.ACCENT if selected else self.PANEL_ALT,
                fg=self.TEXT if selected else self.MUTED,
                activebackground=self.ACCENT_ACTIVE if selected else self.BORDER,
            )

    def _primary_action(self) -> None:
        if self._state in {"starting", "running"}:
            self._request_stop()
        elif self._state in {"idle", "error"}:
            with contextlib.suppress(ValueError, tk.TclError):
                self.actions.put(("start", self.current_settings()))

    def _request_stop(self) -> None:
        if self._state in {"starting", "running"}:
            self.actions.put(("stop", None))

    def _on_close(self) -> None:
        self.alive = False
        self.actions.put(("close", None))

    def set_devices(self, catalog: DeviceCatalog) -> None:
        self.devices = catalog
        self._set_combo_devices(self.input_combo, catalog.audio_inputs)
        self._set_combo_devices(self.output_combo, catalog.audio_outputs)
        self._set_combo_devices(self.camera_combo, catalog.cameras)

        ready = bool(catalog.audio_inputs and catalog.audio_outputs and catalog.cameras)
        if ready:
            self.set_state("idle", "设备就绪")
            self.append_log("本地设备检测完成，可以开始训练。", "success")
        else:
            missing: list[str] = []
            if not catalog.audio_inputs:
                missing.append("麦克风")
            if not catalog.audio_outputs:
                missing.append("扬声器")
            if not catalog.cameras:
                missing.append("摄像头（需要安装 ffmpeg）")
            self.set_state("error", "设备不可用")
            self.append_log("未找到" + "、".join(missing) + "。", "error")

    def _set_combo_devices(self, combo: ttk.Combobox, devices: list[Any]) -> None:
        names = [self._device_name(device) for device in devices]
        combo.configure(values=names, state="readonly" if names else "disabled")
        if not names:
            combo.set("未检测到")
            return
        default_index = next(
            (index for index, device in enumerate(devices) if getattr(device, "is_default", False)),
            0,
        )
        combo.current(default_index)

    @staticmethod
    def _device_name(device: Any) -> str:
        name = str(getattr(device, "name", "未知设备"))
        return f"{name}（默认）" if getattr(device, "is_default", False) else name

    def current_settings(self) -> SessionSettings:
        if self.devices is None:
            raise ValueError("设备尚未检测完成")
        indices = (
            self.input_combo.current(),
            self.output_combo.current(),
            self.camera_combo.current(),
        )
        if min(indices) < 0:
            raise ValueError("请选择完整的音视频设备")
        target = max(1, min(99, int(self.target_var.get())))
        return SessionSettings(
            audio_input=self.devices.audio_inputs[indices[0]],
            audio_output=self.devices.audio_outputs[indices[1]],
            camera=self.devices.cameras[indices[2]],
            exercise=self._exercise,
            target_reps=target,
        )

    def set_state(self, state: str, detail: str = "") -> None:
        self._state = state
        labels = {
            "detecting": ("正在检测设备", self.MUTED),
            "idle": ("待命", self.MUTED),
            "starting": ("正在启动", self.ACCENT),
            "running": ("训练中", self.GREEN),
            "stopping": ("正在停止", self.ACCENT),
            "error": ("需要处理", self.DANGER),
        }
        label, color = labels.get(state, (state, self.MUTED))
        self.status_label.configure(text=detail or label)
        self.status_dot.configure(fg=color)

        is_configurable = state in {"idle", "error"}
        has_devices = bool(
            self.devices
            and self.devices.audio_inputs
            and self.devices.audio_outputs
            and self.devices.cameras
        )
        combo_state = "readonly" if is_configurable and has_devices else "disabled"
        for combo in (self.input_combo, self.output_combo, self.camera_combo):
            combo.configure(state=combo_state)
        self.target_spinbox.configure(state="normal" if is_configurable else "disabled")
        self.refresh_button.configure(state="normal" if is_configurable else "disabled")
        for button in self.exercise_buttons.values():
            button.configure(state="normal" if is_configurable else "disabled")

        if state in {"starting", "running"}:
            self.primary_button.configure(
                text="停止训练",
                state="normal",
                bg=self.PANEL_ALT,
                fg=self.TEXT,
                cursor="hand2",
            )
        elif state == "stopping":
            self.primary_button.configure(
                text="正在停止...",
                state="disabled",
                bg=self.PANEL_ALT,
                fg=self.MUTED,
                cursor="arrow",
            )
        elif has_devices:
            self.primary_button.configure(
                text="开始训练",
                state="normal",
                bg=self.ACCENT,
                fg=self.TEXT,
                cursor="hand2",
            )
        else:
            self.primary_button.configure(
                text="设备不可用",
                state="disabled",
                bg=self.PANEL_ALT,
                fg=self.MUTED,
                cursor="arrow",
            )

        if state == "running":
            self._session_started_at = asyncio.get_running_loop().time()
            self.video_canvas.itemconfigure(self.video_title_id, state="hidden")
            self.video_canvas.itemconfigure(self.video_hint_id, state="hidden")
            self.frame_status_label.configure(text="实时姿态分析", fg=self.GREEN)
        elif state in {"idle", "error"}:
            self._session_started_at = None
            self.elapsed_label.configure(text="00:00")
            self.frame_status_label.configure(text="等待视频流", fg=self.MUTED)
            if self._photo is None:
                self.video_canvas.itemconfigure(self.video_title_id, state="normal")
                self.video_canvas.itemconfigure(self.video_hint_id, state="normal")

    def submit_frame(self, frame: Any) -> None:
        while self.frames.full():
            with contextlib.suppress(queue.Empty):
                self.frames.get_nowait()
        with contextlib.suppress(queue.Full):
            self.frames.put_nowait(frame)

    def append_log(self, text: str, tag: str = "normal") -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n", tag)
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 180:
            self.log_text.delete("1.0", f"{line_count - 160}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def update(self) -> None:
        self._drain_logs()
        self._draw_latest_frame()
        self._update_elapsed()

    def _drain_logs(self) -> None:
        for _ in range(30):
            try:
                message = self.logs.get_nowait()
            except queue.Empty:
                break
            lower = message.lower()
            if "error" in lower or "失败" in message:
                tag = "error"
            elif "ready" in lower or "成功" in message or "已开始" in message:
                tag = "success"
            else:
                tag = "normal"
            self.append_log(message, tag)

    def _draw_latest_frame(self) -> None:
        latest = None
        while True:
            try:
                latest = self.frames.get_nowait()
            except queue.Empty:
                break
        if latest is None:
            return

        width = max(2, self.video_canvas.winfo_width())
        height = max(2, self.video_canvas.winfo_height())
        scale = min(width / latest.width, height / latest.height)
        fit_width = max(2, int(latest.width * scale))
        fit_height = max(2, int(latest.height * scale))
        rgb = latest.reformat(width=fit_width, height=fit_height, format="rgb24")
        pixels = rgb.to_ndarray()
        header = f"P6 {fit_width} {fit_height} 255 ".encode()
        self._photo = tk.PhotoImage(data=header + pixels.tobytes())
        self.video_canvas.itemconfigure(self.video_image_id, image=self._photo)
        self.video_canvas.coords(self.video_image_id, width / 2, height / 2)
        self.video_canvas.itemconfigure(self.video_title_id, state="hidden")
        self.video_canvas.itemconfigure(self.video_hint_id, state="hidden")

    def _update_elapsed(self) -> None:
        if self._session_started_at is None:
            return
        elapsed = max(0, int(asyncio.get_running_loop().time() - self._session_started_at))
        self.elapsed_label.configure(text=f"{elapsed // 60:02d}:{elapsed % 60:02d}")


def discover_devices() -> DeviceCatalog:
    from vision_agents.plugins.local.devices import (
        CameraDevice,
        list_audio_input_devices,
        list_audio_output_devices,
        list_cameras,
    )

    is_macos_without_ffmpeg = platform.system() == "Darwin" and shutil.which("ffmpeg") is None
    cameras = [] if is_macos_without_ffmpeg else list_cameras()
    if not cameras and platform.system() == "Darwin":
        # PyAV can open AVFoundation device 0 even when the ffmpeg CLI used by
        # the SDK's name enumerator is not installed.
        cameras = [CameraDevice(index=0, name="系统默认摄像头", device="0")]

    return DeviceCatalog(
        audio_inputs=list_audio_input_devices(),
        audio_outputs=list_audio_output_devices(),
        cameras=cameras,
    )


def make_dashboard_edge(
    settings: SessionSettings, frame_sink: Callable[[Any], None]
) -> Any:
    """Create a LocalEdge that renders processed frames in the main dashboard."""

    import aiortc
    from vision_agents.plugins.local import LocalEdge

    class DashboardLocalEdge(LocalEdge):
        async def _forward_video(self, source: aiortc.MediaStreamTrack) -> None:
            try:
                while True:
                    frame = await source.recv()
                    frame_sink(frame)
            except asyncio.CancelledError:
                raise
            except (aiortc.MediaStreamError, RuntimeError):
                LOGGER.debug("本地视频流已结束")

        async def open_demo_for_agent(self, *_args: Any, **_kwargs: Any) -> None:
            return

    return DashboardLocalEdge(
        audio_input=settings.audio_input,
        audio_output=settings.audio_output,
        video_input=settings.camera,
        video_width=640,
        video_height=480,
        video_fps=30,
    )


def session_instructions(settings: SessionSettings) -> str:
    return (
        "Read @docs/COACHING_INSTRUCTIONS.md\n\n"
        f"本次训练项目：{settings.exercise}。目标：{settings.target_reps} 次。"
        "优先观察这个动作，清晰计数，并只在必要时给出一句纠正。"
    )


def valid_dashscope_key() -> bool:
    value = os.getenv("DASHSCOPE_API_KEY", "").strip()
    return bool(value and value != "your_dashscope_api_key_here")


class SessionController:
    def __init__(self, ui: FitnessCoachUI) -> None:
        self.ui = ui
        self.task: asyncio.Task[None] | None = None
        self.stop_event: asyncio.Event | None = None

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

    async def _run(self, settings: SessionSettings) -> None:
        agent = None
        processor = None
        try:
            if not valid_dashscope_key():
                raise RuntimeError("请先在 .env 中配置 DASHSCOPE_API_KEY")

            self.ui.set_state("starting", "正在加载姿态模型")
            LOGGER.info("正在加载 YOLO 姿态模型...")

            from vision_agents.core import Agent, User
            from vision_agents.plugins import qwen, ultralytics

            processor = await asyncio.to_thread(
                ultralytics.YOLOPoseProcessor,
                model_path="yolo11n-pose.pt",
                device=os.getenv("YOLO_DEVICE", "mps"),
                fps=10,
                max_workers=2,
            )
            if self.stop_event is None or self.stop_event.is_set():
                await processor.close()
                return

            self.ui.set_state("starting", "正在连接实时教练")
            edge = make_dashboard_edge(settings, self.ui.submit_frame)
            agent = Agent(
                edge=edge,
                agent_user=User(name="AI 健身教练"),
                instructions=session_instructions(settings),
                llm=qwen.Realtime(
                    fps=1,
                    include_video=True,
                    base_url=os.getenv(
                        "DASHSCOPE_BASE_URL",
                        "wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
                    ),
                    voice=os.getenv("QWEN_VOICE", "Ethan"),
                    vad_threshold=0.35,
                ),
                processors=[processor],
            )

            call_id = f"local-{uuid.uuid4().hex[:8]}"
            call = await agent.create_call("local", call_id)
            async with agent.join(call, participant_wait_timeout=0):
                self.ui.set_state("running", f"{settings.exercise} · {settings.target_reps} 次")
                LOGGER.info("训练已开始，请保持全身位于画面内并直接说话。")
                if self.stop_event is not None:
                    await self.stop_event.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("训练启动失败")
            self.ui.set_state("error", "启动失败")
            self.ui.append_log(str(exc), "error")
        finally:
            if agent is not None:
                with contextlib.suppress(Exception):
                    await agent.close()
            elif processor is not None:
                with contextlib.suppress(Exception):
                    await processor.close()
            if self.ui.alive and self.ui._state != "error":
                self.ui.set_state("idle", "待命")
                LOGGER.info("训练已停止。")


async def run_desktop() -> None:
    root = tk.Tk()
    ui = FitnessCoachUI(root)
    controller = SessionController(ui)

    handler = UiLogHandler(ui.logs)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))
    root_logger.addHandler(handler)

    discovery_task: asyncio.Task[DeviceCatalog] | None = asyncio.create_task(
        asyncio.to_thread(discover_devices), name="device-discovery"
    )

    try:
        while ui.alive:
            try:
                root.update_idletasks()
                root.update()
            except tk.TclError:
                ui.alive = False
                break

            if discovery_task is not None and discovery_task.done():
                try:
                    ui.set_devices(discovery_task.result())
                except Exception as exc:
                    ui.set_state("error", "设备检测失败")
                    ui.append_log(f"设备检测失败：{exc}", "error")
                discovery_task = None

            while True:
                try:
                    action, payload = ui.actions.get_nowait()
                except queue.Empty:
                    break
                if action == "start":
                    controller.start(payload)
                elif action == "stop":
                    controller.stop()
                elif action == "refresh" and discovery_task is None:
                    ui.set_state("detecting")
                    discovery_task = asyncio.create_task(
                        asyncio.to_thread(discover_devices), name="device-refresh"
                    )
                elif action == "close":
                    ui.alive = False

            ui.update()
            await asyncio.sleep(1 / 60)
    finally:
        if discovery_task is not None:
            discovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await discovery_task
        await controller.shutdown()
        root_logger.removeHandler(handler)
        with contextlib.suppress(tk.TclError):
            root.destroy()


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(run_desktop())
    except KeyboardInterrupt:
        LOGGER.info("本地训练台已关闭")


if __name__ == "__main__":
    if sys.platform == "darwin" and sys.version_info < (3, 11):
        raise SystemExit("本地界面需要 Python 3.11 或更高版本")
    main()
