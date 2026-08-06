"""Three reported faults around `/clear` and concurrent sessions.

All observed live, in `--no-worktree` auto-commit mode (`agitrack --no-worktree`, the mode a
`/clear` inside the backend lands in):

1. **A turn after a native `/clear` did not commit until aGiTrack exited.** The switch watcher
   detects the new conversation and deliberately commits its turn *before* opening any dialog —
   but in no-worktree AUTO that commit only writes a hidden LATENT commit, and the throttled poll
   that folds latent turns into real ones was blocked by the very dialog the sync commit was
   meant to get ahead of. Killed rather than quit, the turn was never committed at all.
2. **The session was asked to be named again.** The dialog was "New conversation detected —
   Name:", asked because the `/clear` conversation had not been seen before. Worktree mode had
   always kept the session's own name here; no-worktree now does too.
3. **The TUI slowed down with several concurrent sessions.** Background sessions are serviced on
   the REACTOR thread (the pass swaps `self.active`, so it cannot go on the git worker), and each
   pass is a burst of git. At a flat `POLL_SECONDS` that is a permanent tax per background
   session on the thread that also services the keyboard.
"""

from __future__ import annotations

import types

from tests.test_proxy import _bg_session, _mux_runner, make_runner


# ---------------------------------------------------- 1 & 2: the native /clear switch


def _switch_runner(monkeypatch, *, known_name=None):
    """A runner whose durable name record reports ``known_name`` for any conversation."""
    runner = make_runner(name="alpha", worktree=None)
    runner._render = lambda: None
    runner._set_message = lambda *a, **k: None
    runner.base_repo = types.SimpleNamespace(repo="base")
    monkeypatch.setattr(
        "agitrack.proxy.runner.AgitrackState",
        lambda *a, **k: types.SimpleNamespace(session_name_for=lambda sid: known_name),
    )
    return runner


def test_a_clear_switch_keeps_the_session_name_and_never_asks(monkeypatch):
    # The reported second fault. A conversation started with `/clear` is this session
    # continuing, so it inherits the name the user already gave — and, because the question was
    # a modal, not asking is also what keeps the reactor (and the commit fold) running.
    runner = _switch_runner(monkeypatch)  # the conversation is unknown to the record
    asked: list = []
    persisted: list = []
    runner._prompt_session_name = lambda *a, **k: asked.append(a) or "renamed"
    runner._persist_session_name = lambda sid: persisted.append(sid)

    runner._restore_or_ask_session_name("new-conversation-id", adopt=True)

    assert asked == []  # no modal, so nothing blocks the reactor
    assert persisted == ["new-conversation-id"]
    assert runner.name == "alpha"  # the session keeps the name the user chose


def test_an_unknown_conversation_without_adopt_still_asks(monkeypatch):
    # The other two cases must not change: a conversation arriving from somewhere else is new
    # work and is still named, so `adopt` is genuinely narrow.
    runner = _switch_runner(monkeypatch)
    asked: list = []
    runner._prompt_session_name = lambda *a, **k: asked.append(a) or "chosen"
    runner._persist_session_name = lambda sid: None
    runner._next_session_name = lambda: "suggested"

    runner._restore_or_ask_session_name("other-conversation")

    assert len(asked) == 1
    assert runner.name == "chosen"


def test_a_known_conversation_is_restored_not_adopted(monkeypatch):
    # Going BACK to a conversation aGiTrack has seen restores ITS name, even under adopt —
    # otherwise `/resume` would relabel an old conversation with the current session's name.
    runner = _switch_runner(monkeypatch, known_name="earlier-name")
    asked: list = []
    runner._prompt_session_name = lambda *a, **k: asked.append(a) or "wrong"

    runner._restore_or_ask_session_name("seen-before", adopt=True)

    assert asked == []
    assert runner.name == "earlier-name"


