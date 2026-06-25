"""Cross-platform process primitives (POSIX + native Windows).

aGiTrack spawns detached helpers (the dashboard daemon, the self-update) and
probes/stops processes by pid. The POSIX idioms — ``start_new_session`` to detach,
``os.kill(pid, 0)`` to probe liveness, ``os.kill(pid, SIGTERM)`` to stop — don't all
exist on native Windows, so these helpers select the right mechanism per platform and
keep every call site identical and platform-agnostic.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess

# The one canonical Windows flag for the package. Prefer a plain ``os.name == "nt"`` inline
# for a runtime branch; import THIS constant instead in code whose Windows branch is unit-
# tested on a POSIX host (backends, this module) — a test flips ``_IS_WINDOWS`` directly,
# whereas monkeypatching the global ``os.name`` would also switch ``pathlib`` to WindowsPath
# semantics and break unrelated path handling on the test host.
_IS_WINDOWS = os.name == "nt"

# Windows: a process that is still running reports this as its exit code.
_STILL_ACTIVE = 259


def resolve_subprocess_command(command: list[str]) -> list[str]:
    """Resolve *command* for ``subprocess`` on the current OS.

    On Windows, look the executable up via PATH/PATHEXT (so a bare ``claude`` finds the npm
    ``claude.cmd`` shim — which ``CreateProcess`` would otherwise miss, since it only tries
    ``.exe``) and run a ``.cmd``/``.bat`` through ``cmd.exe /c``, the only way
    ``CreateProcess`` (what ``subprocess`` uses with ``shell=False``) executes a batch file.
    On POSIX, returns *command* unchanged.

    This is the flat-list ``subprocess`` counterpart of the ConPTY launch path's
    ``_resolve_windows_command``: without it, every non-TUI backend invocation (summarizer,
    model listing) raised ``FileNotFoundError`` on Windows, so summarization silently fell
    back to the prompt-led commit message.
    """
    if not _IS_WINDOWS or not command:
        return command
    exe = shutil.which(command[0]) or command[0]
    if exe.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", exe, *command[1:]]
    return [exe, *command[1:]]


def detach_kwargs() -> dict:
    """``subprocess`` keyword args that fully detach a child so it outlives the parent
    and the parent's terminal closing.

    POSIX starts a new session (``setsid``), so a terminal-close ``SIGHUP`` isn't
    delivered to the child. Windows has no sessions; the equivalent is a new process
    group that is detached from the console with no window of its own.
    """
    if _IS_WINDOWS:
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        return {"creationflags": flags}
    return {"start_new_session": True}


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` names a live process.

    POSIX sends signal 0, which probes without delivering anything. Windows opens the
    process and checks it hasn't exited (an absent process can't be opened).
    """
    if _IS_WINDOWS:
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def terminate_pid(pid: int) -> None:
    """Ask process ``pid`` to stop — best effort, never raises.

    POSIX delivers ``SIGTERM`` (aGiTrack's helpers handle it for a clean shutdown).
    Windows has no ``SIGTERM`` delivery, so it calls ``TerminateProcess``.
    """
    if _IS_WINDOWS:
        _windows_terminate(pid)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # Windows-only
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _windows_terminate(pid: int) -> None:
    import ctypes

    process_terminate = 0x0001
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # Windows-only
    handle = kernel32.OpenProcess(process_terminate, False, pid)
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)
