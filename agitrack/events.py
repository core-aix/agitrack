"""A user-facing **event log**: an append-only record of the notable things aGiTrack does on
your behalf — a daemon starting or stopping, an AI change detected, a commit made, an update
becoming available — written to a plain-text file you choose (``--log-file`` / the ``log_file``
config key). It works in **every** mode: the interactive proxy TUI, the headless background
tracker, and the one-shot ``--prompt``/``--json`` shell, so you can ``tail -f`` one file and
watch exactly what aGiTrack is doing.

The set of events above is exhaustive. It deliberately no longer claims a "merge integrated"
line: none was ever emitted, and a promise in ``--help`` that no code keeps is worse than a
missing feature — someone waits for a line that cannot come.

Design mirrors the DEBUG_RAW capture: open+append+close per line so the log survives a hard
kill, and every call is best-effort and **never raises** — writing the log must never break
tracking. Lines are simple and greppable::

    2026-07-04T18:30:00 daemon-start backend=claude mode="auto commits" repo=/path
    2026-07-04T18:30:12 ai-change-detected backend=claude session=abc123
    2026-07-04T18:30:14 commit sha=deadbeef type=agent subject="Add input validation"
    2026-07-04T18:31:02 update-available current=0.1.16 latest=0.2.0
"""

from __future__ import annotations

import threading
import time
from pathlib import Path


def resolve_log_path(spec: str | None, repo_root: Path) -> Path | None:
    """Resolve a ``--log-file`` / ``log_file`` value to an absolute path, or None when unset.

    ``~`` is expanded; a relative path is taken relative to the repo root (so the same config
    value points at the same file regardless of the shell's cwd)."""
    if not spec or not str(spec).strip():
        return None
    path = Path(str(spec)).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path


def exclude_log_file(repo_root: Path, path: Path | None) -> None:
    """Git-ignore the event log when the user pointed it INSIDE the repository.

    The default place to put it is the repo root (a relative ``--log-file events.log`` resolves
    there), and nothing excluded it. In the TUI that left a permanent ``?? events.log``; under
    ``-b`` it was worse — the tracker's own ``git add`` swept the file into the AGENT'S commit,
    so aGiTrack's telemetry about a turn ended up inside the turn, attributed to the AI, in
    permanent history. Confirmed identically on all three backends.

    Best-effort and idempotent; a log outside the repo needs nothing and is left alone."""
    if path is None:
        return
    try:
        relative = path.resolve().relative_to(Path(repo_root).resolve())
    except (OSError, ValueError):
        return  # outside the repo (or unresolvable) ⇒ git never sees it
    from agitrack.config.state import AgitrackState

    AgitrackState(Path(repo_root)).add_local_ignore(relative.as_posix())


class EventLog:
    """Append notable aGiTrack events to a user-chosen file. A disabled log (``path is None``)
    makes every ``emit`` a no-op, so callers never have to branch on whether logging is on."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def emit(self, event: str, **fields: object) -> None:
        """Append one ``<iso-timestamp> <event> [k=v …]`` line. Best-effort: any failure
        (unwritable path, disk full) is swallowed — logging never breaks tracking. Values with
        whitespace are quoted; newlines are flattened so one event is always one line."""
        if self.path is None:
            return
        try:
            parts = [time.strftime("%Y-%m-%dT%H:%M:%S"), event]
            for key, value in fields.items():
                if value is None:
                    continue
                text = str(value).replace("\n", " ").replace("\r", " ").strip()
                if text == "" or " " in text or '"' in text:
                    text = '"' + text.replace('"', "'") + '"'
                parts.append(f"{key}={text}")
            line = " ".join(parts) + "\n"
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except Exception:
            pass  # best-effort; never let a logging failure propagate
