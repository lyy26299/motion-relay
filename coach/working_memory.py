"""Bounded session state shared by the local motion runtime and coach loop."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from coach.models import CoachEvent, MotionSnapshot, PoseSnapshot, RepRecord


@dataclass(frozen=True, slots=True)
class DialogueTurn:
    role: str
    text: str
    occurred_at: float


@dataclass(frozen=True, slots=True)
class WorkingMemoryView:
    session_id: str
    state_version: int
    current_pose: PoseSnapshot | None
    current_motion: MotionSnapshot | None
    poses: tuple[PoseSnapshot, ...]
    events: tuple[CoachEvent, ...]
    reps: tuple[RepRecord, ...]
    dialogue: tuple[DialogueTurn, ...]
    active_task: dict[str, Any] | None
    pause_reason: str | None


class WorkingMemory:
    """In-memory, per-session memory with explicit TTL and bounded collections."""

    def __init__(
        self,
        *,
        session_id: str,
        pose_window_s: float = 30.0,
        pose_max: int = 300,
        event_window_s: float = 120.0,
        event_max: int = 100,
        dialogue_max: int = 8,
        rep_max: int = 100,
    ) -> None:
        if (
            min(pose_window_s, event_window_s) <= 0
            or min(pose_max, event_max, dialogue_max, rep_max) <= 0
        ):
            raise ValueError("Working memory limits must be positive")
        self.session_id = session_id
        self.pose_window_s = pose_window_s
        self.event_window_s = event_window_s
        self.dialogue_max = dialogue_max
        self._poses: deque[PoseSnapshot] = deque(maxlen=pose_max)
        self._events: deque[CoachEvent] = deque(maxlen=event_max)
        self._reps: deque[RepRecord] = deque(maxlen=rep_max)
        self._dialogue: deque[DialogueTurn] = deque(maxlen=dialogue_max)
        self._current_pose: PoseSnapshot | None = None
        self._current_motion: MotionSnapshot | None = None
        self._active_task: dict[str, Any] | None = None
        self._pause_reason: str | None = None
        self._state_version = 0

    @property
    def state_version(self) -> int:
        return self._state_version

    def add_pose(self, snapshot: PoseSnapshot) -> None:
        self._assert_session(snapshot.session_id)
        if self._current_pose and (
            snapshot.stream_epoch,
            snapshot.frame_id,
        ) <= (self._current_pose.stream_epoch, self._current_pose.frame_id):
            return
        self._current_pose = snapshot
        self._poses.append(snapshot)
        self._prune(snapshot.observed_at)
        self._touch()

    def add_motion(
        self,
        motion: MotionSnapshot,
        events: tuple[CoachEvent, ...] = (),
        reps: tuple[RepRecord, ...] = (),
    ) -> None:
        self._assert_session(motion.session_id)
        self._current_motion = motion
        self._events.extend(event for event in events if event.session_id == self.session_id)
        self._reps.extend(rep for rep in reps if rep.session_id == self.session_id)
        if motion.paused and self._pause_reason is None:
            self._pause_reason = "motion_runtime"
        self._prune(motion.observed_at)
        self._touch()

    def add_dialogue(self, role: str, text: str, *, occurred_at: float | None = None) -> None:
        if role not in {"user", "coach", "system"}:
            raise ValueError("Dialogue role must be user, coach or system")
        text = text.strip()
        if not text:
            return
        self._dialogue.append(DialogueTurn(role, text, occurred_at or time.monotonic()))
        self._touch()

    def pause(self, reason: str) -> None:
        reason = reason.strip()
        if not reason:
            raise ValueError("Pause reason is required")
        self._pause_reason = reason
        self._touch()

    def resume(self) -> None:
        # A safety pause must be cleared by an explicit application action.
        self._pause_reason = None
        self._touch()

    def begin_task(self, task_id: str, *, kind: str, deadline: float, **metadata: Any) -> int:
        self._active_task = {
            "task_id": task_id,
            "kind": kind,
            "deadline": deadline,
            "generation": self._state_version + 1,
            **metadata,
        }
        self._touch()
        return int(self._active_task["generation"])

    def cancel_task(self) -> None:
        self._active_task = None
        self._touch()

    def task_is_current(self, task_id: str, generation: int) -> bool:
        task = self._active_task
        return bool(task and task["task_id"] == task_id and task["generation"] == generation)

    def view(self) -> WorkingMemoryView:
        return WorkingMemoryView(
            session_id=self.session_id,
            state_version=self._state_version,
            current_pose=self._current_pose,
            current_motion=self._current_motion,
            poses=tuple(self._poses),
            events=tuple(self._events),
            reps=tuple(self._reps),
            dialogue=tuple(self._dialogue),
            active_task=dict(self._active_task) if self._active_task else None,
            pause_reason=self._pause_reason,
        )

    def _prune(self, latest_at: float) -> None:
        pose_cutoff = latest_at - self.pose_window_s
        while self._poses and self._poses[0].observed_at < pose_cutoff:
            self._poses.popleft()
        event_cutoff = latest_at - self.event_window_s
        while self._events and self._events[0].occurred_at < event_cutoff:
            self._events.popleft()

    def _assert_session(self, session_id: str) -> None:
        if session_id != self.session_id:
            raise ValueError("Working memory session scope mismatch")

    def _touch(self) -> None:
        self._state_version += 1
