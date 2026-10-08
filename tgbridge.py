#!/usr/bin/env python3
"""tgbridge - minimal Telegram bridge for a local CLI agent.

Bot API long-poll -> gate (chat allowlist + sender policy + group trigger)
-> agent run (per-chat session) -> reply to source chat.

Architecture: the poll loop never blocks. Slash commands are answered inline;
prompts are consumed serially per chat, with independent chats in parallel. Agent stdout
is streamed live via Popen, so the status message shows a real-time tool
trail and answer preview (claudegram/xhyu/OpenClaw pattern).

Extra surfaces: `tgbridge.py --send <chat_id> <text>` lets the agent itself
post to allowlisted chats (Telegram-Bridge-MCP idea, no MCP protocol).

Outbound rendering borrows from Hermes' own gateway (hermes-agent sources):
UTF-16-aware chunk limits, inline-code split avoidance, GFM table
conversion, placeholder-stashed HTML conversion, a clean-markup plain-text
fallback, fence-language carry across chunks, one-element blockquote
merging (incl. expandable), and native bullet markers.
"""

import html
from functools import wraps
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from tgbridge_core.rendering import (
    _balanced,
    _strip_html_markup,
    _wrap_markdown_tables,
    md_to_html,
    split_chunks,
)
from tgbridge_core.health import (
    classify_network_error,
    now_iso,
    polling_health,
    proxy_diagnostics,
    systemd_notify,
)
from tgbridge_core.storage import ensure_private_dir, load_json, save_json
from tgbridge_core.chat_runtime import ChatDispatcher
from tgbridge_core.runtime import LOADED_CODE, source_identity
from tgbridge_core.context import reply_context
from tgbridge_core import telegram_io, execution, opencode_transport, codex_transport, pi_transport, ingress
from tgbridge_core.commands import COMMAND_NAMES, botcmd, command_menu, command_help, unsupported_command
from tgbridge_core.ownership import AlreadyRunning, ProcessLease
from tgbridge_core.inbox import Inbox, InboxError
from tgbridge_core.questions import Questions
from tgbridge_core.progress import Journal, activity_body, short_preview, cli_event, codex_event, opencode_snapshot, pi_event, redact
from tgbridge_core.runners import (
    AUTO_OPENCODE_GO_MODEL,
    RUNNERS,
    SERVER_RUNNERS,
    RunnerError,
    _bin,
    apply_runner_policy,
    classify_run_error,
    discover_opencode_go_models,
    fallback_chain,
    pi_binary,
    probe_runners,
    resolve_model,
    trail_line,
)

CONFIG_DIR = os.path.expanduser("~/.config/tgbridge")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")
AUDIT_PATH = os.path.join(CONFIG_DIR, "audit.jsonl")
HEALTH_PATH = os.path.join(CONFIG_DIR, "health.json")
HEALTH_OWNER_PID = None
OPENCODE_SERVER = os.environ.get("OPENCODE_SERVER", "http://localhost:4096")
RUN_TIMEOUT_S = 900
RUN_MAX_S = 43200
SERVER_POLL_S = 0.5
SERVER_QUIET_S = 0.75
INPUT_DEBOUNCE_S = 1.5
CHUNK = 3900
CANCEL_MSG = "🛑 cancelled by user"

