"""Durable, user-scoped coaching memory primitives."""

from .retrieval import (
    EvidenceRef,
    MemoryRetrieval,
    MemoryRetriever,
    MemoryService,
    RetrievalEnvelope,
    RetrievalService,
    ScopeChangedError,
)
from .store import MemoryStore
from .consolidate import build_session_summary, consolidate_session

__all__ = [
    "EvidenceRef",
    "MemoryRetriever",
    "MemoryRetrieval",
    "MemoryService",
    "MemoryStore",
    "RetrievalEnvelope",
    "RetrievalService",
    "ScopeChangedError",
    "build_session_summary",
    "consolidate_session",
]
