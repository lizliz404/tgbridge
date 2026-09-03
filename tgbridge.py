#!/usr/bin/env python3
"""tgbridge - minimal Telegram bridge for a local CLI agent.

Bot API long-poll -> gate (chat allowlist + sender policy + group trigger)
-> agent run (per-chat session) -> reply to source chat.

Architecture: the poll loop never blocks. Slash commands are answered inline;
prompts are enqueued and consumed serially by a worker thread. Agent stdout
is streamed live via Popen, so the status message shows a real-time tool
trail and answer preview (claudegram/xhyu/OpenClaw pattern).

Extra surfaces: `tgbridge.py --send <chat_id> <text>` lets the agent itself
post to allowlisted chats (Telegram-Bridge-MCP idea, no MCP protocol).

Outbound rendering borrows from Hermes' own gateway (hermes-agent sources):
UTF-16-aware chunk limits, inline-code split avoidance, GFM table
conversion, placeholder-stashed HTML conversion, a clean-markup plain-text
fallback, fence-language carry across chunks, one-element blockquote
merging (incl. expandable), and native bullet markers.
"""

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG_DIR = os.path.expanduser("~/.config/tgbridge")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")
AUDIT_PATH = os.path.join(CONFIG_DIR, "audit.jsonl")
OPENCODE = os.environ.get(
    "OPENCODE_BIN", os.path.expanduser("~/.local/share/mise/shims/opencode")
)
RUN_TIMEOUT_S = 900
CHUNK = 3900
CANCEL_MSG = "🛑 cancelled by user"