PROMPT_Q = queue.Queue()
STATE_LOCK = threading.Lock()
AUDIT_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()
INGRESS_LOCK = threading.Lock()
CODEX_WRITE_LOCK = threading.Lock()
PI_RPC_WRITE_LOCK = threading.Lock()
PENDING_PROMPTS: dict = {}
CHAT_DISPATCHER = None
RUN_STATE: dict = {
    "busy": False,
    "current": None,
    "proc": None,
    "cancel": False,
    "server_sid": None,
    "server_directory": None,
    "server_url": None,
    "server_api": None,
    "server_run_id": 0,
    "cli_run_id": 0,
    "codex_thread_id": None,
    "codex_turn_id": None,
    "codex_run_id": 0,
    "codex_steers": {},
    "pi_sid": None,
    "pi_run_id": 0,
    "pi_request_id": 0,
    "pi_steers": {},
    "steer_count": 0,
    "steer_pending": 0,
    "steer_errors": [],
    "run_started": None,
    "last_progress": None,
}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def ensure_private_storage():
    ensure_private_dir(CONFIG_DIR)
    for path in (
        CONFIG_PATH,
        STATE_PATH,
        AUDIT_PATH,
        HEALTH_PATH,
        os.path.join(CONFIG_DIR, "tgbridge.log"),
    ):
        if os.path.isfile(path):
            os.chmod(path, 0o600)


def audit(event, **fields):
    """Append-only JSONL trail of bridge actions (claude-code-telegram pattern)."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
    try:
        ensure_private_dir(CONFIG_DIR)
        with AUDIT_LOCK:
            fd = os.open(AUDIT_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.chmod(AUDIT_PATH, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def update_health(**fields):
    # One snapshot per service lifetime. Prior stop/crash details belong in
    # the audit log, not alongside this process's current healthy status.
    health = {} if fields.get("status") == "starting" else load_json(HEALTH_PATH, {})
    health.update(fields)
    if HEALTH_OWNER_PID is not None:
        if os.getpid() != HEALTH_OWNER_PID:
            raise RuntimeError("non-owner process cannot write service health")
        health["pid"] = HEALTH_OWNER_PID
    health["updated_at"] = now_iso()
    save_json(HEALTH_PATH, health)
    return health


# --- Pi RPC transport -------------------------------------------------------
#
# Pi's `--mode json` is one-shot (the CLI adapter in tgbridge_core/runners.py):
# text deltas can only be re-rendered into a throwaway status line and every
# `message_end` overwrites the previous segment, so a long multi-step turn
# reaches the chat as one final block. `--mode rpc` is the same session-event
# stream on a long-lived JSONL child, plus the commands this bridge needs:
# every completed assistant segment is delivered as its own chat message,
# `steer` injects a human message into the live turn (after the current tool
# call, before the next LLM call), and `clear_queue`/`abort` stop it cleanly.

PI_RPC_STATE_CMD = "tgbridge-state"
PI_RPC_PROMPT_CMD = "tgbridge-prompt"
PI_RPC_SETUP_TIMEOUT = 30
PI_RPC_PROMPT_TIMEOUT = 60


def _bind(function):
    """Bind one implementation to this runtime, preserving tgbridge's API."""
    @wraps(function)
    def call(*args, **kwargs):
        return function(sys.modules[__name__], *args, **kwargs)
    call._runtime_impl = function
    return call


# Telegram Bot API I/O, media and question-card delivery.
api = _bind(telegram_io.api)
react = _bind(telegram_io.react)
send = _bind(telegram_io.send)
send_retry = _bind(telegram_io.send_retry)
preserve_unconfirmed = _bind(telegram_io.preserve_unconfirmed)
progress_journal = _bind(telegram_io.progress_journal)
publish_progress = _bind(telegram_io.publish_progress)
publish_snapshot = _bind(telegram_io.publish_snapshot)
_post = _bind(telegram_io._post)
announce_all = _bind(telegram_io.announce_all)
typing_loop = _bind(telegram_io.typing_loop)
edit_status = _bind(telegram_io.edit_status)
question_broker = _bind(telegram_io.question_broker)
multipart = _bind(telegram_io.multipart)
transcribe = _bind(telegram_io.transcribe)
save_attachment = _bind(telegram_io.save_attachment)
send_document = _bind(telegram_io.send_document)

