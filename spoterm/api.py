"""Thin Spotify Web API layer that returns small typed objects instead of raw JSON.

Everything here blocks on the network, so it is only ever called from worker threads.
"""

import http.client
import ssl
import time
from dataclasses import dataclass

from .auth import Auth, AuthError
from .config import Settings
from .net import ApiError, Client

PAGE_SIZE = 50
PLAYLIST_PAGE_SIZE = 100
SEARCH_PAGE_SIZE = 10     # Spotify rejects search limits above 10
ENGINE_WAIT = 20.0        # seconds to wait for SpoTerm's own player to appear after it starts
_ITEM_FIELDS = "uri,name,duration_ms,type,is_local,is_playable,artists(name),album(name),show(name)"
# Spotify renamed each playlist entry's "track" key to "item"; ask for both.
_PLAYLIST_FIELDS = f"total,next,items(is_local,item({_ITEM_FIELDS}),track({_ITEM_FIELDS}))"


@dataclass(frozen=True, slots=True)
class Track:
    uri: str
    name: str
    artists: str
    album: str
    duration_ms: int
    pos: int              # index within its source list (playlist offset)
    playable: bool = True

    @property
    def id(self) -> str:
        return self.uri.rsplit(":", 1)[-1]

    @property
    def is_track(self) -> bool:
        return self.uri.startswith("spotify:track:")


@dataclass(frozen=True, slots=True)
class Playlist:
    id: str
    uri: str
    name: str
    total: int


@dataclass(frozen=True, slots=True)
class Device:
    id: str
    name: str
    type: str
    is_active: bool


@dataclass(slots=True)
class Playback:
    track: Track | None
    is_playing: bool
    progress_ms: int
    fetched_at: float          # time.monotonic() when progress_ms was valid
    device_id: str | None
    device_name: str
    volume: int | None
    shuffle: bool
    repeat: str                # "off" | "context" | "track"
    context_uri: str | None

    def progress(self, now: float) -> int:
        ms = self.progress_ms
        if self.is_playing:
            ms += int((now - self.fetched_at) * 1000)
        dur = self.track.duration_ms if self.track else 0
        return max(0, min(ms, dur)) if dur else max(0, ms)

    def rebase(self, now: float) -> None:
        """Freeze the interpolated progress into progress_ms (before changing is_playing)."""
        self.progress_ms = self.progress(now)
        self.fetched_at = now


@dataclass(frozen=True, slots=True)
class Page:
    tracks: list
    total: int
    next_offset: int | None


class NoDeviceError(Exception):
    """No device to play on. The message, when there is one, says why."""


def _track(item: dict, pos: int, is_local: bool = False) -> Track | None:
    if not item or not item.get("uri"):
        return None
    if item.get("type") == "episode":
        artists = (item.get("show") or {}).get("name", "")
        album = ""
    else:
        artists = ", ".join(a.get("name") or "" for a in item.get("artists") or [])
        album = (item.get("album") or {}).get("name") or ""
    return Track(
        uri=item["uri"],
        name=item.get("name") or "Unknown",
        artists=artists,
        album=album,
        duration_ms=item.get("duration_ms") or 0,
        pos=pos,
        playable=not (is_local or item.get("is_local") or item.get("is_playable") is False),
    )


def describe_error(err: BaseException) -> str:
    """Turn an exception into a short, human-readable status message."""
    if isinstance(err, NoDeviceError):
        return str(err) or "No Spotify device found. Open Spotify somewhere, or press d"
    if isinstance(err, AuthError):
        return str(err)
    if isinstance(err, ApiError):
        reason = err.message.rsplit(":", 1)[-1].strip() or f"HTTP {err.status}"
        if err.status == 404 and "device" in reason.lower():
            return "No active device. Press d to pick one"
        if err.status == 403:
            if "premium" in err.message.lower() or "premium" in err.reason.lower():
                return "Spotify Premium is required for playback control"
            return f"Not allowed: {reason}"
        if err.status == 429:
            return "Rate limited by Spotify, slowing down"
        return reason
    if isinstance(err, TimeoutError):
        return "Spotify took too long to respond"
    if isinstance(err, ssl.SSLCertVerificationError):
        return "Secure connection to Spotify failed (certificate not trusted)"
    if isinstance(err, (OSError, http.client.HTTPException)):
        return "Network error: can't reach Spotify"
    return f"{type(err).__name__}: {err}"


