"""Immutable JSON-shaped facts, frozen once at the working-memory boundary."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class FrozenFacts(dict):
    """JSON-compatible mapping with recursively immutable values.

    This protects ordinary consumers from accidental mutation, not hostile
    Python code invoking dict.__setitem__ directly. No arbitrary objects are
    admitted into a sensor fact snapshot.
    """
    def __init__(self, values=(), **kwargs):
        source = dict(values, **kwargs)
        dict.__init__(self, ((key, freeze(value)) for key, value in source.items()))

    def _readonly(self, *args, **kwargs):
        raise TypeError("fact snapshots are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _readonly
    __ior__ = _readonly

    def __deepcopy__(self, memo):
        return self


def freeze(value: Any) -> Any:
    if isinstance(value, FrozenFacts):
        return value
    if isinstance(value, Mapping):
        return FrozenFacts(value)
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("facts must contain JSON-shaped values")
