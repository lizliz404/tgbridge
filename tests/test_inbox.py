"""Fault boundaries: accepted input survives, uncertain execution never replays."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import tgbridge
from tgbridge_core.inbox import Inbox, InboxError


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'inputs.json')
        self.inbox = Inbox(self.path)

    def receive(self, identity='tg:42:5'):
        return self.inbox.receive(identity, 42, 5, 'change it', 7, 'private')

    def test_received_and_queued_recover_but_executing_and_steering_do_not_replay(self):
        for state in ('received', 'queued', 'executing', 'steering', 'completed', 'cancelled'):
            identity = state
            self.receive(identity)
            self.inbox.transition([identity], state)
        ready, interrupted = Inbox(self.path).recover()
        self.assertEqual([e['input_ids'][0] for e in ready], ['received', 'queued'])
        self.assertEqual([e['id'] for e in interrupted], ['executing', 'steering'])
        self.assertEqual(self.inbox.path, self.path)
        self.assertEqual(Path(self.path).stat().st_mode & 0o777, 0o600)

    def test_repeated_telegram_update_is_not_dispatched_twice(self):
        cfg = {'bot_token': 'fixture', 'allowed_chats': [42], 'allowed_user_ids': [7], '_inbox': self.inbox}
        msg = {'chat': {'id': 42, 'type': 'private'}, 'message_id': 5, 'from': {'id': 7}, 'text': 'change it'}
        with mock.patch.object(tgbridge, 'defer_prompt') as defer, mock.patch.object(tgbridge, 'audit'):
            tgbridge.handle_update(cfg, {}, {'message': msg}, state_path=None)
            tgbridge.handle_update(cfg, {}, {'message': msg}, state_path=None)
        defer.assert_called_once()
        self.assertEqual(defer.call_args.kwargs['input_ids'], ['tg:42:5'])
        self.assertEqual(Inbox(self.path).data['tg:42:5']['status'], 'received')

    def test_failed_write_raises_and_does_not_create_phantom_receipt(self):
        with mock.patch('tgbridge_core.inbox.save_json', side_effect=OSError('disk full')):
            with self.assertRaises(InboxError):
                self.receive()
        self.assertEqual(self.inbox.data, {})
        self.assertTrue(self.receive())
        self.assertFalse(self.receive())

    def test_failed_durable_acceptance_stops_poll_before_offset_commit(self):
        state = {'offset': 7}
        def api(token, method, **params):
            if method == 'getMe':
                return {'ok': True, 'result': {'username': 'fixture_bot'}}
            if method == 'getUpdates':
                return {'ok': True, 'result': [{'update_id': 20, 'message': {}}]}
            return {'ok': True}
        with mock.patch.object(tgbridge, 'api', side_effect=api), \
             mock.patch.object(tgbridge, 'load_json', return_value=state), \
             mock.patch.object(tgbridge, 'save_json'), \
             mock.patch.object(tgbridge, 'update_health'), \
             mock.patch.object(tgbridge, 'systemd_notify'), \
             mock.patch.object(tgbridge, 'probe_runners', return_value={}), \
             mock.patch.object(tgbridge, 'audit'), \
             mock.patch.object(tgbridge, 'rearm_at'), \
             mock.patch.object(tgbridge, 'home_chat', return_value=None), \
             mock.patch.object(tgbridge.threading, 'Thread'), \
             mock.patch.object(tgbridge, 'handle_update', side_effect=InboxError('disk full')):
            with self.assertRaises(InboxError):
                tgbridge.run({'bot_token': 'fixture', 'allowed_chats': [], '_inbox': self.inbox})
        self.assertEqual(state['offset'], 7)

    def test_corrupt_journal_fails_closed_not_silently_emptied(self):
        Path(self.path).write_text('not json')
        with self.assertRaises(InboxError):
            Inbox(self.path)

    def test_explicit_continuation_is_chat_scoped_reserved_and_guarded(self):
        self.receive(); self.inbox.transition(['tg:42:5'], 'executing')
        self.inbox.recover()
        self.assertIsNone(self.inbox.continuation('tg:42:5', 43))
        entry = self.inbox.continuation('tg:42:5', 42)
        self.assertIn('Do NOT blindly repeat', entry['prompt'])
        self.assertIsNone(self.inbox.continuation('tg:42:5', 42))
        self.assertEqual(Inbox(self.path).recover()[0][0]['input_ids'], ['tg:42:5'])

    def test_crash_after_execution_before_send_preserves_result_without_replay(self):
        self.receive(); self.inbox.transition(['tg:42:5'], 'executing')
        self.inbox.stage_result(['tg:42:5'], 'actual result')
        ready, interrupted = Inbox(self.path).recover()
        self.assertEqual(ready, []); self.assertEqual(interrupted, [])
        self.assertEqual(Inbox(self.path).result('tg:42:5', 42), 'actual result')
        self.assertEqual(Inbox(self.path).pending(42)[0]['status'], 'result_ready')

    def test_unconfirmed_result_is_preserved_without_replaying_execution(self):
        self.receive(); self.inbox.transition(['tg:42:5'], 'executing')
        self.inbox.settle(['tg:42:5'], 'completed', 'actual result', False)
        ready, interrupted = Inbox(self.path).recover()
        self.assertEqual(ready, []); self.assertEqual(interrupted, [])
        self.assertEqual(self.inbox.result('tg:42:5', 42), 'actual result')
        self.assertIsNone(self.inbox.result('tg:42:5', 43))
        self.assertEqual(self.inbox.pending(42)[0]['status'], 'result_unconfirmed')

    def test_pi_ack_alone_is_not_consumption_and_rejected_steer_stays_queued(self):
        cfg = {'bot_token': 'fixture', '_inbox': self.inbox}
        self.receive(); self.inbox.transition(['tg:42:5'], 'steering')
        meta = {'sid': 's', 'text': 'change it', 'chat_id': 42, 'message_id': 5, 'input_ids': ['tg:42:5']}
        with mock.patch.object(tgbridge, 'audit'), mock.patch.object(tgbridge, 'send'), \
             mock.patch.object(tgbridge, 'PROMPT_Q') as q:
            with tgbridge.RUN_LOCK:
                tgbridge.RUN_STATE.update(pi_run_id=1, pi_steers={'steer-1-1': meta}, steer_pending=1)
            tgbridge._pi_handle_steer_response(cfg, {'id': 'steer-1-1', 'success': True, 'data': {'disposition': 'queued'}}, 1)
            self.assertEqual(self.inbox.data['tg:42:5']['status'], 'steering')
            tgbridge._steer_fallback(cfg, meta, 'closed before consumption')
            self.assertEqual(self.inbox.data['tg:42:5']['status'], 'queued')
            self.assertEqual(q.put.call_args.args[0]['input_ids'], ['tg:42:5'])
        with tgbridge.RUN_LOCK:
            tgbridge.RUN_STATE.update(pi_steers={}, steer_pending=0)


if __name__ == '__main__':
    unittest.main()
