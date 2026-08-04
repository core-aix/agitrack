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

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from agitrack.backends.base import TokenUsage
from agitrack.commits import ManualCommitTracker
from agitrack.commits.manual import prune_abandoned_refs
from agitrack.commits.message import (
    build_agent_commit_message,
    build_manual_squash_trailer,
    build_pending_trailer,
    is_fully_tracked_message,
)
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


def test_pending_trailer_prefers_completed_turns_over_in_flight():
    # A completed turn carries the full trace and token usage, so it always wins; the
    # in-flight block is strictly a fallback for when there is nothing else.
    in_flight = {"backend": "claude", "backend_session_id": "s1", "model": "m", "prompt": "do x"}
    trailer = build_pending_trailer(
        agitrack_session_id="sid", latent_bodies=[_agent_body("do x", 10)], in_flight=in_flight
    )
    assert "commit_type: user" in trailer  # the squash header
    assert "in_flight: true" not in trailer


def test_pending_trailer_attributes_an_agent_commit_made_mid_turn():
    # THE regression: the agent runs `git commit` itself before its turn ends. A turn only
    # becomes a pending latent turn once it COMPLETES, so there is nothing to fold — the
    # commit used to land with no aGiTrack metadata at all and its lines were attributed to
    # nobody. The in-flight block attributes it instead.
    in_flight = {"backend": "claude", "backend_session_id": "s1", "model": "claude-opus-4-8", "prompt": "do x"}
    trailer = build_pending_trailer(agitrack_session_id="sid", latent_bodies=[], in_flight=in_flight)

    stat = _parse_commit("abc1234", "me", "me@x", "1700000000", "Agent's own commit\n\n" + trailer)
    assert stat.kind == "agent"  # attributed, not "untracked"
    assert stat.backend == "claude" and stat.model == "claude-opus-4-8"
    assert stat.prompt == "do x"
    # No token counts: the same turn's completed record carries them, and counting them in
    # both places would double-count the turn.
    assert not stat.tokens
    assert "in_flight: true" in trailer


def test_in_flight_note_sits_inside_the_interaction_trace_section():
    # The explanatory note belongs INSIDE the "# Interaction Trace" section as a lead-in
    # blockquote (like the session-event / covered-commits notes), not floating above the
    # header. It leads the trace, above the running prompt, and stays above the metadata block.
    in_flight = {"backend": "claude", "backend_session_id": "s1", "model": "m", "prompt": "do x"}
    trailer = build_pending_trailer(agitrack_session_id="sid", latent_bodies=[], in_flight=in_flight)

    note_at = trailer.index("The agent committed this itself")
    assert trailer.index("# Interaction Trace") < note_at  # note is INSIDE the trace section
    assert note_at < trailer.index("## User")  # ...leading it, above the running prompt
    assert note_at < trailer.index("# aGiTrack Metadata")  # ...and above the metadata block


def test_pending_trailer_is_empty_without_pending_or_in_flight_work():
    assert build_pending_trailer(agitrack_session_id="sid", latent_bodies=[], in_flight=None) == ""


def test_in_flight_attribution_requires_both_a_running_turn_and_tree_changes(tmp_path):
    # Both conditions are load-bearing. A running turn that has not touched the tree means the
    # commit holds no AI work, and stamping it would break the "no AI work ⇒ no footprint"
    # promise that keeps a human's own commit clean.
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    facts = {"backend": "claude", "backend_session_id": "s1", "model": "m", "prompt": "do x"}
    running: dict | None = dict(facts)
    tracker = ManualCommitTracker(repo, repo, state, in_flight_fn=lambda: running)

    assert tracker.in_flight_attribution() is None  # turn running, but the tree is clean

    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")
    assert tracker.in_flight_attribution() == facts  # running + real changes

    running = None
    assert tracker.in_flight_attribution() is None  # changes, but no turn running (human's own)

    # No supplier at all (a driver that never reports in-flight state) stays inert.
    assert ManualCommitTracker(repo, repo, state).in_flight_attribution() is None


# --- the trace between two commits must be WHOLE, across session and branch switches ---------


def _record(tracker, name: str, tokens: int) -> None:
    tracker.gate()
    tracker.record(
        f"<aGiTrack> {name}\n\n# Interaction Trace\n\n## User\n\n{name} prompt\n\n"
        f"# aGiTrack Metadata\ncommit_type: agent\ntokens_since_last_commit_output: {tokens}\n"
    )


def test_a_session_switch_does_not_orphan_the_turns_recorded_before_it(tmp_path):
    # THE gap. A session's turns live on `refs/agitrack/manual/<agitrack_session_id>`, and that id
    # changes when the user starts a new session (or a backend switch opens one). Folding only the
    # CURRENT session's ref silently dropped every turn recorded before the switch, so the trace
    # between the previous commit and the new one had a hole in it. Manual mode is always
    # no-worktree, so all those sessions edited the same tree and the commit captures all of their
    # work — the trace must cover all of them.
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    first = ManualCommitTracker(repo, repo, state)
    first.setup()
    (tmp_path / "a.txt").write_text("one\nturn one\n", encoding="utf-8")
    _record(first, "one", 100)
    first_ref = first.ref()  # captured before the switch: `first` shares the mutated state object

    state.new_agitrack_session_id()  # the user starts a new session
    second = ManualCommitTracker(repo, repo, AgitrackState(tmp_path, default_backend="claude"))
    assert second.ref() != first_ref
    (tmp_path / "a.txt").write_text("one\nturn one\nturn two\n", encoding="utf-8")
    _record(second, "two", 200)
    second.render_trailer()

    assert second.pending_count() == 2  # BOTH sessions' turns are pending
    trailer = (tmp_path / ".agitrack" / "manual-pending-trailer").read_text(encoding="utf-8")
    assert "one prompt" in trailer and "two prompt" in trailer
    # Every ref the commit will fold is listed for the post-commit hook to advance, or the older
    # session's turns would stay "pending" and fold a second time into the NEXT commit.
    listed = (tmp_path / ".agitrack" / "manual-ref").read_text(encoding="utf-8").split()
    assert set(listed) == {first_ref, second.ref()}


def test_work_already_folded_on_another_branch_is_never_folded_again(tmp_path):
    # THE double-count. After a fold the post-commit hook advances the latent ref to the new
    # commit — a real branch commit. Switching to a branch that does not contain it made a plain
    # `HEAD..ref` walk report that already-folded work as pending, so it folded a SECOND time:
    # the trace duplicated and its tokens counted twice (exactly the two-branches-then-merge case).
    # "Reachable from no branch" is the exact test for "still latent".
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    state.ensure_local_ignore()  # as a real run does, so `git add -A` never stages .agitrack/
    tracker = ManualCommitTracker(repo, repo, state)
    tracker.setup()
    base = repo.current_branch()  # captured, not assumed: `init.defaultBranch` varies per machine

    _git(repo, "checkout", "-qb", "featX")
    (tmp_path / "x.py").write_text("X = 1\n", encoding="utf-8")
    _record(tracker, "X", 100)
    tracker.render_trailer()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "commit on X")
    assert "X prompt" in _git(repo, "log", "-1", "--format=%B")

    # A different branch, diverged from before X's commit.
    _git(repo, "checkout", "-q", base)
    _git(repo, "checkout", "-qb", "featY")
    tracker.render_trailer()
    assert tracker.pending_count() == 0  # X's work already landed on a branch — not pending

    (tmp_path / "y.py").write_text("Y = 1\n", encoding="utf-8")
    _record(tracker, "Y", 200)
    tracker.render_trailer()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "commit on Y")

    message = _git(repo, "log", "-1", "--format=%B")
    assert "Y prompt" in message
    assert "X prompt" not in message  # X's turn is NOT folded a second time
    assert message.count("tokens_since_last_commit_output") == 1


def test_unlanded_commits_reports_only_what_no_branch_contains(tmp_path):
    # The primitive the fold rests on, pinned directly.
    repo = _init_repo(tmp_path)
    head = repo.rev_parse("HEAD")
    latent = repo.commit_tree(repo.rev_parse("HEAD^{tree}"), parents=[head], message="latent turn")
    repo.update_ref("refs/agitrack/manual/s1", latent)

    assert repo.unlanded_commits("refs/agitrack/manual/s1") == [latent]  # on no branch yet

    _git(repo, "branch", "landed", latent)  # now a branch contains it
    assert repo.unlanded_commits("refs/agitrack/manual/s1") == []


def _noworktree_proxy(tmp_path, *, manual: bool):
    """An interactive proxy runner in no-worktree mode over a real repo — the mode that folds
    via the prepare-commit-msg hook (manual OR auto; worktree mode covers instead)."""
    from proxy_helpers import make_runner

    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    runner = make_runner(
        repo=repo, base_repo=repo, state=state, _use_worktrees=False, _manual_commits=manual, worktree=None
    )
    return runner, repo


@pytest.mark.parametrize("manual", [True, False], ids=["manual-commits", "auto-commits"])
def test_proxy_no_worktree_attributes_an_agent_commit_made_mid_turn(tmp_path, manual):
    # Same regression as the daemon's, on the OTHER driver. The proxy has no flush path — the
    # pre-commit hook deliberately skips nudging an interactive TUI — so it must render the
    # trailer when it LEARNS a turn started, not only when one completes.
    runner, repo = _noworktree_proxy(tmp_path, manual=manual)
    assert git_hooks.install_manual_commit_hooks(repo.repo / ".git" / "hooks")
    (tmp_path / "a.txt").write_text("one\nagent work\n", encoding="utf-8")

    runner._note_in_flight({"backend": "claude", "backend_session_id": "s1", "model": "m", "prompt": "do x"})

    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "Agent's own commit")
    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "commit_type: agent" in msg and "in_flight: true" in msg
    assert "do x" in msg


