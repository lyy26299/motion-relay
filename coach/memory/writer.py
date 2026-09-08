"""Bounded, single-threaded durable writer for motion facts.

Pose callbacks run on the media event loop.  This writer owns its SQLite
connection in a dedicated thread, so a database stall cannot delay inference
or the browser media path.  Batches are idempotent and a completed repetition
is committed in the same transaction as its ``rep_completed`` event.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from coach.memory.store import MemoryStore
from coach.models import CoachEvent, RepRecord

WriteStatus = Literal["starting", "ready", "queued", "writing", "saved", "failed", "closed"]


@dataclass(frozen=True, slots=True)
class FactBatch:
    events: tuple[CoachEvent, ...]
    reps: tuple[RepRecord, ...]


class LedgerWriter:
    """Persist motion facts without doing synchronous SQLite work in callbacks."""

    def __init__(
        self,
        *,
        path: str | Path,
        user_id: str,
        session_id: str,
        exercise: str = "squat",
        rule_version: str = "squat-v1",
        queue_size: int = 256,
        status_sink: Callable[[WriteStatus, str], None] | None = None,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self.path = str(path)
        self.user_id = str(user_id)
        self.session_id = str(session_id)
        self.exercise = str(exercise)
        self.rule_version = str(rule_version)
        self._queue: queue.Queue[FactBatch | None] = queue.Queue(maxsize=queue_size)
        self._status_sink = status_sink
        self._status: WriteStatus = "closed"
        self._detail = ""
        self._status_lock = threading.Lock()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._close_status = "completed"

    @property
    def status(self) -> WriteStatus:
        with self._status_lock:
            return self._status

    @property
    def detail(self) -> str:
        with self._status_lock:
            return self._detail

    @property
    def error(self) -> BaseException | None:
        return self._error

    def _set_status(self, status: WriteStatus, detail: str = "") -> None:
        with self._status_lock:
            self._status = status
            self._detail = detail
        if self._status_sink is not None:
            with suppress(Exception):
                self._status_sink(status, detail)

    def start_and_wait(self, timeout: float = 10.0) -> None:
        if self._thread is not None:
            raise RuntimeError("ledger writer already started")
        self._set_status("starting", "正在打开训练事实账本")
        self._thread = threading.Thread(
            target=self._run,
            name="coach-ledger-writer",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(max(0.1, float(timeout))):
            raise TimeoutError("timed out opening the coaching memory store")
        if self._error is not None:
            raise RuntimeError("unable to open the coaching memory store") from self._error

    def submit(
        self,
        events: tuple[CoachEvent, ...] = (),
        reps: tuple[RepRecord, ...] = (),
    ) -> bool:
        """Enqueue a batch and return false if the bounded queue is full."""

        if not events and not reps:
            return True
        if self._thread is None or self._stopped.is_set() or self.status in {"failed", "closed"}:
            self._set_status("failed", "账本 writer 未运行")
            return False
        batch = FactBatch(tuple(events), tuple(reps))
        try:
            self._queue.put_nowait(batch)
        except queue.Full:
            self._set_status("failed", "账本队列已满，事实未确认落盘")
            return False
        self._set_status("queued", f"待写入 {self._queue.qsize()} 批")
        return True

    def close(self, *, status: str = "completed", timeout: float = 8.0) -> None:
        thread = self._thread
        if thread is None or self._stopped.is_set():
            return
        status = str(status).strip() or "completed"
        with self._status_lock:
            self._close_status = status
        # The sentinel is queued after all accepted batches, so normal close
        # drains durable facts before marking the session finished.
        try:
            self._queue.put(None, timeout=max(0.1, float(timeout)))
        except queue.Full:
            self._set_status("failed", "关闭时无法排空账本队列")
        thread.join(max(0.1, float(timeout)))
        if thread.is_alive():
            self._set_status("failed", "账本 writer 关闭超时")

    def _run(self) -> None:
        store: MemoryStore | None = None
        try:
            store = MemoryStore(self.path)
            store.ensure_user(self.user_id)
            store.create_session(
                self.user_id,
                session_id=self.session_id,
                exercise=self.exercise,
                rule_version=self.rule_version,
            )
            self._set_status("ready", "账本已连接")
            self._ready.set()
            while True:
                batch = self._queue.get()
                try:
                    if batch is None:
                        break
                    self._set_status("writing", f"写入 {len(batch.events)} 个事件")
                    self._write_batch(store, batch)
                    self._set_status("saved", "事实已落盘")
                finally:
                    self._queue.task_done()
            status = self._close_status
            store.finish_session(self.user_id, self.session_id, status=status)
            self._set_status(
                "closed",
                "训练事实已保存" if status == "completed" else "训练事实已保存，训练未正常结束",
            )
        except BaseException as exc:  # noqa: BLE001 - writer must report all failures
            self._error = exc
            self._set_status("failed", str(exc)[:200])
            self._ready.set()
        finally:
            if store is not None:
                store.close()
            self._stopped.set()

    def _write_batch(self, store: MemoryStore, batch: FactBatch) -> None:
        reps_by_id = {rep.rep_id: rep for rep in batch.reps}
        handled_reps: set[str] = set()
        with store.transaction():
            for event in batch.events:
                rep_id = event.facts.get("rep_id") if isinstance(event.facts, dict) else None
                rep = reps_by_id.get(rep_id)
                store.record_event_and_rep(
                    self.user_id,
                    self.session_id,
                    event,
                    rep,
                    source={
                        "type": "motion_fsm",
                        "rule_version": event.rule_version,
                    },
                )
                if rep is not None:
                    handled_reps.add(rep.rep_id)
            for rep in batch.reps:
                if rep.rep_id not in handled_reps:
                    store.record_rep(self.user_id, self.session_id, rep)
