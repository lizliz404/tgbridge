"""Flat command discovery, legacy controls and keyboard-free plain replies."""
import ast
import json
from pathlib import Path
import unittest
from unittest import mock

import tgbridge
from tgbridge_core import ingress


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'bot_token': 'fixture', 'allowed_chats': [42, -42],
                    'allowed_user_ids': [7]}

    def message(self, text, group=False, user=7):
        return {'message': {'message_id': 10, 'chat': {
            'id': -42 if group else 42, 'type': 'supergroup' if group else 'private'},
            'from': {'id': user}, 'text': text}}

    def test_menu_and_help_share_complete_flat_catalog_and_handlers(self):
        menu = tgbridge.command_menu()
        names = [row['command'] for row in menu]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), {'status', 'cancel', 'new', 'pending',
            'resume', 'result', 'at', 'runners', 'runner', 'help'})
        body = tgbridge.command_help()
        self.assertEqual(len([line for line in body.split('\n\n')[0].splitlines() if line.startswith('/')]), len(menu))
        for row in menu:
            self.assertIn(row['description'], body)
            self.assertLessEqual(len(row['description']), 256)
        self.assertIn('only to this chat', body)
        self.assertIn('not old sessions', body)
        tree = ast.parse(Path(ingress.__file__).read_text())
        handler = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name == 'handle_update')
        handled = set()
        for node in ast.walk(handler):
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == 'cmd':
                handled.update(n.value.lstrip('/') for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith('/'))
        self.assertEqual(handled, set(names))

    def test_help_with_bot_suffix_is_handled_inline_and_threaded(self):
        with mock.patch.object(tgbridge, 'send') as send, \
             mock.patch.object(tgbridge, 'defer_prompt') as defer:
            tgbridge.handle_update(self.cfg, {'bot_username': 'fixture_bot'},
                self.message('/help@fixture_bot', group=True), state_path=None)
            self.assertIn('/status@fixture_bot', send.call_args.args[2])
            self.assertEqual(send.call_args.kwargs['reply_to'], 10)
            defer.assert_not_called()

    def test_other_bot_commands_are_ignored_even_in_replies(self):
        for group in (False, True):
            with self.subTest(group=group), mock.patch.object(tgbridge, 'send') as send, \
                 mock.patch.object(tgbridge, 'defer_prompt') as defer, \
                 mock.patch.object(tgbridge, 'merge_open_burst') as merge:
                update = self.message('/help@other_bot', group=group)
                update['message']['reply_to_message'] = {'from': {'username': 'fixture_bot'}}
                tgbridge.handle_update(self.cfg, {'bot_username': 'fixture_bot'},
                    update, state_path=None)
                send.assert_not_called(); defer.assert_not_called(); merge.assert_not_called()

    def test_unaddressed_group_commands_do_not_merge_into_agent_bursts(self):
        with mock.patch.object(tgbridge, 'merge_open_burst', return_value=True) as merge, \
             mock.patch.object(tgbridge, 'send') as send, \
             mock.patch.object(tgbridge, 'defer_prompt') as defer:
            tgbridge.handle_update(self.cfg, {'bot_username': 'fixture_bot'},
                self.message('/status', group=True), state_path=None)
            merge.assert_not_called(); send.assert_not_called(); defer.assert_not_called()

    def test_legacy_controls_never_reach_agent_or_change_state(self):
        for cmd in ('/restart', '/update', '/stop', '/model', '/dbs', '/review', '/learn', '/made_up'):
            with self.subTest(cmd=cmd), mock.patch.object(tgbridge, 'send') as send, \
                 mock.patch.object(tgbridge, 'defer_prompt') as defer, \
                 mock.patch.object(tgbridge, 'save_json') as save:
                state = {'sessions': {'42': 'keep'}}
                tgbridge.handle_update(self.cfg, state, self.message(cmd + ' extra'), state_path=None)
                self.assertIn('Unknown command', send.call_args.args[2])
                defer.assert_not_called(); save.assert_not_called()
                self.assertEqual(state, {'sessions': {'42': 'keep'}})

    def test_free_text_and_paths_still_reach_agent(self):
        for text in ('review this diff', 'use dbs for this', '/home/liz/dev/tgbridge', '/docs/SOP', 'restart 是什么？'):
            with self.subTest(text=text), mock.patch.object(tgbridge, 'defer_prompt') as defer, \
                 mock.patch.object(tgbridge, 'audit'):
                tgbridge.handle_update(self.cfg, {}, self.message(text), state_path=None)
                self.assertEqual(defer.call_args.args[3], text)

    def test_unauthorized_legacy_control_is_silent(self):
        with mock.patch.object(tgbridge, 'send') as send, \
             mock.patch.object(tgbridge, 'defer_prompt') as defer:
            tgbridge.handle_update(self.cfg, {}, self.message('/restart', user=8), state_path=None)
            send.assert_not_called(); defer.assert_not_called()

    def test_plain_replies_remove_persistent_keyboard_only_when_targetable(self):
        cases = ((42, None, {'remove_keyboard': True, 'selective': False}),
                 (-42, 10, {'remove_keyboard': True, 'selective': True}),
                 (-42, None, None))
        for chat, reply, want in cases:
            with self.subTest(chat=chat, reply=reply), \
                 mock.patch.object(tgbridge, 'api', return_value={'ok': True}) as api:
                self.assertTrue(tgbridge.send('fixture', chat, 'reply', reply_to=reply))
                markup = api.call_args.kwargs.get('reply_markup')
                self.assertEqual(json.loads(markup) if markup else None, want)

    def test_parse_fallback_preserves_keyboard_removal(self):
        with mock.patch.object(tgbridge, 'api', side_effect=[
                {'ok': False, 'error_code': 400, 'description': 'cannot parse entity'},
                {'ok': True}]) as api:
            self.assertTrue(tgbridge.send('fixture', 42, '**reply**'))
            self.assertTrue(json.loads(api.call_args.kwargs['reply_markup'])['remove_keyboard'])

    def test_inline_question_buttons_are_not_replaced(self):
        keyboard = [[{'text': 'Choose', 'callback_data': 'q:1'}]]
        with mock.patch.object(tgbridge, 'api', return_value={
                'ok': True, 'result': {'message_id': 99}}) as api:
            tgbridge.question_broker(self.cfg).send(42, 'Choose', keyboard)
            self.assertEqual(json.loads(api.call_args.kwargs['reply_markup']),
                             {'inline_keyboard': keyboard})


if __name__ == '__main__':
    unittest.main()
