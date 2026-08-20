"""`agitrack stop` opens an UNTRACKED stretch, and nothing said in it reaches a commit message.

The leak these tests pin down: aGiTrack's per-conversation watermark is persistent on purpose,
so stopping the tracker and carrying the conversation on in the backend's own UI left every one
of those turns looking committable to the next `agitrack -b` — which folded them, verbatim, into
the next commit message. A conversation STARTED during the stretch was worse: no watermark at
all, so it exported whole. Git history is not something a later command can take that back out
of, so the stop is recorded and the next start stamps a floor.
"""

from __future__ import annotations

import math
import subprocess
import types
from pathlib import Path

from agitrack import tracking_gap
from agitrack.backends.base import TokenUsage
from agitrack.config import AgitrackState
from agitrack.git import GitRepo
from agitrack.proxy.commit_engine import CommitEngine
from agitrack.proxy.session import Session
from agitrack.transcripts import ExportedSession
from agitrack.transcripts.types import SessionTurn


def _init_repo(path: Path) -> GitRepo:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return GitRepo(path)


def _turn(uid: str, aid: str, prompt: str, response: str, *, started: int, ended: int | None = None):
    return SessionTurn(
        uid,
        aid,
        prompt,
        response,
        TokenUsage(total=10, output=5, input=5),
        None,
        started_at=started,
        ended_at=ended if ended is not None else started + 1,
    )


class _Repo:
    """Minimal GitRepo stand-in that still knows where it lives (the floor is per repo)."""

    def __init__(self, root: Path, *, staged: bool = True):
        self.repo = root
        self._staged = staged
        self.message: str | None = None

    def add_tracked(self) -> None:
        pass

    def has_staged_changes(self) -> bool:
        return self._staged

    def commit(self, message: str) -> str:
        self.message = message
        return "dead1234"

    def untracked_files(self) -> list[str]:
        return []

    def stage_paths(self, paths) -> None:
        pass


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------


def test_floor_is_unset_until_a_stop_has_actually_happened(tmp_path):
    # Costs one small read on every commit path, so the common case — aGiTrack was never
    # stopped — must answer "no floor" and change nothing at all.
    assert tracking_gap.tracking_floor(tmp_path) is None
    assert tracking_gap.split_untracked_turns([_turn("u", "a", "p", "r", started=5)], None)[0] == []


def test_a_second_stop_inside_one_gap_keeps_when_the_gap_began(tmp_path):
    # `agitrack stop` marks, then calls through to the tracker's own stop path which marks
    # again. The gap began when the user first said so, not on the second write.
    assert tracking_gap.mark_stopped(tmp_path, now=1000.0) == 1000.0
    assert tracking_gap.mark_stopped(tmp_path, now=1002.0) == 1000.0


def test_resume_stamps_the_floor_once_per_stop(tmp_path):
    tracking_gap.mark_stopped(tmp_path, now=1000.0)
    assert tracking_gap.resume_tracking(tmp_path, now=2000.0) == 2000.0
    # A restart of the same tracking run (a crash, a self-update swap) is not a new resume:
    # the floor stays where the user's own restart put it.
    assert tracking_gap.resume_tracking(tmp_path, now=2500.0) is None
    assert tracking_gap.tracking_floor(tmp_path) == 2000.0
    # Stopping again re-opens the stretch; until the next start NOTHING is tracked.
    tracking_gap.mark_stopped(tmp_path, now=3000.0)
    assert tracking_gap.gap_is_open(tmp_path) is True
    assert tracking_gap.tracking_floor(tmp_path) == math.inf
    assert tracking_gap.resume_tracking(tmp_path, now=4000.0) == 4000.0
    assert tracking_gap.gap_is_open(tmp_path) is False


def test_while_the_stop_stands_no_turn_is_tracked_at_all(tmp_path):
    # A stop can FAIL to reach a wedged daemon (`agitrack stop` says so and exits 1). The user
    # has still said stop, so a tracker that outlives the command records nothing.
    tracking_gap.mark_stopped(tmp_path, now=1000.0)
    turns = [_turn("u1", "a1", "after the stop", "ok", started=2000)]
    assert tracking_gap.split_untracked_turns(turns, tracking_gap.tracking_floor(tmp_path)) == (turns, [])


def test_resume_on_a_repo_that_was_never_stopped_does_nothing(tmp_path):
    assert tracking_gap.resume_tracking(tmp_path, now=99.0) is None
    assert tracking_gap.tracking_floor(tmp_path) is None


