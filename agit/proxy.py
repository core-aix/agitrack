from __future__ import annotations

import os
import pty
import select
import signal
import sys
import termios
import textwrap
import time
import tty

import pyte

from agit.actions import AgitActions
from agit.commit_message import build_agent_commit_message, build_user_commit_message
from agit.git import GitRepo
from agit.opencode_session import export_session, latest_session_id, session_belongs_to_repo, turns_after
from agit.state import AgitState


class ProxyInput:
    COMMANDS = ["help", "status", "stage", "unstaged", "user-commit", "agent", "exit"]

    def __init__(self) -> None:
        self.capturing = False
        self.buffer = bytearray()
        self.selected_index = 0
        self.escape_buffer: bytearray | None = None

    def feed(self, data: bytes) -> tuple[list[bytes], bytes, str | None, bool]:
        forwarded: list[bytes] = []
        command = None
        should_exit = False
        for byte in data:
            char = bytes([byte])
            if char == b"\x03":
                should_exit = True
                break
            if self.capturing:
                if self.escape_buffer is not None:
                    self.escape_buffer.extend(char)
                    sequence = bytes(self.escape_buffer)
                    if sequence in {b"\x1b[A", b"\x1b[B"}:
                        self._move_selection(-1 if sequence == b"\x1b[A" else 1)
                        self.escape_buffer = None
                    elif sequence.startswith(b"\x1b[<"):
                        if sequence[-1:] in {b"M", b"m"}:
                            self.escape_buffer = None
                    elif sequence.startswith(b"\x1b[M"):
                        if len(sequence) >= 6:
                            self.escape_buffer = None
                    elif len(sequence) >= 3:
                        self.escape_buffer = None
                    continue
                if char == b"\x1b":
                    self.escape_buffer = bytearray(char)
                    continue
                if char in {b"\r", b"\n"}:
                    typed = self.buffer.decode(errors="ignore").strip()
                    command = typed or (self.selected() or "")
                    self.buffer.clear()
                    self.capturing = False
                    self.selected_index = 0
                elif char in {b"\x7f", b"\b"}:
                    if self.buffer:
                        self.buffer.pop()
                        self.selected_index = 0
                elif char == b"\t":
                    match = self.selected()
                    if match:
                        self.buffer = bytearray(match.encode())
                        self.selected_index = 0
                else:
                    self.buffer.extend(char)
                    self.selected_index = 0
                continue

            if char == b"\x07":
                self.capturing = True
                self.selected_index = 0
                self.escape_buffer = None
                continue

            forwarded.append(char)
        return forwarded, b"", command, should_exit

    def text(self) -> str:
        return self.buffer.decode(errors="ignore")

    def matches(self) -> list[str]:
        text = self.text().removeprefix(":")
        if not text:
            return self.COMMANDS
        return [command for command in self.COMMANDS if command.startswith(text)] or self.COMMANDS

    def selected(self) -> str | None:
        matches = self.matches()
        if not matches:
            return None
        self.selected_index = min(self.selected_index, len(matches) - 1)
        return matches[self.selected_index]

    def _move_selection(self, delta: int) -> None:
        matches = self.matches()
        if matches:
            self.selected_index = (self.selected_index + delta) % len(matches)

    def best_match(self) -> str | None:
        return next(iter(self.matches()), None)


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
        self.message: str | None = None
        self.message_until = 0.0

    def run(self) -> int:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("Proxy mode requires an interactive terminal. Use --mode json for non-TTY use.")
        self.state.save()
        if self.actions.has_pre_agent_user_changes():
            print("User changes detected before OpenCode starts.")
            self.actions.create_user_commit()
        self._initialize_session_baseline()
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
                if self.input.capturing:
                    self._render()
                if forwarded:
                    if any(chunk in {b"\r", b"\n"} for chunk in forwarded):
                        self._pre_agent_commit_if_needed()
                    os.write(self.master_fd, b"".join(forwarded))
                if command:
                    self._run_command(command)
            self._maybe_agent_commit()
            if self.child_pid is not None:
                done, status = os.waitpid(self.child_pid, os.WNOHANG)
                if done:
                    return os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else 0
        return 0

    def _initialize_session_baseline(self) -> None:
        if not self._should_continue_session():
            self.state.backend_session_id = None
            self.state.last_backend_message_id = None
            return
        session = export_session(self.repo.repo, self.state.backend_session_id)
        if not session:
            self.state.last_backend_message_id = None
            return
        if session.model:
            self.state.model = session.model
        complete = [turn for turn in session.turns if turn.assistant_message_id]
        self.state.last_backend_message_id = complete[-1].assistant_message_id if complete else None
        self.state.clear_trace()

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
        for mode in (b"9", b"1000", b"1001", b"1002", b"1003", b"1004", b"1005", b"1006", b"1007", b"1015", b"1016", b"2004"):
            if b"\x1b[?" + mode + b"h" in output:
                os.write(sys.stdout.fileno(), b"\x1b[?" + mode + b"h")
            if b"\x1b[?" + mode + b"l" in output:
                os.write(sys.stdout.fileno(), b"\x1b[?" + mode + b"l")

    def _render(self) -> None:
        if self.screen is None:
            return
        parts = ["\x1b[0m\x1b[?25l\x1b[H"]
        for line in self.screen.display[: max(self.rows - 1, 1)]:
            # Prefer reliable visibility over partial color fidelity. pyte does
            # not preserve every terminal color/palette mode OpenCode uses, and
            # reconstructing styles cell-by-cell can render text invisible when
            # foreground/background defaults are interpreted differently by the
            # host terminal.
            parts.append("\x1b[0m" + line[: self.cols].ljust(self.cols))
            parts.append("\r\n")
        parts.append(self._status_line())
        if self.input.capturing:
            self._append_command_palette(parts)
        elif self.message and time.monotonic() < self.message_until:
            self._append_message_popup(parts, self.message)
        cursor = self.screen.cursor
        cursor_row = min(cursor.y + 1, max(self.rows - 1, 1))
        cursor_col = min(cursor.x + 1, self.cols)
        parts.append(f"\x1b[{cursor_row};{cursor_col}H\x1b[?25h")
        os.write(sys.stdout.fileno(), "".join(parts).encode())

    def _append_command_palette(self, parts: list[str]) -> None:
        width = min(max(52, self.cols // 2), self.cols - 4)
        row = 2
        col = max(2, (self.cols - width) // 2)
        text = self.input.text()
        matches = self.input.matches()
        selected = self.input.selected()
        lines = [
            "aGiT commands",
            f"> {text}",
            "Up/Down selects. Tab completes. Enter runs. Ctrl-C exits.",
            "",
        ]
        lines.extend(matches[:8])
        self._append_box(parts, row, col, width, lines, highlight=selected)

    def _append_message_popup(self, parts: list[str], message: str) -> None:
        width = min(max(52, self.cols // 2), self.cols - 4)
        row = 2
        col = max(2, (self.cols - width) // 2)
        self._append_box(parts, row, col, width, message.splitlines() or [message])

    def _append_box(self, parts: list[str], row: int, col: int, width: int, lines: list[str], highlight: str | None = None) -> None:
        inner = max(width - 2, 1)
        border_top = "┌" + "─" * inner + "┐"
        border_bottom = "└" + "─" * inner + "┘"
        box_lines = [border_top]
        wrapped_lines: list[str] = []
        for line in lines:
            wrapped_lines.extend(textwrap.wrap(line, width=inner) or [""])
        max_body = max(self.rows - row - 2, 1)
        for line in wrapped_lines[:max_body]:
            content = line[:inner].ljust(inner)
            if highlight and line == highlight:
                box_lines.append("│" + "\x1b[7m" + content + "\x1b[0m" + "│")
            else:
                box_lines.append("│" + content + "│")
        box_lines.append(border_bottom)
        for offset, line in enumerate(box_lines):
            if row + offset >= self.rows:
                break
            parts.append(f"\x1b[{row + offset};{col}H\x1b[0m{line}")

    def _render_row(self, row: int) -> str:
        assert self.screen is not None
        cells = self.screen.buffer.get(row, {})
        rendered = []
        current_style = ""
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
        raw_fg = getattr(cell, "fg", "default")
        raw_bg = getattr(cell, "bg", "default")
        # Avoid invisible text when a TUI emits the same foreground/background.
        if raw_fg == raw_bg and raw_fg != "default":
            raw_fg = "default"
        fg = self._color_code(raw_fg, foreground=True)
        bg = self._color_code(raw_bg, foreground=False)
        if fg and fg != bg:
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
            "yellow": 3,
            "blue": 4,
            "magenta": 5,
            "cyan": 6,
            "white": 7,
            "grey": 7,
            "gray": 7,
        }
        if len(color) == 6 and all(char in "0123456789abcdefABCDEF" for char in color):
            red = int(color[0:2], 16)
            green = int(color[2:4], 16)
            blue = int(color[4:6], 16)
            prefix = "38" if foreground else "48"
            return f"{prefix};2;{red};{green};{blue}"
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
        name, _, arg = command.partition(" ")
        name = name.removeprefix(":")
        if name in {"exit", "quit"}:
            self.running = False
            self._exit_child()
            return

        if name in {"stage", "user-commit"}:
            if name == "stage":
                self._review_untracked_popup(include_declined=True)
                self._set_message("Finished staging review.")
            else:
                created = self._create_user_commit_popup()
                self._set_message("Created <user> commit." if created else "No staged user changes to commit.")
            self._render()
            return

        if name == "status":
            self._set_message(self.repo.status_short() or "Working tree clean")
        elif name == "unstaged":
            declined = self.state.declined_untracked()
            if declined:
                self._set_message("Intentionally unstaged: " + ", ".join(declined))
            else:
                self._set_message("No intentionally unstaged files.")
        elif name == "agent":
            selected = arg.strip() or self._select_popup("Backend Agent", ["opencode"])
            if selected == "opencode":
                self.state.backend = "opencode"
                self._set_message("Backend set to opencode")
            else:
                self._set_message("Only the opencode backend is available.")
        elif name in {"help", ""}:
            self._set_message("Commands: status stage unstaged user-commit agent opencode exit")
        else:
            self._set_message(f"Unknown aGiT command: {name}")
        self._render()

    def _prompt_popup(self, title: str, prompt: str, *, default: str = "") -> str:
        value = default
        escape_buffer: bytearray | None = None
        while True:
            self._set_message(f"{title}\n{prompt}\n> {value}", seconds=60)
            self._render()
            data = os.read(sys.stdin.fileno(), 32)
            for byte in data:
                char = bytes([byte])
                if escape_buffer is not None:
                    escape_buffer.extend(char)
                    if len(escape_buffer) >= 3:
                        escape_buffer = None
                    continue
                if char == b"\x03":
                    self._exit_child()
                    return value
                if char == b"\x1b":
                    escape_buffer = bytearray(char)
                    continue
                if char in {b"\r", b"\n"}:
                    self.message = None
                    return value
                if char in {b"\x7f", b"\b"}:
                    value = value[:-1]
                elif byte >= 32:
                    value += char.decode(errors="ignore")

    def _select_popup(self, title: str, options: list[str]) -> str:
        if not options:
            return ""
        selected = 0
        escape_buffer: bytearray | None = None
        while True:
            lines = [title, "Up/Down selects. Enter confirms.", ""]
            for index, option in enumerate(options):
                prefix = "> " if index == selected else "  "
                lines.append(prefix + option)
            self._set_message("\n".join(lines), seconds=60)
            self._render()
            data = os.read(sys.stdin.fileno(), 32)
            for byte in data:
                char = bytes([byte])
                if escape_buffer is not None:
                    escape_buffer.extend(char)
                    sequence = bytes(escape_buffer)
                    if sequence == b"\x1b[A":
                        selected = (selected - 1) % len(options)
                        escape_buffer = None
                    elif sequence == b"\x1b[B":
                        selected = (selected + 1) % len(options)
                        escape_buffer = None
                    elif len(sequence) >= 3:
                        escape_buffer = None
                    continue
                if char == b"\x03":
                    self._exit_child()
                    return options[selected]
                if char == b"\x1b":
                    escape_buffer = bytearray(char)
                    continue
                if char in {b"\r", b"\n"}:
                    self.message = None
                    return options[selected]

    def _create_user_commit_popup(self) -> bool:
        self.repo.add_tracked()
        self._review_untracked_popup(include_declined=False)
        if not self.repo.has_staged_changes():
            return False
        message = self._prompt_popup("User Commit", "Commit message, or blank for default:")
        self.repo.commit(build_user_commit_message(message=message, agit_session_id=self.state.session_id))
        self.state.clear_trace()
        return True

    def _review_untracked_popup(self, *, include_declined: bool) -> None:
        untracked = self.repo.untracked_files()
        declined = set(self.state.declined_untracked())
        candidates = untracked if include_declined else [path for path in untracked if path not in declined]
        if not candidates:
            return
        answer = self._prompt_popup(
            "Untracked Files",
            "Stage all new files? [y/N]\n" + "\n".join(candidates),
        ).strip().lower()
        if answer in {"y", "yes"}:
            self.repo.stage_paths(candidates)
            self.state.remove_declined(candidates)
        else:
            self.state.add_declined(candidates)

    def _create_agent_commit_from_turns_popup(self, *, turns, backend: str, backend_session_id: str | None, model: str | None, quiet: bool) -> bool:
        if not turns:
            return False
        for turn in turns:
            if turn.user_prompt:
                self.state.append_trace("user", turn.user_prompt)
            if turn.final_response:
                self.state.append_trace("agent", turn.final_response)
            self.state.add_token_usage(turn.tokens)

        self.repo.add_tracked()
        self._review_untracked_popup(include_declined=False)
        if not self.repo.has_staged_changes():
            return False

        latest_prompt = next((turn.user_prompt for turn in reversed(turns) if turn.user_prompt), "OpenCode changes")
        self.repo.commit(
            build_agent_commit_message(
                latest_prompt=latest_prompt,
                trace=self.state.pending_trace(),
                backend=backend,
                backend_session_id=backend_session_id,
                agit_session_id=self.state.session_id,
                model=model or self.state.model,
                token_usage=self.state.pending_token_usage(),
            )
        )
        self.state.clear_trace()
        if not quiet:
            self._set_message("Created <agent> commit.")
        return True

    def _set_message(self, message: str, *, seconds: float = 4.0) -> None:
        self.message = message
        self.message_until = time.monotonic() + seconds

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
        self._commit_available_agent_turns(quiet=True)
        if self.actions.has_pre_agent_user_changes():
            self._set_message("User changes detected before agent runs.")
            self._render()
            self._create_user_commit_popup()

    def _commit_available_agent_turns(self, *, quiet: bool) -> bool:
        session_id = latest_session_id(self.repo.repo) or self.state.backend_session_id
        session = export_session(self.repo.repo, session_id) if session_id else None
        if not session:
            return False
        self.state.backend_session_id = session.session_id or session_id
        if session.model:
            self.state.model = session.model
        turns = turns_after(session, self.state.last_backend_message_id)
        complete_turns = [turn for turn in turns if turn.final_response]
        if not complete_turns:
            return False
        committed = self._create_agent_commit_from_turns_popup(
            turns=complete_turns,
            backend="opencode",
            backend_session_id=self.state.backend_session_id,
            model=session.model or self.state.model,
            quiet=quiet,
        )
        if committed:
            self.state.last_backend_message_id = complete_turns[-1].assistant_message_id
            self.last_status = ""
        return committed

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
        committed = self._commit_available_agent_turns(quiet=not self.verbose)
        if not committed:
            if self.verbose:
                self._render_status("git changes found; no new final response available")
            return
        if self.verbose:
            self._render_status("Created <agent> commit.")
        else:
            self._set_message("Created <agent> commit.")

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
            b"\x1b[?9l\x1b[?1001l\x1b[?1004l\x1b[?1007l\x1b[?1016l\x1b[?2004l"
            b"\x1b[?25h\x1b[?1049l\x1b[0m\r\n",
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
