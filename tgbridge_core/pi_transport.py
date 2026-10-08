"""Pi RPC transport, native UI responses and streaming reconciliation.

The explicit app argument supplies the single runtime owner and services.
No copied globals, entry-point import or hidden module-level runtime.
"""
import json
import queue
import signal
import subprocess
import threading
import time

def pi_rpc_ok(app, _cfg=None):
    """Installed Pi must expose the RPC transport used for streaming/steering."""
    try:
        result = subprocess.run(
            [app.pi_binary(), "--help"],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return False
    return result.returncode == 0 and "rpc" in (result.stdout or "")


def _pi_rpc_write(app, proc, payload):
    """Write one JSONL command; stdin is shared with the steering thread."""
    if proc is None or proc.stdin is None or proc.poll() is not None:
        raise RuntimeError("Pi RPC process is not running")
    line = json.dumps(payload, ensure_ascii=False)
    with app.PI_RPC_WRITE_LOCK:
        proc.stdin.write(line + "\n")
        proc.stdin.flush()


def _pi_rpc_read_events(app, stream, events):
    try:
        for raw in stream:
            raw = raw.strip()
            if not raw:
                continue
            try:
                events.put(json.loads(raw))
            except json.JSONDecodeError:
                continue
    finally:
        events.put(None)


def _pi_rpc_wait_response(app, events, request_id, timeout=30, keep=None, proc=None, ui_handler=None, ui_waiting=None):
    """Wait for one command response; session events are kept aside, not lost."""
    deadline = time.monotonic() + timeout
    while True:
        if ui_waiting and ui_waiting():
            deadline = time.monotonic() + timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"Pi RPC {request_id} timed out")
        try:
            event = events.get(timeout=min(0.5, remaining))
        except queue.Empty:
            continue
        if event is None:
            raise RuntimeError("Pi RPC exited during setup")
        if event.get("type") == "response" and event.get("id") == request_id:
            if not event.get("success", False):
                raise RuntimeError(str(event.get("error") or "command failed")[:600])
            return event.get("data") or {}
        if event.get("type") == "extension_ui_request" and proc is not None:
            # Startup/input extensions may ask before the command acknowledgement.
            (ui_handler or (lambda e: app._pi_rpc_decline_ui(proc, e)))(event)
        elif keep is not None:
            keep.append(event)


def _pi_message_text(app, message):
    """Join the text blocks of one assistant message."""
    blocks = message.get("content")
    if isinstance(blocks, str):
        return blocks.strip()
    texts = [
        block.get("text", "")
        for block in (blocks or [])
        if isinstance(block, dict)
        and block.get("type") == "text"
        and block.get("text")
    ]
    return "\n\n".join(texts).strip()


def _pi_rpc_decline_ui(app, proc, event):
    """Decline extension dialogs: the bridge has no interactive UI for them."""
    method = event.get("method")
    if method not in ("select", "confirm", "input", "editor"):
        return
    try:
        app._pi_rpc_write(
            proc,
            {
                "type": "extension_ui_response",
                "id": event.get("id"),
                "cancelled": True,
            },
        )
    except Exception as e:
        app.log(f"pi rpc extension ui decline: {e}")
    app.audit("pi_extension_ui_declined", method=method, title=str(event.get("title"))[:80])


