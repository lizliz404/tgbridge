import unittest
from unittest import mock

import tgbridge


class ServerFailoverTests(unittest.TestCase):
    def test_runner_modes_override_the_legacy_global_mode(self):
        cfg = {
            "runner_mode": "server",
            "runner_modes": {"opencode": "cli"},
        }
        self.assertEqual(tgbridge.resolve_runner_mode(cfg, "codex"), "server")
        self.assertEqual(tgbridge.resolve_runner_mode(cfg, "opencode"), "cli")

    def test_server_primary_fails_over_to_fresh_server_session(self):
        calls = []

        def failed_codex(cfg, session_id, prompt, live):
            calls.append(
                (cfg["runner"], session_id, cfg.get("model"), cfg.get("run_timeout_s"))
            )
            return session_id, None, "weekly limit reached"

        def working_opencode(cfg, session_id, prompt, live):
            calls.append(
                (cfg["runner"], session_id, cfg.get("model"), cfg.get("run_timeout_s"))
            )
            return "open-session", "fallback answer", None

        adapters = {
            "codex": {"healthy": lambda cfg: True, "run": failed_codex},
            "opencode": {"healthy": lambda cfg: True, "run": working_opencode},
        }
        cfg = {
            "runner": "codex",
            "runner_mode": "server",
            "run_timeout_s": 1800,
            "fallback_run_timeout_s": 45,
            "runner_fallbacks": [
                {"runner": "opencode", "model": "opencode-go/muse"}
            ],
        }
        result_meta = {}
        with mock.patch.object(tgbridge, "SERVER_RUNNERS", adapters):
            sid, answer, error = tgbridge.run_with_fallbacks(
                cfg, "codex-session", "prompt", result_meta=result_meta
            )

        self.assertEqual(
            calls,
            [
                ("codex", "codex-session", "", 1800),
                ("opencode", None, "opencode-go/muse", 45),
            ],
        )
        self.assertEqual(sid, "open-session")
        self.assertIn("answered via opencode", answer)
        self.assertIsNone(error)
        self.assertEqual(result_meta["runner"], "opencode")


class RunnerSessionTests(unittest.TestCase):
    def test_legacy_session_is_owned_by_file_primary(self):
        state = {"sessions": {"7": "codex-session"}}
        self.assertIsNone(
            tgbridge.runner_session(
                state, 7, "opencode", legacy_runner="codex"
            )
        )
        self.assertEqual(
            tgbridge.runner_session(state, 7, "codex", legacy_runner="codex"),
            "codex-session",
        )

    def test_sessions_are_kept_per_runner_and_cleared_together(self):
        state = {}
        tgbridge.store_runner_session(state, 7, "codex", "codex-session")
        tgbridge.store_runner_session(state, 7, "opencode", "open-session")
        self.assertEqual(tgbridge.runner_session(state, 7, "codex"), "codex-session")
        self.assertEqual(
            tgbridge.runner_session(state, 7, "opencode"), "open-session"
        )
        tgbridge.clear_runner_sessions(state, 7)
        self.assertIsNone(tgbridge.runner_session(state, 7, "codex"))
        self.assertIsNone(tgbridge.runner_session(state, 7, "opencode"))


if __name__ == "__main__":
    unittest.main()