def test_the_record_lives_in_the_base_repo_not_the_session_worktree(tmp_path):
    # A worktree session and the base repo must read ONE floor: `agitrack stop` is typed in the
    # base repo, and it is the base repo the next tracker starts in.
    worktree = tmp_path / ".agitrack" / "worktrees" / "brave-otter"
    worktree.mkdir(parents=True)
    tracking_gap.mark_stopped(worktree, now=1000.0)
    tracking_gap.resume_tracking(tmp_path, now=2000.0)
    assert tracking_gap.tracking_floor(worktree) == 2000.0
    assert tracking_gap.marker_path(worktree) == tmp_path / ".agitrack" / "tracking-gap.json"


def test_the_record_is_written_where_git_already_ignores_it(tmp_path):
    tracking_gap.mark_stopped(tmp_path, now=1000.0)
    assert (tmp_path / ".agitrack" / "tracking-gap.json").exists()
    # atomic_write_text self-ignores `.agitrack/`, so a stop never leaves the user staring at
    # an untracked file in a repo it just said it had stopped touching.
    assert (tmp_path / ".agitrack" / ".gitignore").exists()


# ---------------------------------------------------------------------------
# Splitting turns against the floor
# ---------------------------------------------------------------------------


def test_turns_prompted_before_the_floor_are_split_off_as_untracked(tmp_path):
    turns = [
        _turn("u1", "a1", "while stopped", "sure", started=100),
        _turn("u2", "a2", "still stopped", "ok", started=200),
        _turn("u3", "a3", "after restarting", "done", started=400),
    ]
    untracked, tracked = tracking_gap.split_untracked_turns(turns, 300.0)
    assert [t.user_prompt for t in untracked] == ["while stopped", "still stopped"]
    assert [t.user_prompt for t in tracked] == ["after restarting"]


def test_a_turn_straddling_the_restart_is_excluded_whole(tmp_path):
    # Prompted while recording was off. Committing its second half would put the user's
    # untracked prompt into the message just the same, so it goes entirely.
    straddling = _turn("u1", "a1", "asked while stopped", "answered after", started=100, ended=500)
    untracked, tracked = tracking_gap.split_untracked_turns([straddling], 300.0)
    assert untracked == [straddling] and tracked == []


def test_a_turn_prompted_in_the_very_second_of_the_restart_is_tracked(tmp_path):
    # Transcript stamps have second resolution; sub-second rounding must not silently eat a
    # turn the user prompted after starting aGiTrack.
    turn = _turn("u1", "a1", "go", "done", started=300)
    assert tracking_gap.split_untracked_turns([turn], 300.9)[1] == [turn]


def test_a_turn_with_no_timestamps_is_kept(tmp_path):
    # It cannot be placed either side of the floor. Real transcripts always stamp times, and
    # dropping every turn on a backend that did not would silently stop committing at all.
    turn = SessionTurn("u1", "a1", "p", "r", TokenUsage(), None)
    assert tracking_gap.split_untracked_turns([turn], 300.0) == ([], [turn])


# ---------------------------------------------------------------------------
# What the commit pipeline does with them
# ---------------------------------------------------------------------------


def test_commit_turns_never_writes_a_gap_turn_into_the_message(tmp_path):
    repo = _Repo(tmp_path)
    state = AgitrackState(tmp_path)
    tracking_gap.mark_stopped(tmp_path, now=100.0)
    tracking_gap.resume_tracking(tmp_path, now=300.0)

    committed = CommitEngine(repo, state).commit_turns(
        turns=[
            _turn("u1", "a1", "my private detour", "secret answer", started=150),
            _turn("u2", "a2", "back on the record", "done", started=400),
        ],
        backend="claude",
        backend_session_id="ses-1",
        model="m",
        stage_untracked_fn=lambda *a: None,
    )

    assert committed
    assert "my private detour" not in repo.message
    assert "secret answer" not in repo.message
    assert "back on the record" in repo.message


def test_commit_turns_makes_no_commit_when_every_turn_is_from_the_gap(tmp_path):
    repo = _Repo(tmp_path)
    state = AgitrackState(tmp_path)
    tracking_gap.mark_stopped(tmp_path, now=100.0)
    tracking_gap.resume_tracking(tmp_path, now=300.0)

    committed = CommitEngine(repo, state).commit_turns(
        turns=[_turn("u1", "a1", "my private detour", "secret answer", started=150)],
        backend="claude",
        backend_session_id="ses-1",
        model="m",
        stage_untracked_fn=lambda *a: None,
    )

    assert committed is False
    assert repo.message is None


def _finish(engine, session, commit_fn):
    return engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )


