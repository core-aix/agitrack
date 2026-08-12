"""The user-wide list of repositories aGiTrack knows about.

One dashboard now serves every repository on one port, switching between them by URL path, and
that needs an answer to a question nothing in aGiTrack could previously answer: *which*
repositories? Each repo's state lives inside the repo, and the daemon registry only knows what is
running this second, so a repo you tracked yesterday and closed the terminal on had vanished.

So every repository aGiTrack is pointed at is remembered here, in ``~/.agitrack/repos.json``
(``AGITRACK_CONFIG_DIR`` moves it, like every other global file). An entry is cheap: a path, a
display name, a stable slug for URLs, when it was last seen, and the small amount of per-repo
dashboard state that has to outlive any one daemon.

Two design points worth keeping:

* **The slug is derived, not allocated.** ``agitrack`` + a short hash of the resolved path means
  the URL for a repo is the same in every process, computable without reading this file, and
  stable across restarts. Two repos with the same basename differ in the hash.
* **A remembered repo is not a running one.** This list is history ("aGiTrack has worked here"),
  not process state; :mod:`agitrack.daemons` still owns "what is alive right now". Entries whose
  directory has gone are dropped on read, since a repo that no longer exists cannot be served.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Dashboard views a repo can be shown in. "active" is aGiTrack's own tracking; "backtrace" is the
# reconstruction from local agent transcripts.
ACTIVE = "active"
BACKTRACE = "backtrace"


@dataclass
class RepoEntry:
    """One remembered repository."""

    path: str
    name: str = ""
    slug: str = ""
    first_seen: int = 0
    last_seen: int = 0
    # The view this repo was last shown in, so the dashboard reopens where the user left it.
    view: str = ""
    # Set once the repo has been seen to hold aGiTrack-tracked commits carrying token counts.
    # It is what makes the automatic backtrace-to-active switch happen EXACTLY ONCE: after that
    # the user's own choice of view stands, and a repo never silently jumps views again.
    tracked_seen: bool = False
    # Whether the dashboard should still offer this repo. `agitrack stop` clears it: the entry is
    # KEPT (so the remembered view and the once-only switch survive a stop/start cycle) but the
    # repo drops out of the switcher, because a dashboard that keeps listing a project the user
    # explicitly stopped is ignoring them.
    served: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def directory(self) -> Path:
        return Path(self.path)

    def to_dict(self) -> dict[str, Any]:
        record = {
            "path": self.path,
            "name": self.name,
            "slug": self.slug,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "view": self.view,
            "tracked_seen": self.tracked_seen,
            "served": self.served,
        }
        record.update(self.extra)
        return record

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RepoEntry | None":
        path = str(data.get("path") or "").strip()
        if not path:
            return None
        known = {"path", "name", "slug", "first_seen", "last_seen", "view", "tracked_seen", "served"}
        return cls(
            path=path,
            name=str(data.get("name") or Path(path).name),
            slug=str(data.get("slug") or slug_for(path)),
            first_seen=int(data.get("first_seen") or 0),
            last_seen=int(data.get("last_seen") or 0),
            view=str(data.get("view") or ""),
            tracked_seen=bool(data.get("tracked_seen")),
            # Absent in files written before `agitrack stop` could unmount a repo: those entries
            # are all repos aGiTrack was working on, so the missing value means served.
            served=bool(data.get("served", True)),
            # Anything a NEWER aGiTrack wrote is carried through untouched rather than dropped on
            # the next save: the file is shared by every version the user has installed.
            extra={k: v for k, v in data.items() if k not in known},
        )


# --------------------------------------------------------------------------- storage


def registry_path() -> Path:
    from agitrack.env import getenv_compat

    config_dir = getenv_compat("CONFIG_DIR")
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".agitrack"
    return base / "repos.json"


def _resolve(path: str | os.PathLike[str]) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(Path(path).expanduser())


def slug_for(path: str | os.PathLike[str]) -> str:
    """A short, stable, URL-safe id for ``path``.

    Derived from the path rather than allocated, so every process computes the same slug for the
    same repository without coordinating through this file: the URL a background tracker prints
    is the URL the dashboard serves. The readable half is the directory name (so a URL is
    recognisable at a glance) and the hash half keeps two ``src`` directories apart."""
    resolved = _resolve(path)
    stem = re.sub(r"[^a-z0-9]+", "-", Path(resolved).name.lower()).strip("-") or "repo"
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{digest}"


def _read() -> list[RepoEntry]:
    try:
        with registry_path().open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("repos") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out: list[RepoEntry] = []
    for row in rows:
        if isinstance(row, dict):
            entry = RepoEntry.from_dict(row)
            if entry is not None:
                out.append(entry)
    return out


def _write(entries: list[RepoEntry]) -> None:
    from agitrack.fileio import atomic_write_text

    path = registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"repos": [entry.to_dict() for entry in entries]}, indent=2))
    except OSError:
        # Remembering a repo is a convenience, never a precondition: a read-only or full home
        # directory must not stop aGiTrack from tracking or serving anything.
        pass


def _update(path: str | os.PathLike[str], mutate, *, touch: bool = False) -> RepoEntry:
    """Read-modify-write one entry, creating it if needed. Returns the stored entry.

    Not locked. Two aGiTrack processes remembering two different repos at the same instant can
    lose one of the two writes, and the cost of that is a repo missing from a menu until the next
    time it is opened — which is not worth a lock file on a path this hot."""
    resolved = _resolve(path)
    entries = _read()
    for entry in entries:
        if _resolve(entry.path) == resolved:
            break
    else:
        entry = RepoEntry(
            path=resolved,
            name=Path(resolved).name,
            slug=slug_for(resolved),
            first_seen=int(time.time()),
        )
        entries.append(entry)
    entry.path = resolved
    entry.name = entry.name or Path(resolved).name
    entry.slug = entry.slug or slug_for(resolved)
    mutate(entry)
    if touch:
        # Move it to the end, so file order records the order repos were last worked on. The
        # ``last_seen`` timestamps are whole seconds and two repos opened in the same second tie;
        # without a second key the "most recent first" list would reorder itself at random.
        entries.remove(entry)
        entries.append(entry)
    _write(entries)
    return entry


# --------------------------------------------------------------------------- public API


def remember(path: str | os.PathLike[str], *, view: str = "") -> RepoEntry:
    """Record that aGiTrack is (or was just) working on ``path``, and return its entry.

    Called from every mode's startup, so the dashboard's repo list is "everywhere you have used
    aGiTrack" rather than "wherever a daemon happens to be running"."""

    def mutate(entry: RepoEntry) -> None:
        entry.last_seen = int(time.time())
        entry.served = True  # working on a repo is what puts it back in the switcher
        if view:
            entry.view = view

    return _update(path, mutate, touch=True)


