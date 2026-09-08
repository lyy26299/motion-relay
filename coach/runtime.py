"""Local motion runtime: PoseSnapshot -> SquatFSM -> bounded working memory."""

from __future__ import annotations

import time
from collections.abc import Callable

from coach.exercises.squat import SquatFSM, SquatThresholds
from coach.models import CoachEvent, MotionSnapshot, PoseSnapshot, RepRecord
from coach.working_memory import WorkingMemory


class MotionRuntime:
    """Owns the only path allowed to create authoritative rep facts."""

    def __init__(
        self,
        *,
        session_id: str,
        memory: WorkingMemory | None = None,
        thresholds: SquatThresholds | None = None,
        event_sink: Callable[[CoachEvent], None] | None = None,
        rep_sink: Callable[[RepRecord], None] | None = None,
        fact_sink: Callable[[tuple[CoachEvent, ...], tuple[RepRecord, ...]], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self.memory = memory or WorkingMemory(session_id=session_id)
        self.fsm = SquatFSM(session_id=session_id, thresholds=thresholds)
        self.event_sink = event_sink
        self.rep_sink = rep_sink
        # A batch sink lets a durable writer commit all events and repetitions
        # from one observation without blocking the pose callback.
        self.fact_sink = fact_sink
        self._last_order: tuple[int, int] = (0, 0)
        self._last_observed_at: float | None = None
        self._watchdog_emitted = False
        self._last_motion: MotionSnapshot | None = None

    def ingest(
        self, snapshot: PoseSnapshot
    ) -> tuple[MotionSnapshot, tuple[CoachEvent, ...], tuple[RepRecord, ...]]:
        if snapshot.session_id != self.session_id:
            raise ValueError("Motion runtime session scope mismatch")
        order = (snapshot.stream_epoch, snapshot.frame_id)
        if order <= self._last_order:
            current = self.fsm_snapshot()
            return current, (), ()
        self._last_order = order
        self._last_observed_at = snapshot.observed_at
        self._watchdog_emitted = False
        self.memory.add_pose(snapshot)
        motion, events, reps = self.fsm.update(snapshot)
        self._last_motion = motion
        self.memory.add_motion(motion, events, reps)
        self._emit(events, reps)
        return motion, events, reps

    def watchdog(
        self, *, now: float | None = None, timeout_s: float = 1.5
    ) -> tuple[CoachEvent, ...]:
        now = time.monotonic() if now is None else now
        if self._last_observed_at is None or now - self._last_observed_at <= timeout_s:
            return ()
        if self._watchdog_emitted:
            return ()
        self._watchdog_emitted = True
        self.fsm.pause()
        event = CoachEvent(
            event_id=f"evt-{self.session_id}-watchdog-{self.memory.state_version + 1}",
            session_id=self.session_id,
            kind="visibility_lost",
            occurred_at=now,
            frame_id=self._last_order[1],
            rule_version=self.fsm.thresholds.rule_version,
            evidence_start_frame=None,
            evidence_end_frame=self._last_order[1],
            facts={"reason": "frame_timeout", "timeout_s": timeout_s},
        )
        self.memory.pause("frame_timeout")
        paused_motion = self.fsm_snapshot(observed_at=now, paused=True)
        self._last_motion = paused_motion
        self.memory.add_motion(paused_motion, (event,), ())
        self._emit((event,), ())
        return (event,)

    def resume(self) -> None:
        self.memory.resume()

    def pause(self, reason: str) -> MotionSnapshot:
        """Latch an application-requested pause and discard a partial rep.

        Safety and operator pauses use the same local ownership boundary as
        watchdog pauses: the FSM stops first, then WorkingMemory records the
        explicit reason.  No language-model result can create or complete a
        repetition through this method.
        """

        reason = str(reason).strip()
        if not reason:
            raise ValueError("Pause reason is required")
        self.fsm.pause()
        self.memory.pause(reason)
        motion = self.fsm_snapshot(observed_at=time.monotonic(), paused=True)
        self._last_motion = motion
        self.memory.add_motion(motion)
        return motion

    def fsm_snapshot(
        self, *, observed_at: float | None = None, paused: bool | None = None
    ) -> MotionSnapshot:
        if paused is None and self._last_motion is not None:
            return self._last_motion
        return MotionSnapshot(
            session_id=self.session_id,
            frame_id=self._last_order[1],
            observed_at=observed_at if observed_at is not None else self._last_observed_at or 0.0,
            phase="paused" if paused else self.fsm.phase,
            completed_reps=self.fsm.completed_reps,
            valid_reps=self.fsm.valid_reps,
            visible=False,
            paused=(
                bool(paused) if paused is not None else self.memory.view().pause_reason is not None
            ),
            knee_angle_deg=None,
            hip_angle_deg=None,
            rule_version=self.fsm.thresholds.rule_version,
        )

    def _emit(self, events: tuple[CoachEvent, ...], reps: tuple[RepRecord, ...]) -> None:
        if self.fact_sink and (events or reps):
            self.fact_sink(events, reps)
            return
        if self.event_sink:
            for event in events:
                self.event_sink(event)
        if self.rep_sink:
            for rep in reps:
                self.rep_sink(rep)