PROMPT_Q = queue.Queue()
STATE_LOCK = threading.Lock()
AUDIT_LOCK = threading.Lock()
RUN_STATE: dict = {"busy": False, "current": None, "proc": None, "cancel": False}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def ensure_private_dir(path):
    """Create private runtime storage and repair permissive existing modes."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def ensure_private_storage():
    ensure_private_dir(CONFIG_DIR)
    for path in (
        CONFIG_PATH,
        STATE_PATH,
        AUDIT_PATH,
        os.path.join(CONFIG_DIR, "tgbridge.log"),
    ):
        if os.path.isfile(path):
            os.chmod(path, 0o600)


def audit(event, **fields):
    """Append-only JSONL trail of bridge actions (claude-code-telegram pattern)."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
    try:
        ensure_private_dir(CONFIG_DIR)
        with AUDIT_LOCK:
            fd = os.open(AUDIT_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.chmod(AUDIT_PATH, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    ensure_private_dir(os.path.dirname(path) or ".")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def api(token, method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}", data=data
    )
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=70) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 409:
                log(
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
            log(f"api {method} http {e.code}: {body[:150]}")
            return None
        except Exception as e:
            log(f"api {method} error: {e}")
            return None
    return None


def react(cfg, chat_id, message_id, emoji):
    if not cfg.get("reactions", True):
        return
    api(
        cfg["bot_token"],
        "setMessageReaction",
        chat_id=chat_id,
        message_id=message_id,
        reaction=json.dumps([{"type": "emoji", "emoji": emoji}]),
    )


def utf16_len(s):
    """Telegram's 4096 cap counts UTF-16 code units, not Python codepoints
    (astral emoji cost 2). Ported from hermes-agent gateway/platforms/base.py."""
    return len(s.encode("utf-16-le")) // 2


def _prefix_within_utf16_limit(s, limit):
    """Longest prefix whose UTF-16 length <= limit; the codepoint-slice never
    lands mid-character (hermes-agent gateway/platforms/base.py)."""
    if utf16_len(s) <= limit:
        return s
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if utf16_len(s[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return s[:lo]


def _wrap_markdown_tables(text):
    """Rewrite GFM pipe tables into bold-heading + bullet groups.

    Telegram HTML has no table entity, so raw pipe rows render as escape
    noise. Ported from hermes-agent gateway/platforms/helpers.py
    convert_table_to_bullets: tables inside fenced code blocks are left alone.
    """
    if "|" not in text or "-" not in text:
        return text

    def _split_row(line):
        s = line.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        return [c.strip() for c in s.split("|")]

    def _render_block(block):
        headers = _split_row(block[0])
        if len(headers) < 2:
            return "\n".join(block)
        groups = []
        for index, row in enumerate(block[2:], start=1):
            cells = _split_row(row)
            while len(cells) < len(headers):
                cells.append("")
            cells = cells[: len(headers)]
            raw_heading = next((c for c in cells if c), f"Row {index}")
            bullets = [f"• {h}: {v}" for h, v in zip(headers, cells) if v != raw_heading]
            # headings flatten inner bold (hermes _convert_header): a bold
            # cell would render <b><b>…</b></b>, same-type nesting Telegram
            # refuses — which would demote the whole chunk to plain text
            heading = re.sub(r"\*\*(.+?)\*\*", r"\1", raw_heading)
            groups.append("\n".join([f"**{heading}**", *bullets]))
        return "\n\n".join(groups)

    sep = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$")
    out, in_fence, lines, i = [], False, text.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if (
            not in_fence
            and "|" in line
            and i + 1 < len(lines)
            and sep.match(lines[i + 1])
        ):
            block = [line, lines[i + 1]]
            j = i + 2
            while j < len(lines) and lines[j].strip() and "|" in lines[j]:
                block.append(lines[j])
                j += 1
            out.append(_render_block(block))
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def inline_html(line):
    """One markdown line -> Telegram HTML. Line-local by design: no entity
    ever spans lines, so per-chunk conversion stays balanced even when a
    chunk boundary lands mid-paragraph. Converted code spans and links are
    stashed hermes-style (format_message placeholders) so later substitutions
    never touch their contents."""
    s = esc(line)
    stash = []

    def _keep(m):
        key = f"\x00tg{len(stash)}\x00"
        stash.append((key, m.group(0)))
        return key

    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"<code>[^<]*</code>", _keep, s)
    s = MD_LINK.sub(r'<a href="\2">\1</a>', s)
    s = re.sub(r"<a href=[^>]*>[^<]*</a>", _keep, s)
    # headers flatten inner bold (hermes _convert_header strips redundant
    # bold markers — <b><b>…</b></b> is same-type nesting Telegram refuses,
    # which would demote the whole chunk to plain text)
    s = re.sub(
        r"^#{1,6}\s+(.*)",
        lambda m: "<b>" + re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(1)) + "</b>",
        s,
    )
    # markdown list markers -> Telegram's native bullet glyph (hermes tables
    # and lists render bullets as "• ", not literal -/*/+)
    s = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", s)
    # ***x*** must run before ** or it is eaten as bold + stray asterisks
    s = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    # emphasis delimiters never flank whitespace and never hug word chars
    # (hermes format_message guards bullets via [^*\n]+; the space-flank rule
    # also kills "a * b * c" arithmetic and "* item *" false positives)
    s = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", s)
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
    s = re.sub(r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", s)
    for key, val in stash:
        s = s.replace(key, val)
    return s


def _quote_body(line):
    """Classify one blockquote line -> (is_quote, expandable, content).

    hermes _convert_blockquote: '> text' is a plain quote, '**> text' opens
    an expandable quote closed by a trailing '||'."""
    ls = line.lstrip()
    if ls.startswith("**> "):
        return True, True, ls[4:].rstrip()
    if ls.startswith(">") and (len(ls) == 1 or ls[1] in " >"):
        return True, False, ls.lstrip("> ").rstrip()
    return False, False, ""


def md_to_html(md, in_pre=False, pre_lang=""):
    """Markdown -> Telegram HTML. Returns (html, in_pre_after, pre_lang_after)
    so a fenced block split across chunks stays a valid <pre> in every chunk,
    carrying the original language tag (hermes truncate_message carry_lang)."""
    out = []
    if in_pre:
        # continuation chunk reopens the carried fence with its language tag
        out.append(
            f'<pre><code class="language-{esc(pre_lang)}">'
            if pre_lang
            else "<pre>"
        )
    lines = md.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            tag = line.lstrip()[3:].strip()
            lang = tag.split()[0] if tag else ""
            if in_pre:
                out.append("</code></pre>" if pre_lang else "</pre>")
            else:
                # language tag -> Telegram's <code class="language-x">,
                # rendered with syntax highlighting in official clients
                out.append(
                    f'<pre><code class="language-{esc(lang)}">'
                    if lang
                    else "<pre>"
                )
            in_pre = not in_pre
            pre_lang = lang
            i += 1
            continue
        if in_pre:
            out.append(esc(line))
            i += 1
            continue
        is_quote, expandable, _ = _quote_body(line)
        if is_quote:
            # merge consecutive quote lines into ONE blockquote (hermes
            # treats quote blocks as blocks, not per-line entities — N
            # stacked boxes is visual noise); **> makes it expandable
            j, parts = i, []
            while j < len(lines):
                q_is, q_exp, q_body = _quote_body(lines[j])
                if not q_is:
                    break
                expandable = expandable or q_exp
                parts.append(q_body)
                j += 1
            if expandable and parts and parts[-1].endswith("||"):
                # hermes: trailing || is the expandable-quote end marker
                parts[-1] = parts[-1][:-2].rstrip()
            inner = "\n".join(inline_html(p) for p in parts)
            open_tag = "<blockquote expandable>" if expandable else "<blockquote>"
            out.append(open_tag + inner + "</blockquote>")
            i = j
            continue
        out.append(inline_html(line))
        i += 1
    if in_pre:
        out.append("</code></pre>" if pre_lang else "</pre>")
    return "\n".join(out), in_pre, pre_lang


def _strip_html_markup(md):
    """Markdown -> clean plain text for the fallback send (hermes-agent's
    _strip_mdv2 contract: the resend must never show raw **/```/[]()
    syntax that the failed formatted attempt would have consumed)."""
    s = re.sub(r"```[^\n]*\n?", "", md)
    s = re.sub(r"``([^`]+)``", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", s)
    s = re.sub(r"~~(.+?)~~", r"\1", s)
    s = re.sub(r"\|\|(.+?)\|\|", r"\1", s)
    s = MD_LINK.sub(r"\1 (\2)", s)
    s = re.sub(r"^#{1,6}\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^> ?", "", s, flags=re.MULTILINE)
    return s.rstrip()


def _balanced(html):
    """True when every <tag> in the chunk is closed and nesting matches —
    an unbalanced chunk must never be offered to Telegram as HTML."""
    stack = []
    for m in re.finditer(r"<(/?)([a-z][a-z0-9-]*)(?:\s[^>]*)?>", html):
        if m.group(1):
            if not stack or stack[-1] != m.group(2):
                return False
            stack.pop()
        else:
            stack.append(m.group(2))
    return not stack


def split_chunks(text, limit=CHUNK):
    if utf16_len(text) <= limit:
        return [text]
    chunks, rest = [], text
    while rest:
        if utf16_len(rest) <= limit:
            chunks.append(rest)
            break
        region = _prefix_within_utf16_limit(rest, limit)
        cut = region.rfind("\n\n")
        if cut < 1:
            cut = region.rfind("\n")
        if cut < 1:
            cut = region.rfind(" ")
        if cut < 1:
            cut = len(region)
        # Never cut inside an inline code span: an odd number of unescaped
        # backticks means the split lands in an open span (hermes-agent
        # truncate_message); pull the cut back before the unpaired backtick.
        candidate = rest[:cut]
        if (candidate.count("`") - candidate.count("\\`")) % 2 == 1:
            last = candidate.rfind("`")
            while last > 0 and candidate[last - 1] == "\\":
                last = candidate.rfind("`", 0, last)
            safe = max(candidate.rfind("\n", 0, last), candidate.rfind(" ", 0, last))
            if safe >= 1 and safe >= cut // 4:
                cut = safe
        if cut < 1:
            cut = 1  # degenerate budget: always consume one codepoint
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return [c for c in chunks if c] or [""]


def send(token, chat_id, text, reply_to=None, chunk_limit=CHUNK):
    """Send markdown text rendered as Telegram HTML; any chunk Telegram
    refuses (bad entity, overlong tag) falls back to plain text so a
    formatting bug can never drop the payload. Chunks whose HTML is
    unbalanced locally skip the doomed HTML attempt entirely."""
    ok = True
    in_pre = False
    pre_lang = ""
    text = _wrap_markdown_tables(text or "")
    for i, chunk in enumerate(split_chunks(text, chunk_limit)):
        html, in_pre, pre_lang = md_to_html(chunk, in_pre, pre_lang)
        params = {
            "chat_id": chat_id,
            "text": html,
            "link_preview_options": json.dumps({"is_disabled": True}),
        }
        if _balanced(html):
            params["parse_mode"] = "HTML"
        if i == 0 and reply_to:
            params["reply_parameters"] = json.dumps({"message_id": reply_to})
        res = api(token, "sendMessage", **params) if "parse_mode" in params else None
        if not res or not res.get("ok"):
            log("html send failed — resending chunk as plain text")
            params.pop("parse_mode", None)
            params.pop("link_preview_options", None)
            params["text"] = _strip_html_markup(chunk) or chunk
            res = api(token, "sendMessage", **params)
        if not res or not res.get("ok"):
            ok = False
    return ok


def send_retry(cfg, chat_id, text, reply_to=None):
    """One retry with backoff for must-not-lose payloads (the run's answer).

    A failed sendMessage otherwise silently deletes a 6-minute agent run. If
    both attempts fail: audit + log loudly + persist to undelivered/ so the
    content survives even though Telegram never saw it."""
    limit = cfg.get("chunk") or CHUNK
    for attempt in (1, 2):
        if send(cfg["bot_token"], chat_id, text, reply_to=reply_to, chunk_limit=limit):
            return True
        if attempt == 1:
            time.sleep(2)
    audit("delivery_failed", chat_id=chat_id, chars=len(text or ""))
    log(f"chat={chat_id} DELIVERY FAILED after retry ({len(text or '')} chars)")
    try:
        d = os.path.join(CONFIG_DIR, "undelivered")
        ensure_private_dir(d)
        path = os.path.join(d, time.strftime("%Y%m%d-%H%M%S") + f"-{chat_id}.txt")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text or "")
        os.chmod(path, 0o600)
        log("saved undelivered payload")
    except OSError:
        pass
    return False


def _post(url, data, timeout):
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def announce_all(cfg, text, post=_post):
    """Best-effort death notice to every allowed chat. 3s timeout each,
    never raises — usable from crash paths and signal handlers."""
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
            log(f"announce {chat_id}: {e}")


def kill_after(proc, delay):
    """Escalate to SIGKILL if proc hasn't exited after delay seconds."""

    def _k():
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    t = threading.Timer(delay, _k)
    t.daemon = True
    t.start()


def run_timeout(cfg):
    try:
        return int(cfg.get("run_timeout_s", RUN_TIMEOUT_S))
    except (TypeError, ValueError):
        return RUN_TIMEOUT_S


def outbox_dir(cfg):
    return cfg.get("outbox_dir") or os.path.join(cfg["workdir"], ".tgbridge-outbox")


def home_chat(cfg):
    """The DM chat: an allowed_chats entry that is also an allowed user id."""
    users = cfg.get("allowed_user_ids") or []
    for c in cfg.get("allowed_chats") or []:
        if c in users:
            return c
    ch = cfg.get("allowed_chats") or []
    return ch[0] if ch else None


def typing_loop(token, chat_id, stop_event):
    while not stop_event.wait(4.0):
        api(token, "sendChatAction", chat_id=chat_id, action="typing")


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


def edit_status(cfg, live, final=None):
    now = time.time()
    if not final and now - live.get("last_edit", 0) < 8:
        return
    live["last_edit"] = now
    elapsed = int(now - live["start"])
    if final:
        extra = ""
        if live.get("cost"):
            extra += f" · {live['tokens'] or 0} tok · ${live['cost']:.4f}"
        text = f"{final} · {elapsed}s · {len(live['trail'])} tool calls{extra}"
    else:
        trail = "\n".join(live["trail"][-5:])
        text = f"⚙️ working… {elapsed}s\n{trail}"
        thinking = (live.get("thinking") or "").strip().replace("\n", " ")
        if thinking:
            text += f"\n💭 …{thinking[-200:]}"
        preview = (live.get("preview") or "").strip().replace("\n", " ")
        if preview:
            text += f"\n💬 …{preview[-200:]}"
    if live.get("status_id"):
        api(
            cfg["bot_token"],
            "editMessageText",
            chat_id=live["chat_id"],
            message_id=live["status_id"],
            text=text,
        )


def _bin(env_key, name):
    p = os.environ.get(env_key) or shutil.which(name)
    if not p:
        raise RunnerError(
            f"runner {name!r} not found on PATH; install it or set {env_key}"
        )
    return p


RUNNERS = {}


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


def run_agent(cfg, session_id, prompt, live=None):
    """Stream the runner's JSON events live (Popen).

    A watchdog Timer enforces RUN_TIMEOUT_S without blocking the read loop;
    stderr is drained on a side thread so the pipe can never fill and deadlock.
    """
    rname = cfg.get("runner", "opencode")
    runner_fn = RUNNERS.get(rname)
    if not runner_fn:
        return (
            session_id,
            None,
            (f"unknown runner {rname!r} (available: {', '.join(sorted(RUNNERS))})"),
        )
    try:
        cmd, parse = runner_fn(session_id, prompt, cfg.get("model"))
    except RunnerError as e:
        return session_id, None, str(e)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cfg["workdir"],
    )
    RUN_STATE["proc"] = proc  # exposed for /cancel and the shutdown path
    assert proc.stdout and proc.stderr  # guaranteed: both opened with PIPE
    timeout_s = run_timeout(cfg)
    timed_out = []
    killer = threading.Timer(timeout_s, lambda: (timed_out.append(1), proc.kill()))
    killer.daemon = True
    killer.start()
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
            trail = parse(ev, acc)
            sid = acc["sid"]
            if live is not None:
                if trail:
                    live["trail"].append(trail)
                    edit_status(cfg, live)
                if acc.get("thinking"):
                    live["thinking"] = acc["thinking"]
                    edit_status(cfg, live)
                if acc["texts"]:
                    live["preview"] = acc["texts"][-1]
                    edit_status(cfg, live)
        proc.wait()
    finally:
        killer.cancel()
        RUN_STATE["proc"] = None
    cancelled = RUN_STATE.pop("cancel", False)
    if timed_out:
        partial = "\n".join(acc["texts"]).strip()
        if partial:
            return (
                sid,
                partial
                + "\n\n⚠️ (partial answer — hit the %ss timeout and was killed)"
                % timeout_s,
                None,
            )
        return sid, None, "agent timed out after %ss and was killed" % timeout_s
    if cancelled and proc.returncode != 0:
        partial = "\n".join(acc["texts"]).strip()
        if partial:
            return sid, partial + "\n\n🛑 (cancelled by user — partial answer)", None
        return sid, None, CANCEL_MSG
    if proc.returncode != 0:
        tail = (errbuf[0] if errbuf else "").strip()[-600:]
        return (
            sid,
            None,
            f"{cfg.get('runner', 'opencode')} failed rc={proc.returncode}\n{tail}",
        )
    if not acc["texts"]:
        return sid, None, "agent returned no text"
    return sid, "\n".join(acc["texts"]).strip(), None


def multipart(fields, file_field, filename, data, ctype):
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


def transcribe(cfg, token, file_id):
    """Voice note -> text via any OpenAI-compatible /audio/transcriptions API.

    Optional: only runs when transcribe_key is set. Stdlib multipart, no deps.
    """
    g = api(token, "getFile", file_id=file_id)
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
    boundary, body = multipart(
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


def unpack_entry(entry):
    """Queue items arrive as (chat_id, message_id, prompt) tuples (prompt path)
    or dicts (scheduled /at jobs). Accept both — never crash the worker."""
    if isinstance(entry, dict):
        return entry["chat_id"], entry["message_id"], entry["prompt"]
    return entry


def selftest():
    """Regression gate: cheap checks that catch the bugs we actually shipped."""
    import json as _json

    fails = []

    # worker unpack: both queue shapes
    if unpack_entry((1, 2, "p")) != (1, 2, "p"):
        fails.append("unpack tuple")
    if unpack_entry({"chat_id": 1, "message_id": 2, "prompt": "p"}) != (1, 2, "p"):
        fails.append("unpack dict")

    # chunker
    if split_chunks("a" * 100, limit=10)[0] != "a" * 10:
        fails.append("chunk hard cut")
    parts = split_chunks("x\n\n" + "y" * 100, limit=20)
    if parts[0] != "x" or len(parts) < 2:
        fails.append("chunk boundary")

    # markdown -> telegram HTML
    h, pre, _lang = md_to_html("code `<b>&</b>` and **bold** [t](http://x/y)")
    if (
        h
        != (
            "code <code>&lt;b&gt;&amp;&lt;/b&gt;</code> and <b>bold</b> "
            '<a href="http://x/y">t</a>'
        )
        or pre
    ):
        fails.append("md inline")
    # new inline entities: italic / strikethrough / spoiler; bold wins over
    # inner single asterisks (hermes format_message parity)
    h, pre, _lang = md_to_html("*it* ~~gone~~ ||shh|| **b*bold*i**")
    if h != "<i>it</i> <s>gone</s> <tg-spoiler>shh</tg-spoiler> <b>b*bold*i</b>" or pre:
        fails.append("md inline rich")
    # ***bold italic*** renders as nested tags, not broken crossing ones
    h, pre, _lang = md_to_html("***both***")
    if h != "<b><i>both</i></b>" or pre:
        fails.append("md bold italic")
    # _x_ italics, but snake_case and bare arithmetic stay literal
    h, pre, _lang = md_to_html("my_var and a * b and *real*")
    if h != "my_var and a * b and <i>real</i>" or pre:
        fails.append("md italic guards")
    # list markers -> native bullet glyph
    h, pre, _lang = md_to_html("- one\n* two\n+ three\nplain - dash")
    if (
        h
        != "• one\n• two\n• three\nplain - dash"
        or pre
    ):
        fails.append("md bullets")
    # header flattens inner bold (no <b><b> nesting Telegram refuses)
    h, pre, _lang = md_to_html("## **Title** here")
    if h != "<b>Title here</b>" or pre:
        fails.append("md header flat")
    # blockquote line
    h, pre, _lang = md_to_html("text\n> quoted line\nafter")
    if h != "text\n<blockquote>quoted line</blockquote>\nafter" or pre:
        fails.append("md blockquote")
    # consecutive quote lines merge into ONE blockquote
    h, pre, _lang = md_to_html("> line1\n> line2\nafter")
    if h != "<blockquote>line1\nline2</blockquote>\nafter" or pre:
        fails.append("md blockquote merge")
    # expandable quote: **> opener + trailing || closer (hermes)
    h, pre, _lang = md_to_html("**> details\n> more||\nafter")
    if h != '<blockquote expandable>details\nmore</blockquote>\nafter' or pre:
        fails.append("md blockquote expandable")
    # blockquote chars in prose text are NOT blockquotes
    h, pre, _lang = md_to_html("a > b implies")
    if "<blockquote>" in h:
        fails.append("md blockquote false positive")
    # tables convert to bullets exactly like hermes convert_table_to_bullets:
    # heading = first non-empty cell, bullet duplicating the heading is dropped
    h = _wrap_markdown_tables("| a | b |\n|---|---|\n| 1 | 2 |")
    if "**1**" not in h or "• b: 2" not in h or "|" in h:
        fails.append("md table")
    # bold cells are plain markdown for the renderer — the HTML must never
    # contain redundant <b><b> (heading cells are flattened pre-wrap)
    html, _pre, _lang = md_to_html(
        _wrap_markdown_tables("| **a** | b |\n|---|---|\n| 1 | **2** |")
    )
    if "<b><b>" in html or "<b>1</b>" not in html or "• b: <b>2</b>" not in html:
        fails.append("md table bold")
    h = _wrap_markdown_tables("```\n| a | b |\n|---|---|\n| 1 | 2 |\n```")
    if "| 1 | 2 |" not in h:
        fails.append("md table in fence")
    # fenced block with language -> <pre><code class="language-x">
    h, pre, lang = md_to_html("a\n```py\nx < y\n```\nb")
    if (
        pre
        or lang
        or h
        != 'a\n<pre><code class="language-py">\nx &lt; y\n</code></pre>\nb'
    ):
        fails.append("md fence")
    # fence split across chunks stays balanced per chunk, carrying the
    # language tag (hermes truncate_message carry_lang)
    h1, pre1, lang1 = md_to_html("intro\n```py\nprint(1)")
    h2, pre2, lang2 = md_to_html("print(2)\n```", in_pre=pre1, pre_lang=lang1)
    if (
        pre1 is not True
        or pre2 is not False
        or lang1 != "py"
        or lang2
        or "</code></pre>" not in h1
        or h2.count("<pre>") != 1
        or 'language-py' not in h2
    ):
        fails.append("md fence continuation")
    # _balanced: valid vs broken chunks
    if not _balanced("<b>a<code>b</code></b> &amp; <i>c</i>"):
        fails.append("balanced ok")
    if _balanced("<b>a<code>b</b>") or _balanced("</b>") or _balanced("ok <b>"):
        fails.append("balanced broken")
    # nested <b><i> is balanced; the renderer must never emit redundant
    # same-type nesting like <b><b> (headers/tables flatten it away)
    if not _balanced("<b><i>x</i></b>") or _balanced("<b>a<code>b</code></i>"):
        fails.append("balanced nesting")
    if "<b><b>" in md_to_html("## **T** x")[0] + md_to_html("***x***")[0]:
        fails.append("no same-type nesting")
    # _strip_html_markup: fallback text has no raw markup syntax left
    plain = _strip_html_markup("# Head\n\n**hi** `x <y>` [t](http://e/x)\n> q\n```py\ncode\n```")
    if (
        "**" in plain
        or "`" in plain
        or "```" in plain
        or "](" in plain
        or plain != "Head\n\nhi x <y> t (http://e/x)\nq\ncode"
    ):
        fails.append("strip markup")
    # utf16 chunking: astral chars counted as 2 units, never split mid-char
    tc = "😀" * 10
    if utf16_len(tc) != 20:
        fails.append("utf16 len")
    if len(split_chunks(tc + "x", limit=15)) != 2 or "".join(
        split_chunks(tc + "x", limit=15)
    ) != tc + "x":
        fails.append("utf16 chunk content")
    # inline-code split avoidance: cut lands outside the backtick span
    parts = split_chunks("word " + "z" * 40 + " `code span` tail", limit=20)
    joined = "\n".join(parts)
    if joined.count("`") % 2 != 0:
        fails.append("chunk inline code")
    # chunk content is preserved across all chunks
    if split_chunks("a" * 45, limit=20) != ["a" * 20, "a" * 20, "a" * 5]:
        fails.append("chunk preserve")
    # table conversion integrates with send path (via fake_api below)

    # send(): HTML refused -> plain-text fallback, never lost
    sent = []

    def fake_api(token, method, **params):
        sent.append(params)
        if params.get("parse_mode") == "HTML" and "<b>" in params["text"]:
            return None  # simulate Telegram rejecting the entity
        return {"ok": True}

    orig_api = api
    globals()["api"] = fake_api
    try:
        if not send("t", 1, "hi **there**"):
            fails.append("send fallback ok")
    finally:
        globals()["api"] = orig_api
    # fallback resend is clean plain text, never the raw markdown
    if (
        len(sent) != 2
        or "parse_mode" in sent[1]
        or sent[1]["text"] != "hi there"
    ):
        fails.append("send fallback shape")

    # table markdown flows through send() as converted bullet HTML
    sent.clear()

    def fake_api2(token, method, **params):
        sent.append(params)
        return {"ok": True}

    globals()["api"] = fake_api2
    try:
        if not send("t", 1, "| a | b |\n|---|---|\n| 1 | 2 |"):
            fails.append("send table ok")
    finally:
        globals()["api"] = orig_api
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
    # An allowed group member can prompt. Human group messages are captured by
    # default for ambient context, but the bot still speaks only when mentioned.
    auth_state = {
        "bot_username": "Bot",
        "sessions": {"-10022": "keep-me"},
        "hints": {"-10022": time.time()},
    }
    def auth_api(token, method, **params):
        return {"ok": True, "result": {"message_id": 1}}

    globals()["api"] = auth_api
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
        )
        if private_state.get("context"):
            fails.append("group context opt-out")
    finally:
        globals()["api"] = orig_api

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
        "runners + unpack + render + chunker + announce + kill + timeout + botcmd + auth",
    )


