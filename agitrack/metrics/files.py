"""Per-file change browser for the dashboard.

The commit log answers "what happened, turn by turn". This answers the complementary
question: for a given FILE, what is its whole change history, and what conversation (and how
many tokens) produced each change. It powers a file browser shown in both the live dashboard
(real git commits) and ``--backtrace`` (reconstructed agent turns) — the two only differ in
how a change's files and per-file diff are sourced, so the model and endpoints are shared.

A :class:`FileBrowser` is built once from a list of :class:`CommitStat` plus two providers —
one that says which files a stat changed (with per-file line counts), and one that yields the
per-file diff for a change on demand — and then serializes to the ``/files`` (summary list),
``/filelog`` (one file's history, with each change's conversation/tokens), and ``/filediff``
(one file's diff for one change) endpoints.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from agitrack.metrics.collect import CommitStat

# A provider that returns the files a commit/turn changed, each as ``(path, insertions,
# deletions)``. Empty for a change that touched no files (a pure-conversation turn, a merge).
ChangedFiles = Callable[[CommitStat], "list[tuple[str, int, int]]"]

# A provider that returns the unified diff a single change made to a single file, on demand
# (``(path, sha) -> patch``). Kept lazy so the potentially large diff text is produced only
# when the user opens that file's change.
FileDiff = Callable[[str, str], str]


@dataclass
class FileChange:
    """One change to one file: a pointer to the commit/turn that made it, with the per-file
    line counts and the full conversation/metadata behind it (``message``)."""

    sha: str
    timestamp: int
    backend: str | None
    model: str | None
    tokens: dict[str, int]
    insertions: int
    deletions: int
    subject: str
    prompt: str
    message: str


@dataclass
class FileEntry:
    path: str
    changes: list[FileChange] = field(default_factory=list)  # newest first

    @property
    def insertions(self) -> int:
        return sum(change.insertions for change in self.changes)

    @property
    def deletions(self) -> int:
        return sum(change.deletions for change in self.changes)

    @property
    def output_tokens(self) -> int:
        return sum(change.tokens.get("output", 0) for change in self.changes)

    @property
    def last_ts(self) -> int:
        return max((change.timestamp for change in self.changes), default=0)


@dataclass
class FileBrowser:
    index: dict[str, FileEntry]
    diff_fn: FileDiff

    def files_payload(self) -> list[dict]:
        """The file list: one row per changed file, most-recently-changed first."""
        rows = [
            {
                "path": entry.path,
                "changes": len(entry.changes),
                "ins": entry.insertions,
                "del": entry.deletions,
                "output_tokens": entry.output_tokens,
                "last_ts": entry.last_ts,
            }
            for entry in self.index.values()
        ]
        rows.sort(key=lambda row: (row["last_ts"], row["changes"]), reverse=True)
        return rows

    def file_log_payload(self, path: str) -> dict:
        """One file's full change history — each change with its conversation and tokens."""
        entry = self.index.get(path)
        if entry is None:
            return {"path": path, "changes": []}
        return {
            "path": path,
            "changes": [
                {
                    "sha": change.sha,
                    "ts": change.timestamp,
                    "backend": change.backend,
                    "model": change.model,
                    "tokens": change.tokens,
                    "ins": change.insertions,
                    "del": change.deletions,
                    "subject": change.subject,
                    "prompt": change.prompt,
                    "message": change.message,
                }
                for change in entry.changes
            ],
        }

    def file_diff(self, path: str, sha: str) -> dict:
        if path not in self.index:
            return {"path": path, "sha": sha, "diff": "", "error": "unknown file"}
        try:
            return {"path": path, "sha": sha, "diff": self.diff_fn(path, sha)}
        except Exception:
            return {"path": path, "sha": sha, "diff": "", "error": "could not read this diff"}


def build_file_browser(
    stats: list[CommitStat],
    changed_files: ChangedFiles,
    diff_fn: FileDiff,
    *,
    keep: "Callable[[str], bool] | None" = None,
    all_files: "Iterable[str] | None" = None,
) -> FileBrowser:
    """Assemble a :class:`FileBrowser` from ``stats`` (oldest first) using ``changed_files`` to
    attribute each change to its files. Each file's history ends up newest-first.

    ``all_files`` (when given) seeds the browser with EVERY file that currently exists, so the
    file tab is a browsable view of the whole tree — a file no agent ever touched still appears,
    with zero changes, instead of the list showing only AI-touched files.

    ``keep`` (when given) restricts the browser to the files that still EXIST — a file that was
    created and later deleted should not clutter the file list, even though its changes still
    counted toward the token/line totals (those come from the commit stats, not this browser).
    """
    index: dict[str, FileEntry] = {}
    for path in all_files or ():
        if path:
            index.setdefault(path, FileEntry(path=path))
    for stat in stats:
        for path, insertions, deletions in changed_files(stat):
            if not path:
                continue
            entry = index.setdefault(path, FileEntry(path=path))
            entry.changes.append(
                FileChange(
                    sha=stat.sha,
                    timestamp=stat.timestamp,
                    backend=stat.backend,
                    model=stat.model,
                    tokens=dict(stat.tokens),
                    insertions=insertions,
                    deletions=deletions,
                    subject=stat.subject,
                    prompt=stat.prompt,
                    message=stat.message,
                )
            )
    for entry in index.values():
        entry.changes.reverse()  # stats are oldest-first; show newest change first
    if keep is not None:
        index = {path: entry for path, entry in index.items() if keep(path)}
    return FileBrowser(index=index, diff_fn=diff_fn)