@pytest.mark.parametrize("backend_name", ["claude", "opencode"])
@pytest.mark.parametrize("manual", [True, False], ids=["manual-commits", "auto-commits"])
def test_proxy_no_worktree_latent_turn_covers_a_mid_turn_agent_commit(tmp_path, manual, backend_name):
    # Parity with the daemon: when the agent commits mid-turn in a no-worktree interactive
    # session and then keeps editing, the completed turn records LATENTLY (dirty tree) but its
    # token count spans the mid-turn commit's work — so the recorded body must list that commit
    # in covered_commits and carry the explanatory note. The latent path used to force
    # backend_commits empty, so the mid-turn commit was attributed to nobody. Backend-agnostic:
    # the metadata format and detection are shared, so it holds for Claude and OpenCode alike.
    runner, repo = _noworktree_proxy(tmp_path, manual=manual)
    runner.state.backend = backend_name
    assert git_hooks.install_manual_commit_hooks(repo.repo / ".git" / "hooks")
    runner._noworktree_base_head = repo.rev_parse("HEAD")  # anchor set at startup in the real flow
    runner._start_commit_summary = lambda *a, **k: None  # never spawn a real summarizer
    runner.untracked_before_turn = set()

    # Mid-turn: the agent edits and commits its own work (the hook folds an in-flight block).
    (tmp_path / "a.txt").write_text("one\nmid-turn work\n", encoding="utf-8")
    runner._note_in_flight({"backend": backend_name, "backend_session_id": "s1", "model": "m", "prompt": "do x"})
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "Agent's own mid-turn commit")
    agent_commit = repo.rev_parse("HEAD")
    assert "in_flight: true" in _git(repo, "log", "-1", "--format=%B", agent_commit)
    runner._reset_stale_manual_ref()  # what the post-commit hook / service does

    # The agent keeps working (more uncommitted edits), then the turn completes.
    (tmp_path / "a.txt").write_text("one\nmid-turn work\nstill more\n", encoding="utf-8")
    runner._note_in_flight(None)  # the completed record takes over
    committed = runner._create_agent_commit_from_turns_popup(
        turns=[SessionTurn("u1", "a1", "do x", "done", TokenUsage(total=120, output=120), "m")],
        backend=backend_name,
        backend_session_id="s1",
        model="m",
        quiet=True,
    )

    assert committed is True
    assert repo.rev_parse("HEAD") == agent_commit  # latent record never moved HEAD
    assert runner._noworktree_base_head == agent_commit  # anchor advanced past the covered commit
    body = runner._manual_pending_bodies()[-1]
    assert "covered_commits:" in body and agent_commit[:7] in body
    assert f"backend: {backend_name}" in body  # recorded for whichever backend is driving
    assert "This commit accounts" in body  # the explanatory note


@pytest.mark.parametrize("backend_name", ["claude", "opencode"])
def test_proxy_no_worktree_stop_finalize_does_not_cover_an_unfinished_turn(tmp_path, backend_name):
    # The constraint, interactive side: the exit finalize (require_complete=False) keeps a
    # still-running turn to capture in-flight work, but it must not attribute the agent's mid-turn
    # commit before the turn's final message. So an unfinished turn leaves that commit OUT of
    # covered_commits and leaves the cover anchor where it was — the completed turn covers it later.
    runner, repo = _noworktree_proxy(tmp_path, manual=True)
    runner.state.backend = backend_name
    assert git_hooks.install_manual_commit_hooks(repo.repo / ".git" / "hooks")
    base = repo.rev_parse("HEAD")
    runner._noworktree_base_head = base
    runner._start_commit_summary = lambda *a, **k: None
    runner.untracked_before_turn = set()

    # Mid-turn: the agent commits its own work (the hook folds an in-flight block).
    (tmp_path / "a.txt").write_text("one\nmid-turn work\n", encoding="utf-8")
    runner._note_in_flight({"backend": backend_name, "backend_session_id": "s1", "model": "m", "prompt": "do x"})
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "Agent's own mid-turn commit")
    agent_commit = repo.rev_parse("HEAD")
    runner._reset_stale_manual_ref()

    # More uncommitted edits, and the turn is STILL unfinished at stop time (complete=False).
    (tmp_path / "a.txt").write_text("one\nmid-turn work\nstill more\n", encoding="utf-8")
    runner._create_agent_commit_from_turns_popup(
        turns=[SessionTurn("u1", "a1", "do x", "", TokenUsage(total=0, output=0), "m", complete=False)],
        backend=backend_name,
        backend_session_id="s1",
        model="m",
        quiet=True,
    )

    assert repo.rev_parse("HEAD") == agent_commit  # no cover placed on top
    assert runner._noworktree_base_head == base  # anchor NOT advanced past the unfinished turn
    for body in runner._manual_pending_bodies():
        assert agent_commit[:7] not in body  # the mid-turn commit is not yet attributed


def test_proxy_no_worktree_leaves_a_human_commit_alone(tmp_path):
    runner, repo = _noworktree_proxy(tmp_path, manual=True)
    assert git_hooks.install_manual_commit_hooks(repo.repo / ".git" / "hooks")
    (tmp_path / "a.txt").write_text("one\nmy own edit\n", encoding="utf-8")

    runner._note_in_flight(None)  # no turn running

    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "My hand-written change")
    assert "# aGiTrack Metadata" not in _git(repo, "log", "-1", "--format=%B", "HEAD")


def test_worktree_proxy_renders_no_trailer(tmp_path):
    # Worktree mode is the one mode that does NOT fold via the hook: it commits per-branch and
    # attributes an agent's own commits with a cover instead, so the trailer stays out of it.
    from proxy_helpers import make_runner

    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    runner = make_runner(repo=repo, base_repo=repo, state=state, _use_worktrees=True, worktree=tmp_path)
    (tmp_path / "a.txt").write_text("one\nagent work\n", encoding="utf-8")

    runner._note_in_flight({"backend": "claude", "backend_session_id": "s1", "model": "m", "prompt": "do x"})

    assert not (repo.repo / ".agitrack" / "manual-pending-trailer").exists()


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