def _pi_rpc_ui(app, cfg, proc, event, live, run_id):
    broker = cfg.get('_questions')
    method = event.get('method')
    if method not in ('select', 'confirm', 'input', 'editor'):
        if live is not None and method in ('notify', 'setStatus', 'setWidget'):
            summary = event.get('message') or event.get('statusText') or '\n'.join(event.get('widgetLines') or [])
            live['activity'] = {'label': 'status', 'preview': summary}
            app.edit_status(cfg, live)
        return
    if not broker or not live or live.get('chat_id') is None:
        app._pi_rpc_decline_ui(proc, event)
        return
    def respond(value):
        with app.RUN_LOCK:
            if app.RUN_STATE.get('proc') is not proc or app.RUN_STATE.get('pi_run_id') != run_id or app.RUN_STATE.get('cancel'):
                raise RuntimeError('question belongs to an ended Pi task')
        payload = {'type': 'extension_ui_response', 'id': event['id']}
        if value is None:
            payload['cancelled'] = True
        elif method == 'confirm':
            payload['confirmed'] = value == 'Yes'
        else:
            payload['value'] = value
        app._pi_rpc_write(proc, payload)
        app.mark_run_progress()
    try:
        broker.offer(live['chat_id'], live.get('requester_user_id'), event.get('title') or 'Your input?',
                     ['Yes', 'No'] if method == 'confirm' else event.get('options', []), respond,
                     owner=app.question_owner('pi', run_id), method=method, message=event.get('message') or '',
                     timeout=event.get('timeout') or app.run_max(cfg) * 1000)
        live['activity'] = {'label': 'waiting', 'preview': 'Your call.'}
        app.edit_status(cfg, live)
    except Exception as e:
        app.audit('pi_question_failed', method=method, error=type(e).__name__)
        app._pi_rpc_decline_ui(proc, event)


def _pi_rpc_abort(app, proc):
    """Drop queued input, then stop the active turn."""
    try:
        app._pi_rpc_write(proc, {"type": "clear_queue"})
        app._pi_rpc_write(proc, {"type": "abort"})
    except Exception as e:
        app.log(f"pi rpc abort: {e}")


def _pi_send_trailer(app, cfg, live, text):
    """A one-line footer for a streamed run (partial / cancelled / error)."""
    chat_id = live.get("chat_id")
    if chat_id is not None:
        app.send_retry(cfg, chat_id, text)


def _pi_steer_deliver(app, cfg, sid, run_id, text, chat_id=None, message_id=None, input_ids=()):
    """Send true same-turn steering to the live Pi RPC child."""
    meta = {
        "sid": sid,
        "text": text,
        "chat_id": chat_id,
        "message_id": message_id,
        "input_ids": input_ids,
    }
    request_id = None
    try:
        with app.RUN_LOCK:
            if not (
                app.RUN_STATE.get("pi_sid") == sid
                and app.RUN_STATE.get("pi_run_id") == run_id
            ):
                raise RuntimeError("active Pi run changed before steering delivery")
            proc = app.RUN_STATE.get("proc")
            seq = app.RUN_STATE.get("pi_request_id", 0) + 1
            app.RUN_STATE["pi_request_id"] = seq
            request_id = f"steer-{run_id}-{seq}"
            app.RUN_STATE.setdefault("pi_steers", {})[request_id] = meta
        app._pi_rpc_write(proc, {"id": request_id, "type": "steer", "message": text})
    except Exception as e:
        with app.RUN_LOCK:
            if request_id:
                app.RUN_STATE.setdefault("pi_steers", {}).pop(request_id, None)
            if app.RUN_STATE.get("pi_run_id") == run_id:
                app.RUN_STATE["steer_pending"] = max(
                    0, app.RUN_STATE.get("steer_pending", 1) - 1
                )
        app.audit(
            "steer_error",
            session=sid,
            transport="pi_rpc_steer",
            err=str(e)[:200],
        )
        app.log(f"pi rpc steer write: {e}")
        app._steer_fallback(cfg, meta, e)


