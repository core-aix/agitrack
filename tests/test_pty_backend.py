"""Unit tests for agitrack.proxy.pty_backend — the cross-platform PTY abstraction.

Two suites, each gated to the right platform:

  posix_only  — exercises _PosixPtyHandle via real pty.fork() children.
  win_only    — exercises _WindowsPtyHandle via ConPTY + pywinpty.

The "common" tests (import, select-ability of master_fd) run on both platforms.
"""

from __future__ import annotations

import os
import select
import sys
import time

import pytest

from agitrack.proxy.pty_backend import spawn_pty

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY only")
win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows ConPTY only")


# ---------------------------------------------------------------------------
# Common — both platforms
# ---------------------------------------------------------------------------


def test_spawn_pty_returns_handle_with_master_fd():
    if sys.platform == "win32":
        handle = spawn_pty(["cmd", "/c", "echo", "ok"], cwd="C:\\")
    else:
        handle = spawn_pty(["/bin/cat"], cwd="/tmp")
    try:
        assert handle.master_fd is not None
        assert isinstance(handle.master_fd, int)
    finally:
        handle.teardown()


def test_master_fd_is_selectable():
    """select.select must accept master_fd without raising on both platforms."""
    if sys.platform == "win32":
        handle = spawn_pty(["cmd", "/c", "echo", "selectable"], cwd="C:\\")
        timeout = 3.0
    else:
        handle = spawn_pty(["/bin/cat"], cwd="/tmp")
        timeout = 0.0  # cat blocks — just check it doesn't raise
    try:
        # Should not raise even if nothing is readable yet.
        readable, _, _ = select.select([handle.master_fd], [], [], timeout)
        # On Windows, cmd /c echo exits quickly and data will be ready.
        # On POSIX with cat, nothing is ready without input — that's fine.
        assert isinstance(readable, list)
    finally:
        handle.teardown()


def test_read_master_returns_bytes():
    """read_master() must return bytes, not raise, on both platforms."""
    if sys.platform == "win32":
        handle = spawn_pty(["cmd", "/c", "echo", "bytes"], cwd="C:\\")
        # Wait for output to arrive.
        readable, _, _ = select.select([handle.master_fd], [], [], 3.0)
    else:
        handle = spawn_pty(["/bin/sh", "-c", "printf hello"], cwd="/tmp")
        time.sleep(0.1)
        readable, _, _ = select.select([handle.master_fd], [], [], 1.0)
    try:
        if readable:
            data = handle.read_master(4096)
            assert isinstance(data, bytes)
    finally:
        handle.teardown()


def test_resize_does_not_raise():
    """resize() must not raise on either platform (even with an active child)."""
    if sys.platform == "win32":
        handle = spawn_pty(["cmd", "/c", "pause"], cwd="C:\\")
    else:
        handle = spawn_pty(["/bin/cat"], cwd="/tmp")
    try:
        handle.resize(30, 120)
        handle.resize(24, 80)
    finally:
        handle.teardown()


def test_teardown_nulls_child_pid_and_master_fd():
    if sys.platform == "win32":
        handle = spawn_pty(["cmd", "/c", "echo", "bye"], cwd="C:\\")
    else:
        handle = spawn_pty(["/bin/cat"], cwd="/tmp")
    handle.teardown()
    assert handle.child_pid is None
    assert handle.master_fd is None


# ---------------------------------------------------------------------------
# POSIX-only: _PosixPtyHandle
# ---------------------------------------------------------------------------


@posix_only
def test_posix_spawn_sets_pid_and_fd():
    handle = spawn_pty(["/bin/cat"], cwd="/tmp")
    try:
        assert handle.child_pid is not None and handle.child_pid > 0
        # master_fd is a real PTY fd on POSIX, not a socket.
        assert handle.master_fd is not None
        stat = os.fstat(handle.master_fd)  # would raise if fd is invalid
        assert stat is not None
    finally:
        handle.teardown()


@posix_only
def test_posix_read_master_returns_echoed_input():
    """Write to the child PTY; PTY echo returns the bytes via read_master."""
    handle = spawn_pty(["/bin/cat"], cwd="/tmp")
    try:
        os.write(handle.master_fd, b"ping\n")
        time.sleep(0.1)
        readable, _, _ = select.select([handle.master_fd], [], [], 2.0)
        assert readable, "timed out waiting for PTY echo"
        data = handle.read_master(4096)
        assert b"ping" in data
    finally:
        handle.teardown()


@posix_only
def test_posix_poll_exited_false_while_alive():
    handle = spawn_pty(["/bin/cat"], cwd="/tmp")
    try:
        assert not handle.poll_exited()
    finally:
        handle.teardown()


