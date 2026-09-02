#!/usr/bin/env python3
"""tgbridge - minimal Telegram bridge for local opencode agent.

Bot API long-poll -> gate (chat allowlist + user allowlist + group trigger)
-> opencode run (per-chat session, --format json) -> reply to source chat.

Architecture: the poll loop never blocks. Slash commands are answered inline;
prompts are enqueued and consumed serially by a worker thread.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_PATH = os.path.expanduser("~/.config/tgbridge/config.json")
STATE_PATH = os.path.expanduser("~/.config/tgbridge/state.json")
OPENCODE = os.environ.get(
    "OPENCODE_BIN", os.path.expanduser("~/.local/share/mise/shims/opencode")
)
RUN_TIMEOUT_S = 900
CHUNK = 3900

PROMPT_Q = queue.Queue()
STATE_LOCK = threading.Lock()
RUN_STATE = {"busy": False}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


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
    for i, chunk in enumerate(split_chunks(text)):
        params = {"chat_id": chat_id, "text": chunk}
        if i == 0 and reply_to:
            params["reply_parameters"] = json.dumps({"message_id": reply_to})
        api(token, "sendMessage", **params)


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
        text = f"{final} · {elapsed}s · {len(live['trail'])} tool calls"
    else:
        trail = "\n".join(live["trail"][-5:])
        text = f"⚙️ working… {elapsed}s\n{trail}"
    if live.get("status_id"):
        api(
            cfg["bot_token"],
            "editMessageText",
            chat_id=live["chat_id"],
            message_id=live["status_id"],
            text=text,
        )


def run_agent(cfg, session_id, prompt, live=None):
    cmd = [OPENCODE, "run", "--format", "json"]
    if session_id:
        cmd += ["--session", session_id]
    cmd.append(prompt)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_S,
            cwd=cfg["workdir"],
        )
    except subprocess.TimeoutExpired:
        return (
            session_id,
            None,
            "agent timed out after %ss and was killed" % RUN_TIMEOUT_S,
        )
    sid, texts = session_id, []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = ev.get("sessionID") or sid
        if ev.get("type") == "tool_use" and live is not None:
            live["trail"].append(trail_line(ev.get("part") or {}))
            edit_status(cfg, live)
        elif ev.get("type") == "text":
            part = ev.get("part") or {}
            if isinstance(part, dict) and part.get("text"):
                texts.append(part["text"])
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-600:]
        return sid, None, f"opencode failed rc={proc.returncode}\n{tail}"
    if not texts:
        return sid, None, "agent returned no text"
    return sid, "\n".join(texts).strip(), None


def worker(cfg, state):
    """Serial agent-run consumer; poll loop stays live for commands."""
    while True:
        chat_id, message_id, prompt = PROMPT_Q.get()
        RUN_STATE["busy"] = True
        with STATE_LOCK:
            session_id = state.get("sessions", {}).get(str(chat_id))
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
        }
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
            react(cfg["bot_token"], chat_id, message_id, "🔴")
            send(cfg["bot_token"], chat_id, f"⚠️ {err}")
            log(f"chat={chat_id} error: {err[:120]}")
        else:
            react(cfg["bot_token"], chat_id, message_id, "✅")
            send(cfg["bot_token"], chat_id, answer or "", reply_to=message_id)
            log(f"chat={chat_id} done ({len(answer or '')} chars, session={new_sid})")
        PROMPT_Q.task_done()


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
            return
        text = text.replace(f"@{bot_username}", "").strip()

    if text in ("/new", f"/new@{bot_username}"):
        with STATE_LOCK:
            state.setdefault("sessions", {}).pop(str(chat_id), None)
            save_json(STATE_PATH, state)
        send(cfg["bot_token"], chat_id, "session cleared. next message starts fresh.")
        return
    if text in ("/status", f"/status@{bot_username}"):
        with STATE_LOCK:
            info = state.get("sessions", {}).get(str(chat_id)) or "(none)"
        send(
            cfg["bot_token"],
            chat_id,
            f"chat {chat_id}\nsession: {info}\ncwd: {cfg['workdir']}\n"
            f"queued: {PROMPT_Q.qsize()}",
        )
        return
    if not text.strip():
        return

    PROMPT_Q.put((chat_id, message_id, text.strip()))
    if RUN_STATE["busy"]:
        send(
            cfg["bot_token"],
            chat_id,
            f"⏳ queued (position {PROMPT_Q.qsize()}) — /status for details",
        )


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
            ]
        ),
    )
    log(f"tgbridge up as @{state['bot_username']}, chats={cfg['allowed_chats']}")

    threading.Thread(target=worker, args=(cfg, state), daemon=True).start()

    offset = state.get("offset")
    while True:
        params = {"timeout": 50, "allowed_updates": json.dumps(["message"])}
        if offset:
            params["offset"] = offset
        res = api(cfg["bot_token"], "getUpdates", **params)
        if not res or not res.get("ok"):
            time.sleep(3)
            continue
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
    main()
