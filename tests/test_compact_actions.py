"""Message count and disclosure bounds, independent of number of tools."""
import html
import json
import re
import time
import unittest
from unittest import mock

import tgbridge
from tgbridge_core.progress import Journal, activity_body, codex_event


class QuietProgressTests(unittest.TestCase):
    def test_three_hundred_actions_create_no_tool_messages_and_keep_full_local_results(self):
        sent, records, status = [], [], []
        live = {}
        journal = Journal(live, lambda text, first: sent.append(text) or True,
                          lambda identity, body: records.append((identity, body)),
                          update_status=lambda: status.append(live['activity']))
        for i in range(300):
            event = {'kind': 'action', 'id': str(i), 'label': 'bash',
                     'state': 'completed', 'inputs': {'command': 'x' * 200},
                     'output': 'COMPLETE_LOCAL_RESULT_' + str(i)}
            journal.emit(event)
            journal.emit(event)
        self.assertEqual(sent, [])
        self.assertEqual(len(records), 300)
        self.assertEqual(len(journal.actions), 300)
        self.assertIn('COMPLETE_LOCAL_RESULT_299', records[-1][1])
        for text in ('I am checking the result.', 'Finished.'):
            journal.emit({'kind': 'text', 'id': text, 'text': text})
        self.assertEqual(sent, ['I am checking the result.', 'Finished.'])
        self.assertIsNone(journal.remaining('\n\n'.join(sent)))

    def test_preview_is_128_characters_redacted_before_cut_and_escaped(self):
        body = activity_body({'label': 'bash', 'inputs': {
            'command': 'API_KEY=SECRET <&😀 ' + '中' * 200}, 'output': 'PRIVATE_OUTPUT'})
        preview = html.unescape(re.search(r'<blockquote>(.*)</blockquote>', body)[1])
        self.assertEqual(len(preview), 128)
        self.assertTrue(preview.endswith('…'))
        self.assertNotIn('SECRET', body)
        self.assertNotIn('PRIVATE_OUTPUT', body)
        self.assertIn('&lt;&amp;', body)
        self.assertNotIn('动作 ', body)
        self.assertNotIn('已完成', body)

    def test_reasoning_has_only_activity_indicator_and_italic_public_text_stays_public(self):
        sent, records, live = [], [], {}
        journal = Journal(live, lambda text, first: sent.append(text) or True,
                          lambda *args: records.append(args))
        journal.emit(codex_event({'method': 'item/started', 'params': {'item': {
            'type': 'reasoning', 'summary': ['PRIVATE_THOUGHT']}}}))
        self.assertEqual(activity_body(live['activity']), '☁️ thinking')
        self.assertEqual(records, [])
        journal.emit({'kind': 'text', 'id': 'public', 'text': '*A public explanation.*'})
        self.assertEqual(sent, ['*A public explanation.*'])
        self.assertNotIn('PRIVATE_THOUGHT', json.dumps(live))

    def test_same_status_message_is_edited_at_most_every_eight_seconds(self):
        live = {'chat_id': 42, 'status_id': 5, 'start': time.time(), 'trail': []}
        calls, records = [], []
        cfg = {'bot_token': 'fixture'}
        with mock.patch.object(tgbridge, 'audit', side_effect=lambda event, **kw: records.append(kw)), \
             mock.patch.object(tgbridge, 'api', side_effect=lambda token, method, **kw:
                               calls.append((method, kw)) or {'ok': True}), \
             mock.patch('tgbridge.time.monotonic', return_value=100):
            for i in range(300):
                tgbridge.publish_progress(cfg, live, {'kind': 'action', 'id': str(i), 'label': 'bash',
                                                     'inputs': {'command': 'pwd'}, 'state': 'running'})
            tgbridge.publish_progress(cfg, live, {'kind': 'activity', 'label': 'todo', 'preview': 'update'})
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], 'editMessageText')
            self.assertEqual(calls[0][1]['message_id'], 5)
            with mock.patch('tgbridge.time.monotonic', return_value=108):
                tgbridge.edit_status(cfg, live)
            self.assertEqual(len(calls), 2)
            tgbridge.edit_status(cfg, live, final='✅ done')
            self.assertEqual(len(calls), 3)
        self.assertEqual(len(records), 300)

    def test_nonzero_command_exit_is_visible_as_failure(self):
        event = codex_event({'method': 'item/completed', 'params': {'item': {
            'id': 'a', 'type': 'commandExecution', 'status': 'completed', 'exitCode': 2}}})
        self.assertEqual(event['state'], 'failed')
        self.assertIn('failed', activity_body(event))
