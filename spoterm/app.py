"""The terminal UI: state, scheduling, input and rendering.

Design notes (why it stays near 0% CPU):
  * The UI thread sleeps inside getch; it only wakes for input, finished
    background jobs, the next once-a-second progress tick, or a timer.
  * When SpoTerm's own engine is the player, commands go down a pipe and state
    comes back as pushed events: no Web API calls and no polling at all.
  * For other devices, progress is interpolated locally and Spotify is polled
    every few seconds (or at track end) rather than continuously.
  * Only the progress line is redrawn each second; everything else is redrawn
    only when something actually changed.
  * All network I/O happens on two worker threads; all state lives on the UI
    thread, so there are no locks and no races.
"""

import json
import os
import queue
import subprocess
import sys
import time

try:
    import curses
except ImportError:
    sys.exit("curses is missing. On Windows run:  pip install windows-curses")

from . import api, config
from .engine import Engine
from .api import Playback, Track, describe_error
from .worker import Worker
from .ui import (ACCENT, DIM, FAINT, MARK, MUTED, SEL, SEL_ACCENT, SEL_DIM, TEXT, TITLE, WARN,
                 ASCII_GLYPHS, UNICODE_GLYPHS, Theme, fit, fmt_time, put, width)

POLL_PLAYING = 5.0     # seconds between polls while playing
POLL_IDLE = 10.0       # while paused or nothing is playing
POLL_ERROR = 15.0      # after a failed poll
VOLUME_STEP = 5
SEEK_STEP_MS = 10_000
PREFETCH_ROWS = 25     # load the next page when the selection gets this close to the end
MIN_H, MIN_W = 14, 60

ENTER = "ENTER"
ESC = "\x1b"
PADENTER = getattr(curses, "PADENTER", -999)
BACKSPACE = (curses.KEY_BACKSPACE, "\b", "\x7f", "\x08")


class Echo:
    """Remembers the last value we asked the engine for, so late echoes of earlier presses
    (volume 55, then 60, while you're already at 65) don't make the display jump back.
    While a change is pending, only an echo of that exact value is shown, whatever order
    the echoes arrive in. Outside the short window everything is shown as is, since it
    came from elsewhere (e.g. your phone)."""
    __slots__ = ("target", "until")

    def __init__(self):
        self.target, self.until = None, 0.0

    def sent(self, now: float, value) -> None:
        self.target, self.until = value, now + 1.5

    def accept(self, now: float, value) -> bool:
        return now >= self.until or value == self.target


class TrackList:
    # A plain __slots__ class: dataclasses would pull inspect, ast and tokenize into memory.
    __slots__ = ("key", "kind", "title", "context_uri", "playlist_id", "query", "tracks", "total",
                 "next_offset", "loading", "error", "stale", "locked", "gen", "sel", "top",
                 "retry_at", "want_end", "restore")

    def __init__(self, key: str, kind: str, title: str, context_uri: str | None = None,
                 playlist_id: str | None = None):
        self.key = key
        self.kind = kind                     # "liked" | "playlist" | "search"
        self.title = title
        self.context_uri = context_uri
        self.playlist_id = playlist_id
        self.query = ""
        self.gen = 0                         # bumped on reset so late results are dropped
        self.reset()
        self.gen = 0

    def reset(self) -> None:
        self.gen += 1
        self.tracks = []
        self.total = 0
        self.next_offset = 0
        self.loading = False
        self.error = ""
        self.stale = False
        self.locked = False
        self.sel = self.top = 0
        self.retry_at = 0.0
        self.want_end = False
        self.restore = None


class SideItem:
    __slots__ = ("kind", "label", "key", "playlist")

    def __init__(self, kind: str, label: str, key: str = "", playlist=None):
        self.kind = kind            # "section" | "gap" | "note" | "liked" | "search" | "playlist"
        self.label = label
        self.key = key
        self.playlist: api.Playlist | None = playlist

    @property
    def selectable(self) -> bool:
        return self.kind in ("liked", "search", "playlist")


HELP = (
    ("Playback", (
        ("space p", "play / pause"),
        ("n  b", "next / previous"),
        (",  .", "seek 10s"),
        ("-  +", "volume"),
        ("s  R", "shuffle / repeat"),
        ("f", "like track"),
        ("d", "devices"),
    )),
    ("Browse", (
        ("/", "search"),
        ("enter", "open / play"),
        ("tab h l", "switch pane"),
        ("j  k", "down / up"),
        ("g  G", "top / bottom"),
        ("[  ]", "prev / next list"),
        ("r", "reload"),
        ("q", "quit"),
    )),
)


class Layout:
    """Screen geometry for one terminal size (see App.layout)."""
    __slots__ = ("side_w", "top", "h", "x0", "w", "head_y", "list_y", "rows", "rule_y")

    def __init__(self, side_w, top, h, x0, w, head_y, list_y, rows, rule_y):
        self.side_w = side_w    # sidebar text width; its rows span columns 1 .. side_w + 2
        self.top = top          # first body row (the list title)
        self.h = h              # body height
        self.x0 = x0            # main pane text column (the selection marker sits at x0 - 1)
        self.w = w              # main pane text width (then one pad column and the scrollbar)
        self.head_y = head_y    # column header row
        self.list_y = list_y    # first track row
        self.rows = rows        # visible track rows
        self.rule_y = rule_y    # player rule; title, artist and progress rows follow it


class Cols:
    """Track-list column widths for one list and pane width (see App.columns)."""
    __slots__ = ("num_w", "title_w", "artist_w", "album_w", "dur_w", "head")

    def __init__(self, num_w, title_w, artist_w, album_w, dur_w, head):
        self.num_w, self.title_w, self.artist_w = num_w, title_w, artist_w
        self.album_w, self.dur_w = album_w, dur_w
        self.head = head        # the column header row, pre-rendered


_UNSET = object()   # "never drawn", distinct from any row key
_SKIPPED = object()  # a queued command that a newer one of the same kind replaced


def _scroll(sel: int, top: int, h: int, n: int) -> int:
    if h <= 0:
        return 0
    if sel < top:
        top = sel
    elif sel >= top + h:
        top = sel - h + 1
    return max(0, min(top, max(0, n - h)))


