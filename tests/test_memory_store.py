"""Durability, scope, and idempotency tests for the coaching fact ledger."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coach.memory.store import MemoryStore, NotFoundError, ScopeError


class MemoryStoreTests(unittest.TestCase):
    def make_store(self, directory: str) -> MemoryStore:
        return MemoryStore(Path(directory) / "coach.sqlite3")

    def test_replay_is_idempotent_and_numeric_queries_are_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_store(directory) as store:
                store.ensure_user("u1", display_name="Test")
                store.create_session(
                    "u1", session_id="s1", exercise="squat", rule_version="squat-v1"
                )
                event = {
                    "event_id": "evt-1",
                    "kind": "rep_completed",
                    "occurred_at": 10.0,
                    "frame_id": 5,
                    "rule_version": "squat-v1",
                    "facts": {"valid": True},
                }
                rep = {
                    "rep_id": "rep-u1-s1-1",
                    "rep_index": 1,
                    "valid": True,
                    "started_at": 8.0,
                    "completed_at": 10.0,
                    "duration_ms": 2000.0,
                    "min_knee_angle_deg": 100.0,
                    "max_knee_angle_deg": 175.0,
                    "usable_sample_ratio": 1.0,
                    "reason_codes": (),
                    "rule_version": "squat-v1",
                    "evidence_start_frame": 2,
                    "evidence_end_frame": 5,
                }
                first_event, first_rep = store.record_event_and_rep("u1", "s1", event, rep)
                replay_event, replay_rep = store.record_event_and_rep("u1", "s1", event, rep)
                self.assertEqual(first_event["event_id"], replay_event["event_id"])
                self.assertEqual(first_rep["rep_id"], replay_rep["rep_id"])
                self.assertEqual(len(store.list_events("u1")), 1)
                self.assertEqual(len(store.list_reps("u1")), 1)
                result = store.query_training("u1", exercise="squat")[0]
                self.assertEqual(result["completed_reps"], 1)
                self.assertEqual(result["valid_reps"], 1)

    def test_same_frame_can_have_multiple_events(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_store(directory) as store:
                store.ensure_user("u1")
                store.create_session("u1", session_id="s1")
                for event_id, kind in (("evt-phase", "phase_changed"), ("evt-rep", "rep_completed")):
                    store.record_event(
                        "u1", "s1", event_id=event_id, kind=kind, occurred_at=1.0, frame_id=7
                    )
                rows = store.list_events("u1", session_id="s1")
                self.assertEqual(len(rows), 2)
                self.assertEqual({row["kind"] for row in rows}, {"phase_changed", "rep_completed"})

    def test_scope_isolation_and_delete_epoch_cancel_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_store(directory) as store:
                store.ensure_user("u1")
                store.ensure_user("u2")
                store.create_session("u1", session_id="s1")
                store.create_session("u2", session_id="s2")
                store.put_memory_note("u1", note="knee alignment improved", note_id="n1")
                job = store.enqueue_outbox(
                    "u1", job_type="embed_note", idempotency_key="n1-v1", source_id="n1"
                )
                self.assertEqual(store.list_memory_notes("u2"), [])
                with self.assertRaises(ScopeError):
                    store.record_event("u2", "s1", event_id="bad", kind="x")
                old_epoch = store.get_memory_epoch("u1")
                deleted = store.delete_user("u1")
                self.assertEqual(deleted["deleted"], 1)
                self.assertGreater(deleted["memory_epoch"], old_epoch)
                self.assertIsNone(store.get_user("u1"))
                self.assertEqual(store.list_outbox("u2"), [])
                # The old job and private FTS row are gone; recreation starts
                # at the tombstone epoch rather than accepting stale writes.
                store.ensure_user("u1")
                self.assertGreater(store.get_memory_epoch("u1"), old_epoch)
                self.assertEqual(store.list_memory_notes("u1"), [])
                self.assertNotIn(job["job_id"], {row["job_id"] for row in store.list_outbox("u1")})

    def test_profile_feedback_outbox_and_restart_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coach.sqlite3"
            with MemoryStore(path) as store:
                store.ensure_user("u1")
                store.create_session("u1", session_id="s1")
                old = store.put_profile_fact("u1", "goal", "strength", fact_id="f1")
                current = store.put_profile_fact("u1", "goal", "mobility", fact_id="f2")
                self.assertTrue(current["active"])
                self.assertFalse(store.list_profile_facts("u1", include_inactive=True)[1]["active"])
                feedback = store.record_feedback(
                    "u1", "s1", cue_text="Knees out", idempotency_key="cue-1"
                )
                self.assertEqual(
                    feedback["feedback_id"],
                    store.record_feedback(
                        "u1", "s1", cue_text="different text", idempotency_key="cue-1"
                    )["feedback_id"],
                )
                job = store.enqueue_outbox(
                    "u1", job_type="summary", idempotency_key="sum-1", available_at=0.0
                )
                claimed = store.claim_outbox("u1", now=100.0)
                self.assertEqual(claimed[0]["job_id"], job["job_id"])
                store.finish_session("u1", "s1", status="completed", ended_at=20.0)
                self.assertEqual(store.recover_interrupted_sessions(user_id="u1"), 0)
                self.assertEqual(old["fact_id"], "f1")
            with MemoryStore(path) as reopened:
                self.assertEqual(reopened.get_user("u1")["user_id"], "u1")
                self.assertEqual(reopened.get_session("u1", "s1")["status"], "completed")
                self.assertEqual(reopened.list_profile_facts("u1")[0]["value"], "mobility")

    def test_transaction_rolls_back_and_missing_outbox_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.make_store(directory) as store:
                store.ensure_user("u1")
                store.create_session("u1", session_id="s1")
                with self.assertRaises(RuntimeError):
                    with store.transaction():
                        store.record_event("u1", "s1", event_id="evt", kind="x")
                        raise RuntimeError("abort")
                self.assertEqual(store.list_events("u1"), [])
                with self.assertRaises(NotFoundError):
                    store.complete_outbox("u1", "missing")


if __name__ == "__main__":
    unittest.main()
