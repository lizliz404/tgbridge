#!/usr/bin/env python3
"""tgbridge - minimal Telegram bridge for local opencode agent.

Bot API long-poll -> gate (chat allowlist + user allowlist + group trigger)
-> opencode run (per-chat session, --format json) -> reply to source chat.

Architecture: the poll loop never blocks. Slash commands are answered inline;
prompts are enqueued and consumed serially by a worker thread. Agent stdout
is streamed live via Popen, so the status message shows a real-time tool
trail and answer preview (claudegram/xhyu/OpenClaw pattern).

Extra surfaces: `tgbridge.py --send <chat_id> <text>` lets the agent itself
post to allowlisted chats (Telegram-Bridge-MCP idea, no MCP protocol).
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_PATH = os.path.expanduser("~/.config/tgbridge/config.json")
STATE_PATH = os.path.expanduser("~/.config/tgbridge/state.json")
AUDIT_PATH = os.path.expanduser("~/.config/tgbridge/audit.jsonl")
OPENCODE = os.environ.get(
    "OPENCODE_BIN", os.path.expanduser("~/.local/share/mise/shims/opencode")
)
RUN_TIMEOUT_S = 900
CHUNK = 3900

PROMPT_Q = queue.Queue()
STATE_LOCK = threading.Lock()
AUDIT_LOCK = threading.Lock()
RUN_STATE: dict = {"busy": False, "current": None}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def audit(event, **fields):
    """Append-only JSONL trail of bridge actions (claude-code-telegram pattern)."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
    try:
        with AUDIT_LOCK, open(AUDIT_PATH, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def api(token, method, **params):
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
            log(f"api {method} error: {e}")
            return None
    return None


def react(token, chat_id, message_id, emoji):
    api(
        token,
        "setMessageReaction",
        chat_id=chat_id,
        message_id=message_id,
        reaction=json.dumps([{"type": "emoji", "emoji": emoji}]),
    )


def split_chunks(text, limit=CHUNK):
    if len(text) <= limit:
        return [text]
    chunks, rest = [], text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind("\n\n", 0, limit)
        if cut < 1:
            cut = rest.rfind("\n", 0, limit)
        if cut < 1:
            cut = rest.rfind(" ", 0, limit)
        if cut < 1:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return [c for c in chunks if c] or [""]


def send(token, chat_id, text, reply_to=None):
    ok = True
    for i, chunk in enumerate(split_chunks(text)):
        params = {"chat_id": chat_id, "text": chunk}
        if i == 0 and reply_to:
            params["reply_parameters"] = json.dumps({"message_id": reply_to})
        res = api(token, "sendMessage", **params)
        if not res or not res.get("ok"):
            ok = False
    return ok


def typing_loop(token, chat_id, stop_event):
    while not stop_event.wait(4.0):
        api(token, "sendChatAction", chat_id=chat_id, action="typing")


def trail_line(part):
    tool = part.get("tool", "?")
    st = part.get("state") or {}
    inp = st.get("input") or {}
    summary = ""
    for v in inp.values():
        if isinstance(v, str) and len(v) > len(summary):
            summary = v
    summary = summary.replace("\n", " ")[:60]
    return f"🔧 {tool}: {summary}" if summary else f"🔧 {tool}"


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


def _bin(env_key, name):
    p = os.environ.get(env_key) or shutil.which(name)
    if not p:
        raise RunnerError(
            f"runner {name!r} not found on PATH; install it or set {env_key}"
        )
    return p


RUNNERS = {}


def runner(name):
    def deco(fn):
        RUNNERS[name] = fn
        return fn
    return deco


class RunnerError(Exception):
    pass


@runner("opencode")
def _opencode(session_id, prompt):
    """opencode run --format json. Events: sessionID / tool_use / text / reasoning."""
    cmd = [OPENCODE, "run", "--format", "json"]
    if session_id:
        cmd += ["--session", session_id]
    else:
        cmd += ["--title", time.strftime("tg %Y%m%d-%H%M")]
    cmd.append(prompt)

    def parse(ev, acc):
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        if ev.get("sessionID"):
            acc["sid"] = ev["sessionID"]
        t = ev.get("type")
        if t == "tool_use":
            return trail_line(part)
        if t == "text" and part.get("text"):
            acc["texts"].append(part["text"])
            acc["thinking"] = None
        elif t == "reasoning" and part.get("text"):
            acc["thinking"] = part["text"]
        elif t == "step_finish":
            acc["cost"] = acc.get("cost", 0.0) + (part.get("cost") or 0.0)
            acc["tokens"] = (part.get("tokens") or {}).get("total")
        return None

    return cmd, parse


@runner("claude")
def _claude(session_id, prompt):
    """claude -p --output-format stream-json (resume via --resume)."""
    cmd = [_bin("CLAUDE_BIN", "claude"), "-p", prompt,
           "--output-format", "stream-json", "--verbose"]
    if session_id:
        cmd += ["--resume", session_id]

    def parse(ev, acc):
        t = ev.get("type")
        if t == "system" and ev.get("session_id"):
            acc["sid"] = ev["session_id"]
        if t == "assistant":
            for blk in (ev.get("message") or {}).get("content") or []:
                bt = blk.get("type")
                if bt == "tool_use":
                    inp = blk.get("input") or {}
                    s = next((v for v in inp.values() if isinstance(v, str)), "")
                    return (f"🔧 {blk.get('tool', '?')}: "
                            + s.replace("\n", " ")[:60])
                if bt == "text" and blk.get("text"):
                    acc["texts"].append(blk["text"])
                    acc["thinking"] = None
                elif bt == "thinking" and blk.get("thinking"):
                    acc["thinking"] = blk["thinking"]
        return None

    return cmd, parse


@runner("codex")
def _codex(session_id, prompt):
    """codex exec --json (resume via `codex exec resume <id>`). Best-effort."""
    cmd = [_bin("CODEX_BIN", "codex"), "exec", "--json"]
    if session_id:
        cmd += ["resume", session_id]
    cmd.append(prompt)

    def parse(ev, acc):
        t = ev.get("type")
        if t == "thread.started" and ev.get("thread_id"):
            acc["sid"] = ev["thread_id"]
        item = ev.get("item") or {}
        it = item.get("type")
        if t in ("item.started", "item.completed") and it == "command_execution":
            return "🔧 bash: " + (item.get("command") or "")[:60]
        if it == "reasoning":
            acc["thinking"] = (item.get("text") or "")[:200] or acc.get("thinking")
        if it == "agent_message" and item.get("text"):
            acc["texts"].append(item["text"])
            acc["thinking"] = None
        return None

    return cmd, parse


def run_agent(cfg, session_id, prompt, live=None):
    """Stream the runner's JSON events live (Popen).

    A watchdog Timer enforces RUN_TIMEOUT_S without blocking the read loop;
    stderr is drained on a side thread so the pipe can never fill and deadlock.
    """
    rname = cfg.get("runner", "opencode")
    runner_fn = RUNNERS.get(rname)
    if not runner_fn:
        return session_id, None, (
            f"unknown runner {rname!r} (available: {', '.join(sorted(RUNNERS))})"
        )
    try:
        cmd, parse = runner_fn(session_id, prompt)
    except RunnerError as e:
        return session_id, None, str(e)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cfg["workdir"],
    )
    assert proc.stdout and proc.stderr  # guaranteed: both opened with PIPE
    timed_out = []
    killer = threading.Timer(RUN_TIMEOUT_S, lambda: (timed_out.append(1), proc.kill()))
    killer.daemon = True
    killer.start()
    errbuf = []
    stderr = proc.stderr
    drain = threading.Thread(
        target=lambda: errbuf.append(stderr.read() or ""), daemon=True
    )
    drain.start()
    acc = {"sid": session_id, "texts": [], "thinking": None, "cost": 0.0, "tokens": None}
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
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
        killer.cancel()
    if timed_out:
        partial = "\n".join(acc["texts"]).strip()
        if partial:
            return (
                sid,
                partial
                + "\n\n⚠️ (partial answer — hit the %ss timeout and was killed)"
                % RUN_TIMEOUT_S,
                None,
            )
        return sid, None, "agent timed out after %ss and was killed" % RUN_TIMEOUT_S
    if proc.returncode != 0:
        tail = (errbuf[0] if errbuf else "").strip()[-600:]
        return sid, None, f"opencode failed rc={proc.returncode}\n{tail}"
    if not acc["texts"]:
        return sid, None, "agent returned no text"
    return sid, "\n".join(acc["texts"]).strip(), None


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


