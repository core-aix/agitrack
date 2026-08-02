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
import threading
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
        # --- stdin reader thread (see _pump) ---
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._wake_r = self._wake_w = -1

    # ------------------------------------------------------------------
    # stdin reader thread
    # ------------------------------------------------------------------
    #
    # WHY THIS EXISTS. A terminal's input queue is about 1 KB, and when it fills the kernel
    # DISCARDS what will not fit — silently, mid-escape-sequence. The reactor used to read fd 0
    # itself, once per pass, with a repaint and the timer phase in between; a wheel burst
    # outruns that easily and the queue overflows while nobody is reading. Everything that came
    # back as "stray text in the input box" (`<65;43;28M`, `6;52M`, half a paste marker) is a
    # sequence chopped in half by that overflow.
    #
    # Reading harder inside the reactor cannot fix it: one `os.read(fd, 4096)` already empties
    # the whole ~1 KB queue, so looping to EAGAIN just returns EAGAIN (measured: 77% -> 72%
    # byte loss). The loss happens in the window where the reactor is not reading AT ALL. So a
    # thread does nothing but drain the tty into memory, and never waits on a repaint:
    # same burst, 77% -> 0.6% loss. Nothing is truncated, so there is no debris to filter.
    #
    # The buffer is in-process rather than a pipe on purpose: a pipe would fill too (64 KB) and
    # block the reader, putting us straight back to an overflowing tty. The pipe here carries
    # only a wake byte, so `select` still works unchanged for the reactor.
    #
    # This mirrors what Windows has always done (``NtHostTerminal``'s reader thread + socket
    # bridge) — POSIX was the odd one out in reading its console inline.

    _MAX_BUFFER = 1 << 20  # a reactor wedged this long has worse problems than lost keystrokes

    def _start_reader(self) -> None:
        """Begin draining stdin. Called from ``set_raw``: the tty must already be in raw mode,
        or the line discipline would hold input back until Enter. Idempotent."""
        if self._reader is not None and self._reader.is_alive():
            self._paused.clear()  # coming back from cooked mode (a prompt, or a resume)
            return
        if not sys.stdin.isatty():
            return  # --json and other non-TTY paths never start a thread
        try:
            self._wake_r, self._wake_w = os.pipe()
            os.set_blocking(self._wake_r, False)
        except OSError:
            self._wake_r = self._wake_w = -1
            return
        self._stop.clear()
        self._paused.clear()
        self._reader = threading.Thread(target=self._pump, name="agitrack-stdin", daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        import select as _select

        fd = sys.stdin.fileno()
        while not self._stop.is_set():
            if self._paused.is_set():
                # Cooked mode: something else owns the terminal (a prompt, or the shell while
                # we are suspended). Reading here would steal its keystrokes.
                self._stop.wait(0.02)
                continue
            try:
                # Bounded wait rather than a blocking read, so a stop is honoured promptly —
                # there is no portable way to interrupt a thread parked in read(2).
                if not _select.select([fd], [], [], 0.2)[0]:
                    continue
                data = os.read(fd, 65536)
            except (BlockingIOError, InterruptedError):
                continue
            except (OSError, ValueError):
                return  # the terminal went away; the reactor notices by its own means
            if not data:
                return
            with self._lock:
                self._buffer.extend(data)
                if len(self._buffer) > self._MAX_BUFFER:
                    del self._buffer[: -self._MAX_BUFFER]
            try:
                os.write(self._wake_w, b"\x00")  # only a signal; the bytes live in _buffer
            except OSError:
                return

    def _stop_reader(self) -> None:
        self._stop.set()
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.join(timeout=1.0)
        for fd in (self._wake_r, self._wake_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._wake_r = self._wake_w = -1

    def start(self) -> None:
        pass  # the reader starts with raw mode, not before it (see _start_reader)

    def stop(self) -> None:
        self._stop_reader()

    def set_raw(self) -> None:
        import termios

        from agitrack.proxy.terminal import TerminalHost

        self._owner.old_attrs = termios.tcgetattr(sys.stdin.fileno())
        TerminalHost.set_raw(self._owner)
        self._start_reader()  # only once the line discipline is out of the way

    def set_cooked(self) -> None:
        from agitrack.proxy.terminal import TerminalHost

        self._paused.set()  # hand the tty back before anything else reads it
        TerminalHost.set_cooked(self._owner)

    def suspend_host(self) -> None:
        from agitrack.proxy.terminal import TerminalHost

        self._paused.set()  # the SHELL is about to own this terminal; do not read behind it
        TerminalHost.suspend_host(self._owner)

    def resume_host(self) -> None:
        from agitrack.proxy.terminal import TerminalHost

        TerminalHost.resume_host(self._owner)
        self._start_reader()  # raw mode is back, so resume draining

    def restore_terminal(self) -> None:
        from agitrack.proxy.terminal import TerminalHost

        self._paused.set()  # the tty is going back to the shell; stop reading behind it
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

    def _reader_owns_stdin(self) -> bool:
        return self._reader is not None and self._reader.is_alive() and not self._paused.is_set()

    def stdin_fileno(self) -> int:
        """What the reactor selects on: the reader's wake pipe while it owns stdin, else the
        tty itself (before raw mode, and while a prompt or the shell has the terminal)."""
        return self._wake_r if self._reader_owns_stdin() and self._wake_r >= 0 else sys.stdin.fileno()

    def read_stdin(self, length: int) -> bytes:
        """Hand back input the reader thread has already taken off the tty.

        Buffered bytes are returned even once the reader has stopped or paused, so a keystroke
        that arrived just before a prompt opened is not lost."""
        with self._lock:
            if self._buffer:
                data = bytes(self._buffer[:length])
                del self._buffer[:length]
                remaining = bool(self._buffer)
            else:
                data = b""
                remaining = False
        if data:
            # Keep the wake pipe consistent with the buffer: drain it, then re-arm if there is
            # still buffered input, or select would not report the remainder as readable.
            try:
                while os.read(self._wake_r, 4096):
                    pass
            except (BlockingIOError, OSError, ValueError):
                pass
            if remaining:
                try:
                    os.write(self._wake_w, b"\x00")
                except OSError:
                    pass
            return data
        if self._reader_owns_stdin():
            return b""  # the reader owns the tty; reading it here would race with it
        return os.read(sys.stdin.fileno(), length)

    def write_stdout(self, data: bytes) -> None:
        os.write(sys.stdout.fileno(), data)

    def flush_input(self) -> None:
        import termios

        with self._lock:
            self._buffer.clear()  # the reader has already drained the tty; flush that too
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
