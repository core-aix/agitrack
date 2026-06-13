"""Backend-made commits get aGiT's trace/metadata attached (issues #35, #58).

Some backends commit on their own (a Claude Code hook, or an agent told to run
`git commit`). Those commits leave the worktree clean, so aGiT's normal
stage-and-commit path never runs and the turn's provenance was lost.

Amending the backend's latest commit (the original #35 fix) changed its hash,
which broke references the agent had already published in PRs and issues
(#58). The trace/metadata now ride a *cover commit* instead — the GitHub PR
merge-commit shape: tree of the backend's head, parents (turn start, backend
head) — so every backend-made hash stays exactly what the agent reported.
"""

from pathlib import Path
from types import SimpleNamespace

from agit.backends.base import TokenUsage
from agit.commits import build_agent_commit_message
from agit.config import AgitState
from agit.git import GitRepo
from agit.proxy.commit_engine import CommitEngine
from agit.transcripts.types import SessionTurn

from proxy_helpers import make_runner

AGIT_BODY = "internal change\n\n# aGiT Metadata\ncommit_type: agent\n"


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
    repo.switch("agit/test/s1/t1", create=True)
    return repo, base


def _backend_commit(repo: GitRepo, name: str, message: str) -> str:
    (repo.repo / name).write_text(f"{name} content\n", encoding="utf-8")
    repo.stage_paths([name])
    repo.commit(message)
    return repo.rev_parse("HEAD")


def _commit_turns(repo: GitRepo, state: AgitState, backend_commits: list[str], **kwargs) -> bool:
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
        agit_session_id="agit-1",
        model=None,
    )
    with_covers = build_agent_commit_message(**kwargs, covered_commits=["abc123"])
    without = build_agent_commit_message(**kwargs)
    assert "covered_commits: abc123" in with_covers
    assert "covered_commits" not in without


# --- commit_turns cover path ---------------------------------------------------


def test_clean_tree_covers_backend_commits_without_rewriting_them(tmp_path):
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitState(tmp_path)
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
    assert head_message.startswith("<aGiT> add the feature")
    assert "# Interaction Trace" in head_message
    assert f"covered_commits: {repo.short_sha(first)} {repo.short_sha(last)}" in head_message
    assert state.pending_trace() == []  # trace consumed by the cover commit


def test_cover_commit_makes_first_parent_log_turn_level(tmp_path):
    # `git log --first-parent` on the branch reads turn-by-turn: one aGiT
    # cover commit, with the backend's commits reachable via the second parent.
    repo, base = _repo_on_turn_branch(tmp_path)
    state = AgitState(tmp_path)
    first = _backend_commit(repo, "a.txt", "backend commit one")
    last = _backend_commit(repo, "b.txt", "backend commit two")

    assert _commit_turns(repo, state, [first, last]) is True

    output = repo._run(
        ["git", "log", "--first-parent", "--format=%s", f"{base}..HEAD"],
    ).stdout.splitlines()
    assert len(output) == 1
    assert output[0].startswith("<aGiT> add the feature")


def test_clean_tree_without_backend_commits_does_not_commit(tmp_path):
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitState(tmp_path)
    head = repo.rev_parse("HEAD")

    assert _commit_turns(repo, state, []) is False
    assert repo.rev_parse("HEAD") == head


def test_cover_refused_when_head_is_not_the_latest_backend_commit(tmp_path):
    # An aGiT commit sits on top of the backend's: it already accounts for the
    # turn, so the engine must not stack another cover commit.
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitState(tmp_path)
    backend_sha = _backend_commit(repo, "a.txt", "backend commit")
    _backend_commit(repo, "b.txt", AGIT_BODY)
    head = repo.rev_parse("HEAD")

    assert _commit_turns(repo, state, [backend_sha]) is False
    assert repo.rev_parse("HEAD") == head
    assert repo.commit_message("HEAD").startswith("internal change")


def test_staged_changes_commit_lists_backend_commits_as_covered(tmp_path):
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitState(tmp_path)
    backend_sha = _backend_commit(repo, "a.txt", "backend commit")
    (repo.repo / "a.txt").write_text("further uncommitted work\n", encoding="utf-8")

    committed = _commit_turns(repo, state, [backend_sha])

    assert committed is True
    head_message = repo.commit_message("HEAD")
    assert head_message.startswith("<aGiT> add the feature")
    assert f"covered_commits: {repo.short_sha(backend_sha)}" in head_message
    # The backend's own commit is preserved below, message intact.
    assert repo.rev_parse("HEAD^") == backend_sha
    assert repo.commit_message("HEAD^").startswith("backend commit")


def test_cover_commit_survives_summary_amend_with_parents_intact(tmp_path):
    # The async summary (#8) amends the COVER commit — aGiT's own commit,
    # created moments earlier — never the backend's. The amend keeps the merge
    # shape, so the backend hashes stay reachable and stable.
    from agit.commits import apply_summary_to_message

    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitState(tmp_path)
    turn_start = repo.rev_parse("HEAD")
    backend_sha = _backend_commit(repo, "a.txt", "backend commit")
    assert _commit_turns(repo, state, [backend_sha]) is True

    message = repo.commit_message("HEAD")
    repo.amend_commit(apply_summary_to_message(message, "Rework the parser error paths"))

    assert repo.commit_message("HEAD").startswith("<aGiT> Rework the parser error paths")
    assert repo.parents("HEAD") == [turn_start, backend_sha]
    assert repo.commit_message(backend_sha).startswith("backend commit")


def test_cover_in_actions_mode_accumulates_trace_first(tmp_path):
    repo, _base = _repo_on_turn_branch(tmp_path)
    state = AgitState(tmp_path)
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
    repo.switch("agit/test/s1/t1", create=True)
    runner = make_runner(
        repo=repo,
        state=AgitState(tmp_path),
        worktree=object(),
        _base_branch=base,
    )
    return runner, repo


def test_uncovered_backend_commits_detects_commits_after_last_agit_commit(tmp_path):
    runner, repo = _detection_runner(tmp_path)
    _backend_commit(repo, "a.txt", "covered by the aGiT commit after it")
    _backend_commit(repo, "b.txt", AGIT_BODY)  # an aGiT commit covers everything before it
    plain = _backend_commit(repo, "c.txt", "backend's own commit, not yet covered")

    assert runner._uncovered_backend_commits() == [plain]


def test_uncovered_backend_commits_empty_after_cover_commit(tmp_path):
    # Backend commits keep their metadata-less messages forever (#58), so the
    # detector must not re-report them once a cover commit accounts for them —
    # even while integration is still pending.
    runner, repo = _detection_runner(tmp_path)
    state = AgitState(tmp_path)
    first = _backend_commit(repo, "a.txt", "backend commit one")
    last = _backend_commit(repo, "b.txt", "backend commit two")
    assert _commit_turns(repo, state, [first, last]) is True

    assert runner._uncovered_backend_commits() == []


def test_uncovered_backend_commits_empty_off_turn_branch(tmp_path):
    runner, repo = _detection_runner(tmp_path)
    _backend_commit(repo, "a.txt", "backend's own commit")
    repo.switch(runner._base_branch)

    assert runner._uncovered_backend_commits() == []


def test_uncovered_backend_commits_empty_without_worktree(tmp_path):
    runner, repo = _detection_runner(tmp_path)
    _backend_commit(repo, "a.txt", "backend's own commit")
    runner.worktree = None  # --no-worktree mode: never touch the user's branch

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
