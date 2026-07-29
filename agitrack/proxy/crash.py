"""Crash reports for the interactive TUI.

When the reactor dies, the terminal is being torn down at the same moment: the traceback
lands on a screen that is switching back out of the alternate buffer, so what the user is
left with is "it just died" and nothing to send anyone. That has already cost several
rounds of guesswork on a scroll-related death nobody could reproduce.

So the runner writes the traceback to ``<repo>/.agitrack/crash-<timestamp>.log`` and tells
the user the path AFTER the terminal is restored. The file is plain text, contains what the
next person actually needs (versions, terminal geometry, backend, what the session was
doing), and never anything from the code or the conversation.
"""

from __future__ import annotations

import platform
import sys
import time
import traceback
from pathlib import Path

# Keep the directory from growing forever: a crash loop should leave evidence, not a mess.
_KEEP_REPORTS = 5


def crash_dir(root: Path) -> Path:
    return Path(root) / ".agitrack"


def write_crash_report(root: Path, error: BaseException, *, context: dict[str, object] | None = None) -> Path | None:
    """Write ``error``'s traceback (plus a little context) next to the repo's aGiTrack state.

    Returns the path, or None when even that failed: a crash report must never itself raise
    on the way out of a session."""
    try:
        directory = crash_dir(root)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = directory / f"crash-{stamp}.log"
        lines = [
            f"aGiTrack crash report {stamp}",
            f"version: {_version()}",
            f"python:  {sys.version.split()[0]} on {platform.platform()}",
        ]
        for key, value in (context or {}).items():
            lines.append(f"{key}: {value}")
        lines.append("")
        lines.append("".join(traceback.format_exception(type(error), error, error.__traceback__)))
        path.write_text("\n".join(lines), encoding="utf-8")
        _prune(directory)
        return path
    except Exception:
        return None


def _version() -> str:
    try:
        from agitrack import __version__

        return str(__version__)
    except Exception:
        return "unknown"


def _prune(directory: Path) -> None:
    reports = sorted(directory.glob("crash-*.log"))
    for stale in reports[:-_KEEP_REPORTS]:
        try:
            stale.unlink()
        except OSError:
            pass


def crash_message(path: Path | None, error: BaseException) -> str:
    """What to tell the user once the terminal is theirs again."""
    what = f"{type(error).__name__}: {error}"
    if path is None:
        return f"aGiTrack stopped unexpectedly ({what})."
    return (
        f"aGiTrack stopped unexpectedly ({what}).\n"
        f"The full details are in {path} - please attach that file when reporting this.\n"
        "Your work is safe: aGiTrack commits each turn as it happens, so nothing was lost."
    )
