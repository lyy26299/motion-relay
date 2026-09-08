"""Bounded, user-scoped retrieval for the coaching agent.

The retrieval layer is intentionally small and boring.  It turns the fixed
queries exposed by :mod:`coach.memory.store` into a stable evidence envelope
that an Agent Loop or an MCP client can validate.  It never accepts SQL,
filesystem paths, or a caller-selected user scope.

The first implementation uses exact SQLite aggregates for numerical training
facts and deterministic lexical ranking for notes and reviewed knowledge.  A
future embedding worker may improve ranking without changing the public
envelope or the ownership rules here.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from .store import MemoryStore, NotFoundError, ScopeError


RETRIEVAL_SCHEMA_VERSION = "coach.retrieval.v1"
MAX_TRAINING_LIMIT = 20
MAX_PROFILE_LIMIT = 50
MAX_EPISODE_TOP_K = 5
MAX_KNOWLEDGE_TOP_K = 5
MAX_EVIDENCE_IDS = 20
MAX_CANDIDATES = 100
MAX_QUERY_CHARS = 512

_TRAINING_METRICS = frozenset(
    {
        "all",
        "completed_reps",
        "valid_reps",
        "avg_duration_ms",
        "min_knee_angle_deg",
        "max_knee_angle_deg",
    }
)

# This is deliberately an allow-list.  A proposal is never written directly
# to profile_facts; the application must show it to the user and explicitly
# confirm it first.
PROFILE_PROPOSAL_KEYS = frozenset(
    {
        "goal",
        "goals",
        "preference",
        "preferences",
        "communication_style",
        "limitation",
        "limitations",
        "training_experience",
        "camera_view",
        "target_reps",
        "exercise",
    }
)


class RetrievalError(RuntimeError):
    """Base class for retrieval failures."""


class ScopeChangedError(RetrievalError):
    """Raised when a service outlives the memory epoch it was bound to."""


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """A stable pointer to a fact returned by a retrieval operation."""

    source_type: str
    source_id: str
    # ``revision`` is intentionally a JSON scalar compatible with the Agent
    # Loop contract.  Ledger rows without an explicit revision use ``1``;
    # reviewed knowledge can use a document-version string.
    revision: int | str = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class RetrievalEnvelope:
    """Serializable result envelope shared by direct and MCP callers."""

    request_id: str
    items: tuple[dict[str, Any], ...]
    evidence_refs: tuple[dict[str, Any], ...]
    as_of: float
    scope_epoch: int
    truncated: bool = False
    error: dict[str, Any] | None = None
    schema_version: str = RETRIEVAL_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "scope_epoch": self.scope_epoch,
            "items": [dict(item) for item in self.items],
            "evidence_refs": [dict(ref) for ref in self.evidence_refs],
            "as_of": self.as_of,
            "truncated": self.truncated,
            "error": self.error,
        }


def _clean_text(value: Any, *, name: str, maximum: int = MAX_QUERY_CHARS) -> str:
    text = str(value or "").replace("\x00", " ").strip()
    if len(text) > maximum:
        raise ValueError(f"{name} is too long")
    return text


def _bounded_limit(value: Any, *, maximum: int, name: str) -> tuple[int, bool]:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        numeric = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if isinstance(value, float) and value != numeric:
        raise ValueError(f"{name} must be an integer")
    if numeric < 1:
        raise ValueError(f"{name} must be positive")
    return min(numeric, maximum), numeric > maximum


def _finite_time(value: Any, *, name: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a timestamp")
    if isinstance(value, str):
        text = value.strip()
        try:
            # Accept ISO-8601 for MCP clients while keeping storage numeric.
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            result = parsed.timestamp()
        except ValueError:
            try:
                result = float(text)
            except ValueError as exc:
                raise ValueError(f"{name} must be a timestamp") from exc
    else:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a timestamp") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _fts_query(query: str) -> str:
    """Turn user text into a safe FTS expression.

    Every term is quoted, so punctuation cannot turn into an FTS operator.
    The original phrase is retained where possible by using OR semantics; the
    Python scorer below remains authoritative for ranking.
    """

    terms = re.findall(r"[\w]+|[\u4e00-\u9fff]", query, flags=re.UNICODE)
    if not terms:
        return '"' + query.replace('"', '""') + '"'
    unique: list[str] = []
    for term in terms:
        term = term.replace('"', '""')
        if term and term not in unique:
            unique.append(term)
    return " OR ".join(f'"{term}"' for term in unique)


def _lexical_score(query: str, text: str) -> float:
    query_folded = query.casefold()
    text_folded = text.casefold()
    if not query_folded or not text_folded:
        return 0.0
    score = 3.0 if query_folded in text_folded else 0.0
    terms = set(re.findall(r"[\w]+|[\u4e00-\u9fff]", query_folded, flags=re.UNICODE))
    for term in terms:
        if term in text_folded:
            score += 1.0
    return score


def _dedupe_refs(refs: Iterable[EvidenceRef]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, int | str]] = set()
    result: list[dict[str, Any]] = []
    for ref in refs:
        key = (ref.source_type, ref.source_id, ref.revision)
        if not ref.source_id or key in seen:
            continue
        seen.add(key)
        result.append(ref.to_dict())
    return result


class RetrievalService:
    """Read-only retrieval bound to one trusted user scope.

    ``user_id`` is supplied by the application, not by model/tool arguments.
    The service captures the current deletion epoch and refuses to serve data
    after that scope has been deleted or recreated.
    """

    def __init__(
        self,
        store: MemoryStore,
        user_id: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.user_id = str(user_id).strip()
        if not self.user_id:
            raise ValueError("user_id is required")
        if self.store.get_user(self.user_id) is None:
            raise ScopeError(f"unknown user scope: {self.user_id}")
        self.scope_epoch = self.store.get_memory_epoch(self.user_id)
        self._clock = clock

    def _check_scope(self) -> None:
        current_user = self.store.get_user(self.user_id)
        current_epoch = self.store.get_memory_epoch(self.user_id)
        if current_user is None or current_epoch != self.scope_epoch:
            raise ScopeChangedError("memory scope changed; create a new retrieval service")

    def _envelope(
        self,
        items: Iterable[Mapping[str, Any]],
        refs: Iterable[EvidenceRef],
        *,
        request_id: str | None = None,
        truncated: bool = False,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return RetrievalEnvelope(
            request_id=request_id or f"retrieval-{uuid.uuid4().hex}",
            items=tuple(dict(item) for item in items),
            evidence_refs=tuple(_dedupe_refs(refs)),
            as_of=float(self._clock()),
            scope_epoch=self.scope_epoch,
            truncated=bool(truncated),
            error=error,
        ).to_dict()

    def get_profile(
        self,
        *,
        fact_key: str | None = None,
        limit: int = MAX_PROFILE_LIMIT,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Return confirmed, non-expired profile facts only."""

        self._check_scope()
        effective, clipped = _bounded_limit(limit, maximum=MAX_PROFILE_LIMIT, name="limit")
        key = _clean_text(fact_key, name="fact_key") if fact_key is not None else None
        rows = self.store.get_profile(self.user_id)
        if key is not None:
            rows = [row for row in rows if row.get("fact_key") == key]
        rows = rows[:effective]
        items: list[dict[str, Any]] = []
        refs: list[EvidenceRef] = []
        for row in rows:
            # Keep the model-facing shape to an intentional field allow-list.
            item = {
                "fact_id": row.get("fact_id"),
                "fact_key": row.get("fact_key"),
                "value": row.get("value"),
                "source": row.get("source"),
                "confirmed_at": row.get("confirmed_at"),
                "valid_until": row.get("valid_until"),
                "active": bool(row.get("active")),
            }
            items.append(item)
            refs.append(EvidenceRef("profile_fact", str(row["fact_id"]), 1))
        return self._envelope(items, refs, request_id=request_id, truncated=clipped)

    def query_training(
        self,
        *,
        exercise: str | None = None,
        session_id: str | None = None,
        since: float | str | None = None,
        until: float | str | None = None,
        metric: str = "all",
        limit: int = MAX_TRAINING_LIMIT,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Query exact per-session training aggregates from the fact ledger."""

        self._check_scope()
        effective, clipped = _bounded_limit(limit, maximum=MAX_TRAINING_LIMIT, name="limit")
        metric = str(metric or "all").strip()
        if metric not in _TRAINING_METRICS:
            raise ValueError(f"unsupported metric: {metric}")
        exercise = _clean_text(exercise, name="exercise") if exercise is not None else None
        session_id = _clean_text(session_id, name="session_id") if session_id is not None else None
        since_value = _finite_time(since, name="since")
        until_value = _finite_time(until, name="until")
        if since_value is not None and until_value is not None and since_value >= until_value:
            raise ValueError("since must be earlier than until")
        rows = self.store.query_training(
            self.user_id,
            exercise=exercise,
            session_id=session_id,
            since=since_value,
            until=until_value,
            limit=effective,
        )
        items: list[dict[str, Any]] = []
        refs: list[EvidenceRef] = []
        for row in rows:
            item = dict(row)
            session_ref = EvidenceRef("session", str(row["session_id"]), 1)
            # A metric projection is additive: callers still receive all exact
            # columns and can cite the session/rep evidence underneath it.
            item["metric"] = metric
            if metric != "all":
                item["metric_value"] = row.get(metric)
            rep_refs: list[EvidenceRef] = []
            try:
                rep_rows = self.store.query_reps(
                    self.user_id, session_id=str(row["session_id"]), limit=MAX_TRAINING_LIMIT
                )
                rep_refs = [EvidenceRef("rep", str(rep["rep_id"]), 1) for rep in rep_rows]
            except (ScopeError, NotFoundError):
                # The aggregate remains valid if a concurrent deletion removes
                # child rows; the session reference is still authoritative.
                rep_refs = []
            item["evidence_refs"] = [session_ref.to_dict(), *[ref.to_dict() for ref in rep_refs]]
            items.append(item)
            refs.extend([session_ref, *rep_refs])
        return self._envelope(items, refs, request_id=request_id, truncated=clipped)

    def search_episodes(
        self,
        query: str,
        *,
        exercise: str | None = None,
        since: float | str | None = None,
        until: float | str | None = None,
        top_k: int = MAX_EPISODE_TOP_K,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Search private notes and session summaries for one user."""

        self._check_scope()
        query = _clean_text(query, name="query")
        effective, clipped = _bounded_limit(top_k, maximum=MAX_EPISODE_TOP_K, name="top_k")
        exercise = _clean_text(exercise, name="exercise") if exercise is not None else None
        since_value = _finite_time(since, name="since")
        until_value = _finite_time(until, name="until")
        if since_value is not None and until_value is not None and since_value >= until_value:
            raise ValueError("since must be earlier than until")
        if not query:
            return self._envelope([], [], request_id=request_id, truncated=clipped)

        candidates: list[tuple[str, dict[str, Any]]] = []
        try:
            notes = self.store.search_memory_notes(
                self.user_id, _fts_query(query), limit=MAX_CANDIDATES
            )
        except (sqlite3.Error, ValueError):
            notes = []
        # FTS tokenization differs across SQLite builds (especially for CJK),
        # so a bounded recent-note fallback keeps the behavior deterministic.
        if not notes:
            notes = self.store.list_memory_notes(self.user_id, limit=MAX_CANDIDATES)
        candidates.extend(("memory_note", dict(row)) for row in notes)
        summaries = self.store.list_summaries(self.user_id, limit=MAX_CANDIDATES)
        candidates.extend(("session_summary", dict(row)) for row in summaries)

        ranked: list[tuple[float, float, str, dict[str, Any]]] = []
        for source_type, row in candidates:
            session_id = row.get("session_id")
            session = None
            if session_id:
                session = self.store.get_session(self.user_id, str(session_id))
            if exercise is not None:
                if session is None or str(session.get("exercise")) != exercise:
                    continue
            event_time = float(
                (session or {}).get("started_at")
                or row.get("updated_at")
                or row.get("created_at")
                or 0.0
            )
            if since_value is not None and event_time < since_value:
                continue
            if until_value is not None and event_time >= until_value:
                continue
            text = str(row.get("summary") or "")
            score = _lexical_score(query, text)
            if score <= 0:
                continue
            source_id_key = "note_id" if source_type == "memory_note" else "summary_id"
            source_id = str(row.get(source_id_key) or "")
            if not source_id:
                continue
            item = dict(row)
            item.update(
                {
                    "source_type": source_type,
                    "source_id": source_id,
                    "text": text,
                    "score": score,
                }
            )
            ranked.append((score, event_time, source_id, item))
        ranked.sort(key=lambda part: (-part[0], -part[1], part[2]))
        selected = [part[3] for part in ranked[:effective]]
        refs = [
            EvidenceRef(str(item["source_type"]), str(item["source_id"]), item.get("revision"))
            for item in selected
        ]
        return self._envelope(selected, refs, request_id=request_id, truncated=clipped)

    def search_knowledge(
        self,
        query: str,
        *,
        exercise: str | None = None,
        view: str | None = None,
        top_k: int = MAX_KNOWLEDGE_TOP_K,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Search only reviewed/approved knowledge chunks."""

        self._check_scope()
        query = _clean_text(query, name="query")
        effective, clipped = _bounded_limit(top_k, maximum=MAX_KNOWLEDGE_TOP_K, name="top_k")
        exercise = _clean_text(exercise, name="exercise") if exercise is not None else None
        view = _clean_text(view, name="view") if view is not None else None
        if not query:
            return self._envelope([], [], request_id=request_id, truncated=clipped)
        rows = self.store.search_knowledge_chunks(
            query, exercise=exercise, view=view, top_k=effective
        )
        items: list[dict[str, Any]] = []
        refs: list[EvidenceRef] = []
        for row in rows:
            item = dict(row)
            item.update(
                {
                    "source_type": "knowledge_chunk",
                    "source_id": str(row["chunk_id"]),
                    "revision": row.get("document_version"),
                    "trust": "approved",
                }
            )
            items.append(item)
            # Document versions are strings in the source corpus.  The common
            # evidence contract allows a revision value of any JSON scalar;
            # preserve it rather than coercing versions such as "2026-09".
            refs.append(
                EvidenceRef(
                    "knowledge_chunk",
                    str(row["chunk_id"]),
                    str(row.get("document_version") or "1"),
                )
            )
        return self._envelope(items, refs, request_id=request_id, truncated=clipped)

    def get_evidence(
        self,
        evidence_ids: Iterable[str],
        *,
        limit: int = MAX_EVIDENCE_IDS,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a bounded list of evidence IDs in the current scope.

        Unknown IDs are returned as explicit ``unavailable`` items.  This is
        important for deletion and retention semantics: an empty result must
        not be mistaken for proof that the evidence never existed.
        """

        self._check_scope()
        effective, clipped = _bounded_limit(limit, maximum=MAX_EVIDENCE_IDS, name="limit")
        if isinstance(evidence_ids, (str, bytes)):
            raise ValueError("evidence_ids must be a list of IDs")
        raw_ids = list(evidence_ids)
        if len(raw_ids) > effective:
            clipped = True
        ids: list[str] = []
        for raw_id in raw_ids[:effective]:
            value = _clean_text(raw_id, name="evidence_id", maximum=256)
            if value and value not in ids:
                ids.append(value)
        profile_rows = self.store.list_profile_facts(self.user_id, include_inactive=True)
        profile_by_id = {str(row["fact_id"]): row for row in profile_rows}
        items: list[dict[str, Any]] = []
        refs: list[EvidenceRef] = []
        for evidence_id in ids:
            row: dict[str, Any] | None = None
            source_type: str | None = None
            # Prefixes make the common path cheap, but IDs are application
            # supplied and may use another naming convention.  Every fallback
            # remains user-scoped; no global lookup is ever performed.
            if evidence_id.startswith("rep-"):
                row = self.store.get_rep(self.user_id, evidence_id)
                source_type = "rep"
            elif evidence_id.startswith(("evt-", "event-")):
                row = self.store.get_event(self.user_id, evidence_id)
                source_type = "event"
            elif evidence_id.startswith("note-"):
                row = self.store.get_memory_note(self.user_id, evidence_id)
                source_type = "memory_note"
            elif evidence_id.startswith("summary-"):
                row = self.store.get_summary(self.user_id, evidence_id)
                source_type = "session_summary"
            elif evidence_id.startswith("fact-"):
                row = profile_by_id.get(evidence_id)
                source_type = "profile_fact"
            elif evidence_id.startswith("chunk-"):
                row = self.store.get_knowledge_chunk(evidence_id, approved_only=True)
                source_type = "knowledge_chunk"
            else:
                row = self.store.get_session(self.user_id, evidence_id)
                source_type = "session"
            if row is None and source_type == "session":
                for candidate_type, candidate in (
                    ("rep", self.store.get_rep(self.user_id, evidence_id)),
                    ("event", self.store.get_event(self.user_id, evidence_id)),
                    ("memory_note", self.store.get_memory_note(self.user_id, evidence_id)),
                    ("session_summary", self.store.get_summary(self.user_id, evidence_id)),
                    ("knowledge_chunk", self.store.get_knowledge_chunk(evidence_id, approved_only=True)),
                ):
                    if candidate is not None:
                        source_type, row = candidate_type, candidate
                        break
            if row is None:
                items.append(
                    {
                        "source_type": source_type or "unknown",
                        "source_id": evidence_id,
                        "evidence_status": "unavailable",
                    }
                )
                continue
            status = "available"
            if source_type == "profile_fact" and (
                not bool(row.get("active"))
                or (row.get("valid_until") is not None and float(row["valid_until"]) <= self._clock())
            ):
                status = "expired"
            item = dict(row)
            item.update(
                {
                    "source_type": source_type,
                    "source_id": evidence_id,
                    "evidence_status": status,
                }
            )
            items.append(item)
            revision = row.get("revision")
            if not isinstance(revision, (int, str)) or isinstance(revision, bool):
                revision = 1
            refs.append(EvidenceRef(source_type or "unknown", evidence_id, revision))
        return self._envelope(items, refs, request_id=request_id, truncated=clipped)

    def propose_profile_update(
        self,
        key: str,
        value: Any,
        source_turn_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a non-persistent profile proposal requiring user approval."""

        self._check_scope()
        key = _clean_text(key, name="key", maximum=64).casefold()
        if key not in PROFILE_PROPOSAL_KEYS:
            raise ValueError(f"unsupported profile key: {key}")
        source_turn_id = _clean_text(source_turn_id, name="source_turn_id", maximum=128)
        if not source_turn_id:
            raise ValueError("source_turn_id is required")
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("value must be JSON-serializable") from exc
        if len(encoded) > 4096:
            raise ValueError("value is too large")
        proposal = {
            "proposal_id": f"proposal-{uuid.uuid4().hex}",
            "fact_key": key,
            "value": value,
            "source_turn_id": source_turn_id,
            "status": "proposed",
            "requires_user_confirmation": True,
            "scope_epoch": self.scope_epoch,
        }
        return self._envelope([proposal], [], request_id=request_id)

    # Friendly aliases used by callers and tests.
    retrieve_profile = get_profile
    retrieve_training = query_training
    retrieve_episodes = search_episodes
    retrieve_knowledge = search_knowledge


MemoryRetriever = RetrievalService
MemoryRetrieval = RetrievalService
MemoryService = RetrievalService


def error_envelope(
    error: Exception,
    *,
    request_id: str | None = None,
    scope_epoch: int = 0,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Convert expected tool failures to a non-sensitive result envelope."""

    if isinstance(error, (ScopeError, ScopeChangedError)):
        code = "scope_error"
    elif isinstance(error, ValueError):
        code = "invalid_arguments"
    elif isinstance(error, NotFoundError):
        code = "not_found"
    else:
        code = "retrieval_error"
    return RetrievalEnvelope(
        request_id=request_id or f"retrieval-{uuid.uuid4().hex}",
        items=(),
        evidence_refs=(),
        as_of=float(clock()),
        scope_epoch=int(scope_epoch),
        error={"code": code, "message": str(error)[:256]},
    ).to_dict()


__all__ = [
    "EvidenceRef",
    "MAX_EPISODE_TOP_K",
    "MAX_EVIDENCE_IDS",
    "MAX_KNOWLEDGE_TOP_K",
    "MAX_PROFILE_LIMIT",
    "MAX_TRAINING_LIMIT",
    "MemoryRetriever",
    "MemoryRetrieval",
    "MemoryService",
    "PROFILE_PROPOSAL_KEYS",
    "RETRIEVAL_SCHEMA_VERSION",
    "RetrievalEnvelope",
    "RetrievalError",
    "RetrievalService",
    "ScopeChangedError",
    "error_envelope",
]
