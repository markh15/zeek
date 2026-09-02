"""Networking primitives for the SISMET downloader.

Only the Python standard library is used.  SISMET (sismet.cooxupe.com.br) is
reachable from Brazilian IP space only, so every request can optionally be
routed through a local SOCKS5 proxy.  Hostname resolution happens *at the
proxy* (the ``socks5h`` behaviour) so DNS never leaks past the tunnel and the
name is resolved from the exit node's point of view.
"""

from __future__ import annotations

import gzip
import http.client
import socket
import ssl
import struct
import time
import urllib.parse
import zlib
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

SOCKS5_ERRORS = {
    0x01: "general SOCKS server failure",
    0x02: "connection not allowed by ruleset",
    0x03: "network unreachable",
    0x04: "host unreachable",
    0x05: "connection refused",
    0x06: "TTL expired",
    0x07: "command not supported",
    0x08: "address type not supported",
}


class NetworkError(Exception):
    """Any transport-level failure (proxy, TLS, socket, HTTP status)."""


class ProxyError(NetworkError):
    """The SOCKS5 proxy refused or mishandled the connection."""


@dataclass
class ProxyConfig:
    """A SOCKS5 proxy, e.g. the local tunnel on 127.0.0.1:1080."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 1080
    username: Optional[str] = None
    password: Optional[str] = None

    def describe(self) -> str:
        if not self.enabled:
            return "direct (no proxy)"
        auth = f"{self.username}@" if self.username else ""
        return f"socks5h://{auth}{self.host}:{self.port}"


@dataclass
class Response:
    status: int
    reason: str
    headers: dict
    body: bytes
    url: str

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def text(self) -> str:
        """Decode the body, honouring the charset the server declared.

        SISMET is a Brazilian system and has been seen serving latin-1 as well
        as utf-8, so fall back rather than raising on undecodable bytes.
        """
        charset = ""
        ctype = self.content_type
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip().strip('"')
        for enc in (charset, "utf-8", "latin-1"):
            if not enc:
                continue
            try:
                return self.body.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return self.body.decode("utf-8", errors="replace")


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise ProxyError("proxy closed the connection mid-handshake")
        buf += chunk
    return buf


def socks5_connect(
    proxy: ProxyConfig, dest_host: str, dest_port: int, timeout: float = 30.0
) -> socket.socket:
    """Open a TCP connection to ``dest_host:dest_port`` via a SOCKS5 proxy.

    Implements RFC 1928 (CONNECT) plus RFC 1929 (username/password auth).  The
    destination is sent as a domain name (ATYP 0x03) rather than an address, so
    the proxy resolves it -- this is what ``socks5h`` means in curl.
    """
    try:
        sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    except OSError as exc:
        raise ProxyError(
            f"cannot reach SOCKS5 proxy at {proxy.host}:{proxy.port} ({exc}). "
            "Is the tunnel running?"
        ) from exc

    try:
        sock.settimeout(timeout)
        methods = [0x00]
        if proxy.username:
            methods.append(0x02)
        sock.sendall(bytes([0x05, len(methods)]) + bytes(methods))

        greeting = _recv_exactly(sock, 2)
        if greeting[0] != 0x05:
            raise ProxyError("proxy did not answer with SOCKS version 5")
        method = greeting[1]
        if method == 0xFF:
            raise ProxyError("proxy rejected every offered authentication method")
        if method == 0x02:
            if not proxy.username:
                raise ProxyError("proxy demands a username/password we do not have")
            user = proxy.username.encode("utf-8")
            pwd = (proxy.password or "").encode("utf-8")
            sock.sendall(bytes([0x01, len(user)]) + user + bytes([len(pwd)]) + pwd)
            auth_reply = _recv_exactly(sock, 2)
            if auth_reply[1] != 0x00:
                raise ProxyError("proxy rejected the username/password")
        elif method != 0x00:
            raise ProxyError(f"proxy selected unsupported auth method {method:#04x}")

        host_bytes = dest_host.encode("idna")
        request = (
            bytes([0x05, 0x01, 0x00, 0x03, len(host_bytes)])
            + host_bytes
            + struct.pack("!H", dest_port)
        )
        sock.sendall(request)

        reply = _recv_exactly(sock, 4)
        if reply[1] != 0x00:
            detail = SOCKS5_ERRORS.get(reply[1], f"unknown error {reply[1]:#04x}")
            raise ProxyError(f"proxy could not connect to {dest_host}:{dest_port}: {detail}")

        atyp = reply[3]
        if atyp == 0x01:
            _recv_exactly(sock, 4)
        elif atyp == 0x03:
            length = _recv_exactly(sock, 1)[0]
            _recv_exactly(sock, length)
        elif atyp == 0x04:
            _recv_exactly(sock, 16)
        else:
            raise ProxyError(f"proxy replied with unsupported address type {atyp:#04x}")
        _recv_exactly(sock, 2)  # bound port, unused
        return sock
    except Exception:
        sock.close()
        raise


class _Socks5HTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection whose TCP socket comes from a SOCKS5 tunnel."""

    def __init__(self, host, port, proxy: ProxyConfig, context, timeout):
        super().__init__(host, port, timeout=timeout, context=context)
        self._proxy = proxy

    def connect(self):  # noqa: D102 - stdlib override
        raw = socks5_connect(self._proxy, self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class _Socks5HTTPConnection(http.client.HTTPConnection):
    """Plain-HTTP variant of the above (SISMET is HTTPS, but be complete)."""

    def __init__(self, host, port, proxy: ProxyConfig, timeout):
        super().__init__(host, port, timeout=timeout)
        self._proxy = proxy

    def connect(self):  # noqa: D102 - stdlib override
        self.sock = socks5_connect(self._proxy, self.host, self.port, self.timeout)


def _decompress(body: bytes, encoding: str) -> bytes:
    encoding = (encoding or "").lower()
    if encoding == "gzip":
        try:
            return gzip.decompress(body)
        except OSError:
            return body
    if encoding == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            try:
                return zlib.decompress(body, -zlib.MAX_WBITS)
            except zlib.error:
                return body
    return body


@dataclass
class HttpClient:
    """A very small HTTP client: SOCKS5-aware, retrying, redirect-following."""

    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    timeout: float = 45.0
    retries: int = 3
    backoff: float = 2.0
    verify_tls: bool = True
    user_agent: str = DEFAULT_USER_AGENT
    log = None  # optional callable(str)

    def _note(self, message: str) -> None:
        if self.log:
            self.log(message)

    def _ssl_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if not self.verify_tls:
            # Opt-in only; the TUI labels this clearly as insecure.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def _open(self, scheme: str, host: str, port: int):
        if self.proxy.enabled:
            if scheme == "https":
                return _Socks5HTTPSConnection(
                    host, port, self.proxy, self._ssl_context(), self.timeout
                )
            return _Socks5HTTPConnection(host, port, self.proxy, self.timeout)
        if scheme == "https":
            return http.client.HTTPSConnection(
                host, port, timeout=self.timeout, context=self._ssl_context()
            )
        return http.client.HTTPConnection(host, port, timeout=self.timeout)

    def get(self, url: str, params: Optional[dict] = None, max_redirects: int = 5) -> Response:
        """GET ``url``, retrying transient failures with exponential backoff."""
        if params:
            separator = "&" if urllib.parse.urlparse(url).query else "?"
            url = url + separator + urllib.parse.urlencode(params, doseq=True)

        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                return self._get_once(url, max_redirects)
            except (NetworkError, OSError, http.client.HTTPException, ssl.SSLError) as exc:
                last_error = exc
                if isinstance(exc, ProxyError) and "cannot reach SOCKS5 proxy" in str(exc):
                    raise  # a missing tunnel will not fix itself; fail fast
                if attempt == self.retries:
                    break
                delay = self.backoff ** attempt
                self._note(f"  attempt {attempt} failed ({exc}); retrying in {delay:.0f}s")
                time.sleep(delay)
        raise NetworkError(f"GET {url} failed after {self.retries} attempts: {last_error}")

    def _get_once(self, url: str, max_redirects: int) -> Response:
        current = url
        for _ in range(max_redirects + 1):
            parts = urllib.parse.urlsplit(current)
            scheme = parts.scheme or "https"
            host = parts.hostname
            if not host:
                raise NetworkError(f"malformed URL: {current}")
            port = parts.port or (443 if scheme == "https" else 80)
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query

            conn = self._open(scheme, host, port)
            try:
                conn.request(
                    "GET",
                    path,
                    headers={
                        "Host": parts.netloc,
                        "User-Agent": self.user_agent,
                        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                        "Accept-Encoding": "gzip, deflate",
                        "Connection": "close",
                    },
                )
                raw = conn.getresponse()
                headers = {k.lower(): v for k, v in raw.getheaders()}
                body = _decompress(raw.read(), headers.get("content-encoding", ""))
                status, reason = raw.status, raw.reason
            finally:
                conn.close()

            if status in (301, 302, 303, 307, 308) and "location" in headers:
                current = urllib.parse.urljoin(current, headers["location"])
                continue

            if status >= 400:
                raise NetworkError(f"HTTP {status} {reason} for {current}")
            return Response(status, reason, headers, body, current)

        raise NetworkError(f"too many redirects starting at {url}")


def check_proxy(proxy: ProxyConfig, timeout: float = 5.0) -> str:
    """Probe the proxy itself so the TUI can report a precise reason for failure."""
    if not proxy.enabled:
        return "proxy disabled - connecting directly"
    sock = socks5_connect(proxy, "example.com", 80, timeout=timeout)
    sock.close()
    return f"SOCKS5 handshake OK via {proxy.host}:{proxy.port}"
