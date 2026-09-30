"""Entry point for `python -m spoterm`, the `spoterm` command and the packaged SpoTerm.exe."""

import sys

from . import __version__

USAGE = """usage: spoterm [--version | --help]

A keyboard-driven Spotify player for the terminal. Press ? inside for the keys."""


def _own_console() -> bool:
    """Whether this process got a console window of its own (SpoTerm.exe double-clicked),
    which Windows closes the moment we exit, taking any error message with it."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    try:
        import ctypes
        return ctypes.windll.kernel32.GetConsoleProcessList((ctypes.c_uint * 2)(), 2) <= 1
    except (OSError, AttributeError):
        return False


def run() -> None:
    args = sys.argv[1:]
    if args[:1] == ["--login"]:          # SpoTerm's sign-in, run as its own process (see app)
        from .login import main as login
        sys.exit(login())
    if args[:1] in (["--version"], ["-V"]):
        print(f"spoterm {__version__}")
        return
    if args:
        print(USAGE)
        return

    from .app import main
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0) and _own_console():
            if not isinstance(e.code, int):
                print(e.code, file=sys.stderr)
            try:
                input("\nPress enter to close.")
            except (EOFError, KeyboardInterrupt):
                pass
            raise SystemExit(1) from None
        raise


if __name__ == "__main__":
    run()
