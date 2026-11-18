"""A deliberately narrow HTTP client for source polling (spec section 2).

* Only URLs under a source's permitted origins and path prefixes are fetched,
  including every redirect hop.
* Hostnames are resolved once; every resolved address must be public, and
  the connection is pinned to a checked address, which closes the DNS
  rebinding gap. TLS still verifies the certificate against the hostname.
* Response bytes, decompressed bytes, total time and redirects are bounded.
* Conditional requests use ETag / Last-Modified; 429 and 503 report
  Retry-After to the poller.

The client does not use proxies. Deployments that must egress through a proxy
should supply their own transport to :class:`~recap.ingest.poller.Poller` and
enforce the same rules at the proxy.
"""

from __future__ import annotations

import email.utils
import http.client
import ipaddress
import socket
import ssl
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.parse import unquote, urljoin, urlsplit

from ..config import normalize_origin

USER_AGENT = "recap-ingest/0.1 (+source polling; conditional GET)"


class FetchError(Exception):
    def __init__(self, kind: str, message: str = ""):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind


@dataclass(frozen=True)
class OriginPolicy:
    origins: frozenset[str]
    paths: tuple[str, ...]

    @classmethod
    def of(cls, origins: Iterable[str], paths: Iterable[str]) -> "OriginPolicy":
        return cls(frozenset(normalize_origin(o) for o in origins), tuple(paths))

    def permits(self, url: str) -> bool:
        try:
            origin = normalize_origin(url)
        except ValueError:
            return False
        if origin not in self.origins:
            return False
        parts = urlsplit(url)
        if parts.username or parts.password:
            return False
        path = parts.path or "/"
        decoded = unquote(path)
        if any(seg in ("..", ".") for seg in decoded.split("/")) or "\\" in decoded:
            return False
        return any(path.startswith(prefix) for prefix in self.paths)


@dataclass
class FetchResult:
    status: int
    body: bytes = b""
    final_url: str = ""
    etag: str | None = None
    last_modified: str | None = None
    retry_after: int | None = None
    content_type: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


_EMBEDDING_V6 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "64:ff9b::/96",      # NAT64 well-known prefix
        "64:ff9b:1::/48",    # NAT64 local-use prefix
        "::/96",             # IPv4-compatible (deprecated)
        "::ffff:0:0:0/96",   # IPv4-translated (SIIT)
        "2001::/32",         # Teredo
    )
)


def address_allowed(ip: str, allow_private: bool = False) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif addr.sixtofour is not None:
            if not allow_private and not addr.sixtofour.is_global:
                return False
        elif not allow_private and any(addr in net for net in _EMBEDDING_V6):
            return False  # these forms can route to an embedded, possibly private, IPv4 address
    if allow_private:
        return not (addr.is_multicast or addr.is_unspecified)
    return addr.is_global and not addr.is_multicast


def parse_retry_after(value: str | None, now: float) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return max(0, int(when.timestamp() - now))


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float, context: ssl.SSLContext):
        super().__init__(host, port, timeout=timeout, context=context)
        self._ip = ip
        self._ctx = context

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._ctx.wrap_socket(sock, server_hostname=self.host)


Resolver = Callable[[str, int], list[str]]


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


class SafeFetcher:
    def __init__(
        self,
        *,
        timeout: float = 10.0,
        total_timeout: float = 30.0,
        max_redirects: int = 3,
        max_decompressed: int = 8_000_000,
        resolver: Resolver = system_resolver,
        allow_private: bool = False,
        ssl_context: ssl.SSLContext | None = None,
        user_agent: str = USER_AGENT,
    ):
        self.timeout = timeout
        self.total_timeout = total_timeout
        self.max_redirects = max_redirects
        self.max_decompressed = max_decompressed
        self.resolver = resolver
        self.allow_private = allow_private
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.user_agent = user_agent

    def _pick_address(self, host: str, port: int) -> str:
        try:
            addresses = self.resolver(host, port)
        except OSError as exc:
            raise FetchError("dns", str(exc)) from None
        if not addresses:
            raise FetchError("dns", f"no addresses for {host}")
        for ip in addresses:
            if not address_allowed(ip, self.allow_private):
                raise FetchError("blocked_destination", f"{host} resolves to non-public address {ip}")
        return addresses[0]

    def get(
        self,
        url: str,
        policy: OriginPolicy,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        max_bytes: int = 2_000_000,
    ) -> FetchResult:
        started = time.monotonic()
        current = url
        for _hop in range(self.max_redirects + 1):
            if not policy.permits(current):
                raise FetchError("not_permitted", current)
            result = self._get_once(current, etag, last_modified, max_bytes, started)
            if result.status in (301, 302, 303, 307, 308):
                location = result.headers.get("location")
                if not location:
                    raise FetchError("bad_redirect", "redirect without Location")
                current = urljoin(current, location)
                continue
            result.final_url = current
            return result
        raise FetchError("too_many_redirects", url)

    def _get_once(self, url: str, etag: str | None, last_modified: str | None, max_bytes: int, started: float) -> FetchResult:
        parts = urlsplit(url)
        host = parts.hostname or ""
        https = parts.scheme == "https"
        port = parts.port or (443 if https else 80)
        ip = self._pick_address(host, port)
        remaining = self.total_timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise FetchError("timeout", "total time exceeded")
        timeout = min(self.timeout, remaining)
        if https:
            conn: http.client.HTTPConnection = _PinnedHTTPSConnection(host, port, ip, timeout, self.ssl_context)
        else:
            conn = _PinnedHTTPConnection(host, port, ip, timeout)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        headers = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate", "Accept": "*/*"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            body = b""
            if resp.status == 200:
                body = self._read_bounded(resp, max_bytes, started)
                body = self._decode(body, hdrs.get("content-encoding"))
            else:
                resp.read(65536)
        except FetchError:
            raise
        except ssl.SSLError as exc:
            raise FetchError("tls", str(exc)) from None
        except (socket.timeout, TimeoutError) as exc:
            raise FetchError("timeout", str(exc)) from None
        except (OSError, http.client.HTTPException) as exc:
            raise FetchError("connection", str(exc)) from None
        finally:
            conn.close()
        retry_after = parse_retry_after(hdrs.get("retry-after"), time.time()) if resp.status in (429, 503) else None
        return FetchResult(
            status=resp.status,
            body=body,
            final_url=url,
            etag=hdrs.get("etag"),
            last_modified=hdrs.get("last-modified"),
            retry_after=retry_after,
            content_type=hdrs.get("content-type"),
            headers=hdrs,
        )

    def _read_bounded(self, resp: http.client.HTTPResponse, max_bytes: int, started: float) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            if time.monotonic() - started > self.total_timeout:
                raise FetchError("timeout", "body took too long")
            chunk = resp.read1(65536)  # returns what has arrived, so a trickling body hits the time limit
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise FetchError("too_large", f"body exceeds {max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    def _decode(self, body: bytes, encoding: str | None) -> bytes:
        if not encoding or encoding.strip().lower() == "identity":
            return body
        enc = encoding.strip().lower()
        if enc not in ("gzip", "x-gzip", "deflate"):
            raise FetchError("bad_encoding", enc)
        d = zlib.decompressobj(wbits=47)  # zlib or gzip header, detected automatically
        try:
            out = d.decompress(body, self.max_decompressed + 1)
        except zlib.error as exc:
            raise FetchError("bad_encoding", str(exc)) from None
        if len(out) > self.max_decompressed or d.unconsumed_tail:
            raise FetchError("decompression_limit", f"decompressed body exceeds {self.max_decompressed} bytes")
        return out
