"""`agit --dashboard` (#54): repo metrics computed from aGiT commit metadata.

The collector reads everything from `git log` alone, so the tests build a real
repository whose history contains every commit kind the classifier knows:
untracked (plain git), user (commit_type: user), agent (commit_type: agent,
with token metadata), backend-made commits covered by a merge-shaped cover
commit (#58), and an agent-merge.
"""

from pathlib import Path

from agit import cli
from agit.commits import build_agent_commit_message, build_agent_merge_message, build_user_commit_message
from agit.git import GitRepo
from agit.metrics import build_dashboard, render_dashboard
from agit.metrics.collect import CommitStat, _detect_loops


def _write_lines(repo: GitRepo, name: str, count: int) -> None:
    (repo.repo / name).write_text("".join(f"line {i}\n" for i in range(count)), encoding="utf-8")
    repo.stage_paths([name])


def _agent_message(prompt: str, *, tokens: dict | None = None, covered: list[str] | None = None) -> str:
    return build_agent_commit_message(
        latest_prompt=prompt,
        trace=[{"role": "user", "content": prompt}, {"role": "agent", "content": "done"}],
        backend="claude",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="claude-opus-4-8",
        token_usage=tokens,
        covered_commits=covered,
    )


_TOKENS = {
    "context": 100,
    "total": 50,
    "input": 1000,
    "output": 50,
    "reasoning": 0,
    "cache_read": 0,
    "cache_write": 0,
}


def _demo_repo(tmp_path: Path) -> GitRepo:
    repo = GitRepo.init(tmp_path)  # seed commit: untracked

    _write_lines(repo, "human.txt", 10)
    repo.commit("plain human commit")  # untracked

    _write_lines(repo, "user.txt", 4)
    repo.commit(build_user_commit_message(message="save my edits", agit_session_id="agit-1"))  # user

    _write_lines(repo, "agent.txt", 20)
    repo.commit(_agent_message("add the feature", tokens=_TOKENS))  # agent, 1050 in / 50 out

    # Backend-made commit covered by a merge-shaped cover commit (#58).
    turn_start = repo.rev_parse("HEAD")
    _write_lines(repo, "backend.txt", 8)
    repo.commit("backend made this itself")
    backend_sha = repo.rev_parse("HEAD")
    repo.cover_commit(
        _agent_message("refactor the parser", tokens=_TOKENS, covered=[repo.short_sha(backend_sha)]),
        first_parent=turn_start,
        second_parent=backend_sha,
    )

    repo.commit_message("HEAD")  # sanity: readable
    repo._run(
        ["git", "commit", "--allow-empty", "-F", "-"],
        input_text=build_agent_merge_message(
            session_name="s1",
            base_branch="main",
            source_branch="agit/claude/s1/t1",
            agit_session_id="agit-1",
            backend="claude",
        ),
    )  # agent-merge
    return repo


def test_dashboard_classifies_every_commit_kind(tmp_path):
    dash = build_dashboard(_demo_repo(tmp_path))

    kinds = {stat.kind for stat in dash.stats}
    assert kinds == {"untracked", "user", "agent", "covered", "agent-merge"}
    assert dash.count("agent") == 2  # the regular agent commit + the cover commit
    assert dash.count("covered") == 1  # the backend-made commit, identified via covered_commits
    assert dash.count("untracked") == 2  # seed + plain human commit
    assert dash.total_commits == 7
    assert dash.tracked_commits == 5
    assert abs(dash.coverage - 5 / 7) < 1e-9


def test_dashboard_attributes_lines_to_ai_and_human(tmp_path):
    dash = build_dashboard(_demo_repo(tmp_path))

    ai_ins, _ = dash.ai_lines
    human_ins, _ = dash.human_lines
    # AI: agent.txt (20) + backend.txt (8, via the covered commit); the cover
    # commit itself is a merge and contributes no numstat — no double count.
    assert ai_ins == 28
    # Human: human.txt (10) + user.txt (4).
    assert human_ins == 14


def test_dashboard_sums_tokens_and_efficiency(tmp_path):
    dash = build_dashboard(_demo_repo(tmp_path))

    totals = dash.token_totals
    assert totals["input"] == 2000  # two agent commits, 1000 input tokens each
    assert totals["output"] == 100
    assert dash.lines_per_1k_output_tokens == (28 + 0) / 100 * 1000


def test_dashboard_groups_by_backend_model_and_author(tmp_path):
    dash = build_dashboard(_demo_repo(tmp_path))

    backend = dash.by_backend["claude"]
    assert backend["commits"] == 2
    # The cover commit's bucket includes the covered backend commit's lines.
    assert backend["insertions"] == 28
    assert dash.by_model["claude-opus-4-8"]["commits"] == 2
    (author_stats,) = dash.by_author.values()
    assert author_stats["commits"] == 7
    assert author_stats["agit_commits"] == 5


