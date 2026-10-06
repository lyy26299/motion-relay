"""Native output admission shares the application arbiter; no media SDK."""
import time
import unittest

from coach.arbiter import FeedbackKind
from coach.memory.store import MemoryStore
from coach.runtime import MotionRuntime
from coach.session_agent import SessionAgentBridge
from coach.working_memory import WorkingMemory


class NativeAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore(":memory:")
        self.store.ensure_user("u")
        self.store.create_session("u", session_id="s")
        memory = WorkingMemory(session_id="s")
        self.bridge = SessionAgentBridge(
            user_id="u", session_id="s", memory=memory, store=self.store,
            session_epoch=0, motion_runtime=MotionRuntime(session_id="s", memory=memory),
        )

    async def asyncTearDown(self):
        await self.bridge.close()
        self.store.close()

    async def test_native_reply_uses_same_arbiter(self):
        guard = self.bridge.admit_native_response("native")
        self.assertTrue(guard())
        self.assertEqual(self.bridge.arbiter.active_kind, FeedbackKind.ANSWER)
        admission = self.bridge.arbiter.admit(FeedbackKind.REP, "rep", deadline=time.monotonic()+4)
        self.assertEqual(admission.reason, "busy")

    async def test_native_reply_cannot_preempt_safety(self):
        self.bridge.arbiter.admit(FeedbackKind.SAFETY, "stop", deadline=time.monotonic()+4)
        self.assertIsNone(self.bridge.admit_native_response("native"))
        self.assertEqual(self.bridge.arbiter.active_kind, FeedbackKind.SAFETY)

    async def test_safety_revokes_native_guard(self):
        guard = self.bridge.admit_native_response("native")
        self.bridge.arbiter.admit(FeedbackKind.SAFETY, "stop", deadline=time.monotonic()+4)
        self.assertFalse(guard())

    async def test_vad_revokes_native_guard(self):
        guard = self.bridge.admit_native_response("native")
        self.bridge.on_user_speech_started()
        self.assertFalse(guard())

    async def test_normal_final_transcript_after_vad_preserves_native_answer(self):
        self.bridge.on_user_speech_started()
        guard = self.bridge.admit_native_response("native")
        self.bridge.on_user_transcript("你好")
        self.assertTrue(guard())

    async def test_routed_final_transcript_still_revokes_native_answer(self):
        self.bridge.on_user_speech_started()
        guard = self.bridge.admit_native_response("native")
        self.bridge.on_user_transcript("上次训练记录是多少")
        self.assertFalse(guard())

    async def test_direct_text_without_vad_invalidates_old_reply(self):
        guard = self.bridge.admit_native_response("native")
        self.bridge.on_user_transcript("你好")
        self.assertFalse(guard())

    async def test_close_revokes_native_guard_and_rejects_new_admission(self):
        guard = self.bridge.admit_native_response("native")
        await self.bridge.close()
        self.assertFalse(guard())
        self.assertIsNone(self.bridge.admit_native_response("late"))

    async def test_native_admission_does_not_access_sqlite(self):
        self.store.close()
        guard = self.bridge.admit_native_response("native")
        self.assertTrue(guard())
