"""Native-Windows implementations of the proxy host-I/O contracts (issue #118).

Imported only when ``os.name == "nt"``. The backend child (ConPTY via ``pywinpty``) and
the host terminal (the Win32 console API via ``ctypes``) are added here; both bridge their
native handles through a ``socket.socketpair`` so the reactor's ``select`` loop — which on
Windows accepts only sockets — keeps working unchanged. The self-wake channel below uses
the same socketpair trick and is the first piece wired through the platform factory.
"""

from __future__ import annotations

import os
import select
import socket
import subprocess
import sys
import threading


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


class NtHostTerminal:
    """Native-Windows host terminal: Win32 console VT raw-mode plus a console→socket stdin
    bridge so the reactor can ``select`` on keyboard input (Windows ``select`` rejects
    console handles). Output is plain ``os.write`` to stdout — VT processing is enabled by
    :class:`~agitrack.proxy.platform._winconsole.RawConsole`, so the existing ANSI render
    bytes work unchanged. Implements the same surface the runner uses on the POSIX
    ``TerminalHost``.
    """

    def __init__(self) -> None:
        from agitrack.proxy.platform import _winconsole

        self._winconsole = _winconsole
        self._console = _winconsole.RawConsole()
        self._rsock, self._wsock = socket.socketpair()
        self._rsock.setblocking(False)
        self._stop = threading.Event()
        self._resize_pending = threading.Event()
        self._last_size: tuple[int, int] | None = None
        self._reader: threading.Thread | None = None
        self._resizer: threading.Thread | None = None

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self._console.leave()
        except Exception:  # noqa: BLE001 - best effort on teardown
            pass
        for sock in (self._rsock, self._wsock):
            try:
                sock.close()
            except OSError:
                pass

    def set_raw(self) -> None:
        self._console.enter()
        if self._reader is None:
            self._reader = threading.Thread(target=self._pump_stdin, name="agitrack-conin-reader", daemon=True)
            self._reader.start()
        if self._resizer is None:
            self._resizer = threading.Thread(target=self._watch_resize, name="agitrack-conin-resize", daemon=True)
            self._resizer.start()

    def set_cooked(self) -> None:
        self._console.leave()

    def restore_terminal(self) -> None:
        self.disable_host_terminal_modes()
        self.set_cooked()
        self.write_stdout(b"\x1b[2J\x1b[H\x1b[?1049l\x1b[0m\r\n")

    def disable_host_terminal_modes(self) -> None:
        # Same reset bytes as the POSIX TerminalHost (no kitty-pop: we never enable it on
        # Windows, where the host wouldn't answer the capability query anyway).
        self.write_stdout(
            b"\x1b[?9l\x1b[?1000l\x1b[?1001l\x1b[?1002l\x1b[?1003l\x1b[?1004l"
            b"\x1b[?1005l\x1b[?1006l\x1b[?1007l\x1b[?1015l\x1b[?1016l\x1b[?2004l"
            b"\x1b[>4;0m\x1b[?25h\x1b[0m"
        )

    def enter_host_screen(self) -> None:
        self.write_stdout(b"\x1b[?1049h\x1b[2J\x1b[H")
        self.enable_host_mouse()

    def enable_host_mouse(self) -> None:
        self.write_stdout(b"\x1b[?1000h\x1b[?1006h")

    def detect_host_terminal(self, debug_fn: object = None) -> None:
        # The Windows console doesn't reliably answer the OSC fg/bg/palette queries the
        # POSIX path caches, and a blocking wait would stall startup — skip it. Colors fall
        # back to the backend's own defaults.
        return None

    def pause_child_ui(self) -> None:
        self.set_cooked()
        self.write_stdout(b"\x1b[0m\r\n")

    def resume_child_ui(self, render_fn: object) -> None:
        self.set_raw()
        if callable(render_fn):
            render_fn()

    def terminal_size(self) -> tuple[int, int]:
        return self._winconsole.terminal_size()

    def stdin_fileno(self) -> int:
        return self._rsock.fileno()

    def read_stdin(self, length: int) -> bytes:
        try:
            return self._rsock.recv(length)
        except (BlockingIOError, OSError):
            return b""

    def write_stdout(self, data: bytes) -> None:
        try:
            os.write(sys.stdout.fileno(), data)
        except OSError:
            pass

    def flush_input(self) -> None:
        try:
            while self._rsock.recv(4096):
                pass
        except (BlockingIOError, OSError):
            pass

    def consume_resize_pending(self) -> bool:
        if self._resize_pending.is_set():
            self._resize_pending.clear()
            return True
        return False

    def _pump_stdin(self) -> None:
        while not self._stop.is_set():
            data = self._winconsole.read_input(4096)
            if not data:
                break
            try:
                self._wsock.sendall(data)
            except OSError:
                break

    def _watch_resize(self) -> None:
        self._last_size = self._winconsole.terminal_size()
        while not self._stop.wait(0.15):
            size = self._winconsole.terminal_size()
            if size != self._last_size:
                self._last_size = size
                self._resize_pending.set()