def worker(cfg, state):
    """Serial agent-run consumer; poll loop stays live for commands."""
    while True:
        entry = PROMPT_Q.get()
        chat_id, message_id, prompt = (
            entry["chat_id"],
            entry["message_id"],
            entry["prompt"],
        )
        RUN_STATE["busy"] = True
        RUN_STATE["current"] = {
            "chat": chat_id,
            "since": time.time(),
            "prompt": prompt[:60],
        }
        with STATE_LOCK:
            session_id = state.get("sessions", {}).get(str(chat_id))
            pending = state.get("pending", [])
            for i, e in enumerate(pending):
                if e.get("message_id") == message_id and e.get("chat_id") == chat_id:
                    del pending[i]
                    break
        outbox = os.path.join(cfg["workdir"], ".tgbridge-outbox")
        prompt = prompt + (
            f"\n\n(To give files to the user, write them into {outbox}/ "
            "— they are delivered automatically after this run.)"
        )
        audit("run_start", chat_id=chat_id, chars=len(prompt), session=session_id)
        react(cfg["bot_token"], chat_id, message_id, "👀")
        status = api(
            cfg["bot_token"],
            "sendMessage",
            chat_id=chat_id,
            text="⚙️ working… 0s",
            reply_parameters=json.dumps({"message_id": message_id}),
            disable_notification=True,
        )
        live = {
            "chat_id": chat_id,
            "status_id": (status.get("result") or {}).get("message_id")
            if status
            else None,
            "trail": [],
            "start": time.time(),
            "last_edit": 0,
            "cost": 0.0,
            "tokens": None,
        }
        with STATE_LOCK:
            state["running"] = dict(entry, status_id=live["status_id"])
            save_json(STATE_PATH, state)
        stop_typing = threading.Event()
        threading.Thread(
            target=typing_loop,
            args=(cfg["bot_token"], chat_id, stop_typing),
            daemon=True,
        ).start()
        log(f"chat={chat_id} run start (session={session_id}, q={PROMPT_Q.qsize()})")
        try:
            new_sid, answer, err = run_agent(cfg, session_id, prompt, live)
        except Exception as e:
            err = f"bridge error: {e}"
            new_sid, answer = session_id, None
        finally:
            stop_typing.set()
            RUN_STATE["busy"] = False
            RUN_STATE["current"] = None
        if err and session_id and "opencode failed" in err:
            live["trail"].append("♻️ stale session — retrying fresh")
            new_sid, answer, err = run_agent(cfg, None, prompt, live)
        if live["status_id"]:
            edit_status(cfg, live, final="✅ done" if not err else "🔴 failed")
        with STATE_LOCK:
            if new_sid and new_sid != session_id:
                state.setdefault("sessions", {})[str(chat_id)] = new_sid
            save_json(STATE_PATH, state)
        if err:
            audit("run_error", chat_id=chat_id, err=err[:200])
            react(cfg["bot_token"], chat_id, message_id, "👎")
            send(cfg["bot_token"], chat_id, f"⚠️ {err}")
            log(f"chat={chat_id} error: {err[:120]}")
        else:
            audit(
                "run_done",
                chat_id=chat_id,
                chars=len(answer or ""),
                secs=int(time.time() - live["start"]),
            )
            react(cfg["bot_token"], chat_id, message_id, "👍")
            send(cfg["bot_token"], chat_id, answer or "", reply_to=message_id)
            log(f"chat={chat_id} done ({len(answer or '')} chars, session={new_sid})")
        try:
            for fn in sorted(os.listdir(outbox)):
                p = os.path.join(outbox, fn)
                if os.path.isfile(p):
                    send_document(cfg["bot_token"], chat_id, p)
                    os.remove(p)
        except FileNotFoundError:
            pass
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


