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
    BackgroundRunner via make_proxy_agent) runs in-process against the fake. ``autostart=False``
    sets ``autotrack_hook: off`` so the fold happens WITHOUT spawning a real daemon subprocess."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(cfg))
    hook = "auto" if autostart else "off"
    (cfg / "config.json").write_text(f'{{"default_backend": "claude", "autotrack_hook": "{hook}"}}', encoding="utf-8")
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
    # By default (autotrack_hook=auto) the sync also auto-starts the daemon for future commits,
    # in the SAME commit mode as the last run (persisted via write_background_mode).
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    backend = FakeBackend()
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    _precommit_env(tmp_path, monkeypatch, backend, autostart=True)
    bg.write_background_mode(repo, manual=True)  # last run was manual-commit mode
    spawned = []
    monkeypatch.setattr(bg, "spawn_background_daemon", lambda repo, *, extra_args: spawned.append(extra_args))
    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")

    assert bg.precommit_sync(repo) == 0
    assert spawned and "--manual-commits" in spawned[0]  # resumes the last run's mode


def test_precommit_sync_off_does_not_spawn_daemon(tmp_path, monkeypatch):
    # autotrack_hook=off: the sync still folds the AI work into the commit but never auto-starts.
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    backend = FakeBackend()
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    _precommit_env(tmp_path, monkeypatch, backend, autostart=False)  # -> autotrack_hook: off
    spawned = []
    monkeypatch.setattr(bg, "spawn_background_daemon", lambda *a, **k: spawned.append(k))
    (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")

    assert bg.precommit_sync(repo) == 0
    assert spawned == []  # folded, but no daemon started
    assert "# aGiTrack Metadata" in (repo.repo / ".agitrack" / "manual-pending-trailer").read_text()


# --- pre-commit flush: keep the trailer fresh when a daemon is running -------


def test_daemon_flush_request_records_and_acks(tmp_path):
    # The daemon services a pre-commit flush request by recording pending completed turns and
    # re-rendering the fold trailer synchronously, then echoing the nonce back. This is what keeps
    # a commit that races ahead of the daemon's poll from folding a stale/empty trailer.
    runner, repo, state, backend = _runner(tmp_path, manual=True)
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")  # AI edit still in the tree
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])

    (repo.repo / ".agitrack" / "flush-request").write_text("nonce-1", encoding="utf-8")
    runner._service_flush_requests()

    assert "# aGiTrack Metadata" in (repo.repo / ".agitrack" / "manual-pending-trailer").read_text()
    assert (repo.repo / ".agitrack" / "flush-done").read_text().strip() == "nonce-1"
    # A repeated identical nonce is a no-op (dedup) — it must not re-service.
    (repo.repo / ".agitrack" / "flush-done").write_text("stale", encoding="utf-8")
    runner._service_flush_requests()
    assert (repo.repo / ".agitrack" / "flush-done").read_text().strip() == "stale"


def test_background_commit_folds_fresh_turn_via_flush(tmp_path):
    # End-to-end: a turn completed but the daemon hasn't polled yet, so the trailer is empty and a
    # naive commit would fold NOTHING (the reported bug). The pre-commit flush records the turn +
    # renders the trailer, so the very next commit carries the full trace/metadata.
    runner, repo, state, backend = _runner(tmp_path, manual=False)  # auto mode; the fold hook is installed too
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    # Before the flush the trailer holds no turn (daemon hasn't recorded it yet).
    trailer = repo.repo / ".agitrack" / "manual-pending-trailer"
    assert "# aGiTrack Metadata" not in (trailer.read_text() if trailer.exists() else "")

    (repo.repo / ".agitrack" / "flush-request").write_text("n1", encoding="utf-8")
    runner._service_flush_requests()  # the daemon flushes on the pre-commit hook's request

    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "my work")
    msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert "my work" in msg
    assert "# aGiTrack Metadata" in msg  # the turn's trace/metadata folded into the commit


def test_precommit_sync_nudges_a_running_daemon_to_flush(tmp_path, monkeypatch):
    # When a LIVE background daemon holds the lock, precommit_sync no longer just bails: it asks the
    # daemon to flush so this commit folds a fresh trailer (it still records nothing itself — the
    # daemon is the single writer).
    import json
    import os

    from agitrack.git import RepoLock
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    backend = FakeBackend()
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    _precommit_env(tmp_path, monkeypatch, backend)
    held = RepoLock(repo.repo / ".agitrack" / "lock")
    assert held.acquire()  # simulate the daemon holding the repo lock
    bg.background_handshake_path(repo).write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(bg, "request_daemon_flush", lambda r, **k: calls.append(r) or True)
    try:
        (tmp_path / "a.txt").write_text("one\nagent edit\n", encoding="utf-8")
        assert bg.precommit_sync(repo) == 0
        assert calls  # it nudged the running daemon to flush
        # Still never records the turn itself (single-writer): no latent ref from the sync.
        assert repo.ref_sha("refs/agitrack/manual/" + state_session_id(tmp_path)) is None
    finally:
        held.release()