def _pi_handle_steer_response(app, cfg, event, run_id):
    request_id = event.get("id")
    if not isinstance(request_id, str) or not request_id.startswith("steer-"):
        return False
    with app.RUN_LOCK:
        steers = app.RUN_STATE.setdefault("pi_steers", {})
        meta = steers.get(request_id)
        if meta and app.RUN_STATE.get("pi_run_id") == run_id and not meta.get("acknowledged"):
            meta["acknowledged"] = True
            app.RUN_STATE["steer_pending"] = max(0, app.RUN_STATE.get("steer_pending", 1) - 1)
            if event.get("success", False):
                app.RUN_STATE["steer_count"] = app.RUN_STATE.get("steer_count", 0) + 1
            if (not event.get("success", False)
                    or (event.get("data") or {}).get("disposition") == "handled"
                    or meta.get("consumed")):
                steers.pop(request_id, None)
        else:
            meta = None
    if not meta:
        return True
    if not event.get("success", False):
        detail = str(event.get("error") or "steer rejected")[:500]
        app.audit("steer_error", session=meta["sid"], transport="pi_rpc_steer", err=detail)
        app._steer_fallback(cfg, meta, detail)
    else:
        if cfg.get("_inbox") and (meta.get("consumed") or
                (event.get("data") or {}).get("disposition") == "handled"):
            cfg["_inbox"].transition(meta.get("input_ids") or [], "executing")
        app.audit(
            "steer_delivered",
            session=meta["sid"],
            transport="pi_rpc_steer",
            chars=len(meta["text"]),
        )
    return True


