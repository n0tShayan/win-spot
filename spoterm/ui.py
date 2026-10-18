"""Drawing primitives: theme, width-aware text fitting and safe writes."""

import curses
import unicodedata
from functools import lru_cache

# Colour roles (pair ids)
TEXT, DIM, FAINT, ACCENT, SEL, SEL_ACCENT, SEL_DIM, WARN, TITLE, MUTED, MARK = range(1, 12)

# Cover art (see Theme.art): its colour pairs start here, its 24-bit colours at 256 + 32.
ART_PAIR0, ART_COLOR0, ART_COLORS = 16, 256 + 32, 64

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
        self.art_pairs = 0          # colour pairs free for cover art; 0: draw covers as text
        self.art_rgb = False        # cover colours exact (24-bit), not the nearest of 256

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
        if n >= 256:
            self.art_pairs = max(0, min(curses.COLOR_PAIRS, 256) - ART_PAIR0)
        self.art_rgb = self.truecolor and curses.COLORS >= ART_COLOR0 + ART_COLORS

    def art(self, palette: list, pairs: list) -> list:
        """Set up a cover's colours (exact where 24-bit colour is safe, else the nearest of
        the 256) and one colour pair per (top, bottom) pixel pair; returns their attributes."""
        try:
            if self.art_rgb:
                for i, c in enumerate(palette):
                    curses.init_color(ART_COLOR0 + i, *((c >> sh & 255) * 1000 // 255 for sh in (16, 8, 0)))
                ids = [ART_COLOR0 + i for i in range(len(palette))]
            else:
                ids = [_xterm(c) for c in palette]
            out = []
            for i, (top, bottom) in enumerate(pairs):
                curses.init_pair(ART_PAIR0 + i, ids[top], ids[bottom])
                out.append(curses.color_pair(ART_PAIR0 + i))
            return out
        except curses.error:
            return []

    def __getitem__(self, pid: int) -> int:
        return self.attr.get(pid, 0)


def _xterm(rgb: int) -> int:
    """The nearest colour of the xterm 256: the 6x6x6 cube or the grey ramp."""
    r, g, b = rgb >> 16, rgb >> 8 & 255, rgb & 255
    lv = (0, 95, 135, 175, 215, 255)
    q = [min(range(6), key=lambda i: abs(lv[i] - v)) for v in (r, g, b)]
    cube = sum((lv[i] - v) ** 2 for i, v in zip(q, (r, g, b)))
    gi = max(0, min(23, ((r + g + b) // 3 - 3) // 10))
    gv = 8 + 10 * gi
    grey = (r - gv) ** 2 + (g - gv) ** 2 + (b - gv) ** 2
    return 16 + 36 * q[0] + 6 * q[1] + q[2] if cube <= grey else 232 + gi


# Everything here renders single-width in Windows Terminal / Cascadia (no emoji presentation).
UNICODE_GLYPHS = {
    "play": "▶", "pause": "‖", "heart": "♥", "bar_on": "━", "bar_off": "─",
    "rule": "─", "dot": "●", "ell": "…", "sep": " · ", "cursor": "›", "mark": "▌",
    "thumb": "┃", "ramp": "▁▂▃▄▅▆▇█", "half": "▀", "note": "♪",
    "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│",
}
ASCII_GLYPHS = {
    "play": ">", "pause": "=", "heart": "<3", "bar_on": "=", "bar_off": "-",
    "rule": "-", "dot": "*", "ell": "~", "sep": " - ", "cursor": ">", "mark": ">",
    "thumb": "|", "ramp": "", "half": "", "note": "~",
    "tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|",
}


def _cw(ch: str) -> int:
    o = ord(ch)
    if o < 0x300:
        return 1 if o >= 0x20 else 0
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def width(s: str) -> int:
    return len(s) if s.isascii() else sum(_cw(c) for c in s)


@lru_cache(maxsize=1024)
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
