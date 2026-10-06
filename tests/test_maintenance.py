"""Supervisor restart receipts: verify PID/version/poll and report failures."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tgbridge_core.maintenance import restart


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config.json').write_text(json.dumps({'bot_token': 'fixture', 'allowed_chats': [42]}))
        (self.root / 'state.json').write_text(json.dumps({'sessions': {'42': 's'}}))
        (self.root / 'health.json').write_text(json.dumps({'pid': 10}))
        self.before = {'MainPID': '10', 'ActiveState': 'active', 'ControlGroup': '/fixture'}

    def test_busy_service_is_not_restarted_and_failure_has_receipt_and_feedback(self):
        with mock.patch('tgbridge_core.maintenance.unit_status', return_value=self.before), \
             mock.patch('tgbridge_core.maintenance.owned_children', return_value={11}), \
             mock.patch('tgbridge_core.maintenance.subprocess.run') as command, \
             mock.patch('tgbridge_core.maintenance.notify_requester', return_value=True) as notify:
            receipt = restart(self.root, self.root, 'tgbridge.service', 42)
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['feedback'], 'confirmed')
        command.assert_not_called(); notify.assert_called_once()
        self.assertEqual(json.loads((self.root / 'maintenance/restart-result.json').read_text())['status'], 'failed')

    def test_new_pid_code_and_full_poll_interval_are_required(self):
        target = {'revision': 'a', 'source_sha256': 'b'}
        health = {'pid': 20, 'loaded_code': target, 'last_poll_ok_at': '2026-10-06T06:00:50+0000'}
        (self.root / 'health.json').write_text(json.dumps(health))
        after = {**self.before, 'MainPID': '20'}
        real_load = __import__('tgbridge_core.storage', fromlist=['load_json']).load_json
        def load(path, default):
            if str(path).endswith('health.json'):
                load.count += 1
                return {'pid': 10} if load.count == 1 else health
            return real_load(path, default)
        load.count = 0
        with mock.patch('tgbridge_core.maintenance.load_json', side_effect=load), \
             mock.patch('tgbridge_core.maintenance.source_identity', return_value=target), \
             mock.patch('tgbridge_core.maintenance.unit_status', side_effect=[self.before, self.before, after]), \
             mock.patch('tgbridge_core.maintenance.owned_children', return_value=set()), \
             mock.patch('tgbridge_core.maintenance.subprocess.run'), \
             mock.patch('tgbridge_core.maintenance.polling_health', return_value={'ok': True}), \
             mock.patch('tgbridge_core.maintenance.time.time', return_value=1791266400), \
             mock.patch('tgbridge_core.maintenance.notify_requester', return_value=True):
            receipt = restart(self.root, self.root, 'tgbridge.service', 42)
        self.assertEqual(receipt['status'], 'succeeded')
        self.assertEqual(receipt['new_pid'], 20)
        self.assertTrue(receipt['session_mappings_preserved'])

    def test_mismatched_health_writer_blocks_restart(self):
        (self.root / 'health.json').write_text(json.dumps({'pid': 999}))
        with mock.patch('tgbridge_core.maintenance.unit_status', return_value=self.before), \
             mock.patch('tgbridge_core.maintenance.subprocess.run') as command, \
             mock.patch('tgbridge_core.maintenance.notify_requester', return_value=True):
            receipt = restart(self.root, self.root, 'tgbridge.service', 42)
        self.assertEqual(receipt['status'], 'failed')
        self.assertIn('resolve ownership', receipt['error'])
        command.assert_not_called()

    def test_failed_supervisor_and_failed_notice_are_persisted(self):
        with mock.patch('tgbridge_core.maintenance.unit_status', return_value=self.before), \
             mock.patch('tgbridge_core.maintenance.owned_children', return_value=set()), \
             mock.patch('tgbridge_core.maintenance.subprocess.run', side_effect=RuntimeError('restart failed')), \
             mock.patch('tgbridge_core.maintenance.notify_requester', side_effect=TimeoutError):
            receipt = restart(self.root, self.root, 'tgbridge.service', 42)
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['feedback'], 'unconfirmed')
        self.assertEqual(receipt['feedback_error'], 'TimeoutError')

    def test_not_allowlisted_and_unrelated_units_cannot_be_restarted(self):
        for unit, chat in [('tgbridge.service', 43), ('unrelated.service', 42)]:
            with self.assertRaises(ValueError):
                restart(self.root, self.root, unit, chat)


if __name__ == '__main__':
    unittest.main()
