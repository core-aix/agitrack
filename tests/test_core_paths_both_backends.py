"""The parity-relevant core paths, run on EVERY backend.

The suite's runner-level tests all build their subject through ``ProxyRunner.for_testing()``,
which seeded a hardcoded ``"claude"`` — so roughly 260 tests exercised one backend and no
runner behaviour was ever executed on OpenCode. That is how OpenCode came to ship with no
turn-end signal at all: every test that would have caught it ran on Claude.

This file covers the paths where the backend is genuinely a variable, parameterized over
``available_backends()`` so a third backend is included the moment it is registered. It is
deliberately NOT a copy of the whole suite — most behaviour is backend-independent and
doubling it would only double the runtime. The chosen paths are the ones that read from,
write to, or branch on the backend:

* the turn parse → commit pipeline (what lands in git, and the metadata it carries);
* session identity: discovery, adoption, and the resume/retarget staging;
* token accounting (each backend reports usage in its own shape);
* the spawn command (each backend takes different flags).

Real-git wherever a commit is involved: the metadata is only true if git actually stored it.
"""

from __future__ import annotations

import subprocess

import pytest

from agitrack.backends.proxy_agents import available_backends
from agitrack.backends.base import TokenUsage
from agitrack.config import AgitrackState
from agitrack.git import GitRepo
from agitrack.transcripts.types import SessionRef, SessionTurn
from proxy_helpers import make_runner

BACKENDS = available_backends()


def _repo(path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


def _runner_on(backend_name, tmp_path):
    """An interactive proxy runner in no-worktree mode over a real repo, on ``backend_name``."""
    repo = _repo(tmp_path)
    state = AgitrackState(repo.repo, default_backend=backend_name)
    runner = make_runner(repo=repo, base_repo=repo, state=state, _use_worktrees=False, worktree=None)
    runner._start_commit_summary = lambda *a, **k: None  # never spawn a real summarizer
    runner.untracked_before_turn = set()
    return runner, repo


# --- turn → commit ----------------------------------------------------------


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_completed_turn_commits_with_its_backend_recorded(tmp_path, backend_name):
    # The core pipeline. Whichever agent produced the work, the commit must exist and must say
    # WHICH agent produced it — that attribution is the whole point of aGiTrack.
    runner, repo = _runner_on(backend_name, tmp_path)
    (tmp_path / "f.txt").write_text("agent edit\n")

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[SessionTurn("u1", "a1", "add a feature", "done", TokenUsage(total=120, output=120), "m")],
        backend=backend_name,
        backend_session_id="s1",
        model="m",
        quiet=True,
    )

    assert committed is True
    # In no-worktree mode with a dirty tree the turn is recorded LATENTLY (on
    # refs/agitrack/manual/<session>) rather than landing on the branch — that is the mode's
    # whole point, so the recorded body is the artifact to check.
    body = runner._manual_pending_bodies()[-1]
    assert f"backend: {backend_name}" in body
    assert "add a feature" in body


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_the_commit_records_the_turns_token_counts(tmp_path, backend_name):
    # Token counts ride on every commit and feed the dashboard's cost reporting. Each backend
    # reports usage in its own shape, so this is exactly where a per-backend bug hides — and a
    # wrong number looks just like a right one.
    runner, repo = _runner_on(backend_name, tmp_path)
    (tmp_path / "f.txt").write_text("agent edit\n")

    runner._create_agent_commit_from_turns_popup(
        turns=[
            SessionTurn(
                "u1",
                "a1",
                "do work",
                "done",
                TokenUsage(total=250, input=1000, output=250, cache_read=50),
                "m",
            )
        ],
        backend=backend_name,
        backend_session_id="s1",
        model="m",
        quiet=True,
    )

    body = runner._manual_pending_bodies()[-1]
    assert "tokens" in body.lower()
    assert "250" in body


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_nothing_is_committed_when_the_agent_changed_nothing(tmp_path, backend_name):
    # An empty turn must not produce an empty commit on any backend — the history would fill
    # with noise that says work happened when none did.
    runner, repo = _runner_on(backend_name, tmp_path)
    head_before = repo.rev_parse("HEAD")

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[SessionTurn("u1", "a1", "think about it", "nothing to change", TokenUsage(), "m")],
        backend=backend_name,
        backend_session_id="s1",
        model="m",
        quiet=True,
    )

    assert committed is False
    assert repo.rev_parse("HEAD") == head_before


