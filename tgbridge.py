#!/usr/bin/env python3
"""tgbridge - minimal Telegram bridge for local opencode agent.

Bot API long-poll -> gate (chat allowlist + user allowlist + group trigger)
-> opencode run (per-chat session, --format json) -> reply to source chat.
Reactions: eyes on accept, check on done, red circle on error.
"""

import json
import os
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


def run_agent(cfg, session_id, prompt):
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
        if ev.get("type") == "text":
            part = ev.get("part") or {}
            if isinstance(part, dict) and part.get("text"):
                texts.append(part["text"])
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-600:]
        return sid, None, f"opencode failed rc={proc.returncode}\n{tail}"
    if not texts:
        return sid, None, "agent returned no text"
    return sid, "\n".join(texts).strip(), None


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

    sessions = state.setdefault("sessions", {})
    session_id = sessions.get(str(chat_id))

    if text in ("/new", f"/new@{bot_username}"):
        sessions.pop(str(chat_id), None)
        save_json(STATE_PATH, state)
        send(cfg["bot_token"], chat_id, "session cleared. next message starts fresh.")
        return
    if text in ("/status", f"/status@{bot_username}"):
        info = session_id or "(none)"
        send(
            cfg["bot_token"],
            chat_id,
            f"chat {chat_id}\nsession: {info}\ncwd: {cfg['workdir']}",
        )
        return
    if not text.strip():
        return

    react(cfg["bot_token"], chat_id, message_id, "👀")
    stop_typing = threading.Event()
    threading.Thread(
        target=typing_loop,
        args=(cfg["bot_token"], chat_id, stop_typing),
        daemon=True,
    ).start()
    log(f"chat={chat_id} run start (session={session_id})")
    try:
        new_sid, answer, err = run_agent(cfg, session_id, text)
    finally:
        stop_typing.set()
    if new_sid and new_sid != session_id:
        sessions[str(chat_id)] = new_sid
    state["offset"] = upd["update_id"] + 1
    save_json(STATE_PATH, state)
    if err:
        react(cfg["bot_token"], chat_id, message_id, "🔴")
        send(cfg["bot_token"], chat_id, f"⚠️ {err}")
        log(f"chat={chat_id} error: {err[:120]}")
    else:
        react(cfg["bot_token"], chat_id, message_id, "✅")
        send(cfg["bot_token"], chat_id, answer or "", reply_to=message_id)
        log(f"chat={chat_id} done ({len(answer or '')} chars, session={new_sid})")


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
            state["offset"] = offset
            save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