def save_document(cfg, msg):
    doc = msg.get("document") or {}
    fid = doc.get("file_id")
    if not fid:
        return None
    name = os.path.basename(doc.get("file_name") or "file") or "file"
    r = api(cfg["bot_token"], "getFile", file_id=fid)
    fp = (r or {}).get("result", {}).get("file_path")
    if not fp:
        return None
    inbox = os.path.expanduser("~/.config/tgbridge/inbox")
    os.makedirs(inbox, exist_ok=True)
    dest = os.path.join(inbox, time.strftime("%Y%m%d-%H%M%S") + "-" + name)
    url = f"https://api.telegram.org/file/bot{cfg['bot_token']}/{fp}"
    try:
        urllib.request.urlretrieve(url, dest)
    except Exception as e:
        log(f"download {name}: {e}")
        return None
    log(f"attachment saved: {dest}")
    return dest


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


def handle_update(cfg, state, upd):
    msg = upd.get("message")
    if not msg:
        return
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    user_id = (msg.get("from") or {}).get("id")
    text = msg.get("text") or msg.get("caption") or ""
    message_id = msg.get("message_id")
    if chat_id not in cfg["allowed_chats"] or user_id not in cfg["allowed_user_ids"]:
        return
    bot_username = state.get("bot_username", "")

    if chat_type != "private":
        reply = msg.get("reply_to_message") or {}
        replied_to_bot = (reply.get("from") or {}).get("username") == bot_username
        if f"@{bot_username}" not in text and not replied_to_bot:
            if text.strip() and not text.lstrip().startswith("/"):
                with STATE_LOCK:
                    buf = state.setdefault("context", {}).setdefault(str(chat_id), [])
                    buf.append({
                        "who": (msg.get("from") or {}).get("first_name") or "?",
                        "t": time.strftime("%H:%M"),
                        "text": text.strip()[:200],
                    })
                    del buf[:-20]
                now = time.time()
                hints = state.get("hints", {})
                if now - hints.get(str(chat_id), 0) > 1800:
                    with STATE_LOCK:
                        state.setdefault("hints", {})[str(chat_id)] = now
                        save_json(STATE_PATH, state)
                    send(
                        cfg["bot_token"],
                        chat_id,
                        f"💡 @{bot_username} at me (or reply to my messages) and I'll answer",
                    )
            return
        text = text.replace(f"@{bot_username}", "").strip()

    if msg.get("document"):
        doc_path = save_document(cfg, msg)
        if doc_path:
            text = (text + f"\n[attachment saved: {doc_path}]").strip()

    cmd, rest = botcmd(text)
    if cmd == "/new":
        with STATE_LOCK:
            state.setdefault("sessions", {}).pop(str(chat_id), None)
            save_json(STATE_PATH, state)
        send(cfg["bot_token"], chat_id, "session cleared. next message starts fresh.")
        return
    if cmd == "/status":
        with STATE_LOCK:
            info = state.get("sessions", {}).get(str(chat_id)) or "(none)"
            pending = sum(
                1 for e in state.get("at", {}).values() if e.get("chat_id") == chat_id
            )
        cur = ""
        if RUN_STATE.get("current"):
            c = RUN_STATE["current"]
            cur = f"\nrunning: {c['prompt']}… ({int(time.time() - c['since'])}s)"
        send(
            cfg["bot_token"],
            chat_id,
            f"chat {chat_id}\nsession: {info}\ncwd: {cfg['workdir']}\n"
            f"queued: {PROMPT_Q.qsize()}\nscheduled: {pending}{cur}",
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
        prompt = m.group(3).strip()
        with STATE_LOCK:
            state.setdefault("at", {})[str(due)] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "prompt": prompt,
            }
            save_json(STATE_PATH, state)
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
            react(cfg["bot_token"], chat_id, message_id, "👎")
            send(cfg["bot_token"], chat_id, f"⚠️ {err}")
            return
        audit("voice", chat_id=chat_id, chars=len(text or ""))
        text = f"(voice note) {text}"

    if not text.strip():
        return

    audit("enqueue", chat_id=chat_id, user_id=user_id, chars=len(text.strip()))
    prompt_text = text.strip()
    if chat_type != "private":
        with STATE_LOCK:
            buf = (state.get("context") or {}).pop(str(chat_id), None) or []
        if buf:
            digest = "\n".join(
                f"- {e['who']} {e['t']}: {e['text']}" for e in buf[-20:]
            )
            prompt_text = (
                "[group messages since your last turn — passive context, "
                "nobody asked you anything yet:\n" + digest + "\n]\n\n" + prompt_text
            )
    PROMPT_Q.put((chat_id, message_id, prompt_text))
    if RUN_STATE["busy"]:
        send(
            cfg["bot_token"],
            chat_id,
            f"⏳ queued (position {PROMPT_Q.qsize()}) — /status for details",
        )


