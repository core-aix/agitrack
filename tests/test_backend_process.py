"""Direct unit tests for BackendProcess (#29, P2).

These tests construct BackendProcess directly (no ProxyRunner.__new__) and
exercise spawn / write / drain / terminate / reap and the exec-failure guard.
"""

from __future__ import annotations

import os
import signal
import sys
import time

import pytest

from agitrack.proxy.process import BackendProcess

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY only")
win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows ConPTY only")


# ---------------------------------------------------------------------------
# Drain
# ---------------------------------------------------------------------------


def test_drain_reads_all_available():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"hello ")
        os.write(write_fd, b"world")
        proc = BackendProcess(master_fd=read_fd)
        assert proc.drain() == b"hello world"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_drain_returns_none_on_eof():
    read_fd, write_fd = os.pipe()
    os.close(write_fd)  # EOF, nothing buffered
    try:
        proc = BackendProcess(master_fd=read_fd)
        assert proc.drain() is None
    finally:
        os.close(read_fd)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def test_write_sends_bytes_to_fd():
    read_fd, write_fd = os.pipe()
    try:
        proc = BackendProcess(master_fd=write_fd)
        proc.write(b"ping")
        assert os.read(read_fd, 16) == b"ping"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_write_propagates_oserror_on_closed_fd():
    # OSError must propagate: call sites have differing error policies (abort
    # the operation, unwind the loop, or swallow) and implement them themselves,
    # exactly as they did around the original bare os.write calls.
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)
    proc = BackendProcess(master_fd=write_fd)
    with pytest.raises(OSError):
        proc.write(b"must raise")


def test_write_is_noop_when_master_fd_is_none():
    proc = BackendProcess(master_fd=None)
    proc.write(b"noop")  # must not raise


# ---------------------------------------------------------------------------
# Spawn + terminate + reap
# ---------------------------------------------------------------------------


@posix_only
def test_spawn_trivial_child_and_terminate():
    """/bin/cat as a trivial child: spawn, write, drain, terminate."""
    proc = BackendProcess.spawn(["/bin/cat"], cwd="/tmp")
    try:
        assert proc.child_pid is not None and proc.child_pid > 0
        assert proc.master_fd is not None

        proc.write(b"hello\n")
        # Give the child a moment to echo.
        time.sleep(0.1)
        output = proc.drain()
        # Cat echoes input (PTY mode), so we must have received something.
        assert output is not None and len(output) > 0

        # terminate() signals and closes but does NOT reap (matching the
        # original _terminate_child); reap here so the test leaves no zombie.
        pid = proc.child_pid
        proc.terminate()
        assert proc.child_pid is None
        assert proc.master_fd is None
        deadline = time.monotonic() + 3.0
        done = 0
        while time.monotonic() < deadline:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            time.sleep(0.05)
        if not done:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    finally:
        if proc.master_fd is not None:
            os.close(proc.master_fd)


@posix_only
def test_cleanup_terminates_child_via_signal_escalation():
    # Under a non-interactive shell the child inherits SIGINT=SIG_IGN (POSIX
    # background-job semantics), so this exercises the SIGTERM escalation path.
    # cleanup() preserves the original semantics verbatim: it reaps only a child
    # that dies inside the 1s SIGINT window and does NOT waitpid after SIGTERM.
    # The contract under test: after cleanup() returns, the child terminates on
    # its own (the test sends no signal of its own).
    proc = BackendProcess.spawn(["/bin/cat"], cwd="/tmp")
    pid = proc.child_pid
    try:
        proc.cleanup()
        deadline = time.monotonic() + 3.0
        done = 0
        try:
            while time.monotonic() < deadline:
                done, _ = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
                time.sleep(0.05)
        except ChildProcessError:
            done = pid  # cleanup reaped it inside the SIGINT window
        if not done:
            os.kill(pid, signal.SIGKILL)  # don't leave a stray cat behind
            os.waitpid(pid, 0)
        assert done, "cleanup() did not terminate the child (escalation lost)"
    finally:
        if proc.master_fd is not None:
            os.close(proc.master_fd)


