"""Manual-commit mode (``--manual-commits`` / ``-m``).

The mode is a strict addition: the agent edits the current branch directly (no worktree)
and each turn is recorded as a hidden "latent" commit on ``refs/agitrack/manual/<id>``
instead of landing on the branch. Commits stay user-triggered — a ``git commit`` (via the
aGiTrack menu or externally) folds the pending latent turns' trace/metadata into that ONE
commit via a ``prepare-commit-msg`` hook, and a ``post-commit`` hook resets the latent ref.

These tests pin the pieces that make that work end to end, and assert the mode is inert
when off (no hooks, no latent commits, existing paths unchanged).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agitrack.backends.base import TokenUsage
from agitrack.commits.message import build_agent_commit_message, build_manual_squash_trailer
from agitrack.config import AgitrackState
from agitrack.config.settings import GlobalConfig
from agitrack.git import GitRepo
from agitrack.git import hooks as git_hooks
from agitrack.metrics.collect import _parse_commit, build_dashboard, collect_manual_pending
from agitrack.proxy.commit_engine import CommitEngine
from agitrack.transcripts.opencode import SessionTurn


def _init_repo(path: Path) -> GitRepo:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return GitRepo(path)


def _git(repo: GitRepo, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo.repo), *args], capture_output=True, text=True, check=True).stdout


# --- GitRepo primitives -----------------------------------------------------


def test_snapshot_worktree_tree_excludes_scaffolding_and_preserves_index(tmp_path):
    repo = _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("one\ntwo\n", encoding="utf-8")  # tracked edit
    (tmp_path / "b.txt").write_text("new\n", encoding="utf-8")  # new file
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "junk").write_text("x\n", encoding="utf-8")  # scaffolding
    # Stage something into the REAL index to prove the snapshot doesn't disturb it.
    _git(repo, "add", "a.txt")
    index_before = _git(repo, "diff", "--cached", "--name-only")

    tree = repo.snapshot_worktree_tree()

    files = _git(repo, "ls-tree", "-r", "--name-only", tree).split()
    assert "a.txt" in files and "b.txt" in files
    assert not any(f.startswith(".claude") for f in files)  # scaffolding excluded
    assert _git(repo, "diff", "--cached", "--name-only") == index_before  # index untouched


def test_commit_tree_records_without_moving_head(tmp_path):
    repo = _init_repo(tmp_path)
    head = repo.rev_parse("HEAD")
    (tmp_path / "b.txt").write_text("x\n", encoding="utf-8")
    tree = repo.snapshot_worktree_tree()
    sha = repo.commit_tree(tree, parents=[head], message="latent")
    repo.update_ref("refs/agitrack/manual/s", sha)

    assert repo.rev_parse("HEAD") == head  # HEAD never moved
    assert repo.ref_sha("refs/agitrack/manual/s") == sha
    assert repo.parents(sha) == [head]


def test_cover_commit_tree_override_adds_no_diff(tmp_path):
    repo = _init_repo(tmp_path)
    head = repo.rev_parse("HEAD")
    # A side commit to act as the (provenance) second parent.
    (tmp_path / "b.txt").write_text("x\n", encoding="utf-8")
    side = repo.commit_tree(repo.snapshot_worktree_tree(), parents=[head], message="side")
    head_tree = repo.rev_parse("HEAD^{tree}")

    repo.cover_commit("cover", first_parent=head, second_parent=side, tree=head_tree)

    assert repo.rev_parse("HEAD^{tree}") == head_tree  # cover introduced no diff
    assert repo.parents("HEAD") == [head, side]  # merge-shaped for provenance


# --- trailer + squash parsing ----------------------------------------------


def _agent_body(prompt: str, out: int) -> str:
    return build_agent_commit_message(
        latest_prompt=prompt,
        trace=[{"role": "user", "content": prompt}, {"role": "agent", "content": "did " + prompt}],
        backend="claude",
        backend_session_id="bs",
        agitrack_session_id="sid",
        model="opus",
        token_usage={"output": out, "input": 5},
        session_name="s",
    )


def test_manual_trailer_squash_parses_as_agent_with_summed_tokens():
    bodies = [_agent_body("add feature", 100), _agent_body("fix bug", 50)]
    trailer = build_manual_squash_trailer(agitrack_session_id="sid", latent_bodies=bodies)
    folded = "Implement thing\n\nbody\n\n" + trailer
    stat = _parse_commit("abc1234", "me", "me@x", "1700000000", folded)
    assert stat.kind == "agent"  # any agent turn ⇒ agent-tracked
    assert stat.subject == "Implement thing"  # the user's own subject leads
    assert [c.kind for c in stat.constituents] == ["user", "agent", "agent"]
    assert stat.tokens.get("output") == 150  # summed across turns


def test_manual_trailer_message_is_chronological_like_any_squash():
    # The raw commit message must list turns CHRONOLOGICALLY (oldest-first), the same order a
    # normal squash merge uses — the newest-first reorder is display-only (see the dashboard
    # test below). This keeps a manual-mode commit reading like any other squash.
    bodies = [_agent_body("first", 10), _agent_body("second", 20), _agent_body("third", 30)]
    trailer = build_manual_squash_trailer(agitrack_session_id="sid", latent_bodies=bodies)
    stat = _parse_commit("h", "me", "me@x", "1", "User commit\n\n" + trailer)
    agent_subjects = [c.subject for c in stat.constituents if "aGiTrack" in (c.subject or "")]
    assert agent_subjects == ["<aGiTrack> first", "<aGiTrack> second", "<aGiTrack> third"]


def test_dashboard_displays_manual_squash_newest_first(tmp_path):
    # The dashboard payload must reorder the (chronological) constituents NEWEST-first so the
    # expansion matches the newest-first commit log — without touching the commit message.
    from agitrack.metrics.web import dashboard_data

    repo = _init_repo(tmp_path)
    bodies = [_agent_body("first", 10), _agent_body("second", 20), _agent_body("third", 30)]
    msg = "User commit\n\n" + build_manual_squash_trailer(agitrack_session_id="sid", latent_bodies=bodies)
    _git(repo, "commit", "--allow-empty", "-m", msg)

    data = dashboard_data(build_dashboard(repo))
    folded = next(c for c in data["commits"] if c["subject"] == "User commit")
    agent_parts = [p["subject"] for p in folded["parts"] if "aGiTrack" in (p["subject"] or "")]
    assert agent_parts == ["<aGiTrack> third", "<aGiTrack> second", "<aGiTrack> first"]


def test_dashboard_squash_main_message_omits_constituents(tmp_path):
    # The main message must NOT duplicate the squashed turns — they are listed (in full) under
    # "parts", so the message keeps only the commit's own leading text.
    from agitrack.metrics.web import dashboard_data

    repo = _init_repo(tmp_path)
    bodies = [_agent_body("first", 10), _agent_body("second", 20)]
    msg = "My user commit\n\nsome body text\n\n" + build_manual_squash_trailer(
        agitrack_session_id="sid", latent_bodies=bodies
    )
    _git(repo, "commit", "--allow-empty", "-m", msg)

    folded = next(c for c in dashboard_data(build_dashboard(repo))["commits"] if c["subject"] == "My user commit")
    # Main message: only the user's own text; no constituent blocks.
    assert "My user commit" in folded["message"] and "some body text" in folded["message"]
    assert "# aGiTrack Metadata" not in folded["message"]
    assert "<aGiTrack> first" not in folded["message"]
    # The turns are still available in full under parts.
    part_text = "\n".join(p["message"] for p in folded["parts"])
    assert "<aGiTrack> first" in part_text and "<aGiTrack> second" in part_text


def test_dashboard_non_squash_message_is_unchanged(tmp_path):
    # A normal (non-squash) agent commit has no parts, so its full message — trace + metadata —
    # is preserved (nothing to de-duplicate).
    from agitrack.metrics.web import dashboard_data

    repo = _init_repo(tmp_path)
    _git(repo, "commit", "--allow-empty", "-m", _agent_body("single turn", 5))
    c = next(x for x in dashboard_data(build_dashboard(repo))["commits"] if "single turn" in (x["subject"] or ""))
    assert not c["parts"]  # not a squash
    assert "# aGiTrack Metadata" in c["message"]  # full message preserved


def test_manual_trailer_with_no_pending_turns_is_empty_no_footprint():
    # No pending AI turns ⇒ the commit holds only the user's own code, so aGiTrack adds
    # NOTHING: the trailer is empty and the commit is left completely untouched (no cover /
    # no attribution when no code was written by AI).
    trailer = build_manual_squash_trailer(agitrack_session_id="sid", latent_bodies=[])
    assert trailer == ""
    folded = "Just my edit\n\n" + trailer
    stat = _parse_commit("def4567", "me", "me@x", "1700000000", folded)
    assert stat.kind == "untracked"  # no aGiTrack metadata at all


# --- hooks ------------------------------------------------------------------


def _setup_manual_ref_and_trailer(repo: GitRepo, trailer: str) -> None:
    agit = repo.repo / ".agitrack"
    agit.mkdir(exist_ok=True)
    (agit / "manual-ref").write_text("refs/agitrack/manual/s\n", encoding="utf-8")
    (agit / "manual-pending-trailer").write_text(trailer, encoding="utf-8")
    repo.update_ref("refs/agitrack/manual/s", repo.rev_parse("HEAD"))


def test_hooks_fold_trailer_into_commit_and_reset_ref(tmp_path):
    repo = _init_repo(tmp_path)
    assert git_hooks.install_manual_commit_hooks(repo.repo / ".git" / "hooks")
    trailer = build_manual_squash_trailer(agitrack_session_id="s", latent_bodies=[_agent_body("do x", 10)])
    _setup_manual_ref_and_trailer(repo, trailer)

    (tmp_path / "a.txt").write_text("one\nedit\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "My change")

    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "My change" in msg and "# aGiTrack Metadata" in msg  # folded, one commit
    assert repo.rev_parse("refs/agitrack/manual/s") == repo.rev_parse("HEAD")  # ref reset
    assert (repo.repo / ".agitrack" / "manual-pending-trailer").read_text() == ""  # cleared
    # Exactly one commit was added (no separate cover commit).
    assert len(_git(repo, "log", "--format=%H").split()) == 2


def test_hook_leaves_commit_untouched_when_no_pending_turns(tmp_path):
    # With no pending AI turns the pre-rendered trailer is empty, so the prepare-commit-msg
    # hook's `[ -s "$_trailer" ]` guard appends nothing: a purely human commit stays a plain
    # commit with zero aGiTrack footprint (no cover, no attribution).
    repo = _init_repo(tmp_path)
    assert git_hooks.install_manual_commit_hooks(repo.repo / ".git" / "hooks")
    trailer = build_manual_squash_trailer(agitrack_session_id="s", latent_bodies=[])  # no turns
    assert trailer == ""
    _setup_manual_ref_and_trailer(repo, trailer)

    (tmp_path / "a.txt").write_text("one\nhuman only\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "My hand-written change")

    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "My hand-written change" in msg
    assert "# aGiTrack Metadata" not in msg  # untouched — no footprint
    assert "commit_type" not in msg


def test_prepare_commit_msg_hook_is_idempotent_and_skips_amend(tmp_path):
    repo = _init_repo(tmp_path)
    git_hooks.install_manual_commit_hooks(repo.repo / ".git" / "hooks")
    _setup_manual_ref_and_trailer(
        repo, build_manual_squash_trailer(agitrack_session_id="s", latent_bodies=[_agent_body("x", 1)])
    )
    (tmp_path / "a.txt").write_text("one\nedit\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "change")
    before = _git(repo, "log", "-1", "--format=%B", "HEAD")
    # An amend (source "commit") must be skipped, so the trailer is not appended again.
    _git(repo, "commit", "--amend", "--no-edit")
    after = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert before == after  # amend left the folded message unchanged


def test_manual_hooks_install_remove_preserves_existing_hook(tmp_path):
    repo = _init_repo(tmp_path)
    hooks_dir = repo.repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    existing = hooks_dir / "post-commit"
    existing.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    existing.chmod(0o755)

    git_hooks.install_manual_commit_hooks(hooks_dir)
    assert (hooks_dir / "post-commit.agitrack-orig").read_text() == "#!/bin/sh\necho mine\n"

    git_hooks.remove_manual_commit_hooks(hooks_dir)
    assert existing.read_text() == "#!/bin/sh\necho mine\n"  # restored
    assert not (hooks_dir / "prepare-commit-msg").exists()


def test_autotrack_precommit_hook_install_remove_and_chain(tmp_path):
    import sys as _sys

    repo = _init_repo(tmp_path)
    hooks_dir = repo.repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    # A pre-existing project pre-commit hook must be preserved (chained), then restored on removal.
    existing = hooks_dir / "pre-commit"
    existing.write_text("#!/bin/sh\necho project\n", encoding="utf-8")
    existing.chmod(0o755)

    assert git_hooks.install_autotrack_precommit_hook(hooks_dir, python_exe=_sys.executable, repo_root=str(repo.repo))
    hook = (hooks_dir / "pre-commit").read_text()
    assert git_hooks.is_autotrack_hook(hooks_dir / "pre-commit")
    assert "--precommit-sync" in hook and _sys.executable in hook and str(repo.repo) in hook
    assert (hooks_dir / "pre-commit.agitrack-orig").read_text() == "#!/bin/sh\necho project\n"

    git_hooks.remove_autotrack_precommit_hook(hooks_dir)
    assert (hooks_dir / "pre-commit").read_text() == "#!/bin/sh\necho project\n"  # restored


def test_remove_all_installed_hooks_removes_everything_and_restores_chains(tmp_path):
    import sys as _sys

    repo = _init_repo(tmp_path)
    hooks_dir = repo.repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    # A pre-existing project pre-commit hook to prove chaining is restored.
    (hooks_dir / "pre-commit").write_text("#!/bin/sh\necho project\n", encoding="utf-8")
    (hooks_dir / "pre-commit").chmod(0o755)
    # Install all of aGiTrack's hooks.
    git_hooks.install_autotrack_precommit_hook(hooks_dir, python_exe=_sys.executable, repo_root=str(repo.repo))
    git_hooks.install_manual_commit_hooks(hooks_dir)
    assert git_hooks.is_autotrack_hook(hooks_dir / "pre-commit")

    removed = git_hooks.remove_all_installed_hooks(hooks_dir)

    assert set(removed) == {"pre-commit", "prepare-commit-msg", "post-commit"}
    assert (hooks_dir / "pre-commit").read_text() == "#!/bin/sh\necho project\n"  # project hook restored
    assert not (hooks_dir / "prepare-commit-msg").exists()
    assert not (hooks_dir / "post-commit").exists()


def test_remove_all_installed_hooks_noop_when_none(tmp_path):
    repo = _init_repo(tmp_path)
    hooks_dir = repo.repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    assert git_hooks.remove_all_installed_hooks(hooks_dir) == []


def test_autotrack_hook_is_a_noop_inside_a_worktree():
    # The hook script must skip (do nothing) when the commit is inside a linked worktree, so it
    # never fights aGiTrack's own worktree-mode handling.
    script = git_hooks._autotrack_precommit_script("/usr/bin/python3", "/repo")
    assert "*/worktrees/*)" in script and "--precommit-sync" in script


# --- CommitEngine manual sink ----------------------------------------------


class _ManualSink:
    """The latent gate/record closures the runner injects, over a real GitRepo."""

    def __init__(self, repo: GitRepo, ref: str):
        self.repo, self.ref, self._tree = repo, ref, None

    def gate(self) -> bool:
        self._tree = self.repo.snapshot_worktree_tree()
        tip = self.repo.ref_sha(self.ref)
        base = self.repo.rev_parse(f"{tip or 'HEAD'}^{{tree}}")
        return self._tree != base

    def record(self, message: str):
        tree, self._tree = self._tree, None
        tip = self.repo.ref_sha(self.ref)
        parent = tip or self.repo.rev_parse("HEAD")
        sha = self.repo.commit_tree(tree, parents=[parent], message=message)
        self.repo.update_ref(self.ref, sha)
        return self.repo.short_sha(sha)


def _turn(prompt: str, response: str) -> SessionTurn:
    return SessionTurn("uid", "aid", prompt, response, TokenUsage(total=6, output=5, input=1), None, complete=True)


def test_commit_engine_manual_sink_records_latent_without_moving_head(tmp_path):
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path)
    ref = f"refs/agitrack/manual/{state.session_id}"
    sink = _ManualSink(repo, ref)
    head = repo.rev_parse("HEAD")
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")

    committed = CommitEngine(repo, state).commit_turns(
        turns=[_turn("do it", "done")],
        backend="claude",
        backend_session_id="s1",
        model="opus",
        stage_untracked_fn=lambda r, s: None,
        manual_gate_fn=sink.gate,
        manual_record_fn=sink.record,
    )

    assert committed is True
    assert repo.rev_parse("HEAD") == head  # HEAD never moved
    tip = repo.ref_sha(ref)
    assert tip and tip != head  # a latent commit landed on the side ref
    assert "# aGiTrack Metadata" in repo.commit_message(tip)


def test_commit_engine_manual_sink_records_nothing_for_a_noop_turn(tmp_path):
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path)
    ref = f"refs/agitrack/manual/{state.session_id}"
    sink = _ManualSink(repo, ref)  # working tree unchanged since HEAD

    committed = CommitEngine(repo, state).commit_turns(
        turns=[_turn("noop", "nothing to do")],
        backend="claude",
        backend_session_id="s1",
        model="opus",
        stage_untracked_fn=lambda r, s: None,
        manual_gate_fn=sink.gate,
        manual_record_fn=sink.record,
    )

    assert committed is False
    assert repo.ref_sha(ref) is None  # nothing recorded


# --- dashboard pending turns ------------------------------------------------


def test_collect_manual_pending_surfaces_turns(tmp_path):
    repo = _init_repo(tmp_path)
    head = repo.rev_parse("HEAD")
    (tmp_path / "a.txt").write_text("one\nA\n", encoding="utf-8")
    c1 = repo.commit_tree(repo.snapshot_worktree_tree(), parents=[head], message=_agent_body("A", 70))
    repo.update_ref("refs/agitrack/manual/sid", c1)

    pending = collect_manual_pending(repo)
    assert [p.pending for p in pending] == [True]
    assert pending[0].kind == "agent"

    dash = build_dashboard(repo, "HEAD")
    assert any(s.pending for s in dash.stats)  # surfaced in the dashboard timeline


# --- config toggle ----------------------------------------------------------


def test_manual_commits_config_default_off_and_settable(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg = GlobalConfig(path=cfg_path)
    assert cfg.manual_commits is False  # opt-in
    cfg.manual_commits = True
    assert GlobalConfig(path=cfg_path).manual_commits is True  # persisted


def test_settings_menu_exposes_manual_commits():
    from tests.proxy_helpers import make_runner

    specs = make_runner()._settings_specs()
    entry = next((s for s in specs if s["key"] == "manual_commits"), None)
    assert entry is not None and entry["kind"] == "bool" and entry.get("restart") is True


# --- runner-level manual mode (the real ProxyRunner methods, not hand-rolled closures) ---


def _manual_runner(tmp_path):
    """A ProxyRunner wired for manual-commit mode over a REAL GitRepo, with the popup UI
    stubbed so the git-commit handler can run headless."""
    from tests.proxy_helpers import make_runner

    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path)
    runner = make_runner(
        repo=repo,
        state=state,
        base_repo=repo,
        _manual_commits=True,
        _use_worktrees=False,
        _base_branch=repo.current_branch(),
    )
    runner._review_untracked_popup = lambda *a, **k: ""
    runner._prompt_popup = lambda *a, **k: "my message"
    runner._set_message = lambda *a, **k: None
    runner._render = lambda *a, **k: None
    return runner, repo, state


def _noworktree_auto_runner(tmp_path):
    """A ProxyRunner wired for no-worktree AUTO mode (not manual): it records turns latently and
    folds them into commits itself, and the prepare-commit-msg hook folds the agent's OWN commits."""
    from tests.proxy_helpers import make_runner

    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path)
    runner = make_runner(
        repo=repo,
        state=state,
        base_repo=repo,
        _manual_commits=False,
        _use_worktrees=False,
        _base_branch=repo.current_branch(),
    )
    runner._review_untracked_popup = lambda *a, **k: ""
    runner._set_message = lambda *a, **k: None
    runner._render = lambda *a, **k: None
    return runner, repo, state


