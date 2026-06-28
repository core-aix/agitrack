"""Base-repo pre-commit guard install/remove (agitrack/git/hooks.py).

The guard blocks an aGiTrack worktree-mode agent from committing into the base repo; it is
scoped by the AGITRACK_WORKTREE_GUARD env marker so it never affects the user or agents run
outside aGiTrack. These tests cover the install/remove/chain logic against a plain directory
(no real git needed)."""

from agitrack.git import hooks as git_hooks


def test_install_creates_guard_hook(tmp_path):
    hooks = tmp_path / "hooks"
    assert git_hooks.install_base_commit_guard(hooks) is True
    hook = hooks / "pre-commit"
    assert hook.exists()
    assert git_hooks.is_ours(hook)
    text = hook.read_text(encoding="utf-8")
    assert git_hooks.ENV_GUARD in text  # only acts when the agent marker is set
    assert "worktrees/" in text  # commits inside a linked worktree are allowed
    assert "--no-verify" in text  # documents the escape hatch


def test_install_preserves_and_chains_existing_user_hook(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

    git_hooks.install_base_commit_guard(hooks)
    orig = hooks / "pre-commit.agitrack-orig"
    assert orig.exists() and "echo mine" in orig.read_text(encoding="utf-8")  # user's hook kept
    guard = hooks / "pre-commit"
    assert git_hooks.is_ours(guard)
    assert ".agitrack-orig" in guard.read_text(encoding="utf-8")  # ours chains to the original


def test_install_idempotent_does_not_double_backup(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    git_hooks.install_base_commit_guard(hooks)
    git_hooks.install_base_commit_guard(hooks)  # second run must not clobber the saved original
    assert "echo mine" in (hooks / "pre-commit.agitrack-orig").read_text(encoding="utf-8")


def test_remove_restores_user_hook(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    git_hooks.install_base_commit_guard(hooks)
    git_hooks.remove_base_commit_guard(hooks)
    guard = hooks / "pre-commit"
    assert guard.exists() and "echo mine" in guard.read_text(encoding="utf-8")  # original restored
    assert not (hooks / "pre-commit.agitrack-orig").exists()
    assert not git_hooks.is_ours(guard)


def test_remove_deletes_guard_when_no_prior_hook(tmp_path):
    hooks = tmp_path / "hooks"
    git_hooks.install_base_commit_guard(hooks)
    git_hooks.remove_base_commit_guard(hooks)
    assert not (hooks / "pre-commit").exists()


def test_remove_leaves_a_foreign_hook_untouched(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\necho not ours\n", encoding="utf-8")
    git_hooks.remove_base_commit_guard(hooks)  # we never installed → must not touch it
    assert (hooks / "pre-commit").read_text(encoding="utf-8") == "#!/bin/sh\necho not ours\n"
