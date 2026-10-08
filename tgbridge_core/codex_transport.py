"""Codex app-server transport, question responses and turn steering.

The explicit app argument supplies the single runtime owner and services.
No copied globals, entry-point import or hidden module-level runtime.
"""
import json
import queue
import signal
import subprocess
import threading
import time

def _codex_rpc_write(app, proc, payload):
    """Write one newline-framed app-server request without interleaving writers."""
    if not proc.stdin or proc.poll() is not None:
        raise RuntimeError("Codex app-server is no longer running")
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    with app.CODEX_WRITE_LOCK:
        proc.stdin.write(line)
        proc.stdin.flush()


def _codex_steer_deliver(
    app, cfg, sid, turn_id, run_id, text, chat_id=None, message_id=None, input_ids=()
):
    """Send true same-turn steering to Codex app-server's `turn/steer`."""
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
                app.RUN_STATE.get("codex_thread_id") == sid
                and app.RUN_STATE.get("codex_turn_id") == turn_id
                and app.RUN_STATE.get("codex_run_id") == run_id
            ):
                raise RuntimeError("active Codex turn changed before steering delivery")
            proc = app.RUN_STATE.get("proc")
            seq = app.RUN_STATE.get("codex_request_id", 10) + 1
            app.RUN_STATE["codex_request_id"] = seq
            request_id = f"steer-{run_id}-{seq}"
            app.RUN_STATE.setdefault("codex_steers", {})[request_id] = meta
        app._codex_rpc_write(
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
        with app.RUN_LOCK:
            if request_id:
                app.RUN_STATE.setdefault("codex_steers", {}).pop(request_id, None)
            if app.RUN_STATE.get("codex_run_id") == run_id:
                app.RUN_STATE["steer_pending"] = max(
                    0, app.RUN_STATE.get("steer_pending", 1) - 1
                )
        app.audit(
            "steer_error",
            session=sid,
            transport="codex_turn_steer",
            err=str(e)[:200],
        )
        app.log(f"codex turn/steer write: {e}")
        app._steer_fallback(cfg, meta, e)


def codex_app_server_ok(app, _cfg=None):
    """Installed Codex must expose the app-server transport used for steering."""
    try:
        result = subprocess.run(
            [app._bin("CODEX_BIN", "codex"), "app-server", "--help"],
            text=True,
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _codex_read_events(app, stream, events):
    try:
        for raw in stream:
            try:
                events.put(json.loads(raw))
            except json.JSONDecodeError:
                continue
    finally:
        events.put(None)


def _codex_wait_response(app, events, request_id, timeout=20, keep=None):
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


def _codex_item_trail(app, item):
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


def _codex_handle_steer_response(app, cfg, event, run_id):
    request_id = event.get("id")
    if not isinstance(request_id, str) or not request_id.startswith("steer-"):
        return False
    with app.RUN_LOCK:
        meta = app.RUN_STATE.setdefault("codex_steers", {}).pop(request_id, None)
        if meta and app.RUN_STATE.get("codex_run_id") == run_id:
            app.RUN_STATE["steer_pending"] = max(0, app.RUN_STATE.get("steer_pending", 1) - 1)
            if not event.get("error"):
                app.RUN_STATE["steer_count"] = app.RUN_STATE.get("steer_count", 0) + 1
    if not meta:
        return True
    if event.get("error"):
        detail = json.dumps(event["error"], ensure_ascii=False)[-500:]
        app.audit(
            "steer_error",
            session=meta["sid"],
            transport="codex_turn_steer",
            err=detail,
        )
        app._steer_fallback(cfg, meta, detail)
    else:
        if cfg.get("_inbox"):
            cfg["_inbox"].transition(meta.get("input_ids") or [], "executing")
        app.audit(
            "steer_delivered",
            session=meta["sid"],
            transport="codex_turn_steer",
            chars=len(meta["text"]),
        )
    return True


def _codex_user_question(app, cfg, proc, event, live, run_id):
    """Return actual human answers under the native question ids."""
    questions = (event.get('params') or {}).get('questions') or []
    broker = cfg.get('_questions')
    answers = {}
    replied = False
    def respond():
        nonlocal replied
        if replied:
            return
        with app.RUN_LOCK:
            if app.RUN_STATE.get('proc') is not proc or app.RUN_STATE.get('codex_run_id') != run_id or app.RUN_STATE.get('cancel'):
                raise RuntimeError('question belongs to an ended Codex task')
        app._codex_rpc_write(proc, {'id': event['id'], 'result': {'answers': answers}})
        replied = True
        app.mark_run_progress()
    def offer(index):
        if index >= len(questions):
            respond()
            return
        question = questions[index]
        options = question.get('options') or []
        def selected(value):
            answers[question['id']] = {'answers': [] if value is None else [value]}
            if value is None:
                respond()
            else:
                offer(index + 1)
        broker.offer(live['chat_id'], live.get('requester_user_id'), question['question'],
                     [option['label'] for option in options], selected, owner=('codex', run_id),
                     message='\n'.join(option['label'] + '：' + option.get('description', '') for option in options),
                     timeout=app.run_max(cfg) * 1000, echo_answer=not question.get('isSecret'))
    if not broker or not live or live.get('chat_id') is None:
        respond()  # an unavailable UI never means a default selection
        return
    try:
        offer(0)
    except Exception as error:
        app.audit('codex_question_failed', error=type(error).__name__)
        respond()


def run_codex_app_server(app, cfg, session_id, prompt, live=None):
    """Run Codex through app-server so `turn/steer` reaches the active turn."""
    sid = session_id
    turn_id = None
    timed_out = False
    cancelled = False
    proc = None
    events = queue.Queue()
    errbuf = []
    answer_final = []
    seen_messages = set()
    previews = {}
    turn_error = None
    run_id = None
    outstanding = []
    try:
        proc = subprocess.Popen(
            [
                app._bin("CODEX_BIN", "codex"),
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
            target=app._codex_read_events, args=(proc.stdout, events), daemon=True
        ).start()
        stderr = proc.stderr
        threading.Thread(
            target=lambda: errbuf.append(stderr.read() or ""), daemon=True
        ).start()
        with app.RUN_LOCK:
            app.RUN_STATE["proc"] = proc
            app.RUN_STATE["codex_run_id"] = app.RUN_STATE.get("codex_run_id", 0) + 1
            run_id = app.RUN_STATE["codex_run_id"]
            app.RUN_STATE["codex_thread_id"] = None
            app.RUN_STATE["codex_turn_id"] = None
            app.RUN_STATE["codex_steers"] = {}
            app.RUN_STATE["steer_count"] = 0
            app.RUN_STATE["steer_pending"] = 0
            app.RUN_STATE["steer_errors"] = []
            app.RUN_STATE["cancel"] = False

        app._codex_rpc_write(
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
        app._codex_wait_response(events, 1)
        app._codex_rpc_write(proc, {"method": "initialized"})

        access = {
            "cwd": cfg["workdir"],
            "approvalPolicy": "never",
            "sandbox": (
                "danger-full-access" if cfg.get("codex_yolo", False) else "read-only"
            ),
        }
        if app.resolve_model(cfg, "codex"):
            access["model"] = app.resolve_model(cfg, "codex")
        setup_id = 2
        if session_id:
            params = {**access, "threadId": session_id}
            app._codex_rpc_write(
                proc,
                {"id": setup_id, "method": "thread/resume", "params": params},
            )
            try:
                setup = app._codex_wait_response(events, setup_id)
            except RuntimeError:
                setup_id += 1
                app._codex_rpc_write(
                    proc,
                    {
                        "id": setup_id,
                        "method": "thread/start",
                        "params": {**access, "ephemeral": False},
                    },
                )
                setup = app._codex_wait_response(events, setup_id)
        else:
            app._codex_rpc_write(
                proc,
                {
                    "id": setup_id,
                    "method": "thread/start",
                    "params": {**access, "ephemeral": False},
                },
            )
            setup = app._codex_wait_response(events, setup_id)
        sid = ((setup.get("thread") or {}).get("id")) or session_id
        if not sid:
            raise RuntimeError("Codex app-server returned no thread id")

        turn_request_id = setup_id + 1
        app._codex_rpc_write(
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
        started_response = app._codex_wait_response(
            events, turn_request_id, keep=early_events
        )
        turn_id = (started_response.get("turn") or {}).get("id")
        if not turn_id:
            raise RuntimeError("Codex app-server returned no active turn id")
        with app.RUN_LOCK:
            if app.RUN_STATE.get("codex_run_id") == run_id:
                app.RUN_STATE["codex_thread_id"] = sid
                app.RUN_STATE["codex_turn_id"] = turn_id
                if app.RUN_STATE.get("current"):
                    app.RUN_STATE["current"]["session"] = sid

        clock_started = app.start_run_clock()
        timeout_reason = None
        completed = False
        while not completed:
            with app.RUN_LOCK:
                cancelled = bool(app.RUN_STATE.get("cancel"))
            if cancelled:
                app.signal_run_process(proc, signal.SIGTERM)
                app.kill_after(proc, 3)
                break
            if cfg.get('_questions') and cfg['_questions'].has_owner(('codex', run_id)):
                app.mark_run_progress()
            timeout_reason = app.run_expiry(cfg, clock_started)
            if timeout_reason:
                timed_out = True
                app.signal_run_process(proc, signal.SIGTERM)
                app.kill_after(proc, 3)
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
            if app._codex_handle_steer_response(cfg, event, run_id):
                continue
            method = event.get("method")
            params = event.get("params") or {}
            if params.get("turnId") not in (None, turn_id):
                continue
            item = params.get("item") or {}
            if method == 'item/tool/requestUserInput' and 'id' in event:
                app._codex_user_question(cfg, proc, event, live, run_id)
                continue
            app.publish_progress(cfg, live, app.codex_event(event))
            if method == "item/started":
                trail = app._codex_item_trail(item)
                if trail and live is not None:
                    live["trail"].append(trail)
                    app.edit_status(cfg, live)
            elif method == "item/agentMessage/delta":
                item_id = params.get("itemId") or "message"
                previews[item_id] = previews.get(item_id, "") + (
                    params.get("delta") or ""
                )
                if live is not None and previews[item_id]:
                    live["preview"] = previews[item_id]
                    app.edit_status(cfg, live)
            elif method == "item/completed" and item.get("type") == "agentMessage":
                item_id = item.get("id")
                if item_id and item_id in seen_messages:
                    continue
                if item_id:
                    seen_messages.add(item_id)
                text = (item.get("text") or "").strip()
                if text:
                    answer_final.append(text)
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
                app._codex_handle_steer_response(cfg, event, run_id)

        with app.RUN_LOCK:
            outstanding = list(app.RUN_STATE.get("codex_steers", {}).values())
            app.RUN_STATE["codex_steers"] = {}
            app.RUN_STATE["steer_pending"] = 0
            cancelled = bool(app.RUN_STATE.pop("cancel", False)) or cancelled
        for meta in outstanding:
            app._steer_fallback(cfg, meta, "app-server closed before steer acknowledgement")

        answer = "\n\n".join(answer_final).strip()
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
            return sid, None, app.CANCEL_MSG
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
        if cfg.get('_questions') and run_id is not None:
            cfg['_questions'].close_owner(('codex', run_id))
        with app.RUN_LOCK:
            if app.RUN_STATE.get("codex_run_id") == run_id:
                app.RUN_STATE["codex_thread_id"] = None
                app.RUN_STATE["codex_turn_id"] = None
                app.RUN_STATE["codex_steers"] = {}
                app.RUN_STATE["steer_pending"] = 0
                app.RUN_STATE["proc"] = None
                app.RUN_STATE.pop("cancel", None)
        if proc and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                proc.wait(timeout=2)
            except Exception:
                app.signal_run_process(proc, signal.SIGTERM)
                app.kill_after(proc, 2)
