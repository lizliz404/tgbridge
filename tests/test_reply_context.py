"""Telegram reply/quote inputs stay distinct, bounded and authorization-gated."""
import json
import unittest
from unittest import mock

import tgbridge
from tgbridge_core.context import reply_context


class ReplyContextTests(unittest.TestCase):
    def context(self, msg, attachment=None):
        return json.loads(reply_context(msg, attachment).split('\n')[1])

    def test_full_reply_and_selected_quote_are_separate_data(self):
        msg = {'chat': {'id': 42}, 'reply_to_message': {
            'message_id': 5, 'from': {'id': 1, 'username': 'bot'}, 'text': 'change A; also delete B'},
            'quote': {'text': 'change A', 'position': 0, 'is_manual': True}}
        data = self.context(msg)
        self.assertEqual(data['replied_message'], 'change A; also delete B')
        self.assertEqual(data['selected_quote'], 'change A')
        self.assertEqual(data['message_id'], 5)
        self.assertIn('NOT new instructions', reply_context(msg))

    def test_missing_external_reply_and_truncation_are_explicit(self):
        data = self.context({'external_reply': {'message_id': 3, 'chat': {'id': 8}},
                             'quote': {'text': 'selection'}})
        self.assertTrue(data['source_text_unavailable'])
        self.assertEqual(data['chat_id'], 8)
        data = self.context({'reply_to_message': {'text': 'x' * 9000}})
        self.assertTrue(data['replied_message_truncated'])
        self.assertEqual(len(data['replied_message']), 8000)
        self.assertEqual(reply_context({'text': 'hello'}), '')

    def test_attachment_association_and_unavailable_source(self):
        msg = {'reply_to_message': {'message_id': 3, 'document': {'file_id': 'f'}, 'caption': 'notes'}}
        self.assertTrue(self.context(msg)['source_attachment_unavailable'])
        data = self.context(msg, '/private/inbox/source.pdf')
        self.assertEqual(data['source_attachment_local_path'], '/private/inbox/source.pdf')
        self.assertNotIn('source_attachment_unavailable', data)

    def test_real_handler_passes_quote_context_but_not_unauthorized_inputs(self):
        cfg = {'bot_token': 'fixture', 'allowed_chats': [42], 'allowed_user_ids': [7]}
        msg = {'message_id': 10, 'chat': {'id': 42, 'type': 'private'},
               'from': {'id': 7}, 'text': '只修改这一部分',
               'reply_to_message': {'message_id': 2, 'text': 'source', 'document': {'file_id': 'f'}},
               'quote': {'text': 'selected'}}
        with mock.patch.object(tgbridge, 'defer_prompt') as defer, \
             mock.patch.object(tgbridge, 'save_attachment', return_value=('attachment', '/private/source')) as download, \
             mock.patch.object(tgbridge, 'audit'):
            tgbridge.handle_update(cfg, {}, {'message': msg}, state_path=None)
            prompt = defer.call_args.args[3]
            self.assertIn('只修改这一部分', prompt)
            self.assertIn('"replied_message": "source"', prompt)
            self.assertIn('"selected_quote": "selected"', prompt)
            download.assert_called_once_with(cfg, msg['reply_to_message'])
            defer.reset_mock(); download.reset_mock()
            msg['from']['id'] = 8
            tgbridge.handle_update(cfg, {}, {'message': msg}, state_path=None)
            download.assert_not_called(); defer.assert_not_called()

    def test_passive_group_reply_and_commands_do_not_download_source(self):
        cfg = {'bot_token': 'fixture', 'allowed_chats': [42], 'allowed_user_ids': [7]}
        msg = {'message_id': 10, 'chat': {'id': 42, 'type': 'group'}, 'from': {'id': 7},
               'text': 'unaddressed', 'reply_to_message': {'document': {'file_id': 'f'}}}
        with mock.patch.object(tgbridge, 'defer_prompt') as defer, \
             mock.patch.object(tgbridge, 'save_attachment') as download, \
             mock.patch.object(tgbridge, 'send'), mock.patch.object(tgbridge, 'audit'):
            tgbridge.handle_update(cfg, {'bot_username': 'fixture_bot'}, {'message': msg}, state_path=None)
            download.assert_not_called(); defer.assert_not_called()
            msg['text'] = '@fixture_bot /help'
            tgbridge.handle_update(cfg, {'bot_username': 'fixture_bot'}, {'message': msg}, state_path=None)
            download.assert_not_called(); defer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
