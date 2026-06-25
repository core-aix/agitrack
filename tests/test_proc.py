"""Cross-platform process primitives (agitrack/proc.py).

The POSIX behaviour is exercised for real on the test host; the Windows branches are
exercised by flipping the platform flag and stubbing the Win32 helpers (so they're
covered on a Linux CI too, without a Windows box)."""

import os
import subprocess
import sys

import agitrack.proc as proc


def test_detach_kwargs_posix_uses_new_session():
    if proc._IS_WINDOWS:  # pragma: no cover - this assertion is for the POSIX test host
        return
    assert proc.detach_kwargs() == {"start_new_session": True}


def test_detach_kwargs_windows_uses_creationflags(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    kwargs = proc.detach_kwargs()
    assert set(kwargs) == {"creationflags"}  # no start_new_session on Windows
    assert "start_new_session" not in kwargs


def test_pid_alive_posix_real():
    if proc._IS_WINDOWS:  # pragma: no cover
        return
    assert proc.pid_alive(os.getpid()) is True
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    assert proc.pid_alive(done.pid) is False


def test_pid_alive_dispatches_to_windows_helper(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    monkeypatch.setattr(proc, "_windows_pid_alive", lambda pid: pid == 123)
    assert proc.pid_alive(123) is True
    assert proc.pid_alive(999) is False


def test_terminate_pid_posix_is_best_effort_for_a_dead_pid():
    if proc._IS_WINDOWS:  # pragma: no cover
        return
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    proc.terminate_pid(done.pid)  # must not raise even though the process is gone


def test_terminate_pid_dispatches_to_windows_helper(monkeypatch):
    seen: list[int] = []
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    monkeypatch.setattr(proc, "_windows_terminate", lambda pid: seen.append(pid))
    proc.terminate_pid(555)
    assert seen == [555]


# --- resolve_subprocess_command (Windows .cmd/.exe resolution, #118) ----------------


def test_resolve_subprocess_command_posix_passthrough(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", False)
    assert proc.resolve_subprocess_command(["claude", "-p", "x"]) == ["claude", "-p", "x"]


def test_resolve_subprocess_command_empty_is_unchanged(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    assert proc.resolve_subprocess_command([]) == []


def test_resolve_subprocess_command_windows_wraps_cmd_shim(monkeypatch):
    # npm installs `claude.cmd`; CreateProcess can't run a batch file, so it must go through
    # cmd.exe /c — otherwise summarization raised FileNotFoundError on Windows.
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    monkeypatch.setattr(proc.shutil, "which", lambda name: r"C:\Users\me\AppData\npm\claude.cmd")
    cmd = proc.resolve_subprocess_command(["claude", "-p", "summarize this"])
    assert cmd[0].lower().endswith("cmd.exe")
    assert cmd[1] == "/c"
    assert r"C:\Users\me\AppData\npm\claude.cmd" in cmd
    assert cmd[-1] == "summarize this"  # args preserved after the shim


def test_resolve_subprocess_command_windows_exe_no_shell(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    monkeypatch.setattr(proc.shutil, "which", lambda name: r"C:\bin\opencode.exe")
    cmd = proc.resolve_subprocess_command(["opencode", "models"])
    assert cmd == [r"C:\bin\opencode.exe", "models"]  # resolved path, run directly


def test_resolve_subprocess_command_windows_unresolved_falls_back(monkeypatch):
    # which() found nothing (backend not on PATH): pass the name through unchanged so the
    # caller still gets its usual FileNotFoundError rather than a surprise.
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    monkeypatch.setattr(proc.shutil, "which", lambda name: None)
    assert proc.resolve_subprocess_command(["claude", "-p", "x"]) == ["claude", "-p", "x"]
