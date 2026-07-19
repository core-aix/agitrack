"""Tests for the dashboard's routing panel (agitrack/metrics/routing.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path


from agitrack.routing import record_event
from agitrack.routing.store import SignalEvent
from agitrack.git import GitRepo
from agitrack.metrics import routing as routing_page


def _init_repo(path: Path) -> GitRepo:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


def _store(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    return repo


def test_routing_state_returns_profile(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    # The user_id resolution goes through the real gh CLI; record under the
    # resolved id so the test passes in any environment.
    from agitrack.routing.store import user_id

    me = user_id(repo, GitRepo.discover(repo))
    record_event(
        repo,
        me,
        SignalEvent(kind="judge_accept", model="claude-opus-4-8", backend="claude"),
    )
    state = routing_page.routing_state(repo, GitRepo.discover(repo))
    assert state["me"] == me
    assert "claude-opus-4-8" in state["profile"]["models"]
    assert isinstance(state["events"], list)


def test_post_rate_records_rating(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    result = routing_page.post_rate(repo, GitRepo.discover(repo), rating=5, model="claude-opus-4-8")
    assert "state" in result
    assert "claude-opus-4-8" in result["state"]["profile"]["models"]
    assert result["state"]["profile"]["models"]["claude-opus-4-8"]["rating_count"] == 1


def test_post_rate_rejects_out_of_range(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    assert "error" in routing_page.post_rate(repo, None, rating=0)
    assert "error" in routing_page.post_rate(repo, None, rating=6)
    assert "error" in routing_page.post_rate(repo, None, rating="not-a-number")


def test_post_sync_no_repo_returns_error(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    result = routing_page.post_sync(repo, None, enabled=True)
    assert "error" in result


def test_routing_html_is_well_formed(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    html = routing_page.routing_html(repo)
    assert html.startswith("<!doctype html>")
    assert "routing-table" in html
    assert "/routing/state" in html
    assert "/routing/rate" in html
    assert "/routing/sync" in html
