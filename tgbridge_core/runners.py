"""Runner capability registries and portable CLI adapters."""

import os
import re
import shutil
import time

OPENCODE = os.environ.get(
    "OPENCODE_BIN", os.path.expanduser("~/.local/share/mise/shims/opencode")
)
CODEX_YOLO_FLAG = "--dangerously-bypass-approvals-and-sandbox"


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


def _bin(env_key, name):
    p = os.environ.get(env_key) or shutil.which(name)
    if not p:
        raise RunnerError(
            f"runner {name!r} not found on PATH; install it or set {env_key}"
        )
    return p


RUNNERS = {}
SERVER_RUNNERS = {}

# Supported runner catalog, in probe/display order. A new agent CLI joins
# this list only together with a verified adapter above — never as a bare
# name. Probing never executes anything; it only proves the binary exists.
RUNNER_CATALOG = ("opencode", "claude", "codex")


def probe_runners():
    """Black-box availability probe: {name: {available, detail}}.

    Construction-only (no subprocess, no network): an adapter that builds
    its command proves its binary is present; RunnerError names what's
    missing. The bridge can never inspect a runner's internals — session,
    quota and network state stay behind the CLI boundary.
    """
    report = {}
    for name in RUNNER_CATALOG:
        fn = RUNNERS.get(name)
        if fn is None:
            report[name] = {"available": False, "detail": "no adapter registered"}
            continue
        try:
            fn(None, "probe", None)
            report[name] = {"available": True, "detail": "binary present"}
        except RunnerError as e:
            report[name] = {"available": False, "detail": str(e)[:160]}
        except Exception as e:  # never let a probe kill startup or /runners
            report[name] = {"available": False, "detail": f"probe error: {e}"[:160]}
    return report


def runner(name):
    def deco(fn):
        RUNNERS[name] = fn
        return fn

    return deco


class RunnerError(Exception):
    pass


