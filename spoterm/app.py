"""The terminal UI: state, scheduling, input and rendering.

Design notes (why it stays near 0% CPU):
  * The UI thread sleeps inside getch; it only wakes for input, finished
    background jobs, the next once-a-second progress tick, or a timer.
  * Playback progress is interpolated locally, so Spotify is polled every few
    seconds (or at track end) rather than continuously.
  * Only the progress line is redrawn each second; everything else is redrawn
    only when something actually changed.
  * All network I/O happens on two worker threads; all state lives on the UI
    thread, so there are no locks and no races.
"""

import logging
import os
import queue
import sys
import time
from dataclasses import dataclass, field

try:
    import curses
except ImportError:
    sys.exit("curses is missing. On Windows run:  pip install windows-curses")

from . import api, config
from .api import Playback, Track, describe_error
from .worker import Worker
from .ui import (ACCENT, DIM, FAINT, SEL, SEL_ACCENT, SEL_DIM, TEXT, TITLE, WARN,
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


@dataclass
class TrackList:
    key: str
    kind: str                      # "liked" | "playlist" | "search"
    title: str
    context_uri: str | None = None
    playlist_id: str | None = None
    query: str = ""
    tracks: list = field(default_factory=list)
    total: int = 0
    next_offset: int | None = 0    # 0 = nothing loaded yet, None = fully loaded
    loading: bool = False
    error: str = ""
    stale: bool = False
    locked: bool = False           # Spotify won't list this playlist's tracks for us
    gen: int = 0                   # bumped on reset so late results are dropped
    sel: int = 0
    top: int = 0

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


@dataclass(frozen=True)
class SideItem:
    kind: str                      # "section" | "gap" | "note" | "liked" | "search" | "playlist"
    label: str
    key: str = ""
    playlist: api.Playlist | None = None

    @property
    def selectable(self) -> bool:
        return self.kind in ("liked", "search", "playlist")


HELP = (
    ("space / p", "play / pause"),
    ("n / b", "next / previous track"),
    (", / .", "seek back / forward 10s"),
    ("- / +", "volume down / up"),
    ("s / r", "shuffle / repeat"),
    ("f", "like / unlike current track"),
    ("d", "choose playback device"),
    ("/", "search"),
    ("enter", "open list / play track"),
    ("tab  h  l", "move between panes"),
    ("j k  g G", "down, up, top, bottom"),
    ("R", "refresh"),
    ("q", "quit"),
)


def _scroll(sel: int, top: int, h: int, n: int) -> int:
    if h <= 0:
        return 0
    if sel < top:
        top = sel
    elif sel >= top + h:
        top = sel - h + 1
    return max(0, min(top, max(0, n - h)))


class App:
    def __init__(self, scr, spotify: api.Spotify, settings: config.Settings):
        self.scr = scr
        self.api = spotify
        self.g = ASCII_GLYPHS if settings.ascii else UNICODE_GLYPHS
        self.theme = Theme()

        self.results: queue.SimpleQueue = queue.SimpleQueue()
        self.ctl = Worker("spoterm-control", self.results)   # playback state + commands
        self.data = Worker("spoterm-data", self.results)     # library lists + search
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
        self.inflight += 1
        worker.submit(fn, done, fail if fail is not None else self.on_error)

    def drain(self) -> None:
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
        self.flash(describe_error(err), warn=True)

    def flash(self, msg: str, warn: bool = False) -> None:
        self.status, self.status_warn = msg, warn
        self.status_until = time.monotonic() + (5.0 if warn else 2.5)
        self.dirty = True

    # ── Playback polling ─────────────────────────────────────────────────────
    def poll(self) -> None:
        self.poll_inflight = True
        self.next_poll = float("inf")
        self.bg(self.ctl, self.api.playback, self.on_playback, self.on_poll_error)

    def poll_soon(self, bursts: int = 2) -> None:
        """Spotify applies commands with a short lag, so re-check a couple of times."""
        self.burst = max(self.burst, bursts)
        if not self.poll_inflight:
            self.next_poll = min(self.next_poll, time.monotonic() + 0.35)

    def on_playback(self, pb: Playback | None) -> None:
        self.poll_inflight = False
        now = time.monotonic()
        if pb and self.vol_target is not None:
            pb.volume = self.vol_target
        self.pb = pb

        track = pb.track if pb else None
        if track and track.uri != self.liked_uri:
            self.liked_uri, self.liked = track.uri, None
            if track.is_track:
                uri, tid = track.uri, track.id
                self.bg(self.ctl, lambda: self.api.is_liked(tid),
                        lambda v: self._set_liked(uri, v), lambda e: None)

        if self.burst > 0:
            self.burst -= 1
            delay = 0.8
        elif pb and pb.is_playing and track:
            remaining = (track.duration_ms - pb.progress(now)) / 1000
            delay = min(POLL_PLAYING, max(0.5, remaining + 0.4))
        else:
            delay = POLL_IDLE
        self.next_poll = now + delay

    def on_poll_error(self, err: BaseException) -> None:
        self.poll_inflight = False
        self.flash(describe_error(err), warn=True)
        self.next_poll = time.monotonic() + POLL_ERROR

    def _set_liked(self, uri: str, value: bool) -> None:
        if uri == self.liked_uri:
            self.liked = value

    # ── Playback commands ────────────────────────────────────────────────────
    def command(self, fn, ok_msg: str | None = None) -> None:
        def done(_):
            if ok_msg:
                self.flash(ok_msg)
            self.poll_soon()

        def fail(err):
            self.flash(describe_error(err), warn=True)
            self.poll_soon(1)   # undo optimistic changes with real state

        self.bg(self.ctl, fn, done, fail)

    def toggle_play(self) -> None:
        pb, now = self.pb, time.monotonic()
        if pb:
            pb.rebase(now)
        if pb and pb.is_playing:
            pb.is_playing = False
            self.command(self.api.pause)
        else:
            if pb and pb.track:
                pb.is_playing = True
            self.command(self.api.resume)
        self.dirty = True

    def seek(self, delta_ms: int) -> None:
        pb = self.pb
        if not pb or not pb.track:
            return
        now = time.monotonic()
        pos = max(0, min(pb.progress(now) + delta_ms, pb.track.duration_ms - 1000))
        pb.progress_ms, pb.fetched_at = pos, now
        self.command(lambda: self.api.seek(pos))
        self.dirty = True

    def change_volume(self, delta: int) -> None:
        pb = self.pb
        base = self.vol_target if self.vol_target is not None else (pb.volume if pb else None)
        if base is None:
            self.flash("Volume can't be changed on this device" if pb else "Nothing is playing", warn=True)
            return
        self.vol_target = max(0, min(100, base + delta))
        pb.volume = self.vol_target
        self.vol_deadline = time.monotonic() + 0.25   # debounce key repeats into one request
        self.dirty = True

    def flush_volume(self) -> None:
        v, self.vol_deadline = self.vol_target, 0.0

        def done(_):
            if self.vol_target == v:
                self.vol_target = None

        def fail(err):
            self.vol_target = None
            self.flash(describe_error(err), warn=True)
            self.poll_soon(1)

        self.bg(self.ctl, lambda: self.api.volume(v), done, fail)

    def toggle_shuffle(self) -> None:
        if not self.pb:
            return
        self.pb.shuffle = on = not self.pb.shuffle
        self.command(lambda: self.api.shuffle(on))
        self.dirty = True

    def cycle_repeat(self) -> None:
        if not self.pb:
            return
        nxt = {"off": "context", "context": "track"}.get(self.pb.repeat, "off")
        self.pb.repeat = nxt
        self.command(lambda: self.api.repeat(nxt))
        self.dirty = True

    def toggle_like(self) -> None:
        pb = self.pb
        if not pb or not pb.track or not pb.track.is_track or self.liked is None:
            return
        uri, tid, new = pb.track.uri, pb.track.id, not self.liked
        self.liked = new

        def done(_):
            self.flash("Added to Liked Songs" if new else "Removed from Liked Songs")
            if "liked" in self.lists:
                self.lists["liked"].stale = True

        def fail(err):
            self._set_liked(uri, not new)
            self.flash(describe_error(err), warn=True)

        self.bg(self.ctl, lambda: self.api.set_liked(tid, new), done, fail)
        self.dirty = True

    def play_selected(self) -> None:
        tl = self.cur
        if not tl.tracks:
            if tl.kind == "playlist" and not tl.loading:
                ctx = tl.context_uri
                self.command(lambda: self.api.play(context_uri=ctx))
            return
        t: Track = tl.tracks[tl.sel]
        if not t.playable:
            self.flash("Local files can't be played through the Spotify API", warn=True)
            return
        if tl.kind == "playlist":
            ctx, off = tl.context_uri, {"position": t.pos}
            fn = lambda: self.api.play(context_uri=ctx, offset=off)
        else:
            uris = [x.uri for x in tl.tracks[tl.sel:tl.sel + 100] if x.playable]
            if tl.kind == "liked":
                fn = lambda: self.api.play_liked(t, uris)
            else:
                fn = lambda: self.api.play(uris=uris)

        now = time.monotonic()
        if self.pb:   # optimistic: show the new track immediately
            self.pb.track, self.pb.progress_ms, self.pb.fetched_at, self.pb.is_playing = t, 0, now, True
        else:
            self.pb = Playback(t, True, 0, now, None, "", None, False, "off", None)
        self.command(fn)
        self.dirty = True

    def open_devices(self) -> None:
        self.overlay, self.devices, self.dev_sel = "devices", None, 0

        def done(devs):
            if self.overlay == "devices":
                self.devices = devs
                self.dev_sel = next((i for i, d in enumerate(devs) if d.is_active), 0)

        def fail(err):
            self.devices = []
            self.on_error(err)

        self.bg(self.ctl, self.api.devices, done, fail)

    def pick_device(self) -> None:
        if not self.devices:
            return
        d = self.devices[self.dev_sel]
        self.overlay = None
        play = not (self.pb and not self.pb.is_playing)
        self.command(lambda: self.api.transfer(d.id, play), f"Playing on {d.name}")

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
        if tl.loading or tl.next_offset is None:
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
            tl.loading, tl.error = False, ""
            tl.tracks.extend(page.tracks)
            tl.total, tl.next_offset = page.total, page.next_offset
            self.prefetch(tl)

        def fail(err):
            if tl.gen != gen:
                return
            tl.loading = False
            if tl.kind == "playlist" and getattr(err, "http_status", None) == 403:
                tl.locked = True
                tl.next_offset = None
            else:
                tl.error = describe_error(err)

        self.bg(self.data, fn, done, fail)

    def prefetch(self, tl: TrackList) -> None:
        if tl.next_offset is not None and tl.sel >= len(tl.tracks) - PREFETCH_ROWS:
            self.load_more(tl)
        elif tl.next_offset is not None and len(tl.tracks) < self.list_rows():
            self.load_more(tl)

    def refresh(self) -> None:
        self.load_playlists()
        tl = self.cur
        if tl.kind != "search" or tl.query:
            tl.reset()
            self.load_more(tl)
        for other in self.lists.values():
            if other is not tl:
                other.stale = True
        self.poll_soon(0)

    def start_search(self) -> None:
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
        return max(1, self.H - 10)

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
            "n": lambda: self.command(self.api.next),
            "b": lambda: self.command(self.api.previous),
            ",": lambda: self.seek(-SEEK_STEP_MS), "<": lambda: self.seek(-SEEK_STEP_MS),
            ".": lambda: self.seek(SEEK_STEP_MS), ">": lambda: self.seek(SEEK_STEP_MS),
            curses.KEY_SLEFT: lambda: self.seek(-SEEK_STEP_MS),
            curses.KEY_SRIGHT: lambda: self.seek(SEEK_STEP_MS),
            "+": lambda: self.change_volume(VOLUME_STEP), "=": lambda: self.change_volume(VOLUME_STEP),
            "-": lambda: self.change_volume(-VOLUME_STEP), "_": lambda: self.change_volume(-VOLUME_STEP),
            "s": self.toggle_shuffle,
            "r": self.cycle_repeat,
            "f": self.toggle_like,
            "d": self.open_devices,
            "R": self.refresh,
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
            self.activate()

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
        if self.status and now >= self.status_until:
            self.status = ""
            self.dirty = True

    def wait_ms(self, now: float) -> int:
        if self.inflight:
            return 40
        t = self.next_poll - now
        pb = self.pb
        if pb and pb.is_playing and pb.track:
            t = min(t, (1000 - pb.progress(now) % 1000) / 1000)
        if self.status:
            t = min(t, self.status_until - now)
        if self.vol_deadline:
            t = min(t, self.vol_deadline - now)
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
        self.dirty = True

    def set_cursor(self, show: bool) -> None:
        if show != self.cursor_shown:
            try:
                curses.curs_set(1 if show else 0)
            except curses.error:
                pass
            self.cursor_shown = show

    # ── Rendering ────────────────────────────────────────────────────────────
    def render(self, now: float) -> None:
        pb = self.pb
        playing = bool(pb and pb.track and pb.is_playing)
        if self.dirty:
            self.draw_all(now)
        elif playing and not self.too_small and not self.overlay \
                and pb.progress(now) // 1000 != self.last_sec:
            self.draw_progress(now)
        else:
            return
        if self.typing and self.cursor_at:
            self.scr.move(*self.cursor_at)
        self.scr.refresh()
        self.dirty = False

    def draw_all(self, now: float) -> None:
        s, T = self.scr, self.theme
        s.erase()
        self.cursor_at = None
        if self.too_small:
            msg = f"Terminal too small ({self.W}x{self.H}, need {MIN_W}x{MIN_H})"
            put(s, self.H // 2, max(0, (self.W - len(msg)) // 2), msg[: self.W - 1], T[WARN])
            self.set_cursor(False)
            return
        self.draw_header()
        self.draw_sidebar()
        self.draw_main()
        self.draw_player(now)
        if self.overlay == "help":
            self.draw_help()
        elif self.overlay == "devices":
            self.draw_devices()
        self.set_cursor(self.typing and self.cursor_at is not None)

    def draw_header(self) -> None:
        s, T, W, g = self.scr, self.theme, self.W, self.g
        put(s, 0, 2, "spoterm", T[ACCENT] | curses.A_BOLD)
        pb = self.pb
        if pb and pb.device_name:
            dev = fit(pb.device_name, 28).rstrip()
            x = W - 2 - width(dev)
            put(s, 0, x - 2, g["dot"], T[ACCENT])
            put(s, 0, x, dev, T[DIM])
        else:
            x = W - 2 - 9
            put(s, 0, x, "no device", T[FAINT])
        avail = x - 13 - 3
        msg = self.status or "? help"
        attr = T[WARN] if self.status_warn and self.status else T[DIM] if self.status else T[FAINT]
        if avail > 4:
            put(s, 0, 13, fit(msg, avail).rstrip(), attr)

    def body(self) -> tuple:
        side_w = max(18, min(32, self.W // 4))
        return 2, self.H - 7, side_w   # top row, height, sidebar width

    def draw_sidebar(self) -> None:
        s, T, g = self.scr, self.theme, self.g
        top, h, sw = self.body()
        self.side_top = _scroll(self.side_sel, self.side_top, h, len(self.side))
        playing_ctx = self.pb.context_uri if self.pb else None
        focused = self.focus == "side" and not self.typing
        for r in range(h):
            i = self.side_top + r
            if i >= len(self.side):
                break
            it, y = self.side[i], top + r
            if it.kind == "section":
                put(s, y, 2, fit(it.label.upper(), sw - 1), T[FAINT] | curses.A_BOLD)
                continue
            if it.kind == "note":
                put(s, y, 2, fit(it.label, sw - 1), T[FAINT])
                continue
            if not it.selectable:
                continue
            active = it.key == self.cur.key
            is_ctx = bool(it.playlist and it.playlist.uri == playing_ctx) or \
                (it.kind == "liked" and bool(playing_ctx) and playing_ctx.endswith(":collection"))
            label = fit(" " + it.label, sw - 2) + (" " + g["play"] if is_ctx else "  ")
            if i == self.side_sel and focused:
                attr = T[SEL_ACCENT] if active else T[SEL]
            elif active:
                attr = T[ACCENT] | curses.A_BOLD
            else:
                attr = T[TEXT]
            put(s, y, 1, fit(label, sw + 1), attr)

    def draw_main(self) -> None:
        s, T, g = self.scr, self.theme, self.g
        top, h, sw = self.body()
        x0 = sw + 4
        w = self.W - x0 - 2
        tl = self.cur

        # title row
        if tl.kind == "search":
            put(s, top, x0, "Search", T[TITLE])
            fx, fw = x0 + 8, max(4, w - 8 - 14)
            put(s, top, fx, g["cursor"], T[ACCENT] if self.typing else T[FAINT])
            q = self.query if self.typing else tl.query
            if q or self.typing:
                shown = q
                while width(shown) > fw - 3 and shown:   # keep the tail visible while typing
                    shown = shown[1:]
                put(s, top, fx + 2, shown, T[TEXT])
                if self.typing:
                    off = width(q[: self.qcur]) - (width(q) - width(shown))
                    self.cursor_at = (top, fx + 2 + max(0, off))
            else:
                put(s, top, fx + 2, "press / to search", T[FAINT])
        else:
            put(s, top, x0, fit(tl.title, max(1, w - 16)).rstrip(), T[TITLE])

        if tl.total:
            info = f"{tl.total:,} " + ("result" if tl.kind == "search" else "song") + ("" if tl.total == 1 else "s")
            put(s, top, x0 + w - len(info), info, T[DIM])

        rows = h - 3
        ly = top + 3
        if not tl.tracks:
            if tl.locked:
                msg, attr = "Spotify doesn't let apps list this playlist. Press enter to play it", T[DIM]
            elif tl.error:
                msg, attr = f"{tl.error}  (R to retry)", T[WARN]
            elif tl.loading:
                msg, attr = "Loading…", T[DIM]
            elif tl.kind == "search" and not tl.query:
                msg, attr = "Type to search Spotify", T[FAINT]
            else:
                msg, attr = "Nothing here", T[FAINT]
            msg = fit(msg, w).strip()
            put(s, ly + rows // 3, x0 + max(0, (w - width(msg)) // 2), msg, attr)
            return

        # columns
        num_w = max(2, len(str(max(tl.total, len(tl.tracks)))))
        dur_w = 7 if any(t.duration_ms >= 3_600_000 for t in tl.tracks[tl.top:tl.top + rows]) else 5
        avail = w - num_w - dur_w - 6
        show_album = avail >= 80
        if show_album:
            title_w = avail * 42 // 100
            artist_w = avail * 28 // 100
            album_w = avail - title_w - artist_w - 2
        else:
            title_w = avail * 58 // 100
            artist_w = avail - title_w
            album_w = 0

        head = [("#", num_w, True), ("  ", 2, False), ("Title", title_w, False), ("  ", 2, False),
                ("Artist", artist_w, False)]
        if show_album:
            head += [("  ", 2, False), ("Album", album_w, False)]
        head += [("  ", 2, False), ("Time", dur_w, True)]
        x = x0
        for text, cw, right in head:
            put(s, top + 2, x, fit(text, cw, right=right), T[FAINT])
            x += cw

        tl.top = _scroll(tl.sel, tl.top, rows, len(tl.tracks))
        now_uri = self.pb.track.uri if self.pb and self.pb.track else None
        focused = self.focus == "list" and not self.typing
        ell = g["ell"]
        for r in range(rows):
            i = tl.top + r
            if i >= len(tl.tracks):
                if tl.loading and r < rows:
                    put(s, ly + r, x0 + num_w + 2, "Loading…", T[FAINT])
                break
            t = tl.tracks[i]
            playing = t.uri == now_uri
            sel = i == tl.sel and focused
            if sel:
                a_num, a_title, a_dim = T[SEL_DIM], (T[SEL_ACCENT] if playing else T[SEL]), T[SEL_DIM]
            elif not t.playable:
                a_num = a_title = a_dim = T[FAINT]
            else:
                a_num = T[ACCENT] if playing else T[FAINT]
                a_title = T[ACCENT] if playing else T[TEXT]
                a_dim = T[DIM]
            if i == tl.sel and not focused:
                a_title |= curses.A_BOLD
            num = g["play"] if playing else str(i + 1)
            cells = [(fit(num, num_w, ell, True), a_num), ("  ", a_dim),
                     (fit(t.name, title_w, ell), a_title), ("  ", a_dim),
                     (fit(t.artists, artist_w, ell), a_dim)]
            if show_album:
                cells += [("  ", a_dim), (fit(t.album, album_w, ell), a_dim)]
            cells += [("  ", a_dim), (fit(fmt_time(t.duration_ms), dur_w, ell, True), a_dim)]
            x, y = x0, ly + r
            for text, attr in cells:
                put(s, y, x, text, attr)
                x += width(text)

    def draw_player(self, now: float) -> None:
        s, T, g, H, W = self.scr, self.theme, self.g, self.H, self.W
        put(s, H - 4, 2, g["rule"] * (W - 4), T[FAINT])
        pb = self.pb
        if not pb or not pb.track:
            put(s, H - 3, 4, "Nothing playing", T[DIM])
            put(s, H - 2, 4, fit("Start Spotify on any device, or press d to choose one", W - 6).rstrip(), T[FAINT])
            return
        t = pb.track

        flags = []
        if t.is_track and self.liked is not None:
            flags.append((g["heart"], T[ACCENT] if self.liked else T[FAINT]))
        flags.append(("shuffle", T[ACCENT] if pb.shuffle else T[FAINT]))
        flags.append(("repeat 1" if pb.repeat == "track" else "repeat",
                      T[ACCENT] if pb.repeat != "off" else T[FAINT]))
        if pb.volume is not None:
            flags.append((f"vol {pb.volume:>3}%", T[DIM]))
        flags_w = sum(width(f) for f, _ in flags) + 3 * (len(flags) - 1)

        icon = g["play"] if pb.is_playing else g["pause"]
        put(s, H - 3, 3, icon, T[ACCENT] | curses.A_BOLD)
        put(s, H - 3, 6, fit(t.name, max(1, W - 12 - flags_w)).rstrip(), T[TITLE])
        x = W - 3 - flags_w
        for text, attr in flags:
            put(s, H - 3, x, text, attr)
            x += width(text) + 3

        sub = t.artists + (g["sep"] + t.album if t.album else "")
        put(s, H - 2, 6, fit(sub, W - 9).rstrip(), T[DIM])
        self.draw_progress(now)

    def draw_progress(self, now: float) -> None:
        s, T, g, W = self.scr, self.theme, self.g, self.W
        pb = self.pb
        y, x = self.H - 1, 6
        cur, dur = pb.progress(now), pb.track.duration_ms
        right = fmt_time(dur)
        left = fmt_time(cur).rjust(len(right))
        bar_w = max(4, W - x - len(left) - len(right) - 6)
        filled = min(bar_w, bar_w * cur // dur) if dur else 0
        put(s, y, x, left + " ", T[DIM])
        x += len(left) + 1
        put(s, y, x, g["bar_on"] * filled, T[ACCENT])
        put(s, y, x + filled, g["bar_off"] * (bar_w - filled), T[FAINT])
        put(s, y, x + bar_w, " " + right, T[DIM])
        self.last_sec = cur // 1000

    def box(self, h: int, w: int, title: str) -> tuple:
        s, T, g = self.scr, self.theme, self.g
        h, w = min(h, self.H - 2), min(w, self.W - 4)
        y, x = (self.H - h) // 2, (self.W - w) // 2
        put(s, y, x, g["tl"] + g["h"] * (w - 2) + g["tr"], T[FAINT])
        for r in range(1, h - 1):
            put(s, y + r, x, g["v"] + " " * (w - 2) + g["v"], T[FAINT])
        put(s, y + h - 1, x, g["bl"] + g["h"] * (w - 2) + g["br"], T[FAINT])
        put(s, y, x + 2, f" {title} ", T[TITLE])
        return y + 1, x + 2, h - 2, w - 4

    def draw_help(self) -> None:
        T = self.theme
        y, x, h, w = self.box(len(HELP) + 4, 48, "keys")
        for r, (k, desc) in enumerate(HELP[: h - 2]):
            put(self.scr, y + 1 + r, x + 1, fit(k, 12), T[ACCENT])
            put(self.scr, y + 1 + r, x + 14, fit(desc, w - 15), T[TEXT])

    def draw_devices(self) -> None:
        s, T, g = self.scr, self.theme, self.g
        devs = self.devices
        n = len(devs) if devs else 1
        y, x, h, w = self.box(n + 4, 52, "devices")
        if devs is None:
            put(s, y + 1, x + 1, "Looking for devices…", T[DIM])
            return
        if not devs:
            put(s, y + 1, x + 1, fit("No devices. Open Spotify on any device.", w - 2), T[DIM])
            return
        for r, d in enumerate(devs[: h - 2]):
            sel = r == self.dev_sel
            mark = g["dot"] if d.is_active else " "
            line = fit(f" {mark} {d.name}", w - 14) + fit(d.type.lower(), 12, right=True) + " "
            put(s, y + 1 + r, x, line, T[SEL] if sel else (T[ACCENT] if d.is_active else T[TEXT]))


def main() -> None:
    try:
        settings = config.load()
    except config.ConfigError as e:
        sys.exit(str(e))

    auth = api.make_auth(settings)
    try:
        if not auth.validate_token(auth.cache_handler.get_cached_token()):
            print("Opening your browser to log in to Spotify…")
        auth.get_access_token(as_dict=False)
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception as e:
        sys.exit(f"Spotify login failed: {e}")

    # Library log output would scribble over the UI, so send it to a file instead.
    logging.basicConfig(filename=config.config_dir() / "spoterm.log", level=logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    os.environ.setdefault("ESCDELAY", "25")
    client = api.Spotify(auth)
    try:
        curses.wrapper(lambda scr: App(scr, client, settings).run())
    except KeyboardInterrupt:
        pass
