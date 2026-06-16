"""Git and working-tree mechanics: the GitRepo CLI facade, per-session
worktrees/branches, and the single-writer lock. Public names are re-exported
here so call sites import ``from agitrack.git import GitRepo`` etc."""

from agitrack.git.lock import RepoLock, already_running_message
from agitrack.git.repo import GitError, GitRepo
from agitrack.git.worktree import (
    BRANCH_PREFIX,
    BRANCH_PREFIXES,
    LEGACY_BRANCH_PREFIX,
    WORKTREES_DIRNAME,
    WorktreeInfo,
    WorktreeManager,
    _sanitize_name,
    is_managed_branch,
)

__all__ = [
    "GitError",
    "GitRepo",
    "WorktreeInfo",
    "WorktreeManager",
    "BRANCH_PREFIX",
    "BRANCH_PREFIXES",
    "LEGACY_BRANCH_PREFIX",
    "WORKTREES_DIRNAME",
    "is_managed_branch",
    "_sanitize_name",
    "RepoLock",
    "already_running_message",
]
