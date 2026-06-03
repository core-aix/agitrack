from __future__ import annotations

import os
import pty
import select
import signal
import sys
import termios
import time
import tty

import pyte

from agit.actions import AgitActions
from agit.git import GitRepo
from agit.opencode_session import export_session, latest_session_id, session_belongs_to_repo, turns_after
from agit.state import AgitState


class ProxyInput:
    def __init__(self) -> None:
        self.capturing = False
        self.buffer = bytearray()

    def feed(self, data: bytes) -> tuple[list[bytes], bytes, str | None, bool]:
        forwarded: list[bytes] = []
        local_echo = bytearray()
        command = None
        should_exit = False
        for byte in data:
            char = bytes([byte])
            if char == b"\x03":
                should_exit = True
                break
            if self.capturing:
                if char in {b"\r", b"\n"}:
                    command = self.buffer.decode(errors="ignore").strip()
                    self.buffer.clear()
                    self.capturing = False
                    local_echo.extend(b"\r\n")
                elif char in {b"\x7f", b"\b"}:
                    if self.buffer:
                        self.buffer.pop()
                        local_echo.extend(b"\b \b")
                else:
                    self.buffer.extend(char)
                    local_echo.extend(char)
                continue

            if char == b"\x07":
                self.capturing = True
                local_echo.extend(b"\r\n[aGiT] ")
                continue

            forwarded.append(char)
        return forwarded, bytes(local_echo), command, should_exit


