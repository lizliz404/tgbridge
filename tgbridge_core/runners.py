"""Runner capability registries and portable CLI adapters."""

import json
import os
import re
import shutil
import subprocess
import threading
import time

OPENCODE = os.environ.get(
    "OPENCODE_BIN", os.path.expanduser("~/.local/share/mise/shims/opencode")
)
PI = os.environ.get("PI_BIN", os.path.expanduser("~/.local/share/mise/shims/pi"))
CODEX_YOLO_FLAG = "--dangerously-bypass-approvals-and-sandbox"
DEVIN_PERMISSION_FLAG = "--permission-mode"
AUTO_OPENCODE_GO_MODEL = "auto:opencode-go"
AUTO_PI_PREFIX = "auto:pi"
DEVIN_DEFAULT_MODEL = "swe-2"
_MODEL_DISCOVERY_CACHE = {}
_MODEL_DISCOVERY_LOCK = threading.Lock()


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


def pi_binary():
    """Resolved Pi executable, shared by the CLI and RPC transports.

    Read from the environment on every call so a service can repoint PI_BIN
    without reimporting, and so tests can inject a fixture binary.
    """
    for candidate in (os.environ.get("PI_BIN"), PI):
        if candidate and os.path.exists(candidate):
            return candidate
    found = shutil.which("pi")
    if not found:
        raise RunnerError("runner 'pi' not found; install pi or set PI_BIN")
    return found


RUNNERS = {}
SERVER_RUNNERS = {}

# Supported runner catalog, in user priority order. Probing never executes
# anything; it only proves the binary and adapter are available.
RUNNER_CATALOG = ("pi", "devin", "codex", "opencode")


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


@runner("pi")
def _pi(session_id, prompt, model=None):
    """pi --mode json (resume via stable native --session-id)."""
    cmd = [pi_binary(), "--mode", "json"]
    if session_id:
        cmd += ["--session-id", session_id]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)

    def parse(ev, acc):
        event_type = ev.get("type")
        if event_type == "session" and ev.get("id"):
            acc["sid"] = ev["id"]
            return None

        if event_type == "tool_execution_start":
            return trail_line(
                {
                    "tool": ev.get("toolName", "?"),
                    "state": {"input": ev.get("args") or {}},
                }
            )

        if event_type == "message_update":
            update = ev.get("assistantMessageEvent") or {}
            update_type = update.get("type")
            if update_type == "text_delta" and update.get("delta"):
                current = acc.get("_pi_text", "") + update["delta"]
                acc["_pi_text"] = current
                acc["texts"] = [current]
                acc["thinking"] = None
            elif update_type == "thinking_delta" and update.get("delta"):
                acc["thinking"] = acc.get("_pi_thinking", "") + update["delta"]
                acc["_pi_thinking"] = acc["thinking"]
            elif update_type == "thinking_end":
                acc["thinking"] = update.get("content") or acc.get("thinking")
            return None

        if event_type == "message_end":
            message = ev.get("message") or {}
            if message.get("role") != "assistant":
                return None
            content = message.get("content") or []
            texts = [
                block.get("text", "")
                for block in content
                if block.get("type") == "text" and block.get("text")
            ]
            if texts:
                acc["texts"] = texts
                acc["thinking"] = None
            usage = message.get("usage") or {}
            acc["tokens"] = (acc.get("tokens") or 0) + (usage.get("totalTokens") or 0)
            acc["cost"] = acc.get("cost", 0.0) + (
                (usage.get("cost") or {}).get("total") or 0.0
            )
            stop_reason = message.get("stopReason")
            if stop_reason in ("error", "aborted"):
                acc["runner_error"] = (
                    message.get("errorMessage") or f"Pi stopped with {stop_reason}"
                )
            else:
                acc.pop("runner_error", None)
            acc.pop("_pi_text", None)
            acc.pop("_pi_thinking", None)
        return None

    return cmd, parse


