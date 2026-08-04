"""Turn-end detection, on EVERY backend.

``_backend_idle_for`` answers one question — "has this turn finished, so we can commit?" —
and it is the single most consequential decision aGiTrack makes on a timer. Both ways of
getting it wrong are silent and both cost the user work:

  * **says busy forever** → the turn end is never detected, so nothing is ever committed
    until the user types their next prompt. The worktree just stays dirty.
  * **says idle too early** → aGiTrack commits a HALF-FINISHED turn, and under
    ``--no-worktree`` also offers to commit the user's own uncommitted changes.

Neither surfaces an error. The user just finds out later.

The mechanism needs two signals, because each alone produces one of those failures: the PTY
(wrong when the TUI emits an idle heartbeat — Claude's measured at 8 bytes/second on Linux,
which pins "busy" forever) and a transcript signal that advances only on real backend work
(wrong when nothing has been written yet). These tests pin both directions on both backends;
before the fix that accompanies them, OpenCode supplied no transcript signal at all and ran
the whole session on the PTY alone.
"""

from __future__ import annotations

import time

import pytest

from proxy_helpers import make_runner

BACKENDS = ["claude", "opencode"]


def _runner_with_transcript(backend_name, tmp_path, *, transcript_age: float | None):
    """A runner whose active session reports its transcript as last touched
    ``transcript_age`` seconds ago (None ⇒ no transcript signal at all)."""
    from agitrack.config import AgitrackState

    state = AgitrackState(tmp_path, default_backend=backend_name)
    state.backend_session_id = "session-under-test"
    runner = make_runner(state=state)
    mtime = None if transcript_age is None else time.time() - transcript_age
    runner._active_transcript_mtime = lambda: mtime
    return runner


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_quiet_subagent_does_not_read_as_the_turn_ending(backend_name, tmp_path):
    # The sub-agent case: the main agent prints nothing to the terminal while a sub-agent
    # works, but the transcript keeps advancing. Committing here would capture a half-finished
    # turn — so a fresh transcript must override a quiet PTY.
    runner = _runner_with_transcript(backend_name, tmp_path, transcript_age=0.0)
    runner.last_child_output = time.monotonic() - 3600  # terminal utterly silent

    assert runner._backend_idle_for(5.0) is False


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_chattering_idle_heartbeat_does_not_prevent_the_turn_from_ending(backend_name, tmp_path):
    # The mirror, and the one that costs the user the most: a TUI heartbeat means the PTY is
    # NEVER quiet, so without a transcript to overrule it nothing is ever committed. A
    # transcript quiet for TRANSCRIPT_IDLE_FACTOR x the window has the final say.
    from agitrack.proxy.runner import ProxyRunner

    window = 5.0
    quiet_enough = window * (ProxyRunner.TRANSCRIPT_IDLE_FACTOR + 1)  # comfortably past the threshold
    runner = _runner_with_transcript(backend_name, tmp_path, transcript_age=quiet_enough)
    runner.last_child_output = time.monotonic()  # heartbeat this very instant

    assert runner._backend_idle_for(window) is True


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_both_quiet_is_the_ordinary_turn_end(backend_name, tmp_path):
    runner = _runner_with_transcript(backend_name, tmp_path, transcript_age=60.0)
    runner.last_child_output = time.monotonic() - 60.0

    assert runner._backend_idle_for(5.0) is True


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_without_a_transcript_signal_the_pty_alone_decides(backend_name, tmp_path):
    # The documented fallback, pinned so its blast radius stays understood: with no transcript
    # the PTY is the only evidence, so a chattering backend reads as busy indefinitely. This is
    # exactly the state OpenCode was stuck in permanently before it gained a signal, and it is
    # why `test_backend_parity.py` requires every backend to offer one.
    runner = _runner_with_transcript(backend_name, tmp_path, transcript_age=None)
    runner.last_child_output = time.monotonic()

    assert runner._backend_idle_for(5.0) is False


# --- the signal itself, per backend ----------------------------------------


