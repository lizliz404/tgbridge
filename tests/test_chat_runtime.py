"""Chat boundaries: parallel execution, serial turns, control and file routing."""
import os
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest import mock

import tgbridge
from tgbridge_core.chat_runtime import ChatDispatcher
from tgbridge_core import execution


class StopWorker(BaseException):
    pass


class ChatRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = {'allowed_chats': [42, -43], 'allowed_user_ids': [7],
                    'workdir': self.tmp.name, 'bot_token': 'fixture', 'runner': 'pi'}
        self.state = {'bot_username': 'fixture_bot'}
        self.dispatcher = ChatDispatcher(tgbridge, self.cfg, self.state)
        self.cfg['_chat_dispatcher'] = self.dispatcher

    def update(self, chat, text):
        return {'message': {'chat': {'id': chat, 'type': 'private' if chat == 42 else 'supergroup'},
                            'from': {'id': 7}, 'message_id': 5, 'text': text,
                            'reply_to_message': {'from': {'username': 'fixture_bot'}}}}

    def test_runtime_owners_are_isolated_and_rebind_server_adapters(self):
        a, b = self.dispatcher.runtime(42), self.dispatcher.runtime(-43)
        a.RUN_STATE['busy'] = True
        a.RUN_STATE['current'] = {'chat': 42}
        a.RUN_STATE['pi_sid'] = 'a'
        a.RUN_STATE['pi_run_id'] = 1
        self.assertTrue(a.should_steer(42))
        self.assertFalse(b.should_steer(-43))
        self.assertFalse(a.should_steer(-43))
        a.RUN_STATE['pi_steers']['x'] = 1
        self.assertEqual(b.RUN_STATE['pi_steers'], {})
        self.assertIsNot(a.RUN_LOCK, b.RUN_LOCK)
        self.assertIs(a.SERVER_RUNNERS['pi']['run'].args[0], a)
        self.assertIs(b.SERVER_RUNNERS['pi']['run'].args[0], b)
        self.assertIs(a.run_with_fallbacks.args[0], a)
        with self.assertRaises(ValueError):
            self.dispatcher.runtime(99)

    def test_two_chats_start_before_either_finishes_and_same_chat_is_serial(self):
        started = {42: threading.Event(), -43: threading.Event()}
        release = threading.Event()
        ended = {42: threading.Event(), -43: threading.Event()}
        seen = []
        guard = threading.Lock()

        def worker(app, cfg, state):
            try:
                while True:
                    item = app.PROMPT_Q.get(timeout=2)
                    with guard:
                        seen.append((app.chat_id, item[2]))
                    started[app.chat_id].set()
                    if item[2] == 'first':
                        release.wait(3)
                    app.PROMPT_Q.task_done()
                    if item[2] == 'last':
                        return
            finally:
                ended[app.chat_id].set()

        with mock.patch.object(tgbridge, 'worker', tgbridge._bind(worker)):
            a, b = self.dispatcher.runtime(42), self.dispatcher.runtime(-43)
            a.PROMPT_Q.put((42, 1, 'first'))
            a.PROMPT_Q.put((42, 2, 'last'))
            b.PROMPT_Q.put((-43, 1, 'first'))
            b.PROMPT_Q.put((-43, 2, 'last'))
            try:
                self.assertTrue(started[42].wait(2))
                self.assertTrue(started[-43].wait(2))
                self.assertCountEqual(seen, [(42, 'first'), (-43, 'first')])
            finally:
                release.set()
                self.assertTrue(ended[42].wait(3))
                self.assertTrue(ended[-43].wait(3))
        self.assertEqual([text for chat, text in seen if chat == 42], ['first', 'last'])
        self.assertEqual(a.PROMPT_Q.unfinished_tasks, 0)
        self.assertEqual(b.PROMPT_Q.unfinished_tasks, 0)

    def test_cancel_and_status_from_dm_do_not_touch_busy_group(self):
        group = self.dispatcher.runtime(-43)
        proc = mock.Mock()
        proc.poll.return_value = None
        group.RUN_STATE.update(busy=True, proc=proc,
                               current={'chat': -43, 'prompt': 'private group work', 'since': 0})
        with mock.patch.object(tgbridge, 'send') as send, \
             mock.patch.object(tgbridge, 'signal_run_process') as signal, \
             mock.patch.object(tgbridge, 'load_json', return_value={}):
            tgbridge.handle_update(self.cfg, self.state, self.update(42, '/cancel'), state_path=None)
            self.assertIn('nothing running', send.call_args.args)
            self.assertFalse(group.RUN_STATE['cancel'])
            signal.assert_not_called()
            tgbridge.handle_update(self.cfg, self.state, self.update(42, '/status'), state_path=None)
            self.assertNotIn('private group work', send.call_args.args[2])
            tgbridge.handle_update(self.cfg, self.state, self.update(-43, '/cancel'), state_path=None)
            self.assertTrue(group.RUN_STATE['cancel'])
            signal.assert_called_once_with(proc, __import__('signal').SIGTERM)
        # Avoid a real kill escalation timer in subsequent tests.
        proc.poll.return_value = 0

    def test_new_clears_only_its_chat_and_cannot_restore_finished_old_session(self):
        tgbridge.store_runner_session(self.state, 42, 'pi', 'old-dm')
        tgbridge.store_runner_session(self.state, -43, 'pi', 'group')
        a = self.dispatcher.runtime(42)
        with mock.patch.object(tgbridge, 'send'):
            tgbridge.handle_update(self.cfg, self.state, self.update(42, '/new'), state_path=None)
        self.assertEqual(a.SESSION_EPOCH, 1)
        self.assertIsNone(tgbridge.runner_session(self.state, 42, 'pi'))
        self.assertEqual(tgbridge.runner_session(self.state, -43, 'pi'), 'group')

    def test_real_worker_delivers_only_its_chat_outbox_and_keeps_new_reset(self):
        a, b = self.dispatcher.runtime(42), self.dispatcher.runtime(-43)
        for runtime in (a, b):
            Path(runtime.outbox_dir(self.cfg)).mkdir(parents=True)
            Path(runtime.outbox_dir(self.cfg), 'answer.txt').write_text(str(runtime.chat_id))
        # A legacy root-level file is never claimed by a concurrent chat.
        legacy = Path(tgbridge.outbox_dir(self.cfg), 'legacy.txt')
        legacy.write_text('unattributed')
        a.PROMPT_Q = mock.Mock()
        a.PROMPT_Q.get.side_effect = [(42, 5, 'fixture task'), StopWorker()]
        prompts = []

        def run(cfg, sid, prompt, live, **kwargs):
            prompts.append(prompt)
            tgbridge.handle_update(self.cfg, self.state, self.update(42, '/new'), state_path=None)
            return 'old-completed', 'done', None

        with mock.patch.object(tgbridge, 'react'), mock.patch.object(tgbridge, 'audit'), \
             mock.patch.object(tgbridge, 'log'), mock.patch.object(tgbridge, 'send'), \
             mock.patch.object(tgbridge, 'save_json'), mock.patch.object(tgbridge, 'edit_status'), \
             mock.patch.object(tgbridge, 'api', return_value={'ok': True}), \
             mock.patch.object(tgbridge, 'typing_loop'), \
             mock.patch.object(tgbridge, 'run_with_fallbacks', side_effect=run), \
             mock.patch.object(tgbridge, 'send_retry', return_value=True), \
             mock.patch.object(tgbridge, 'send_document', return_value={'ok': True}) as document:
            with self.assertRaises(StopWorker):
                a.worker(self.cfg, self.state)
        self.assertIn(a.outbox_dir(self.cfg), prompts[0])
        document.assert_called_once_with('fixture', 42, os.path.join(a.outbox_dir(self.cfg), 'answer.txt'))
        self.assertTrue(Path(b.outbox_dir(self.cfg), 'answer.txt').exists())
        self.assertTrue(legacy.exists())
        self.assertIsNone(tgbridge.runner_session(self.state, 42, 'pi'))
        self.assertFalse(a.RUN_STATE['busy'])

    def test_shutdown_cancels_each_owned_process(self):
        for chat in (42, -43):
            runtime = self.dispatcher.runtime(chat)
            runtime.RUN_STATE['proc'] = mock.Mock()
            runtime.RUN_STATE['proc'].poll.return_value = None
        with mock.patch.object(tgbridge, 'signal_run_process') as signal, \
             mock.patch.object(tgbridge, 'kill_after') as kill:
            for owner in self.dispatcher.owners():
                execution.stop_runtime(owner)
        self.assertEqual(signal.call_count, 2)
        self.assertEqual(kill.call_count, 2)
        self.assertTrue(all(owner.RUN_STATE['cancel'] for owner in self.dispatcher.owners()))

    def test_runner_switch_and_default_reset_are_chat_local(self):
        a, b = self.dispatcher.runtime(42), self.dispatcher.runtime(-43)
        with mock.patch.object(tgbridge, 'send'), mock.patch.object(tgbridge, 'audit'):
            tgbridge.handle_update(self.cfg, self.state,
                                   self.update(42, '/runner codex provider/model'), state_path=None)
            self.assertEqual(a.effective_run_config(self.cfg, self.state)['runner'], 'codex')
            self.assertEqual(a.effective_run_config(self.cfg, self.state)['model'], 'provider/model')
            self.assertEqual(b.effective_run_config(self.cfg, self.state)['runner'], 'pi')
            tgbridge.handle_update(self.cfg, self.state,
                                   self.update(-43, '/runner opencode other/model'), state_path=None)
            tgbridge.handle_update(self.cfg, self.state,
                                   self.update(42, '/runner default'), state_path=None)
        self.assertEqual(a.effective_run_config(self.cfg, self.state)['runner'], 'pi')
        self.assertEqual(b.effective_run_config(self.cfg, self.state)['runner'], 'opencode')
        self.assertEqual(b.effective_run_config(self.cfg, self.state)['model'], 'other/model')
        self.state['runner_override'] = {'runner': 'codex'}
        self.assertEqual(a.effective_run_config(self.cfg, self.state)['runner'], 'pi')
        self.assertEqual(b.effective_run_config(self.cfg, self.state)['runner'], 'opencode')

    def test_questions_with_same_runner_counter_are_chat_scoped(self):
        from tgbridge_core.questions import Questions
        broker = Questions(lambda *args: 5, mock.Mock(), mock.Mock())
        a, b = self.dispatcher.runtime(42), self.dispatcher.runtime(-43)
        for transport in ('pi', 'codex'):
            a_owner = a.question_owner(transport, 1)
            b_owner = b.question_owner(transport, 1)
            self.assertNotEqual(a_owner, b_owner)
            broker.offer(42, 7, 'DM?', ['Yes'], mock.Mock(), owner=a_owner)
            broker.offer(-43, 7, 'Group?', ['Yes'], mock.Mock(), owner=b_owner)
            broker.close_owner(a_owner)
            self.assertFalse(broker.has_owner(a_owner))
            self.assertTrue(broker.has_owner(b_owner))
            broker.close_owner(b_owner)

    def test_close_blocks_worker_resurrection_and_cancels_debounce(self):
        a = self.dispatcher.runtime(42)
        timer = mock.Mock()
        with mock.patch.object(tgbridge, 'PENDING_PROMPTS', {42: {'timer': timer}}), \
             mock.patch('tgbridge_core.chat_runtime.threading.Thread') as thread:
            self.assertEqual(self.dispatcher.close(), [a])
            a.PROMPT_Q.put((42, 1, 'do not execute'))
            self.dispatcher.supervise()
        thread.assert_not_called()
        timer.cancel.assert_called_once()
        self.assertTrue(a.STOPPING)
        with mock.patch.object(tgbridge, 'run_with_fallbacks') as run:
            a.worker(self.cfg, self.state)
        run.assert_not_called()

    def test_parallel_pi_rpc_pipes_and_native_question_owners(self):
        from test_pi_rpc import FakePiFixture
        a, b = self.dispatcher.runtime(42), self.dispatcher.runtime(-43)
        results = {}
        both_live = threading.Barrier(2, timeout=5)
        original = tgbridge._pi_rpc_write._runtime_impl

        def write(app, proc, payload):
            if payload.get('type') == 'prompt':
                both_live.wait()
            return original(app, proc, payload)

        cfg = dict(self.cfg, runner_modes={'pi': 'server'})
        def run(runtime):
            results[runtime.chat_id] = runtime.run_pi_rpc(cfg, None, 'fixture')
        with FakePiFixture(self), mock.patch.object(tgbridge, 'audit'), \
             mock.patch.object(tgbridge, 'log'), \
             mock.patch.object(tgbridge, '_pi_rpc_write', tgbridge._bind(write)):
            threads = [threading.Thread(target=run, args=(runtime,)) for runtime in (a, b)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
                self.assertFalse(thread.is_alive())
        self.assertEqual(set(results), {42, -43})
        for sid, answer, err in results.values():
            self.assertIsNone(err)
            self.assertIn('first segment', answer)
            self.assertIn('second segment', answer)
        self.assertIsNone(a.RUN_STATE.get('proc'))
        self.assertIsNone(b.RUN_STATE.get('proc'))

    def test_bound_nested_calls_from_background_threads_keep_owner(self):
        a, b = self.dispatcher.runtime(42), self.dispatcher.runtime(-43)
        entered = threading.Event()
        def nested(app):
            app.mark_run_progress()
            entered.set()
        with mock.patch.object(tgbridge, 'worker', tgbridge._bind(nested)):
            thread = threading.Thread(target=a.worker)
            thread.start()
            self.assertTrue(entered.wait(2))
            thread.join(2)
        self.assertIsNotNone(a.RUN_STATE['last_progress'])
        self.assertIsNone(b.RUN_STATE['last_progress'])


if __name__ == '__main__':
    unittest.main()
