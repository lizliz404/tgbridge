"""Authorized Telegram input, burst coalescing and bridge-control dispatch.

The explicit app argument supplies the single runtime owner and services.
No copied globals, entry-point import or hidden module-level runtime.
"""
import re
import signal
import threading
import time

from .copy import notice

_DEFAULT_STATE_PATH = object()

def unpack_entry(app, entry):
    """Queue items arrive as (chat_id, message_id, prompt) tuples (prompt path)
    or dicts (scheduled /at jobs). Accept both — never crash the worker."""
    if isinstance(entry, dict):
        if entry.get("kind") == "prompt_batch":
            with app.INGRESS_LOCK:
                if app.PENDING_PROMPTS.get(entry["chat_id"]) is entry:
                    app.PENDING_PROMPTS.pop(entry["chat_id"], None)
                parts = list(entry.get("parts") or [])
            return entry["chat_id"], entry["message_id"], "\n\n".join(parts)
        return entry["chat_id"], entry["message_id"], entry["prompt"]
    return entry


def _flush_prompt_batch(app, cfg, chat_id, batch):
    """Commit one settled Telegram burst to steering or the serial queue."""
    with app.INGRESS_LOCK:
        if app.PENDING_PROMPTS.get(chat_id) is not batch or batch.get("queued"):
            return
        text = "\n\n".join(batch.get("parts") or []).strip()
        batch["timer"] = None
        if not text:
            app.PENDING_PROMPTS.pop(chat_id, None)
            return
        steer_target = app._begin_steer(chat_id)
        if steer_target:
            app.PENDING_PROMPTS.pop(chat_id, None)
        else:
            with app.RUN_LOCK:
                cur = app.RUN_STATE.get("current") or {}
                starting = bool(
                    app.RUN_STATE.get("busy") and cur.get("chat") == chat_id
                    and cur.get("mode") == "server"
                    and cur.get("runner") in ("pi", "codex")
                    and not app.RUN_STATE.get("cancel")
                )
            if starting:
                # Reuse the existing burst timer while the RPC/session starts.
                # Do not commit this input to a queue stuck behind the live run.
                timer = threading.Timer(0.1, app._flush_prompt_batch, args=(cfg, chat_id, batch))
                timer.daemon = True
                batch["timer"] = timer
                timer.start()
                return
            batch["queued"] = True
    if steer_target:
        sid = steer_target["sid"]
        app.audit(
            "steer_received",
            chat_id=chat_id,
            session=sid,
            transport=steer_target["transport"],
            chars=len(text),
            messages=len(batch.get("parts") or []),
        )
        if steer_target["transport"] == "opencode_v2_steer":
            target = app._steer_deliver_v2
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
            target = app._codex_steer_deliver
            args = (
                cfg,
                sid,
                steer_target["turn_id"],
                steer_target["run_id"],
                text,
                chat_id,
                batch["message_id"],
            )
        elif steer_target["transport"] == "pi_rpc_steer":
            target = app._pi_steer_deliver
            args = (
                cfg,
                sid,
                steer_target["run_id"],
                text,
                chat_id,
                batch["message_id"],
            )
        else:
            raise RuntimeError(f"unknown steer transport {steer_target['transport']}")
        input_ids = batch.get("input_ids") or []
        if input_ids and cfg.get("_inbox"):
            cfg["_inbox"].transition(input_ids, "steering")
            args += (input_ids,)
        # Keep recovery keys associated with the active task until it settles.
        with app.RUN_LOCK:
            app.RUN_STATE.setdefault("active_input_ids", []).extend(input_ids)
        # Hermes injects first and acknowledges afterward. Telegram latency,
        # rate limiting or a dead reply target must never gate the RPC write.
        def inject_then_ack():
            target(*args)
            app.send(
                cfg["bot_token"],
                chat_id,
                notice('steering'),
            )

        threading.Thread(target=inject_then_ack, daemon=True).start()
        return

    if cfg.get("_inbox"):
        cfg["_inbox"].transition(batch.get("input_ids") or [], "queued")
    app.PROMPT_Q.put(batch)
    with app.RUN_LOCK:
        busy = bool(app.RUN_STATE.get("busy"))
    if busy:
        app.send(
            cfg["bot_token"],
            chat_id,
            f"{notice('queued')} · {app.PROMPT_Q.qsize()}",
        )


