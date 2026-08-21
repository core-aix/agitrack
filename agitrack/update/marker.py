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
import time
from pathlib import Path
from typing import Any

_MARKER_REL = ".agitrack/update-available.json"

# How long a recorded check is worth repeating. A repository is only re-checked while a tracker
# runs ON IT, so a project nobody has tracked since keeps its last answer for as long as the file
# survives: the dashboard was still repeating a six-week-old comparison of two commits nobody had
# been running for a month. A day is far longer than the check interval of any live tracker
# (`update_check_seconds`, 5 minutes), so a repository in use never loses its marker to this.
_MARKER_MAX_AGE_SECONDS = 24 * 60 * 60


def marker_path(repo_root: Path) -> Path:
    return repo_root / _MARKER_REL


def write_update_marker(repo_root: Path, *, current: str, latest: str, message: str) -> None:
    """Record that ``latest`` is available (best-effort; never raises)."""
    try:
        from agitrack.fileio import atomic_write_text

        # Atomic: a reader never sees a half-written record.
        atomic_write_text(
            marker_path(repo_root),
            # `written` is what makes the record's age readable: see `read_update_marker`.
            json.dumps({"current": current, "latest": latest, "message": message, "written": time.time()}),
        )
    except OSError:
        pass


def read_update_marker(repo_root: Path) -> dict[str, Any] | None:
    """The recorded available-update info, or None when there is no valid, still-current marker.

    A marker is only as good as the check that wrote it, and nothing downstream could tell a
    record written a minute ago from one written in July: every reader simply believed the file.
    An expired record is DELETED rather than skipped, because the thing that would otherwise
    refresh it (a tracker running on this repository) is by definition not running here."""
    path = marker_path(repo_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("latest"):
        return None
    if _age_seconds(path, data) > _MARKER_MAX_AGE_SECONDS:
        clear_update_marker(repo_root)
        return None
    return data


def _age_seconds(path: Path, data: dict[str, Any]) -> float:
    """How long ago the record was written, in seconds.

    ``written`` is what the writer stamped. The file's own mtime is the fallback for a marker left
    by an aGiTrack that predates the stamp, which is exactly the population most likely to be
    stale. A record from the future (a clock that moved, a copied tree) reads as brand new rather
    than as expired: refusing to show a real update is the worse of the two mistakes."""
    stamped = data.get("written")
    if isinstance(stamped, (int, float)) and not isinstance(stamped, bool):
        return max(0.0, time.time() - float(stamped))
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return 0.0


def clear_update_marker(repo_root: Path) -> None:
    """Remove the marker (best-effort) — e.g. once no update is available anymore."""
    try:
        marker_path(repo_root).unlink()
    except OSError:
        pass


def update_reminder_line(repo_root: Path) -> str | None:
    """A one-line reminder for the recorded update, or None. Shared by every surface so the
    wording stays consistent.

    The check's OWN sentence wins when it left one. Two different findings write a marker, and
    they ask for different things: "a newer aGiTrack exists, install it" and "the installation is
    already newer than the code this session loaded, restart it". Rendering both as the first
    told people to reinstall an aGiTrack that was already up to date."""
    info = read_update_marker(repo_root)
    if not info:
        return None
    message = str(info.get("message") or "").strip()
    if message:
        return message
    # Only reachable for a record that stored no sentence of its own, which no current check
    # writes. Kept generic on purpose: without the check's own words there is nothing here to say
    # WHY it could not be installed, and the pre-self-update advice ("choose 'update', or update
    # via pip/pipx") described a version of aGiTrack that no longer needs to be updated by hand.
    current, latest = info.get("current", "?"), info.get("latest", "?")
    return f"aGiTrack update available: {current} → {latest}. Run `agitrack` to update it."
