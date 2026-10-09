"""Run lifecycle, native sessions, failover, steering admission and scheduling.

The explicit app argument supplies the single runtime owner and services.
No copied globals, entry-point import or hidden module-level runtime.
"""
import json
import os
import queue
import signal
import socket
import subprocess
import threading
import time

def signal_run_process(app, proc, sig):
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


def kill_after(app, proc, delay):
    """Escalate the isolated runner process group to SIGKILL."""

    def _k():
        if proc.poll() is None:
            app.signal_run_process(proc, signal.SIGKILL)

    t = threading.Timer(delay, _k)
    t.daemon = True
    t.start()


def run_timeout(app, cfg):
    """Idle timeout: progress and accepted steering renew this lease."""
    try:
        return max(1, int(cfg.get("run_timeout_s", app.RUN_TIMEOUT_S)))
    except (TypeError, ValueError):
        return app.RUN_TIMEOUT_S


def run_max(app, cfg):
    """Absolute safety cap, independent of ongoing output."""
    try:
        return max(app.run_timeout(cfg), int(cfg.get("run_max_s", app.RUN_MAX_S)))
    except (TypeError, ValueError):
        return app.RUN_MAX_S


def start_run_clock(app, ):
    now = time.monotonic()
    with app.RUN_LOCK:
        app.RUN_STATE["run_started"] = now
        app.RUN_STATE["last_progress"] = now
    return now


def mark_run_progress(app, ):
    with app.RUN_LOCK:
        app.RUN_STATE["last_progress"] = time.monotonic()


def run_expiry(app, cfg, started):
    """Return `idle` or `maximum` only when the matching lease expires."""
    now = time.monotonic()
    with app.RUN_LOCK:
        last = app.RUN_STATE.get("last_progress") or started
    if now - started >= app.run_max(cfg):
        return "maximum"
    if now - last >= app.run_timeout(cfg):
        return "idle"
    return None


def input_debounce(app, cfg):
    """Small merge window for Telegram's automatic multi-message splits."""
    try:
        return min(5.0, max(0.2, float(cfg.get("input_debounce_s", app.INPUT_DEBOUNCE_S))))
    except (TypeError, ValueError):
        return app.INPUT_DEBOUNCE_S


def outbox_dir(app, cfg):
    return cfg.get("outbox_dir") or os.path.join(cfg["workdir"], ".tgbridge-outbox")


def home_chat(app, cfg):
    """The DM chat: an allowed_chats entry that is also an allowed user id."""
    users = cfg.get("allowed_user_ids") or []
    for c in cfg.get("allowed_chats") or []:
        if c in users:
            return c
    ch = cfg.get("allowed_chats") or []
    return ch[0] if ch else None


def deliverable_answer(app, live, answer):
    """Text the worker still owes the chat after a finished run.

    Every native adapter uses the same journal to reconcile completed public
    segments with the final answer. Synthetic/legacy callers without a journal
    retain the existing streamed/missing-segment compatibility behavior.
    """
    if (live or {}).get("journal"):
        return live["journal"].remaining(answer)
    # Hermes gateway/run.py: suppress only content confirmed delivered, not
    # merely because some intermediate commentary reached the chat.
    if (live or {}).get("missing_segments"):
        return "\n\n".join(live["missing_segments"])
    if (live or {}).get("streamed"):
        return None
    return answer or ""


def telegram_session_name(app, chat_id, bot_username=None):
    """Origin label inside the agent's native session store, not a new store."""
    parts = ["Telegram", socket.gethostname()]
    if bot_username:
        parts.append("@" + str(bot_username).lstrip("@"))
    parts.append(f"chat {chat_id}")
    return " · ".join(parts)


