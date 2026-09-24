"""Scope-bound business capability for monotonic feedback receipt updates.

The existing MemoryStore remains the transaction/SQL owner. This adapter uses
only its public methods, including the legacy update_feedback() no-field read.
The outer transaction covers BOTH scope checks and the read/merge/write cycle;
serializing asyncio callbacks alone would not protect independent connections.
"""
from __future__ import annotations

from dataclasses import dataclass

from coach.feedback_state import FEEDBACK_STATES, OBSERVATION_STATES, merge_feedback_status
from coach.memory.store import MemoryStore, ScopeError


@dataclass(frozen=True, slots=True)
class FeedbackUpdate:
    changed: bool
    reason: str
    status: str


@dataclass(frozen=True, slots=True)
class FeedbackRecorder:
    """One trusted runtime scope; callers provide observations, never user IDs.

    No method creates receipts, resurrects deleted users/sessions, or accepts
    played/acknowledged timestamps. The legacy store API remains available to
    trusted callers that possess actual playback evidence; it is not exposed
    to the model through this capability.
    """

    store: MemoryStore
    user_id: str
    session_id: str
    memory_epoch: int

    def __post_init__(self) -> None:
        for value in (self.user_id, self.session_id):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("feedback scope identifiers must be nonempty strings")
        if (isinstance(self.memory_epoch, bool) or not isinstance(self.memory_epoch, int)
                or self.memory_epoch < 0):
            raise ValueError("memory_epoch must be a nonnegative integer")

    def apply(
        self, feedback_id: str, state: str, response_id: str | None = None,
    ) -> FeedbackUpdate:
        """Merge one observation atomically; raise on invalid/revoked scope.

        A different nonempty response ID is rejected, never rebound. This is
        identity consistency, NOT evidence of client-request causality. The
        caller must still label the legacy ordered candidate as unverified.
        """
        if (not isinstance(feedback_id, str) or not feedback_id.strip()
                or len(feedback_id) > 256):
            raise ValueError("invalid feedback_id")
        if not isinstance(state, str) or state not in OBSERVATION_STATES:
            raise ValueError("unsupported feedback observation")
        if response_id is not None and (
            not isinstance(response_id, str) or not response_id.strip() or len(response_id) > 256
        ):
            raise ValueError("invalid response_id")
        # BEGIN IMMEDIATE + the store's RLock make this one linearizable update
        # even with multiple MemoryStore instances on the same SQLite file.
        with self.store.transaction():
            user = self.store.get_user(self.user_id)
            session = self.store.get_session(self.user_id, self.session_id)
            if (user is None or user["memory_epoch"] != self.memory_epoch
                    or session is None or session["memory_epoch"] != self.memory_epoch):
                raise ScopeError("feedback observation scope was revoked or replaced")
            # Compatibility read: with no fields the public method returns the
            # existing receipt without changing it. No schema/SQL leaks into
            # the bridge, and no row is silently created when it is missing.
            row = self.store.update_feedback(self.user_id, feedback_id)
            if row["session_id"] != self.session_id:
                raise ScopeError("feedback belongs to another session")
            previous = row["status"]
            if previous not in FEEDBACK_STATES:
                return FeedbackUpdate(False, "unmanaged_existing_status", previous)
            bound_id = row["response_id"]
            if bound_id is not None and response_id is not None and bound_id != response_id:
                return FeedbackUpdate(False, "response_id_conflict", previous)
            merged = merge_feedback_status(previous, state)
            updates = {}
            if merged != previous:
                updates["status"] = merged
            if bound_id is None and response_id is not None:
                updates["response_id"] = response_id
            # A generation observation must NEVER reset interrupted/played to
            # unknown. Preserve explicit independent playback evidence too.
            if (state == "interrupted" and row["played_at"] is None
                    and row["acknowledged_at"] is None
                    and row["playback_state"] not in {"interrupted", "played", "acknowledged"}):
                updates["playback_state"] = "interrupted"
            if updates:
                self.store.update_feedback(self.user_id, feedback_id, **updates)
            if merged == "delivery_conflict" and previous != merged:
                reason = "outcome_conflict"
            elif merged != previous:
                reason = "advanced"
            elif updates:
                reason = "metadata_enriched"
            elif previous == state:
                reason = "duplicate"
            else:
                reason = "regression_ignored"
            return FeedbackUpdate(bool(updates), reason, merged)
