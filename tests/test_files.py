"""Pull-model file delivery: named paths, deliverable scan, /files /send."""
import os
import tempfile
import time
import unittest
from unittest import mock

import tgbridge


class NamedPathsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.report = os.path.join(self.dir, "report.pdf")
        with open(self.report, "w") as f:
            f.write("pdf-bytes")

    def test_named_absolute_path_under_workdir_sends(self):
        text = f"Done — I saved it to {self.report} for you."
        self.assertEqual(tgbridge.named_paths(text, self.dir),
                         [os.path.realpath(self.report)])

    def test_outside_workdir_never_sends(self):
        other = os.path.join(tempfile.mkdtemp(), "secret.txt")
        with open(other, "w") as f:
            f.write("x")
        self.assertEqual(tgbridge.named_paths(f"see {other}", self.dir), [])

    def test_dotfiles_and_missing_paths_filtered(self):
        hidden = os.path.join(self.dir, ".env")
        with open(hidden, "w") as f:
            f.write("k=v")
        self.assertEqual(
            tgbridge.named_paths(f"{hidden} and /nonexistent/f.pdf", self.dir),
            [])

    def test_symlink_escape_is_confined(self):
        outside = os.path.join(tempfile.mkdtemp(), "real.txt")
        with open(outside, "w") as f:
            f.write("x")
        link = os.path.join(self.dir, "link.txt")
        os.symlink(outside, link)
        self.assertEqual(tgbridge.named_paths(link, self.dir), [])

    def test_trailing_punctuation_stripped(self):
        self.assertEqual(tgbridge.named_paths(f"file: {self.report}.", self.dir),
                         [os.path.realpath(self.report)])


class DeliverablesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _mk(self, rel, content=b"x"):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        return path

    def test_filters_extensions_dotfiles_and_skip_dirs(self):
        good = self._mk("out/report.md")
        self._mk("out/code.bin")
        self._mk(".hidden/secret.pdf")
        self._mk("node_modules/pkg/x.pdf")
        self._mk("src/test.py")  # .py is sendable — keep it
        paths = tgbridge.deliverables(self.dir)
        self.assertIn(good, paths)
        self.assertEqual(len([p for p in paths if "node_modules" in p]), 0)
        self.assertEqual(len([p for p in paths if "/.hidden/" in p]), 0)
        self.assertNotIn(os.path.join(self.dir, "out/code.bin"), paths)

    def test_mtime_descending_order(self):
        old = self._mk("a.pdf")
        new = self._mk("b.pdf")
        os.utime(old, (time.time() - 100, time.time() - 100))
        self.assertEqual(tgbridge.deliverables(self.dir)[0], new)

    def test_oversized_files_skipped(self):
        big = self._mk("big.pdf")
        with mock.patch("os.path.getsize", return_value=300 * 1024 * 1024):
            self.assertNotIn(big, tgbridge.deliverables(self.dir))


class ResolveSendNameTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.target = os.path.join(self.dir, "sub", "notes.txt")
        os.makedirs(os.path.dirname(self.target))
        with open(self.target, "w") as f:
            f.write("n")

    def test_basename_resolves_to_deliverable(self):
        self.assertEqual(tgbridge.resolve_send_name(self.dir, "notes.txt"),
                         self.target)

    def test_absolute_path_inside_workdir(self):
        self.assertEqual(tgbridge.resolve_send_name(self.dir, self.target),
                         self.target)

    def test_absolute_path_outside_workdir_rejected(self):
        other = os.path.join(tempfile.mkdtemp(), "x.txt")
        with open(other, "w") as f:
            f.write("x")
        self.assertIsNone(tgbridge.resolve_send_name(self.dir, other))

    def test_unknown_name_returns_none(self):
        self.assertIsNone(tgbridge.resolve_send_name(self.dir, "nope.txt"))
        self.assertIsNone(tgbridge.resolve_send_name(self.dir, ""))


class FileCommandTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = {"bot_token": "fixture", "allowed_chats": [42],
                    "allowed_user_ids": [7], "workdir": self.dir}

    def message(self, text):
        return {"message": {"message_id": 10,
                            "chat": {"id": 42, "type": "private"},
                            "from": {"id": 7}, "text": text}}

    def _mk(self, name):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write("data")
        return path

    def test_files_lists_deliverables(self):
        self._mk("report.pdf")
        with mock.patch.object(tgbridge, "send") as send:
            tgbridge.handle_update(self.cfg, {}, self.message("/files"),
                                   state_path=None)
        self.assertIn("report.pdf", send.call_args.args[2])

    def test_send_delivers_named_file(self):
        path = self._mk("report.pdf")
        with mock.patch.object(tgbridge, "send") as send, \
             mock.patch.object(tgbridge, "send_document",
                               return_value={"ok": True}) as doc:
            tgbridge.handle_update(self.cfg, {}, self.message("/send report.pdf"),
                                   state_path=None)
        self.assertEqual(doc.call_args.args[2], path)
        self.assertIn("sent 1", send.call_args.args[2])

    def test_send_unknown_name_reports(self):
        with mock.patch.object(tgbridge, "send") as send, \
             mock.patch.object(tgbridge, "send_document") as doc:
            tgbridge.handle_update(self.cfg, {}, self.message("/send nope.txt"),
                                   state_path=None)
        doc.assert_not_called()
        self.assertIn("no file matching", send.call_args.args[2])

    def test_send_all_delivers_pool(self):
        for name in ("a.pdf", "b.md"):
            self._mk(name)
        with mock.patch.object(tgbridge, "send") as send, \
             mock.patch.object(tgbridge, "send_document",
                               return_value={"ok": True}) as doc:
            tgbridge.handle_update(self.cfg, {}, self.message("/send all"),
                                   state_path=None)
        self.assertEqual(doc.call_count, 2)
        self.assertIn("sent 2", send.call_args.args[2])


class SessionCommandTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"bot_token": "fixture", "allowed_chats": [42],
                    "allowed_user_ids": [7]}

    def message(self, text):
        return {"message": {"message_id": 10,
                            "chat": {"id": 42, "type": "private"},
                            "from": {"id": 7}, "text": text}}

    def test_session_lists_bound_sessions_with_hints(self):
        state = {"runner_sessions": {"42": {"pi": "sess-1",
                                            "devin": "dev-9"}}}
        with mock.patch.object(tgbridge, "send") as send:
            tgbridge.handle_update(self.cfg, state, self.message("/session"),
                                   state_path=None)
        body = send.call_args.args[2]
        self.assertIn("sess-1", body)
        self.assertIn("pi --session-id sess-1", body)
        self.assertIn("devin --resume dev-9", body)

    def test_session_bind_stores_runner_session(self):
        state = {}
        with mock.patch.object(tgbridge, "send") as send:
            tgbridge.handle_update(self.cfg, state,
                                   self.message("/session codex thread-7"),
                                   state_path=None)
        self.assertEqual(
            state["runner_sessions"]["42"]["codex"], "thread-7")
        self.assertIn("bound codex session thread-7", send.call_args.args[2])

    def test_session_rejects_unknown_runner(self):
        with mock.patch.object(tgbridge, "send") as send:
            tgbridge.handle_update(self.cfg, {},
                                   self.message("/session bogus x"),
                                   state_path=None)
        self.assertIn("usage", send.call_args.args[2])

    def test_session_clear_drops_bindings(self):
        state = {"runner_sessions": {"42": {"pi": "s"}},
                 "sessions": {"42": "s"}, "session_runners": {"42": "pi"}}
        with mock.patch.object(tgbridge, "send") as send:
            tgbridge.handle_update(self.cfg, state,
                                   self.message("/session clear"),
                                   state_path=None)
        self.assertNotIn("pi", state["runner_sessions"].get("42", {}))
        self.assertIn("cleared", send.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
