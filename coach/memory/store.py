"""SQLite-backed fact ledger for the local coaching agent.

The store is deliberately synchronous and dependency free.  Callers that run in
an asyncio loop should use ``asyncio.to_thread`` (or their own single writer
worker).  The database never accepts arbitrary SQL from an agent/model; all
public operations below use fixed, parameterized statements and enforce the
user scope in the query itself.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


class MemoryStoreError(RuntimeError):
    """Base exception for durable memory operations."""


class ScopeError(MemoryStoreError):
    """Raised when an object does not belong to the requested user scope."""


class NotFoundError(MemoryStoreError):
    """Raised when a required parent object is absent."""


def _json(value: Any) -> str:
    """Encode JSON fields deterministically and without accepting NaN values."""

    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _decode(value: str | bytes | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


class MemoryStore:
    """A small, user-scoped SQLite fact ledger.

    ``path`` may be a filesystem path or ``":memory:"``.  A single connection
    is protected by an ``RLock`` so a store can safely be used by a producer and
    a background writer in one process.  Each mutating public method commits an
    atomic transaction; ``transaction()`` can be used to group event and rep
    writes into one commit.
    """

    def __init__(self, path: str | Path = "coach_memory.sqlite3", *, timeout: float = 5.0) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._closed = False
        # check_same_thread=False is intentional: async callers commonly send
        # work through a dedicated thread, while the lock keeps this connection
        # serial and sqlite transactions coherent.
        self._conn = sqlite3.connect(
            self.path,
            timeout=timeout,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            # WAL is persistent for file databases and harmlessly reports
            # ``memory`` for an in-memory connection.
            self._journal_mode = str(self._conn.execute("PRAGMA journal_mode = WAL").fetchone()[0])
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._migrate()

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the connection for diagnostics only; do not issue model SQL."""

        return self._conn

    @property
    def journal_mode(self) -> str:
        return self._journal_mode

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise MemoryStoreError("memory store is closed")

    @contextmanager
    def transaction(self):
        """Run a group of store operations atomically.

        Nested transactions are represented by savepoints.  The context yields
        this store, making ``with store.transaction() as tx`` convenient.
        """

        with self._lock:
            self._ensure_open()
            nested = self._conn.in_transaction
            savepoint = f"sp_{uuid.uuid4().hex}"
            if nested:
                self._conn.execute(f'SAVEPOINT "{savepoint}"')
            else:
                self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                if nested:
                    self._conn.execute(f'ROLLBACK TO SAVEPOINT "{savepoint}"')
                    self._conn.execute(f'RELEASE SAVEPOINT "{savepoint}"')
                else:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                if nested:
                    self._conn.execute(f'RELEASE SAVEPOINT "{savepoint}"')
                else:
                    self._conn.execute("COMMIT")

    def _write(self, fn):
        with self.transaction():
            return fn()

    def _migrate(self) -> None:
        """Create/upgrade the schema without destructive migrations."""

        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                display_name TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                memory_epoch INTEGER NOT NULL DEFAULT 0,
                retention_days INTEGER,
                deleted_at REAL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                exercise TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL,
                status TEXT NOT NULL DEFAULT 'active',
                rule_version TEXT,
                camera_view TEXT,
                settings_json TEXT NOT NULL DEFAULT '{}',
                memory_epoch INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user_time
                ON sessions(user_id, started_at DESC);

            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                kind TEXT NOT NULL,
                occurred_at REAL NOT NULL,
                frame_id INTEGER,
                rule_version TEXT,
                evidence_start_frame INTEGER,
                evidence_end_frame INTEGER,
                source_json TEXT NOT NULL DEFAULT '{}',
                facts_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                UNIQUE(session_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_events_user_time
                ON events(user_id, occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_events_session
                ON events(session_id, sequence);

            CREATE TABLE IF NOT EXISTS reps (
                rep_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                set_index INTEGER NOT NULL DEFAULT 1,
                rep_index INTEGER NOT NULL,
                valid INTEGER NOT NULL CHECK(valid IN (0, 1)),
                started_at REAL NOT NULL,
                completed_at REAL NOT NULL,
                duration_ms REAL,
                min_knee_angle_deg REAL,
                max_knee_angle_deg REAL,
                usable_sample_ratio REAL,
                reason_codes_json TEXT NOT NULL DEFAULT '[]',
                rule_version TEXT,
                evidence_start_frame INTEGER,
                evidence_end_frame INTEGER,
                created_at REAL NOT NULL,
                UNIQUE(session_id, set_index, rep_index)
            );
            CREATE INDEX IF NOT EXISTS idx_reps_user_time
                ON reps(user_id, completed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_reps_session
                ON reps(session_id, set_index, rep_index);

            CREATE TABLE IF NOT EXISTS feedback (
                feedback_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                event_id TEXT REFERENCES events(event_id) ON DELETE SET NULL,
                rep_id TEXT REFERENCES reps(rep_id) ON DELETE SET NULL,
                decision_id TEXT,
                response_id TEXT,
                cue_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'generated',
                playback_state TEXT NOT NULL DEFAULT 'unknown',
                triggered_at REAL NOT NULL,
                played_at REAL,
                acknowledged_at REAL,
                facts_json TEXT NOT NULL DEFAULT '{}',
                idempotency_key TEXT,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_feedback_user_time
                ON feedback(user_id, triggered_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_idempotency
                ON feedback(user_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL;

            CREATE TABLE IF NOT EXISTS profile_facts (
                fact_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                fact_key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                source TEXT NOT NULL,
                confirmed_at REAL NOT NULL,
                valid_until REAL,
                supersedes TEXT REFERENCES profile_facts(fact_id) ON DELETE SET NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                created_at REAL NOT NULL,
                UNIQUE(user_id, fact_id)
            );
            CREATE INDEX IF NOT EXISTS idx_profile_active
                ON profile_facts(user_id, fact_key, active, confirmed_at DESC);

            CREATE TABLE IF NOT EXISTS memory_notes (
                note_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                session_id TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
                summary TEXT NOT NULL,
                source_ids_json TEXT NOT NULL DEFAULT '[]',
                revision INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'draft',
                embedding_status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(user_id, note_id)
            );
            CREATE INDEX IF NOT EXISTS idx_notes_user_time
                ON memory_notes(user_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS session_summaries (
                summary_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                session_id TEXT REFERENCES sessions(session_id) ON DELETE CASCADE,
                summary TEXT NOT NULL,
                source_ids_json TEXT NOT NULL DEFAULT '[]',
                revision INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(user_id, summary_id)
            );
            CREATE INDEX IF NOT EXISTS idx_summaries_user_time
                ON session_summaries(user_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                chunk_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                document_version TEXT NOT NULL,
                section TEXT,
                page INTEGER,
                tags_json TEXT NOT NULL DEFAULT '[]',
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                review_status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                UNIQUE(document_id, document_version, content_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_review
                ON knowledge_chunks(review_status, document_id, document_version);

            CREATE TABLE IF NOT EXISTS vector_items (
                source_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                embedding_version TEXT NOT NULL,
                user_id TEXT REFERENCES users(user_id) ON DELETE CASCADE,
                vector_json TEXT,
                external_ref TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                PRIMARY KEY(source_id, revision, embedding_version)
            );

            CREATE TABLE IF NOT EXISTS outbox (
                job_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                job_type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                source_id TEXT,
                source_revision INTEGER,
                delete_epoch INTEGER NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL,
                locked_at REAL,
                completed_at REAL,
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(user_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_outbox_ready
                ON outbox(status, available_at, created_at);

            -- A tombstone survives account deletion and prevents an old
            -- asynchronous job from writing into a newly recreated scope.
            CREATE TABLE IF NOT EXISTS scope_epochs (
                user_id TEXT PRIMARY KEY,
                memory_epoch INTEGER NOT NULL DEFAULT 0,
                deleted_at REAL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS memory_notes_fts USING fts5(
                note_id UNINDEXED,
                user_id UNINDEXED,
                summary,
                tokenize='unicode61'
            );
            """
        )
        # A few columns/indexes are added defensively for databases created by
        # an earlier development build of this module.
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(feedback)")}
        if "idempotency_key" not in columns:
            self._conn.execute("ALTER TABLE feedback ADD COLUMN idempotency_key TEXT")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_idempotency "
            "ON feedback(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        self._conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("schema_version", str(SCHEMA_VERSION)),
        )

    # ------------------------------------------------------------------
    # Row conversion and bounded query helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _bounded_limit(value: int, *, maximum: int) -> int:
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        if value < 1:
            raise ValueError("limit must be positive")
        return min(value, maximum)

    @staticmethod
    def _safe_text(value: Any) -> str:
        return str(value).replace("\x00", "").strip()

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("expected a database row")
        return dict(row)

    @classmethod
    def _user_dict(cls, row: sqlite3.Row | None) -> dict[str, Any]:
        return cls._row_dict(row)

    @classmethod
    def _session_dict(cls, row: sqlite3.Row | None) -> dict[str, Any]:
        result = cls._row_dict(row)
        result["settings"] = _decode(result.pop("settings_json"), {})
        return result

    @classmethod
    def _event_dict(cls, row: sqlite3.Row | None) -> dict[str, Any]:
        result = cls._row_dict(row)
        result["source"] = _decode(result.pop("source_json"), {})
        result["facts"] = _decode(result.pop("facts_json"), {})
        return result

    @classmethod
    def _rep_dict(cls, row: sqlite3.Row | None) -> dict[str, Any]:
        result = cls._row_dict(row)
        result["valid"] = bool(result["valid"])
        result["reason_codes"] = tuple(_decode(result.pop("reason_codes_json"), []))
        return result

    @classmethod
    def _feedback_dict(cls, row: sqlite3.Row | None) -> dict[str, Any]:
        result = cls._row_dict(row)
        result["facts"] = _decode(result.pop("facts_json"), {})
        return result

    @classmethod
    def _profile_dict(cls, row: sqlite3.Row | None) -> dict[str, Any]:
        result = cls._row_dict(row)
        result["value"] = _decode(result.pop("value_json"), None)
        result["active"] = bool(result["active"])
        return result

    @classmethod
    def _summary_dict(cls, row: sqlite3.Row | None) -> dict[str, Any]:
        result = cls._row_dict(row)
        result["source_ids"] = _decode(result.pop("source_ids_json"), [])
        return result

    @classmethod
    def _note_dict(cls, row: sqlite3.Row | None) -> dict[str, Any]:
        result = cls._row_dict(row)
        result["source_ids"] = _decode(result.pop("source_ids_json"), [])
        return result

    @classmethod
    def _knowledge_dict(cls, row: sqlite3.Row | None) -> dict[str, Any]:
        result = cls._row_dict(row)
        result["tags"] = _decode(result.pop("tags_json"), [])
        return result

    @classmethod
    def _outbox_dict(cls, row: sqlite3.Row | None) -> dict[str, Any]:
        result = cls._row_dict(row)
        result["payload"] = _decode(result.pop("payload_json"), {})
        return result

    @classmethod
    def _training_dict(cls, row: sqlite3.Row | None) -> dict[str, Any]:
        result = cls._row_dict(row)
        for key in ("completed_reps", "valid_reps"):
            result[key] = int(result[key] or 0)
        return result

    def _list_json_rows(
        self,
        *,
        table: str,
        converter,
        user_id: str,
        session_id: str | None,
        limit: int,
        order_column: str,
    ) -> list[dict[str, Any]]:
        # These arguments are selected only from internal call sites above;
        # still keep the table/order allow-list explicit to avoid turning this
        # helper into an arbitrary SQL surface.
        if table not in {"session_summaries", "memory_notes"}:
            raise ValueError("unsupported memory table")
        if order_column not in {"updated_at", "created_at"}:
            raise ValueError("unsupported order column")
        limit = self._bounded_limit(limit, maximum=100)
        with self._lock:
            self._ensure_open()
            self._require_user(user_id)
            params: list[Any] = [user_id]
            where = ["user_id = ?"]
            if session_id is not None:
                where.append("session_id = ?")
                params.append(session_id)
            params.append(limit)
            rows = self._conn.execute(
                f"SELECT * FROM {table} WHERE " + " AND ".join(where) +
                f" ORDER BY {order_column} DESC LIMIT ?",
                params,
            ).fetchall()
            return [converter(row) for row in rows]

    def _owned_outbox(self, user_id: str, job_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM outbox WHERE user_id = ? AND job_id = ?", (user_id, job_id)
        ).fetchone()
        if row is not None:
            return row
        other = self._conn.execute(
            "SELECT user_id FROM outbox WHERE job_id = ?", (job_id,)
        ).fetchone()
        if other is not None:
            raise ScopeError("outbox job belongs to another user")
        raise NotFoundError(f"unknown outbox job: {job_id}")

    # ------------------------------------------------------------------
    # Scope and session lifecycle
    # ------------------------------------------------------------------

    def _scope_epoch(self, user_id: str) -> int:
        row = self._conn.execute(
            "SELECT memory_epoch FROM scope_epochs WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def _require_user(self, user_id: str, *, include_deleted: bool = False) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None or (not include_deleted and row["deleted_at"] is not None):
            raise ScopeError(f"unknown user scope: {user_id}")
        return row

    def ensure_user(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        retention_days: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Create a user scope or update its non-fact metadata idempotently."""

        user_id = str(user_id).strip()
        if not user_id:
            raise ValueError("user_id is required")
        if retention_days is not None and int(retention_days) < 0:
            raise ValueError("retention_days must be non-negative or None")
        now = time.time() if now is None else float(now)

        def write() -> dict[str, Any]:
            epoch = self._scope_epoch(user_id)
            row = self._conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO users(user_id, display_name, created_at, updated_at, "
                    "memory_epoch, retention_days, deleted_at) VALUES(?, ?, ?, ?, ?, ?, NULL)",
                    (user_id, display_name, now, now, epoch, retention_days),
                )
            else:
                self._conn.execute(
                    "UPDATE users SET display_name = COALESCE(?, display_name), "
                    "retention_days = COALESCE(?, retention_days), updated_at = ?, "
                    "deleted_at = NULL WHERE user_id = ?",
                    (display_name, retention_days, now, user_id),
                )
            return self._user_dict(self._conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone())

        return self._write(write)

    # Common spelling used by callers integrating a repository-style store.
    create_user = ensure_user

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT * FROM users WHERE user_id = ? AND deleted_at IS NULL", (user_id,)
            ).fetchone()
            return self._user_dict(row) if row else None

    def get_memory_epoch(self, user_id: str) -> int:
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT memory_epoch FROM users WHERE user_id = ? AND deleted_at IS NULL",
                (user_id,),
            ).fetchone()
            return int(row[0]) if row else self._scope_epoch(user_id)

    def create_session(
        self,
        user_id: str,
        *,
        session_id: str | None = None,
        exercise: str = "squat",
        started_at: float | None = None,
        status: str = "active",
        rule_version: str | None = None,
        camera_view: str | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or return a session, rejecting cross-user reuse."""

        user_id = str(user_id)
        session_id = session_id or f"session-{uuid.uuid4().hex}"
        exercise = str(exercise).strip()
        if not exercise:
            raise ValueError("exercise is required")
        started_at = time.time() if started_at is None else float(started_at)
        now = time.time()

        def write() -> dict[str, Any]:
            user = self._require_user(user_id)
            existing = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if existing:
                if existing["user_id"] != user_id:
                    raise ScopeError("session belongs to another user")
                return self._session_dict(existing)
            self._conn.execute(
                "INSERT INTO sessions(session_id, user_id, exercise, started_at, status, "
                "rule_version, camera_view, settings_json, memory_epoch, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    user_id,
                    exercise,
                    started_at,
                    status,
                    rule_version,
                    camera_view,
                    _json(dict(settings or {})),
                    int(user["memory_epoch"]),
                    now,
                    now,
                ),
            )
            return self._session_dict(self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone())

        return self._write(write)

    def get_session(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            ).fetchone()
            return self._session_dict(row) if row else None

    def finish_session(
        self,
        user_id: str,
        session_id: str,
        *,
        status: str = "completed",
        ended_at: float | None = None,
    ) -> dict[str, Any]:
        ended_at = time.time() if ended_at is None else float(ended_at)

        def write() -> dict[str, Any]:
            self._require_session(user_id, session_id)
            self._conn.execute(
                "UPDATE sessions SET status = ?, ended_at = ?, updated_at = ? "
                "WHERE user_id = ? AND session_id = ?",
                (status, ended_at, time.time(), user_id, session_id),
            )
            return self._session_dict(self._conn.execute(
                "SELECT * FROM sessions WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            ).fetchone())

        return self._write(write)

    def recover_interrupted_sessions(self, *, user_id: str | None = None) -> int:
        """Mark active sessions interrupted after an unclean process restart."""

        def write() -> int:
            if user_id is None:
                cursor = self._conn.execute(
                    "UPDATE sessions SET status = 'interrupted', ended_at = COALESCE(ended_at, ?), "
                    "updated_at = ? WHERE status = 'active'",
                    (time.time(), time.time()),
                )
            else:
                self._require_user(user_id)
                cursor = self._conn.execute(
                    "UPDATE sessions SET status = 'interrupted', ended_at = COALESCE(ended_at, ?), "
                    "updated_at = ? WHERE user_id = ? AND status = 'active'",
                    (time.time(), time.time(), user_id),
                )
            return int(cursor.rowcount)

        return self._write(write)

    def _require_session(self, user_id: str, session_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE user_id = ? AND session_id = ?",
            (user_id, session_id),
        ).fetchone()
        if row is None:
            # Distinguish an existing session in another scope from a missing
            # session, without exposing any of its facts.
            other = self._conn.execute(
                "SELECT user_id FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if other is not None:
                raise ScopeError("session belongs to another user")
            raise NotFoundError(f"unknown session: {session_id}")
        return row

    # ------------------------------------------------------------------
    # Events and repetitions
    # ------------------------------------------------------------------

    @staticmethod
    def _event_values(event: Any) -> dict[str, Any]:
        if is_dataclass(event):
            raw = asdict(event)
        elif isinstance(event, Mapping):
            raw = dict(event)
        else:
            raw = {name: getattr(event, name) for name in (
                "event_id", "kind", "occurred_at", "frame_id", "rule_version",
                "evidence_start_frame", "evidence_end_frame", "facts"
            ) if hasattr(event, name)}
        if not raw.get("event_id"):
            raise ValueError("event_id is required")
        if not raw.get("kind"):
            raise ValueError("event kind is required")
        return raw

    @staticmethod
    def _rep_values(rep: Any) -> dict[str, Any]:
        if is_dataclass(rep):
            raw = asdict(rep)
        elif isinstance(rep, Mapping):
            raw = dict(rep)
        else:
            names = (
                "rep_id", "rep_index", "valid", "started_at", "completed_at", "duration_ms",
                "min_knee_angle_deg", "max_knee_angle_deg", "usable_sample_ratio",
                "reason_codes", "rule_version", "evidence_start_frame", "evidence_end_frame",
            )
            raw = {name: getattr(rep, name) for name in names if hasattr(rep, name)}
        if not raw.get("rep_id"):
            raise ValueError("rep_id is required")
        if raw.get("rep_index") is None:
            raise ValueError("rep_index is required")
        return raw

    def _next_event_sequence(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row[0])

    def _event_insert(
        self,
        user_id: str,
        session_id: str,
        event: Any,
        *,
        sequence: int | None = None,
        source: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        self._require_user(user_id)
        self._require_session(user_id, session_id)
        raw = self._event_values(event)
        event_id = str(raw["event_id"])
        sequence = sequence if sequence is not None else raw.get("sequence")
        # A frame can legitimately produce several events (for example a
        # phase change and a completed rep on the same observation).  Frame IDs
        # therefore cannot be the event sequence.  Callers may provide a
        # replay-stable sequence; otherwise allocate one inside this transaction.
        sequence = int(sequence) if sequence is not None else self._next_event_sequence(session_id)
        now = time.time() if now is None else float(now)
        existing_by_id = self._conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if existing_by_id:
            if existing_by_id["user_id"] != user_id or existing_by_id["session_id"] != session_id:
                raise ScopeError("event belongs to another user or session")
            return self._event_dict(existing_by_id)
        existing_by_seq = self._conn.execute(
            "SELECT * FROM events WHERE user_id = ? AND session_id = ? AND sequence = ?",
            (user_id, session_id, sequence),
        ).fetchone()
        if existing_by_seq:
            return self._event_dict(existing_by_seq)
        self._conn.execute(
            "INSERT INTO events(event_id, user_id, session_id, sequence, kind, occurred_at, "
            "frame_id, rule_version, evidence_start_frame, evidence_end_frame, source_json, "
            "facts_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                user_id,
                session_id,
                sequence,
                str(raw["kind"]),
                float(raw.get("occurred_at", now)),
                raw.get("frame_id"),
                raw.get("rule_version"),
                raw.get("evidence_start_frame"),
                raw.get("evidence_end_frame"),
                _json(dict(source or raw.get("source") or {})),
                _json(dict(raw.get("facts") or {})),
                now,
            ),
        )
        return self._event_dict(self._conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone())

    def record_event(
        self,
        user_id: str,
        session_id: str,
        event: Any = None,
        *,
        sequence: int | None = None,
        source: Mapping[str, Any] | None = None,
        **event_fields: Any,
    ) -> dict[str, Any]:
        """Persist one event, treating event ID/sequence replays as no-ops.

        ``event`` may be a ``CoachEvent`` or mapping.  For integration code a
        mapping can be omitted and fields supplied as keywords.
        """

        if event is None:
            event = event_fields
        elif event_fields:
            merged = dict(event) if isinstance(event, Mapping) else asdict(event)
            merged.update(event_fields)
            event = merged

        return self._write(lambda: self._event_insert(
            user_id, session_id, event, sequence=sequence, source=source
        ))

    put_event = record_event

    def _rep_insert(
        self,
        user_id: str,
        session_id: str,
        rep: Any,
        *,
        set_index: int = 1,
        now: float | None = None,
    ) -> dict[str, Any]:
        self._require_user(user_id)
        self._require_session(user_id, session_id)
        raw = self._rep_values(rep)
        rep_id = str(raw["rep_id"])
        now = time.time() if now is None else float(now)
        existing_by_id = self._conn.execute(
            "SELECT * FROM reps WHERE rep_id = ?", (rep_id,)
        ).fetchone()
        if existing_by_id:
            if existing_by_id["user_id"] != user_id or existing_by_id["session_id"] != session_id:
                raise ScopeError("rep belongs to another user or session")
            return self._rep_dict(existing_by_id)
        existing_by_index = self._conn.execute(
            "SELECT * FROM reps WHERE user_id = ? AND session_id = ? AND set_index = ? AND rep_index = ?",
            (user_id, session_id, int(set_index), int(raw["rep_index"])),
        ).fetchone()
        if existing_by_index:
            return self._rep_dict(existing_by_index)
        started = raw.get("started_at")
        completed = raw.get("completed_at")
        if started is None or completed is None:
            raise ValueError("rep started_at and completed_at are required")
        self._conn.execute(
            "INSERT INTO reps(rep_id, user_id, session_id, set_index, rep_index, valid, "
            "started_at, completed_at, duration_ms, min_knee_angle_deg, max_knee_angle_deg, "
            "usable_sample_ratio, reason_codes_json, rule_version, evidence_start_frame, "
            "evidence_end_frame, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rep_id,
                user_id,
                session_id,
                int(set_index),
                int(raw["rep_index"]),
                int(bool(raw.get("valid", False))),
                float(started),
                float(completed),
                raw.get("duration_ms"),
                raw.get("min_knee_angle_deg"),
                raw.get("max_knee_angle_deg"),
                raw.get("usable_sample_ratio"),
                _json(list(raw.get("reason_codes") or ())),
                raw.get("rule_version"),
                raw.get("evidence_start_frame"),
                raw.get("evidence_end_frame"),
                now,
            ),
        )
        return self._rep_dict(self._conn.execute(
            "SELECT * FROM reps WHERE rep_id = ?", (rep_id,)
        ).fetchone())

    def record_rep(
        self,
        user_id: str,
        session_id: str,
        rep: Any = None,
        *,
        set_index: int = 1,
        **rep_fields: Any,
    ) -> dict[str, Any]:
        if rep is None:
            rep = rep_fields
        elif rep_fields:
            merged = dict(rep) if isinstance(rep, Mapping) else asdict(rep)
            merged.update(rep_fields)
            rep = merged
        return self._write(lambda: self._rep_insert(
            user_id, session_id, rep, set_index=set_index
        ))

    put_rep = record_rep

    def record_event_and_rep(
        self,
        user_id: str,
        session_id: str,
        event: Any,
        rep: Any | None = None,
        *,
        sequence: int | None = None,
        set_index: int = 1,
        source: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Atomically write a motion event and optional completed repetition."""

        with self.transaction():
            event_row = self._event_insert(
                user_id, session_id, event, sequence=sequence, source=source
            )
            rep_row = self._rep_insert(
                user_id, session_id, rep, set_index=set_index
            ) if rep is not None else None
            return event_row, rep_row

    put_event_and_rep = record_event_and_rep

    def list_events(
        self,
        user_id: str,
        *,
        session_id: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = self._bounded_limit(limit, maximum=500)
        with self._lock:
            self._ensure_open()
            params: list[Any] = [user_id]
            where = ["user_id = ?"]
            if session_id is not None:
                where.append("session_id = ?")
                params.append(session_id)
            if kind is not None:
                where.append("kind = ?")
                params.append(kind)
            params.append(limit)
            rows = self._conn.execute(
                "SELECT * FROM events WHERE " + " AND ".join(where) +
                " ORDER BY occurred_at, sequence LIMIT ?", params
            ).fetchall()
            return [self._event_dict(row) for row in rows]

    def get_event(self, user_id: str, event_id: str) -> dict[str, Any] | None:
        """Read one event inside a user's scope."""

        with self._lock:
            self._ensure_open()
            self._require_user(user_id)
            row = self._conn.execute(
                "SELECT * FROM events WHERE user_id = ? AND event_id = ?",
                (user_id, str(event_id)),
            ).fetchone()
            return self._event_dict(row) if row else None

    def list_reps(
        self,
        user_id: str,
        *,
        session_id: str | None = None,
        set_index: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = self._bounded_limit(limit, maximum=500)
        with self._lock:
            self._ensure_open()
            params: list[Any] = [user_id]
            where = ["user_id = ?"]
            if session_id is not None:
                where.append("session_id = ?")
                params.append(session_id)
            if set_index is not None:
                where.append("set_index = ?")
                params.append(int(set_index))
            params.append(limit)
            rows = self._conn.execute(
                "SELECT * FROM reps WHERE " + " AND ".join(where) +
                " ORDER BY completed_at, set_index, rep_index LIMIT ?", params
            ).fetchall()
            return [self._rep_dict(row) for row in rows]

    def get_rep(self, user_id: str, rep_id: str) -> dict[str, Any] | None:
        """Read one repetition inside a user's scope."""

        with self._lock:
            self._ensure_open()
            self._require_user(user_id)
            row = self._conn.execute(
                "SELECT * FROM reps WHERE user_id = ? AND rep_id = ?",
                (user_id, str(rep_id)),
            ).fetchone()
            return self._rep_dict(row) if row else None

    # ------------------------------------------------------------------
    # Feedback and profile facts
    # ------------------------------------------------------------------

    def record_feedback(
        self,
        user_id: str,
        session_id: str,
        *,
        feedback_id: str | None = None,
        cue_text: str,
        event_id: str | None = None,
        rep_id: str | None = None,
        decision_id: str | None = None,
        response_id: str | None = None,
        status: str = "generated",
        playback_state: str = "unknown",
        triggered_at: float | None = None,
        facts: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Store a cue and its delivery state; no state implies playback."""

        cue_text = str(cue_text).strip()
        if not cue_text:
            raise ValueError("cue_text is required")
        feedback_id = feedback_id or f"feedback-{uuid.uuid4().hex}"
        triggered_at = time.time() if triggered_at is None else float(triggered_at)

        def write() -> dict[str, Any]:
            self._require_user(user_id)
            self._require_session(user_id, session_id)
            if event_id is not None:
                row = self._conn.execute(
                    "SELECT user_id, session_id FROM events WHERE event_id = ?", (event_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"unknown event: {event_id}")
                if row["user_id"] != user_id or row["session_id"] != session_id:
                    raise ScopeError("event belongs to another user or session")
            if rep_id is not None:
                row = self._conn.execute(
                    "SELECT user_id, session_id FROM reps WHERE rep_id = ?", (rep_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"unknown rep: {rep_id}")
                if row["user_id"] != user_id or row["session_id"] != session_id:
                    raise ScopeError("rep belongs to another user or session")
            existing = self._conn.execute(
                "SELECT * FROM feedback WHERE feedback_id = ?", (feedback_id,)
            ).fetchone()
            if existing:
                if existing["user_id"] != user_id or existing["session_id"] != session_id:
                    raise ScopeError("feedback belongs to another user or session")
                return self._feedback_dict(existing)
            if idempotency_key is not None:
                existing = self._conn.execute(
                    "SELECT * FROM feedback WHERE user_id = ? AND idempotency_key = ?",
                    (user_id, idempotency_key),
                ).fetchone()
                if existing:
                    return self._feedback_dict(existing)
            now = time.time()
            self._conn.execute(
                "INSERT INTO feedback(feedback_id, user_id, session_id, event_id, rep_id, "
                "decision_id, response_id, cue_text, status, playback_state, triggered_at, "
                "facts_json, idempotency_key, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    feedback_id,
                    user_id,
                    session_id,
                    event_id,
                    rep_id,
                    decision_id,
                    response_id,
                    cue_text,
                    status,
                    playback_state,
                    triggered_at,
                    _json(dict(facts or {})),
                    idempotency_key,
                    now,
                ),
            )
            return self._feedback_dict(self._conn.execute(
                "SELECT * FROM feedback WHERE feedback_id = ?", (feedback_id,)
            ).fetchone())

        return self._write(write)

    put_feedback = record_feedback

    def update_feedback(
        self,
        user_id: str,
        feedback_id: str,
        *,
        status: str | None = None,
        playback_state: str | None = None,
        played_at: float | None = None,
        acknowledged_at: float | None = None,
        response_id: str | None = None,
    ) -> dict[str, Any]:
        def write() -> dict[str, Any]:
            row = self._conn.execute(
                "SELECT * FROM feedback WHERE user_id = ? AND feedback_id = ?",
                (user_id, feedback_id),
            ).fetchone()
            if row is None:
                other = self._conn.execute(
                    "SELECT user_id FROM feedback WHERE feedback_id = ?", (feedback_id,)
                ).fetchone()
                if other is not None:
                    raise ScopeError("feedback belongs to another user")
                raise NotFoundError(f"unknown feedback: {feedback_id}")
            updates: list[str] = []
            params: list[Any] = []
            for column, value in (
                ("status", status), ("playback_state", playback_state),
                ("played_at", played_at), ("acknowledged_at", acknowledged_at),
                ("response_id", response_id),
            ):
                if value is not None:
                    updates.append(f"{column} = ?")
                    params.append(value)
            if updates:
                params.extend([user_id, feedback_id])
                self._conn.execute(
                    "UPDATE feedback SET " + ", ".join(updates) +
                    " WHERE user_id = ? AND feedback_id = ?", params
                )
            return self._feedback_dict(self._conn.execute(
                "SELECT * FROM feedback WHERE user_id = ? AND feedback_id = ?",
                (user_id, feedback_id),
            ).fetchone())

        return self._write(write)

    update_feedback_playback = update_feedback

    def put_profile_fact(
        self,
        user_id: str,
        fact_key: str,
        value: Any,
        *,
        source: str = "user",
        confirmed_at: float | None = None,
        valid_until: float | None = None,
        fact_id: str | None = None,
        supersedes: str | None = None,
    ) -> dict[str, Any]:
        """Append a confirmed profile revision and deactivate its predecessor."""

        fact_key = str(fact_key).strip()
        if not fact_key:
            raise ValueError("fact_key is required")
        if not str(source).strip():
            raise ValueError("source is required")
        fact_id = fact_id or f"fact-{uuid.uuid4().hex}"
        confirmed_at = time.time() if confirmed_at is None else float(confirmed_at)
        requested_supersedes = supersedes

        def write() -> dict[str, Any]:
            self._require_user(user_id)
            existing = self._conn.execute(
                "SELECT * FROM profile_facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if existing:
                if existing["user_id"] != user_id:
                    raise ScopeError("profile fact belongs to another user")
                return self._profile_dict(existing)
            predecessor = None
            supersedes_value = requested_supersedes
            if supersedes_value is not None:
                predecessor = self._conn.execute(
                    "SELECT * FROM profile_facts WHERE fact_id = ?", (supersedes_value,)
                ).fetchone()
                if predecessor is None:
                    raise NotFoundError(f"unknown profile fact: {supersedes}")
                if predecessor["user_id"] != user_id:
                    raise ScopeError("profile fact belongs to another user")
            else:
                predecessor = self._conn.execute(
                    "SELECT * FROM profile_facts WHERE user_id = ? AND fact_key = ? AND active = 1 "
                    "ORDER BY confirmed_at DESC LIMIT 1", (user_id, fact_key)
                ).fetchone()
                if predecessor is not None:
                    supersedes_value = predecessor["fact_id"]
            if predecessor is not None:
                self._conn.execute(
                    "UPDATE profile_facts SET active = 0 WHERE user_id = ? AND fact_id = ?",
                    (user_id, predecessor["fact_id"]),
                )
            self._conn.execute(
                "INSERT INTO profile_facts(fact_id, user_id, fact_key, value_json, source, "
                "confirmed_at, valid_until, supersedes, active, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (fact_id, user_id, fact_key, _json(value), str(source), confirmed_at,
                 valid_until, supersedes_value, time.time()),
            )
            return self._profile_dict(self._conn.execute(
                "SELECT * FROM profile_facts WHERE fact_id = ?", (fact_id,)
            ).fetchone())

        return self._write(write)

    def list_profile_facts(
        self,
        user_id: str,
        *,
        include_inactive: bool = False,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        now = time.time() if now is None else float(now)
        with self._lock:
            self._ensure_open()
            self._require_user(user_id)
            where = ["user_id = ?"]
            params: list[Any] = [user_id]
            if not include_inactive:
                where.append("active = 1")
                where.append("(valid_until IS NULL OR valid_until > ?)")
                params.append(now)
            rows = self._conn.execute(
                "SELECT * FROM profile_facts WHERE " + " AND ".join(where) +
                " ORDER BY confirmed_at DESC", params
            ).fetchall()
            return [self._profile_dict(row) for row in rows]

    def get_profile(self, user_id: str, *, now: float | None = None) -> list[dict[str, Any]]:
        return self.list_profile_facts(user_id, now=now)

    # ------------------------------------------------------------------
    # Summaries / notes / knowledge and outbox
    # ------------------------------------------------------------------

    def put_summary(
        self,
        user_id: str,
        *,
        summary: str,
        session_id: str | None = None,
        summary_id: str | None = None,
        source_ids: Iterable[str] = (),
        revision: int = 1,
        status: str = "draft",
        updated_at: float | None = None,
    ) -> dict[str, Any]:
        """Upsert a deterministic/session summary with source references."""

        summary = str(summary).strip()
        if not summary:
            raise ValueError("summary is required")
        summary_id = summary_id or f"summary-{uuid.uuid4().hex}"
        revision = int(revision)
        if revision < 1:
            raise ValueError("revision must be positive")
        updated_at = time.time() if updated_at is None else float(updated_at)

        def write() -> dict[str, Any]:
            self._require_user(user_id)
            if session_id is not None:
                self._require_session(user_id, session_id)
            existing = self._conn.execute(
                "SELECT * FROM session_summaries WHERE summary_id = ?", (summary_id,)
            ).fetchone()
            if existing:
                if existing["user_id"] != user_id:
                    raise ScopeError("summary belongs to another user")
                # Replays with an older/equal revision are no-ops.  A newer
                # revision updates the same identity while retaining its ID.
                if revision <= int(existing["revision"]):
                    return self._summary_dict(existing)
                self._conn.execute(
                    "UPDATE session_summaries SET summary = ?, source_ids_json = ?, revision = ?, "
                    "status = ?, updated_at = ?, session_id = COALESCE(?, session_id) "
                    "WHERE user_id = ? AND summary_id = ?",
                    (self._safe_text(summary), _json(list(source_ids)), revision, status, updated_at,
                     session_id, user_id, summary_id),
                )
            else:
                self._conn.execute(
                    "INSERT INTO session_summaries(summary_id, user_id, session_id, summary, "
                    "source_ids_json, revision, status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (summary_id, user_id, session_id, summary, _json(list(source_ids)), revision,
                     status, updated_at, updated_at),
                )
            return self._summary_dict(self._conn.execute(
                "SELECT * FROM session_summaries WHERE summary_id = ?", (summary_id,)
            ).fetchone())

        return self._write(write)

    put_session_summary = put_summary

    def put_memory_note(
        self,
        user_id: str,
        *,
        note: str,
        session_id: str | None = None,
        note_id: str | None = None,
        source_ids: Iterable[str] = (),
        revision: int = 1,
        status: str = "draft",
        embedding_status: str = "pending",
        updated_at: float | None = None,
    ) -> dict[str, Any]:
        note = str(note).strip()
        if not note:
            raise ValueError("note is required")
        note_id = note_id or f"note-{uuid.uuid4().hex}"
        revision = int(revision)
        if revision < 1:
            raise ValueError("revision must be positive")
        updated_at = time.time() if updated_at is None else float(updated_at)

        def write() -> dict[str, Any]:
            self._require_user(user_id)
            if session_id is not None:
                self._require_session(user_id, session_id)
            existing = self._conn.execute(
                "SELECT * FROM memory_notes WHERE note_id = ?", (note_id,)
            ).fetchone()
            if existing:
                if existing["user_id"] != user_id:
                    raise ScopeError("memory note belongs to another user")
                if revision <= int(existing["revision"]):
                    return self._note_dict(existing)
                self._conn.execute(
                    "UPDATE memory_notes SET summary = ?, source_ids_json = ?, revision = ?, status = ?, "
                    "embedding_status = ?, updated_at = ?, session_id = COALESCE(?, session_id) "
                    "WHERE user_id = ? AND note_id = ?",
                    (note, _json(list(source_ids)), revision, status, embedding_status, updated_at,
                     session_id, user_id, note_id),
                )
            else:
                self._conn.execute(
                    "INSERT INTO memory_notes(note_id, user_id, session_id, summary, source_ids_json, "
                    "revision, status, embedding_status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (note_id, user_id, session_id, note, _json(list(source_ids)), revision, status,
                     embedding_status, updated_at, updated_at),
                )
            self._conn.execute("DELETE FROM memory_notes_fts WHERE note_id = ?", (note_id,))
            self._conn.execute(
                "INSERT INTO memory_notes_fts(note_id, user_id, summary) VALUES(?, ?, ?)",
                (note_id, user_id, note),
            )
            return self._note_dict(self._conn.execute(
                "SELECT * FROM memory_notes WHERE note_id = ?", (note_id,)
            ).fetchone())

        return self._write(write)

    put_note = put_memory_note

    def list_summaries(
        self,
        user_id: str,
        *,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self._list_json_rows(
            table="session_summaries", converter=self._summary_dict,
            user_id=user_id, session_id=session_id, limit=limit, order_column="updated_at"
        )

    def get_summary(self, user_id: str, summary_id: str) -> dict[str, Any] | None:
        """Read one session summary inside a user's scope."""

        with self._lock:
            self._ensure_open()
            self._require_user(user_id)
            row = self._conn.execute(
                "SELECT * FROM session_summaries WHERE user_id = ? AND summary_id = ?",
                (user_id, str(summary_id)),
            ).fetchone()
            return self._summary_dict(row) if row else None

    def list_memory_notes(
        self,
        user_id: str,
        *,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self._list_json_rows(
            table="memory_notes", converter=self._note_dict,
            user_id=user_id, session_id=session_id, limit=limit, order_column="updated_at"
        )

    def get_memory_note(self, user_id: str, note_id: str) -> dict[str, Any] | None:
        """Read one private memory note inside a user's scope."""

        with self._lock:
            self._ensure_open()
            self._require_user(user_id)
            row = self._conn.execute(
                "SELECT * FROM memory_notes WHERE user_id = ? AND note_id = ?",
                (user_id, str(note_id)),
            ).fetchone()
            return self._note_dict(row) if row else None

    def search_memory_notes(self, user_id: str, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        query = str(query).strip()
        if not query:
            return []
        limit = self._bounded_limit(limit, maximum=20)
        with self._lock:
            self._ensure_open()
            self._require_user(user_id)
            # FTS MATCH is still parameterized; scope is applied both in the
            # virtual table and the authoritative notes table.
            rows = self._conn.execute(
                "SELECT n.* FROM memory_notes_fts f JOIN memory_notes n ON n.note_id = f.note_id "
                "WHERE f.user_id = ? AND memory_notes_fts MATCH ? "
                "ORDER BY rank LIMIT ?", (user_id, query, limit)
            ).fetchall()
            return [self._note_dict(row) for row in rows]

    def put_knowledge_chunk(
        self,
        *,
        chunk_id: str | None = None,
        document_id: str,
        document_version: str,
        content: str,
        content_hash: str,
        section: str | None = None,
        page: int | None = None,
        tags: Iterable[str] = (),
        review_status: str = "pending",
    ) -> dict[str, Any]:
        content = str(content).strip()
        if not content:
            raise ValueError("content is required")
        chunk_id = chunk_id or f"chunk-{uuid.uuid4().hex}"

        def write() -> dict[str, Any]:
            row = self._conn.execute(
                "SELECT * FROM knowledge_chunks WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()
            if row:
                return self._knowledge_dict(row)
            duplicate = self._conn.execute(
                "SELECT * FROM knowledge_chunks WHERE document_id = ? AND document_version = ? AND content_hash = ?",
                (document_id, document_version, content_hash),
            ).fetchone()
            if duplicate:
                return self._knowledge_dict(duplicate)
            now = time.time()
            self._conn.execute(
                "INSERT INTO knowledge_chunks(chunk_id, document_id, document_version, section, page, tags_json, content, content_hash, review_status, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chunk_id, document_id, document_version, section, page, _json(list(tags)), content,
                 content_hash, review_status, now),
            )
            return self._knowledge_dict(self._conn.execute(
                "SELECT * FROM knowledge_chunks WHERE chunk_id = ?", (chunk_id,)
            ).fetchone())

        return self._write(write)

    def list_knowledge_chunks(
        self,
        *,
        review_status: str | None = "approved",
        document_id: str | None = None,
        document_version: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List bounded knowledge rows using a fixed, read-only query.

        Knowledge is a shared, reviewed corpus rather than private user data.
        Callers should normally keep the default ``approved`` filter.  The
        explicit status argument is useful for ingestion/admin diagnostics and
        is still constrained to a single fixed SQL statement.
        """

        limit = self._bounded_limit(limit, maximum=500)
        with self._lock:
            self._ensure_open()
            where = ["review_status = ?"]
            params: list[Any] = [review_status] if review_status is not None else []
            if review_status is None:
                where = ["1 = 1"]
            if document_id is not None:
                where.append("document_id = ?")
                params.append(str(document_id))
            if document_version is not None:
                where.append("document_version = ?")
                params.append(str(document_version))
            params.append(limit)
            rows = self._conn.execute(
                "SELECT * FROM knowledge_chunks WHERE " + " AND ".join(where) +
                " ORDER BY created_at DESC, chunk_id LIMIT ?",
                params,
            ).fetchall()
            return [self._knowledge_dict(row) for row in rows]

    def get_knowledge_chunk(
        self,
        chunk_id: str,
        *,
        approved_only: bool = True,
    ) -> dict[str, Any] | None:
        """Return one knowledge chunk by ID, optionally requiring approval."""

        with self._lock:
            self._ensure_open()
            where = "chunk_id = ?"
            params: list[Any] = [str(chunk_id)]
            if approved_only:
                where += " AND review_status = ?"
                params.append("approved")
            row = self._conn.execute(
                "SELECT * FROM knowledge_chunks WHERE " + where,
                params,
            ).fetchone()
            return self._knowledge_dict(row) if row else None

    def search_knowledge_chunks(
        self,
        query: str,
        *,
        exercise: str | None = None,
        view: str | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Search approved knowledge with deterministic lexical scoring.

        The first vertical slice intentionally avoids a second vector-store
        dependency.  The candidate read is a fixed parameterized query; text
        scoring is performed in Python so Chinese text and tag matches work
        without relying on SQLite tokenization.  An embedding worker can later
        replace the scorer while preserving this method's contract.
        """

        query = str(query or "").replace("\x00", " ").strip()
        if not query:
            return []
        top_k = self._bounded_limit(top_k, maximum=20)
        # Keep the candidate scan bounded.  This is deliberately larger than
        # the result cap to make ranking stable when recent chunks do not match.
        candidates = self.list_knowledge_chunks(review_status="approved", limit=500)
        query_folded = query.casefold()
        # Keep contiguous CJK characters as searchable units while treating
        # normal words as one term.  The whole query is also retained for
        # phrase matches.
        terms = [part for part in re.findall(r"[\w]+|[\u4e00-\u9fff]", query_folded, re.UNICODE) if part]
        if not terms:
            terms = [query_folded]
        exercise_folded = str(exercise).strip().casefold() if exercise is not None else None
        view_folded = str(view).strip().casefold() if view is not None else None
        ranked: list[tuple[float, dict[str, Any]]] = []
        for row in candidates:
            tags = tuple(str(tag).casefold() for tag in (row.get("tags") or ()))
            tag_text = " ".join(tags)
            if exercise_folded and exercise_folded not in tags and exercise_folded not in tag_text:
                continue
            if view_folded and view_folded not in tags and view_folded not in tag_text:
                continue
            haystack = " ".join(
                str(value or "").casefold()
                for value in (row.get("content"), row.get("section"), tag_text)
            )
            score = 0.0
            if query_folded in haystack:
                score += 3.0
            for term in terms:
                if term in haystack:
                    score += 1.0
            if score <= 0:
                continue
            ranked.append((score, row))
        ranked.sort(
            key=lambda pair: (
                -pair[0],
                -float(pair[1].get("created_at") or 0.0),
                str(pair[1].get("chunk_id") or ""),
            )
        )
        return [row for _, row in ranked[:top_k]]

    # Short aliases used by retrieval adapters.
    search_knowledge = search_knowledge_chunks

    def enqueue_outbox(
        self,
        user_id: str,
        *,
        job_type: str,
        idempotency_key: str,
        payload: Mapping[str, Any] | None = None,
        source_id: str | None = None,
        source_revision: int | None = None,
        available_at: float | None = None,
    ) -> dict[str, Any]:
        """Queue a derived-memory job tied to the current deletion epoch."""

        if not str(job_type).strip() or not str(idempotency_key).strip():
            raise ValueError("job_type and idempotency_key are required")
        available_at = time.time() if available_at is None else float(available_at)

        def write() -> dict[str, Any]:
            user = self._require_user(user_id)
            existing = self._conn.execute(
                "SELECT * FROM outbox WHERE user_id = ? AND idempotency_key = ?",
                (user_id, idempotency_key),
            ).fetchone()
            if existing:
                return self._outbox_dict(existing)
            now = time.time()
            job_id = f"job-{uuid.uuid4().hex}"
            self._conn.execute(
                "INSERT INTO outbox(job_id, user_id, job_type, idempotency_key, source_id, source_revision, "
                "delete_epoch, payload_json, status, attempts, available_at, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
                (job_id, user_id, str(job_type), str(idempotency_key), source_id, source_revision,
                 int(user["memory_epoch"]), _json(dict(payload or {})), available_at, now, now),
            )
            return self._outbox_dict(self._conn.execute(
                "SELECT * FROM outbox WHERE job_id = ?", (job_id,)
            ).fetchone())

        return self._write(write)

    def claim_outbox(
        self,
        user_id: str,
        *,
        limit: int = 10,
        now: float | None = None,
        lease_s: float = 60.0,
    ) -> list[dict[str, Any]]:
        """Atomically lease pending jobs from one user's scope."""

        limit = self._bounded_limit(limit, maximum=100)
        now = time.time() if now is None else float(now)
        lease_s = max(1.0, float(lease_s))

        def write() -> list[dict[str, Any]]:
            user = self._require_user(user_id)
            rows = self._conn.execute(
                "SELECT * FROM outbox WHERE user_id = ? AND delete_epoch = ? AND "
                "((status = 'pending' AND available_at <= ?) OR "
                "(status = 'processing' AND locked_at < ?)) "
                "ORDER BY available_at, created_at LIMIT ?",
                (user_id, int(user["memory_epoch"]), now, now - lease_s, limit),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                self._conn.execute(
                    "UPDATE outbox SET status = 'processing', attempts = attempts + 1, "
                    "locked_at = ?, updated_at = ? WHERE user_id = ? AND job_id = ?",
                    (now, now, user_id, row["job_id"]),
                )
                claimed_row = self._conn.execute(
                    "SELECT * FROM outbox WHERE user_id = ? AND job_id = ?",
                    (user_id, row["job_id"]),
                ).fetchone()
                claimed.append(self._outbox_dict(claimed_row))
            return claimed

        return self._write(write)

    def complete_outbox(self, user_id: str, job_id: str, *, completed_at: float | None = None) -> dict[str, Any]:
        completed_at = time.time() if completed_at is None else float(completed_at)

        def write() -> dict[str, Any]:
            row = self._owned_outbox(user_id, job_id)
            if row["status"] == "completed":
                return self._outbox_dict(row)
            self._conn.execute(
                "UPDATE outbox SET status = 'completed', completed_at = ?, locked_at = NULL, "
                "updated_at = ? WHERE user_id = ? AND job_id = ?",
                (completed_at, completed_at, user_id, job_id),
            )
            return self._outbox_dict(self._conn.execute(
                "SELECT * FROM outbox WHERE user_id = ? AND job_id = ?", (user_id, job_id)
            ).fetchone())

        return self._write(write)

    def fail_outbox(
        self,
        user_id: str,
        job_id: str,
        *,
        error: str,
        retry_at: float | None = None,
    ) -> dict[str, Any]:
        retry_at = time.time() if retry_at is None else float(retry_at)

        def write() -> dict[str, Any]:
            self._owned_outbox(user_id, job_id)
            self._conn.execute(
                "UPDATE outbox SET status = 'pending', available_at = ?, locked_at = NULL, "
                "last_error = ?, updated_at = ? WHERE user_id = ? AND job_id = ?",
                (retry_at, str(error)[:2000], time.time(), user_id, job_id),
            )
            return self._outbox_dict(self._conn.execute(
                "SELECT * FROM outbox WHERE user_id = ? AND job_id = ?", (user_id, job_id)
            ).fetchone())

        return self._write(write)

    def cancel_outbox(self, user_id: str, *, source_id: str | None = None) -> int:
        def write() -> int:
            self._require_user(user_id)
            if source_id is None:
                cursor = self._conn.execute(
                    "DELETE FROM outbox WHERE user_id = ? AND status != 'completed'", (user_id,)
                )
            else:
                cursor = self._conn.execute(
                    "DELETE FROM outbox WHERE user_id = ? AND source_id = ? AND status != 'completed'",
                    (user_id, source_id),
                )
            return int(cursor.rowcount)

        return self._write(write)

    def list_outbox(
        self,
        user_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = self._bounded_limit(limit, maximum=500)
        with self._lock:
            self._ensure_open()
            self._require_user(user_id)
            params: list[Any] = [user_id]
            where = ["user_id = ?"]
            if status is not None:
                where.append("status = ?")
                params.append(status)
            params.append(limit)
            rows = self._conn.execute(
                "SELECT * FROM outbox WHERE " + " AND ".join(where) +
                " ORDER BY created_at LIMIT ?", params
            ).fetchall()
            return [self._outbox_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Exact training retrieval and deletion
    # ------------------------------------------------------------------

    def query_training(
        self,
        user_id: str,
        *,
        exercise: str | None = None,
        session_id: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return exact per-session rep aggregates for numerical questions."""

        limit = self._bounded_limit(limit, maximum=20)
        with self._lock:
            self._ensure_open()
            self._require_user(user_id)
            params: list[Any] = [user_id]
            where = ["s.user_id = ?"]
            if exercise is not None:
                where.append("s.exercise = ?")
                params.append(exercise)
            if session_id is not None:
                where.append("s.session_id = ?")
                params.append(session_id)
            if since is not None:
                where.append("s.started_at >= ?")
                params.append(float(since))
            if until is not None:
                where.append("s.started_at < ?")
                params.append(float(until))
            params.append(limit)
            rows = self._conn.execute(
                "SELECT s.session_id, s.user_id, s.exercise, s.started_at, s.ended_at, s.status, "
                "s.rule_version, COUNT(r.rep_id) AS completed_reps, "
                "COALESCE(SUM(CASE WHEN r.valid = 1 THEN 1 ELSE 0 END), 0) AS valid_reps, "
                "AVG(r.duration_ms) AS avg_duration_ms, MIN(r.min_knee_angle_deg) AS min_knee_angle_deg, "
                "MAX(r.max_knee_angle_deg) AS max_knee_angle_deg "
                "FROM sessions s LEFT JOIN reps r ON r.session_id = s.session_id AND r.user_id = s.user_id "
                "WHERE " + " AND ".join(where) +
                " GROUP BY s.session_id ORDER BY s.started_at DESC LIMIT ?", params
            ).fetchall()
            return [self._training_dict(row) for row in rows]

    def query_reps(
        self,
        user_id: str,
        *,
        exercise: str | None = None,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return individual reps with their session exercise and evidence IDs."""

        limit = self._bounded_limit(limit, maximum=20)
        with self._lock:
            self._ensure_open()
            self._require_user(user_id)
            params: list[Any] = [user_id]
            where = ["r.user_id = ?"]
            if exercise is not None:
                where.append("s.exercise = ?")
                params.append(exercise)
            if session_id is not None:
                where.append("r.session_id = ?")
                params.append(session_id)
            params.append(limit)
            rows = self._conn.execute(
                "SELECT r.*, s.exercise FROM reps r JOIN sessions s ON s.session_id = r.session_id "
                "AND s.user_id = r.user_id WHERE " + " AND ".join(where) +
                " ORDER BY r.completed_at DESC LIMIT ?", params
            ).fetchall()
            return [self._rep_dict(row) for row in rows]

    def delete_session(self, user_id: str, session_id: str) -> dict[str, int]:
        """Delete one session and all facts/derived data linked to it."""

        def write() -> dict[str, int]:
            self._require_session(user_id, session_id)
            # source_id is intentionally not an FK because it can refer to a
            # note/embedding revision; explicitly remove those derived jobs.
            outbox_count = self._conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE user_id = ? AND source_id = ?",
                (user_id, session_id),
            ).fetchone()[0]
            self._conn.execute(
                "DELETE FROM outbox WHERE user_id = ? AND source_id = ?", (user_id, session_id)
            )
            # Bump epoch before deleting the parent so a cached task token is
            # invalid even if the same user starts another session immediately.
            self._conn.execute(
                "UPDATE users SET memory_epoch = memory_epoch + 1, updated_at = ? WHERE user_id = ?",
                (time.time(), user_id),
            )
            cursor = self._conn.execute(
                "DELETE FROM sessions WHERE user_id = ? AND session_id = ?", (user_id, session_id)
            )
            return {"sessions": int(cursor.rowcount), "outbox": int(outbox_count)}

        return self._write(write)

    def delete_user(self, user_id: str) -> dict[str, int | str]:
        """Delete all private facts, preserving only a scope epoch tombstone."""

        def write() -> dict[str, int | str]:
            row = self._conn.execute(
                "SELECT memory_epoch FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                # Preserve/advance a tombstone even for an already-deleted
                # scope; this makes repeated deletion idempotent.
                old_epoch = self._scope_epoch(user_id)
                epoch = old_epoch + 1
                self._conn.execute(
                    "INSERT INTO scope_epochs(user_id, memory_epoch, deleted_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET memory_epoch = excluded.memory_epoch, deleted_at = excluded.deleted_at",
                    (user_id, epoch, time.time()),
                )
                return {"user_id": user_id, "deleted": 0, "memory_epoch": epoch}
            epoch = int(row[0]) + 1
            self._conn.execute(
                "INSERT INTO scope_epochs(user_id, memory_epoch, deleted_at) VALUES(?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET memory_epoch = excluded.memory_epoch, deleted_at = excluded.deleted_at",
                (user_id, epoch, time.time()),
            )
            # ON DELETE CASCADE removes all private child facts, notes, vectors
            # and outbox entries.  FTS is external to FK and needs explicit
            # cleanup first.
            self._conn.execute("DELETE FROM memory_notes_fts WHERE user_id = ?", (user_id,))
            cursor = self._conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            return {"user_id": user_id, "deleted": int(cursor.rowcount), "memory_epoch": epoch}

        return self._write(write)

    # Friendly alias used by privacy/UI integrations.
    erase_user = delete_user

    def delete_memory(self, user_id: str, *, session_id: str | None = None) -> dict[str, int | str]:
        if session_id is not None:
            return self.delete_session(user_id, session_id)
        return self.delete_user(user_id)
