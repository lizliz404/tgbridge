"""Exercise public delivery over real native CLI/RPC fixture pipes."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

import tgbridge
from tgbridge_core import runners


FAKE_CODEX = r'''
import json, sys
def emit(value):
    print(json.dumps(value), flush=True)
def item(method, value):
    emit({'method': method, 'params': {'turnId': 'turn1', 'item': value}})
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize':
        emit({'id': request['id'], 'result': {}})
    elif method in ('thread/start', 'thread/resume'):
        emit({'id': request['id'], 'result': {'thread': {'id': 'thread1'}}})
    elif method == 'turn/start':
        emit({'id': request['id'], 'result': {'turn': {'id': 'turn1'}}})
        item('item/completed', {'id': 'private', 'type': 'reasoning', 'content': ['PRIVATE_SENTINEL']})
        item('item/completed', {'id': 'one', 'type': 'agentMessage', 'phase': 'commentary', 'text': 'checking files'})
        item('item/started', {'id': 'bash1', 'type': 'commandExecution', 'command': 'python3 check.py', 'cwd': '/fixture'})
        item('item/completed', {'id': 'bash1', 'type': 'commandExecution', 'command': 'python3 check.py', 'exitCode': 0, 'aggregatedOutput': 'OK'})
        item('item/completed', {'id': 'file1', 'type': 'fileChange', 'status': 'completed', 'changes': [{'path': '/fixture/a.py', 'diff': '-old\n+new', 'kind': {'type': 'update'}}]})
        value = {'id': 'two', 'type': 'agentMessage', 'phase': 'final_answer', 'text': 'finished'}
        item('item/completed', value)
        item('item/completed', value)
        emit({'method': 'turn/completed', 'params': {'turn': {'id': 'turn1'}}})
'''


class TransportProgressTests(unittest.TestCase):
    def setUp(self):
        self.texts = []
        self.actions = {}
        self.next_id = 100
        self.records = []
        self.status_updates = []
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(tgbridge, 'audit', side_effect=lambda event, **kw: self.records.append(kw) if event == 'tool_action' else None).start()
        mock.patch.object(tgbridge, 'send_retry', side_effect=lambda cfg, chat, text, **kw: self.texts.append(text) or True).start()
        def api(token, method, **params):
            if method == 'sendMessage':
                self.next_id += 1
                self.actions[self.next_id] = params['text']
                return {'ok': True, 'result': {'message_id': self.next_id}}
            if method == 'editMessageText':
                self.status_updates.append(params)
            return {'ok': True}
        mock.patch.object(tgbridge, 'api', side_effect=api).start()
        with tgbridge.RUN_LOCK:
            tgbridge.RUN_STATE.update(busy=True, current={'chat': 42}, cancel=False, proc=None)
        self.addCleanup(self.reset)

    def reset(self):
        with tgbridge.RUN_LOCK:
            tgbridge.RUN_STATE.update(busy=False, current=None, cancel=False, proc=None)

    def live(self):
        return {'chat_id': 42, 'reply_to': 99, 'status_id': 5, 'trail': [], 'start': time.time()}

    def binary(self, root, source):
        path = Path(root) / 'runner'
        path.write_text('#!' + sys.executable + '\n' + source)
        path.chmod(0o700)
        return str(path)

    def test_codex_rpc_keeps_actions_and_commentary_as_separate_messages(self):
        live = self.live()
        with tempfile.TemporaryDirectory() as root:
            binary = self.binary(root, FAKE_CODEX)
            with mock.patch.object(tgbridge, '_bin', return_value=binary):
                sid, answer, err = tgbridge.run_codex_app_server({'runner': 'codex', 'bot_token': '123456789:fixture-test-token', 'workdir': root}, None, 'go', live)
        self.assertIsNone(err)
        self.assertEqual(sid, 'thread1')
        self.assertEqual(self.texts, ['checking files', 'finished'])
        self.assertEqual(len(self.actions), 0)
        self.assertTrue(all(p['message_id'] == 5 for p in self.status_updates))
        action_text = '\n'.join(p['body'] for p in self.records)
        self.assertIn('python3 check.py', action_text)
        self.assertIn('output:\nOK', action_text)
        self.assertIn('/fixture/a.py', action_text)
        self.assertIn('+new', action_text)
        self.assertNotIn('PRIVATE_SENTINEL', action_text + ''.join(self.texts))
        self.assertIsNone(tgbridge.deliverable_answer(live, answer))

    def test_opencode_and_pi_cli_use_the_same_delivery_ledger(self):
        cases = {
            'opencode': [
                {'type': 'text', 'part': {'id': 'one', 'text': 'checking files'}},
                {'type': 'tool_use', 'part': {'id': 'bash1', 'tool': 'bash', 'state': {'status': 'completed', 'input': {'command': 'python3 check.py'}, 'output': 'OK'}}},
                {'type': 'text', 'part': {'id': 'two', 'text': 'finished'}},
            ],
            'pi': [
                {'type': 'message_end', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'checking files'}]}},
                {'type': 'tool_execution_start', 'toolCallId': 'bash1', 'toolName': 'bash', 'args': {'command': 'python3 check.py'}},
                {'type': 'tool_execution_end', 'toolCallId': 'bash1', 'toolName': 'bash', 'result': {'content': [{'type': 'text', 'text': 'OK'}]}},
                {'type': 'message_end', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'finished'}]}},
            ],
        }
        for runner, records in cases.items():
            with self.subTest(runner=runner), tempfile.TemporaryDirectory() as root:
                self.texts.clear()
                self.actions.clear()
                self.records.clear()
                binary = self.binary(root, 'import json\nfor record in ' + repr(records) + ':\n    print(json.dumps(record), flush=True)\n')
                env = 'PI_BIN' if runner == 'pi' else 'OPENCODE_BIN'
                live = self.live()
                with mock.patch.dict(os.environ, {env: binary}), mock.patch.object(runners, 'OPENCODE', binary):
                    _, answer, err = tgbridge.run_agent({'runner': runner, 'bot_token': '123456789:fixture-test-token', 'workdir': root}, None, 'go', live)
                self.assertIsNone(err)
                self.assertEqual(self.texts, ['checking files', 'finished'])
                self.assertEqual(len(self.actions), 0)
                recorded = '\n'.join(p['body'] for p in self.records)
                self.assertIn('python3 check.py', recorded)
                self.assertIn('OK', recorded)
                self.assertIsNone(tgbridge.deliverable_answer(live, answer))


if __name__ == '__main__':
    unittest.main()
