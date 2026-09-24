"""Offline delivery receipts: reordered observers, transaction and scope races."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from itertools import permutations, product
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from coach.feedback_state import FEEDBACK_STATES, merge_feedback_status
from coach.memory.feedback import FeedbackRecorder
from coach.memory.store import MemoryStore, NotFoundError, ScopeError
from coach.operations import OperationCapacityError
from coach.runtime import MotionRuntime
from coach.session_agent import SessionAgentBridge


class FeedbackAlgebraTests(unittest.TestCase):
    def test_join_is_idempotent_for_every_state(self):
        for state in FEEDBACK_STATES:
            self.assertEqual(merge_feedback_status(state, state), state)

    def test_join_is_commutative_for_every_pair(self):
        for a, b in product(FEEDBACK_STATES, repeat=2):
            self.assertEqual(merge_feedback_status(a, b), merge_feedback_status(b, a))

    def test_join_is_associative_for_every_triple(self):
        for a, b, c in product(FEEDBACK_STATES, repeat=3):
            self.assertEqual(merge_feedback_status(merge_feedback_status(a, b), c),
                             merge_feedback_status(a, merge_feedback_status(b, c)))

    def test_interruption_is_absorbing_not_a_generation_success(self):
        for state in FEEDBACK_STATES:
            self.assertEqual(merge_feedback_status(state, "interrupted"), "interrupted")

    def test_different_known_terminal_outcomes_are_not_silently_success(self):
        self.assertEqual(merge_feedback_status("generation_completed", "generation_failed"),
                         "delivery_conflict")
        self.assertEqual(merge_feedback_status("generation_unknown", "generation_completed"),
                         "generation_completed")

    def test_invalid_states_are_rejected(self):
        for invalid in (None, 1, [], "", "played", "acknowledged", "COMPLETED"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                merge_feedback_status("accepted", invalid)


class FeedbackStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore(":memory:")
        self.addCleanup(self.store.close)
        self.store.ensure_user("u")
        self.store.create_session("u", session_id="s")
        self.receipt()
        self.recorder = FeedbackRecorder(self.store, "u", "s", 0)

    def receipt(self, **kwargs):
        return self.store.record_feedback("u", "s", feedback_id="f", cue_text="fixture",
                                          status="accepted", **kwargs)

    def row(self):
        return self.store.update_feedback("u", "f")

    def test_late_queued_cannot_erase_interruption(self):
        self.recorder.apply("f", "generated", "r")
        self.recorder.apply("f", "interrupted", "r")
        result = self.recorder.apply("f", "queued")
        self.assertFalse(result.changed)
        self.assertEqual(result.reason, "regression_ignored")
        self.assertEqual(self.row()["status"], "interrupted")
        self.assertEqual(self.row()["playback_state"], "interrupted")

    def test_all_arrival_orders_reach_same_completed_receipt(self):
        observations = ("queued", "generated", "generation_unknown", "generation_completed")
        for i, ordering in enumerate(permutations(observations)):
            fid = f"perm-{i}"
            self.store.record_feedback("u", "s", feedback_id=fid, cue_text="fixture",
                                       status="accepted")
            for state in ordering:
                self.recorder.apply(fid, state, "r")
            row = self.store.update_feedback("u", fid)
            self.assertEqual(row["status"], "generation_completed")
            self.assertEqual(row["playback_state"], "unknown")
            self.assertIsNone(row["played_at"])
            self.assertIsNone(row["acknowledged_at"])

    def test_conflicting_terminal_observations_are_explicit(self):
        self.recorder.apply("f", "generation_completed", "r")
        result = self.recorder.apply("f", "generation_failed", "r")
        self.assertEqual(result.reason, "outcome_conflict")
        self.assertEqual(self.row()["status"], "delivery_conflict")
        self.recorder.apply("f", "generated", "r")
        self.assertEqual(self.row()["status"], "delivery_conflict")

    def test_duplicates_do_not_rewrite_receipt(self):
        self.recorder.apply("f", "generated", "r")
        before = self.row()
        result = self.recorder.apply("f", "generated", "r")
        self.assertEqual(result.reason, "duplicate")
        self.assertFalse(result.changed)
        self.assertEqual(before, self.row())

    def test_response_id_binds_once_and_foreign_observation_is_rejected(self):
        self.recorder.apply("f", "generated", "r")
        before = self.row()
        result = self.recorder.apply("f", "interrupted", "foreign")
        self.assertEqual(result.reason, "response_id_conflict")
        self.assertEqual(before, self.row())
        self.recorder.apply("f", "generation_completed", None)
        self.assertEqual(self.row()["response_id"], "r")

    def test_late_id_enriches_without_regressing_status(self):
        self.recorder.apply("f", "interrupted")
        result = self.recorder.apply("f", "generated", "r")
        self.assertEqual(result.reason, "metadata_enriched")
        self.assertEqual(self.row()["status"], "interrupted")
        self.assertEqual(self.row()["response_id"], "r")

    def test_generation_and_interrupt_preserve_independent_playback_evidence(self):
        self.store.update_feedback("u", "f", playback_state="acknowledged",
                                   played_at=11.0, acknowledged_at=12.0)
        self.recorder.apply("f", "generation_completed", "r")
        self.recorder.apply("f", "interrupted", "r")
        row = self.row()
        self.assertEqual(row["playback_state"], "acknowledged")
        self.assertEqual(row["played_at"], 11.0)
        self.assertEqual(row["acknowledged_at"], 12.0)

    def test_invalid_input_never_mutates_or_creates_receipt(self):
        before = self.row()
        for fid, state, rid in (("f", "played", None), ("f", "accepted", None),
                                ("f", "delivery_conflict", None), ("", "queued", None),
                                ("f", "queued", ""), ("f", "queued", "x"*257),
                                ("f", None, None), ("f", "queued", 5)):
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.recorder.apply(fid, state, rid)
        with self.assertRaises(NotFoundError):
            self.recorder.apply("missing", "queued")
        self.assertEqual(before, self.row())

    def test_unmanaged_legacy_status_is_preserved(self):
        self.store.update_feedback("u", "f", status="legacy_external_observation")
        result = self.recorder.apply("f", "queued", "r")
        self.assertEqual(result.reason, "unmanaged_existing_status")
        self.assertEqual(self.row()["status"], "legacy_external_observation")
        self.assertIsNone(self.row()["response_id"])

    def test_same_user_other_session_cannot_update_receipt(self):
        self.store.create_session("u", session_id="other")
        other = FeedbackRecorder(self.store, "u", "other", 0)
        with self.assertRaises(ScopeError):
            other.apply("f", "interrupted")
        self.assertEqual(self.row()["status"], "accepted")

    def test_other_user_cannot_update_receipt(self):
        self.store.ensure_user("v")
        self.store.create_session("v", session_id="v-session")
        other = FeedbackRecorder(self.store, "v", "v-session", 0)
        with self.assertRaises(ScopeError):
            other.apply("f", "interrupted")
        self.assertEqual(self.row()["status"], "accepted")

    def test_deleted_session_is_not_resurrected(self):
        self.store.delete_session("u", "s")
        with self.assertRaises(ScopeError):
            self.recorder.apply("f", "queued")
        self.assertIsNone(self.store.get_session("u", "s"))

    def test_recreated_session_and_receipt_reject_old_epoch(self):
        self.store.delete_session("u", "s")
        self.store.create_session("u", session_id="s")
        self.receipt()
        with self.assertRaises(ScopeError):
            self.recorder.apply("f", "interrupted")
        self.assertEqual(self.row()["status"], "accepted")
        new = FeedbackRecorder(self.store, "u", "s", self.store.get_memory_epoch("u"))
        self.assertEqual(new.apply("f", "queued").status, "queued")

    def test_deleted_user_is_not_resurrected(self):
        self.store.delete_user("u")
        with self.assertRaises(ScopeError):
            self.recorder.apply("f", "queued")
        self.assertIsNone(self.store.get_user("u"))

    def test_recreated_user_rejects_old_epoch(self):
        self.store.delete_user("u")
        self.store.ensure_user("u")
        self.store.create_session("u", session_id="s")
        self.receipt()
        with self.assertRaises(ScopeError):
            self.recorder.apply("f", "interrupted")
        self.assertEqual(self.row()["status"], "accepted")

    def test_finished_session_accepts_late_observation_in_same_epoch(self):
        self.store.finish_session("u", "s")
        self.assertEqual(self.recorder.apply("f", "generation_completed", "r").status,
                         "generation_completed")

    def test_outer_transaction_rolls_back_partial_update_on_failure(self):
        original = self.store.update_feedback
        def fail_after_write(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs:
                raise RuntimeError("injected write failure")
            return result
        with patch.object(self.store, "update_feedback", side_effect=fail_after_write):
            with self.assertRaises(RuntimeError):
                self.recorder.apply("f", "generated", "r")
        self.assertEqual(self.row()["status"], "accepted")
        self.assertIsNone(self.row()["response_id"])

    def test_scope_is_immutable_and_validated(self):
        with self.assertRaises(FrozenInstanceError):
            self.recorder.user_id = "other"
        for user, session, epoch in (("", "s", 0), ("u", "", 0), ("u", "s", True),
                                      ("u", "s", -1), ("u", "s", 0.5)):
            with self.subTest(epoch=epoch), self.assertRaises(ValueError):
                FeedbackRecorder(self.store, user, session, epoch)


class FeedbackConnectionRaceTests(unittest.TestCase):
    def test_two_connections_serialize_read_merge_write_not_just_final_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipts.sqlite"
            a, b = MemoryStore(path), MemoryStore(path)
            try:
                a.ensure_user("u")
                a.create_session("u", session_id="s")
                a.record_feedback("u", "s", feedback_id="f", cue_text="fixture", status="accepted")
                ar, br = FeedbackRecorder(a, "u", "s", 0), FeedbackRecorder(b, "u", "s", 0)
                read, release, second_started = threading.Event(), threading.Event(), threading.Event()
                original = a.update_feedback
                def held_read(*args, **kwargs):
                    row = original(*args, **kwargs)
                    if not kwargs:
                        read.set()
                        if not release.wait(3):
                            raise TimeoutError("test release not signalled")
                    return row
                def interrupt():
                    second_started.set()
                    return br.apply("f", "interrupted", "r")
                with ThreadPoolExecutor(max_workers=2) as pool:
                    with patch.object(a, "update_feedback", side_effect=held_read):
                        older = pool.submit(ar.apply, "f", "queued", "r")
                        try:
                            self.assertTrue(read.wait(2))
                            newer = pool.submit(interrupt)
                            self.assertTrue(second_started.wait(2))
                            self.assertFalse(newer.done())
                        finally:
                            release.set()
                        older.result(3)
                        newer.result(3)
                self.assertEqual(a.update_feedback("u", "f")["status"], "interrupted")
            finally:
                a.close()
                b.close()

    def test_late_worker_after_other_connection_deletes_user_cannot_recreate_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipts.sqlite"
            a, b = MemoryStore(path), MemoryStore(path)
            try:
                a.ensure_user("u")
                a.create_session("u", session_id="s")
                a.record_feedback("u", "s", feedback_id="f", cue_text="fixture")
                recorder = FeedbackRecorder(a, "u", "s", 0)
                entered, release = threading.Event(), threading.Event()
                def late():
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError("test release not signalled")
                    return recorder.apply("f", "generated", "r")
                with ThreadPoolExecutor(max_workers=1) as pool:
                    task = pool.submit(late)
                    try:
                        self.assertTrue(entered.wait(2))
                        b.delete_user("u")
                    finally:
                        release.set()
                    with self.assertRaises(ScopeError):
                        task.result(3)
                self.assertIsNone(a.get_user("u"))
            finally:
                a.close()
                b.close()


class FeedbackBridgeRaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MemoryStore(":memory:")
        self.store.ensure_user("u")
        self.store.create_session("u", session_id="s")
        self.store.record_feedback("u", "s", feedback_id="f", cue_text="fixture", status="accepted")
        runtime = MotionRuntime(session_id="s")
        self.bridge = SessionAgentBridge(user_id="u", session_id="s", store=self.store,
                                        memory=runtime.memory, motion_runtime=runtime, session_epoch=0)

    async def asyncTearDown(self):
        await self.bridge.close()
        self.store.close()

    async def test_late_worker_cannot_regress_interrupted_callback(self):
        entered, release = threading.Event(), threading.Event()
        original = FeedbackRecorder.apply
        def delay(recorder, fid, state, rid=None):
            if state == "queued":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test release not signalled")
            return original(recorder, fid, state, rid)
        with patch.object(FeedbackRecorder, "apply", new=delay):
            old = asyncio.create_task(self.bridge.feedback_state("queued", "f", "r"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                await self.bridge.feedback_state("interrupted", "f", "r")
            finally:
                release.set()
                await old
        self.assertEqual(self.store.update_feedback("u", "f")["status"], "interrupted")
        self.assertEqual(self.bridge.feedback_metrics["regression_ignored"], 1)

    async def test_cancelled_wait_keeps_worker_owned_and_late_commit_monotonic(self):
        entered, release = threading.Event(), threading.Event()
        original = FeedbackRecorder.apply
        def delay(recorder, fid, state, rid=None):
            if state == "queued":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test release not signalled")
            return original(recorder, fid, state, rid)
        with patch.object(FeedbackRecorder, "apply", new=delay):
            old = asyncio.create_task(self.bridge.feedback_state("queued", "f", "r"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                old.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await old
                self.assertEqual(self.bridge.io.pending, 1)
                await self.bridge.feedback_state("interrupted", "f", "r")
            finally:
                release.set()
                # Close waits for actual completion instead of treating caller cancellation as rollback.
                self.assertEqual(await self.bridge.io.close(timeout_s=2), 0)
        self.assertEqual(self.bridge.feedback_metrics["wait_cancelled"], 1)
        self.assertEqual(self.store.update_feedback("u", "f")["status"], "interrupted")

    async def test_scope_rejection_is_observable_not_a_new_receipt(self):
        self.store.delete_user("u")
        await self.bridge.feedback_state("generated", "f", "r")
        self.assertEqual(self.bridge.feedback_metrics["scope_rejected"], 1)
        self.assertIsNone(self.store.get_user("u"))

    async def test_invalid_input_and_response_conflict_have_separate_metrics(self):
        await self.bridge.feedback_state("played", "f", "r")
        await self.bridge.feedback_state("generated", "f", "r")
        await self.bridge.feedback_state("interrupted", "f", "other")
        self.assertEqual(self.bridge.feedback_metrics["invalid_observation"], 1)
        self.assertEqual(self.bridge.feedback_metrics["response_id_conflict"], 1)
        self.assertEqual(self.store.update_feedback("u", "f")["status"], "generated")

    async def test_capacity_rejection_and_commit_wait_timeout_are_not_commit_success(self):
        with patch.object(self.bridge, "_io", new=AsyncMock(side_effect=OperationCapacityError("full"))):
            await self.bridge.feedback_state("queued", "f", None)
        with patch.object(self.bridge, "_io", new=AsyncMock(side_effect=TimeoutError("wait"))):
            await self.bridge.feedback_state("queued", "f", None)
        self.assertEqual(self.bridge.feedback_metrics["capacity_rejected"], 1)
        self.assertEqual(self.bridge.feedback_metrics["commit_wait_timeout"], 1)
        self.assertEqual(self.store.update_feedback("u", "f")["status"], "accepted")

    async def test_storage_exception_is_counted_and_propagates(self):
        with patch.object(self.bridge, "_io", new=AsyncMock(side_effect=RuntimeError("fixture"))):
            with self.assertRaises(RuntimeError):
                await self.bridge.feedback_state("queued", "f", None)
        self.assertEqual(self.bridge.feedback_metrics["storage_error"], 1)

    async def test_closed_bridge_does_not_accept_new_observations(self):
        await self.bridge.close()
        with patch.object(self.bridge, "_io", new=AsyncMock()) as io:
            await self.bridge.feedback_state("queued", "f", None)
            io.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
