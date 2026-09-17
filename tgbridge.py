#!/usr/bin/env python3
"""tgbridge - minimal Telegram bridge for a local CLI agent.

Bot API long-poll -> gate (chat allowlist + sender policy + group trigger)
-> agent run (per-chat session) -> reply to source chat.

Architecture: the poll loop never blocks. Slash commands are answered inline;
prompts are enqueued and consumed serially by a worker thread. Agent stdout
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

import json
import os
import queue
import re
import signal
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
    proxy_diagnostics,
)
from tgbridge_core.storage import ensure_private_dir, load_json, save_json
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
    probe_runners,
    resolve_model,
)

CONFIG_DIR = os.path.expanduser("~/.config/tgbridge")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")
AUDIT_PATH = os.path.join(CONFIG_DIR, "audit.jsonl")
HEALTH_PATH = os.path.join(CONFIG_DIR, "health.json")
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
PENDING_PROMPTS: dict = {}
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
    health = load_json(HEALTH_PATH, {})
    health.update(fields)
    health["updated_at"] = now_iso()
    save_json(HEALTH_PATH, health)
    return health


def api(token, method, _error=None, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}", data=data
    )
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=70) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if _error is not None:
                _error.update(kind=classify_network_error(e), message=f"HTTP {e.code}")
            if e.code == 409:
                log(
                    "409 CONFLICT: another poller holds this bot token — is an old bridge still running?"
                )
                time.sleep(15)
                return None
            if e.code == 429 and attempt == 1:
                try:
                    time.sleep(
                        json.loads(body).get("parameters", {}).get("retry_after", 3)
                    )
                except json.JSONDecodeError:
                    time.sleep(3)
                continue
            log(f"api {method} http {e.code}: {body[:150]}")
            return None
        except Exception as e:
            if _error is not None:
                _error.update(kind=classify_network_error(e), message=str(e)[:200])
            log(f"api {method} error: {type(e).__name__}: {e}")
            return None
    return None


def react(cfg, chat_id, message_id, emoji):
    if not cfg.get("reactions", True):
        return
    api(
        cfg["bot_token"],
        "setMessageReaction",
        chat_id=chat_id,
        message_id=message_id,
        reaction=json.dumps([{"type": "emoji", "emoji": emoji}]),
    )


def send(token, chat_id, text, reply_to=None, chunk_limit=CHUNK):
    """Send markdown text rendered as Telegram HTML; any chunk Telegram
    refuses (bad entity, overlong tag) falls back to plain text so a
    formatting bug can never drop the payload. Chunks whose HTML is
    unbalanced locally skip the doomed HTML attempt entirely."""
    ok = True
    in_pre = False
    pre_lang = ""
    text = _wrap_markdown_tables(text or "")
    for i, chunk in enumerate(split_chunks(text, chunk_limit)):
        html, in_pre, pre_lang = md_to_html(chunk, in_pre, pre_lang)
        params = {
            "chat_id": chat_id,
            "text": html,
            "link_preview_options": json.dumps({"is_disabled": True}),
        }
        if _balanced(html):
            params["parse_mode"] = "HTML"
        if i == 0 and reply_to:
            params["reply_parameters"] = json.dumps({"message_id": reply_to})
        res = api(token, "sendMessage", **params) if "parse_mode" in params else None
        if not res or not res.get("ok"):
            log("html send failed — resending chunk as plain text")
            params.pop("parse_mode", None)
            params.pop("link_preview_options", None)
            params["text"] = _strip_html_markup(chunk) or chunk
            res = api(token, "sendMessage", **params)
        if not res or not res.get("ok"):
            ok = False
    return ok


def send_retry(cfg, chat_id, text, reply_to=None):
    """One retry with backoff for must-not-lose payloads (the run's answer).

    A failed sendMessage otherwise silently deletes a 6-minute agent run. If
    both attempts fail: audit + log loudly + persist to undelivered/ so the
    content survives even though Telegram never saw it."""
    limit = cfg.get("chunk") or CHUNK
    for attempt in (1, 2):
        if send(cfg["bot_token"], chat_id, text, reply_to=reply_to, chunk_limit=limit):
            return True
        if attempt == 1:
            time.sleep(2)
    audit("delivery_failed", chat_id=chat_id, chars=len(text or ""))
    log(f"chat={chat_id} DELIVERY FAILED after retry ({len(text or '')} chars)")
    try:
        d = os.path.join(CONFIG_DIR, "undelivered")
        ensure_private_dir(d)
        path = os.path.join(d, time.strftime("%Y%m%d-%H%M%S") + f"-{chat_id}.txt")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text or "")
        os.chmod(path, 0o600)
        log("saved undelivered payload")
    except OSError:
        pass
    return False


def _post(url, data, timeout):
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def announce_all(cfg, text, post=_post):
    """Best-effort death notice to every allowed chat. 3s timeout each,
    never raises — usable from crash paths and signal handlers."""
    for chat_id in cfg.get("allowed_chats") or []:
        if chat_id is None:
            continue
        try:
            post(
                f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
                urllib.parse.urlencode(
                    {"chat_id": chat_id, "text": str(text)[:3500]}
                ).encode(),
                3,
            )
        except Exception as e:
            log(f"announce {chat_id}: {e}")


def signal_run_process(proc, sig):
    """Signal only the runner process group created by run_agent()."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, sig)
    except (AttributeError, ProcessLookupError):
        try:
            proc.send_signal(sig)
        except Exception:
            pass
    except Exception:
        try:
            proc.send_signal(sig)
        except Exception:
            pass


def kill_after(proc, delay):
    """Escalate the isolated runner process group to SIGKILL."""

    def _k():
        if proc.poll() is None:
            signal_run_process(proc, signal.SIGKILL)

    t = threading.Timer(delay, _k)
    t.daemon = True
    t.start()


def run_timeout(cfg):
    """Idle timeout: progress and accepted steering renew this lease."""
    try:
        return max(1, int(cfg.get("run_timeout_s", RUN_TIMEOUT_S)))
    except (TypeError, ValueError):
        return RUN_TIMEOUT_S


def run_max(cfg):
    """Absolute safety cap, independent of ongoing output."""
    try:
        return max(run_timeout(cfg), int(cfg.get("run_max_s", RUN_MAX_S)))
    except (TypeError, ValueError):
        return RUN_MAX_S


def start_run_clock():
    now = time.monotonic()
    with RUN_LOCK:
        RUN_STATE["run_started"] = now
        RUN_STATE["last_progress"] = now
    return now


def mark_run_progress():
    with RUN_LOCK:
        RUN_STATE["last_progress"] = time.monotonic()


def run_expiry(cfg, started):
    """Return `idle` or `maximum` only when the matching lease expires."""
    now = time.monotonic()
    with RUN_LOCK:
        last = RUN_STATE.get("last_progress") or started
    if now - started >= run_max(cfg):
        return "maximum"
    if now - last >= run_timeout(cfg):
        return "idle"
    return None


def input_debounce(cfg):
    """Small merge window for Telegram's automatic multi-message splits."""
    try:
        return min(5.0, max(0.2, float(cfg.get("input_debounce_s", INPUT_DEBOUNCE_S))))
    except (TypeError, ValueError):
        return INPUT_DEBOUNCE_S


def outbox_dir(cfg):
    return cfg.get("outbox_dir") or os.path.join(cfg["workdir"], ".tgbridge-outbox")


def home_chat(cfg):
    """The DM chat: an allowed_chats entry that is also an allowed user id."""
    users = cfg.get("allowed_user_ids") or []
    for c in cfg.get("allowed_chats") or []:
        if c in users:
            return c
    ch = cfg.get("allowed_chats") or []
    return ch[0] if ch else None


def typing_loop(token, chat_id, stop_event):
    while not stop_event.wait(4.0):
        api(token, "sendChatAction", chat_id=chat_id, action="typing")


def edit_status(cfg, live, final=None):
    now = time.time()
    if not final and now - live.get("last_edit", 0) < 8:
        return
    live["last_edit"] = now
    elapsed = int(now - live["start"])
    if final:
        extra = ""
        if live.get("cost"):
            extra += f" · {live['tokens'] or 0} tok · ${live['cost']:.4f}"
        text = f"{final} · {elapsed}s · {len(live['trail'])} tool calls{extra}"
    else:
        trail = "\n".join(live["trail"][-5:])
        text = f"⚙️ working… {elapsed}s\n{trail}"
        thinking = (live.get("thinking") or "").strip().replace("\n", " ")
        if thinking:
            text += f"\n💭 …{thinking[-200:]}"
        preview = (live.get("preview") or "").strip().replace("\n", " ")
        if preview:
            text += f"\n💬 …{preview[-200:]}"
    if live.get("status_id"):
        api(
            cfg["bot_token"],
            "editMessageText",
            chat_id=live["chat_id"],
            message_id=live["status_id"],
            text=text,
        )


def run_agent(cfg, session_id, prompt, live=None):
    """Stream the runner's JSON events live (Popen).

    A watchdog Timer enforces RUN_TIMEOUT_S without blocking the read loop;
    stderr is drained on a side thread so the pipe can never fill and deadlock.
    """
    rname = cfg.get("runner", "opencode")
    runner_fn = RUNNERS.get(rname)
    if not runner_fn:
        return (
            session_id,
            None,
            (f"unknown runner {rname!r} (available: {', '.join(sorted(RUNNERS))})"),
        )
    try:
        cmd, parse = runner_fn(session_id, prompt, cfg.get("model"))
        cmd = apply_runner_policy(rname, cmd, cfg)
    except RunnerError as e:
        return session_id, None, str(e)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cfg["workdir"],
        start_new_session=True,
    )
    with RUN_LOCK:
        RUN_STATE["proc"] = proc  # exposed for /cancel and the shutdown path
        RUN_STATE["cli_run_id"] = RUN_STATE.get("cli_run_id", 0) + 1
        cli_run_id = RUN_STATE["cli_run_id"]
        RUN_STATE["steer_count"] = 0
        RUN_STATE["steer_pending"] = 0
        RUN_STATE["steer_errors"] = []
    assert proc.stdout and proc.stderr  # guaranteed: both opened with PIPE
    started = start_run_clock()
    timed_out = []
    stop_timeout = threading.Event()

    def watch_timeout():
        while not stop_timeout.wait(1.0):
            reason = run_expiry(cfg, started)
            if reason:
                timed_out.append(reason)
                signal_run_process(proc, signal.SIGKILL)
                return

    timeout_thread = threading.Thread(target=watch_timeout, daemon=True)
    timeout_thread.start()
    errbuf = []
    stderr = proc.stderr
    drain = threading.Thread(
        target=lambda: errbuf.append(stderr.read() or ""), daemon=True
    )
    drain.start()
    acc = {
        "sid": session_id,
        "texts": [],
        "thinking": None,
        "cost": 0.0,
        "tokens": None,
    }
    sid = session_id
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            mark_run_progress()
            trail = parse(ev, acc)
            sid = acc["sid"]
            if live is not None:
                if trail:
                    live["trail"].append(trail)
                    edit_status(cfg, live)
                if acc.get("thinking"):
                    live["thinking"] = acc["thinking"]
                    edit_status(cfg, live)
                if acc["texts"]:
                    live["preview"] = acc["texts"][-1]
                    edit_status(cfg, live)
        proc.wait()
    finally:
        stop_timeout.set()
        with RUN_LOCK:
            if RUN_STATE.get("cli_run_id") == cli_run_id:
                RUN_STATE["proc"] = None
            cancelled = RUN_STATE.pop("cancel", False)
    if timed_out:
        reason = "idle" if timed_out[-1] == "idle" else "absolute maximum"
        partial = "\n".join(acc["texts"]).strip()
        if partial:
            return (
                sid,
                partial
                + "\n\n⚠️ (partial answer — hit the %s timeout and was killed)" % reason,
                None,
            )
        return sid, None, "agent hit the %s timeout and was killed" % reason
    if cancelled and proc.returncode != 0:
        partial = "\n".join(acc["texts"]).strip()
        if partial:
            return sid, partial + "\n\n🛑 (cancelled by user — partial answer)", None
        return sid, None, CANCEL_MSG
    if proc.returncode != 0:
        tail = (errbuf[0] if errbuf else "").strip()[-600:]
        return (
            sid,
            None,
            f"{cfg.get('runner', 'opencode')} failed rc={proc.returncode}\n{tail}",
        )
    if not acc["texts"]:
        return sid, None, "agent returned no text"
    return sid, "\n".join(acc["texts"]).strip(), None