def _finish_helpers(tmp_path, exported, *, last_message_id=None):
    state = AgitrackState(tmp_path)
    session = Session.bare()
    session.state = state
    session.backend = types.SimpleNamespace(name="claude")
    session.agent_parse_result = (exported.session_id, exported, last_message_id, state)
    session.agent_parse_thread = None
    commits: list[dict] = []

    def commit_fn(**kwargs):
        commits.append(kwargs)
        return True

    return CommitEngine(_Repo(tmp_path), state), state, session, commits, commit_fn


def test_a_conversation_started_while_stopped_is_not_exported_whole(tmp_path):
    # The worst case: the user starts a fresh conversation during the gap, so there is no
    # watermark to hold anything back and the daemon adopts it as "the newest human-driven
    # session here". Every turn of it used to reach the next commit message.
    exported = ExportedSession(
        "ses-during-the-gap",
        "m",
        None,
        [
            _turn("u1", "a1", "personal detour one", "ok", started=120),
            _turn("u2", "a2", "personal detour two", "ok", started=140),
        ],
    )
    engine, state, session, commits, commit_fn = _finish_helpers(tmp_path, exported)
    tracking_gap.mark_stopped(tmp_path, now=100.0)
    tracking_gap.resume_tracking(tmp_path, now=300.0)

    result, _ = _finish(engine, session, commit_fn)

    assert result is False and commits == []
    # ...and the watermark moved past them, so the daemon does not re-export and re-drop the
    # same turns on every poll for the rest of the conversation's life.
    assert state.backend_message_id_for("ses-during-the-gap") == "a2"


def test_turns_after_the_restart_still_commit_normally(tmp_path):
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        [
            _turn("u1", "a1", "asked while stopped", "answered while stopped", started=150),
            _turn("u2", "a2", "asked after restarting", "answered after restarting", started=400),
        ],
    )
    engine, state, session, commits, commit_fn = _finish_helpers(tmp_path, exported)
    tracking_gap.mark_stopped(tmp_path, now=100.0)
    tracking_gap.resume_tracking(tmp_path, now=300.0)

    result, _ = _finish(engine, session, commit_fn)

    assert result is True
    assert [t.user_prompt for t in commits[0]["turns"]] == ["asked after restarting"]


def test_without_a_stop_every_turn_still_commits(tmp_path):
    # The guard must be inert on the overwhelmingly common path: a repo nobody ever stopped.
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        [_turn("u1", "a1", "first", "done", started=10), _turn("u2", "a2", "second", "done", started=20)],
    )
    engine, _state, session, commits, commit_fn = _finish_helpers(tmp_path, exported)

    result, _ = _finish(engine, session, commit_fn)

    assert result is True
    assert [t.user_prompt for t in commits[0]["turns"]] == ["first", "second"]


# ---------------------------------------------------------------------------
# Wiring: the commands that open and close the stretch
# ---------------------------------------------------------------------------


def test_agitrack_stop_records_the_stretch_even_with_nothing_running(tmp_path, capsys):
    # "Nothing was running" is not "nothing to record": the user is about to keep talking to
    # the agent, and an auto-start hook could bring a tracker back at any moment.
    from agitrack.stop import stop_everything

    repo = _init_repo(tmp_path)
    assert stop_everything(repo, assume_yes=True) == 0

    assert tracking_gap.mark_stopped(tmp_path, now=9e9) != 9e9  # a gap is already open
    assert "stay out of commit messages" in capsys.readouterr().out


def test_only_a_start_the_user_typed_closes_the_stretch(tmp_path, capsys):
    # `agitrack -b` is the one background entry point a person types, so it is the one that ends
    # the stretch. Reproduced live before this split: with an OpenCode plugin armed, a tracker was
    # back 1.6s after `agitrack stop`, closed the stretch itself, and committed the very
    # conversation the user had switched recording off for.
    from agitrack.proxy.background import _autostart_daemon_args, _resume_tracking_for, stop_background

    repo = _init_repo(tmp_path)
    stop_background(repo)
    assert tracking_gap.gap_is_open(tmp_path) is True

    # An auto-start hook firing here does not start a tracker, and does not close the stretch.
    assert _autostart_daemon_args(repo) is None
    assert tracking_gap.gap_is_open(tmp_path) is True

    _resume_tracking_for(repo)  # what `agitrack -b` does

    assert tracking_gap.gap_is_open(tmp_path) is False
    assert tracking_gap.tracking_floor(tmp_path) is not None
    assert "Tracking again" in capsys.readouterr().out


