"""Thin Spotify Web API layer that returns small typed objects instead of raw JSON.

The HTTPS requests themselves are made by the engine process (see engine.Engine.call),
so this process never loads TLS or an HTTP stack. Everything here blocks until the
answer arrives, so it is only ever called from worker threads.
"""

import time

PAGE_SIZE = 50
PLAYLIST_PAGE_SIZE = 100
SEARCH_PAGE_SIZE = 10     # Spotify rejects search limits above 10
_ITEM_FIELDS = ("uri,name,duration_ms,type,is_local,is_playable,linked_from(uri),artists(name),"
                "album(name),show(name)")
# Spotify renamed each playlist entry's "track" key to "item"; ask for both.
_PLAYLIST_FIELDS = f"total,next,items(is_local,item({_ITEM_FIELDS}),track({_ITEM_FIELDS}))"


# Plain __slots__ classes rather than dataclasses: dataclasses imports inspect, ast and
# tokenize, which cost more memory than the whole API layer.
class Track:
    __slots__ = ("uri", "name", "artists", "album", "duration_ms", "pos", "playable")

    def __init__(self, uri: str, name: str, artists: str, album: str, duration_ms: int,
                 pos: int, playable: bool = True):
        self.uri = uri
        self.name = name
        self.artists = artists
        self.album = album
        self.duration_ms = duration_ms
        self.pos = pos              # index within its source list (playlist offset)
        self.playable = playable

    @property
    def id(self) -> str:
        return self.uri.rsplit(":", 1)[-1]

    @property
    def is_track(self) -> bool:
        return self.uri.startswith("spotify:track:")


class Playlist:
    __slots__ = ("id", "uri", "name", "total")

    def __init__(self, id: str, uri: str, name: str, total: int):
        self.id, self.uri, self.name, self.total = id, uri, name, total


class Device:
    __slots__ = ("id", "name", "type", "is_active")

    def __init__(self, id: str, name: str, type: str, is_active: bool):
        self.id, self.name, self.type, self.is_active = id, name, type, is_active


class Playback:
    __slots__ = ("track", "is_playing", "progress_ms", "fetched_at", "device_id", "device_name",
                 "volume", "shuffle", "repeat", "context_uri")

    def __init__(self, track, is_playing: bool, progress_ms: int, fetched_at: float,
                 device_id, device_name: str, volume, shuffle: bool, repeat: str, context_uri):
        self.track: Track | None = track
        self.is_playing = is_playing
        self.progress_ms = progress_ms
        self.fetched_at = fetched_at      # time.monotonic() when progress_ms was valid
        self.device_id: str | None = device_id
        self.device_name = device_name
        self.volume: int | None = volume
        self.shuffle = shuffle
        self.repeat = repeat              # "off" | "context" | "track"
        self.context_uri: str | None = context_uri

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


class Page:
    __slots__ = ("tracks", "total", "next_offset")

    def __init__(self, tracks: list, total: int, next_offset):
        self.tracks, self.total, self.next_offset = tracks, total, next_offset


class ApiError(Exception):
    """A failed Web API call. `status` 0 means no HTTP answer (network, engine down)."""

    def __init__(self, status: int, message: str, reason: str = "", retry_after: float = 0.0,
                 login: bool = False):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status
        self.message = message
        self.reason = reason
        self.retry_after = retry_after
        self.login = login          # SpoTerm's sign-in has expired: restart to sign in again

    @property
    def http_status(self) -> int:
        return self.status


class NoDeviceError(Exception):
    """No device to play on. The message, when there is one, says why."""


