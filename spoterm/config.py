"""Settings, loaded from the environment and .env files."""

import os
import sys

# Plain os.path and a plain class on purpose: pathlib and dataclasses pull in a dozen
# more stdlib modules, and memory is a feature here.
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCOPE = " ".join((
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-library-read",
    "user-library-modify",
    "playlist-read-private",
    "playlist-read-collaborative",
    "streaming",
))


def config_dir() -> str:
    # Not %APPDATA% on Windows: Microsoft Store Python silently redirects writes there to a
    # private per-package folder, so the engine (a separate program) and the user would
    # look in the wrong place. The home folder is never redirected.
    base = os.environ.get("XDG_CONFIG_HOME") if sys.platform != "win32" else None
    return os.path.join(base or os.path.join(os.path.expanduser("~"), ".config"), "spoterm")


DEBUG = False   # SPOTERM_DEBUG=1: trace keys, playback decisions and API calls (set by load)
_trace = None


def debug(msg: str) -> None:
    """Append a timestamped line to spoterm.log. Callers check DEBUG first, so it's free when off."""
    global _trace, DEBUG
    if _trace is None:
        try:
            _trace = open(os.path.join(config_dir(), "spoterm.log"), "a", encoding="utf-8", buffering=1)
        except OSError:
            DEBUG = False
            return
    import time
    _trace.write(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {msg}\n")


class Settings:
    __slots__ = ("client_id", "client_secret", "redirect_uri", "token_path", "ascii",
                 "engine", "engine_name", "engine_bitrate", "engine_bin")

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, token_path: str,
                 ascii: bool, engine: bool = True, engine_name: str = "SpoTerm",
                 engine_bitrate: int = 160, engine_bin: str = ""):
        self.client_id = client_id
        self.client_secret = client_secret   # optional: "" means PKCE-only (the usual case)
        self.redirect_uri = redirect_uri
        self.token_path = token_path
        self.ascii = ascii
        self.engine = engine                 # play audio here (off: remote control only)
        self.engine_name = engine_name
        self.engine_bitrate = engine_bitrate # 96 | 160 | 320; lower is lighter on CPU and network
        self.engine_bin = engine_bin         # explicit path to the spoterm-engine binary


class ConfigError(Exception):
    pass


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def parse_env(text: str) -> dict:
    """Parse a .env file: KEY=VALUE lines, optional `export `, # comments, '…' and "…" quoting.

    Nothing is expanded or executed; malformed lines are skipped.
    """
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.isascii() or not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            q = value[0]
            end, buf, i = -1, [], 1
            while i < len(value):
                c = value[i]
                if q == '"' and c == "\\" and i + 1 < len(value):
                    buf.append(_ESCAPES.get(value[i + 1], "\\" + value[i + 1]))
                    i += 2
                    continue
                if c == q:
                    end = i
                    break
                buf.append(c)
                i += 1
            if end < 0:
                continue
            value = "".join(buf)
        else:
            # An unquoted value ends at a " #" comment.
            for i, c in enumerate(value):
                if c == "#" and i and value[i - 1] in " \t":
                    value = value[:i].rstrip()
                    break
        if "\0" not in value:      # os.environ rejects NUL
            out[key] = value
    return out


# Only SpoTerm's own settings are taken from .env files: a stray .env (they are read from
# the current folder) must not be able to set e.g. SSL_CERT_FILE, PATH or PYTHON* for us.
_ENV_PREFIXES = ("SPOTIPY_", "SPOTERM_")
_ENV_KEYS = frozenset(("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"))


def _load_env_files() -> None:
    # Real environment variables win; then the first .env that defines a key.
    for folder in (os.getcwd(), PROJECT_DIR, config_dir()):
        try:
            with open(os.path.join(folder, ".env"), encoding="utf-8-sig") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        for k, v in parse_env(text).items():
            if k.startswith(_ENV_PREFIXES) or k in _ENV_KEYS:
                os.environ.setdefault(k, v)


def _migrate_token(token_path: str) -> None:
    """Carry the login over from the old %APPDATA% location, so moving folders isn't a re-login."""
    old = os.path.join(os.environ.get("APPDATA") or "", "spoterm", "token.json")
    if sys.platform != "win32" or os.path.exists(token_path) or not os.path.isfile(old):
        return
    try:
        with open(old, "rb") as src, open(token_path, "wb") as dst:
            dst.write(src.read())
    except OSError:
        pass


def _flag(name: str, default: bool) -> bool:
    v = os.getenv(name, "").strip().lower()
    return default if not v else v in ("1", "true", "yes", "on")


def load() -> Settings:
    global DEBUG
    _load_env_files()
    DEBUG = _flag("SPOTERM_DEBUG", False)
    os.makedirs(config_dir(), mode=0o700, exist_ok=True)   # token, logs and the engine's login live here

    client_id = os.getenv("SPOTIPY_CLIENT_ID", "").strip()
    if not client_id:
        raise ConfigError(
            "Missing Spotify client ID.\n"
            "Copy .env.example to .env and set SPOTIPY_CLIENT_ID "
            "(from your app at https://developer.spotify.com/dashboard)."
        )

    token_path = os.getenv("SPOTERM_TOKEN_PATH") or os.path.join(config_dir(), "token.json")
    os.makedirs(os.path.dirname(os.path.abspath(token_path)), mode=0o700, exist_ok=True)
    _migrate_token(token_path)

    bitrate = os.getenv("SPOTERM_BITRATE", "").strip()
    return Settings(
        client_id=client_id,
        client_secret=os.getenv("SPOTIPY_CLIENT_SECRET", "").strip(),
        redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback").strip(),
        token_path=token_path,
        ascii=_flag("SPOTERM_ASCII", False),
        engine=_flag("SPOTERM_ENGINE", True),
        engine_name=os.getenv("SPOTERM_DEVICE_NAME", "").strip() or "SpoTerm",
        engine_bitrate=int(bitrate) if bitrate in ("96", "160", "320") else 160,
        engine_bin=os.getenv("SPOTERM_ENGINE_BIN", "").strip(),
    )
