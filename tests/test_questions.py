import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import tgbridge
from tgbridge_core.questions import Questions


class QuestionTests(unittest.TestCase):
    def setUp(self):
        self.sent, self.edits, self.acks, self.answers = [], [], [], []
        self.broker = Questions(lambda *a: self.sent.append(a) or 12,
                                lambda *a: self.edits.append(a),
                                lambda *a: self.acks.append(a))
        self.key = self.broker.offer(42, 7, 'Which?', ['A', 'B'], self.answers.append,
                                     owner=('pi', 1))

    def query(self, choice='1', **kw):
        return {'id': 'callback', 'data': f'q:{self.key}:{choice}',
                'from': {'id': kw.get('user', 7)},
                'message': {'message_id': kw.get('message', 12),
                            'chat': {'id': kw.get('chat', 42), 'type': 'private'}}}

    def test_no_default_and_duplicate_click_answers_once(self):
        self.assertEqual(self.answers, [])
        self.broker.callback(self.query())
        self.broker.callback(self.query())
        self.assertEqual(self.answers, ['B'])
        self.assertEqual(self.edits[-1][-1], [])

    def test_foreign_chat_user_or_message_cannot_answer(self):
        for kw in ({'chat': 43}, {'user': 8}, {'message': 13}):
            self.broker.callback(self.query(**kw))
        self.assertEqual(self.answers, [])
        self.assertIn(self.key, self.broker.pending)

    def test_other_then_text_and_unrelated_reply(self):
        self.broker.callback(self.query('text'))
        self.assertEqual(self.answers, [])
        self.assertFalse(self.broker.answer_text(42, 7, 'unrelated', 99))
        self.assertTrue(self.broker.answer_text(42, 7, 'my actual answer', 12))
        self.assertEqual(self.answers, ['my actual answer'])

    def test_numeric_answer_and_confirm_require_explicit_selection(self):
        self.broker.answer_text(42, 7, '2', 12)
        self.assertEqual(self.answers, ['B'])
        self.broker.offer(42, 7, 'Proceed?', ['是', '否'], self.answers.append,
                          owner=('pi', 1), method='confirm')
        self.broker.answer_text(42, 7, 'maybe', 12)
        self.assertEqual(self.answers, ['B'])
        self.broker.answer_text(42, 7, '否', 12)
        self.assertEqual(self.answers, ['B', '否'])

    def test_cancel_expire_and_interrupt_never_choose_default(self):
        self.broker.callback(self.query('cancel'))
        self.assertEqual(self.answers, [None])
        self.key = self.broker.offer(42, 7, 'Again?', ['A'], self.answers.append,
                                     owner=('pi', 2), timeout=1)
        self.broker.pending[self.key]['deadline'] = time.monotonic() - 1
        self.broker.callback(self.query('0'))
        self.assertEqual(self.answers, [None])
        self.assertFalse(self.broker.has_owner(('pi', 2)))
        self.broker.close_owner(('pi', 2))
        self.assertFalse(self.broker.pending)
        self.assertEqual(self.answers, [None])

    def test_failed_delivery_cancels(self):
        broker = Questions(lambda *a: None, lambda *a: None, lambda *a: None)
        with self.assertRaises(RuntimeError):
            broker.offer(42, 7, 'Which?', ['A'], self.answers.append, owner='run')
        self.assertEqual(self.answers, [None])
        self.assertFalse(broker.pending)

    def test_authorization_and_answers_are_not_injects(self):
        cfg = {'allowed_chats': [42], 'allowed_user_ids': [7], '_questions': self.broker}
        tgbridge.handle_update(cfg, {}, {'callback_query': self.query(user=8)}, state_path=None)
        self.assertEqual(self.answers, [])
        with mock.patch.object(tgbridge, 'defer_prompt') as defer:
            tgbridge.handle_update(cfg, {}, {'message': {
                'message_id': 20, 'chat': {'id': 42, 'type': 'private'}, 'from': {'id': 7},
                'text': '2', 'reply_to_message': {'message_id': 12}}}, state_path=None)
        defer.assert_not_called()
        self.assertEqual(self.answers, ['B'])


FAKE_PI = r'''
import json, sys
def emit(event): print(json.dumps(event), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    kind = request['type']
    if kind == 'get_state':
        emit({'type': 'response', 'id': request['id'], 'success': True,
              'data': {'sessionId': 'native-question'}})
    elif kind == 'prompt':
        emit({'type': 'response', 'id': request['id'], 'success': True, 'data': {}})
        emit({'type': 'extension_ui_request', 'method': 'select', 'id': 'ask-1',
              'title': 'Choose', 'options': ['A', 'B']})
    elif kind == 'extension_ui_response':
        assert request['id'] == 'ask-1'
        answer = 'ACTUAL_CHOICE=' + request.get('value', 'CANCELLED')
        emit({'type': 'message_end', 'message': {'role': 'assistant',
              'content': [{'type': 'text', 'text': answer}]}})
        emit({'type': 'agent_settled'})
    elif request.get('id'):
        emit({'type': 'response', 'id': request['id'], 'success': True, 'data': {}})
'''


