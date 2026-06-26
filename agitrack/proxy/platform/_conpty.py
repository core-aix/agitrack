"""A self-contained Windows ConPTY (pseudo-console), implemented directly on the Win32 API.

This replaces the ``pywinpty`` dependency. ``pywinpty``/winpty-rs's ConPTY backend works in a
normal Python process but **breaks when the app is frozen by PyInstaller** (the MSI build):
every ConPTY child is killed on launch with ``STATUS_CONTROL_C_EXIT`` (0xC000013A), so no
agent backend can start. The OS's own ``CreatePseudoConsole`` works fine frozen — so we drive
it ourselves. This also drops a compiled native dependency from the bundle.

The public surface deliberately mirrors the bits of ``pywinpty.PTY`` that
``NtChildProcess`` used (``spawn``/``read``/``write``/``set_size``/``isalive``/
``get_exitstatus``/``terminate`` + the ``pid`` attribute) so the wrapper barely changed.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import POINTER, Structure, byref, c_void_p, sizeof, wintypes

# ctypes.WinDLL / WinError / get_last_error exist only on Windows; this module is imported
# only there (lazily, from NtChildProcess.spawn), but mypy analyses it on Linux too.
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
_win_error = ctypes.WinError  # type: ignore[attr-defined]
_last_error = ctypes.get_last_error  # type: ignore[attr-defined]

_STILL_ACTIVE = 259
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016


class _COORD(Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _STARTUPINFOW(Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", c_void_p)]


class _PROCESS_INFORMATION(Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


_k32.CreatePseudoConsole.argtypes = [_COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD, POINTER(wintypes.HANDLE)]
_k32.CreatePseudoConsole.restype = ctypes.c_long
_k32.ResizePseudoConsole.argtypes = [wintypes.HANDLE, _COORD]
_k32.ResizePseudoConsole.restype = ctypes.c_long
_k32.ClosePseudoConsole.argtypes = [wintypes.HANDLE]
_k32.CreatePipe.argtypes = [POINTER(wintypes.HANDLE), POINTER(wintypes.HANDLE), c_void_p, wintypes.DWORD]
_k32.CreatePipe.restype = wintypes.BOOL
_k32.ReadFile.argtypes = [wintypes.HANDLE, c_void_p, wintypes.DWORD, POINTER(wintypes.DWORD), c_void_p]
_k32.ReadFile.restype = wintypes.BOOL
_k32.WriteFile.argtypes = [wintypes.HANDLE, c_void_p, wintypes.DWORD, POINTER(wintypes.DWORD), c_void_p]
_k32.WriteFile.restype = wintypes.BOOL
_k32.PeekNamedPipe.argtypes = [
    wintypes.HANDLE,
    c_void_p,
    wintypes.DWORD,
    POINTER(wintypes.DWORD),
    POINTER(wintypes.DWORD),
    POINTER(wintypes.DWORD),
]
_k32.PeekNamedPipe.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, POINTER(wintypes.DWORD)]
_k32.GetExitCodeProcess.restype = wintypes.BOOL
_k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_k32.InitializeProcThreadAttributeList.argtypes = [c_void_p, wintypes.DWORD, wintypes.DWORD, POINTER(ctypes.c_size_t)]
_k32.UpdateProcThreadAttribute.argtypes = [
    c_void_p,
    wintypes.DWORD,
    ctypes.c_size_t,
    c_void_p,
    ctypes.c_size_t,
    c_void_p,
    c_void_p,
]
_k32.DeleteProcThreadAttributeList.argtypes = [c_void_p]
_k32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    c_void_p,
    c_void_p,
    wintypes.BOOL,
    wintypes.DWORD,
    c_void_p,
    wintypes.LPCWSTR,
    POINTER(_STARTUPINFOW),
    POINTER(_PROCESS_INFORMATION),
]
_k32.CreateProcessW.restype = wintypes.BOOL


def _pipe() -> tuple[wintypes.HANDLE, wintypes.HANDLE]:
    read_end, write_end = wintypes.HANDLE(), wintypes.HANDLE()
    if not _k32.CreatePipe(byref(read_end), byref(write_end), None, 0):
        raise _win_error(_last_error())
    return read_end, write_end


class ConPTY:
    """A pseudo-console plus the process attached to it."""

    def __init__(self, cols: int, rows: int) -> None:
        # The child reads from in_read and writes to out_write; we write to in_write and read
        # from out_read. CreatePseudoConsole takes the child's ends; after spawn we hand them
        # to the console and close our copies (the console owns them).
        self._in_read, self._in_write = _pipe()
        self._out_read, self._out_write = _pipe()
        self._hpc = wintypes.HANDLE()
        hr = _k32.CreatePseudoConsole(_COORD(cols, rows), self._in_read, self._out_write, 0, byref(self._hpc))
        if hr != 0:
            raise _win_error(hr & 0xFFFF)
        self._proc = wintypes.HANDLE()
        self._thread = wintypes.HANDLE()
        self.pid: int | None = None
        self._exit: int | None = None
        self._closed = False

    def spawn(self, appname: str, cmdline: str = "", cwd: str | None = None, env: str | None = None) -> None:
        full = appname if not cmdline else f"{appname} {cmdline}"
        # Attribute list carrying the pseudoconsole handle.
        size = ctypes.c_size_t(0)
        _k32.InitializeProcThreadAttributeList(None, 1, 0, byref(size))
        attr_buf = (ctypes.c_byte * size.value)()
        attr_list = ctypes.cast(attr_buf, c_void_p)
        if not _k32.InitializeProcThreadAttributeList(attr_list, 1, 0, byref(size)):
            raise _win_error(_last_error())
        if not _k32.UpdateProcThreadAttribute(
            attr_list, 0, _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, byref(self._hpc), sizeof(wintypes.HANDLE), None, None
        ):
            raise _win_error(_last_error())

        si = _STARTUPINFOEXW()
        si.StartupInfo.cb = sizeof(_STARTUPINFOEXW)
        si.lpAttributeList = attr_list
        pi = _PROCESS_INFORMATION()
        flags = _EXTENDED_STARTUPINFO_PRESENT
        env_buf = None
        if env:
            env_buf = ctypes.create_unicode_buffer(env)
            flags |= _CREATE_UNICODE_ENVIRONMENT
        cmd_buf = ctypes.create_unicode_buffer(full)
        # bInheritHandles=FALSE: the child must take its std handles from the pseudoconsole
        # (via the attribute below), NOT inherit the parent's — otherwise an interactive
        # backend sees a non-TTY stdout and drops into non-interactive mode.
        ok = _k32.CreateProcessW(
            None,
            cmd_buf,
            None,
            None,
            False,
            flags,
            env_buf,
            cwd,
            ctypes.cast(byref(si), POINTER(_STARTUPINFOW)),
            byref(pi),
        )
        _k32.DeleteProcThreadAttributeList(attr_list)
        # The pseudoconsole now owns the child's pipe ends; drop our copies so reads see EOF
        # once the console is closed.
        _k32.CloseHandle(self._in_read)
        _k32.CloseHandle(self._out_write)
        self._in_read = self._out_write = None  # type: ignore[assignment]
        if not ok:
            raise _win_error(_last_error())
        self._proc, self._thread = pi.hProcess, pi.hThread
        self.pid = int(pi.dwProcessId)

    def read(self, blocking: bool = True) -> bytes:
        """Currently-available child output, or ``b""`` at EOF (the child exited and the pipe
        drained). Polls the output pipe so the reader thread unblocks when the child dies —
        a blocking ``ReadFile`` would hang, since the console keeps the pipe open until close."""
        buf = (ctypes.c_byte * 65536)()
        avail, read = wintypes.DWORD(0), wintypes.DWORD(0)
        while not self._closed:
            if _k32.PeekNamedPipe(self._out_read, None, 0, None, byref(avail), None) and avail.value:
                want = min(len(buf), avail.value)
                if _k32.ReadFile(self._out_read, buf, want, byref(read), None) and read.value:
                    return bytes(buf[: read.value])
                return b""
            if not self.isalive():
                if _k32.PeekNamedPipe(self._out_read, None, 0, None, byref(avail), None) and avail.value:
                    want = min(len(buf), avail.value)
                    if _k32.ReadFile(self._out_read, buf, want, byref(read), None) and read.value:
                        return bytes(buf[: read.value])
                return b""
            if not blocking:
                return b""
            time.sleep(0.005)
        return b""

    def write(self, data: bytes) -> None:
        written = wintypes.DWORD(0)
        cbuf = (ctypes.c_byte * len(data)).from_buffer_copy(data)
        _k32.WriteFile(self._in_write, cbuf, len(data), byref(written), None)

    def set_size(self, cols: int, rows: int) -> None:
        if self._hpc:
            _k32.ResizePseudoConsole(self._hpc, _COORD(cols, rows))

    def isalive(self) -> bool:
        if not self._proc:
            return False
        code = wintypes.DWORD(0)
        if not _k32.GetExitCodeProcess(self._proc, byref(code)):
            return False
        if code.value == _STILL_ACTIVE:
            return True
        self._exit = int(code.value)
        return False

    def get_exitstatus(self) -> int | None:
        if self._exit is not None:
            return self._exit
        self.isalive()
        return self._exit

    def terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._proc:
            _k32.TerminateProcess(self._proc, 1)
        if self._hpc:
            _k32.ClosePseudoConsole(self._hpc)
            self._hpc = wintypes.HANDLE()
        for handle in (self._in_write, self._out_read, self._proc, self._thread):
            if handle:
                _k32.CloseHandle(handle)
        self._in_write = self._out_read = None  # type: ignore[assignment]