def run_pi_rpc(app, cfg, session_id, prompt, live=None):
    """Run Pi over `--mode rpc`.

    Turns one agent run into a chat-shaped stream: each completed assistant
    segment and each tool action use the shared durable progress journal,
    and a same-chat human message is steered into the live turn instead
    of waiting for a possibly 30-minute run to finish.
    """
    sid = session_id
    proc = None
    events: queue.Queue = queue.Queue()
    errbuf = []
    early_events = []
    segments = []
    tokens = 0
    cost = 0.0
    turn_error = None
    timeout_reason = None
    cancelled = False
    handled = False
    run_id = None
    queued_steers = 0
    if live is not None:
        live["missing_segments"] = []
        live["thinking"] = ""
    try:
        command = [app.pi_binary(), "--mode", "rpc"]
        if cfg.get("session_name"):
            command += ["--name", cfg["session_name"]]
        if sid:
            command += ["--session-id", sid]
        model = app.resolve_model(cfg, "pi")
        if model:
            command += ["--model", model]
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cfg["workdir"],
            start_new_session=True,
            bufsize=1,
            encoding="utf-8",
        )
        assert proc.stdout and proc.stderr
        threading.Thread(
            target=app._pi_rpc_read_events, args=(proc.stdout, events), daemon=True
        ).start()
        stderr = proc.stderr
        threading.Thread(
            target=lambda: errbuf.append(stderr.read() or ""), daemon=True
        ).start()
        with app.RUN_LOCK:
            app.RUN_STATE["proc"] = proc
            app.RUN_STATE["pi_run_id"] = app.RUN_STATE.get("pi_run_id", 0) + 1
            run_id = app.RUN_STATE["pi_run_id"]
            app.RUN_STATE["pi_sid"] = None
            app.RUN_STATE["pi_steers"] = {}
            app.RUN_STATE["pi_request_id"] = 0
            app.RUN_STATE["steer_count"] = 0
            app.RUN_STATE["steer_pending"] = 0
            app.RUN_STATE["steer_errors"] = []
            app.RUN_STATE["cancel"] = False

        app._pi_rpc_write(proc, {"id": app.PI_RPC_STATE_CMD, "type": "get_state"})
        try:
            state = app._pi_rpc_wait_response(
                events, app.PI_RPC_STATE_CMD, timeout=app.PI_RPC_SETUP_TIMEOUT,
                keep=early_events, proc=proc,
                ui_handler=lambda event: app._pi_rpc_ui(cfg, proc, event, live, run_id),
                ui_waiting=lambda: bool(cfg.get('_questions') and cfg['_questions'].has_owner(app.question_owner('pi', run_id))),
            )
        except RuntimeError as e:
            app.log(f"pi rpc get_state: {e}")
            state = {}
        sid = state.get("sessionId") or sid
        if sid:
            with app.RUN_LOCK:
                if app.RUN_STATE.get("pi_run_id") == run_id:
                    app.RUN_STATE["pi_sid"] = sid
                    if app.RUN_STATE.get("current"):
                        app.RUN_STATE["current"]["session"] = sid

        app._pi_rpc_write(
            proc, {"id": app.PI_RPC_PROMPT_CMD, "type": "prompt", "message": prompt}
        )
        accepted = app._pi_rpc_wait_response(
            events, app.PI_RPC_PROMPT_CMD, timeout=app.PI_RPC_PROMPT_TIMEOUT,
            keep=early_events, proc=proc,
            ui_handler=lambda event: app._pi_rpc_ui(cfg, proc, event, live, run_id),
            ui_waiting=lambda: bool(cfg.get('_questions') and cfg['_questions'].has_owner(app.question_owner('pi', run_id))),
        )
        handled = (accepted.get("disposition") or "") == "handled"

        clock_started = app.start_run_clock()
        settled = handled
        while not settled:
            with app.RUN_LOCK:
                cancelled = bool(app.RUN_STATE.get("cancel"))
            if cancelled:
                app._pi_rpc_abort(proc)
                break
            if cfg.get('_questions') and cfg['_questions'].has_owner(app.question_owner('pi', run_id)):
                app.mark_run_progress()  # human decision wait, still bounded by run_max_s
            timeout_reason = app.run_expiry(cfg, clock_started)
            if timeout_reason:
                app.signal_run_process(proc, signal.SIGKILL)
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
            app.mark_run_progress()
            if app._pi_handle_steer_response(cfg, event, run_id):
                continue
            etype = event.get("type")
            app.publish_progress(cfg, live, app.pi_event(event))
            if etype == "extension_ui_request":
                app._pi_rpc_ui(cfg, proc, event, live, run_id)
            elif etype == "message_update":
                update = event.get("assistantMessageEvent") or {}
                utype = update.get("type")
                # Private reasoning is not a user-facing progress channel.
                if utype == "error":
                    turn_error = str(
                        update.get("error") or update.get("reason") or "stream error"
                    )[-600:]
            elif etype == "tool_execution_start":
                if live is not None:
                    live["trail"].append(f"🔧 {event.get('toolName', '?')}")
                    app.edit_status(cfg, live)
            elif etype == "message_end":
                message = event.get("message") or {}
                if message.get("role") == "user":
                    # An RPC steer ack means queued, not consumed. Only its
                    # user message proves the running agent received it.
                    text = app._pi_message_text(message)
                    with app.RUN_LOCK:
                        for request_id, meta in list(app.RUN_STATE.get("pi_steers", {}).items()):
                            if not meta.get("consumed") and meta["text"].strip() == text:
                                meta["consumed"] = True
                                if meta.get("acknowledged"):
                                    app.RUN_STATE["pi_steers"].pop(request_id, None)
                                if cfg.get("_inbox"):
                                    cfg["_inbox"].transition(meta.get("input_ids") or [], "executing")
                                break
                    continue
                if message.get("role") != "assistant":
                    continue
                usage = message.get("usage") or {}
                tokens += usage.get("totalTokens") or 0
                cost += (usage.get("cost") or {}).get("total") or 0.0
                stop_reason = message.get("stopReason")
                if stop_reason in ("error", "aborted"):
                    turn_error = str(
                        message.get("errorMessage") or f"Pi stopped with {stop_reason}"
                    )[-600:]
                else:
                    turn_error = None
                if live is not None:
                    live["thinking"] = ""
                text = app._pi_message_text(message)
                if not text:
                    continue
                segments.append(text)
                if live is None:
                    continue
            elif etype == "queue_update":
                pending = len(event.get("steering") or [])
                if live is not None and pending != queued_steers:
                    queued_steers = pending
                    note = (
                        f"🧭 steering queued ({pending})"
                        if pending
                        else "🧭 steering delivered"
                    )
                    live.setdefault("notes", []).append(note)
                    app.edit_status(cfg, live)
            elif etype == "auto_retry_end":
                turn_error = (None if event.get("success", True) else
                              str(event.get("finalError") or "auto retry failed")[-600:])
            elif etype == "agent_settled":
                settled = True
                with app.RUN_LOCK:
                    app.RUN_STATE["pi_sid"] = None

        # A steer acknowledgement can land just before agent_settled; drain the
        # queue before concluding that an injection was never accepted.
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            if event is not None:
                app._pi_handle_steer_response(cfg, event, run_id)

        with app.RUN_LOCK:
            app.RUN_STATE["pi_sid"] = None
            outstanding = list(app.RUN_STATE.get("pi_steers", {}).values())
            app.RUN_STATE["pi_steers"] = {}
            app.RUN_STATE["steer_pending"] = 0
            cancelled = bool(app.RUN_STATE.pop("cancel", False)) or cancelled
        for meta in outstanding:
            if not cancelled and not meta.get("consumed"):
                app._steer_fallback(cfg, meta, "Pi RPC closed before steering was consumed")

        if live is not None:
            live["tokens"] = tokens
            live["cost"] = cost
        answer = "\n\n".join(segments).strip()
        delivered = bool(live is not None and live.get("streamed"))
        if live is not None and live.get("missing_segments"):
            if timeout_reason:
                live["missing_segments"].append(f"⚠️ (partial — hit the {timeout_reason} timeout)")
            elif cancelled:
                live["missing_segments"].append("🛑 (cancelled — partial answer)")
            elif turn_error:
                live["missing_segments"].append("⚠️ (agent turn ended with an error)")
        if timeout_reason:
            if delivered:
                app._pi_send_trailer(
                    cfg, live, f"⚠️ (partial — hit the {timeout_reason} timeout)"
                )
                return sid, answer, None
            if answer:
                return (
                    sid,
                    answer + f"\n\n⚠️ (partial — hit the {timeout_reason} timeout)",
                    None,
                )
            return sid, None, f"agent hit the {timeout_reason} timeout"
        if cancelled:
            if delivered:
                app._pi_send_trailer(cfg, live, "🛑 (cancelled — partial answer)")
                return sid, answer, None
            if answer:
                return sid, answer + "\n\n🛑 (cancelled — partial answer)", None
            return sid, None, app.CANCEL_MSG
        if handled:
            return sid, "", None
        if not settled and not timeout_reason and not cancelled:
            # Never replay a partially executed task automatically after a crash.
            return sid, answer or None, "Pi RPC exited before agent_settled"
        if turn_error and answer:
            if delivered:
                app._pi_send_trailer(cfg, live, "⚠️ (agent turn ended with an error)")
                return sid, answer, None
            return sid, answer + "\n\n⚠️ (agent turn ended with an error)", None
        if turn_error:
            return sid, None, f"Pi error: {turn_error}"
        if not answer:
            tail = (errbuf[0] if errbuf else "").strip()[-500:]
            return sid, None, "agent returned no text" + (f"\n{tail}" if tail else "")
        return sid, answer, None
    except Exception as e:
        with app.RUN_LOCK:
            if app.RUN_STATE.get("cancel"):
                cancelled = True
                return sid, "\n\n".join(segments) or None, app.CANCEL_MSG
        tail = (errbuf[0] if errbuf else "").strip()[-400:]
        detail = f"Pi RPC error: {e}"
        if tail:
            detail += "\n" + tail
        return sid, "\n\n".join(segments) or None, detail
    finally:
        orphaned = []
        if cfg.get('_questions') and run_id is not None:
            cfg['_questions'].close_owner(app.question_owner('pi', run_id))
        with app.RUN_LOCK:
            if app.RUN_STATE.get("pi_run_id") == run_id:
                app.RUN_STATE["pi_sid"] = None
                orphaned = list(app.RUN_STATE.get("pi_steers", {}).values())
                app.RUN_STATE["pi_steers"] = {}
                app.RUN_STATE["steer_pending"] = 0
                app.RUN_STATE["proc"] = None
                app.RUN_STATE.pop("cancel", None)
        for meta in orphaned:
            if not cancelled and not meta.get("consumed"):
                app._steer_fallback(cfg, meta, "Pi RPC closed before steering was consumed")
        if proc and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                proc.wait(timeout=3)
            except Exception:
                app.signal_run_process(proc, signal.SIGTERM)
                app.kill_after(proc, 2)
