"""Provider-limit governor: park a quota-killed turn, resume after cooldown.

A prompt that dies on a provider limit ("weekly limit reached", "rate limit
reset in 14h", a retryable server refusal) is not a task failure — the account
just needs to cool down. Ported from hanzili/elite-devin-bridge's
limit_governor.py and adapted to this bridge's per-chat queue model: parked
units are (chat_id, message_id, prompt, input_ids) replayed through PROMPT_Q
instead of thread-bound ACP sessions, and classification reuses
runners.classify_run_error's markers so multi-runner failover semantics stay
unchanged. The durable registry (<CONFIG_DIR>/.tgbridge_limits.json) plus an
append-only JSONL log (.tgbridge_limits.log) keep the story of each limit
window reviewable after restarts.
"""
import calendar
import json
import os
import random
import re
import threading
import time

# Wire messages seen in production:
#   "Reached free model rate limit. ... Your limit will reset in
#    14 hours 52 minutes (at Nov 8 04:01 UTC)."
RESET_IN_RE = re.compile(
    r"reset\s+in\s+(?:(\d+)\s*hours?)?\s*(?:(\d+)\s*minutes?)?"
    r"\s*(?:(\d+)\s*seconds?)?", re.IGNORECASE)
RESET_AT_RE = re.compile(
    r"\(at\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{1,2}):(\d{2})"
    r"(?::(\d{2}))?\s*(UTC|GMT)\)", re.IGNORECASE)
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

REGISTRY_FILE = ".tgbridge_limits.json"
LOG_FILE = ".tgbridge_limits.log"

RESUME_NOTE = (
    "[Auto-resume: the previous attempt was cut by a server-side limit — "
    "continue the unfinished work; if it already completed, a brief 'done' "
    "reply is fine.]\n\n")


def parse_reset(msg):
    """Epoch when the server says the limit resets, or None.
    Handles both 'reset in 14 hours 52 minutes' and '(at Nov 8 04:01
    UTC)' — the message usually carries both."""
    m = RESET_IN_RE.search(msg or "")
    if m and any(m.groups()):
        sec = (int(m.group(1) or 0) * 3600
               + int(m.group(2) or 0) * 60
               + int(m.group(3) or 0))
        if sec:
            return time.time() + sec
    m = RESET_AT_RE.search(msg or "")
    if m:
        mon = MONTHS.get(m.group(1).lower())
        if mon:
            now = time.gmtime()
            year = now.tm_year
            day = int(m.group(2))
            hh, mm = int(m.group(3)), int(m.group(4))
            ss = int(m.group(5) or 0)
            for y in (year, year + 1):
                try:
                    t = calendar.timegm((y, mon, day, hh, mm, ss,
                                         0, 0, 0))
                except (ValueError, OverflowError):
                    continue
                if t > time.time() - 60:
                    return t
    return None


def backoff(attempt, base=30.0, cap=300.0):
    """Exponential backoff for retryable errors (no server window):
    30s, 60s, 120s, 240s, 300s…, plus jitter so a batch of parked turns
    doesn't resume in lockstep."""
    delay = min(base * (2 ** max(0, attempt - 1)), cap)
    return delay + random.uniform(0, min(15.0, delay * 0.3))


class Governor:
    """Durable registry of parked turns + append-only event log.

    entries[key] = {
        chat_id, message_id, prompt, input_ids,   # the turn to replay
        error, error_kind,                        # what parked it
        attempt, first_hit, resume_at,
    }
    """

    def __init__(self, config_dir, printer=None):
        self.path = os.path.join(config_dir, REGISTRY_FILE)
        self.log_path = os.path.join(config_dir, LOG_FILE)
        self.lock = threading.RLock()
        self._print = printer or (lambda msg: None)
        data = self._read()
        self.entries = data.get("entries") or {}

    def _read(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"entries": self.entries}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def event(self, name, **fields):
        rec = {"t": time.time(), "event": name}
        rec.update(fields)
        try:
            with self.lock:
                fd = os.open(self.log_path,
                             os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
        detail = " ".join(f"{k}={v}" for k, v in fields.items()
                          if v is not None and k != "error")
        err = fields.get("error")
        if err:
            detail += (" " if detail else "") + f"err={str(err)[:160]}"
        self._print(f"[limit] {name} {detail}".rstrip())

    def park(self, key, entry):
        with self.lock:
            old = self.entries.get(key) or {}
            entry = dict(entry)
            entry.setdefault("attempt", old.get("attempt", 0) + 1)
            entry.setdefault("first_hit", old.get("first_hit") or time.time())
            self.entries[key] = entry
            self._save()
            self.event("parked", key=key, kind=entry.get("error_kind"),
                       attempt=entry.get("attempt"),
                       resume_in=int(entry.get("resume_at", 0) - time.time()),
                       error=entry.get("error"))
            return entry

    def pop(self, key, reason=""):
        with self.lock:
            e = self.entries.pop(key, None)
            if e is not None:
                self._save()
                self.event("unparked", key=key, reason=reason)
            return e

    def get(self, key):
        with self.lock:
            e = self.entries.get(key)
            return dict(e) if e else None

    def due(self, now=None):
        """Parked entries whose cooldown has elapsed, earliest first."""
        now = time.time() if now is None else now
        with self.lock:
            ready = [(k, dict(e)) for k, e in self.entries.items()
                     if e.get("resume_at", 0) <= now]
        ready.sort(key=lambda kv: kv[1].get("resume_at", 0))
        return ready

    def snapshot(self):
        with self.lock:
            out = [dict(e, key=k) for k, e in self.entries.items()]
        out.sort(key=lambda e: e.get("resume_at", 0))
        return out

    def events_tail(self, n=15):
        try:
            with open(self.log_path) as f:
                lines = f.readlines()[-n:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
