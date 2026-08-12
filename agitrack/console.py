"""Cooked-mode terminal helpers for the questions aGiTrack asks before the TUI starts.

First run walks the user through a series of prompts (install git / the GitHub CLI, pick a
coding agent and install it, choose a summarizer model, sign in to gh, …) with long-running
installers in between. Two things go wrong with a plain ``input()`` there:

* **Stale keypresses answer the NEXT question.** An installer can run for minutes; whatever
  the user pressed while waiting (usually Enter, to check whether it is still alive) sits in
  the terminal's input queue and is handed straight to the following ``input()``, which then
  looks like a question that was skipped. :func:`ask` discards the queue immediately before
  reading, so every answer is a keypress made with the question on screen.
* **A silent installer looks like a hang.** :func:`progress_ticker` prints a periodic
  "still working" line so a quiet download is visibly alive.

Both are best-effort and never raise: a prompt must still work when the process has no real
terminal (a pipe, a test, an editor-hosted run).

It also owns the question those prompts depend on — :func:`stdin_is_interactive` /
:func:`stdout_is_interactive`, "is there a human here at all?". That belongs here rather than at
each call site because the obvious answer, ``sys.stdin.isatty()``, is **wrong on Windows**; see
:func:`_is_windows_nul`.
"""

from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator


def _is_windows_nul(stream: Any) -> bool:
    """Whether *stream* is the Windows ``NUL`` device (or another non-console character
    device), which ``isatty()`` reports as a terminal.

    ``isatty()`` on Windows is ``GetFileType(handle) == FILE_TYPE_CHAR``, and ``NUL`` IS a
    character device — so ``sys.stdin.isatty()`` returns **True** for a process whose stdin is
    ``NUL``, which is exactly how a detached process, a scheduled task, a service, or a plain
    ``agitrack ... < NUL`` gets its stdin. Every "is there a human here to answer?" guard built
    on ``isatty()`` therefore fell open on Windows: `--daemons stop` skipped straight to
    ``input()``, got ``EOFError``, and printed "Cancelled. Nothing was stopped." with exit 0 —
    never naming `--yes`, and telling a script it had succeeded.

    A real console answers ``GetConsoleMode``; ``NUL`` does not. That pair (character device
    but not a console) is the precise discriminator, and it is the ONLY case this rejects — a
    pipe, a file, or a redirected fd is FILE_TYPE_PIPE/DISK and falls straight through to
    ``isatty()``'s own answer. Never raises: any stream that cannot produce a real fd is left
    to ``isatty()``."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if kernel32.GetFileType(ctypes.c_void_p(handle)) != 0x0002:  # FILE_TYPE_CHAR
            return False
        mode = ctypes.c_ulong()
        return not kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode))
    except Exception:
        return False


def stdin_is_interactive() -> bool:
    """Whether there is a human at the keyboard who could answer a prompt.

    Use this — never a bare ``sys.stdin.isatty()`` — wherever the answer decides whether to ask
    a question or refuse; see :func:`_is_windows_nul` for why the bare call is wrong on
    Windows."""
    try:
        if not sys.stdin.isatty():
            return False
    except Exception:
        return False
    return not _is_windows_nul(sys.stdin)


def stdout_is_interactive() -> bool:
    """Whether output would land on a terminal a human is watching. The ``NUL`` caveat in
    :func:`_is_windows_nul` applies to stdout too — a detached Windows process writing to
    ``NUL`` reports a tty and would print prompts nobody can see."""
    try:
        if not sys.stdout.isatty():
            return False
    except Exception:
        return False
    return not _is_windows_nul(sys.stdout)


def drain_terminal_input() -> None:
    """Discard any unread bytes in the terminal's input queue.

    A no-op when stdin isn't a real terminal (or the platform API is unavailable), so it is
    safe to call unconditionally. Best-effort: never raises."""
    if not stdin_is_interactive():
        return
    try:
        import termios  # POSIX

        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        return
    except Exception:
        pass
    try:
        import msvcrt  # Windows

        while msvcrt.kbhit():  # type: ignore[attr-defined]
            msvcrt.getwch()  # type: ignore[attr-defined]
    except Exception:
        pass


def ask(question: str, *, input_fn: Callable[[str], str] = input) -> str:
    """Ask *question* on the terminal, ignoring anything typed before it was shown.

    The default entry point for aGiTrack's pre-TUI prompts. Input typed while an earlier
    step ran (an install, a network check) is dropped first — see the module docstring —
    so a stray Enter can't answer this question for the user."""
    drain_terminal_input()
    return input_fn(question)


@contextmanager
def progress_ticker(
    message: str,
    *,
    output_fn: Callable[[str], None] = print,
    interval: float = 15.0,
    enabled: bool | None = None,
) -> Iterator[None]:
    """Print ``message`` with the elapsed time every *interval* seconds inside the block.

    For steps that can be silent for minutes (downloading and installing an agent CLI):
    without it the terminal looks frozen and users start pressing keys. Only ticks on an
    interactive stdout by default — piped/redirected output stays clean — and the ticking
    thread is a daemon, so it can never hold up an exit."""
    if enabled is None:
        enabled = stdout_is_interactive()
    if not enabled:
        yield
        return
    done = threading.Event()
    started = time.monotonic()

    def tick() -> None:
        while not done.wait(interval):
            elapsed = int(time.monotonic() - started)
            try:
                output_fn(f"  … {message} ({elapsed}s elapsed)")
            except Exception:
                return

    thread = threading.Thread(target=tick, name="agitrack-progress", daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
