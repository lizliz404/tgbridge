#!/usr/bin/env python3
"""Opt-in authenticated Pi acceptance, never discovered by offline unittest.

Uses existing Pi auth/model defaults. Native sessions and two read-only tool
operations are isolated; every Telegram API/send/audit call is intercepted.
Report contains delivery events, transcript evidence and behavior, no secrets.
"""
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tgbridge


def main():
    import shutil
    binary = shutil.which('pi')
    if not binary:
        raise SystemExit('Pi not found')
    root = Path(tempfile.mkdtemp(prefix='tgbridge-pi-acceptance-'))
    root.chmod(0o700)
    (root / 'fixture.txt').write_text('read-only QA fixture\n')
    wrapper = root / 'pi'
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(binary)
                       + ' --no-extensions --no-skills --no-prompt-templates --no-context-files "$@"\n')
    wrapper.chmod(0o700)
    texts, calls, edits, records = [], [], [], []
    submitted = False
    token = 'STEER_BEHAVIOR_QA_7264'
    cfg = {'runner': 'pi', 'runner_mode': 'server', 'bot_token': 'fixture-not-a-real-bot',
           'workdir': str(root), 'run_timeout_s': 90, 'run_max_s': 120}
    live = {'chat_id': 42, 'reply_to': 1, 'status_id': 5, 'trail': [], 'start': time.time()}
    original = tgbridge.publish_progress

    def publish(cfg, live, event):
        nonlocal submitted
        original(cfg, live, event)
        if event and event.get('kind') == 'action':
            calls.append({'id': event.get('id'), 'state': event.get('state'), 'label': event.get('label')})
            if event.get('label') == 'bash' and event.get('state') == 'running' and not submitted:
                submitted = True
                with tgbridge.RUN_LOCK:
                    sid = tgbridge.RUN_STATE['pi_sid']
                    run_id = tgbridge.RUN_STATE['pi_run_id']
                    tgbridge.RUN_STATE['steer_pending'] += 1
                tgbridge._pi_steer_deliver(cfg, sid, run_id,
                    'Change the final answer: include exactly ' + token + '. Do not run more commands.', 42, 2)

    def api(bot, method, **params):
        if method in ('sendMessage', 'editMessageText'):
            edits.append({'method': method, 'id': params.get('message_id'), 'text': params.get('text')})
        return {'ok': True, 'result': {'message_id': len(edits) + 100}}

    with mock.patch.dict(os.environ, {'PI_BIN': str(wrapper), 'PI_CODING_AGENT_SESSION_DIR': str(root / 'sessions')}), \
         mock.patch.object(tgbridge, 'api', side_effect=api), \
         mock.patch.object(tgbridge, 'send_retry', side_effect=lambda cfg, chat, text, **kw: texts.append(text) or True), \
         mock.patch.object(tgbridge, 'send', return_value=True), \
         mock.patch.object(tgbridge, 'audit', side_effect=lambda event, **kw: records.append(kw) if event == 'tool_action' else None), \
         mock.patch.object(tgbridge, 'publish_progress', side_effect=publish):
        with tgbridge.RUN_LOCK:
            tgbridge.RUN_STATE.update(busy=True, current={'chat': 42, 'runner': 'pi', 'mode': 'server'}, cancel=False)
        try:
            sid, answer, error = tgbridge.run_pi_rpc(cfg, None,
                'This is a read-only transport QA task. Say a short public sentence before each action. '
                'First use read to inspect fixture.txt. Then use bash to run exactly "sleep 5; pwd". '
                'After those two tools, briefly state the result. Do not edit files or run other commands.', live)
        finally:
            with tgbridge.RUN_LOCK:
                tgbridge.RUN_STATE.update(busy=False, current=None, proc=None)
    sessions = list((root / 'sessions').rglob('*.jsonl'))
    user_text = []
    for path in sessions:
        for line in path.read_text().splitlines():
            record = json.loads(line)
            message = record.get('message') or {}
            if message.get('role') == 'user':
                user_text.append(tgbridge._pi_message_text(message))
    report = {'root': str(root), 'session': sid, 'error': error, 'actions': calls,
              'public_segments': texts, 'steer_submitted': submitted,
              'steer_in_native_transcript': any(token in x for x in user_text),
              'steer_changes_answer': token in (answer or ''),
              'final_not_repeated': tgbridge.deliverable_answer(live, answer) is None,
              'action_message_count': sum(e['method'] == 'sendMessage' for e in edits),
              'action_record_count': len(records),
              'single_status_message': bool(edits) and all(e['id'] == 5 for e in edits)}
    (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if (not error and submitted and report['steer_in_native_transcript']
                 and report['steer_changes_answer'] and len(texts) >= 2
                 and report['action_message_count'] == 0 and report['action_record_count'] >= 2
                 and report['single_status_message'] and report['final_not_repeated']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
