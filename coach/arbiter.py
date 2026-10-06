"""Single-slot feedback admission: no backlog of obsolete spoken corrections.

This is an application-loop-owned policy, not a TTS engine. Pending queue
capacity is deliberately zero: a lower-priority proposal gets an explicit
rejection instead of speaking several seconds later. Native provider speech
is not governed here until it is routed through the application boundary.
"""
from __future__ import annotations

import math
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from enum import IntEnum


class FeedbackKind(IntEnum):
    SILENCE = 0
    SUMMARY = 10
    ENCOURAGEMENT = 20
    REP = 50
    INSTRUCTION = 60
    CORRECTION = 70
    ANSWER = 80
    SAFETY = 90
    EMERGENCY = 100


@dataclass(frozen=True, slots=True)
class FeedbackLease:
    generation: int
    kind: FeedbackKind
    key: str
    deadline: float


@dataclass(frozen=True, slots=True)
class Admission:
    lease: FeedbackLease | None
    reason: str
    preempted: bool = False


class FeedbackArbiter:
    """One active lease, zero pending messages, bounded deduplication history."""
    COOLDOWN = {
        FeedbackKind.REP: 0.8,
        FeedbackKind.CORRECTION: 3.0,
        FeedbackKind.ENCOURAGEMENT: 8.0,
        FeedbackKind.SUMMARY: 20.0,
    }

    def __init__(self, *, clock=time.monotonic, history_size: int = 128) -> None:
        if isinstance(history_size, bool) or not isinstance(history_size, int) or history_size < 1:
            raise ValueError("history_size must be a positive integer")
        self.clock = clock
        self.history_size = history_size
        self._generation = 0
        self._active: FeedbackLease | None = None
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._next: dict[FeedbackKind, float] = {}
        self.metrics: Counter[str] = Counter()

    @property
    def active_kind(self) -> FeedbackKind | None:
        if self._active is not None and self.current(self._active):
            return self._active.kind
        return None

    def admit(self, kind: FeedbackKind, key: str, *, deadline: float) -> Admission:
        if not isinstance(kind, FeedbackKind) or not key or len(key) > 256:
            raise ValueError("feedback requires a known kind and a bounded nonempty key")
        if not math.isfinite(deadline):
            raise ValueError("deadline must be finite")
        now = self.clock()
        reason = ""
        if kind == FeedbackKind.SILENCE:
            reason = "silence"
        elif now >= deadline:
            reason = "stale"
        elif self._seen.get(key, -math.inf) > now:
            reason = "duplicate"
        elif now < self._next.get(kind, -math.inf):
            reason = "cooldown"
        elif self._active and self.current(self._active) and kind <= self._active.kind:
            reason = "busy"
        if reason:
            self.metrics[reason] += 1
            return Admission(None, reason)
        preempted = bool(self._active and self.current(self._active))
        self._generation += 1
        lease = FeedbackLease(self._generation, kind, key, deadline)
        self._active = lease
        self._seen[key] = deadline
        self._seen.move_to_end(key)
        while len(self._seen) > self.history_size:
            self._seen.popitem(last=False)
        self._next[kind] = now + self.COOLDOWN.get(kind, 0.0)
        self.metrics["admitted"] += 1
        self.metrics["preempted"] += int(preempted)
        return Admission(lease, "admitted", preempted)

    def current(self, lease: FeedbackLease) -> bool:
        return self._active == lease and self.clock() < lease.deadline

    def finish(self, lease: FeedbackLease) -> bool:
        if self._active != lease:
            return False
        self._active = None
        return True

    def interrupt(self) -> None:
        self._generation += 1
        self._active = None
        self.metrics["interrupted"] += 1
