"""Health diagnostics must describe the effective, currently running bridge."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import tgbridge


class DoctorTests(unittest.TestCase):
    def test_restart_resets_snapshot_without_touching_session_state(self):
        with tempfile.TemporaryDirectory() as root:
            health = os.path.join(root, "health.json")
            state = Path(root) / "state.json"
            state.write_text('{"offset": 42, "sessions": {"chat": "sid"}}')
            before = state.read_bytes()
            with mock.patch.object(tgbridge, "HEALTH_PATH", health):
                tgbridge.update_health(status="crashed", crash_error="old crash", last_poll_ok_at="old poll")
                tgbridge.update_health(status="failed", exit_error="old startup failure", stopped_at="old stop")
                starting = tgbridge.update_health(status="starting", pid=123)
                for key in ("crash_error", "exit_error", "stopped_at", "last_poll_ok_at"):
                    self.assertNotIn(key, starting)
                recovered = tgbridge.update_health(status="healthy", last_poll_ok_at="new poll")
                self.assertEqual(recovered["pid"], 123)
                self.assertEqual(recovered["last_poll_ok_at"], "new poll")
            self.assertEqual(state.read_bytes(), before)

    def report(self, service_ok, state=None):
        cfg = {"bot_token": "fixture", "runner": "codex", "workdir": "/fixture"}
        def load(path, default):
            return {tgbridge.CONFIG_PATH: cfg, tgbridge.STATE_PATH: state or {},
                    tgbridge.HEALTH_PATH: {"status": "healthy"}}.get(path, default)
        runners = {name: mock.Mock(return_value=([], None)) for name in ("codex", "opencode")}
        with mock.patch.object(tgbridge, "load_json", side_effect=load), \
             mock.patch.object(tgbridge, "ensure_private_storage"), \
             mock.patch.object(tgbridge, "proxy_diagnostics", return_value={}), \
             mock.patch.object(tgbridge, "polling_health", return_value={"ok": service_ok}), \
             mock.patch.object(tgbridge.os, "access", return_value=True), \
             mock.patch.object(tgbridge, "RUNNERS", runners), \
             mock.patch.object(tgbridge, "api", return_value={"ok": True, "result": {"username": "fixture_bot"}}):
            return tgbridge.doctor_report(), runners

    def test_reachable_api_does_not_hide_stopped_or_stale_bridge(self):
        report, _ = self.report(False)
        self.assertTrue(report["telegram"]["ok"])
        self.assertTrue(report["runner"]["ok"])
        self.assertFalse(report["ok"])

    def test_doctor_uses_persisted_runner_override(self):
        report, runners = self.report(True, {"runner_override": {"runner": "opencode", "model": "provider/model"}})
        self.assertTrue(report["ok"])
        self.assertEqual(report["runner"]["name"], "opencode")
        runners["codex"].assert_not_called()
        runners["opencode"].assert_called_once_with(None, "doctor", "provider/model")


if __name__ == "__main__":
    unittest.main()