def forget(path: str | os.PathLike[str]) -> bool:
    """Drop ``path`` from the list. True when it was there."""
    resolved = _resolve(path)
    entries = _read()
    kept = [entry for entry in entries if _resolve(entry.path) != resolved]
    if len(kept) == len(entries):
        return False
    _write(kept)
    return True


def list_repos(*, existing_only: bool = True, served_only: bool = True) -> list[RepoEntry]:
    """Every repository the dashboard should offer, most recently used first.

    Directories that have gone are dropped: the list exists to be switched between, and offering
    a repo the dashboard cannot open is worse than not offering it. Repos stopped with
    ``agitrack stop`` are dropped for the same reason, without losing their remembered state."""
    entries = _read()
    if served_only:
        entries = [entry for entry in entries if entry.served]
    if existing_only:
        entries = [entry for entry in entries if _is_dir(entry.directory)]
    # Latest-in-file first, then a STABLE sort by timestamp: within one second (which is as fine
    # as ``last_seen`` gets) the order repos were last opened in decides.
    entries.reverse()
    entries.sort(key=lambda entry: entry.last_seen, reverse=True)
    return entries


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def find(slug: str) -> RepoEntry | None:
    """The remembered repo with this slug, or None."""
    for entry in list_repos():
        if entry.slug == slug:
            return entry
    return None


def entry_for(path: str | os.PathLike[str]) -> RepoEntry | None:
    """The remembered entry for ``path``, without creating one."""
    resolved = _resolve(path)
    for entry in _read():
        if _resolve(entry.path) == resolved:
            return entry
    return None


def set_view(path: str | os.PathLike[str], view: str) -> RepoEntry:
    """Remember which view (``active`` / ``backtrace``) this repo was last shown in."""
    return _update(path, lambda entry: setattr(entry, "view", view))


def set_served(path: str | os.PathLike[str], served: bool) -> RepoEntry:
    """Add or remove this repo from the set the dashboard offers (see :attr:`RepoEntry.served`)."""
    return _update(path, lambda entry: setattr(entry, "served", served))


def mark_tracked_seen(path: str | os.PathLike[str]) -> RepoEntry:
    """Record that this repo has been seen holding tracked commits with token counts.

    The flag is one-way on purpose. It is the memory behind "switch to the active dashboard the
    first time there is something to show there, and only the first time"."""
    return _update(path, lambda entry: setattr(entry, "tracked_seen", True))
