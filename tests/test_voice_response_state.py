"""Pure response identity tests; no media SDK or cloud required."""
import unittest

from coach.voice_state import ResponseWindow


class ResponseWindowTests(unittest.TestCase):
    def test_one_active_response_cannot_be_overwritten(self):
        window = ResponseWindow()
        self.assertEqual(window.begin("one"), "started")
        self.assertEqual(window.begin("two"), "conflicting_created")
        self.assertEqual(window.active_id, "one")
        self.assertTrue(window.retired("two"))

    def test_duplicate_created_does_not_reopen_audio(self):
        window = ResponseWindow()
        window.begin("one")
        window.finish_audio("one")
        self.assertEqual(window.begin("one"), "duplicate_created")
        self.assertFalse(window.accepts("one", audio=True))

    def test_missing_or_foreign_ids_are_not_aliases_for_current(self):
        window = ResponseWindow()
        window.begin("current")
        for value in (None, "", "other", [], {}, 1, True):
            with self.subTest(value=value):
                self.assertFalse(window.accepts(value))
                self.assertFalse(window.finish(value))
                self.assertEqual(window.active_id, "current")

    def test_terminal_identity_cannot_restart(self):
        window = ResponseWindow()
        window.begin("one")
        self.assertTrue(window.finish("one"))
        self.assertEqual(window.begin("one"), "retired_created")
        self.assertIsNone(window.active_id)

    def test_terminal_before_created_is_retired(self):
        window = ResponseWindow()
        self.assertFalse(window.finish("one"))
        self.assertEqual(window.begin("one"), "retired_created")

    def test_audio_end_is_once_but_transcript_can_finish(self):
        window = ResponseWindow()
        window.begin("one")
        self.assertTrue(window.finish_audio("one"))
        self.assertFalse(window.finish_audio("one"))
        self.assertTrue(window.accepts("one"))
        self.assertFalse(window.accepts("one", audio=True))

    def test_interruption_retires_identity_and_allows_new_one(self):
        window = ResponseWindow()
        window.begin("one")
        window.invalidate()
        self.assertFalse(window.accepts("one"))
        self.assertEqual(window.begin("one"), "retired_created")
        self.assertEqual(window.begin("two"), "started")

    def test_replay_window_is_bounded_and_horizon_is_explicit(self):
        window = ResponseWindow(history_size=4)
        for i in range(1000):
            self.assertEqual(window.begin(str(i)), "started")
            window.finish(str(i))
        self.assertEqual(window.retired_count, 4)
        self.assertTrue(window.retired("999"))
        self.assertFalse(window.retired("0"))  # Not lifetime replay protection.

    def test_invalid_capacity_and_identity_fail_closed(self):
        for value in (0, -1, True, 1.2):
            with self.assertRaises(ValueError):
                ResponseWindow(history_size=value)
        window = ResponseWindow()
        for value in (None, "", "  ", "a" * 257, 3, {}):
            self.assertEqual(window.begin(value), "invalid_id")
        self.assertIsNone(window.active_id)


