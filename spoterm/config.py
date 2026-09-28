"""Settings, loaded from the environment and .env files."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent

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


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "spoterm"


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str         # optional: "" means PKCE-only (the usual case)
    redirect_uri: str
    token_path: Path
    ascii: bool
    engine: bool = True        # run librespot as SpoTerm's own playback device
    engine_name: str = "SpoTerm"
    engine_bitrate: int = 160  # 96 | 160 | 320; lower is lighter on CPU and network
    librespot: str = ""        # explicit path to the librespot binary


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
    for path in (Path.cwd() / ".env", PROJECT_DIR / ".env", config_dir() / ".env"):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        for k, v in parse_env(text).items():
            if k.startswith(_ENV_PREFIXES) or k in _ENV_KEYS:
                os.environ.setdefault(k, v)


def _flag(name: str, default: bool) -> bool:
    v = os.getenv(name, "").strip().lower()
    return default if not v else v in ("1", "true", "yes", "on")


def load() -> Settings:
    _load_env_files()
    config_dir().mkdir(mode=0o700, parents=True, exist_ok=True)   # logs and librespot live here

    client_id = os.getenv("SPOTIPY_CLIENT_ID", "").strip()
    if not client_id:
        raise ConfigError(
            "Missing Spotify client ID.\n"
            "Copy .env.example to .env and set SPOTIPY_CLIENT_ID "
            "(from your app at https://developer.spotify.com/dashboard)."
        )

    token_path = Path(os.getenv("SPOTERM_TOKEN_PATH") or config_dir() / "token.json")
    token_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

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
        librespot=os.getenv("SPOTERM_LIBRESPOT", "").strip(),
    )
