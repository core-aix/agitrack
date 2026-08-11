"""Deriving the host terminal's background when it refuses to answer OSC 11.

macOS Terminal.app is the case that forced this: it renders true colour but implements no colour
REPORT, so detection times out on every launch. aGiTrack then had nothing to relay to the backend,
which fell back to its own default (dark) and painted a dark UI in a white terminal — and aGiTrack
could not tell that was wrong, because it had no background to compare the agent's scheme against.
"""

from __future__ import annotations

import plistlib
import sys

import pytest

from agitrack.proxy import host_background as hb

WHITE = b"rgb:ffff/ffff/ffff"
BLACK = b"rgb:0000/0000/0000"


def _archived(field: str, components: str) -> bytes:
    """An ``NSColor`` archived the way Terminal.app stores a profile background."""
    return plistlib.dumps(
        {
            "$objects": ["$null", {field: components.encode() + b"\x00", "NSColorSpace": 1}],
            "$version": 100000,
            "$archiver": "NSKeyedArchiver",
            "$top": {"root": plistlib.UID(1)},
        },
        fmt=plistlib.FMT_BINARY,
    )


def _prefs(tmp_path, monkeypatch, profile: dict | None, name: str = "Basic") -> None:
    home = tmp_path / "home"
    (home / "Library" / "Preferences").mkdir(parents=True, exist_ok=True)  # called twice per test
    (home / "Library" / "Preferences" / "com.apple.Terminal.plist").write_bytes(
        plistlib.dumps(
            {"Startup Window Settings": name, "Window Settings": {name: profile if profile is not None else {}}},
            fmt=plistlib.FMT_BINARY,
        )
    )
    monkeypatch.setattr(hb.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(sys, "platform", "darwin")


APPLE = {"TERM_PROGRAM": "Apple_Terminal"}


def test_colorfgbg_is_read_when_the_terminal_sets_it():
    # rxvt/konsole and others publish the pair rather than answering a query.
    assert hb.detect_host_background({"COLORFGBG": "0;15"}) == WHITE
    assert hb.detect_host_background({"COLORFGBG": "15;0"}) == BLACK
    assert hb.detect_host_background({"COLORFGBG": "0;default;7"}) == WHITE  # three-field form


def test_a_missing_or_unparseable_colorfgbg_is_not_guessed_at():
    for environ in ({}, {"COLORFGBG": ""}, {"COLORFGBG": "0;bogus"}, {"COLORFGBG": ";"}):
        assert hb._from_colorfgbg(environ) is None


@pytest.mark.skipif(sys.platform == "win32", reason="the Terminal.app profile reader is POSIX-pathed")
def test_apple_terminals_default_profile_means_white_not_unknown(tmp_path, monkeypatch):
    """A profile stores ``BackgroundColor`` only when it DIFFERS from Terminal's own default.

    Every stock profile carries one except ``Basic`` — which is that default, and is white. So a
    missing key is not a failed read: it positively means the light default. Treating it as
    unknown is what left the backend guessing dark in a white terminal.
    """
    _prefs(tmp_path, monkeypatch, profile={"name": "Basic"})
    assert hb.detect_host_background(APPLE) == WHITE


@pytest.mark.skipif(sys.platform == "win32", reason="the Terminal.app profile reader is POSIX-pathed")
def test_apple_terminal_greyscale_and_rgb_profiles_both_decode(tmp_path, monkeypatch):
    # Terminal stores greyscale profiles (Homebrew, Pro) under NSWhite with ONE component and
    # colour ones (Ocean, Grass) under NSRGB with three. Missing either shape loses real profiles.
    _prefs(tmp_path, monkeypatch, profile={"BackgroundColor": _archived("NSWhite", "0 0.9")})
    assert hb.detect_host_background(APPLE) == BLACK

    _prefs(tmp_path, monkeypatch, profile={"BackgroundColor": _archived("NSRGB", "1 1 1 1")})
    assert hb.detect_host_background(APPLE) == WHITE


@pytest.mark.skipif(sys.platform == "win32", reason="the Terminal.app profile reader is POSIX-pathed")
def test_a_corrupt_profile_reports_unknown_rather_than_a_wrong_colour(tmp_path, monkeypatch):
    # "Unknown" is a legitimate answer — callers treat it as "do not repaint the user's screen".
    # Inventing a colour from an unreadable archive would repaint it wrongly instead.
    _prefs(tmp_path, monkeypatch, profile={"BackgroundColor": b"not a plist"})
    assert hb.detect_host_background(APPLE) is None

    _prefs(tmp_path, monkeypatch, profile={"BackgroundColor": _archived("NSRGB", "5 5 5")})  # out of range
    assert hb.detect_host_background(APPLE) is None


def test_nothing_is_derived_for_a_terminal_that_is_not_apple_terminal(monkeypatch):
    # The reader is keyed to TERM_PROGRAM: a different terminal's silence is genuinely unknown,
    # and reading Terminal.app's profile to describe it would be a fabrication.
    monkeypatch.setattr(sys, "platform", "darwin")
    assert hb._apple_terminal_background({"TERM_PROGRAM": "iTerm.app"}) is None
    assert hb._apple_terminal_background({}) is None
