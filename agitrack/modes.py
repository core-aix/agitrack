"""The modes aGiTrack runs in, and the menu bare ``agitrack`` uses to pick one.

aGiTrack does several quite different things — it can run the agent itself inside a TUI, sit
behind an agent you drive from its own CLI, or just show you what past sessions did — and for a
long time the only one you got without reading the help was the TUI, because that is what a bare
``agitrack`` happened to start. Nothing on the command line said the others existed.

So the modes are enumerated HERE, once, as data: the menu renders from this table, the help text
quotes it, and every entry carries the exact command it stands for, so the menu doubles as the
documentation for skipping it next time.

Two things the table encodes deliberately:

* **Worktree and no-worktree interactive runs are separate modes**, not a flag on one mode. They
  behave differently enough (isolated branch and an integration step, versus the agent editing
  your working tree live) that collapsing them hides the choice that matters most.
* **Background with auto commits is the default.** It is the mode that asks least of the user:
  the terminal stays yours, the agent stays whatever you already use, and the tracking happens
  anyway.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Mode:
    """One row of the mode menu."""

    name: str
    """Short name, also accepted as typed input."""

    headline: str
    """What it is, in a few words."""

    summary: str
    """What it is FOR — one sentence, shown under the headline."""

    argv: tuple[str, ...]
    """The command line this mode stands for. ``agitrack`` + these arguments goes straight here."""

    @property
    def command(self) -> str:
        return "agitrack " + " ".join(self.argv)


# Order matters: the first entry is what a bare Enter selects.
MODES: tuple[Mode, ...] = (
    Mode(
        name="background",
        headline="background tracking, automatic commits",
        summary=(
            "Keep using your coding agent exactly as you do now, in its own CLI or your IDE. "
            "aGiTrack watches the session from the outside and commits each finished turn, with "
            "the prompts, replies and token counts behind it. No TUI: your terminal stays yours."
        ),
        argv=("-b",),
    ),
    Mode(
        name="background-manual",
        headline="background tracking, commits you make",
        summary=(
            "The same tracking, but nothing lands on your branch until you commit. Each agent "
            "turn is held aside and folded into the commit you make yourself, so you decide what "
            "a commit contains."
        ),
        argv=("-b", "-m"),
    ),
    Mode(
        name="interactive",
        headline="interactive TUI, isolated worktree",
        summary=(
            "Run the agent inside aGiTrack's own TUI. It works in a separate worktree and its "
            "finished turns are merged back, so several sessions can run at once without "
            "standing on each other."
        ),
        argv=("-i",),
    ),
    Mode(
        name="interactive-here",
        headline="interactive TUI, your working tree",
        summary=(
            "The same TUI, but the agent edits your current branch directly: every change is "
            "visible live in your editor. No isolation, so run one session at a time."
        ),
        argv=("-i", "--no-worktree"),
    ),
    Mode(
        name="interactive-manual",
        headline="interactive TUI, commits you make",
        summary=(
            "The TUI on your current branch, with commits left to you: the agent's turns are "
            "recorded aside and folded into the commit you make when you are ready."
        ),
        argv=("-i", "-m"),
    ),
    Mode(
        name="dashboard",
        headline="dashboard",
        summary=(
            "Open this repository's tracking dashboard in your browser: coverage, tokens, models, "
            "who changed what, and the conversation behind each commit."
        ),
        argv=("-d",),
    ),
    Mode(
        name="backtrace",
        headline="backtrace past sessions",
        summary=(
            "Reconstruct what past agent conversations did in this directory from the transcripts "
            "on this machine, even if aGiTrack has never run here."
        ),
        argv=("--backtrace",),
    ),
    Mode(
        name="status",
        headline="status",
        summary="Report what aGiTrack is currently running for this repository, and in which mode.",
        argv=("-s",),
    ),
    Mode(
        name="stop",
        headline="stop",
        summary="Stop whatever aGiTrack is running for this repository, in any mode.",
        argv=("stop",),
    ),
)

DEFAULT_MODE = MODES[0]


def mode_by_name(name: str) -> Mode | None:
    """The mode a typed answer names: its name, its 1-based number, or the flag it stands for."""
    answer = name.strip().lower()
    if not answer:
        return None
    if answer.isdigit():
        index = int(answer) - 1
        return MODES[index] if 0 <= index < len(MODES) else None
    for mode in MODES:
        if answer == mode.name or answer == " ".join(mode.argv) or answer in mode.argv:
            return mode
    return None


# --------------------------------------------------------------------------- rendering

_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_SELECTED = "\x1b[7m"  # reverse video: readable on every theme, unlike any colour we could pick


def _width() -> int:
    try:
        return max(40, min(os.get_terminal_size().columns, 110))
    except OSError:
        return 80


def _wrap(text: str, width: int, indent: str) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=max(20, width - len(indent))) or [""]


def _height() -> int:
    try:
        return max(10, os.get_terminal_size().lines)
    except OSError:
        return 40


def render(selected: int, *, colour: bool = True, compact: bool | None = None) -> list[str]:
    """The menu as a list of lines, with row ``selected`` highlighted.

    Returned rather than printed so the caller can count the lines it has to move back over to
    repaint, and so a test can read the menu without a terminal.

    ``compact`` drops every summary except the selected row's. Decided by the terminal's height
    when not given: the menu repaints in place by moving the cursor back over its own output, and
    a menu taller than the screen has already scrolled by the time it is repainted — the cursor
    lands in the middle of it and each keypress smears a new copy down the terminal."""
    width = _width()

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if colour and code else text

    def rows(short: bool) -> list[str]:
        lines = ["", "aGiTrack runs in several modes. Which one do you want?", ""]
        for index, mode in enumerate(MODES):
            marker = "❯" if index == selected else " "
            label = f" {marker} {index + 1}. {mode.headline}"
            # The command sits on the same line as its mode, right-aligned, so the menu answers
            # "how do I skip this next time?" for every row rather than only the chosen one.
            gap = width - len(label) - len(mode.command) - 1
            head = f"{label}{' ' * max(2, gap)}{mode.command}"
            lines.append(paint(head, _SELECTED if index == selected else ""))
            if short and index != selected:
                continue
            for line in _wrap(mode.summary, width, "        "):
                lines.append(paint(f"       {line}", _DIM))
        lines.append("")
        lines.append(paint("  ↑/↓ to move · Enter to choose · a number to jump · q to quit", _DIM))
        return lines

    if compact is not None:
        return rows(compact)
    full = rows(False)
    return full if len(full) < _height() else rows(True)


# --------------------------------------------------------------------------- key input


class _RawKeys:
    """Single-keypress reads for the duration of the menu, or nothing at all.

    ``ok`` is False when the terminal cannot be put in raw mode (a pipe, a test, an editor's
    pseudo-terminal that refuses); the caller then falls back to a typed answer rather than
    leaving the user in front of a menu whose arrow keys do nothing."""

    def __init__(self) -> None:
        self.ok = False
        self._fd: int | None = None
        # The saved termios attributes, restored on exit. Untyped because termios' own type is
        # platform-specific and this attribute simply does not exist on Windows.
        self._saved: Any = None

    def __enter__(self) -> "_RawKeys":
        if os.name == "nt":
            try:
                import msvcrt  # noqa: F401

                self.ok = True
            except ImportError:  # pragma: no cover - msvcrt is always present on Windows
                self.ok = False
            return self
        try:
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self.ok = True
        except Exception:
            self.ok = False
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._fd is None or self._saved is None:
            return
        try:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        except Exception:
            pass

    def read(self) -> str:
        """The next keypress as a token: ``up``, ``down``, ``enter``, ``quit``, or the character."""
        if os.name == "nt":
            return self._read_windows()
        char = os.read(self._fd, 1) if self._fd is not None else b""  # type: ignore[arg-type]
        if not char:
            return "quit"
        if char in (b"\r", b"\n"):
            return "enter"
        if char in (b"\x03", b"\x04"):  # Ctrl-C / Ctrl-D
            return "quit"
        if char == b"\x1b":
            # An escape sequence (arrow keys) or a bare Escape. cbreak mode delivers the whole
            # sequence in one burst, so anything still pending belongs to this key.
            rest = self._pending()
            if rest.endswith(b"A"):
                return "up"
            if rest.endswith(b"B"):
                return "down"
            return "quit" if not rest else ""
        try:
            return char.decode("utf-8", errors="ignore").lower()
        except Exception:
            return ""

    def _pending(self) -> bytes:
        import select

        out = bytearray()
        while self._fd is not None:
            readable, _, _ = select.select([self._fd], [], [], 0.02)
            if not readable:
                break
            chunk = os.read(self._fd, 8)
            if not chunk:
                break
            out += chunk
        return bytes(out)

    def _read_windows(self) -> str:  # pragma: no cover - exercised on Windows only
        import msvcrt

        char = msvcrt.getch()  # type: ignore[attr-defined]
        if char in (b"\x00", b"\xe0"):  # a function/arrow key: lead byte + scancode
            code = msvcrt.getch()  # type: ignore[attr-defined]
            return {b"H": "up", b"P": "down"}.get(code, "")
        if char in (b"\r", b"\n"):
            return "enter"
        if char in (b"\x03", b"\x1b"):
            return "quit"
        return char.decode("utf-8", errors="ignore").lower()


def choose(*, stream=None) -> Mode | None:
    """Show the menu and return the chosen mode, or None if the user quit.

    Arrow keys move the selection and Enter takes it; a digit jumps straight to that row and
    chooses it, so someone who already knows the answer never waits for a repaint. Where raw
    keys are unavailable the same menu is printed once and answered by typing."""
    out = stream or sys.stdout
    selected = 0
    with _RawKeys() as keys:
        if not keys.ok:
            return _choose_by_typing(out)
        painted = 0
        while True:
            lines = render(selected)
            if painted:
                # Back over the previous rendering and clear to the end of the screen, so the
                # menu updates in place instead of scrolling a new copy past the user.
                out.write(f"\x1b[{painted}A\x1b[J")
            out.write("\n".join(lines) + "\n")
            out.flush()
            painted = len(lines)
            key = keys.read()
            if key == "quit" or key == "q":
                out.write("\n")
                out.flush()
                return None
            if key == "up":
                selected = (selected - 1) % len(MODES)
            elif key == "down":
                selected = (selected + 1) % len(MODES)
            elif key == "enter":
                return MODES[selected]
            elif key.isdigit():
                mode = mode_by_name(key)
                if mode is not None:
                    return mode


# How many unrecognized answers the typed menu will take before giving up. A person who has
# mistyped three times is being told something the prompt is not managing to say; a STREAM that
# answers the same unusable thing three times is not a person at all, and the loop that kept
# asking it forever hung anything driving aGiTrack through a pipe.
_MAX_TYPED_ATTEMPTS = 3


def _choose_by_typing(out) -> Mode | None:
    """The menu without raw keys: printed once, answered with a number or a name."""
    out.write("\n".join(render(0, colour=False)) + "\n")
    out.flush()
    for attempt in range(_MAX_TYPED_ATTEMPTS):
        try:
            answer = input(f"Choose [{DEFAULT_MODE.name}]: ").strip().lower()
        except (EOFError, KeyboardInterrupt, OSError):
            # OSError covers a stdin that cannot be read at all (a closed stream, or pytest's
            # captured stdin): there is no one to ask, so choose nothing rather than raise.
            out.write("\n")
            return None
        if not answer:
            return DEFAULT_MODE
        if answer in {"q", "quit", "exit"}:
            return None
        mode = mode_by_name(answer)
        if mode is not None:
            return mode
        if attempt < _MAX_TYPED_ATTEMPTS - 1:
            out.write("Sorry, I did not recognize that. Type a number, a mode name, or q to quit.\n")
            out.flush()
    out.write("Not a mode I recognize. Run `agitrack --help` to see the options, or `agitrack -i` for the TUI.\n")
    out.flush()
    return None
