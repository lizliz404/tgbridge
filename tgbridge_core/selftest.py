"""Full regression gate, isolated from the production entry point."""

# Runtime symbols are deliberately injected from the entry module so this
# regression gate exercises the same registries and mutable run state.
# ruff: noqa: F821

from .health import redact_proxy_url
from .runners import CODEX_YOLO_FLAG, fallback_chain, is_quota_error, resolve_model


def _set_app_global(app, name, value):
    setattr(app, name, value)
    globals()[name] = value


def run_selftest(app):
    """Run the full regression suite against the injected bridge module."""
    globals().update(
        {name: value for name, value in vars(app).items() if not name.startswith("__")}
    )
    fails = []

    # worker unpack: both queue shapes
    if unpack_entry((1, 2, "p")) != (1, 2, "p"):
        fails.append("unpack tuple")
    if unpack_entry({"chat_id": 1, "message_id": 2, "prompt": "p"}) != (1, 2, "p"):
        fails.append("unpack dict")
    batch = {
        "kind": "prompt_batch",
        "chat_id": 1,
        "message_id": 2,
        "parts": ["part one", "part two"],
    }
    if unpack_entry(batch) != (1, 2, "part one\n\npart two"):
        fails.append("unpack merged batch")

    # Startup smoke only; exhaustive rendering coverage lives in tests/.
    if split_chunks("a" * 25, limit=10) != ["a" * 10, "a" * 10, "a" * 5]:
        fails.append("chunk smoke")
    if md_to_html("**ok**")[0] != "<b>ok</b>":
        fails.append("render smoke")
    if redact_proxy_url("http://user:secret@127.0.0.1:7897/path") != (
        "http://127.0.0.1:7897"
    ):
        fails.append("proxy credential redaction")
    refused = urllib.error.URLError(ConnectionRefusedError(61, "refused"))
    dead_proxy = {
        "telegram_bypassed": False,
        "local_endpoints": [{"listening": False}],
    }
    if classify_network_error(refused, dead_proxy) != "proxy_refused":
        fails.append("dead proxy classification")

    # Table conversion integrates with the live send path below.

    # send(): HTML refused -> plain-text fallback, never lost
    sent = []

    def fake_api(token, method, **params):
        sent.append(params)
        if params.get("parse_mode") == "HTML" and "<b>" in params["text"]:
            return None  # simulate Telegram rejecting the entity
        return {"ok": True}

    orig_api = api
    _set_app_global(app, "api", fake_api)
    try:
        if not send("t", 1, "hi **there**"):
            fails.append("send fallback ok")
    finally:
        _set_app_global(app, "api", orig_api)
    # fallback resend is clean plain text, never the raw markdown
    if len(sent) != 2 or "parse_mode" in sent[1] or sent[1]["text"] != "hi there":
        fails.append("send fallback shape")

    # table markdown flows through send() as converted bullet HTML
    sent.clear()

    def fake_api2(token, method, **params):
        sent.append(params)
        return {"ok": True}

    _set_app_global(app, "api", fake_api2)
    try:
        if not send("t", 1, "| a | b |\n|---|---|\n| 1 | 2 |"):
            fails.append("send table ok")
    finally:
        _set_app_global(app, "api", orig_api)
    if (
        len(sent) != 1
        or sent[0].get("parse_mode") != "HTML"
        or "<b>1</b>" not in sent[0]["text"]
        or "• b: 2" not in sent[0]["text"]
        or "|" in sent[0]["text"]
    ):
        fails.append("send table shape")

    # every runner builds a cmd and parses a synthetic event
    for name, fn in RUNNERS.items():
        try:
            cmd, parse = fn(None, "hi", None)
            assert cmd and callable(parse), f"{name}: bad cmd/parse"
            cmd2, _ = fn(None, "hi", "test-model")
            i = cmd2.index("--model")
            if cmd2[i + 1] != "test-model":
                fails.append(f"{name} model flag")
        except RunnerError:
            pass  # binary not installed — acceptable, runtime reports it
        except Exception as e:
            fails.append(f"{name} cmd: {e}")

    codex_cmd, _ = RUNNERS["codex"](None, "hi", None)
    yolo_cmd = apply_runner_policy("codex", codex_cmd, {"codex_yolo": True})
    if CODEX_YOLO_FLAG not in yolo_cmd or yolo_cmd.index(CODEX_YOLO_FLAG) != 2:
        fails.append("codex yolo flag")
    if apply_runner_policy("codex", codex_cmd, {}) != codex_cmd:
        fails.append("codex yolo default off")
    if apply_runner_policy("claude", ["claude", "-p"], {"codex_yolo": True}) != [
        "claude",
        "-p",
    ]:
        fails.append("codex yolo isolation")

    ev = {
        "sessionID": "s1",
        "type": "tool_use",
        "part": {"tool": "bash", "state": {"input": {"command": "ls"}}},
    }
    cmd, parse = RUNNERS["opencode"](None, "hi", None)
    acc = {"sid": None, "texts": [], "thinking": None, "cost": 0.0, "tokens": None}
    if parse(ev, acc) is None or acc["sid"] != "s1":
        fails.append("opencode parse")

    cmd, parse = RUNNERS["claude"](None, "hi", None)
    acc = {"sid": None, "texts": [], "thinking": None, "cost": 0.0, "tokens": None}
    parse({"type": "system", "session_id": "s2"}, acc)
    parse(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "tool": "Bash", "input": {"command": "ls"}},
                    {"type": "text", "text": "ok"},
                ]
            },
        },
        acc,
    )
    if acc["sid"] != "s2" or acc["texts"] != ["ok"]:
        fails.append("claude parse")

    # death announcement: one call per chat, one bad chat must not raise
    calls = []

    def fake_post(url, data, timeout):
        calls.append((url, timeout))
        if "-100999" in url:
            raise OSError("boom")

    announce_all(
        {"bot_token": "t", "allowed_chats": [1, -100999, 2]}, "bye", post=fake_post
    )
    if len(calls) != 3:
        fails.append("announce per-chat")
    if any(t > 5 for _, t in calls):
        fails.append("announce timeout>5s")

    # /cancel kill paths against a real Popen
    for name, kill in (
        ("terminate path", lambda p: p.terminate()),
        ("kill_after escalation", lambda p: kill_after(p, 0.2)),
    ):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        kill(p)
        time.sleep(0.6)
        if p.poll() is None:
            fails.append(name)
            p.kill()
        p.wait()

    # run_timeout config plumbing
    for cfg_case, want in (
        ({}, RUN_TIMEOUT_S),
        ({"run_timeout_s": "5"}, 5),
        ({"run_timeout_s": "abc"}, RUN_TIMEOUT_S),
        ({"run_timeout_s": None}, RUN_TIMEOUT_S),
    ):
        if run_timeout(cfg_case) != want:
            fails.append(f"run_timeout {cfg_case}")
    if run_max({"run_timeout_s": 20, "run_max_s": 10}) != 20:
        fails.append("run maximum must not undercut idle timeout")
    if input_debounce({"input_debounce_s": 0}) != 0.2:
        fails.append("input debounce minimum")
    if input_debounce({"input_debounce_s": "bad"}) != INPUT_DEBOUNCE_S:
        fails.append("input debounce fallback")

    # botcmd: command routing incl. new commands and @BotName suffix
    if (
        botcmd("/cancel@Bot") != ("/cancel", "")
        or botcmd("/at@B 5m hi") != ("/at", "5m hi")
        or botcmd("plain words") != (None, "")
    ):
        fails.append("botcmd")

    # authorization: chats are always explicit; DMs always require a user ID.
    # Group-wide trust is opt-in and applies only inside an allowed group.
    auth_cfg = {
        "bot_token": "t",
        "allowed_chats": [11, -10022],
        "allowed_user_ids": [11],
        "allow_all_users_in_allowed_groups": True,
    }
    if not is_authorized(auth_cfg, 11, "private", 11):
        fails.append("auth allowed dm")
    if is_authorized(auth_cfg, 12, "private", 12):
        fails.append("auth unknown dm")
    if not is_authorized(auth_cfg, -10022, "supergroup", 99):
        fails.append("auth allowed group member")
    if is_authorized(auth_cfg, -10023, "supergroup", 99):
        fails.append("auth unknown group")
    if is_authorized(auth_cfg, -10022, "supergroup", None):
        fails.append("auth anonymous group sender")
    if is_authorized(auth_cfg, -10022, "channel", 99):
        fails.append("auth channel")
    strict_cfg = {"allowed_chats": [-10022], "allowed_user_ids": [11]}
    if is_authorized(strict_cfg, -10022, "supergroup", 99):
        fails.append("auth strict group")

    # Per-sender operator instructions apply only to the configured human ID.
    sender_cfg = {"sender_instructions": {"11": "use the cheap lane"}}
    tagged = apply_sender_instructions(sender_cfg, 11, "delegate this")
    if "use the cheap lane" not in tagged or not tagged.endswith("delegate this"):
        fails.append("sender instructions configured")
    if apply_sender_instructions(sender_cfg, 12, "plain") != "plain":
        fails.append("sender instructions isolation")
    if apply_sender_instructions({"sender_instructions": []}, 11, "plain") != "plain":
        fails.append("sender instructions invalid config")

    # server-mode event folding: tool trail dedupe, text snapshot replace
    acc = {"parts": {}, "order": [], "thinking": None}
    seen = set()
    t1 = server_event(
        {
            "type": "message.part.updated",
            "data": {
                "part": {
                    "id": "p1",
                    "type": "tool",
                    "tool": "bash",
                    "state": {"input": {"command": "ls /x"}},
                }
            },
        },
        acc,
        seen,
    )
    if t1 != "🔧 bash: ls /x" or seen != {"p1"}:
        fails.append("server tool trail")
    t2 = server_event(
        {
            "type": "message.part.updated",
            "data": {
                "part": {
                    "id": "p1",
                    "type": "tool",
                    "tool": "bash",
                    "state": {"input": {"command": "ls /y"}},
                }
            },
        },
        acc,
        seen,
    )
    if t2 is not None:
        fails.append("server tool dedupe")
    server_event(
        {
            "type": "message.part.updated",
            "data": {"part": {"id": "p2", "type": "text", "text": "par"}},
        },
        acc,
        seen,
    )
    server_event(
        {
            "type": "message.part.updated",
            "data": {"part": {"id": "p2", "type": "text", "text": "partial answer"}},
        },
        acc,
        seen,
    )
    if acc["parts"].get("p2") != "partial answer" or acc["order"] != ["p2"]:
        fails.append("server text snapshot")
    server_event(
        {
            "type": "message.part.updated",
            "data": {"part": {"id": "p3", "type": "reasoning", "text": "hmm"}},
        },
        acc,
        seen,
    )
    if acc["thinking"] != "hmm":
        fails.append("server reasoning")
    if server_event({"type": "session.updated", "data": {}}, acc, seen) is not None:
        fails.append("server ignore other events")

    # Poll snapshots exclude prior turns, preserve new message/part order, and
    # surface assistant completion metadata for the quiescence check.
    poll_acc = {"parts": {}, "order": [], "thinking": None, "errors": []}
    poll_seen = set()
    trails, infos = server_messages(
        [
            {
                "info": {"id": "old", "role": "assistant", "time": {}},
                "parts": [{"id": "oldp", "type": "text", "text": "ignore"}],
            },
            {
                "info": {
                    "id": "new",
                    "role": "assistant",
                    "time": {"completed": 123},
                },
                "parts": [
                    {
                        "id": "tool",
                        "type": "tool",
                        "tool": "bash",
                        "state": {"input": {"command": "pwd"}},
                    },
                    {"id": "text", "type": "text", "text": "new answer"},
                ],
            },
        ],
        {"old"},
        poll_acc,
        poll_seen,
    )
    if (
        trails != ["🔧 bash: pwd"]
        or [i.get("id") for i in infos] != ["new"]
        or poll_acc["order"] != ["text"]
        or poll_acc["parts"].get("text") != "new answer"
    ):
        fails.append("server message polling")
    v2_acc = {"parts": {}, "order": [], "thinking": None, "errors": []}
    v2_trails, v2_infos = server_messages_v2(
        {
            "data": [
                {"id": "old", "type": "assistant", "content": []},
                {
                    "id": "new-v2",
                    "type": "assistant",
                    "time": {"completed": 123},
                    "content": [
                        {
                            "id": "tool-v2",
                            "type": "tool",
                            "name": "bash",
                            "state": {"input": {"command": "pwd"}},
                        },
                        {"id": "text-v2", "type": "text", "text": "v2 answer"},
                    ],
                },
            ]
        },
        {"old"},
        v2_acc,
        set(),
    )
    if (
        v2_trails != ["🔧 bash: pwd"]
        or [i.get("id") for i in v2_infos] != ["new-v2"]
        or v2_acc["parts"].get("text-v2") != "v2 answer"
    ):
        fails.append("v2 server message polling")
    if _opencode_model({"model": "opencode/muse"}, v2=True) != {
        "providerID": "opencode",
        "id": "muse",
    }:
        fails.append("v2 model config")
    # Failover gate: quota classification, per-runner default model, chain.
    if not is_quota_error("weekly limit reached, resets Monday"):
        fails.append("quota classification")
    if is_quota_error("opencode failed rc=1\nconnection refused"):
        fails.append("non-quota must stay loud")
    if (
        resolve_model(
            {"model": "a/b", "runner_models": {"opencode": "c/d"}}, "opencode"
        )
        != "c/d"
    ):
        fails.append("per-runner default model")
    if fallback_chain(
        {
            "runner": "codex",
            "runner_models": {"opencode": "c/d"},
            "runner_fallbacks": [{"runner": "codex"}, {"runner": "opencode"}],
        }
    ) != [("opencode", "c/d")]:
        fails.append("fallback chain dedupe")
    # Failover orchestration: quota on primary -> fresh fallback session + header.
    fb_calls = []
    fb_audits = []
    orig_run_agent = run_agent
    orig_audit = audit

    def fake_run_agent(cfg, session_id, prompt, live=None):
        fb_calls.append((cfg.get("runner"), cfg.get("model"), session_id))
        if len(fb_calls) == 1:
            return session_id, None, "codex failed rc=1\nweekly limit reached"
        return "new-sid", "fallback answer", None

    _set_app_global(app, "run_agent", fake_run_agent)
    _set_app_global(app, "audit", lambda event, **kw: fb_audits.append(event))
    try:
        fb_sid, fb_answer, fb_err = run_with_fallbacks(
            {
                "runner": "codex",
                "workdir": "/tmp",
                "runner_models": {"opencode": "opencode-go/muse-spark-1.3-contributor"},
                "runner_fallbacks": [{"runner": "opencode"}],
            },
            "old-sid",
            "hi",
            None,
        )
    finally:
        _set_app_global(app, "run_agent", orig_run_agent)
        _set_app_global(app, "audit", orig_audit)
    if (
        fb_err is not None
        or fb_sid != "new-sid"
        or "answered via opencode" not in (fb_answer or "")
    ):
        fails.append("quota failover")
    if fb_calls != [
        ("codex", "", "old-sid"),
        ("opencode", "opencode-go/muse-spark-1.3-contributor", None),
    ]:
        fails.append("failover fresh session + model")
    if "run_fallback" not in fb_audits:
        fails.append("failover audit")
    # Any failure (not just quota) fails over; only cancel stops the chain.
    fb_calls.clear()

    def dead_run_agent(cfg, session_id, prompt, live=None):
        fb_calls.append((cfg.get("runner"), cfg.get("model"), session_id))
        return session_id, None, "opencode failed rc=1\nconnection refused"

    _set_app_global(app, "run_agent", dead_run_agent)
    try:
        _, _, dead_err = run_with_fallbacks(
            {
                "runner": "codex",
                "runner_fallbacks": [{"runner": "opencode", "model": "c/d"}],
            },
            "s",
            "hi",
            None,
        )
    finally:
        _set_app_global(app, "run_agent", orig_run_agent)
    if "all runners exhausted (codex -> opencode)" not in (dead_err or ""):
        fails.append("any-failure fails over")
    if [c[:2] for c in fb_calls] != [("codex", ""), ("opencode", "c/d")]:
        fails.append("failover walks the whole chain")
    _set_app_global(
        app,
        "run_agent",
        lambda cfg, session_id, prompt, live=None: (session_id, None, CANCEL_MSG),
    )
    try:
        _, _, cancel_err = run_with_fallbacks(
            {"runner": "codex", "runner_fallbacks": [{"runner": "opencode"}]},
            "s",
            "hi",
            None,
        )
    finally:
        _set_app_global(app, "run_agent", orig_run_agent)
    if cancel_err != CANCEL_MSG:
        fails.append("cancel never fails over")
    if _server_poll_interval({"server_poll_s": 0}) != 0.1:
        fails.append("server poll minimum")
    if _server_poll_interval({"server_poll_s": "bad"}) != SERVER_POLL_S:
        fails.append("server poll fallback")
    if (
        runner_mode({}) != "cli"
        or runner_mode({"server_runner": True}) != "server"
        or runner_mode({"server_runner": True, "runner_mode": "cli"}) != "cli"
        or "opencode" not in SERVER_RUNNERS
    ):
        fails.append("runner transport config")

    # steering route: only a same-chat message during a server-mode run steers
    with RUN_LOCK:
        RUN_STATE["busy"] = True
        RUN_STATE["server_sid"] = "s9"
        RUN_STATE["server_directory"] = "/tmp/project"
        RUN_STATE["server_url"] = "http://127.0.0.1:4096"
        RUN_STATE["server_api"] = "v2"
        RUN_STATE["server_run_id"] = 4
        RUN_STATE["steer_pending"] = 0
        RUN_STATE["current"] = {"chat": 7}
    if not should_steer(7) or should_steer(8) or should_steer(11):
        fails.append("steer route")
    if _begin_steer(7) != {
        "transport": "opencode_v2_steer",
        "sid": "s9",
        "run_id": 4,
        "base_url": "http://127.0.0.1:4096",
    }:
        fails.append("steer reservation")
    with RUN_LOCK:
        if RUN_STATE["steer_pending"] != 1:
            fails.append("steer pending")
        RUN_STATE["busy"] = False
        RUN_STATE["server_sid"] = None
        RUN_STATE["server_directory"] = None
        RUN_STATE["server_url"] = None
        RUN_STATE["server_api"] = None
        RUN_STATE["steer_pending"] = 0
        RUN_STATE["current"] = None
    if should_steer(7):
        fails.append("steer route idle")
    with RUN_LOCK:
        RUN_STATE["busy"] = True
        RUN_STATE["server_sid"] = "old-server"
        RUN_STATE["server_api"] = "v1"
        RUN_STATE["current"] = {"chat": 7}
    if should_steer(7) or _begin_steer(7):
        fails.append("legacy server must queue instead of unsafe steering")
    with RUN_LOCK:
        RUN_STATE["busy"] = False
        RUN_STATE["server_sid"] = None
        RUN_STATE["server_api"] = None
        RUN_STATE["current"] = None
    # Codex steering is bound to the exact active app-server turn.
    with RUN_LOCK:
        RUN_STATE["busy"] = True
        RUN_STATE["codex_thread_id"] = "codex-thread"
        RUN_STATE["codex_turn_id"] = "codex-turn"
        RUN_STATE["codex_run_id"] = 8
        RUN_STATE["steer_pending"] = 0
        RUN_STATE["current"] = {"chat": 7, "runner": "codex"}
    codex_target = _begin_steer(7)
    if codex_target != {
        "transport": "codex_turn_steer",
        "sid": "codex-thread",
        "turn_id": "codex-turn",
        "run_id": 8,
    }:
        fails.append("codex turn/steer reservation")
    with RUN_LOCK:
        RUN_STATE["busy"] = False
        RUN_STATE["codex_thread_id"] = None
        RUN_STATE["codex_turn_id"] = None
        RUN_STATE["steer_pending"] = 0
        RUN_STATE["current"] = None
    # An allowed group member can prompt. Human group messages are captured by
    # default for ambient context, but the bot still speaks only when mentioned.
    auth_state = {
        "bot_username": "Bot",
        "sessions": {"-10022": "keep-me"},
        "hints": {"-10022": time.time()},
    }

    def auth_api(token, method, **params):
        return {"ok": True, "result": {"message_id": 1}}

    auth_writes = []

    def auth_save(path, data):
        auth_writes.append(path)

    orig_save_json = save_json
    _set_app_global(app, "api", auth_api)
    _set_app_global(app, "save_json", auth_save)
    try:
        handle_update(
            auth_cfg,
            auth_state,
            {
                "message": {
                    "chat": {"id": -10022, "type": "supergroup"},
                    "from": {"id": 99, "first_name": "member"},
                    "message_id": 1,
                    "text": "background conversation",
                }
            },
            state_path=None,
        )
        if len((auth_state.get("context") or {}).get("-10022", [])) != 1:
            fails.append("group context default")
        private_state = {"bot_username": "Bot"}
        handle_update(
            {**auth_cfg, "capture_group_context": False},
            private_state,
            {
                "message": {
                    "chat": {"id": -10022, "type": "supergroup"},
                    "from": {"id": 99, "first_name": "member"},
                    "message_id": 2,
                    "text": "explicitly ignored context",
                }
            },
            state_path=None,
        )
        if private_state.get("context"):
            fails.append("group context opt-out")
        if auth_writes:
            fails.append("selftest state isolation")
    finally:
        _set_app_global(app, "api", orig_api)
        _set_app_global(app, "save_json", orig_save_json)

    # Runtime metadata may contain prompts/session IDs, so modes are repaired
    # even when a permissive umask or an older version created the files.
    import stat as _stat
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as private_dir:
        os.chmod(private_dir, 0o755)
        private_state = os.path.join(private_dir, "state.json")
        save_json(private_state, {"sessions": {}})
        if _stat.S_IMODE(os.stat(private_dir).st_mode) != 0o700:
            fails.append("private dir mode")
        if _stat.S_IMODE(os.stat(private_state).st_mode) != 0o600:
            fails.append("private state mode")

    if fails:
        for f in fails:
            print(f"SELFTEST FAIL: {f}")
        sys.exit(1)
    print(
        "selftest OK:",
        ", ".join(sorted(RUNNERS)),
        "runners + unpack + render + chunker + announce + kill + timeout + botcmd + auth + steer",
    )