def _resolve_windows_command(command: list[str]) -> tuple[str, list[str]]:
    """Turn a logical ``[exe, *args]`` into the ``(appname, args)`` ConPTY needs.

    ConPTY's ``CreateProcess`` does NOT search ``PATH``/``PATHEXT`` for the application
    name and cannot run a batch script directly — but Windows backends are routinely on
    PATH only via an extension (``claude.cmd`` from npm, ``opencode.exe``), so:

    * resolve the executable against PATH/PATHEXT with ``which_executable`` — which (unlike
      ``shutil.which``) returns only a real runnable ``.exe``/``.cmd``/``.bat``, not the bare
      extensionless shell script or ``.ps1`` a half-installed npm package may leave (those
      can't be launched and would exit instantly), and
    * for a ``.cmd``/``.bat`` (Claude's npm shim), run it through ``cmd.exe /c`` — the only
      way ``CreateProcess`` will execute a batch file.

    This is what makes BOTH the ``claude`` and ``opencode`` backends launchable on native
    Windows regardless of how their CLI was installed.
    """
    from agitrack.proc import which_executable

    exe = which_executable(command[0]) or command[0]
    rest = command[1:]
    if exe.lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return comspec, ["/c", exe, *rest]
    return exe, rest


def _env_block(extra_env: dict[str, str] | None) -> str | None:
    """The null-separated environment block ``winpty`` expects, or ``None`` to inherit
    aGiTrack's environment unchanged (the common case — no per-child overrides)."""
    if not extra_env:
        return None
    merged = {**os.environ, **extra_env}
    return "".join(f"{key}={value}\0" for key, value in merged.items()) + "\0"


