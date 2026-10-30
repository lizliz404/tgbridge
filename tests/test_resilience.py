"""No production bot calls: prove control-loop recovery and watchdog behavior."""
import io
import queue
import signal
import urllib.error
import unittest
from unittest import mock

import tgbridge


class StopFixture(BaseException):
    pass


class ResilienceTests(unittest.TestCase):
    def test_stop_is_control_flow_not_a_recoverable_operation_error(self):
        self.assertFalse(issubclass(tgbridge.BridgeStop, Exception))
        with self.assertRaises(tgbridge.BridgeStop):
            try:
                tgbridge.on_stop(signal.SIGTERM, None)
            except Exception:
                self.fail('generic operation handler swallowed stop')

    def test_stop_propagates_from_network_and_rate_limit_backoff(self):
        for response in (
            tgbridge.BridgeStop(signal.SIGTERM),
            urllib.error.HTTPError('https://fixture', 429, 'limited', {}, io.BytesIO(b'{"parameters":{"retry_after":3}}')),
            urllib.error.HTTPError('https://fixture', 409, 'conflict', {}, io.BytesIO(b'conflict')),
        ):
            with self.subTest(response=type(response).__name__), \
                 mock.patch.object(tgbridge.urllib.request, 'urlopen', side_effect=response), \
                 mock.patch.object(tgbridge.time, 'sleep', side_effect=tgbridge.BridgeStop(signal.SIGTERM)):
                with self.assertRaises(tgbridge.BridgeStop):
                    tgbridge.api('fixture', 'getUpdates')

    def test_api_method_timeouts_are_bounded(self):
        for method, timeout in (('getMe', 10), ('getUpdates', 70), ('sendMessage', 70)):
            with self.subTest(method=method), \
                 mock.patch.object(tgbridge.urllib.request, 'urlopen', side_effect=TimeoutError) as request, \
                 mock.patch.object(tgbridge, 'log'):
                error = {}
                self.assertIsNone(tgbridge.api('fixture', method, _error=error))
                self.assertEqual(request.call_count, 1)
                self.assertEqual(request.call_args.kwargs['timeout'], timeout)
                self.assertTrue(error)

    def test_prolonged_startup_outage_then_recovery(self):
        health = []
        attempts = 0
        inbox = mock.Mock()
        inbox.recover.return_value = ([], [])
        inbox.claim_interruption_notices.return_value = []

        def api(token, method, _error=None, **params):
            nonlocal attempts
            if method == 'getMe':
                attempts += 1
                self.assertIsInstance(_error, dict)
                self.assertEqual(_error, {})
                if attempts <= 25:
                    _error.update(kind='timeout')
                    return None
                return {'ok': True, 'result': {'username': 'fixture_bot'}}
            if method == 'getUpdates':
                raise StopFixture()
            return {'ok': True}

        with mock.patch.object(tgbridge, 'api', side_effect=api), \
             mock.patch.object(tgbridge, 'load_json', return_value={}), \
             mock.patch.object(tgbridge, 'save_json'), \
             mock.patch.object(tgbridge, 'update_health', side_effect=lambda **k: health.append(k)), \
             mock.patch.object(tgbridge, 'systemd_notify') as notify, \
             mock.patch.object(tgbridge, 'probe_runners', return_value={}), \
             mock.patch.object(tgbridge, 'audit'), \
             mock.patch.object(tgbridge, 'log'), \
             mock.patch.object(tgbridge, 'rearm_at'), \
             mock.patch.object(tgbridge, 'home_chat', return_value=None), \
             mock.patch.object(tgbridge, 'CHAT_DISPATCHER'), \
             mock.patch.object(tgbridge, 'ChatDispatcher'), \
             mock.patch.object(tgbridge.threading, 'Thread'), \
             mock.patch.object(tgbridge.urllib.request, 'urlopen') as raw_probe, \
             mock.patch.object(tgbridge.time, 'sleep') as sleep:
            with self.assertRaises(StopFixture):
                tgbridge.run({'bot_token': 'fixture', 'allowed_chats': [],
                              'runner': 'fixture', 'runner_mode': 'cli',
                              '_inbox': inbox, '_questions': mock.Mock()})
        self.assertEqual(attempts, 26)
        raw_probe.assert_not_called()
        self.assertEqual(sleep.call_args_list, [mock.call(3), mock.call(6), mock.call(12)] + [mock.call(15)] * 22)
        self.assertEqual(sum(h.get('status') == 'waiting_for_network' for h in health), 25)
        self.assertEqual(health[-1]['status'], 'polling')
        self.assertEqual(notify.call_args_list.count(mock.call('WATCHDOG=1')), 28)

    def test_startup_401_exits_without_retry_or_duplicate_probe(self):
        for http_error in (True, False):
            with self.subTest(http_error=http_error):
                response = mock.MagicMock()
                response.__enter__.return_value = io.BytesIO(b'{"ok":false,"error_code":401}')
                error = urllib.error.HTTPError('https://fixture', 401, 'Unauthorized', {}, io.BytesIO(b'Unauthorized'))
                with mock.patch.object(tgbridge.urllib.request, 'urlopen',
                                       side_effect=error if http_error else None,
                                       return_value=response) as request, \
                     mock.patch.object(tgbridge, 'load_json', return_value={}), \
                     mock.patch.object(tgbridge, 'save_json') as save, \
                     mock.patch.object(tgbridge, 'update_health'), \
                     mock.patch.object(tgbridge, 'systemd_notify'), \
                     mock.patch.object(tgbridge, 'log'), \
                     mock.patch.object(tgbridge.time, 'sleep') as sleep:
                    with self.assertRaisesRegex(SystemExit, '401 Unauthorized'):
                        tgbridge.run({'bot_token': 'fixture', 'allowed_chats': [],
                                      '_inbox': mock.Mock(), '_questions': mock.Mock()})
                self.assertEqual(request.call_count, 1)
                self.assertEqual(request.call_args.kwargs['timeout'], 10)
                sleep.assert_not_called()
                save.assert_not_called()

    def test_poll_failures_then_success_keep_control_loop_watchdog_progress(self):
        health = []
        calls = []
        polls = iter([None] * 25 + [{'ok': True, 'result': []}])
        inbox = mock.Mock()
        inbox.recover.return_value = ([], [])
        inbox.claim_interruption_notices.return_value = []

        def api(token, method, **params):
            calls.append(method)
            if method == 'getMe':
                return {'ok': True, 'result': {'username': 'fixture_bot'}}
            if method == 'getUpdates':
                try:
                    return next(polls)
                except StopIteration:
                    raise StopFixture()
            return {'ok': True}

        with mock.patch.object(tgbridge, 'api', side_effect=api), \
             mock.patch.object(tgbridge, 'load_json', return_value={}), \
             mock.patch.object(tgbridge, 'save_json'), \
             mock.patch.object(tgbridge, 'update_health', side_effect=lambda **k: health.append(k)), \
             mock.patch.object(tgbridge, 'systemd_notify') as notify, \
             mock.patch.object(tgbridge, 'probe_runners', return_value={}), \
             mock.patch.object(tgbridge, 'audit'), \
             mock.patch.object(tgbridge, 'rearm_at'), \
             mock.patch.object(tgbridge, 'home_chat', return_value=None), \
             mock.patch.object(tgbridge, 'CHAT_DISPATCHER'), \
             mock.patch.object(tgbridge, 'ChatDispatcher') as dispatcher, \
             mock.patch.object(tgbridge.threading, 'Thread') as thread, \
             mock.patch.object(tgbridge.time, 'sleep') as sleep:
            with self.assertRaises(StopFixture):
                tgbridge.run({'bot_token': 'fixture', 'allowed_chats': [],
                              'runner': 'fixture', 'runner_mode': 'cli',
                              '_inbox': inbox, '_questions': mock.Mock()})
        self.assertEqual([h['consecutive_poll_failures'] for h in health
                          if h.get('status') == 'unhealthy'], list(range(1, 26)))
        self.assertEqual(calls.count('getUpdates'), 27)
        dispatcher.return_value.close.assert_not_called()
        self.assertEqual(dispatcher.return_value.supervise.call_count, 27)
        thread.return_value.start.assert_called_once()
        self.assertEqual(health[-1]['status'], 'healthy')
        self.assertEqual(health[-1]['consecutive_poll_failures'], 0)
        self.assertEqual(notify.call_args_list.count(mock.call('WATCHDOG=1')), 29)
        self.assertEqual(sleep.call_args_list, [mock.call(3), mock.call(6), mock.call(12), mock.call(24)] + [mock.call(30)] * 21)

    def test_explicit_poll_failure_exit_threshold_remains_supported(self):
        inbox = mock.Mock()
        inbox.recover.return_value = ([], [])
        inbox.claim_interruption_notices.return_value = []

        def api(token, method, **params):
            if method == 'getMe':
                return {'ok': True, 'result': {'username': 'fixture_bot'}}
            return None if method == 'getUpdates' else {'ok': True}

        with mock.patch.object(tgbridge, 'api', side_effect=api), \
             mock.patch.object(tgbridge, 'load_json', return_value={}), \
             mock.patch.object(tgbridge, 'save_json'), \
             mock.patch.object(tgbridge, 'update_health'), \
             mock.patch.object(tgbridge, 'systemd_notify'), \
             mock.patch.object(tgbridge, 'probe_runners', return_value={}), \
             mock.patch.object(tgbridge, 'audit'), \
             mock.patch.object(tgbridge, 'rearm_at'), \
             mock.patch.object(tgbridge, 'home_chat', return_value=None), \
             mock.patch.object(tgbridge, 'CHAT_DISPATCHER'), \
             mock.patch.object(tgbridge, 'ChatDispatcher'), \
             mock.patch.object(tgbridge.threading, 'Thread'), \
             mock.patch.object(tgbridge.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, '2 consecutive attempts'):
                tgbridge.run({'bot_token': 'fixture', 'allowed_chats': [],
                              'runner': 'fixture', 'runner_mode': 'cli',
                              'poll_failure_exit_threshold': 2,
                              '_inbox': inbox, '_questions': mock.Mock()})
        sleep.assert_called_once_with(3)

    def test_early_worker_error_clears_phantom_busy_run(self):
        class Queue:
            def __init__(self):
                self.calls = 0
            def get(self):
                self.calls += 1
                if self.calls > 1:
                    raise StopFixture()
                return (42, 99, 'fixture prompt')
            def qsize(self):
                return 0
            def task_done(self):
                pass
        with mock.patch.object(tgbridge, 'PROMPT_Q', Queue()), \
             mock.patch.object(tgbridge, 'audit'), \
             mock.patch.object(tgbridge, 'ensure_private_dir'), \
             mock.patch.object(tgbridge, 'react', side_effect=RuntimeError('early fixture failure')), \
             mock.patch.object(tgbridge, 'send'), \
             mock.patch.object(tgbridge.os, 'listdir', side_effect=FileNotFoundError):
            with self.assertRaises(StopFixture):
                tgbridge.worker({'workdir': '/fixture', 'bot_token': 'fixture'}, {})
        self.assertFalse(tgbridge.RUN_STATE['busy'])
        self.assertIsNone(tgbridge.RUN_STATE['current'])


if __name__ == '__main__':
    unittest.main()
