"""Drawing primitives: theme, width-aware text fitting and safe writes."""

import curses
import unicodedata
from functools import lru_cache

# Colour roles (pair ids)
TEXT, DIM, FAINT, ACCENT, SEL, SEL_ACCENT, SEL_DIM, WARN, TITLE, MUTED, MARK = range(1, 12)

# Palette for 256-colour terminals, and the exact colours used where 24-bit colour is safe.
XTERM = {"text": 252, "dim": 246, "faint": 240, "accent": 41, "band": 236, "white": 255,
         "sel_dim": 249, "warn": 209, "muted": 65}
RGB = {"text": 0xD4D4D4, "dim": 0x9E9E9E, "faint": 0x5A5A5A, "accent": 0x1DB954, "band": 0x2A2A2A,
       "white": 0xFFFFFF, "sel_dim": 0xB3B3B3, "warn": 0xF0925A, "muted": 0x4B8A61}


class Theme:
    """Colour roles: text in three strengths, one green accent, a selection band and a warning."""

    def __init__(self):
        self.attr = {}
        self.truecolor = False

    def init(self) -> None:
        try:
            curses.start_color()
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK

        extra = {}
        n = curses.COLORS if curses.has_colors() else 0
        if n >= 256:
            c = dict(XTERM)
            # PDCurses (Windows Terminal) emits colours past 256 as 24-bit SGR and leaves the
            # terminal's palette alone. ncurses would rewrite the palette itself, so stay off it.
            if n > 256 and curses.can_change_color():
                try:
                    for i, (name, v) in enumerate(RGB.items()):
                        curses.init_color(256 + i, *((v >> sh & 255) * 1000 // 255 for sh in (16, 8, 0)))
                        c[name] = 256 + i
                    self.truecolor = True
                except curses.error:
                    c = dict(XTERM)
            band = c["band"]
            pairs = {
                TEXT: (c["text"], bg), DIM: (c["dim"], bg), FAINT: (c["faint"], bg),
                ACCENT: (c["accent"], bg), SEL: (c["white"], band), SEL_ACCENT: (c["accent"], band),
                SEL_DIM: (c["sel_dim"], band), WARN: (c["warn"], bg), TITLE: (c["white"], bg),
                MUTED: (c["muted"], bg), MARK: (c["accent"], band),
            }
        elif n >= 8:
            W, G, K, Y = curses.COLOR_WHITE, curses.COLOR_GREEN, curses.COLOR_BLACK, curses.COLOR_YELLOW
            faint = 8 if n >= 16 else W   # bright black is a real grey when it exists
            pairs = {
                TEXT: (W, bg), DIM: (W, bg), FAINT: (faint, bg), ACCENT: (G, bg),
                SEL: (K, W), SEL_ACCENT: (K, W), SEL_DIM: (K, W), WARN: (Y, bg), TITLE: (W, bg),
                MUTED: (W, bg), MARK: (G, W),
            }
            extra = {DIM: curses.A_DIM, MUTED: curses.A_DIM, SEL_ACCENT: curses.A_BOLD}
            if n < 16:
                extra[FAINT] = curses.A_DIM
        else:
            R, B, D = curses.A_REVERSE, curses.A_BOLD, curses.A_DIM
            self.attr = {TEXT: 0, DIM: D, FAINT: D, ACCENT: B, SEL: R, SEL_ACCENT: R | B,
                         SEL_DIM: R, WARN: B, TITLE: B, MUTED: D, MARK: R | B}
            return

        for pid, (fg, bgc) in pairs.items():
            try:
                curses.init_pair(pid, fg, bgc)
                self.attr[pid] = curses.color_pair(pid) | extra.get(pid, 0)
            except curses.error:
                self.attr[pid] = 0
        self.attr[TITLE] |= curses.A_BOLD

    def __getitem__(self, pid: int) -> int:
        return self.attr.get(pid, 0)


# Everything here renders single-width in Windows Terminal / Cascadia (no emoji presentation).
UNICODE_GLYPHS = {
    "play": "▶", "pause": "‖", "heart": "♥", "bar_on": "━", "bar_off": "─",
    "rule": "─", "dot": "●", "ell": "…", "sep": " · ", "cursor": "›", "mark": "▌",
    "thumb": "┃", "ramp": "▁▂▃▄▅▆▇█",
    "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│",
}
ASCII_GLYPHS = {
    "play": ">", "pause": "=", "heart": "<3", "bar_on": "=", "bar_off": "-",
    "rule": "-", "dot": "*", "ell": "~", "sep": " - ", "cursor": ">", "mark": ">",
    "thumb": "|", "ramp": "",
    "tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|",
}


def _cw(ch: str) -> int:
    o = ord(ch)
    if o < 0x300:
        return 1 if o >= 0x20 else 0
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def width(s: str) -> int:
    return len(s) if s.isascii() else sum(_cw(c) for c in s)


@lru_cache(maxsize=4096)
def fit(s: str, w: int, ell: str = "…", right: bool = False) -> str:
    """Pad or truncate s to exactly w terminal columns."""
    if w <= 0:
        return ""
    if s.isascii() and s.isprintable():
        if len(s) > w:
            s = s[: w - len(ell)] + ell if w > len(ell) else s[:w]
        return s.rjust(w) if right else s.ljust(w)
    s = "".join(c for c in s if c.isprintable())
    sw = width(s)
    if sw > w:
        out, used, limit = [], 0, w - width(ell)
        for c in s:
            cw = _cw(c)
            if used + cw > limit:
                break
            out.append(c)
            used += cw
        s, sw = "".join(out) + ell, used + width(ell)
    pad = " " * (w - sw)
    return pad + s if right else s + pad


def put(win, y: int, x: int, s: str, attr: int = 0) -> None:
    """addstr that never raises (writing the bottom-right cell is an error in curses)."""
    try:
        win.addstr(y, x, s, attr)
    except curses.error:
        pass


def fmt_time(ms: int) -> str:
    s = max(0, ms) // 1000
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