def test_post_commit_hook_resets_refs_written_with_windows_line_endings(tmp_path):
    # Windows-only regression, pinned on every platform. `Path.write_text` uses newline=None, which
    # rewrites "\n" as "\r\n" on Windows, and the hook's `read -r` keeps that CR — so the ref name
    # was invalid, `git update-ref` silently refused, the latent refs were never advanced, and the
    # turns folded a SECOND time into the next commit. aGiTrack now writes these hook-read files
    # with LF, and the hook strips a trailing CR so a file left by an OLDER aGiTrack still resets.
    repo = _init_repo(tmp_path)
    assert git_hooks.install_manual_commit_hooks(repo.repo / ".git" / "hooks")
    trailer = build_manual_squash_trailer(agitrack_session_id="s", latent_bodies=[_agent_body("do x", 10)])
    _setup_manual_ref_and_trailer(repo, trailer)
    agit = repo.repo / ".agitrack"
    # Exactly what an older aGiTrack on Windows left behind: CRLF endings, two refs.
    repo.update_ref("refs/agitrack/manual/other", repo.rev_parse("HEAD"))
    (agit / "manual-ref").write_bytes(b"refs/agitrack/manual/s\r\nrefs/agitrack/manual/other\r\n")

    (tmp_path / "a.txt").write_text("one\nedit\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "My change")

    head = repo.rev_parse("HEAD")
    assert repo.rev_parse("refs/agitrack/manual/s") == head  # CR tolerated, ref advanced
    assert repo.rev_parse("refs/agitrack/manual/other") == head  # every listed ref, not just the first


def test_hook_read_files_are_written_with_lf_on_every_platform(tmp_path):
    # The files the sh hooks read must never carry CRLF: `manual-ref` a line at a time, and
    # `manual-pending-trailer` straight into the commit message.
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    tracker = ManualCommitTracker(repo, repo, state)
    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")
    tracker.gate()
    tracker.record("<aGiTrack> t\n\n# aGiTrack Metadata\ncommit_type: agent\n")
    tracker.render_trailer()

    for name in ("manual-ref", "manual-pending-trailer"):
        assert b"\r\n" not in (tmp_path / ".agitrack" / name).read_bytes(), name


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

    assert git_hooks.install_autotrack_precommit_hook(
        hooks_dir, invoke=[_sys.executable, "-m", "agitrack"], repo_root=str(repo.repo)
    )
    hook = (hooks_dir / "pre-commit").read_text()
    assert git_hooks.is_autotrack_hook(hooks_dir / "pre-commit")
    assert "--precommit-sync" in hook and _sys.executable in hook and str(repo.repo) in hook
    assert "|| agitrack --precommit-sync" in hook  # PATH fallback so it calls the CURRENT aGiTrack
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
    git_hooks.install_autotrack_precommit_hook(
        hooks_dir, invoke=[_sys.executable, "-m", "agitrack"], repo_root=str(repo.repo)
    )
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
    script = git_hooks._autotrack_precommit_script(["/usr/bin/python3", "-m", "agitrack"], "/repo", "1.2.3")
    assert "*/worktrees/*)" in script and "--precommit-sync" in script


def test_autotrack_hook_is_frozen_aware_and_has_path_fallback():
    # Frozen (MSI) build: the exe is run directly (`-m agitrack` is invalid there); a normal build
    # runs `python -m agitrack`. Both bake a PATH fallback so a self-update's new binary is used.
    frozen = git_hooks._autotrack_precommit_script(["/opt/agitrack.exe"], "/repo", "1.2.3")
    assert "'/opt/agitrack.exe' --precommit-sync" in frozen and "-m agitrack --precommit-sync" not in frozen
    assert "|| agitrack --precommit-sync" in frozen


def test_autotrack_hook_stamps_version_and_replaces_older_schema(tmp_path):
    import sys as _sys

    repo = _init_repo(tmp_path)
    hooks_dir = repo.repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    invoke = [_sys.executable, "-m", "agitrack"]

    # First install stamps the version into the hook.
    git_hooks.install_autotrack_precommit_hook(hooks_dir, invoke=invoke, repo_root=str(repo.repo), version="1.0.0")
    hook = hooks_dir / "pre-commit"
    assert git_hooks.autotrack_hook_version(hook) == "1.0.0"
    assert "# AGITRACK-AUTOTRACK-VERSION 1.0.0" in hook.read_text()

    # A newer aGiTrack removes the old hook and installs the current schema (version advances).
    git_hooks.install_autotrack_precommit_hook(hooks_dir, invoke=invoke, repo_root=str(repo.repo), version="1.2.0")
    assert git_hooks.autotrack_hook_version(hook) == "1.2.0"

    # A same/older version is a no-op replacement but still leaves a valid, current hook.
    git_hooks.install_autotrack_precommit_hook(hooks_dir, invoke=invoke, repo_root=str(repo.repo), version="1.2.0")
    assert git_hooks.autotrack_hook_version(hook) == "1.2.0"
    assert git_hooks.is_autotrack_hook(hook)


def test_autotrack_hook_replacement_preserves_chained_project_hook(tmp_path):
    import sys as _sys

    repo = _init_repo(tmp_path)
    hooks_dir = repo.repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    (hooks_dir / "pre-commit").write_text("#!/bin/sh\necho project\n", encoding="utf-8")
    (hooks_dir / "pre-commit").chmod(0o755)
    invoke = [_sys.executable, "-m", "agitrack"]

    git_hooks.install_autotrack_precommit_hook(hooks_dir, invoke=invoke, repo_root=str(repo.repo), version="1.0.0")
    # A schema-version bump replaces the hook; the chained project hook must survive the swap.
    git_hooks.install_autotrack_precommit_hook(hooks_dir, invoke=invoke, repo_root=str(repo.repo), version="2.0.0")
    assert git_hooks.autotrack_hook_version(hooks_dir / "pre-commit") == "2.0.0"
    assert (hooks_dir / "pre-commit.agitrack-orig").read_text() == "#!/bin/sh\necho project\n"

    git_hooks.remove_autotrack_precommit_hook(hooks_dir)
    assert (hooks_dir / "pre-commit").read_text() == "#!/bin/sh\necho project\n"  # restored intact


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


def test_noworktree_auto_force_fold_lands_even_with_summary_pending(tmp_path):
    # The throttled live fold defers while a turn summary is in flight (so the summary can ride
    # the commit). The EXIT finalize can't wait on the poll, so it folds with force=True — which
    # must land the commit even though a summary is still pending.
    import types

    runner, repo, state = _noworktree_auto_runner(tmp_path)
    runner._setup_manual_commit_mode()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("do x", 20))
    head_before = repo.rev_parse("HEAD")

    # A summary is still being computed: the normal (unforced) fold must DEFER.
    runner._summary_pending = {"sha": "deadbeef", "since": time.monotonic()}
    runner._summary_thread = types.SimpleNamespace(is_alive=lambda: True)
    runner._auto_fold_latent_pending()
    assert repo.rev_parse("HEAD") == head_before  # deferred while summarizing

    # Exit-style forced fold lands it anyway (summary becomes notes-only).
    runner._auto_fold_latent_pending(force=True)
    assert repo.rev_parse("HEAD") != head_before
    assert runner._manual_pending_count() == 0


@pytest.mark.parametrize("backend_name", ["claude", "opencode"])
def test_noworktree_auto_new_prompt_while_summarizing_does_not_offer_user_commit(tmp_path, backend_name):
    # Reported bug: submitting a NEW prompt while aGiTrack is still summarizing the just-finished
    # turn (no-worktree auto) popped the "commit your uncommitted changes" modal — but those
    # uncommitted files are the AGENT's just-finished turn, not the user's. ``turn_awaiting_commit``
    # has already dropped (the latent commit was recorded, which clears it) and the throttled real
    # fold is DEFERRED while the turn summary is in flight, so the tree is still dirty. The pre-agent
    # offer must land that owed turn commit itself and NEVER prompt the user.
    import types

    from agitrack.commits.actions import AgitrackActions

    runner, repo, state = _noworktree_auto_runner(tmp_path)
    runner.actions = AgitrackActions(repo, state, interactive=False)
    runner._setup_manual_commit_mode()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(
        build_agent_commit_message(
            latest_prompt="do x",
            trace=[{"role": "user", "content": "do x"}, {"role": "agent", "content": "did x"}],
            backend=backend_name,
            backend_session_id="bs",
            agitrack_session_id="sid",
            model="opus",
            token_usage={"output": 20, "input": 5},
            session_name="s",
        )
    )
    head_before = repo.rev_parse("HEAD")
    # Post-record state: turn reconciled (flag cleared) but its summary is still running, so the
    # normal fold defers and the agent's turn output sits uncommitted in the tree.
    runner.turn_awaiting_commit = False
    runner._summary_pending = {"sha": "deadbeef", "since": time.monotonic()}
    runner._summary_thread = types.SimpleNamespace(is_alive=lambda: True)
    assert runner.actions.has_pre_agent_user_changes() is True  # dirty with the agent's turn output

    prompted: list[int] = []
    runner._create_user_commit_popup = lambda *a, **k: prompted.append(1) or True

    warn = runner._offer_pre_agent_user_commit()

    assert prompted == []  # never asked the user about the agent's just-finished files
    assert warn is False
    assert repo.rev_parse("HEAD") != head_before  # the owed turn commit was LANDED instead
    assert "do x" in _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert runner._manual_pending_count() == 0
    assert runner.actions.has_pre_agent_user_changes() is False  # tree clean after the fold


def test_noworktree_auto_exit_finalize_folds_pending_latent(tmp_path):
    # The reported bug: a turn recorded latently right before quitting was never folded on exit
    # (the poll that folds doesn't run during teardown), so its changes never reached HEAD.
    # The exit finalize must now fold pending latent turns itself.
    runner, repo, state = _noworktree_auto_runner(tmp_path)
    runner._setup_manual_commit_mode()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("apply the change", 20))
    head_before = repo.rev_parse("HEAD")

    # Isolate the fold: stub the surrounding exit steps (worktree integration / teardown), which
    # are no-ops for a no-worktree session anyway.
    runner._summary_thread = None
    runner._service_commit_summary = lambda *a, **k: None
    runner._integrate_session_on_exit = lambda *a, **k: None
    runner._finalize_worktree_on_exit = lambda *a, **k: None

    runner._finalize_summary_then_integrate_on_exit()

    assert repo.rev_parse("HEAD") != head_before  # the pending turn landed on HEAD
    assert "apply the change" in _git(repo, "log", "-1", "--format=%B", "HEAD")
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


# --- the agent committing MID-TURN in no-worktree auto mode ------------------
#
# The reported loss. When the agent runs `git commit` ITSELF while its turn is still running,
# the prepare-commit-msg hook stamps that commit with an IN-FLIGHT block: attribution only, no
# trace, no tokens — and the block says in so many words that "the turn's full interaction
# trace and token usage land in a later commit".
#
# That later commit has to actually happen. The agent has already committed every file it
# touched, so when the turn finishes the tree is CLEAN — and both the fold and the staleness
# check used to read "clean" as "already accounted for" and stop. The turn's tokens then lived
# only on the hidden latent ref and never reached the branch: silently lost, exactly as the
# in-flight commit promised they would not be.
#
# `is_fully_tracked_message` already encodes the right rule for this (an in-flight-only block
# does NOT account for a turn) and `_uncovered_backend_commits` already applies it. These pin
# that the no-worktree fold applies it too.


def _agent_committed_mid_turn(tmp_path, *, manual: bool = False):
    """A no-worktree session where the agent committed its own work mid-turn, and the turn has
    since finished. Returns (runner, repo, the agent's commit)."""
    runner, repo = _noworktree_proxy(tmp_path, manual=manual)
    assert git_hooks.install_manual_commit_hooks(repo.repo / ".git" / "hooks")
    runner._manual_hooks_installed = True
    runner._set_noworktree_base_head(repo.rev_parse("HEAD"))  # persisted, as production does
    runner._start_commit_summary = lambda *a, **k: None
    runner.untracked_before_turn = set()

    # Mid-turn: the agent edits and commits its own work. The hook stamps it in-flight.
    (tmp_path / "a.txt").write_text("one\nagent work\n", encoding="utf-8")
    runner._note_in_flight({"backend": "claude", "backend_session_id": "s1", "model": "m", "prompt": "do x"})
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "Agent's own commit")
    agent_commit = repo.rev_parse("HEAD")
    body = _git(repo, "log", "-1", "--format=%B", agent_commit)
    assert "in_flight: true" in body  # attribution only...
    assert not is_fully_tracked_message(body)  # ...and explicitly NOT a complete record

    # The turn finishes, having left nothing further to commit: the tree is clean. Recording it
    # is what the tests below exercise, so the helper stops here.
    runner._note_in_flight(None)
    assert repo.snapshot_worktree_tree() == repo.rev_parse("HEAD^{tree}"), "the tree should be clean"
    return runner, repo, agent_commit


