"""SSRF guard for fetching admin-configured URLs (proxy list sources).

Validates the target before every request hop: http(s) scheme only, and the
hostname must resolve exclusively to public IPs (no private / loopback /
link-local / multicast / reserved / unspecified). Redirects are followed
manually with each hop re-validated — never httpx follow_redirects=True,
which would let an allow-listed URL bounce to 169.254.169.254.

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
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
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
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            validate_fetch_url(current)
            resp = client.get(current, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code in _REDIRECT_CODES:
                loc = resp.headers.get("location")
                if not loc:
                    raise ValueError("redirect without Location header")
                current = urljoin(current, loc)
                continue
            resp.raise_for_status()
            if len(resp.content) > max_bytes:
                raise ValueError("response exceeds size cap")
            return str(resp.url), resp.text
    raise ValueError("too many redirects")