def effective_run_config(cfg, state):
    """Copy of cfg honoring a Telegram-set global runner override.

    state["runner_override"] = {"runner": ..., "model": ...} (model may be
    ""), written by /runner and cleared by `/runner default`. Absent/invalid
    override → cfg unchanged, so file config stays the source of truth.
    """
    override = {}
    try:
        override = (state or {}).get("runner_override") or {}
    except AttributeError:
        override = {}
    rname = override.get("runner")
    if rname not in RUNNERS:
        return dict(cfg)
    out = dict(cfg, runner=rname)
    if override.get("model") is not None:
        out["model"] = override["model"]
    return out


def runner_session(state, chat_id, runner_name, legacy_runner=None):
    """Return one runner's native session, migrating the legacy flat index.

    Session identifiers are not portable across Codex, OpenCode, and Claude.
    Keep a per-runner map while retaining the flat fields for older versions.
    """
    key = str(chat_id)
    sessions = state.setdefault("runner_sessions", {}).setdefault(key, {})
    legacy_sid = state.get("sessions", {}).get(key)
    owner = state.get("session_runners", {}).get(key)
    if legacy_sid and not owner:
        owner = legacy_runner or runner_name
        state.setdefault("session_runners", {})[key] = owner
    if legacy_sid and owner and owner not in sessions:
        sessions[owner] = legacy_sid
    return sessions.get(runner_name)


def store_runner_session(state, chat_id, runner_name, session_id):
    """Persist a native session without overwriting other runners' sessions."""
    if not session_id:
        return
    key = str(chat_id)
    state.setdefault("runner_sessions", {}).setdefault(key, {})[
        runner_name
    ] = session_id
    # Compatibility projection for older bridges and external state readers.
    state.setdefault("sessions", {})[key] = session_id
    state.setdefault("session_runners", {})[key] = runner_name


def clear_runner_sessions(state, chat_id):
    key = str(chat_id)
    state.setdefault("sessions", {}).pop(key, None)
    state.setdefault("session_runners", {}).pop(key, None)
    state.setdefault("runner_sessions", {}).pop(key, None)


def run_one(cfg, session_id, prompt, live=None):
    """Run one configured runner through its declared CLI/server transport."""
    rname = cfg.get("runner", "opencode")
    if cfg.get("model") == AUTO_OPENCODE_GO_MODEL:
        try:
            cfg = dict(cfg, model=discover_opencode_go_models(cfg)[0])
        except RunnerError as exc:
            return session_id, None, str(exc)
    mode = resolve_runner_mode(cfg, rname)
    if mode == "cli":
        return run_agent(cfg, session_id, prompt, live)
    if mode != "server":
        return session_id, None, f"unknown runner_mode {mode!r} (available: cli, server)"
    adapter = SERVER_RUNNERS.get(rname)
    if not adapter:
        return (
            session_id,
            None,
            f"runner {rname!r} has no server transport; use runner_mode='cli'",
        )
    if not adapter["healthy"](cfg):
        where = f" at {server_url(cfg)}" if rname == "opencode" else ""
        return (
            session_id,
            None,
            f"{rname} server transport unavailable{where}; "
            "start/install it or use runner_mode='cli'",
        )
    return adapter["run"](cfg, session_id, prompt, live)


def run_with_fallbacks(cfg, session_id, prompt, live=None, result_meta=None):
    """Run the primary runner, failing over across runners on ANY failure.

    The bridge does not care how or why a runner broke — quota, dead
    binary, broken network path, empty answer: if it is unusable, tag it
    (audit kind=quota|unavailable|other) and try the next entry of
    `runner_fallbacks`. Only a user cancel stops the chain; everything else
    walks it. Fallback steps always start a fresh session — session/thread
    IDs are runner-native and cannot resume across runners — and the
    delivered answer carries a one-line 🔀 header naming the runner that
    actually answered.
    """
    rname = cfg.get("runner", "opencode")
    model = resolve_model(cfg, rname)
    new_sid, answer, err = run_one(
        dict(cfg, runner=rname, model=model), session_id, prompt, live
    )
    if answer is not None or (err or "") == CANCEL_MSG:
        if result_meta is not None:
            result_meta.update(runner=rname, model=model)
        return new_sid, answer, err
    kind = classify_run_error(err, cfg.get("quota_markers"))
    reason = "hit a limit" if kind == "quota" else "is unusable"
    tried = [(rname, model)]
    with RUN_LOCK:
        if RUN_STATE.get("current"):
            RUN_STATE["current"]["runner"] = rname
    for step_runner, step_model in fallback_chain(cfg):
        note = f"🔀 {rname} {reason} — failing over to {step_runner}"
        log(
            f"run failover ({kind}): {rname} -> {step_runner} ({step_model or 'runner default'})"
        )
        audit(
            "run_fallback",
            kind=kind,
            from_runner=rname,
            to_runner=step_runner,
            to_model=step_model,
            err=(err or "")[:200],
        )
        if live is not None:
            live["trail"].append(note)
            edit_status(cfg, live)
        with RUN_LOCK:
            if RUN_STATE.get("current"):
                RUN_STATE["current"]["runner"] = step_runner
        step_cfg = dict(cfg, runner=step_runner, model=step_model)
        try:
            fallback_timeout = int(cfg.get("fallback_run_timeout_s", 0))
        except (TypeError, ValueError):
            fallback_timeout = 0
        if fallback_timeout > 0:
            step_cfg["run_timeout_s"] = min(run_timeout(cfg), fallback_timeout)
        new_sid, answer, err = run_one(step_cfg, None, prompt, live)
        rname = step_runner
        tried.append((step_runner, step_model))
        if answer is not None:
            if result_meta is not None:
                result_meta.update(runner=step_runner, model=step_model)
            header = f"🔀 {tried[0][0]} {reason} — answered via {step_runner}"
            if step_model:
                header += f" ({step_model})"
            return new_sid, header + "\n\n" + answer, None
    chain = " -> ".join(r for r, _ in tried)
    return session_id, None, f"all runners exhausted ({chain}): {err}"


def _server_call(method, path, body=None, timeout=15, directory=None, base_url=None):
    """JSON call to the local OpenCode server; returns parsed body or None."""
    if directory:
        separator = "&" if "?" in path else "?"
        path += separator + urllib.parse.urlencode({"directory": directory})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        (base_url or OPENCODE_SERVER).rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else None


def runner_mode(cfg):
    """Explicit transport mode with compatibility for the WIP boolean key."""
    mode = cfg.get("runner_mode")
    if mode is None:
        return "server" if cfg.get("server_runner") else "cli"
    return str(mode).lower()


def resolve_runner_mode(cfg, runner_name):
    """Per-runner transport override with the legacy global mode as fallback."""
    modes = cfg.get("runner_modes") or {}
    if isinstance(modes, dict) and modes.get(runner_name):
        return str(modes[runner_name]).lower()
    return runner_mode(cfg)


def server_url(cfg):
    return str(cfg.get("server_url") or OPENCODE_SERVER).rstrip("/")


def server_ok(cfg=None):
    try:
        health = _server_call(
            "GET",
            "/global/health",
            timeout=2,
            base_url=server_url(cfg or {}),
        )
        return bool((health or {}).get("healthy"))
    except Exception:
        return False


def server_event(ev, acc, seen):
    """Fold one server part snapshot, replacing text revisions by part id."""
    data = ev.get("data") or {}
    if ev.get("type") != "message.part.updated":
        return None
    part = data.get("part") or {}
    pid = part.get("id")
    pt = part.get("type")
    if pt == "text" and part.get("text") is not None and pid:
        acc["parts"][pid] = part["text"]
        if pid not in seen:
            seen.add(pid)
            acc["order"].append(pid)
    elif pt == "reasoning" and part.get("text"):
        acc["thinking"] = part["text"]
    elif pt == "tool" and part.get("tool") and pid not in seen:
        seen.add(pid)
        inp = (part.get("state") or {}).get("input") or {}
        summary = ""
        for v in inp.values():
            if isinstance(v, str) and len(v) > len(summary):
                summary = v
        summary = summary.replace("\n", " ")[:60]
        return f"🔧 {part['tool']}: {summary}" if summary else f"🔧 {part['tool']}"
    return None


def server_messages(messages, baseline, acc, seen):
    """Fold authoritative GET /session/{id}/message snapshots.

    Only assistant messages created after the pre-prompt baseline belong to
    this bridge run. Returns (new trail lines, relevant assistant infos).
    """
    trails = []
    infos = []
    for message in messages or []:
        info = message.get("info") or {}
        mid = info.get("id")
        if not mid or mid in baseline or info.get("role") != "assistant":
            continue
        infos.append(info)
        error_ids = acc.setdefault("error_ids", set())
        if info.get("error") and mid not in error_ids:
            acc.setdefault("errors", []).append(info["error"])
            error_ids.add(mid)
        for part in message.get("parts") or []:
            trail = server_event(
                {"type": "message.part.updated", "data": {"part": part}},
                acc,
                seen,
            )
            if trail:
                trails.append(trail)
    return trails, infos


def _server_poll_interval(cfg):
    try:
        return min(5.0, max(0.1, float(cfg.get("server_poll_s", SERVER_POLL_S))))
    except (TypeError, ValueError):
        return SERVER_POLL_S