def defer_prompt(app, cfg, chat_id, message_id, text, input_ids=()):
    """Merge adjacent messages, including Telegram's automatic text splits."""
    live_steer = app.should_steer(chat_id)
    with app.INGRESS_LOCK:
        batch = app.PENDING_PROMPTS.get(chat_id)
        if batch:
            batch.setdefault("parts", []).append(text)
            batch.setdefault("input_ids", []).extend(input_ids)
            app.audit(
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
                "input_ids": list(input_ids),
                "queued": False,
                "timer": None,
            }
            app.PENDING_PROMPTS[chat_id] = batch
        if not live_steer:
            timer = threading.Timer(
                app.input_debounce(cfg), app._flush_prompt_batch, args=(cfg, chat_id, batch)
            )
            timer.daemon = True
            batch["timer"] = timer
            timer.start()
    if live_steer:
        # Ordinary mid-run text should reach the native queue immediately;
        # only idle/startup bursts need Telegram split-message coalescing.
        app._flush_prompt_batch(cfg, chat_id, batch)


def merge_open_burst(app, cfg, chat_id, text):
    """Attach an unaddressed split tail/media item to an addressed open burst."""
    with app.INGRESS_LOCK:
        batch = app.PENDING_PROMPTS.get(chat_id)
        if not batch or batch.get("queued"):
            return False
        batch.setdefault("parts", []).append(text)
        old_timer = batch.get("timer")
        if old_timer:
            old_timer.cancel()
        timer = threading.Timer(
            app.input_debounce(cfg), app._flush_prompt_batch, args=(cfg, chat_id, batch)
        )
        timer.daemon = True
        batch["timer"] = timer
        timer.start()
        app.audit(
            "input_merged",
            chat_id=chat_id,
            messages=len(batch["parts"]),
            chars=len(text),
            unaddressed_tail=True,
        )
        return True


def is_authorized(app, cfg, chat_id, chat_type, user_id):
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


def apply_sender_instructions(app, cfg, user_id, prompt):
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


