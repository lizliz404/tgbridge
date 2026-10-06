"""Capture immutable startup evidence, not the checkout's later disk HEAD.

The bridge imports this snapshot once. Updating source on disk cannot make an
already running Python process advertise the new version as loaded.
"""
import hashlib
from pathlib import Path
import subprocess


def source_identity(root):
    root = Path(root)
    files = [root / "tgbridge.py", *sorted((root / "tgbridge_core").glob("*.py"))]
    digest = hashlib.sha256()
    try:
        for path in files:
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    except OSError:
        return {"revision": None, "source_sha256": None}
    revision = None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            candidate = result.stdout.strip()
            if len(candidate) == 40 and all(c in "0123456789abcdef" for c in candidate):
                revision = candidate
    except (OSError, subprocess.TimeoutExpired):
        pass  # source archives need no Git executable or .git directory
    return {"revision": revision, "source_sha256": digest.hexdigest()}


LOADED_CODE = source_identity(Path(__file__).resolve().parents[1])
