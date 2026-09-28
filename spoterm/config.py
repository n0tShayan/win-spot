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
    client_secret: str
    redirect_uri: str
    token_path: Path
    ascii: bool


class ConfigError(Exception):
    pass


def load() -> Settings:
    from dotenv import load_dotenv

    # Real environment variables win; then the first .env that defines a key.
    for path in (Path.cwd() / ".env", PROJECT_DIR / ".env", config_dir() / ".env"):
        if path.is_file():
            load_dotenv(path, override=False)

    client_id = os.getenv("SPOTIPY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIPY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise ConfigError(
            "Missing Spotify credentials.\n"
            "Copy .env.example to .env and set SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET."
        )

    token_path = Path(os.getenv("SPOTERM_TOKEN_PATH") or config_dir() / "token.json")
    token_path.parent.mkdir(parents=True, exist_ok=True)

    return Settings(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback").strip(),
        token_path=token_path,
        ascii=os.getenv("SPOTERM_ASCII", "").strip().lower() in ("1", "true", "yes"),
    )
