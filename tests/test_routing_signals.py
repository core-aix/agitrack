"""Tests for agitrack.routing.signals: convenience recorders."""

from __future__ import annotations

import subprocess
from pathlib import Path


from agitrack.routing.signals import (
    record_cancel,
    record_discard,
    record_post_agent_edit,
    record_rating,
    record_redo_followup,
    record_reroute,
    record_revert,
    record_switch,
)
from agitrack.routing.store import (
    EVENT_KIND_CANCEL,
    EVENT_KIND_DISCARD,
    EVENT_KIND_POST_EDIT,
    EVENT_KIND_REDO,
    EVENT_KIND_REROUTE,
    EVENT_KIND_SWITCH,
    load_profile,
    user_id,
)


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


def _store(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    return repo


def test_record_rating_films_in_correct_cell(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    record_rating(
        repo,
        backend="claude",
        model="claude-opus-4-8",
        rating=5,
        commit="abc123",
    )
    profile = load_profile(repo, user_id(repo, None))
    record = profile["models"]["claude-opus-4-8"]
    assert record["rating_count"] == 1
    assert record["ratings"] == [5]


def test_record_rating_ignores_out_of_range(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    record_rating(repo, backend="claude", model="opus", rating=0)
    record_rating(repo, backend="claude", model="opus", rating=6)
    profile = load_profile(repo, user_id(repo, None))
    assert profile["models"] == {}


def test_record_discard(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    record_discard(repo, backend="claude", model="opus", commit="abc")
    profile = load_profile(repo, user_id(repo, None))
    assert profile["models"]["opus"]["discards"] == 1
    events = profile["events"]
    assert events and events[0]["kind"] == EVENT_KIND_DISCARD


def test_record_cancel(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    record_cancel(repo, backend="claude", model="opus")
    profile = load_profile(repo, user_id(repo, None))
    events = profile["events"]
    assert events[0]["kind"] == EVENT_KIND_CANCEL


def test_record_revert(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    record_revert(repo, backend="claude", model="opus", commit="abc")
    profile = load_profile(repo, user_id(repo, None))
    assert profile["models"]["opus"]["reverts"] == 1


def test_record_redo_followup(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    record_redo_followup(repo, backend="claude", model="opus")
    profile = load_profile(repo, user_id(repo, None))
    assert profile["events"][0]["kind"] == EVENT_KIND_REDO


def test_record_post_agent_edit(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    record_post_agent_edit(repo, backend="claude", model="opus")
    profile = load_profile(repo, user_id(repo, None))
    assert profile["events"][0]["kind"] == EVENT_KIND_POST_EDIT


def test_record_switch(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    record_switch(repo, backend="claude", from_model="opus", to_model="haiku", session="main")
    profile = load_profile(repo, user_id(repo, None))
    events = profile["events"]
    assert events[0]["kind"] == EVENT_KIND_SWITCH
    assert events[0]["value"] == {"from": "opus", "to": "haiku"}


def test_record_reroute(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    record_reroute(repo, backend="claude", from_model="opus", to_model="haiku")
    profile = load_profile(repo, user_id(repo, None))
    events = profile["events"]
    assert events[0]["kind"] == EVENT_KIND_REROUTE


def test_signals_swallows_corrupt_store(tmp_path: Path) -> None:
    """A failure inside the recorder must never propagate. The runner's
    contract is that signal recording is best-effort."""
    repo = _store(tmp_path)
    # Patch the store to raise on load.
    import agitrack.routing.signals as sig

    original_record = sig.record_event

    def boom(*args, **kwargs):
        raise RuntimeError("store corrupted")

    sig.record_event = boom
    try:
        # None of these should raise.
        record_rating(repo, backend="claude", model="opus", rating=5)
        record_discard(repo, backend="claude", model="opus")
        record_cancel(repo, backend="claude", model="opus")
        record_revert(repo, backend="claude", model="opus")
        record_redo_followup(repo, backend="claude", model="opus")
        record_post_agent_edit(repo, backend="claude", model="opus")
        record_switch(repo, backend="claude", from_model="opus", to_model="haiku")
        record_reroute(repo, backend="claude", from_model="opus", to_model="haiku")
    finally:
        sig.record_event = original_record
