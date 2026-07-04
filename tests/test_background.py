"""Background (headless) mode: aGiTrack tracks a user-driven native backend session without the
interactive TUI. Always no-worktree; manual (latent + fold on the user's commit) or auto (aGiTrack
folds the pending turns into a commit itself). Reuses the same CommitEngine + ManualCommitTracker
as the proxy, so token/turn accounting is identical."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agitrack.backends.base import TokenUsage
from agitrack.commits import ManualCommitTracker
from agitrack.config import AgitrackState
from agitrack.config.settings import GlobalConfig
from agitrack.git import GitRepo
from agitrack.proxy.background import (
    BackgroundRunner,
    background_handshake_path,
    background_status,
    stop_background,
)
from agitrack.transcripts.types import ExportedSession, SessionTurn


def _init_repo(path: Path) -> GitRepo:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return GitRepo(path)


def _git(repo: GitRepo, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo.repo), *args], capture_output=True, text=True, check=True).stdout


class FakeBackend:
    name = "claude"

    def __init__(self) -> None:
        self.sessions: dict[str, ExportedSession] = {}
        self.latest: str | None = None

    def latest_session_id(self, repo):
        return self.latest

    def export_session(self, repo, session_id):
        return self.sessions.get(session_id)

    def set_session(self, session_id: str, turns: list[SessionTurn], *, model: str = "claude-opus-4-8") -> None:
        self.sessions[session_id] = ExportedSession(session_id, model, None, turns)
        self.latest = session_id


def _turn(uid: str, aid: str, prompt: str, response: str, out: int) -> SessionTurn:
    return SessionTurn(uid, aid, prompt, response, TokenUsage(total=out, output=out), "claude-opus-4-8")


def _runner(tmp_path, *, manual: bool):
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    gc = GlobalConfig(path=tmp_path / "gc.json")
    runner = BackgroundRunner(repo, manual_commits=manual, _global_config=gc, _state=state)
    runner.backend = FakeBackend()
    runner._make_summarizer = lambda: None  # never spawn a real summarizer LLM in tests
    runner._summarization_enabled = lambda: False  # ...so auto-fold doesn't wait for a summary
    return runner, repo, state, runner.backend


# --- manual mode ------------------------------------------------------------


def test_background_manual_records_latent_and_freezes_head(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    runner._manual.setup()
    head = repo.rev_parse("HEAD")
    # The user drives the agent: it edits a file and the transcript records a completed turn.
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])

    assert runner._process_once() is True
    assert repo.rev_parse("HEAD") == head  # HEAD never moves in manual mode
    assert repo.ref_sha(runner._manual.ref()) is not None  # recorded on the latent ref
    # The fold trailer the prepare-commit-msg hook reads was rendered.
    assert "# aGiTrack Metadata" in (repo.repo / ".agitrack" / "manual-pending-trailer").read_text()


def test_background_manual_folds_into_user_commit_via_hook(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()

    # The user commits their work (the fold + reset hooks are installed).
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "my work")

    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "my work" in msg
    assert msg.count("# aGiTrack Metadata") == 2  # user block + the one folded turn
    assert len(_git(repo, "log", "--format=%H").split()) == 2  # init + one folded commit


# --- auto mode --------------------------------------------------------------


def test_background_auto_folds_pending_into_a_commit_itself(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()  # records the latent turn

    runner._auto_fold_pending()  # aGiTrack commits it itself (no user action)

    log = _git(repo, "log", "--format=%H").split()
    assert len(log) == 2  # init + the auto commit
    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    # A CLEAN agent commit: the prompt is the subject (not a generic "commit agent turns"), with a
    # single agent metadata block — NOT the manual squash-into-a-user-commit format.
    assert msg.startswith("<aGiTrack> do x")
    assert "commit agent turns" not in msg
    assert "commit_type: user" not in msg
    assert msg.count("# aGiTrack Metadata") == 1 and "commit_type: agent" in msg
    # Ref reset to HEAD, so nothing is pending after the auto commit.
    assert repo.ref_sha(runner._manual.ref()) == repo.rev_parse("HEAD")
    assert runner._manual.pending_count() == 0


def test_background_auto_fold_waits_for_summary_then_uses_it_as_subject(tmp_path):
    # When summarization is on, the auto-fold must WAIT for the LLM summary and use it as the
    # commit subject/lead (the daemon never amends HEAD, so the summary must be in before commit).
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._summarization_enabled = lambda: True  # summaries on for this test
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()

    # No summary note yet ⇒ fold is deferred (no commit made).
    runner._auto_fold_pending()
    assert len(_git(repo, "log", "--format=%H").split()) == 1  # still just init

    # The summary lands as a git note on the latent tip; now the fold proceeds and uses it.
    tip = repo.ref_sha(runner._manual.ref())
    repo.notes_add(tip, "Add the greeting helper", namespace="agitrack/commit-summary")
    runner._auto_fold_pending()

    assert len(_git(repo, "log", "--format=%H").split()) == 2
    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert msg.startswith("<aGiTrack> Add the greeting helper")  # summary leads the subject
    assert "commit_type: agent" in msg


def test_background_auto_skips_when_agent_committed_its_own_work(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()
    before = repo.rev_parse("HEAD")
    # The agent commits its own work (the fold hook folded tracking into it, resetting the ref).
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "agent commit")
    runner._manual.service()  # react to the commit: drop the now-stale latent chain

    runner._auto_fold_pending()  # clean tree ⇒ nothing more to do

    # Only the agent's own commit was added — aGiTrack did NOT add a second commit on top.
    assert _git(repo, "log", "--format=%H").split()[0] != before
    assert len(_git(repo, "log", "--format=%H").split()) == 2


# --- discovery / accounting -------------------------------------------------


def test_background_follows_latest_session_and_counts_once(tmp_path):
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    runner._manual.setup()
    # First conversation, one turn.
    (tmp_path / "a.txt").write_text("one\nfirst\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "first", "done", 10)])
    assert runner._process_once() is True
    # Re-processing the SAME transcript records nothing new (watermark already past it).
    assert runner._process_once() is False

    # The user switches to a new conversation inside the backend; aGiTrack follows the latest.
    (tmp_path / "a.txt").write_text("one\nfirst\nsecond\n", encoding="utf-8")
    backend.set_session("s2", [_turn("u2", "m2", "second", "done", 15)])
    assert runner._process_once() is True
    assert state.backend_session_id == "s2"  # followed the switch
    # Two latent turns pending (one per conversation), each counted once.
    assert runner._manual.pending_count() == 2


# --- regression: a completed turn with changes is always committed ----------


def test_auto_pipeline_commits_completed_turn_on_dirty_tree(tmp_path):
    # Regression guard for "files left uncommitted": a completed agent turn whose edits sit in
    # the working tree MUST be committed by the auto (no-worktree, direct-commit) pipeline — HEAD
    # advances and the tree goes clean, never silently left for the user to commit by hand.
    import types

    from agitrack.proxy.commit_engine import CommitEngine
    from agitrack.proxy.session import Session

    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")  # agent's uncommitted work

    session = Session.bare()
    session.state = state
    session.backend = types.SimpleNamespace(name="claude")
    exported = ExportedSession("s1", "m", None, [_turn("u1", "m1", "do it", "done", 7)])
    session.agent_parse_result = ("s1", exported, None, state)
    session.agent_parse_thread = None

    engine = CommitEngine(repo, state)
    head_before = repo.rev_parse("HEAD")

    def commit_fn(*, turns, backend, backend_session_id, model, quiet, prompt_untracked=True):
        def stage_untracked_fn(r, s):
            r.stage_paths(r.untracked_entries())

        return engine.commit_turns(
            turns=turns,
            backend=backend,
            backend_session_id=backend_session_id,
            model=model,
            stage_untracked_fn=stage_untracked_fn,
        )

    committed, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=True,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda _s: None,
        mirror_fn=lambda _s: None,
        commit_fn=commit_fn,
    )

    assert committed is True
    assert repo.rev_parse("HEAD") != head_before  # a real commit was made, not left uncommitted
    assert "# aGiTrack Metadata" in repo.commit_message("HEAD")
    assert _git(repo, "status", "--short").strip() == ""  # working tree is now clean


# --- ManualCommitTracker direct --------------------------------------------


def test_manual_tracker_gate_and_record(tmp_path):
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    tracker = ManualCommitTracker(repo, repo, state)
    head = repo.rev_parse("HEAD")

    assert tracker.gate() is False  # clean tree ⇒ nothing to record
    (tmp_path / "a.txt").write_text("one\nx\n", encoding="utf-8")
    assert tracker.gate() is True
    sha = tracker.record("<aGiTrack> t\n\n# aGiTrack Metadata\ncommit_type: agent\n")

    assert sha is not None
    assert repo.rev_parse("HEAD") == head  # HEAD frozen
    assert repo.ref_sha(tracker.ref()) is not None
    assert tracker.pending_count() == 1


# --- stop / status handshake ------------------------------------------------


def test_background_status_reports_none_when_not_running(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    assert background_status(repo) == 0
    assert "No aGiTrack background tracker" in capsys.readouterr().out


def test_background_status_reports_running_for_live_pid(tmp_path, capsys):
    import json
    import os

    repo = _init_repo(tmp_path)
    path = background_handshake_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "mode": "auto commits"}), encoding="utf-8")

    assert background_status(repo) == 0
    out = capsys.readouterr().out
    assert "is running" in out and "auto commits" in out


def test_background_stop_cleans_stale_handshake(tmp_path, capsys):
    import json

    repo = _init_repo(tmp_path)
    path = background_handshake_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A dead pid (very high, not alive) ⇒ treated as not running and the stale file is removed.
    path.write_text(json.dumps({"pid": 2_000_000_000, "mode": "auto commits"}), encoding="utf-8")

    assert stop_background(repo) == 0
    assert "No aGiTrack background tracker" in capsys.readouterr().out
    assert not path.exists()  # stale handshake cleaned up


def test_background_run_writes_and_removes_handshake(tmp_path, monkeypatch):
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    monkeypatch.setattr("agitrack.backends.setup.backend_installed", lambda name: True)
    # Stop immediately after the first loop iteration so run() returns.
    runner._stop.set()
    monkeypatch.setattr(runner, "_install_signal_handlers", lambda: None)

    assert runner.run() == 0
    # After a clean run the handshake is removed again.
    assert not background_handshake_path(repo).exists()


def test_start_background_daemon_reuses_running(tmp_path, capsys, monkeypatch):
    import json
    import os

    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    path = background_handshake_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "mode": "auto commits"}), encoding="utf-8")
    spawned = []
    monkeypatch.setattr(bg, "spawn_background_daemon", lambda *a, **k: spawned.append(k) or None)

    assert bg.start_background_daemon(repo, extra_args=[]) == 0
    assert "already running" in capsys.readouterr().out
    assert spawned == []  # never spawns a duplicate


def test_start_background_daemon_spawns_and_reports(tmp_path, capsys, monkeypatch):
    import json

    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)

    class _FakeProc:
        pid = 4242

    def fake_spawn(r, *, extra_args):
        # Simulate the detached child publishing its handshake right after launch.
        p = background_handshake_path(r)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"pid": 4242, "mode": "auto commits"}), encoding="utf-8")
        return _FakeProc()

    monkeypatch.setattr(bg, "spawn_background_daemon", fake_spawn)
    monkeypatch.setattr(bg, "pid_alive", lambda pid: True)

    assert bg.start_background_daemon(repo, extra_args=["--auto-commit"]) == 0
    out = capsys.readouterr().out
    assert "daemon live" in out and "4242" in out and "agitrack -b stop" in out


def test_start_background_daemon_reports_failure_when_child_dies(tmp_path, capsys, monkeypatch):
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)

    class _FakeProc:
        pid = 4243

    # Child never writes a handshake and is not alive ⇒ wait_for_handshake gives up fast.
    monkeypatch.setattr(bg, "spawn_background_daemon", lambda r, *, extra_args: _FakeProc())
    monkeypatch.setattr(bg, "pid_alive", lambda pid: False)

    assert bg.start_background_daemon(repo, extra_args=[]) == 1
    assert "did not start" in capsys.readouterr().out


def test_daemon_installs_autotrack_hook_by_default_and_skips_when_off(tmp_path):
    from agitrack.git import hooks as git_hooks

    runner, repo, state, backend = _runner(tmp_path, manual=False)
    hook = repo.repo / ".git" / "hooks" / "pre-commit"

    # Default (keep): the persistent hook is installed.
    runner._install_autotrack_hook()
    assert hook.exists() and git_hooks.is_autotrack_hook(hook)

    # autotrack_hook=off: installing removes it (the user opted out via the -b prompt).
    runner.global_config.autotrack_hook = "off"
    runner._install_autotrack_hook()
    assert not hook.exists()


def test_background_writes_event_log(tmp_path):
    # The event log records notable events in background mode too: an AI change detected and
    # the commit aGiTrack makes for it. Works with --log-file / config in every mode.
    from agitrack.events import EventLog

    runner, repo, state, backend = _runner(tmp_path, manual=False)
    log = tmp_path / "events.log"
    runner.events = EventLog(log)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])

    assert runner._process_once() is True  # records the latent turn → ai-change-detected
    runner._auto_fold_pending()  # folds into a real commit → commit event

    text = log.read_text(encoding="utf-8")
    assert "ai-change-detected" in text and "backend=claude" in text
    assert "commit " in text and "type=agent" in text


def _precommit_env(tmp_path, monkeypatch, backend: "FakeBackend", *, autostart: bool = False):
    """Set up a repo + injected fake backend so precommit_sync (which builds its own
    BackgroundRunner via make_proxy_agent) runs in-process against the fake."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(cfg))
    (cfg / "config.json").write_text(
        f'{{"default_backend": "claude", "background_autostart": {str(autostart).lower()}}}', encoding="utf-8"
    )
    monkeypatch.setattr("agitrack.proxy.background.make_proxy_agent", lambda name: backend)
    monkeypatch.setattr("agitrack.backends.setup.backend_installed", lambda name: True)