def _idle_and_ready_to_cover(runner, repo, *, turn):
    """Put the runner in the state the reactor reaches when a turn has just finished and the
    tree is clean, with the finished turn waiting at the parse boundary."""
    from agitrack.backends.proxy_agents import make_proxy_agent
    from agitrack.transcripts.types import ExportedSession

    runner.backend = make_proxy_agent("claude")
    runner.active.agent_parse_result = (
        "s1",
        ExportedSession(session_id="s1", model="m", turns=[turn], updated=1.0),
        None,
        runner.state,
    )
    runner.active.agent_parse_thread = None
    runner.agent_in_flight = False
    runner.last_child_output = 0.0  # the backend has gone quiet
    runner.CHILD_IDLE_SECONDS = 0.0
    runner.BASE_POLL_SECONDS = 0.0
    runner._idle_integrate_at = 0.0
    runner._summary_blocks_integration = lambda _now: False


def test_a_turn_the_agent_committed_itself_still_lands_its_tokens(tmp_path):
    """The reported loss, end to end.

    aGiTrack's whole promise is that the agent's work — and its accounting — reaches git. The
    agent having committed the CODE itself must not cost the user the TOKENS. This drives the
    production path (`_integrate_agent_made_commits_if_idle`, which the reactor calls from
    `_maybe_agent_commit`'s clean-tree branch), not a helper.
    """
    runner, repo, agent_commit = _agent_committed_mid_turn(tmp_path)
    assert runner._uncovered_backend_commits() == [agent_commit], "the in-flight commit is still owed a record"
    _idle_and_ready_to_cover(
        runner, repo, turn=SessionTurn("u1", "a1", "do x", "done", TokenUsage(total=4321, output=4321), "m")
    )

    runner._integrate_agent_made_commits_if_idle(time.monotonic())

    head = repo.rev_parse("HEAD")
    assert head != agent_commit, "no commit was made to carry the finished turn's trace/tokens"
    body = _git(repo, "log", "-1", "--format=%B", head)
    assert "4321" in body, f"the turn's tokens never reached the branch; body was:\n{body}"
    assert is_fully_tracked_message(body)
    assert not runner._uncovered_backend_commits(), "the agent's commit is still unaccounted for"


def test_the_cover_for_an_agent_commit_introduces_no_diff(tmp_path):
    # The agent already committed the code, so the accounting must ride a metadata-only commit:
    # it must never re-apply or revert a single file.
    runner, repo, agent_commit = _agent_committed_mid_turn(tmp_path)
    _idle_and_ready_to_cover(
        runner, repo, turn=SessionTurn("u1", "a1", "do x", "done", TokenUsage(total=7, output=7), "m")
    )

    runner._integrate_agent_made_commits_if_idle(time.monotonic())

    assert repo.rev_parse("HEAD^{tree}") == repo.rev_parse(f"{agent_commit}^{{tree}}")
    assert (tmp_path / "a.txt").read_text() == "one\nagent work\n"


def test_manual_mode_keeps_the_record_pending_instead_of_committing(tmp_path):
    # In manual-commit mode the user decides when to commit, so aGiTrack must NOT add a cover
    # commit of its own — the record waits on the latent chain for the user's next commit. The
    # fix must not trade one broken promise for another.
    runner, repo = _noworktree_proxy(tmp_path, manual=True)
    assert git_hooks.install_manual_commit_hooks(repo.repo / ".git" / "hooks")
    runner._manual_hooks_installed = True
    runner._noworktree_base_head = repo.rev_parse("HEAD")
    (tmp_path / "a.txt").write_text("agent work\n", encoding="utf-8")
    runner._note_in_flight({"backend": "claude", "backend_session_id": "s1", "model": "m", "prompt": "do x"})
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "Agent's own commit")
    agent_commit = repo.rev_parse("HEAD")
    runner._note_in_flight(None)
    _idle_and_ready_to_cover(
        runner, repo, turn=SessionTurn("u1", "a1", "do x", "done", TokenUsage(total=9, output=9), "m")
    )

    runner._integrate_agent_made_commits_if_idle(time.monotonic())

    assert repo.rev_parse("HEAD") == agent_commit, "manual mode must not commit on the user's behalf"
    # "Did not commit" must not quietly mean "dropped the record" — but the record is made on the
    # turn-completion path, not here, so that half is asserted in
    # `test_manual_mode_records_a_turn_whose_only_action_was_its_own_midturn_commit`. Its absence
    # is how the record-side refusal survived the first fix: the gate was widened to allow an
    # unchanged tree and `record()` then vetoed it one step later, with nothing checking.


def test_a_clean_tree_under_a_fully_tracked_head_still_drops_the_chain(tmp_path):
    # The other side, so the fix stays narrow: once HEAD really does account for the turn, a
    # clean tree means the pending chain IS redundant and must still be dropped — otherwise it
    # would re-attach its trace to some later, unrelated commit.
    runner, repo = _noworktree_proxy(tmp_path, manual=False)
    runner._noworktree_base_head = repo.rev_parse("HEAD")
    (tmp_path / "a.txt").write_text("agent work\n", encoding="utf-8")
    runner._start_commit_summary = lambda *a, **k: None
    runner.untracked_before_turn = set()
    runner._create_agent_commit_from_turns_popup(
        turns=[SessionTurn("u1", "a1", "do x", "done", TokenUsage(total=10, output=10), "m")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        quiet=True,
    )
    runner._auto_fold_latent_pending()  # aGiTrack folds it itself: HEAD now fully accounts
    assert is_fully_tracked_message(_git(repo, "log", "-1", "--format=%B", "HEAD"))

    runner._reset_stale_manual_ref()

    assert not runner._manual_pending_bodies()


# --- the coverage anchor must survive a restart -----------------------------


def test_the_noworktree_anchor_survives_a_restart(tmp_path):
    """`_noworktree_base_head` is what separates "work aGiTrack must account for" from the
    user's pre-existing history. It used to live only in memory and re-anchor to the current
    HEAD on every start — so if the agent committed mid-turn (an in-flight block that still
    owes its trace and tokens) and aGiTrack restarted before the turn finished, the anchor
    moved ONTO that commit and it could never be seen as uncovered again. The accounting was
    gone for good. The background daemon had already solved this by persisting its watermark.
    """
    runner, repo, agent_commit = _agent_committed_mid_turn(tmp_path)
    assert runner._uncovered_backend_commits() == [agent_commit]

    # A fresh instance over the SAME repo — the restart. Built directly rather than via the
    # helper, which would re-init the repo and add history that isn't part of this scenario.
    from proxy_helpers import make_runner

    restarted = make_runner(
        repo=repo,
        base_repo=repo,
        state=AgitrackState(tmp_path, default_backend="claude"),
        _use_worktrees=False,
        _manual_commits=False,
        worktree=None,
    )
    restarted._load_noworktree_anchor()  # what startup does

    assert restarted._uncovered_backend_commits() == [agent_commit], (
        "the restart re-anchored past the agent's in-flight commit, losing its accounting"
    )


def test_a_first_ever_run_anchors_at_head_and_claims_no_history(tmp_path):
    # The other half: on a repo aGiTrack has never tracked, everything already in the history
    # belongs to the user and must never be retroactively attributed to the agent.
    runner, repo = _noworktree_proxy(tmp_path, manual=False)
    (tmp_path / "prior.txt").write_text("the user's own work\n", encoding="utf-8")
    _git(repo, "add", "prior.txt")
    _git(repo, "commit", "-m", "a commit from before aGiTrack existed here")

    runner._load_noworktree_anchor()

    assert runner._noworktree_base_head == repo.rev_parse("HEAD")
    assert runner._uncovered_backend_commits() == []


def test_an_anchor_naming_a_vanished_commit_re_anchors_instead_of_failing(tmp_path):
    # A hard reset or a re-clone can leave a watermark git can no longer resolve. Scanning from
    # it would raise on every poll; re-anchoring is the safe recovery.
    runner, repo = _noworktree_proxy(tmp_path, manual=False)
    runner._noworktree_anchor_path().parent.mkdir(parents=True, exist_ok=True)
    runner._noworktree_anchor_path().write_text("0" * 40 + "\n", encoding="utf-8")

    runner._load_noworktree_anchor()

    assert runner._noworktree_base_head == repo.rev_parse("HEAD")


# --- `git commit --no-verify` -----------------------------------------------
#
# `--no-verify` skips `pre-commit` and `commit-msg`. It does NOT skip `prepare-commit-msg` or
# `post-commit` — verified against real git. That distinction decides how much it can cost:
# in every no-worktree mode the FOLD happens in `prepare-commit-msg`, so the tracking still
# lands. Worth pinning, because the natural assumption is that --no-verify defeats everything.


def test_no_verify_still_folds_the_pending_trace_in_no_worktree_mode(tmp_path):
    runner, repo = _noworktree_proxy(tmp_path, manual=True)
    assert git_hooks.install_manual_commit_hooks(repo.repo / ".git" / "hooks")
    runner._manual_hooks_installed = True
    runner._noworktree_base_head = repo.rev_parse("HEAD")
    (tmp_path / "a.txt").write_text("agent work\n", encoding="utf-8")
    runner._note_in_flight({"backend": "claude", "backend_session_id": "s1", "model": "m", "prompt": "do x"})

    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "committed with --no-verify", "--no-verify")

    body = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "# aGiTrack Metadata" in body, "--no-verify must not cost the commit its tracking"
    assert "do x" in body