@runner("devin")
def _devin(session_id, prompt, model=None):
    """Devin CLI non-interactive mode; ATIF export supplies the native session id."""
    cmd = [_bin("DEVIN_BIN", "devin")]
    if session_id:
        cmd += ["--resume", session_id]
    cmd += [
        "--model", model or DEVIN_DEFAULT_MODEL,
        "--respect-workspace-trust", "false",
        "--print", "--", prompt,
    ]

    def parse(_event, _acc):
        return None  # Devin --print writes plain response text, not JSONL events.

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
    """Apply bridge-owned permission policy after native command construction."""
    if runner_name == "codex" and cfg.get("codex_yolo", True):
        if CODEX_YOLO_FLAG not in cmd:
            return cmd[:2] + [CODEX_YOLO_FLAG] + cmd[2:]
    if runner_name == "devin" and DEVIN_PERMISSION_FLAG not in cmd:
        permission = cfg.get("devin_permission_mode", "dangerous")
        if permission:
            return cmd[:1] + [DEVIN_PERMISSION_FLAG, str(permission)] + cmd[1:]
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


def _version_tuple(value):
    """Natural numeric version ordering: 1.10 is newer than 1.9."""
    return tuple(int(part) for part in re.split(r"[._-]", value) if part.isdigit())


def select_opencode_go_models(models):
    """Return newest Go Muse Contributor, then newest Go GLM fallback.

    Model names are discovered, never enumerated here. This keeps a future
    Muse Spark 1.4 (or GLM 5.4) eligible without a bridge release. At the
    same GLM version, prefer the full model over its ``-flash`` variant;
    flash is still selected when it is the only newest variant.
    """
    go_models = []
    for raw in models or []:
        name = str(raw).strip()
        if name.lower().startswith("opencode-go/"):
            go_models.append((name, name.lower()))

    muse = []
    for name, lower in go_models:
        match = re.search(r"/muse[-_.]?spark[-_.]?v?(\d+(?:[._-]\d+)*)", lower)
        if match and "contributor" in lower:
            muse.append((_version_tuple(match.group(1)), name))

    glm = []
    for name, lower in go_models:
        match = re.search(r"/glm[-_.]?v?(\d+(?:[._-]\d+)*)", lower)
        if match:
            glm.append(
                (
                    _version_tuple(match.group(1)),
                    not lower.endswith("-flash"),
                    name,
                )
            )

    selected = []
    if muse:
        selected.append(max(muse, key=lambda item: (item[0], item[1]))[1])
    if glm:
        selected.append(max(glm, key=lambda item: (item[0], item[1], item[2]))[2])
    return selected