def worker(cfg, state):
    """Serial agent-run consumer; poll loop stays live for commands.

    One item = one try/except: a bad item must never kill the thread."""
    while True:
        entry = PROMPT_Q.get()
        chat_id = message_id = prompt = None
        try:
            chat_id, message_id, prompt = unpack_entry(entry)
            RUN_STATE["busy"] = True
            RUN_STATE["current"] = {
                "chat": chat_id,
                "since": time.time(),
                "prompt": prompt[:60],
            }
            with STATE_LOCK:
                session_id = state.get("sessions", {}).get(str(chat_id))
            outbox = outbox_dir(cfg)
            prompt = prompt + (
                f"\n\n(To give files to the user, write them into {outbox}/ "
                "— they are delivered automatically after this run.)"
            )
            audit("run_start", chat_id=chat_id, chars=len(prompt), session=session_id)
            react(cfg, chat_id, message_id, "👀")
            status = api(
                cfg["bot_token"],
                "sendMessage",
                chat_id=chat_id,
                text="⚙️ working… 0s",
                disable_notification=True,
                reply_parameters=json.dumps({"message_id": message_id}),
            )
            live = {
                "chat_id": chat_id,
                "status_id": (status.get("result") or {}).get("message_id")
                if status
                else None,
                "trail": [],
                "start": time.time(),
                "last_edit": 0,
            }
            stop_typing = threading.Event()
            threading.Thread(
                target=typing_loop,
                args=(cfg["bot_token"], chat_id, stop_typing),
                daemon=True,
            ).start()
            log(
                f"chat={chat_id} run start (session={session_id}, q={PROMPT_Q.qsize()})"
            )
            try:
                new_sid, answer, err = run_agent(cfg, session_id, prompt, live)
            except Exception as e:
                err = f"bridge error: {e}"
                new_sid, answer = session_id, None
            finally:
                stop_typing.set()
                RUN_STATE["busy"] = False
                RUN_STATE["current"] = None
            if err and session_id and "failed rc=" in err and err != CANCEL_MSG:
                live["trail"].append("♻️ stale session — retrying fresh")
                new_sid, answer, err = run_agent(cfg, None, prompt, live)
            if live["status_id"]:
                edit_status(cfg, live, final="✅ done" if not err else "🔴 failed")
            with STATE_LOCK:
                if new_sid and new_sid != session_id:
                    state.setdefault("sessions", {})[str(chat_id)] = new_sid
                save_json(STATE_PATH, state)
            if err == CANCEL_MSG:
                audit("run_cancelled", chat_id=chat_id)
                send_retry(cfg, chat_id, CANCEL_MSG)
                log(f"chat={chat_id} cancelled")
            elif err:
                react(cfg, chat_id, message_id, "👎")
                send_retry(cfg, chat_id, f"⚠️ {err}")
                audit("run_error", chat_id=chat_id, err=err[:200])
                log(f"chat={chat_id} error: {err[:120]}")
            else:
                react(cfg, chat_id, message_id, "👍")
                send_retry(cfg, chat_id, answer or "", reply_to=message_id)
                audit(
                    "run_done",
                    chat_id=chat_id,
                    chars=len(answer or ""),
                    secs=int(time.time() - live["start"]),
                )
                log(
                    f"chat={chat_id} done ({len(answer or '')} chars, session={new_sid})"
                )
        except Exception as e:
            log(f"worker item error: {e}")
            audit("worker_error", err=str(e)[:200])
            if chat_id:
                send(cfg["bot_token"], chat_id, f"⚠️ bridge error: {e}")
        finally:
            try:
                outbox = outbox_dir(cfg)
                for fn in sorted(os.listdir(outbox)):
                    p = os.path.join(outbox, fn)
                    if os.path.isfile(p) and chat_id is not None:
                        res = send_document(cfg["bot_token"], chat_id, p)
                        if res and res.get("ok"):
                            os.remove(p)  # keep the file if delivery failed
            except FileNotFoundError:
                pass
            except Exception as e:
                log(f"outbox delivery: {e}")
            PROMPT_Q.task_done()