def test_manual_mode_records_a_turn_whose_only_action_was_its_own_midturn_commit(tmp_path):
    """Manual mode, agent self-commits mid-turn, turn ends with a clean tree.

    Two guards had to agree for this to work and only one did. `_manual_gate` was widened to
    allow an unchanged tree when commits are still owed a record — but `_manual_record` kept its
    own "nothing new since the latent tip" check and vetoed the very record the gate approved.
    The turn's trace and tokens were dropped with `committed=False` and no error.

    Manual mode is where this costs the most: aGiTrack must not commit for the user, so the
    latent chain is the ONLY thing holding the accounting until their next commit folds it in.
    """
    runner, repo, _agent_commit = _agent_committed_mid_turn(tmp_path, manual=True)

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[SessionTurn("u1", "a1", "do x", "done", TokenUsage(total=42, output=42), "m")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        quiet=True,
    )

    assert committed is True, "the turn was refused outright"
    bodies = runner._manual_pending_bodies()
    assert bodies, "nothing was recorded, so the turn's tokens are gone"
    assert "42" in bodies[-1]
    assert "do x" in bodies[-1]


def test_the_record_guard_still_refuses_a_turn_that_genuinely_changed_nothing(tmp_path):
    # The guard is not simply removed: with no commits owed a record, an unchanged tree really
    # does mean nothing happened, and recording would chain an empty latent commit per poll.
    runner, repo = _noworktree_proxy(tmp_path, manual=True)
    assert git_hooks.install_manual_commit_hooks(repo.repo / ".git" / "hooks")
    runner._manual_hooks_installed = True
    runner._set_noworktree_base_head(repo.rev_parse("HEAD"))

    # Record a first turn so a latent tip exists — the guard compares against that tip, so with
    # an empty chain there is nothing for it to refuse.
    (tmp_path / "a.txt").write_text("agent work\n", encoding="utf-8")
    assert runner._manual_gate() is True
    assert runner._manual_record("<aGiTrack> first turn\n") is not None

    # Now nothing has changed and no commit is owed a record: the guard must hold, or every poll
    # would chain another empty latent commit.
    assert runner._manual_gate() is False
    assert runner._manual_record("<aGiTrack> nothing\n") is None


def test_setup_falls_back_to_poll_cover_under_a_real_core_hookspath(tmp_path):
    """The fold hooks can't run under a custom `core.hooksPath`, so aGiTrack must detect that
    and fall back to poll+cover. Every existing test set `_manual_hooks_installed` by hand; this
    drives the real `GitRepo.core_hooks_path()` detection, which is what actually decides.

    If detection ever misreported, aGiTrack would believe the fold hook was live when nothing
    was installed — and every commit in that repo would silently lose its trace and tokens, with
    the fallback that exists for exactly this case never running.
    """
    runner, repo = _noworktree_proxy(tmp_path, manual=True)
    custom = tmp_path / "shared-hooks"
    custom.mkdir()
    _git(repo, "config", "core.hooksPath", str(custom))

    runner._setup_manual_commit_mode()

    assert runner._manual_hooks_installed is False, "aGiTrack thinks its hooks are live when they cannot run"
    assert not (repo.repo / ".git" / "hooks" / "prepare-commit-msg").exists()

    # …and the fallback really covers a commit the hook could not fold.
    runner._set_noworktree_base_head(repo.rev_parse("HEAD"))
    (tmp_path / "a.txt").write_text("agent work\n", encoding="utf-8")
    assert runner._manual_gate() is True
    assert runner._manual_record("<aGiTrack> turn\n\n# aGiTrack Metadata\ncommit_type: agent\n") is not None
    runner._manual_last_head = None
    runner._reconcile_manual_external_commit()  # establishes the baseline at the current HEAD

    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "the user's own commit")
    user_head = repo.rev_parse("HEAD")

    runner._reconcile_manual_external_commit()  # now HEAD has moved: cover the user's commit

    assert repo.rev_parse("HEAD") != user_head, "the poll+cover fallback never added the cover"
    assert "# aGiTrack Metadata" in _git(repo, "log", "-1", "--format=%B", "HEAD")


def test_hooks_are_installed_when_no_custom_hookspath_is_set(tmp_path):
    # The other side of the same detection, so the fallback stays the exception.
    runner, repo = _noworktree_proxy(tmp_path, manual=True)

    runner._setup_manual_commit_mode()

    assert runner._manual_hooks_installed is True
    assert (repo.repo / ".git" / "hooks" / "prepare-commit-msg").exists()


# --- commit shapes that SKIP the fold --------------------------------------
#
# `prepare-commit-msg` deliberately declines to fold for an amend/squash/merge-template commit
# (git passes source `commit`/`squash`/`merge`) — folding there would duplicate a trailer, or
# attach one to a merge that contains no agent work. `post-commit` used to reset the latent
# chain regardless, so the pending turns were cleared with the trailer never landing anywhere:
# a silent, permanent loss of that turn's trace and tokens. The two hooks must agree, and the
# commit's own message is the only honest evidence of what was folded.


def _tracker(tmp_path, *, hooks=True):
    repo = _init_repo(tmp_path)
    if hooks:
        assert git_hooks.install_manual_commit_hooks(repo.repo / ".git" / "hooks")
    state = AgitrackState(tmp_path, default_backend="claude")
    tracker = ManualCommitTracker(repo, repo, state)
    tracker.setup()
    return tracker, repo


def test_amend_does_not_silently_drop_a_still_pending_turn(tmp_path):
    tracker, repo = _tracker(tmp_path)
    (tmp_path / "a.txt").write_text("agent edit\n", encoding="utf-8")
    _record(tracker, "pending", 999)
    tracker.render_trailer()
    assert tracker.pending_count() == 1

    # The user amends an earlier commit, reusing its message: git source is "commit", so the
    # fold is skipped by design.
    _git(repo, "commit", "--amend", "--no-edit")

    assert "# aGiTrack Metadata" not in _git(repo, "log", "-1", "--format=%B", "HEAD")  # not folded…
    # …so it must still be pending, ready for the user's next real commit. Asserted on the
    # CONTENT rather than the count: an amend rewrites HEAD, so the orphaned old commit also
    # falls in `HEAD..ref` and inflates the count — the turn surviving is what matters.
    assert "pending prompt" in "".join(tracker.pending_bodies()), "the turn was cleared without being folded"


def test_a_squash_merge_commit_does_not_silently_drop_a_pending_turn(tmp_path):
    tracker, repo = _tracker(tmp_path)
    base = repo.current_branch()
    _git(repo, "checkout", "-qb", "feature")
    (tmp_path / "f.txt").write_text("feature work\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "feature work")
    _git(repo, "checkout", "-q", base)
    # A REAL uncommitted agent edit, so the turn is genuinely pending. It used to be recorded
    # against an untouched tree, which only worked because `record()`'s guard skipped an empty
    # chain — the turn then existed for a reason the flow being tested had nothing to do with.
    (tmp_path / "a.txt").write_text("agent edit\n", encoding="utf-8")
    _record(tracker, "pending", 555)
    tracker.render_trailer()
    assert tracker.pending_count() == 1

    _git(repo, "merge", "-q", "--squash", "feature")
    subprocess.run(
        ["git", "-C", str(repo.repo), "commit", "-q"],
        check=True,
        env={**os.environ, "GIT_EDITOR": "true"},  # no -m ⇒ git source "squash" ⇒ fold skipped
    )

    assert "pending prompt" in "".join(tracker.pending_bodies()), (
        "the turn was cleared by a commit that never folded it"
    )


def test_an_ordinary_commit_still_folds_and_clears_the_chain(tmp_path):
    # The gate must stay narrow: a normal commit DOES fold, and must still reset the chain, or
    # every later commit would fold the same turns again.
    tracker, repo = _tracker(tmp_path)
    (tmp_path / "a.txt").write_text("agent edit\n", encoding="utf-8")
    _record(tracker, "folded", 321)
    tracker.render_trailer()

    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "the user's own commit")

    body = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "# aGiTrack Metadata" in body and "321" in body
    assert tracker.pending_count() == 0, "a folded chain must be cleared or it folds twice"