def test_opencode_reports_activity_from_its_session_store(tmp_path, monkeypatch):
    # OpenCode has no transcript FILE, so its signal is a bounded read-only query against its
    # SQLite store. Build a store with the real schema and check the runner-facing answer.
    import sqlite3

    from agitrack.transcripts import opencode

    root = tmp_path / "opencode"
    root.mkdir()
    connection = sqlite3.connect(root / "opencode.db")
    connection.execute(
        "CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, "
        "time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL)"
    )
    # Milliseconds, as OpenCode stores them.
    connection.executemany(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        [
            ("m1", "ses_alpha", 1_700_000_000_000, 1_700_000_000_000, "{}"),
            ("m2", "ses_alpha", 1_700_000_005_000, 1_700_000_005_000, "{}"),
            ("m3", "ses_beta", 1_700_000_900_000, 1_700_000_900_000, "{}"),
        ],
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(opencode, "_opencode_data_root", lambda: root)

    # The NEWEST message in THIS session — never the newest overall.
    assert opencode.session_last_activity("ses_alpha") == 1_700_000_005.0
    assert opencode.session_last_activity("ses_beta") == 1_700_000_900.0
    # Session-scoping is the load-bearing property: a signal shared across sessions would read
    # as "still running" whenever any OTHER session was busy, and this one would never commit.
    assert opencode.session_last_activity("ses_alpha") != opencode.session_last_activity("ses_beta")


def test_opencode_activity_is_none_when_the_store_is_unusable(tmp_path, monkeypatch):
    # Every failure mode — no database, wrong schema, corrupt file — must degrade to None
    # rather than raise: this is polled from the reactor, where an exception is fatal.
    from agitrack.transcripts import opencode

    root = tmp_path / "opencode"
    root.mkdir()
    monkeypatch.setattr(opencode, "_opencode_data_root", lambda: root)
    assert opencode.session_last_activity("ses_alpha") is None  # no database at all

    (root / "opencode.db").write_text("this is not a database")
    assert opencode.session_last_activity("ses_alpha") is None  # corrupt

    import sqlite3

    (root / "opencode.db").unlink()
    connection = sqlite3.connect(root / "opencode.db")
    connection.execute("CREATE TABLE unrelated (x integer)")
    connection.commit()
    connection.close()
    assert opencode.session_last_activity("ses_alpha") is None  # schema from another version


def test_opencode_never_writes_to_the_users_database(tmp_path, monkeypatch):
    # Opened mode=ro. aGiTrack polls this from the reactor while OpenCode itself is writing;
    # taking a write lock (or creating a stray database) would be a genuine hazard to the
    # user's own sessions.
    import sqlite3

    from agitrack.transcripts import opencode

    root = tmp_path / "opencode"
    root.mkdir()
    monkeypatch.setattr(opencode, "_opencode_data_root", lambda: root)

    database = root / "opencode.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, "
        "time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL)"
    )
    connection.commit()
    connection.close()
    before = database.stat().st_mtime

    for _ in range(5):
        opencode.session_last_activity("ses_alpha")

    assert database.stat().st_mtime == before
    assert not (root / "opencode.db-journal").exists()


def test_runner_prefers_a_direct_activity_signal_over_a_transcript_path(tmp_path):
    # The runner asks for the direct signal first, so a backend without a stat-able transcript
    # is served. A backend offering both must not be forced through the path.
    runner = _runner_with_transcript("claude", tmp_path, transcript_age=None)
    runner.state.backend_session_id = "sid"
    del runner._active_transcript_mtime  # exercise the real implementation

    class _Backend:
        def session_activity_mtime(self, session_id):
            assert session_id == "sid"
            return 1234.0

        def session_transcript_path(self, session_id):  # pragma: no cover - must not be reached
            raise AssertionError("the direct signal should have answered first")

    runner.backend = _Backend()
    assert runner._active_transcript_mtime() == 1234.0


def test_runner_survives_a_backend_whose_activity_signal_raises(tmp_path):
    # Best-effort means best-effort: a backend store that is locked or mid-upgrade must not
    # take the reactor down.
    runner = _runner_with_transcript("claude", tmp_path, transcript_age=None)
    runner.state.backend_session_id = "sid"
    del runner._active_transcript_mtime  # exercise the real implementation

    class _Backend:
        def session_activity_mtime(self, session_id):
            raise RuntimeError("database is locked")

    runner.backend = _Backend()
    assert runner._active_transcript_mtime() is None


def test_opencode_resolves_the_model_from_its_session_store(tmp_path, monkeypatch):
    """OpenCode's event stream names no model; its store does.

    Verified against the real CLI: none of `step_start`, `text` or `step_finish` carries a
    model, so every headless OpenCode run (the summarizer, the learning page) came back with
    `model=None` and the commit metadata recorded no model at all — while the same commit on
    Claude recorded a real one. Caught by `-m live`, which is exactly the drift mocked tests
    cannot see; pinned here so it stays fixed without needing the CLI.
    """
    import json
    import sqlite3

    from agitrack.transcripts import opencode

    root = tmp_path / "opencode"
    root.mkdir()
    connection = sqlite3.connect(root / "opencode.db")
    connection.execute("CREATE TABLE session (id text PRIMARY KEY, model text)")
    connection.execute(
        "INSERT INTO session VALUES (?, ?)",
        ("ses_x", json.dumps({"id": "gpt-5.5", "providerID": "openai", "variant": "minimal"})),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(opencode, "_opencode_data_root", lambda: root)

    assert opencode.session_model("ses_x") == "openai/gpt-5.5"
    assert opencode.session_model("ses_unknown") is None
    assert opencode.session_model("") is None


def test_opencode_model_lookup_tolerates_an_unexpected_store_shape(tmp_path, monkeypatch):
    # A future OpenCode may store a plain name, or nothing at all. Neither may raise: this runs
    # on the summarizer's path, where an exception loses the summary.
    import sqlite3

    from agitrack.transcripts import opencode

    root = tmp_path / "opencode"
    root.mkdir()
    connection = sqlite3.connect(root / "opencode.db")
    connection.execute("CREATE TABLE session (id text PRIMARY KEY, model text)")
    connection.executemany("INSERT INTO session VALUES (?, ?)", [("ses_plain", "claude-opus-5"), ("ses_null", None)])
    connection.commit()
    connection.close()
    monkeypatch.setattr(opencode, "_opencode_data_root", lambda: root)

    assert opencode.session_model("ses_plain") == "claude-opus-5"  # plain string still works
    assert opencode.session_model("ses_null") is None
