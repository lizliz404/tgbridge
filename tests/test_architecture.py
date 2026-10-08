"""Bounded modules and a single runtime owner are regression contracts."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import tgbridge
from tgbridge_core import telegram_io
from tgbridge_core.commands import command_menu
from tgbridge_core.copy import NOTICES, notice

ROOT = Path(tgbridge.__file__).parent
MODULES = ('telegram_io', 'execution', 'ingress', 'opencode_transport',
           'codex_transport', 'pi_transport')


class ArchitectureTests(unittest.TestCase):
    def test_source_files_do_not_exceed_1500_lines(self):
        files = [ROOT / 'tgbridge.py', *ROOT.glob('tgbridge_core/*.py'), *ROOT.glob('tests/*.py')]
        for path in files:
            with self.subTest(path=path.name):
                self.assertLessEqual(len(path.read_text().splitlines()), 1500)

    def test_implementations_do_not_import_entrypoint_or_copy_runtime_globals(self):
        for name in MODULES:
            tree = ast.parse((ROOT / 'tgbridge_core' / (name + '.py')).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertNotIn('tgbridge', [a.name for a in node.names])
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, 'tgbridge')
                if isinstance(node, ast.Call):
                    self.assertFalse(isinstance(node.func, ast.Name) and node.func.id in ('globals', 'exec'))
            for node in tree.body:
                if isinstance(node, ast.FunctionDef):
                    self.assertEqual(node.args.args[0].arg, 'app')

    def test_implementation_can_use_an_independent_runtime(self):
        app = SimpleNamespace(CHUNK=3900, _wrap_markdown_tables=tgbridge._wrap_markdown_tables,
            split_chunks=tgbridge.split_chunks, md_to_html=tgbridge.md_to_html,
            _balanced=tgbridge._balanced, api=mock.Mock(return_value={'ok': True}))
        self.assertTrue(telegram_io.send(app, 'fixture', 42, '**hello**'))
        self.assertEqual(app.api.call_args.kwargs['text'], '<b>hello</b>')

    def test_owned_copy_is_english_and_short(self):
        for row in command_menu():
            self.assertTrue(row['description'].isascii())
            self.assertLessEqual(len(row['description']), 24)
        for state, variants in NOTICES.items():
            self.assertIn(notice(state), variants)
            self.assertTrue(all(len(v) <= 32 for v in variants))
        for path in [ROOT / 'tgbridge.py', *ROOT.glob('tgbridge_core/*.py')]:
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertFalse(any('\u4e00' <= c <= '\u9fff' for c in node.value),
                                     f'Chinese bridge copy in {path.name}:{node.lineno}')


if __name__ == '__main__':
    unittest.main()