def test_precommit_sync_folds_ai_work_into_the_commit(tmp_path, monkeypatch):
    # The persistent auto-track pre-commit hook path: with AI work pending and no tracker running,
    # precommit_sync records the turn + renders the trailer + installs the fold hooks, so the
    # user's in-progress commit carries the full trace/metadata (the user's requirement).
    from agitrack.proxy.background import precommit_sync

    repo = _init_repo(tmp_path)
    backend = FakeBackend()
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    _precommit_env(tmp_path, monkeypatch, backend)
    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")  # AI-made change in the tree

    assert precommit_sync(repo) == 0
    # The trailer was rendered for the prepare-commit-msg hook, and the fold hooks are installed.
    assert "# aGiTrack Metadata" in (repo.repo / ".agitrack" / "manual-pending-trailer").read_text()

    # The user now commits; the installed fold hook folds the trace into that ONE commit.
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "My change")
    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "My change" in msg and "# aGiTrack Metadata" in msg  # single, fully-tracked commit


def test_precommit_sync_git_ignores_agitrack_dir(tmp_path, monkeypatch):
    # aGiTrack's own state files (the trailer, ref, …) must never leak into a user commit: the
    # sync must git-ignore .agitrack/ before writing any of them.
    from agitrack.proxy.background import precommit_sync

    repo = _init_repo(tmp_path)
    backend = FakeBackend()
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    _precommit_env(tmp_path, monkeypatch, backend)
    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")

    assert precommit_sync(repo) == 0
    exclude = (repo.repo / ".git" / "info" / "exclude").read_text()
    assert ".agitrack/" in exclude.splitlines()
    # A blanket `git add -A` must not stage any .agitrack file.
    _git(repo, "add", "-A")
    staged = _git(repo, "diff", "--cached", "--name-only")
    assert not any(line.startswith(".agitrack/") for line in staged.splitlines())