# Run lifecycle, native sessions, failover, steering admission and scheduling.
signal_run_process = _bind(execution.signal_run_process)
kill_after = _bind(execution.kill_after)
run_timeout = _bind(execution.run_timeout)
run_max = _bind(execution.run_max)
start_run_clock = _bind(execution.start_run_clock)
mark_run_progress = _bind(execution.mark_run_progress)
run_expiry = _bind(execution.run_expiry)
input_debounce = _bind(execution.input_debounce)
outbox_dir = _bind(execution.outbox_dir)
home_chat = _bind(execution.home_chat)
deliverable_answer = _bind(execution.deliverable_answer)
telegram_session_name = _bind(execution.telegram_session_name)
run_agent = _bind(execution.run_agent)
effective_run_config = _bind(execution.effective_run_config)
runner_session = _bind(execution.runner_session)
store_runner_session = _bind(execution.store_runner_session)
clear_runner_sessions = _bind(execution.clear_runner_sessions)
question_owner = _bind(execution.question_owner)
run_one = _bind(execution.run_one)
run_with_fallbacks = _bind(execution.run_with_fallbacks)
runner_mode = _bind(execution.runner_mode)
resolve_runner_mode = _bind(execution.resolve_runner_mode)
_begin_steer = _bind(execution._begin_steer)
should_steer = _bind(execution.should_steer)
_steer_fallback = _bind(execution._steer_fallback)
worker = _bind(execution.worker)
fire_at = _bind(execution.fire_at)
rearm_at = _bind(execution.rearm_at)

# OpenCode HTTP transports and safe-boundary steering.
_server_call = _bind(opencode_transport._server_call)
server_url = _bind(opencode_transport.server_url)
server_ok = _bind(opencode_transport.server_ok)
server_event = _bind(opencode_transport.server_event)
server_messages = _bind(opencode_transport.server_messages)
_server_poll_interval = _bind(opencode_transport._server_poll_interval)
_steer_deliver = _bind(opencode_transport._steer_deliver)
_steer_deliver_v2 = _bind(opencode_transport._steer_deliver_v2)
run_agent_server_v1 = _bind(opencode_transport.run_agent_server_v1)
opencode_v2_supported = _bind(opencode_transport.opencode_v2_supported)
_opencode_model = _bind(opencode_transport._opencode_model)
server_messages_v2 = _bind(opencode_transport.server_messages_v2)
run_agent_server_v2 = _bind(opencode_transport.run_agent_server_v2)
run_agent_server = _bind(opencode_transport.run_agent_server)

# Codex app-server transport, question responses and turn steering.
_codex_rpc_write = _bind(codex_transport._codex_rpc_write)
_codex_steer_deliver = _bind(codex_transport._codex_steer_deliver)
codex_app_server_ok = _bind(codex_transport.codex_app_server_ok)
_codex_read_events = _bind(codex_transport._codex_read_events)
_codex_wait_response = _bind(codex_transport._codex_wait_response)
_codex_item_trail = _bind(codex_transport._codex_item_trail)
_codex_handle_steer_response = _bind(codex_transport._codex_handle_steer_response)
_codex_user_question = _bind(codex_transport._codex_user_question)
run_codex_app_server = _bind(codex_transport.run_codex_app_server)

# Pi RPC transport, native UI responses and streaming reconciliation.
pi_rpc_ok = _bind(pi_transport.pi_rpc_ok)
_pi_rpc_write = _bind(pi_transport._pi_rpc_write)
_pi_rpc_read_events = _bind(pi_transport._pi_rpc_read_events)
_pi_rpc_wait_response = _bind(pi_transport._pi_rpc_wait_response)
_pi_message_text = _bind(pi_transport._pi_message_text)
_pi_rpc_decline_ui = _bind(pi_transport._pi_rpc_decline_ui)
_pi_rpc_ui = _bind(pi_transport._pi_rpc_ui)
_pi_rpc_abort = _bind(pi_transport._pi_rpc_abort)
_pi_send_trailer = _bind(pi_transport._pi_send_trailer)
_pi_steer_deliver = _bind(pi_transport._pi_steer_deliver)
_pi_handle_steer_response = _bind(pi_transport._pi_handle_steer_response)
run_pi_rpc = _bind(pi_transport.run_pi_rpc)

