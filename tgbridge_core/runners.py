"""Runner capability registries and portable CLI adapters."""

import os
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