class ProxyRunner:
    def __init__(self, repo: GitRepo, *, verbose: bool = False) -> None:
        self.repo = repo
        self.state = AgitState(repo.repo)
        self.actions = AgitActions(repo, self.state, verbose=verbose)
        self.verbose = verbose
        self.input = ProxyInput()
        self.child_pid: int | None = None
        self.master_fd: int | None = None
        self.last_poll = 0.0
        self.running = True
        self.old_attrs = None
        self.original_sigwinch = None
        self.rows = 24
        self.cols = 80
        self.screen: pyte.Screen | None = None
        self.stream: pyte.ByteStream | None = None
        self.last_child_output = 0.0
        self.last_status = ""
        self.last_status_change = 0.0

    def run(self) -> int:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("Proxy mode requires an interactive terminal. Use --mode json for non-TTY use.")
        self.state.save()
        if self.actions.has_pre_agent_user_changes():
            print("User changes detected before OpenCode starts.")
            self.actions.create_user_commit()
        self._init_screen()
        self._spawn()
        self.old_attrs = termios.tcgetattr(sys.stdin.fileno())
        try:
            self._enter_host_screen()
            self._set_raw()
            self._resize_child()
            self.original_sigwinch = signal.getsignal(signal.SIGWINCH)
            signal.signal(signal.SIGWINCH, lambda _signum, _frame: self._resize_child())
            return self._loop()
        finally:
            if self.original_sigwinch is not None:
                signal.signal(signal.SIGWINCH, self.original_sigwinch)
            self._cleanup_child()
            self._restore_terminal()
            if self.master_fd is not None:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass

    def _spawn(self) -> None:
        command = ["opencode"]
        if self.state.model:
            command.extend(["--model", self.state.model])
        if self._should_continue_session():
            command.extend(["--session", self.state.backend_session_id])
        command.append(str(self.repo.repo))
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.repo.repo)
            os.execvp(command[0], command)
        self.child_pid = pid
        self.master_fd = fd

    def _should_continue_session(self) -> bool:
        session_id = self.state.backend_session_id
        if not session_id:
            return False
        if self.state.backend_session_matches_repo():
            return True
        return session_belongs_to_repo(self.repo.repo, session_id)

    def _loop(self) -> int:
        assert self.master_fd is not None
        while self.running:
            readable, _, _ = select.select([sys.stdin.fileno(), self.master_fd], [], [], 0.2)
            if self.master_fd in readable:
                try:
                    output = os.read(self.master_fd, 4096)
                except OSError:
                    break
                if not output:
                    break
                self.last_child_output = time.monotonic()
                self._sync_terminal_modes(output)
                self._feed_child_output(output)
                self._render()
            if sys.stdin.fileno() in readable:
                data = os.read(sys.stdin.fileno(), 4096)
                forwarded, local_echo, command, should_exit = self.input.feed(data)
                if should_exit:
                    self._exit_child()
                    break
                if local_echo:
                    self._render_status(local_echo.decode(errors="ignore"))
                if forwarded:
                    os.write(self.master_fd, b"".join(forwarded))
                if command:
                    self._run_command(command)
            self._maybe_agent_commit()
            if self.child_pid is not None:
                done, status = os.waitpid(self.child_pid, os.WNOHANG)
                if done:
                    return os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else 0
        return 0

    def _init_screen(self) -> None:
        self.rows, self.cols = self._terminal_size()
        self.screen = pyte.Screen(self.cols, max(self.rows - 1, 1))
        self.stream = pyte.ByteStream(self.screen)

    def _feed_child_output(self, output: bytes) -> None:
        if self.stream is not None:
            self.stream.feed(output)

    def _sync_terminal_modes(self, output: bytes) -> None:
        # OpenCode enables mouse reporting on its PTY. Because aGiT renders the
        # screen itself, the host terminal never sees those mode switches unless
        # we mirror them explicitly.
        for mode in (b"1000", b"1002", b"1003", b"1005", b"1006", b"1015", b"2004"):
            if b"\x1b[?" + mode + b"h" in output:
                os.write(sys.stdout.fileno(), b"\x1b[?" + mode + b"h")
            if b"\x1b[?" + mode + b"l" in output:
                os.write(sys.stdout.fileno(), b"\x1b[?" + mode + b"l")

    def _render(self) -> None:
        if self.screen is None:
            return
        parts = ["\x1b[?25l\x1b[H"]
        for row in range(max(self.rows - 1, 1)):
            parts.append(self._render_row(row))
            parts.append("\r\n")
        parts.append(self._status_line())
        cursor = self.screen.cursor
        cursor_row = min(cursor.y + 1, max(self.rows - 1, 1))
        cursor_col = min(cursor.x + 1, self.cols)
        parts.append(f"\x1b[{cursor_row};{cursor_col}H\x1b[?25h")
        os.write(sys.stdout.fileno(), "".join(parts).encode())

    def _render_row(self, row: int) -> str:
        assert self.screen is not None
        cells = self.screen.buffer.get(row, {})
        rendered = []
        current_style = "\x1b[0m"
        for col in range(self.cols):
            cell = cells.get(col)
            if cell is None:
                style = "\x1b[0m"
                char = " "
            else:
                style = self._cell_style(cell)
                char = cell.data or " "
            if style != current_style:
                rendered.append(style)
                current_style = style
            rendered.append(char)
        rendered.append("\x1b[0m")
        return "".join(rendered)

    def _cell_style(self, cell) -> str:
        codes = []
        if getattr(cell, "bold", False):
            codes.append("1")
        if getattr(cell, "italics", False):
            codes.append("3")
        if getattr(cell, "underscore", False):
            codes.append("4")
        if getattr(cell, "reverse", False):
            codes.append("7")
        fg = self._color_code(getattr(cell, "fg", "default"), foreground=True)
        bg = self._color_code(getattr(cell, "bg", "default"), foreground=False)
        if fg:
            codes.append(fg)
        if bg:
            codes.append(bg)
        return "\x1b[" + (";".join(codes) if codes else "0") + "m"

    def _color_code(self, color: str, *, foreground: bool) -> str | None:
        if color in {"default", ""}:
            return None
        base = 30 if foreground else 40
        bright_base = 90 if foreground else 100
        colors = {
            "black": 0,
            "red": 1,
            "green": 2,
            "brown": 3,
            "blue": 4,
            "magenta": 5,
            "cyan": 6,
            "white": 7,
        }
        if color.startswith("bright"):
            key = color.removeprefix("bright")
            return str(bright_base + colors[key]) if key in colors else None
        return str(base + colors[color]) if color in colors else None

    def _status_line(self) -> str:
        declined = len([path for path in self.state.declined_untracked() if (self.repo.repo / path).exists()])
        left = " aGiT Ctrl-G commands | OpenCode passthrough "
        right = f" unstaged:{declined} " if declined else ""
        padding = " " * max(self.cols - len(left) - len(right), 0)
        return f"\x1b[7m{left}{padding}{right}\x1b[0m"

    def _render_status(self, text: str) -> None:
        prompt = text.replace("\r", "").replace("\n", "")
        line = f" aGiT> {prompt}"[: self.cols].ljust(self.cols)
        os.write(sys.stdout.fileno(), f"\x1b[{self.rows};1H\x1b[7m{line}\x1b[0m".encode())

    def _enter_host_screen(self) -> None:
        os.write(sys.stdout.fileno(), b"\x1b[?1049h\x1b[2J\x1b[H")

    def _run_command(self, command: str) -> None:
        self._pause_child_ui()
        name, _, arg = command.partition(" ")
        name = name.removeprefix(":")
        if name in {"exit", "quit"}:
            self.running = False
            self._exit_child()
        elif name == "status":
            print(self.repo.status_short() or "Working tree clean")
        elif name == "unstaged":
            declined = self.state.declined_untracked()
            if declined:
                print("Intentionally unstaged files:")
                for path in declined:
                    print(f"  {path}")
            else:
                print("No intentionally unstaged files.")
        elif name == "stage":
            self.actions.review_untracked(include_declined=True)
        elif name == "user-commit":
            self.actions.create_user_commit()
        elif name == "model":
            self.state.model = arg.strip() or None
            print(f"Model set to {self.state.model or 'backend default'}; restart aGiT to relaunch OpenCode with it.")
        elif name == "agent":
            if arg.strip() == "opencode":
                self.state.backend = "opencode"
                print("Backend set to opencode")
            else:
                print("Only the opencode backend is available.")
        elif name in {"help", ""}:
            print("aGiT commands: status  stage  unstaged  user-commit  model <model>  agent opencode  exit")
        else:
            print(f"Unknown aGiT command: {name}")
        self._resume_child_ui()

    def _exit_child(self) -> None:
        self.running = False
        if self.child_pid:
            try:
                os.kill(self.child_pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    def _cleanup_child(self) -> None:
        if not self.child_pid:
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
        except ChildProcessError:
            return
        except ProcessLookupError:
            return

    def _pre_agent_commit_if_needed(self) -> None:
        if self.actions.has_pre_agent_user_changes():
            self._pause_child_ui()
            print("User changes detected before agent runs.")
            self.actions.create_user_commit()
            self._resume_child_ui()

    def _maybe_agent_commit(self) -> None:
        now = time.monotonic()
        if now - self.last_poll < 2.0:
            return
        self.last_poll = now
        status = self.repo.status_short()
        if status != self.last_status:
            self.last_status = status
            self.last_status_change = now
        if not status.strip():
            if self.verbose:
                self._render_status("no git changes")
            return
        if now - self.last_status_change < 3.0 or now - self.last_child_output < 2.0:
            if self.verbose:
                self._render_status("git changes found; waiting for OpenCode to become idle")
            return
        session_id = latest_session_id(self.repo.repo) or self.state.backend_session_id
        session = export_session(self.repo.repo, session_id) if session_id else None
        if not session:
            if self.verbose:
                self._render_status(f"git changes found; could not export session {session_id}")
            return
        self.state.backend_session_id = session.session_id or session_id
        if session.model:
            self.state.model = session.model
        turns = turns_after(session, self.state.last_backend_message_id)
        complete_turns = [turn for turn in turns if turn.final_response]
        if not complete_turns:
            if self.verbose:
                self._render_status(f"git changes found; session {session_id} has no new final response")
            return
        self._pause_child_ui()
        try:
            committed = self.actions.create_agent_commit_from_turns(
                turns=complete_turns,
                backend="opencode",
                backend_session_id=self.state.backend_session_id,
                model=session.model or self.state.model,
                quiet=not self.verbose,
            )
            if committed:
                self.state.last_backend_message_id = complete_turns[-1].assistant_message_id
                self.last_status = ""
                if self.verbose:
                    print("Created <agent> commit.")
        finally:
            self._resume_child_ui()

    def _pause_child_ui(self) -> None:
        self._set_cooked()
        os.write(sys.stdout.fileno(), b"\x1b[0m\r\n")

    def _resume_child_ui(self) -> None:
        self._set_raw()
        self._render()

    def _set_raw(self) -> None:
        tty.setraw(sys.stdin.fileno())

    def _set_cooked(self) -> None:
        if self.old_attrs is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_attrs)

    def _restore_terminal(self) -> None:
        self._set_cooked()
        # Reset modes commonly enabled by full-screen TUIs: mouse tracking,
        # bracketed paste, alternate screen, cursor visibility, and styling.
        os.write(
            sys.stdout.fileno(),
            b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1005l\x1b[?1006l\x1b[?1015l"
            b"\x1b[?2004l\x1b[?25h\x1b[?1049l\x1b[0m\r\n",
        )

    def _resize_child(self) -> None:
        if self.master_fd is None:
            return
        try:
            import fcntl
            import struct

            self.rows, self.cols = self._terminal_size()
            if self.screen is not None:
                self.screen.resize(max(self.rows - 1, 1), self.cols)
            winsize = struct.pack("HHHH", max(self.rows - 1, 1), self.cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            self._render()
        except OSError:
            pass

    def _terminal_size(self) -> tuple[int, int]:
        try:
            size = os.get_terminal_size(sys.stdout.fileno())
            return size.lines, size.columns
        except OSError:
            return 24, 80
