"""Host-terminal control for the proxy package (#29, P1).

Contains :class:`TerminalHost` — extracted from :class:`~agitrack.proxy.runner.ProxyRunner`
— which owns all interactions with the *host* terminal: raw/cooked mode
switching, alt-screen enter/leave, mouse enable/disable, terminal-capability
detection (fg/bg/palette/DA query-and-cache), and terminal-size querying.
"""

from __future__ import annotations

import ctypes
import os
import re
import select
import sys
import time
from typing import Any, Protocol

if sys.platform != "win32":
    import termios
    import tty

# ---------------------------------------------------------------------------
# Windows console-mode helpers
# ---------------------------------------------------------------------------
# These constants and helpers are only used on Windows; on POSIX the code path
# uses termios / tty as before.

if sys.platform == "win32":
    # Console mode flags (wincon.h)
    _ENABLE_PROCESSED_INPUT = 0x0001
    _ENABLE_LINE_INPUT = 0x0002
    _ENABLE_ECHO_INPUT = 0x0004
    _ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
    _ENABLE_PROCESSED_OUTPUT = 0x0001
    _ENABLE_WRAP_AT_EOL_OUTPUT = 0x0002
    _ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    _ENABLE_DISABLE_NEWLINE_AUTO_RETURN = 0x0008

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _STD_INPUT_HANDLE = -10
    _STD_OUTPUT_HANDLE = -11

    def _win_get_console_mode(handle) -> int:
        mode = ctypes.c_ulong()
        _kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        return mode.value

    def _win_set_console_mode(handle, mode: int) -> None:
        _kernel32.SetConsoleMode(handle, ctypes.c_ulong(mode))

    def _win_stdin_handle():
        return _kernel32.GetStdHandle(_STD_INPUT_HANDLE)

    def _win_stdout_handle():
        return _kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)


class TerminalHostState(Protocol):
    """Structural type for whatever ``TerminalHost``'s methods run against.

    ``TerminalHost`` satisfies this directly; :class:`~agitrack.proxy.runner.ProxyRunner`
    satisfies it via its host-terminal attributes and thin delegator methods, so the
    runner can call ``TerminalHost.method(self, ...)`` unbound and still type-check.
    """

    old_attrs: Any
    host_fg_value: bytes | None
    host_bg_value: bytes | None
    host_palette: dict[bytes, bytes]
    host_da: bytes | None
    host_kitty_keyboard: bool

    def set_raw(self) -> None: ...
    def set_cooked(self) -> None: ...
    def enable_host_mouse(self) -> None: ...
    def disable_host_terminal_modes(self) -> None: ...
    def parse_host_terminal_responses(self, data: bytes, *, debug_fn=...) -> None: ...