def fire_at(cfg, state, due):
    with STATE_LOCK:
        entry = state.get("at", {}).pop(str(due), None)
        if entry:
            save_json(STATE_PATH, state)
    if not entry:
        return
    audit("at_fire", chat_id=entry["chat_id"], prompt=entry["prompt"][:80])
    PROMPT_Q.put((entry["chat_id"], entry["message_id"], entry["prompt"]))


def rearm_at(cfg, state):
    """Re-arm /at timers after a restart — nothing scheduled is silently lost."""
    for due in list(state.get("at", {})):
        try:
            d = float(due)
        except ValueError:
            continue
        t = threading.Timer(max(d - time.time(), 1), fire_at, args=(cfg, state, d))
        t.daemon = True
        t.start()


def save_document(cfg, msg):
    doc = msg.get("document") or {}
    fid = doc.get("file_id")
    if not fid:
        return None
    name = os.path.basename(doc.get("file_name") or "file") or "file"
    r = api(cfg["bot_token"], "getFile", file_id=fid)
    fp = (r or {}).get("result", {}).get("file_path")
    if not fp:
        return None
    inbox = os.path.join(CONFIG_DIR, "inbox")
    ensure_private_dir(inbox)
    dest = os.path.join(inbox, time.strftime("%Y%m%d-%H%M%S") + "-" + name)
    url = f"https://api.telegram.org/file/bot{cfg['bot_token']}/{fp}"
    try:
        urllib.request.urlretrieve(url, dest)
        os.chmod(dest, 0o600)
    except Exception as e:
        log(f"download {name}: {e}")
        return None
    log(f"attachment saved: {dest}")
    return dest