@runner("opencode")
def _opencode(session_id, prompt, model=None):
    """opencode run --format json. Events: sessionID / tool_use / text / reasoning."""
    p = OPENCODE if os.path.exists(OPENCODE) else shutil.which("opencode")
    if not p:
        raise RunnerError(
            "runner 'opencode' not found; install opencode or set OPENCODE_BIN"
        )
    cmd = [p, "run", "--format", "json"]
    if session_id:
        cmd += ["--session", session_id]
    else:
        cmd += ["--title", time.strftime("tg %Y%m%d-%H%M")]
    if model:
        cmd += ["--model", model]  # flag verified live against opencode CLI
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
def _claude(session_id, prompt, model=None):
    """claude -p --output-format stream-json (resume via --resume)."""
    cmd = [
        _bin("CLAUDE_BIN", "claude"),
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if session_id:
        cmd += ["--resume", session_id]
    if model:
        cmd += ["--model", model]

    def parse(ev, acc):
        t = ev.get("type")
        if t == "system" and ev.get("session_id"):
            acc["sid"] = ev["session_id"]
        if t == "assistant":
            trail = None
            for blk in (ev.get("message") or {}).get("content") or []:
                bt = blk.get("type")
                if bt == "tool_use":
                    inp = blk.get("input") or {}
                    s = next((v for v in inp.values() if isinstance(v, str)), "")
                    trail = f"🔧 {blk.get('tool', '?')}: " + s.replace("\n", " ")[:60]
                elif bt == "text" and blk.get("text"):
                    acc["texts"].append(blk["text"])
                    acc["thinking"] = None
                elif bt == "thinking" and blk.get("thinking"):
                    acc["thinking"] = blk["thinking"]
            return trail
        return None

    return cmd, parse


@runner("codex")
def _codex(session_id, prompt, model=None):
    """codex exec --json (resume via `codex exec resume <id>`). Best-effort."""
    cmd = [_bin("CODEX_BIN", "codex"), "exec", "--json"]
    if session_id:
        cmd += ["resume", session_id]
    if model:
        cmd += ["--model", model]
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


def apply_runner_policy(runner_name, cmd, cfg):
    """Apply explicit bridge-owned runner policy after command construction."""
    if runner_name == "codex" and cfg.get("codex_yolo", False):
        return cmd[:2] + [CODEX_YOLO_FLAG] + cmd[2:]
    return cmd


# Built-in quota/limit markers (case-insensitive regexes). A runner failure
# whose error text matches any of these is treated as "this runner is
# temporarily unusable (weekly limit, quota, billing...)" and the bridge may
# fail over to the next entry of `runner_fallbacks` instead of giving up.
# Users can extend (never replace) this list via `quota_markers` in config.
QUOTA_MARKERS = (
    r"weekly.?limit",
    r"usage.?limit",
    r"limit.?(reached|exceeded|hit)",
    r"(exceeded|hit|reached).?limit",
    r"\bquota\b",
    r"over.?quota",
    r"insufficient.?(quota|credit|balance|funds)",
    r"rate.?limit",
    r"too many requests",
    r"\b429\b",
    r"resource.?exhausted",
    r"credits?.?(exhausted|depleted|expired|insufficient)",
    r"trial.?(ended|expired)",
    r"billing",
    r"payment.?required",
    r"plan.?(limit|quota)",
    r"usage.?(cap|allowance)",
)


def is_quota_error(text, extra_markers=None):
    """True when an error string looks like a quota/limit/billing refusal."""
    return classify_run_error(text, extra_markers) == "quota"


# Markers for "the runner exists but cannot serve right now": missing
# binary, dead local transport, or network path failures. Deliberately NOT
# matching the bridge's own timeout messages ("hit the idle timeout ..."),
# which are per-run leases, not runner outages.
UNAVAILABLE_MARKERS = (
    r"not found on PATH",
    r"no server transport",
    r"server transport unavailable",
    r"connection (refused|reset|aborted|timed out)",
    r"network (is )?unreachable",
    r"\bENOTFOUND\b",
    r"\bEAI_AGAIN\b",
    r"\bECONNREFUSED\b",
    r"\bECONNRESET\b",
    r"TLS|SSL[^a-zA-Z]|certificate|handshake",
    r"\b50[234]\b",
    r"service unavailable",
    r"temporarily unavailable",
    r"could not connect|can't connect|failed to connect",
    r"getaddrinfo failed",
    r"socket (hang up|timeout)",
)


def classify_run_error(text, extra_markers=None):
    """Tag a failure: quota | unavailable | other. Label only.

    The bridge fails over on ANY failure (except user cancel); this tag
    exists so the audit trail and the 🔀 note say what kind of breakage
    was seen, not to decide whether to switch.
    """
    if not text:
        return "other"
    patterns = list(QUOTA_MARKERS) + [m for m in (extra_markers or []) if m]
    if any(re.search(p, text, re.IGNORECASE) for p in patterns):
        return "quota"
    if any(re.search(p, text, re.IGNORECASE) for p in UNAVAILABLE_MARKERS):
        return "unavailable"
    return "other"


def resolve_model(cfg, runner_name, explicit=None):
    """Pick the model for a runner step.

    Precedence: explicit step model > runner_models[runner] > cfg model.
    Empty string means "runner default" (unchanged historical behavior).
    """
    if explicit:
        return explicit
    per_runner = cfg.get("runner_models") or {}
    if isinstance(per_runner, dict) and per_runner.get(runner_name):
        return per_runner[runner_name]
    return cfg.get("model") or ""


def fallback_chain(cfg):
    """Ordered [(runner, model)] failover steps, excluding the primary.

    Each entry is {"runner": ..., "model": ...}; a missing model falls back
    to resolve_model() so per-runner defaults keep working when entries only
    name a runner. Unknown runners are kept (fail loud at attempt time with
    a clear message) but exact duplicates of the primary are dropped.
    """
    primary = (
        cfg.get("runner", "opencode"),
        resolve_model(cfg, cfg.get("runner", "opencode")),
    )
    chain = []
    seen = {primary}
    raw = cfg.get("runner_fallbacks") or []
    if not isinstance(raw, list):
        return chain
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("runner"):
            continue
        rname = entry["runner"]
        step = (rname, resolve_model(cfg, rname, entry.get("model") or None))
        if step in seen:
            continue
        seen.add(step)
        chain.append(step)
    return chain
