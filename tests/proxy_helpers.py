"""Test helper utilities for ProxyRunner tests (#29, P7).

This module provides ``make_runner`` — the canonical factory for constructing
:class:`~agitrack.proxy.runner.ProxyRunner` instances in unit tests, replacing
the ``ProxyRunner.__new__(ProxyRunner)`` idiom from earlier test files.

Usage::

    from tests.proxy_helpers import make_runner

    def test_something(tmp_path):
        runner = make_runner(state=AgitrackState(tmp_path), repo=FakeRepo())
        runner.agent_in_flight = True
        assert runner.active.agent_in_flight is True
"""

from __future__ import annotations

import contextlib
import os
import pty
import subprocess
import sys
from collections.abc import Iterator

from agitrack.proxy.runner import ProxyRunner


@contextlib.contextmanager
def capture_fd(fd: int | None = None) -> Iterator[list[bytes]]:
    """Capture everything written to a real OS file descriptor (default: stdout).

    aGiTrack writes terminal control bytes straight to ``sys.stdout.fileno()`` with
    ``os.write`` — bypassing Python-level ``sys.stdout`` and therefore pytest's normal
    capture. Redirect that fd to a pipe so terminal-output tests can assert on the exact
    bytes emitted (escape sequences, status line, mode resets). The captured bytes are
    appended to the yielded list once the block exits.

    ``fd`` defaults to ``sys.stdout.fileno()`` *resolved at call time* — important under
    pytest, whose fd-level capture remaps stdout to a temp fd that is not literally 1, so
    a hardcoded 1 would redirect the wrong descriptor and catch nothing.

    The pipe buffer is bounded; these tests emit well under that, so a drain-on-exit is
    enough (no reader thread needed)."""
    if fd is None:
        fd = sys.stdout.fileno()
    out: list[bytes] = []
    read_fd, write_fd = os.pipe()
    saved = os.dup(fd)
    os.dup2(write_fd, fd)
    os.close(write_fd)
    try:
        yield out
    finally:
        os.dup2(saved, fd)
        os.close(saved)
        os.set_blocking(read_fd, False)
        chunks: list[bytes] = []
        try:
            while True:
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except BlockingIOError:
            pass
        os.close(read_fd)
        out.append(b"".join(chunks))


def run_in_pty(argv: list[str]) -> bytes:
    """Run ``argv`` under a real pseudo-terminal and return its combined output bytes.

    This exercises the same OS pty read path the proxy uses for the backend — the part
    of aGiTrack that differs between macOS (BSD) and Linux. The child sees a real TTY on
    stdin/stdout/stderr, so it emits the framed/cursor output a TUI would.

    The parent keeps its own copy of the slave fd open until after draining. On macOS
    (BSD) a pty discards any unread output the moment its *last* slave fd is closed, so a
    short-lived child that writes and exits before the parent reads (e.g. ``printf``)
    would otherwise race: the child's exit closes the only slave reference and the buffered
    output is dropped, intermittently yielding a blank read in CI. Holding the slave open
    across ``wait()`` keeps the output buffered; we then drain the master non-blocking. The
    child's output here is tiny (well under the pty buffer), so waiting first can't
    deadlock on a full buffer."""
    import select

    master, slave = pty.openpty()
    process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    process.wait()  # parent still holds `slave`, so BSD keeps the output buffered
    chunks: list[bytes] = []
    while True:
        readable, _, _ = select.select([master], [], [], 0.5)
        if master not in readable:
            break  # nothing left buffered
        try:
            chunk = os.read(master, 65536)
        except OSError:  # last slave fd gone → EIO/EOF on the master side
            break
        if not chunk:
            break
        chunks.append(chunk)
    os.close(slave)
    os.close(master)
    return b"".join(chunks)


def make_runner(**overrides) -> ProxyRunner:
    """Build a :class:`ProxyRunner` for tests without production dependencies.

    Delegates to :meth:`ProxyRunner.for_testing`; all keyword arguments are
    forwarded as overrides (session-level fields such as ``repo``, ``state``,
    ``backend``, ``master_fd`` are routed to the session; runner-level fields
    such as ``verbose``, ``cols``, ``color_mode``, ``_base_branch`` are set
    directly on the runner).

    Returns a fully-initialized runner whose ``active`` session carries real
    :class:`~agitrack.proxy.session.Session` state.  No filesystem access,
    no TTY, no child process is involved.
    """
    return ProxyRunner.for_testing(**overrides)
