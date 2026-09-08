"""Controlled MCP tools for the local coaching memory service.

The server exposes a deliberately tiny allow-list of read/proposal tools.  A
trusted application binds ``user_id`` when constructing the server; it is not
part of any model-visible tool schema.  Sensor facts are written through the
application's ledger path, never through MCP and never through arbitrary SQL.

The module targets the installed MCP Python SDK v1.x (currently 1.29.1).  It
uses ``FastMCP`` for tool discovery and stdio serving, while keeping a plain
``MCPMemoryDispatcher`` available for deterministic unit tests and a local
Agent Loop that does not need to spawn a subprocess.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .memory.retrieval import (
    MAX_EPISODE_TOP_K,
    MAX_EVIDENCE_IDS,
    MAX_KNOWLEDGE_TOP_K,
    MAX_PROFILE_LIMIT,
    MAX_TRAINING_LIMIT,
    RetrievalService,
    error_envelope,
)
from .memory.store import MemoryStore

try:  # Keep importing the memory ledger possible in minimal/offline tooling.
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - exercised only without optional SDK
    FastMCP = None  # type: ignore[assignment,misc]
    _MCP_IMPORT_ERROR = exc
else:
    _MCP_IMPORT_ERROR = None


MCP_SCHEMA_VERSION = "coach.mcp.memory.v1"
TRAINING_METRIC = Literal[
    "all",
    "completed_reps",
    "valid_reps",
    "avg_duration_ms",
    "min_knee_angle_deg",
    "max_knee_angle_deg",
]


class RetrievalEnvelopeModel(BaseModel):
    """JSON-schema-backed MCP result envelope."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default="coach.retrieval.v1")
    request_id: str
    scope_epoch: int
    items: list[dict[str, Any]]
    evidence_refs: list[dict[str, Any]]
    as_of: float
    truncated: bool = False
    error: dict[str, Any] | None = None


