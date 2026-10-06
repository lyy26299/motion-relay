"""Connection-local response identity gate; no SDK, network, or audio devices.

Only an explicit current response ID may mutate output state. Retired IDs are
remembered in a bounded replay window, not an unbounded conversation history.
This gate deliberately does NOT infer which client request caused a response.
"""
from __future__ import annotations

from collections import Counter, OrderedDict


class ResponseWindow:
    """One active response, bounded retired identities, synchronous loop owner."""

    def __init__(self, *, history_size: int = 256) -> None:
        if isinstance(history_size, bool) or not isinstance(history_size, int) or history_size < 1:
            raise ValueError("history_size must be a positive integer")
        self.history_size = history_size
        self.active_id: str | None = None
        self.audio_closed = False
        self._retired: OrderedDict[str, None] = OrderedDict()
        self.metrics: Counter[str] = Counter()

    @staticmethod
    def valid_id(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip()) and len(value) <= 256

    @property
    def retired_count(self) -> int:
        return len(self._retired)

    def retired(self, response_id: object) -> bool:
        return self.valid_id(response_id) and response_id in self._retired

    def retire(self, response_id: object) -> None:
        if not self.valid_id(response_id):
            return
        self._retired[response_id] = None
        self._retired.move_to_end(response_id)
        while len(self._retired) > self.history_size:
            self._retired.popitem(last=False)

    def begin(self, response_id: object) -> str:
        if not self.valid_id(response_id):
            reason = "invalid_id"
        elif response_id in self._retired:
            reason = "retired_created"
        elif response_id == self.active_id:
            reason = "duplicate_created"
        elif self.active_id is not None:
            # An uncorrelated created event must not replace a live successor.
            self.retire(response_id)
            reason = "conflicting_created"
        else:
            self.active_id = response_id
            self.audio_closed = False
            reason = "started"
        self.metrics[reason] += 1
        return reason

    def accepts(self, response_id: object, *, audio: bool = False) -> bool:
        valid = (self.valid_id(response_id) and response_id == self.active_id
                 and (not audio or not self.audio_closed))
        if not valid:
            self.metrics["rejected_event"] += 1
        return valid

    def finish_audio(self, response_id: object) -> bool:
        if not self.accepts(response_id, audio=True):
            return False
        self.audio_closed = True
        self.metrics["audio_finished"] += 1
        return True

    def finish(self, response_id: object) -> bool:
        if not self.accepts(response_id):
            # Terminal-before-created also forbids subsequent resurrection.
            self.retire(response_id)
            return False
        self.retire(response_id)
        self.active_id = None
        self.audio_closed = False
        self.metrics["finished"] += 1
        return True

    def invalidate(self) -> None:
        self.retire(self.active_id)
        self.active_id = None
        self.audio_closed = False
        self.metrics["invalidated"] += 1
