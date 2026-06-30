"""ConPTY child for the mouse-input harness.

Runs INSIDE a ConPTY created by harness.py. It mimics what a TUI backend
(claude/opencode) does: put its console input into raw VT mode, request SGR mouse
reporting (and optionally win32-input-mode) by writing the DEC private mode sets to
stdout, then read whatever conhost delivers on stdin and append the raw bytes (hex)
to a log file so the parent can inspect exactly what arrived.

argv:  mouse_child.py <logpath> <modes>
  <modes> is a comma list of any of: vt, mouse, mouseinput, win32
    vt          -> ENABLE_VIRTUAL_TERMINAL_INPUT on the console (read VT from stdin)
    mouse       -> write \\x1b[?1000h\\x1b[?1006h to stdout (request SGR mouse)
    mouseinput  -> ENABLE_MOUSE_INPUT in the console mode
    win32       -> write \\x1b[?9001h to stdout (request win32-input-mode)
    readinput   -> read raw INPUT_RECORDs via ReadConsoleInputW (logs KEY vs MOUSE
                   record types) instead of os.read of the VT byte stream

The child quits when it reads a 'q' byte / a 'q' key record, or after ~8s.

NOTE: this child must be launched in a REAL console (e.g. via Start-Process), not through
the non-interactive PowerShell/Bash agent tools — those lack a true attached console, so the
ConPTY child's std handles don't bind to the pseudoconsole and input/output won't route.
"""

import ctypes
import os
import sys
import time
from ctypes import wintypes

STD_INPUT_HANDLE = -10
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_MOUSE_INPUT = 0x0010
ENABLE_EXTENDED_FLAGS = 0x0080
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
# Without explicit restype/argtypes, ctypes treats a returned HANDLE as a 32-bit c_int and
# TRUNCATES it on Win64, so GetConsoleMode/SetConsoleMode then operate on a bogus handle and
# silently no-op (mode reads back 0x0000). Declare the real signatures.
k32.GetStdHandle.restype = wintypes.HANDLE
k32.GetStdHandle.argtypes = [wintypes.DWORD]
k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
k32.GetConsoleMode.restype = wintypes.BOOL
k32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.SetConsoleMode.restype = wintypes.BOOL


class _COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("UnicodeChar", ctypes.c_wchar),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class _MOUSE_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("dwMousePosition", _COORD),
        ("dwButtonState", wintypes.DWORD),
        ("dwControlKeyState", wintypes.DWORD),
        ("dwEventFlags", wintypes.DWORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("KeyEvent", _KEY_EVENT_RECORD),
        ("MouseEvent", _MOUSE_EVENT_RECORD),
        ("_pad", ctypes.c_byte * 16),
    ]


class _INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _INPUT_UNION)]


def _read_input_records(f, hin, deadline: float) -> None:
    """Read raw INPUT_RECORDs and log KEY vs MOUSE — the definitive probe of what conhost
    actually generates from the input pipe bytes."""
    k32.ReadConsoleInputW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_INPUT_RECORD),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.ReadConsoleInputW.restype = wintypes.BOOL
    k32.GetNumberOfConsoleInputEvents.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    buf = (_INPUT_RECORD * 64)()
    nread = wintypes.DWORD(0)
    while time.monotonic() < deadline:
        navail = wintypes.DWORD(0)
        k32.GetNumberOfConsoleInputEvents(hin, ctypes.byref(navail))
        if not navail.value:
            time.sleep(0.01)
            continue
        if not k32.ReadConsoleInputW(hin, buf, 64, ctypes.byref(nread)):
            break
        quit_ = False
        for i in range(nread.value):
            rec = buf[i]
            if rec.EventType == 0x0001:  # KEY_EVENT
                ke = rec.Event.KeyEvent
                f.write(
                    f"REC KEY down={ke.bKeyDown} vk={ke.wVirtualKeyCode} "
                    f"sc={ke.wVirtualScanCode} char={ke.UnicodeChar!r} cks=0x{ke.dwControlKeyState:x}\n"
                )
                if ke.UnicodeChar == "q":
                    quit_ = True
            elif rec.EventType == 0x0002:  # MOUSE_EVENT
                me = rec.Event.MouseEvent
                f.write(
                    f"REC MOUSE pos=({me.dwMousePosition.X},{me.dwMousePosition.Y}) "
                    f"btn=0x{me.dwButtonState:x} flags=0x{me.dwEventFlags:x} cks=0x{me.dwControlKeyState:x}\n"
                )
            else:
                f.write(f"REC OTHER type=0x{rec.EventType:x}\n")
        f.flush()
        if quit_:
            break


def main() -> None:
    logpath = sys.argv[1]
    modes = set(sys.argv[2].split(",")) if len(sys.argv) > 2 else set()

    hin = k32.GetStdHandle(STD_INPUT_HANDLE)
    old = wintypes.DWORD(0)
    k32.GetConsoleMode(hin, ctypes.byref(old))

    new = ENABLE_EXTENDED_FLAGS  # extended flags lets us clear quick-edit
    if "vt" in modes:
        new |= ENABLE_VIRTUAL_TERMINAL_INPUT
    if "mouseinput" in modes:
        new |= ENABLE_MOUSE_INPUT
    # raw: no line/echo/processed/quickedit
    set_ok = k32.SetConsoleMode(hin, new)
    set_err = ctypes.get_last_error()

    actual = wintypes.DWORD(0)
    k32.GetConsoleMode(hin, ctypes.byref(actual))

    with open(logpath, "w", encoding="utf-8") as f:
        f.write(
            f"hin=0x{(hin or 0):x} set_ok={set_ok} set_err={set_err} "
            f"old_mode=0x{old.value:04x} requested=0x{new:04x} actual=0x{actual.value:04x}\n"
        )
        f.flush()

        # Request mouse / win32-input the way a TUI would: via stdout DEC private modes.
        if "mouse" in modes:
            os.write(1, b"\x1b[?1000h\x1b[?1006h")
        if "win32" in modes:
            os.write(1, b"\x1b[?9001h")
        os.write(1, b"READY\n")

        deadline = time.monotonic() + 8.0
        if "readinput" in modes:
            _read_input_records(f, hin, deadline)
        else:
            while time.monotonic() < deadline:
                try:
                    data = os.read(0, 4096)
                except OSError:
                    break
                if not data:
                    time.sleep(0.01)
                    continue
                f.write(f"recv hex={data.hex()} repr={data!r}\n")
                f.flush()
                if b"q" in data:
                    break
        f.write("DONE\n")

    k32.SetConsoleMode(hin, old)


if __name__ == "__main__":
    main()