# ---------------------------------------------------------------------------
# Providers: backtrace (reconstructed edits) and git (real commits)
# ---------------------------------------------------------------------------


def backtrace_browser(stats: list[CommitStat], file_edits: dict, *, directory: "Path | None" = None) -> FileBrowser:
    """A file browser for the ``--backtrace`` view, sourced from the per-turn
    :class:`~agitrack.transcripts.types.FileEdit`s (``sha -> [FileEdit]``).

    When ``directory`` is given, the browser lists every file that currently exists under it (so
    files no agent touched are still browsable) and drops files an agent created and later deleted
    (their edits still counted toward the totals)."""
    from agitrack.transcripts.edits import combine_patches

    def changed(stat: CommitStat) -> list[tuple[str, int, int]]:
        return [(edit.path, edit.insertions, edit.deletions) for edit in file_edits.get(stat.sha, [])]

    def diff(path: str, sha: str) -> str:
        edits = [edit for edit in file_edits.get(sha, []) if edit.path == path]
        return combine_patches(edits)

    keep = None
    current: set[str] | None = None
    if directory is not None:
        root = directory
        current = current_disk_files(root)
        keep = lambda path: (root / path).exists()  # noqa: E731 — a small local predicate reads fine inline
    return build_file_browser(stats, changed, diff, keep=keep, all_files=current)


def git_browser(repo, stats: list[CommitStat], ref: str = "HEAD") -> FileBrowser:
    """A file browser for the live dashboard, sourced from real git history: which files each
    commit changed (from ``git log --numstat``) and the per-file diff on demand (``git show``).
    Only commits present in ``stats`` are attributed, so it matches the dashboard's scope. Every
    file in ``ref``'s tree is listed — including ones no AI commit ever touched — while a
    since-deleted file's changes still count toward the totals but don't clutter the file tab."""
    known = {stat.sha for stat in stats}
    numstat = _numstat_by_commit(repo, ref, known)
    current = _current_files(repo, ref)

    def changed(stat: CommitStat) -> list[tuple[str, int, int]]:
        return numstat.get(stat.sha, [])

    def diff(path: str, sha: str) -> str:
        return _git_file_diff(repo, sha, path)

    return build_file_browser(stats, changed, diff, keep=lambda path: path in current, all_files=current)


# Directories a file listing must never descend into: VCS/tooling state and dependency trees.
# They hold no user-authored source and would swamp the file tab (and the payload).
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".agitrack",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".next",
        ".cache",
        "dist",
        "build",
        "target",
    }
)
_MAX_DISK_FILES = 20000


def current_disk_files(directory: Path) -> set[str]:
    """Every file that currently exists under ``directory``, as directory-relative paths.

    Prefers ``git ls-files`` (tracked + untracked-but-not-ignored) so a repo's listing matches what
    git considers part of the project. Falls back to a bounded walk for a plain directory, because
    ``--backtrace`` must work where there is no git history at all."""
    try:
        done = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if done.returncode == 0:
            names = {name for name in done.stdout.split("\x00") if name}
            if names:
                return names
    except (OSError, subprocess.SubprocessError):
        pass
    names = set()
    for root, dirs, filenames in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for filename in filenames:
            if filename.startswith("."):
                continue
            names.add(os.path.relpath(os.path.join(root, filename), directory))
            if len(names) >= _MAX_DISK_FILES:
                return names
    return names


def _current_files(repo, ref: str) -> set[str]:
    """The paths that still exist in ``ref``'s tree (NUL-separated so odd filenames are safe)."""
    out = repo._run(["git", "ls-tree", "-r", "--name-only", "-z", ref, "--"], check=False).stdout
    return {name for name in out.split("\x00") if name}


def _numstat_by_commit(repo, ref: str, known: set[str]) -> dict[str, list[tuple[str, int, int]]]:
    """Parse ``git log --numstat`` for ``ref`` into ``sha -> [(path, insertions, deletions)]``,
    keeping only commits the dashboard knows. Reads local blobs only, like the dashboard's
    line-count pass, so it never triggers a blobless clone to lazily fetch history."""
    out: dict[str, list[tuple[str, int, int]]] = {}
    output = repo._run(
        ["git", "log", "--numstat", "--format=%x01%H", ref, "--"], check=False, allow_lazy_fetch=False
    ).stdout
    current: str | None = None
    for line in output.splitlines():
        if line.startswith("\x01"):
            current = line[1:].strip()
            continue
        if current is None or current not in known:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        adds, dels, path = parts[0], parts[1], parts[2]
        # A rename renders as ``old => new`` (or ``dir/{old => new}/file``); keep the new path
        # (best-effort — renames are rare and this only affects which file the change lists under).
        if "=>" in path:
            path = path.replace("{", "").replace("}", "").split("=>")[-1].strip().replace("//", "/")
        out.setdefault(current, []).append(
            (path, int(adds) if adds.isdigit() else 0, int(dels) if dels.isdigit() else 0)
        )
    return out


def _git_file_diff(repo, sha: str, path: str) -> str:
    """The diff a single commit made to a single file, for ANY commit — a normal commit, a
    merge/cover commit (``--first-parent`` shows its change against the mainline, matching
    :meth:`GitRepo.show_commit`, so merges aren't blank), or the root commit (all-additions).
    A binary file shows git's ``Binary files … differ`` line, which the UI turns into a hint."""
    import re

    if not re.fullmatch(r"[0-9a-fA-F]{4,64}", sha or ""):
        return ""
    return repo._run(
        ["git", "show", "--format=", "--no-color", "--first-parent", "--patch", sha, "--", path], check=False
    ).stdout
