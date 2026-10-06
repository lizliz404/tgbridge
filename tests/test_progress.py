"""Freeze public progress semantics across native Pi/Codex/OpenCode records."""
import json
import unittest

from tgbridge_core.progress import Journal, cli_event, codex_event, opencode_snapshot, pi_event


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.actions = []
        self.live = {}
        def send_action(content, message_id):
            self.actions.append((message_id, content))
            return message_id if message_id is not None else len(self.actions) + 100
        self.journal = Journal(self.live, lambda content, first: self.sent.append(content) or True, send_action)

    def test_each_action_is_durable_and_completion_edits_its_own_message(self):
        start = {'method': 'item/started', 'params': {'item': {
            'id': 'bash1', 'type': 'commandExecution', 'command': 'python3 tests/run_offline.py', 'cwd': '/repo'}}}
        end = {'method': 'item/completed', 'params': {'item': {
            'id': 'bash1', 'type': 'commandExecution', 'command': 'python3 tests/run_offline.py',
            'status': 'completed', 'exitCode': 0, 'aggregatedOutput': '78 tests OK'}}}
        self.journal.emit(codex_event(start))
        self.journal.emit(codex_event(start))
        self.journal.emit(codex_event(end))
        self.journal.emit(codex_event(end))
        self.assertEqual(len(self.actions), 2)
        self.assertIsNone(self.actions[0][0])
        self.assertEqual(self.actions[1][0], 101)
        self.assertIn('python3 tests/run_offline.py', self.actions[1][1])
        self.assertIn('78 tests OK', self.actions[1][1])
        self.assertIn('exit code:\n0', self.actions[1][1])
        self.assertFalse(self.live.get('streamed'))  # tools cannot suppress the final answer

    def test_native_status_names_have_one_public_definition(self):
        event = codex_event({'method': 'item/started', 'params': {'item': {
            'id': 'bash1', 'type': 'commandExecution', 'status': 'inProgress', 'command': 'pwd'}}})
        self.assertEqual(event['state'], 'running')

    def test_ambiguous_action_send_is_not_replayed_on_completion(self):
        calls = []
        self.journal.send_action = lambda content, message_id: calls.append(content)
        for method in ('item/started', 'item/completed'):
            self.journal.emit(codex_event({'method': method, 'params': {'item': {
                'id': 'bash1', 'type': 'commandExecution', 'command': 'pwd'}}}))
        self.assertEqual(len(calls), 1)

    def test_public_text_segments_are_ordered_deduplicated_and_not_repeated_at_end(self):
        for identity, phase, content in [('a', 'commentary', 'checking now'), ('b', 'final_answer', 'fixed it')]:
            event = codex_event({'method': 'item/completed', 'params': {'item': {
                'id': identity, 'type': 'agentMessage', 'phase': phase, 'text': content}}})
            self.journal.emit(event)
            self.journal.emit(event)
        self.assertEqual(self.sent, ['checking now', 'fixed it'])
        self.assertIsNone(self.journal.remaining('checking now\n\nfixed it'))
        self.assertEqual(self.journal.remaining('checking now\n\nfixed it\n\n⚠️ partial'), '⚠️ partial')

    def test_failed_text_holds_later_text_and_recovers_only_the_missing_segments(self):
        outcomes = iter([True, False])
        calls = []
        self.journal.send_text = lambda content, first: calls.append(content) or next(outcomes)
        for identity, content in [('a', 'one'), ('b', 'two'), ('c', 'three')]:
            self.journal.emit({'kind': 'text', 'id': identity, 'text': content})
        self.assertEqual(calls, ['one', 'two'])
        self.assertEqual(self.journal.remaining('one\n\ntwo\n\nthree'), 'two\n\nthree')

    def test_pi_and_opencode_actions_share_command_file_and_result_rules(self):
        records = [
            pi_event({'type': 'tool_execution_start', 'toolCallId': 'pi1', 'toolName': 'bash', 'args': {'command': 'pwd'}}),
            pi_event({'type': 'tool_execution_end', 'toolCallId': 'pi1', 'toolName': 'bash', 'result': {'content': [{'type': 'text', 'text': '/repo'}]}}),
            cli_event('opencode', {'type': 'tool_use', 'part': {'id': 'oc1', 'tool': 'edit', 'state': {
                'status': 'completed', 'input': {'filePath': '/repo/a.py', 'oldString': 'before', 'newString': 'after'}}}}),
        ]
        for event in records:
            self.journal.emit(event)
        self.assertEqual(len(self.actions), 3)
        self.assertIn('command:\npwd', self.actions[1][1])
        self.assertIn('output:\n/repo', self.actions[1][1])
        self.assertIn('/repo/a.py', self.actions[2][1])
        self.assertIn('before', self.actions[2][1])
        self.assertIn('after', self.actions[2][1])

    def test_long_commands_and_diffs_are_split_instead_of_discarded(self):
        diff = '+ 中文😀\n' * 1200
        self.journal.emit(codex_event({'method': 'item/completed', 'params': {'item': {
            'id': 'file1', 'type': 'fileChange', 'status': 'completed',
            'changes': [{'path': '/repo/a.py', 'kind': {'type': 'update'}, 'diff': diff}]}}}))
        combined = ''.join(content for _, content in self.actions)
        self.assertIn(diff.strip(), combined)
        self.assertGreater(len(self.actions), 1)
        for _, content in self.actions:
            self.assertLessEqual(len(content.encode('utf-16-le')) // 2, 3900)

    def test_reasoning_and_credentials_do_not_become_public_progress(self):
        self.journal.emit(codex_event({'method': 'item/completed', 'params': {'item': {
            'id': 'reason1', 'type': 'reasoning', 'summary': ['PRIVATE_SENTINEL']}}}))
        self.journal.emit(pi_event({'type': 'message_end', 'message': {'role': 'assistant', 'content': [
            {'type': 'thinking', 'text': 'PRIVATE_SENTINEL'}, {'type': 'text', 'text': 'public'}]}}))
        self.journal.emit(pi_event({'type': 'tool_execution_start', 'toolCallId': 'p1', 'toolName': 'bash',
                                   'args': {'command': 'API_KEY=SECRET_SENTINEL python3 build.py'}}))
        outbound = json.dumps(self.sent + self.actions)
        self.assertNotIn('PRIVATE_SENTINEL', outbound)
        self.assertNotIn('SECRET_SENTINEL', outbound)
        self.assertIn('python3 build.py', outbound)

    def test_opencode_v1_v2_snapshots_keep_baseline_and_streaming_text_out(self):
        for v2 in (False, True):
            with self.subTest(v2=v2):
                meta = {'id': 'new', 'role': 'assistant', 'type': 'assistant', 'time': {'created': 2}}
                parts = [{'type': 'reasoning', 'id': 'private', 'text': 'private'},
                         {'type': 'text', 'id': 'text1', 'text': 'public'},
                         {'type': 'tool', 'id': 'tool1', 'tool': 'bash', 'name': 'bash',
                          'state': {'input': {'command': 'pwd'}, 'status': 'running'}}]
                message = {**meta, 'content': parts} if v2 else {'info': meta, 'parts': parts}
                response = {'data': [message]} if v2 else [message]
                self.assertEqual([e['kind'] for e in opencode_snapshot(response, set(), v2)], ['action'])
                self.assertEqual(list(opencode_snapshot(response, {'new'}, v2)), [])
                meta['time']['completed'] = 3
                self.assertEqual([e['kind'] for e in opencode_snapshot(response, set(), v2)], ['text', 'action'])


if __name__ == '__main__':
    unittest.main()
