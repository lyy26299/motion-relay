"""Order-independent delivery observations, not proof of physical playback.

This small join is deliberately independent of the SDK, asyncio and SQLite.
Concurrent observers may finish in any order; merging the same observations
must not turn a terminal outcome back into queued/generated. Interruption is
an absorbing delivery invalidation, not a claim that generation never happened.
"""
from __future__ import annotations

PROGRESS_STATES = ("accepted", "queued", "generated", "generation_unknown")
TERMINAL_STATES = frozenset({
    "generation_completed", "generation_failed", "generation_incomplete", "rejected",
})
FEEDBACK_STATES = frozenset(PROGRESS_STATES) | TERMINAL_STATES | {
    "delivery_conflict", "interrupted",
}
OBSERVATION_STATES = FEEDBACK_STATES - {"accepted", "delivery_conflict"}


def merge_feedback_status(previous: str, observed: str) -> str:
    """Join two delivery states: associative, commutative and idempotent.

    Specific terminal outcomes refine generation_unknown. Contradictory known
    outcomes become delivery_conflict, rather than silently choosing success.
    Interruption invalidates delivery regardless of generation outcome. It does
    not erase separate played_at/acknowledged_at evidence in the repository.
    """
    for value in (previous, observed):
        if not isinstance(value, str) or value not in FEEDBACK_STATES:
            raise ValueError("unsupported feedback state")
    if previous == observed:
        return previous
    if "interrupted" in (previous, observed):
        return "interrupted"
    if "delivery_conflict" in (previous, observed):
        return "delivery_conflict"
    previous_terminal = previous in TERMINAL_STATES
    observed_terminal = observed in TERMINAL_STATES
    if previous_terminal and observed_terminal:
        return "delivery_conflict"
    if previous_terminal:
        return previous
    if observed_terminal:
        return observed
    return max((previous, observed), key=PROGRESS_STATES.index)