# --- session identity -------------------------------------------------------


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_conversation_touched_since_launch_is_adopted(tmp_path, backend_name):
    # `_discover_spawned_session` is how aGiTrack follows a switch made inside the backend. The
    # safety property is "since launch": a conversation nobody has touched can never be adopted,
    # however recent it looks.
    runner, _ = _runner_on(backend_name, tmp_path)
    runner._pre_spawn_sessions = {"old": 100.0, "untouched": 900.0}

    class _Backend:
        name = backend_name

        def list_sessions(self, _repo):
            return [
                SessionRef(id="old", updated=500.0, label="moved on since launch"),
                SessionRef(id="untouched", updated=900.0, label="looks recent, never touched"),
            ]

    runner.backend = _Backend()

    assert runner._discover_spawned_session() == "old"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_nothing_is_adopted_when_no_conversation_moved(tmp_path, backend_name):
    runner, _ = _runner_on(backend_name, tmp_path)
    runner._pre_spawn_sessions = {"a": 100.0}

    class _Backend:
        name = backend_name

        def list_sessions(self, _repo):
            return [SessionRef(id="a", updated=100.0, label="unchanged")]

    runner.backend = _Backend()

    assert runner._discover_spawned_session() is None


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_resume_staging_retargets_the_recorded_working_dir(tmp_path, backend_name):
    # `_stage_backend_resume` runs on every backend and is what stops a resumed session from
    # reopening in an old worktree. Both agents implement `retarget_working_dir`; this checks
    # the runner actually calls it, with this session's launch directory.
    runner, repo = _runner_on(backend_name, tmp_path)
    calls = []

    class _Backend:
        name = backend_name

        def ensure_resumable(self, _repo, _sid):
            return True

        def retarget_working_dir(self, repo_path, session_id, cwd, *, git_branch=None):
            calls.append((session_id, cwd, git_branch))
            return True

    runner.backend = _Backend()

    runner._stage_backend_resume("session-1")

    assert calls == [("session-1", str(repo.repo), "main")]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_resume_staging_survives_a_backend_that_raises(tmp_path, backend_name):
    # Staging is best-effort and runs during startup. An exception here would abort the launch
    # entirely, which is far worse than a resume that lands in the wrong directory.
    runner, _ = _runner_on(backend_name, tmp_path)

    class _Backend:
        name = backend_name

        def ensure_resumable(self, _repo, _sid):
            raise RuntimeError("transcript store unavailable")

        def retarget_working_dir(self, *a, **k):
            raise RuntimeError("also broken")

    runner.backend = _Backend()

    runner._stage_backend_resume("session-1")  # must not raise


# --- spawn ------------------------------------------------------------------


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_the_factory_can_build_a_runner_on_any_backend(tmp_path, backend_name):
    # The lever this file exists to exercise. While for_testing() hardcoded "claude", no
    # runner-level test could run on another backend at all.
    # A state with NO backend recorded — the situation the factory's seeding exists for.
    state = AgitrackState(tmp_path)
    runner = make_runner(state=state, backend_name=backend_name)
    assert runner.state.backend == backend_name


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_resume_spawn_names_the_session_on_every_backend(tmp_path, backend_name):
    # Each backend takes a different flag for "resume this conversation" (`--resume` vs
    # `--session`), but the id must appear in the argv either way — otherwise the agent starts a
    # brand-new conversation and the user silently loses their history.
    from agitrack.backends.proxy_agents import make_proxy_agent

    agent = make_proxy_agent(backend_name)
    command = agent.spawn_command(tmp_path, session_id="ses-42", resume=True)

    assert "ses-42" in command