def state_session_id(tmp_path) -> str:
    return AgitrackState(tmp_path, default_backend="claude").session_id


# --- agent commits its own work mid-turn (final message comes after the commit) ---


def test_daemon_covers_agent_commit_made_during_an_unfinished_turn(tmp_path):
    # The hard case: the agent `git commit`s its own work (mid-turn, so its final message comes
    # after the commit), and by the time the turn finishes the tree is clean — the tree-gated
    # recorder would drop it. The daemon reconciles from its persistent watermark: HEAD has advanced
    # past it with an untracked commit, so it covers that commit with the turn's trace/metadata.
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    runner._load_tracked_head()
    init_head = repo.rev_parse("HEAD")
    assert runner._tracked_head == init_head  # watermark starts at HEAD

    # The agent commits its OWN work — HEAD moves, the working tree ends clean.
    (tmp_path / "a.txt").write_text("one\nagent code\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "agent's own commit")
    agent_head = repo.rev_parse("HEAD")

    # The turn completes ⇒ the daemon covers the untracked commit since the watermark.
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()

    head = repo.rev_parse("HEAD")
    assert head != agent_head  # a cover was added on top
    # ONE cover commit, merge-shaped: first parent = the watermark, second = the agent's commit.
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert parents[1:] == [init_head, agent_head]
    cover_msg = _git(repo, "log", "-1", "--format=%B", "HEAD")
    assert cover_msg.count("# aGiTrack Metadata") == 1  # metadata EXACTLY once — never duplicated
    assert "do x" in cover_msg  # the turn's trace
    assert "covered_commits:" in cover_msg and agent_head[:7] in cover_msg  # attributes its lines to AI
    assert repo.rev_parse("HEAD^{tree}") == repo.rev_parse(agent_head + "^{tree}")  # no diff
    assert _git(repo, "cat-file", "-t", agent_head).strip() == "commit"  # agent's commit keeps its hash
    assert runner._tracked_head == head  # watermark advanced to the cover


def test_watermark_persists_and_covers_a_commit_made_while_the_daemon_was_down(tmp_path):
    # The watermark is what makes coverage survive a restart / the daemon being down at commit time:
    # a commit made with no daemon running is still covered when the next turn completes, because the
    # PERSISTED watermark predates it (the old in-flight snapshot was lost on every restart).
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    runner._load_tracked_head()  # persists watermark = init HEAD to .agitrack/tracked-head
    init_head = repo.rev_parse("HEAD")

    # "Daemon down": a commit is made with nothing observing the turn in flight.
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "agent commit while daemon down")
    agent_head = repo.rev_parse("HEAD")

    # A fresh runner (a restart) reloads the PERSISTED watermark — init_head, not the current HEAD.
    restarted = BackgroundRunner(
        repo, manual_commits=False, _global_config=GlobalConfig(path=tmp_path / "gc2.json"), _state=state
    )
    restarted.backend = backend
    restarted._make_summarizer = lambda: None
    restarted._summarization_enabled = lambda: False
    restarted._manual.setup()
    restarted._load_tracked_head()
    assert restarted._tracked_head == init_head  # survived the restart (not agent_head)

    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    restarted._process_once()

    assert repo.rev_parse("HEAD") != agent_head  # the commit made while down is now covered
    assert "covered_commits:" in _git(repo, "log", "-1", "--format=%B", "HEAD")


def test_daemon_does_not_cover_a_pure_qa_turn(tmp_path):
    # A completed turn that made NO commit (pure Q&A, no file change) must not create a cover:
    # HEAD == watermark, so there is nothing new to attribute.
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._manual.setup()
    runner._load_tracked_head()
    head = repo.rev_parse("HEAD")

    backend.set_session("s1", [_turn("u1", "m1", "explain this", "here is why", 15)])  # pure Q&A
    runner._process_once()

    assert repo.rev_parse("HEAD") == head  # nothing committed ⇒ no cover


