"""Bounded Linux restart transaction with durable requester-visible receipt.

Explicit operator entry point only: never scheduled implicitly by source edits.
Mac launchd needs a separate adapter/acceptance, not guessed systemd behavior.
"""
import datetime
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .health import now_iso, polling_health
from .runtime import source_identity
from .storage import load_json, save_json


def unit_status(unit):
    result = subprocess.run(['systemctl', '--user', 'show', unit, '--property=MainPID',
                             '--property=ActiveState', '--property=ControlGroup'],
                            capture_output=True, text=True, timeout=10, check=True)
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def owned_children(status):
    group = status.get('ControlGroup')
    if not group or not group.startswith('/') or '..' in Path(group).parts:
        raise RuntimeError('cannot establish service cgroup ownership')
    root = Path('/sys/fs/cgroup') / group.lstrip('/')
    pids = set()
    for path in root.rglob('cgroup.procs'):
        pids.update(int(value) for value in path.read_text().split())
    main = int(status.get('MainPID') or 0)
    return pids - {main}


def notify_requester(cfg, chat, text):
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
        data=urllib.parse.urlencode({'chat_id': chat, 'text': text}).encode())
    # One best-effort attempt; an ambiguous timeout is never blindly resent.
    with urllib.request.urlopen(request, timeout=5) as response:
        return bool(json.load(response).get('ok'))


def restart(directory, source_root, unit, chat, timeout=120):
    directory = Path(directory)
    cfg = load_json(str(directory / 'config.json'), {})
    if chat not in cfg.get('allowed_chats', []):
        raise ValueError('requester chat is not allowlisted')
    if unit not in ('tgbridge.service', 'tgbridge-pi.service'):
        raise ValueError('only existing TGBridge units may be restarted')
    target = source_identity(source_root)
    receipt_path = directory / 'maintenance' / 'restart-result.json'
    receipt = {'status': 'preflight', 'started_at': now_iso(), 'requester_chat': chat,
               'unit': unit, 'target': target}
    def persist(**fields):
        receipt.update(fields)
        save_json(str(receipt_path), receipt, durable=True)
    try:
        persist()
        before = unit_status(unit)
        old_pid = int(before.get('MainPID') or 0)
        if before.get('ActiveState') != 'active' or old_pid <= 0:
            raise RuntimeError('service is not active; inspect startup rather than blindly restart')
        health_before = load_json(str(directory / 'health.json'), {})
        if health_before.get('pid') != old_pid:
            raise RuntimeError('health writer PID differs from supervisor; resolve ownership before restart')
        inputs = load_json(str(directory / 'inputs.json'), {})
        if any(e.get('status') in ('received', 'queued', 'executing', 'steering') for e in inputs.values()):
            raise RuntimeError('unsettled durable inputs present; restart refused at unsafe task boundary')
        if owned_children(before):
            raise RuntimeError('active bridge children present; restart refused at unsafe task boundary')
        state_before = load_json(str(directory / 'state.json'), {})
        sessions = {k: state_before.get(k, {}) for k in ('sessions', 'runner_sessions', 'session_runners')}
        # Recheck immediately before supervisor operation, after disk preflight.
        if owned_children(unit_status(unit)):
            raise RuntimeError('agent started during preflight; restart refused')
        persist(status='restarting', old_pid=old_pid)
        restarted_at = time.time()
        subprocess.run(['systemctl', '--user', 'restart', unit], timeout=35,
                       capture_output=True, check=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            service = unit_status(unit)
            health = load_json(str(directory / 'health.json'), {})
            pid = int(service.get('MainPID') or 0)
            readiness = polling_health(health)
            stamp = health.get('last_poll_ok_at')
            try:
                poll_time = datetime.datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%S%z').timestamp()
            except (TypeError, ValueError):
                poll_time = 0
            if (pid > 0 and pid != old_pid and pid == health.get('pid')
                    and service.get('ActiveState') == 'active' and readiness['ok']
                    and health.get('loaded_code') == target and poll_time >= restarted_at + 50):
                state_after = load_json(str(directory / 'state.json'), {})
                if any(state_after.get(key, {}) != value for key, value in sessions.items()):
                    raise RuntimeError('native session mappings changed during restart acceptance')
                persist(status='succeeded', new_pid=pid, health_poll_at=stamp,
                        finished_at=now_iso(), session_mappings_preserved=True)
                break
            time.sleep(0.5)
        else:
            raise RuntimeError('new owner/version/full poll interval not verified before deadline')
    except Exception as error:
        persist(status='failed', error=type(error).__name__ + ': ' + str(error)[:250], finished_at=now_iso())
    message = ('✅ TGBridge updated. Sessions preserved.'
               if receipt['status'] == 'succeeded' else
               '⚠️ Restart check failed: ' + receipt.get('error', 'unknown'))
    try:
        confirmed = notify_requester(cfg, chat, message)
        persist(feedback='confirmed' if confirmed else 'unconfirmed')
    except Exception as error:
        persist(feedback='unconfirmed', feedback_error=type(error).__name__)
    return receipt