class NativeQuestionTests(unittest.TestCase):
    def test_pi_question_returns_actual_telegram_selection_over_rpc(self):
        self.check_native('pi', FAKE_PI)

    def test_codex_question_returns_actual_telegram_selection_over_rpc(self):
        self.check_native('codex', FAKE_CODEX)

    def check_native(self, runner, source):
        frames, texts, failures = [], [], []
        def api(token, method, **params):
            frames.append((method, params))
            return {'ok': True, 'result': {'message_id': 12}}
        with tempfile.TemporaryDirectory() as root:
            binary = Path(root) / runner
            binary.write_text('#!' + sys.executable + '\n' + source)
            binary.chmod(0o700)
            cfg = {'runner': runner, 'bot_token': 'fixture', 'workdir': root,
                   'allowed_chats': [42], 'allowed_user_ids': [7], 'run_timeout_s': 1, 'run_max_s': 5}
            live = {'chat_id': 42, 'requester_user_id': 7, 'status_id': 5,
                    'trail': [], 'start': time.time()}
            with mock.patch.object(tgbridge, 'api', side_effect=api), \
                 mock.patch.object(tgbridge, 'audit'), \
                 mock.patch.object(tgbridge, 'send_retry', side_effect=lambda c, chat, text, **kw: texts.append(text) or True), \
                 mock.patch.dict(os.environ, {'PI_BIN': str(binary), 'CODEX_BIN': str(binary)}):
                cfg['_questions'] = tgbridge.question_broker(cfg)
                def answer():
                    try:
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline:
                            with cfg['_questions'].lock:
                                entries = list(cfg['_questions'].pending.items())
                            if entries and entries[0][1]['message_id']:
                                key, entry = entries[0]
                                tgbridge.handle_update(cfg, {}, {'callback_query': {
                                    'id': 'click', 'data': f'q:{key}:1', 'from': {'id': 7},
                                    'message': {'message_id': entry['message_id'],
                                                'chat': {'id': 42, 'type': 'private'}}}}, state_path=None)
                                return
                            time.sleep(.01)
                        failures.append('question not delivered')
                    except Exception as error:
                        failures.append(str(error))
                thread = threading.Thread(target=answer)
                thread.start()
                try:
                    run = tgbridge.run_pi_rpc if runner == 'pi' else tgbridge.run_codex_app_server
                    sid, result, error = run(cfg, None, 'ask', live)
                finally:
                    thread.join(4)
                    with tgbridge.RUN_LOCK:
                        tgbridge.RUN_STATE.update(proc=None, cancel=False, busy=False, current=None)
        self.assertEqual(failures, [])
        self.assertIsNone(error)
        self.assertEqual(sid, 'native-question')
        self.assertEqual(result, 'ACTUAL_CHOICE=B')
        self.assertEqual(texts, ['ACTUAL_CHOICE=B'])
        self.assertEqual(sum(method == 'sendMessage' for method, _ in frames), 1)
        self.assertTrue(any(method == 'answerCallbackQuery' for method, _ in frames))
        self.assertFalse(cfg['_questions'].pending)


FAKE_CODEX = r'''
import json, sys
def emit(event): print(json.dumps(event), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize': emit({'id': request['id'], 'result': {}})
    elif method in ('thread/start', 'thread/resume'):
        emit({'id': request['id'], 'result': {'thread': {'id': 'native-question'}}})
    elif method == 'turn/start':
        emit({'id': request['id'], 'result': {'turn': {'id': 'turn1'}}})
        emit({'method': 'item/tool/requestUserInput', 'id': 'ask-1', 'params': {
              'threadId': 'native-question', 'turnId': 'turn1', 'itemId': 'ask-item',
              'isBlocking': True, 'questions': [{'id': 'stable-q', 'header': 'Choice',
              'question': 'Choose', 'options': [{'label': 'A', 'description': 'first'},
                                               {'label': 'B', 'description': 'second'}]}]}})
    elif request.get('id') == 'ask-1':
        assert request['result']['answers'] == {'stable-q': {'answers': ['B']}}
        emit({'method': 'item/completed', 'params': {'turnId': 'turn1', 'item': {
             'id': 'answer', 'type': 'agentMessage', 'text': 'ACTUAL_CHOICE=B'}}})
        emit({'method': 'turn/completed', 'params': {'turn': {'id': 'turn1'}}})
'''
