"""Local desktop interface for the real-time Vision Agent fitness coach."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import queue
import sys
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any

from agent_local_agent import LOGGER, SessionController
from coach.models import MotionSnapshot, PoseSnapshot


@dataclass(frozen=True)
class SessionSettings:
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
        self.actions: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.logs: queue.Queue[str] = queue.Queue(maxsize=400)
        self.frames: queue.Queue[Any] = queue.Queue(maxsize=2)
        self.poses: queue.Queue[PoseSnapshot] = queue.Queue(maxsize=1)
        self.motions: queue.Queue[MotionSnapshot] = queue.Queue(maxsize=1)
        self.memory_statuses: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=4)
        self._pose_session_id: str | None = None
        self._latest_pose: PoseSnapshot | None = None
        self._latest_motion: MotionSnapshot | None = None
        self._pose_order = (0, 0)
        self._photo: tk.PhotoImage | None = None
        self._exercise = self.EXERCISES[0]
        self._session_started_at: float | None = None
        self._state = "idle"

        self._configure_window()
        self._build_styles()
        self._build_layout()
        self._bind_events()

    @property
    def state(self) -> str:
        return self._state

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
            text="待命",
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
            text="点击开始训练，在浏览器中授权音视频设备",
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

        metrics = tk.Frame(left, bg=self.PANEL, height=132)
        metrics.grid(row=2, column=0, sticky="ew")
        metrics.grid_propagate(False)
        metrics.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="pose-metric")
        self.pose_angle_labels: list[tk.Label] = []
        for column, title in enumerate(("左膝 · 2D", "右膝 · 2D", "左髋 · 2D", "右髋 · 2D")):
            tk.Label(
                metrics,
                text=title,
                bg=self.PANEL,
                fg=self.MUTED,
                font=(self.font_family, 11),
            ).grid(row=0, column=column, sticky="w", padx=16)
            label = tk.Label(
                metrics,
                text="--",
                bg=self.PANEL,
                fg=self.TEXT,
                font=("Menlo", 16, "bold"),
                width=7,
                anchor="w",
            )
            label.grid(row=1, column=column, sticky="w", padx=16, pady=(2, 4))
            self.pose_angle_labels.append(label)
        self.pose_status_label = tk.Label(
            metrics,
            text="等待姿态数据",
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 11),
            anchor="w",
            justify="left",
        )
        self.pose_status_label.grid(row=2, column=0, columnspan=4, sticky="ew", padx=16)
        self.motion_status_label = tk.Label(
            metrics,
            text="动作：-- · 完成 0 · 有效 0 · 阶段 --",
            bg=self.PANEL,
            fg=self.TEXT,
            font=(self.font_family, 12, "bold"),
            anchor="w",
        )
        self.motion_status_label.grid(row=3, column=0, columnspan=4, sticky="ew", padx=16, pady=(7, 0))
        self.memory_status_label = tk.Label(
            metrics,
            text="记忆：未连接",
            bg=self.PANEL,
            fg=self.MUTED,
            font=(self.font_family, 11),
            anchor="w",
        )
        self.memory_status_label.grid(row=4, column=0, columnspan=4, sticky="ew", padx=16, pady=(3, 0))
        metrics.bind(
            "<Configure>",
            lambda event: self.pose_status_label.configure(wraplength=max(100, event.width - 32)),
        )

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

        self._section_title(settings, "浏览器音视频").grid(row=3, column=0, sticky="w")
        tk.Label(
            settings,
            text="开始后自动打开浏览器\n请授权麦克风与摄像头\n教练声音在浏览器播放\n全双工回声消除 · 支持同时说话",
            bg=self.PANEL,
            fg=self.MUTED,
            justify="left",
            font=(self.font_family, 12),
        ).grid(row=4, column=0, sticky="w", pady=(10, 0))

        self.primary_button = tk.Button(
            side,
            text="开始训练",
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

    def current_settings(self) -> SessionSettings:
        target = max(1, min(99, int(self.target_var.get())))
        return SessionSettings(
            exercise=self._exercise,
            target_reps=target,
        )

    def set_state(self, state: str, detail: str = "") -> None:
        self._state = state
        if state not in {"starting", "running"}:
            self._pose_session_id = None
            self._reset_pose()
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
        self.target_spinbox.configure(state="normal" if is_configurable else "disabled")
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
        elif is_configurable:
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

    def _reset_pose(self) -> None:
        self._latest_pose = None
        self._pose_order = (0, 0)
        while not self.poses.empty():
            with contextlib.suppress(queue.Empty):
                self.poses.get_nowait()
        self._draw_pose()

    def submit_motion(self, motion: MotionSnapshot) -> None:
        """Queue authoritative local motion state for the Tk thread."""
        motions = getattr(self, "motions", None)
        if motions is None:
            return
        while motions.full():
            with contextlib.suppress(queue.Empty):
                motions.get_nowait()
        with contextlib.suppress(queue.Full):
            motions.put_nowait(motion)

    def submit_memory_status(self, status: str, detail: str = "") -> None:
        statuses = getattr(self, "memory_statuses", None)
        if statuses is None:
            return
        while statuses.full():
            with contextlib.suppress(queue.Empty):
                statuses.get_nowait()
        with contextlib.suppress(queue.Full):
            statuses.put_nowait((str(status), str(detail)))

    def begin_pose_session(self, session_id: str) -> None:
        self._pose_session_id = session_id
        self._reset_pose()

    def submit_pose(self, snapshot: PoseSnapshot) -> None:
        if (
            self._state not in {"starting", "running"}
            or snapshot.session_id != self._pose_session_id
        ):
            return
        order = (snapshot.stream_epoch, snapshot.frame_id)
        if order <= self._pose_order:
            return
        self._pose_order = order
        if self.poses.full():
            with contextlib.suppress(queue.Empty):
                self.poses.get_nowait()
        with contextlib.suppress(queue.Full):
            self.poses.put_nowait(snapshot)

    def _draw_pose(self) -> None:
        while not self.poses.empty():
            try:
                snapshot = self.poses.get_nowait()
            except queue.Empty:
                break
            if snapshot.session_id != self._pose_session_id:
                continue
            if self._latest_pose is None or (snapshot.stream_epoch, snapshot.frame_id) > (
                self._latest_pose.stream_epoch,
                self._latest_pose.frame_id,
            ):
                self._latest_pose = snapshot
        snapshot = self._latest_pose
        stale = snapshot is not None and snapshot.is_stale(time.monotonic())
        angles = snapshot.angles.values() if snapshot is not None and not stale else (None,) * 4
        for label, value in zip(self.pose_angle_labels, angles):
            label.configure(text=f"{value:.1f}°" if value is not None else "--")
        statuses = {
            "observable": "单人 · 投影角度可观测",
            "partial": "单人 · 部分关键点不可用",
            "unobservable": "关键点不可用 · 角度不可判定",
            "no_person": "未检测到人体",
            "multiple_people": "多人入镜 · 角度不可判定",
            "inference_error": "姿态推理失败",
        }
        if snapshot is None:
            text = "等待姿态数据"
        elif stale:
            text = "姿态数据已过期"
        else:
            text = statuses[snapshot.status]
            if snapshot.status != "inference_error":
                text += f" · {len(snapshot.detections)} 人 · {snapshot.processing_ms:.0f} ms"
        self.pose_status_label.configure(text=text)

    def _draw_motion(self) -> None:
        motions = getattr(self, "motions", None)
        if motions is not None:
            while True:
                try:
                    self._latest_motion = motions.get_nowait()
                except queue.Empty:
                    break
        motion = getattr(self, "_latest_motion", None)
        label = getattr(self, "motion_status_label", None)
        if motion is not None and label is not None:
            phase_names = {
                "unknown": "未就绪",
                "standing": "站立",
                "descending": "下蹲",
                "bottom": "底部",
                "ascending": "起身",
                "paused": "已暂停",
            }
            phase = phase_names.get(motion.phase, motion.phase)
            visibility = "可见" if motion.visible else "不可见"
            paused = " · 暂停" if motion.paused else ""
            label.configure(
                text=(
                    f"动作：深蹲 · 完成 {motion.completed_reps} · "
                    f"有效 {motion.valid_reps} · 阶段 {phase} · {visibility}{paused}"
                )
            )

        statuses = getattr(self, "memory_statuses", None)
        memory_label = getattr(self, "memory_status_label", None)
        if statuses is not None and memory_label is not None:
            latest_status: tuple[str, str] | None = None
            while True:
                try:
                    latest_status = statuses.get_nowait()
                except queue.Empty:
                    break
            if latest_status is not None:
                status, detail = latest_status
                names = {
                    "starting": "连接中",
                    "ready": "已连接",
                    "queued": "排队写入",
                    "writing": "正在写入",
                    "saved": "已保存",
                    "failed": "写入失败",
                    "closed": "已关闭",
                }
                memory_label.configure(text=f"记忆：{names.get(status, status)}" + (f" · {detail}" if detail else ""))

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
        self._draw_pose()
        self._draw_motion()
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


async def run_desktop() -> None:
    root = tk.Tk()
    ui = FitnessCoachUI(root)
    controller = SessionController(ui)

    handler = UiLogHandler(ui.logs)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))
    root_logger.addHandler(handler)

    ui.set_state("idle", "待命")
    ui.append_log("音视频由浏览器采集与播放，开始训练后请完成浏览器授权。")

    try:
        while ui.alive:
            try:
                root.update_idletasks()
                root.update()
            except tk.TclError:
                ui.alive = False
                break

            while True:
                try:
                    action, payload = ui.actions.get_nowait()
                except queue.Empty:
                    break
                if action == "start":
                    controller.start(payload)
                elif action == "stop":
                    controller.stop()
                elif action == "close":
                    ui.alive = False

            ui.update()
            await asyncio.sleep(1 / 60)
    finally:
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
