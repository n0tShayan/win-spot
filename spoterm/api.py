"""Thin Spotify Web API layer that returns small typed objects instead of raw JSON.

Everything here blocks on the network, so it is only ever called from worker threads.
"""

import time
from dataclasses import dataclass

import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheFileHandler

from .config import SCOPE, Settings

PAGE_SIZE = 50
PLAYLIST_PAGE_SIZE = 100
SEARCH_PAGE_SIZE = 10     # Spotify rejects search limits above 10
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
    pass


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
        return "No Spotify device found. Open Spotify somewhere, or press d"
    if isinstance(err, spotipy.SpotifyException):
        reason = (err.msg or "").rsplit(":", 1)[-1].strip() or f"HTTP {err.http_status}"
        if err.http_status == 404 and "device" in reason.lower():
            return "No active device. Press d to pick one"
        if err.http_status == 403:
            if "premium" in (err.msg or "").lower() or "premium" in str(err.reason or "").lower():
                return "Spotify Premium is required for playback control"
            return f"Not allowed: {reason}"
        if err.http_status == 429:
            return "Rate limited by Spotify, slowing down"
        return reason
    if isinstance(err, requests.exceptions.ConnectionError):
        return "Network error: can't reach Spotify"
    if isinstance(err, requests.exceptions.Timeout):
        return "Spotify took too long to respond"
    return f"{type(err).__name__}: {err}"


def make_auth(settings: Settings) -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        redirect_uri=settings.redirect_uri,
        scope=SCOPE,
        cache_handler=CacheFileHandler(cache_path=str(settings.token_path)),
        open_browser=True,
    )


class Spotify:
    def __init__(self, auth: SpotifyOAuth):
        self.sp = spotipy.Spotify(
            auth_manager=auth,
            requests_timeout=10,
            retries=2,
            status_retries=2,
            backoff_factor=0.5,
        )
        self._user_id: str | None = None

    # ── Reads ────────────────────────────────────────────────────────────────
    def playback(self) -> Playback | None:
        r = self.sp.current_playback(additional_types="episode")
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
            self._user_id = self.sp.me()["id"]
        return self._user_id

    def playlists(self) -> list:
        out, r = [], self.sp.current_user_playlists(limit=PAGE_SIZE)
        while r:
            for p in r.get("items") or []:
                if p and p.get("id"):
                    total = ((p.get("tracks") or p.get("items") or {}).get("total")) or 0
                    out.append(Playlist(p["id"], p["uri"], p.get("name") or "Untitled", total))
            r = self.sp.next(r) if r.get("next") else None
        return out

    def liked_page(self, offset: int) -> Page:
        r = self.sp.current_user_saved_tracks(limit=PAGE_SIZE, offset=offset)
        return self._page(r, offset, lambda i: i.get("track"))

    def playlist_page(self, playlist_id: str, offset: int) -> Page:
        r = self.sp.playlist_items(
            playlist_id, limit=PLAYLIST_PAGE_SIZE, offset=offset,
            fields=_PLAYLIST_FIELDS, additional_types=("track", "episode"),
        )
        return self._page(r, offset, lambda i: i.get("item") or i.get("track"))

    def search_page(self, query: str, offset: int) -> Page:
        r = self.sp.search(q=query, type="track", limit=SEARCH_PAGE_SIZE, offset=offset)
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
        r = self.sp.devices() or {}
        return [
            Device(d["id"], d.get("name") or "Unknown", d.get("type") or "", bool(d.get("is_active")))
            for d in r.get("devices") or [] if d.get("id")
        ]

    def is_liked(self, track_id: str) -> bool:
        return bool(self.sp.current_user_saved_tracks_contains([track_id])[0])

    # ── Commands ─────────────────────────────────────────────────────────────
    def _with_device(self, fn):
        """Run fn(device_id); if Spotify has no active device, retry on the first available one."""
        try:
            return fn(None)
        except spotipy.SpotifyException as e:
            if e.http_status != 404:
                raise
        devs = self.devices()
        if not devs:
            raise NoDeviceError()
        return fn(next((d for d in devs if d.is_active), devs[0]).id)

    def play(self, *, context_uri=None, uris=None, offset=None):
        self._with_device(lambda dev: self.sp.start_playback(
            device_id=dev, context_uri=context_uri, uris=uris, offset=offset))

    def play_liked(self, track: Track, fallback_uris: list):
        """Play inside the Liked Songs context so the queue continues; fall back to a URI list."""
        ctx = f"spotify:user:{self.user_id()}:collection"
        try:
            self.play(context_uri=ctx, offset={"uri": track.uri})
        except spotipy.SpotifyException as e:
            if e.http_status in (403, 429) or (e.http_status == 404 and "device" in (e.msg or "").lower()):
                raise
            self.play(uris=fallback_uris)

    def resume(self):
        self._with_device(lambda dev: self.sp.start_playback(device_id=dev))

    def pause(self):
        self.sp.pause_playback()

    def next(self):
        self.sp.next_track()

    def previous(self):
        self.sp.previous_track()

    def seek(self, ms: int):
        self.sp.seek_track(max(0, int(ms)))

    def volume(self, pct: int):
        self.sp.volume(max(0, min(100, int(pct))))

    def shuffle(self, on: bool):
        self.sp.shuffle(on)

    def repeat(self, mode: str):
        self.sp.repeat(mode)

    def transfer(self, device_id: str, play: bool):
        self.sp.transfer_playback(device_id, force_play=play)

    def set_liked(self, track_id: str, liked: bool):
        if liked:
            self.sp.current_user_saved_tracks_add([track_id])
        else:
            self.sp.current_user_saved_tracks_delete([track_id])
