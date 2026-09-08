"""MCP tool discovery and dispatch contract tests."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from coach.mcp_server import (
    MCPMemoryDispatcher,
    RetrievalEnvelopeModel,
    create_mcp_server,
)
from coach.memory.retrieval import RetrievalService
from coach.memory.store import MemoryStore


class MCPContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tempdir.name) / "coach.sqlite3")
        self.store.ensure_user("u1")
        self.store.create_session("u1", session_id="s1", exercise="squat", started_at=1.0)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_fixed_tool_names_and_scope_is_absent_from_schemas(self):
        server = create_mcp_server(self.store, "u1")
        tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
        self.assertEqual(names, set(MCPMemoryDispatcher.TOOL_NAMES))
        for tool in tools:
            self.assertNotIn("user_id", tool.inputSchema.get("properties", {}))
            self.assertTrue(tool.outputSchema)

    def test_dispatch_and_structured_call_return_validated_envelope(self):
        dispatcher = MCPMemoryDispatcher(RetrievalService(self.store, "u1"))
        result = dispatcher.dispatch("memory.query_training", {"exercise": "squat"})
        envelope = RetrievalEnvelopeModel.model_validate(result)
        self.assertEqual(envelope.schema_version, "coach.retrieval.v1")
        self.assertEqual(envelope.items[0]["session_id"], "s1")

        server = create_mcp_server(self.store, "u1")
        content, structured = asyncio.run(
            server.call_tool("memory.get_profile", {"limit": 1})
        )
        self.assertTrue(content)
        self.assertEqual(structured["schema_version"], "coach.retrieval.v1")

    def test_dispatch_rejects_scope_and_unknown_arguments(self):
        dispatcher = MCPMemoryDispatcher(RetrievalService(self.store, "u1"))
        with self.assertRaises(ValueError):
            dispatcher.dispatch("memory.get_profile", {"user_id": "u2"})
        with self.assertRaises(ValueError):
            dispatcher.dispatch("memory.get_profile", {"sql": "SELECT 1"})
        with self.assertRaises(ValueError):
            dispatcher.dispatch("memory.no_such_tool", {})

        safe = dispatcher.safe_dispatch("memory.query_training", {"limit": 0})
        self.assertEqual(safe["error"]["code"], "invalid_arguments")
        self.assertEqual(safe["items"], [])

    def test_profile_proposal_is_not_persisted(self):
        dispatcher = MCPMemoryDispatcher(RetrievalService(self.store, "u1"))
        result = dispatcher.dispatch(
            "profile.propose_update",
            {"key": "goal", "value": "mobility", "source_turn_id": "turn-1"},
        )
        self.assertTrue(result["items"][0]["requires_user_confirmation"])
        self.assertEqual(self.store.get_profile("u1"), [])


if __name__ == "__main__":
    unittest.main()
