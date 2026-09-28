"""SpoTerm's own playback device: a managed librespot process (a Spotify Connect receiver).

librespot signs in once (interactive OAuth before curses starts, or silently with
SpoTerm's own token), caches reusable credentials in <config_dir>/librespot and then
starts silently on later runs. It never advertises itself on the LAN (discovery off),
writes only to a log file, and dies with SpoTerm: a Windows Job Object kills it even
if SpoTerm crashes; elsewhere it is killed on exit.
"""

import atexit
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import config

LOGIN_TIMEOUT = 300        # seconds to wait for the interactive browser login
TOKEN_LOGIN_TIMEOUT = 20   # seconds to wait for a silent login with SpoTerm's token
STOP_TIMEOUT = 2.0
# librespot's --quiet hides the one info line we need ("Authenticated as ..."), so filter
# explicitly: warnings everywhere, plus session info (a handful of lines per start).
LOG_FILTER = "warn,librespot_core::session=info,libmdns=off"
_READY = "Authenticated as"
_BAD_LOGIN = ("Bad credentials", "Login failed")
_WIN = sys.platform == "win32"


def find_binary(explicit: str = "") -> str | None:
    """SPOTERM_LIBRESPOT, then PATH, then ~/.cargo/bin, then <config_dir>/bin."""
    if explicit:
        return explicit if Path(explicit).is_file() else None
    exe = "librespot.exe" if _WIN else "librespot"
    found = shutil.which("librespot")
    if found:
        return found
    for d in (Path.home() / ".cargo" / "bin", config.config_dir() / "bin"):
        if (d / exe).is_file():
            return str(d / exe)
    return None


