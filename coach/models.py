"""Immutable, CPU-only observations; not exercise judgments or user identities."""

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

COCO_KEYPOINT_NAMES = (
    "nose",
    "left_eye",
    "right_eye",
    "left_ear",
    "right_ear",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
)

PoseStatus = Literal[
    "observable", "partial", "unobservable", "no_person", "multiple_people", "inference_error"
]

MotionPhase = Literal["unknown", "standing", "descending", "bottom", "ascending", "paused"]
MotionEventKind = Literal[
    "phase_changed", "rep_completed", "visibility_lost", "multiple_people", "reset"
]


@dataclass(frozen=True, slots=True)
class MotionSnapshot:
    """Derived motion state. It is authoritative only when produced by MotionRuntime."""

    session_id: str
    frame_id: int
    observed_at: float
    phase: MotionPhase
    completed_reps: int
    valid_reps: int
    visible: bool
    paused: bool
    knee_angle_deg: float | None
    hip_angle_deg: float | None
    rule_version: str
    evidence_start_frame: int | None = None
    evidence_end_frame: int | None = None


@dataclass(frozen=True, slots=True)
class CoachEvent:
    event_id: str
    session_id: str
    kind: MotionEventKind
    occurred_at: float
    frame_id: int
    rule_version: str
    evidence_start_frame: int | None
    evidence_end_frame: int | None
    facts: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RepRecord:
    rep_id: str
    session_id: str
    rep_index: int
    valid: bool
    started_at: float
    completed_at: float
    duration_ms: float
    min_knee_angle_deg: float | None
    max_knee_angle_deg: float | None
    usable_sample_ratio: float
    reason_codes: tuple[str, ...]
    rule_version: str
    evidence_start_frame: int | None
    evidence_end_frame: int | None


@dataclass(frozen=True, slots=True)
class Keypoint:
    # x / image_width, y / image_height; None means a non-finite/missing value.
    x: float | None
    y: float | None
    confidence: float | None


@dataclass(frozen=True, slots=True)
class PersonPose:
    # An index in ONE frame, never a persistent track_id or user_id.
    detection_index: int
    keypoints: tuple[Keypoint, ...]
    bbox_xyxy: tuple[float, float, float, float] | None
    confidence: float | None


@dataclass(frozen=True, slots=True)
class ProjectedAngles:
    left_knee: float | None = None
    right_knee: float | None = None
    left_hip: float | None = None
    right_hip: float | None = None

    def values(self) -> tuple[float | None, ...]:
        return self.left_knee, self.right_knee, self.left_hip, self.right_hip


@dataclass(frozen=True, slots=True)
class PoseSnapshot:
    session_id: str
    stream_epoch: int
    frame_id: int
    observed_at: float  # Process-local monotonic seconds at the adapter input.
    processed_at: float
    media_time_s: float | None  # Input PTS * time_base, not a wall/camera clock.
    width: int
    height: int
    model: str
    keypoint_threshold: float
    detections: tuple[PersonPose, ...]
    angles: ProjectedAngles
    status: PoseStatus
    processing_ms: float
    schema_version: str = field(default="coach.pose.v1", init=False)
    source: str = field(default="ultralytics_pose", init=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_stale(self, now: float, max_age_s: float = 1.5) -> bool:
        return now - self.observed_at > max_age_s
