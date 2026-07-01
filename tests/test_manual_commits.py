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


def test_manual_trailer_with_no_pending_turns_is_a_plain_user_commit():
    trailer = build_manual_squash_trailer(agitrack_session_id="sid", latent_bodies=[])
    folded = "Just my edit\n\n" + trailer
    stat = _parse_commit("def4567", "me", "me@x", "1700000000", folded)
    assert stat.kind == "user"  # still attributed to the session, not "untracked"


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
