"""Cross-platform PTY backend for spawning a child process in a terminal.

POSIX (Linux / macOS): uses the stdlib ``pty`` module, ``os.read``/``os.write``,
and ``fcntl TIOCSWINSZ``.  The master fd returned is a real PTY fd that can be
passed directly to ``select.select``.

Windows: uses ``pywinpty`` (PyPI package ``pywinpty >= 2.0``), which wraps the
Windows ConPTY API.  Because Windows ``select.select`` only supports sockets, a
background thread pumps pywinpty output into one half of a ``socket.socketpair``;
the other half is the exposed ``master_fd`` so the reactor's ``select`` call works
without change.  Write and resize operations go directly to the pywinpty handle,
bypassing the socket.

Factory
-------
``spawn_pty(command, cwd, extra_env, rows, cols)`` → ``_PosixPtyHandle`` or
``_WindowsPtyHandle``; both expose the same attributes and methods used by
``BackendProcess``.
"""

from __future__ import annotations

import os
import sys
import threading
import time


if sys.platform == "win32":
    import socket as _socket

    class _WindowsPtyHandle:
        """PTY handle for Windows: wraps a ``winpty.PtyProcess`` + socket bridge."""

        def __init__(
            self,
            proc,  # winpty.PtyProcess
            r_sock: "_socket.socket",
            w_sock: "_socket.socket",
            pump_thread: threading.Thread,
        ) -> None:
            self._proc = proc
            self._r_sock = r_sock
            self._w_sock = w_sock
            self._pump_thread = pump_thread
            self._write_lock = threading.Lock()
            # The selectable fd for select(); backed by the read end of the socket pair.
            self.master_fd: int | None = r_sock.fileno()
            self.child_pid: int | None = proc.pid

        def write(self, data: bytes) -> None:
            with self._write_lock:
                try:
                    self._proc.write(data.decode("utf-8", errors="replace"))
                except Exception:
                    pass

        def resize(self, rows: int, cols: int) -> None:
            try:
                self._proc.setwinsize(rows, cols)
            except Exception:
                pass

        def send_interrupt(self) -> None:
            try:
                self._proc.sendintr()
            except Exception:
                pass

        def send_signal(self, sig: int) -> None:
            self.send_interrupt()

        def read_master(self, n: int) -> bytes:
            """Read up to *n* bytes from the output socket.

            Returns ``b""`` when the pump has closed ``w_sock`` (EOF / process
            exited) or when no data is immediately available on the non-blocking
            socket.  OSError is silenced so the caller's loop can treat any
            failure as end-of-stream.
            """
            try:
                return self._r_sock.recv(n)
            except OSError:
                return b""

        def close_master(self) -> None:
            fd = self.master_fd
            self.master_fd = None
            if fd is not None:
                try:
                    self._r_sock.close()
                except OSError:
                    pass

        def poll_exited(self) -> bool:
            try:
                return not self._proc.isalive()
            except Exception:
                return True

        def terminate_graceful(self) -> None:
            """Send interrupt, wait up to 1 s, then force-terminate."""
            self.send_interrupt()
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if self.poll_exited():
                    return
                time.sleep(0.05)
            try:
                self._proc.terminate()
            except Exception:
                pass

        def teardown(self) -> None:
            self.terminate_graceful()
            self.close_master()
            self.child_pid = None

    def spawn_pty(
        command: list[str],
        cwd: str,
        extra_env: dict[str, str] | None = None,
        rows: int = 24,
        cols: int = 80,
    ) -> _WindowsPtyHandle:
        """Spawn *command* in a Windows ConPTY; return a selectable handle."""
        try:
            import winpty  # pywinpty >= 2.0
        except ImportError:
            raise RuntimeError(
                "pywinpty is required to run aGiTrack on Windows.\nInstall it with:  pip install pywinpty"
            ) from None

        env = {**os.environ, **(extra_env or {})}
        proc = winpty.PtyProcess.spawn(command, cwd=cwd, env=env, dimensions=(rows, cols))

        # Bridge pywinpty output → socket so select() can watch it.
        r_sock, w_sock = _socket.socketpair()
        r_sock.setblocking(False)

        def _pump() -> None:
            try:
                while proc.isalive():
                    try:
                        data = proc.read(65536)
                    except Exception:
                        break
                    if not data:
                        time.sleep(0.005)
                        continue
                    if isinstance(data, str):
                        data = data.encode("utf-8", errors="replace")
                    try:
                        w_sock.sendall(data)
                    except OSError:
                        break
            finally:
                try:
                    w_sock.close()
                except OSError:
                    pass

        pump_thread = threading.Thread(target=_pump, daemon=True, name="agitrack-pty-pump")
        pump_thread.start()
        return _WindowsPtyHandle(proc, r_sock, w_sock, pump_thread)