def test_noworktree_auto_is_latent_but_not_manual(tmp_path):
    runner, repo, _ = _noworktree_auto_runner(tmp_path)
    assert runner._latent_tracking is True  # no-worktree ⇒ latent record + fold
    assert runner._noworktree_auto is True  # auto (not manual) ⇒ aGiTrack folds itself
    assert runner._manual_commits is False


def test_noworktree_auto_folds_latent_turn_into_commit(tmp_path):
    runner, repo, state = _noworktree_auto_runner(tmp_path)
    runner._setup_manual_commit_mode()  # installs the fold hooks + renders the trailer
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("do x", 20))  # a turn recorded latently (HEAD frozen)
    head_before = repo.rev_parse("HEAD")

    runner._auto_fold_latent_pending()  # aGiTrack commits it itself — no user action, no cover

    assert repo.rev_parse("HEAD") != head_before
    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    # A CLEAN agent commit (subject = the prompt, one agent metadata block) — NOT the manual
    # squash-into-a-user-commit format with a generic subject and a spurious commit_type: user.
    assert msg.startswith("<aGiTrack> do x")
    assert "commit agent turns" not in msg and "commit_type: user" not in msg
    assert msg.count("# aGiTrack Metadata") == 1 and "commit_type: agent" in msg
    assert repo.ref_sha(runner._manual_ref()) == repo.rev_parse("HEAD")  # ref reset
    assert runner._manual_pending_count() == 0