def _begin_steer(chat_id):
    """Reserve a supported same-chat steer without racing run completion."""
    with RUN_LOCK:
        cur = RUN_STATE.get("current")
        if not (RUN_STATE.get("busy") and cur and cur.get("chat") == chat_id):
            return None
        thread_id = RUN_STATE.get("codex_thread_id")
        turn_id = RUN_STATE.get("codex_turn_id")
        if thread_id and turn_id:
            RUN_STATE["steer_pending"] = RUN_STATE.get("steer_pending", 0) + 1
            RUN_STATE["last_progress"] = time.monotonic()
            return {
                "transport": "codex_turn_steer",
                "sid": thread_id,
                "turn_id": turn_id,
                "run_id": RUN_STATE.get("codex_run_id", 0),
            }
        sid = RUN_STATE.get("server_sid")
        if sid and RUN_STATE.get("server_api") == "v2":
            RUN_STATE["steer_pending"] = RUN_STATE.get("steer_pending", 0) + 1
            RUN_STATE["last_progress"] = time.monotonic()
            return {
                "transport": "opencode_v2_steer",
                "sid": sid,
                "run_id": RUN_STATE.get("server_run_id", 0),
                "base_url": RUN_STATE.get("server_url"),
            }
        return None


def should_steer(chat_id):
    """Whether the active same-chat transport supports live steering."""
    with RUN_LOCK:
        cur = RUN_STATE.get("current")
        return bool(
            RUN_STATE.get("busy")
            and cur
            and cur.get("chat") == chat_id
            and (
                (RUN_STATE.get("server_sid") and RUN_STATE.get("server_api") == "v2")
                or (RUN_STATE.get("codex_thread_id") and RUN_STATE.get("codex_turn_id"))
            )
        )


def _steer_deliver(sid, run_id, directory, base_url, text):
    """Submit one non-blocking prompt and publish its delivery atomically."""
    err = None
    try:
        _server_call(
            "POST",
            f"/session/{sid}/prompt_async",
            {
                "parts": [
                    {
                        "type": "text",
                        "text": "(steering from the human, mid-run — adjust course "
                        "accordingly)\n" + text,
                    }
                ]
            },
            timeout=5,
            directory=directory,
            base_url=base_url,
        )
        audit("steer_delivered", session=sid, chars=len(text))
    except Exception as e:
        err = str(e)
        audit("steer_error", session=sid, err=err[:200])
        log(f"steer deliver: {e}")
    finally:
        with RUN_LOCK:
            if (
                RUN_STATE.get("server_sid") == sid
                and RUN_STATE.get("server_run_id") == run_id
                and RUN_STATE.get("server_directory") == directory
                and RUN_STATE.get("server_url") == base_url
            ):
                if err:
                    RUN_STATE.setdefault("steer_errors", []).append(err)
                else:
                    RUN_STATE["steer_count"] = RUN_STATE.get("steer_count", 0) + 1
                RUN_STATE["steer_pending"] = max(
                    0, RUN_STATE.get("steer_pending", 1) - 1
                )


def _steer_deliver_v2(cfg, sid, run_id, base_url, text, chat_id, message_id):
    """Admit a durable native OpenCode v2 steer at the next safe boundary."""
    meta = {
        "sid": sid,
        "text": text,
        "chat_id": chat_id,
        "message_id": message_id,
    }
    err = None
    try:
        with RUN_LOCK:
            if not (
                RUN_STATE.get("server_sid") == sid
                and RUN_STATE.get("server_run_id") == run_id
                and RUN_STATE.get("server_api") == "v2"
            ):
                raise RuntimeError("active OpenCode run changed before steer delivery")
        _server_call(
            "POST",
            f"/api/session/{sid}/prompt",
            {
                "prompt": {
                    "text": "(steering from the human, mid-run — adjust course "
                    "accordingly)\n" + text
                },
                "delivery": "steer",
            },
            timeout=5,
            base_url=base_url,
        )
        audit(
            "steer_delivered",
            session=sid,
            transport="opencode_v2_steer",
            chars=len(text),
        )
    except Exception as e:
        err = str(e)
        audit(
            "steer_error",
            session=sid,
            transport="opencode_v2_steer",
            err=err[:200],
        )
        log(f"OpenCode v2 steer deliver: {e}")
    finally:
        with RUN_LOCK:
            if (
                RUN_STATE.get("server_sid") == sid
                and RUN_STATE.get("server_run_id") == run_id
            ):
                if err:
                    RUN_STATE.setdefault("steer_errors", []).append(err)
                else:
                    RUN_STATE["steer_count"] = RUN_STATE.get("steer_count", 0) + 1
                RUN_STATE["steer_pending"] = max(
                    0, RUN_STATE.get("steer_pending", 1) - 1
                )
        if err:
            _steer_fallback(cfg, meta, err)


def _codex_rpc_write(proc, payload):
    """Write one newline-framed app-server request without interleaving writers."""
    if not proc.stdin or proc.poll() is not None:
        raise RuntimeError("Codex app-server is no longer running")
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    with CODEX_WRITE_LOCK:
        proc.stdin.write(line)
        proc.stdin.flush()


def _steer_fallback(cfg, meta, err):
    """Preserve a rejected live steer as a normal next turn and tell the user."""
    chat_id = meta.get("chat_id")
    text = meta.get("text") or ""
    if chat_id is None:
        return
    PROMPT_Q.put((chat_id, meta.get("message_id"), text))
    audit(
        "steer_fallback_queued",
        chat_id=chat_id,
        session=meta.get("sid"),
        chars=len(text),
        err=str(err)[:200],
    )
    send(
        cfg["bot_token"],
        chat_id,
        "⚠️ this turn could no longer accept steering; kept safely as the next turn",
    )


def _codex_steer_deliver(
    cfg, sid, turn_id, run_id, text, chat_id=None, message_id=None
):
    """Send true same-turn steering to Codex app-server's `turn/steer`."""
    meta = {
        "sid": sid,
        "text": text,
        "chat_id": chat_id,
        "message_id": message_id,
    }
    request_id = None
    try:
        with RUN_LOCK:
            if not (
                RUN_STATE.get("codex_thread_id") == sid
                and RUN_STATE.get("codex_turn_id") == turn_id
                and RUN_STATE.get("codex_run_id") == run_id
            ):
                raise RuntimeError("active Codex turn changed before steering delivery")
            proc = RUN_STATE.get("proc")
            seq = RUN_STATE.get("codex_request_id", 10) + 1
            RUN_STATE["codex_request_id"] = seq
            request_id = f"steer-{run_id}-{seq}"
            RUN_STATE.setdefault("codex_steers", {})[request_id] = meta
        _codex_rpc_write(
            proc,
            {
                "id": request_id,
                "method": "turn/steer",
                "params": {
                    "threadId": sid,
                    "expectedTurnId": turn_id,
                    "input": [{"type": "text", "text": text}],
                },
            },
        )
    except Exception as e:
        with RUN_LOCK:
            if request_id:
                RUN_STATE.setdefault("codex_steers", {}).pop(request_id, None)
            if RUN_STATE.get("codex_run_id") == run_id:
                RUN_STATE["steer_pending"] = max(
                    0, RUN_STATE.get("steer_pending", 1) - 1
                )
        audit(
            "steer_error",
            session=sid,
            transport="codex_turn_steer",
            err=str(e)[:200],
        )
        log(f"codex turn/steer write: {e}")
        _steer_fallback(cfg, meta, e)


def run_agent_server_v1(cfg, session_id, prompt, live=None):
    """Compatibility transport for old OpenCode servers.

    `/prompt_async` starts the initial message non-blockingly. Busy-session
    steering is intentionally disabled: old servers can persist a prompt
    between a tool call and its result, corrupting provider message order.
    `/session/{id}/message` is the authoritative transcript; `/session/status`
    supplies the busy/idle boundary (idle sessions are omitted by OpenCode
    1.1.12). A quiet grace prevents a just-arriving steer from being split into
    a second Telegram run.
    """
    sid = session_id
    timed_out = False
    cancelled = False
    directory = cfg["workdir"]
    base_url = server_url(cfg)
    acc = {"parts": {}, "order": [], "thinking": None, "errors": []}
    seen = set()
    try:
        if session_id:
            sid = session_id
        else:
            created = _server_call(
                "POST",
                "/session",
                {"title": time.strftime("tg %Y%m%d-%H%M")},
                directory=directory,
                base_url=base_url,
            )
            sid = (created or {}).get("id")
            if not sid:
                return session_id, None, "server: could not create session"
        before = (
            _server_call(
                "GET",
                f"/session/{sid}/message",
                timeout=5,
                directory=directory,
                base_url=base_url,
            )
            or []
        )
        baseline = {
            (message.get("info") or {}).get("id")
            for message in before
            if (message.get("info") or {}).get("id")
        }
        body: dict = {"parts": [{"type": "text", "text": prompt}]}
        model = resolve_model(cfg, cfg.get("runner", "opencode"))
        if model and "/" in model:
            prov, _, mid = model.partition("/")
            body["model"] = {"providerID": prov, "modelID": mid}
        _server_call(
            "POST",
            f"/session/{sid}/prompt_async",
            body,
            timeout=5,
            directory=directory,
            base_url=base_url,
        )
        with RUN_LOCK:
            RUN_STATE["server_run_id"] = RUN_STATE.get("server_run_id", 0) + 1
            RUN_STATE["server_sid"] = sid
            RUN_STATE["server_directory"] = directory
            RUN_STATE["server_url"] = base_url
            RUN_STATE["server_api"] = "v1"
            RUN_STATE["steer_count"] = 0
            RUN_STATE["steer_pending"] = 0
            RUN_STATE["steer_errors"] = []
            RUN_STATE["cancel"] = False
            if RUN_STATE.get("current"):
                RUN_STATE["current"]["session"] = sid
        started = start_run_clock()
        timeout_reason = None
        last_snapshot = json.dumps(before, sort_keys=True, ensure_ascii=False)
        poll_s = _server_poll_interval(cfg)
        quiet_s = max(SERVER_QUIET_S, poll_s)
        while True:
            with RUN_LOCK:
                cancelled = bool(RUN_STATE.get("cancel"))
            if cancelled:
                try:
                    _server_call(
                        "POST",
                        f"/session/{sid}/abort",
                        timeout=5,
                        directory=directory,
                        base_url=base_url,
                    )
                except Exception as e:
                    log(f"server abort: {e}")
                break
            timeout_reason = run_expiry(cfg, started)
            if timeout_reason:
                timed_out = True
                try:
                    _server_call(
                        "POST",
                        f"/session/{sid}/abort",
                        timeout=5,
                        directory=directory,
                        base_url=base_url,
                    )
                except Exception as e:
                    log(f"server timeout abort: {e}")
                break

            messages = (
                _server_call(
                    "GET",
                    f"/session/{sid}/message",
                    timeout=5,
                    directory=directory,
                    base_url=base_url,
                )
                or []
            )
            snapshot = json.dumps(messages, sort_keys=True, ensure_ascii=False)
            if snapshot != last_snapshot:
                last_snapshot = snapshot
                mark_run_progress()
            trails, infos = server_messages(messages, baseline, acc, seen)
            if live is not None:
                if trails:
                    live["trail"].extend(trails)
                    edit_status(cfg, live)
                if acc["thinking"]:
                    live["thinking"] = acc["thinking"]
                    edit_status(cfg, live)
                if acc["order"]:
                    last = acc["parts"].get(acc["order"][-1])
                    if last:
                        live["preview"] = last
                        edit_status(cfg, live)

            statuses = (
                _server_call(
                    "GET",
                    "/session/status",
                    timeout=5,
                    directory=directory,
                    base_url=base_url,
                )
                or {}
            )
            active = statuses.get(sid)
            busy = bool(active and active.get("type") != "idle")
            if busy:
                mark_run_progress()
            completed = bool(infos) and all(
                (info.get("time") or {}).get("completed") or info.get("error")
                for info in infos
            )
            with RUN_LOCK:
                pending = RUN_STATE.get("steer_pending", 0)
                generation = RUN_STATE.get("steer_count", 0)
            if not busy and completed and pending == 0:
                time.sleep(quiet_s)
                with RUN_LOCK:
                    stable = (
                        RUN_STATE.get("steer_pending", 0) == 0
                        and RUN_STATE.get("steer_count", 0) == generation
                        and not RUN_STATE.get("cancel")
                    )
                if stable:
                    # One final read captures the last text snapshot after idle.
                    messages = (
                        _server_call(
                            "GET",
                            f"/session/{sid}/message",
                            timeout=5,
                            directory=directory,
                            base_url=base_url,
                        )
                        or []
                    )
                    trails, _ = server_messages(messages, baseline, acc, seen)
                    if live is not None and trails:
                        live["trail"].extend(trails)
                    break
            time.sleep(poll_s)

        # Abort can race the last model write; retain whatever was persisted.
        try:
            messages = (
                _server_call(
                    "GET",
                    f"/session/{sid}/message",
                    timeout=5,
                    directory=directory,
                    base_url=base_url,
                )
                or []
            )
            server_messages(messages, baseline, acc, seen)
        except Exception as e:
            log(f"server final transcript: {e}")

        with RUN_LOCK:
            steer_errors = list(RUN_STATE.get("steer_errors") or [])
            RUN_STATE["server_sid"] = None
            RUN_STATE["server_directory"] = None
            RUN_STATE["server_url"] = None
            RUN_STATE["server_api"] = None
            RUN_STATE["steer_pending"] = 0
            RUN_STATE["steer_errors"] = []
            cancelled = bool(RUN_STATE.pop("cancel", False)) or cancelled
        answer = "\n\n".join(
            acc["parts"][pid] for pid in acc["order"] if acc["parts"].get(pid)
        ).strip()
        if timed_out and answer:
            return (
                sid,
                answer + f"\n\n⚠️ (partial — hit the {timeout_reason} timeout)",
                None,
            )
        if timed_out:
            return sid, None, f"agent hit the {timeout_reason} timeout"
        if cancelled and answer:
            return sid, answer + "\n\n🛑 (cancelled — partial answer)", None
        if cancelled:
            return sid, None, CANCEL_MSG
        if steer_errors:
            detail = steer_errors[-1][-300:]
            if answer:
                return (
                    sid,
                    answer + "\n\n⚠️ (a mid-run steering message failed to deliver)",
                    None,
                )
            return sid, None, f"steering delivery failed: {detail}"
        if acc["errors"]:
            detail = json.dumps(acc["errors"][-1], ensure_ascii=False)[-500:]
            if answer:
                return sid, answer + "\n\n⚠️ (agent turn ended with an error)", None
            return sid, None, f"server agent error: {detail}"
        if not answer:
            return sid, None, "agent returned no text"
        return sid, answer, None
    except Exception as e:
        return sid, None, f"server runner error: {e}"
    finally:
        with RUN_LOCK:
            if RUN_STATE.get("server_sid") == sid:
                RUN_STATE["server_sid"] = None
                RUN_STATE["server_directory"] = None
                RUN_STATE["server_url"] = None
                RUN_STATE["server_api"] = None
                RUN_STATE["steer_pending"] = 0
                RUN_STATE["steer_errors"] = []
                RUN_STATE.pop("cancel", None)


