"""BackendProcess: child-process / PTY lifecycle for the proxy backend (#29, P2).

Owns the fork/exec mechanics, PTY drain, window-size ioctl, signal-based
teardown, and all writes to the child's PTY.  Policy decisions (command
construction, sandbox wrapping, session selection) stay in ProxyRunner.

Cross-platform
--------------
On POSIX the child PTY is a real Unix master fd; on Windows it is the read
end of a ``socket.socketpair`` bridged from ``pywinpty`` by a pump thread
(see :mod:`agitrack.proxy.pty_backend`).  Both are selectable by
``select.select`` and readable via ``os.read``, so the rest of runner.py
is unchanged.  Write and resize go through the pty handle directly (not the
socket) so data flows correctly on both platforms.

Ownership note (P3)
-------------------
Each proxy :class:`~agitrack.proxy.session.Session` owns one BackendProcess
instance (``session.process``) for its whole lifetime.  ``child_pid`` and
``master_fd`` remain addressable as plain fields — on the Session via
properties over its process, and on the runner via the P3 compat properties
that delegate to the active session — so tests built via
``ProxyRunner.__new__`` that set/read these fields directly keep working.
"""

from __future__ import annotations

import os
import select
import signal
import sys
import threading
import time

from agitrack.proxy.pty_backend import PtyHandle, spawn_pty

if sys.platform != "win32":
    # Only needed for the POSIX cleanup path (os.waitpid with WNOHANG).
    pass  # all POSIX-specific imports are inside the methods that need them


