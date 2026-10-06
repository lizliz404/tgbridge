"""Cross-platform proxy inspection and network error classification."""

import datetime
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

def systemd_notify(message):
    """Optional nonblocking sd_notify, adapted from Hermes gateway/systemd_notify.py.

    Main-loop heartbeats let systemd restart a live but wedged bridge. No
    timer, dependency or separate supervisor; noop outside a managed service.
    """
    address = os.environ.get("NOTIFY_SOCKET", "").strip()
    if not address or not hasattr(socket, "AF_UNIX"):
        return False
    pid = os.environ.get("WATCHDOG_PID")
    if pid and pid != str(os.getpid()):
        return False  # agent child imports must not feed its parent's watchdog
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.setblocking(False)
            sender.connect(address)
            sender.send(message.encode("utf-8"))
        return True
    except (OSError, UnicodeError, ValueError):
        return False


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def polling_health(health, stale_after_s=180):
    """Require a live service and recent successful poll, not just getMe.

    The freshness window covers the 50-second long poll and bounded network
    retry. Runtime readiness stays distinct from one-off API reachability.
    """
    result = {"ok": False, "pid_alive": False, "poll_age_s": None}
    pid = health.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        try:
            os.kill(pid, 0)
            result["pid_alive"] = True
        except PermissionError:
            result["pid_alive"] = True
        except (OSError, OverflowError):
            pass
    if not result["pid_alive"]:
        result["error"] = "service_not_running"
        return result
    if health.get("status") != "healthy":
        result["error"] = "poll_not_healthy"
        return result
    try:
        # now_iso uses +HHMM; fromisoformat only accepts that on Python 3.11+.
        stamp = datetime.datetime.strptime(health["last_poll_ok_at"], "%Y-%m-%dT%H:%M:%S%z")
        age = time.time() - stamp.timestamp()
    except (KeyError, TypeError, ValueError, OverflowError):
        result["error"] = "invalid_poll_timestamp"
        return result
    result["poll_age_s"] = round(age, 1)
    if not -5 <= age <= stale_after_s:
        result["error"] = "stale_poll"
        return result
    result["ok"] = True
    return result


def redact_proxy_url(value):
    """Return useful proxy coordinates without leaking embedded credentials."""
    if not value or not isinstance(value, str):
        return value
    candidate = value if "://" in value else "http://" + value
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return "<invalid>"
    host = parsed.hostname
    if not host:
        return "<invalid>"
    host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return "<invalid>"
    scheme = parsed.scheme or "http"
    return f"{scheme}://{host}{port}"


def proxy_diagnostics():
    """Describe proxy routing and whether localhost proxy endpoints are alive."""
    proxies = {
        str(kind): redact_proxy_url(str(value))
        for kind, value in urllib.request.getproxies().items()
    }
    local = []
    seen = set()
    for kind, value in proxies.items():
        if kind == "no" or not value or value == "<invalid>":
            continue
        try:
            parsed = urllib.parse.urlsplit(value)
            host, port = parsed.hostname, parsed.port
        except ValueError:
            continue
        if host not in ("localhost", "127.0.0.1", "::1") or not port:
            continue
        endpoint = (host, port)
        if endpoint in seen:
            continue
        seen.add(endpoint)
        listening = False
        try:
            with socket.create_connection(endpoint, timeout=0.4):
                listening = True
        except OSError:
            pass
        local.append(
            {"host": host, "port": port, "listening": listening, "source": kind}
        )
    return {
        "proxies": proxies,
        "telegram_bypassed": urllib.request.proxy_bypass("api.telegram.org"),
        "local_endpoints": local,
    }


def classify_network_error(exc, proxy_info=None):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    refused = isinstance(reason, ConnectionRefusedError) or getattr(
        reason, "errno", None
    ) in (61, 111)
    info = proxy_info if proxy_info is not None else proxy_diagnostics()
    dead_local_proxy = any(
        not endpoint.get("listening") for endpoint in info.get("local_endpoints", [])
    )
    if refused and dead_local_proxy and not info.get("telegram_bypassed"):
        return "proxy_refused"
    if refused:
        return "connection_refused"
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
        return "timeout"
    return type(reason).__name__.lower()