def test_noworktree_auto_agent_selfcommit_folds_via_hook_no_cover(tmp_path):
    runner, repo, state = _noworktree_auto_runner(tmp_path)
    runner._setup_manual_commit_mode()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("do x", 20))
    n_before = len(_git(repo, "log", "--format=%H").split())

    # The AGENT commits its own work: the installed prepare-commit-msg hook folds the pending
    # tracking straight into THAT commit (single commit), and post-commit resets the ref.
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "agent's own commit")

    runner._auto_fold_latent_pending()  # clean tree ⇒ nothing more to do (no separate cover)

    assert len(_git(repo, "log", "--format=%H").split()) == n_before + 1  # ONLY the agent's commit
    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "agent's own commit" in msg and msg.count("# aGiTrack Metadata") == 2  # user block + turn


def test_noworktree_auto_reconcile_covers_when_hook_unavailable(tmp_path):
    # Backup path: with the fold hook not installed (custom core.hooksPath), an agent/user commit
    # is covered by a metadata-only cover commit instead — cover is the backup, per the design.
    runner, repo, state = _noworktree_auto_runner(tmp_path)
    runner._manual_hooks_installed = False
    runner._manual_last_head = repo.rev_parse("HEAD")
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("do y", 15))
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "external commit")
    user_head = repo.rev_parse("HEAD")

    runner._reconcile_manual_external_commit()

    cover = repo.rev_parse("HEAD")
    assert cover != user_head and repo.parents(cover)[0] == user_head  # cover on top of the commit
    assert "# aGiTrack Metadata" in repo.commit_message(cover)