# Authorized Telegram input, burst coalescing and bridge-control dispatch.
unpack_entry = _bind(ingress.unpack_entry)
_flush_prompt_batch = _bind(ingress._flush_prompt_batch)
defer_prompt = _bind(ingress.defer_prompt)
merge_open_burst = _bind(ingress.merge_open_burst)
is_authorized = _bind(ingress.is_authorized)
apply_sender_instructions = _bind(ingress.apply_sender_instructions)
handle_update = _bind(ingress.handle_update)

SERVER_RUNNERS.update({
    'opencode': {'run': run_agent_server, 'healthy': server_ok, 'feature': 'native same-turn steer (v2)'},
    'codex': {'run': run_codex_app_server, 'healthy': codex_app_server_ok, 'feature': 'same-turn turn/steer'},
    'pi': {'run': run_pi_rpc, 'healthy': pi_rpc_ok, 'feature': 'segmented replies + live steer (rpc)'},
})


def startup_smoke():
    """Side-effect-free checks safe to run before every service start."""
    if md_to_html("**ok**")[0] != "<b>ok</b>":
        raise RuntimeError("render smoke failed")
    if split_chunks("a" * 25, limit=10) != ["a" * 10, "a" * 10, "a" * 5]:
        raise RuntimeError("chunk smoke failed")
    if runner_mode({}) != "cli" or set(RUNNERS) != {"opencode", "codex", "pi"}:
        raise RuntimeError("runner registry smoke failed")


def selftest():
    """Run the exhaustive suite on demand and in the normal test runner."""
    from tgbridge_core.selftest import run_selftest

    run_selftest(sys.modules[__name__])


def cli_send(args):
    """Agent-initiated outbound: tgbridge.py --send <chat_id> <text>."""
    if len(args) < 2:
        sys.exit("usage: tgbridge.py --send <chat_id> <text>")
    ensure_private_storage()
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit(f"missing config {CONFIG_PATH}")
    try:
        chat_id = int(args[0])
    except ValueError:
        sys.exit("chat_id must be an integer")
    if chat_id not in cfg["allowed_chats"]:
        sys.exit(f"chat {chat_id} not in allowed_chats — refusing")
    send(cfg["bot_token"], chat_id, args[1])
    audit("send", chat_id=chat_id, chars=len(args[1]))
    log(f"--send -> {chat_id} ({len(args[1])} chars)")


