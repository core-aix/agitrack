"""Backend-made commits get aGiTrack's trace/metadata attached (issues #35, #58).

Some backends commit on their own (a Claude Code hook, or an agent told to run
`git commit`). Those commits leave the worktree clean, so aGiTrack's normal
stage-and-commit path never runs and the turn's provenance was lost.

Amending the backend's latest commit (the original #35 fix) changed its hash,
which broke references the agent had already published in PRs and issues
(#58). The trace/metadata now ride a *cover commit* instead — the GitHub PR
merge-commit shape: tree of the backend's head, parents (turn start, backend
head) — so every backend-made hash stays exactly what the agent reported.
"""

from pathlib import Path
from types import SimpleNamespace

from agitrack.backends.base import TokenUsage
from agitrack.commits import build_agent_commit_message
from agitrack.config import AgitrackState
from agitrack.git import GitRepo
from agitrack.proxy.commit_engine import CommitEngine
from agitrack.transcripts.types import SessionTurn

from proxy_helpers import make_runner

AGITRACK_BODY = "internal change\n\n# aGiTrack Metadata\ncommit_type: agent\n"


def _turn(prompt: str = "add the feature", response: str = "done") -> SessionTurn:
    return SessionTurn(
        user_message_id="u1",
        assistant_message_id="a1",
        user_prompt=prompt,
        final_response=response,
        tokens=TokenUsage(input=10, output=5),
        model="m1",
        complete=True,
        interrupted=False,
    )


def _repo_on_turn_branch(tmp_path: Path) -> tuple[GitRepo, str]:
    repo = GitRepo.init(tmp_path)
    base = repo.current_branch()
    repo.switch("agitrack/test/s1/t1", create=True)
    return repo, base


def _backend_commit(repo: GitRepo, name: str, message: str) -> str:
    (repo.repo / name).write_text(f"{name} content\n", encoding="utf-8")
    repo.stage_paths([name])
    repo.commit(message)
    return repo.rev_parse("HEAD")


def _commit_turns(repo: GitRepo, state: AgitrackState, backend_commits: list[str], **kwargs) -> bool:
    return CommitEngine(repo, state).commit_turns(
        turns=[_turn()],
        backend="claude",
        backend_session_id="ses-1",
        model="m1",
        stage_untracked_fn=lambda repo, state: None,
        session_name="s1",
        backend_commits=backend_commits,
        **kwargs,
    )


# --- message builder ----------------------------------------------------------


def test_agent_commit_message_covered_commits_line():
    kwargs = dict(
        latest_prompt="do things",
        trace=[],
        backend="claude",
        backend_session_id=None,
        agitrack_session_id="agit-1",
        model=None,
    )
    with_covers = build_agent_commit_message(**kwargs, covered_commits=["abc123"])
    without = build_agent_commit_message(**kwargs)
    assert "covered_commits: abc123" in with_covers
    assert "covered_commits" not in without


def test_covered_commits_message_carries_an_agent_made_note():
    # A commit that accounts for the agent's OWN commit(s) must EXPLAIN the covered hashes: a
    # reader of the covering commit should understand where they came from and why this commit's
    # token counts span work already in history. A plain aGiTrack commit gets no such note.
    kwargs = dict(
        latest_prompt="do things",
        trace=[],
        backend="claude",
        backend_session_id=None,
        agitrack_session_id="agit-1",
        model=None,
    )
    with_covers = build_agent_commit_message(**kwargs, covered_commits=["abc123"])
    without = build_agent_commit_message(**kwargs)
    assert "This commit accounts" in with_covers  # the explanatory note
    assert "This commit accounts" not in without  # no covered commits ⇒ no note
    # The note LEADS the interaction trace, above the metadata line that lists the hashes.
    assert with_covers.index("This commit accounts") < with_covers.index("covered_commits:")


# --- commit_turns cover path ---------------------------------------------------