def discover_opencode_go_models(cfg=None):
    """Ask the installed OpenCode CLI for current Go models, with a short TTL."""
    cfg = cfg or {}
    try:
        cache_s = max(0, int(cfg.get("model_discovery_cache_s", 300)))
        timeout_s = max(1, int(cfg.get("model_discovery_timeout_s", 15)))
    except (TypeError, ValueError):
        cache_s, timeout_s = 300, 15
    now = time.monotonic()
    with _MODEL_DISCOVERY_LOCK:
        cached = _MODEL_DISCOVERY_CACHE.get("opencode-go")
        if cached and now - cached[0] < cache_s:
            return list(cached[1])

    path = OPENCODE if os.path.exists(OPENCODE) else shutil.which("opencode")
    if not path:
        raise RunnerError(
            "cannot discover OpenCode Go models: opencode is not installed"
        )
    try:
        result = subprocess.run(
            [path, "models", "opencode-go"],
            text=True,
            capture_output=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError(f"OpenCode Go model discovery failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()[-300:]
        raise RunnerError(f"OpenCode Go model discovery failed: {detail}")
    selected = select_opencode_go_models(result.stdout.splitlines())
    if not selected:
        raise RunnerError("OpenCode Go has no Muse Spark Contributor or GLM model")
    with _MODEL_DISCOVERY_LOCK:
        _MODEL_DISCOVERY_CACHE["opencode-go"] = (now, tuple(selected))
    return selected


def select_pi_models(rows):
    """Interleave one model per upstream group, in first-seen group order.

    ``rows`` are (provider, model_id) pairs from ``pi --list-models``. The
    first id segment names the upstream inside a gateway provider (e.g.
    magpie's hanzilla/commandcode/devin routing namespaces, or its managed
    auto/ and group/ aliases); a single-segment id keys on the provider
    itself. Taking one model per group per round means one dead upstream
    costs a single attempt instead of blocking the rest of the chain.
    """
    groups = {}
    order = []
    for provider, model_id in rows:
        key = model_id.split("/", 1)[0] if "/" in model_id else provider
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(f"{provider}/{model_id}")
    out = []
    depth = 0
    while True:
        added = False
        for key in order:
            if depth < len(groups[key]):
                out.append(groups[key][depth])
                added = True
        if not added:
            return out
        depth += 1


def discover_pi_model_catalog(cfg=None, refresh=False):
    """Return Pi's current provider/model rows as printed by `--list-models`."""
    cfg = cfg or {}
    try:
        cache_s = max(0, int(cfg.get("model_discovery_cache_s", 300)))
        timeout_s = max(1, int(cfg.get("model_discovery_timeout_s", 15)))
    except (TypeError, ValueError):
        cache_s, timeout_s = 300, 15
    key = ("pi-catalog", "")
    now = time.monotonic()
    with _MODEL_DISCOVERY_LOCK:
        cached = _MODEL_DISCOVERY_CACHE.get(key)
        if not refresh and cached and now - cached[0] < cache_s:
            return list(cached[1])
    try:
        result = subprocess.run(
            [pi_binary(), "--list-models"],
            text=True,
            capture_output=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError(f"Pi model discovery failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()[-300:]
        raise RunnerError(f"Pi model discovery failed: {detail}")
    rows = []
    for line in result.stdout.splitlines():
        fields = re.sub(r"\x1b\[[0-9;]*m", "", line).split()
        if len(fields) >= 4 and fields[0] != "provider":
            rows.append((fields[0], fields[1]))
    if not rows:
        raise RunnerError("Pi reported no models")
    with _MODEL_DISCOVERY_LOCK:
        _MODEL_DISCOVERY_CACHE[key] = (now, tuple(rows))
    return rows


def discover_pi_models(cfg=None, search=None):
    """Ask the installed Pi CLI for its model catalog, with a short TTL.

    ``search`` is passed to ``pi --list-models <search>`` (Pi's own fuzzy
    match), so ``auto:pi:hanzilla`` scopes the failover set without config
    churn when the catalog changes. Discovery reads the live catalog so a
    provider reload never requires a bridge restart.
    """
    cfg = cfg or {}
    if not search:
        return select_pi_models(discover_pi_model_catalog(cfg))
    try:
        cache_s = max(0, int(cfg.get("model_discovery_cache_s", 300)))
        timeout_s = max(1, int(cfg.get("model_discovery_timeout_s", 15)))
    except (TypeError, ValueError):
        cache_s, timeout_s = 300, 15
    key = ("pi", search or "")
    now = time.monotonic()
    with _MODEL_DISCOVERY_LOCK:
        cached = _MODEL_DISCOVERY_CACHE.get(key)
        if cached and now - cached[0] < cache_s:
            return list(cached[1])
    cmd = [pi_binary(), "--list-models"]
    if search:
        cmd.append(search)
    try:
        result = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError(f"Pi model discovery failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()[-300:]
        raise RunnerError(f"Pi model discovery failed: {detail}")
    rows = []
    for line in result.stdout.splitlines():
        fields = re.sub(r"\x1b\[[0-9;]*m", "", line).split()
        # Real table rows carry provider, model, context, max-out, ...
        if len(fields) >= 4 and fields[0] != "provider":
            rows.append((fields[0], fields[1]))
    selected = select_pi_models(rows)
    if not selected:
        raise RunnerError("Pi reported no models")
    with _MODEL_DISCOVERY_LOCK:
        _MODEL_DISCOVERY_CACHE[key] = (now, tuple(selected))
    return selected


def discover_devin_models(cfg=None, refresh=False):
    """Return Devin CLI's live family/variant catalog in machine-readable form."""
    cfg = cfg or {}
    try:
        cache_s = max(0, int(cfg.get("model_discovery_cache_s", 300)))
        timeout_s = max(1, int(cfg.get("model_discovery_timeout_s", 15)))
    except (TypeError, ValueError):
        cache_s, timeout_s = 300, 15
    key = ("devin", "models")
    now = time.monotonic()
    with _MODEL_DISCOVERY_LOCK:
        cached = _MODEL_DISCOVERY_CACHE.get(key)
        if not refresh and cached and now - cached[0] < cache_s:
            return cached[1]
    try:
        result = subprocess.run(
            [_bin("DEVIN_BIN", "devin"), "models", "list", "--format", "json"],
            text=True,
            capture_output=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError(f"Devin model discovery failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()[-300:]
        raise RunnerError(f"Devin model discovery failed: {detail}")
    try:
        catalog = json.loads(result.stdout)
    except ValueError as exc:
        raise RunnerError("Devin returned invalid model-list JSON") from exc
    families = catalog.get("families") if isinstance(catalog, dict) else None
    if not isinstance(families, list) or not families:
        raise RunnerError("Devin reported no model families")
    with _MODEL_DISCOVERY_LOCK:
        _MODEL_DISCOVERY_CACHE[key] = (now, catalog)
    return catalog


def _model_spec(cfg, runner_name, explicit=None):
    if explicit:
        return explicit
    per_runner = cfg.get("runner_models") or {}
    if isinstance(per_runner, dict) and per_runner.get(runner_name):
        return per_runner[runner_name]
    return cfg.get("model") or ""


def _auto_candidates(cfg, spec):
    """Discovery-backed candidates for a known auto:* selector, else None."""
    if spec == AUTO_OPENCODE_GO_MODEL:
        return discover_opencode_go_models(cfg)
    if spec == AUTO_PI_PREFIX or (
        isinstance(spec, str) and spec.startswith(AUTO_PI_PREFIX + ":")
    ):
        return discover_pi_models(
            cfg, spec[len(AUTO_PI_PREFIX):].lstrip(":") or None
        )
    return None


def model_candidates(cfg, model_spec):
    """Expand a spec (or list of specs) into ordered concrete model names.

    A list spec is flattened, so runner_models and runner_fallbacks entries
    may each name several models. A failed discovery keeps a loud
    placeholder in the chain: run_one resolves it strictly and reports the
    discovery error instead of silently using a runner default.
    """
    if isinstance(model_spec, (list, tuple)):
        out = []
        for spec in model_spec:
            out.extend(model_candidates(cfg, spec))
        return out
    if isinstance(model_spec, str) and model_spec.startswith("auto:"):
        try:
            discovered = _auto_candidates(cfg, model_spec)
        except RunnerError:
            discovered = None
        if discovered is not None:
            return discovered
    return [model_spec]


def resolve_auto_model(cfg, spec):
    """Strict auto:* resolution for direct run_one callers; errors are loud."""
    candidates = _auto_candidates(cfg, spec)
    if candidates is None:
        raise RunnerError(f"unknown model selector {spec!r}")
    if not candidates:
        raise RunnerError(f"model selector {spec!r} resolved no models")
    return candidates[0]


def resolve_model(cfg, runner_name, explicit=None):
    """Pick the model for a runner step.

    Precedence: explicit step model > runner_models[runner] > cfg model.
    Empty string means "runner default" (unchanged historical behavior).
    """
    spec = _model_spec(cfg, runner_name, explicit)
    return model_candidates(cfg, spec)[0]


def fallback_chain(cfg):
    """Ordered [(runner, model)] failover steps, excluding the primary.

    Each entry is {"runner": ..., "model": ...}; a missing model falls back
    to resolve_model() so per-runner defaults keep working when entries only
    name a runner. Unknown runners are kept (fail loud at attempt time with
    a clear message) but exact duplicates of the primary are dropped.
    """
    primary_runner = cfg.get("runner", "opencode")
    primary_models = (
        model_candidates(cfg, _model_spec(cfg, primary_runner)) or [""]
    )
    chain = []
    seen = {(primary_runner, model) for model in primary_models}
    raw = cfg.get("runner_fallbacks") or []
    if not isinstance(raw, list):
        return chain
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("runner"):
            continue
        rname = entry["runner"]
        spec = _model_spec(cfg, rname, entry.get("model") or None)
        for model in model_candidates(cfg, spec):
            step = (rname, model)
            if step in seen:
                continue
            seen.add(step)
            chain.append(step)
    return chain


def run_plan(cfg):
    """Ordered [(runner, model)] attempts for one run.

    The primary's own spec expands to all of its candidates, so an auto
    selector or a model list on the primary already rotates same-runner
    models before runner_fallbacks are even reached. Everything is deduped
    and max_fallback_steps bounds the failover breadth (0 = unlimited).
    """
    runner_name = cfg.get("runner", "opencode")
    candidates = model_candidates(cfg, _model_spec(cfg, runner_name)) or [""]
    steps = [(runner_name, model) for model in candidates]
    seen = set(steps)
    for step in fallback_chain(cfg):
        if step not in seen:
            seen.add(step)
            steps.append(step)
    try:
        cap = int(cfg.get("max_fallback_steps", 12))
    except (TypeError, ValueError):
        cap = 12
    if cap > 0:
        steps = steps[: 1 + cap]
    return steps