def test_runner_manual_gate_and_record_freeze_head(tmp_path):
    runner, repo, _ = _manual_runner(tmp_path)
    head = repo.rev_parse("HEAD")
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")

    assert runner._manual_gate() is True
    sha = runner._manual_record("<aGiTrack> t\n\n# aGiTrack Metadata\ncommit_type: agent\nbackend: claude\n")

    assert sha is not None
    assert repo.rev_parse("HEAD") == head  # never moved
    assert repo.ref_sha(runner._manual_ref()) is not None
    # The trailer + ref-name files the hook reads were rendered.
    agit = repo.repo / ".agitrack"
    assert (agit / "manual-ref").read_text().strip() == runner._manual_ref()
    assert "# aGiTrack Metadata" in (agit / "manual-pending-trailer").read_text()


def test_runner_manual_gate_false_when_tree_unchanged(tmp_path):
    runner, repo, _ = _manual_runner(tmp_path)
    assert runner._manual_gate() is False  # clean tree ⇒ nothing to record


def test_manual_turn_marks_activity_even_without_tree_change(tmp_path):
    # A planning/Q&A manual turn produces no net working-tree change, so the manual
    # gate skips recording a latent commit and on_commit_fn never fires. It is still
    # genuine session activity (the user conversed, tokens were spent), so it must be
    # marked shareable — otherwise _auto_share_on_exit silently skips the session and
    # "the last manual session couldn't be shared."
    runner, repo, state = _manual_runner(tmp_path)
    assert runner._manual_gate() is False  # clean tree, so nothing will be recorded

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[SessionTurn("u1", "a1", "explain the code", "here's how", TokenUsage(total=1, output=1), None)],
        backend="claude",
        backend_session_id="bs",
        model="opus",
        quiet=True,
    )

    assert committed is False  # no latent commit (tree unchanged)
    assert runner.state.session_id in runner._sessions_with_activity  # but still active


