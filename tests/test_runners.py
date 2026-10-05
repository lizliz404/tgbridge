import unittest
from unittest import mock

from tgbridge_core.runners import (
    CODEX_YOLO_FLAG,
    RUNNERS,
    apply_runner_policy,
)


class RunnerAdapterTests(unittest.TestCase):
    def test_all_cli_adapters_are_registered(self):
        self.assertEqual(set(RUNNERS), {"opencode", "codex", "pi"})

    def test_codex_permission_policy_is_explicit_and_isolated(self):
        command = ["codex", "exec", "--json", "prompt"]
        yolo = apply_runner_policy("codex", command, {"codex_yolo": True})
        self.assertEqual(yolo[2], CODEX_YOLO_FLAG)
        self.assertEqual(apply_runner_policy("codex", command, {}), command)
        self.assertEqual(
            apply_runner_policy("pi", ["pi", "--mode", "json"], {"codex_yolo": True}),
            ["pi", "--mode", "json"],
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
    @mock.patch("tgbridge_core.runners.PI", "/bin/echo")
    def test_pi_and_codex_commands_remain_native(self, _binary):
        pi, parse = RUNNERS["pi"]("s1", "hello", "provider/model")
        codex, _ = RUNNERS["codex"]("s2", "hello", None)
        self.assertEqual(
            pi[1:],
            [
                "--mode",
                "json",
                "--session-id",
                "s1",
                "--model",
                "provider/model",
                "hello",
            ],
        )
        self.assertEqual(codex[1:4], ["exec", "--json", "resume"])

        accumulator = {
            "sid": None,
            "texts": [],
            "thinking": None,
            "cost": 0.0,
            "tokens": None,
        }
        parse({"type": "session", "id": "pi-session"}, accumulator)
        trail = parse(
            {
                "type": "tool_execution_start",
                "toolName": "bash",
                "args": {"command": "pwd"},
            },
            accumulator,
        )
        parse(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "partial"},
            },
            accumulator,
        )
        parse(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "final"}],
                    "stopReason": "stop",
                    "usage": {"totalTokens": 12, "cost": {"total": 0.1}},
                },
            },
            accumulator,
        )
        self.assertEqual(accumulator["sid"], "pi-session")
        self.assertEqual(accumulator["texts"], ["final"])
        self.assertEqual(accumulator["tokens"], 12)
        self.assertEqual(accumulator["cost"], 0.1)
        self.assertEqual(trail, "🔧 bash: pwd")


if __name__ == "__main__":
    unittest.main()
