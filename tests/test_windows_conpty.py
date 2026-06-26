"""Native-Windows ConPTY child smoke (#118).

Runs only on Windows (the raw-ConPTY path); skipped on POSIX, where the existing
``BackendProcess`` PTY path is exercised instead. Validates that ``NtChildProcess`` spawns a
child under a pseudo-console, bridges its output to a select-able socket, and reports a clean
exit — the building block the whole Windows TUI rests on."""

import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="ConPTY is native-Windows only")


def _interactive_session() -> bool:
    """True if running in an interactive window station (Session 1+), False in the headless
    Session 0 of a CI service runner. ConPTY only routes a child's *own* stdout through the
    pseudoconsole in an interactive session; in Session 0 a fast console child's output leaks
    to the parent console instead. aGiTrack itself only ever runs interactively, so the full
    round-trip is asserted there; the headless runner can still validate the plumbing."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    sid = wintypes.DWORD()
    if not k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(sid)):
        return True  # can't tell — assume interactive and check fully
    return sid.value != 0


def test_ntchildprocess_spawns_reads_and_exits():
    from agitrack.proxy.platform.nt import NtChildProcess

    proc = NtChildProcess.spawn(["cmd", "/c", "echo ci-conpty-ok& exit"], "C:\\")
    buf = b""
    for _ in range(120):
        chunk = proc.drain()
        if chunk:
            buf += chunk
        if proc.poll() is not None:
            chunk = proc.drain()
            if chunk:
                buf += chunk
            break
        time.sleep(0.05)
    proc.teardown()
    # Always: the child spawned under a pseudoconsole, its handshake was bridged to the
    # select-able socket, and it exited cleanly.
    assert buf, "no bytes bridged from the pseudoconsole"
    assert proc.poll() == 0
    # The child's own stdout only round-trips through the pty in an interactive session
    # (see _interactive_session); a headless Session-0 runner routes it to the parent console.
    if _interactive_session():
        assert b"ci-conpty-ok" in buf, repr(buf)


def test_resolve_windows_command_wraps_cmd_scripts(tmp_path):
    # A .cmd backend shim (e.g. npm's claude.cmd) must run through cmd.exe /c — the only way
    # ConPTY's CreateProcess executes a batch file.
    from agitrack.proxy.platform.nt import _resolve_windows_command

    script = tmp_path / "mybackend.cmd"
    script.write_text("@echo off\r\n")
    appname, args = _resolve_windows_command([str(script), "run"])
    assert appname.lower().endswith("cmd.exe")
    assert args[0] == "/c" and str(script) in args
