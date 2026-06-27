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


# --- console_isolation_kwargs (keep child subprocesses off the host console) --------


def test_console_isolation_kwargs_windows_detaches_stdin_and_hides_console(monkeypatch):
    # On Windows a captured child must not inherit our console (it would reset raw mode and
    # make input echo as escape codes) — give it its own hidden console and a detached stdin.
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    kwargs = proc.console_isolation_kwargs()
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert "creationflags" in kwargs  # CREATE_NO_WINDOW → child gets its own console, not ours


def test_console_isolation_kwargs_windows_keeps_stdin_when_feeding_input(monkeypatch):
    # When the caller feeds the child via input=, subprocess already pipes stdin; passing our
    # own stdin= too would be a conflict, so detach_stdin=False omits it (creationflags stay).
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    kwargs = proc.console_isolation_kwargs(detach_stdin=False)
    assert "stdin" not in kwargs
    assert "creationflags" in kwargs


def test_console_isolation_kwargs_posix_only_detaches_stdin(monkeypatch):
    # POSIX has no console coupling, so there are no creationflags — just the harmless stdin
    # detach (which also stops a TTY-probing CLI from hanging the menu thread).
    monkeypatch.setattr(proc, "_IS_WINDOWS", False)
    assert proc.console_isolation_kwargs() == {"stdin": subprocess.DEVNULL}
    assert proc.console_isolation_kwargs(detach_stdin=False) == {}


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


# --- which_executable: Windows-correct executable lookup (#half-installed npm shims) -------


def test_which_executable_posix_is_plain_which(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", False)
    monkeypatch.setattr(proc.shutil, "which", lambda name: "/usr/bin/" + name)
    assert proc.which_executable("claude") == "/usr/bin/claude"


def test_which_executable_windows_finds_cmd_shim(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    # Only claude.cmd exists (the proper npm shim); .exe does not.
    monkeypatch.setattr(proc.shutil, "which", lambda name: r"C:\npm\claude.cmd" if name == "claude.cmd" else None)
    assert proc.which_executable("claude") == r"C:\npm\claude.cmd"


def test_which_executable_windows_rejects_extensionless_and_ps1(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    # A half-installed npm package: bare 'claude' (shell script) and claude.ps1 exist, but
    # no .exe/.cmd/.bat — raw shutil.which would return the bare file, which_executable must not.
    present = {"claude": r"C:\npm\claude", "claude.ps1": r"C:\npm\claude.ps1"}
    monkeypatch.setattr(proc.shutil, "which", lambda name: present.get(name))
    assert proc.which_executable("claude") is None


def test_which_executable_windows_honours_explicit_extension(monkeypatch):
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    monkeypatch.setattr(proc.shutil, "which", lambda name: r"C:\bin\opencode.exe" if name == "opencode.exe" else None)
    assert proc.which_executable("opencode.exe") == r"C:\bin\opencode.exe"