def test_runner_git_commit_menu_folds_pending_and_resets_ref(tmp_path):
    runner, repo, state = _manual_runner(tmp_path)
    # One pending latent turn.
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("do x", 20))
    # The user then edits and commits via the git-commit menu handler.
    (tmp_path / "a.txt").write_text("one\nagent\nuser\n", encoding="utf-8")

    created = runner._create_user_commit_popup(repo=repo, state=state, include_declined=True)

    assert created is True
    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "my message" in msg and msg.count("# aGiTrack Metadata") == 2  # user block + 1 turn
    assert repo.rev_parse(runner._manual_ref()) == repo.rev_parse("HEAD")  # ref reset
    assert runner._manual_last_head == repo.rev_parse("HEAD")
    assert len(_git(repo, "log", "--format=%H").split()) == 2  # init + one folded commit


def test_pre_agent_flow_forwards_immediately_without_checking_in_manual_mode(tmp_path):
    # A dirty tree (the agent's latently-tracked work) must NOT trigger the "checking existing
    # git changes…" pre-flight parse/defer — the prompt goes straight to the backend.
    runner, repo, _ = _manual_runner(tmp_path)
    (tmp_path / "a.txt").write_text("one\nagent working\n", encoding="utf-8")
    messages: list[str] = []
    runner._set_message = lambda msg, **k: messages.append(msg)
    runner._clear_agent_in_flight_if_idle = lambda: None
    runner._finish_agent_parse_if_ready = lambda quiet=False: None
    runner._agent_is_active = lambda: False
    recorded: list[str] = []
    runner._record_user_prompt = lambda text: recorded.append(text)
    started: list[int] = []
    runner._start_agent_parse = lambda: started.append(1) or True  # must NOT run

    result = runner._pre_agent_commit_if_needed("do something")

    assert result is True  # forwarded immediately, not deferred
    assert started == []  # no pre-flight parse
    assert recorded == ["do something"]
    assert not any("checking existing git changes" in m for m in messages)