def send_document(token, chat_id, path):
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
        log(f"send_document {fn}: {e}")
        return None


def botcmd(text):
    """/cmd@BotName args -> ('/cmd', 'args'); non-commands -> (None, '')."""
    parts = text.split(maxsplit=1)
    if not parts or not parts[0].startswith("/"):
        return None, ""
    return parts[0].split("@")[0].lower(), (parts[1] if len(parts) > 1 else "").strip()


def is_authorized(cfg, chat_id, chat_type, user_id):
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


def handle_update(cfg, state, upd):
    msg = upd.get("message")
    if not msg:
        return
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    user_id = (msg.get("from") or {}).get("id")
    text = msg.get("text") or msg.get("caption") or ""
    message_id = msg.get("message_id")
    if not is_authorized(cfg, chat_id, chat_type, user_id):
        return
    bot_username = state.get("bot_username", "")

    if chat_type != "private":
        reply = msg.get("reply_to_message") or {}
        replied_to_bot = (reply.get("from") or {}).get("username") == bot_username
        if f"@{bot_username}" not in text and not replied_to_bot:
            if (
                cfg.get("capture_group_context", True)
                and text.strip()
                and not text.lstrip().startswith("/")
            ):
                with STATE_LOCK:
                    buf = state.setdefault("context", {}).setdefault(str(chat_id), [])
                    buf.append(
                        {
                            "who": (msg.get("from") or {}).get("first_name") or "?",
                            "t": time.strftime("%H:%M"),
                            "text": text.strip()[:200],
                        }
                    )
                    del buf[:-20]
                now = time.time()
                hints = state.get("hints", {})
                if now - hints.get(str(chat_id), 0) > 1800:
                    with STATE_LOCK:
                        state.setdefault("hints", {})[str(chat_id)] = now
                        save_json(STATE_PATH, state)
                    send(
                        cfg["bot_token"],
                        chat_id,
                        f"💡 @{bot_username} at me (or reply to my messages) and I'll answer",
                    )
            return
        text = text.replace(f"@{bot_username}", "").strip()

    if msg.get("document"):
        doc_path = save_document(cfg, msg)
        if doc_path:
            text = (text + f"\n[attachment saved: {doc_path}]").strip()

    cmd, rest = botcmd(text)
    if cmd == "/help":
        send(
            cfg["bot_token"],
            chat_id,
            "commands: /new reset session · /status state · /at 30m <prompt> "
            "schedule · /cancel abort current run · anything else goes to the agent",
        )
        return
    if cmd == "/new":
        with STATE_LOCK:
            state.setdefault("sessions", {}).pop(str(chat_id), None)
            save_json(STATE_PATH, state)
        send(cfg["bot_token"], chat_id, "session cleared. next message starts fresh.")
        return
    if cmd == "/cancel":
        proc = RUN_STATE.get("proc")
        cur = RUN_STATE.get("current")
        if not (proc and cur and proc.poll() is None):
            send(cfg["bot_token"], chat_id, "nothing running")
            return
        if chat_type != "private" and cur["chat"] != chat_id:
            send(
                cfg["bot_token"],
                chat_id,
                f"run belongs to chat {cur['chat']} — cancel from there",
            )
            return
        try:
            proc.terminate()
        except Exception:
            pass
        kill_after(proc, 5)
        RUN_STATE["cancel"] = True
        audit("cancel_requested", by_chat=chat_id, run_chat=cur["chat"])
        send(cfg["bot_token"], chat_id, "🛑 stopping current run…")
        return
    if cmd == "/status":
        with STATE_LOCK:
            info = state.get("sessions", {}).get(str(chat_id)) or "(none)"
            pending = sum(
                1 for e in state.get("at", {}).values() if e.get("chat_id") == chat_id
            )
        cur = ""
        if RUN_STATE.get("current"):
            c = RUN_STATE["current"]
            cur = f"\nrunning: {c['prompt']}… ({int(time.time() - c['since'])}s)"
        send(
            cfg["bot_token"],
            chat_id,
            f"chat {chat_id}\nrunner: {cfg.get('runner', 'opencode')}\n"
            f"session: {info}\ncwd: {cfg['workdir']}\n"
            f"queued: {PROMPT_Q.qsize()}\nscheduled: {pending}{cur}",
        )
        return
    if cmd == "/at":
        m = re.match(r"^(\d+)([smh])\s+(.+)$", rest, re.I)
        if not m:
            send(cfg["bot_token"], chat_id, "usage: /at 30m <prompt>  (s/m/h, max 7d)")
            return
        delay = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2).lower()]
        if delay > 7 * 86400:
            send(cfg["bot_token"], chat_id, "/at max is 7d")
            return
        due = int(time.time()) + delay
        prompt = m.group(3).strip()
        with STATE_LOCK:
            state.setdefault("at", {})[str(due)] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "prompt": prompt,
            }
            save_json(STATE_PATH, state)
        t = threading.Timer(delay, fire_at, args=(cfg, state, due))
        t.daemon = True
        t.start()
        audit("at_set", chat_id=chat_id, delay=delay, prompt=prompt[:80])
        send(
            cfg["bot_token"],
            chat_id,
            f"⏰ scheduled in {delay}s — fires via this chat's session",
        )
        return

    if msg.get("voice") and not text.strip():
        if not cfg.get("transcribe_key"):
            send(
                cfg["bot_token"],
                chat_id,
                "🎤 voice note received but transcription not configured "
                "(set transcribe_key / transcribe_base_url / transcribe_model)",
            )
            return
        text, err = transcribe(cfg, cfg["bot_token"], msg["voice"].get("file_id"))
        if err:
            react(cfg, chat_id, message_id, "👎")
            send(cfg["bot_token"], chat_id, f"⚠️ {err}")
            return
        audit("voice", chat_id=chat_id, chars=len(text or ""))
        text = f"(voice note) {text}"

    if not text.strip():
        return

    audit("enqueue", chat_id=chat_id, user_id=user_id, chars=len(text.strip()))
    prompt_text = text.strip()
    if chat_type != "private" and cfg.get("capture_group_context", True):
        with STATE_LOCK:
            buf = (state.get("context") or {}).pop(str(chat_id), None) or []
        if buf:
            digest = "\n".join(f"- {e['who']} {e['t']}: {e['text']}" for e in buf[-20:])
            prompt_text = (
                "[group messages since your last turn — passive context, "
                "nobody asked you anything yet:\n" + digest + "\n]\n\n" + prompt_text
            )
    PROMPT_Q.put((chat_id, message_id, prompt_text))
    if RUN_STATE["busy"]:
        send(
            cfg["bot_token"],
            chat_id,
            f"⏳ queued (position {PROMPT_Q.qsize()}) — /status for details",
        )


