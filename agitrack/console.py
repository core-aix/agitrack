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
"""

from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator


def drain_terminal_input() -> None:
    """Discard any unread bytes in the terminal's input queue.

    A no-op when stdin isn't a real terminal (or the platform API is unavailable), so it is
    safe to call unconditionally. Best-effort: never raises."""
    try:
        if not sys.stdin.isatty():
            return
    except Exception:
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
        try:
            enabled = bool(sys.stdout.isatty())
        except Exception:
            enabled = False
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
