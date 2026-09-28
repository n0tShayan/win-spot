"""Drawing primitives: theme, width-aware text fitting and safe writes."""

import curses
import unicodedata
from functools import lru_cache

# Colour pair ids
TEXT, DIM, FAINT, ACCENT, SEL, SEL_ACCENT, SEL_DIM, WARN, TITLE = range(1, 10)


class Theme:
    def __init__(self):
        self.attr = {}

    def init(self) -> None:
        try:
            curses.start_color()
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK

        if curses.has_colors() and curses.COLORS >= 256:
            pairs = {
                TEXT: (252, bg), DIM: (244, bg), FAINT: (238, bg), ACCENT: (41, bg),
                SEL: (255, 236), SEL_ACCENT: (41, 236), SEL_DIM: (247, 236),
                WARN: (209, bg), TITLE: (255, bg),
            }
            extra = {DIM: 0, FAINT: 0}
        elif curses.has_colors():
            W, G, K, Y = curses.COLOR_WHITE, curses.COLOR_GREEN, curses.COLOR_BLACK, curses.COLOR_YELLOW
            pairs = {
                TEXT: (W, bg), DIM: (W, bg), FAINT: (W, bg), ACCENT: (G, bg),
                SEL: (K, W), SEL_ACCENT: (K, G), SEL_DIM: (K, W), WARN: (Y, bg), TITLE: (W, bg),
            }
            extra = {DIM: curses.A_DIM, FAINT: curses.A_DIM}
        else:
            self.attr = {TEXT: 0, DIM: curses.A_DIM, FAINT: curses.A_DIM, ACCENT: curses.A_BOLD,
                         SEL: curses.A_REVERSE, SEL_ACCENT: curses.A_REVERSE | curses.A_BOLD,
                         SEL_DIM: curses.A_REVERSE, WARN: curses.A_BOLD, TITLE: curses.A_BOLD}
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


UNICODE_GLYPHS = {
    "play": "▶", "pause": "‖", "heart": "♥", "bar_on": "━", "bar_off": "─",
    "rule": "─", "dot": "●", "ell": "…", "sep": " · ", "cursor": "›",
    "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│",
}
ASCII_GLYPHS = {
    "play": ">", "pause": "=", "heart": "<3", "bar_on": "=", "bar_off": "-",
    "rule": "-", "dot": "*", "ell": "~", "sep": " - ", "cursor": ">",
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
