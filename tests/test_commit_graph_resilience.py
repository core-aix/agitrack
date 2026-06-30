"""History reads must survive a stale/corrupt commit-graph (dashboard "no commits" bug).

git writes a commit-graph during background ``git gc --auto``, which a busy repo triggers
constantly — aGiTrack commits every turn. If a later repack moves the objects the graph
indexes, the graph's position-based lookups stop matching the store and ``git log`` aborts
mid-traversal with a NON-ZERO exit ("commit <sha> exists in commit-graph but not in the
object database"), emitting truncated or empty output. Every read path passes ``check=False``,
so that partial output was used as-is and the dashboard showed *no commits* — but only on the
active repos that gc enough to stale their graph (small or idle repos never reproduce it,
which is exactly why it "works for smaller repos"). ``GitRepo._run`` therefore opts read-only
traversals out of the commit-graph so git walks the object store directly and always returns
the full history.
"""

import subprocess
from pathlib import Path

import agitrack.git.repo as repo_mod
from agitrack.git import GitRepo
from agitrack.metrics.collect import collect_commit_stats


def _captured_git_commands(monkeypatch) -> list[list[str]]:
    """Record every argv ``GitRepo._run`` hands to ``subprocess.run``."""
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(command, *args, **kwargs):
        if isinstance(command, list):
            seen.append(list(command))
        return real(command, *args, **kwargs)

    monkeypatch.setattr(repo_mod.subprocess, "run", spy)
    return seen


def test_history_traversals_opt_out_of_commit_graph(tmp_path: Path, monkeypatch) -> None:
    repo = GitRepo.init(tmp_path)
    (tmp_path / "f.txt").write_text("hi\n", encoding="utf-8")
    repo.stage_paths(["f.txt"])
    repo.commit("c1")

    seen = _captured_git_commands(monkeypatch)
    repo._run(["git", "log", "--format=%H", "HEAD"], check=False)
    repo._run(["git", "rev-list", "--count", "HEAD"], check=False)
    repo._run(["git", "shortlog", "-s", "HEAD"], check=False)

    for cmd in seen:
        sub = cmd[1] if len(cmd) > 1 else ""
        # Each traversal runs as ``git -c core.commitGraph=false <sub> ...`` so a stale
        # graph can never truncate it.
        assert cmd[:3] == ["git", "-c", "core.commitGraph=false"], cmd
        assert sub == "-c"
        assert cmd[3] in ("log", "rev-list", "shortlog"), cmd


def test_mutations_and_plumbing_keep_the_commit_graph(tmp_path: Path, monkeypatch) -> None:
    repo = GitRepo.init(tmp_path)
    (tmp_path / "f.txt").write_text("hi\n", encoding="utf-8")
    repo.stage_paths(["f.txt"])

    seen = _captured_git_commands(monkeypatch)
    repo.commit("c1")
    repo.current_branch()  # git rev-parse --abbrev-ref
    repo.list_branches()  # git for-each-ref

    # Only history *traversal* opts out — the flag must not leak onto commits (which write
    # the graph) or onto plumbing that never traverses, where it would be dead weight.
    for cmd in seen:
        if "core.commitGraph=false" in cmd:
            assert cmd[3] in ("log", "rev-list", "shortlog"), cmd


def test_collect_reads_full_history_without_the_commit_graph(tmp_path: Path, monkeypatch) -> None:
    repo = GitRepo.init(tmp_path)
    for i in range(6):
        (tmp_path / f"f{i}.txt").write_text(f"{i}\n", encoding="utf-8")
        repo.stage_paths([f"f{i}.txt"])
        repo.commit(f"c{i}")
    # Even with a commit-graph present, the collector must not route any ``git log`` through it.
    repo._run(["git", "commit-graph", "write", "--reachable"], check=False)

    seen = _captured_git_commands(monkeypatch)
    stats = collect_commit_stats(repo, "HEAD")

    assert len(stats) == 7  # the GitRepo.init seed commit + the 6 here
    log_calls = [c for c in seen if "log" in c]
    assert log_calls, "collect_commit_stats should run git log"
    for cmd in log_calls:
        assert "core.commitGraph=false" in cmd, cmd