def test_runner_base_user_edit_commit_is_suppressed_in_manual_mode(tmp_path):
    # The bug fix: aGiTrack must NOT auto-prompt to commit the (intentionally dirty) tree.
    runner, repo, _ = _manual_runner(tmp_path)
    (tmp_path / "a.txt").write_text("one\ndirty\n", encoding="utf-8")  # base tree dirty
    calls: list[int] = []
    runner._create_user_commit_popup = lambda *a, **k: calls.append(1) or True

    runner._commit_base_user_edits_if_needed()

    assert calls == []  # never prompted


def test_runner_reconcile_covers_external_commit_without_hook(tmp_path):
    runner, repo, _ = _manual_runner(tmp_path)
    runner._manual_hooks_installed = False  # no-hook fallback path
    runner._manual_last_head = repo.rev_parse("HEAD")
    # A pending latent turn, then the user commits OUTSIDE aGiTrack (no fold hook ran).
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("do y", 15))
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "external commit")
    user_head = repo.rev_parse("HEAD")
    user_tree = repo.rev_parse("HEAD^{tree}")

    runner._reconcile_manual_external_commit()

    cover = repo.rev_parse("HEAD")
    assert cover != user_head  # a cover commit was added on top
    assert repo.parents(cover)[0] == user_head  # first-parent = the user's own commit
    assert repo.rev_parse("HEAD^{tree}") == user_tree  # cover added NO diff of its own
    assert "# aGiTrack Metadata" in repo.commit_message(cover)  # carries the pending tracking
    assert repo.ref_sha(runner._manual_ref()) == cover  # ref reset


def test_runner_reconcile_is_noop_when_fold_hook_installed(tmp_path):
    # With the hook installed the fold already happened; the poll fallback must NOT also
    # add a cover commit (that would double the tracking).
    runner, repo, _ = _manual_runner(tmp_path)
    runner._manual_hooks_installed = True
    runner._manual_last_head = repo.rev_parse("HEAD")
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("t", 5))
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "ext")
    head_after_user = repo.rev_parse("HEAD")

    runner._reconcile_manual_external_commit()

    assert repo.rev_parse("HEAD") == head_after_user  # no cover added


def test_runner_reconcile_is_noop_when_head_unchanged(tmp_path):
    runner, repo, _ = _manual_runner(tmp_path)
    runner._manual_hooks_installed = False
    head = repo.rev_parse("HEAD")
    runner._manual_last_head = head  # HEAD hasn't moved since we last looked

    runner._reconcile_manual_external_commit()

    assert repo.rev_parse("HEAD") == head  # nothing to reconcile


