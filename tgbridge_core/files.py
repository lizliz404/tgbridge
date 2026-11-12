"""Pull-model file delivery: reply-named paths and /files /send.

Ported from hanzili/elite-devin-bridge, scoped tighter: every resolved path
is confined to the configured workdir (their bridge can ship any absolute
path the reply mentions; a Telegram bridge running agents unattended should
not). The outbox push model still carries turn output; this module is the
pull side for "send me that report" without re-prompting the agent.
"""
import os
import re

SEND_EXT = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".txt", ".md", ".zip",
    ".png", ".jpg", ".jpeg", ".mp4", ".json", ".html", ".log",
    ".py", ".svg", ".webp",
}
SKIP_DIRS = ("inbox", "node_modules", "__pycache__", "dist", ".venv",
             "venv", ".git", ".hg", ".svn", ".tox", ".mypy_cache", ".idea")
WORKFILE = {"devin-progress.json"}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # Telegram sendDocument limit
AUTO_SEND_NAMED = 4  # named-in-reply paths auto-sent per turn (intent-gated)
POOL_SHOW = 15

_INTERNAL_RE = re.compile(r"(^|/)\.[^/]+$|(^|/)(memory\.md|\.devin-goal[^/]*)$",
                          re.IGNORECASE)
_ABS_PATH_RE = re.compile(r"(/[^\s\"'`<>\[\]{}()|]+)")


def _uploadable(path):
    if not os.path.isfile(path):
        return False
    try:
        return 0 < os.path.getsize(path) <= MAX_UPLOAD_BYTES
    except OSError:
        return False


def _in_workdir(path, workdir):
    root = os.path.realpath(workdir)
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


def _internal(path):
    base = os.path.basename(path)
    return _INTERNAL_RE.search(path) or base in WORKFILE


def named_paths(app, text, workdir):
    """Absolute paths a reply explicitly names — deliverable intent.

    Realpath must resolve inside workdir; symlink escapes don't send."""
    out = []
    for match in _ABS_PATH_RE.findall(text or ""):
        path = match.rstrip(".,;:，。；：）)")
        real = os.path.realpath(path)
        if (_in_workdir(real, workdir) and _uploadable(real)
                and not _internal(real)):
            out.append(real)
    return list(dict.fromkeys(out))


def deliverables(app, workdir):
    """Candidate deliverables under workdir, mtime-desc — /files + /send all."""
    out = []
    for dirpath, dirnames, filenames in os.walk(workdir):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and not d.startswith(".")]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if (os.path.splitext(name)[1].lower() in SEND_EXT
                    and not _internal(path) and _uploadable(path)):
                out.append((os.path.getmtime(path), path))
    out.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in out]


def resolve_name(app, workdir, name):
    """Resolve a /send argument: workdir-confined path or basename match."""
    name = (name or "").strip()
    if not name:
        return None
    if os.path.isabs(name):
        real = os.path.realpath(name)
        return real if (_in_workdir(real, workdir) and _uploadable(real)) else None
    for path in deliverables(app, workdir):
        if os.path.basename(path) == name:
            return path
    return None