def test_a_second_commit_does_not_refold_an_already_folded_turn(tmp_path):
    # The consequence of getting the reset wrong in the other direction.
    tracker, repo = _tracker(tmp_path)
    (tmp_path / "a.txt").write_text("agent edit\n", encoding="utf-8")
    _record(tracker, "once", 42)
    tracker.render_trailer()
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "first")

    (tmp_path / "b.txt").write_text("the user's own later work\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "second, unrelated")

    assert "once prompt" not in _git(repo, "log", "-1", "--format=%B", "HEAD")


# --- the latent ref surviving damage ---------------------------------------


def test_a_pruned_latent_object_does_not_kill_all_future_tracking(tmp_path):
    """`git gc --prune` can collect a latent commit — it is unreachable from any branch by
    design. Every lookup against the now-dangling ref then raised, and because `record()` runs on
    EVERY turn, the session stopped tracking entirely until someone deleted the ref by hand. The
    turns already on the pruned chain are gone; the ones after it must not be.
    """
    tracker, repo = _tracker(tmp_path, hooks=False)
    (tmp_path / "a.txt").write_text("turn one\n", encoding="utf-8")
    _record(tracker, "one", 50)
    tip = repo.ref_sha(tracker.ref())
    loose = repo.repo / ".git" / "objects" / tip[:2] / tip[2:]
    # git stores loose objects READ-ONLY, which on Windows makes unlink() an access error. Make
    # it writable first so this simulates a prune on every platform rather than only POSIX.
    loose.chmod(stat.S_IWRITE | stat.S_IREAD)
    loose.unlink()  # what a prune leaves behind: a ref naming an object that is gone

    (tmp_path / "a.txt").write_text("turn one\nturn two\n", encoding="utf-8")
    assert tracker.gate() is True
    sha = tracker.record(
        "<aGiTrack> two\n\n# Interaction Trace\n\n## User\n\ntwo prompt\n\n"
        "# aGiTrack Metadata\ncommit_type: agent\ntokens_since_last_commit_output: 50\n"
    )

    assert sha is not None, "record() must re-anchor past a pruned tip, not raise"
    assert "two prompt" in "".join(tracker.pending_bodies())


def test_the_proxys_own_manual_copy_also_survives_a_pruned_latent_object(tmp_path):
    # The re-anchor lived ONLY in the tracker, so the headless daemon survived a `git gc --prune`
    # and interactive `-m` — the path a user actually types in — did not: `_manual_record` looked
    # up the dangling tip and raised, on every turn, for the rest of the session. The two copies
    # must stay in lockstep (AGENTS.md pins this).
    from tests.proxy_helpers import make_runner

    repo = _init_repo(tmp_path)
    runner = make_runner(
        repo=repo,
        base_repo=repo,
        state=AgitrackState(tmp_path, default_backend="claude"),
        _manual_commits=True,
        _use_worktrees=False,
        worktree=None,
    )
    (tmp_path / "a.txt").write_text("turn one\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("one", 50))
    tip = repo.ref_sha(runner._manual_ref())
    loose = repo.repo / ".git" / "objects" / tip[:2] / tip[2:]
    loose.chmod(stat.S_IWRITE | stat.S_IREAD)  # git stores loose objects read-only; Windows enforces it
    loose.unlink()

    (tmp_path / "a.txt").write_text("turn one\nturn two\n", encoding="utf-8")
    assert runner._manual_gate() is True
    sha = runner._manual_record(_agent_body("two", 50))

    assert sha is not None, "the proxy's copy must re-anchor past a pruned tip, not raise"
    assert "did two" in "".join(runner._manual_pending_bodies())


# --- the trailer files the sh hooks read ------------------------------------


def test_the_trailer_is_written_atomically(tmp_path):
    """The `sh` hooks read these files from another process during a `git commit` that can land
    at any moment. An in-place rewrite can be read half-finished — an empty read is harmlessly
    skipped, but a partial-but-non-empty one folds a truncated metadata block into the user's
    permanent commit message. Temp file + rename makes a reader see only the old or the new.
    """
    from agitrack.commits.manual import write_lf

    target = tmp_path / "nested" / "trailer"
    write_lf(target, "first\n")
    write_lf(target, "second, much longer than the first\n")

    assert target.read_text() == "second, much longer than the first\n"
    assert list(target.parent.glob("*.tmp")) == [], "a temp file was left behind"


def test_the_trailer_keeps_lf_endings_on_every_platform(tmp_path):
    # The hooks read `manual-ref` a line at a time; a CRLF ending leaves a trailing CR on the ref
    # name, `git update-ref` rejects it, and the turns fold a SECOND time into the next commit.
    from agitrack.commits.manual import write_lf

    target = tmp_path / "manual-ref"
    write_lf(target, "refs/agitrack/manual/s1\n")

    assert target.read_bytes() == b"refs/agitrack/manual/s1\n"


# --- abandoned sessions ------------------------------------------------------
#
# Manual mode is always no-worktree, so every session edits the SAME tree and a user commit
# folds the pending turns of all of them. Right while those turns still explain the code being
# committed; wrong once they don't. `reset_stale_ref` only ever looked at the CALLER's ref, so
# a session abandoned mid-work — a crash, a Ctrl-C, a mode switch, or edits the user discarded —
# left a chain nothing revisited, and its turns rode into an unrelated later commit, attributing
# AI authorship and tokens to code that never contained them.
#
# The rule, decided from git rather than a clock: keep a session's turns while its code changes
# are still uncommitted; discard a trailing run of turns that changed nothing.


def _abandoned_chain(repo, name: str, tokens: int, session_id: str):
    """Record one turn under a DIFFERENT session id, as an abandoned session would leave it."""
    state = AgitrackState(repo.repo, default_backend="claude")
    state.data["agitrack_session_id"] = session_id
    tracker = ManualCommitTracker(repo, repo, state)
    _record(tracker, name, tokens)
    return tracker.ref()


def test_a_discarded_sessions_turns_never_ride_into_an_unrelated_commit(tmp_path):
    # The reported shape: the agent edits, the user throws the edit away rather than committing
    # it, and commits something unrelated later. That commit contains none of the agent's work,
    # so it must carry none of its attribution.
    repo = _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("agent edit\n", encoding="utf-8")
    ref = _abandoned_chain(repo, "abandoned", 50, "gone-session")
    _git(repo, "checkout", "--", "a.txt")  # the user discards the agent's work

    prune_abandoned_refs(repo, "refs/agitrack/manual/a-live-session", [ref])

    assert repo.ref_sha(ref) == repo.rev_parse("HEAD"), "the abandoned chain was left to fold later"


def test_an_abandoned_sessions_uncommitted_work_is_kept(tmp_path):
    # The other half of your rule: work that is still uncommitted DOES get committed with its
    # trace. Discarding here would lose real AI authorship.
    repo = _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("agent edit that survives\n", encoding="utf-8")
    ref = _abandoned_chain(repo, "real work", 50, "gone-session")

    prune_abandoned_refs(repo, "refs/agitrack/manual/a-live-session", [ref])

    assert repo.ref_sha(ref) != repo.rev_parse("HEAD"), "uncommitted agent work lost its trace"


def test_a_turn_that_only_talked_never_reaches_the_chain_at_all(tmp_path):
    # Why the tail case is rare, and worth pinning because it is the reason a weaker assertion on
    # the trim below would pass vacuously: `gate()` already refuses a turn that changed no code,
    # so a pure-Q&A turn is never recorded and there is nothing for the prune to trim.
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    tracker = ManualCommitTracker(repo, repo, state)
    (tmp_path / "a.txt").write_text("real agent work\n", encoding="utf-8")
    _record(tracker, "did the work", 100)

    _record(tracker, "just discussed it", 5)  # same tree ⇒ never recorded

    bodies = "".join(tracker.pending_bodies())
    assert "did the work prompt" in bodies
    assert "just discussed it prompt" not in bodies
    assert len(tracker.pending_bodies()) == 1


def _chain_with_a_trailing_turn_that_matches_head(repo: GitRepo, path: Path):
    """An abandoned chain whose LAST turn left the code exactly as HEAD has it, with someone
    else's work still uncommitted so the whole-chain discard doesn't apply.

    This is the shape the tail trim exists for: the agent wrote something, then undid it, and the
    trailing turn therefore contributes nothing to the commit about to be made. Returns
    ``(sha_to_keep, sha_to_trim, ref)``."""
    original = (path / "a.txt").read_text(encoding="utf-8")
    state = AgitrackState(path, default_backend="claude")
    state.data["agitrack_session_id"] = "gone-session"
    tracker = ManualCommitTracker(repo, repo, state)
    (path / "a.txt").write_text("real agent work\n", encoding="utf-8")
    _record(tracker, "did the work", 100)
    keep = repo.ref_sha(tracker.ref())
    (path / "a.txt").write_text(original, encoding="utf-8")  # …and then undid it
    _record(tracker, "undid it again", 5)
    trim = repo.ref_sha(tracker.ref())
    assert keep != trim, "precondition: both turns are on the chain"
    # A LIVE session's uncommitted work, so `working_tree_is_clean` is False and the prune must
    # reach the trim rather than discarding the chain outright.
    (path / "b.txt").write_text("another session is mid-edit\n", encoding="utf-8")
    return keep, trim, tracker.ref()


def test_a_trailing_turn_that_left_the_code_as_head_has_it_is_trimmed(tmp_path):
    # The tail trim, actually exercised. The previous version of this test recorded a
    # "conversation-only" turn that `gate()` silently refused, so the chain never grew a tail and
    # the assertion (`len <= before`) held no matter what the prune did.
    repo = _init_repo(tmp_path)
    keep, trim, ref = _chain_with_a_trailing_turn_that_matches_head(repo, tmp_path)

    changed = prune_abandoned_refs(repo, "refs/agitrack/manual/a-live-session", [ref])

    assert changed == [ref]
    assert repo.ref_sha(ref) == keep, "the trailing turn was not trimmed"
    assert repo.ref_sha(ref) != trim


def test_the_trim_keeps_the_turn_that_actually_wrote_code(tmp_path):
    # The other half: trimming must stop at the first turn that contributed code. Walking past it
    # would discard real AI authorship for work that is still uncommitted.
    repo = _init_repo(tmp_path)
    _keep, _trim, ref = _chain_with_a_trailing_turn_that_matches_head(repo, tmp_path)

    prune_abandoned_refs(repo, "refs/agitrack/manual/a-live-session", [ref])

    bodies = "".join(_bodies_on(repo, ref))
    assert "did the work prompt" in bodies, "the turn that wrote code was discarded"
    assert "undid it again prompt" not in bodies


def _bodies_on(repo: GitRepo, ref: str) -> list[str]:
    """Commit messages still on *ref* and on no branch — what a fold would carry."""
    return [repo.commit_message(sha) or "" for sha in repo.unlanded_commits(ref)]


def test_the_live_sessions_own_chain_is_never_pruned(tmp_path):
    # A live session owns its chain — `reset_stale_ref` governs it. Pruning it here would fight
    # that and could drop a turn the session is about to fold itself.
    repo = _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("agent edit\n", encoding="utf-8")
    ref = _abandoned_chain(repo, "mine", 50, "my-session")
    _git(repo, "checkout", "--", "a.txt")  # clean tree: an abandoned chain WOULD be dropped

    prune_abandoned_refs(repo, ref, [ref])  # …but this one is our own

    assert repo.ref_sha(ref) != repo.rev_parse("HEAD")


def test_an_already_folded_chain_is_left_alone(tmp_path):
    # Reachable from HEAD means it was folded/committed already; touching it would be pointless
    # churn on the ref.
    repo = _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("agent edit\n", encoding="utf-8")
    ref = _abandoned_chain(repo, "folded", 50, "gone-session")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "the user's commit")
    repo.update_ref(ref, repo.rev_parse("HEAD"))
    before = repo.ref_sha(ref)

    prune_abandoned_refs(repo, "refs/agitrack/manual/live", [ref])

    assert repo.ref_sha(ref) == before


# --- what the user can see ---------------------------------------------------


def test_the_exit_dialog_does_not_claim_tracking_is_lost_when_it_is_not(tmp_path):
    """The persistent auto-track hook folds the tracking into a `git commit` made after aGiTrack
    exits — verified. Telling the user otherwise pushed them into committing before they were
    ready, in the one mode built around them choosing when."""
    runner, repo, _ = _manual_runner(tmp_path)
    (tmp_path / "a.txt").write_text("agent edit\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("t", 1))
    runner._menu_label = lambda: "Ctrl-G"
    runner._autotrack_hook_will_survive_exit = lambda: True
    captured: dict = {}
    runner._exit_confirmation_popup = lambda title, opts: captured.setdefault("title", title) or opts[1]

    runner._confirm_exit()

    assert "still folds in the interaction tracking" in captured["title"]
    assert "won't" not in captured["title"]


def test_the_exit_dialog_does_warn_when_the_hook_will_not_survive(tmp_path):
    # The warning is right when it IS right: a custom core.hooksPath or the autotrack opt-out.
    runner, repo, _ = _manual_runner(tmp_path)
    (tmp_path / "a.txt").write_text("agent edit\n", encoding="utf-8")
    runner._manual_gate()
    runner._manual_record(_agent_body("t", 1))
    runner._menu_label = lambda: "Ctrl-G"
    runner._autotrack_hook_will_survive_exit = lambda: False
    captured: dict = {}
    runner._exit_confirmation_popup = lambda title, opts: captured.setdefault("title", title) or opts[1]

    runner._confirm_exit()

    assert "won't" in captured["title"]


def test_the_status_bar_shows_uncommitted_turns_in_manual_mode():
    # The count existed only in the exit dialog, so while working the user could not tell whether
    # the agent had done nothing or twenty turns' worth since they last committed.
    from agitrack.proxy.renderer import ScreenRenderer

    line = ScreenRenderer.status_line(
        None,  # status_line reads only its keyword args, never `self`
        cols=200,
        name="s",
        session_id=None,
        backend_name="claude",
        base_branch=None,
        worktree=None,
        scroll_back=0,
        user_declined=[],
        short_session_fn=lambda s: "",
        manual_pending=3,
    )

    assert "3 uncommitted turns" in line


def test_the_status_bar_says_nothing_when_no_turns_are_pending():
    from agitrack.proxy.renderer import ScreenRenderer

    line = ScreenRenderer.status_line(
        None,  # status_line reads only its keyword args, never `self`
        cols=200,
        name="s",
        session_id=None,
        backend_name="claude",
        base_branch=None,
        worktree=None,
        scroll_back=0,
        user_declined=[],
        short_session_fn=lambda s: "",
        manual_pending=0,
    )

    assert "uncommitted" not in line


# --- the coverage anchor vs a rewritten history ------------------------------
#
# The persisted anchor separates "work aGiTrack must account for" from the user's own history.
# It was validated only by "does this object exist", which a rewritten history passes: a rebase,
# squash, amend, `reset --hard` or someone else's force-push leaves the old commit as a reachable
# OBJECT while it stops describing the branch. `git log <orphan>..HEAD` still succeeds against it,
# so the scan silently ran over the wrong range — walking commits already accounted for and able
# to re-attribute them. Observed for real after rewriting this repo's own history.


def _anchor_runner(tmp_path):
    from proxy_helpers import make_runner

    repo = _init_repo(tmp_path)
    runner = make_runner(
        repo=repo,
        base_repo=repo,
        state=AgitrackState(tmp_path, default_backend="claude"),
        _use_worktrees=False,
        worktree=None,
    )
    return runner, repo


def test_an_anchor_orphaned_by_a_rewrite_is_re_anchored(tmp_path):
    runner, repo = _anchor_runner(tmp_path)
    (tmp_path / "a.txt").write_text("work\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "a commit that a rewrite will replace")
    orphaned = repo.rev_parse("HEAD")
    runner._set_noworktree_base_head(orphaned)

    # Rewrite history: the old commit survives as an object but no longer describes the branch.
    _git(repo, "commit", "--amend", "-m", "rewritten")
    assert repo.has_object_local(orphaned), "precondition: the old object is still present"
    assert not repo.is_ancestor(orphaned, "HEAD"), "precondition: it is no longer on the branch"

    runner._noworktree_base_head = None
    runner._load_noworktree_anchor()

    assert runner._noworktree_base_head == repo.rev_parse("HEAD"), "a stale anchor must re-anchor at HEAD"


def test_a_valid_anchor_still_survives_a_restart(tmp_path):
    # The check must stay narrow: an anchor that IS on the branch is the whole point of persisting
    # it, and re-anchoring it would lose coverage of the agent's commits since.
    runner, repo = _anchor_runner(tmp_path)
    anchor = repo.rev_parse("HEAD")
    runner._set_noworktree_base_head(anchor)
    (tmp_path / "a.txt").write_text("agent work after the anchor\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "the agent's own commit")

    runner._noworktree_base_head = None
    runner._load_noworktree_anchor()

    assert runner._noworktree_base_head == anchor, "a still-valid anchor must be kept"
    assert runner._uncovered_backend_commits(), "the commit after the anchor must still read as uncovered"


def test_the_daemon_re_anchors_a_watermark_orphaned_by_a_rewrite(tmp_path):
    # The daemon persists the same watermark and had the same flaw: it checked only that the sha
    # parsed, which an orphaned commit does.
    from agitrack.config import GlobalConfig
    from agitrack.proxy.background import BackgroundRunner

    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    runner = BackgroundRunner(
        repo, manual_commits=False, _global_config=GlobalConfig(path=tmp_path / "gc.json"), _state=state
    )
    (tmp_path / "a.txt").write_text("work\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "to be rewritten")
    runner._set_tracked_head(repo.rev_parse("HEAD"))
    _git(repo, "commit", "--amend", "-m", "rewritten")

    runner._tracked_head = None
    runner._load_tracked_head()

    assert runner._tracked_head == repo.rev_parse("HEAD")


# --- the user is told when attribution goes away -----------------------------
#
# Every failure path in this area used to log only to `self._debug`, invisible without
# --verbose. That is the worst possible property for a feature whose whole job is not losing AI
# attribution: a silent loss can never be noticed, so it can never be reported or investigated.


def test_a_rewritten_history_tells_the_user_the_marker_was_reset(tmp_path):
    runner, repo = _anchor_runner(tmp_path)
    (tmp_path / "a.txt").write_text("work\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "to be rewritten")
    runner._set_noworktree_base_head(repo.rev_parse("HEAD"))
    _git(repo, "commit", "--amend", "-m", "rewritten")

    runner._noworktree_base_head = None
    runner._load_noworktree_anchor()

    assert runner.message is not None, "the reset was silent"
    assert "rewritten" in runner.message and "won't be attributed" in runner.message


def test_a_valid_anchor_says_nothing(tmp_path):
    # The notice must be an exception, not noise on every start.
    runner, repo = _anchor_runner(tmp_path)
    runner._set_noworktree_base_head(repo.rev_parse("HEAD"))

    runner._noworktree_base_head = None
    runner._load_noworktree_anchor()

    assert runner.message is None


def test_discarding_an_abandoned_chain_tells_the_user(tmp_path):
    # Discarding is the right call — the work is no longer uncommitted — but it is still AI
    # attribution going away, and the user should hear it rather than find a history quietly
    # missing a turn.
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    tracker = ManualCommitTracker(repo, repo, state)
    (tmp_path / "a.txt").write_text("agent edit\n", encoding="utf-8")
    _abandoned_chain(repo, "abandoned", 50, "gone-session")
    _git(repo, "checkout", "--", "a.txt")  # the user discarded it

    tracker.setup()

    assert tracker.dropped_chains, "the tracker did not record what it discarded"


def test_nothing_is_reported_when_no_chain_is_dropped(tmp_path):
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    tracker = ManualCommitTracker(repo, repo, state)

    tracker.setup()

    assert tracker.dropped_chains == []


# --- a repo that TRACKS the agent scaffolding dirs ---------------------------
#
# Committing `.claude/settings.json`, a `.claude/commands/` dir or an `.opencode/` config is
# ordinary practice — a team shares its agent setup the same way it shares an editorconfig. But
# EVERY "has the working tree changed?" question in manual / no-worktree / background mode is a
# comparison between `snapshot_worktree_tree()` and some commit's tree, and the snapshot
# deliberately STRIPS those dirs while a raw `^{tree}` keeps them. So in such a repo the two
# could never be equal and every one of those questions answered "dirty" forever, which broke
# each mode in a different direction:
#
#   -b  the daemon covered NO agent self-commit at all (`_agent_committed_own_work` bails on a
#       dirty tree) — the exact data loss the persistent watermark exists to prevent;
#   -m  `reset_stale_ref` never reset a stale chain and `prune_abandoned_refs` was a total no-op,
#       so discarded turns rode into unrelated commits, and `gate()` recorded a latent turn for a
#       turn that changed nothing;
#   both  a purely human commit made while an agent was mid-turn got stamped as agent work.
#
# `GitRepo.comparable_tree` strips the same paths from the commit side, so the comparison means
# what it says. These tests pin each consequence, because a raw `^{tree}` reads as obviously
# correct and would be reintroduced by anyone who had not seen this.


def _scaffolded_repo(path: Path) -> GitRepo:
    """A repo like `_init_repo`, except it TRACKS a `.claude/` file the way real projects do."""
    repo = _init_repo(path)
    (path / ".claude").mkdir(exist_ok=True)
    (path / ".claude" / "settings.json").write_text('{"model": "opus"}\n', encoding="utf-8")
    _git(repo, "add", "-f", ".claude/settings.json")
    _git(repo, "commit", "-m", "share the agent config, as teams do")
    return repo


def test_comparable_tree_strips_tracked_scaffolding_so_a_clean_tree_reads_as_clean(tmp_path):
    repo = _scaffolded_repo(tmp_path)

    assert repo.rev_parse("HEAD^{tree}") != repo.snapshot_worktree_tree(), "precondition"
    assert repo.comparable_tree("HEAD") == repo.snapshot_worktree_tree()


def test_comparable_tree_is_a_no_op_when_no_scaffolding_is_tracked(tmp_path):
    # The overwhelmingly common repo. The stripped tree must be the raw tree exactly — otherwise
    # this changes behavior for everyone to fix a minority case.
    repo = _init_repo(tmp_path)

    assert repo.comparable_tree("HEAD") == repo.rev_parse("HEAD^{tree}")


def test_comparable_tree_is_idempotent_on_an_already_stripped_tree(tmp_path):
    # It is applied to latent tips, whose trees are already snapshots. Stripping twice must not
    # move the answer, or the record guard would compare a tree against a different spelling of
    # itself and record a duplicate turn.
    repo = _scaffolded_repo(tmp_path)
    (tmp_path / "a.txt").write_text("agent edit\n", encoding="utf-8")
    latent = repo.commit_tree(repo.snapshot_worktree_tree(), parents=[repo.rev_parse("HEAD")], message="turn")

    assert repo.comparable_tree(latent) == repo.rev_parse(f"{latent}^{{tree}}")


def test_a_turn_that_changed_nothing_records_no_latent_commit_in_a_scaffolded_repo(tmp_path):
    # `gate()` answering True on a genuinely untouched tree means a pure-Q&A turn is recorded as
    # a latent commit: a token bill and an AI-authorship claim attached to no code at all.
    repo = _scaffolded_repo(tmp_path)
    tracker = ManualCommitTracker(repo, repo, AgitrackState(tmp_path, default_backend="claude"))
    tracker.setup()

    assert tracker.gate() is False
    assert tracker.record(_agent_body("just a question", 5)) is None
    assert tracker.pending_count() == 0


def test_a_real_edit_is_still_recorded_in_a_scaffolded_repo(tmp_path):
    # The other direction: the strip must not blind the gate to actual work.
    repo = _scaffolded_repo(tmp_path)
    tracker = ManualCommitTracker(repo, repo, AgitrackState(tmp_path, default_backend="claude"))
    tracker.setup()
    (tmp_path / "a.txt").write_text("agent wrote this\n", encoding="utf-8")

    assert tracker.gate() is True
    assert tracker.record(_agent_body("do the work", 100)) is not None
    assert tracker.pending_count() == 1


def test_an_edit_to_the_tracked_scaffolding_itself_is_never_agent_work(tmp_path):
    # Deliberate consequence of the strip, stated so it is a decision rather than an accident:
    # the snapshot cannot represent `.claude/` changes, so a turn that ONLY rewrote the agent's
    # own config records nothing. Attributing it would be worse — the latent commit's tree would
    # be identical to the previous one, so it would claim the whole turn against no diff.
    repo = _scaffolded_repo(tmp_path)
    tracker = ManualCommitTracker(repo, repo, AgitrackState(tmp_path, default_backend="claude"))
    tracker.setup()
    (tmp_path / ".claude" / "settings.json").write_text('{"model": "haiku"}\n', encoding="utf-8")

    assert tracker.gate() is False


def test_a_stale_chain_is_still_reset_in_a_scaffolded_repo(tmp_path):
    # `reset_stale_ref` decides on a CLEAN tree. Never seeing one meant a chain whose work the
    # user discarded was kept and folded into some unrelated later commit.
    repo = _scaffolded_repo(tmp_path)
    tracker = ManualCommitTracker(repo, repo, AgitrackState(tmp_path, default_backend="claude"))
    (tmp_path / "a.txt").write_text("agent edit\n", encoding="utf-8")
    _record(tracker, "work", 50)
    _git(repo, "checkout", "--", "a.txt")  # the user discards it

    assert tracker.reset_stale_ref() is True
    assert repo.ref_sha(tracker.ref()) == repo.rev_parse("HEAD")


def test_an_abandoned_chain_is_still_pruned_in_a_scaffolded_repo(tmp_path):
    # Both halves of `prune_abandoned_refs` were dead here: the clean-tree discard (its
    # `working_tree_is_clean` never held) and the tail trim (it compared a latent tree against a
    # raw HEAD tree). So the whole feature the previous commit added did nothing in such a repo.
    repo = _scaffolded_repo(tmp_path)
    (tmp_path / "a.txt").write_text("agent edit\n", encoding="utf-8")
    ref = _abandoned_chain(repo, "abandoned", 50, "gone-session")
    _git(repo, "checkout", "--", "a.txt")

    dropped = prune_abandoned_refs(repo, "refs/agitrack/manual/live", [ref])

    assert dropped == [ref]
    assert repo.ref_sha(ref) == repo.rev_parse("HEAD")


def test_a_trailing_turn_matching_head_is_still_trimmed_in_a_scaffolded_repo(tmp_path):
    repo = _scaffolded_repo(tmp_path)
    kept, trimmed, ref = _chain_with_a_trailing_turn_that_matches_head(repo, tmp_path)

    prune_abandoned_refs(repo, "refs/agitrack/manual/live", [ref])

    assert repo.ref_sha(ref) == kept, "the trailing turn was not trimmed"
    assert repo.ref_sha(ref) != trimmed


def test_a_human_commit_during_an_agent_turn_gets_no_footprint_in_a_scaffolded_repo(tmp_path):
    # The "no AI work ⇒ no footprint" promise. The tree check is the only thing enforcing it, so
    # a permanently-dirty reading stamped the user's own commit as agent work.
    repo = _scaffolded_repo(tmp_path)
    tracker = ManualCommitTracker(
        repo,
        repo,
        AgitrackState(tmp_path, default_backend="claude"),
        in_flight_fn=lambda: {"backend": "claude", "model": "opus"},
    )
    tracker.setup()

    assert tracker.in_flight_attribution() is None

    (tmp_path / "a.txt").write_text("but now the agent HAS edited\n", encoding="utf-8")
    assert tracker.in_flight_attribution() is not None  # …and real work still is attributed


def test_the_proxys_own_manual_copy_agrees_with_the_tracker_in_a_scaffolded_repo(tmp_path):
    # Interactive `-m` runs the ProxyRunner's parallel `_manual_*` copy, which is the path users
    # actually type in. It carried the identical raw-`^{tree}` defect and must stay in lockstep.
    from tests.proxy_helpers import make_runner

    repo = _scaffolded_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    runner = make_runner(
        repo=repo, base_repo=repo, state=state, _manual_commits=True, _use_worktrees=False, worktree=None
    )
    tracker = ManualCommitTracker(repo, repo, state)

    assert runner._manual_gate() is False
    assert runner._manual_gate() == tracker.gate()
    assert runner._reset_stale_manual_ref() is False  # nothing recorded yet ⇒ nothing to reset

    (tmp_path / "a.txt").write_text("agent edit\n", encoding="utf-8")
    assert runner._manual_gate() is True
    assert runner._manual_record(_agent_body("work", 10)) is not None
    _git(repo, "checkout", "--", "a.txt")
    assert runner._reset_stale_manual_ref() is True