def test_clean_tree_covers_backend_commits_without_rewriting_them(tmp_path):
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitrackState(tmp_path)
    turn_start = repo.rev_parse("HEAD")
    first = _backend_commit(repo, "a.txt", "backend commit one")
    last = _backend_commit(repo, "b.txt", "backend commit two")

    committed = _commit_turns(repo, state, [first, last])

    assert committed is True
    # The backend's commits keep their hashes AND their messages (#58): any
    # reference the agent published (PR/issue comments) stays valid.
    assert repo.rev_parse("HEAD^2") == last
    assert repo.commit_message(first).startswith("backend commit one")
    assert repo.commit_message(last).startswith("backend commit two")
    # The cover commit is merge-shaped, GitHub-PR style: parents are the turn
    # start and the backend's head, and its tree is the backend head's tree.
    assert repo.parents("HEAD") == [turn_start, last]
    assert repo.rev_parse("HEAD^{tree}") == repo.rev_parse(f"{last}^{{tree}}")
    head_message = repo.commit_message("HEAD")
    assert head_message.startswith("<aGiTrack> add the feature")
    assert "# Interaction Trace" in head_message
    assert f"covered_commits: {repo.short_sha(first)} {repo.short_sha(last)}" in head_message
    assert state.pending_trace() == []  # trace consumed by the cover commit


def test_cover_commit_notes_the_commits_were_agent_made(tmp_path):
    # Parity with the no-worktree fold: when the backend agent makes a commit in worktree
    # (interactive) mode, the cover commit that attaches the trace must also NOTE that the
    # covered commits were the agent's own — the note used to appear only via the no-worktree
    # in-flight trailer, so worktree cover commits explained nothing.
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitrackState(tmp_path)
    first = _backend_commit(repo, "a.txt", "backend commit one")

    assert _commit_turns(repo, state, [first]) is True

    head_message = repo.commit_message("HEAD")
    assert "This commit accounts" in head_message  # explains the covered hash
    assert f"covered_commits: {repo.short_sha(first)}" in head_message
    # The note leads the trace, above the metadata block.
    assert head_message.index("This commit accounts") < head_message.index("# aGiTrack Metadata")


def test_on_commit_fn_flags_cover_vs_plain(tmp_path):
    # The callback is told whether the commit is a cover (over the backend's own
    # commits) so the UI can explain it; a plain aGiTrack commit is flagged False.
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitrackState(tmp_path)
    seen: list = []
    first = _backend_commit(repo, "a.txt", "backend commit one")
    last = _backend_commit(repo, "b.txt", "backend commit two")

    _commit_turns(repo, state, [first, last], on_commit_fn=lambda sha, trace, is_cover: seen.append(is_cover))
    assert seen == [True]  # covered the backend's commits

    # A plain aGiTrack commit (no backend commits to cover) flags False.
    (repo.repo / "mine.txt").write_text("aGiTrack-staged\n", encoding="utf-8")
    repo.stage_paths(["mine.txt"])
    seen.clear()
    _commit_turns(repo, state, [], on_commit_fn=lambda sha, trace, is_cover: seen.append(is_cover))
    assert seen == [False]


def test_cover_commit_makes_first_parent_log_turn_level(tmp_path):
    # `git log --first-parent` on the branch reads turn-by-turn: one aGiTrack
    # cover commit, with the backend's commits reachable via the second parent.
    repo, base = _repo_on_turn_branch(tmp_path)
    state = AgitrackState(tmp_path)
    first = _backend_commit(repo, "a.txt", "backend commit one")
    last = _backend_commit(repo, "b.txt", "backend commit two")

    assert _commit_turns(repo, state, [first, last]) is True

    output = repo._run(
        ["git", "log", "--first-parent", "--format=%s", f"{base}..HEAD"],
    ).stdout.splitlines()
    assert len(output) == 1
    assert output[0].startswith("<aGiTrack> add the feature")


def test_clean_tree_without_backend_commits_does_not_commit(tmp_path):
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitrackState(tmp_path)
    head = repo.rev_parse("HEAD")

    assert _commit_turns(repo, state, []) is False
    assert repo.rev_parse("HEAD") == head


def test_cover_refused_when_head_is_not_the_latest_backend_commit(tmp_path):
    # An aGiTrack commit sits on top of the backend's: it already accounts for the
    # turn, so the engine must not stack another cover commit.
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitrackState(tmp_path)
    backend_sha = _backend_commit(repo, "a.txt", "backend commit")
    _backend_commit(repo, "b.txt", AGITRACK_BODY)
    head = repo.rev_parse("HEAD")

    assert _commit_turns(repo, state, [backend_sha]) is False
    assert repo.rev_parse("HEAD") == head
    assert repo.commit_message("HEAD").startswith("internal change")


