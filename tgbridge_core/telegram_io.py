"""Telegram Bot API I/O, media and question-card delivery.

The explicit app argument supplies the single runtime owner and services.
No copied globals, entry-point import or hidden module-level runtime.
"""
import html
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

def api(app, token, method, _error=None, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}", data=data
    )
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=70) as r:
                return json.load(r)
        except app.BridgeStop:
            raise
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if _error is not None:
                _error.update(kind=app.classify_network_error(e), message=f"HTTP {e.code}",
                              code=e.code, description=body[:300])
            if e.code == 409:
                app.log(
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
            app.log(f"api {method} http {e.code}")
            return None
        except Exception as e:
            if _error is not None:
                _error.update(kind=app.classify_network_error(e), message=type(e).__name__)
            # urllib exceptions may contain the request URL (and bot token).
            app.log(f"api {method} error: {type(e).__name__}")
            return None
    return None


def react(app, cfg, chat_id, message_id, emoji):
    if not cfg.get("reactions", True):
        return
    app.api(
        cfg["bot_token"],
        "setMessageReaction",
        chat_id=chat_id,
        message_id=message_id,
        reaction=json.dumps([{"type": "emoji", "emoji": emoji}]),
    )


def send(app, token, chat_id, text, reply_to=None, chunk_limit=None):
    """Send markdown text rendered as Telegram HTML; any chunk Telegram
    refuses (bad entity, overlong tag) falls back to plain text so a
    formatting bug can never drop the payload. Chunks whose HTML is
    unbalanced locally skip the doomed HTML attempt entirely."""
    if chunk_limit is None:
        chunk_limit = app.CHUNK
    ok = True
    in_pre = False
    pre_lang = ""
    text = app._wrap_markdown_tables(text or "")
    for i, chunk in enumerate(app.split_chunks(text, chunk_limit)):
        html, in_pre, pre_lang = app.md_to_html(chunk, in_pre, pre_lang)
        params = {
            "chat_id": chat_id,
            "text": html,
            "link_preview_options": json.dumps({"is_disabled": True}),
        }
        # Remove inherited persistent keyboards, never the inline question
        # cards. In groups target only the sender of the replied-to message;
        # unthreaded announcements must not alter other members' keyboards.
        if i == 0 and (chat_id > 0 or reply_to):
            params["reply_markup"] = json.dumps(
                {"remove_keyboard": True, "selective": chat_id < 0}
            )
        if app._balanced(html):
            params["parse_mode"] = "HTML"
        if i == 0 and reply_to:
            params["reply_parameters"] = json.dumps({"message_id": reply_to})
        # Adapt Hermes Telegram adapter.send: retries belong to the current
        # chunk, never the whole message; plain fallback is for parse errors
        # only. Ambiguous read timeouts may have sent, so do not retry them.
        res = None
        for attempt in (1, 2):
            error = {}
            if "parse_mode" not in params:
                params["text"] = app._strip_html_markup(chunk) or chunk
            res = app.api(token, "sendMessage", _error=error, **params)
            if res and res.get("ok"):
                break
            code = error.get("code") or (res or {}).get("error_code")
            description = (error.get("description") or
                           (res or {}).get("description") or "").lower()
            if code == 400 and any(s in description for s in ("parse", "entity", "too long")):
                if "parse_mode" in params:
                    params.pop("parse_mode", None)
                    params["text"] = app._strip_html_markup(chunk) or chunk
                    res = app.api(token, "sendMessage", **params)
                    break
            if code == 400 and "message to be replied not found" in description:
                if "reply_parameters" in params:
                    params.pop("reply_parameters", None)
                    continue
            if attempt == 1 and (code == 429 or code in (500, 502, 503, 504)
                    or error.get("kind") in ("connection_refused", "proxy_refused", "dns")):
                time.sleep(2)
                continue
            break
        if not res or not res.get("ok"):
            ok = False
    return ok


def send_retry(app, cfg, chat_id, text, reply_to=None):
    """Send with per-chunk retries and preserve any unconfirmed payload.

    Do not retry the whole answer: already delivered chunks would duplicate.
    Generic read timeouts are ambiguous, matching Hermes's retry policy.
    """
    limit = cfg.get("chunk") or app.CHUNK
    if app.send(cfg["bot_token"], chat_id, text, reply_to=reply_to, chunk_limit=limit):
        return True
    app.preserve_unconfirmed(chat_id, text)
    return False


def preserve_unconfirmed(app, chat_id, text):
    """Private recovery copy shared by assistant text and action messages."""
    app.audit("delivery_failed", chat_id=chat_id, chars=len(text or ""))
    app.log(f"chat={chat_id} DELIVERY UNCONFIRMED ({len(text or '')} chars)")
    try:
        d = os.path.join(app.CONFIG_DIR, "undelivered")
        app.ensure_private_dir(d)
        import tempfile

        fd, path = tempfile.mkstemp(prefix=f"{chat_id}-", suffix=".txt", dir=d)
        with os.fdopen(fd, "w") as f:
            f.write(text or "")
        os.chmod(path, 0o600)
        app.log("saved undelivered payload")
    except OSError:
        pass


def progress_journal(app, cfg, live):
    if live is None or live.get("chat_id") is None:
        return None
    if "journal" not in live:
        def send_text(text, first):
            return app.send_retry(cfg, live["chat_id"], text,
                              reply_to=live.get("reply_to") if first else None)

        def record_action(identity, body):
            app.audit("tool_action", chat_id=live["chat_id"], action_id=identity,
                  run_status_id=live.get("status_id"), body=body)

        secrets = [cfg.get("bot_token"), cfg.get("transcribe_key")]
        live["journal"] = app.Journal(live, send_text, record_action, secrets,
                                  update_status=lambda: app.edit_status(cfg, live))
    return live["journal"]


def publish_progress(app, cfg, live, event):
    journal = app.progress_journal(cfg, live)
    if journal:
        journal.emit(event)


def publish_snapshot(app, cfg, live, messages, baseline, v2=False):
    for event in app.opencode_snapshot(messages, baseline, v2):
        app.publish_progress(cfg, live, event)


def _post(app, url, data, timeout):
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def announce_all(app, cfg, text, post=None):
    """Best-effort death notice to every allowed chat. 3s timeout each,
    never raises — usable from crash paths and signal handlers."""
    post = app._post if post is None else post
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
            app.log(f"announce {chat_id}: {e}")


def typing_loop(app, token, chat_id, stop_event):
    while not stop_event.wait(4.0):
        app.api(token, "sendChatAction", chat_id=chat_id, action="typing")


def edit_status(app, cfg, live, final=None):
    now = time.monotonic()
    if not final and now - live.get("last_status_edit", -8) < 8:
        return
    live["last_status_edit"] = now
    elapsed = max(0, int(time.time() - live["start"]))
    if final:
        extra = ""
        if live.get("cost"):
            extra += f" · {live['tokens'] or 0} tok · ${live['cost']:.4f}"
        text = f"{final} · {elapsed}s{extra}"
    else:
        secrets = [cfg.get("bot_token"), cfg.get("transcribe_key")]
        text = f"⚙️ working… {elapsed}s"
        activity = live.get("activity")
        if not activity and live.get("trail"):
            label, _, detail = live["trail"][-1].removeprefix("🔧 ").partition(": ")
            activity = {"label": label, "preview": detail}
        if activity:
            text += "\n" + app.activity_body(activity, secrets)
        notes = live.get("notes") or []
        if notes:
            text += "\n" + html.escape(app.short_preview(notes[-1], secrets))
        preview = (live.get("preview") or "").strip().replace("\n", " ")
        if preview and not (activity and activity.get("label") == "thinking"):
            text += "\n💬 " + html.escape(app.short_preview(preview, secrets))
    if live.get("status_id"):
        app.api(
            cfg["bot_token"],
            "editMessageText",
            chat_id=live["chat_id"],
            message_id=live["status_id"],
            text=text,
            parse_mode="HTML",
        )


def question_broker(app, cfg):
    def send_question(chat, body, keyboard):
        result = app.api(cfg['bot_token'], 'sendMessage', chat_id=chat, text=body,
                     reply_markup=json.dumps({'inline_keyboard': keyboard}))
        return (result.get('result') or {}).get('message_id') if result and result.get('ok') else None
    def edit_question(chat, message, body, keyboard):
        if message is not None:
            app.api(cfg['bot_token'], 'editMessageText', chat_id=chat, message_id=message, text=body,
                reply_markup=json.dumps({'inline_keyboard': keyboard}))
    def acknowledge(identity, text):
        app.api(cfg['bot_token'], 'answerCallbackQuery', callback_query_id=identity, text=text)
    return app.Questions(send_question, edit_question, acknowledge, record=app.audit)


def multipart(app, fields, file_field, filename, data, ctype):
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


def transcribe(app, cfg, token, file_id):
    """Voice note -> text via any OpenAI-compatible /audio/transcriptions API.

    Optional: only runs when transcribe_key is set. Stdlib multipart, no deps.
    """
    g = app.api(token, "getFile", file_id=file_id)
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
    boundary, body = app.multipart(
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


def save_attachment(app, cfg, msg):
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
    r = app.api(cfg["bot_token"], "getFile", file_id=fid)
    fp = (r or {}).get("result", {}).get("file_path")
    if not fp:
        return None, None
    inbox = os.path.join(app.CONFIG_DIR, "inbox")
    app.ensure_private_dir(inbox)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    chat_id = (msg.get("chat") or {}).get("id", "chat")
    dest = os.path.join(inbox, f"{stamp}-{chat_id}-{msg.get('message_id', 'x')}-{name}")
    url = f"https://api.telegram.org/file/bot{cfg['bot_token']}/{fp}"
    try:
        urllib.request.urlretrieve(url, dest)
        os.chmod(dest, 0o600)
    except Exception as e:
        app.log(f"download {name}: {type(e).__name__}")
        try:
            os.unlink(dest)
        except OSError:
            pass
        return None, None
    app.log(f"{kind} saved: {dest}")
    app.audit("inbound_file", kind=kind, path=dest, bytes=os.path.getsize(dest))
    return kind, dest


def send_document(app, token, chat_id, path):
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
        app.log(f"send_document {fn}: {e}")
        return None
