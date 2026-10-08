"""OpenCode HTTP transports and safe-boundary steering.

The explicit app argument supplies the single runtime owner and services.
No copied globals, entry-point import or hidden module-level runtime.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

def _server_call(app, method, path, body=None, timeout=15, directory=None, base_url=None):
    """JSON call to the local OpenCode server; returns parsed body or None."""
    if directory:
        separator = "&" if "?" in path else "?"
        path += separator + urllib.parse.urlencode({"directory": directory})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        (base_url or app.OPENCODE_SERVER).rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else None


def server_url(app, cfg):
    return str(cfg.get("server_url") or app.OPENCODE_SERVER).rstrip("/")


def server_ok(app, cfg=None):
    try:
        health = app._server_call(
            "GET",
            "/global/health",
            timeout=2,
            base_url=app.server_url(cfg or {}),
        )
        return bool((health or {}).get("healthy"))
    except Exception:
        return False


def server_event(app, ev, acc, seen):
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


def server_messages(app, messages, baseline, acc, seen):
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
            trail = app.server_event(
                {"type": "message.part.updated", "data": {"part": part}},
                acc,
                seen,
            )
            if trail:
                trails.append(trail)
    return trails, infos


def _server_poll_interval(app, cfg):
    try:
        return min(5.0, max(0.1, float(cfg.get("server_poll_s", app.SERVER_POLL_S))))
    except (TypeError, ValueError):
        return app.SERVER_POLL_S


def _steer_deliver(app, sid, run_id, directory, base_url, text):
    """Submit one non-blocking prompt and publish its delivery atomically."""
    err = None
    try:
        app._server_call(
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
        app.audit("steer_delivered", session=sid, chars=len(text))
    except Exception as e:
        err = str(e)
        app.audit("steer_error", session=sid, err=err[:200])
        app.log(f"steer deliver: {e}")
    finally:
        with app.RUN_LOCK:
            if (
                app.RUN_STATE.get("server_sid") == sid
                and app.RUN_STATE.get("server_run_id") == run_id
                and app.RUN_STATE.get("server_directory") == directory
                and app.RUN_STATE.get("server_url") == base_url
            ):
                if err:
                    app.RUN_STATE.setdefault("steer_errors", []).append(err)
                else:
                    app.RUN_STATE["steer_count"] = app.RUN_STATE.get("steer_count", 0) + 1
                app.RUN_STATE["steer_pending"] = max(
                    0, app.RUN_STATE.get("steer_pending", 1) - 1
                )


def _steer_deliver_v2(app, cfg, sid, run_id, base_url, text, chat_id, message_id, input_ids=()):
    """Admit a durable native OpenCode v2 steer at the next safe boundary."""
    meta = {
        "sid": sid,
        "text": text,
        "chat_id": chat_id,
        "message_id": message_id,
        "input_ids": input_ids,
    }
    err = None
    try:
        with app.RUN_LOCK:
            if not (
                app.RUN_STATE.get("server_sid") == sid
                and app.RUN_STATE.get("server_run_id") == run_id
                and app.RUN_STATE.get("server_api") == "v2"
            ):
                raise RuntimeError("active OpenCode run changed before steer delivery")
        app._server_call(
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
        if cfg.get("_inbox"):
            cfg["_inbox"].transition(input_ids, "executing")
        app.audit(
            "steer_delivered",
            session=sid,
            transport="opencode_v2_steer",
            chars=len(text),
        )
    except Exception as e:
        err = str(e)
        app.audit(
            "steer_error",
            session=sid,
            transport="opencode_v2_steer",
            err=err[:200],
        )
        app.log(f"OpenCode v2 steer deliver: {e}")
    finally:
        with app.RUN_LOCK:
            if (
                app.RUN_STATE.get("server_sid") == sid
                and app.RUN_STATE.get("server_run_id") == run_id
            ):
                if err:
                    app.RUN_STATE.setdefault("steer_errors", []).append(err)
                else:
                    app.RUN_STATE["steer_count"] = app.RUN_STATE.get("steer_count", 0) + 1
                app.RUN_STATE["steer_pending"] = max(
                    0, app.RUN_STATE.get("steer_pending", 1) - 1
                )
        if err:
            app._steer_fallback(cfg, meta, err)


def run_agent_server_v1(app, cfg, session_id, prompt, live=None):
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
    base_url = app.server_url(cfg)
    acc = {"parts": {}, "order": [], "thinking": None, "errors": []}
    seen = set()
    try:
        if session_id:
            sid = session_id
        else:
            created = app._server_call(
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
            app._server_call(
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
        model = app.resolve_model(cfg, cfg.get("runner", "opencode"))
        if model and "/" in model:
            prov, _, mid = model.partition("/")
            body["model"] = {"providerID": prov, "modelID": mid}
        app._server_call(
            "POST",
            f"/session/{sid}/prompt_async",
            body,
            timeout=5,
            directory=directory,
            base_url=base_url,
        )
        with app.RUN_LOCK:
            app.RUN_STATE["server_run_id"] = app.RUN_STATE.get("server_run_id", 0) + 1
            app.RUN_STATE["server_sid"] = sid
            app.RUN_STATE["server_directory"] = directory
            app.RUN_STATE["server_url"] = base_url
            app.RUN_STATE["server_api"] = "v1"
            app.RUN_STATE["steer_count"] = 0
            app.RUN_STATE["steer_pending"] = 0
            app.RUN_STATE["steer_errors"] = []
            app.RUN_STATE["cancel"] = False
            if app.RUN_STATE.get("current"):
                app.RUN_STATE["current"]["session"] = sid
        started = app.start_run_clock()
        timeout_reason = None
        last_snapshot = json.dumps(before, sort_keys=True, ensure_ascii=False)
        poll_s = app._server_poll_interval(cfg)
        quiet_s = max(app.SERVER_QUIET_S, poll_s)
        while True:
            with app.RUN_LOCK:
                cancelled = bool(app.RUN_STATE.get("cancel"))
            if cancelled:
                try:
                    app._server_call(
                        "POST",
                        f"/session/{sid}/abort",
                        timeout=5,
                        directory=directory,
                        base_url=base_url,
                    )
                except Exception as e:
                    app.log(f"server abort: {e}")
                break
            timeout_reason = app.run_expiry(cfg, started)
            if timeout_reason:
                timed_out = True
                try:
                    app._server_call(
                        "POST",
                        f"/session/{sid}/abort",
                        timeout=5,
                        directory=directory,
                        base_url=base_url,
                    )
                except Exception as e:
                    app.log(f"server timeout abort: {e}")
                break

            messages = (
                app._server_call(
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
                app.mark_run_progress()
            trails, infos = app.server_messages(messages, baseline, acc, seen)
            app.publish_snapshot(cfg, live, messages, baseline)
            if live is not None:
                if trails:
                    live["trail"].extend(trails)
                    app.edit_status(cfg, live)
                if acc["thinking"]:
                    live["thinking"] = acc["thinking"]
                    app.edit_status(cfg, live)
                if acc["order"]:
                    last = acc["parts"].get(acc["order"][-1])
                    if last:
                        live["preview"] = last
                        app.edit_status(cfg, live)

            statuses = (
                app._server_call(
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
                app.mark_run_progress()
            completed = bool(infos) and all(
                (info.get("time") or {}).get("completed") or info.get("error")
                for info in infos
            )
            with app.RUN_LOCK:
                pending = app.RUN_STATE.get("steer_pending", 0)
                generation = app.RUN_STATE.get("steer_count", 0)
            if not busy and completed and pending == 0:
                time.sleep(quiet_s)
                with app.RUN_LOCK:
                    stable = (
                        app.RUN_STATE.get("steer_pending", 0) == 0
                        and app.RUN_STATE.get("steer_count", 0) == generation
                        and not app.RUN_STATE.get("cancel")
                    )
                if stable:
                    # One final read captures the last text snapshot after idle.
                    messages = (
                        app._server_call(
                            "GET",
                            f"/session/{sid}/message",
                            timeout=5,
                            directory=directory,
                            base_url=base_url,
                        )
                        or []
                    )
                    trails, _ = app.server_messages(messages, baseline, acc, seen)
                    app.publish_snapshot(cfg, live, messages, baseline)
                    if live is not None and trails:
                        live["trail"].extend(trails)
                    break
            time.sleep(poll_s)

        # Abort can race the last model write; retain whatever was persisted.
        try:
            messages = (
                app._server_call(
                    "GET",
                    f"/session/{sid}/message",
                    timeout=5,
                    directory=directory,
                    base_url=base_url,
                )
                or []
            )
            app.server_messages(messages, baseline, acc, seen)
            app.publish_snapshot(cfg, live, messages, baseline)
        except Exception as e:
            app.log(f"server final transcript: {e}")

        with app.RUN_LOCK:
            steer_errors = list(app.RUN_STATE.get("steer_errors") or [])
            app.RUN_STATE["server_sid"] = None
            app.RUN_STATE["server_directory"] = None
            app.RUN_STATE["server_url"] = None
            app.RUN_STATE["server_api"] = None
            app.RUN_STATE["steer_pending"] = 0
            app.RUN_STATE["steer_errors"] = []
            cancelled = bool(app.RUN_STATE.pop("cancel", False)) or cancelled
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
            return sid, None, app.CANCEL_MSG
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
        with app.RUN_LOCK:
            if app.RUN_STATE.get("server_sid") == sid:
                app.RUN_STATE["server_sid"] = None
                app.RUN_STATE["server_directory"] = None
                app.RUN_STATE["server_url"] = None
                app.RUN_STATE["server_api"] = None
                app.RUN_STATE["steer_pending"] = 0
                app.RUN_STATE["steer_errors"] = []
                app.RUN_STATE.pop("cancel", None)


def opencode_v2_supported(app, cfg):
    """Detect the durable `/api/... delivery=steer` contract."""
    try:
        spec = app._server_call("GET", "/doc", timeout=3, base_url=app.server_url(cfg)) or {}
        route = (spec.get("paths") or {}).get("/api/session/{sessionID}/prompt")
        return bool(route and route.get("post"))
    except Exception:
        return False


def _opencode_model(app, cfg, v2=False):
    model = str(app.resolve_model(cfg, cfg.get("runner", "opencode")) or "").strip()
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


def server_messages_v2(app, response, baseline, acc, seen):
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


def run_agent_server_v2(app, cfg, session_id, prompt, live=None):
    """Run OpenCode v2 and admit follow-ups as native safe-boundary steers."""
    sid = session_id
    timed_out = False
    cancelled = False
    base_url = app.server_url(cfg)
    acc = {"parts": {}, "order": [], "thinking": None, "errors": []}
    seen = set()
    try:
        if sid:
            try:
                app._server_call("GET", f"/api/session/{sid}", timeout=5, base_url=base_url)
            except urllib.error.HTTPError as eably:
                if eably.code != 404:
                    raise
                sid = None
            else:
                model_ref = app._opencode_model(cfg, v2=True)
                if model_ref:
                    app._server_call(
                        "POST",
                        f"/api/session/{sid}/model",
                        {"model": model_ref},
                        timeout=5,
                        base_url=base_url,
                    )
        if not sid:
            create_body = {"location": {"directory": cfg["workdir"]}}
            model_ref = app._opencode_model(cfg, v2=True)
            if model_ref:
                create_body["model"] = model_ref
            created = app._server_call(
                "POST", "/api/session", create_body, timeout=10, base_url=base_url
            )
            sid = ((created or {}).get("data") or {}).get("id")
            if not sid:
                return session_id, None, "OpenCode v2: could not create session"
        before = (
            app._server_call(
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
        app._server_call(
            "POST",
            f"/api/session/{sid}/prompt",
            {"prompt": {"text": prompt}, "delivery": "steer"},
            timeout=10,
            base_url=base_url,
        )
        with app.RUN_LOCK:
            app.RUN_STATE["server_run_id"] = app.RUN_STATE.get("server_run_id", 0) + 1
            app.RUN_STATE["server_sid"] = sid
            app.RUN_STATE["server_directory"] = cfg["workdir"]
            app.RUN_STATE["server_url"] = base_url
            app.RUN_STATE["server_api"] = "v2"
            app.RUN_STATE["steer_count"] = 0
            app.RUN_STATE["steer_pending"] = 0
            app.RUN_STATE["steer_errors"] = []
            app.RUN_STATE["cancel"] = False
            if app.RUN_STATE.get("current"):
                app.RUN_STATE["current"]["session"] = sid
        started = app.start_run_clock()
        timeout_reason = None
        last_snapshot = json.dumps(before, sort_keys=True, ensure_ascii=False)
        poll_s = app._server_poll_interval(cfg)
        quiet_s = max(app.SERVER_QUIET_S, poll_s)
        while True:
            with app.RUN_LOCK:
                cancelled = bool(app.RUN_STATE.get("cancel"))
            timeout_reason = app.run_expiry(cfg, started)
            if cancelled or timeout_reason:
                timed_out = bool(timeout_reason) and not cancelled
                try:
                    app._server_call(
                        "POST",
                        f"/api/session/{sid}/interrupt",
                        timeout=5,
                        base_url=base_url,
                    )
                except Exception as eably:
                    app.log(f"OpenCode v2 interrupt: {eably}")
                break
            messages = (
                app._server_call(
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
                app.mark_run_progress()
            trails, infos = app.server_messages_v2(messages, baseline, acc, seen)
            app.publish_snapshot(cfg, live, messages, baseline, v2=True)
            if live is not None:
                if trails:
                    live["trail"].extend(trails)
                    app.edit_status(cfg, live)
                if acc["thinking"]:
                    live["thinking"] = acc["thinking"]
                    app.edit_status(cfg, live)
                if acc["order"]:
                    latest = acc["parts"].get(acc["order"][-1])
                    if latest:
                        live["preview"] = latest
                        app.edit_status(cfg, live)
            active = (
                app._server_call("GET", "/api/session/active", timeout=5, base_url=base_url)
                or {}
            )
            busy = sid in (active.get("data") or {})
            if busy:
                app.mark_run_progress()
            completed = bool(infos) and all(
                (info.get("time") or {}).get("completed") or info.get("error")
                for info in infos
            )
            with app.RUN_LOCK:
                pending = app.RUN_STATE.get("steer_pending", 0)
                generation = app.RUN_STATE.get("steer_count", 0)
            if not busy and completed and pending == 0:
                time.sleep(quiet_s)
                with app.RUN_LOCK:
                    stable = (
                        app.RUN_STATE.get("steer_pending", 0) == 0
                        and app.RUN_STATE.get("steer_count", 0) == generation
                        and not app.RUN_STATE.get("cancel")
                    )
                if stable:
                    final_messages = (
                        app._server_call(
                            "GET",
                            f"/api/session/{sid}/message?order=desc&limit=100",
                            timeout=10,
                            base_url=base_url,
                        )
                        or {}
                    )
                    app.server_messages_v2(final_messages, baseline, acc, seen)
                    app.publish_snapshot(cfg, live, final_messages, baseline, v2=True)
                    break
            time.sleep(poll_s)
        with app.RUN_LOCK:
            steer_errors = list(app.RUN_STATE.get("steer_errors") or [])
            cancelled = bool(app.RUN_STATE.pop("cancel", False)) or cancelled
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
                else (sid, None, app.CANCEL_MSG)
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
        with app.RUN_LOCK:
            if app.RUN_STATE.get("server_sid") == sid:
                app.RUN_STATE["server_sid"] = None
                app.RUN_STATE["server_directory"] = None
                app.RUN_STATE["server_url"] = None
                app.RUN_STATE["server_api"] = None
                app.RUN_STATE["steer_pending"] = 0
                app.RUN_STATE["steer_errors"] = []
                app.RUN_STATE.pop("cancel", None)


def run_agent_server(app, cfg, session_id, prompt, live=None):
    if session_id and not str(session_id).startswith("ses"):
        session_id = None
    if app.opencode_v2_supported(cfg):
        return app.run_agent_server_v2(cfg, session_id, prompt, live)
    return app.run_agent_server_v1(cfg, session_id, prompt, live)
