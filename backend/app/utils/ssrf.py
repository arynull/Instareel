"""SSRF guard for fetching admin-configured URLs (proxy list sources).

Validates the target before every request hop: http(s) scheme only, and the
hostname must resolve exclusively to globally-routable IPs. Redirects are
followed manually with each hop re-validated — never httpx
follow_redirects=True, which would let an allow-listed URL bounce to
169.254.169.254. Bodies stream with a hard byte cap so a malicious server
can't blow memory before the size check runs.

The single `not addr.is_global` test (plus an explicit multicast check —
multicast reports `is_global == True` on this Python) covers private /
loopback / link-local / multicast / reserved / unspecified AND shared
address space (CGNAT 100.64.0.0/10, which `is_private` misses) and
documentation ranges.

Known limitation: DNS is resolved at validation time, so a DNS-rebinding
TOCTOU between check and connect is theoretically possible. This raises the
bar for the realistic threat here (a malicious or compromised list URL, or
an open-redirect chain); it is not a network sandbox.
"""
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

MAX_REDIRECTS = 5
_REDIRECT_CODES = (301, 302, 303, 307, 308)


def _host_ips(host: str) -> list[str]:
    host = host.strip().strip("[]")
    try:
        # Literal IP — no DNS involved.
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve host {host!r}: {exc}") from exc
    return list({info[4][0] for info in infos})


def validate_fetch_url(url: str) -> str:
    """Return the URL unchanged if it is safe to fetch, else raise ValueError."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("URL must use http(s)")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no host")
    for ip in _host_ips(host):
        addr = ipaddress.ip_address(ip)
        if addr.is_multicast or not addr.is_global:
            raise ValueError(f"URL resolves to non-public IP {ip}")
    return url


def fetch_url_guarded(
    url: str, *, timeout: int = 30, max_bytes: int = 1024 * 1024
) -> tuple[str, str]:
    """GET with per-hop SSRF validation. Returns (final_url, text).

    Raises ValueError on unsafe targets, redirect loops, or oversize bodies;
    httpx.HTTPError (via raise_for_status) on HTTP failures.
    """
    import httpx

    current = url
    seen = set()
    # trust_env=False: an HTTP(S)_PROXY in the environment would route the
    # request through the proxy, letting it connect to hosts we just
    # rejected — a full bypass of this guard.
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            if current in seen:
                raise ValueError("redirect loop detected")
            seen.add(current)
            validate_fetch_url(current)
            with client.stream("GET", current, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status_code in _REDIRECT_CODES:
                    loc = resp.headers.get("location")
                    if not loc:
                        raise ValueError("redirect without Location header")
                    current = urljoin(current, loc)
                else:
                    resp.raise_for_status()
                    # Stream with a hard cap — never buffer an unbounded body.
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes(65536):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("response exceeds size cap")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    return str(resp.url), body.decode(resp.encoding or "utf-8", errors="replace")
            # Redirect hop: the stream is closed; the for loop re-validates
            # the next URL from the top.
    raise ValueError("too many redirects")