@posix_only
def test_spawn_applies_extra_env_to_child_only(tmp_path):
    """extra_env reaches the forked child's environment but never the aGiTrack process."""
    out = tmp_path / "env.txt"
    proc = BackendProcess.spawn(
        ["/bin/sh", "-c", f"printf '%s' \"$OPENCODE_DISABLE_AUTOUPDATE\" > {out}"],
        cwd="/tmp",
        extra_env={"OPENCODE_DISABLE_AUTOUPDATE": "1"},
    )
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not (out.exists() and out.stat().st_size > 0):
        time.sleep(0.05)
    if proc.master_fd is not None:
        os.close(proc.master_fd)
    assert out.read_text() == "1"  # the child saw the injected var
    assert "OPENCODE_DISABLE_AUTOUPDATE" not in os.environ  # parent env untouched


@posix_only
def test_spawn_exec_failure_guard_exits_127():
    """A non-existent command must exit with code 127, not leave a duplicate runner."""
    proc = BackendProcess.spawn(["/nonexistent-command-agit-test"], cwd="/tmp")
    pid = proc.child_pid
    # The child should exit almost immediately with code 127.
    deadline = time.monotonic() + 3.0
    exit_code = None
    while time.monotonic() < deadline:
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            exit_code = 127  # already reaped elsewhere — treat as success
            break
        if done:
            if hasattr(os, "waitstatus_to_exitcode"):
                exit_code = os.waitstatus_to_exitcode(status)
            else:
                exit_code = (status >> 8) & 0xFF
            break
        time.sleep(0.05)
    if proc.master_fd is not None:
        os.close(proc.master_fd)
    assert exit_code == 127, f"expected exit code 127, got {exit_code}"


@posix_only
def test_teardown_clears_fd_and_pid():
    proc = BackendProcess.spawn(["/bin/cat"], cwd="/tmp")
    assert proc.child_pid is not None
    assert proc.master_fd is not None
    proc.teardown()
    assert proc.child_pid is None
    assert proc.master_fd is None


# ---------------------------------------------------------------------------
# Resize (ioctl)
# ---------------------------------------------------------------------------


def test_resize_is_noop_when_master_fd_is_none():
    proc = BackendProcess(master_fd=None)
    proc.resize(24, 80)  # must not raise


# ---------------------------------------------------------------------------
# Re-export from agitrack.proxy
# ---------------------------------------------------------------------------


def test_backendprocess_importable_from_agitrack_proxy():
    from agitrack.proxy import BackendProcess as BP  # noqa: F401

    assert BP is BackendProcess


# ---------------------------------------------------------------------------
# Windows ConPTY spawn (cmd.exe)
# ---------------------------------------------------------------------------


@win_only
def test_spawn_cmd_child_on_windows():
    """Spawn cmd /c echo hello via ConPTY; drain output; teardown cleans up.

    pywinpty may emit PTY init escape sequences before the actual command output,
    so we drain in a loop until "hello" appears or we time out.
    """
    import select

    proc = BackendProcess.spawn(["cmd", "/c", "echo", "hello"], cwd="C:\\")
    try:
        assert proc.child_pid is not None
        assert proc.master_fd is not None

        # master_fd is a socket fd on Windows — verify select works on it.
        combined = b""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            readable, _, _ = select.select([proc.master_fd], [], [], 0.5)
            if readable:
                chunk = proc.drain()
                if chunk:
                    combined += chunk
                if b"hello" in combined.lower():
                    break
            elif proc._handle is not None and proc._handle.poll_exited():
                break
        assert b"hello" in combined.lower(), f"output not found in: {combined!r}"
    finally:
        proc.teardown()
    assert proc.master_fd is None
    assert proc.child_pid is None


@win_only
def test_spawn_extra_env_reaches_cmd_child(tmp_path):
    """extra_env is visible inside the ConPTY child and not leaked to the parent."""
    import select

    marker = "AGITRACK_WIN_TEST_VAR"
    out_file = str(tmp_path / "env.txt")
    proc = BackendProcess.spawn(
        ["cmd", "/c", f"echo %{marker}% > {out_file}"],
        cwd=str(tmp_path),
        extra_env={marker: "win32_ok"},
    )
    try:
        # Wait for the child to exit (it's a one-shot command).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if proc._handle is not None and proc._handle.poll_exited():
                break
            time.sleep(0.1)
    finally:
        proc.teardown()
    result = (tmp_path / "env.txt").read_text(encoding="utf-8", errors="replace").strip()
    assert "win32_ok" in result
    assert marker not in os.environ
