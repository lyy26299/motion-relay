"""Replayable, single-person squat phase machine.

The FSM consumes a representative knee angle (usually the visible side average).
It deliberately refuses to count invisible, multi-person or stale observations.
"""

from __future__ import annotations

from dataclasses import dataclass

from coach.models import CoachEvent, MotionPhase, MotionSnapshot, PoseSnapshot, RepRecord


@dataclass(frozen=True, slots=True)
class SquatThresholds:
    standing_angle_deg: float = 160.0
    bottom_angle_deg: float = 115.0
    min_rep_ms: int = 250
    max_gap_ms: int = 1200
    min_usable_sample_ratio: float = 0.7
    rule_version: str = "squat-v1"


class SquatFSM:
    """Small hysteresis FSM; no user identity or model inference lives here."""

    def __init__(self, *, session_id: str, thresholds: SquatThresholds | None = None) -> None:
        self.session_id = session_id
        self.thresholds = thresholds or SquatThresholds()
        self.phase: MotionPhase = "unknown"
        self.completed_reps = 0
        self.valid_reps = 0
        self._sequence = 0
        self._rep_started_at: float | None = None
        self._rep_start_frame: int | None = None
        self._last_observed_at: float | None = None
        self._min_knee: float | None = None
        self._max_knee: float | None = None
        self._usable = 0
        self._samples = 0
        self._paused = False

    @staticmethod
    def _knee_angle(snapshot: PoseSnapshot) -> float | None:
        values = [
            value
            for value in (snapshot.angles.left_knee, snapshot.angles.right_knee)
            if value is not None
        ]
        return sum(values) / len(values) if values else None

    def update(
        self, snapshot: PoseSnapshot
    ) -> tuple[MotionSnapshot, tuple[CoachEvent, ...], tuple[RepRecord, ...]]:
        self._samples += 1
        angle = (
            self._knee_angle(snapshot)
            if snapshot.status == "observable" or snapshot.status == "partial"
            else None
        )
        now = snapshot.observed_at
        gap_ms = (now - self._last_observed_at) * 1000 if self._last_observed_at is not None else 0
        self._last_observed_at = now
        events: list[CoachEvent] = []
        records: list[RepRecord] = []
        if gap_ms > self.thresholds.max_gap_ms:
            events.append(self._event("visibility_lost", snapshot, {"gap_ms": gap_ms}))
            self._reset_rep()
            self._paused = True
            self.phase = "unknown"

        if snapshot.status == "multiple_people":
            self._paused = True
            events.append(self._event("multiple_people", snapshot, {"reason": "multiple_people"}))
            self._reset_rep()
            self.phase = "paused"
        elif snapshot.status in {"no_person", "unobservable", "inference_error"}:
            if not self._paused:
                events.append(self._event("visibility_lost", snapshot, {"status": snapshot.status}))
            self._paused = True
            self._reset_rep()
            self.phase = "paused"
        elif self._paused:
            # Visibility recovery requires a fresh standing observation.  Do
            # not resume halfway through a squat and accidentally stitch a
            # pre-gap descent to a post-gap ascent.
            if angle is not None and angle >= self.thresholds.standing_angle_deg:
                self._paused = False
                previous = self.phase
                self.phase = "standing"
                events.append(
                    self._event(
                        "phase_changed",
                        snapshot,
                        {"from": previous, "to": "standing", "knee_angle_deg": angle},
                    )
                )
            else:
                self.phase = "paused"

        if angle is None and snapshot.status in {"observable", "partial"} and not self._paused:
            # A partial detector status is not enough to continue a phase if
            # neither knee angle can be observed.
            self._paused = True
            self._reset_rep()
            self.phase = "paused"
            events.append(self._event("visibility_lost", snapshot, {"status": snapshot.status}))

        if angle is not None and not self._paused:
            self._samples += 0  # Keep sample counters explicit for auditability.
            self._usable += 1
            self._min_knee = angle if self._min_knee is None else min(self._min_knee, angle)
            self._max_knee = angle if self._max_knee is None else max(self._max_knee, angle)
            next_phase = self._phase_for(angle)
            if next_phase != self.phase:
                previous = self.phase
                self.phase = next_phase
                events.append(
                    self._event(
                        "phase_changed",
                        snapshot,
                        {"from": previous, "to": next_phase, "knee_angle_deg": angle},
                    )
                )
            if self.phase == "descending" and self._rep_started_at is None:
                self._rep_started_at = now
                self._rep_start_frame = snapshot.frame_id
            if self.phase == "bottom" and self._rep_started_at is None:
                self._rep_started_at = now
                self._rep_start_frame = snapshot.frame_id
            if self.phase == "standing" and self._rep_started_at is not None:
                record = self._complete_rep(snapshot)
                records.append(record)
                self.completed_reps += 1
                if record.valid:
                    self.valid_reps += 1
                events.append(
                    self._event(
                        "rep_completed",
                        snapshot,
                        {
                            "rep_id": record.rep_id,
                            "rep_index": record.rep_index,
                            "valid": record.valid,
                            "reason_codes": record.reason_codes,
                        },
                    )
                )
                self._reset_rep()
        snapshot_out = MotionSnapshot(
            session_id=self.session_id,
            frame_id=snapshot.frame_id,
            observed_at=now,
            phase=self.phase,
            completed_reps=self.completed_reps,
            valid_reps=self.valid_reps,
            visible=angle is not None,
            paused=self._paused,
            knee_angle_deg=angle,
            hip_angle_deg=self._hip_angle(snapshot),
            rule_version=self.thresholds.rule_version,
            evidence_start_frame=self._rep_start_frame,
            evidence_end_frame=snapshot.frame_id,
        )
        return snapshot_out, tuple(events), tuple(records)

    def _phase_for(self, angle: float) -> MotionPhase:
        if angle >= self.thresholds.standing_angle_deg:
            return "standing"
        if angle <= self.thresholds.bottom_angle_deg:
            return "bottom"
        if self.phase in {"bottom", "ascending"}:
            return "ascending"
        return "descending"

    @staticmethod
    def _hip_angle(snapshot: PoseSnapshot) -> float | None:
        values = [
            value
            for value in (snapshot.angles.left_hip, snapshot.angles.right_hip)
            if value is not None
        ]
        return sum(values) / len(values) if values else None

    def _complete_rep(self, snapshot: PoseSnapshot) -> RepRecord:
        start = self._rep_started_at or snapshot.observed_at
        duration_ms = max(0.0, (snapshot.observed_at - start) * 1000)
        ratio = self._usable / self._samples if self._samples else 0.0
        reasons: list[str] = []
        if duration_ms < self.thresholds.min_rep_ms:
            reasons.append("too_fast")
        if self._min_knee is None or self._min_knee > self.thresholds.bottom_angle_deg:
            reasons.append("insufficient_depth")
        if ratio < self.thresholds.min_usable_sample_ratio:
            reasons.append("insufficient_observation")
        return RepRecord(
            # The sequence is deterministic for replay and database idempotency.
            rep_id=f"rep-{self.session_id}-{self.completed_reps + 1}",
            session_id=self.session_id,
            rep_index=self.completed_reps + 1,
            valid=not reasons,
            started_at=start,
            completed_at=snapshot.observed_at,
            duration_ms=duration_ms,
            min_knee_angle_deg=self._min_knee,
            max_knee_angle_deg=self._max_knee,
            usable_sample_ratio=max(0.0, min(1.0, ratio)),
            reason_codes=tuple(reasons),
            rule_version=self.thresholds.rule_version,
            evidence_start_frame=self._rep_start_frame,
            evidence_end_frame=snapshot.frame_id,
        )

    def _reset_rep(self) -> None:
        self._rep_started_at = None
        self._rep_start_frame = None
        self._min_knee = None
        self._max_knee = None
        self._usable = 0
        self._samples = 0

    def pause(self) -> None:
        """Latch a watchdog pause and discard any in-progress repetition."""

        self._paused = True
        self._reset_rep()
        self.phase = "paused"

    def _event(self, kind, snapshot, facts):
        self._sequence += 1
        return CoachEvent(
            event_id=f"evt-{self.session_id}-{self._sequence}",
            session_id=self.session_id,
            kind=kind,
            occurred_at=snapshot.observed_at,
            frame_id=snapshot.frame_id,
            rule_version=self.thresholds.rule_version,
            evidence_start_frame=self._rep_start_frame,
            evidence_end_frame=snapshot.frame_id,
            facts=facts,
        )