def run_agent(app, cfg, session_id, prompt, live=None):
    """Stream the runner's JSON events live (Popen).

    A watchdog Timer enforces RUN_TIMEOUT_S without blocking the read loop;
    stderr is drained on a side thread so the pipe can never fill and deadlock.
    """
    rname = cfg.get("runner", "opencode")
    runner_fn = app.RUNNERS.get(rname)
    if not runner_fn:
        return (
            session_id,
            None,
            (f"unknown runner {rname!r} (available: {', '.join(sorted(app.RUNNERS))})"),
        )
    try:
        cmd, parse = runner_fn(session_id, prompt, cfg.get("model"))
        name = cfg.get("session_name")
        if name and rname == "pi":
            cmd[1:1] = ["--name", name]
        elif name and rname == "opencode" and not session_id:
            if "--title" in cmd:
                cmd[cmd.index("--title") + 1] = name
            else:
                cmd[1:1] = ["--title", name]
        cmd = app.apply_runner_policy(rname, cmd, cfg)
    except app.RunnerError as e:
        return session_id, None, str(e)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cfg["workdir"],
        start_new_session=True,
    )
    with app.RUN_LOCK:
        app.RUN_STATE["proc"] = proc  # exposed for /cancel and the shutdown path
        app.RUN_STATE["cli_run_id"] = app.RUN_STATE.get("cli_run_id", 0) + 1
        cli_run_id = app.RUN_STATE["cli_run_id"]
        app.RUN_STATE["steer_count"] = 0
        app.RUN_STATE["steer_pending"] = 0
        app.RUN_STATE["steer_errors"] = []
    assert proc.stdout and proc.stderr  # guaranteed: both opened with PIPE
    started = app.start_run_clock()
    timed_out = []
    stop_timeout = threading.Event()

    def watch_timeout():
        while not stop_timeout.wait(1.0):
            reason = app.run_expiry(cfg, started)
            if reason:
                timed_out.append(reason)
                app.signal_run_process(proc, signal.SIGKILL)
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
            app.mark_run_progress()
            trail = parse(ev, acc)
            app.publish_progress(cfg, live, app.cli_event(rname, ev))
            sid = acc["sid"]
            if live is not None:
                if trail:
                    live["trail"].append(trail)
                    app.edit_status(cfg, live)
                if acc.get("thinking"):
                    live["thinking"] = acc["thinking"]
                    app.edit_status(cfg, live)
                if acc["texts"]:
                    live["preview"] = acc["texts"][-1]
                    app.edit_status(cfg, live)
        proc.wait()
        journal = (live or {}).get("journal")
        if journal and journal.texts:
            acc["texts"] = list(journal.texts)
    finally:
        stop_timeout.set()
        with app.RUN_LOCK:
            if app.RUN_STATE.get("cli_run_id") == cli_run_id:
                app.RUN_STATE["proc"] = None
            cancelled = app.RUN_STATE.pop("cancel", False)
    if timed_out:
        reason = "idle" if timed_out[-1] == "idle" else "absolute maximum"
        partial = "\n\n".join(acc["texts"]).strip()
        if partial:
            return (
                sid,
                partial
                + "\n\n⚠️ (partial answer — hit the %s timeout and was killed)" % reason,
                None,
            )
        return sid, None, "agent hit the %s timeout and was killed" % reason
    if cancelled and proc.returncode != 0:
        partial = "\n\n".join(acc["texts"]).strip()
        if partial:
            return sid, partial + "\n\n🛑 (cancelled by user — partial answer)", None
        return sid, None, app.CANCEL_MSG
    if proc.returncode != 0:
        tail = (errbuf[0] if errbuf else "").strip()[-600:]
        return (
            sid,
            None,
            f"{cfg.get('runner', 'opencode')} failed rc={proc.returncode}\n{tail}",
        )
    if acc.get("runner_error"):
        return sid, None, str(acc["runner_error"])[-600:]
    if not acc["texts"]:
        return sid, None, "agent returned no text"
    return sid, "\n\n".join(acc["texts"]).strip(), None


