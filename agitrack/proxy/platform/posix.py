"""POSIX implementations of the proxy host-I/O contracts (issue #118).

These wrap the primitives the reactor has always used directly, behind the
:mod:`agitrack.proxy.platform.base` contracts, so the Windows implementations can stand in
without the reactor noticing. The backend child (:class:`~agitrack.proxy.process.BackendProcess`)
and host terminal (:class:`~agitrack.proxy.terminal.TerminalHost`) already live in their own
modules and satisfy the contracts as-is; only the self-wake channel is wrapped here.
"""

from __future__ import annotations

import os


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