def opencode_v2_supported(cfg):
    """Detect the durable `/api/... delivery=steer` contract."""
    try:
        spec = _server_call("GET", "/doc", timeout=3, base_url=server_url(cfg)) or {}
        route = (spec.get("paths") or {}).get("/api/session/{sessionID}/prompt")
        return bool(route and route.get("post"))
    except Exception:
        return False


def _opencode_model(cfg, v2=False):
    model = str(resolve_model(cfg, cfg.get("runner", "opencode")) or "").strip()
    if "/" not in model:
        return None
    provider, _, model_id = model.partition("/")
    if not provider or not model_id:
        return None
    return (
        {"providerID": provider, "id": model_id}
        if v2
        else {"providerID": provider, "modelID": model_id}
    )


def server_messages_v2(response, baseline, acc, seen):
    """Fold OpenCode v2 projected messages into the existing live accumulator."""
    trails = []
    infos = []
    messages = (response or {}).get("data") if isinstance(response, dict) else response
    ordered = sorted(
        messages or [], key=lambda item: (item.get("time") or {}).get("created", 0)
    )
    for message in ordered:
        mid = message.get("id")
        if not mid or mid in baseline or message.get("type") != "assistant":
            continue
        infos.append(message)
        error_ids = acc.setdefault("error_ids", set())
        if message.get("error") and mid not in error_ids:
            acc.setdefault("errors", []).append(message["error"])
            error_ids.add(mid)
        for part in message.get("content") or []:
            pid = part.get("id")
            kind = part.get("type")
            if kind == "text" and pid and part.get("text") is not None:
                acc["parts"][pid] = part["text"]
                if pid not in seen:
                    seen.add(pid)
                    acc["order"].append(pid)
            elif kind == "reasoning" and part.get("text"):
                acc["thinking"] = part["text"]
            elif kind == "tool" and pid not in seen:
                seen.add(pid)
                name = part.get("name") or "tool"
                inp = (part.get("state") or {}).get("input") or {}
                summary = max(
                    (v for v in inp.values() if isinstance(v, str)),
                    key=len,
                    default="",
                ).replace("\n", " ")[:60]
                trails.append(f"🔧 {name}: {summary}" if summary else f"🔧 {name}")
    return trails, infos


def run_agent_server_v2(cfg, session_id, prompt, live=None):
    """Run OpenCode v2 and admit follow-ups as native safe-boundary steers."""
    sid = session_id
    timed_out = False
    cancelled = False
    base_url = server_url(cfg)
    acc = {"parts": {}, "order": [], "thinking": None, "errors": []}
    seen = set()
    try:
        if sid:
            try:
                _server_call("GET", f"/api/session/{sid}", timeout=5, base_url=base_url)
            except urllib.error.HTTPError as eably:
                if eably.code != 404:
                    raise
                sid = None
            else:
                model_ref = _opencode_model(cfg, v2=True)
                if model_ref:
                    _server_call(
                        "POST",
                        f"/api/session/{sid}/model",
                        {"model": model_ref},
                        timeout=5,
                        base_url=base_url,
                    )
        if not sid:
            create_body = {"location": {"directory": cfg["workdir"]}}
            model_ref = _opencode_model(cfg, v2=True)
            if model_ref:
                create_body["model"] = model_ref
            created = _server_call(
                "POST", "/api/session", create_body, timeout=10, base_url=base_url
            )
            sid = ((created or {}).get("data") or {}).get("id")
            if not sid:
                return session_id, None, "OpenCode v2: could not create session"
        before = (
            _server_call(
                "GET",
                f"/api/session/{sid}/message?order=desc&limit=100",
                timeout=10,
                base_url=base_url,
            )
            or {}
        )
        baseline = {
            message.get("id") for message in before.get("data", []) if message.get("id")
        }
        _server_call(
            "POST",
            f"/api/session/{sid}/prompt",
            {"prompt": {"text": prompt}, "delivery": "steer"},
            timeout=10,
            base_url=base_url,
        )
        with RUN_LOCK:
            RUN_STATE["server_run_id"] = RUN_STATE.get("server_run_id", 0) + 1
            RUN_STATE["server_sid"] = sid
            RUN_STATE["server_directory"] = cfg["workdir"]
            RUN_STATE["server_url"] = base_url
            RUN_STATE["server_api"] = "v2"
            RUN_STATE["steer_count"] = 0
            RUN_STATE["steer_pending"] = 0
            RUN_STATE["steer_errors"] = []
            RUN_STATE["cancel"] = False
            if RUN_STATE.get("current"):
                RUN_STATE["current"]["session"] = sid
        started = start_run_clock()
        timeout_reason = None
        last_snapshot = json.dumps(before, sort_keys=True, ensure_ascii=False)
        poll_s = _server_poll_interval(cfg)
        quiet_s = max(SERVER_QUIET_S, poll_s)
        while True:
            with RUN_LOCK:
                cancelled = bool(RUN_STATE.get("cancel"))
            timeout_reason = run_expiry(cfg, started)
            if cancelled or timeout_reason:
                timed_out = bool(timeout_reason) and not cancelled
                try:
                    _server_call(
                        "POST",
                        f"/api/session/{sid}/interrupt",
                        timeout=5,
                        base_url=base_url,
                    )
                except Exception as eably:
                    log(f"OpenCode v2 interrupt: {eably}")
                break
            messages = (
                _server_call(
                    "GET",
                    f"/api/session/{sid}/message?order=desc&limit=100",
                    timeout=10,
                    base_url=base_url,
                )
                or {}
            )
            snapshot = json.dumps(messages, sort_keys=True, ensure_ascii=False)
            if snapshot != last_snapshot:
                last_snapshot = snapshot
                mark_run_progress()
            trails, infos = server_messages_v2(messages, baseline, acc, seen)
            if live is not None:
                if trails:
                    live["trail"].extend(trails)
                    edit_status(cfg, live)
                if acc["thinking"]:
                    live["thinking"] = acc["thinking"]
                    edit_status(cfg, live)
                if acc["order"]:
                    latest = acc["parts"].get(acc["order"][-1])
                    if latest:
                        live["preview"] = latest
                        edit_status(cfg, live)
            active = (
                _server_call("GET", "/api/session/active", timeout=5, base_url=base_url)
                or {}
            )
            busy = sid in (active.get("data") or {})
            if busy:
                mark_run_progress()
            completed = bool(infos) and all(
                (info.get("time") or {}).get("completed") or info.get("error")
                for info in infos
            )
            with RUN_LOCK:
                pending = RUN_STATE.get("steer_pending", 0)
                generation = RUN_STATE.get("steer_count", 0)
            if not busy and completed and pending == 0:
                time.sleep(quiet_s)
                with RUN_LOCK:
                    stable = (
                        RUN_STATE.get("steer_pending", 0) == 0
                        and RUN_STATE.get("steer_count", 0) == generation
                        and not RUN_STATE.get("cancel")
                    )
                if stable:
                    final_messages = (
                        _server_call(
                            "GET",
                            f"/api/session/{sid}/message?order=desc&limit=100",
                            timeout=10,
                            base_url=base_url,
                        )
                        or {}
                    )
                    server_messages_v2(final_messages, baseline, acc, seen)
                    break
            time.sleep(poll_s)
        with RUN_LOCK:
            steer_errors = list(RUN_STATE.get("steer_errors") or [])
            cancelled = bool(RUN_STATE.pop("cancel", False)) or cancelled
        answer = "\n\n".join(
            acc["parts"][pid] for pid in acc["order"] if acc["parts"].get(pid)
        ).strip()
        if timed_out:
            return (
                sid,
                answer + f"\n\n⚠️ (partial — hit the {timeout_reason} timeout)"
                if answer
                else None,
                None if answer else f"agent hit the {timeout_reason} timeout",
            )
        if cancelled:
            return (
                (sid, answer + "\n\n🛑 (cancelled — partial answer)", None)
                if answer
                else (sid, None, CANCEL_MSG)
            )
        if steer_errors:
            return sid, answer or None, None if answer else "steering delivery failed"
        if acc["errors"]:
            detail = json.dumps(acc["errors"][-1], ensure_ascii=False)[-500:]
            return (
                sid,
                answer or None,
                None if answer else f"OpenCode v2 error: {detail}",
            )
        if not answer:
            return sid, None, "agent returned no text"
        return sid, answer, None
    except Exception as eably:
        return sid, None, f"OpenCode v2 server error: {eably}"
    finally:
        with RUN_LOCK:
            if RUN_STATE.get("server_sid") == sid:
                RUN_STATE["server_sid"] = None
                RUN_STATE["server_directory"] = None
                RUN_STATE["server_url"] = None
                RUN_STATE["server_api"] = None
                RUN_STATE["steer_pending"] = 0
                RUN_STATE["steer_errors"] = []
                RUN_STATE.pop("cancel", None)


