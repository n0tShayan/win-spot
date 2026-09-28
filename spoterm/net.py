"""A minimal HTTPS JSON client on http.client.

Each thread keeps its own keep-alive connection per host, so concurrent worker
threads never share a socket and need no locks. TLS is verified with the system
trust store; stale keep-alive sockets are reopened transparently.
"""

import base64
import http.client
import json
import os
import ssl
import threading
import time
import zlib
from urllib.parse import unquote, urlencode, urlsplit

TIMEOUT = 10.0            # seconds, applies to connect and to each socket read
RETRIES = 2               # extra attempts for idempotent requests (5xx, timeouts, resets)
BACKOFF = 0.5             # seconds, doubled per attempt
MAX_RETRY_AFTER = 5.0     # longer 429 waits are surfaced to the caller instead of blocking
MAX_BODY = 16 << 20       # bytes; a larger (or larger decompressed) response is refused
USER_AGENT = "spoterm"
_IDEMPOTENT = frozenset(("GET", "HEAD", "PUT", "DELETE"))
# Errors that mean a reused keep-alive socket was closed by the server before it answered.
_STALE = (http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError,
          BrokenPipeError)

_ctx: ssl.SSLContext | None = None
_ctx_lock = threading.Lock()


def _ssl_context() -> ssl.SSLContext:
    global _ctx
    with _ctx_lock:
        if _ctx is None:
            ctx = ssl.create_default_context()   # verifies certificates and hostnames
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            _ctx = ctx
        return _ctx


class ApiError(Exception):
    """A non-2xx response. `message` is the server's own error text when it sent one."""

    def __init__(self, status: int, message: str, reason: str = "", retry_after: float = 0.0):
        super().__init__(f"HTTP {status}: {message}" if message else f"HTTP {status}")
        self.status = status
        self.message = message
        self.reason = reason
        self.retry_after = retry_after

    @property
    def http_status(self) -> int:   # spotipy's name, still used by callers
        return self.status


def _json(data: bytes):
    """Decoded JSON, or None when it isn't valid (or nests deeply enough to blow the stack)."""
    try:
        return json.loads(data)
    except (ValueError, RecursionError):
        return None


def _gunzip(data: bytes) -> bytes:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(data, MAX_BODY + 1)
    except zlib.error as e:
        raise http.client.HTTPException(f"bad gzip response: {e}") from None
    if len(out) > MAX_BODY:
        raise http.client.HTTPException("response too large")
    return out


def _error(status: int, body: bytes, headers) -> ApiError:
    message = reason = ""
    data = _json(body)
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):           # Web API: {"error": {"status", "message", "reason"}}
            message, reason = str(err.get("message") or ""), str(err.get("reason") or "")
        elif isinstance(err, str):          # OAuth: {"error": "invalid_grant", "error_description"}
            message, reason = str(data.get("error_description") or err), err
    if not message:
        message = http.client.responses.get(status, "")
    try:
        retry_after = float(headers.get("Retry-After") or 0)
    except ValueError:
        retry_after = 0.0
    return ApiError(status, message, reason, retry_after)


def _proxy_for(host: str) -> tuple[str, int, dict] | None:
    """HTTPS_PROXY support (plain http:// proxies), honouring NO_PROXY suffixes."""
    url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not url:
        return None
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    for entry in filter(None, (e.strip().lstrip(".") for e in no_proxy.split(","))):
        if entry == "*" or host == entry or host.endswith("." + entry):
            return None
    p = urlsplit(url if "://" in url else "http://" + url)
    headers = {}
    if p.username:
        cred = f"{unquote(p.username)}:{unquote(p.password or '')}".encode()
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(cred).decode()
    return p.hostname or "", p.port or 8080, headers


class Client:
    """JSON over HTTPS to one host. Safe to share between threads."""

    def __init__(self, host: str, timeout: float = TIMEOUT):
        self.host = host
        self.timeout = timeout
        self._local = threading.local()

    def _conn(self) -> tuple[http.client.HTTPSConnection, bool]:
        """This thread's connection, and whether it has been used before."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn, True
        proxy = _proxy_for(self.host)
        if proxy:
            conn = http.client.HTTPSConnection(proxy[0], proxy[1], timeout=self.timeout,
                                               context=_ssl_context())
            conn.set_tunnel(self.host, 443, headers=proxy[2])
        else:
            conn = http.client.HTTPSConnection(self.host, timeout=self.timeout,
                                               context=_ssl_context())
        self._local.conn = conn
        return conn, False

    def _drop(self) -> None:
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            conn.close()

    def close(self) -> None:
        """Close this thread's connection."""
        self._drop()

    def request(self, method: str, path: str, *, params: dict | None = None,
                json_body=None, form: dict | None = None, headers: dict | None = None):
        """Send a request and return the decoded JSON body (None when empty). Raises ApiError."""
        if path.startswith("https://"):     # pagination links are absolute URLs
            u = urlsplit(path)
            if u.hostname != self.host:
                raise ValueError(f"refusing to follow a link to another host: {u.hostname}")
            path = u.path + ("?" + u.query if u.query else "")
        if params:
            q = urlencode({k: v for k, v in params.items() if v is not None})
            path += ("&" if "?" in path else "?") + q
        hdrs = {"Accept": "application/json", "Accept-Encoding": "gzip", "User-Agent": USER_AGENT}
        body = None
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":")).encode()
            hdrs["Content-Type"] = "application/json"
        elif form is not None:
            body = urlencode(form).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        if headers:
            hdrs.update(headers)

        idempotent = method in _IDEMPOTENT
        attempt = 0
        stale_retry = True
        while True:
            conn, reused = self._conn()
            try:
                conn.request(method, path, body=body, headers=hdrs)
                resp = conn.getresponse()
                data = resp.read(MAX_BODY + 1)
            except ssl.SSLCertVerificationError:
                self._drop()
                raise
            except _STALE:
                self._drop()
                # The server closed an idle socket before reading the request: always safe to resend once.
                if reused and stale_retry:
                    stale_retry = False
                    continue
                if not idempotent or attempt >= RETRIES:
                    raise
            except (TimeoutError, OSError, http.client.HTTPException):
                self._drop()
                if not idempotent or attempt >= RETRIES:
                    raise
            else:
                if len(data) > MAX_BODY:
                    self._drop()        # the rest is still unread on this socket
                    raise http.client.HTTPException("response too large")
                if resp.will_close:
                    self._drop()
                if resp.getheader("Content-Encoding", "").lower() == "gzip" and data:
                    data = _gunzip(data)
                status = resp.status
                if 200 <= status < 300:
                    if not data:
                        return None     # 202/204: player commands answer with an empty body
                    return _json(data)
                err = _error(status, data, resp.headers)
                if status == 429 and 0 < err.retry_after <= MAX_RETRY_AFTER and attempt < RETRIES:
                    time.sleep(err.retry_after)     # the request was rejected, so any method may retry
                    attempt += 1
                    continue
                if status < 500 or not idempotent or attempt >= RETRIES:
                    raise err
            time.sleep(BACKOFF * 2 ** attempt)
            attempt += 1