def cli_send(args):
    """Agent-initiated outbound: tgbridge.py --send <chat_id> <text>."""
    if len(args) < 2:
        sys.exit("usage: tgbridge.py --send <chat_id> <text>")
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


def main():
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit(f"missing config {CONFIG_PATH}")
    state = load_json(STATE_PATH, {})
    me = api(cfg["bot_token"], "getMe")
    if not me or not me.get("ok"):
        sys.exit("getMe failed: bad token?")
    state["bot_username"] = me["result"]["username"]
    save_json(STATE_PATH, state)
    api(
        cfg["bot_token"],
        "setMyCommands",
        commands=json.dumps(
            [
                {"command": "new", "description": "Reset session for this chat"},
                {"command": "status", "description": "Show session info"},
                {"command": "at", "description": "Schedule a prompt: /at 30m <text>"},
            ]
        ),
    )
    log(f"tgbridge up as @{state['bot_username']}, chats={cfg['allowed_chats']}")

    threading.Thread(target=worker, args=(cfg, state), daemon=True).start()
    rearm_at(cfg, state)

    offset = state.get("offset")
    backoff = 3
    while True:
        params = {"timeout": 50, "allowed_updates": json.dumps(["message"])}
        if offset:
            params["offset"] = offset
        res = api(cfg["bot_token"], "getUpdates", **params)
        if not res or not res.get("ok"):
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        backoff = 3
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


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--send":
        cli_send(sys.argv[2:])
    else:
        main()