def test_runner_git_commit_with_no_pending_turns_is_plain_user_commit(tmp_path):
    runner, repo, state = _manual_runner(tmp_path)
    (tmp_path / "a.txt").write_text("one\njust me\n", encoding="utf-8")  # only user edits, 0 turns

    created = runner._create_user_commit_popup(repo=repo, state=state, include_declined=True)

    assert created is True
    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    stat = _parse_commit("h", "me", "me@x", "1", msg)
    # No AI turns ⇒ no aGiTrack footprint at all (the commit is a plain, untracked user commit).
    assert stat.kind == "untracked"
    assert "# aGiTrack Metadata" not in msg
    assert repo.rev_parse(runner._manual_ref()) == repo.rev_parse("HEAD")  # ref still reset


def test_runner_recovery_resets_ref_after_external_commit_then_restart(tmp_path):
    # The user's scenario: pending turns, exit (hooks removed), commit OUTSIDE aGiTrack, restart.
    # The diverged latent ref must be dropped so its trace can't re-attach to a later commit —
    # and there is no git conflict (the ref is only reset, never merged).
    runner, repo, _ = _manual_runner(tmp_path)
    old_head = repo.rev_parse("HEAD")
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("t", 5))
    tip = repo.ref_sha(runner._manual_ref())
    assert not repo.is_ancestor(tip, old_head)  # latent chain diverges (turns not in HEAD)
    # User commits the working tree with a plain `git commit` (no fold hook ran).
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "external commit")
    new_head = repo.rev_parse("HEAD")
    assert not repo.is_ancestor(tip, new_head)  # ref still diverged from the new HEAD

    runner._setup_manual_commit_mode()  # restart

    assert repo.ref_sha(runner._manual_ref()) == new_head  # stale chain dropped
    assert runner._manual_pending_count() == 0


def test_runner_recovery_keeps_pending_turns_when_tree_dirty(tmp_path):
    # A normal mid-session restart: the agent's work is still uncommitted (tree dirty), so the
    # pending turns must be preserved and fold into the user's next commit.
    runner, repo, _ = _manual_runner(tmp_path)
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("t", 5))
    assert runner._manual_pending_count() == 1

    runner._setup_manual_commit_mode()  # restart with the agent's work still uncommitted

    assert runner._manual_pending_count() == 1  # preserved


def test_runner_setup_installs_hooks_and_resets_stale_ref(tmp_path):
    runner, repo, _ = _manual_runner(tmp_path)
    head0 = repo.rev_parse("HEAD")
    repo.update_ref(runner._manual_ref(), head0)  # stale ref left behind HEAD
    (tmp_path / "a.txt").write_text("one\nuser\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "user commit")  # HEAD now ahead of the stale ref

    runner._setup_manual_commit_mode()

    hooks = repo.repo / ".git" / "hooks"
    assert (hooks / "prepare-commit-msg").exists() and (hooks / "post-commit").exists()
    assert runner._manual_hooks_installed is True
    # Recovery: the stale ref (contained in HEAD) was reset to HEAD so old turns aren't re-folded.
    assert repo.ref_sha(runner._manual_ref()) == repo.rev_parse("HEAD")

    runner._teardown_manual_commit_mode()
    assert not (hooks / "prepare-commit-msg").exists()
    assert not (hooks / "post-commit").exists()


def test_manual_pending_bodies_fold_in_summary_note_when_available(tmp_path):
    # Metadata is written synchronously at record time; the LLM summary lands later as a note.
    # The fold must include the summary when it has arrived, and work fine when it hasn't.
    runner, repo, _ = _manual_runner(tmp_path)
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("do x", 5))
    tip = repo.ref_sha(runner._manual_ref())

    before = runner._manual_pending_bodies()
    assert "# aGiTrack Metadata" in before[0]  # metadata always present
    assert "Concise headline" not in before[0]  # no summary yet — omitted gracefully

    repo.notes_add(tip, "Concise headline\n\ndetail", namespace="agitrack/commit-summary")
    after = runner._manual_pending_bodies()
    assert "Concise headline" in after[0]  # folded in once it lands


def test_git_commit_menu_flushes_pending_turn_before_folding(tmp_path):
    # A turn that finished while the user opened the menu must be captured before the fold.
    runner, repo, state = _manual_runner(tmp_path)
    flushed: list[bool] = []
    runner._finish_agent_parse_if_ready = lambda quiet=False: flushed.append(quiet)
    (tmp_path / "a.txt").write_text("one\nuser\n", encoding="utf-8")

    runner._create_user_commit_popup(repo=repo, state=state, include_declined=True)

    assert flushed == [True]  # the parse/record flush ran before committing


