"""Native-Windows implementations of the proxy host-I/O contracts (issue #118).

Imported only when ``os.name == "nt"``. The backend child (ConPTY via ``pywinpty``) and
the host terminal (the Win32 console API via ``ctypes``) are added here; both bridge their
native handles through a ``socket.socketpair`` so the reactor's ``select`` loop — which on
Windows accepts only sockets — keeps working unchanged. The self-wake channel below uses
the same socketpair trick and is the first piece wired through the platform factory.
"""

from __future__ import annotations

import socket


class NtWaker:
    """Reactor self-wake via a loopback ``socketpair``.

    Windows ``select`` accepts sockets but not pipes/console handles, so the POSIX
    self-pipe can't be used; a socketpair is the equivalent ``select``-able wake channel.
    """

    def __init__(self) -> None:
        self._r, self._w = socket.socketpair()
        self._r.setblocking(False)

    def wake_fileno(self) -> int:
        return self._r.fileno()

    def wake(self) -> None:
        try:
            self._w.sendall(b"\x00")
        except OSError:
            pass

    def drain(self) -> None:
        try:
            while self._r.recv(4096):
                pass
        except (BlockingIOError, OSError):
            pass

    def close(self) -> None:
        for sock in (self._r, self._w):
            try:
                sock.close()
            except OSError:
                pass