def doctor_report():
    """Return a redacted, cross-platform runtime readiness report."""
    ensure_private_storage()
    cfg = load_json(CONFIG_PATH, None)
    report = {
        "ok": False,
        "checked_at": now_iso(),
        "config_path": CONFIG_PATH,
        "state_path": STATE_PATH,
        "health_path": HEALTH_PATH,
        "proxy": proxy_diagnostics(),
        "health": load_json(HEALTH_PATH, {}),
    }
    report["service"] = polling_health(report["health"])
    expected_code = source_identity(os.path.dirname(os.path.abspath(__file__)))
    loaded_code = report["health"].get("loaded_code") or {}
    report["code"] = {
        "expected": expected_code,
        "loaded": loaded_code,
        "ok": bool(expected_code.get("source_sha256")
                   and expected_code == loaded_code),
    }
    if not cfg:
        report["config"] = {"ok": False, "error": "missing or invalid config"}
        return report
    report["config"] = {"ok": True}
    state_parent = os.path.dirname(STATE_PATH)
    state_writable = os.access(state_parent, os.W_OK) and (
        not os.path.exists(STATE_PATH) or os.access(STATE_PATH, os.W_OK)
    )
    report["state"] = {"writable": state_writable}

    cfg = effective_run_config(cfg, load_json(STATE_PATH, {}))
    rname = cfg.get("runner", "opencode")
    mode = resolve_runner_mode(cfg, rname)
    runner_check = {"name": rname, "mode": mode, "ok": False}
    try:
        if rname not in RUNNERS:
            raise RunnerError(f"unknown runner {rname!r}")
        RUNNERS[rname](None, "doctor", resolve_model(cfg, rname) or None)
        if mode == "server":
            adapter = SERVER_RUNNERS.get(rname)
            if not adapter:
                raise RunnerError(f"runner {rname!r} has no server transport")
            if not adapter["healthy"](cfg):
                raise RunnerError("server transport unavailable")
        elif mode != "cli":
            raise RunnerError(f"unknown runner_mode {mode!r}")
        runner_check["ok"] = True
    except Exception as e:
        runner_check["error"] = str(e)[:200]
    report["runner"] = runner_check
    fallback_checks = []
    for step_runner, step_model in fallback_chain(cfg):
        step_mode = resolve_runner_mode(cfg, step_runner)
        check = {
            "runner": step_runner,
            "model": step_model or "runner default",
            "mode": step_mode,
            "ok": False,
        }
        try:
            if step_runner not in RUNNERS:
                raise RunnerError(f"unknown runner {step_runner!r}")
            RUNNERS[step_runner](None, "doctor", step_model or None)
            if step_mode == "server":
                adapter = SERVER_RUNNERS.get(step_runner)
                if not adapter:
                    raise RunnerError(
                        f"runner {step_runner!r} has no server transport"
                    )
                if not adapter["healthy"](dict(cfg, runner=step_runner)):
                    raise RunnerError("server transport unavailable")
            elif step_mode != "cli":
                raise RunnerError(f"unknown runner_mode {step_mode!r}")
            check["ok"] = True
        except Exception as e:
            check["error"] = str(e)[:200]
        fallback_checks.append(check)
    report["fallbacks"] = fallback_checks

    telegram_error = {}
    me = api(cfg["bot_token"], "getMe", _error=telegram_error)
    telegram_ok = bool(me and me.get("ok"))
    report["telegram"] = {
        "ok": telegram_ok,
        "bot_username": (me.get("result") or {}).get("username")
        if telegram_ok
        else None,
        "error": telegram_error or None,
    }
    report["ok"] = bool(
        state_writable and runner_check["ok"] and telegram_ok
        and report["service"]["ok"] and report["code"]["ok"]
    )
    return report


def cli_doctor():
    report = doctor_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


class BridgeStop(BaseException):
    """Raised by the SIGTERM/SIGINT handler — a graceful stop, not a crash."""


def on_stop(signum, frame):
    raise BridgeStop(signum)


