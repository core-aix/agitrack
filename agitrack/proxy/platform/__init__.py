"""Platform abstraction for the proxy's host I/O (issue #118).

The interactive TUI is built on POSIX primitives — a pseudo-terminal for the backend,
``select`` on file descriptors, ``termios`` raw mode. Native Windows provides none of
these, so this package isolates the platform-specific pieces behind contracts
(:mod:`agitrack.proxy.platform.base`) with a POSIX implementation
(:mod:`agitrack.proxy.platform.posix`) and a Windows one
(:mod:`agitrack.proxy.platform.nt`, ConPTY via ``pywinpty`` + the Win32 console API),
selected at runtime by the factories here. The reactor in ``runner.py`` talks only to the
contracts, so it stays platform-agnostic.
"""

from __future__ import annotations

from agitrack.proxy.platform.base import ChildProcess, ReactorWaker

__all__ = ["ChildProcess", "ReactorWaker", "make_child_process", "make_waker"]


def make_child_process(command: list[str], cwd: str, extra_env: dict[str, str] | None = None) -> ChildProcess:
    """Spawn the backend child under a pseudo-terminal for this platform: a POSIX PTY
    (``BackendProcess``) or a Windows ConPTY (``NtChildProcess``). The returned object
    exposes a ``select``-able ``master_fd`` either way, so the reactor is unchanged."""
    import os

    if os.name == "nt":
        from agitrack.proxy.platform.nt import NtChildProcess

        return NtChildProcess.spawn(command, cwd, extra_env)
    from agitrack.proxy.process import BackendProcess

    return BackendProcess.spawn(command, cwd, extra_env)


def make_waker() -> ReactorWaker:
    """A reactor self-wake channel for this platform: a POSIX self-pipe, or a Windows
    socketpair (the only ``select``-able wake primitive there)."""
    import os

    if os.name == "nt":
        from agitrack.proxy.platform.nt import NtWaker

        return NtWaker()
    from agitrack.proxy.platform.posix import PosixWaker

    return PosixWaker()
