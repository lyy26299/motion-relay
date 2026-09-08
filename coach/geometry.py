"""2D projected angles, gated by visibility and computed in pixel proportions."""

import math

from coach.models import Keypoint, PersonPose, PoseStatus, ProjectedAngles


def projected_angle(
    a: Keypoint,
    joint: Keypoint,
    b: Keypoint,
    *,
    width: int,
    height: int,
    min_confidence: float = 0.5,
) -> float | None:
    if width <= 0 or height <= 0:
        return None
    points = (a, joint, b)
    for point in points:
        if any(v is None or not math.isfinite(v) for v in (point.x, point.y, point.confidence)):
            return None
        if not (0 <= point.x <= 1 and 0 <= point.y <= 1):
            return None
        if not min_confidence <= point.confidence <= 1:
            return None
    # Independent x/y normalization changes angles on non-square images.
    ux, uy = (a.x - joint.x) * width, (a.y - joint.y) * height
    vx, vy = (b.x - joint.x) * width, (b.y - joint.y) * height
    u_length, v_length = math.hypot(ux, uy), math.hypot(vx, vy)
    if min(u_length, v_length) <= 1e-6:
        return None
    cosine = (ux * vx + uy * vy) / (u_length * v_length)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def pose_angles(
    person: PersonPose, *, width: int, height: int, min_confidence: float = 0.5
) -> ProjectedAngles:
    if len(person.keypoints) != 17:
        return ProjectedAngles()

    def angle(a: int, joint: int, b: int) -> float | None:
        return projected_angle(
            person.keypoints[a],
            person.keypoints[joint],
            person.keypoints[b],
            width=width,
            height=height,
            min_confidence=min_confidence,
        )

    return ProjectedAngles(
        left_knee=angle(11, 13, 15),
        right_knee=angle(12, 14, 16),
        left_hip=angle(5, 11, 13),
        right_hip=angle(6, 12, 14),
    )


def single_person_angles(
    detections: tuple[PersonPose, ...], *, width: int, height: int, min_confidence: float = 0.5
) -> tuple[PoseStatus, ProjectedAngles]:
    if not detections:
        return "no_person", ProjectedAngles()
    if len(detections) != 1:
        return "multiple_people", ProjectedAngles()
    angles = pose_angles(detections[0], width=width, height=height, min_confidence=min_confidence)
    available = sum(value is not None for value in angles.values())
    status: PoseStatus = (
        "observable" if available == 4 else "partial" if available else "unobservable"
    )
    return status, angles
