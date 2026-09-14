import unittest
from unittest import mock

from tgbridge_core.runners import (
    CODEX_YOLO_FLAG,
    RUNNERS,
    apply_runner_policy,
)


class RunnerAdapterTests(unittest.TestCase):
    def test_all_cli_adapters_are_registered(self):
        self.assertEqual(set(RUNNERS), {"opencode", "claude", "codex"})

    def test_codex_permission_policy_is_explicit_and_isolated(self):
        command = ["codex", "exec", "--json", "prompt"]
        yolo = apply_runner_policy("codex", command, {"codex_yolo": True})
        self.assertEqual(yolo[2], CODEX_YOLO_FLAG)
        self.assertEqual(apply_runner_policy("codex", command, {}), command)
        self.assertEqual(
            apply_runner_policy("claude", ["claude", "-p"], {"codex_yolo": True}),
            ["claude", "-p"],
        )

    @mock.patch("tgbridge_core.runners.OPENCODE", "/bin/echo")
    def test_opencode_command_and_parser_contract(self):
        command, parse = RUNNERS["opencode"](None, "hello", "provider/model")
        self.assertEqual(command[-3:], ["--model", "provider/model", "hello"])
        accumulator = {
            "sid": None,
            "texts": [],
            "thinking": None,
            "cost": 0.0,
            "tokens": None,
        }
        trail = parse(
            {
                "sessionID": "session-1",
                "type": "tool_use",
                "part": {"tool": "bash", "state": {"input": {"command": "pwd"}}},
            },
            accumulator,
        )
        self.assertEqual(accumulator["sid"], "session-1")
        self.assertEqual(trail, "🔧 bash: pwd")

    @mock.patch("tgbridge_core.runners._bin", return_value="/bin/echo")
    def test_claude_and_codex_commands_remain_native(self, _binary):
        claude, _ = RUNNERS["claude"]("s1", "hello", None)
        codex, _ = RUNNERS["codex"]("s2", "hello", None)
        self.assertIn("--resume", claude)
        self.assertEqual(codex[1:4], ["exec", "--json", "resume"])


if __name__ == "__main__":
    unittest.main()
