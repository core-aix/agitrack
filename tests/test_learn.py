"""Tests for the dashboard's learning page (agitrack/metrics/learn.py).

Real temp git repos throughout (per the testing practice); the backend agent is the only
thing faked, via a canned-JSON stand-in for ``LearningBackendChoice.build`` — so the
digest building, JSON normalization, per-user store, progress tracking, exercise logging
and git progress-sync all run for real.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from agitrack.backends.base import AgentResult, TokenUsage
from agitrack.git import GitRepo
from agitrack.metrics import learn
from agitrack.metrics.collect import CommitStat


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    (path / "README.md").write_text("# Demo project\nA thing under test.\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


def _write_state(path, backend="claude", model=None):
    agit = path / ".agitrack"
    agit.mkdir(exist_ok=True)
    (agit / "state.json").write_text(json.dumps({"backend": backend, "model": model}))


class _FakeBackend:
    """Stands in for ClaudeBackend/OpenCodeBackend: returns a canned final_response."""

    def __init__(self, response, exit_code=0):
        self.response = response
        self.exit_code = exit_code
        self.calls = []

    def run(
        self, prompt, *, model, session_id, bare=False, system_prompt=None, commit_guidance=True, timeout_seconds=None
    ):
        self.calls.append(
            {"prompt": prompt, "model": model, "bare": bare, "system": system_prompt, "timeout": timeout_seconds}
        )
        return AgentResult(
            backend="claude",
            session_id=None,
            model=model,
            final_response=self.response,
            exit_code=self.exit_code,
            tokens=TokenUsage(),
        )


@pytest.fixture
def fixed_identity(monkeypatch):
    monkeypatch.setattr(learn, "learner_id", lambda root, repo: "alice")
    return "alice"


def _fake_agent(monkeypatch, response, exit_code=0):
    fake = _FakeBackend(response, exit_code)
    monkeypatch.setattr(learn.LearningBackendChoice, "build", lambda self: fake)
    return fake


def _stat(prompt="fix the failing tests please", kind="agent", ts=1_750_000_000):
    return CommitStat(
        sha="a" * 40,
        author="tester",
        email="t@t",
        subject=prompt,
        kind=kind,
        timestamp=ts,
        prompt=prompt,
        user_prompts=[prompt],
        tokens={"output": 10},
    )


_SUGGEST_JSON = json.dumps(
    {
        "assessment": "You drive the agent confidently and iterate fast.",
        "gaps": [
            {
                "id": "git-rebase",
                "title": "Interactive rebase",
                "detail": "History edits get delegated wholesale.",
                "kind": "coding",
                "evidence": "several 'untangle my branches' prompts",
            }
        ],
        "suggestions": [
            {
                "id": "rebase-basics",
                "title": "Rebase without fear",
                "minutes": 10,
                "kind": "coding",
                "gap_id": "git-rebase",
                "why": "You asked the agent to fix branch history 4 times.",
                "teaser": "Next time you will untangle it yourself in two commands.",
            },
            {
                "id": "repo-tour",
                "title": "A tour of your hot files",
                "minutes": 15,
                "kind": "codebase",
                "gap_id": "",
                "why": "metrics/web.py is your rework hotspot.",
                "teaser": "Know where things live.",
            },
        ],
    }
)

_LESSON_JSON = json.dumps(
    {
        "title": "Rebase without fear",
        "minutes": 10,
        "steps": [
            {"title": "Why rebase", "content_md": "Because history matters to you specifically."},
            {
                "title": "Reading a rebase todo",
                "content_md": "```\npick abc1 first\nsquash def2 second\n```\nThis melds the second commit into the first.",
            },
            {"title": "Try this next time", "content_md": "Ask your agent to show the rebase plan before applying it."},
        ],
        "links": [
            {
                "title": "Git book: rewriting history",
                "url": "https://git-scm.com/book",
                "note": "the canonical chapter",
            },
            {"title": "bad", "url": "notaurl", "note": "dropped"},
        ],
        "quiz": [
            {
                "question": "What does rebase -i let you do?",
                "choices": ["Edit history", "Delete the repo", "Push force"],
                "answer": 0,
                "explain": "Interactive rebase rewrites commits.",
            }
        ],
        "exercise": {
            "task": "Create a throwaway branch, make two commits, squash them into one with rebase -i.",
            "hint": "git rebase -i HEAD~2, then change the second 'pick' to 'squash'.",
        },
    }
)


# --------------------------------------------------------------- backend resolution


def test_resolve_prefers_config_over_latest_session(tmp_path):
    repo = _init_repo(tmp_path)
    _write_state(tmp_path, backend="claude", model="claude-opus-4-8")
    (tmp_path / ".agitrack" / "config.json").write_text(json.dumps({"learning_backend": "opencode"}))
    choice = learn.resolve_learning_backend(repo.repo)
    assert choice.backend_name == "opencode"
    assert choice.backend_source == "config"
    # The session model is a Claude-style id: incompatible with OpenCode, so dropped.
    assert choice.model is None


def test_resolve_falls_back_to_latest_session(tmp_path):
    repo = _init_repo(tmp_path)
    _write_state(tmp_path, backend="claude", model="claude-haiku-4-5-20251001")
    choice = learn.resolve_learning_backend(repo.repo)
    assert (choice.backend_name, choice.backend_source) == ("claude", "session")
    assert choice.model == "claude-haiku-4-5-20251001"
    assert choice.model_source == "session"


def test_resolve_config_model_wins(tmp_path):
    repo = _init_repo(tmp_path)
    _write_state(tmp_path, backend="claude", model="claude-haiku-4-5-20251001")
    (tmp_path / ".agitrack" / "config.json").write_text(json.dumps({"learning_model": "claude-opus-4-8"}))
    choice = learn.resolve_learning_backend(repo.repo)
    assert choice.model == "claude-opus-4-8"
    assert choice.model_source == "config"


def test_resolve_without_any_backend_raises(tmp_path):
    repo = _init_repo(tmp_path)
    with pytest.raises(learn.LearnAgentError):
        learn.resolve_learning_backend(repo.repo)


def test_set_learning_config_roundtrip(tmp_path):
    repo = _init_repo(tmp_path)
    _write_state(tmp_path, backend="claude")
    result = learn.set_learning_config(repo.repo, backend="opencode", model="anthropic/claude-haiku-4-5")
    assert result["backend_info"]["backend"] == "opencode"
    stored = json.loads((tmp_path / ".agitrack" / "config.json").read_text())
    assert stored["learning_backend"] == "opencode"
    assert stored["learning_model"] == "anthropic/claude-haiku-4-5"
    # Unsetting falls back to the session backend and removes the keys.
    result = learn.set_learning_config(repo.repo, backend="", model="")
    stored = json.loads((tmp_path / ".agitrack" / "config.json").read_text())
    assert "learning_backend" not in stored and "learning_model" not in stored
    assert result["backend_info"]["backend"] == "claude"


def test_set_learning_config_rejects_unknown_backend(tmp_path):
    repo = _init_repo(tmp_path)
    assert "error" in learn.set_learning_config(repo.repo, backend="cursor", model="")


# --------------------------------------------------------------------- JSON parsing


def test_extract_json_tolerates_fences_and_prose():
    assert learn._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert learn._extract_json('Sure! Here it is: {"a": {"b": 2}} hope that helps') == {"a": {"b": 2}}
    assert learn._extract_json("no json here") is None
    assert learn._extract_json("{broken") is None


# -------------------------------------------------------------------------- digest


def test_digest_covers_prompts_files_insights_readme_and_progress(tmp_path):
    _init_repo(tmp_path)
    stats = [_stat("please fix the flaky test"), _stat("(background task completed) and now refactor")]
    insights = [{"title": "Correction loops", "summary": "1 in 4 prompts is corrective", "suggestion": "smaller asks"}]
    files = [{"path": "agitrack/metrics/web.py", "changes": 9, "insertions": 200, "deletions": 120}]
    profile = {
        "lessons": [{"title": "Rebase without fear", "status": "completed"}],
        "gaps": [{"title": "Interactive rebase", "status": "addressed"}],
    }
    digest = learn.build_trace_digest(stats, insights, files, tmp_path, profile)
    assert "please fix the flaky test" in digest
    assert "(background task completed)" not in digest  # synthetic marker stripped
    assert "Correction loops" in digest
    assert "agitrack/metrics/web.py (9 changes, +200/-120)" in digest
    assert "Demo project" in digest  # README head
    assert "Rebase without fear" in digest  # already learned
    assert "Interactive rebase" in digest  # addressed gap


def test_digest_is_capped(tmp_path):
    stats = [_stat(f"prompt number {i} " + "x" * 170) for i in range(60)]
    digest = learn.build_trace_digest(stats, [], [], tmp_path, {}, limit=2000)
    assert len(digest) <= 2000


# ------------------------------------------------------------------- suggest flow


def test_suggest_persists_profile_per_user(tmp_path, monkeypatch, fixed_identity):
    repo = _init_repo(tmp_path)
    _write_state(tmp_path)
    fake = _fake_agent(monkeypatch, _SUGGEST_JSON)
    result = learn.suggest(repo.repo, repo, [_stat()], [], [], source="", minutes=15, mood="okay")
    assert "error" not in result
    profile = result["profile"]
    assert profile["assessment"].startswith("You drive")
    assert [gap["id"] for gap in profile["gaps"]] == ["git-rebase"]
    assert [s["id"] for s in profile["suggestions"]] == ["rebase-basics", "repo-tour"]
    # Persisted under the resolved GitHub id, not a client-supplied name.
    stored = json.loads((tmp_path / ".agitrack" / "learning.json").read_text())
    assert "alice" in stored["profiles"]
    # The digest and check-in context reached the agent.
    assert "TRACE DIGEST" in fake.calls[0]["prompt"]
    assert fake.calls[0]["bare"]
    assert fake.calls[0]["timeout"] == learn._SUGGEST_TIMEOUT_SECONDS


def test_suggest_reports_agent_failure_as_error(tmp_path, monkeypatch, fixed_identity):
    repo = _init_repo(tmp_path)
    _write_state(tmp_path)
    _fake_agent(monkeypatch, "boom", exit_code=1)
    result = learn.suggest(repo.repo, repo, [_stat()], [], [], source="", minutes=15, mood="okay")
    assert "exited with code 1" in result["error"]


def test_suggest_with_no_turns_explains_instead_of_calling_agent(tmp_path, monkeypatch, fixed_identity):
    repo = _init_repo(tmp_path)
    _write_state(tmp_path)
    fake = _fake_agent(monkeypatch, _SUGGEST_JSON)
    result = learn.suggest(repo.repo, repo, [], [], [], source="", minutes=15, mood="okay")
    assert "No agent turns" in result["error"]
    assert fake.calls == []


def test_agent_lock_reports_busy(tmp_path, monkeypatch, fixed_identity):
    repo = _init_repo(tmp_path)
    _write_state(tmp_path)
    _fake_agent(monkeypatch, _SUGGEST_JSON)
    assert learn._AGENT_LOCK.acquire(blocking=False)
    try:
        assert learn.suggest(repo.repo, repo, [_stat()], [], [], source="", minutes=15, mood="okay") == {"busy": True}
    finally:
        learn._AGENT_LOCK.release()


# ------------------------------------------------------- lesson, progress, exercise


def _suggested(repo, tmp_path, monkeypatch):
    _write_state(tmp_path)
    _fake_agent(monkeypatch, _SUGGEST_JSON)
    learn.suggest(repo.repo, repo, [_stat()], [], [], source="", minutes=15, mood="okay")


def test_lesson_generation_normalizes_and_persists(tmp_path, monkeypatch, fixed_identity):
    repo = _init_repo(tmp_path)
    _suggested(repo, tmp_path, monkeypatch)
    _fake_agent(monkeypatch, _LESSON_JSON)
    result = learn.make_lesson(repo.repo, repo, [_stat()], [], [], suggestion_id="rebase-basics")
    lesson = result["lesson"]
    assert lesson["title"] == "Rebase without fear"
    assert lesson["status"] == "started"
    assert len(lesson["links"]) == 1  # the non-http "url" was dropped
    assert lesson["quiz"][0]["answer"] == 0
    assert lesson["exercise"]["status"] == "open"
    assert lesson["gap_id"] == "git-rebase"
    # The guided walk: steps preserved, and the joined content_md view derived for
    # the chat/exercise prompts.
    assert [step["title"] for step in lesson["steps"]] == [
        "Why rebase",
        "Reading a rebase todo",
        "Try this next time",
    ]
    assert "### Why rebase" in lesson["content_md"]
    assert "squash def2 second" in lesson["content_md"]
    stored = json.loads((tmp_path / ".agitrack" / "learning.json").read_text())
    assert stored["profiles"]["alice"]["lessons"][0]["id"] == lesson["id"]


def test_unknown_suggestion_is_an_error(tmp_path, monkeypatch, fixed_identity):
    repo = _init_repo(tmp_path)
    _suggested(repo, tmp_path, monkeypatch)
    result = learn.make_lesson(repo.repo, repo, [_stat()], [], [], suggestion_id="nope")
    assert "no longer stored" in result["error"]


def test_progress_tracks_time_quiz_completion_and_closes_gap(tmp_path, monkeypatch, fixed_identity):
    repo = _init_repo(tmp_path)
    _suggested(repo, tmp_path, monkeypatch)
    _fake_agent(monkeypatch, _LESSON_JSON)
    lesson = learn.make_lesson(repo.repo, repo, [_stat()], [], [], suggestion_id="rebase-basics")["lesson"]
    learn.record_progress(repo.repo, repo, lesson_id=lesson["id"], seconds=65)
    learn.record_progress(repo.repo, repo, lesson_id=lesson["id"], quiz_correct=1, quiz_total=1)
    profile = learn.record_progress(repo.repo, repo, lesson_id=lesson["id"], status="completed", seconds=30)["profile"]
    stored = profile["lessons"][0]
    assert stored["seconds_spent"] == 95
    assert (stored["quiz_correct"], stored["quiz_total"]) == (1, 1)
    assert stored["status"] == "completed"
    # Completing the lesson closes the gap it addressed.
    assert profile["gaps"][0]["status"] == "addressed"
    assert learn.record_progress(repo.repo, repo, lesson_id="missing", seconds=5) == {"error": "Unknown lesson."}


def test_exercise_check_logs_attempt_and_marks_done(tmp_path, monkeypatch, fixed_identity):
    repo = _init_repo(tmp_path)
    _suggested(repo, tmp_path, monkeypatch)
    _fake_agent(monkeypatch, _LESSON_JSON)
    lesson = learn.make_lesson(repo.repo, repo, [_stat()], [], [], suggestion_id="rebase-basics")["lesson"]
    _fake_agent(monkeypatch, json.dumps({"passed": True, "feedback": "Nailed it: one clean commit."}))
    result = learn.exercise_check(repo.repo, repo, lesson_id=lesson["id"], notes="squashed two commits into one")
    assert result["passed"] is True
    stored = json.loads((tmp_path / ".agitrack" / "learning.json").read_text())
    exercise = stored["profiles"]["alice"]["lessons"][0]["exercise"]
    assert exercise["status"] == "done"
    assert exercise["attempts"][0]["feedback"].startswith("Nailed it")
    assert learn.exercise_check(repo.repo, repo, lesson_id=lesson["id"], notes="") == {
        "error": "Type your answer first."
    }


def test_exercise_skip_via_progress(tmp_path, monkeypatch, fixed_identity):
    repo = _init_repo(tmp_path)
    _suggested(repo, tmp_path, monkeypatch)
    _fake_agent(monkeypatch, _LESSON_JSON)
    lesson = learn.make_lesson(repo.repo, repo, [_stat()], [], [], suggestion_id="rebase-basics")["lesson"]
    profile = learn.record_progress(repo.repo, repo, lesson_id=lesson["id"], exercise_status="skipped")["profile"]
    assert profile["lessons"][0]["exercise"]["status"] == "skipped"


def test_lesson_chat_appends_bounded_history(tmp_path, monkeypatch, fixed_identity):
    repo = _init_repo(tmp_path)
    _suggested(repo, tmp_path, monkeypatch)
    _fake_agent(monkeypatch, _LESSON_JSON)
    lesson = learn.make_lesson(repo.repo, repo, [_stat()], [], [], suggestion_id="rebase-basics")["lesson"]
    fake = _fake_agent(monkeypatch, "Great question! Squash melds commits.")
    result = learn.lesson_chat(repo.repo, repo, lesson_id=lesson["id"], message="what does squash do?")
    assert "Squash melds" in result["reply"]
    assert "what does squash do?" in fake.calls[0]["prompt"]
    stored = json.loads((tmp_path / ".agitrack" / "learning.json").read_text())
    chat = stored["profiles"]["alice"]["lessons"][0]["chat"]
    assert [turn["role"] for turn in chat] == ["user", "mentor"]


# ------------------------------------------------------------------ progress sync


def test_sync_progress_writes_ref_and_pushes_to_origin(tmp_path, monkeypatch, fixed_identity):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    repo = _init_repo(work)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=work, check=True)
    _write_state(work)
    _fake_agent(monkeypatch, _SUGGEST_JSON)
    learn.suggest(repo.repo, repo, [_stat()], [], [], source="", minutes=15, mood="okay")

    result = learn.set_sync(repo.repo, repo, True)
    assert result["sync"]["enabled"] is True
    assert result["sync"]["last"]["ok"] is True
    # The local ref holds this user's progress entry, scoped by repo fingerprint.
    users = learn.synced_users(repo)
    assert [user["gid"] for user in users] == ["alice"]
    fingerprint = repo.root_commit()
    raw = repo.read_ref_blob(learn.PROGRESS_REF, f"{fingerprint}/alice/progress.json")
    payload = json.loads(raw)
    assert payload["profile"]["suggestions"]
    # And the push reached origin's copy of the ref.
    on_origin = subprocess.run(["git", "rev-parse", learn.PROGRESS_REF], cwd=origin, capture_output=True, text=True)
    assert on_origin.returncode == 0
    # Disabling stops future syncs but keeps the local log.
    assert learn.set_sync(repo.repo, repo, False)["sync"]["enabled"] is False
    assert learn.sync_enabled(work) is False


def test_progress_restores_on_a_new_machine(tmp_path, monkeypatch, fixed_identity):
    # Machine A syncs progress to origin; a fresh clone (machine B, empty learning.json)
    # gets it back automatically on the first learn_state call, with sync re-enabled.
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work_a = tmp_path / "machine-a"
    work_a.mkdir()
    repo_a = _init_repo(work_a)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=work_a, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=work_a, check=True)
    _write_state(work_a)
    _fake_agent(monkeypatch, _SUGGEST_JSON)
    learn.suggest(repo_a.repo, repo_a, [_stat()], [], [], source="", minutes=15, mood="okay")
    assert learn.set_sync(repo_a.repo, repo_a, True)["sync"]["last"]["ok"] is True

    subprocess.run(["git", "clone", "-q", str(origin), str(tmp_path / "machine-b")], check=True)
    work_b = tmp_path / "machine-b"
    _write_state(work_b)
    repo_b = GitRepo.discover(work_b)
    learn._restore_checked.discard(str(repo_b.repo))
    state = learn.learn_state(repo_b.repo, repo_b)
    assert state["restored"] is True
    assert [s["id"] for s in state["profile"]["suggestions"]] == ["rebase-basics", "repo-tour"]
    assert state["sync"]["enabled"] is True
    # A second load doesn't re-fetch or re-report; the profile is simply there now.
    state = learn.learn_state(repo_b.repo, repo_b)
    assert state["restored"] is False
    assert state["profile"]["suggestions"]


def test_sync_without_remote_still_records_locally(tmp_path, monkeypatch, fixed_identity):
    repo = _init_repo(tmp_path)
    _write_state(tmp_path)
    _fake_agent(monkeypatch, _SUGGEST_JSON)
    learn.suggest(repo.repo, repo, [_stat()], [], [], source="", minutes=15, mood="okay")
    result = learn.set_sync(repo.repo, repo, True)
    assert result["sync"]["last"]["ok"] is True
    assert [user["gid"] for user in learn.synced_users(repo)] == ["alice"]


def test_two_users_coexist_on_the_sync_ref(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _write_state(tmp_path)
    _fake_agent(monkeypatch, _SUGGEST_JSON)
    monkeypatch.setattr(learn, "learner_id", lambda root, repo: "alice")
    learn.suggest(repo.repo, repo, [_stat()], [], [], source="", minutes=15, mood="okay")
    learn.sync_progress_now(repo, "alice")
    monkeypatch.setattr(learn, "learner_id", lambda root, repo: "bob")
    learn.suggest(repo.repo, repo, [_stat()], [], [], source="", minutes=15, mood="okay")
    learn.sync_progress_now(repo, "bob")
    assert {user["gid"] for user in learn.synced_users(repo)} == {"alice", "bob"}


# ------------------------------------------------------------------------ state


def test_learn_state_shape(tmp_path, monkeypatch, fixed_identity):
    repo = _init_repo(tmp_path)
    _write_state(tmp_path)
    state = learn.learn_state(repo.repo, repo)
    assert state["me"] == "alice"
    assert state["profile"]["lessons"] == []
    assert state["backend_info"]["backend"] == "claude"
    assert state["sync"]["enabled"] is False


def test_learner_id_falls_back_to_git_user_name(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    from agitrack.sessions import identity

    monkeypatch.setattr(identity, "_gh_login", lambda: None)
    learn._identity_cache.clear()
    assert learn.learner_id(repo.repo, repo) == "tester"
    learn._identity_cache.clear()


# ----------------------------------------------------------------------- the page


# -------------------------------------------------- backtrace mode (repo may be None)


def test_learn_works_without_a_git_repo(tmp_path, monkeypatch, fixed_identity):
    # The backtrace view can serve a plain directory that is not a git repo: the learn
    # page still works there (repo=None), with progress kept in <dir>/.agitrack/ and
    # git sync reported unavailable instead of failing.
    _write_state(tmp_path)  # a backend choice, no git init
    _fake_agent(monkeypatch, _SUGGEST_JSON)
    result = learn.suggest(tmp_path, None, [_stat()], [], [], source="", minutes=15, mood="okay")
    assert [s["id"] for s in result["profile"]["suggestions"]] == ["rebase-basics", "repo-tour"]
    assert (tmp_path / ".agitrack" / "learning.json").exists()
    state = learn.learn_state(tmp_path, None)
    assert state["sync"] == {"available": False, "enabled": False, "last": None, "users": []}
    assert "git repository" in learn.set_sync(tmp_path, None, True)["error"]


def test_handle_learn_post_dispatches_and_404s(tmp_path, monkeypatch, fixed_identity):
    # The shared POST dispatcher both the live and backtrace servers use.
    repo = _init_repo(tmp_path)
    _suggested(repo, tmp_path, monkeypatch)
    seen = {}

    def view(source, frm, to):
        seen["args"] = (source, frm, to)
        return [_stat()], [], []

    _fake_agent(monkeypatch, _LESSON_JSON)
    result = learn.handle_learn_post(
        "/learn/lesson",
        {"source": "alice", "from": 5, "to": 9, "suggestion_id": "rebase-basics"},
        root=repo.repo,
        repo=repo,
        view=view,
    )
    assert result is not None and result["lesson"]["title"] == "Rebase without fear"
    assert seen["args"] == ("alice", 5, 9)
    assert learn.handle_learn_post("/learn/nope", {}, root=repo.repo, repo=repo, view=view) is None


# ----------------------------------------------------------------------- the page


def test_learn_html_contains_the_page(tmp_path):
    repo = _init_repo(tmp_path)
    html = learn.learn_html(repo.repo)
    assert "find me something worth learning" in html
    assert "coach engine" in html
    assert "—" not in html  # no em-dashes on a shipped page


def test_dashboard_serves_learn_routes(tmp_path, monkeypatch, fixed_identity):
    # End-to-end over HTTP: the live dashboard server exposes the page, the state
    # endpoint, and the POST endpoints (here: a progress write for an unknown lesson,
    # which must come back as an in-page error, not a 500).
    import threading
    import urllib.request

    from agitrack.metrics.server import build_server

    repo = _init_repo(tmp_path)
    _write_state(tmp_path)
    server = build_server(repo, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        page = urllib.request.urlopen(f"{base}/learn", timeout=10).read().decode()
        assert "find me something worth learning" in page
        state = json.loads(urllib.request.urlopen(f"{base}/learn/state", timeout=10).read())
        assert state["me"] == "alice"
        assert "committers" in state
        request = urllib.request.Request(
            f"{base}/learn/progress",
            data=json.dumps({"lesson_id": "missing", "seconds": 9}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        posted = json.loads(urllib.request.urlopen(request, timeout=10).read())
        assert posted == {"error": "Unknown lesson."}
    finally:
        server.shutdown()
        server.server_close()


def test_reset_suggestions_clears_picks_but_keeps_progress(tmp_path, monkeypatch, fixed_identity):
    # "Start over": stale picks (new commits, changed filters) can be cleared without
    # losing anything earned: lessons, gaps and the assessment stay.
    repo = _init_repo(tmp_path)
    _suggested(repo, tmp_path, monkeypatch)
    _fake_agent(monkeypatch, _LESSON_JSON)
    learn.make_lesson(repo.repo, repo, [_stat()], [], [], suggestion_id="rebase-basics")
    profile = learn.reset_suggestions(repo.repo, repo)["profile"]
    assert profile["suggestions"] == []
    assert "suggest_context" not in profile
    assert profile["lessons"] and profile["gaps"] and profile["assessment"]
    # Wired through the shared POST dispatcher too.
    result = learn.handle_learn_post("/learn/reset", {}, root=repo.repo, repo=repo, view=lambda *a: ([], [], []))
    assert result is not None and result["profile"]["suggestions"] == []


def test_norm_lesson_falls_back_to_single_step_for_a_blob():
    # A model (or an old stored lesson) that returned one content_md blob instead of
    # steps still works: it becomes a single step, and the page further splits legacy
    # blobs on ### headings client-side.
    raw = {"title": "T", "minutes": 5, "content_md": "### A\nbody text"}
    lesson = learn._norm_lesson(raw, {"minutes": 5})
    assert lesson["steps"] == [{"title": "", "content_md": "### A\nbody text"}]
    assert lesson["content_md"] == "### A\nbody text"
