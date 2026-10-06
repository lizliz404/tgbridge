"""Independent review reproductions: real fixture pipes, all outbound mocked."""
import json
import os
import threading
import time
import unittest
from unittest import mock

import tgbridge
import test_pi_rpc
from test_pi_rpc import FakePiFixture


def end(text, reason='stop'):
    return {'type': 'message_end', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}], 'stopReason': reason}}


class Crosscheck(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.status = []
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(tgbridge, 'audit', lambda *a, **k: None).start()
        mock.patch.object(tgbridge, 'send_retry', side_effect=self.send).start()
        mock.patch.object(tgbridge, 'api', side_effect=lambda token, method, **p: self.status.append((method, p)) or {'ok': True}).start()
        with tgbridge.RUN_LOCK:
            tgbridge.RUN_STATE.update(busy=True, current={'chat': 42}, proc=None, cancel=False,
                                     pi_sid=None, pi_steers={}, steer_pending=0)
        self.addCleanup(self.reset)

    def reset(self):
        with tgbridge.RUN_LOCK:
            tgbridge.RUN_STATE.update(busy=False, current=None, proc=None, pi_sid=None, pi_steers={}, steer_pending=0)

    def send(self, cfg, chat, text, reply_to=None):
        self.sent.append(text)
        return True

    def run_events(self, events, outcomes=None, crash=False):
        body = '\n'.join('        emit(' + repr(e) + ')' for e in events)
        if crash:
            body += '\n        os._exit(7)'
        else:
            body += '\n        emit({"type": "agent_settled"})'
        source = test_pi_rpc.FAKE_PI
        start = source.index('        emit({"type": "agent_start"}')
        stop = source.index('    elif kind == "steer":')
        source = source[:start] + body + '\n' + source[stop:]
        live = {'chat_id': 42, 'reply_to': 99, 'status_id': 5, 'trail': [], 'notes': [], 'start': time.time(), 'last_edit': 0, 'streamed': False}
        with mock.patch.object(test_pi_rpc, 'FAKE_PI', source), FakePiFixture(self) as fixture:
            if outcomes is not None:
                mock.patch.object(tgbridge, 'send_retry', side_effect=outcomes).start()
            result = tgbridge.run_pi_rpc({'bot_token': 'fixture', 'workdir': fixture.tmp.name}, 's1', 'go', live)
        return result, live

    def test_public_command_is_visible_but_private_thinking_and_keys_are_redacted(self):
        events = [{'type': 'message_update', 'assistantMessageEvent': {'type': 'thinking_delta', 'delta': 'PRIVATE_SENTINEL'}},
                  {'type': 'tool_execution_start', 'toolName': 'bash', 'args': {'command': 'API_KEY=SECRET_SENTINEL python3 build.py'}}, end('public')]
        _, live = self.run_events(events)
        # Exercise status again: its 8s throttle must not mask tool leakage.
        live['last_status_edit'] = -8
        tgbridge.edit_status({'bot_token': 'fixture'}, live)
        outbound = json.dumps(self.status) + json.dumps(self.sent)
        self.assertNotIn('PRIVATE_SENTINEL', outbound)
        self.assertNotIn('SECRET_SENTINEL', outbound)
        self.assertIn('python3 build.py', outbound)

    def test_first_failed_segment_is_not_hidden_by_later_success(self):
        (_, answer, err), live = self.run_events([end('lost'), end('sent')], [False, True])
        self.assertIsNone(err)
        self.assertIn('lost', tgbridge.deliverable_answer(live, answer) or '')

    def test_middle_failed_segment_is_not_hidden_by_later_success(self):
        (_, answer, err), live = self.run_events([end('sent1'), end('lost'), end('sent2')], [True, False, True])
        self.assertIsNone(err)
        self.assertIn('lost', tgbridge.deliverable_answer(live, answer) or '')

    def test_recovered_provider_retry_has_no_error_footer(self):
        (_, _, err), _ = self.run_events([end('', 'error'), {'type': 'auto_retry_start'}, end('recovered'), {'type': 'auto_retry_end', 'success': True}])
        self.assertIsNone(err)
        self.assertFalse(any('ended with an error' in s for s in self.sent))

    def test_child_crash_is_not_reported_as_clean_success(self):
        (_, _, err), _ = self.run_events([end('partial', 'toolUse')], crash=True)
        self.assertIsNotNone(err)

    def test_cancel_during_fallback_stops_chain(self):
        calls = []
        def run(cfg, sid, prompt, live=None):
            calls.append(cfg['runner'])
            return sid, None, 'broken' if len(calls) == 1 else tgbridge.CANCEL_MSG
        with mock.patch.object(tgbridge, 'run_one', side_effect=run):
            _, _, err = tgbridge.run_with_fallbacks({'runner': 'pi', 'runner_fallbacks': [{'runner': 'codex'}, {'runner': 'opencode'}]}, None, 'go')
        self.assertEqual(calls, ['pi', 'codex'])
        self.assertEqual(err, tgbridge.CANCEL_MSG)

    def test_dialog_before_prompt_ack_is_declined_without_deadlock(self):
        source = test_pi_rpc.FAKE_PI.replace('DELAY = float', 'UI_DONE = threading.Event()\nDELAY = float')
        source = source.replace('        respond(command["id"], "prompt", {"disposition": "started"})',
                                '        emit({"type": "extension_ui_request", "method": "confirm", "id": "ui-1"})\n        if not UI_DONE.wait(2):\n            os._exit(8)\n        respond(command["id"], "prompt", {"disposition": "started"})')
        source = source.replace('    elif kind == "steer":', '    elif kind == "extension_ui_response":\n        UI_DONE.set()\n    elif kind == "steer":')
        with mock.patch.object(test_pi_rpc, 'FAKE_PI', source), FakePiFixture(self) as fixture:
            _, answer, err = tgbridge.run_pi_rpc({'bot_token': 'fixture', 'workdir': fixture.tmp.name}, None, 'go')
            commands = fixture.commands()
        self.assertIsNone(err)
        self.assertTrue(any(c.get('type') == 'extension_ui_response' and c.get('cancelled') for c in commands))

    def test_acknowledged_but_unconsumed_steer_is_preserved(self):
        meta = {'sid': 's1', 'chat_id': 42, 'message_id': 1, 'text': 'do not lose me'}
        with tgbridge.RUN_LOCK:
            tgbridge.RUN_STATE.update(pi_run_id=7, pi_steers={'steer-7-1': meta}, steer_pending=1)
        tgbridge._pi_handle_steer_response({'bot_token': 'fixture'}, {'type': 'response', 'id': 'steer-7-1', 'success': True, 'data': {'disposition': 'queued'}}, 7)
        self.assertIn('steer-7-1', tgbridge.RUN_STATE['pi_steers'])

    def test_failed_segment_holds_later_segments_in_order(self):
        live = {'chat_id': 42, 'missing_segments': ['first']}
        with mock.patch.object(tgbridge, 'send_retry') as send:
            tgbridge.publish_progress({'bot_token': 'fixture'}, live, {'kind': 'text', 'id': 'second', 'text': 'second'})
        send.assert_not_called()
        self.assertEqual(live['missing_segments'], ['first', 'second'])

    def test_status_privacy_also_applies_to_fallback_transports(self):
        live = {'chat_id': 42, 'status_id': 5, 'trail': ['🔧 bash: API_KEY=SECRET_SENTINEL python3 build.py'],
                'thinking': 'PRIVATE_SENTINEL', 'start': time.time()}
        tgbridge.edit_status({'bot_token': 'fixture'}, live)
        self.assertNotIn('SENTINEL', json.dumps(self.status))
        self.assertIn('python3 build.py', json.dumps(self.status))

    def test_utf8_and_unicode_line_separators_preserve_jsonl_records(self):
        import io
        import queue
        events = queue.Queue()
        content = '中文😀\u2028next\u2029last'
        record = json.dumps(end(content), ensure_ascii=False) + '\r\n'
        tgbridge._pi_rpc_read_events(io.StringIO(record), events)
        self.assertEqual(tgbridge._pi_message_text(events.get()['message']), content)
        self.assertIsNone(events.get())

    def test_native_session_storage_is_not_redirected(self):
        with FakePiFixture(self) as fixture:
            real_popen = tgbridge.subprocess.Popen
            with mock.patch.object(tgbridge.subprocess, 'Popen', wraps=real_popen) as popen:
                tgbridge.run_pi_rpc({'bot_token': 'fixture', 'workdir': fixture.tmp.name}, 'native-id', 'go')
            command = popen.call_args.args[0]
        self.assertIn('--session-id', command)
        self.assertEqual(command[command.index('--session-id') + 1], 'native-id')
        self.assertNotIn('--session-dir', command)
        self.assertNotIn('--no-session', command)

    def test_telegram_origin_is_native_pi_session_name(self):
        with mock.patch.object(tgbridge.socket, 'gethostname', return_value='fixture-host'):
            name = tgbridge.telegram_session_name(42, 'fixture_bot')
        self.assertEqual(name, 'Telegram · fixture-host · @fixture_bot · chat 42')
        with FakePiFixture(self) as fixture:
            real_popen = tgbridge.subprocess.Popen
            with mock.patch.object(tgbridge.subprocess, 'Popen', wraps=real_popen) as popen:
                tgbridge.run_pi_rpc({'bot_token': 'fixture', 'workdir': fixture.tmp.name, 'session_name': name}, 'native-id', 'go')
            command = popen.call_args.args[0]
        self.assertEqual(command[command.index('--name') + 1], name)
        self.assertNotIn('--session-dir', command)
        self.assertNotIn('--no-session', command)

    def test_retry_is_chunk_local_and_never_replays_earlier_chunks(self):
        calls = []
        def api(token, method, _error=None, **params):
            calls.append(params['text'])
            if calls == ['aaaa', 'bbbb']:
                _error.update(code=503)
                return None
            return {'ok': True}
        with mock.patch.object(tgbridge, 'api', side_effect=api), mock.patch.object(tgbridge.time, 'sleep'):
            self.assertTrue(tgbridge.send('fixture', 42, 'aaaabbbb', chunk_limit=4))
        self.assertEqual(calls, ['aaaa', 'bbbb', 'bbbb'])

    def test_ambiguous_timeout_is_not_retried_as_plain_text(self):
        def timeout(token, method, _error=None, **params):
            _error.update(kind='timeout')
            return None
        with mock.patch.object(tgbridge, 'api', side_effect=timeout) as api:
            self.assertFalse(tgbridge.send('fixture', 42, 'public'))
        self.assertEqual(api.call_count, 1)

    def test_undelivered_segments_in_same_second_do_not_overwrite(self):
        import tempfile
        from pathlib import Path
        # Stop the fixture sender patch so the real persistence path is tested.
        mock.patch.stopall()
        with tempfile.TemporaryDirectory() as root, mock.patch.object(tgbridge, 'CONFIG_DIR', root), \
             mock.patch.object(tgbridge, 'audit'), mock.patch.object(tgbridge, 'send', return_value=False):
            for text in ('first', 'second'):
                self.assertFalse(tgbridge.send_retry({'bot_token': 'fixture'}, 42, text))
            files = list((Path(root)/'undelivered').glob('*.txt'))
            self.assertEqual(sorted(p.read_text() for p in files), ['first', 'second'])

    def test_midrun_input_skips_idle_debounce_and_ack_cannot_block_rpc(self):
        injected = threading.Event()
        release_ack = threading.Event()
        ack_started = threading.Event()
        def inject(*args):
            injected.set()
        def slow_ack(*args, **kwargs):
            ack_started.set()
            release_ack.wait(2)
            return True
        with tgbridge.RUN_LOCK:
            tgbridge.RUN_STATE.update(pi_sid='s1', pi_run_id=12)
        with mock.patch.object(tgbridge, '_pi_steer_deliver', side_effect=inject), \
             mock.patch.object(tgbridge, 'send', side_effect=slow_ack), \
             mock.patch.object(tgbridge.threading, 'Timer') as timer:
            try:
                tgbridge.defer_prompt({'bot_token': 'fixture'}, 42, 99, 'change course now')
                self.assertTrue(injected.wait(1))
                self.assertTrue(ack_started.wait(1))
                timer.assert_not_called()
                self.assertNotIn(42, tgbridge.PENDING_PROMPTS)
            finally:
                release_ack.set()

    def test_startup_message_is_not_committed_behind_current_run(self):
        class Timer:
            def __init__(self, delay, fn, args):
                self.fn, self.args = fn, args
            def start(self):
                pass
        cfg = {'bot_token': 'fixture'}
        batch = {'parts': ['arrived during startup'], 'message_id': 99, 'queued': False}
        tgbridge.PENDING_PROMPTS[42] = batch
        self.addCleanup(lambda: tgbridge.PENDING_PROMPTS.pop(42, None))
        with tgbridge.RUN_LOCK:
            tgbridge.RUN_STATE.update(pi_sid=None, current={'chat': 42, 'runner': 'pi', 'mode': 'server'})
        with mock.patch.object(tgbridge.threading, 'Timer', Timer), \
             mock.patch.object(tgbridge.PROMPT_Q, 'put') as put:
            tgbridge._flush_prompt_batch(cfg, 42, batch)
        self.assertFalse(batch['queued'])
        put.assert_not_called()
        self.assertIn('timer', batch)
        # If startup fails or the run ends, preserve it exactly once as next turn.
        with tgbridge.RUN_LOCK:
            tgbridge.RUN_STATE['busy'] = False
        with mock.patch.object(tgbridge.PROMPT_Q, 'put') as put:
            batch['timer'].fn(*batch['timer'].args)
            tgbridge._flush_prompt_batch(cfg, 42, batch)
        put.assert_called_once_with(batch)

    def test_cancelled_run_cannot_reserve_new_steering(self):
        with tgbridge.RUN_LOCK:
            tgbridge.RUN_STATE.update(pi_sid='s1', pi_run_id=12, cancel=True)
        self.assertFalse(tgbridge.should_steer(42))
        self.assertIsNone(tgbridge._begin_steer(42))

    def test_native_queue_receives_steer_before_next_tool_not_agent_end(self):
        source = test_pi_rpc.FAKE_PI.replace('DELAY = float', 'STEER_SEEN = threading.Event()\nDELAY = float')
        start = source.index('        emit({"type": "agent_start"}')
        stop = source.index('    elif kind == "steer":')
        body = '''        emit({"type": "tool_execution_start", "toolName": "bash", "toolCallId": "tool1", "args": {}})
        emit({"type": "tool_execution_end", "toolName": "bash", "toolCallId": "tool1", "result": {}, "isError": False})
        emit({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "tool1 complete"}], "stopReason": "toolUse"}})
        if not STEER_SEEN.wait(2):
            os._exit(9)
        record({"type": "fixture_next_tool"})
        emit({"type": "tool_execution_start", "toolName": "bash", "toolCallId": "tool2", "args": {}})
        emit({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "steered before tool2"}], "stopReason": "stop"}})
        emit({"type": "agent_settled"})
'''
        source = source[:start] + body + source[stop:]
        source = source.replace('"role": "user", "content": command["message"]}})', '"role": "user", "content": command["message"]}})\n        STEER_SEEN.set()')
        ack_started = threading.Event()
        release_ack = threading.Event()
        def slow_ack(*a, **k):
            ack_started.set()
            release_ack.wait(3)
            return True
        with mock.patch.object(test_pi_rpc, 'FAKE_PI', source), FakePiFixture(self) as fixture:
            cfg = {'bot_token': 'fixture', 'workdir': fixture.tmp.name}
            def segment(cfg, chat, text, reply_to=None):
                if text == 'tool1 complete':
                    tgbridge.defer_prompt(cfg, 42, 99, 'check this before tool2')
                return True
            live = {'chat_id': 42, 'trail': [], 'start': time.time(), 'streamed': False}
            with mock.patch.object(tgbridge, 'send_retry', side_effect=segment), \
                 mock.patch.object(tgbridge, 'send', side_effect=slow_ack):
                try:
                    _, answer, err = tgbridge.run_pi_rpc(cfg, 's1', 'go', live)
                    commands = fixture.commands()
                    self.assertIsNone(err)
                    self.assertIn('steered before tool2', answer)
                    kinds = [c['type'] for c in commands]
                    self.assertLess(kinds.index('steer'), kinds.index('fixture_next_tool'))
                    self.assertNotIn('set_steering_mode', kinds)
                    self.assertTrue(ack_started.wait(1))
                finally:
                    release_ack.set()

    def test_same_chat_steering_and_cross_chat_isolation(self):
        with tgbridge.RUN_LOCK:
            tgbridge.RUN_STATE.update(pi_sid='s1', pi_run_id=1)
        self.assertIsNone(tgbridge._begin_steer(43))
        self.assertEqual(tgbridge._begin_steer(42)['transport'], 'pi_rpc_steer')


if __name__ == '__main__':
    unittest.main()
