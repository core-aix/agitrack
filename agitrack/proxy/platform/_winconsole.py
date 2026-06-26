"""Win32 console helpers (via ``ctypes``) for the native-Windows host terminal (#118).

Imported only on Windows. Wraps the handful of console calls the host terminal needs:
switching the console to VT-passthrough raw mode — so the *same* ANSI input bytes and
output sequences the POSIX path already produces/consumes work unchanged — and querying
the window size (to replace ``SIGWINCH``). Guarded by ``sys.platform`` at the call sites so
mypy never type-checks ``ctypes.windll`` on POSIX.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

_kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # Windows-only

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
_CP_UTF8 = 65001

# Console input modes (SetConsoleMode on the input handle).
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_WINDOW_INPUT = 0x0008
ENABLE_MOUSE_INPUT = 0x0010
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
# Console output modes (SetConsoleMode on the output handle).
ENABLE_PROCESSED_OUTPUT = 0x0001
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
DISABLE_NEWLINE_AUTO_RETURN = 0x0008


class _COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _SMALL_RECT(ctypes.Structure):
    _fields_ = [
        ("Left", ctypes.c_short),
        ("Top", ctypes.c_short),
        ("Right", ctypes.c_short),
        ("Bottom", ctypes.c_short),
    ]


class _CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", _COORD),
        ("dwCursorPosition", _COORD),
        ("wAttributes", wintypes.WORD),
        ("srWindow", _SMALL_RECT),
        ("dwMaximumWindowSize", _COORD),
    ]


def _std_handle(which: int) -> int:
    return _kernel32.GetStdHandle(which)


def _get_mode(handle: int) -> int:
    mode = wintypes.DWORD()
    _kernel32.GetConsoleMode(handle, ctypes.byref(mode))
    return mode.value


def _set_mode(handle: int, mode: int) -> None:
    _kernel32.SetConsoleMode(handle, mode)


def read_input(length: int) -> bytes:
    """Blocking read of up to *length* bytes from the console input handle.

    With ``ENABLE_VIRTUAL_TERMINAL_INPUT`` set (see :class:`RawConsole`), the console
    delivers keystrokes as the same VT escape-byte stream the POSIX path reads from stdin,
    so the reader thread can copy these straight into the bridge socket. Returns ``b""`` on
    EOF / error so the reader loop ends cleanly.
    """
    handle = _std_handle(STD_INPUT_HANDLE)
    buf = ctypes.create_string_buffer(length)
    read = wintypes.DWORD()
    ok = _kernel32.ReadFile(handle, buf, length, ctypes.byref(read), None)
    if not ok or read.value == 0:
        return b""
    return buf.raw[: read.value]


def terminal_size() -> tuple[int, int]:
    """``(rows, cols)`` of the console window, or a ``(24, 80)`` fallback."""
    info = _CONSOLE_SCREEN_BUFFER_INFO()
    handle = _std_handle(STD_OUTPUT_HANDLE)
    if not _kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
        return 24, 80
    win = info.srWindow
    rows = win.Bottom - win.Top + 1
    cols = win.Right - win.Left + 1
    return (rows if rows > 0 else 24, cols if cols > 0 else 80)


class RawConsole:
    """Switch the console to VT raw mode and restore the prior modes on exit.

    Input: drop line/echo/processed/mouse/quick-edit, enable VT input (so keystrokes
    arrive as the same VT byte sequences the POSIX path parses) and window-input (so a
    resize is observable). Output: enable VT processing so the existing ANSI output
    sequences render.
    """

    def __init__(self) -> None:
        self._in = _std_handle(STD_INPUT_HANDLE)
        self._out = _std_handle(STD_OUTPUT_HANDLE)
        self._saved_in: int | None = None
        self._saved_out: int | None = None
        self._saved_cp: int | None = None
        self._saved_out_cp: int | None = None

    def enter(self) -> None:
        # aGiTrack emits UTF-8 (box-drawing, status line, agent output); without UTF-8
        # code pages the console renders multi-byte sequences as garbage. Switch both code
        # pages to CP_UTF8 (65001) and restore them on leave.
        self._saved_cp = _kernel32.GetConsoleCP()
        self._saved_out_cp = _kernel32.GetConsoleOutputCP()
        _kernel32.SetConsoleCP(_CP_UTF8)
        _kernel32.SetConsoleOutputCP(_CP_UTF8)
        self._saved_in = _get_mode(self._in)
        self._saved_out = _get_mode(self._out)
        in_mode = self._saved_in
        in_mode &= ~(
            ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_PROCESSED_INPUT | ENABLE_MOUSE_INPUT | ENABLE_QUICK_EDIT_MODE
        )
        in_mode |= ENABLE_EXTENDED_FLAGS | ENABLE_VIRTUAL_TERMINAL_INPUT | ENABLE_WINDOW_INPUT
        _set_mode(self._in, in_mode)
        # Discard anything already sitting in the console input buffer before the stdin
        # bridge starts forwarding it to the backend — the POSIX path does the same with
        # termios.tcflush. Without this, a keystroke left over from the cooked-mode startup
        # prompts (notably a stray Ctrl-C, which raw mode delivers as a 0x03 byte) would be
        # forwarded straight into the freshly-spawned backend and kill it on launch
        # (Windows STATUS_CONTROL_C_EXIT, 0xC000013A).
        _kernel32.FlushConsoleInputBuffer(self._in)
        out_mode = (
            self._saved_out | ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING | DISABLE_NEWLINE_AUTO_RETURN
        )
        _set_mode(self._out, out_mode)

    def leave(self) -> None:
        if self._saved_in is not None:
            _set_mode(self._in, self._saved_in)
        if self._saved_out is not None:
            _set_mode(self._out, self._saved_out)
        if self._saved_cp is not None:
            _kernel32.SetConsoleCP(self._saved_cp)
        if self._saved_out_cp is not None:
            _kernel32.SetConsoleOutputCP(self._saved_out_cp)
