"""Chat boundaries: parallel execution, serial turns, control and file routing."""
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
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

    def test_idle_submission_does_not_queue_behind_itself(self):
        runtime = self.dispatcher.runtime(42)
        batch = {'chat_id': 42, 'message_id': 5, 'parts': ['first task'],
                 'queued': False}

        def worker_starts_during_put(entry):
            # Deterministically reproduce the worker winning the enqueue race.
            runtime.RUN_STATE.update(busy=True, current={'chat': 42, 'mode': 'cli'})

        with mock.patch.object(runtime, 'PENDING_PROMPTS', {42: batch}), \
             mock.patch.object(tgbridge, 'send') as send, \
             mock.patch.object(runtime.PROMPT_Q, 'put', side_effect=worker_starts_during_put):
            runtime._flush_prompt_batch(self.cfg, 42, batch)
        send.assert_not_called()

    def test_genuine_same_chat_wait_keeps_queue_notice(self):
        runtime = self.dispatcher.runtime(42)
        runtime.RUN_STATE.update(busy=True, current={'chat': 42, 'mode': 'cli'})
        batch = {'chat_id': 42, 'message_id': 5, 'parts': ['next task'],
                 'queued': False}
        with mock.patch.object(runtime, 'PENDING_PROMPTS', {42: batch}), \
             mock.patch.object(tgbridge, 'send') as send, \
             mock.patch.object(runtime.PROMPT_Q, 'put') as put:
            runtime._flush_prompt_batch(self.cfg, 42, batch)
        put.assert_called_once_with(batch)
        self.assertTrue(send.call_args.args[2].endswith(' · 1'))

    def test_existing_backlog_is_reported_even_before_worker_marks_busy(self):
        runtime = self.dispatcher.runtime(42)
        runtime.PROMPT_Q = queue.Queue()
        runtime.PROMPT_Q.put((42, 1, 'predecessor'))
        batch = {'chat_id': 42, 'message_id': 5, 'parts': ['next task'],
                 'queued': False}
        with mock.patch.object(runtime, 'PENDING_PROMPTS', {42: batch}), \
             mock.patch.object(tgbridge, 'send') as send:
            runtime._flush_prompt_batch(self.cfg, 42, batch)
        self.assertTrue(send.call_args.args[2].endswith(' · 2'))

    def test_claimed_task_remains_backlog_during_worker_setup(self):
        runtime = self.dispatcher.runtime(42)
        runtime.PROMPT_Q = queue.Queue()
        runtime.PROMPT_Q.put((42, 1, 'predecessor'))
        runtime.PROMPT_Q.get()
        self.assertFalse(runtime.RUN_STATE['busy'])
        self.assertEqual(runtime.PROMPT_Q.qsize(), 0)
        batch = {'chat_id': 42, 'message_id': 5, 'parts': ['next task'],
                 'queued': False}
        with mock.patch.object(runtime, 'PENDING_PROMPTS', {42: batch}), \
             mock.patch.object(tgbridge, 'send') as send:
            runtime._flush_prompt_batch(self.cfg, 42, batch)
        self.assertTrue(send.call_args.args[2].endswith(' · 1'))

    def test_ingress_to_real_rpc_parallel_chat_and_new_session_lanes(self):
        from test_pi_rpc import FakePiFixture
        from tgbridge_core.inbox import Inbox

        cases = ((42, -43, False), (-43, 42, False),
                 (42, 42, True), (-43, -43, True))
        for first, second, reset in cases:
            with self.subTest(first=first, reset=reset), tempfile.TemporaryDirectory() as root:
                gate = Path(root, 'release')
                entered = Path(root, 'entered')
                inbox = Inbox(str(Path(root, 'inputs.json')))
                cfg = dict(self.cfg, _inbox=inbox, input_debounce_s=0.2,
                           runner_modes={'pi': 'server'}, workdir=root)
                state = {'bot_username': 'fixture_bot'}
                dispatcher = ChatDispatcher(tgbridge, cfg, state)
                cfg['_chat_dispatcher'] = dispatcher
                first_done, second_delivered, steer_consumed = (
                    threading.Event() for _ in range(3))
                sent = []
                original_transition = inbox.transition

                def transition(ids, status, **kwargs):
                    result = original_transition(ids, status, **kwargs)
                    if status == 'executing' and f'tg:{first}:6' in ids:
                        steer_consumed.set()
                    return result

                def send(token, chat, text, **kwargs):
                    sent.append((chat, text))
                    if chat == second and text == 'second segment':
                        second_delivered.set()
                    return True

                def audit(event, **fields):
                    if (event == 'run_done' and fields.get('chat_id') == first
                            and (not reset or gate.exists())):
                        first_done.set()

                with FakePiFixture(self) as fixture, \
                     mock.patch.object(tgbridge, 'PENDING_PROMPTS', {}), \
                     mock.patch.object(tgbridge, 'send', side_effect=send), \
                     mock.patch.object(tgbridge, 'send_retry', side_effect=send), \
                     mock.patch.object(tgbridge, 'audit', side_effect=audit), \
                     mock.patch.object(tgbridge, 'log'), \
                     mock.patch.object(tgbridge, 'react'), \
                     mock.patch.object(tgbridge, 'save_json'), \
                     mock.patch.object(tgbridge, 'edit_status'), \
                     mock.patch.object(tgbridge, 'typing_loop'), \
                     mock.patch.object(tgbridge, 'api', return_value={
                         'ok': True, 'result': {'message_id': 5}}), \
                     mock.patch.object(inbox, 'transition', side_effect=transition):
                    binary = Path(fixture.binary)
                    script = binary.read_text().replace(
                        '"sessionId": "sid-fixture"',
                        '"sessionId": "sid-fixture-" + str(os.getpid())').replace(
                        'emit({"type": "agent_start"}, delay=True)',
                        'emit({"type": "agent_start"}, delay=True)\n'
                        '        if "hold-first-chat" in command["message"]:\n'
                        f'            open({str(entered)!r}, "w").close()\n'
                        f'            while not os.path.exists({str(gate)!r}):\n'
                        '                time.sleep(0.01)')
                    binary.write_text(script)
                    try:
                        tgbridge.handle_update(cfg, state,
                            self.update(first, 'hold-first-chat'), state_path=None)
                        deadline = time.monotonic() + 5
                        while not entered.exists() and time.monotonic() < deadline:
                            first_done.wait(0.01)
                        self.assertTrue(entered.exists(), 'first child never received prompt')
                        old = dispatcher.runtime(first)
                        if reset:
                            tgbridge.handle_update(cfg, state,
                                self.update(first, '/new'), state_path=None)
                            self.assertIsNot(old, dispatcher.runtime(first))
                            self.assertNotEqual(old.question_owner('pi', 1),
                                dispatcher.runtime(first).question_owner('pi', 1))
                            self.assertNotEqual(old.outbox_dir(cfg),
                                dispatcher.runtime(first).outbox_dir(cfg))
                        request = self.update(second, 'independent task')
                        request['message']['message_id'] = 7
                        tgbridge.handle_update(cfg, state, request, state_path=None)
                        self.assertTrue(second_delivered.wait(5),
                                        'second lane waited for unfinished first lane')
                        self.assertFalse(first_done.is_set())
                        self.assertTrue(old.RUN_STATE['busy'])
                        self.assertFalse(any(text.startswith('⏳') for _, text in sent), sent)
                        if not reset:
                            steer = self.update(first, 'change only first chat')
                            steer['message']['message_id'] = 6
                            tgbridge.handle_update(cfg, state, steer, state_path=None)
                            self.assertTrue(steer_consumed.wait(5))
                            steers = [c for c in fixture.commands() if c['type'] == 'steer']
                            self.assertEqual(len(steers), 1)
                            self.assertTrue(steers[0]['message'].endswith('change only first chat'))
                        gate.touch()
                        self.assertTrue(first_done.wait(5))
                    finally:
                        gate.touch()
                        owners = dispatcher.close()
                        for runtime in owners:
                            runtime.PROMPT_Q.put((runtime.chat_id, 0, 'shutdown'))
                        for runtime in owners:
                            thread = runtime.thread
                            if thread is not None:
                                thread.join(5)
                                self.assertFalse(thread.is_alive())
                self.assertEqual(inbox.data[f'tg:{second}:7']['status'], 'completed')
                self.assertEqual(inbox.data[f'tg:{first}:5']['status'], 'completed')
                if reset:
                    self.assertNotEqual(tgbridge.runner_session(state, first, 'pi'),
                                        old.runner_session(state, first, 'pi'))
                else:
                    self.assertEqual(inbox.data[f'tg:{first}:6']['status'], 'completed')

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
        new = self.dispatcher.runtime(42)
        self.assertIsNot(new, a)
        self.assertEqual(new.SESSION_EPOCH, 1)
        self.assertTrue(a.RETIRED)
        self.assertEqual(a.runner_session(self.state, 42, 'pi'), 'old-dm')
        self.assertIsNone(tgbridge.runner_session(self.state, 42, 'pi'))
        self.assertEqual(tgbridge.runner_session(self.state, -43, 'pi'), 'group')

    def test_new_keeps_pre_reset_burst_and_queued_turns_on_old_owner(self):
        old = self.dispatcher.runtime(42)
        tgbridge.store_runner_session(self.state, 42, 'pi', 'old-native')
        old.PROMPT_Q = queue.Queue()
        with mock.patch.object(tgbridge, 'audit'), \
             mock.patch('tgbridge_core.ingress.threading.Timer') as timer:
            old.defer_prompt(self.cfg, 42, 5, 'old split one')
            old.defer_prompt(self.cfg, 42, 6, 'old split two')
        batch = old.PENDING_PROMPTS[42]
        with mock.patch.object(tgbridge, 'send'):
            tgbridge.handle_update(self.cfg, self.state,
                                   self.update(42, '/new'), state_path=None)
        new = self.dispatcher.runtime(42)
        self.assertEqual(new.PENDING_PROMPTS, {})
        self.assertIsNot(old.PENDING_PROMPTS, new.PENDING_PROMPTS)
        timer.return_value.cancel.assert_called_once()
        with mock.patch.object(tgbridge, 'send') as send:
            old._flush_prompt_batch(self.cfg, 42, batch)
        send.assert_not_called()
        entry = old.PROMPT_Q.get_nowait()
        self.assertEqual(old.unpack_entry(entry), (42, 5, 'old split one\n\nold split two'))
        old.PROMPT_Q.task_done()
        self.assertEqual(old.runner_session(self.state, 42, 'pi'), 'old-native')
        self.assertIsNone(new.runner_session(self.state, 42, 'pi'))
        self.assertEqual(new.PROMPT_Q.qsize(), 0)

    def test_new_controls_and_shutdown_preserve_old_execution_owner(self):
        old = self.dispatcher.runtime(42)
        proc = mock.Mock()
        proc.poll.return_value = None
        old.RUN_STATE.update(busy=True, proc=proc, current={'chat': 42})
        with mock.patch.object(tgbridge, 'send') as send, \
             mock.patch.object(tgbridge, 'signal_run_process') as signal:
            tgbridge.handle_update(self.cfg, self.state, self.update(42, '/new'), state_path=None)
            tgbridge.handle_update(self.cfg, self.state, self.update(42, '/cancel'), state_path=None)
            self.assertIn('nothing running', send.call_args.args)
        signal.assert_not_called()
        self.assertFalse(old.RUN_STATE['cancel'])
        owners = self.dispatcher.close()
        self.assertIn(old, owners)
        self.assertIn(self.dispatcher.runtime(42), owners)
        with mock.patch.object(tgbridge, 'signal_run_process') as signal, \
             mock.patch.object(tgbridge, 'kill_after'):
            for owner in owners:
                execution.stop_runtime(owner)
        signal.assert_called_once_with(proc, __import__('signal').SIGTERM)

    def test_retired_worker_rechecks_late_enqueue_before_releasing_ownership(self):
        old = self.dispatcher.runtime(42)
        with mock.patch.object(tgbridge, 'send'):
            tgbridge.handle_update(self.cfg, self.state, self.update(42, '/new'), state_path=None)
        thread = mock.Mock()
        old.thread = thread
        with mock.patch.object(old.PROMPT_Q, 'start_worker'):
            old.PROMPT_Q.put((42, 5, 'late fallback'))
        self.assertFalse(self.dispatcher.release_retired_worker(old))
        self.assertIs(old.thread, thread)
        old.PROMPT_Q.get_nowait()
        old.PROMPT_Q.task_done()
        self.assertTrue(self.dispatcher.release_retired_worker(old))
        self.assertIsNone(old.thread)
        with mock.patch('tgbridge_core.chat_runtime.threading.Thread') as create:
            old.PROMPT_Q.put((42, 6, 'after exit'))
        create.return_value.start.assert_called_once()

    def test_scheduled_owner_survives_multiple_new_sessions_and_shutdown(self):
        old = self.dispatcher.runtime(42)
        with mock.patch('tgbridge_core.ingress.threading.Timer') as timer, \
             mock.patch.object(tgbridge, 'send'), mock.patch.object(tgbridge, 'audit'), \
             mock.patch.object(tgbridge, 'save_json'):
            tgbridge.handle_update(self.cfg, self.state,
                self.update(42, '/at 10m scheduled task'), state_path=None)
            for _ in range(2):
                tgbridge.handle_update(self.cfg, self.state,
                    self.update(42, '/new'), state_path=None)
        callback = timer.call_args.args[1]
        args = timer.call_args.kwargs['args']
        self.assertIn(old, self.dispatcher.owners())
        self.dispatcher.close()
        with mock.patch('tgbridge_core.chat_runtime.threading.Thread') as create, \
             mock.patch.object(tgbridge, 'save_json'), mock.patch.object(tgbridge, 'audit'):
            callback(*args)
        create.assert_not_called()
        self.assertTrue(old.STOPPING)
        self.assertEqual(old.PROMPT_Q.qsize(), 1)

    def test_old_question_does_not_consume_new_session_input(self):
        from tgbridge_core.questions import Questions
        old = self.dispatcher.runtime(42)
        answered = mock.Mock()
        broker = Questions(lambda *args: 123, mock.Mock(), mock.Mock())
        self.cfg['_questions'] = broker
        broker.offer(42, 7, 'Old question?', ['Yes'], answered,
                     owner=old.question_owner('pi', 1))
        with mock.patch.object(tgbridge, 'send'):
            tgbridge.handle_update(self.cfg, self.state, self.update(42, '/new'), state_path=None)
        new = self.dispatcher.runtime(42)
        with mock.patch.object(tgbridge, 'defer_prompt') as defer, \
             mock.patch.object(tgbridge, 'audit'):
            message = self.update(42, 'new independent prompt')
            message['message'].pop('reply_to_message')
            tgbridge.handle_update(self.cfg, self.state, message, state_path=None)
        defer.assert_called_once()
        answered.assert_not_called()
        self.assertTrue(broker.answer_text(42, 7, 'Yes', 123,
                                          execution_id=new.execution_id))
        answered.assert_called_once_with('Yes')

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
        with mock.patch.object(a, 'PENDING_PROMPTS', {42: {'timer': timer}}), \
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
