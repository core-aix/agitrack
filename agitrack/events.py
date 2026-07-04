"""A user-facing **event log**: an append-only record of the notable things aGiTrack does on
your behalf — an AI change detected, a turn recorded, a commit made, a merge integrated, an
update becoming available — written to a plain-text file you choose (``--log-file`` / the
``log_file`` config key). It works in **every** mode: the interactive proxy TUI *and* the
headless background tracker (with or without ``-b``), so you can ``tail -f`` one file and watch
exactly what aGiTrack is doing.

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
