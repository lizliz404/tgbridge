import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

import tgbridge
from tgbridge_core import execution


SCHEMA = """
create table sessions (id text, working_directory text, backend_type text,
    model text, agent_mode text, created_at integer, last_activity_at integer,
    title text, main_chain_id integer, shell_last_seen_index integer,
    cogs_json text, workspace_dirs text, hidden integer, metadata text);
create table tool_call_state (session_id text, tool_call_id text,
    tool_call_json text, tool_call_update_json text, source_node_ids text);
create table message_nodes (row_id integer primary key autoincrement,
    session_id text, node_id integer, parent_node_id integer,
    chat_message text, created_at integer, metadata text);
create table prompt_history (id integer primary key autoincrement,
    content text, timestamp integer, session_id text, is_shell integer);
"""


class FakeProc:
    """poll() stays None until `done` is set, mimicking a live child."""
    def __init__(self):
        self.done = threading.Event()
        self.returncode = None

    def poll(self):
        return self.returncode if self.done.is_set() else None


class DevinWatchTest(unittest.TestCase):
    def _db(self, path):
        db = sqlite3.connect(path)
        db.executescript(SCHEMA)
        return db

    def test_tool_calls_and_preview_reach_live(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = os.path.join(root, 'sessions.db')
            db = self._db(db_path)
            now = int(time.time())
            db.execute(
                "insert into sessions (id, working_directory, created_at)"
                " values (?,?,?)", ('sess-1', root, now))
            db.execute(
                "insert into prompt_history (content, timestamp, session_id)"
                " values (?,?,?)", ('fix the flaky test please', now, 'sess-1'))
            db.commit()

            events = []
            edits = []
            live = {'trail': [], 'start': time.time()}
            proc = FakeProc()
            cfg = {'workdir': root, 'devin_watch_poll_s': 0.05}
            with mock.patch.dict(os.environ, {'DEVIN_STATE_DB': db_path}), \
                 mock.patch.object(
                     tgbridge, 'publish_progress',
                     lambda c, l, ev: events.append(ev)), \
                 mock.patch.object(
                     tgbridge, 'edit_status',
                     lambda c, l, **kw: edits.append(l.get('preview'))):
                watcher = threading.Thread(
                    target=execution.devin_progress_watch,
                    args=(tgbridge, cfg, live, proc, time.time(),
                          'fix the flaky test please'),
                    daemon=True)
                watcher.start()
                time.sleep(0.25)
                db.execute(
                    "insert into tool_call_state"
                    " (session_id, tool_call_id, tool_call_json,"
                    "  tool_call_update_json) values (?,?,?,?)",
                    ('sess-1', 'call-1',
                     json.dumps({'toolCallId': 'call-1', 'title': 'Ran ls',
                                 'kind': 'execute',
                                 'content': [{'type': 'content', 'content': {
                                     'type': 'resource', 'resource': {
                                         'mimeType': 'text/x-shellscript',
                                         'text': 'ls -la'}}}]}),
                     json.dumps({'status': 'in_progress'})))
                db.execute(
                    "insert into message_nodes"
                    " (session_id, node_id, parent_node_id, chat_message,"
                    "  created_at) values (?,?,?,?,?)",
                    ('sess-1', 1, 0,
                     json.dumps({'role': 'assistant',
                                 'content': 'Reading the test file now'}),
                     now))
                db.commit()
                time.sleep(0.35)
                db.execute(
                    "update tool_call_state set tool_call_update_json=?"
                    " where tool_call_id='call-1'",
                    (json.dumps({'status': 'completed'}),))
                db.commit()
                time.sleep(0.35)
                proc.done.set()
                proc.returncode = 0
                watcher.join(timeout=5)
            db.close()

            states = [
                (e['id'], e['state']) for e in events if e['kind'] == 'action'
            ]
            self.assertIn(('call-1', 'running'), states)
            self.assertIn(('call-1', 'completed'), states)
            self.assertEqual(
                events[0]['inputs'].get('command'), 'ls -la')
            texts = [e for e in events if e['kind'] == 'text']
            self.assertEqual(len(texts), 1)
            self.assertEqual(texts[0]['text'], 'Reading the test file now')
            self.assertTrue(
                tgbridge.RUN_STATE.get('last_progress'),
                'watcher must renew the idle lease on new rows')


if __name__ == '__main__':
    unittest.main()
