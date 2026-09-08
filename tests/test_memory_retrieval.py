"""Bounded retrieval and evidence-scope contract tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coach.memory.retrieval import RetrievalService, ScopeChangedError
from coach.memory.store import MemoryStore, ScopeError


class RetrievalServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tempdir.name) / "coach.sqlite3")
        self.store.ensure_user("u1")
        self.store.ensure_user("u2")
        self.store.create_session(
            "u1", session_id="s1", exercise="squat", started_at=100.0, rule_version="squat-v1"
        )
        self.store.create_session(
            "u2", session_id="s2", exercise="squat", started_at=100.0, rule_version="squat-v1"
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def add_rep(self, rep_id: str, index: int, *, valid: bool = True) -> None:
        self.store.record_rep(
            "u1",
            "s1",
            rep_id=rep_id,
            rep_index=index,
            valid=valid,
            started_at=100.0 + index,
            completed_at=101.0 + index,
            duration_ms=1000.0,
            min_knee_angle_deg=95.0,
            max_knee_angle_deg=175.0,
            usable_sample_ratio=1.0,
            reason_codes=(),
            rule_version="squat-v1",
        )

    def test_exact_training_query_has_numeric_facts_and_bounded_limit(self):
        self.add_rep("rep-u1-1", 1, valid=True)
        self.add_rep("rep-u1-2", 2, valid=False)
        service = RetrievalService(self.store, "u1")

        result = service.query_training(exercise="squat", metric="valid_reps")
        self.assertEqual(result["items"][0]["completed_reps"], 2)
        self.assertEqual(result["items"][0]["valid_reps"], 1)
        self.assertEqual(result["items"][0]["metric_value"], 1)
        self.assertEqual(result["evidence_refs"][0]["source_type"], "session")
        self.assertTrue(any(ref["source_id"] == "rep-u1-1" for ref in result["evidence_refs"]))

        clipped = service.query_training(limit=99)
        self.assertTrue(clipped["truncated"])
        with self.assertRaises(ValueError):
            service.query_training(limit=0)
        with self.assertRaises(ValueError):
            service.query_training(since=10, until=10)

    def test_profile_episode_knowledge_are_scoped_and_filtered(self):
        self.store.put_profile_fact("u1", "goal", "strength", fact_id="fact-goal")
        self.store.put_profile_fact(
            "u1", "old_goal", "old", fact_id="fact-expired", valid_until=50.0
        )
        self.store.put_memory_note(
            "u1", note="膝盖方向需要保持稳定", note_id="note-knee", session_id="s1"
        )
        self.store.put_memory_note("u2", note="膝盖方向 secret", note_id="note-other", session_id="s2")
        self.store.put_summary(
            "u1", summary="本组深蹲节奏改善", summary_id="summary-s1", session_id="s1"
        )
        self.store.put_knowledge_chunk(
            chunk_id="chunk-approved",
            document_id="coach-guide",
            document_version="v1",
            content="深蹲时保持膝盖方向与脚尖一致",
            content_hash="approved-hash",
            tags=("squat", "side"),
            review_status="approved",
        )
        self.store.put_knowledge_chunk(
            chunk_id="chunk-pending",
            document_id="coach-guide",
            document_version="v1",
            content="膝盖方向 pending，不应提供",
            content_hash="pending-hash",
            tags=("squat",),
            review_status="pending",
        )
        service = RetrievalService(self.store, "u1")

        profile = service.get_profile()
        self.assertEqual([item["fact_id"] for item in profile["items"]], ["fact-goal"])
        episodes = service.search_episodes("膝盖", top_k=99)
        self.assertTrue(episodes["truncated"])
        self.assertEqual({item["source_id"] for item in episodes["items"]}, {"note-knee"})
        self.assertNotIn("note-other", {item["source_id"] for item in episodes["items"]})
        knowledge = service.search_knowledge("膝盖", exercise="squat", view="side")
        self.assertEqual([item["source_id"] for item in knowledge["items"]], ["chunk-approved"])
        self.assertEqual(knowledge["items"][0]["trust"], "approved")

    def test_evidence_resolution_reports_unavailable_and_scope_epoch_invalidates(self):
        self.add_rep("rep-u1-1", 1)
        service = RetrievalService(self.store, "u1")
        evidence = service.get_evidence(["rep-u1-1", "rep-u2-1", "missing-id"])
        statuses = {item["source_id"]: item["evidence_status"] for item in evidence["items"]}
        self.assertEqual(statuses["rep-u1-1"], "available")
        self.assertEqual(statuses["rep-u2-1"], "unavailable")
        self.assertEqual(statuses["missing-id"], "unavailable")

        self.store.delete_user("u1")
        with self.assertRaises(ScopeChangedError):
            service.get_profile()

    def test_unknown_user_scope_is_rejected_without_data_access(self):
        with self.assertRaises(ScopeError):
            RetrievalService(self.store, "not-a-user")


if __name__ == "__main__":
    unittest.main()