def test_daemon_never_retroactively_covers_preexisting_history(tmp_path):
    # On a repo it has never tracked, the watermark initializes to the CURRENT HEAD, so human commits
    # made BEFORE the daemon are never attributed to AI — only commits after the watermark are.
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    (tmp_path / "h.txt").write_text("human\n", encoding="utf-8")  # a human commit made before tracking
    _git(repo, "add", "h.txt")
    _git(repo, "commit", "-m", "human work")
    human_head = repo.rev_parse("HEAD")
    runner._manual.setup()
    runner._load_tracked_head()
    assert runner._tracked_head == human_head  # watermark = current HEAD, not the repo root

    backend.set_session("s1", [_turn("u1", "m1", "hi", "hello", 15)])  # a turn that makes no commit
    runner._process_once()

    assert repo.rev_parse("HEAD") == human_head  # the pre-existing human commit is NOT covered


def test_repo_status_reports_each_mode(tmp_path, capsys):
    import json
    import os

    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    agit = repo.repo / ".agitrack"
    agit.mkdir(exist_ok=True)

    # Not running — and it reports the auto-start (autotrack_hook) state.
    assert bg.repo_status(repo) == 0
    out = capsys.readouterr().out.lower()
    assert "not running" in out and "auto-start on commit:" in out

    # Background daemon (live pid via our own pid) with a manual-commit handshake.
    bg.background_handshake_path(repo).write_text(
        json.dumps({"pid": os.getpid(), "mode": "manual commits", "backend": "opencode"}), encoding="utf-8"
    )
    assert bg.repo_status(repo) == 0
    out = capsys.readouterr().out
    assert "BACKGROUND" in out and "manual-commit" in out and "no worktree" in out and "opencode" in out
    bg.background_handshake_path(repo).unlink()

    # Interactive proxy (live pid) with worktree + auto commit.
    bg.proxy_status_path(repo).write_text(
        json.dumps({"pid": os.getpid(), "mode": "interactive", "commits": "auto", "worktree": True}), encoding="utf-8"
    )
    assert bg.repo_status(repo) == 0
    out = capsys.readouterr().out
    assert "INTERACTIVE" in out and "auto-commit" in out and "worktree" in out and "no worktree" not in out


def test_proxy_status_write_and_clear(tmp_path):
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    (repo.repo / ".agitrack").mkdir(exist_ok=True)
    bg.write_proxy_status(repo, commits="manual", worktree=False)
    info = bg._read_proxy_status(repo)
    assert info["commits"] == "manual" and info["worktree"] is False
    bg.clear_proxy_status(repo)
    assert bg._read_proxy_status(repo) is None


def test_global_summarization_disabled_is_not_shadowed_by_state_default(tmp_path):
    # A global `summarization_enabled: false` must win — the per-repo AgitrackState defaults it to
    # True, and that default must NOT shadow the global toggle (else you can't turn it off in bg).
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    del runner._summarization_enabled  # drop the test stub to exercise the real method
    runner.global_config.summarization_enabled = False
    assert state.summarization_enabled is True  # state still defaults on
    assert runner._summarization_enabled() is False  # ...but the global 'off' wins


def test_fold_summary_ready_bails_when_worker_finished_without_note(tmp_path):
    # When the summary worker has finished but produced NO note (the summarizer errored), the fold
    # must proceed immediately rather than waiting out the full summary_wait_seconds.
    import threading

    runner, repo, state, backend = _runner(tmp_path, manual=False)
    runner._summarization_enabled = lambda: True
    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nagent\n", encoding="utf-8")
    backend.set_session("s1", [_turn("u1", "m1", "do x", "done", 20)])
    runner._process_once()
    tip = repo.ref_sha(runner._manual.ref())

    # No note, and a still-running worker ⇒ keep waiting.
    alive = threading.Event()
    t = threading.Thread(target=alive.wait)
    t.start()
    runner._summary_threads[tip] = t
    assert runner._fold_summary_ready(tip) is False
    alive.set()
    t.join()

    # Worker finished with no note ⇒ fold now (don't wait out the deadline).
    assert runner._fold_summary_ready(tip) is True


def test_background_mode_persist_roundtrip(tmp_path):
    from agitrack.proxy import background as bg

    repo = _init_repo(tmp_path)
    assert bg.read_background_mode(repo) is None  # nothing recorded yet
    bg.write_background_mode(repo, manual=True)
    assert bg.read_background_mode(repo) is True
    bg.write_background_mode(repo, manual=False)
    assert bg.read_background_mode(repo) is False


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