class MCPMemoryDispatcher:
    """Dispatch fixed MCP names to a scope-bound :class:`RetrievalService`."""

    TOOL_NAMES = (
        "memory.get_profile",
        "memory.query_training",
        "memory.search_episodes",
        "memory.get_evidence",
        "knowledge.search",
        "profile.propose_update",
    )

    _ARGUMENTS = {
        "memory.get_profile": frozenset({"fact_key", "limit"}),
        "memory.query_training": frozenset(
            {"exercise", "session_id", "since", "until", "metric", "limit"}
        ),
        "memory.search_episodes": frozenset(
            {"query", "exercise", "since", "until", "top_k"}
        ),
        "memory.get_evidence": frozenset({"evidence_ids", "limit"}),
        "knowledge.search": frozenset({"query", "exercise", "view", "top_k"}),
        "profile.propose_update": frozenset({"key", "value", "source_turn_id"}),
    }

    def __init__(self, retrieval: RetrievalService) -> None:
        self.retrieval = retrieval

    @property
    def user_id(self) -> str:
        """The trusted scope, useful to application diagnostics only."""

        return self.retrieval.user_id

    def dispatch(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Execute one allow-listed operation.

        Unknown names and unknown arguments fail closed.  The method raises a
        regular ``ValueError`` so local callers can distinguish programming or
        validation errors; MCP wrappers use :meth:`safe_dispatch` to return a
        typed error envelope instead of leaking an exception over the wire.
        """

        if name not in self.TOOL_NAMES:
            raise ValueError(f"unknown memory tool: {name}")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise ValueError("tool arguments must be an object")
        args = dict(arguments)
        unknown = set(args).difference(self._ARGUMENTS[name])
        if unknown:
            raise ValueError(f"unsupported arguments for {name}: {', '.join(sorted(unknown))}")
        # This explicit check documents and enforces the critical boundary even
        # if a future tool argument allow-list is accidentally widened.
        if "user_id" in args:
            raise ValueError("user_id is bound by the application")

        if name == "memory.get_profile":
            return self.retrieval.get_profile(**args)
        if name == "memory.query_training":
            return self.retrieval.query_training(**args)
        if name == "memory.search_episodes":
            return self.retrieval.search_episodes(**args)
        if name == "memory.get_evidence":
            return self.retrieval.get_evidence(**args)
        if name == "knowledge.search":
            return self.retrieval.search_knowledge(**args)
        # The names above are exhaustive; keeping this final branch explicit
        # makes a new tool require an intentional code review.
        if name == "profile.propose_update":
            return self.retrieval.propose_profile_update(**args)
        raise ValueError(f"unreachable memory tool: {name}")

    def safe_dispatch(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Dispatch and convert expected failures to a bounded envelope."""

        try:
            return self.dispatch(name, arguments)
        except Exception as exc:  # wrappers must never expose stack traces
            return error_envelope(
                exc,
                scope_epoch=self.retrieval.scope_epoch,
            )


def _require_fastmcp() -> type:
    if FastMCP is None:  # pragma: no cover - depends on installation
        raise RuntimeError(
            "MCP SDK is not installed; install mcp==1.29.1 to run coach.mcp_server"
        ) from _MCP_IMPORT_ERROR
    return FastMCP


def _model_result(dispatcher: MCPMemoryDispatcher, name: str, args: Mapping[str, Any]) -> RetrievalEnvelopeModel:
    return RetrievalEnvelopeModel.model_validate(dispatcher.safe_dispatch(name, args))


def _register_tools(server: Any, dispatcher: MCPMemoryDispatcher) -> None:
    """Register the fixed tool surface on a FastMCP instance."""

    @server.tool(
        name="memory.get_profile",
        title="Get current profile",
        description="Read confirmed, non-expired profile facts for the bound user scope.",
        structured_output=True,
    )
    def get_profile(
        fact_key: str | None = None,
        limit: int = MAX_PROFILE_LIMIT,
    ) -> RetrievalEnvelopeModel:
        return _model_result(
            dispatcher,
            "memory.get_profile",
            {"fact_key": fact_key, "limit": limit},
        )

    @server.tool(
        name="memory.query_training",
        title="Query exact training facts",
        description="Read exact per-session training aggregates; numeric facts come from the local ledger.",
        structured_output=True,
    )
    def query_training(
        exercise: str | None = None,
        session_id: str | None = None,
        since: float | str | None = None,
        until: float | str | None = None,
        metric: TRAINING_METRIC = "all",
        limit: int = MAX_TRAINING_LIMIT,
    ) -> RetrievalEnvelopeModel:
        return _model_result(
            dispatcher,
            "memory.query_training",
            {
                "exercise": exercise,
                "session_id": session_id,
                "since": since,
                "until": until,
                "metric": metric,
                "limit": limit,
            },
        )

    @server.tool(
        name="memory.search_episodes",
        title="Search training episodes",
        description="Search private notes and session summaries within the bound user scope.",
        structured_output=True,
    )
    def search_episodes(
        query: str,
        exercise: str | None = None,
        since: float | str | None = None,
        until: float | str | None = None,
        top_k: int = MAX_EPISODE_TOP_K,
    ) -> RetrievalEnvelopeModel:
        return _model_result(
            dispatcher,
            "memory.search_episodes",
            {
                "query": query,
                "exercise": exercise,
                "since": since,
                "until": until,
                "top_k": top_k,
            },
        )

    @server.tool(
        name="memory.get_evidence",
        title="Resolve evidence",
        description="Resolve bounded evidence IDs and report unavailable or expired records explicitly.",
        structured_output=True,
    )
    def get_evidence(
        evidence_ids: list[str],
        limit: int = MAX_EVIDENCE_IDS,
    ) -> RetrievalEnvelopeModel:
        return _model_result(
            dispatcher,
            "memory.get_evidence",
            {"evidence_ids": evidence_ids, "limit": limit},
        )

    @server.tool(
        name="knowledge.search",
        title="Search reviewed coaching knowledge",
        description="Search only approved coaching knowledge chunks with source metadata.",
        structured_output=True,
    )
    def search_knowledge(
        query: str,
        exercise: str | None = None,
        view: str | None = None,
        top_k: int = MAX_KNOWLEDGE_TOP_K,
    ) -> RetrievalEnvelopeModel:
        return _model_result(
            dispatcher,
            "knowledge.search",
            {"query": query, "exercise": exercise, "view": view, "top_k": top_k},
        )

    @server.tool(
        name="profile.propose_update",
        title="Propose profile update",
        description="Create a user-confirmation proposal; this tool never writes profile facts.",
        structured_output=True,
    )
    def propose_update(
        key: str,
        value: Any,
        source_turn_id: str,
    ) -> RetrievalEnvelopeModel:
        return _model_result(
            dispatcher,
            "profile.propose_update",
            {"key": key, "value": value, "source_turn_id": source_turn_id},
        )


def create_mcp_server(
    store: MemoryStore,
    user_id: str,
    *,
    name: str = "coach-memory",
    instructions: str | None = None,
) -> Any:
    """Build a scope-bound FastMCP server.

    The returned object is the SDK ``FastMCP`` instance, so callers can use
    ``list_tools()``, ``call_tool()`` and ``run('stdio')`` directly.  For local
    orchestration, ``server.retrieval_service`` and ``server.dispatcher`` are
    intentionally attached as diagnostic/application handles; neither is
    visible to MCP clients.
    """

    mcp_type = _require_fastmcp()
    retrieval = RetrievalService(store, user_id)
    dispatcher = MCPMemoryDispatcher(retrieval)
    server = mcp_type(
        name=name,
        instructions=instructions
        or "Read-only coaching memory tools. User scope is bound by the host application.",
    )
    _register_tools(server, dispatcher)
    # FastMCP is a normal Python object; these attributes do not alter the MCP
    # protocol and make in-process tests/AgentLoop integration straightforward.
    server.retrieval_service = retrieval
    server.dispatcher = dispatcher
    server.tool_names = MCPMemoryDispatcher.TOOL_NAMES
    return server


build_mcp_server = create_mcp_server


class MemoryMCPServer:
    """Small object-oriented facade around :func:`create_mcp_server`.

    This facade is convenient for dependency injection while retaining the
    underlying FastMCP app at ``.app``.  Unknown attributes delegate to it, so
    it can be used anywhere a FastMCP instance is expected.
    """

    def __init__(
        self,
        store: MemoryStore,
        user_id: str,
        *,
        name: str = "coach-memory",
        instructions: str | None = None,
    ) -> None:
        self.app = create_mcp_server(store, user_id, name=name, instructions=instructions)

    @property
    def dispatcher(self) -> MCPMemoryDispatcher:
        return self.app.dispatcher

    @property
    def retrieval_service(self) -> RetrievalService:
        return self.app.retrieval_service

    def __getattr__(self, name: str) -> Any:
        return getattr(self.app, name)


def main(argv: list[str] | None = None) -> None:
    """Run a local stdio MCP server for diagnostics or an Agent Loop process."""

    parser = argparse.ArgumentParser(description="Vision Coach memory MCP server")
    parser.add_argument(
        "--db",
        default=os.environ.get("COACH_MEMORY_DB", "coach_memory.sqlite3"),
        help="SQLite path (default: COACH_MEMORY_DB or coach_memory.sqlite3)",
    )
    parser.add_argument(
        "--user-id",
        default=os.environ.get("COACH_USER_ID"),
        help="Trusted user scope (default: COACH_USER_ID)",
    )
    args = parser.parse_args(argv)
    if not args.user_id:
        parser.error("--user-id or COACH_USER_ID is required")
    with MemoryStore(args.db) as store:
        # CLI startup is an explicit local operator action; the in-process
        # factory remains strict and requires an already-established scope.
        if store.get_user(args.user_id) is None:
            store.ensure_user(args.user_id)
        server = create_mcp_server(store, args.user_id)
        server.run("stdio")


if __name__ == "__main__":  # pragma: no cover - subprocess entry point
    main()


__all__ = [
    "MCPMemoryDispatcher",
    "MCP_SCHEMA_VERSION",
    "MemoryMCPServer",
    "RetrievalEnvelopeModel",
    "build_mcp_server",
    "create_mcp_server",
    "main",
]
