"""SpoTerm's engine: the `spoterm-engine` helper process (Rust, librespot).

It does the two heavy jobs so this Python process stays tiny:

* plays audio as the "SpoTerm" Spotify Connect device, and
* makes every Web API call over its own HTTPS stack (so Python never loads TLS).

We write one JSON command per line to its stdin; it answers with one JSON event per
line on stdout. A reader thread blocks on the pipe and costs nothing while idle:
player events go to `events` for the UI thread, API answers wake the worker thread
that asked. The helper exits when its stdin closes, which the OS does when SpoTerm
exits for any reason, crashes included.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time

from . import config

_WIN = sys.platform == "win32"
EXE = "spoterm-engine.exe" if _WIN else "spoterm-engine"
STOP_TIMEOUT = 1.5
CALL_TIMEOUT = 40.0     # the engine itself gives up on a request well before this
MALFORMED = "Spotify sent a malformed response"


def find_binary(explicit: str = "") -> str | None:
    """SPOTERM_ENGINE_BIN, then the project's own build, then <config_dir>/bin, then PATH."""
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    for folder in (os.path.join(config.PROJECT_DIR, "engine", "target", "release"),
                   os.path.join(config.config_dir(), "bin"),
                   *os.environ.get("PATH", "").split(os.pathsep)):
        path = os.path.join(folder, EXE)
        if folder and os.path.isfile(path):
            return path
    return None


