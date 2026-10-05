"""No production bot calls: prove control-loop recovery and watchdog behavior."""
import queue
import unittest
from unittest import mock

import tgbridge


class StopFixture(BaseException):
    pass


class ResilienceTests(unittest.TestCase):
    def test_poll_failures_then_success_keep_control_loop_watchdog_progress(self):
        health = []
        calls = []
        polls = iter([None, {'ok': True, 'result': []}])

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
             mock.patch.object(tgbridge, 'rearm_at'), \
             mock.patch.object(tgbridge, 'home_chat', return_value=None), \
             mock.patch.object(tgbridge.threading, 'Thread'), \
             mock.patch.object(tgbridge.time, 'sleep') as sleep:
            with self.assertRaises(StopFixture):
                tgbridge.run({'bot_token': 'fixture', 'allowed_chats': []})
        self.assertTrue(any(h.get('status') == 'unhealthy' for h in health))
        self.assertEqual(health[-1]['status'], 'healthy')
        self.assertEqual(health[-1]['consecutive_poll_failures'], 0)
        self.assertGreaterEqual(notify.call_args_list.count(mock.call('WATCHDOG=1')), 4)
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
             mock.patch.object(tgbridge, 'react', side_effect=RuntimeError('early fixture failure')), \
             mock.patch.object(tgbridge, 'send'), \
             mock.patch.object(tgbridge.os, 'listdir', side_effect=FileNotFoundError):
            with self.assertRaises(StopFixture):
                tgbridge.worker({'workdir': '/fixture', 'bot_token': 'fixture'}, {})
        self.assertFalse(tgbridge.RUN_STATE['busy'])
        self.assertIsNone(tgbridge.RUN_STATE['current'])


if __name__ == '__main__':
    unittest.main()
