"""Light or dark: the `theme` setting, and what "auto" finds out (#77).

Textual cannot tell what the terminal looks like, so "auto" asks before the app
takes the terminal over. In order:

1. The terminal itself, with an OSC 11 query for its background colour. This
   is the only answer that is about the window the app will draw in, and the
   only one that works on a login node over SSH.
2. ``COLORFGBG``, which rxvt, Konsole and some others export as "fg;bg".
3. On macOS, the system appearance (``AppleInterfaceStyle``).

Anything else is dark, which is what LazySlurm has always been.
"""

from __future__ import annotations

import os
import re
import select
import subprocess
import sys

THEMES = {"dark": "textual-dark", "light": "textual-light"}

# What "auto" found at startup: "light", "dark", or None for "could not tell".
# Detected once, in main(), while the terminal is still ours to query.
_detected: str | None = None

# How long to wait for the terminal to answer the background query. A terminal
# that does not support it says nothing at all, so this is paid in full there.
_OSC_TIMEOUT = 0.15

_OSC_REPLY = re.compile(r"\]11;rgba?:([0-9a-f]+)/([0-9a-f]+)/([0-9a-f]+)", re.IGNORECASE)


def textual_theme(setting: str) -> str:
    """The Textual theme name for a `theme` setting of auto, dark or light."""
    if setting == "auto":
        setting = _detected or "dark"
    return THEMES.get(setting, THEMES["dark"])


def detect() -> str | None:
    """Work out whether the terminal is light or dark, and remember it."""
    global _detected
    _detected = _from_terminal() or _from_colorfgbg() or _from_macos()
    return _detected


def _is_light(red: float, green: float, blue: float) -> bool:
    """Relative luminance above the midpoint (channels in 0..1)."""
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue > 0.5


def parse_osc11(reply: str) -> str | None:
    """`\\e]11;rgb:ffff/ffff/ffff\\e\\\\` -> "light". Channels may be 1-4 hex digits."""
    match = _OSC_REPLY.search(reply)
    if not match:
        return None
    channels = [int(h, 16) / (16 ** len(h) - 1) for h in match.groups()]
    return "light" if _is_light(*channels) else "dark"


def parse_colorfgbg(value: str) -> str | None:
    """`15;0` -> "dark". The background is the last field, an ANSI colour index."""
    bg = value.rsplit(";", 1)[-1].strip()
    if not bg.isdigit():
        return None
    # 7 is white and 9-15 the bright colours; 8 is bright black, i.e. grey.
    return "light" if int(bg) == 7 or 9 <= int(bg) <= 15 else "dark"


def _from_colorfgbg() -> str | None:
    value = os.environ.get("COLORFGBG", "")
    return parse_colorfgbg(value) if value else None


def _from_macos() -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # The key only exists in dark mode; reading it fails in light mode.
    return "dark" if result.stdout.strip().lower() == "dark" else "light"


def _from_terminal() -> str | None:
    """Ask the terminal for its background colour (OSC 11)."""
    try:
        import termios
        import tty
    except ImportError:
        return None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return None
    try:
        saved = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            os.write(fd, b"\033]11;?\033\\")
            reply = b""
            while select.select([fd], [], [], _OSC_TIMEOUT)[0]:
                reply += os.read(fd, 64)
                if reply.endswith((b"\033\\", b"\a")):
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSAFLUSH, saved)
    except (OSError, termios.error):
        return None
    finally:
        os.close(fd)
    return parse_osc11(reply.decode("ascii", "replace"))
