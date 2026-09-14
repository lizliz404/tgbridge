"""Cross-platform proxy inspection and network error classification."""

import socket
import time
import urllib.error
import urllib.parse
import urllib.request

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


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