class App:
    def __init__(self, scr, spotify: api.Spotify, settings: config.Settings,
                 engine: Engine | None = None):
        self.scr = scr
        self.api = spotify
        self.engine = engine
        # SpoTerm's own player: "" (no engine), "off", "not installed", "login needed",
        # "starting", "ready", "stopped" or "exited (N)". Refreshed by timers().
        self.engine_status = engine.status if engine else ""
        self.g = ASCII_GLYPHS if settings.ascii else UNICODE_GLYPHS
        self.theme = Theme()

        self.results: queue.SimpleQueue = queue.SimpleQueue()
        self.ctl = Worker("spoterm-poll", self.results)      # playback state polls
        self.cmd = Worker("spoterm-command", self.results)   # commands: never stuck behind a poll
        self.data = Worker("spoterm-data", self.results)     # library lists, search, likes
        self.inflight = 0

        # playback
        self.pb: Playback | None = None
        self.liked_uri: str | None = None      # track the liked flag belongs to
        self.liked: bool | None = None
        self.poll_inflight = False
        self.next_poll = 0.0
        self.burst = 0                          # quick re-polls left after a command
        self.vol_target: int | None = None
        self.vol_deadline = 0.0
        self.seek_target: int | None = None
        self.seek_deadline = 0.0
        self.cmd_seq = 0                        # bumped per command; older polls are stale
        self.local = False                      # our engine is the device playing
        self.expect_until = 0.0                 # engine reply expected: wake quickly till then
        self.pending_play = None                # a play request waiting for the engine to start
        self.pending_until = 0.0
        self.busy_since = 0.0                   # when the UI started waiting on a reply
        self.engine_volume: int | None = None
        self.restarts: list = []                # recent engine restart times
        self.restart_at = 0.0
        # After a local play: ignore the engine's late events for anything but this track,
        # and ignore Spotify still naming the previous device, for a moment. Events held back
        # meanwhile are kept, so if the track never shows up the screen still ends up true.
        self.expect_uri: str | None = None
        self.expect_name = ""
        self.expect_deadline = 0.0
        self.expect_retry = None                # plan B if the engine starts the wrong track
        self.held_track: tuple | None = None    # (event, time) held back while expecting
        self.held_state: tuple | None = None
        self.local_grace_until = 0.0
        self.vol_echo, self.shuffle_echo, self.repeat_echo = Echo(), Echo(), Echo()
        self.play_echo, self.seek_echo = Echo(), Echo()
        # After a remote play or skip: (uri wanted, its name, uri to move away from, deadline).
        # Spotify reports the previous track for a moment; those polls are skipped.
        self.remote_expect: tuple | None = None
        self.remote_acked = False               # Spotify confirmed the remote play request
        self.remote_from: str | None = None     # what played before it
        self.remote_fix = None                  # a second try if another track starts instead
        self.cmd_gen: dict = {}                 # command key -> newest generation (see command)
        # Changes made on a remote device, shown until Spotify reports them (it lags):
        # field -> (value, deadline).
        self.holds: dict = {}
        self.user_id: str | None = None
        self.last_load: tuple | None = None     # (cmd, args) of the last local play, to resume it
        self.reload_needed = False              # the player reconnected: its queue is gone
        self.player_warned = False              # the player-offline notice was shown this outage
        self.poll_err = ""                      # last poll error shown (shown once, not per poll)

        # library
        self.playlists: list = []
        self.playlists_loaded = False
        self.side: list = []
        self.side_sel = 0
        self.side_top = 0
        self.lists: dict = {}
        self.cur = self._list("liked")
        self.focus = "list"

        # search input
        self.typing = False
        self.query = ""
        self.qcur = 0

        # overlays: None | "help" | "devices"
        self.overlay: str | None = None
        self.devices: list | None = None
        self.dev_sel = 0

        self.status = ""
        self.status_warn = False
        self.status_until = 0.0

        self.running = True
        self.dirty = True
        self.last_sec = -1
        self.cursor_at: tuple | None = None
        self.cursor_shown: bool | None = None
        self.H = self.W = 0
        self.too_small = False

    # ── Background jobs ──────────────────────────────────────────────────────
    def bg(self, worker, fn, done=None, fail=None) -> None:
        if not self.inflight:
            self.busy_since = time.monotonic()
        self.inflight += 1
        worker.submit(fn, done, fail if fail is not None else self.on_error)

    def drain(self) -> None:
        if self.engine:
            events = self.engine.events
            while True:
                try:
                    ev = events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.on_engine(ev)
                except Exception as e:
                    self.flash(f"Internal error: {describe_error(e)}", warn=True)
        while True:
            try:
                cb, value = self.results.get_nowait()
            except queue.Empty:
                return
            self.inflight -= 1
            self.dirty = True
            if cb:
                try:
                    cb(value)
                except Exception as e:   # a malformed response must never take the UI down
                    self.flash(f"Internal error: {describe_error(e)}", warn=True)

    def on_error(self, err: BaseException) -> None:
        self.flash(self.describe(err), warn=True)

    def describe(self, err: BaseException) -> str:
        """describe_error, but saying why nothing can play when our own player is down too."""
        no_dev = isinstance(err, api.NoDeviceError) or (
            getattr(err, "http_status", 0) == 404 and "device" in str(err).lower())
        if no_dev and self.engine and self.engine.status == "retrying":
            return ("Spotify lists no active device and SpoTerm's player can't connect "
                    "(Spotify service trouble). Retrying, press r to retry now")
        if no_dev:
            return "Spotify lists no active device. Open Spotify on a device, or press d"
        return describe_error(err)

    def player_offline(self) -> bool:
        """Our player is the device but its connection dropped: say so instead of acting."""
        if self.local and self.engine and not self.engine.ready:
            self.flash("SpoTerm's player is reconnecting to Spotify" + self.g["ell"], warn=True)
            return True
        return False

    def flash(self, msg: str, warn: bool = False) -> None:
        self.status, self.status_warn = msg, warn
        self.status_until = time.monotonic() + (5.0 if warn else 2.5)
        self.dirty = True

    # ── Built-in engine ──────────────────────────────────────────────────────
    def send(self, cmd: str, **args) -> bool:
        """Send a command to our own player if it is the active device."""
        if not (self.local and self.engine and self.engine.send(cmd, **args)):
            return False
        now = time.monotonic()
        if now >= self.expect_until:
            self.busy_since = now
        self.expect_until = now + 2.0
        return True

    def _local_pb(self, now: float) -> Playback:
        """The playback state of our own device, taking over the display from any other."""
        eng, pb = self.engine, self.pb
        if not self.local or pb is None or pb.device_id != eng.device_id:
            old = pb if pb is not None and pb.device_id == eng.device_id else None
            # Shuffle and repeat follow you from device to device, as in Spotify's own apps.
            pb = Playback(old.track if old else None, False, 0, now, eng.device_id, eng.name,
                          old.volume if old else self.engine_volume,
                          pb.shuffle if pb else False, pb.repeat if pb else "off", None)
            self.pb, self.local = pb, True
            self.holds.clear()
            self.remote_expect = None
            self.next_poll = float("inf")   # events keep us current: stop polling
        return pb

    def on_engine(self, ev: dict) -> None:
        # Every field is checked: a malformed event must never reach the screen.
        kind, now = ev.get("ev"), time.monotonic()
        text = lambda k, default="": ev[k] if isinstance(ev.get(k), str) else default
        num = lambda k: max(0, api._num(ev.get(k)))
        self.dirty = True
        if kind == "ready":
            if ev.get("volume") is not None:
                self.engine_volume = min(100, num("volume"))
            if self.player_warned:
                self.player_warned = False
                self.flash("SpoTerm's player is connected")
            if self.pending_play:
                fn, self.pending_play = self.pending_play, None
                fn()
            return
        if kind == "exit":
            was_local, self.local = self.local, False
            self.pending_play = self.expect_uri = self.expect_retry = None
            self.held_track = self.held_state = None
            self.next_poll = now        # polling was off while the engine played: resume it
            if was_local:
                self.pb = None
            # The engine carries both the player and every API call, so bring it back.
            # A crash loop gets three tries a minute, then waits for `r`.
            self.restarts = [t for t in self.restarts if now - t < 60] + [now]
            if len(self.restarts) <= 3:
                self.flash("SpoTerm's engine stopped: restarting it…", warn=True)
                self.restart_at = now + 1.0
            else:
                self.flash(f"SpoTerm's engine keeps stopping (see {self.engine.log_path}). "
                           "Press r to try again", warn=True)
            return
        if kind == "error":
            if ev.get("kind") != "login":
                self.flash(f"Player: {text('msg', 'command failed')}", warn=True)
            return
        if kind == "player":
            if ev.get("state") == "login":
                self.pending_play = None
                self.flash("SpoTerm's player needs its sign-in: restart SpoTerm", warn=True)
            elif ev.get("state") == "retrying":
                why = text("msg")
                if self.pending_play:
                    self.pending_play = None
                    self.player_warned = False      # this press deserves an answer
                if not self.player_warned:          # once per outage, not once per retry
                    self.player_warned = True
                    if "503" in why or "unavailable" in why.lower():
                        msg = ("Spotify's playback service is unavailable (their side), so "
                               "SpoTerm can't play here yet. It keeps retrying; other devices still work")
                    else:
                        msg = f"SpoTerm's player can't connect ({why or 'unknown error'}). Retrying"
                    self.flash(msg, warn=True)
                    self.status_until = now + 8.0
            return
        if kind == "reconnecting":
            if self.local and self.pb:
                self.pb.rebase(now)
                self.pb.is_playing = False  # the audio stopped with the connection
                self.reload_needed = True
            self.next_poll = min(self.next_poll, now + POLL_IDLE)   # events stopped: poll meanwhile
            self.flash("Connection to Spotify lost, reconnecting" + self.g["ell"], warn=True)
            return
        if kind == "reconnected":
            self.player_warned = False
            self.flash("Reconnected to Spotify" + (": press space to resume" if self.reload_needed else ""))
            return
        if kind == "active":
            if not ev.get("on") and self.local and now >= self.local_grace_until:
                self.poll_soon(0)       # playback may have moved to another device
            return
        if kind in ("volume", "shuffle", "repeat"):
            echo, value = {"volume": (self.vol_echo, min(100, num("pct"))),
                           "shuffle": (self.shuffle_echo, ev.get("on") is True),
                           "repeat": (self.repeat_echo, text("mode", "off"))}[kind]
            if not echo.accept(now, value):
                return                  # an echo of an earlier press; ours is on its way
            if kind == "volume":
                self.engine_volume = min(100, num("pct"))
            if not self.local or self.pb is None:
                return
            if kind == "volume":
                self.pb.volume = self.engine_volume
            elif kind == "shuffle":
                self.pb.shuffle = ev.get("on") is True
            else:
                mode = text("mode", "off")
                self.pb.repeat = mode if mode in ("off", "context", "track") else "off"
            return
        if kind not in ("track", "playing", "paused", "loading", "pos", "stopped", "end", "unavailable"):
            return

        if self.expect_uri and now >= self.expect_deadline:
            self.expect_done(now)
        if self.expect_uri:
            # Just after you pick a track, the engine is still reporting on the one before it
            # (stopped, paused, even "track" for it). Only the track you picked may update the
            # screen until it shows up. The rest is held back, not dropped: if the track never
            # shows up, expect_done shows what the engine is really playing.
            uri = text("uri")
            if uri == self.expect_uri or (kind == "track" and text("name") == self.expect_name):
                if kind in ("track", "playing", "paused", "unavailable"):
                    self.expect_uri = self.expect_retry = None
                    self.held_track = self.held_state = None
            elif kind == "track" and self.expect_retry:
                # The engine started some other track (librespot only sees the first page of
                # a long playlist): have Spotify start the right one on this device instead.
                retry, self.expect_retry = self.expect_retry, None
                self.held_track, self.held_state = (ev, now), None
                self.expect_deadline = now + 6.0
                retry()
                return
            else:
                if kind == "track":
                    self.held_track, self.held_state = (ev, now), None
                elif kind in ("playing", "paused", "pos", "stopped"):
                    self.held_state = (ev, now)
                return
        self.apply_engine(ev, now, now)

    def expect_done(self, now: float) -> None:
        """The track asked for never showed up: show what the engine is really doing."""
        held = (self.held_track, self.held_state)
        self.expect_uri = self.expect_retry = None
        self.held_track = self.held_state = None
        for h in held:
            if h:
                self.apply_engine(h[0], h[1], now)
        self.dirty = True

    def apply_engine(self, ev: dict, at: float, now: float) -> None:
        """Show one engine playback event that arrived at `at`."""
        kind = ev.get("ev")
        text = lambda k, default="": ev[k] if isinstance(ev.get(k), str) else default
        num = lambda k: max(0, api._num(ev.get(k)))
        uri = text("uri")
        if kind == "unavailable":
            # Also sent for a queued track that failed to preload: only speak up for this one.
            pb = self.pb
            if self.local and pb and pb.track and pb.track.uri == uri:
                self.flash("That track isn't available here, skipping", warn=True)
            return
        if kind == "end":
            return
        if kind in ("playing", "paused") and not self.play_echo.accept(now, kind == "playing"):
            return                      # echo of an earlier play/pause press
        if kind == "pos" and not self.seek_echo.accept(now, num("pos")):
            return                      # echo of an earlier seek

        pb = self._local_pb(now)
        if kind == "track":
            if not (pb.track and pb.track.uri == uri):   # same track: keep it, nothing to redraw
                pb.track = Track(uri, text("name") or "Unknown", text("artists"), text("album"),
                                 num("duration_ms"), 0)
            pb.progress_ms, pb.fetched_at = 0, now
            self.track_changed(pb.track)
        elif kind in ("playing", "paused", "loading", "pos"):
            if kind == "playing":
                pb.is_playing = True
            elif kind == "paused":
                pb.is_playing = False
            late = int((now - at) * 1000) if pb.is_playing and kind != "loading" else 0
            pb.progress_ms, pb.fetched_at = num("pos") + late, now
        elif kind == "stopped":
            pb.rebase(now)
            pb.is_playing = False
            if now >= self.local_grace_until:
                self.poll_soon(0)       # stopped often means another device took over

    def track_changed(self, track: Track | None) -> None:
        """Reset the like flag for a new current track and look it up."""
        if track and track.uri != self.liked_uri:
            self.liked_uri, self.liked = track.uri, None
            if track.is_track:
                uri, tid = track.uri, track.id
                def fail(err, uri=uri):
                    if uri == self.liked_uri:
                        self.liked_uri = None   # look it up again next time instead of sticking
                self.bg(self.data, lambda: self.api.is_liked(tid),
                        lambda v: self._set_liked(uri, v), fail)

    # ── Playback polling ─────────────────────────────────────────────────────
    def poll(self) -> None:
        self.poll_inflight = True
        self.next_poll = float("inf")
        seq = self.cmd_seq
        self.bg(self.ctl, self.api.playback, lambda pb: self.on_playback(pb, seq), self.on_poll_error)

    def poll_soon(self, bursts: int = 2, delay: float = 0.35) -> None:
        """Spotify applies commands with a short lag, so re-check a couple of times."""
        self.burst = max(self.burst, bursts)
        if not self.poll_inflight:
            self.next_poll = min(self.next_poll, time.monotonic() + delay)

    def on_playback(self, pb: Playback | None, seq: int = -1) -> None:
        self.poll_inflight = False
        self.poll_err = ""
        now = time.monotonic()
        self.next_poll = now + POLL_ERROR   # never left at inf if the rest of this raises
        if seq != self.cmd_seq:
            # Started before the latest command: it would undo the instant update. Ask again.
            self.next_poll = now + 0.6
            return
        eng_id = self.engine.device_id if self.engine else None
        if self.local:
            if pb is None or (eng_id and pb.device_id == eng_id):
                # Still ours: the engine's events are fresher, unless it's reconnecting.
                self.next_poll = float("inf") if self.engine.ready else now + POLL_IDLE
                return
            if now < self.local_grace_until:
                # Spotify takes a few seconds to notice we took over; ask again after that.
                self.next_poll = self.local_grace_until + 0.5
                return
            self.local = False                  # playback moved to another device
        exp = self.remote_expect
        if exp:
            want, want_name, avoid, deadline = exp
            t = pb.track if pb else None
            if now >= deadline or (t and (t.uri == want or t.name == want_name if want
                                          else t.uri != avoid)):
                self.remote_expect = self.remote_fix = None
            elif want and t and self.remote_acked and self.remote_fix and t.uri != self.remote_from:
                # Spotify confirmed the request, yet a third track is playing: it started the
                # wrong one (seen with shuffle on phones). Ask once more, the other way.
                if config.DEBUG:
                    config.debug(f"wrong track after play: got {t.name!r} {t.uri}, wanted {want}")
                fix, self.remote_fix, self.remote_acked = self.remote_fix, None, False
                self.remote_from = t.uri
                self.remote_expect = (want, want_name, None, now + 8.0)
                self.command(fix, key="select", then=lambda: setattr(self, "remote_acked", True))
                self.next_poll = now + 0.7
                return
            else:
                self.next_poll = now + 0.7      # still the previous track: Spotify is lagging
                return
        if pb and self.vol_target is not None:
            pb.volume = self.vol_target
        pending = bool(pb and self.holds and self.apply_holds(pb, now))
        old = self.pb.track if self.pb else None
        if pb and pb.track and old and old.uri == pb.track.uri and old.name == pb.track.name \
                and old.duration_ms == pb.track.duration_ms:
            pb.track = old      # the same object: the player rows aren't redrawn for nothing
        self.pb = pb
        track = pb.track if pb else None
        self.track_changed(track)
        if pending:
            self.next_poll = now + 0.7          # re-check soon until Spotify catches up
            return

        if self.burst > 0:
            self.burst -= 1
            delay = 0.8
        elif pb and pb.is_playing and track and track.duration_ms:
            remaining = (track.duration_ms - pb.progress(now)) / 1000
            delay = min(POLL_PLAYING, max(0.5, remaining + 0.4))
        elif pb and pb.is_playing:
            delay = POLL_PLAYING
        else:
            delay = POLL_IDLE
        self.next_poll = now + delay

    def hold(self, field: str, value, secs: float = 4.0) -> None:
        """Keep showing a change made on a remote device until Spotify confirms it."""
        if not self.local:
            self.holds[field] = (value, time.monotonic() + secs)

    def apply_holds(self, pb: Playback, now: float) -> bool:
        """Overlay unconfirmed remote changes on a poll result. True if any are still pending."""
        pending = False
        for field, (value, deadline) in list(self.holds.items()):
            if field == "progress":
                got = pb.progress(now)
                want = value + (int((now - (deadline - 4.0)) * 1000) if pb.is_playing else 0)
                confirmed = abs(got - want) < 3000
            else:
                confirmed = getattr(pb, field) == value
            if confirmed or now >= deadline:
                del self.holds[field]
                continue
            pending = True
            if field == "progress":
                old = self.pb
                pb.progress_ms = old.progress(now) if old else value
                pb.fetched_at = now
            else:
                setattr(pb, field, value)
        return pending

    def on_poll_error(self, err: BaseException) -> None:
        self.poll_inflight = False
        # Honour a long 429 Retry-After (net.py only waits out short ones itself).
        wait = min(max(POLL_ERROR, getattr(err, "retry_after", 0) or 0), 300.0)
        self.next_poll = time.monotonic() + wait
        msg = self.describe(err)
        if msg != self.poll_err:        # once, not every 15 s while it lasts
            self.poll_err = msg
            self.flash(msg, warn=True)

    def _set_liked(self, uri: str, value: bool) -> None:
        if uri == self.liked_uri:
            self.liked = value

    # ── Playback commands ────────────────────────────────────────────────────
    def command(self, fn, ok_msg: str | None = None, key: str | None = None,
                then=None, failed=None) -> None:
        """Send a Web API command. Spotify only answers once the device has applied it
        (1-2.5 s for a phone), and commands run one at a time, so a newer command with the
        same `key` (play/pause, shuffle, ...) replaces one still waiting its turn."""
        self.cmd_seq += 1
        run = fn
        if key:
            gen = self.cmd_gen[key] = self.cmd_gen.get(key, 0) + 1

            def run():
                if self.cmd_gen.get(key) != gen:    # read on the worker: newer press queued
                    return _SKIPPED
                return fn()

        def done(r):
            if r is _SKIPPED:
                return
            if ok_msg:
                self.flash(ok_msg)
            # Applied on the device by now. Keep showing what was asked for until a poll
            # agrees, counting from now rather than from the key press.
            now = time.monotonic()
            for f, (v, d) in list(self.holds.items()):
                if f != "progress":     # its deadline also dates the seek (see apply_holds)
                    self.holds[f] = (v, max(d, now + 2.5))
            if then:
                then()
            self.poll_soon(1, delay=0.25)

        def fail(err):
            if config.DEBUG:
                config.debug(f"command failed: {err!r}")
            if failed and failed(err):
                return
            self.flash(self.describe(err), warn=True)
            self.poll_soon(1)   # undo optimistic changes with real state

        self.bg(self.cmd, run, done, fail)

    def toggle_play(self) -> None:
        pb, now = self.pb, time.monotonic()
        if self.player_offline():
            return
        if pb:
            pb.rebase(now)
        if self.local and self.reload_needed and pb and pb.track and not pb.is_playing \
                and self.last_load:
            self.resume_load(pb, now)
            return
        if pb and pb.is_playing:
            pb.is_playing = False
            if self.send("pause"):
                self.play_echo.sent(now, False)
            else:
                self.hold("is_playing", False)
                self.command(self.api.pause, key="playpause")
        elif (not pb or not pb.device_id) and self.engine_can_play():
            self.play_selected()        # nothing to resume anywhere: play the highlighted track here
        else:
            if pb and pb.track:
                pb.is_playing = True
            if self.send("play"):
                self.play_echo.sent(now, True)
            else:
                self.hold("is_playing", True)
                self.command(self.api.resume, key="playpause")
        self.dirty = True

    def resume_load(self, pb: Playback, now: float) -> None:
        """After a reconnect the player has no queue: load the last list again, where it was."""
        cmd, args = self.last_load
        args = dict(args, position_ms=pb.progress(now))
        args["start_uri" if cmd == "play_tracks" else "track_uri"] = pb.track.uri
        self.local_grace_until = now + 8.0
        if self.send(cmd, shuffle=pb.shuffle, repeat=pb.repeat, **args):
            self.reload_needed = False
            pb.is_playing = True
            self.play_echo.until = 0.0
        self.dirty = True

    def engine_can_play(self) -> bool:
        return bool(self.engine and (self.engine.ready or self.engine.status == "starting"))

    def skip(self, forward: bool) -> None:
        pb, now = self.pb, time.monotonic()
        if self.player_offline():
            return
        # A pending seek belongs to the track being left: don't apply it to the next one.
        self.seek_target, self.seek_deadline = None, 0.0
        self.holds.pop("progress", None)
        # "previous" past the first 3 seconds restarts the same track.
        changes = bool(pb and pb.track and (forward or pb.progress(now) < 3000))
        if pb and pb.track:
            pb.progress_ms, pb.fetched_at = 0, now   # instant feedback
        if not self.send("next" if forward else "prev"):
            if changes and not self.local:
                # Until Spotify moves on, polls still name the old track: don't show it again.
                self.remote_expect = (None, None, pb.track.uri, now + 4.0)
            self.command(self.api.next if forward else self.api.previous)
        self.dirty = True

    def seek(self, delta_ms: int) -> None:
        pb = self.pb
        if self.player_offline():
            return
        if not pb or not pb.track:
            return
        now = time.monotonic()
        pos = max(0, pb.progress(now) + delta_ms)
        if pb.track.duration_ms > 1000:   # 0 means unknown: don't clamp everything to 0
            pos = min(pos, pb.track.duration_ms - 1000)
        pb.progress_ms, pb.fetched_at = pos, now
        self.dirty = True
        self.cmd_seq += 1   # a poll already in flight predates this seek: don't let it snap back
        if self.send("seek", ms=pos):
            self.seek_echo.sent(now, pos)
            return
        self.seek_target, self.seek_deadline = pos, now + 0.3   # debounce held keys into one request
        self.hold("progress", pos)

    def change_volume(self, delta: int) -> None:
        pb = self.pb
        if self.player_offline():
            return
        # pb may be gone (a poll found nothing playing) while a volume request is still pending.
        base = (self.vol_target if self.vol_target is not None else pb.volume) if pb else None
        if base is None:
            self.flash("Volume can't be changed on this device" if pb else "Nothing is playing", warn=True)
            return
        self.vol_target = max(0, min(100, base + delta))
        pb.volume = self.vol_target
        self.dirty = True
        if self.send("volume", pct=self.vol_target):
            self.vol_target = None                    # local: applied instantly, no debounce
            self.vol_echo.sent(time.monotonic(), pb.volume)
            return
        self.vol_deadline = time.monotonic() + 0.25   # debounce key repeats into one request

    def flush_volume(self) -> None:
        v, self.vol_deadline = self.vol_target, 0.0

        def done(_):
            if self.vol_target == v:
                self.vol_target = None
                self.hold("volume", v)

        def fail(err):
            self.vol_target = None
            self.flash(self.describe(err), warn=True)
            self.poll_soon(1)

        self.cmd_seq += 1
        self.bg(self.cmd, lambda: self.api.volume(v), done, fail)

    def flush_seek(self) -> None:
        pos, self.seek_target, self.seek_deadline = self.seek_target, None, 0.0
        self.command(lambda: self.api.seek(pos), key="seek")

    def toggle_shuffle(self) -> None:
        if self.player_offline():
            return
        if not self.pb:
            return
        self.pb.shuffle = on = not self.pb.shuffle
        if self.send("shuffle", on=on):
            self.shuffle_echo.sent(time.monotonic(), on)
        else:
            self.hold("shuffle", on)
            self.command(lambda: self.api.shuffle(on), key="shuffle")
        self.dirty = True

    def cycle_repeat(self) -> None:
        if self.player_offline():
            return
        if not self.pb:
            return
        nxt = {"off": "context", "context": "track"}.get(self.pb.repeat, "off")
        self.pb.repeat = nxt
        if self.send("repeat", mode=nxt):
            self.repeat_echo.sent(time.monotonic(), nxt)
        else:
            self.hold("repeat", nxt)
            self.command(lambda: self.api.repeat(nxt), key="repeat")
        self.dirty = True

    def toggle_like(self) -> None:
        pb = self.pb
        if not pb or not pb.track or not pb.track.is_track:
            return
        if self.liked is None or pb.track.uri != self.liked_uri:
            self.flash("Still checking whether this track is liked…")
            self.track_changed(pb.track)
            return
        track, uri, tid, new = pb.track, pb.track.uri, pb.track.id, not self.liked
        self.liked = new

        def done(_):
            self.flash("Added to Liked Songs" if new else "Removed from Liked Songs")
            liked = self.lists.get("liked")
            if liked and liked.tracks:     # keep the open list in step instead of reloading it
                if new and all(x.uri != uri for x in liked.tracks):
                    liked.tracks.insert(0, track)
                    liked.total += 1
                    if liked.sel or liked.tracks[1:]:
                        liked.sel += 1
                elif not new:
                    keep = [x for x in liked.tracks if x.uri != uri]
                    liked.total -= len(liked.tracks) - len(keep)
                    liked.tracks = keep
                    liked.sel = min(liked.sel, max(0, len(keep) - 1))

        def fail(err):
            self._set_liked(uri, not new)
            self.flash(self.describe(err), warn=True)

        self.bg(self.cmd, lambda: self.api.set_liked(tid, new), done, fail)
        self.dirty = True

    def play_selected(self) -> None:
        tl = self.cur
        t: Track | None = tl.tracks[tl.sel] if tl.tracks else None
        if t is None and not (tl.kind == "playlist" and not tl.loading):
            return
        if t is not None and not t.playable:
            self.flash("This track can't be played (local file or not available)", warn=True)
            return
        pb = self.pb
        eng_id = self.engine.device_id if self.engine else None
        # Whichever device Spotify says is active gets the song, paused or not, as in
        # Spotify's own apps: pausing your phone doesn't mean "play on this PC next".
        # SpoTerm's own player takes over only when no device is active at all.
        remote = bool(pb and not self.local and pb.device_id and pb.device_id != eng_id)
        if config.DEBUG:
            config.debug(f"play {tl.key} sel={tl.sel} -> {t.name if t else None!r} "
                         f"{t.uri if t else ''} pos={t.pos if t else ''} "
                         f"{'remote ' + pb.device_name if remote else 'here'} "
                         f"shuffle={pb.shuffle if pb else None}")
        if not remote and self.engine_can_play():
            self.play_here(tl, t)
            return
        if t is None:
            ctx = tl.context_uri
            self.command(lambda: self.api.play(context_uri=ctx), key="select")
            return
        shuffle = bool(pb and pb.shuffle)
        following = [x.uri for x in tl.tracks[tl.sel:tl.sel + 100] if x.playable]
        fix = lambda: self.api.play(uris=following)     # plan B: exactly this track, then the rest
        if tl.kind == "playlist":
            # Start from the track itself, not its position: with shuffle on, Spotify applies
            # a position offset to the shuffled order and starts a neighbouring track. Only a
            # track that is in the playlist twice needs its position (which one you picked).
            ctx, pos = tl.context_uri, t.pos
            twice = sum(x.uri == t.uri for x in tl.tracks) > 1
            off = {"position": pos} if twice and not shuffle else {"uri": t.uri}
            fn = lambda: self.api.play(context_uri=ctx, offset=off)
            if not shuffle and "uri" in off:
                fix = lambda: self.api.play(context_uri=ctx, offset={"position": pos})
        elif tl.kind == "liked":
            fn = lambda: self.api.play_liked(t, following)
        else:
            fn = fix

        now = time.monotonic()
        ctx = self.list_context(tl)
        self.remote_from = pb.track.uri if pb and pb.track else None
        if self.pb:   # optimistic: show the new track immediately
            self.pb.track, self.pb.progress_ms, self.pb.fetched_at, self.pb.is_playing = t, 0, now, True
            self.pb.context_uri = ctx
        else:
            self.pb = Playback(t, True, 0, now, None, "", None, False, "off", ctx)
        self.track_changed(t)
        self.remote_expect = (t.uri, t.name, None, now + 8.0)
        self.remote_acked, self.remote_fix = False, fix
        self.seek_target, self.seek_deadline = None, 0.0
        self.holds.pop("progress", None)
        self.hold("is_playing", True)

        def acked():
            self.remote_acked = True
            exp = self.remote_expect
            if exp and exp[0] == t.uri:     # give the phone time to report it
                self.remote_expect = exp[:3] + (max(exp[3], time.monotonic() + 4.0),)

        def failed(err):
            # The phone went away (app closed): play here instead of just failing.
            gone = isinstance(err, api.NoDeviceError) or (
                getattr(err, "http_status", 0) == 404 and "device" in str(err).lower())
            if gone and self.engine_can_play() and self.cur is tl:
                self.remote_expect = self.remote_fix = None
                self.pb = None
                self.play_here(tl, t)
                return True
            return False

        self.command(fn, key="select", then=acked, failed=failed)
        self.dirty = True

    def list_context(self, tl: TrackList) -> str | None:
        """The context URI Spotify reports while this list plays (marks it in the sidebar)."""
        if tl.kind == "liked":
            return f"spotify:user:{self.user_id}:collection" if self.user_id else None
        return tl.context_uri

    def play_here(self, tl: TrackList, t: Track | None) -> None:
        """Play on SpoTerm's own engine: one line down a pipe, no Web API call."""
        def go():
            now = time.monotonic()
            pb = self.pb
            # Shuffle and repeat carry over (a load would otherwise reset them to off).
            shuffle, repeat = (pb.shuffle, pb.repeat) if pb else (False, "off")
            if tl.kind == "playlist":
                cmd, args = "play_context", {"uri": tl.context_uri, "track_uri": t.uri if t else None}
            else:
                # Liked Songs and search results play as a track list (what Myx does too), so
                # the queue runs through everything loaded and shuffle covers all of it.
                cmd, args = "play_tracks", {"uris": [x.uri for x in tl.tracks if x.playable],
                                            "start_uri": t.uri}
            self.local = True
            self.local_grace_until = now + 8.0
            pb = self._local_pb(now)
            pb.context_uri = self.list_context(tl)
            pb.shuffle, pb.repeat = shuffle, repeat
            self.play_echo.until = self.seek_echo.until = 0.0   # earlier presses are moot now
            self.seek_target, self.seek_deadline = None, 0.0
            self.held_track = self.held_state = self.expect_retry = None
            if t is not None:
                pb.track, pb.progress_ms, pb.fetched_at, pb.is_playing = t, 0, now, True
                self.track_changed(t)
                self.expect_uri, self.expect_name, self.expect_deadline = t.uri, t.name, now + 5.0
                dev = self.engine.device_id
                if tl.kind == "playlist" and dev:
                    ctx, off = tl.context_uri, {"uri": t.uri}
                    self.expect_retry = lambda: self.bg(
                        self.cmd, lambda: self.api.play_on(dev, context_uri=ctx, offset=off))
            else:
                self.expect_uri = None
            self.last_load, self.reload_needed = (cmd, args), False
            if not self.send(cmd, shuffle=shuffle, repeat=repeat, **args):
                self.local = False
                self.expect_uri = self.expect_retry = None
                self.flash("SpoTerm's player isn't running", warn=True)
            self.dirty = True

        if self.engine.ready:
            go()
        else:
            now = time.monotonic()
            self.pending_play, self.pending_until, self.busy_since = go, now + 20.0, now
            self.flash("Starting SpoTerm's player…")

    def open_devices(self) -> None:
        self.overlay, self.devices, self.dev_sel = "devices", None, 0

        def done(devs):
            if self.overlay == "devices":
                self.devices = devs
                self.dev_sel = next((i for i, d in enumerate(devs) if d.is_active), 0)

        def fail(err):
            self.devices = []
            self.on_error(err)

        self.bg(self.data, self.api.devices, done, fail)

    def pick_device(self) -> None:
        if not self.devices:
            return
        d = self.devices[self.dev_sel]
        self.overlay = None
        play = not (self.pb and not self.pb.is_playing)
        self.command(lambda: self.api.transfer(d.id, play),
                     f"{'Playing' if play else 'Switched'} on {d.name}")

    # ── Library ──────────────────────────────────────────────────────────────
    def _list(self, key: str) -> TrackList:
        tl = self.lists.get(key)
        if tl is None:
            if key == "liked":
                tl = TrackList(key, "liked", "Liked Songs")
            elif key == "search":
                tl = TrackList(key, "search", "Search")
            else:
                p = next(p for p in self.playlists if "pl:" + p.id == key)
                tl = TrackList(key, "playlist", p.name, context_uri=p.uri, playlist_id=p.id)
            self.lists[key] = tl
        return tl

    def load_playlists(self) -> None:
        def done(pls):
            self.playlists, self.playlists_loaded = pls, True
            self.build_side()

        def fail(err):
            self.playlists_loaded = True
            self.build_side()
            self.on_error(err)

        self.bg(self.data, self.api.playlists, done, fail)

    def build_side(self) -> None:
        keep = self.side[self.side_sel].key if self.side else self.cur.key
        items = [
            SideItem("section", "Library"),
            SideItem("liked", "Liked Songs", "liked"),
            SideItem("search", "Search", "search"),
            SideItem("gap", ""),
            SideItem("section", "Playlists"),
        ]
        if not self.playlists:
            items.append(SideItem("note", "No playlists" if self.playlists_loaded else "Loading…"))
        items += [SideItem("playlist", p.name, "pl:" + p.id, p) for p in self.playlists]
        self.side = items
        self.side_sel = next((i for i, it in enumerate(items) if it.key == keep), 1)

    def open_list(self, key: str) -> None:
        tl = self._list(key)
        if tl.stale:
            tl.reset()
        self.cur = tl
        if tl.kind != "search" or tl.query:
            self.load_more(tl)

    def load_more(self, tl: TrackList) -> None:
        if tl.loading or tl.next_offset is None or time.monotonic() < tl.retry_at:
            return
        tl.loading, off, gen = True, tl.next_offset, tl.gen
        if tl.kind == "liked":
            fn = lambda: self.api.liked_page(off)
        elif tl.kind == "playlist":
            pid = tl.playlist_id
            fn = lambda: self.api.playlist_page(pid, off)
        else:
            q = tl.query
            fn = lambda: self.api.search_page(q, off)

        def done(page: api.Page):
            if tl.gen != gen:
                return
            tl.loading, tl.error, tl.retry_at = False, "", 0.0
            tl.tracks.extend(page.tracks)
            tl.total, tl.next_offset = page.total, page.next_offset
            if tl.restore is not None and tl.tracks:
                tl.sel = min(tl.restore, len(tl.tracks) - 1)
                if tl.sel == tl.restore or tl.next_offset is None:
                    tl.restore = None
            if tl.want_end and tl.tracks:
                tl.sel = len(tl.tracks) - 1
                if tl.next_offset is None:
                    tl.want_end = False
            self.prefetch(tl)

        def fail(err):
            if tl.gen != gen:
                return
            tl.loading = False
            if tl.kind == "playlist" and getattr(err, "http_status", None) == 403 and not tl.tracks:
                tl.locked = True
                tl.next_offset = None
            elif tl.tracks and getattr(err, "http_status", None) in (400, 404):
                tl.next_offset = None       # past what Spotify will page through (e.g. search cap)
            else:
                tl.error = describe_error(err)
                tl.retry_at = time.monotonic() + 5.0
                tl.want_end = False
                if tl.tracks:
                    self.flash(f"Couldn't load more: {tl.error}", warn=True)

        self.bg(self.data, fn, done, fail)

    def prefetch(self, tl: TrackList) -> None:
        if tl.next_offset is not None and (tl.want_end or tl.restore is not None
                                           or tl.sel >= len(tl.tracks) - PREFETCH_ROWS):
            self.load_more(tl)
        elif tl.next_offset is not None and len(tl.tracks) < self.list_rows():
            self.load_more(tl)

    def refresh(self) -> None:
        if self.engine and not self.engine.running():
            self.restarts.clear()
            self.bg(self.data, self.engine.restart, fail=self.on_error)
        elif self.engine and self.engine.status == "retrying" and self.engine.reconnect():
            self.player_warned = False      # report how this attempt goes
            self.flash("Retrying SpoTerm's player" + self.g["ell"])
        self.load_playlists()
        tl = self.cur
        if tl.kind != "search" or tl.query:
            keep = tl.sel
            tl.reset()
            tl.restore = keep or None
            self.load_more(tl)
        for other in self.lists.values():
            if other is not tl:
                other.stale = True
        self.poll_soon(0)

    def start_search(self) -> None:
        if self.cur.kind != "search":
            self.before_search = (self.cur, self.side_sel, self.focus)
        self.side_sel = next(i for i, it in enumerate(self.side) if it.key == "search")
        self.cur = self._list("search")
        self.typing, self.focus = True, "list"
        self.query, self.qcur = self.cur.query, len(self.cur.query)

    def submit_search(self) -> None:
        self.typing = False
        q = self.query.strip()
        if not q:
            return
        tl = self._list("search")
        tl.query, tl.title = q, "Search"
        tl.reset()
        self.load_more(tl)

    # ── Navigation ───────────────────────────────────────────────────────────
    def list_rows(self) -> int:
        return max(1, self.layout().rows)

    def move(self, delta: int) -> None:
        if self.focus == "side":
            step, i, target = (1 if delta > 0 else -1), self.side_sel, self.side_sel
            for _ in range(abs(delta)):
                i += step
                while 0 <= i < len(self.side) and not self.side[i].selectable:
                    i += step
                if not 0 <= i < len(self.side):
                    break
                target = i
            self.side_sel = target
        else:
            tl = self.cur
            if tl.tracks:
                tl.sel = max(0, min(len(tl.tracks) - 1, tl.sel + delta))
                self.prefetch(tl)
        self.dirty = True

    def jump(self, end: bool) -> None:
        tl = self.cur
        if self.focus == "list":
            tl.want_end = end and tl.next_offset is not None
            tl.restore = None
        self.move(10**9 if end else -10**9)

    def activate(self) -> None:
        if self.focus == "side":
            it = self.side[self.side_sel]
            if it.kind == "search":
                self.start_search()
            elif it.selectable:
                self.open_list(it.key)
                self.focus = "list"
        else:
            self.play_selected()
        self.dirty = True

    # ── Input ────────────────────────────────────────────────────────────────
    def handle_key(self, key) -> None:
        if key in ("\n", "\r", curses.KEY_ENTER, PADENTER):
            key = ENTER
        if key == curses.KEY_RESIZE:
            self.resize()
            return
        if config.DEBUG:
            tl = self.cur
            t = tl.tracks[tl.sel] if tl.sel < len(tl.tracks) else None
            config.debug(f"key {key!r} focus={self.focus} list={tl.key} sel={tl.sel} top={tl.top} "
                         f"at={t.name if t else None!r}")
        self.dirty = True
        if self.typing:
            self.key_typing(key)
        elif self.overlay:
            self.key_overlay(key)
        else:
            self.key_normal(key)

    def key_typing(self, key) -> None:
        q, c = self.query, self.qcur
        if key == ENTER:
            self.submit_search()
        elif key == ESC:
            self.typing = False
            back = getattr(self, "before_search", None)
            if back and not self.cur.query:
                self.cur, self.side_sel, self.focus = back
            self.before_search = None
        elif key in BACKSPACE:
            if c > 0:
                self.query, self.qcur = q[:c - 1] + q[c:], c - 1
        elif key == curses.KEY_DC:
            self.query = q[:c] + q[c + 1:]
        elif key == curses.KEY_LEFT:
            self.qcur = max(0, c - 1)
        elif key == curses.KEY_RIGHT:
            self.qcur = min(len(q), c + 1)
        elif key in (curses.KEY_HOME, "\x01"):
            self.qcur = 0
        elif key in (curses.KEY_END, "\x05"):
            self.qcur = len(q)
        elif key == "\x15":   # ctrl+u
            self.query, self.qcur = "", 0
        elif isinstance(key, str) and key.isprintable():
            self.query, self.qcur = q[:c] + key + q[c:], c + len(key)

    def key_overlay(self, key) -> None:
        if key in (ESC, "q", "?") or (key == "d" and self.overlay == "devices"):
            self.overlay = None
        elif self.overlay == "devices" and self.devices:
            if key in ("j", curses.KEY_DOWN):
                self.dev_sel = min(len(self.devices) - 1, self.dev_sel + 1)
            elif key in ("k", curses.KEY_UP):
                self.dev_sel = max(0, self.dev_sel - 1)
            elif key == ENTER:
                self.pick_device()

    def key_normal(self, key) -> None:
        page = self.list_rows()
        actions = {
            "q": self.quit,
            "?": lambda: setattr(self, "overlay", "help"),
            "/": self.start_search,
            " ": self.toggle_play, "p": self.toggle_play,
            "n": lambda: self.skip(True),
            "b": lambda: self.skip(False),
            ",": lambda: self.seek(-SEEK_STEP_MS), "<": lambda: self.seek(-SEEK_STEP_MS),
            ".": lambda: self.seek(SEEK_STEP_MS), ">": lambda: self.seek(SEEK_STEP_MS),
            curses.KEY_SLEFT: lambda: self.seek(-SEEK_STEP_MS),
            curses.KEY_SRIGHT: lambda: self.seek(SEEK_STEP_MS),
            "+": lambda: self.change_volume(VOLUME_STEP), "=": lambda: self.change_volume(VOLUME_STEP),
            "-": lambda: self.change_volume(-VOLUME_STEP), "_": lambda: self.change_volume(-VOLUME_STEP),
            "s": self.toggle_shuffle,
            "R": self.cycle_repeat,
            "f": self.toggle_like,
            "d": self.open_devices,
            "r": self.refresh,
            "[": lambda: self.cycle_list(-1), "]": lambda: self.cycle_list(1),
            "\t": self.toggle_focus, curses.KEY_BTAB: self.toggle_focus,
            "h": self.focus_side, curses.KEY_LEFT: self.focus_side,
            "l": self.focus_list, curses.KEY_RIGHT: self.focus_list,
            "j": lambda: self.move(1), curses.KEY_DOWN: lambda: self.move(1),
            "k": lambda: self.move(-1), curses.KEY_UP: lambda: self.move(-1),
            curses.KEY_NPAGE: lambda: self.move(page), "\x04": lambda: self.move(page // 2),
            curses.KEY_PPAGE: lambda: self.move(-page), "\x15": lambda: self.move(-page // 2),
            "g": lambda: self.jump(False), curses.KEY_HOME: lambda: self.jump(False),
            "G": lambda: self.jump(True), curses.KEY_END: lambda: self.jump(True),
            ENTER: self.activate,
        }
        action = actions.get(key)
        if action:
            action()

    def quit(self) -> None:
        self.running = False

    def toggle_focus(self) -> None:
        self.focus = "side" if self.focus == "list" else "list"

    def focus_side(self) -> None:
        self.focus = "side"

    def focus_list(self) -> None:
        if self.focus == "side":
            it = self.side[self.side_sel]
            if it.kind == "search" or it.key == self.cur.key:
                self.focus = "list"     # already open: just move over
            else:
                self.activate()

    def cycle_list(self, step: int) -> None:
        """Open the previous / next list in the sidebar."""
        keys = [i for i, it in enumerate(self.side) if it.selectable and it.kind != "search"]
        if not keys:
            return
        cur = next((k for k in keys if self.side[k].key == self.cur.key), keys[0])
        self.side_sel = keys[(keys.index(cur) + step) % len(keys)]
        self.open_list(self.side[self.side_sel].key)

    # ── Main loop ────────────────────────────────────────────────────────────
    def run(self) -> None:
        self.theme.init()
        self.set_cursor(False)
        self.scr.keypad(True)
        if hasattr(curses, "set_escdelay"):
            try:
                curses.set_escdelay(25)
            except curses.error:
                pass
        self.resize()
        self.build_side()
        self.poll()
        self.load_playlists()
        self.open_list("liked")
        if self.engine:
            self.bg(self.data, self.engine.start, fail=self.on_error)
        self.bg(self.data, self.api.user_id, lambda u: setattr(self, "user_id", u), lambda e: None)

        while self.running:
            now = time.monotonic()
            self.drain()
            self.timers(now)
            self.render(now)

            self.scr.timeout(self.wait_ms(now))
            try:
                key = self.scr.get_wch()
            except curses.error:
                continue
            # Handle every key already buffered before redrawing (key repeat, paste).
            self.scr.timeout(0)
            while True:
                self.handle_key(key)
                if not self.running:
                    break
                try:
                    key = self.scr.get_wch()
                except curses.error:
                    break

    def timers(self, now: float) -> None:
        if not self.poll_inflight and now >= self.next_poll:
            self.poll()
        if self.vol_deadline and now >= self.vol_deadline:
            self.flush_volume()
        if self.seek_deadline and now >= self.seek_deadline:
            self.flush_seek()
        if self.restart_at and now >= self.restart_at:
            self.restart_at = 0.0
            self.bg(self.data, self.engine.restart, lambda _: self.poll_soon(0), self.on_error)
        if self.expect_uri and now >= self.expect_deadline:
            self.expect_done(now)
        if self.pending_play and now >= self.pending_until:
            self.pending_play = None
            self.flash("SpoTerm's player didn't start. Press r to retry, or d to pick a device",
                       warn=True)
        if self.status and now >= self.status_until:
            self.status = ""
            self.dirty = True
        if self.engine:
            st = self.engine.status
            if st != self.engine_status:
                self.engine_status, self.dirty = st, True

    def wait_ms(self, now: float) -> int:
        if self.inflight or now < self.expect_until or self.pending_play:
            # Replies usually land within a second: check often, then back off on a slow link.
            return 40 if now - self.busy_since < 1.5 else 120
        t = self.next_poll - now
        pb = self.pb
        if pb and pb.is_playing and pb.track:
            t = min(t, (1000 - pb.progress(now) % 1000) / 1000)
        if self.status:
            t = min(t, self.status_until - now)
        if self.vol_deadline:
            t = min(t, self.vol_deadline - now)
        if self.seek_deadline:
            t = min(t, self.seek_deadline - now)
        if self.restart_at:
            t = min(t, self.restart_at - now)
        if self.expect_uri:
            t = min(t, self.expect_deadline - now)
        return int(max(10, min(2000, t * 1000 + 5)))

    def resize(self) -> None:
        if sys.platform == "win32":
            try:
                curses.resize_term(0, 0)
            except curses.error:
                pass
        try:
            curses.update_lines_cols()
        except (AttributeError, curses.error):
            pass
        self.H, self.W = self.scr.getmaxyx()
        self.too_small = self.H < MIN_H or self.W < MIN_W
        self.scr.clear()
        self._size = None   # the screen was wiped: repaint every region
        self.dirty = True

    def set_cursor(self, show: bool) -> None:
        if show != self.cursor_shown:
            try:
                curses.curs_set(1 if show else 0)
            except curses.error:
                pass
            self.cursor_shown = show

    # ── Rendering ────────────────────────────────────────────────────────────
    # Every row remembers the key it was last drawn with (self._drawn: slot -> key) and is
    # only rewritten when that key changes: a poll that changed nothing costs a few tuple
    # compares, moving the selection rewrites two rows, and the screen is only erased when
    # the terminal size or the overlay changes. Render state lives in class defaults so
    # __init__ stays untouched; draw_all gives each App its own dict on the first frame.
    _size: tuple | None = None
    _drawn: dict = {}
    _lay: tuple | None = None
    _cols_key: tuple | None = None
    _cols: Cols | None = None

    def layout(self) -> Layout:
        H, W = self.H, self.W
        c = self._lay
        if c is None or c[0] != (H, W):
            roomy = H >= 20                    # blank rows around the list header and player
            sw = max(16, min(30, W // 4))
            x0, top = sw + 6, 2
            rule_y = H - 5 if roomy else H - 4
            h = max(1, rule_y - 1 - top)
            head_y = top + (2 if roomy else 1)
            rows = max(1, top + h - head_y - 1)
            c = self._lay = ((H, W), Layout(sw, top, h, x0, max(10, W - x0 - 3), head_y, head_y + 1,
                                            rows, rule_y))
        return c[1]

    def _changed(self, slot, key) -> bool:
        if self._drawn.get(slot, _UNSET) == key:
            return False
        self._drawn[slot] = key
        return True

    def render(self, now: float) -> None:
        pb = self.pb
        if self.dirty:
            drew = self.draw_all(now)
        elif pb and pb.track and pb.is_playing and not self.too_small and not self.overlay \
                and pb.progress(now) // 1000 != self.last_sec:
            drew = self.draw_progress(now)
        else:
            return
        self.dirty = False
        if self.typing and self.cursor_at:
            self.scr.move(*self.cursor_at)
            drew = True
        if drew:
            self.scr.refresh()

    def draw_all(self, now: float) -> bool:
        s, T = self.scr, self.theme
        # The devices box resizes when the list arrives, so its row count is geometry too.
        ndev = len(self.devices or ()) if self.overlay == "devices" else 0
        size = (self.H, self.W, self.too_small, self.overlay, ndev)
        fresh = size != self._size
        if fresh:
            self._size, self._drawn = size, {}
            s.erase()
        self.cursor_at = None
        if self.too_small:
            if fresh:
                y = self.H // 2 - 1
                for dy, text, attr in ((0, "Terminal too small", T[WARN]),
                                       (1, f"{self.W}x{self.H}, need {MIN_W}x{MIN_H}", T[FAINT])):
                    put(s, y + dy, max(0, (self.W - len(text)) // 2), text[: self.W - 1], attr)
            self.set_cursor(False)
            return fresh
        lay = self.layout()
        drew = self.draw_header()
        drew |= self.draw_sidebar(lay)
        drew |= self.draw_main(lay)
        drew |= self.draw_player(now, lay)
        if self.overlay:
            devs = tuple(self.devices) if self.devices is not None else None
            if self._changed("overlay", (devs, self.dev_sel)) or drew:
                if self.overlay == "help":
                    self.draw_help()
                elif self.overlay == "devices":
                    self.draw_devices()
                drew = True
        self.set_cursor(self.typing and self.cursor_at is not None)
        return drew

    def draw_header(self) -> bool:
        pb = self.pb
        dev = pb.device_name if pb else ""
        live = bool(pb and pb.track and pb.is_playing)
        eng = getattr(self, "engine_status", "")
        if not dev and eng == "ready":
            dev = self.engine.name       # idle, ready to play here
        if not self._changed("header", (dev, live, eng, self.status, self.status_warn)):
            return False
        s, T, g, W = self.scr, self.theme, self.g, self.W
        put(s, 0, 0, " " * W)
        put(s, 0, 2, "spoterm", T[ACCENT] | curses.A_BOLD)
        x = W - 2
        if dev:
            name = fit(dev, 28).rstrip()
            x -= width(name)
            put(s, 0, x, name, T[DIM])
            x -= 2
            put(s, 0, x, g["dot"], T[ACCENT] if live else T[FAINT])
        else:
            x -= 9
            put(s, 0, x, "no device", T[FAINT])
        label = {"starting": "starting player" + g["ell"], "retrying": "player offline, retrying" + g["ell"],
                 "login needed": "player: sign-in needed", "not built": "player not built",
                 }.get(eng, "player stopped" if eng.startswith("stopped") else "")
        if label and not self.status:    # a status message outranks the player label
            x -= width(label) + 3
            if x > 12:
                put(s, 0, x, label, T[FAINT] if eng in ("starting", "retrying") else T[WARN])
            else:
                x += width(label) + 3
        avail = x - 12 - 3
        if avail > 4:
            msg = self.status
            attr = (T[WARN] if self.status_warn else T[TEXT]) if msg else T[FAINT]
            put(s, 0, 12, fit(msg or "? help", avail).rstrip(), attr)
        return True

    def draw_sidebar(self, lay: Layout) -> bool:
        s, T, g = self.scr, self.theme, self.g
        top, sw, side = lay.top, lay.side_w, self.side
        self.side_top = _scroll(self.side_sel, self.side_top, lay.h, len(side))
        ctx = self.pb.context_uri if self.pb else None
        liked_ctx = bool(ctx) and ctx.endswith(":collection")
        focused = self.focus == "side" and not self.typing
        cur, drew = self.cur.key, False
        for r in range(lay.h):
            i = self.side_top + r
            it = side[i] if i < len(side) else None
            if it is not None and it.selectable:
                is_ctx = liked_ctx if it.kind == "liked" else bool(it.playlist and it.playlist.uri == ctx)
                state = (focused and i == self.side_sel, it.key == cur, is_ctx)
            else:
                state = focused
            if not self._changed(("side", r), (it, state)):
                continue
            drew, y = True, top + r
            if it is None or not it.selectable:
                if it is not None and it.kind == "section":   # headings brighten with focus
                    text, attr = it.label.upper(), (T[DIM] if focused else T[FAINT]) | curses.A_BOLD
                else:
                    text, attr = (it.label if it else ""), T[FAINT]
                put(s, y, 1, " " + fit(text, sw + 1), attr)
                continue
            sel, active, is_ctx = state
            if sel:
                attr = T[SEL_ACCENT] if active else T[SEL]
            else:
                attr = T[ACCENT] | curses.A_BOLD if active else T[TEXT]
            put(s, y, 1, g["mark"] if sel else " ", T[MARK] if sel else 0)
            put(s, y, 2, fit(it.label, sw - 1) + "  ", attr)
            if is_ctx:
                put(s, y, sw + 1, g["play"], T[SEL_ACCENT] if sel else T[ACCENT])
        return drew

    def columns(self, tl: TrackList, w: int) -> Cols:
        """Column widths and header text, recomputed only when the list or width changes."""
        key = (tl.key, tl.gen, len(tl.tracks), tl.total, w)
        if key != self._cols_key:
            num_w = max(2, len(str(max(tl.total, len(tl.tracks)))))
            dur_w = 7 if any(t.duration_ms >= 3_600_000 for t in tl.tracks) else 5
            avail = w - num_w - dur_w - 6
            if avail >= 64:
                title_w, artist_w = avail * 42 // 100, avail * 28 // 100
                album_w = avail - title_w - artist_w - 2
            else:
                title_w = avail * 58 // 100
                artist_w, album_w = avail - title_w, 0
            head = " " + fit("#", num_w, right=True) + "  " + fit("Title", title_w) + "  " + fit("Artist", artist_w)
            if album_w:
                head += "  " + fit("Album", album_w)
            head += "  " + fit("Time", dur_w, right=True) + "  "
            self._cols_key, self._cols = key, Cols(num_w, title_w, artist_w, album_w, dur_w, head)
        return self._cols

    def draw_main(self, lay: Layout) -> bool:
        tl = self.cur
        drew = self.draw_list_title(lay, tl)
        if not tl.tracks:
            return self.draw_empty(lay, tl) or drew
        s, T, x0, w = self.scr, self.theme, lay.x0, lay.w
        cols = self.columns(tl, w)
        for y in range(lay.top + 1, lay.head_y):          # spacer rows
            if self._changed(("main", y), None):
                put(s, y, x0 - 1, " " * (w + 3))
                drew = True
        if self._changed(("main", lay.head_y), cols):
            put(s, lay.head_y, x0 - 1, cols.head, T[FAINT])
            drew = True

        rows, n = lay.rows, len(tl.tracks)
        tl.top = top = _scroll(tl.sel, tl.top, rows, n)
        span = n if tl.next_offset is None else max(n, tl.total)   # scrollbar covers the whole list
        th = t0 = 0
        if span > rows:
            th = max(1, rows * rows // span)
            t0 = min(rows - th, round((rows - th) * top / (span - rows)))
        pb = self.pb
        now_uri = pb.track.uri if pb and pb.track else None
        now_state = 1 if pb and pb.is_playing else 2
        focused = self.focus == "list" and not self.typing
        for r in range(rows):
            i, y, thumb = top + r, lay.list_y + r, t0 <= r < t0 + th
            if i >= n:
                more = tl.loading and i == n
                if self._changed(("main", y), (more, thumb)):
                    drew = True
                    text = fit(" " * (cols.num_w + 2) + ("Loading" + self.g["ell"] if more else ""), w + 1)
                    put(s, y, x0 - 1, " " + text + (self.g["thumb"] if thumb else " "), T[FAINT])
                continue
            t = tl.tracks[i]
            sel = (2 if focused else 1) if i == tl.sel else 0
            cur = now_state if t.uri == now_uri else 0
            if self._changed(("main", y), (t, i, sel, cur, cols, thumb)):
                self.draw_track(y, lay, cols, t, i, sel, cur, thumb)
                drew = True
        return drew

    def draw_track(self, y: int, lay: Layout, c: Cols, t: Track, i: int, sel: int, cur: int,
                   thumb: bool) -> None:
        """One list row. sel: 0 no, 1 cursor in an unfocused pane, 2 focused; cur: 0, 1 playing, 2 paused."""
        s, T, g = self.scr, self.theme, self.g
        x, ell = lay.x0, g["ell"]
        if sel == 2:
            a_mark, a_dim = T[MARK], T[SEL_DIM]
            a_num, a_title = (T[SEL_ACCENT], T[SEL_ACCENT]) if cur else (T[SEL_DIM], T[SEL])
        elif not t.playable:
            a_mark = a_num = a_title = a_dim = T[FAINT]
        else:
            a_mark, a_dim = T[FAINT], T[DIM]
            a_num = (T[ACCENT] if cur == 1 else T[MUTED]) if cur else T[FAINT]
            a_title = T[ACCENT] if cur else T[TEXT]
        if sel == 1:
            a_title |= curses.A_BOLD
        num = (g["play"] if cur == 1 else g["pause"]) if cur else str(i + 1)
        put(s, y, x - 1, g["mark"] if sel else " ", a_mark)
        put(s, y, x, fit(num, c.num_w, ell, True), a_num)
        x += c.num_w
        put(s, y, x, "  " + fit(t.name, c.title_w, ell), a_title)
        rest = "  " + fit(t.artists, c.artist_w, ell)
        if c.album_w:
            rest += "  " + fit(t.album, c.album_w, ell)
        rest += "  " + fit(fmt_time(t.duration_ms), c.dur_w, ell, True) + " "
        put(s, y, x + 2 + c.title_w, rest, a_dim)
        put(s, y, lay.x0 + lay.w + 1, g["thumb"] if thumb else " ", T[FAINT])

    def draw_list_title(self, lay: Layout, tl: TrackList) -> bool:
        s, T, g = self.scr, self.theme, self.g
        x0, w, y = lay.x0, lay.w, lay.top
        info = ""
        if tl.total:
            info = f"{tl.total:,} " + ("result" if tl.kind == "search" else "song") + ("" if tl.total == 1 else "s")
        if tl.kind == "search":
            fx, fw = x0 + 8, max(4, w - 8 - len(info) - 2)
            q = self.query if self.typing else tl.query
            room = max(1, fw - 3)
            start = min(getattr(self, "_qstart", 0), self.qcur) if self.typing else 0
            while self.typing and start < self.qcur and width(q[start:self.qcur]) > room:
                start += 1
            self._qstart = start
            shown = fit(q[start:], room).rstrip() if width(q[start:]) > room else q[start:]
            if self.typing:   # needed every frame, even when the row itself is unchanged
                self.cursor_at = (y, fx + 2 + width(q[start:self.qcur]))
            key = ("search", shown, self.typing, info)
        else:
            focused = self.focus == "list"
            key = ("title", tl.title, info, focused)
        if not self._changed(("main", y), key):
            return False
        put(s, y, x0 - 1, " " * (w + 3))
        if tl.kind == "search":
            put(s, y, x0, "Search", T[TITLE])
            put(s, y, fx, g["cursor"], T[ACCENT] if self.typing else T[FAINT])
            if shown or self.typing:
                put(s, y, fx + 2, shown, T[TEXT])
            else:
                put(s, y, fx + 2, "press / to search", T[FAINT])
        else:
            attr = T[TITLE] if focused else T[TEXT] | curses.A_BOLD
            put(s, y, x0, fit(tl.title, max(1, w - len(info) - 2)).rstrip(), attr)
        if info:
            put(s, y, x0 + w - len(info), info, T[DIM])
        return True

    def draw_empty(self, lay: Layout, tl: TrackList) -> bool:
        ell = self.g["ell"]
        if tl.locked:
            msg, hint, role = "Spotify doesn't let apps list this playlist", "press enter to play it", DIM
        elif tl.error:
            msg, hint, role = tl.error, "press R to retry", WARN
        elif tl.loading:
            msg, hint, role = "Loading" + ell, "", DIM
        elif tl.kind == "search" and not tl.query:
            msg, hint, role = "Search Spotify", "type, then press enter" if self.typing else "press / to start", DIM
        elif tl.kind == "search":
            msg, hint, role = f'No results for "{tl.query}"', "try different words", DIM
        else:
            msg, hint, role = "Nothing here", "", DIM
        s, T, x0, w = self.scr, self.theme, lay.x0, lay.w
        my, drew = lay.top + 1 + (lay.h - 1) // 3, False
        for y in range(lay.top + 1, lay.top + lay.h):
            text, attr = (msg, T[role]) if y == my else (hint, T[FAINT]) if y == my + 1 else ("", 0)
            if self._changed(("main", y), ("empty", text, attr)):
                text = fit(text, w, ell).strip()
                put(s, y, x0 - 1, " " + fit(" " * ((w - width(text)) // 2) + text, w + 2), attr)
                drew = True
        return drew

    def draw_player(self, now: float, lay: Layout) -> bool:
        s, T, g, W = self.scr, self.theme, self.g, self.W
        y, drew = lay.rule_y, False
        if self._changed("rule", None):
            put(s, y, 2, g["rule"] * (W - 4), T[FAINT])
            drew = True
        pb = self.pb
        t = pb.track if pb else None
        key = (t, pb.is_playing, self.liked if t.is_track else None, pb.shuffle, pb.repeat,
               pb.volume) if t else None
        if self._changed("player", key):
            drew, blank = True, " " * (W - 1)
            put(s, y + 1, 0, blank)
            put(s, y + 2, 0, blank)
            if t is None:
                put(s, y + 1, 6, "Nothing playing", T[DIM])
                hint = "Pick a song and press enter to play here, or press d to choose a device"
                put(s, y + 2, 6, fit(hint, W - 9).rstrip(), T[FAINT])
                put(s, y + 3, 0, blank)
                self._drawn.pop("bar", None)
            else:
                self.draw_now_playing(y + 1, pb, t)
        if t is not None:
            drew |= self.draw_progress(now)
        return drew

    def draw_now_playing(self, y: int, pb: Playback, t: Track) -> None:
        s, T, g, W = self.scr, self.theme, self.g, self.W
        live, right = pb.is_playing, W - 3
        put(s, y, 3, g["play"] if live else g["pause"], T[ACCENT] | curses.A_BOLD if live else T[MUTED])

        # title row: name and like state on the left, shuffle and repeat on the right
        rep = "repeat 1" if pb.repeat == "track" else "repeat"
        x = right - len(rep)
        put(s, y, x, rep, T[ACCENT] if pb.repeat != "off" else T[FAINT])
        x -= 3 + 7
        put(s, y, x, "shuffle", T[ACCENT] if pb.shuffle else T[FAINT])
        heart = g["heart"] if t.is_track and self.liked is not None else ""
        name = fit(t.name, max(1, x - 9 - (width(heart) + 2 if heart else 0))).rstrip()
        put(s, y, 6, name, T[TITLE])
        if heart:
            put(s, y, 6 + width(name) + 2, heart, T[ACCENT] if self.liked else T[FAINT])

        # artist row, with the volume meter on the right
        x = right
        if pb.volume is not None:
            vol, ramp = pb.volume, g["ramp"]
            x -= 3
            put(s, y + 1, x, f"{vol:>3}", T[DIM])
            if ramp:
                k = (vol * len(ramp) + 50) // 100
                x -= len(ramp) + 1
                put(s, y + 1, x, ramp[:k], T[TEXT])
                put(s, y + 1, x + k, ramp[k:], T[FAINT])
            else:
                x -= 4
                put(s, y + 1, x, "vol", T[FAINT])
        sub = t.artists + (g["sep"] + t.album if t.album else "")
        put(s, y + 1, 6, fit(sub, max(1, x - 9)).rstrip(), T[DIM])

    def draw_progress(self, now: float) -> bool:
        """The once-a-second path: usually rewrites just the elapsed time and one bar cell."""
        s, T, g, W = self.scr, self.theme, self.g, self.W
        pb = self.pb
        t = pb.track
        y = self.layout().rule_y + 3
        cur, dur = pb.progress(now), t.duration_ms
        self.last_sec = cur // 1000
        right = fmt_time(dur)
        left = fmt_time(cur).rjust(len(right))
        bx = 6 + len(left) + 2
        bw = max(4, W - 3 - len(right) - 2 - bx)
        filled = min(bw, bw * cur // dur) if dur else 0
        on = T[ACCENT] if pb.is_playing else T[MUTED]
        geo = (t, pb.is_playing, bw)
        old = self._drawn.get("bar")
        if old is not None and old[0] == geo:
            if old[1] == left and old[2] == filled:
                return False
            if old[1] != left:
                put(s, y, 6, left, T[DIM])
            if filled > old[2]:
                put(s, y, bx + old[2], g["bar_on"] * (filled - old[2]), on)
            elif filled < old[2]:
                put(s, y, bx + filled, g["bar_off"] * (old[2] - filled), T[FAINT])
        else:
            put(s, y, 0, " " * (W - 1))
            put(s, y, 6, left, T[DIM])
            put(s, y, bx, g["bar_on"] * filled, on)
            put(s, y, bx + filled, g["bar_off"] * (bw - filled), T[FAINT])
            put(s, y, bx + bw + 2, right, T[DIM])
        self._drawn["bar"] = (geo, left, filled)
        return True

    def box(self, h: int, w: int, title: str, hint: str = "") -> tuple:
        """A centred panel; returns the content area (y, x, h, w) inside a 2-column padding."""
        s, T, g = self.scr, self.theme, self.g
        h, w = min(h, self.H - 2), min(w, self.W - 4)
        y, x = (self.H - h) // 2, (self.W - w) // 2
        put(s, y, x, g["tl"] + g["h"] * (w - 2) + g["tr"], T[FAINT])
        put(s, y, x + 2, f" {title} ", T[TITLE])
        side = g["v"] + " " * (w - 2) + g["v"]
        for r in range(1, h - 1):
            put(s, y + r, x, side, T[FAINT])
        put(s, y + h - 1, x, g["bl"] + g["h"] * (w - 2) + g["br"], T[FAINT])
        if hint and len(hint) + 8 <= w:
            put(s, y + h - 1, x + w - len(hint) - 4, f" {hint} ", T[DIM])
        return y + 1, x + 3, h - 2, w - 6

    def draw_help(self) -> None:
        s, T = self.scr, self.theme
        kw = 8
        dw = max(len(d) for _, items in HELP for _, d in items)
        colw, n = kw + dw, max(len(items) for _, items in HELP)
        two = self.W - 4 - 6 >= 2 * colw + 2
        want = (n + 5, 2 * colw + 10) if two else (sum(len(i) + 2 for _, i in HELP) + 3, colw + 6)
        y, x, h, w = self.box(*want, "Keys", "esc close")
        r = 1
        for c, (name, items) in enumerate(HELP):
            cx = x + c * (w - colw) if two else x
            if two:
                r = 1
            for k, d in ((name.upper(), None), *items):
                if r >= h:
                    break
                if d is None:
                    put(s, y + r, cx, fit(k, colw), T[FAINT] | curses.A_BOLD)
                else:
                    put(s, y + r, cx, fit(k, kw), T[ACCENT])
                    put(s, y + r, cx + kw, fit(d, dw), T[TEXT])
                r += 1
            r += 1

    def draw_devices(self) -> None:
        s, T, g = self.scr, self.theme, self.g
        devs = self.devices
        y, x, h, w = self.box((len(devs) if devs else 2) + 4, 56, "Devices",
                              "enter select  esc close" if devs else "esc close")
        if devs is None:
            put(s, y + 1, x, "Looking for devices" + g["ell"], T[DIM])
            return
        if not devs:
            put(s, y + 1, x, fit("No devices found", w), T[DIM])
            put(s, y + 2, x, fit("Open Spotify on a phone or computer", w), T[FAINT])
            return
        for r, d in enumerate(devs[: h - 2]):
            sel = r == self.dev_sel
            name = fit(f"{g['dot'] if d.is_active else ' '} {d.name}", w - 12)
            put(s, y + 1 + r, x - 1, " " + name, T[SEL] if sel else T[ACCENT] if d.is_active else T[TEXT])
            put(s, y + 1 + r, x + w - 12, fit(d.type.lower(), 12, right=True) + " ", T[SEL_DIM] if sel else T[DIM])


def _signed_in(settings: config.Settings) -> bool:
    """SpoTerm's own sign-in exists and covers every scope we ask for."""
    try:
        with open(settings.token_path, encoding="utf-8") as f:
            tok = json.load(f)
        granted = set(str(tok.get("scope") or "").split())
        return bool(tok.get("access_token")) and set(config.SCOPE.split()) <= granted
    except (OSError, ValueError, AttributeError):
        return False


def _run_login() -> bool:
    """SpoTerm's browser sign-in, in its own process so TLS never loads into this one."""
    try:
        return subprocess.call([sys.executable, "-m", "spoterm.login"], cwd=config.PROJECT_DIR) == 0
    except KeyboardInterrupt:
        return False


def main() -> None:
    try:
        settings = config.load()
    except config.ConfigError as e:
        sys.exit(str(e))

    engine = Engine(settings)
    if not engine.available():
        sys.exit("SpoTerm's engine isn't built yet. From the project folder run:\n"
                 "  cd engine\n  cargo build --release\n(see the README for details)")
    if not _signed_in(settings) and not _run_login():
        sys.exit(1)
    if engine.needs_login():
        engine.login()

    try:
        engine.start()
        state = engine.check_web()
        if state == "login":            # the saved sign-in was revoked or expired for good
            engine.stop()
            if not _run_login():
                sys.exit(1)
            engine.restart()
            state = engine.check_web()
        if state != "ok":
            print(f"Warning: {state}. Starting anyway.")
    except KeyboardInterrupt:
        engine.stop()
        sys.exit(1)

    os.environ.setdefault("ESCDELAY", "25")
    client = api.Spotify(engine)
    try:
        curses.wrapper(lambda scr: App(scr, client, settings, engine).run())
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        path = os.path.join(config.config_dir(), "crash.log")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(time.strftime("\n--- %Y-%m-%d %H:%M:%S ---\n") + traceback.format_exc())
        except OSError:
            pass
        sys.exit(f"SpoTerm hit a bug and closed. Details were saved to {path}")
    finally:
        engine.stop()