def make_auth(settings: Settings) -> Auth:
    return Auth(settings)


class Spotify:
    def __init__(self, auth: Auth, engine=None):
        self.auth = auth
        self.engine = engine        # engine.Engine: SpoTerm's own device, used when none is active
        self.http = Client("api.spotify.com")
        self._user_id: str | None = None

    def _call(self, method: str, path: str, params: dict | None = None, body=None):
        if not path.startswith("https://"):
            path = "/v1/" + path
        token = self.auth.token()
        for attempt in (0, 1):
            try:
                return self.http.request(method, path, params=params, json_body=body,
                                         headers={"Authorization": "Bearer " + token})
            except ApiError as e:
                if e.status != 401 or attempt:
                    raise
            token = self.auth.refresh(token)    # expired or revoked early: refresh once, retry

    def _get(self, path: str, **params) -> dict:
        return self._call("GET", path, params) or {}

    # ── Reads ────────────────────────────────────────────────────────────────
    def playback(self) -> Playback | None:
        r = self._call("GET", "me/player", {"additional_types": "episode"})
        now = time.monotonic()
        if not r:
            return None
        dev = r.get("device") or {}
        return Playback(
            track=_track(r.get("item"), 0),
            is_playing=bool(r.get("is_playing")),
            progress_ms=r.get("progress_ms") or 0,
            fetched_at=now,
            device_id=dev.get("id"),
            device_name=dev.get("name") or "",
            volume=dev.get("volume_percent") if dev.get("supports_volume", True) else None,
            shuffle=bool(r.get("shuffle_state")),
            repeat=r.get("repeat_state") or "off",
            context_uri=(r.get("context") or {}).get("uri"),
        )

    def user_id(self) -> str:
        if self._user_id is None:
            self._user_id = self._get("me")["id"]
        return self._user_id

    def playlists(self) -> list:
        out, r = [], self._get("me/playlists", limit=PAGE_SIZE)
        while r:
            for p in r.get("items") or []:
                if p and p.get("id"):
                    total = ((p.get("tracks") or p.get("items") or {}).get("total")) or 0
                    out.append(Playlist(p["id"], p["uri"], p.get("name") or "Untitled", total))
            r = self._get(r["next"]) if r.get("next") else None
        return out

    def liked_page(self, offset: int) -> Page:
        r = self._get("me/tracks", limit=PAGE_SIZE, offset=offset)
        return self._page(r, offset, lambda i: i.get("track"))

    def playlist_page(self, playlist_id: str, offset: int) -> Page:
        r = self._get(f"playlists/{playlist_id}/items", limit=PLAYLIST_PAGE_SIZE, offset=offset,
                      fields=_PLAYLIST_FIELDS, additional_types="track,episode")
        return self._page(r, offset, lambda i: i.get("item") or i.get("track"))

    def search_page(self, query: str, offset: int) -> Page:
        r = self._get("search", q=query, type="track", limit=SEARCH_PAGE_SIZE, offset=offset)
        return self._page((r or {}).get("tracks") or {}, offset, lambda i: i)

    @staticmethod
    def _page(r: dict, offset: int, get_item) -> Page:
        items = r.get("items") or []
        tracks = []
        for i, it in enumerate(items):
            t = _track(get_item(it) if it else None, offset + i, bool(it and it.get("is_local")))
            if t:
                tracks.append(t)
        total = r.get("total") or 0
        nxt = offset + len(items)
        return Page(tracks, total, nxt if r.get("next") and items else None)

    def devices(self) -> list:
        r = self._get("me/player/devices")
        return [
            Device(d["id"], d.get("name") or "Unknown", d.get("type") or "", bool(d.get("is_active")))
            for d in r.get("devices") or [] if d.get("id")
        ]

    def is_liked(self, track_id: str) -> bool:
        r = self._call("GET", "me/library/contains", {"uris": f"spotify:track:{track_id}"})
        return bool(r and r[0])

    # ── Commands ─────────────────────────────────────────────────────────────
    def _with_device(self, fn):
        """Run fn(device_id); if Spotify has no active device, retry on the first available one."""
        try:
            return fn(None)
        except ApiError as e:
            if e.status != 404:
                raise
        return fn(self._fallback_device().id)

    def _fallback_device(self) -> Device:
        """The active device, else SpoTerm's own (waiting while it starts up), else the first."""
        eng, delay, restarted = self.engine, 0.5, False
        deadline = time.monotonic() + ENGINE_WAIT
        while True:
            devs = self.devices()
            dev = (next((d for d in devs if d.is_active), None)
                   or next((d for d in devs if eng and d.name == eng.name), None))
            if dev:
                return dev
            if eng and eng.available() and not eng.needs_login() and time.monotonic() < deadline:
                if not eng.running() and not restarted:
                    # It died (or never started): restart it once per command, not every
                    # few seconds, so a crash loop can't respawn it while this waits.
                    eng.start()
                    restarted = True
                if eng.running():
                    time.sleep(delay)
                    delay = min(delay * 2, 4.0)
                    continue
            if devs:
                return devs[0]
            if eng and eng.enabled:
                why = {"login needed": "needs a login (restart SpoTerm)",
                       "not installed": "is not installed (librespot)"}.get(eng.status, "didn't start")
                raise NoDeviceError(f"SpoTerm player {why}. Open Spotify somewhere, or press d")
            raise NoDeviceError()

    def play(self, *, context_uri=None, uris=None, offset=None):
        body = {"context_uri": context_uri, "uris": uris, "offset": offset}
        body = {k: v for k, v in body.items() if v is not None}
        self._with_device(lambda dev: self._player("PUT", "play", dev, body=body))

    def play_liked(self, track: Track, fallback_uris: list):
        """Play inside the Liked Songs context so the queue continues; fall back to a URI list."""
        ctx = f"spotify:user:{self.user_id()}:collection"
        try:
            self.play(context_uri=ctx, offset={"uri": track.uri})
        except ApiError as e:
            if e.status in (403, 429) or (e.status == 404 and "device" in e.message.lower()):
                raise
            self.play(uris=fallback_uris)

    def _player(self, method: str, action: str, device_id: str | None = None, body=None, **params):
        params["device_id"] = device_id
        self._call(method, "me/player/" + action, params, body)

    def resume(self):
        self._with_device(lambda dev: self._player("PUT", "play", dev))

    def pause(self):
        self._player("PUT", "pause")

    def next(self):
        self._player("POST", "next")

    def previous(self):
        self._player("POST", "previous")

    def seek(self, ms: int):
        self._player("PUT", "seek", position_ms=max(0, int(ms)))

    def volume(self, pct: int):
        self._player("PUT", "volume", volume_percent=max(0, min(100, int(pct))))

    def shuffle(self, on: bool):
        self._player("PUT", "shuffle", state="true" if on else "false")

    def repeat(self, mode: str):
        self._player("PUT", "repeat", state=mode)

    def transfer(self, device_id: str, play: bool):
        self._call("PUT", "me/player", body={"device_ids": [device_id], "play": play})

    def set_liked(self, track_id: str, liked: bool):
        self._call("PUT" if liked else "DELETE", "me/library", {"uris": f"spotify:track:{track_id}"})