def test_staged_changes_commit_covers_backend_and_tracks_all_changes(tmp_path):
    # Backend committed a.txt, then there are further (uncommitted) changes on top.
    # The aGiTrack commit must COVER the backend commit AND track all the file changes
    # — the covered commit's plus the extra staged ones — as a merge-shaped cover,
    # not hide them behind a plain single-parent commit that only shows the extra
    # delta (#35).
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitrackState(tmp_path)
    turn_start = repo.rev_parse("HEAD")
    backend_sha = _backend_commit(repo, "a.txt", "backend commit")
    (repo.repo / "a.txt").write_text("further uncommitted work\n", encoding="utf-8")

    committed = _commit_turns(repo, state, [backend_sha])

    assert committed is True
    head_message = repo.commit_message("HEAD")
    assert head_message.startswith("<aGiTrack> add the feature")
    assert f"covered_commits: {repo.short_sha(backend_sha)}" in head_message
    # Merge-shaped cover: first parent is the turn start (so --first-parent shows
    # the whole change), second parent is the backend commit, preserved intact.
    assert repo.rev_parse("HEAD^1") == turn_start
    assert repo.rev_parse("HEAD^2") == backend_sha
    assert repo.commit_message("HEAD^2").startswith("backend commit")
    # The cover's first-parent diff tracks the full a.txt change, including the
    # staged work layered on top of the backend's commit.
    first_parent_diff = repo.diff_range(turn_start, "HEAD")
    assert "further uncommitted work" in first_parent_diff


def test_cover_commit_survives_summary_amend_with_parents_intact(tmp_path):
    # The async summary (#8) amends the COVER commit — aGiTrack's own commit,
    # created moments earlier — never the backend's. The amend keeps the merge
    # shape, so the backend hashes stay reachable and stable.
    from agitrack.commits import apply_summary_to_message

    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitrackState(tmp_path)
    turn_start = repo.rev_parse("HEAD")
    backend_sha = _backend_commit(repo, "a.txt", "backend commit")
    assert _commit_turns(repo, state, [backend_sha]) is True

    message = repo.commit_message("HEAD")
    repo.amend_commit(apply_summary_to_message(message, "Rework the parser error paths"))

    assert repo.commit_message("HEAD").startswith("<aGiTrack> Rework the parser error paths")
    assert repo.parents("HEAD") == [turn_start, backend_sha]
    assert repo.commit_message(backend_sha).startswith("backend commit")


def test_cover_in_actions_mode_accumulates_trace_first(tmp_path):
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitrackState(tmp_path)
    sha = _backend_commit(repo, "a.txt", "backend commit")

    committed = _commit_turns(repo, state, [sha], accumulate_trace_only_on_commit=True)

    assert committed is True
    head_message = repo.commit_message("HEAD")
    assert "add the feature" in head_message
    assert f"covered_commits: {repo.short_sha(sha)}" in head_message


# --- runner detection and attach flow ----------------------------------------


def _detection_runner(tmp_path):
    repo = GitRepo.init(tmp_path)
    base = repo.current_branch()
    repo.switch("agitrack/test/s1/t1", create=True)
    runner = make_runner(
        repo=repo,
        state=AgitrackState(tmp_path),
        worktree=object(),
        _base_branch=base,
    )
    return runner, repo


def test_uncovered_backend_commits_detects_commits_after_last_agitrack_commit(tmp_path):
    runner, repo = _detection_runner(tmp_path)
    _backend_commit(repo, "a.txt", "covered by the aGiTrack commit after it")
    _backend_commit(repo, "b.txt", AGITRACK_BODY)  # an aGiTrack commit covers everything before it
    plain = _backend_commit(repo, "c.txt", "backend's own commit, not yet covered")

    assert runner._uncovered_backend_commits() == [plain]


def test_uncovered_backend_commits_empty_after_cover_commit(tmp_path):
    # Backend commits keep their metadata-less messages forever (#58), so the
    # detector must not re-report them once a cover commit accounts for them —
    # even while integration is still pending.
    runner, repo = _detection_runner(tmp_path)
    state = AgitrackState(tmp_path)
    first = _backend_commit(repo, "a.txt", "backend commit one")
    last = _backend_commit(repo, "b.txt", "backend commit two")
    assert _commit_turns(repo, state, [first, last]) is True

    assert runner._uncovered_backend_commits() == []


def test_uncovered_backend_commits_empty_off_turn_branch(tmp_path):
    runner, repo = _detection_runner(tmp_path)
    _backend_commit(repo, "a.txt", "backend's own commit")
    repo.switch(runner._base_branch)

    assert runner._uncovered_backend_commits() == []


