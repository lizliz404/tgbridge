"""Limit governor: classification, reset windows, durable park/resume."""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import tgbridge
from tgbridge_core import execution
from tgbridge_core.inbox import Inbox
from tgbridge_core.limits import Governor, backoff, parse_reset


class ParseResetTests(unittest.TestCase):
    def test_reset_in_hours_minutes(self):
        t = parse_reset("Your limit will reset in 14 hours 52 minutes "
                        "(at Nov 8 04:01 UTC)")
        self.assertIsNotNone(t)
        self.assertGreater(t, time.time() + 14 * 3600)
        self.assertLess(t, time.time() + 15 * 3600)

    def test_reset_in_seconds_and_minutes(self):
        t = parse_reset("rate limit, reset in 3 minutes 20 seconds")
        self.assertIsNotNone(t)
        self.assertAlmostEqual(t, time.time() + 200, delta=5)

    def test_reset_at_absolute(self):
        t = parse_reset("quota exceeded (at Dec 31 23:59 UTC)")
        self.assertIsNotNone(t)
        self.assertGreater(t, time.time() - 60)

    def test_no_reset_marker(self):
        self.assertIsNone(parse_reset("something else went wrong"))
        self.assertIsNone(parse_reset(None))


class BackoffTests(unittest.TestCase):
    def test_monotonic_and_capped(self):
        d1 = backoff(1, base=30.0, cap=300.0)
        d4 = backoff(4, base=30.0, cap=300.0)
        d10 = backoff(10, base=30.0, cap=300.0)
        self.assertGreaterEqual(d1, 30.0)
        self.assertGreaterEqual(d4, 240.0)
        self.assertLessEqual(d10, 300.0 + 15.0)

    def test_jitter_within_bound(self):
        for _ in range(20):
            self.assertLess(backoff(1, base=30.0, cap=300.0), 30.0 + 15.0)


class GovernorTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.gov = Governor(self.dir)

    def test_park_due_pop_snapshot(self):
        e = self.gov.park("42", {"chat_id": 42, "prompt": "p",
                                 "resume_at": time.time() + 60})
        self.assertEqual(e["attempt"], 1)
        self.assertEqual(self.gov.get("42")["prompt"], "p")
        self.assertEqual(self.gov.due(time.time() + 61)[0][0], "42")
        self.assertEqual(self.gov.due(), [])
        self.assertEqual(len(self.gov.snapshot()), 1)
        popped = self.gov.pop("42", "test")
        self.assertEqual(popped["prompt"], "p")
        self.assertIsNone(self.gov.pop("42"))

    def test_attempt_increments_on_repark(self):
        self.gov.park("1", {"resume_at": time.time() + 10})
        e = self.gov.park("1", {"resume_at": time.time() + 20})
        self.assertEqual(e["attempt"], 2)

    def test_registry_survives_reopen_and_log_appends(self):
        self.gov.park("7", {"chat_id": 7, "prompt": "x",
                            "resume_at": time.time() + 30})
        self.gov.pop("7", "gone")
        reopened = Governor(self.dir)
        self.assertEqual(reopened.snapshot(), [])
        events = reopened.events_tail()
        kinds = [e["event"] for e in events]
        self.assertIn("parked", kinds)
        self.assertIn("unparked", kinds)

    def test_events_tail(self):
        self.gov.park("9", {"resume_at": 1})
        tail = self.gov.events_tail(5)
        self.assertEqual(tail[-1]["event"], "parked")


class LimitParkTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = {"_limits": Governor(self.dir), "bot_token": "t",
                    "workdir": self.dir}
        self.timers = []

    def tearDown(self):
        for t in self.timers:
            t.cancel()

    def _park(self, err="weekly limit reached", prompt="do the thing"):
        with mock.patch.object(execution, "_limit_timer",
                               side_effect=lambda *a: self.timers.append(
                                   mock.Mock()) or self.timers[-1]):
            return tgbridge.limit_park(self.cfg, 42, 10, prompt,
                                       ["tg:42:10"], err)

    def test_quota_error_parks_with_reset_window(self):
        err = ("Reached free model rate limit. Your limit will reset in "
               "2 hours 30 minutes (at Dec 31 04:01 UTC).")
        parked = self._park(err=err)
        self.assertIsNotNone(parked)
        self.assertEqual(parked["kind"], "rate_limited")
        self.assertEqual(parked["attempt"], 1)
        entry = self.cfg["_limits"].get("42")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["prompt"], "do the thing")
        self.assertEqual(entry["input_ids"], ["tg:42:10"])
        # ~2.5h out, not the short backoff
        self.assertGreater(entry["resume_at"], time.time() + 2 * 3600)

    def test_unavailable_error_parks_with_backoff(self):
        parked = self._park(err="opencode not found on PATH")
        self.assertIsNotNone(parked)
        self.assertEqual(parked["kind"], "retryable")
        entry = self.cfg["_limits"].get("42")
        self.assertLess(entry["resume_at"], time.time() + 60)

    def test_other_error_does_not_park(self):
        self.assertIsNone(self._park(err="the model returned garbage"))
        self.assertIsNone(self.cfg["_limits"].get("42"))

    def test_cancel_never_parks(self):
        self.assertIsNone(self._park(err=tgbridge.CANCEL_MSG))

    def test_attempt_budget_exhausts(self):
        for _ in range(8):
            self.assertIsNotNone(self._park())
        self.assertIsNone(self._park())


class LimitFireTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.gov = Governor(self.dir)
        self.inbox = Inbox(os.path.join(self.dir, "inputs.json"))
        self.cfg = {"_limits": self.gov, "_inbox": self.inbox,
                    "bot_token": "t", "workdir": self.dir}

    def test_fire_requeues_interrupted_inputs_with_resume_note(self):
        self.inbox.receive("tg:42:10", 42, 10, "orig", user_id=7)
        self.inbox.transition(["tg:42:10"], "interrupted")
        self.gov.park("42", {"chat_id": 42, "message_id": 10,
                             "prompt": "suffixed prompt",
                             "input_ids": ["tg:42:10"],
                             "attempt": 1, "resume_at": 1})
        sent = []
        q = __import__("queue").Queue()
        with mock.patch.object(tgbridge, "send",
                               side_effect=lambda *a, **k: sent.append(a)), \
             mock.patch.object(tgbridge, "PROMPT_Q", q):
            tgbridge.limit_fire(self.cfg, "42")
        entry = q.get_nowait()
        self.assertEqual(entry["chat_id"], 42)
        self.assertTrue(entry["prompt"].startswith("[Auto-resume"))
        self.assertIn("suffixed prompt", entry["prompt"])
        self.assertTrue(entry["limit_resumed"])
        self.assertEqual(entry["input_ids"], ["tg:42:10"])
        self.assertEqual(self.inbox.data["tg:42:10"]["status"], "queued")
        self.assertIsNone(self.gov.get("42"))
        self.assertTrue(sent)

    def test_fire_keeps_slash_commands_dispatchable(self):
        """A parked /goal-* must resume as a command, not note-prefixed prose."""
        self.gov.park("42", {"chat_id": 42, "message_id": 10,
                             "prompt": "/goal-resume", "input_ids": [],
                             "attempt": 1, "resume_at": 1})
        q = __import__("queue").Queue()
        with mock.patch.object(tgbridge, "send"), \
             mock.patch.object(tgbridge, "PROMPT_Q", q):
            tgbridge.limit_fire(self.cfg, "42")
        self.assertEqual(q.get_nowait()["prompt"], "/goal-resume")

    def test_fire_superseded_when_inputs_already_resumed(self):
        self.inbox.receive("tg:42:10", 42, 10, "orig", user_id=7)
        self.inbox.transition(["tg:42:10"], "executing")
        self.gov.park("42", {"chat_id": 42, "message_id": 10,
                             "prompt": "p", "input_ids": ["tg:42:10"],
                             "attempt": 1, "resume_at": 1})
        q = __import__("queue").Queue()
        with mock.patch.object(tgbridge, "send"), \
             mock.patch.object(tgbridge, "PROMPT_Q", q):
            tgbridge.limit_fire(self.cfg, "42")
        self.assertTrue(q.empty())
        self.assertIsNone(self.gov.get("42"))  # dropped as superseded

    def test_new_turn_supersedes_parked_retry(self):
        self.gov.park("42", {"chat_id": 42, "prompt": "p",
                             "resume_at": time.time() + 60})
        tgbridge.limit_supersede(self.cfg, 42)
        self.assertIsNone(self.gov.get("42"))

    def test_rearm_schedules_timers_for_persisted_entries(self):
        self.gov.park("42", {"chat_id": 42, "prompt": "p",
                             "resume_at": time.time() + 30})
        with mock.patch.object(execution, "_limit_timer") as timer:
            tgbridge.rearm_limits(self.cfg)
        timer.assert_called_once()