class _KillOnCloseJob:
    """A Windows Job Object that kills its processes when the last handle to it closes,
    which the OS does when SpoTerm exits for any reason, crashes included."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic),
                        ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                                wintypes.LPVOID, wintypes.DWORD)
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self._k32 = k32
        self.handle = k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000     # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(self.handle, 9,  # JobObjectExtendedLimitInformation
                                           ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, proc: subprocess.Popen) -> bool:
        return bool(self._k32.AssignProcessToJobObject(self.handle, int(proc._handle)))


class Engine:
    def __init__(self, settings: config.Settings):
        self.name = settings.engine_name
        self.enabled = settings.engine
        self.bitrate = settings.engine_bitrate
        self.binary = find_binary(settings.librespot) if self.enabled else None
        self.cache_dir = config.config_dir() / "librespot"
        self.log_path = config.config_dir() / "librespot.log"
        self._proc: subprocess.Popen | None = None
        self._job: _KillOnCloseJob | None = None
        self._lock = threading.Lock()
        self._ready = False
        self._bad_login = False
        self._log_pos = 0
        self._next_scan = 0.0
        atexit.register(self.stop)

    # ── State ────────────────────────────────────────────────────────────────
    def available(self) -> bool:
        return self.enabled and self.binary is not None

    def needs_login(self) -> bool:
        return self._bad_login or not (self.cache_dir / "credentials.json").is_file()

    def running(self) -> bool:
        p = self._proc
        return p is not None and p.poll() is None

    @property
    def status(self) -> str:
        """"off", "not installed", "login needed", "starting", "ready", "stopped" or "exited (N)"."""
        if not self.enabled:
            return "off"
        if not self.binary:
            return "not installed"
        with self._lock:
            p = self._proc
            if p is not None and not self._ready:
                self._scan_log()
            if p is None:
                return "login needed" if self.needs_login() else "stopped"
            code = p.poll()
            if code is not None:
                return "login needed" if self._bad_login else f"exited ({code})"
            return "ready" if self._ready else "starting"

    def _scan_log(self) -> None:
        """Read new log lines while starting, to notice sign-in success or failure."""
        now = time.monotonic()
        if now < self._next_scan:
            return
        self._next_scan = now + 0.5
        try:
            with open(self.log_path, "rb") as f:
                f.seek(self._log_pos)
                chunk = f.read(65536)
        except OSError:
            return
        # Keep the last partial line for the next scan.
        cut = chunk.rfind(b"\n") + 1
        self._log_pos += cut
        text = chunk[:cut].decode("utf-8", "replace")
        if _READY in text:
            self._ready = True
        if any(s in text for s in _BAD_LOGIN):
            self._bad_login = True
            # Drop the rejected credentials so the next launch runs the login again.
            (self.cache_dir / "credentials.json").unlink(missing_ok=True)

    # ── Process ──────────────────────────────────────────────────────────────
    def _argv(self) -> list:
        return [
            self.binary,
            "--name", self.name,
            "--device-type", "computer",
            "--backend", "rodio",
            "--bitrate", str(self.bitrate),
            "--system-cache", str(self.cache_dir),   # credentials + volume only, no audio cache
            "--disable-discovery",                   # no zeroconf/mDNS on the LAN
        ]

    def _spawn(self, argv: list, *, stdout, stderr, env: dict | None = None) -> subprocess.Popen:
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # librespot reads any LIBRESPOT_<OPTION> variable as a flag: pass only ours. It has no
        # use for SpoTerm's own settings either (SPOTIPY_CLIENT_SECRET among them).
        child_env = {k: v for k, v in os.environ.items()
                     if not k.upper().startswith(("LIBRESPOT_", "SPOTIPY_", "SPOTERM_"))
                     and k.upper() != "RUST_BACKTRACE"}
        child_env.update(env or {}, RUST_LOG=LOG_FILTER)
        kw: dict = {"stdin": subprocess.DEVNULL, "stdout": stdout, "stderr": stderr,
                    "env": child_env, "close_fds": True}
        if _WIN:
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True      # keep terminal signals (ctrl+c) away from it
        proc = subprocess.Popen(argv, **kw)
        if _WIN:
            try:
                if self._job is None:
                    self._job = _KillOnCloseJob()
                self._job.assign(proc)
            except OSError:
                pass        # still stopped by stop()/atexit on a normal exit
        return proc

    def start(self) -> None:
        """Spawn librespot in the background if it isn't running. Never blocks for long."""
        with self._lock:
            if not self.available() or self.needs_login():
                return
            if self._proc is not None and self._proc.poll() is None:
                return
            self._ready = False
            self._log_pos = 0
            self._next_scan = 0.0
            with open(self.log_path, "wb") as log:  # the child keeps its own handle
                self._proc = self._spawn(self._argv(), stdout=log, stderr=subprocess.STDOUT)

    def stop(self) -> None:
        with self._lock:
            p, self._proc = self._proc, None
        if p is None or p.poll() is not None:
            return
        p.terminate()       # TerminateProcess on Windows, SIGTERM elsewhere
        try:
            p.wait(STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.wait(STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass

    # ── One-time login (before curses) ───────────────────────────────────────
    def login(self, token: Callable[[], str] | None = None, out=print) -> None:
        """Sign librespot in once and cache its credentials. Blocking; run before curses.

        With `token` (SpoTerm's own access token, which has the `streaming` scope) this is
        tried silently first; otherwise, or if Spotify refuses it, librespot's interactive
        OAuth opens a browser. Raises RuntimeError on failure, KeyboardInterrupt to skip.
        """
        if not self.available():
            raise RuntimeError("librespot is not installed")
        creds = self.cache_dir / "credentials.json"
        self.stop()
        creds.unlink(missing_ok=True)       # stale credentials (Spotify refused them)
        self._bad_login = False
        if token is not None:
            out(f"Signing in SpoTerm's built-in player (\"{self.name}\")…")
            try:
                # The token goes through the environment, not argv: command lines are
                # visible to other users' processes, a process's environment is not.
                env = {"LIBRESPOT_ACCESS_TOKEN": token()}
                if self._login_run([], env, TOKEN_LOGIN_TIMEOUT):
                    out("Built-in player signed in.\n")
                    return
            except Exception:
                pass
            creds.unlink(missing_ok=True)
        out(f"SpoTerm's built-in player (\"{self.name}\") needs a one-time Spotify login.\n"
            "A browser window will open; if it doesn't, open the URL below.\n"
            "Press ctrl+c to skip (SpoTerm will then only control other devices).\n")
        if not self._login_run(["--enable-oauth"], None, LOGIN_TIMEOUT, out=out):
            raise RuntimeError(f"librespot login failed; see {self.log_path}")
        out("Built-in player signed in.\n")

    def _login_run(self, extra: list, env: dict | None, timeout: float, out=None) -> bool:
        """Run librespot until it has cached credentials (then stop it); True on success.

        With `out`, librespot's stdout is relayed so the user sees its "Browse to: <url>".
        """
        creds = self.cache_dir / "credentials.json"
        with open(self.log_path, "wb") as log:
            proc = self._spawn(self._argv() + extra, env=env, stderr=log,
                               stdout=subprocess.PIPE if out else log)
        if out:
            def relay():
                for raw in proc.stdout:
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("Browse to:"):
                        out("  " + line.split(":", 1)[1].strip() + "\n")
            threading.Thread(target=relay, daemon=True).start()
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline and proc.poll() is None:
                if creds.is_file() and creds.stat().st_size:
                    time.sleep(0.3)         # let the write finish
                    return True
                time.sleep(0.2)
            return creds.is_file() and creds.stat().st_size > 0
        finally:
            proc.kill()
            proc.wait()