def handle_update(app, cfg, state, upd, *, state_path=_DEFAULT_STATE_PATH):
    """Handle one Telegram update.

    ``state_path=None`` keeps pure/self-test calls from persisting fixture state
    into the live bridge store. Runtime callers use the real path by default.
    """
    if state_path is _DEFAULT_STATE_PATH:
        state_path = app.STATE_PATH
    query = upd.get('callback_query')
    if query:
        message = query.get('message') or {}
        chat = message.get('chat') or {}
        user = (query.get('from') or {}).get('id')
        if app.is_authorized(cfg, chat.get('id'), chat.get('type'), user) and cfg.get('_questions'):
            cfg['_questions'].callback(query)
        return
    msg = upd.get("message")
    if not msg:
        return
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    user_id = (msg.get("from") or {}).get("id")
    text = msg.get("text") or msg.get("caption") or ""
    message_id = msg.get("message_id")
    if not app.is_authorized(cfg, chat_id, chat_type, user_id):
        return
    dispatcher = cfg.get('_chat_dispatcher')
    if dispatcher is not None:
        app = dispatcher.runtime(chat_id)
    bot_username = state.get("bot_username", "")
    raw_cmd, _ = app.botcmd(text)
    if raw_cmd and "@" in text.split(maxsplit=1)[0]:
        target = text.split(maxsplit=1)[0].split("@", 1)[1]
        if target.casefold() != bot_username.casefold():
            return
    has_attachment = bool(msg.get("document") or msg.get("photo"))
    attachment_kind = attachment_path = None
    if has_attachment:
        attachment_kind, attachment_path = app.save_attachment(cfg, msg)
    attachment_note = (
        f"[{attachment_kind} saved: {attachment_path}]" if attachment_path else ""
    )

    if chat_type != "private":
        reply = msg.get("reply_to_message") or {}
        replied_to_bot = (reply.get("from") or {}).get("username") == bot_username
        if f"@{bot_username}" not in text and not replied_to_bot:
            context_text = "\n".join(p for p in (text.strip(), attachment_note) if p)
            if not raw_cmd and context_text and app.merge_open_burst(cfg, chat_id, context_text):
                return
            if (
                cfg.get("capture_group_context", True)
                and context_text
                and not text.lstrip().startswith("/")
            ):
                with app.STATE_LOCK:
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
                        app.save_json(state_path, state)
                now = time.time()
                hints = state.get("hints", {})
                if now - hints.get(str(chat_id), 0) > 1800:
                    with app.STATE_LOCK:
                        state.setdefault("hints", {})[str(chat_id)] = now
                        if state_path is not None:
                            app.save_json(state_path, state)
                    app.send(
                        cfg["bot_token"],
                        chat_id,
                        f"💡 @{bot_username} at me (or reply to my messages) and I'll answer",
                    )
            return
        text = text.replace(f"@{bot_username}", "").strip()

    if attachment_note:
        text = "\n".join(p for p in (text, attachment_note) if p).strip()
    elif has_attachment and not text.strip():
        app.send(
            cfg["bot_token"], chat_id, "⚠️ I could not download that file from Telegram"
        )
        return

    cmd, rest = app.botcmd(text)
    if not cmd and not has_attachment and cfg.get('_questions'):
        reply_id = (msg.get('reply_to_message') or {}).get('message_id')
        if cfg['_questions'].answer_text(chat_id, user_id, text, reply_id):
            return
    if cmd == "/help":
        app.send(
            cfg["bot_token"], chat_id,
            app.command_help(bot_username, group=chat_type != "private"),
            reply_to=message_id,
        )
        return
    if cmd and cmd not in app.COMMAND_NAMES:
        app.send(cfg["bot_token"], chat_id, app.unsupported_command(cmd), reply_to=message_id)
        return
    if cmd in ("/pending", "/resume", "/result"):
        inbox = cfg.get("_inbox")
        if not inbox:
            app.send(cfg["bot_token"], chat_id, "durable input recovery unavailable")
            return
        if cmd == "/pending":
            rows = inbox.pending(chat_id)
            body = "\n".join(f"{e['id']} · {e['status']} · {e['prompt'][-80:]}" for e in rows[-20:])
            app.send(cfg["bot_token"], chat_id, body or "No pending tasks.")
        elif cmd == "/result":
            result = inbox.result(rest.strip(), chat_id)
            app.send(cfg["bot_token"], chat_id, result or "No saved result.")
        else:
            entry = inbox.continuation(rest.strip(), chat_id)
            if entry:
                app.PROMPT_Q.put(entry)
                app.send(cfg["bot_token"], chat_id, "Queued for review and continuation.")
            else:
                app.send(cfg["bot_token"], chat_id, "Task not found. See /pending.")
        return
    if cmd == "/new":
        with app.STATE_LOCK:
            if hasattr(app, 'SESSION_EPOCH'):
                app.SESSION_EPOCH += 1
            app.clear_runner_sessions(state, chat_id)
            if state_path is not None:
                app.save_json(state_path, state)
        app.send(cfg["bot_token"], chat_id, "New session on your next message.", reply_to=message_id)
        return
    if cmd == "/cancel":
        with app.RUN_LOCK:
            proc = app.RUN_STATE.get("proc")
            cur = app.RUN_STATE.get("current")
            server_sid = app.RUN_STATE.get("server_sid")
            server_directory = app.RUN_STATE.get("server_directory")
            active_server_url = app.RUN_STATE.get("server_url")
            active_server_api = app.RUN_STATE.get("server_api")
        if server_sid:
            if not cur:
                app.send(cfg["bot_token"], chat_id, "nothing running")
                return
            if chat_type != "private" and cur["chat"] != chat_id:
                app.send(
                    cfg["bot_token"],
                    chat_id,
                    f"run belongs to chat {cur['chat']} — cancel from there",
                )
                return
            with app.RUN_LOCK:
                app.RUN_STATE["cancel"] = True
            try:
                app._server_call(
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
                app.log(f"server abort request: {e}")
            app.audit("cancel_requested", by_chat=chat_id, run_chat=cur["chat"])
            app.send(cfg["bot_token"], chat_id, "🛑 stopping current run…")
            return
        if not (proc and cur and proc.poll() is None):
            app.send(cfg["bot_token"], chat_id, "nothing running")
            return
        if chat_type != "private" and cur["chat"] != chat_id:
            app.send(
                cfg["bot_token"],
                chat_id,
                f"run belongs to chat {cur['chat']} — cancel from there",
            )
            return
        with app.RUN_LOCK:
            app.RUN_STATE["cancel"] = True
            is_pi_rpc = bool(app.RUN_STATE.get("pi_sid"))
        if not is_pi_rpc:
            app.signal_run_process(proc, signal.SIGTERM)
        app.kill_after(proc, 5)
        app.audit("cancel_requested", by_chat=chat_id, run_chat=cur["chat"])
        app.send(cfg["bot_token"], chat_id, "🛑 stopping current run…")
        return
    if cmd == "/status":
        status_cfg = app.effective_run_config(cfg, state)
        runner_name = status_cfg.get("runner", "opencode")
        with app.STATE_LOCK:
            info = (
                app.runner_session(
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
        with app.INGRESS_LOCK:
            incoming = app.PENDING_PROMPTS.get(chat_id)
            incoming_count = len((incoming or {}).get("parts") or [])
        with app.RUN_LOCK:
            c = app.RUN_STATE.get("current")
        health = app.load_json(app.HEALTH_PATH, {})
        poll_health = health.get("status", "unknown")
        failures = health.get("consecutive_poll_failures", 0)
        cur = ""
        if c:
            cur = f"\nrunning: {c['prompt']}… ({int(time.time() - c['since'])}s)"
        mode = app.resolve_runner_mode(status_cfg, runner_name)
        capability = (app.SERVER_RUNNERS.get(runner_name) or {}).get("feature")
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
        model_label = app.resolve_model(status_cfg, runner_name) or "runner default"
        chain = app.fallback_chain(status_cfg)
        chain_label = (
            "none"
            if not chain
            else " -> ".join(f"{r}/{m or 'runner default'}" for r, m in chain)
        )
        override_note = ""
        selected = (state.get('chat_runner_overrides') or {}).get(
            str(chat_id), state.get('runner_override') or {})
        if selected.get('runner'):
            override_note = f" (override, file says {cfg.get('runner', 'opencode')})"
        app.send(
            cfg["bot_token"],
            chat_id,
            f"chat {chat_id}\nrunner: {runner_name} ({mode_label}){override_note}\n"
            f"model: {model_label}\nfallbacks: {chain_label}\n"
            f"session: {info}\ncwd: {cfg['workdir']}\n"
            f"incoming parts: {incoming_count}\nqueued batches: {app.PROMPT_Q.qsize()}\n"
            f"scheduled: {pending}\ntelegram poll: {poll_health} "
            f"(failures: {failures}){cur}",
        )
        return
    if cmd == "/runners":
        probe = app.probe_runners()
        eff = app.effective_run_config(cfg, state)
        lines = [
            f"{'●' if p['available'] else '○'} {name}"
            + ("" if p["available"] else f" — {p['detail']}")
            for name, p in probe.items()
        ]
        chain = app.fallback_chain(eff)
        lines.append(
            "fallbacks: "
            + (
                "none"
                if not chain
                else " -> ".join(f"{r}/{m or 'runner default'}" for r, m in chain)
            )
        )
        ov = (state.get('chat_runner_overrides') or {}).get(
            str(chat_id), state.get('runner_override') or {}).get('runner')
        lines.append(
            f"primary: {eff.get('runner', 'opencode')}"
            + (
                f" (via /runner, file says {cfg.get('runner', 'opencode')})"
                if ov
                else ""
            )
        )
        app.send(cfg["bot_token"], chat_id, "runners:\n" + "\n".join(lines))
        return
    if cmd == "/runner":
        args = (rest or "").split()
        if not args:
            eff = app.effective_run_config(cfg, state)
            app.send(
                cfg["bot_token"],
                chat_id,
                f"primary: {eff.get('runner', 'opencode')} "
                f"({app.resolve_model(eff, eff.get('runner', 'opencode')) or 'runner default'})\n"
                "usage: /runner <name> [model] · /runner default",
            )
            return
        if args[0] == "default":
            with app.STATE_LOCK:
                if hasattr(app, 'chat_id'):
                    state.setdefault('chat_runner_overrides', {})[str(chat_id)] = {}
                else:
                    state.pop('runner_override', None)
                if state_path is not None:
                    app.save_json(state_path, state)
            app.audit("runner_override_cleared", chat_id=chat_id)
            app.send(
                cfg["bot_token"],
                chat_id,
                f"primary back to file config: {cfg.get('runner', 'opencode')}",
            )
            return
        name, model = args[0], (args[1] if len(args) > 1 else "")
        if name not in app.RUNNERS:
            app.send(
                cfg["bot_token"],
                chat_id,
                f"unknown runner {name!r} (available: {', '.join(sorted(app.RUNNERS))})",
            )
            return
        with app.STATE_LOCK:
            selection = {'runner': name, 'model': model}
            if hasattr(app, 'chat_id'):
                state.setdefault('chat_runner_overrides', {})[str(chat_id)] = selection
            else:
                state['runner_override'] = selection
            if state_path is not None:
                app.save_json(state_path, state)
        app.audit("runner_override_set", chat_id=chat_id, runner=name, model=model)
        app.send(
            cfg["bot_token"],
            chat_id,
            f"primary now {name} ({model or 'runner default'}) — applies to the next run",
        )
        return
    if cmd == "/at":
        m = re.match(r"^(\d+)([smh])\s+(.+)$", rest, re.I)
        if not m:
            app.send(cfg["bot_token"], chat_id, "usage: /at 30m <prompt>  (s/m/h, max 7d)")
            return
        delay = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2).lower()]
        if delay > 7 * 86400:
            app.send(cfg["bot_token"], chat_id, "/at max is 7d")
            return
        due = int(time.time()) + delay
        prompt = app.apply_sender_instructions(cfg, user_id, m.group(3).strip())
        with app.STATE_LOCK:
            state.setdefault("at", {})[str(due)] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "prompt": prompt,
            }
            if state_path is not None:
                app.save_json(state_path, state)
        t = threading.Timer(delay, app.fire_at, args=(cfg, state, due))
        t.daemon = True
        t.start()
        app.audit("at_set", chat_id=chat_id, delay=delay, prompt=prompt[:80])
        app.send(
            cfg["bot_token"],
            chat_id,
            f"⏰ scheduled in {delay}s — fires via this chat's session",
        )
        return

    if msg.get("voice") and not text.strip():
        if not cfg.get("transcribe_key"):
            app.send(
                cfg["bot_token"],
                chat_id,
                "🎤 voice note received but transcription not configured "
                "(set transcribe_key / transcribe_base_url / transcribe_model)",
            )
            return
        text, err = app.transcribe(cfg, cfg["bot_token"], msg["voice"].get("file_id"))
        if err:
            app.react(cfg, chat_id, message_id, "👎")
            app.send(cfg["bot_token"], chat_id, f"⚠️ {err}")
            return
        app.audit("voice", chat_id=chat_id, chars=len(text or ""))
        text = f"(voice note) {text}"

    if not text.strip():
        return

    app.audit("enqueue", chat_id=chat_id, user_id=user_id, chars=len(text.strip()))
    source_attachment = None
    reply = msg.get("reply_to_message") or {}
    if reply.get("document") or reply.get("photo"):
        # Only addressed, authorized agent input downloads source attachments;
        # passive group traffic and slash commands cannot trigger this work.
        _, source_attachment = app.save_attachment(cfg, reply)
    prompt_text = app.reply_context(msg, source_attachment) + text.strip()
    if chat_type != "private" and cfg.get("capture_group_context", True):
        with app.STATE_LOCK:
            buf = (state.get("context") or {}).pop(str(chat_id), None) or []
        if buf:
            digest = "\n".join(f"- {e['who']} {e['t']}: {e['text']}" for e in buf[-20:])
            prompt_text = (
                "[group messages since your last turn — passive context, "
                "nobody asked you anything yet:\n" + digest + "\n]\n\n" + prompt_text
            )
    prompt_text = app.apply_sender_instructions(cfg, user_id, prompt_text)
    inbox = cfg.get("_inbox")
    if inbox:
        identity = f"tg:{chat_id}:{message_id}"
        # Persist before defer/timer/RPC and before run() commits Telegram offset.
        if not inbox.receive(identity, chat_id, message_id, prompt_text, user_id, chat_type):
            return
        app.defer_prompt(cfg, chat_id, message_id, prompt_text, input_ids=[identity])
    else:
        app.defer_prompt(cfg, chat_id, message_id, prompt_text)