@posix_only
def test_posix_poll_exited_true_after_exit():
    handle = spawn_pty(["/bin/sh", "-c", "exit 0"], cwd="/tmp")
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if handle.poll_exited():
                break
            time.sleep(0.05)
        assert handle.poll_exited()
    finally:
        handle.teardown()


@posix_only
def test_posix_terminate_graceful_stops_child():
    handle = spawn_pty(["/bin/cat"], cwd="/tmp")
    pid = handle.child_pid
    assert pid is not None
    handle.terminate_graceful()
    # After terminate_graceful, the child should be gone.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done:
                break
        except ChildProcessError:
            break  # already reaped
        time.sleep(0.05)
    else:
        import signal as _signal

        os.kill(pid, _signal.SIGKILL)
        os.waitpid(pid, 0)
        pytest.fail("terminate_graceful did not stop the child")


@posix_only
def test_posix_extra_env_reaches_child(tmp_path):
    out = tmp_path / "val.txt"
    handle = spawn_pty(
        ["/bin/sh", "-c", f"printf '%s' \"$_AGIT_TEST_VAR\" > {out}"],
        cwd=str(tmp_path),
        extra_env={"_AGIT_TEST_VAR": "posix_ok"},
    )
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if out.exists() and out.stat().st_size > 0:
            break
        time.sleep(0.05)
    handle.teardown()
    assert out.read_text() == "posix_ok"
    assert "_AGIT_TEST_VAR" not in os.environ


# ---------------------------------------------------------------------------
# Windows-only: _WindowsPtyHandle
# ---------------------------------------------------------------------------


@win_only
def test_windows_master_fd_is_socket():
    """master_fd must be a Windows socket handle, selectable via select.select."""
    import socket

    handle = spawn_pty(["cmd", "/c", "echo", "socket-check"], cwd="C:\\")
    try:
        fd = handle.master_fd
        assert fd is not None
        # Creating a socket from the fd verifies it is a socket handle.
        sock = socket.fromfd(fd, socket.AF_INET, socket.SOCK_STREAM)
        sock.detach()  # don't close the underlying fd
    finally:
        handle.teardown()


@win_only
def test_windows_read_master_returns_echo_output():
    """cmd /c echo hello → output appears via read_master within a short timeout.

    pywinpty may emit PTY init escape sequences before the actual command output,
    so we drain all available data until the expected text appears or we time out.
    """
    handle = spawn_pty(["cmd", "/c", "echo", "hello-win"], cwd="C:\\")
    try:
        combined = b""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            readable, _, _ = select.select([handle.master_fd], [], [], 0.5)
            if readable:
                chunk = handle.read_master(4096)
                if chunk:
                    combined += chunk
                if b"hello-win" in combined.lower():
                    break
            elif handle.poll_exited():
                break
        assert b"hello-win" in combined.lower(), f"output not found in: {combined!r}"
    finally:
        handle.teardown()


@win_only
def test_windows_pump_thread_closes_wsock_on_exit():
    """After the child exits the pump closes w_sock, making r_sock return b'' (EOF)."""
    handle = spawn_pty(["cmd", "/c", "echo", "pump-exit"], cwd="C:\\")
    try:
        # Wait for the process to finish and the pump to close w_sock.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if handle.poll_exited():
                break
            time.sleep(0.05)
        assert handle.poll_exited(), "cmd child did not exit"

        # Give the pump thread a moment to close w_sock.
        time.sleep(0.1)

        # Drain all remaining data.
        chunks = []
        while True:
            readable, _, _ = select.select([handle.master_fd], [], [], 1.0)
            if not readable:
                break
            data = handle.read_master(4096)
            if not data:
                break  # EOF — pump closed w_sock
            chunks.append(data)
        output = b"".join(chunks)
        assert b"pump-exit" in output.lower() or b"pump-exit" in output
    finally:
        handle.teardown()


@win_only
def test_windows_poll_exited_false_while_alive():
    """A long-running cmd child reports not-yet-exited."""
    handle = spawn_pty(["cmd", "/c", "timeout", "/t", "10", "/nobreak"], cwd="C:\\")
    try:
        assert not handle.poll_exited()
    finally:
        handle.teardown()


@win_only
def test_windows_poll_exited_true_after_exit():
    handle = spawn_pty(["cmd", "/c", "echo", "quick-exit"], cwd="C:\\")
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if handle.poll_exited():
                break
            time.sleep(0.05)
        assert handle.poll_exited()
    finally:
        handle.teardown()