def run_agent_server(cfg, session_id, prompt, live=None):
    if session_id and not str(session_id).startswith("ses"):
        session_id = None
    if opencode_v2_supported(cfg):
        return run_agent_server_v2(cfg, session_id, prompt, live)
    return run_agent_server_v1(cfg, session_id, prompt, live)


SERVER_RUNNERS["opencode"] = {
    "run": run_agent_server,
    "healthy": server_ok,
    "feature": "native same-turn steer (v2)",
}


def codex_app_server_ok(_cfg=None):
    """Installed Codex must expose the app-server transport used for steering."""
    try:
        result = subprocess.run(
            [_bin("CODEX_BIN", "codex"), "app-server", "--help"],
            text=True,
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _codex_read_events(stream, events):
    try:
        for raw in stream:
            try:
                events.put(json.loads(raw))
            except json.JSONDecodeError:
                continue
    finally:
        events.put(None)


def _codex_wait_response(events, request_id, timeout=20, keep=None):
    """Wait for one setup RPC response; pre-turn notifications are disposable."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = events.get(timeout=min(0.5, deadline - time.monotonic()))
        except queue.Empty:
            continue
        if event is None:
            raise RuntimeError("Codex app-server exited during setup")
        if event.get("id") == request_id:
            if event.get("error"):
                detail = json.dumps(event["error"], ensure_ascii=False)[-600:]
                raise RuntimeError(detail)
            return event.get("result") or {}
        if keep is not None:
            keep.append(event)
    raise RuntimeError(f"Codex app-server RPC {request_id} timed out")


def _codex_item_trail(item):
    """Render a compact tool trail from a v2 app-server ThreadItem."""
    kind = item.get("type")
    if kind == "commandExecution":
        return "🔧 bash: " + (item.get("command") or "")[:60]
    if kind == "mcpToolCall":
        label = "/".join(str(v) for v in (item.get("server"), item.get("tool")) if v)
        return "🔧 " + (label or "MCP tool")[:70]
    if kind == "dynamicToolCall":
        return "🔧 " + str(item.get("tool") or "dynamic tool")[:70]
    if kind == "collabAgentToolCall":
        return "🔧 " + str(item.get("tool") or "agent")[:70]
    if kind == "webSearch":
        return "🔧 web: " + str(item.get("query") or "search")[:60]
    if kind == "fileChange":
        return "🔧 file change"
    if kind == "imageView":
        return "🔧 image: " + str(item.get("path") or "view")[-60:]
    if kind == "imageGeneration":
        return "🔧 image generation"
    return None


def _codex_handle_steer_response(cfg, event, run_id):
    request_id = event.get("id")
    if not isinstance(request_id, str) or not request_id.startswith("steer-"):
        return False
    with RUN_LOCK:
        meta = RUN_STATE.setdefault("codex_steers", {}).pop(request_id, None)
        if meta and RUN_STATE.get("codex_run_id") == run_id:
            RUN_STATE["steer_pending"] = max(0, RUN_STATE.get("steer_pending", 1) - 1)
            if not event.get("error"):
                RUN_STATE["steer_count"] = RUN_STATE.get("steer_count", 0) + 1
    if not meta:
        return True
    if event.get("error"):
        detail = json.dumps(event["error"], ensure_ascii=False)[-500:]
        audit(
            "steer_error",
            session=meta["sid"],
            transport="codex_turn_steer",
            err=detail,
        )
        _steer_fallback(cfg, meta, detail)
    else:
        audit(
            "steer_delivered",
            session=meta["sid"],
            transport="codex_turn_steer",
            chars=len(meta["text"]),
        )
    return True


def run_codex_app_server(cfg, session_id, prompt, live=None):
    """Run Codex through app-server so `turn/steer` reaches the active turn."""
    sid = session_id
    turn_id = None
    timed_out = False
    cancelled = False
    proc = None
    events = queue.Queue()
    errbuf = []
    answer_final = []
    answer_unknown = []
    seen_messages = set()
    previews = {}
    turn_error = None
    run_id = None
    outstanding = []
    try:
        proc = subprocess.Popen(
            [
                _bin("CODEX_BIN", "codex"),
                "app-server",
                "--listen",
                "stdio://",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cfg["workdir"],
            start_new_session=True,
            bufsize=1,
        )
        assert proc.stdout and proc.stderr
        threading.Thread(
            target=_codex_read_events, args=(proc.stdout, events), daemon=True
        ).start()
        stderr = proc.stderr
        threading.Thread(
            target=lambda: errbuf.append(stderr.read() or ""), daemon=True
        ).start()
        with RUN_LOCK:
            RUN_STATE["proc"] = proc
            RUN_STATE["codex_run_id"] = RUN_STATE.get("codex_run_id", 0) + 1
            run_id = RUN_STATE["codex_run_id"]
            RUN_STATE["codex_thread_id"] = None
            RUN_STATE["codex_turn_id"] = None
            RUN_STATE["codex_steers"] = {}
            RUN_STATE["steer_count"] = 0
            RUN_STATE["steer_pending"] = 0
            RUN_STATE["steer_errors"] = []
            RUN_STATE["cancel"] = False

        _codex_rpc_write(
            proc,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "tgbridge", "version": "0.1"},
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        _codex_wait_response(events, 1)
        _codex_rpc_write(proc, {"method": "initialized"})

        access = {
            "cwd": cfg["workdir"],
            "approvalPolicy": "never",
            "sandbox": (
                "danger-full-access" if cfg.get("codex_yolo", False) else "read-only"
            ),
        }
        if resolve_model(cfg, "codex"):
            access["model"] = resolve_model(cfg, "codex")
        setup_id = 2
        if session_id:
            params = {**access, "threadId": session_id}
            _codex_rpc_write(
                proc,
                {"id": setup_id, "method": "thread/resume", "params": params},
            )
            try:
                setup = _codex_wait_response(events, setup_id)
            except RuntimeError:
                setup_id += 1
                _codex_rpc_write(
                    proc,
                    {
                        "id": setup_id,
                        "method": "thread/start",
                        "params": {**access, "ephemeral": False},
                    },
                )
                setup = _codex_wait_response(events, setup_id)
        else:
            _codex_rpc_write(
                proc,
                {
                    "id": setup_id,
                    "method": "thread/start",
                    "params": {**access, "ephemeral": False},
                },
            )
            setup = _codex_wait_response(events, setup_id)
        sid = ((setup.get("thread") or {}).get("id")) or session_id
        if not sid:
            raise RuntimeError("Codex app-server returned no thread id")

        turn_request_id = setup_id + 1
        _codex_rpc_write(
            proc,
            {
                "id": turn_request_id,
                "method": "turn/start",
                "params": {
                    "threadId": sid,
                    "input": [{"type": "text", "text": prompt}],
                },
            },
        )
        early_events = []
        started_response = _codex_wait_response(
            events, turn_request_id, keep=early_events
        )
        turn_id = (started_response.get("turn") or {}).get("id")
        if not turn_id:
            raise RuntimeError("Codex app-server returned no active turn id")
        with RUN_LOCK:
            if RUN_STATE.get("codex_run_id") == run_id:
                RUN_STATE["codex_thread_id"] = sid
                RUN_STATE["codex_turn_id"] = turn_id
                if RUN_STATE.get("current"):
                    RUN_STATE["current"]["session"] = sid

        clock_started = start_run_clock()
        timeout_reason = None
        completed = False
        while not completed:
            with RUN_LOCK:
                cancelled = bool(RUN_STATE.get("cancel"))
            if cancelled:
                signal_run_process(proc, signal.SIGTERM)
                kill_after(proc, 3)
                break
            timeout_reason = run_expiry(cfg, clock_started)
            if timeout_reason:
                timed_out = True
                signal_run_process(proc, signal.SIGTERM)
                kill_after(proc, 3)
                break
            if early_events:
                event = early_events.pop(0)
            else:
                try:
                    event = events.get(timeout=0.25)
                except queue.Empty:
                    continue
            if event is None:
                break
            mark_run_progress()
            if _codex_handle_steer_response(cfg, event, run_id):
                continue
            method = event.get("method")
            params = event.get("params") or {}
            if params.get("turnId") not in (None, turn_id):
                continue
            item = params.get("item") or {}
            if method == "item/started":
                trail = _codex_item_trail(item)
                if trail and live is not None:
                    live["trail"].append(trail)
                    edit_status(cfg, live)
            elif method == "item/agentMessage/delta":
                item_id = params.get("itemId") or "message"
                previews[item_id] = previews.get(item_id, "") + (
                    params.get("delta") or ""
                )
                if live is not None and previews[item_id]:
                    live["preview"] = previews[item_id]
                    edit_status(cfg, live)
            elif method == "item/completed" and item.get("type") == "agentMessage":
                item_id = item.get("id")
                if item_id and item_id in seen_messages:
                    continue
                if item_id:
                    seen_messages.add(item_id)
                text = (item.get("text") or "").strip()
                if text and item.get("phase") == "final_answer":
                    answer_final.append(text)
                elif text and item.get("phase") is None:
                    answer_unknown.append(text)
            elif method == "error":
                if not params.get("willRetry"):
                    turn_error = json.dumps(
                        params.get("error") or params, ensure_ascii=False
                    )[-600:]
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                if turn.get("id") == turn_id:
                    if turn.get("error"):
                        turn_error = json.dumps(turn["error"], ensure_ascii=False)[
                            -600:
                        ]
                    completed = True

        # Responses normally precede turn/completed, but drain any already
        # buffered steer acknowledgements before tearing down the transport.
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            if event is not None:
                _codex_handle_steer_response(cfg, event, run_id)

        with RUN_LOCK:
            outstanding = list(RUN_STATE.get("codex_steers", {}).values())
            RUN_STATE["codex_steers"] = {}
            RUN_STATE["steer_pending"] = 0
            cancelled = bool(RUN_STATE.pop("cancel", False)) or cancelled
        for meta in outstanding:
            _steer_fallback(cfg, meta, "app-server closed before steer acknowledgement")

        answer = "\n\n".join(answer_final or answer_unknown).strip()
        if timed_out and answer:
            return (
                sid,
                answer + f"\n\n⚠️ (partial — hit the {timeout_reason} timeout)",
                None,
            )
        if timed_out:
            return sid, None, f"agent hit the {timeout_reason} timeout"
        if cancelled and answer:
            return sid, answer + "\n\n🛑 (cancelled — partial answer)", None
        if cancelled:
            return sid, None, CANCEL_MSG
        if turn_error and answer:
            return sid, answer + "\n\n⚠️ (agent turn ended with an error)", None
        if turn_error:
            return sid, None, f"Codex app-server error: {turn_error}"
        if not answer:
            tail = (errbuf[0] if errbuf else "").strip()[-500:]
            return sid, None, "agent returned no text" + (f"\n{tail}" if tail else "")
        return sid, answer, None
    except Exception as e:
        tail = (errbuf[0] if errbuf else "").strip()[-400:]
        detail = f"Codex app-server error: {e}"
        if tail:
            detail += "\n" + tail
        return sid, None, detail
    finally:
        with RUN_LOCK:
            if RUN_STATE.get("codex_run_id") == run_id:
                RUN_STATE["codex_thread_id"] = None
                RUN_STATE["codex_turn_id"] = None
                RUN_STATE["codex_steers"] = {}
                RUN_STATE["steer_pending"] = 0
                RUN_STATE["proc"] = None
                RUN_STATE.pop("cancel", None)
        if proc and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                proc.wait(timeout=2)
            except Exception:
                signal_run_process(proc, signal.SIGTERM)
                kill_after(proc, 2)


SERVER_RUNNERS["codex"] = {
    "run": run_codex_app_server,
    "healthy": codex_app_server_ok,
    "feature": "same-turn turn/steer",
}


def multipart(fields, file_field, filename, data, ctype):
    b = "----tgbridge" + os.urandom(12).hex()
    body = bytearray()
    for k, v in fields:
        body += (
            f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
        ).encode()
    body += (
        f'--{b}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'
    ).encode()
    body += data + b"\r\n"
    body += f"--{b}--\r\n".encode()
    return b, bytes(body)


def transcribe(cfg, token, file_id):
    """Voice note -> text via any OpenAI-compatible /audio/transcriptions API.

    Optional: only runs when transcribe_key is set. Stdlib multipart, no deps.
    """
    g = api(token, "getFile", file_id=file_id)
    fp = ((g or {}).get("result") or {}).get("file_path")
    if not fp:
        return None, "voice getFile failed"
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/file/bot{token}/{fp}", timeout=60
        ) as r:
            audio = r.read()
    except Exception as e:
        return None, f"voice download failed: {e}"
    if len(audio) > 19 * 1024 * 1024:
        return None, "voice file too large for bot download (20MB cap)"
    boundary, body = multipart(
        [("model", cfg.get("transcribe_model", "whisper-1"))],
        "file",
        "voice.oga",
        audio,
        "audio/ogg",
    )
    base = cfg.get("transcribe_base_url", "https://api.openai.com/v1").rstrip("/")
    req = urllib.request.Request(
        f"{base}/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {cfg['transcribe_key']}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        return (
            None,
            f"transcribe http {e.code}: {e.read().decode(errors='replace')[:150]}",
        )
    except Exception as e:
        return None, f"transcribe failed: {e}"
    text = (out or {}).get("text", "").strip()
    if not text:
        return None, "transcription empty"
    return text, None


def unpack_entry(entry):
    """Queue items arrive as (chat_id, message_id, prompt) tuples (prompt path)
    or dicts (scheduled /at jobs). Accept both — never crash the worker."""
    if isinstance(entry, dict):
        if entry.get("kind") == "prompt_batch":
            with INGRESS_LOCK:
                if PENDING_PROMPTS.get(entry["chat_id"]) is entry:
                    PENDING_PROMPTS.pop(entry["chat_id"], None)
                parts = list(entry.get("parts") or [])
            return entry["chat_id"], entry["message_id"], "\n\n".join(parts)
        return entry["chat_id"], entry["message_id"], entry["prompt"]
    return entry


def _flush_prompt_batch(cfg, chat_id, batch):
    """Commit one settled Telegram burst to steering or the serial queue."""
    with INGRESS_LOCK:
        if PENDING_PROMPTS.get(chat_id) is not batch or batch.get("queued"):
            return
        text = "\n\n".join(batch.get("parts") or []).strip()
        batch["timer"] = None
        if not text:
            PENDING_PROMPTS.pop(chat_id, None)
            return
        steer_target = _begin_steer(chat_id)
        if steer_target:
            PENDING_PROMPTS.pop(chat_id, None)
        else:
            batch["queued"] = True
    if steer_target:
        sid = steer_target["sid"]
        audit(
            "steer_received",
            chat_id=chat_id,
            session=sid,
            transport=steer_target["transport"],
            chars=len(text),
            messages=len(batch.get("parts") or []),
        )
        if steer_target["transport"] == "opencode_v2_steer":
            target = _steer_deliver_v2
            args = (
                cfg,
                sid,
                steer_target["run_id"],
                steer_target["base_url"],
                text,
                chat_id,
                batch["message_id"],
            )
        elif steer_target["transport"] == "codex_turn_steer":
            target = _codex_steer_deliver
            args = (
                cfg,
                sid,
                steer_target["turn_id"],
                steer_target["run_id"],
                text,
                chat_id,
                batch["message_id"],
            )
        else:
            raise RuntimeError(f"unknown steer transport {steer_target['transport']}")
        send(
            cfg["bot_token"],
            chat_id,
            "🧭 received — it will be injected into this agent after the current tool call",
        )
        threading.Thread(target=target, args=args, daemon=True).start()
        return

    PROMPT_Q.put(batch)
    with RUN_LOCK:
        busy = bool(RUN_STATE.get("busy"))
    if busy:
        send(
            cfg["bot_token"],
            chat_id,
            f"⏳ queued as one batch (position {PROMPT_Q.qsize()}) — /status for details",
        )


def defer_prompt(cfg, chat_id, message_id, text):
    """Merge adjacent messages, including Telegram's automatic text splits."""
    with INGRESS_LOCK:
        batch = PENDING_PROMPTS.get(chat_id)
        if batch:
            batch.setdefault("parts", []).append(text)
            audit(
                "input_merged",
                chat_id=chat_id,
                messages=len(batch["parts"]),
                chars=len(text),
            )
            if batch.get("queued"):
                return
            old_timer = batch.get("timer")
            if old_timer:
                old_timer.cancel()
        else:
            batch = {
                "kind": "prompt_batch",
                "chat_id": chat_id,
                "message_id": message_id,
                "parts": [text],
                "queued": False,
                "timer": None,
            }
            PENDING_PROMPTS[chat_id] = batch
        timer = threading.Timer(
            input_debounce(cfg), _flush_prompt_batch, args=(cfg, chat_id, batch)
        )
        timer.daemon = True
        batch["timer"] = timer
        timer.start()


def merge_open_burst(cfg, chat_id, text):
    """Attach an unaddressed split tail/media item to an addressed open burst."""
    with INGRESS_LOCK:
        batch = PENDING_PROMPTS.get(chat_id)
        if not batch or batch.get("queued"):
            return False
        batch.setdefault("parts", []).append(text)
        old_timer = batch.get("timer")
        if old_timer:
            old_timer.cancel()
        timer = threading.Timer(
            input_debounce(cfg), _flush_prompt_batch, args=(cfg, chat_id, batch)
        )
        timer.daemon = True
        batch["timer"] = timer
        timer.start()
        audit(
            "input_merged",
            chat_id=chat_id,
            messages=len(batch["parts"]),
            chars=len(text),
            unaddressed_tail=True,
        )
        return True


def startup_smoke():
    """Side-effect-free checks safe to run before every service start."""
    if md_to_html("**ok**")[0] != "<b>ok</b>":
        raise RuntimeError("render smoke failed")
    if split_chunks("a" * 25, limit=10) != ["a" * 10, "a" * 10, "a" * 5]:
        raise RuntimeError("chunk smoke failed")
    if runner_mode({}) != "cli" or not {"opencode", "claude", "codex"} <= set(RUNNERS):
        raise RuntimeError("runner registry smoke failed")


def selftest():
    """Run the exhaustive suite on demand and in the normal test runner."""
    from tgbridge_core.selftest import run_selftest

    run_selftest(sys.modules[__name__])


def worker(cfg, state):
    """Serial agent-run consumer; poll loop stays live for commands.

    One item = one try/except: a bad item must never kill the thread."""
    while True:
        entry = PROMPT_Q.get()
        chat_id = message_id = prompt = None
        try:
            chat_id, message_id, prompt = unpack_entry(entry)
            with STATE_LOCK:
                run_cfg = effective_run_config(cfg, state)
            rname = run_cfg.get("runner", "opencode")
            mode = resolve_runner_mode(run_cfg, rname)
            with RUN_LOCK:
                RUN_STATE["busy"] = True
                RUN_STATE["current"] = {
                    "chat": chat_id,
                    "since": time.time(),
                    "prompt": prompt[:60],
                    "runner": rname,
                    "mode": mode,
                }
            with STATE_LOCK:
                session_id = runner_session(
                    state,
                    chat_id,
                    rname,
                    legacy_runner=cfg.get("runner", "opencode"),
                )
            outbox = outbox_dir(cfg)
            prompt = prompt + (
                f"\n\n(To give files to the user, write them into {outbox}/ "
                "— they are delivered automatically after this run.)"
            )
            audit(
                "run_start",
                chat_id=chat_id,
                chars=len(prompt),
                session=session_id,
                runner=rname,
                mode=mode,
            )
            react(cfg, chat_id, message_id, "👀")
            status = api(
                cfg["bot_token"],
                "sendMessage",
                chat_id=chat_id,
                text="⚙️ working… 0s",
                disable_notification=True,
                reply_parameters=json.dumps({"message_id": message_id}),
            )
            live = {
                "chat_id": chat_id,
                "status_id": (status.get("result") or {}).get("message_id")
                if status
                else None,
                "trail": [],
                "start": time.time(),
                "last_edit": 0,
            }
            stop_typing = threading.Event()
            threading.Thread(
                target=typing_loop,
                args=(cfg["bot_token"], chat_id, stop_typing),
                daemon=True,
            ).start()
            log(
                f"chat={chat_id} run start (runner={rname}/{mode}, "
                f"session={session_id}, q={PROMPT_Q.qsize()})"
            )
            result_meta = {}
            try:
                new_sid, answer, err = run_with_fallbacks(
                    run_cfg,
                    session_id,
                    prompt,
                    live,
                    result_meta=result_meta,
                )
            except Exception as e:
                err = f"bridge error: {e}"
                new_sid, answer = session_id, None
            finally:
                stop_typing.set()
                with RUN_LOCK:
                    RUN_STATE["busy"] = False
                    RUN_STATE["current"] = None
            if (
                err
                and session_id
                and "failed rc=" in err
                and not err.startswith("all runners exhausted")
                and err != CANCEL_MSG
            ):
                live["trail"].append("♻️ stale session — retrying fresh")
                result_meta.clear()
                new_sid, answer, err = run_with_fallbacks(
                    run_cfg, None, prompt, live, result_meta=result_meta
                )
            if live["status_id"]:
                edit_status(cfg, live, final="✅ done" if not err else "🔴 failed")
            with STATE_LOCK:
                used_runner = result_meta.get("runner", rname)
                if new_sid:
                    store_runner_session(state, chat_id, used_runner, new_sid)
                save_json(STATE_PATH, state)
            if err == CANCEL_MSG:
                audit("run_cancelled", chat_id=chat_id)
                send_retry(cfg, chat_id, CANCEL_MSG)
                log(f"chat={chat_id} cancelled")
            elif err:
                react(cfg, chat_id, message_id, "👎")
                send_retry(cfg, chat_id, f"⚠️ {err}")
                audit("run_error", chat_id=chat_id, err=err[:200])
                log(f"chat={chat_id} error: {err[:120]}")
            else:
                react(cfg, chat_id, message_id, "👍")
                send_retry(cfg, chat_id, answer or "", reply_to=message_id)
                audit(
                    "run_done",
                    chat_id=chat_id,
                    runner=used_runner,
                    chars=len(answer or ""),
                    secs=int(time.time() - live["start"]),
                )
                log(
                    f"chat={chat_id} done ({len(answer or '')} chars, "
                    f"runner={used_runner}, session={new_sid})"
                )
        except Exception as e:
            log(f"worker item error: {e}")
            audit("worker_error", err=str(e)[:200])
            if chat_id:
                send(cfg["bot_token"], chat_id, f"⚠️ bridge error: {e}")
        finally:
            try:
                outbox = outbox_dir(cfg)
                for fn in sorted(os.listdir(outbox)):
                    p = os.path.join(outbox, fn)
                    if os.path.isfile(p) and chat_id is not None:
                        res = send_document(cfg["bot_token"], chat_id, p)
                        if res and res.get("ok"):
                            os.remove(p)  # keep the file if delivery failed
            except FileNotFoundError:
                pass
            except Exception as e:
                log(f"outbox delivery: {e}")
            PROMPT_Q.task_done()


def fire_at(cfg, state, due):
    with STATE_LOCK:
        entry = state.get("at", {}).pop(str(due), None)
        if entry:
            save_json(STATE_PATH, state)
    if not entry:
        return
    audit("at_fire", chat_id=entry["chat_id"], prompt=entry["prompt"][:80])
    PROMPT_Q.put((entry["chat_id"], entry["message_id"], entry["prompt"]))


def rearm_at(cfg, state):
    """Re-arm /at timers after a restart — nothing scheduled is silently lost."""
    for due in list(state.get("at", {})):
        try:
            d = float(due)
        except ValueError:
            continue
        t = threading.Timer(max(d - time.time(), 1), fire_at, args=(cfg, state, d))
        t.daemon = True
        t.start()


def save_attachment(cfg, msg):
    """Download an inbound document or highest-resolution Telegram photo."""
    kind = None
    item = msg.get("document") or {}
    if item.get("file_id"):
        kind = "attachment"
        name = os.path.basename(item.get("file_name") or "file") or "file"
    else:
        photos = msg.get("photo") or []
        item = photos[-1] if photos else {}
        if not item.get("file_id"):
            return None, None
        kind = "photo"
        name = f"photo-{item.get('file_unique_id') or msg.get('message_id') or 'image'}.jpg"
    fid = item.get("file_id")
    if not fid:
        return None, None
    r = api(cfg["bot_token"], "getFile", file_id=fid)
    fp = (r or {}).get("result", {}).get("file_path")
    if not fp:
        return None, None
    inbox = os.path.join(CONFIG_DIR, "inbox")
    ensure_private_dir(inbox)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    chat_id = (msg.get("chat") or {}).get("id", "chat")
    dest = os.path.join(inbox, f"{stamp}-{chat_id}-{msg.get('message_id', 'x')}-{name}")
    url = f"https://api.telegram.org/file/bot{cfg['bot_token']}/{fp}"
    try:
        urllib.request.urlretrieve(url, dest)
        os.chmod(dest, 0o600)
    except Exception as e:
        log(f"download {name}: {e}")
        try:
            os.unlink(dest)
        except OSError:
            pass
        return None, None
    log(f"{kind} saved: {dest}")
    audit("inbound_file", kind=kind, path=dest, bytes=os.path.getsize(dest))
    return kind, dest


def send_document(token, chat_id, path):
    boundary = "----tgbridge" + str(int(time.time() * 1000))
    fn = os.path.basename(path)
    with open(path, "rb") as f:
        data = f.read()
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="document"; filename="{fn}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except Exception as e:
        log(f"send_document {fn}: {e}")
        return None


def botcmd(text):
    """/cmd@BotName args -> ('/cmd', 'args'); non-commands -> (None, '')."""
    parts = text.split(maxsplit=1)
    if not parts or not parts[0].startswith("/"):
        return None, ""
    return parts[0].split("@")[0].lower(), (parts[1] if len(parts) > 1 else "").strip()


def is_authorized(cfg, chat_id, chat_type, user_id):
    """Gate access without weakening DMs or admitting messages from other chats.

    By default, both the chat and sender must be allowlisted. An installation may
    explicitly trust membership of an allowlisted group instead, which lets
    collaborators in that one group use the bot without collecting every
    member's Telegram user ID. Private chats always keep the sender allowlist.
    """
    if chat_id not in (cfg.get("allowed_chats") or []):
        return False
    if (
        chat_type in ("group", "supergroup")
        and user_id is not None
        and cfg.get("allow_all_users_in_allowed_groups", False)
    ):
        return True
    return user_id in (cfg.get("allowed_user_ids") or [])


def apply_sender_instructions(cfg, user_id, prompt):
    """Prepend trusted operator rules for one configured Telegram sender."""
    instructions = cfg.get("sender_instructions") or {}
    if not isinstance(instructions, dict) or user_id is None:
        return prompt
    rule = instructions.get(str(user_id))
    if not isinstance(rule, str) or not rule.strip():
        return prompt
    return (
        "[Operator-configured instructions for this Telegram sender]\n"
        + rule.strip()
        + "\n[/Operator-configured instructions]\n\n"
        + prompt
    )


def handle_update(cfg, state, upd, *, state_path=STATE_PATH):
    """Handle one Telegram update.

    ``state_path=None`` keeps pure/self-test calls from persisting fixture state
    into the live bridge store. Runtime callers use the real path by default.
    """
    msg = upd.get("message")
    if not msg:
        return
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    user_id = (msg.get("from") or {}).get("id")
    text = msg.get("text") or msg.get("caption") or ""
    message_id = msg.get("message_id")
    if not is_authorized(cfg, chat_id, chat_type, user_id):
        return
    bot_username = state.get("bot_username", "")
    has_attachment = bool(msg.get("document") or msg.get("photo"))
    attachment_kind = attachment_path = None
    if has_attachment:
        attachment_kind, attachment_path = save_attachment(cfg, msg)
    attachment_note = (
        f"[{attachment_kind} saved: {attachment_path}]" if attachment_path else ""
    )

    if chat_type != "private":
        reply = msg.get("reply_to_message") or {}
        replied_to_bot = (reply.get("from") or {}).get("username") == bot_username
        if f"@{bot_username}" not in text and not replied_to_bot:
            context_text = "\n".join(p for p in (text.strip(), attachment_note) if p)
            if context_text and merge_open_burst(cfg, chat_id, context_text):
                return
            if (
                cfg.get("capture_group_context", True)
                and context_text
                and not text.lstrip().startswith("/")
            ):
                with STATE_LOCK:
                    buf = state.setdefault("context", {}).setdefault(str(chat_id), [])
                    buf.append(
                        {
                            "who": (msg.get("from") or {}).get("first_name") or "?",
                            "t": time.strftime("%H:%M"),
                            "text": context_text[:500],
                        }
                    )
                    del buf[:-20]
                    if state_path is not None:
                        save_json(state_path, state)
                now = time.time()
                hints = state.get("hints", {})
                if now - hints.get(str(chat_id), 0) > 1800:
                    with STATE_LOCK:
                        state.setdefault("hints", {})[str(chat_id)] = now
                        if state_path is not None:
                            save_json(state_path, state)
                    send(
                        cfg["bot_token"],
                        chat_id,
                        f"💡 @{bot_username} at me (or reply to my messages) and I'll answer",
                    )
            return
        text = text.replace(f"@{bot_username}", "").strip()

    if attachment_note:
        text = "\n".join(p for p in (text, attachment_note) if p).strip()
    elif has_attachment and not text.strip():
        send(
            cfg["bot_token"], chat_id, "⚠️ I could not download that file from Telegram"
        )
        return

    cmd, rest = botcmd(text)
    if cmd == "/help":
        send(
            cfg["bot_token"],
            chat_id,
            "commands: /new reset session · /status state · /runners list agents · "
            "/runner <name> [model] switch agent · /at 30m <prompt> "
            "schedule · /cancel abort current run · anything else goes to the agent",
        )
        return
    if cmd == "/new":
        with STATE_LOCK:
            clear_runner_sessions(state, chat_id)
            if state_path is not None:
                save_json(state_path, state)
        send(cfg["bot_token"], chat_id, "session cleared. next message starts fresh.")
        return
    if cmd == "/cancel":
        with RUN_LOCK:
            proc = RUN_STATE.get("proc")
            cur = RUN_STATE.get("current")
            server_sid = RUN_STATE.get("server_sid")
            server_directory = RUN_STATE.get("server_directory")
            active_server_url = RUN_STATE.get("server_url")
            active_server_api = RUN_STATE.get("server_api")
        if server_sid:
            if not cur:
                send(cfg["bot_token"], chat_id, "nothing running")
                return
            if chat_type != "private" and cur["chat"] != chat_id:
                send(
                    cfg["bot_token"],
                    chat_id,
                    f"run belongs to chat {cur['chat']} — cancel from there",
                )
                return
            with RUN_LOCK:
                RUN_STATE["cancel"] = True
            try:
                _server_call(
                    "POST",
                    (
                        f"/api/session/{server_sid}/interrupt"
                        if active_server_api == "v2"
                        else f"/session/{server_sid}/abort"
                    ),
                    timeout=5,
                    directory=server_directory if active_server_api != "v2" else None,
                    base_url=active_server_url,
                )
            except Exception as e:
                log(f"server abort request: {e}")
            audit("cancel_requested", by_chat=chat_id, run_chat=cur["chat"])
            send(cfg["bot_token"], chat_id, "🛑 stopping current run…")
            return
        if not (proc and cur and proc.poll() is None):
            send(cfg["bot_token"], chat_id, "nothing running")
            return
        if chat_type != "private" and cur["chat"] != chat_id:
            send(
                cfg["bot_token"],
                chat_id,
                f"run belongs to chat {cur['chat']} — cancel from there",
            )
            return
        signal_run_process(proc, signal.SIGTERM)
        kill_after(proc, 5)
        with RUN_LOCK:
            RUN_STATE["cancel"] = True
        audit("cancel_requested", by_chat=chat_id, run_chat=cur["chat"])
        send(cfg["bot_token"], chat_id, "🛑 stopping current run…")
        return
    if cmd == "/status":
        status_cfg = effective_run_config(cfg, state)
        runner_name = status_cfg.get("runner", "opencode")
        with STATE_LOCK:
            info = (
                runner_session(
                    state,
                    chat_id,
                    runner_name,
                    legacy_runner=cfg.get("runner", "opencode"),
                )
                or "(none)"
            )
            pending = sum(
                1 for e in state.get("at", {}).values() if e.get("chat_id") == chat_id
            )
        with INGRESS_LOCK:
            incoming = PENDING_PROMPTS.get(chat_id)
            incoming_count = len((incoming or {}).get("parts") or [])
        with RUN_LOCK:
            c = RUN_STATE.get("current")
        health = load_json(HEALTH_PATH, {})
        poll_health = health.get("status", "unknown")
        failures = health.get("consecutive_poll_failures", 0)
        cur = ""
        if c:
            cur = f"\nrunning: {c['prompt']}… ({int(time.time() - c['since'])}s)"
        mode = resolve_runner_mode(status_cfg, runner_name)
        capability = (SERVER_RUNNERS.get(runner_name) or {}).get("feature")
        mode_label = (
            f"{mode}: {capability}" if mode == "server" and capability else mode
        )
        if runner_name == "codex":
            policy = (
                "yolo"
                if cfg.get("codex_yolo")
                else ("read-only" if mode == "server" else "default permissions")
            )
            mode_label += f", {policy}"
        model_label = resolve_model(status_cfg, runner_name) or "runner default"
        chain = fallback_chain(status_cfg)
        chain_label = (
            "none"
            if not chain
            else " -> ".join(f"{r}/{m or 'runner default'}" for r, m in chain)
        )
        override_note = ""
        if (state.get("runner_override") or {}).get("runner"):
            override_note = f" (override, file says {cfg.get('runner', 'opencode')})"
        send(
            cfg["bot_token"],
            chat_id,
            f"chat {chat_id}\nrunner: {runner_name} ({mode_label}){override_note}\n"
            f"model: {model_label}\nfallbacks: {chain_label}\n"
            f"session: {info}\ncwd: {cfg['workdir']}\n"
            f"incoming parts: {incoming_count}\nqueued batches: {PROMPT_Q.qsize()}\n"
            f"scheduled: {pending}\ntelegram poll: {poll_health} "
            f"(failures: {failures}){cur}",
        )
        return
    if cmd == "/runners":
        probe = probe_runners()
        eff = effective_run_config(cfg, state)
        lines = [
            f"{'●' if p['available'] else '○'} {name}"
            + ("" if p["available"] else f" — {p['detail']}")
            for name, p in probe.items()
        ]
        chain = fallback_chain(eff)
        lines.append(
            "fallbacks: "
            + (
                "none"
                if not chain
                else " -> ".join(f"{r}/{m or 'runner default'}" for r, m in chain)
            )
        )
        ov = (state.get("runner_override") or {}).get("runner")
        lines.append(
            f"primary: {eff.get('runner', 'opencode')}"
            + (
                f" (via /runner, file says {cfg.get('runner', 'opencode')})"
                if ov
                else ""
            )
        )
        send(cfg["bot_token"], chat_id, "runners:\n" + "\n".join(lines))
        return
    if cmd == "/runner":
        args = (rest or "").split()
        if not args:
            eff = effective_run_config(cfg, state)
            send(
                cfg["bot_token"],
                chat_id,
                f"primary: {eff.get('runner', 'opencode')} "
                f"({resolve_model(eff, eff.get('runner', 'opencode')) or 'runner default'})\n"
                "usage: /runner <name> [model] · /runner default",
            )
            return
        if args[0] == "default":
            with STATE_LOCK:
                state.pop("runner_override", None)
                if state_path is not None:
                    save_json(state_path, state)
            audit("runner_override_cleared", chat_id=chat_id)
            send(
                cfg["bot_token"],
                chat_id,
                f"primary back to file config: {cfg.get('runner', 'opencode')}",
            )
            return
        name, model = args[0], (args[1] if len(args) > 1 else "")
        if name not in RUNNERS:
            send(
                cfg["bot_token"],
                chat_id,
                f"unknown runner {name!r} (available: {', '.join(sorted(RUNNERS))})",
            )
            return
        with STATE_LOCK:
            state["runner_override"] = {"runner": name, "model": model}
            if state_path is not None:
                save_json(state_path, state)
        audit("runner_override_set", chat_id=chat_id, runner=name, model=model)
        send(
            cfg["bot_token"],
            chat_id,
            f"primary now {name} ({model or 'runner default'}) — applies to the next run",
        )
        return
    if cmd == "/at":
        m = re.match(r"^(\d+)([smh])\s+(.+)$", rest, re.I)
        if not m:
            send(cfg["bot_token"], chat_id, "usage: /at 30m <prompt>  (s/m/h, max 7d)")
            return
        delay = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2).lower()]
        if delay > 7 * 86400:
            send(cfg["bot_token"], chat_id, "/at max is 7d")
            return
        due = int(time.time()) + delay
        prompt = apply_sender_instructions(cfg, user_id, m.group(3).strip())
        with STATE_LOCK:
            state.setdefault("at", {})[str(due)] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "prompt": prompt,
            }
            if state_path is not None:
                save_json(state_path, state)
        t = threading.Timer(delay, fire_at, args=(cfg, state, due))
        t.daemon = True
        t.start()
        audit("at_set", chat_id=chat_id, delay=delay, prompt=prompt[:80])
        send(
            cfg["bot_token"],
            chat_id,
            f"⏰ scheduled in {delay}s — fires via this chat's session",
        )
        return

    if msg.get("voice") and not text.strip():
        if not cfg.get("transcribe_key"):
            send(
                cfg["bot_token"],
                chat_id,
                "🎤 voice note received but transcription not configured "
                "(set transcribe_key / transcribe_base_url / transcribe_model)",
            )
            return
        text, err = transcribe(cfg, cfg["bot_token"], msg["voice"].get("file_id"))
        if err:
            react(cfg, chat_id, message_id, "👎")
            send(cfg["bot_token"], chat_id, f"⚠️ {err}")
            return
        audit("voice", chat_id=chat_id, chars=len(text or ""))
        text = f"(voice note) {text}"

    if not text.strip():
        return

    audit("enqueue", chat_id=chat_id, user_id=user_id, chars=len(text.strip()))
    prompt_text = text.strip()
    if chat_type != "private" and cfg.get("capture_group_context", True):
        with STATE_LOCK:
            buf = (state.get("context") or {}).pop(str(chat_id), None) or []
        if buf:
            digest = "\n".join(f"- {e['who']} {e['t']}: {e['text']}" for e in buf[-20:])
            prompt_text = (
                "[group messages since your last turn — passive context, "
                "nobody asked you anything yet:\n" + digest + "\n]\n\n" + prompt_text
            )
    prompt_text = apply_sender_instructions(cfg, user_id, prompt_text)
    defer_prompt(cfg, chat_id, message_id, prompt_text)


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
    if not cfg:
        report["config"] = {"ok": False, "error": "missing or invalid config"}
        return report
    report["config"] = {"ok": True}
    state_parent = os.path.dirname(STATE_PATH)
    state_writable = os.access(state_parent, os.W_OK) and (
        not os.path.exists(STATE_PATH) or os.access(STATE_PATH, os.W_OK)
    )
    report["state"] = {"writable": state_writable}

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
    report["ok"] = bool(state_writable and runner_check["ok"] and telegram_ok)
    return report


