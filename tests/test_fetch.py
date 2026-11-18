import gzip
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from recap.ingest.fetch import FetchError, OriginPolicy, SafeFetcher, address_allowed, parse_retry_after


@pytest.mark.parametrize(
    "ip, allowed",
    [
        ("93.184.216.34", True),
        ("2606:4700::1111", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("::1", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("::ffff:127.0.0.1", False),
        ("224.0.0.1", False),
        ("0.0.0.0", False),
    ],
)
def test_address_allowed(ip, allowed):
    assert address_allowed(ip) is allowed


def test_origin_policy():
    p = OriginPolicy.of(["https://feeds.example.org"], ["/lumen/"])
    assert p.permits("https://feeds.example.org/lumen/feed.atom")
    assert p.permits("https://FEEDS.example.org:443/lumen/x")
    assert not p.permits("https://feeds.example.org:8443/lumen/x")
    assert not p.permits("http://feeds.example.org/lumen/x")
    assert not p.permits("https://feeds.example.org/other/x")
    assert not p.permits("https://feeds.example.org/lumen/../admin")
    assert not p.permits("https://feeds.example.org/lumen/%2e%2e/admin")
    assert not p.permits("https://user:pw@feeds.example.org/lumen/x")
    assert not p.permits("https://feeds.example.org.evil.test/lumen/x")
    assert not p.permits("file:///etc/passwd")


def test_private_resolution_is_blocked():
    f = SafeFetcher(resolver=lambda host, port: ["10.0.0.5"])
    with pytest.raises(FetchError) as exc:
        f.get("https://feeds.example.org/lumen/x", OriginPolicy.of(["https://feeds.example.org"], ["/"]))
    assert exc.value.kind == "blocked_destination"
    mixed = SafeFetcher(resolver=lambda host, port: ["93.184.216.34", "127.0.0.1"])
    with pytest.raises(FetchError):
        mixed.get("https://feeds.example.org/lumen/x", OriginPolicy.of(["https://feeds.example.org"], ["/"]))


def test_retry_after_parsing():
    assert parse_retry_after("120", 0) == 120
    assert parse_retry_after("Wed, 18 Nov 2026 10:02:00 GMT", 1794996000) == 120
    assert parse_retry_after("soon", 0) is None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/feed":
            if self.headers.get("If-None-Match") == '"v1"':
                return self._reply(304)
            return self._reply(200, b"hello", {"ETag": '"v1"'})
        if self.path == "/big":
            return self._reply(200, b"x" * 5000)
        if self.path == "/bomb":
            return self._reply(200, gzip.compress(b"\0" * 2_000_000), {"Content-Encoding": "gzip"})
        if self.path == "/gz":
            return self._reply(200, gzip.compress(b"compressed ok"), {"Content-Encoding": "gzip"})
        if self.path == "/redirect-in":
            return self._reply(302, headers={"Location": "/feed"})
        if self.path == "/redirect-out":
            return self._reply(302, headers={"Location": "http://elsewhere.example:1/feed"})
        if self.path == "/limited":
            return self._reply(429, headers={"Retry-After": "120"})
        return self._reply(404)


@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()


def test_local_server_behaviour(server):
    origin = f"http://localhost:{server}"
    policy = OriginPolicy.of([origin], ["/"])
    f = SafeFetcher(resolver=lambda host, port: ["127.0.0.1"], allow_private=True, max_decompressed=100_000)
    r = f.get(f"{origin}/feed", policy)
    assert r.status == 200 and r.body == b"hello" and r.etag == '"v1"'
    assert f.get(f"{origin}/feed", policy, etag='"v1"').status == 304
    assert f.get(f"{origin}/redirect-in", policy).body == b"hello"
    assert f.get(f"{origin}/gz", policy).body == b"compressed ok"
    assert f.get(f"{origin}/limited", policy).retry_after == 120
    with pytest.raises(FetchError) as exc:
        f.get(f"{origin}/redirect-out", policy)
    assert exc.value.kind == "not_permitted"
    with pytest.raises(FetchError) as exc:
        f.get(f"{origin}/big", policy, max_bytes=1000)
    assert exc.value.kind == "too_large"
    with pytest.raises(FetchError) as exc:
        f.get(f"{origin}/bomb", policy)
    assert exc.value.kind == "decompression_limit"
    # Without the test-only private override, loopback is refused.
    with pytest.raises(FetchError) as exc:
        SafeFetcher(resolver=lambda host, port: ["127.0.0.1"]).get(f"{origin}/feed", policy)
    assert exc.value.kind == "blocked_destination"
