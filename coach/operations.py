"""Owned, bounded operations whose results may outlive a cancelled caller.

An async cancellation is only a request. A running thread is never falsely
counted as stopped. All pending work retains a capacity slot and its exception
is consumed. This is containment, not a way to kill Python threads/providers.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any


class OperationCapacityError(RuntimeError):
    """All operation slots are occupied, or the owner has closed."""


class OperationPool:
    def __init__(self, max_pending: int = 8) -> None:
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or max_pending < 1:
            raise ValueError("max_pending must be a positive integer")
        self.max_pending = max_pending
        self._tasks: dict[asyncio.Task[Any], bool] = {}
        self._closed = False
        self.rejected = 0
        self.high_water = 0

    @property
    def pending(self) -> int:
        return sum(not task.done() for task in self._tasks)

    def start(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> asyncio.Task[Any]:
        if self._closed or self.pending >= self.max_pending:
            self.rejected += 1
            raise OperationCapacityError("operation_capacity_exhausted_or_closed")
        is_async = inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
            getattr(callback, "__call__", None)
        )

        async def invoke() -> Any:
            if is_async:
                return await callback(*args, **kwargs)
            result = await asyncio.to_thread(callback, *args, **kwargs)
            return await result if inspect.isawaitable(result) else result

        task = asyncio.create_task(invoke(), name="coach-owned-operation")
        self._tasks[task] = is_async
        self.high_water = max(self.high_water, self.pending)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.pop(task, None)
        if not task.cancelled():
            task.exception()  # Harvest late exceptions even when the caller has left.

    def cancel(self, task: asyncio.Task[Any]) -> None:
        if self._tasks.get(task) and not task.done() and not task.cancelling():
            task.cancel()
        # A sync invocation keeps running, and therefore keeps its capacity slot.

    async def close(self, timeout_s: float = 0.05) -> int:
        self._closed = True
        tasks = tuple(task for task in self._tasks if not task.done())
        for task in tasks:
            self.cancel(task)
        if tasks:
            await asyncio.wait(tasks, timeout=max(0.0, timeout_s))
        return self.pending
