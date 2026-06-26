"""POSIX implementations of the proxy host-I/O contracts (issue #118).

These wrap the primitives the reactor has always used directly, behind the
:mod:`agitrack.proxy.platform.base` contracts, so the Windows implementations can stand in
without the reactor noticing. The backend child (:class:`~agitrack.proxy.process.BackendProcess`)
and host terminal (:class:`~agitrack.proxy.terminal.TerminalHost`) already live in their own
modules and satisfy the contracts as-is; only the self-wake channel is wrapped here.
"""

from __future__ import annotations

import os
import sys
from typing import Any


class PosixHostTerminal:
    """POSIX host terminal: a thin adapter over :class:`~agitrack.proxy.terminal.TerminalHost`.

    ``TerminalHost``'s methods operate on the runner's own attributes (``old_attrs`` and the
    cached ``host_*`` capability responses) via the ``TerminalHostState`` protocol, so this
    adapter just forwards to them with the runner as state — keeping POSIX behavior
    byte-for-byte identical while giving the reactor a single host object whose Windows
    counterpart (``NtHostTerminal``) can stand in. stdin/stdout are the real fds here.
    """

    def __init__(self, owner: Any) -> None:
        self._owner = owner  # the ProxyRunner (holds old_attrs + host_* capability state)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def set_raw(self) -> None:
        import termios

        from agitrack.proxy.terminal import TerminalHost

        self._owner.old_attrs = termios.tcgetattr(sys.stdin.fileno())
        TerminalHost.set_raw(self._owner)

    def set_cooked(self) -> None:
        from agitrack.proxy.terminal import TerminalHost

        TerminalHost.set_cooked(self._owner)

    def restore_terminal(self) -> None:
        from agitrack.proxy.terminal import TerminalHost

        TerminalHost.restore_terminal(self._owner)

    def disable_host_terminal_modes(self) -> None:
        from agitrack.proxy.terminal import TerminalHost

        TerminalHost.disable_host_terminal_modes(self._owner)

    def enter_host_screen(self) -> None:
        from agitrack.proxy.terminal import TerminalHost

        TerminalHost.enter_host_screen(self._owner)

    def detect_host_terminal(self, debug_fn: Any = None) -> None:
        from agitrack.proxy.terminal import TerminalHost

        TerminalHost.detect_host_terminal(self._owner, debug_fn=debug_fn)

    def pause_child_ui(self) -> None:
        from agitrack.proxy.terminal import TerminalHost

        TerminalHost.pause_child_ui(self._owner)

    def resume_child_ui(self, render_fn: Any) -> None:
        from agitrack.proxy.terminal import TerminalHost

        TerminalHost.resume_child_ui(self._owner, render_fn)

    def terminal_size(self) -> tuple[int, int]:
        from agitrack.proxy.terminal import TerminalHost

        return TerminalHost.terminal_size(self._owner)

    def stdin_fileno(self) -> int:
        return sys.stdin.fileno()

    def read_stdin(self, length: int) -> bytes:
        return os.read(sys.stdin.fileno(), length)

    def write_stdout(self, data: bytes) -> None:
        os.write(sys.stdout.fileno(), data)

    def flush_input(self) -> None:
        import termios

        try:
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except (termios.error, OSError, ValueError):
            pass

    def consume_resize_pending(self) -> bool:
        return False  # POSIX learns about resizes via SIGWINCH, handled in the runner


class PosixWaker:
    """Reactor self-wake via a non-blocking ``os.pipe`` — the mechanism the reactor has
    always used. A worker thread writes one byte to break the reactor out of ``select``;
    the reactor drains the pipe when it wakes."""

    def __init__(self) -> None:
        self._r, self._w = os.pipe()
        os.set_blocking(self._r, False)

    def wake_fileno(self) -> int:
        return self._r

    def wake(self) -> None:
        try:
            os.write(self._w, b"\x00")
        except OSError:
            pass

    def drain(self) -> None:
        try:
            while os.read(self._r, 4096):
                pass
        except (BlockingIOError, OSError):
            pass

    def close(self) -> None:
        for fd in (self._r, self._w):
            try:
                os.close(fd)
            except OSError:
                pass
