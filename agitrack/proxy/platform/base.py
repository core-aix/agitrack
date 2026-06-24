"""Platform abstraction contracts for the proxy's host I/O (issue #118).

The interactive TUI multiplexes three I/O sources with ``select``: the host terminal's
stdin, each backend child's output, and a self-wake channel. POSIX exposes all three as
file descriptors directly. Native Windows can't ``select`` on console / pipe / ConPTY
handles, so its implementations (:mod:`agitrack.proxy.platform.nt`) bridge each source
through a ``socket.socketpair`` — a reader thread copies bytes from the native handle into
a socket the reactor *can* ``select`` on. These Protocols are the contract that both the
POSIX (:mod:`agitrack.proxy.platform.posix`) and Windows implementations satisfy, so the
reactor and session code stay platform-agnostic.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ChildProcess(Protocol):
    """One backend session's child process: a POSIX PTY child, or a Windows ConPTY child.

    ``master_fd`` is the ``select``-able fd carrying the child's output — the real PTY
    master on POSIX, a bridge socket on Windows — so the reactor's ``select`` loop and its
    ``drain()`` reads are identical on both. ``child_pid`` is the OS pid (or ``None`` when
    not running). The lifecycle methods mirror today's ``BackendProcess``.
    """

    master_fd: int | None
    child_pid: int | None

    def read_fileno(self) -> int | None: ...
    def drain(self) -> bytes | None: ...
    def write(self, data: bytes) -> None: ...
    def resize(self, rows: int, cols: int) -> None: ...
    def interrupt(self) -> None: ...
    def terminate(self) -> None: ...
    def cleanup(self) -> None: ...
    def teardown(self) -> None: ...
    def signal_exit(self) -> None: ...
    def poll(self) -> int | None: ...


@runtime_checkable
class ReactorWaker(Protocol):
    """A ``select``-able channel worker threads use to wake the reactor on demand.

    POSIX backs it with a self-pipe (``os.pipe``); Windows with a ``socketpair`` (the only
    ``select``-able wake primitive there). ``wake_fileno()`` is what the reactor adds to
    its ``select`` set; ``wake()`` is what a worker calls to break the reactor out of a
    blocking ``select``; ``drain()`` clears the pending wake byte(s).
    """

    def wake_fileno(self) -> int: ...
    def wake(self) -> None: ...
    def drain(self) -> None: ...
    def close(self) -> None: ...