class BackendProcess:
    """Owns PTY/child-process mechanics for one backend session.

    Parameters
    ----------
    master_fd:
        The master end of the PTY (or the socket bridge read-end on Windows).
        ``None`` means the process has not been spawned yet (or has been torn
        down).
    child_pid:
        PID of the child process.  ``None`` when not running.
    """

    # Cap on input queued for a child that is not reading. A backend this far behind has
    # already lost the thread of its input; the alternative is growing without bound while
    # the user keeps scrolling. Generous enough that ordinary typing and pastes never trip it.
    MAX_PENDING_INPUT = 1 << 20

    # Class-level defaults, because `spawn` builds instances with `__new__` and sets fields by
    # hand rather than going through `__init__`. Both are rebound per instance on first use, so
    # nothing is shared; declaring them here just means no construction path can miss them.
    _pending = b""
    _nonblocking = False

    def __init__(self, master_fd: int | None = None, child_pid: int | None = None) -> None:
        self.master_fd = master_fd
        self.child_pid = child_pid
        self._write_lock = threading.Lock()
        # Input the child's PTY would not take yet (see `write`). Flushed by the reactor.
        self._pending = b""
        self._nonblocking = False  # set by _ensure_nonblocking on first read/write
        # Pty handle (set by spawn); None for manually-constructed instances.
        self._handle: PtyHandle | None = None

    # ------------------------------------------------------------------
    # Spawn
    # ------------------------------------------------------------------

    @classmethod
    def spawn(cls, command: list[str], cwd: str, extra_env: dict[str, str] | None = None) -> "BackendProcess":
        """Spawn *command* in a PTY and return a new BackendProcess.

        On POSIX uses ``pty.fork()``; on Windows uses pywinpty + a socket
        bridge so the resulting ``master_fd`` is selectable by
        ``select.select``.

        The child changes to *cwd* before exec.  If exec fails the child exits
        with code 127 so the fork never silently propagates as a duplicate
        runner.

        ``extra_env`` is applied to the CHILD's environment only (set after
        the fork, before exec), so it never leaks into the aGiTrack process
        or its own subprocesses.
        """
        handle = spawn_pty(command, cwd, extra_env)
        inst = cls.__new__(cls)
        inst._handle = handle
        inst.master_fd = handle.master_fd
        inst.child_pid = handle.child_pid
        inst._write_lock = threading.Lock()
        return inst

    # ------------------------------------------------------------------
    # Drain
    # ------------------------------------------------------------------

    def read_fileno(self) -> int | None:
        """The ``select``-able fd carrying the child's output — the PTY master here.

        Part of the cross-platform ``ChildProcess`` contract (see
        ``agitrack/proxy/platform/base.py``): the reactor selects on this fd. On Windows
        the implementation returns a socket fd bridged from the ConPTY instead, so the
        same ``select`` loop works unchanged.
        """
        return self.master_fd

    def interrupt(self) -> None:
        """Forward a Ctrl-C (SIGINT) to the child — the programmatic interrupt used by
        teardown. POSIX sends the signal; the Windows impl writes an ETX byte into the
        ConPTY, which it translates to a console Ctrl-C for the child."""
        if self.child_pid:
            try:
                os.kill(self.child_pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    def poll(self) -> int | None:
        """The child's exit code if it has exited, else ``None`` — without blocking.

        Replaces direct ``os.waitpid(WNOHANG)`` use in the reactor so the exit check is
        platform-agnostic (Windows has no zombie reaping; it queries the ConPTY child).
        Reaps the zombie on POSIX when the child has exited.
        """
        if not self.child_pid:
            return None
        try:
            done, status = os.waitpid(self.child_pid, os.WNOHANG)
        except ChildProcessError:
            return None  # already reaped elsewhere
        except OSError:
            return None
        if done == 0:
            return None
        return os.waitstatus_to_exitcode(status)

    def drain(self) -> bytes | None:
        """Read all currently-available output from the PTY (bounded).

        Returns the concatenated bytes, ``b""`` when nothing is available right now, or
        ``None`` on EOF / read error with nothing buffered — ``None`` is the caller's signal
        that the child is GONE, so "nothing to read yet" must never be reported as ``None``.
        That distinction matters because the master fd is non-blocking (see
        :meth:`_ensure_nonblocking`): a read with no data raises ``BlockingIOError`` rather
        than returning empty, and treating that as EOF would fake a backend exit.

        On POSIX reads from the PTY master fd via ``os.read``.  On Windows reads
        via ``handle.read_master`` which calls ``socket.recv`` on the socket
        bridge (``os.read`` does not work on Windows socket handles).
        """
        if self.master_fd is None:
            return None
        self._ensure_nonblocking()
        chunks: list[bytes] = []
        total = 0
        gone = False
        while total < 262_144:
            try:
                if self._handle is not None:
                    data = self._handle.read_master(65536)
                else:
                    data = os.read(self.master_fd, 65536)
            except BlockingIOError:
                break  # drained for now — NOT end of stream
            except OSError:
                gone = True  # EIO/EBADF: the slave side is closed, the child has exited
                break
            if not data:
                gone = True
                break
            chunks.append(data)
            total += len(data)
            try:
                readable, _, _ = select.select([self.master_fd], [], [], 0)
            except OSError:
                break  # Windows: pipe fds aren't selectable; stop after first read
            if self.master_fd not in readable:
                break
        if chunks:
            return b"".join(chunks)
        return None if gone else b""

    def _ensure_nonblocking(self) -> None:
        """Put the PTY master in non-blocking mode, once.

        Both directions need it. Writing: ``select`` reporting the master writable only means
        SOME room, so a blocking ``os.write`` of the next chunk can still park the single
        reactor thread until the child reads — and a child that has stopped reading never
        will, which is the deadlock this whole path exists to prevent. Reading: ``drain`` is
        always guarded by ``select``, so it loses nothing by not blocking.

        Windows keeps its own semantics: ``master_fd`` there is a socket the bridge already
        set non-blocking, and ``os.set_blocking`` does not apply to it.
        """
        if self._nonblocking or self.master_fd is None or sys.platform == "win32":
            return
        try:
            os.set_blocking(self.master_fd, False)
        except OSError:
            pass
        self._nonblocking = True

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write(self, data: bytes) -> None:
        """Queue *data* for the child's PTY and push as much of it as the child will take.

        This MUST NOT block. A PTY's input queue is tiny (1022 bytes on macOS) and the
        reactor is single-threaded: a plain blocking ``os.write`` here stops aGiTrack draining
        the child's OUTPUT, so the child fills its output queue and blocks in ``write`` too,
        and neither side can ever move again. Reproduced with a backend that owns the mouse
        (OpenCode enables mouse reporting, so every wheel event is forwarded to it): 255 wheel
        events was enough to wedge the reactor permanently on ``os_write``, screen frozen on
        the last frame, no crash report, nothing to do but kill it.

        A short write is the same hazard in miniature — the PTY accepted 2 bytes of a 15-byte
        report in testing — and the old code ignored the return value, so the rest was silently
        dropped and the backend saw a mouse report chopped in half.

        Anything the child will not take now stays in ``_pending`` for :meth:`flush_input`.

        On Windows the pywinpty handle owns its own buffering, so that path is unchanged.
        ``OSError`` propagates; call sites handle it as appropriate.
        """
        if self.master_fd is None:
            return
        with self._write_lock:
            if self._handle is not None and sys.platform == "win32":
                self._handle.write(data)
                return
            self._pending += data
            if len(self._pending) > self.MAX_PENDING_INPUT:
                # The child has stopped reading entirely. Keep the most RECENT input: the
                # oldest is stalest, and dropping is better than unbounded growth.
                self._pending = self._pending[-self.MAX_PENDING_INPUT :]
            self._flush_locked()

    def flush_input(self) -> bool:
        """Push queued input the child could not take earlier. Returns True while more is
        owed, so the reactor knows to keep the fast poll and try again."""
        if self.master_fd is None:
            return False
        with self._write_lock:
            if not self._pending:
                return False
            self._flush_locked()
            return bool(self._pending)

    def pending_input(self) -> int:
        """Bytes queued for the child that it has not accepted yet (diagnostics/reactor)."""
        return len(self._pending)

    def _flush_locked(self) -> None:
        """Push as much of ``_pending`` as the PTY takes right now. Caller holds the lock."""
        assert self.master_fd is not None
        self._ensure_nonblocking()
        while self._pending:
            try:
                written = os.write(self.master_fd, self._pending)
            except BlockingIOError:
                return  # the child's input queue is full — try again next reactor pass
            except OSError:
                self._pending = b""  # the child is gone; nothing left to deliver it to
                raise
            if not written:
                return
            self._pending = self._pending[written:]

    # ------------------------------------------------------------------
    # Resize (PTY ioctl only)
    # ------------------------------------------------------------------

    def resize(self, rows: int, cols: int) -> None:
        """Resize the child PTY.

        On POSIX sends ``TIOCSWINSZ`` via ``fcntl.ioctl``.  On Windows calls
        the pywinpty ``setwinsize`` method.  ``OSError`` propagates so the
        caller can skip follow-up work (e.g. a repaint) when the operation
        failed.
        """
        if self.master_fd is None:
            return
        if self._handle is not None:
            self._handle.resize(rows, cols)
        elif sys.platform != "win32":
            import fcntl
            import struct
            import termios

            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def terminate(self) -> None:
        """Send interrupt to the child and close the master fd.

        Callers that need to wait for the child to exit should pass the pid to
        ``_note_pid_for_reaping`` / ``_reap_stopped_children`` on the runner.
        This method fires the interrupt, closes the fd, and nulls the local
        references; the runner clears its own ``child_pid`` / ``master_fd``
        after calling this.
        """
        if self._handle is not None:
            self._handle.send_interrupt()
            self._handle.close_master()
        elif self.child_pid:
            try:
                os.kill(self.child_pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            if self.master_fd is not None:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
        self.master_fd = None
        self.child_pid = None

    def cleanup(self) -> None:
        """Interrupt → wait up to 1 s → terminate the child process.

        This is the graceful shutdown used by ``_cleanup_child`` on exit/signal.
        Does nothing if no child is running.
        """
        if self._handle is not None:
            self._handle.terminate_graceful()
            return
        if not self.child_pid:
            return
        if sys.platform == "win32":
            try:
                os.kill(self.child_pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            return
        try:
            done, _status = os.waitpid(self.child_pid, os.WNOHANG)
            if done:
                return
            os.kill(self.child_pid, signal.SIGINT)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                done, _status = os.waitpid(self.child_pid, os.WNOHANG)
                if done:
                    return
                time.sleep(0.05)
            os.kill(self.child_pid, signal.SIGTERM)
        except (ChildProcessError, ProcessLookupError):
            return

    def teardown(self) -> None:
        """``cleanup()`` then close the master fd and null out both fields."""
        if self._handle is not None:
            self._handle.teardown()
            self.master_fd = None
            self.child_pid = None
            return
        self.cleanup()
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        self.child_pid = None

    def signal_exit(self) -> None:
        """Send interrupt to the child without waiting (used by ``_exit_child``).

        Does not close the master fd -- the caller handles that in the ``run``
        ``finally`` block so it always runs even if the child is already gone.
        """
        if self._handle is not None:
            self._handle.send_interrupt()
            return
        if self.child_pid:
            try:
                os.kill(self.child_pid, signal.SIGINT)
            except ProcessLookupError:
                pass
