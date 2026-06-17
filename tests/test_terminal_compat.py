"""Cross-terminal compatibility tests for the host-terminal layer.

aGiTrack is a POSIX TUI: it drives the host terminal directly (raw mode, alt screen,
mouse, capability queries) and must degrade correctly across emulators that answer its
startup queries differently — iTerm2/xterm (truecolor + kitty keyboard), Apple Terminal
(no kitty protocol), and the raw Linux console / minimal setups (no alt screen, no kitty,
sometimes no colour queries at all). Getting this wrong leaks raw escape codes into the
user's shell on exit (#70 and the kitty-pop leak), which is invisible to logic-only tests.

These tests feed canned per-emulator query responses into the pure parser and capture the
exact control bytes aGiTrack writes, so the degradation paths are pinned without a real
terminal. (Windows is not a target: aGiTrack imports POSIX-only ``termios``/``pty`` and runs
on Windows only under WSL, which is Linux — see ``docs`` / README Requirements.)
"""

from __future__ import annotations

import os

import pytest

from agitrack.proxy.terminal import TerminalHost
from proxy_helpers import capture_fd, make_runner

pytestmark = pytest.mark.skipif(os.name != "posix", reason="aGiTrack is POSIX-only (Windows runs under WSL = Linux)")


# Canned responses to aGiTrack's startup capability queries (OSC 10/11 colours, OSC 4
# palette, CSI ?u kitty-keyboard, CSI c device-attributes), one per emulator class.
ITERM2_XTERM = (
    b"\x1b]10;rgb:c7c7/c7c7/c7c7\x07"
    b"\x1b]11;rgb:0000/0000/0000\x07"
    b"\x1b]4;1;rgb:ffff/0000/0000\x07"
    b"\x1b[?1u"  # speaks the kitty keyboard protocol
    b"\x1b[?62;c"  # primary device attributes
)
APPLE_TERMINAL = (
    b"\x1b]10;rgb:0000/0000/0000\x07"
    b"\x1b]11;rgb:ffff/ffff/ffff\x07"
    b"\x1b[?6c"  # DA reply, but NO kitty ?u reply
)
RAW_LINUX_CONSOLE = b"\x1b[?6c"  # only device attributes; no colours, no kitty
DUMB_TERMINAL = b""  # answers nothing at all


def test_iterm2_xterm_profile_parsed_fully():
    host = TerminalHost()
    host.parse_host_terminal_responses(ITERM2_XTERM)
    assert host.host_fg_value == b"rgb:c7c7/c7c7/c7c7"
    assert host.host_bg_value == b"rgb:0000/0000/0000"
    assert host.host_palette[b"1"] == b"rgb:ffff/0000/0000"
    assert host.host_da == b"\x1b[?62;c"
    assert host.host_kitty_keyboard is True


def test_apple_terminal_profile_has_colours_but_no_kitty():
    host = TerminalHost()
    host.parse_host_terminal_responses(APPLE_TERMINAL)
    assert host.host_fg_value == b"rgb:0000/0000/0000"
    assert host.host_bg_value == b"rgb:ffff/ffff/ffff"
    assert host.host_kitty_keyboard is False  # must NOT be sent kitty push/pop later


def test_raw_linux_console_profile_is_all_defaults():
    host = TerminalHost()
    host.parse_host_terminal_responses(RAW_LINUX_CONSOLE)
    assert host.host_fg_value is None
    assert host.host_bg_value is None
    assert host.host_palette == {}
    assert host.host_kitty_keyboard is False
    assert host.host_da == b"\x1b[?6c"


def test_dumb_terminal_profile_leaves_everything_unset():
    host = TerminalHost()
    host.parse_host_terminal_responses(DUMB_TERMINAL)
    assert host.host_fg_value is None and host.host_da is None
    assert host.host_kitty_keyboard is False


def test_kitty_pop_sent_only_to_kitty_capable_host():
    # The kitty keyboard "pop" (CSI < u) must reach ONLY a terminal that speaks the
    # protocol; sending it to Apple Terminal / the raw console leaks a visible code on
    # exit. modifyOtherKeys-off (CSI >4;0m) is an ordinary CSI, so it is always emitted.
    kitty = TerminalHost()
    kitty.host_kitty_keyboard = True
    with capture_fd() as out:
        kitty.disable_host_terminal_modes()
    assert b"\x1b[<u" in out[0]
    assert b"\x1b[>4;0m" in out[0]

    plain = TerminalHost()
    plain.host_kitty_keyboard = False
    with capture_fd() as out:
        plain.disable_host_terminal_modes()
    assert b"\x1b[<u" not in out[0]  # no kitty pop on a non-kitty host
    assert b"\x1b[>4;0m" in out[0]


def test_restore_terminal_always_leaves_alt_screen_and_restores_cursor():
    # Regardless of host, exit must leave the alt screen, re-show the cursor and reset
    # SGR — on terminals without real alt-screen support these are harmless no-ops, but
    # omitting them leaves aGiTrack's UI on screen after exit (#70).
    host = TerminalHost()
    with capture_fd() as out:
        host.restore_terminal()
    emitted = out[0]
    assert b"\x1b[?1049l" in emitted  # leave alt screen
    assert b"\x1b[?25h" in emitted  # cursor visible
    assert b"\x1b[2J\x1b[H" in emitted  # clear+home before leaving (the #70 fix)


def test_enter_host_screen_enables_alt_screen_and_mouse():
    host = TerminalHost()
    with capture_fd() as out:
        host.enter_host_screen()
    emitted = out[0]
    assert b"\x1b[?1049h" in emitted  # alt screen
    assert b"\x1b[?1000h" in emitted and b"\x1b[?1006h" in emitted  # SGR mouse reporting


def test_terminal_size_falls_back_when_not_a_tty(monkeypatch):
    host = TerminalHost()

    def boom(_fd):
        raise OSError("not a tty")

    monkeypatch.setattr("agitrack.proxy.terminal.os.get_terminal_size", boom)
    assert host.terminal_size() == (24, 80)


def test_sync_terminal_modes_mirrors_kitty_key_negotiation_only_when_host_supports_it():
    # When the backend negotiates the kitty keyboard protocol (a ``CSI > … u`` push),
    # aGiTrack mirrors it to the host ONLY if the host speaks it; the modifyOtherKeys
    # form (``CSI >4;2m``) is mirrored unconditionally. Otherwise the kitty push leaks
    # as visible text on a plain terminal.
    backend_output = b"\x1b[>1u\x1b[>4;2m"

    runner = make_runner()
    runner.host_kitty_keyboard = True
    with capture_fd() as out:
        runner._sync_terminal_modes(backend_output)
    assert b"\x1b[>1u" in out[0]
    assert b"\x1b[>4;2m" in out[0]

    runner = make_runner()
    runner.host_kitty_keyboard = False
    with capture_fd() as out:
        runner._sync_terminal_modes(backend_output)
    assert b"\x1b[>1u" not in out[0]  # kitty push withheld from a non-kitty host
    assert b"\x1b[>4;2m" in out[0]  # modifyOtherKeys still mirrored