def run(cfg):
    global CHAT_DISPATCHER
    systemd_notify("WATCHDOG=1")
    update_health(
        status="starting",
        pid=os.getpid(),
        started_at=now_iso(),
        loaded_code=dict(LOADED_CODE),
        consecutive_poll_failures=0,
    )
    state = load_json(STATE_PATH, {})
    inbox = cfg.get("_inbox")
    if inbox is None:
        inbox = cfg["_inbox"] = Inbox(os.path.join(CONFIG_DIR, "inputs.json"))
    if cfg.get('_questions') is None:
        cfg['_questions'] = question_broker(cfg)
    if not cfg.get("capture_group_context", True):
        state.pop("context", None)
        state.pop("hints", None)
    me = None
    for attempt in range(1, 6):
        systemd_notify("WATCHDOG=1")
        me = api(cfg["bot_token"], "getMe")
        if me and me.get("ok"):
            break
        # api() returns None for both a dead token and a transient network
        # fault — a single attempt must not misreport a blip as "bad token".
        # Probe the raw HTTP status: only 401 means the token is wrong.
        try:
            probe = urllib.request.Request(
                f"https://api.telegram.org/bot{cfg['bot_token']}/getMe",
                data=b"",
            )
            with urllib.request.urlopen(probe, timeout=15) as r:
                json.load(r)
            log(f"getMe attempt {attempt}/5: no ok payload, retrying")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit("getMe failed: bad token (401 Unauthorized)")
            log(f"getMe attempt {attempt}/5 transient http {e.code}, retrying")
        except BridgeStop:
            raise
        except Exception as e:
            log(f"getMe attempt {attempt}/5 transient {type(e).__name__}")
        me = None
        time.sleep(3)
    if not me or not me.get("ok"):
        sys.exit(
            "getMe failed after 5 retries: Telegram unreachable (token not verified)"
        )
    state["bot_username"] = me["result"]["username"]
    save_json(STATE_PATH, state)
    menu = api(
        cfg["bot_token"], "setMyCommands",
        commands=json.dumps(command_menu(), ensure_ascii=False),
    )
    if not menu or not menu.get("ok"):
        log("command menu registration failed; /help remains available")
    log(f"tgbridge up as @{state['bot_username']}, chats={cfg['allowed_chats']}")
    audit("startup", pid=os.getpid(), loaded_code=LOADED_CODE)
    update_health(status="polling", bot_username=state["bot_username"])
    if cfg.get("runner") == "codex" and cfg.get("codex_yolo"):
        log("WARNING codex_yolo=true: Telegram prompts have unsandboxed OS access")

    # Startup sanity: a missing runner binary must not kill the bridge —
    # commands still work; warn once in the home chat, runs report the error.
    # Full availability probe is logged here and served live via /runners.
    probe = probe_runners()
    log(
        "runner probe: "
        + ", ".join(
            f"{name}={'ok' if p['available'] else 'missing'}"
            for name, p in probe.items()
        )
    )
    rname = cfg.get("runner", "opencode")
    hc = home_chat(cfg)
    warn = None
    if rname not in RUNNERS:
        warn = (
            f"⚠️ unknown runner {rname!r} (available: {', '.join(sorted(RUNNERS))}) "
            "— commands work, runs will fail until config.json is fixed"
        )
    else:
        try:
            RUNNERS[rname](None, "sanity", None)
        except RunnerError as e:
            warn = f"⚠️ startup check: {e} — commands work, runs will error"
        except Exception as e:
            log(f"startup runner check: {e}")
    mode = resolve_runner_mode(cfg, rname)
    if not warn and mode not in ("cli", "server"):
        warn = f"⚠️ unknown runner_mode {mode!r} (available: cli, server)"
    if not warn and mode == "server":
        adapter = SERVER_RUNNERS.get(rname)
        if not adapter:
            warn = f"⚠️ runner {rname!r} has no server transport — use runner_mode='cli'"
        elif not adapter["healthy"](cfg):
            where = f" at {server_url(cfg)}" if rname == "opencode" else ""
            warn = (
                f"⚠️ {rname} server transport unavailable{where} — "
                "start/install it before prompting or use runner_mode='cli'"
            )
    if warn and hc:
        send(cfg["bot_token"], hc, warn)

    ready, interrupted = inbox.recover()
    for entry in ready:
        if is_authorized(cfg, entry["chat_id"], entry.get("chat_type"), entry.get("user_id")):
            PROMPT_Q.put(entry)
    for entry in interrupted:
        if is_authorized(cfg, entry["chat_id"], entry.get("chat_type"), entry.get("user_id")):
            send_retry(cfg, entry["chat_id"],
                       f"⚠️ Task interrupted. Review and continue: /resume {entry['id']}")
    CHAT_DISPATCHER = cfg['_chat_dispatcher'] = ChatDispatcher(sys.modules[__name__], cfg, state)
    worker_t = threading.Thread(target=CHAT_DISPATCHER.consume, daemon=True)
    worker_t.start()
    rearm_at(cfg, state)

    offset = state.get("offset")
    backoff = 3
    poll_failures = 0
    try:
        failure_exit_threshold = max(0, int(cfg.get("poll_failure_exit_threshold", 20)))
    except (TypeError, ValueError):
        failure_exit_threshold = 20
    systemd_notify("READY=1\nSTATUS=Telegram bridge polling")
    while True:
        # Feed only from this control loop, never an independent timer which
        # would falsely report a wedged poll/handler as healthy (Hermes pattern).
        systemd_notify("WATCHDOG=1")
        if not worker_t.is_alive():
            log("worker thread died — respawning")
            audit("worker_respawn")
            announce_all(cfg, "💀 bridge worker thread died — respawned")
            worker_t = threading.Thread(target=CHAT_DISPATCHER.consume, daemon=True)
            worker_t.start()
        CHAT_DISPATCHER.supervise()
        params = {"timeout": 50, "allowed_updates": json.dumps(["message", "callback_query"])}
        if offset:
            params["offset"] = offset
        poll_error = {}
        res = api(cfg["bot_token"], "getUpdates", _error=poll_error, **params)
        if not res or not res.get("ok"):
            poll_failures += 1
            update_health(
                status="unhealthy",
                consecutive_poll_failures=poll_failures,
                last_poll_error=poll_error
                or {
                    "kind": "invalid_response",
                    "message": "Telegram returned no ok payload",
                },
            )
            if failure_exit_threshold and poll_failures >= failure_exit_threshold:
                raise RuntimeError(
                    f"Telegram polling unhealthy for {poll_failures} consecutive attempts"
                )
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        poll_failures = 0
        backoff = 3
        update_health(
            status="healthy",
            last_poll_ok_at=now_iso(),
            consecutive_poll_failures=0,
            last_poll_error=None,
        )
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            try:
                handle_update(cfg, state, upd)
            except (BridgeStop, InboxError):
                # Do not advance offset when durable acceptance failed.
                raise
            except Exception as e:
                log(f"update {upd.get('update_id')} handler error: {e}")
                m = upd.get("message") or {}
                cid = (m.get("chat") or {}).get("id")
                if cid:
                    send(cfg["bot_token"], cid, f"⚠️ bridge error: {e}")
            with STATE_LOCK:
                state["offset"] = offset
                save_json(STATE_PATH, state)


