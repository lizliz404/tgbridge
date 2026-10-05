"""Pi RPC transport: segmented delivery, tool trail, and live steering.

The fixture is a real child process that speaks the documented Pi RPC
protocol (docs/rpc.md, docs/json.md), so the adapter is exercised over an
actual pipe — not a mock of its own parsing.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import tgbridge

FAKE_PI = r'''#!/usr/bin/env python3
"""Fixture Pi RPC child: canned records, records every stdin command."""

import json
import os
import sys
import threading
import time

LOG = os.environ["PI_FAKE_LOG"]
DELAY = float(os.environ.get("PI_FAKE_STEP_DELAY", "0"))
HELP = "--mode <mode>                  Output mode: text (default), json, or rpc"


def record(entry):
    with open(LOG, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def emit(entry, delay=False):
    if delay and DELAY:
        time.sleep(DELAY)
    sys.stdout.write(json.dumps(entry) + "\n")
    sys.stdout.flush()


def respond(req_id, command, data=None):
    entry = {"id": req_id, "type": "response", "command": command, "success": True}
    if data is not None:
        entry["data"] = data
    emit(entry)


if "--help" in sys.argv:
    print(HELP)
    sys.exit(0)


def handle(command):
    kind = command.get("type")
    if kind == "get_state":
        respond(command["id"], "get_state", {"sessionId": "sid-fixture", "isStreaming": False})
    elif kind == "prompt":
        respond(command["id"], "prompt", {"disposition": "started"})
        emit({"type": "agent_start"}, delay=True)
        emit({"type": "message_update", "assistantMessageEvent": {
            "type": "thinking_delta", "contentIndex": 0, "delta": "weighing options"}}, delay=True)
        emit({"type": "message_end", "message": {
            "role": "assistant",
            "stopReason": "toolUse",
            "content": [{"type": "text", "text": "first segment"}],
            "usage": {"totalTokens": 11, "cost": {"total": 0.01}}}}, delay=True)
        emit({"type": "tool_execution_start", "toolCallId": "c1",
              "toolName": "bash", "args": {"command": "ls -la /tmp"}}, delay=True)
        emit({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
              "result": {}, "isError": False}, delay=True)
        emit({"type": "message_end", "message": {
            "role": "assistant",
            "stopReason": "stop",
            "content": [{"type": "text", "text": "second segment"}],
            "usage": {"totalTokens": 7, "cost": {"total": 0.02}}}}, delay=True)
        emit({"type": "queue_update", "steering": ["stop"], "followUp": []}, delay=True)
        emit({"type": "queue_update", "steering": [], "followUp": []}, delay=True)
        emit({"type": "agent_end", "messages": [], "willRetry": False}, delay=True)
        emit({"type": "agent_settled"})
    elif kind == "steer":
        respond(command["id"], "steer", {"disposition": "queued"})
    elif kind == "clear_queue":
        respond(command["id"], "clear_queue", {"steering": [], "followUp": []})
    elif kind == "abort":
        respond(command["id"], "abort", {})
    else:
        respond(command.get("id"), str(kind), {})


# Real Pi handles stdin while a run streams; the fixture must do the same or a
# steer sent mid-run would only be answered after the prompt sequence finished.
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        command = json.loads(line)
    except json.JSONDecodeError:
        continue
    record(command)
    threading.Thread(target=handle, args=(command,), daemon=True).start()
'''


class FakePiFixture:
    """A temporary PI_BIN fixture plus the record of what it received."""

    def __init__(self, case, step_delay=0.0):
        self.case = case
        self.step_delay = step_delay
        self.tmp = tempfile.TemporaryDirectory()
        self.binary = os.path.join(self.tmp.name, "pi")
        self.command_log = os.path.join(self.tmp.name, "commands.jsonl")
        with open(self.binary, "w") as fh:
            fh.write(FAKE_PI)
        os.chmod(self.binary, 0o755)
        self._env = None

    def __enter__(self):
        self._env = mock.patch.dict(
            os.environ,
            {
                "PI_BIN": self.binary,
                "PI_FAKE_LOG": self.command_log,
                "PI_FAKE_STEP_DELAY": str(self.step_delay),
            },
        )
        self._env.start()
        return self

    def __exit__(self, *exc):
        self._env.stop()
        self.tmp.cleanup()
        return False

    def commands(self):
        if not os.path.exists(self.command_log):
            return []
        with open(self.command_log) as fh:
            return [json.loads(line) for line in fh if line.strip()]


class PiRpcTransportTests(unittest.TestCase):
    def setUp(self):
        # Keep the production audit trail clean: these runs are fixtures.
        patcher = mock.patch.object(tgbridge, "audit", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        sent = []
        self.sent = sent

        def fake_send(token, chat_id, text, reply_to=None, chunk_limit=None):
            sent.append({"chat_id": chat_id, "text": text, "reply_to": reply_to})
            return True

        sender = mock.patch.object(tgbridge, "send", side_effect=fake_send)
        sender.start()
        self.addCleanup(sender.stop)
        api = mock.patch.object(
            tgbridge, "api", lambda *a, **k: {"ok": True, "result": {"message_id": 5}}
        )
        api.start()
        self.addCleanup(api.stop)
        with tgbridge.RUN_LOCK:
            tgbridge.RUN_STATE.update(
                {
                    "busy": True,
                    "current": {"chat": 42},
                    "proc": None,
                    "cancel": False,
                    "pi_sid": None,
                    "pi_run_id": 0,
                    "steer_pending": 0,
                    "steer_count": 0,
                }
            )
        self.addCleanup(self._reset_run_state)

    def _reset_run_state(self):
        with tgbridge.RUN_LOCK:
            tgbridge.RUN_STATE.update(
                {
                    "busy": False,
                    "current": None,
                    "proc": None,
                    "cancel": False,
                    "pi_sid": None,
                    "pi_steers": {},
                    "steer_pending": 0,
                    "steer_count": 0,
                }
            )

    def _live(self):
        return {
            "chat_id": 42,
            "status_id": 5,
            "trail": [],
            "notes": [],
            "start": time.time(),
            "last_edit": 0,
            "reply_to": 99,
            "streamed": False,
            "tokens": 0,
            "cost": 0.0,
        }

    def test_each_assistant_segment_becomes_its_own_message(self):
        with FakePiFixture(self) as fixture:
            cfg = {"bot_token": "token", "workdir": fixture.tmp.name}
            live = self._live()
            sid, answer, err = tgbridge.run_pi_rpc(cfg, None, "do the thing", live)

        self.assertIsNone(err)
        self.assertEqual(sid, "sid-fixture")
        self.assertEqual(
            [m["text"] for m in self.sent], ["first segment", "second segment"]
        )
        # The first segment answers the triggering message; later ones stand alone.
        self.assertEqual(self.sent[0]["reply_to"], 99)
        self.assertIsNone(self.sent[1]["reply_to"])
        self.assertTrue(live["streamed"])
        self.assertEqual(answer, "first segment\n\nsecond segment")
        self.assertIn("🔧 bash: ls -la /tmp", live["trail"])
        self.assertEqual(live["trail"].count("🔧 bash: ls -la /tmp"), 1)
        self.assertEqual(live["tokens"], 18)
        self.assertAlmostEqual(live["cost"], 0.03)
        # Thinking never leaks into a chat message; it stays in the status line.
        self.assertEqual(live["thinking"], "")
        self.assertNotIn("weighing options", " ".join(m["text"] for m in self.sent))

    def test_without_a_live_sink_the_joined_answer_is_returned(self):
        with FakePiFixture(self) as fixture:
            cfg = {"bot_token": "token", "workdir": fixture.tmp.name}
            sid, answer, err = tgbridge.run_pi_rpc(cfg, None, "do the thing")

        self.assertIsNone(err)
        self.assertEqual(sid, "sid-fixture")
        self.assertEqual(answer, "first segment\n\nsecond segment")
        self.assertEqual(self.sent, [])

    def test_human_message_steers_the_live_turn_and_is_acknowledged(self):
        steered = {}

        def steer_thread():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with tgbridge.RUN_LOCK:
                    sid = tgbridge.RUN_STATE.get("pi_sid")
                    run_id = tgbridge.RUN_STATE.get("pi_run_id")
                if sid and run_id:
                    break
                time.sleep(0.02)
            target = tgbridge._begin_steer(42)
            steered["target"] = target
            if target:
                tgbridge._pi_steer_deliver(
                    {"bot_token": "token"}, target["sid"], target["run_id"],
                    "stop and check the tests", 42, 99,
                )

        worker = threading.Thread(target=steer_thread, daemon=True)
        worker.start()
        with FakePiFixture(self, step_delay=0.3) as fixture:
            cfg = {"bot_token": "token", "workdir": fixture.tmp.name}
            live = self._live()
            sid, answer, err = tgbridge.run_pi_rpc(cfg, "sid-fixture", "go", live)
            commands = fixture.commands()
        worker.join(timeout=5)

        self.assertIsNone(err)
        self.assertEqual(steered["target"]["transport"], "pi_rpc_steer")
        self.assertEqual(steered["target"]["sid"], "sid-fixture")
        steers = [c for c in commands if c.get("type") == "steer"]
        self.assertEqual(len(steers), 1)
        self.assertEqual(steers[0]["message"], "stop and check the tests")
        self.assertTrue(steers[0]["id"].startswith("steer-"))
        with tgbridge.RUN_LOCK:
            self.assertEqual(tgbridge.RUN_STATE["steer_count"], 1)
            self.assertEqual(tgbridge.RUN_STATE["steer_pending"], 0)
        # A steer must not produce a fallback "kept as the next turn" message.
        self.assertEqual(
            [m for m in self.sent if "next turn" in m["text"]], []
        )

    def test_queue_update_renders_steering_in_the_status_not_as_a_message(self):
        with FakePiFixture(self) as fixture:
            cfg = {"bot_token": "token", "workdir": fixture.tmp.name}
            live = self._live()
            tgbridge.run_pi_rpc(cfg, None, "do the thing", live)

        self.assertEqual(
            live["notes"], ["🧭 steering queued (1)", "🧭 steering delivered"]
        )
        self.assertNotIn("🧭", " ".join(m["text"] for m in self.sent))
        self.assertEqual(len(live["trail"]), 1)


class PiRpcHealthTests(unittest.TestCase):
    def test_health_probe_accepts_an_rpc_capable_binary(self):
        with FakePiFixture(self) as fixture:
            self.assertTrue(tgbridge.pi_rpc_ok({}))
            self.assertTrue(os.path.exists(fixture.binary))

    def test_health_probe_rejects_a_binary_without_rpc(self):
        result = mock.Mock(returncode=0, stdout="--mode <mode>  text or json\n", stderr="")
        with mock.patch.object(tgbridge.subprocess, "run", return_value=result):
            self.assertFalse(tgbridge.pi_rpc_ok({}))


class StreamedDeliveryTests(unittest.TestCase):
    def test_streamed_runs_do_not_resend_the_joined_answer(self):
        self.assertIsNone(tgbridge.deliverable_answer({"streamed": True}, "hello"))
        self.assertEqual(tgbridge.deliverable_answer({"streamed": False}, "hello"), "hello")
        self.assertEqual(tgbridge.deliverable_answer(None, "hello"), "hello")
        self.assertEqual(tgbridge.deliverable_answer({"streamed": False}, None), "")

    def test_fallback_attempt_clears_the_streamed_flag(self):
        live = {"streamed": True, "trail": [], "start": time.time(), "last_edit": 0}

        def failed(cfg, session_id, prompt, live=None):
            self.assertFalse(live["streamed"])
            # Simulate a Pi run that streamed segments and then died.
            live["streamed"] = True
            return session_id, None, "quota reached"

        def answered(cfg, session_id, prompt, live=None):
            self.assertFalse(live["streamed"])
            return "fallback-session", "fallback answer", None

        adapters = {
            "pi": {"healthy": lambda cfg: True, "run": failed},
            "codex": {"healthy": lambda cfg: True, "run": answered},
        }
        cfg = {
            "runner": "pi",
            "runner_mode": "server",
            "runner_fallbacks": [{"runner": "codex"}],
        }
        with mock.patch.object(tgbridge, "SERVER_RUNNERS", adapters):
            sid, answer, err = tgbridge.run_with_fallbacks(cfg, "s1", "go", live)
        self.assertIsNone(err)
        self.assertEqual(sid, "fallback-session")
        self.assertIn("fallback answer", answer)
        self.assertFalse(live["streamed"])


if __name__ == "__main__":
    unittest.main()
