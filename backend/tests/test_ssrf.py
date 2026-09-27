"""SSRF guard exhaustiveness: is_global coverage, streaming cap, redirects."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.utils import ssrf


# ---------------------------------------------------------------------------
# validate_fetch_url
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ip", [
    "10.0.0.1", "172.16.5.4", "192.168.1.1",          # RFC1918
    "127.0.0.1",                                       # loopback
    "169.254.169.254",                                 # link-local (cloud metadata)
    "224.0.0.1",                                       # multicast
    "0.0.0.0",                                         # unspecified
    "240.0.0.1",                                       # reserved
    "100.64.0.5", "100.127.255.254",                   # CGNAT (is_private misses these)
    "192.0.2.1", "203.0.113.9", "198.51.100.7",        # documentation ranges
])
def test_literal_nonpublic_ipv4_rejected(ip):
    with pytest.raises(ValueError, match="non-public IP"):
        ssrf.validate_fetch_url(f"http://{ip}/list.txt")


@pytest.mark.parametrize("ip", ["::1", "fe80::1", "ff02::1", "2001:db8::1", "::"])
def test_literal_nonpublic_ipv6_rejected(ip):
    with pytest.raises(ValueError, match="non-public IP"):
        ssrf.validate_fetch_url(f"http://[{ip}]/list.txt")


def test_public_ipv4_and_ipv6_accepted(monkeypatch):
    monkeypatch.setattr(ssrf, "_host_ips", lambda host: ["93.184.216.34"])
    assert ssrf.validate_fetch_url("http://example.com/list.txt").startswith("http")
    monkeypatch.setattr(ssrf, "_host_ips", lambda host: ["2606:2800:220:1:248:1893:25c8:1946"])
    assert ssrf.validate_fetch_url("http://example.com/list.txt").startswith("http")


def test_mixed_dns_one_bad_ip_rejects(monkeypatch):
    # A hostname resolving to public + private: one bad apple spoils it.
    monkeypatch.setattr(ssrf, "_host_ips", lambda host: ["93.184.216.34", "10.9.9.9"])
    with pytest.raises(ValueError, match="non-public IP"):
        ssrf.validate_fetch_url("http://example.com/list.txt")


def test_cgnat_via_dns_rejected(monkeypatch):
    monkeypatch.setattr(ssrf, "_host_ips", lambda host: ["100.64.0.5"])
    with pytest.raises(ValueError, match="non-public IP"):
        ssrf.validate_fetch_url("http://example.com/list.txt")


@pytest.mark.parametrize("url", [
    "ftp://93.184.216.34/list.txt",
    "file:///etc/passwd",
    "gopher://93.184.216.34/",
])
def test_non_http_scheme_rejected(url):
    with pytest.raises(ValueError, match="http\\(s\\)"):
        ssrf.validate_fetch_url(url)


def test_decimal_ip_trick_rejected():
    # http://2130706433/ == http://127.0.0.1/ in some resolvers.
    with pytest.raises(ValueError):
        ssrf.validate_fetch_url("http://2130706433/")


def test_unresolvable_host_rejected(monkeypatch):
    import socket

    def _fail(host, *a, **k):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    with pytest.raises(ValueError, match="cannot resolve"):
        ssrf.validate_fetch_url("http://example.com/list.txt")


# ---------------------------------------------------------------------------
# fetch_url_guarded transport behavior (local test server)
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/ok":
            body = b"proxy1\nproxy2\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/big":
            # 2 MiB body advertised honestly; cap is 1 KiB in the test.
            self.send_response(200)
            self.send_header("Content-Length", str(2 * 1024 * 1024))
            self.end_headers()
            chunk = b"x" * 65536
            for _ in range(32):
                self.wfile.write(chunk)
        elif self.path == "/loop-a":
            self.send_response(302)
            self.send_header("Location", "/loop-b")
            self.end_headers()
        elif self.path == "/loop-b":
            self.send_response(302)
            self.send_header("Location", "/loop-a")
            self.end_headers()
        elif self.path.startswith("/chain-"):
            n = int(self.path.rsplit("-", 1)[1])
            self.send_response(302)
            self.send_header("Location", f"/chain-{n + 1}")
            self.end_headers()
        elif self.path == "/to-evil":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
        elif self.path == "/no-location":
            self.send_response(302)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture()
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture()
def lax_validate(monkeypatch):
    """Allow the local test server through, validate everything else for real."""
    real = ssrf.validate_fetch_url

    def _lax(url):
        if "127.0.0.1" in url:
            return url
        return real(url)

    monkeypatch.setattr(ssrf, "validate_fetch_url", _lax)


def test_happy_path(server, lax_validate):
    final, text = ssrf.fetch_url_guarded(server + "/ok")
    assert text == "proxy1\nproxy2\n"
    assert final.endswith("/ok")


def test_oversize_body_rejected_while_streaming(server, lax_validate):
    with pytest.raises(ValueError, match="exceeds size cap"):
        ssrf.fetch_url_guarded(server + "/big", max_bytes=1024)


def test_redirect_loop_detected(server, lax_validate):
    with pytest.raises(ValueError, match="redirect loop"):
        ssrf.fetch_url_guarded(server + "/loop-a")


def test_too_many_redirects(server, lax_validate):
    with pytest.raises(ValueError, match="too many redirects"):
        ssrf.fetch_url_guarded(server + "/chain-0")


def test_redirect_without_location(server, lax_validate):
    with pytest.raises(ValueError, match="without Location"):
        ssrf.fetch_url_guarded(server + "/no-location")


def test_per_hop_revalidation_blocks_evil_redirect(server, lax_validate):
    # First hop is the (allowed) local server; it bounces to link-local.
    with pytest.raises(ValueError, match="non-public IP"):
        ssrf.fetch_url_guarded(server + "/to-evil")