def test_sync_commit_folds_the_latent_turn_in_noworktree_auto():
    # The reported first fault. Callers use `_commit_latest_turn_sync` precisely when the
    # throttled fold poll is about to stop running (a modal opening, a context swap, teardown),
    # so the sync commit has to land the real commit itself, not just the latent record.
    runner = make_runner()
    folded: list = []
    runner.agent_parse_thread = None
    runner._finish_agent_parse_if_ready = lambda **k: None
    runner._start_agent_parse = lambda: False
    runner._auto_fold_latent_pending = lambda **k: folded.append(k)
    runner._use_worktrees = False  # `_noworktree_auto` is (no worktrees) and (not manual)
    runner._manual_commits = False

    runner._commit_latest_turn_sync()

    assert folded == [{"force": True}]  # forced: a late summary amends into the fold commit


def test_sync_commit_does_not_fold_outside_noworktree_auto():
    # Worktree and manual modes have their own paths; folding there would commit for the user.
    runner = make_runner()
    folded: list = []
    runner.agent_parse_thread = None
    runner._finish_agent_parse_if_ready = lambda **k: None
    runner._start_agent_parse = lambda: False
    runner._auto_fold_latent_pending = lambda **k: folded.append(k)
    runner._use_worktrees = True  # worktree mode: the fold is not this path's job

    runner._commit_latest_turn_sync()

    assert folded == []


# ---------------------------------------------------- 3: background poll backoff


def test_background_poll_backs_off_while_a_session_stays_quiet():
    runner = _mux_runner()
    runner.POLL_SECONDS = 2.0
    session = _bg_session("B")
    session.last_child_output = 5.0

    # First pass: the session has spoken since the (zero) watermark, so it is on the fast cadence.
    runner._note_background_poll(session)
    assert runner._background_poll_interval(session) == 2.0

    # Each pass that finds no new backend output doubles the interval, up to the idle cap.
    seen = []
    for _ in range(6):
        runner._note_background_poll(session)
        seen.append(runner._background_poll_interval(session))

    assert seen == [4.0, 8.0, 16.0, 30.0, 30.0, 30.0]  # 2*16=32, capped at the idle cadence
    assert max(seen) == runner.BACKGROUND_IDLE_POLL_SECONDS


def test_background_poll_returns_to_the_fast_cadence_when_the_agent_speaks():
    # The backoff must never delay a finished turn: a turn cannot end without the agent
    # writing something, and that resets the cadence for the very next pass.
    runner = _mux_runner()
    runner.POLL_SECONDS = 2.0
    session = _bg_session("B")
    session.last_child_output = 1.0
    for _ in range(6):
        runner._note_background_poll(session)
    assert runner._background_poll_interval(session) == runner.BACKGROUND_IDLE_POLL_SECONDS

    session.last_child_output = 99.0  # the backend said something
    runner._note_background_poll(session)

    assert runner._background_poll_interval(session) == 2.0


def test_backed_off_session_is_skipped_then_serviced_when_due():
    # End to end through the reactor's pass: a quiet session stops being polled every couple of
    # seconds, and is still serviced once its (longer) interval elapses.
    runner = _mux_runner()
    runner.merge_ctx = None
    runner.CHILD_IDLE_SECONDS = 4.0
    runner.POLL_SECONDS = 2.0
    session = _bg_session("B")
    session.last_child_output = 0.0  # long idle
    session._bg_poll_misses = 4  # already backed off to the cap
    session._bg_poll_seen = 0.0
    runner.sessions.append(session)
    serviced: list = []
    runner._with_session = lambda s, fn: serviced.append(s.name) or "integrated"

    import time as _time

    session.last_poll = _time.monotonic() - 5.0  # 5s ago: due at 2s, not at 30s
    runner._service_background_sessions()
    assert serviced == []

    session.last_poll = _time.monotonic() - 31.0  # past the idle cap
    runner._service_background_sessions()
    assert serviced == ["B"]