class LimitCommandTests(unittest.TestCase):
    """The /limits surface follows the test_commands.py style."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = {"bot_token": "fixture", "allowed_chats": [42],
                    "allowed_user_ids": [7],
                    "_limits": Governor(self.dir)}

    def message(self, text):
        return {"message": {"message_id": 10,
                            "chat": {"id": 42, "type": "private"},
                            "from": {"id": 7}, "text": text}}

    def test_limits_lists_parked_entries_and_events(self):
        self.cfg["_limits"].park("42", {
            "chat_id": 42, "prompt": "p", "error": "weekly limit",
            "error_kind": "rate_limited", "attempt": 2,
            "resume_at": time.time() + 300})
        with mock.patch.object(tgbridge, "send") as send:
            tgbridge.handle_update(self.cfg, {}, self.message("/limits"),
                                   state_path=None)
        body = send.call_args.args[2]
        self.assertIn("attempt 2", body)
        self.assertIn("rate_limited", body)
        self.assertIn("parked", body)

    def test_limits_clear_pops_this_chat(self):
        self.cfg["_limits"].park("42", {"chat_id": 42,
                                        "resume_at": time.time() + 60})
        with mock.patch.object(tgbridge, "send") as send:
            tgbridge.handle_update(self.cfg, {}, self.message("/limits clear"),
                                   state_path=None)
        self.assertIsNone(self.cfg["_limits"].get("42"))
        self.assertIn("cleared", send.call_args.args[2])

    def test_limits_empty_queue(self):
        with mock.patch.object(tgbridge, "send") as send:
            tgbridge.handle_update(self.cfg, {}, self.message("/limits"),
                                   state_path=None)
        self.assertIn("empty", send.call_args.args[2])


class WorkerParkIntegrationTests(unittest.TestCase):
    """worker() parks quota errors instead of failing the turn."""

    def test_quota_error_parks_and_notifies(self):
        dir_ = tempfile.mkdtemp()
        cfg = {"_limits": Governor(dir_), "bot_token": "t",
               "workdir": dir_, "runner": "pi"}
        inbox = Inbox(os.path.join(dir_, "inputs.json"))
        cfg["_inbox"] = inbox
        inbox.receive("tg:42:10", 42, 10, "build x", user_id=7)
        sent = []
        entry = {"chat_id": 42, "message_id": 10, "prompt": "build x",
                 "input_ids": ["tg:42:10"]}
        dispatcher = mock.Mock()
        dispatcher.release_retired_worker.return_value = True
        dispatcher.stopped = False
        cfg["_chat_dispatcher"] = dispatcher
        with mock.patch.object(tgbridge, "run_with_fallbacks",
                               return_value=("sid", None, "weekly limit reached")), \
             mock.patch.object(tgbridge, "send_retry",
                               side_effect=lambda *a, **k: sent.append(a) or True), \
             mock.patch.object(tgbridge, "api",
                               return_value={"ok": True, "result": {}}), \
             mock.patch.object(tgbridge, "react"), \
             mock.patch.object(tgbridge, "edit_status"), \
             mock.patch.object(tgbridge, "typing_loop"), \
             mock.patch.object(tgbridge, "save_json"), \
             mock.patch.object(tgbridge, "audit"), \
             mock.patch.object(tgbridge, "log"), \
             mock.patch.object(execution, "_limit_timer") as timer, \
             mock.patch.object(tgbridge, "send_document"):
            from tgbridge_core.execution import worker
            rt = mock.MagicMock(wraps=tgbridge)
            rt.PROMPT_Q = __import__("queue").Queue()
            rt.STOPPING = False
            rt.RUN_LOCK = threading.Lock()
            rt.STATE_LOCK = threading.Lock()
            rt.RUN_STATE = {"busy": False, "current": None, "cancel": False,
                            "active_input_ids": []}
            rt.execution_id = "x"  # timed-get path → retires when queue drains
            rt.PROMPT_Q.put(entry)
            worker(rt, cfg, {})
        parked = cfg["_limits"].get("42")
        self.assertIsNotNone(parked)
        self.assertEqual(parked["prompt"].split("\n\n(To give files")[0],
                         "build x")
        self.assertTrue(timer.called)
        self.assertTrue(any("parked" in str(c[2]) for c in sent))
