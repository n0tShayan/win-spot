"""SpoTerm's own playback device: the `spoterm-engine` helper (librespot, in Rust).

The helper is a Spotify Connect player driven over pipes. SpoTerm writes one JSON
command per line to its stdin and it answers with one JSON event per line on
stdout. Commands never touch the network on our side, so play, pause, seek and
volume act instantly, and player state is pushed to us, so there is nothing to poll.

A reader thread blocks on the pipe and queues events for the UI thread; it costs
nothing while idle. The helper dies with SpoTerm: its stdin closes when we exit,
and on Windows a Job Object kills it even if SpoTerm crashes.
"""

import atexit
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import config

_WIN = sys.platform == "win32"
EXE = "spoterm-engine.exe" if _WIN else "spoterm-engine"
EXIT_LOGIN = 3
STOP_TIMEOUT = 1.5


def find_binary(explicit: str = "") -> str | None:
    """SPOTERM_ENGINE_BIN, then the project's own build, then <config_dir>/bin, then PATH."""
    if explicit:
        return explicit if Path(explicit).is_file() else None
    for p in (config.PROJECT_DIR / "engine" / "target" / "release" / EXE, config.config_dir() / "bin" / EXE):
        if p.is_file():
            return str(p)
    return shutil.which("spoterm-engine")


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
        self.binary = find_binary(settings.engine_bin) if self.enabled else None
        self.cache_dir = config.config_dir() / "engine"
        self.log_path = config.config_dir() / "engine.log"
        self.events: queue.SimpleQueue = queue.SimpleQueue()   # drained by the UI thread
        self.device_id: str | None = None
        self.ready = False
        self._proc: subprocess.Popen | None = None
        self._wlock = threading.Lock()
        self._job = None
        self._exit_code: int | None = None
        self._login_needed = False
        atexit.register(self.stop)

    # ── State ────────────────────────────────────────────────────────────────
    def available(self) -> bool:
        return bool(self.enabled and self.binary)

    def needs_login(self) -> bool:
        return self._login_needed or not (self.cache_dir / "credentials.json").is_file()

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def status(self) -> str:
        """"off", "not built", "login needed", "starting", "ready", or "stopped (N)"."""
        if not self.enabled:
            return "off"
        if not self.binary:
            return "not built"
        if self._login_needed:
            return "login needed"
        if self.running():
            return "ready" if self.ready else "starting"
        if self._exit_code is not None:
            return f"stopped ({self._exit_code})"
        return "starting" if self._proc is None else "stopped"

    # ── Login (before curses) ────────────────────────────────────────────────
    def login(self) -> bool:
        """One-time browser sign-in for the player. Blocking; prints to the terminal."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        print("Signing in SpoTerm's built-in player. Your browser will open; "
              "approve it and come back here (ctrl+c to skip).")
        try:
            code = subprocess.call([self.binary, "login", "--cache", str(self.cache_dir)],
                                   env=self._env())
        except KeyboardInterrupt:
            print("\nSkipped. SpoTerm will only control other Spotify devices this time.")
            return False
        if code != 0:
            print(f"Player sign-in failed (exit {code}). Details: {self.log_path}")
            return False
        self._login_needed = False
        return True

    # ── Process ──────────────────────────────────────────────────────────────
    def start(self) -> None:
        if not self.available() or self.running() or self.needs_login():
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        argv = [self.binary, "run", "--cache", str(self.cache_dir), "--name", self.name,
                "--bitrate", str(self.bitrate)]
        log = open(self.log_path, "wb")          # the child keeps its own handle
        kwargs = {}
        if _WIN:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        try:
            self._proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=log, env=self._env(), bufsize=0, **kwargs)
        finally:
            log.close()
        self.ready, self.device_id, self._exit_code = False, None, None
        if _WIN:
            try:
                self._job = self._job or _KillOnCloseJob()
                self._job.assign(self._proc)
            except OSError:
                pass   # stdin EOF still stops it when SpoTerm exits normally
        threading.Thread(target=self._read, args=(self._proc,), name="spoterm-engine",
                         daemon=True).start()

    def _env(self) -> dict:
        # The helper needs nothing of ours: keep secrets and settings out of its environment.
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SPOTIPY_", "SPOTERM_"))}
        env.setdefault("RUST_LOG", "warn")
        return env

    def _read(self, proc: subprocess.Popen) -> None:
        for raw in proc.stdout:   # blocks in the OS until the helper writes; no polling
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            kind = ev.get("ev")
            if kind == "ready":
                self.ready, self.device_id = True, ev.get("device_id")
            elif kind == "reconnected":
                self.device_id = ev.get("device_id") or self.device_id
            elif kind == "error" and ev.get("kind") == "login":
                self._login_needed = True
            self.events.put(ev)
        code = proc.wait()
        self.ready = False
        self._exit_code = code
        if code == EXIT_LOGIN:
            self._login_needed = True
        self.events.put({"ev": "exit", "code": code})

    def send(self, cmd: str, **args) -> bool:
        """Queue a command for the player. Never blocks on the network."""
        proc = self._proc
        if not proc or proc.poll() is not None or not self.ready:
            return False
        line = (json.dumps({"cmd": cmd, **args}, separators=(",", ":")) + "\n").encode()
        with self._wlock:
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except OSError:
                return False
        return True

    def stop(self) -> None:
        self.enabled = False   # nothing may respawn it after this
        proc, self._proc = self._proc, None
        if not proc or proc.poll() is not None:
            return
        try:
            with self._wlock:
                proc.stdin.write(b'{"cmd":"quit"}\n')
                proc.stdin.close()
            proc.wait(STOP_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
