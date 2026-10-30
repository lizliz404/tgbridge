import unittest
from unittest import mock

import tgbridge


class ServerFailoverTests(unittest.TestCase):
    def test_crash_after_tool_execution_does_not_replay_with_fallback(self):
        live = {'trail': []}
        cfg = {'runner': 'pi', 'runner_fallbacks': [{'runner': 'codex'}]}
        def failed(cfg, session_id, prompt, live):
            live['journal'] = mock.Mock(actions={'write-1': {'state': 'running'}})
            return 's', None, 'Pi RPC exited before agent_settled'
        with mock.patch.object(tgbridge, 'run_one', side_effect=failed) as run:
            result = tgbridge.run_with_fallbacks(cfg, None, 'mutate production', live)
        run.assert_called_once()
        self.assertEqual(result[2], 'Pi RPC exited before agent_settled')

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
            "runner_fallbacks": [{"runner": "opencode", "model": "opencode-go/muse"}],
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
        self.assertIn("answered via opencode", answer or "")
        self.assertIsNone(error)
        self.assertEqual(result_meta["runner"], "opencode")

    def test_dynamic_muse_failure_continues_to_discovered_glm(self):
        calls = []

        def run_one(cfg, session_id, prompt, live=None):
            model = cfg.get("model")
            calls.append((cfg["runner"], model))
            if model == "opencode-go/glm-5.4-flash":
                return "glm-session", "glm answer", None
            return session_id, None, "model unavailable"

        cfg = {
            "runner": "codex",
            "runner_fallbacks": [{"runner": "opencode", "model": "auto:opencode-go"}],
        }
        with (
            mock.patch.object(tgbridge, "run_one", side_effect=run_one),
            mock.patch(
                "tgbridge_core.runners.discover_opencode_go_models",
                return_value=[
                    "opencode-go/muse-spark-1.4-contributor",
                    "opencode-go/glm-5.4-flash",
                ],
            ),
        ):
            sid, answer, error = tgbridge.run_with_fallbacks(cfg, None, "prompt")

        self.assertEqual(
            calls,
            [
                ("codex", ""),
                ("opencode", "opencode-go/muse-spark-1.4-contributor"),
                ("opencode", "opencode-go/glm-5.4-flash"),
            ],
        )
        self.assertEqual(sid, "glm-session")
        self.assertIn("glm answer", answer or "")
        self.assertIsNone(error)

    def test_same_runner_model_failover_resumes_the_chat_session(self):
        calls = []

        def run_one(cfg, session_id, prompt, live=None):
            calls.append((cfg["runner"], cfg.get("model"), session_id))
            if cfg.get("model") == "magpie/auto/gpt-6-luna":
                return "pi-session", "answer via fallback model", None
            return session_id, None, "Pi error: 503: upstream unavailable"

        cfg = {
            "runner": "pi",
            "runner_fallbacks": [{"runner": "pi", "model": "magpie/auto/gpt-6-luna"}],
        }
        with mock.patch.object(tgbridge, "run_one", side_effect=run_one):
            sid, answer, error = tgbridge.run_with_fallbacks(
                cfg, "pi-session", "prompt"
            )

        # A dead model must not cost the chat its session context.
        self.assertEqual(
            calls,
            [
                ("pi", "", "pi-session"),
                ("pi", "magpie/auto/gpt-6-luna", "pi-session"),
            ],
        )
        self.assertEqual(sid, "pi-session")
        self.assertIn("answer via fallback model", answer or "")
        self.assertIn("answered via pi/magpie/auto/gpt-6-luna", answer or "")
        self.assertIsNone(error)

    def test_cross_runner_steps_still_start_fresh_sessions(self):
        calls = []

        def run_one(cfg, session_id, prompt, live=None):
            calls.append((cfg["runner"], session_id))
            return ("new-pi" if cfg["runner"] == "pi" else session_id), None, "down"

        cfg = {
            "runner": "pi",
            "runner_fallbacks": [
                {"runner": "codex"},
                {"runner": "pi", "model": "m/b"},
            ],
        }
        with mock.patch.object(tgbridge, "run_one", side_effect=run_one):
            tgbridge.run_with_fallbacks(cfg, "pi-session", "prompt")

        # codex never sees pi's session; the second pi step resumes the
        # session the first pi attempt actually created.
        self.assertEqual(
            calls,
            [("pi", "pi-session"), ("codex", None), ("pi", "new-pi")],
        )

    def test_cli_tool_trail_blocks_replay_on_failover(self):
        live = {"trail": []}

        def run_one(cfg, session_id, prompt, live=None):
            live["trail"].append("🔧 bash: rm -rf staging")
            return session_id, None, "opencode failed rc=1"

        cfg = {"runner": "opencode", "runner_fallbacks": [{"runner": "codex"}]}
        with mock.patch.object(tgbridge, "run_one", side_effect=run_one) as run:
            _, _, err = tgbridge.run_with_fallbacks(cfg, None, "mutate", live)

        run.assert_called_once()
        self.assertEqual(err, "opencode failed rc=1")

    def test_exhausted_message_names_each_runner_and_model(self):
        def run_one(cfg, session_id, prompt, live=None):
            return session_id, None, f"{cfg.get('model') or 'default'} is down"

        cfg = {
            "runner": "pi",
            "runner_fallbacks": [
                {"runner": "pi", "model": "m/a"},
                {"runner": "codex"},
            ],
        }
        with mock.patch.object(tgbridge, "run_one", side_effect=run_one):
            _, _, err = tgbridge.run_with_fallbacks(cfg, "s", "prompt")

        self.assertIn("all runners exhausted (pi -> pi/m/a -> codex)", err)
        self.assertIn("default is down", err)

    def test_provider_auth_failure_fails_over_instead_of_stopping(self):
        calls = []

        def run_one(cfg, session_id, prompt, live=None):
            calls.append((cfg["runner"], session_id))
            if cfg["runner"] == "opencode":
                return (
                    session_id,
                    None,
                    (
                        "OpenCode v2 error: Provider request failed with HTTP 401: "
                        "missing_api_key"
                    ),
                )
            return "codex-session", "fallback answer", None

        cfg = {
            "runner": "opencode",
            "runner_fallbacks": [{"runner": "codex"}],
        }
        with mock.patch.object(tgbridge, "run_one", side_effect=run_one):
            sid, answer, error = tgbridge.run_with_fallbacks(
                cfg, "open-session", "prompt"
            )

        self.assertEqual(calls, [("opencode", "open-session"), ("codex", None)])
        self.assertEqual(sid, "codex-session")
        self.assertIn("answered via codex", answer or "")
        self.assertIsNone(error)


class RunnerSessionTests(unittest.TestCase):
    def test_legacy_session_is_owned_by_file_primary(self):
        state = {"sessions": {"7": "codex-session"}}
        self.assertIsNone(
            tgbridge.runner_session(state, 7, "opencode", legacy_runner="codex")
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
        self.assertEqual(tgbridge.runner_session(state, 7, "opencode"), "open-session")
        tgbridge.clear_runner_sessions(state, 7)
        self.assertIsNone(tgbridge.runner_session(state, 7, "codex"))
        self.assertIsNone(tgbridge.runner_session(state, 7, "opencode"))


if __name__ == "__main__":
    unittest.main()