class Engine:
    def __init__(self, settings: config.Settings):
        self.name = settings.engine_name
        self.player_enabled = settings.engine
        self.bitrate = settings.engine_bitrate
        self.client_id = settings.client_id
        self.token_path = settings.token_path
        self.binary = find_binary(settings.engine_bin)
        self.cache_dir = os.path.join(config.config_dir(), "engine")
        self.log_path = os.path.join(config.config_dir(), "engine.log")
        self.events: queue.SimpleQueue = queue.SimpleQueue()   # player events, for the UI thread
        self.device_id: str | None = None
        self.ready = False             # the player is connected
        self.player_state = ""         # "", "login", "retrying"
        self.alive = threading.Event() # the helper said hello: API calls can flow
        self._proc: subprocess.Popen | None = None
        self._wlock = threading.Lock()
        self._calls: dict = {}         # id -> [threading.Event, answer]
        self._next_id = 0
        self._exit_code: int | None = None
        self._stopping = False

    # ── State ────────────────────────────────────────────────────────────────
    def available(self) -> bool:
        return self.binary is not None

    def needs_login(self) -> bool:
        """Whether the player (not SpoTerm's own sign-in) still needs its one-time login."""
        return self.player_enabled and (
            self.player_state == "login"
            or not os.path.isfile(os.path.join(self.cache_dir, "credentials.json")))

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def status(self) -> str:
        """"off", "not built", "login needed", "starting", "retrying", "ready" or "stopped (N)"."""
        if not self.player_enabled:
            return "off"
        if not self.binary:
            return "not built"
        if self.player_state == "login":
            return "login needed"
        if self.running():
            if self.ready:
                return "ready"
            return "retrying" if self.player_state == "retrying" else "starting"
        if self._exit_code is not None:
            return f"stopped ({self._exit_code})"
        return "starting"

    # ── Login (before curses) ────────────────────────────────────────────────
    def login(self) -> bool:
        """One-time browser sign-in for the player. Blocking; prints to the terminal."""
        os.makedirs(self.cache_dir, exist_ok=True)
        print("Signing in SpoTerm's player. Your browser will open: approve it, then come "
              "back here (ctrl+c skips this; SpoTerm then only controls other devices).")
        try:
            code = subprocess.call([self.binary, "login", "--cache", self.cache_dir], env=self._env())
        except KeyboardInterrupt:
            print("\nSkipped.")
            return False
        except OSError as e:
            print(f"Couldn't run the player: {e}")
            return False
        if code != 0:
            print(f"Player sign-in didn't finish (exit {code}).")
            return False
        self.player_state = ""
        return True

    # ── Process ──────────────────────────────────────────────────────────────
    def start(self) -> None:
        if not self.binary or self.running() or self._stopping:
            return
        os.makedirs(self.cache_dir, exist_ok=True)
        argv = [self.binary, "run", "--cache", self.cache_dir, "--name", self.name,
                "--bitrate", str(self.bitrate), "--client-id", self.client_id,
                "--token", self.token_path]
        if self.needs_login() or not self.player_enabled:
            argv.append("--no-player")
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if _WIN else {"start_new_session": True}
        # Buffered pipes: unbuffered, every line read would cost one system call per byte
        # (tens of thousands for a page of tracks). Writes are flushed line by line anyway.
        with open(self.log_path, "wb") as log:     # the child keeps its own handle
            self._proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=log, env=self._env(), **kwargs)
        self.ready, self.device_id, self._exit_code = False, None, None
        self.alive.clear()
        threading.Thread(target=self._read, args=(self._proc,), name="spoterm-engine",
                         daemon=True).start()

    def _env(self) -> dict:
        # The helper needs nothing of ours: keep secrets and settings out of its environment.
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SPOTIPY_", "SPOTERM_"))}
        env.setdefault("RUST_LOG", "warn")
        return env

    def _read(self, proc: subprocess.Popen) -> None:
        try:
            for raw in proc.stdout:   # blocks in the OS until the helper writes; no polling
                try:
                    ev = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if isinstance(ev, dict) and proc is self._proc:
                    self._dispatch(ev)
        except (OSError, ValueError):
            pass
        code = proc.wait()
        if self._proc is not proc:
            return      # stopped on purpose (stop() already reset everything) or replaced
        self.ready = False
        self.alive.clear()
        self._exit_code = code
        self._fail_calls("SpoTerm's engine stopped")
        if not self._stopping:
            self.events.put({"ev": "exit", "code": code})

    def _dispatch(self, ev: dict) -> None:
        kind = ev.get("ev")
        if kind == "api":
            slot = self._calls.pop(ev.get("id"), None)
            if slot:
                slot[1] = ev
                slot[0].set()
            return
        if kind == "hello":
            self.alive.set()
            return
        dev = ev.get("device_id") if isinstance(ev.get("device_id"), str) else None
        if kind == "ready":
            self.ready, self.device_id, self.player_state = True, dev, ""
        elif kind == "reconnected":
            self.device_id = dev or self.device_id
        elif kind == "player":
            state = ev.get("state")
            self.player_state = state if state in ("login", "retrying") else ""
        self.events.put(ev)

    def _fail_calls(self, why: str) -> None:
        calls, self._calls = self._calls, {}
        for slot in calls.values():
            slot[1] = {"ev": "api", "status": 0, "error": why}
            slot[0].set()

    def _write(self, obj: dict) -> bool:
        proc = self._proc
        if not proc or proc.poll() is not None:
            return False
        line = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self._wlock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (OSError, ValueError):
                return False
        return True

    def send(self, cmd: str, **args) -> bool:
        """Queue a player command. Never blocks on the network."""
        return self.ready and self._write({"cmd": cmd, **args})

    def call(self, method: str, path: str, query: dict | None = None, body=None):
        """A Web API call made by the engine. Blocks (on a worker thread) until it answers."""
        from .api import ApiError
        if not self._wait_alive(10):
            raise ApiError(0, "SpoTerm's engine isn't running")
        with self._wlock:
            self._next_id += 1
            cid = self._next_id
        slot = [threading.Event(), None]
        self._calls[cid] = slot
        if not self._write({"cmd": "api", "id": cid, "method": method, "path": path,
                            "query": query or None, "body": body}):
            self._calls.pop(cid, None)
            raise ApiError(0, "SpoTerm's engine isn't running")
        t0 = time.monotonic()
        if not slot[0].wait(CALL_TIMEOUT):
            self._calls.pop(cid, None)
            if config.DEBUG:
                config.debug(f"api {method} {path} {query or ''} {body or ''} -> timeout")
            raise TimeoutError()
        ev = slot[1]
        status = ev.get("status") if isinstance(ev.get("status"), int) else 0
        if config.DEBUG:
            config.debug(f"api {method} {path} {query or ''} {body or ''} -> {status} "
                         f"{ev.get('error') or ''} {(time.monotonic() - t0) * 1000:.0f}ms")
        if method != "GET" and status == 0 and ev.get("error") == MALFORMED:
            # Player commands now answer 200 with a bare text id; engines built before that
            # fix report it as malformed. The command worked.
            return None
        if "error" in ev or status >= 400 or status <= 0:
            b = ev.get("body")
            ra = ev.get("retry_after")
            raise ApiError(status, str(ev.get("error") or ""),
                           retry_after=float(ra) if isinstance(ra, (int, float)) and 0 <= ra < 86400 else 0.0,
                           login=isinstance(b, dict) and bool(b.get("login")))
        return ev.get("body")

    def _wait_alive(self, timeout: float) -> bool:
        """Wait for the helper's hello, giving up at once if it has already exited."""
        for _ in range(int(timeout / 0.25)):
            if self.alive.wait(0.25):
                return True
            if self._proc is not None and self._proc.poll() is not None:
                return False
        return self.alive.is_set()

    def check_web(self) -> str:
        """Check SpoTerm's own sign-in before the UI starts: "ok", "login" or an error text."""
        from .api import ApiError
        try:
            self.call("GET", "me")
            return "ok"
        except ApiError as e:
            return "login" if e.login else (e.message or f"HTTP {e.status}")
        except TimeoutError:
            return "Spotify took too long to respond"

    def stop(self) -> None:
        self._stopping = True          # nothing may respawn it after this
        proc, self._proc = self._proc, None
        self.ready = False
        self.alive.clear()
        self._fail_calls("SpoTerm's engine stopped")
        if not proc or proc.poll() is not None:
            return
        try:
            with self._wlock:
                proc.stdin.write(b'{"cmd":"quit"}\n')
                proc.stdin.close()
            proc.wait(STOP_TIMEOUT)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            proc.kill()

    def restart(self) -> None:
        """Stop and start again, e.g. after a sign-in so the new login is picked up."""
        self.stop()
        self._stopping = False
        self.player_state = ""
        self.start()
