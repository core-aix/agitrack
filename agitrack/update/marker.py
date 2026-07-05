"""A tiny shared **update-available marker** under ``<repo>/.agitrack/update-available.json``.

Installing a newer aGiTrack can't be fully automated (it may need pip/pipx/brew/an MSI), so the
background tracker never updates itself — it just *records* that an update exists, and the places
a user actually looks read that record and remind them:

* the background tracker writes/clears it on its periodic check,
* ``agitrack -b status`` shows it,
* the persistent auto-track pre-commit hook prints it at commit time, and
* the dashboard shows it as a banner.

Dependency-free (json + pathlib) so every reader can import it cheaply, and best-effort — a
missing/corrupt marker simply reads as "no update"."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_MARKER_REL = ".agitrack/update-available.json"


def marker_path(repo_root: Path) -> Path:
    return repo_root / _MARKER_REL


def write_update_marker(repo_root: Path, *, current: str, latest: str, message: str) -> None:
    """Record that ``latest`` is available (best-effort; never raises)."""
    try:
        path = marker_path(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"current": current, "latest": latest, "message": message}), encoding="utf-8")
        tmp.replace(path)  # atomic: a reader never sees a half-written record
    except OSError:
        pass


def read_update_marker(repo_root: Path) -> dict[str, Any] | None:
    """The recorded available-update info, or None when there is no (valid) marker."""
    try:
        data = json.loads(marker_path(repo_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("latest") else None


def clear_update_marker(repo_root: Path) -> None:
    """Remove the marker (best-effort) — e.g. once no update is available anymore."""
    try:
        marker_path(repo_root).unlink()
    except OSError:
        pass


def update_reminder_line(repo_root: Path) -> str | None:
    """A one-line reminder for the recorded update, or None. Shared by every surface so the
    wording stays consistent."""
    info = read_update_marker(repo_root)
    if not info:
        return None
    current, latest = info.get("current", "?"), info.get("latest", "?")
    return f"aGiTrack update available: {current} → {latest} (run `agitrack` and choose 'update', or update via pip/pipx/brew)."
