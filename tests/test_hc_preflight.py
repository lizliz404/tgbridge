"""Offline ownership diagnostics: no bridge config, runner or bot calls."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import tgbridge
from tgbridge_core import maintenance


SCHEMA = {'methods': [{'name': name, 'params': []} for name in
                      ('terminal.run', 'terminal.attach', 'machine.exec')]}
STATUS = {'MainPID': '100', 'ActiveState': 'active', 'ControlGroup': '/fixture'}


class HcPreflightTests(unittest.TestCase):
    def fixture(self, root):
        tasks = root / '100' / 'task'
        for tid, children in (('100', ''), ('101', '201 201 202 203'), ('102', '201')):
            path = tasks / tid
            path.mkdir(parents=True)
            (path / 'children').write_text(children)
        for pid, parent, argv in (
                (201, 100, ['pi', '--mode', 'rpc', '--name', 'PRIVATE_PROMPT']),
                (202, 201, ['pi', '--mode', 'rpc']),
                (203, 100, ['bash', '-c', 'echo rpc'])):
            path = root / str(pid)
            path.mkdir()
            (path / 'cmdline').write_bytes(('\0'.join(argv) + '\0').encode())
            (path / 'status').write_text(f'Name:\tfixture\nPPid:\t{parent}\n')

    def check(self, terminals=None, facts=None, schema=None):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[:3] == ['systemctl', '--user', 'show']:
                output = '\n'.join(f'{k}={v}' for k, v in STATUS.items())
            elif argv == ['hc', 'schema']:
                output = json.dumps(schema or SCHEMA)
            elif argv == ['hc', 'ls', '--json']:
                output = json.dumps({'terminals': terminals or []})
            elif argv[:3] == ['hc', 'get', '--json']:
                output = json.dumps(facts)
            else:
                raise AssertionError(f'not a read-only command: {argv}')
            return subprocess.CompletedProcess(argv, 0, output, '')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            with mock.patch.object(maintenance.subprocess, 'run', side_effect=run):
                report = maintenance.hc_preflight(proc_root=root)
        return report, calls

    def test_worker_thread_rpc_child_not_main_thread_and_deduplicated(self):
        report, _ = self.check()
        self.assertEqual(report['supervisor']['main_pid'], 100)
        self.assertEqual(report['rpc_children'], [{'pid': 201, 'parent_pid': 100, 'mode': 'rpc'}])
        self.assertNotIn('PRIVATE_PROMPT', json.dumps(report))
        self.assertEqual(report['hc']['visibility'], 'not_listed_by_pid')
        self.assertEqual(report['hc']['native_pipe_rpc'], 'unsupported')
        self.assertFalse(report['migration_ready'])

    def test_terminal_visibility_is_pid_not_name_or_agent_status(self):
        report, calls = self.check(
            terminals=[{'address': 't1', 'agent': 'pi', 'status': 'unknown'},
                       {'address': 'remote/t1'}],
            facts={'pid': 300, 'foreground': {'pid': 201}, 'state': 'live',
                   'status': {'state': 'unknown'}})
        self.assertEqual(report['hc']['visibility'], 'pid_match')
        self.assertEqual(report['hc']['matches'][0]['pids'], [201])
        self.assertFalse(report['migration_ready'])
        index = calls.index(['hc', 'get', '--json', 't1'])
        self.assertEqual(calls[index - 1], ['hc', 'ls', '--json'])
        self.assertNotIn(['hc', 'get', '--json', 'remote/t1'], calls)

    def test_new_hc_api_requires_review_not_automatic_readiness(self):
        schema = {'methods': SCHEMA['methods'] + [{'name': 'terminal.adopt', 'params': []}]}
        report, _ = self.check(schema=schema)
        self.assertEqual(report['hc']['native_pipe_rpc'], 'needs_review')
        self.assertFalse(report['migration_ready'])

    def test_unavailable_commands_are_unknown_not_manageable(self):
        with mock.patch.object(maintenance.subprocess, 'run', side_effect=FileNotFoundError):
            report = maintenance.hc_preflight()
        self.assertEqual(report['supervisor']['kind'], 'unknown')
        self.assertEqual(report['hc']['visibility'], 'unknown')
        self.assertEqual(report['hc']['native_pipe_rpc'], 'unknown')
        self.assertFalse(report['migration_ready'])

    def test_cli_bypasses_production_storage_and_networked_doctor(self):
        report = {'migration_ready': False, 'read_only': True}
        with mock.patch.object(tgbridge.sys, 'argv', ['tgbridge.py', '--hc-preflight']), \
             mock.patch.object(maintenance, 'hc_preflight', return_value=report), \
             mock.patch.object(tgbridge, 'ensure_private_storage') as storage, \
             mock.patch.object(tgbridge, 'load_json') as load, \
             mock.patch.object(tgbridge, 'api') as api, \
             mock.patch.object(tgbridge, 'cli_doctor') as doctor, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as exit_:
                tgbridge.main()
        self.assertEqual(exit_.exception.code, 2)
        self.assertEqual(json.loads(output.getvalue()), report)
        for method in (storage, load, api, doctor):
            method.assert_not_called()


if __name__ == '__main__':
    unittest.main()