def test_precommit_sync_no_ai_work_is_a_noop(tmp_path, monkeypatch):
    # No pending AI turns ⇒ precommit_sync leaves no footprint: no trailer content, no reminder.
    from agitrack.proxy.background import precommit_sync

    repo = _init_repo(tmp_path)
    backend = FakeBackend()  # no session set ⇒ nothing to track
    _precommit_env(tmp_path, monkeypatch, backend)
    (tmp_path / "a.txt").write_text("one\njust me\n", encoding="utf-8")  # human-only edit

    assert precommit_sync(repo) == 0
    trailer_file = repo.repo / ".agitrack" / "manual-pending-trailer"
    # Either no trailer file, or an empty one — a human commit is left untouched.
    assert not trailer_file.exists() or trailer_file.read_text().strip() == ""


def test_precommit_sync_defers_to_a_running_tracker(tmp_path, monkeypatch):
    # When a live tracker holds the repo lock, precommit_sync does nothing (that tracker's own
    # fold hooks handle this commit) — it must never double-track.
    from agitrack.git import RepoLock
    from agitrack.proxy.background import precommit_sync

    repo = _init_repo(tmp_path)
    backend = FakeBackend()
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    _precommit_env(tmp_path, monkeypatch, backend)
    held = RepoLock(repo.repo / ".agitrack" / "lock")
    assert held.acquire()  # simulate a running tracker
    try:
        (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")
        assert precommit_sync(repo) == 0
        # Deferred: no latent ref recorded by the sync (the running tracker owns that).
        assert (
            repo.ref_sha("refs/agitrack/manual/" + AgitrackState(tmp_path, default_backend="claude").session_id) is None
        )
    finally:
        held.release()


def test_precommit_sync_autostart_spawns_daemon(tmp_path, monkeypatch):
    # With background_autostart on, the sync also starts the background daemon for future commits.
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    backend = FakeBackend()
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    _precommit_env(tmp_path, monkeypatch, backend, autostart=True)
    spawned = []
    monkeypatch.setattr(bg, "spawn_background_daemon", lambda repo, *, extra_args: spawned.append(extra_args))
    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")

    assert bg.precommit_sync(repo) == 0
    assert spawned and "--auto-commit" in spawned[0]  # daemon started in the configured (auto) mode


def test_daemon_update_check_writes_marker_and_clears(tmp_path, monkeypatch):
    # The daemon's periodic check RECORDS an available update (it never auto-installs), and clears
    # a stale marker once up to date. It also emits an event-log line.
    from agitrack.events import EventLog
    from agitrack.update.marker import read_update_marker

    runner, repo, state, backend = _runner(tmp_path, manual=False)
    log = tmp_path / "events.log"
    runner.events = EventLog(log)

    class _Status:
        ok = True
        available = True
        current = "0.1.16"
        latest = "0.2.0"
        message = "aGiTrack update available: 0.1.16 → 0.2.0."

    monkeypatch.setattr(
        "agitrack.update.updater.Updater", lambda *a, **k: type("U", (), {"check": lambda self: _Status()})()
    )
    runner._run_update_check()
    info = read_update_marker(repo.repo)
    assert info and info["latest"] == "0.2.0"
    assert "update-available" in log.read_text() and "0.2.0" in log.read_text()

    # Now up to date ⇒ the stale marker is cleared.
    class _None(_Status):
        available = False

    monkeypatch.setattr(
        "agitrack.update.updater.Updater", lambda *a, **k: type("U", (), {"check": lambda self: _None()})()
    )
    runner._run_update_check()
    assert read_update_marker(repo.repo) is None


def test_background_status_shows_available_update(tmp_path, capsys):
    from agitrack.update.marker import write_update_marker

    repo = _init_repo(tmp_path)
    write_update_marker(repo.repo, current="0.1.16", latest="0.2.0", message="u")
    assert background_status(repo) == 0
    out = capsys.readouterr().out
    assert "update available" in out.lower() and "0.2.0" in out


def test_precommit_sync_reminds_about_update_on_every_commit(tmp_path, monkeypatch, capsys):
    from agitrack.proxy.background import precommit_sync
    from agitrack.update.marker import write_update_marker

    repo = _init_repo(tmp_path)
    backend = FakeBackend()  # no AI work — the reminder must still show
    _precommit_env(tmp_path, monkeypatch, backend)
    write_update_marker(repo.repo, current="0.1.16", latest="0.2.0", message="u")

    assert precommit_sync(repo) == 0
    err = capsys.readouterr().err
    assert "update available" in err.lower() and "0.2.0" in err


def test_manual_tracker_reconcile_covers_external_commit(tmp_path):
    repo = _init_repo(tmp_path)
    state = AgitrackState(tmp_path, default_backend="claude")
    tracker = ManualCommitTracker(repo, repo, state)
    tracker.hooks_installed = False  # force the cover fallback
    tracker.last_head = repo.rev_parse("HEAD")
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    tracker.gate()
    tracker.record("<aGiTrack> t\n\n# aGiTrack Metadata\ncommit_type: agent\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "external")
    user_head = repo.rev_parse("HEAD")
    user_tree = repo.rev_parse("HEAD^{tree}")

    tracker.reconcile_external_commit()

    cover = repo.rev_parse("HEAD")
    assert cover != user_head  # a cover commit was added
    assert repo.parents(cover)[0] == user_head  # first-parent = the user's own commit
    assert repo.rev_parse("HEAD^{tree}") == user_tree  # cover introduced no diff
    assert "# aGiTrack Metadata" in repo.commit_message(cover)
    assert repo.ref_sha(tracker.ref()) == cover  # ref reset