def cli_doctor():
    report = doctor_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


class BridgeStop(Exception):
    """Raised by the SIGTERM/SIGINT handler — a graceful stop, not a crash."""


def on_stop(signum, frame):
    raise BridgeStop(signum)


def run(cfg):
    update_health(
        status="starting",
        pid=os.getpid(),
        started_at=now_iso(),
        consecutive_poll_failures=0,
    )
    state = load_json(STATE_PATH, {})
    if not cfg.get("capture_group_context", True):
        state.pop("context", None)
        state.pop("hints", None)
    me = None
    for attempt in range(1, 6):
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
        except Exception as e:
            log(f"getMe attempt {attempt}/5 transient {type(e).__name__}: {e}")
        me = None
        time.sleep(3)
    if not me or not me.get("ok"):
        sys.exit(
            "getMe failed after 5 retries: Telegram unreachable (token not verified)"
        )
    state["bot_username"] = me["result"]["username"]
    save_json(STATE_PATH, state)
    api(
        cfg["bot_token"],
        "setMyCommands",
        commands=json.dumps(
            [
                {"command": "new", "description": "Reset session for this chat"},
                {"command": "status", "description": "Show session info"},
                {"command": "runners", "description": "List available agents"},
                {
                    "command": "runner",
                    "description": "Switch agent: /runner <name> [model]",
                },
                {"command": "at", "description": "Schedule a prompt: /at 30m <text>"},
                {"command": "cancel", "description": "Abort the current run"},
                {"command": "help", "description": "List commands"},
            ]
        ),
    )
    log(f"tgbridge up as @{state['bot_username']}, chats={cfg['allowed_chats']}")
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

    worker_t = threading.Thread(target=worker, args=(cfg, state), daemon=True)
    worker_t.start()
    rearm_at(cfg, state)

    offset = state.get("offset")
    backoff = 3
    poll_failures = 0
    try:
        failure_exit_threshold = max(0, int(cfg.get("poll_failure_exit_threshold", 20)))
    except (TypeError, ValueError):
        failure_exit_threshold = 20
    while True:
        if not worker_t.is_alive():
            log("worker thread died — respawning")
            audit("worker_respawn")
            announce_all(cfg, "💀 bridge worker thread died — respawned")
            worker_t = threading.Thread(target=worker, args=(cfg, state), daemon=True)
            worker_t.start()
        params = {"timeout": 50, "allowed_updates": json.dumps(["message"])}
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
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    try:
        run(cfg)
    except BridgeStop:
        update_health(status="stopped", stopped_at=now_iso())
        with RUN_LOCK:
            p = RUN_STATE.get("proc")
            server_sid = RUN_STATE.get("server_sid")
            server_directory = RUN_STATE.get("server_directory")
            active_server_url = RUN_STATE.get("server_url")
            active_server_api = RUN_STATE.get("server_api")
        if p is not None and p.poll() is None:
            signal_run_process(p, signal.SIGTERM)  # don't orphan a burning agent run
            kill_after(p, 2)
        if server_sid:
            try:
                _server_call(
                    "POST",
                    (
                        f"/api/session/{server_sid}/interrupt"
                        if active_server_api == "v2"
                        else f"/session/{server_sid}/abort"
                    ),
                    timeout=5,
                    directory=server_directory if active_server_api != "v2" else None,
                    base_url=active_server_url,
                )
            except Exception:
                pass
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


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--send":
        cli_send(sys.argv[2:])
    else:
        main()