def cli_send(args):
    """Agent-initiated outbound: tgbridge.py --send <chat_id> <text>."""
    if len(args) < 2:
        sys.exit("usage: tgbridge.py --send <chat_id> <text>")
    ensure_private_storage()
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit(f"missing config {CONFIG_PATH}")
    try:
        chat_id = int(args[0])
    except ValueError:
        sys.exit("chat_id must be an integer")
    if chat_id not in cfg["allowed_chats"]:
        sys.exit(f"chat {chat_id} not in allowed_chats — refusing")
    send(cfg["bot_token"], chat_id, args[1])
    audit("send", chat_id=chat_id, chars=len(args[1]))
    log(f"--send -> {chat_id} ({len(args[1])} chars)")


class BridgeStop(Exception):
    """Raised by the SIGTERM/SIGINT handler — a graceful stop, not a crash."""


def on_stop(signum, frame):
    raise BridgeStop(signum)


def run(cfg):
    state = load_json(STATE_PATH, {})
    if not cfg.get("capture_group_context", True):
        state.pop("context", None)
        state.pop("hints", None)
    me = api(cfg["bot_token"], "getMe")
    if not me or not me.get("ok"):
        sys.exit("getMe failed: bad token?")
    state["bot_username"] = me["result"]["username"]
    save_json(STATE_PATH, state)
    api(
        cfg["bot_token"],
        "setMyCommands",
        commands=json.dumps(
            [
                {"command": "new", "description": "Reset session for this chat"},
                {"command": "status", "description": "Show session info"},
                {"command": "at", "description": "Schedule a prompt: /at 30m <text>"},
                {"command": "cancel", "description": "Abort the current run"},
                {"command": "help", "description": "List commands"},
            ]
        ),
    )
    log(f"tgbridge up as @{state['bot_username']}, chats={cfg['allowed_chats']}")

    # Startup sanity: a missing runner binary must not kill the bridge —
    # commands still work; warn once in the home chat, runs report the error.
    rname = cfg.get("runner", "opencode")
    hc = home_chat(cfg)
    warn = None
    if rname not in RUNNERS:
        warn = (
            f"⚠️ unknown runner {rname!r} (available: {', '.join(sorted(RUNNERS))}) "
            "— commands work, runs will fail until config.json is fixed"
        )
    else:
        try:
            RUNNERS[rname](None, "sanity", None)
        except RunnerError as e:
            warn = f"⚠️ startup check: {e} — commands work, runs will error"
        except Exception as e:
            log(f"startup runner check: {e}")
    if warn and hc:
        send(cfg["bot_token"], hc, warn)

    worker_t = threading.Thread(target=worker, args=(cfg, state), daemon=True)
    worker_t.start()
    rearm_at(cfg, state)

    offset = state.get("offset")
    backoff = 3
    while True:
        if not worker_t.is_alive():
            log("worker thread died — respawning")
            audit("worker_respawn")
            announce_all(cfg, "💀 bridge worker thread died — respawned")
            worker_t = threading.Thread(target=worker, args=(cfg, state), daemon=True)
            worker_t.start()
        params = {"timeout": 50, "allowed_updates": json.dumps(["message"])}
        if offset:
            params["offset"] = offset
        res = api(cfg["bot_token"], "getUpdates", **params)
        if not res or not res.get("ok"):
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        backoff = 3
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            try:
                handle_update(cfg, state, upd)
            except Exception as e:
                log(f"update {upd.get('update_id')} handler error: {e}")
                m = upd.get("message") or {}
                cid = (m.get("chat") or {}).get("id")
                if cid:
                    send(cfg["bot_token"], cid, f"⚠️ bridge error: {e}")
            with STATE_LOCK:
                state["offset"] = offset
                save_json(STATE_PATH, state)


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    selftest()  # regression gate — a bridge that fails checks must not go live

    ensure_private_storage()
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit(f"missing config {CONFIG_PATH}")
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    try:
        run(cfg)
    except BridgeStop:
        p = RUN_STATE.get("proc")
        if p is not None and p.poll() is None:
            try:
                p.terminate()  # don't orphan a burning agent run
            except Exception:
                pass
        audit("stop", reason="signal")
        announce_all(cfg, "💀 bridge stopping")
        log("bridge stopped by signal")
        sys.exit(0)
    except SystemExit as e:
        announce_all(cfg, f"💀 bridge exited: {e.code or ''}".rstrip())
        raise
    except BaseException as e:
        audit("crash", err=str(e)[:300])
        announce_all(cfg, f"💀 bridge crashed: {e} — restarting")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--send":
        cli_send(sys.argv[2:])
    else:
        main()
