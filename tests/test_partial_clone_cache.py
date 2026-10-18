"""On a partial clone, a restarted dashboard must not diff the same commits again every build.

The per-commit diff caches only recorded commits that produced diff rows there, since "no rows"
can mean "the blobs are still on the remote". Merges never produce rows, and commits whose blobs
were never fetched produce none either, so every rebuild diffed all of them again: 9 s for the
line counts and 27 s for the file browser on this repository, after every restart.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agitrack.git import GitRepo
from agitrack.metrics import collect, files


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


def _partial_clone(tmp_path: Path) -> tuple[GitRepo, dict[str, str]]:
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "t")
    _git(src, "config", "uploadpack.allowFilter", "true")
    _git(src, "config", "uploadpack.allowAnySHA1InWant", "true")
    (src / "a.txt").write_text("first version\n", encoding="utf-8")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "one")
    (src / "a.txt").write_text("second version\nmore\n", encoding="utf-8")
    _git(src, "commit", "-qam", "two")  # its diff needs the FIRST version, which is never fetched
    _git(src, "checkout", "-qb", "side")
    (src / "b.txt").write_text("side\n", encoding="utf-8")
    _git(src, "add", ".")
    _git(src, "commit", "-qm", "side")
    _git(src, "checkout", "-q", "main")
    _git(src, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    dst = tmp_path / "dst"
    subprocess.run(["git", "clone", "-q", "--filter=blob:none", f"file://{src}", str(dst)], check=True)
    repo = GitRepo(dst)
    assert repo.is_partial_clone()
    shas = {line.split(" ", 1)[1]: line.split(" ", 1)[0] for line in _git(dst, "log", "--format=%H %s").splitlines()}
    return repo, shas


def _count_numstat_calls(monkeypatch, module) -> list[list[str]]:
    calls: list[list[str]] = []
    real = GitRepo._run

    def counting(self, args, *a, **k):
        if "--numstat" in args:
            calls.append(list(args))
        return real(self, args, *a, **k)

    monkeypatch.setattr(GitRepo, "_run", counting)
    return calls


def test_a_rebuild_on_a_partial_clone_diffs_nothing_it_has_diffed_before(tmp_path, monkeypatch):
    repo, shas = _partial_clone(tmp_path)
    collect.collect_dashboard_stats(repo, "HEAD")  # the first index fills the cache

    cached = collect._read_numstat_cache(repo)
    assert cached[shas["merge side"]][:2] == (0, 0)  # a merge is recorded: its 0 is final
    assert cached[shas["two"]][2].startswith(collect._NUMSTAT_MISSING)  # blobs still remote

    calls = _count_numstat_calls(monkeypatch, collect)
    collect.collect_dashboard_stats(repo, "HEAD")
    assert calls == []  # the restarted dashboard asks git for no diff at all


def test_a_commit_whose_blobs_were_remote_is_tried_again_a_day_later(tmp_path, monkeypatch):
    repo, shas = _partial_clone(tmp_path)
    collect.collect_dashboard_stats(repo, "HEAD")
    later = collect.time.time() + collect._MISSING_RETRY_SECONDS + 60
    monkeypatch.setattr(collect.time, "time", lambda: later)
    calls = _count_numstat_calls(monkeypatch, collect)
    collect.collect_dashboard_stats(repo, "HEAD")
    assert len(calls) == 1  # retried, not frozen at zero for good...
    cached = collect._read_numstat_cache(repo)
    assert cached[shas["two"]][2].startswith(collect._NUMSTAT_MISSING)  # ...and stamped afresh


def test_the_file_browser_on_a_partial_clone_diffs_nothing_twice(tmp_path, monkeypatch):
    repo, shas = _partial_clone(tmp_path)
    known = set(shas.values())
    first = files._numstat_by_commit(repo, known)
    assert shas["merge side"] not in first or first[shas["merge side"]] == []

    calls = _count_numstat_calls(monkeypatch, files)
    files._numstat_by_commit(repo, known)
    assert calls == []
