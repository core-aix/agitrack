from __future__ import annotations

from typing import List

import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from agitrack.git.repo import GitRepo
from agitrack.proc import console_isolation_kwargs


@lru_cache(maxsize=1)
def _rsync_path() -> str | None:
    """Path to ``rsync`` if installed, else None. Memoized — looked up once per process."""
    return shutil.which("rsync")


# All aGiTrack-managed branches live under this prefix so they can be recognised
# (for cleanup / stale recovery) and never collide with the user's branches.
BRANCH_PREFIX = "agitrack/"
WORKTREES_DIRNAME = "worktrees"


def is_managed_branch(branch: str) -> bool:
    """True for an aGiTrack-managed turn branch."""
    return branch.startswith(BRANCH_PREFIX)


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
    branches ``agitrack/<backend>/<name>/tN`` are created lazily on the first commit,
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
        # Detached at base: no branch exists until the session actually commits. The base
        # environment (untracked + git-ignored files) is NOT copied here — git only checks out
        # tracked files, and whether to also copy the full environment is the caller's choice
        # (it can be slow), made via copy_base_environment after the user opts in.
        self.main_repo.worktree_add_detached(str(path), base=base)
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
            # untracked_entries (not untracked_files) so a wholly-untracked directory is ONE
            # ``dir/`` entry — matching how the copy-back offer reports it (collapsed), and
            # letting it copy in one shot.
            entries = self.main_repo.untracked_entries() + self.main_repo.ignored_files()
        except Exception:
            return []
        return [rel for rel in entries if rel.split("/", 1)[0] not in self._ENV_SKIP_TOP]

    def copy_base_environment(self, dest: Path) -> list[str]:
        """Copy the base repo's environment (untracked + git-ignored content) into ``dest``,
        skipping anything already present there (so a freshly created worktree gets the full
        environment, and reused worktrees keep their own edits). Uses rsync's mtime+size delta
        when available so unchanged files are never re-transferred. Best-effort and per-entry
        guarded: one unreadable path never aborts the rest. Returns the repo-relative paths of
        the entries actually copied (so the caller can mark them as base-origin)."""
        return self._copy_entries(dest, self._base_env_entries(), overwrite=False)

    def base_missing_entries(self, dest: Path) -> list[str]:
        """Base environment entries (untracked + git-ignored files/dirs) that ``dest`` doesn't
        have at all — files the base gained since this worktree was last synced. Offered so a
        reused worktree can be brought up to date with new base content, not just changed files."""
        base = self.main_repo.repo
        missing: list[str] = []
        for rel in self._base_env_entries():
            try:
                if (base / rel).exists() and not (dest / rel).exists():
                    missing.append(rel)
            except OSError:
                continue
        return missing

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
                # Compare at whole-second resolution: rsync (and some filesystems) only preserve
                # mtime to the second, so a freshly copied file would otherwise look "newer" by a
                # fraction and trigger spurious overwrite prompts. A change within the same second
                # as the copy is treated as in-sync — close enough for an env file.
                if int(src.stat().st_mtime) > int(dst.stat().st_mtime):
                    newer.append(rel)
            except OSError:
                continue
        return newer

    def _copy_entries(self, dest: Path, rels: list[str], *, overwrite: bool) -> list[str]:
        base = self.main_repo.repo
        rsync = _rsync_path()
        copied: list[str] = []
        for rel in rels:
            src, target = base / rel, dest / rel
            try:
                if not src.exists():
                    continue
                if not overwrite and not src.is_dir() and target.exists():
                    continue  # a plain file already present; leave the worktree's copy alone
                target.parent.mkdir(parents=True, exist_ok=True)
                if rsync is not None and self._rsync_copy(rsync, src, target, overwrite=overwrite):
                    copied.append(rel)
                elif self._shutil_copy(src, target, overwrite=overwrite):
                    copied.append(rel)
            except Exception:
                continue  # skip an odd/unreadable path; copy the rest
        return copied

    @staticmethod
    def _rsync_copy(rsync: str, src: Path, target: Path, *, overwrite: bool) -> bool:
        """Delta-copy ``src`` → ``target`` with rsync (archive mode: only changed files move,
        compared by mtime+size). ``--ignore-existing`` makes a non-overwriting copy skip files
        already in the worktree. Returns True on success, False on failure (caller falls back)."""
        args = [rsync, "-a"]
        if not overwrite:
            args.append("--ignore-existing")
        # A trailing slash on a directory source copies its CONTENTS into target (not target/dir).
        if src.is_dir():
            args += [f"{src}/", f"{target}/"]
        else:
            args += [str(src), str(target)]
        try:
            result = subprocess.run(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **console_isolation_kwargs()
            )
            return result.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _shutil_copy(src: Path, target: Path, *, overwrite: bool) -> bool:
        if src.is_dir():
            shutil.copytree(src, target, dirs_exist_ok=True, symlinks=True)
        elif not overwrite and target.exists():
            return False
        else:
            shutil.copy2(src, target, follow_symlinks=False)
        return True

    def copy_entries(self, dest: Path, rels: list[str]) -> list[str]:
        """Copy specific environment ``rels`` from the base repo into ``dest``, overwriting what
        is there (used to refresh a reused worktree with newer base files). Returns the copied
        repo-relative paths."""
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
        # (agitrack/<backend>/<name>/tN). The session name is the second-to-last path
        # segment.
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