class NtChildProcess:
    """A backend session's child on native Windows: a ConPTY (``pywinpty``) whose output
    is bridged to a ``socketpair`` so the reactor can ``select`` on it exactly as it does
    the POSIX PTY master. Satisfies the :class:`~agitrack.proxy.platform.base.ChildProcess`
    contract; the reactor and session code can't tell it apart from ``BackendProcess``.

    A single daemon reader thread copies ConPTY output into the bridge socket. That gives
    the same backpressure the POSIX PTY does: if the reactor falls behind, the socket
    buffer fills, the reader blocks on ``sendall``, the ConPTY output pipe fills, and the
    backend throttles — never an unbounded in-memory queue.
    """

    def __init__(self, pty: object, child_pid: int | None) -> None:
        self._pty = pty
        self.child_pid = child_pid
        self._rsock, self._wsock = socket.socketpair()
        self._rsock.setblocking(False)
        self._write_lock = threading.Lock()
        self._exit_code: int | None = None
        self._pump_done = False  # set once the reader thread has drained all output + EOF'd
        self._closed = False
        self._reader = threading.Thread(target=self._pump, name="agitrack-conpty-reader", daemon=True)
        self._reader.start()

    @classmethod
    def spawn(cls, command: list[str], cwd: str, extra_env: dict[str, str] | None = None) -> "NtChildProcess":
        # Our own raw-ConPTY driver, NOT pywinpty: pywinpty's ConPTY backend kills every child
        # on launch with STATUS_CONTROL_C_EXIT when the app is PyInstaller-frozen (the MSI
        # build), while the OS's own CreatePseudoConsole works fine frozen. See _conpty.py.
        from agitrack.proxy.platform._conpty import ConPTY

        rows, cols = 24, 80
        pty = ConPTY(cols, rows)
        appname, args = _resolve_windows_command(command)
        cmdline = subprocess.list2cmdline(args) if args else ""
        pty.spawn(appname, cmdline=cmdline, cwd=cwd, env=_env_block(extra_env))
        return cls(pty, pty.pid)

    @property
    def master_fd(self) -> int | None:
        """The bridge socket's fd — the ``select``-able stand-in for the PTY master."""
        if self._closed:
            return None
        return self._rsock.fileno()

    @master_fd.setter
    def master_fd(self, value: int | None) -> None:
        # Teardown nulls master_fd to mark the child gone; mirror that by closing the bridge.
        if value is None:
            self._close_bridge()

    def read_fileno(self) -> int | None:
        return self.master_fd

    def _pump(self) -> None:
        """Copy ConPTY output → bridge socket until the child exits/EOF."""
        try:
            while True:
                try:
                    # pywinpty's PTY.read takes ONLY ``blocking`` (it returns all currently
                    # available ConPTY output as a str); a length arg collides with it.
                    data = self._pty.read(blocking=True)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001 - any winpty read error means the child is gone
                    break
                if not data:
                    break
                payload = data.encode("utf-8", "surrogatepass") if isinstance(data, str) else data
                try:
                    self._wsock.sendall(payload)
                except OSError:
                    break
        finally:
            self._exit_code = self._read_exitstatus()
            self._pump_done = True
            try:
                self._wsock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def _read_exitstatus(self) -> int | None:
        getter = getattr(self._pty, "get_exitstatus", None)
        if getter is None:
            return None
        try:
            return getter()
        except Exception:  # noqa: BLE001 - best effort; treat as unknown
            return None

    def drain(self) -> bytes | None:
        """All currently-available bridged output (bounded), or ``None`` at EOF — the same
        contract as ``BackendProcess.drain``."""
        if self._closed:
            return None
        chunks: list[bytes] = []
        total = 0
        while total < 262_144:
            try:
                data = self._rsock.recv(65536)
            except BlockingIOError:
                break
            except OSError:
                break
            if not data:
                break  # bridge EOF: the reader thread saw the child go
            chunks.append(data)
            total += len(data)
            readable, _, _ = select.select([self._rsock], [], [], 0)
            if self._rsock not in readable:
                break
        if not chunks:
            return None
        return b"".join(chunks)

    def write(self, data: bytes) -> None:
        if self._closed:
            return
        with self._write_lock:
            try:
                self._pty.write(data)  # type: ignore[attr-defined]  # ConPTY.write takes bytes
            except Exception:  # noqa: BLE001 - match os.write's "let the caller decide" is N/A here
                pass

    def resize(self, rows: int, cols: int) -> None:
        if self._closed:
            return
        try:
            self._pty.set_size(cols, rows)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - a failed resize is non-fatal
            pass

    def interrupt(self) -> None:
        # Ctrl-C reaches the ConPTY child as an ETX byte, which it turns into a console
        # Ctrl-C — process-group-safe, unlike GenerateConsoleCtrlEvent.
        self.write(b"\x03")

    def poll(self) -> int | None:
        if self._exit_code is not None:
            return self._exit_code
        # Don't report the child gone off the raw process state: a fast child's trailing
        # output is flushed by the console only after the process dies, so the reader thread
        # keeps draining for a short grace after exit. Wait for it to finish — otherwise a
        # caller that stops reading the moment poll() goes non-None loses that output.
        if self._pump_done:
            return self._exit_code if self._exit_code is not None else -1
        return None

    def cleanup(self) -> None:
        if self._closed:
            return
        self.interrupt()
        # Give the child a moment to exit on the Ctrl-C, then force it.
        import time

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if self.poll() is not None:
                return
            time.sleep(0.05)
        self._terminate_pty()

    def terminate(self) -> None:
        self._terminate_pty()
        self._close_bridge()

    def teardown(self) -> None:
        self.cleanup()
        self._close_bridge()

    def signal_exit(self) -> None:
        self.interrupt()

    def _terminate_pty(self) -> None:
        terminate = getattr(self._pty, "terminate", None)
        if terminate is not None:
            try:
                terminate()
            except Exception:  # noqa: BLE001
                pass

    def _close_bridge(self) -> None:
        if self._closed:
            return
        self._closed = True
        for sock in (self._rsock, self._wsock):
            try:
                sock.close()
            except OSError:
                pass
        self.child_pid = None
