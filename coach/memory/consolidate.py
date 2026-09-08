"""Deterministic session consolidation for the coaching memory ledger.

Consolidation deliberately does not ask an LLM to invent metrics.  It reads
the exact session aggregate written by the local motion FSM and stores a small
summary with the session and repetition IDs as evidence.  A later language
model may phrase that summary, but it cannot change the numbers here.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .store import MemoryStore


def _fmt_number(value: Any, digits: int = 1) -> str:
    if value is None:
        return "未知"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "未知"


def build_session_summary(
    store: MemoryStore,
    user_id: str,
    session_id: str,
    *,
    exercise: str = "squat",
) -> dict[str, Any] | None:
    """Create or update a deterministic summary for one completed session.

    The summary is intentionally compact and only contains facts from
    ``query_training``.  ``None`` means the session does not exist in the
    requested user scope.
    """

    session = store.get_session(user_id, session_id)
    if session is None:
        return None
    rows = store.query_training(user_id, exercise=exercise, session_id=session_id, limit=1)
    if not rows:
        return None
    row = rows[0]
    reps = store.query_reps(user_id, exercise=exercise, session_id=session_id, limit=20)
    source_ids = [session_id, *[str(rep["rep_id"]) for rep in reps]]
    completed = int(row.get("completed_reps") or 0)
    valid = int(row.get("valid_reps") or 0)
    status = str(row.get("status") or "unknown")
    text = (
        f"{exercise} 训练 {status}：完成 {completed} 次，有效 {valid} 次；"
        f"平均动作时长 {_fmt_number(row.get('avg_duration_ms'))} ms；"
        f"膝角观测范围 {_fmt_number(row.get('min_knee_angle_deg'))}°-"
        f"{_fmt_number(row.get('max_knee_angle_deg'))}°。"
    )
    return store.put_summary(
        user_id,
        summary=text,
        session_id=session_id,
        summary_id=f"summary-{session_id}",
        source_ids=source_ids,
        revision=1,
        status="confirmed",
    )


def consolidate_session(
    path: str,
    user_id: str,
    session_id: str,
    *,
    exercise: str = "squat",
) -> dict[str, Any] | None:
    """Open a short-lived store connection and consolidate one session."""

    with MemoryStore(path) as store:
        return build_session_summary(store, user_id, session_id, exercise=exercise)


__all__ = ["build_session_summary", "consolidate_session"]