def effective_run_config(app, cfg, state):
    """Honor the chat's runner selection; old global selection is a baseline.

    Empty chat override means file defaults, not a peer's model selection.
    Unscoped CLI/doctor callers can still inspect legacy configuration.
    """
    override = {}
    try:
        chat_id = getattr(app, 'chat_id', None)
        overrides = (state or {}).get('chat_runner_overrides') or {}
        override = overrides.get(str(chat_id), (state or {}).get('runner_override') or {})
    except AttributeError:
        override = {}
    rname = override.get("runner")
    if rname not in app.RUNNERS:
        return dict(cfg)
    out = dict(cfg, runner=rname)
    if override.get("model") is not None:
        out["model"] = override["model"]
    return out


def runner_session(app, state, chat_id, runner_name, legacy_runner=None):
    """Return one runner's native session, migrating the legacy flat index.

    Session identifiers are not portable across Codex, OpenCode, and Pi.
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


def store_runner_session(app, state, chat_id, runner_name, session_id):
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


def clear_runner_sessions(app, state, chat_id):
    key = str(chat_id)
    state.setdefault("sessions", {}).pop(key, None)
    state.setdefault("session_runners", {}).pop(key, None)
    state.setdefault("runner_sessions", {}).pop(key, None)


def run_one(app, cfg, session_id, prompt, live=None):
    """Run one configured runner through its declared CLI/server transport."""
    rname = cfg.get("runner", "opencode")
    if cfg.get("model") == app.AUTO_OPENCODE_GO_MODEL:
        try:
            cfg = dict(cfg, model=app.discover_opencode_go_models(cfg)[0])
        except app.RunnerError as exc:
            return session_id, None, str(exc)
    mode = app.resolve_runner_mode(cfg, rname)
    if mode == "cli":
        return app.run_agent(cfg, session_id, prompt, live)
    if mode != "server":
        return session_id, None, f"unknown runner_mode {mode!r} (available: cli, server)"
    adapter = app.SERVER_RUNNERS.get(rname)
    if not adapter:
        return (
            session_id,
            None,
            f"runner {rname!r} has no server transport; use runner_mode='cli'",
        )
    if not adapter["healthy"](cfg):
        where = f" at {app.server_url(cfg)}" if rname == "opencode" else ""
        return (
            session_id,
            None,
            f"{rname} server transport unavailable{where}; "
            "start/install it or use runner_mode='cli'",
        )
    return adapter["run"](cfg, session_id, prompt, live)


def run_with_fallbacks(app, cfg, session_id, prompt, live=None, result_meta=None):
    """Fail over an unusable runner only before observed tool execution.

    Tag quota/dead binary/network/empty-answer failures and try the next
    fallback when no tool action was observed. Cancel and uncertain executed
    actions stop the chain; replaying them in a fresh runner is unsafe. Fallback steps always start a fresh session — session/thread
    IDs are runner-native and cannot resume across runners — and the
    delivered answer carries a one-line 🔀 header naming the runner that
    actually answered.
    """
    rname = cfg.get("runner", "opencode")
    model = app.resolve_model(cfg, rname)
    if live is not None:
        # Each attempt owns its own delivery state: a streamed Pi run that
        # dies must not make a fallback answer look already-delivered.
        live["streamed"] = False
        live["missing_segments"] = []
        live.pop("journal", None)
    new_sid, answer, err = app.run_one(
        dict(cfg, runner=rname, model=model), session_id, prompt, live
    )
    uncertain = bool(live and live.get("journal") and live["journal"].actions)
    if answer is not None or (err or "") == app.CANCEL_MSG or uncertain:
        # A tool already ran: retrying the original prompt in a fresh runner
        # could repeat external side effects whose outcome we do not know.
        if result_meta is not None:
            result_meta.update(runner=rname, model=model)
        return new_sid, answer, err
    kind = app.classify_run_error(err, cfg.get("quota_markers"))
    reason = "hit a limit" if kind == "quota" else "is unusable"
    tried = [(rname, model)]
    with app.RUN_LOCK:
        if app.RUN_STATE.get("current"):
            app.RUN_STATE["current"]["runner"] = rname
    for step_runner, step_model in app.fallback_chain(cfg):
        note = f"🔀 {rname} {reason} — failing over to {step_runner}"
        app.log(
            f"run failover ({kind}): {rname} -> {step_runner} ({step_model or 'runner default'})"
        )
        app.audit(
            "run_fallback",
            kind=kind,
            from_runner=rname,
            to_runner=step_runner,
            to_model=step_model,
            err=(err or "")[:200],
        )
        if live is not None:
            live["trail"].append(note)
            app.edit_status(cfg, live)
        with app.RUN_LOCK:
            if app.RUN_STATE.get("current"):
                app.RUN_STATE["current"]["runner"] = step_runner
                app.RUN_STATE["current"]["mode"] = app.resolve_runner_mode(cfg, step_runner)
        step_cfg = dict(cfg, runner=step_runner, model=step_model)
        if live is not None:
            live["streamed"] = False
            live["missing_segments"] = []
            live.pop("journal", None)
        try:
            fallback_timeout = int(cfg.get("fallback_run_timeout_s", 0))
        except (TypeError, ValueError):
            fallback_timeout = 0
        if fallback_timeout > 0:
            step_cfg["run_timeout_s"] = min(app.run_timeout(cfg), fallback_timeout)
        new_sid, answer, err = app.run_one(step_cfg, None, prompt, live)
        rname = step_runner
        tried.append((step_runner, step_model))
        if err == app.CANCEL_MSG or (err and live and live.get("journal") and live["journal"].actions):
            return new_sid, answer, err
        if answer is not None:
            if result_meta is not None:
                result_meta.update(runner=step_runner, model=step_model)
            header = f"🔀 {tried[0][0]} {reason} — answered via {step_runner}"
            if step_model:
                header += f" ({step_model})"
            return new_sid, header + "\n\n" + answer, err
    chain = " -> ".join(r for r, _ in tried)
    return session_id, None, f"all runners exhausted ({chain}): {err}"


def runner_mode(app, cfg):
    """Explicit transport mode with compatibility for the WIP boolean key."""
    mode = cfg.get("runner_mode")
    if mode is None:
        return "server" if cfg.get("server_runner") else "cli"
    return str(mode).lower()


def resolve_runner_mode(app, cfg, runner_name):
    """Per-runner transport override with the legacy global mode as fallback."""
    modes = cfg.get("runner_modes") or {}
    if isinstance(modes, dict) and modes.get(runner_name):
        return str(modes[runner_name]).lower()
    return app.runner_mode(cfg)


def _begin_steer(app, chat_id):
    """Reserve a supported same-chat steer without racing run completion."""
    with app.RUN_LOCK:
        cur = app.RUN_STATE.get("current")
        if not (app.RUN_STATE.get("busy") and cur and cur.get("chat") == chat_id
                and not app.RUN_STATE.get("cancel")):
            return None
        thread_id = app.RUN_STATE.get("codex_thread_id")
        turn_id = app.RUN_STATE.get("codex_turn_id")
        if thread_id and turn_id:
            app.RUN_STATE["steer_pending"] = app.RUN_STATE.get("steer_pending", 0) + 1
            app.RUN_STATE["last_progress"] = time.monotonic()
            return {
                "transport": "codex_turn_steer",
                "sid": thread_id,
                "turn_id": turn_id,
                "run_id": app.RUN_STATE.get("codex_run_id", 0),
            }
        if app.RUN_STATE.get("pi_sid") and app.RUN_STATE.get("pi_run_id"):
            app.RUN_STATE["steer_pending"] = app.RUN_STATE.get("steer_pending", 0) + 1
            app.RUN_STATE["last_progress"] = time.monotonic()
            return {
                "transport": "pi_rpc_steer",
                "sid": app.RUN_STATE["pi_sid"],
                "run_id": app.RUN_STATE["pi_run_id"],
            }
        sid = app.RUN_STATE.get("server_sid")
        if sid and app.RUN_STATE.get("server_api") == "v2":
            app.RUN_STATE["steer_pending"] = app.RUN_STATE.get("steer_pending", 0) + 1
            app.RUN_STATE["last_progress"] = time.monotonic()
            return {
                "transport": "opencode_v2_steer",
                "sid": sid,
                "run_id": app.RUN_STATE.get("server_run_id", 0),
                "base_url": app.RUN_STATE.get("server_url"),
            }
        return None


def should_steer(app, chat_id):
    """Whether the active same-chat transport supports live steering."""
    with app.RUN_LOCK:
        cur = app.RUN_STATE.get("current")
        return bool(
            app.RUN_STATE.get("busy")
            and cur
            and cur.get("chat") == chat_id
            and not app.RUN_STATE.get("cancel")
            and (
                (app.RUN_STATE.get("pi_sid") and app.RUN_STATE.get("pi_run_id"))
                or (app.RUN_STATE.get("server_sid") and app.RUN_STATE.get("server_api") == "v2")
                or (app.RUN_STATE.get("codex_thread_id") and app.RUN_STATE.get("codex_turn_id"))
            )
        )


def _steer_fallback(app, cfg, meta, err):
    """Preserve a rejected live steer as a normal next turn and tell the user."""
    chat_id = meta.get("chat_id")
    text = meta.get("text") or ""
    if chat_id is None:
        return
    input_ids = meta.get("input_ids") or []
    if input_ids and cfg.get("_inbox"):
        cfg["_inbox"].transition(input_ids, "queued")
        app.PROMPT_Q.put({"chat_id": chat_id, "message_id": meta.get("message_id"),
                      "prompt": text, "input_ids": input_ids})
    else:
        app.PROMPT_Q.put((chat_id, meta.get("message_id"), text))
    app.audit(
        "steer_fallback_queued",
        chat_id=chat_id,
        session=meta.get("sid"),
        chars=len(text),
        err=str(err)[:200],
    )
    app.send(
        cfg["bot_token"],
        chat_id,
        "⏳ Queued for the next turn.",
    )


def question_owner(app, transport, run_id):
    """Question broker is shared; run counters alone are only chat-local."""
    return (transport, getattr(app, 'chat_id', None),
            getattr(app, 'execution_id', None), run_id)


def stop_runtime(app):
    """Stop this execution owner's process/server, never another chat's run."""
    with app.RUN_LOCK:
        app.RUN_STATE['cancel'] = True
        proc = app.RUN_STATE.get('proc')
        sid = app.RUN_STATE.get('server_sid')
        directory = app.RUN_STATE.get('server_directory')
        base_url = app.RUN_STATE.get('server_url')
        v2 = app.RUN_STATE.get('server_api') == 'v2'
    if proc is not None and proc.poll() is None:
        app.signal_run_process(proc, signal.SIGTERM)
        app.kill_after(proc, 2)
    if sid:
        try:
            app._server_call('POST', f'/api/session/{sid}/interrupt' if v2 else f'/session/{sid}/abort',
                             timeout=5, directory=None if v2 else directory, base_url=base_url)
        except Exception:
            pass


def worker(app, cfg, state):
    """Serial per-owner consumer; independent chats have separate owners.

    One item = one try/except: a bad item must never kill the thread."""
    while not getattr(app, 'STOPPING', False):
        if getattr(app, 'execution_id', None):
            try:
                entry = app.PROMPT_Q.get(timeout=0.1)
            except queue.Empty:
                if cfg['_chat_dispatcher'].release_retired_worker(app):
                    return
                continue
        else:
            entry = app.PROMPT_Q.get()
        if getattr(app, 'STOPPING', False):
            app.PROMPT_Q.task_done()
            return
        chat_id = message_id = prompt = None
        input_ids = []
        inbox = cfg.get("_inbox")
        completed = False
        execution_finished = False
        delivery_confirmed = True
        try:
            chat_id, message_id, prompt = app.unpack_entry(entry)
            input_ids = (entry.get("input_ids") or []) if isinstance(entry, dict) else []
            if inbox:
                inbox.transition(input_ids, "executing")
            with app.STATE_LOCK:
                run_cfg = app.effective_run_config(cfg, state)
            run_cfg["session_name"] = app.telegram_session_name(chat_id, state.get("bot_username"))
            rname = run_cfg.get("runner", "opencode")
            mode = app.resolve_runner_mode(run_cfg, rname)
            with app.RUN_LOCK:
                app.RUN_STATE["busy"] = True
                app.RUN_STATE["active_input_ids"] = []
                app.RUN_STATE["cancel"] = False
                app.RUN_STATE["current"] = {
                    "chat": chat_id,
                    "since": time.time(),
                    "prompt": prompt[:60],
                    "runner": rname,
                    "mode": mode,
                }
            with app.STATE_LOCK:
                session_epoch = getattr(app, 'SESSION_EPOCH', 0)
                session_id = app.runner_session(
                    state,
                    chat_id,
                    rname,
                    legacy_runner=cfg.get("runner", "opencode"),
                )
            outbox = app.outbox_dir(cfg)
            app.ensure_private_dir(outbox)
            prompt = prompt + (
                f"\n\n(To give files to the user, write them into {outbox}/ "
                "— they are delivered automatically after this run.)"
            )
            app.audit(
                "run_start",
                chat_id=chat_id,
                chars=len(prompt),
                session=session_id,
                runner=rname,
                mode=mode,
            )
            app.react(cfg, chat_id, message_id, "👀")
            status = app.api(
                cfg["bot_token"],
                "sendMessage",
                chat_id=chat_id,
                text="⚙️ working… 0s",
                disable_notification=True,
                reply_parameters=json.dumps({"message_id": message_id}),
            )
            live = {
                "chat_id": chat_id,
                "requester_user_id": next((e.get('user_id') for e in inbox.pending(chat_id)
                                           if e['id'] in input_ids), None) if inbox else None,
                "status_id": (status.get("result") or {}).get("message_id")
                if status
                else None,
                "trail": [],
                "notes": [],
                "start": time.time(),
                "last_edit": 0,
                "reply_to": message_id,
                "streamed": False,
                "tokens": 0,
                "cost": 0.0,
            }
            stop_typing = threading.Event()
            threading.Thread(
                target=app.typing_loop,
                args=(cfg["bot_token"], chat_id, stop_typing),
                daemon=True,
            ).start()
            app.log(
                f"chat={chat_id} run start (runner={rname}/{mode}, "
                f"session={session_id}, q={app.PROMPT_Q.qsize()})"
            )
            result_meta = {}
            try:
                new_sid, answer, err = app.run_with_fallbacks(
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
                # Keep task ownership during final reconciliation. Otherwise
                # a near-end input can be mistaken for an idle/new task before
                # the current result and recovery state are settled.
            if (
                err
                and session_id
                and "failed rc=" in err
                and not err.startswith("all runners exhausted")
                and err != app.CANCEL_MSG
                and not (live.get("journal") and live["journal"].actions)
            ):
                live["trail"].append("♻️ stale session — retrying fresh")
                result_meta.clear()
                new_sid, answer, err = app.run_with_fallbacks(
                    run_cfg, None, prompt, live, result_meta=result_meta
                )
            if live["status_id"]:
                app.edit_status(cfg, live, final="✅ done" if not err else "🔴 failed")
            with app.STATE_LOCK:
                used_runner = result_meta.get("runner", rname)
                if new_sid and session_epoch == getattr(app, 'SESSION_EPOCH', 0):
                    app.store_runner_session(
                        app.session_state if getattr(app, 'RETIRED', False) else state,
                        chat_id, used_runner, new_sid,
                    )
                app.save_json(app.STATE_PATH, state)
            if inbox:
                inbox.stage_result(input_ids, answer or err or "")
            execution_finished = True
            # An error is new content, not proof earlier segments were delivered.
            if err and live.get("missing_segments"):
                for segment in live["missing_segments"]:
                    delivery_confirmed = bool(app.send_retry(cfg, chat_id, segment, reply_to=message_id)) and delivery_confirmed
            if err == app.CANCEL_MSG:
                app.audit("run_cancelled", chat_id=chat_id)
                delivery_confirmed = bool(app.send_retry(cfg, chat_id, app.CANCEL_MSG)) and delivery_confirmed
                app.log(f"chat={chat_id} cancelled")
            elif err:
                app.react(cfg, chat_id, message_id, "👎")
                delivery_confirmed = bool(app.send_retry(cfg, chat_id, f"⚠️ {err}")) and delivery_confirmed
                app.audit("run_error", chat_id=chat_id, err=err[:200])
                app.log(f"chat={chat_id} error: {err[:120]}")
            else:
                app.react(cfg, chat_id, message_id, "👍")
                streamed = bool(live.get("streamed"))
                payload = app.deliverable_answer(live, answer)
                if live.get("missing_segments"):
                    for segment in live["missing_segments"]:
                        delivery_confirmed = bool(app.send_retry(cfg, chat_id, segment, reply_to=message_id)) and delivery_confirmed
                elif payload:
                    delivery_confirmed = bool(app.send_retry(cfg, chat_id, payload, reply_to=message_id)) and delivery_confirmed
                app.audit(
                    "run_done",
                    chat_id=chat_id,
                    runner=used_runner,
                    chars=len(answer or ""),
                    secs=int(time.time() - live["start"]),
                    streamed=streamed,
                )
                app.log(
                    f"chat={chat_id} done ({len(answer or '')} chars, "
                    f"runner={used_runner}, session={new_sid}"
                    f"{', streamed' if streamed else ''})"
                )
            completed = True
            if inbox:
                with app.RUN_LOCK:
                    additional = list(app.RUN_STATE.get("active_input_ids") or [])
                # Rejected steers queued by fallback stay replayable.
                accepted = [e['id'] for e in inbox.pending(chat_id)
                            if e['id'] in additional and e['status'] == 'executing']
                inbox.settle(list(input_ids) + accepted, "cancelled" if err == app.CANCEL_MSG else
                             "interrupted" if err else "completed", answer or err or "", delivery_confirmed)
        except Exception as e:
            app.log(f"worker item error: {e}")
            app.audit("worker_error", err=str(e)[:200])
            if chat_id:
                app.send(cfg["bot_token"], chat_id, f"⚠️ bridge error: {e}")
        finally:
            if inbox and not completed:
                inbox.transition(input_ids, "result_unconfirmed" if execution_finished else "interrupted")
            # Early errors (status/reaction/setup) must not leave a phantom
            # busy run which keeps subsequent input waiting for live steering.
            with app.RUN_LOCK:
                app.RUN_STATE["busy"] = False
                app.RUN_STATE["current"] = None
            try:
                outbox = app.outbox_dir(cfg)
                for fn in sorted(os.listdir(outbox)):
                    p = os.path.join(outbox, fn)
                    if os.path.isfile(p) and chat_id is not None:
                        res = app.send_document(cfg["bot_token"], chat_id, p)
                        if res and res.get("ok"):
                            os.remove(p)  # keep the file if delivery failed
            except FileNotFoundError:
                pass
            except Exception as e:
                app.log(f"outbox delivery: {e}")
            app.PROMPT_Q.task_done()


def fire_at(app, cfg, state, due):
    with app.STATE_LOCK:
        entry = state.get("at", {}).pop(str(due), None)
        if entry:
            app.save_json(app.STATE_PATH, state)
    if not entry:
        return
    app.audit("at_fire", chat_id=entry["chat_id"], prompt=entry["prompt"][:80])
    app.PROMPT_Q.put((entry["chat_id"], entry["message_id"], entry["prompt"]))


def rearm_at(app, cfg, state):
    """Re-arm /at timers after a restart — nothing scheduled is silently lost."""
    for due in list(state.get("at", {})):
        try:
            d = float(due)
        except ValueError:
            continue
        t = threading.Timer(max(d - time.time(), 1), app.fire_at, args=(cfg, state, d))
        t.daemon = True
        t.start()