def main():
    global HEALTH_OWNER_PID
    if "--doctor" in sys.argv:
        sys.exit(cli_doctor())
    if "--selftest" in sys.argv:
        selftest()
        return
    startup_smoke()  # fast and side-effect-free; full suite is `--selftest`

    ensure_private_storage()
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit(f"missing config {CONFIG_PATH}")
    lease = ProcessLease(CONFIG_DIR)
    try:
        lease.acquire()
    except AlreadyRunning as e:
        # Never announce, poll or overwrite the active owner's health/state.
        sys.exit(str(e))
    HEALTH_OWNER_PID = os.getpid()
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    try:
        run(cfg)
    except BridgeStop:
        systemd_notify("STOPPING=1")
        update_health(status="stopped", stopped_at=now_iso())
        owners = CHAT_DISPATCHER.close() if CHAT_DISPATCHER else [sys.modules[__name__]]
        for owner in owners:
            execution.stop_runtime(owner)
        audit("stop", reason="signal")
        announce_all(cfg, "💀 bridge stopping")
        log("bridge stopped by signal")
        sys.exit(0)
    except SystemExit as e:
        update_health(status="failed", exit_error=str(e.code or ""))
        announce_all(cfg, f"💀 bridge exited: {e.code or ''}".rstrip())
        raise
    except BaseException as e:
        update_health(status="crashed", crash_error=str(e)[:300])
        audit("crash", err=str(e)[:300])
        announce_all(cfg, f"💀 bridge crashed: {e} — restarting")
        sys.exit(1)
    finally:
        HEALTH_OWNER_PID = None
        lease.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--send":
        cli_send(sys.argv[2:])
    else:
        main()
