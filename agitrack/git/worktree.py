from __future__ import annotations

from typing import List

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from agitrack.git.repo import GitRepo

# All aGiTrack-managed branches live under this prefix so they can be recognised
# (for cleanup / stale recovery) and never collide with the user's branches. The
# legacy `agit/` prefix (pre-rename) is still recognised so in-flight sessions
# created by an older aGiT continue to integrate and clean up.
BRANCH_PREFIX = "agitrack/"
LEGACY_BRANCH_PREFIX = "agit/"
BRANCH_PREFIXES = (BRANCH_PREFIX, LEGACY_BRANCH_PREFIX)
WORKTREES_DIRNAME = "worktrees"


def is_managed_branch(branch: str) -> bool:
    """True for an aGiTrack-managed turn branch (current or legacy prefix)."""
    return branch.startswith(BRANCH_PREFIXES)


@dataclass
class WorktreeInfo:
    name: str
    path: Path
    branch: str


def _sanitize_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-")
    return cleaned or "session"


class WorktreeManager:
    """Creates and tracks aGiTrack session worktrees under ``.agitrack/worktrees`` of the
    main working tree. A worktree is created detached at the base; per-turn
    branches ``agit/<backend>/<name>/tN`` are created lazily on the first commit,
    so an unused session never leaves a branch behind."""

    def __init__(self, main_repo: GitRepo) -> None:
        self.main_repo = main_repo
        self.root = main_repo.repo / ".agitrack" / WORKTREES_DIRNAME

    def worktree_path(self, name: str) -> Path:
        return self.root / _sanitize_name(name)

    def turn_branch(self, name: str, turn: int, *, backend: str) -> str:
        # Turn branches live under refs/heads/agitrack/<backend>/<name>/ ; there is
        # deliberately no bare parent ref (it would D/F-conflict with these).
        return f"{BRANCH_PREFIX}{_sanitize_name(backend)}/{_sanitize_name(name)}/t{turn}"

    def is_agitrack_branch(self, branch: str) -> bool:
        return is_managed_branch(branch)

    def create(self, name: str, *, base: str) -> WorktreeInfo:
        path = self.worktree_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Drop any stale admin entry first: if a previous worktree directory was
        # removed out-of-band, git keeps a "prunable" registration that would make
        # `worktree add` fail with "already registered". Pruning clears only
        # entries whose directories are gone, so live worktrees are untouched.
        self.main_repo.worktree_prune()
        # Detached at base: no branch exists until the session actually commits.
        self.main_repo.worktree_add_detached(str(path), base=base)
        # Give the new worktree the SAME full environment the base repo has: git only
        # checks out tracked files, so untracked + git-ignored content (.env, node_modules,
        # virtualenvs, build output, local data) would be missing and the agent would run
        # in a stripped-down tree. Copy it across so the worktree is a faithful clone of the
        # working environment, not just of the committed files.
        self.copy_base_environment(path)
        return WorktreeInfo(name=_sanitize_name(name), path=path, branch="")

    # Never copied between base and worktree: ``.git`` is each worktree's own link file,
    # and ``.agitrack`` holds the worktrees themselves (copying it would recurse). git's
    # ls-files already omits both, but the top-level guard makes the intent explicit and
    # guards a path that slips through (e.g. a custom ignore rule).
    _ENV_SKIP_TOP = {".git", ".agitrack"}

    def _base_env_entries(self) -> list[str]:
        """Repo-relative paths of the base working tree's non-tracked content (untracked
        files plus git-ignored files/dirs) — everything `git worktree add` does NOT place,
        which is exactly what a fresh worktree is missing. Wholly-ignored directories come
        back as a single ``dir/`` entry so they copy in one shot."""
        try:
            entries = self.main_repo.untracked_files() + self.main_repo.ignored_files()
        except Exception:
            return []
        return [rel for rel in entries if rel.split("/", 1)[0] not in self._ENV_SKIP_TOP]

    def copy_base_environment(self, dest: Path) -> int:
        """Copy the base repo's environment (untracked + git-ignored content) into ``dest``,
        skipping anything already present there (so a freshly created worktree gets the full
        environment, and reused worktrees keep their own edits). Best-effort and per-entry
        guarded: one unreadable path never aborts the rest, nor blocks creating the worktree.
        Returns the number of top-level entries copied."""
        return self._copy_entries(dest, self._base_env_entries(), overwrite=False)

    def base_newer_entries(self, dest: Path) -> list[str]:
        """Environment FILES whose base-repo copy is newer than the one already in ``dest``
        — the base moved on (e.g. a regenerated .env or lockfile) while this worktree kept an
        older copy. Compared at file granularity (a wholly-ignored ``dir/`` is treated as a
        unit: offered only when missing, never deep-compared, since walking a big deps tree on
        every start would be slow). Missing entries are NOT returned here — those are copied
        unconditionally by :meth:`copy_base_environment`."""
        base = self.main_repo.repo
        newer: list[str] = []
        for rel in self._base_env_entries():
            src, dst = base / rel, dest / rel
            if rel.endswith("/") or not dst.exists() or not src.exists():
                continue  # a dir unit, a missing dest (handled by copy), or a vanished src
            try:
                if src.stat().st_mtime_ns > dst.stat().st_mtime_ns:
                    newer.append(rel)
            except OSError:
                continue
        return newer

    def _copy_entries(self, dest: Path, rels: list[str], *, overwrite: bool) -> int:
        base = self.main_repo.repo
        copied = 0
        for rel in rels:
            src, target = base / rel, dest / rel
            try:
                if not overwrite and target.exists():
                    continue
                if src.is_dir():
                    shutil.copytree(src, target, dirs_exist_ok=True, symlinks=True)
                elif src.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target, follow_symlinks=False)
                else:
                    continue
                copied += 1
            except Exception:
                continue  # skip an odd/unreadable path; copy the rest
        return copied

    def copy_entries(self, dest: Path, rels: list[str]) -> int:
        """Copy specific environment ``rels`` from the base repo into ``dest``, overwriting
        what is there (used to refresh a reused worktree with newer base files)."""
        return self._copy_entries(dest, rels, overwrite=True)

    def move(self, old_name: str, new_name: str) -> WorktreeInfo:
        """Move the worktree directory ``old_name`` → ``new_name`` (a session
        rename). The caller must have released the worktree first (no running
        process with its cwd inside). Returns the new :class:`WorktreeInfo`."""
        old_path = self.worktree_path(old_name)
        new_path = self.worktree_path(new_name)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        self.main_repo.worktree_prune()  # clear any stale admin entry at the target
        self.main_repo.worktree_move(str(old_path), str(new_path))
        repo = GitRepo(new_path)
        return WorktreeInfo(name=_sanitize_name(new_name), path=new_path, branch=repo.current_branch())

    def remove(self, name: str, *, force: bool = True) -> None:
        path = self.worktree_path(name)
        try:
            self.main_repo.worktree_remove(str(path), force=force)
        except Exception:
            # git refused (e.g. locked/modified). Force-clean the directory and
            # prune the administrative entry so we never leave a worktree behind.
            shutil.rmtree(path, ignore_errors=True)
        self.main_repo.worktree_prune()
        if path.exists():
            # The directory still survives; keep its branches too so a worktree and
            # its branch never drift out of sync (the user can retry the removal).
            return
        # Directory is gone: delete this session's turn branches under any backend
        # (agit/<backend>/<name>/tN, and legacy agit/<name>/tN). The session name
        # is the second-to-last path segment in both layouts.
        sanitized = _sanitize_name(name)
        for branch in self.main_repo.list_branches(BRANCH_PREFIX):
            parts = branch.split("/")
            if len(parts) >= 3 and parts[-2] == sanitized:
                self.main_repo.delete_branch(branch, force=True)

    def list(self) -> List[WorktreeInfo]:
        infos: list[WorktreeInfo] = []
        root = self.root.resolve()
        for entry in self.main_repo.worktree_list():
            path = Path(entry.get("path", ""))
            try:
                inside = path.resolve().parent == root
            except OSError:
                inside = False
            if inside:
                infos.append(WorktreeInfo(name=path.name, path=path, branch=entry.get("branch", "")))
        return infos

    def stale(self) -> List[WorktreeInfo]:
        """aGiTrack worktrees left behind by a previous run (used at startup for
        recovery). With no live manager, every aGiTrack worktree is stale."""
        return self.list()
