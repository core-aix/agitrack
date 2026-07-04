"""Switching a repo between the modes in the README "Modes at a glance" matrix must never
break the next run. The two axes — worktree vs no-worktree (interactive-only worktree) and
auto vs manual commits — plus background vs interactive, install different durable state in
the BASE repo: the manual/auto fold hooks (``prepare-commit-msg`` + ``post-commit``), the
base-commit guard (``pre-commit``), a latent side ref, and the background handshake file. A
run that ends by CRASHING leaves its state behind, and the *next* run may be a different mode.

These tests pin the invariant that each startup begins from a clean hook slate and installs
only what its own mode needs, so a stale hook from the previous mode can neither rewrite this
run's commits (a leftover fold hook) nor block them (a leftover base guard), and a stale
latent chain / dead background handshake is ignored. They complement the per-mode tests in
test_manual_commits.py and test_background.py by exercising the *transitions* between modes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agitrack.config import AgitrackState
from agitrack.git import GitRepo
from agitrack.git import hooks as git_hooks


def _init_repo(path: Path) -> GitRepo:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return GitRepo(path)


def _runner_over(repo: GitRepo, state: AgitrackState, *, use_worktrees: bool, manual: bool):
    """A ProxyRunner over an EXISTING base repo, wired for one matrix cell (worktree/no-worktree
    × auto/manual). UI is stubbed so the hook setup/teardown can run headless."""
    from proxy_helpers import make_runner

    runner = make_runner(
        repo=repo,
        state=state,
        base_repo=repo,
        _manual_commits=manual,
        _use_worktrees=use_worktrees,
        _base_branch=repo.current_branch(),
    )
    runner._set_message = lambda *a, **k: None
    runner._render = lambda *a, **k: None
    return runner


def _mode_runner(tmp_path, *, use_worktrees: bool, manual: bool):
    """Create a fresh repo and a runner over it for one matrix cell."""
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path)
    return _runner_over(repo, state, use_worktrees=use_worktrees, manual=manual), repo, state


def _slate_clean_and_install(runner) -> None:
    """Reproduce the startup hook sequence from ProxyRunner.run(): clear any stale hooks left by
    a prior (possibly crashed) run of the OTHER mode, then install exactly what this mode wants."""
    runner._remove_base_commit_guard()
    runner._teardown_manual_commit_mode()
    runner._install_base_commit_guard()
    runner._setup_manual_commit_mode()


def _hooks_dir(repo: GitRepo) -> Path:
    return repo.repo / ".git" / "hooks"


def _has_manual_hooks(repo: GitRepo) -> bool:
    d = _hooks_dir(repo)
    return (d / "prepare-commit-msg").exists() and (d / "post-commit").exists()


def _has_base_guard(repo: GitRepo) -> bool:
    return git_hooks.is_ours(_hooks_dir(repo) / "pre-commit")


# --- worktree ↔ no-worktree: the fold hooks and base guard must not cross over ---------------


def test_switch_noworktree_manual_to_worktree_removes_stale_fold_hooks(tmp_path):
    # A no-worktree/manual run installs the fold hooks, then CRASHES (no teardown). The next run
    # is a worktree run: its startup must clear the stale fold hooks so they can't rewrite the
    # worktree run's commits — and worktree mode does NOT reinstall them.
    repo = _init_repo(tmp_path)
    git_hooks.install_manual_commit_hooks(_hooks_dir(repo))
    runner = _runner_over(repo, AgitrackState(tmp_path), use_worktrees=True, manual=False)
    assert _has_manual_hooks(repo)  # stale hooks present before the worktree run starts

    _slate_clean_and_install(runner)

    assert not _has_manual_hooks(repo)  # cleared, and worktree mode did not reinstall them


def test_switch_worktree_to_noworktree_removes_stale_base_guard_and_installs_fold_hooks(tmp_path):
    # A worktree run installs the base-commit guard, then CRASHES. The next run is no-worktree,
    # where the user/agent commit onto the base directly — a stale guard would BLOCK those
    # commits. Startup must clear it and install this mode's fold hooks instead.
    repo = _init_repo(tmp_path)
    git_hooks.install_base_commit_guard(_hooks_dir(repo))
    runner = _runner_over(repo, AgitrackState(tmp_path), use_worktrees=False, manual=True)
    assert _has_base_guard(repo)  # stale guard present before the no-worktree run starts

    _slate_clean_and_install(runner)

    assert not _has_base_guard(repo)  # the guard that would block base commits is gone
    assert _has_manual_hooks(repo)  # and this mode's fold hooks are installed


def test_worktree_teardown_removes_stale_manual_hooks_even_in_worktree_mode(tmp_path):
    # _teardown_manual_commit_mode is unconditional (not gated on latent tracking), so a worktree
    # run's own teardown also clears a stale fold hook — the graceful-exit counterpart of the
    # startup slate-clean above.
    runner, repo, _ = _mode_runner(tmp_path, use_worktrees=True, manual=False)
    git_hooks.install_manual_commit_hooks(_hooks_dir(repo))
    assert _has_manual_hooks(repo)

    runner._teardown_manual_commit_mode()

    assert not _has_manual_hooks(repo)


def test_full_mode_cycle_leaves_only_the_current_modes_hooks(tmp_path):
    # Cycle a single repo through every matrix cell in turn. After each run's startup slate-clean,
    # the base repo carries ONLY the hooks that cell needs — never a leftover from the prior cell.
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path)
    from proxy_helpers import make_runner

    def run_mode(use_worktrees: bool, manual: bool):
        runner = make_runner(
            repo=repo,
            state=state,
            base_repo=repo,
            _manual_commits=manual,
            _use_worktrees=use_worktrees,
            _base_branch=repo.current_branch(),
        )
        runner._set_message = lambda *a, **k: None
        runner._render = lambda *a, **k: None
        _slate_clean_and_install(runner)
        return runner

    # no-worktree modes install the fold hooks; worktree mode installs neither here (the base
    # guard needs an active worktree, absent in this unit runner) but must never carry fold hooks.
    run_mode(use_worktrees=False, manual=False)  # no-worktree auto
    assert _has_manual_hooks(repo)
    run_mode(use_worktrees=True, manual=False)  # → interactive worktree auto
    assert not _has_manual_hooks(repo)
    run_mode(use_worktrees=False, manual=True)  # → no-worktree manual
    assert _has_manual_hooks(repo)
    run_mode(use_worktrees=True, manual=False)  # → back to worktree
    assert not _has_manual_hooks(repo)


# --- auto ↔ manual (both no-worktree): a stale latent chain must not re-fold old turns --------


def test_switch_manual_to_auto_resets_stale_latent_chain(tmp_path):
    # Both no-worktree modes share the latent ref. If a manual run left a latent chain that is
    # already contained in HEAD (the user committed after exit), the next run's setup must reset
    # it so those turns aren't re-folded into an unrelated future commit.
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path)
    runner = _runner_over(repo, state, use_worktrees=False, manual=True)
    runner._setup_manual_commit_mode()
    # Record a latent turn (HEAD frozen), then commit the same change directly — as would happen
    # after the session exits and its fold hook is gone. The working tree is now CLEAN (the change
    # is in HEAD), so the pending latent chain is redundant and must not re-fold into a later commit.
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record("<aGiTrack> t\n\n# aGiTrack Metadata\ncommit_type: manual\n")
    stale_tip = repo.ref_sha(runner._manual_ref())
    subprocess.run(["git", "-C", str(repo.repo), "commit", "-aqm", "user commit"], check=True)
    assert stale_tip != repo.rev_parse("HEAD")  # the chain still points at the pre-commit latent tip

    # A fresh no-worktree AUTO run starts over the same repo/session: setup resets the now-stale
    # chain to HEAD (clean-tree rule) so its turn can't attach to an unrelated future commit.
    auto = _runner_over(repo, state, use_worktrees=False, manual=False)
    auto._setup_manual_commit_mode()
    assert auto.repo.ref_sha(auto._manual_ref()) == auto.repo.rev_parse("HEAD")


# --- background ↔ interactive: a dead handshake must read as "not running" --------------------


def test_stale_background_handshake_with_dead_pid_reads_as_not_running(tmp_path):
    # A background tracker writes .agitrack/background.json with its pid; if it crashed, the file
    # lingers. A later `-b status` / a foreground start must treat a dead-pid handshake as "no
    # tracker running" (the RepoLock is the real mutual-exclusion guard), not refuse to start.
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    path = bg.background_handshake_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    dead_pid = _find_dead_pid()
    path.write_text(f'{{"pid": {dead_pid}}}', encoding="utf-8")

    # A dead-pid handshake reads as "no tracker", and the stale file is cleaned up so it can't
    # keep masquerading as a live tracker on later checks.
    assert bg._live_background_pid(repo) is None
    assert not path.exists()


def _find_dead_pid() -> int:
    # A pid that is not currently a live process. Scan downward from a high value; os.kill(pid, 0)
    # raises ProcessLookupError for a free pid (and we skip our own / permission cases).
    for candidate in range(999_999, 100_000, -1):
        if candidate == os.getpid():
            continue
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
    return 999_999