def test_uncovered_backend_commits_empty_in_worktree_mode_without_a_worktree(tmp_path):
    # Defensive: worktree MODE (use_worktrees True) but the worktree object is missing —
    # nothing to scan. (The real --no-worktree path is keyed off _use_worktrees, below.)
    runner, repo = _detection_runner(tmp_path)
    _backend_commit(repo, "a.txt", "backend's own commit")
    runner.worktree = None

    assert runner._uncovered_backend_commits() == []


def _no_worktree_runner(tmp_path):
    """A runner in real --no-worktree mode, anchored at the current HEAD."""
    repo = GitRepo.init(tmp_path)
    _backend_commit(repo, "seed.txt", "pre-existing user history before aGiTrack")
    runner = make_runner(repo=repo, state=AgitrackState(tmp_path), _base_branch=repo.current_branch())
    runner.worktree = None
    runner._use_worktrees = False
    runner._noworktree_base_head = repo.rev_parse("HEAD")  # anchor at session start
    return runner, repo


def test_uncovered_backend_commits_covers_agent_commits_in_no_worktree_mode(tmp_path):
    # --no-worktree: the agent commits on the current branch directly; those commits
    # (past the session-start anchor, no aGiTrack metadata) must be detected so the
    # cover machinery can wrap them (#35).
    runner, repo = _no_worktree_runner(tmp_path)
    one = _backend_commit(repo, "a.txt", "agent commit one")
    two = _backend_commit(repo, "b.txt", "agent commit two")

    assert runner._uncovered_backend_commits() == [one, two]


def test_uncovered_backend_commits_no_worktree_excludes_pre_existing_history(tmp_path):
    # The anchor floors the scan: commits made before the session started are the user's
    # history and must NEVER be covered.
    runner, repo = _no_worktree_runner(tmp_path)
    new = _backend_commit(repo, "new.txt", "agent commit after session start")

    uncovered = runner._uncovered_backend_commits()
    assert uncovered == [new]  # only the post-anchor commit; the seed is untouched


def test_uncovered_backend_commits_no_worktree_metadata_commit_resets(tmp_path):
    # An aGiTrack metadata (cover) commit accounts for everything before it, so only
    # commits newer than it stay uncovered.
    runner, repo = _no_worktree_runner(tmp_path)
    _backend_commit(repo, "a.txt", "agent commit")
    _backend_commit(repo, "meta.txt", AGITRACK_BODY)  # aGiTrack metadata commit resets
    after = _backend_commit(repo, "c.txt", "agent commit after the cover")

    assert runner._uncovered_backend_commits() == [after]


def test_uncovered_backend_commits_no_worktree_without_anchor_is_empty(tmp_path):
    # No anchor recorded → never walk back into unbounded history; report nothing.
    runner, repo = _no_worktree_runner(tmp_path)
    runner._noworktree_base_head = None
    _backend_commit(repo, "a.txt", "agent commit")

    assert runner._uncovered_backend_commits() == []


def _attach_runner(uncovered):
    runner = make_runner()
    runner._uncovered_backend_commits = lambda: list(uncovered)
    runner.PARSE_COOLDOWN_SECONDS = 10.0
    runner._attach_uncovered_until = 0.0
    runner.calls = SimpleNamespace(started=0)

    def start_parse():
        runner.calls.started += 1
        return True

    runner._start_agent_parse = start_parse
    return runner


def test_attach_proceeds_immediately_when_nothing_uncovered():
    runner = _attach_runner([])
    assert runner._attach_trace_to_backend_commits(100.0) is True


def test_attach_defers_integration_while_parse_pending():
    runner = _attach_runner(["abc"])
    runner._finish_agent_parse_if_ready = lambda **kw: None
    assert runner._attach_trace_to_backend_commits(100.0) is False
    assert runner.calls.started == 1  # kicked off the parse that will attach


def test_attach_proceeds_after_cover():
    runner = _attach_runner(["abc"])
    runner._finish_agent_parse_if_ready = lambda **kw: True
    assert runner._attach_trace_to_backend_commits(100.0) is True
    assert runner.calls.started == 0


def test_attach_gives_up_after_deadline():
    runner = _attach_runner(["abc"])
    runner._finish_agent_parse_if_ready = lambda **kw: None
    assert runner._attach_trace_to_backend_commits(100.0) is False
    # Well past the 3x parse-cooldown deadline: integrate as-is, don't stall.
    assert runner._attach_trace_to_backend_commits(200.0) is True
