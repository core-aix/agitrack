"""Work out the host terminal's background when it will not answer OSC 11.

Most of aGiTrack's colour behaviour rests on knowing what the terminal is drawing on: the
agent-theme canvas compares the agent's scheme against it, and the capability relay reports it
to the backend so a self-theming agent picks the same side. The primary source is the terminal's
own answer to ``OSC 11 ; ? BEL``.

**Some terminals never answer.** macOS Terminal.app is the common one — it renders 256 colours
and true colour perfectly well but implements no colour *report*. Everything downstream was then
left guessing, and the guesses compounded: with nothing to relay, the backend fell back to its
own default (dark) and painted a dark UI in a white terminal, and aGiTrack could not tell that
was wrong because it had no background to compare against either.

So when the query goes unanswered, ask the platform instead. Every source here is read-only,
non-interactive and bounded — nothing may prompt the user (an AppleScript query to Terminal.app
would raise an Automation permission dialog, which is why the profile is read from preferences)
and nothing may delay startup measurably. Returning None stays a legitimate answer: callers
treat "unknown" as "do not repaint the user's screen".
"""

from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

# `COLORFGBG` is set by rxvt, konsole and others as "<fg>;<bg>" (sometimes "<fg>;<x>;<bg>"),
# the background being an ANSI colour index. 7 and 15 are the light greys a light theme uses.
_LIGHT_INDEXES = {"7", "15"}


def _rgb(red: float, green: float, blue: float) -> bytes:
    """Components in 0..1 as the ``rgb:rrrr/gggg/bbbb`` shape an OSC 11 reply carries."""
    parts = "/".join(f"{round(value * 255):02x}" * 2 for value in (red, green, blue))
    return f"rgb:{parts}".encode()


def _from_colorfgbg(environ) -> bytes | None:
    raw = environ.get("COLORFGBG")
    if not raw:
        return None
    fields = [field.strip() for field in raw.split(";") if field.strip()]
    if not fields or not fields[-1].isdigit():
        return None
    return _rgb(1, 1, 1) if fields[-1] in _LIGHT_INDEXES else _rgb(0, 0, 0)


def _unarchive_color(blob: bytes) -> tuple[float, float, float] | None:
    """The RGB of an archived ``NSColor`` as Terminal.app stores a profile's background.

    Two shapes appear, and both must be handled or the common profiles are missed: a greyscale
    colour keeps one component under ``NSWhite`` (Homebrew, Pro), an RGB one keeps three under
    ``NSRGB`` (Ocean, Novel, Grass). Each is ASCII floats with a trailing NUL, and an optional
    alpha follows the colour components — which is ignored, since a terminal composites its
    background against the desktop, not against another terminal colour.
    """
    try:
        archive = plistlib.loads(blob)
        fields = archive["$objects"][1]
    except Exception:
        return None
    for key, count in (("NSRGB", 3), ("NSWhite", 1)):
        raw = fields.get(key)
        if not isinstance(raw, (bytes, bytearray)):
            continue
        try:
            values = [float(part) for part in bytes(raw).rstrip(b"\x00").split()]
        except ValueError:
            return None
        if len(values) < count:
            return None
        components = values[:count]
        if not all(0.0 <= value <= 1.0 for value in components):
            return None
        return (components[0], components[0], components[0]) if count == 1 else tuple(components)  # type: ignore[return-value]
    return None


def _apple_terminal_background(environ) -> bytes | None:
    """Terminal.app's background, read from the profile it opens windows with.

    Terminal.app answers no colour query, but it does record which profile it starts with and
    that profile's background. A profile only STORES ``BackgroundColor`` when it differs from
    Terminal's own default — checked against every stock profile on this machine, all of which
    carry one except ``Basic``, which is the default and is white. So a missing key is not a
    failure to read: it positively means the default, light background.
    """
    if sys.platform != "darwin" or environ.get("TERM_PROGRAM") != "Apple_Terminal":
        return None
    path = Path.home() / "Library" / "Preferences" / "com.apple.Terminal.plist"
    try:
        settings = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    name = settings.get("Startup Window Settings") or settings.get("Default Window Settings")
    profile = (settings.get("Window Settings") or {}).get(name)
    if not isinstance(profile, dict):
        return None
    blob = profile.get("BackgroundColor")
    if blob is None:
        return _rgb(1, 1, 1)  # the stock default profile: white
    rgb = _unarchive_color(bytes(blob))
    return _rgb(*rgb) if rgb else None


def detect_host_background(environ=None) -> bytes | None:
    """The terminal's background as an OSC-11-shaped value, or None if it cannot be determined.

    Only consulted when the terminal itself did not answer.
    """
    environ = os.environ if environ is None else environ
    for source in (_from_colorfgbg, _apple_terminal_background):
        try:
            value = source(environ)
        except Exception:
            value = None
        if value:
            return value
    return None
