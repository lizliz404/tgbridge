"""Human-first action chrome with expandable, escaped and redacted facts."""
import html
import re
import unittest

from tgbridge_core.progress import Journal, compact_action, codex_event


class CompactActionTests(unittest.TestCase):
    def test_summary_separates_observed_status_from_technical_details(self):
        event = {'label': 'bash', 'state': 'completed', 'inputs': {'command': 'python3 check.py'},
                 'output': 'OK', 'kind': 'action', 'id': 'a'}
        body = compact_action(event, 2)[0]
        self.assertTrue(body.startswith('<b>✅ 动作 2 · 执行命令 · 已完成</b>'))
        self.assertIn('<blockquote expandable>', body)
        self.assertIn('python3 check.py', body)
        self.assertIn('output:\nOK', body)

    def test_nonzero_exit_is_failure_even_when_native_item_says_completed(self):
        event = codex_event({'method': 'item/completed', 'params': {'item': {
            'id': 'a', 'type': 'commandExecution', 'status': 'completed', 'exitCode': 2}}})
        self.assertEqual(event['state'], 'failed')
        self.assertIn('失败', compact_action(event, 1)[0])

    def test_entities_unicode_and_secrets_are_safe_and_details_are_lossless(self):
        output = '<&😀\n' * 2400
        bodies = compact_action({'label': 'edit', 'state': 'failed', 'inputs': {
            'token': 'SECRET', 'path': '/repo/a.py'}, 'output': output}, 1, ['SECRET'])
        self.assertGreater(len(bodies), 1)
        details = []
        for body in bodies:
            self.assertLessEqual(len(body.encode('utf-16-le')) // 2, 3900)
            self.assertNotIn('SECRET', body)
            details.append(html.unescape(re.search(r'<blockquote expandable>(.*)</blockquote>', body, re.S)[1]))
        self.assertIn(output, ''.join(details))

    def test_identity_and_result_update_stay_on_the_same_message(self):
        calls = []
        def send(body, identity):
            calls.append((identity, body)); return identity or 11
        journal = Journal({}, lambda *args: True, send, compact=True)
        for state in ('running', 'completed', 'completed'):
            journal.emit({'kind': 'action', 'id': 'a', 'label': 'read', 'state': state,
                          'inputs': {'path': '/repo/file'}})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0], 11)
        self.assertIn('动作 1', calls[1][1])
        self.assertIn('已完成', calls[1][1])


if __name__ == '__main__':
    unittest.main()