def test_a_tracker_running_with_the_stop_still_standing_says_so(tmp_path, capsys):
    # Auto-start refuses while the stop stands, so this should be unreachable. If a tracker gets
    # there anyway (a survivor the stop could not kill), it must not look like normal tracking:
    # it records nothing, and the log says why.
    from agitrack.proxy.background import BackgroundRunner

    repo = _init_repo(tmp_path)
    tracking_gap.mark_stopped(tmp_path)
    runner = types.SimpleNamespace(repo=repo, _debug=lambda *a, **k: None, printed=[])
    runner._print = runner.printed.append

    BackgroundRunner._report_open_stop(runner)

    assert any("has not been started again" in line for line in runner.printed)


def test_agitrack_stop_removes_the_autostart_hooks_for_every_backend(tmp_path, capsys):
    # Reported: `agitrack stop` left the hooks armed whenever the tracker was not the thing
    # running (a dashboard, an interactive session) — the disarm hung off the "nothing was
    # running" branch, which any of those makes false. Removal is never backend-scoped: the same
    # repo can be driven by Claude today and Codex tomorrow, so a stop sweeps all three.
    from agitrack.backends import agent_hooks
    from agitrack.stop import stop_everything

    repo = _init_repo(tmp_path)
    for backend in ("claude", "codex", "opencode"):
        agent_hooks.install_agent_autostart(tmp_path, backend)
    assert sorted(agent_hooks.installed_autostart_backends(tmp_path)) == ["claude", "codex", "opencode"]

    # Something other than the tracker is what `stop` finds running here.
    stopped = []
    import agitrack.stop as stop_module

    monkey = stop_module._stop_views
    stop_module._stop_views = lambda repo, survivors: ["dashboard (this repository is no longer served)"]
    try:
        assert stop_everything(repo, assume_yes=True) == 0
    finally:
        stop_module._stop_views = monkey
    del stopped

    assert agent_hooks.installed_autostart_backends(tmp_path) == []
    out = capsys.readouterr().out
    assert "Auto-start is off for this repo" in out


# ---------------------------------------------------------------------------
# End to end: a real repo, the real daemon, a real commit message
# ---------------------------------------------------------------------------


def test_the_real_daemon_writes_no_gap_conversation_into_a_real_commit(tmp_path):
    # The whole reported flow, driven through the real BackgroundRunner against a real git repo:
    # the user stops aGiTrack, keeps talking to the agent in its own UI, starts aGiTrack again,
    # and asks for one more thing. Only that last exchange may appear in git history.
    from agitrack.config.settings import GlobalConfig
    from agitrack.proxy.background import BackgroundRunner, _resume_tracking_for, stop_background

    repo = _init_repo(tmp_path)

    class _Backend:
        name = "claude"

        def __init__(self):
            self.exported: ExportedSession | None = None

        def latest_session_id(self, _repo):
            return self.exported.session_id if self.exported else None

        def export_session(self, _repo, session_id):
            return self.exported if self.exported and self.exported.session_id == session_id else None

    state = AgitrackState(tmp_path, default_backend="claude")
    backend = _Backend()
    runner = BackgroundRunner(
        repo, manual_commits=False, _global_config=GlobalConfig(path=tmp_path / "gc.json"), _state=state
    )
    runner.backend = backend
    runner._make_summarizer = lambda: None  # never call a real summarizer LLM in a test
    runner._summarization_enabled = lambda: False

    stop_background(repo)  # the user switches tracking off
    _resume_tracking_for(repo)  # ...and types `agitrack -b` again, which stamps the floor
    floor = tracking_gap.tracking_floor(tmp_path)
    assert floor is not None

    runner._manual.setup()
    (tmp_path / "a.txt").write_text("one\nedited while untracked\nedited after restarting\n", encoding="utf-8")
    backend.exported = ExportedSession(
        "ses-1",
        "claude-opus-4-8",
        None,
        [
            _turn("u1", "m1", "my private detour", "here is the secret answer", started=int(floor) - 60),
            _turn("u2", "m2", "rename the helper", "renamed it", started=int(floor) + 60),
        ],
    )

    runner._process_once()
    runner._auto_fold_pending()

    message = subprocess.run(
        ["git", "-C", str(tmp_path), "log", "-1", "--format=%B", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "my private detour" not in message
    assert "here is the secret answer" not in message
    assert "rename the helper" in message
    # The file the agent changed WHILE untracked is still committed: work in the tree still needs
    # committing, and hiding it would be a different (and worse) surprise. Only the conversation
    # is withheld.
    tree = subprocess.run(
        ["git", "-C", str(tmp_path), "show", "HEAD:a.txt"], check=True, capture_output=True, text=True
    ).stdout
    assert "edited while untracked" in tree