else:
    import pty as _pty
    import signal as _signal

    class _PosixPtyHandle:
        """PTY handle for POSIX: wraps a (pid, master_fd) pair from pty.fork()."""

        def __init__(self, pid: int, fd: int) -> None:
            self._pid: int | None = pid
            self._fd: int | None = fd
            self._write_lock = threading.Lock()
            self.child_pid: int | None = pid

        @property
        def master_fd(self) -> int | None:
            return self._fd

        @master_fd.setter
        def master_fd(self, value: int | None) -> None:
            self._fd = value

        def read_master(self, n: int) -> bytes:
            """Read up to *n* bytes from the PTY master fd.  OSError propagates."""
            return os.read(self._fd, n)  # type: ignore[arg-type]

        def write(self, data: bytes) -> None:
            if self._fd is not None:
                with self._write_lock:
                    os.write(self._fd, data)

        def resize(self, rows: int, cols: int) -> None:
            if self._fd is None:
                return
            import fcntl
            import struct
            import termios as _termios

            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._fd, _termios.TIOCSWINSZ, winsize)

        def send_interrupt(self) -> None:
            if self._pid is not None:
                try:
                    os.kill(self._pid, _signal.SIGINT)
                except ProcessLookupError:
                    pass

        def send_signal(self, sig: int) -> None:
            if self._pid is not None:
                try:
                    os.kill(self._pid, sig)
                except ProcessLookupError:
                    pass

        def close_master(self) -> None:
            fd = self._fd
            self._fd = None
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

        def poll_exited(self) -> bool:
            if self._pid is None:
                return True
            try:
                done, _ = os.waitpid(self._pid, os.WNOHANG)
                return done != 0
            except ChildProcessError:
                return True

        def terminate_graceful(self) -> None:
            """SIGINT → wait up to 1 s → SIGTERM."""
            if not self._pid:
                return
            try:
                done, _ = os.waitpid(self._pid, os.WNOHANG)
                if done:
                    return
                os.kill(self._pid, _signal.SIGINT)
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    done, _ = os.waitpid(self._pid, os.WNOHANG)
                    if done:
                        return
                    time.sleep(0.05)
                os.kill(self._pid, _signal.SIGTERM)
            except (ChildProcessError, ProcessLookupError):
                return

        def teardown(self) -> None:
            self.terminate_graceful()
            self.close_master()
            self.child_pid = None

    def spawn_pty(
        command: list[str],
        cwd: str,
        extra_env: dict[str, str] | None = None,
        rows: int = 24,
        cols: int = 80,
    ) -> _PosixPtyHandle:
        """Fork a PTY child, exec *command* inside it, and return a handle."""
        pid, fd = _pty.fork()
        if pid == 0:
            try:
                os.chdir(cwd)
                if extra_env:
                    os.environ.update(extra_env)
                os.execvp(command[0], command)
            except BaseException:
                os._exit(127)
        handle = _PosixPtyHandle(pid, fd)
        if rows != 24 or cols != 80:
            handle.resize(rows, cols)
        return handle
