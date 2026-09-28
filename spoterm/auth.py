"""Spotify OAuth: Authorization Code login with PKCE, a token file, thread-safe refresh.

PKCE means no client secret is needed. If SPOTIPY_CLIENT_SECRET is set it is only
used to refresh tokens that were issued to the confidential client (older caches).
"""

import base64
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .config import SCOPE, Settings
from .net import ApiError, Client

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
ACCOUNTS_HOST = "accounts.spotify.com"
REFRESH_MARGIN = 60        # seconds before expiry to refresh
LOGIN_TIMEOUT = 300        # seconds to wait for the browser redirect
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


log = logging.getLogger(__name__)


class AuthError(Exception):
    pass


class StateMismatch(AuthError):
    pass


@dataclass(frozen=True, slots=True)
class Token:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str
    pkce: bool             # issued to the public (PKCE) client, so refresh without the secret

    def to_json(self) -> dict:
        # Same keys as spotipy's cache, so either can read the other's file.
        return {"access_token": self.access_token, "token_type": "Bearer",
                "expires_in": max(0, int(self.expires_at - time.time())),
                "expires_at": int(self.expires_at), "refresh_token": self.refresh_token,
                "scope": self.scope, "pkce": self.pkce}


def _read_token(path) -> Token | None:
    path = Path(path)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return Token(str(d["access_token"]), str(d.get("refresh_token") or ""),
                     float(d.get("expires_at") or 0), str(d.get("scope") or ""),
                     bool(d.get("pkce", False)))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _write_token(path, token: Token) -> None:
    """Atomic write, owner-only. mkstemp creates the file 0600 on POSIX; on Windows it
    inherits the ACL of the per-user %APPDATA% folder."""
    import tempfile
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".token-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(token.to_json(), f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>spoterm</title>
<style>body{{font:16px system-ui,sans-serif;background:#121212;color:#eee;display:grid;
place-items:center;height:90vh}}h1{{color:{color};font-weight:600}}</style></head>
<body><div><h1>{title}</h1><p>{body}</p></div></body></html>"""


class Auth:
    """Holds the current token; token() and refresh() may be called from any thread."""

    def __init__(self, settings: Settings, scope: str = SCOPE, check_scope: bool = True):
        self.settings = settings
        self.scope = scope
        self.check_scope = check_scope      # tests reuse an older cache with fewer scopes
        self._http = Client(ACCOUNTS_HOST)
        self._lock = threading.Lock()
        self._token = _read_token(settings.token_path)

    # ── State ────────────────────────────────────────────────────────────────
    def needs_login(self) -> bool:
        t = self._token
        if t is None or not t.refresh_token:
            return True
        return self.check_scope and not set(self.scope.split()) <= set(t.scope.split())

    def token(self) -> str:
        """A valid access token, refreshed shortly before it expires."""
        with self._lock:
            t = self._token
            if t is None:
                raise AuthError("Not logged in to Spotify. Restart spoterm to log in")
            if t.expires_at - time.time() < REFRESH_MARGIN:
                t = self._refresh_locked()
            return t.access_token

    def refresh(self, rejected: str) -> str:
        """Called after a 401 with the token that was rejected; refreshes at most once."""
        with self._lock:
            t = self._token
            if t is None:
                raise AuthError("Not logged in to Spotify. Restart spoterm to log in")
            if t.access_token == rejected:
                t = self._refresh_locked()
            return t.access_token

    def _refresh_locked(self) -> Token:
        old = self._token
        # Another spoterm process may have refreshed (and rotated the refresh token) already.
        disk = _read_token(self.settings.token_path)
        if disk and disk.access_token != old.access_token and disk.expires_at - time.time() > REFRESH_MARGIN:
            self._token = disk
            return disk
        if not old.refresh_token:
            raise AuthError("Spotify session expired. Restart spoterm to log in again")
        form = {"grant_type": "refresh_token", "refresh_token": old.refresh_token}
        headers = {}
        secret = self.settings.client_secret
        if secret and not old.pkce:
            cred = f"{self.settings.client_id}:{secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(cred).decode()
        else:
            form["client_id"] = self.settings.client_id
        try:
            r = self._http.request("POST", "/api/token", form=form, headers=headers)
        except ApiError as e:
            if e.status in (400, 401):   # invalid_grant: revoked, rotated away, or wrong client
                raise AuthError("Spotify session expired. Restart spoterm to log in again") from e
            raise
        new = self._token_from(r, old.refresh_token, old.scope, old.pkce)
        self._save(new)
        return new

    def _token_from(self, r, refresh_token: str, scope: str, pkce: bool) -> Token:
        if not isinstance(r, dict) or not r.get("access_token"):
            raise AuthError("Spotify returned an invalid token response")
        return Token(r["access_token"], r.get("refresh_token") or refresh_token,
                     time.time() + int(r.get("expires_in") or 3600),
                     r.get("scope", scope) or scope, pkce)

    def _save(self, token: Token) -> None:
        self._token = token
        try:
            _write_token(self.settings.token_path, token)
        except OSError as e:     # keep working with the in-memory token
            log.warning("could not save the Spotify token: %s", e)

    # ── Login (interactive, before curses starts) ────────────────────────────
    def login(self, open_browser: bool = True, out=print) -> None:
        import hashlib
        import secrets
        verifier = secrets.token_urlsafe(64)      # 86 chars from [A-Za-z0-9_-]
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = secrets.token_urlsafe(24)
        redirect = self.settings.redirect_uri
        url = AUTHORIZE_URL + "?" + urlencode({
            "client_id": self.settings.client_id, "response_type": "code",
            "redirect_uri": redirect, "scope": self.scope, "state": state,
            "code_challenge_method": "S256", "code_challenge": challenge,
        })

        server = self._callback_server(redirect)
        out("Log in to Spotify in your browser. If it doesn't open, visit:\n\n  " + url + "\n")
        if open_browser:
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
        if server:
            try:
                code = self._wait_for_code(server, urlsplit(redirect).path or "/", state)
            finally:
                server.server_close()
        else:
            reply = input("Paste the full URL you were redirected to: ").strip()
            code = self._code_from_query(urlsplit(reply).query, state)

        r = self._http.request("POST", "/api/token", form={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
            "client_id": self.settings.client_id, "code_verifier": verifier,
        })
        self._save(self._token_from(r, "", self.scope, pkce=True))
        if not self._token.refresh_token:
            raise AuthError("Spotify did not return a refresh token")

    @staticmethod
    def _code_from_query(query: str, state: str) -> str:
        import hmac
        q = {k: v[0] for k, v in parse_qs(query).items()}
        if not hmac.compare_digest(q.get("state", "").encode(), state.encode()):
            raise StateMismatch("Login response had the wrong state (possible CSRF); try again")
        if "error" in q:
            raise AuthError(f"Spotify login was refused: {q['error']}")
        if not q.get("code"):
            raise AuthError("Login response had no authorization code")
        return q["code"]

    @staticmethod
    def _callback_server(redirect: str):
        """Listen on the redirect URI's loopback port; None means fall back to pasting the URL."""
        from http.server import BaseHTTPRequestHandler, HTTPServer
        u = urlsplit(redirect)
        if u.scheme != "http" or u.hostname not in _LOOPBACK:
            return None
        host = "::1" if u.hostname == "::1" else "127.0.0.1"   # never bind all interfaces
        cls = HTTPServer
        if host == "::1":
            import socket
            cls = type("HTTPServer6", (HTTPServer,), {"address_family": socket.AF_INET6})
        try:
            server = cls((host, u.port or 80), BaseHTTPRequestHandler, bind_and_activate=True)
        except OSError as e:
            print(f"Can't listen on {host}:{u.port or 80} ({e}).")
            return None
        server.timeout = 1.0
        server.handle_error = lambda request, addr: None   # a dropped connection: no traceback
        return server

    def _wait_for_code(self, server, path: str, state: str) -> str:
        import html
        from http.server import BaseHTTPRequestHandler
        result: dict = {}

        class Handler(BaseHTTPRequestHandler):
            timeout = 5     # a stalled connection can't block the login

            def do_GET(self):
                u = urlsplit(self.path)
                if u.path != path:
                    return self._reply(404, "Not found", "", "#999")
                try:
                    result["code"] = Auth._code_from_query(u.query, state)
                except StateMismatch:           # not our redirect: ignore it and keep waiting
                    return self._reply(400, "Invalid request", "", "#e22")
                except AuthError as e:
                    result["error"] = e
                    return self._reply(400, "Login failed", html.escape(str(e)), "#e22")
                self._reply(200, "Logged in to spoterm",
                            "You can close this tab and return to the terminal.", "#1db954")

            def _reply(self, status, title, body, color):
                data = _PAGE.format(title=title, body=body, color=color).encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server.RequestHandlerClass = Handler
        deadline = time.monotonic() + LOGIN_TIMEOUT
        while not result:
            if time.monotonic() > deadline:
                raise AuthError("Timed out waiting for the Spotify login")
            server.handle_request()
        if "error" in result:
            raise result["error"]
        return result["code"]