def test_menu_commit_folds_summaries_and_dashboard_shows_newest_first(tmp_path):
    # End-to-end through the actual menu handler: each turn's LLM summary (attached as a note)
    # is folded into the commit, the raw message stays chronological, and the dashboard shows
    # the turns newest-first.
    from agitrack.metrics.web import dashboard_data

    runner, repo, state = _manual_runner(tmp_path)
    (tmp_path / "a.txt").write_text("one\nt1\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("first turn", 10))
    (tmp_path / "a.txt").write_text("one\nt1\nt2\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("second turn", 20))
    old, new = repo.log_shas("HEAD", repo.ref_sha(runner._manual_ref()))  # oldest, newest
    repo.notes_add(old, "Did the first thing", namespace="agitrack/commit-summary")
    repo.notes_add(new, "Did the second thing", namespace="agitrack/commit-summary")

    (tmp_path / "a.txt").write_text("one\nt1\nt2\nuser\n", encoding="utf-8")
    runner._create_user_commit_popup(repo=repo, state=state, include_declined=True)

    # Message: chronological (oldest-first), summaries folded in.
    msg_subjects = [
        c.subject
        for c in _parse_commit("h", "m", "m@x", "1", _git(repo, "log", "-1", "--format=%B", "HEAD")).constituents
        if c.kind == "agent"
    ]
    assert "first thing" in msg_subjects[0] and "second thing" in msg_subjects[1]
    # Dashboard: same commit shown newest-first.
    folded = next(c for c in dashboard_data(build_dashboard(repo))["commits"] if c["kind"] == "agent")
    disp = [p["subject"] for p in folded["parts"] if p["kind"] == "agent"]
    assert "second thing" in disp[0] and "first thing" in disp[1]


def test_runner_manual_pending_count(tmp_path):
    runner, repo, _ = _manual_runner(tmp_path)
    assert runner._manual_pending_count() == 0
    (tmp_path / "a.txt").write_text("one\nx\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("t", 1))
    assert runner._manual_pending_count() == 1


def test_exit_finalize_message_does_not_say_committing_in_manual_mode(tmp_path):
    runner, _, _ = _manual_runner(tmp_path)
    runner.sessions = []
    runner._agent_is_active = lambda: True  # a turn is in flight at exit

    msg = runner._describe_exit_finalize()

    assert msg is not None
    assert "recording the latest agent turn" in msg
    assert "committing" not in msg and "merging" not in msg  # never claims a branch commit


def test_exit_finalize_message_is_none_when_idle_in_manual_mode(tmp_path):
    runner, _, _ = _manual_runner(tmp_path)
    runner.sessions = []
    runner._agent_is_active = lambda: False
    assert runner._describe_exit_finalize() is None  # clean, silent exit


def test_exit_confirmation_reminds_to_commit_when_turns_pending(tmp_path):
    runner, repo, _ = _manual_runner(tmp_path)
    (tmp_path / "a.txt").write_text("one\nx\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("t", 1))
    captured: dict = {}
    runner._menu_label = lambda: "Ctrl-G"

    def fake_popup(title, opts):
        captured["title"] = title
        return "Yes, exit (Ctrl-C again)"

    runner._exit_confirmation_popup = fake_popup

    assert runner._confirm_exit() is True
    assert "uncommitted agent turn" in captured["title"]
    assert "git-commit" in captured["title"] and "won't" in captured["title"]


def test_exit_confirmation_has_no_reminder_when_nothing_pending(tmp_path):
    runner, _, _ = _manual_runner(tmp_path)
    captured: dict = {}

    def fake_popup(title, opts):
        captured["title"] = title
        return "No, keep working"

    runner._exit_confirmation_popup = fake_popup

    runner._confirm_exit()
    assert captured["title"] == "Exit aGiTrack?"  # plain prompt, no pending-turn reminder


def test_reset_stale_manual_ref_resets_on_clean_tree_keeps_on_dirty(tmp_path):
    runner, repo, _ = _manual_runner(tmp_path)
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("t", 1))
    # Dirty tree, diverged tip → real pending work, keep it.
    assert runner._reset_stale_manual_ref() is False
    assert runner._manual_pending_count() == 1
    # A commit lands (here, outside aGiTrack) → tree clean → the stale chain is dropped.
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "ext")
    assert runner._reset_stale_manual_ref() is True
    assert runner._manual_pending_count() == 0


def test_service_resets_ref_after_external_commit_midsession(tmp_path):
    # A commit made outside aGiTrack WHILE it runs: the fold hook already combined the pending
    # turns into it, so the poll must also drop the now-stale latent chain.
    runner, repo, _ = _manual_runner(tmp_path)
    runner._manual_hooks_installed = True
    runner._manual_last_head = repo.rev_parse("HEAD")
    runner._manual_poll_at = 0.0
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("t", 1))
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "external while running")

    runner._service_manual_commit_mode()

    assert runner._manual_pending_count() == 0  # stale chain dropped
    assert runner._manual_last_head == repo.rev_parse("HEAD")


def test_runner_service_refreshes_after_post_commit_signal(tmp_path):
    runner, repo, _ = _manual_runner(tmp_path)
    runner._manual_hooks_installed = True
    runner._manual_poll_at = 0.0  # bypass the throttle
    (repo.repo / ".agitrack").mkdir(exist_ok=True)
    (repo.repo / ".agitrack" / "manual-commit-signal").write_text("x", encoding="utf-8")

    runner._service_manual_commit_mode()  # must not raise; re-renders the trailer

    assert (repo.repo / ".agitrack" / "manual-pending-trailer").exists()
    assert runner._manual_last_head == repo.rev_parse("HEAD")