class TerminalHost:
    """Host-terminal control: raw mode, alt screen, mouse, capability detection.

    The class is instantiated once per ProxyRunner (host-level, not per
    session).  It holds the raw-mode save/restore attrs and the cached
    host-terminal colour responses so OpenCode can learn the real terminal
    theme.
    """

    def __init__(self) -> None:
        # Saved cooked-mode attrs so _set_cooked can restore them.
        self.old_attrs = None

        # Raw responses captured from the host terminal so we can answer the
        # same queries OpenCode makes (foreground/background/palette colors and
        # device attributes). Without these, OpenCode cannot detect the real
        # terminal theme and its colors do not match a native session.
        self.host_fg_value: bytes | None = None
        self.host_bg_value: bytes | None = None
        self.host_palette: dict[bytes, bytes] = {}
        self.host_da: bytes | None = None
        # Whether the host terminal supports the kitty keyboard protocol (it
        # answered the ``CSI ? u`` query). Terminals that don't (e.g. the raw
        # Linux console, some minimal Ubuntu setups) must NOT be sent the
        # protocol's push/pop sequences — they leak as visible text.
        self.host_kitty_keyboard: bool = False

    # ------------------------------------------------------------------
    # Terminal mode
    # ------------------------------------------------------------------

    def set_raw(self: TerminalHostState) -> None:
        if sys.platform == "win32":
            h = _win_stdin_handle()
            mode = _win_get_console_mode(h)
            # Disable line-buffering and echo; enable VT input sequences.
            mode &= ~(_ENABLE_LINE_INPUT | _ENABLE_ECHO_INPUT | _ENABLE_PROCESSED_INPUT)
            mode |= _ENABLE_VIRTUAL_TERMINAL_INPUT
            _win_set_console_mode(h, mode)
            # Enable VT processing on stdout so ANSI escape sequences render.
            ho = _win_stdout_handle()
            mo = _win_get_console_mode(ho)
            mo |= _ENABLE_VIRTUAL_TERMINAL_PROCESSING | _ENABLE_DISABLE_NEWLINE_AUTO_RETURN
            _win_set_console_mode(ho, mo)
        else:
            tty.setraw(sys.stdin.fileno())

    def set_cooked(self: TerminalHostState) -> None:
        if sys.platform == "win32":
            if self.old_attrs is not None:
                _win_set_console_mode(_win_stdin_handle(), self.old_attrs)
        elif self.old_attrs is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_attrs)

    def restore_terminal(self: TerminalHostState) -> None:
        # Disable mouse reporting *before* handing the terminal back, then drop any
        # mouse reports the terminal already queued — otherwise those buffered SGR
        # sequences (e.g. "\x1b[<35;..M") leak to the shell as stray hex after exit.
        self.disable_host_terminal_modes()
        if sys.platform != "win32":
            try:
                termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
            except (termios.error, OSError, ValueError):
                pass
        self.set_cooked()
        # Clear+home *before* leaving the alt screen, then leave it. On terminals
        # that support the alternate screen (macOS, VTE/gnome-terminal) the clear
        # only touches the alt buffer we're about to discard — `?1049l` still
        # restores the pre-agit content, so nothing changes. On terminals without
        # alt-screen support (raw Linux console, some Ubuntu setups, tmux with
        # altscreen disabled) `?1049h`/`?1049l` are no-ops and aGiTrack drew straight
        # onto the main screen, so without this clear its UI would linger after
        # exit (#70). Doing it in this order fixes that case without nuking the
        # user's scrollback on terminals where the alt screen does work.
        os.write(sys.stdout.fileno(), b"\x1b[2J\x1b[H\x1b[?1049l\x1b[0m\r\n")

    def pause_child_ui(self: TerminalHostState) -> None:
        self.set_cooked()
        os.write(sys.stdout.fileno(), b"\x1b[0m\r\n")

    def resume_child_ui(self: TerminalHostState, render_fn) -> None:
        self.set_raw()
        render_fn()

    # ------------------------------------------------------------------
    # Alt screen / mouse
    # ------------------------------------------------------------------

    def enter_host_screen(self: TerminalHostState) -> None:
        os.write(sys.stdout.fileno(), b"\x1b[?1049h\x1b[2J\x1b[H")
        self.enable_host_mouse()

    def enable_host_mouse(self: TerminalHostState) -> None:
        # Enable SGR mouse reporting on the host (1000 = button press/release +
        # wheel) so aGiTrack receives wheel events for scrollback and press/release
        # for its own copy. This is the minimal mode that reliably reports the
        # wheel; richer motion tracking (1002/1003) changes wheel reporting on
        # some terminals and is avoided. Backends that manage the mouse
        # themselves (OpenCode) re-assert their own modes afterwards.
        os.write(sys.stdout.fileno(), b"\x1b[?1000h\x1b[?1006h")

    def disable_host_terminal_modes(self: TerminalHostState) -> None:
        # Reset modes commonly enabled by full-screen TUIs: mouse tracking,
        # focus reporting, bracketed paste, alternate-scroll, cursor visibility,
        # and styling. Emit this independently from cooked-mode restoration so it
        # can also run from signal handlers before Python exits.
        reset = bytearray(
            b"\x1b[?9l\x1b[?1000l\x1b[?1001l\x1b[?1002l\x1b[?1003l\x1b[?1004l"
            b"\x1b[?1005l\x1b[?1006l\x1b[?1007l\x1b[?1015l\x1b[?1016l\x1b[?2004l"
        )
        # Pop any kitty keyboard flags we mirrored for the backend — but ONLY on a
        # host that speaks the protocol. Sending ``CSI < u`` to a terminal that
        # doesn't understand it leaks as a stray visible code at exit (the raw Linux
        # console / some Ubuntu setups). modifyOtherKeys off is left unconditional —
        # it's an xterm sequence terminals consume as an ordinary CSI.
        if getattr(self, "host_kitty_keyboard", False):
            reset += b"\x1b[<u"
        reset += b"\x1b[>4;0m\x1b[?25h\x1b[0m"
        os.write(sys.stdout.fileno(), bytes(reset))

    # ------------------------------------------------------------------
    # Terminal size
    # ------------------------------------------------------------------

    def terminal_size(self: TerminalHostState) -> tuple[int, int]:
        try:
            size = os.get_terminal_size(sys.stdout.fileno())
            return size.lines, size.columns
        except OSError:
            return 24, 80

    # ------------------------------------------------------------------
    # Host-terminal capability detection
    # ------------------------------------------------------------------

    def detect_host_terminal(self: TerminalHostState, debug_fn=None) -> None:
        # Ask the host terminal the same questions OpenCode asks on startup and
        # cache the raw answers. OpenCode adapts its entire theme to the
        # reported foreground/background, so relaying the real values is what
        # makes its colors match a native session.
        # On Windows, terminal capability detection via escape-sequence queries is
        # unreliable (the console may not respond to OSC queries), so we skip it.
        if sys.platform == "win32":
            return
        queries = bytearray(b"\x1b]10;?\x07\x1b]11;?\x07")
        for index in range(16):
            queries += b"\x1b]4;%d;?\x07" % index
        queries += b"\x1b[?u"  # kitty keyboard protocol query (answered only if supported)
        queries += b"\x1b[c"  # primary device attributes; also a response sentinel
        try:
            os.write(sys.stdout.fileno(), bytes(queries))
        except OSError:
            return
        buffer = bytearray()
        deadline = time.monotonic() + 0.5
        stdin_fd = sys.stdin.fileno()
        while time.monotonic() < deadline:
            readable, _, _ = select.select([stdin_fd], [], [], deadline - time.monotonic())
            if stdin_fd not in readable:
                break
            try:
                chunk = os.read(stdin_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            if re.search(rb"\x1b\[\?[0-9;]*c", bytes(buffer)):
                break
        self.parse_host_terminal_responses(bytes(buffer), debug_fn=debug_fn)
        # Drop any response bytes still queued (a slow/extra reply that arrived after
        # the DA sentinel) so they don't leak into the session as visible hex codes.
        try:
            termios.tcflush(stdin_fd, termios.TCIFLUSH)
        except (termios.error, OSError, ValueError):
            pass

    def parse_host_terminal_responses(self: TerminalHostState, data: bytes, *, debug_fn=None) -> None:
        if not data:
            return
        fg = re.search(rb"\x1b\]10;([^\x07\x1b]*)(?:\x07|\x1b\\)", data)
        if fg:
            self.host_fg_value = fg.group(1)
        bg = re.search(rb"\x1b\]11;([^\x07\x1b]*)(?:\x07|\x1b\\)", data)
        if bg:
            self.host_bg_value = bg.group(1)
        for match in re.finditer(rb"\x1b\]4;(\d+);([^\x07\x1b]*)(?:\x07|\x1b\\)", data):
            self.host_palette[match.group(1)] = match.group(2)
        da = re.search(rb"\x1b\[\?[0-9;]*c", data)
        if da:
            self.host_da = da.group(0)
        # A ``CSI ? <flags> u`` reply means the host speaks the kitty keyboard
        # protocol (distinct from the DA reply above, which ends in ``c``).
        if re.search(rb"\x1b\[\?[0-9;]*u", data):
            self.host_kitty_keyboard = True
        if debug_fn:
            debug_fn(
                f"host terminal fg={self.host_fg_value!r} bg={self.host_bg_value!r} "
                f"palette={len(self.host_palette)} da={self.host_da!r} kitty={self.host_kitty_keyboard}"
            )

    # ------------------------------------------------------------------
    # Resize (stdout write side only; PTY ioctl stays in runner for now)
    # ------------------------------------------------------------------