def _num(v, default: int = 0) -> int:
    """Spotify's numbers, tolerating null, floats or strings."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _track(item: dict, pos: int, is_local: bool = False) -> Track | None:
    if not isinstance(item, dict) or not isinstance(item.get("uri"), str):
        return None
    # With a market, Spotify swaps in a regional copy of some tracks (relinking). Keep the
    # original URI: it is the one in the playlist, so playing "from this track" finds it,
    # and it matches what the player reports.
    linked = item.get("linked_from")
    uri = linked["uri"] if isinstance(linked, dict) and isinstance(linked.get("uri"), str)         and linked["uri"].startswith("spotify:") else item["uri"]
    if item.get("type") == "episode":
        artists = (item.get("show") or {}).get("name", "")
        album = ""
    else:
        artists = ", ".join(str(a.get("name") or "") for a in item.get("artists") or [] if isinstance(a, dict))
        album = (item.get("album") or {}).get("name") or ""
    return Track(
        uri=uri,
        name=str(item.get("name") or "Unknown"),
        artists=artists,
        album=album,
        duration_ms=max(0, _num(item.get("duration_ms"))),
        pos=pos,
        playable=not (is_local or item.get("is_local") or item.get("is_playable") is False),
    )


def describe_error(err: BaseException) -> str:
    """Turn an exception into a short, human-readable status message."""
    if isinstance(err, NoDeviceError):
        return str(err) or "No Spotify device found. Open Spotify somewhere, or press d"
    if isinstance(err, ApiError):
        if err.login:
            return "Your Spotify sign-in expired: restart SpoTerm to sign in again"
        if not err.status:
            return err.message or "Network error: can't reach Spotify"
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
    if isinstance(err, OSError):
        return "Network error: can't reach Spotify"
    return f"{type(err).__name__}: {err}"


class Spotify:
    def __init__(self, engine):
        self.engine = engine        # makes the HTTPS calls; see Engine.call
        self._user_id: str | None = None

    def _call(self, method: str, path: str, params: dict | None = None, body=None):
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        return self.engine.call(method, path, params, body)

    def _get(self, path: str, **params) -> dict:
        r = self._call("GET", path, params)
        return r if isinstance(r, dict) else {}

    # ── Reads ────────────────────────────────────────────────────────────────
    def playback(self) -> Playback | None:
        r = self._call("GET", "me/player", {"additional_types": "episode", "market": "from_token"})
        now = time.monotonic()
        if not isinstance(r, dict) or not r:
            return None
        dev = r.get("device") if isinstance(r.get("device"), dict) else {}
        vol = dev.get("volume_percent")
        return Playback(
            track=_track(r.get("item"), 0),
            is_playing=bool(r.get("is_playing")),
            progress_ms=max(0, _num(r.get("progress_ms"))),
            fetched_at=now,
            device_id=dev.get("id"),
            device_name=str(dev.get("name") or ""),
            volume=_num(vol) if vol is not None and dev.get("supports_volume", True) else None,
            shuffle=bool(r.get("shuffle_state")),
            repeat=r.get("repeat_state") or "off",
            context_uri=(r.get("context") or {}).get("uri") if isinstance(r.get("context"), dict) else None,
        )

    def user_id(self) -> str:
        if self._user_id is None:
            uid = self._get("me").get("id")
            if not uid:
                raise ApiError(0, "Spotify didn't say who you are")
            self._user_id = str(uid)
        return self._user_id

    def playlists(self) -> list:
        out, r = [], self._get("me/playlists", limit=PAGE_SIZE)
        while r:
            for p in r.get("items") or []:
                # The id goes into a request path, so only Spotify's base62 ids are taken.
                if (isinstance(p, dict) and isinstance(p.get("id"), str) and p["id"].isascii()
                        and p["id"].isalnum() and p.get("uri")):
                    counts = p.get("tracks") or p.get("items")
                    total = _num(counts.get("total")) if isinstance(counts, dict) else 0
                    out.append(Playlist(str(p["id"]), str(p["uri"]), str(p.get("name") or "Untitled"), total))
            nxt = r.get("next")
            r = self._get(nxt) if isinstance(nxt, str) and len(out) < 10_000 else None
        return out

    def liked_page(self, offset: int) -> Page:
        r = self._get("me/tracks", limit=PAGE_SIZE, offset=offset, market="from_token")
        return self._page(r, offset, lambda i: i.get("track"))

    def playlist_page(self, playlist_id: str, offset: int) -> Page:
        r = self._get(f"playlists/{playlist_id}/items", limit=PLAYLIST_PAGE_SIZE, offset=offset,
                      fields=_PLAYLIST_FIELDS, additional_types="track,episode", market="from_token")
        return self._page(r, offset, lambda i: i.get("item") or i.get("track"))

    def search_page(self, query: str, offset: int) -> Page:
        r = self._get("search", q=query, type="track", limit=SEARCH_PAGE_SIZE, offset=offset)
        tracks = r.get("tracks")
        return self._page(tracks if isinstance(tracks, dict) else {}, offset, lambda i: i)

    @staticmethod
    def _page(r: dict, offset: int, get_item) -> Page:
        items = r.get("items") if isinstance(r.get("items"), list) else []
        tracks = []
        for i, it in enumerate(items):
            ok = isinstance(it, dict)
            t = _track(get_item(it) if ok else None, offset + i, bool(ok and it.get("is_local")))
            if t:
                tracks.append(t)
        total = max(_num(r.get("total")), offset + len(items))
        nxt = offset + len(items)
        return Page(tracks, total, nxt if r.get("next") and items else None)

    def devices(self) -> list:
        r = self._get("me/player/devices")
        return [
            Device(str(d["id"]), str(d.get("name") or "Unknown"), str(d.get("type") or ""),
                   bool(d.get("is_active")))
            for d in r.get("devices") or [] if isinstance(d, dict) and d.get("id")
        ]

    def is_liked(self, track_id: str) -> bool:
        r = self._call("GET", "me/library/contains", {"uris": f"spotify:track:{track_id}"})
        return bool(isinstance(r, list) and r and r[0])

    # ── Commands ─────────────────────────────────────────────────────────────
    def _with_device(self, fn):
        """Run fn(device_id); if Spotify has no active device, retry on the first available one.

        Only used for remote devices: when nothing is active SpoTerm plays on its own
        engine instead, without going through the Web API at all.
        """
        try:
            return fn(None)
        except ApiError as e:
            if e.status != 404:
                raise
        devs = self.devices()
        if not devs:
            raise NoDeviceError()
        return fn(next((d for d in devs if d.is_active), devs[0]).id)

    def play(self, *, context_uri=None, uris=None, offset=None):
        body = {"context_uri": context_uri, "uris": uris, "offset": offset}
        body = {k: v for k, v in body.items() if v is not None}
        self._with_device(lambda dev: self._player("PUT", "play", dev, body=body))

    def play_on(self, device_id: str, *, context_uri: str, offset: dict | None = None):
        """Start a context on one given device (Spotify resolves the context server side)."""
        body = {"context_uri": context_uri, "offset": offset}
        self._player("PUT", "play", device_id, body={k: v for k, v in body.items() if v is not None})

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
