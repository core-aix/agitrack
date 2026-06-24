"""Native-Windows ConPTY child smoke (#118).

Runs only on Windows (the ConPTY/pywinpty path); skipped on POSIX, where the existing
``BackendProcess`` PTY path is exercised instead. Validates that ``NtChildProcess`` spawns a
child under a pseudo-console, bridges its output to a select-able socket, and reports a clean
exit — the building block the whole Windows TUI rests on."""

import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="ConPTY is native-Windows only")


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
    assert b"ci-conpty-ok" in buf, repr(buf)
    assert proc.poll() == 0


def test_resolve_windows_command_wraps_cmd_scripts(tmp_path):
    # A .cmd backend shim (e.g. npm's claude.cmd) must run through cmd.exe /c — the only way
    # ConPTY's CreateProcess executes a batch file.
    from agitrack.proxy.platform.nt import _resolve_windows_command

    script = tmp_path / "mybackend.cmd"
    script.write_text("@echo off\r\n")
    appname, args = _resolve_windows_command([str(script), "run"])
    assert appname.lower().endswith("cmd.exe")
    assert args[0] == "/c" and str(script) in args