def test_render_dashboard_contains_all_sections(tmp_path):
    text = render_dashboard(_demo_repo(tmp_path))

    for heading in (
        "aGiT Dashboard",
        "Coverage",
        "Code changes",
        "Tokens",
        "By backend",
        "By model",
        "By committer",
        "Possible loops",
    ):
        assert heading in text
    assert "aGiT-tracked commits: 5/7" in text
    assert "claude-opus-4-8" in text


def test_pr_merge_commit_does_not_double_count_the_cover_turn(tmp_path):
    """A GitHub-style merge that inherits the cover commit's message (and so its
    metadata block) must not be counted as a second turn (#58 regression)."""
    repo = GitRepo.init(tmp_path)

    _write_lines(repo, "agent.txt", 20)
    cover_message = _agent_message("add the feature", tokens=_TOKENS)
    repo.commit(cover_message)
    cover_sha = repo.rev_parse("HEAD")

    base = build_dashboard(repo)

    # Integrate the branch with a real merge commit whose message is a verbatim
    # copy of the cover commit's — exactly what GitHub does on PR merge.
    repo._run(["git", "checkout", "-q", "-b", "base", "HEAD~1"])
    repo._run(
        ["git", "merge", "--no-ff", "-m", cover_message, cover_sha],
    )

    merged = build_dashboard(repo)

    assert merged.token_totals == base.token_totals
    assert merged.count("agent") == base.count("agent")
    assert merged.by_backend["claude"]["output_tokens"] == base.by_backend["claude"]["output_tokens"]


# --- loop detection ------------------------------------------------------------


def _agent_stat(sha: str, prompt: str, output: int = 10, user_prompts: list[str] | None = None) -> CommitStat:
    return CommitStat(
        sha=sha,
        author="a",
        subject=f"<aGiT> {prompt}",
        kind="agent",
        tokens={"output": output},
        prompt=prompt,
        user_prompts=user_prompts or [],
    )


def test_loop_detected_across_consecutive_similar_prompts():
    stats = [
        _agent_stat("a" * 40, "fix the failing parser test"),
        _agent_stat("b" * 40, "fix the failing parser test again"),
        _agent_stat("c" * 40, "please fix the failing parser test"),
        _agent_stat("d" * 40, "write the changelog"),
    ]

    (finding,) = _detect_loops(stats)
    assert finding.shas == ["a" * 7, "b" * 7, "c" * 7]
    assert finding.output_tokens == 30
    assert finding.within_commit is False


def test_no_loop_for_two_similar_prompts_or_distinct_prompts():
    stats = [
        _agent_stat("a" * 40, "fix the failing parser test"),
        _agent_stat("b" * 40, "fix the failing parser test again"),  # a retry, not a loop
        _agent_stat("c" * 40, "add the dashboard renderer"),
    ]
    assert _detect_loops(stats) == []


def test_loop_detected_within_a_single_commit_trace():
    stats = [
        _agent_stat(
            "a" * 40,
            "make the tests pass",
            output=99,
            user_prompts=["make the tests pass", "make the tests pass", "make the tests pass now"],
        )
    ]

    (finding,) = _detect_loops(stats)
    assert finding.within_commit is True
    assert finding.output_tokens == 99


def test_dashboard_loops_from_real_history(tmp_path):
    repo = GitRepo.init(tmp_path)
    for index, name in enumerate(("a.txt", "b.txt", "c.txt")):
        _write_lines(repo, name, 2)
        repo.commit(_agent_message("fix the flaky integration test", tokens=_TOKENS))

    dash = build_dashboard(repo)
    assert len(dash.loops) == 1
    assert len(dash.loops[0].shas) == 3
    assert "Possible loops" in render_dashboard(repo)


# --- CLI -----------------------------------------------------------------------


def test_cli_dashboard_prints_report_and_exits(tmp_path, capsys, monkeypatch):
    _demo_repo(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("dashboard must not prompt")))

    rc = cli.main(["--dashboard", "--repo", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "aGiT Dashboard" in out
    assert "aGiT-tracked commits" in out


def test_cli_dashboard_outside_repo_fails_cleanly(tmp_path, capsys):
    (tmp_path / "plain").mkdir()

    rc = cli.main(["--dashboard", "--repo", str(tmp_path / "plain")])

    assert rc == 1
    assert "Not a Git repository" in capsys.readouterr().out


def test_cli_dashboard_missing_directory_fails_cleanly(tmp_path):
    assert cli.main(["--dashboard", "--repo", str(tmp_path / "nowhere")]) == 1
