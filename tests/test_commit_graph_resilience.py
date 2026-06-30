"""History reads must survive a busy repo's git internals (dashboard "no commits" bug).

aGiTrack commits every turn, which constantly fires background ``git gc --auto``. That
destabilises history reads two ways, and ``check=False`` (every read path) then uses the
aborted/partial output as-is, so the dashboard shows a truncated — or empty — commit log.
Small/idle repos never gc enough to reproduce it, which is why it "works for smaller repos":

  1. STALE COMMIT-GRAPH — a repack moves the objects the graph indexes, so a walk aborts
     NON-ZERO ("commit <sha> exists in commit-graph but not in the object database").
  2. LAZY (PROMISOR) FETCH on a blobless partial clone — mid-repack, a walk briefly can't
     find a local object and tries to fetch it from origin; for aGiTrack's own local-only
     commits the remote answers "not our ref" and the read aborts. The count then flaps.

``GitRepo._run`` disables the commit-graph for read-only traversals, and disables lazy
fetching for the ones that don't need file CONTENT (so they use the local objects instead
of a doomed network fetch). Content reads (``--numstat`` line counts, ``-p`` diffs) keep
lazy fetch ON, since on a blobless clone they legitimately fetch absent blobs.
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


def _config_values(cmd: list[str], key: str) -> list[str]:
    """Values of every ``-c <key>=<value>`` pair in a git argv."""
    out = []
    for i, a in enumerate(cmd):
        if a == "-c" and i + 1 < len(cmd) and cmd[i + 1].startswith(key + "="):
            out.append(cmd[i + 1].split("=", 1)[1])
    return out


def test_content_free_traversals_disable_commit_graph_and_lazy_fetch(tmp_path: Path, monkeypatch) -> None:
    repo = GitRepo.init(tmp_path)
    (tmp_path / "f.txt").write_text("hi\n", encoding="utf-8")
    repo.stage_paths(["f.txt"])
    repo.commit("c1")

    seen = _captured_git_commands(monkeypatch)
    repo._run(["git", "log", "--format=%H", "HEAD"], check=False)
    repo._run(["git", "rev-list", "--count", "HEAD"], check=False)
    repo._run(["git", "shortlog", "-s", "HEAD"], check=False)

    for cmd in seen:
        # ``git -c core.commitGraph=false -c fetch.disableLazyFetch=true <sub> ...`` — neither a
        # stale graph nor a doomed promisor fetch can truncate a content-free walk.
        assert _config_values(cmd, "core.commitGraph") == ["false"], cmd
        assert _config_values(cmd, "fetch.disableLazyFetch") == ["true"], cmd
        # The subcommand is the first bare token (skip ``-c`` and its ``key=value`` operands).
        sub = next(a for a in cmd[1:] if not a.startswith("-") and "=" not in a)
        assert sub in ("log", "rev-list", "shortlog"), cmd


def test_numstat_keeps_lazy_fetch_for_blob_content(tmp_path: Path, monkeypatch) -> None:
    repo = GitRepo.init(tmp_path)
    (tmp_path / "f.txt").write_text("hi\n", encoding="utf-8")
    repo.stage_paths(["f.txt"])
    repo.commit("c1")

    seen = _captured_git_commands(monkeypatch)
    repo._run(["git", "log", "--numstat", "--format=%H", "HEAD"], check=False)

    cmd = seen[0]
    # A line-count read still needs blob objects, which a blobless partial clone fetches
    # lazily — so the commit-graph is still bypassed, but lazy fetch must stay ON or the
    # numbers would be zeroed.
    assert _config_values(cmd, "core.commitGraph") == ["false"], cmd
    assert _config_values(cmd, "fetch.disableLazyFetch") == [], cmd


def test_mutations_and_plumbing_are_untouched(tmp_path: Path, monkeypatch) -> None:
    repo = GitRepo.init(tmp_path)
    (tmp_path / "f.txt").write_text("hi\n", encoding="utf-8")
    repo.stage_paths(["f.txt"])

    seen = _captured_git_commands(monkeypatch)
    repo.commit("c1")
    repo.current_branch()  # git rev-parse --abbrev-ref
    repo.list_branches()  # git for-each-ref

    # Only history *traversal* opts out — the flags must not leak onto commits (which write
    # the graph) or onto plumbing that never traverses, where they would be dead weight.
    for cmd in seen:
        injected = _config_values(cmd, "core.commitGraph") + _config_values(cmd, "fetch.disableLazyFetch")
        if injected:
            assert any(sub in cmd for sub in ("log", "rev-list", "shortlog")), cmd


def test_collect_reads_full_history_resiliently(tmp_path: Path, monkeypatch) -> None:
    repo = GitRepo.init(tmp_path)
    for i in range(6):
        (tmp_path / f"f{i}.txt").write_text(f"{i}\n", encoding="utf-8")
        repo.stage_paths([f"f{i}.txt"])
        repo.commit(f"c{i}")
    # Even with a commit-graph present, the collector must not trust it for any ``git log``.
    repo._run(["git", "commit-graph", "write", "--reachable"], check=False)

    seen = _captured_git_commands(monkeypatch)
    stats = collect_commit_stats(repo, "HEAD")

    assert len(stats) == 7  # the GitRepo.init seed commit + the 6 here
    log_calls = [c for c in seen if "log" in c]
    assert log_calls, "collect_commit_stats should run git log"
    for cmd in log_calls:
        assert "core.commitGraph=false" in cmd, cmd
        # The plain commit-list / parents walks (no --numstat) also drop lazy fetch.
        if "--numstat" not in cmd:
            assert "fetch.disableLazyFetch=true" in cmd, cmd
