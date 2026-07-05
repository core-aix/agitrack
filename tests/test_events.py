"""The user-facing event log (``--log-file`` / ``log_file`` config): append-only, greppable,
best-effort. Works in every mode; here we lock the primitive (path resolution + line format)."""

from __future__ import annotations

from pathlib import Path

from agitrack.events import EventLog, resolve_log_path


def test_resolve_log_path_none_relative_absolute_and_tilde(tmp_path):
    assert resolve_log_path(None, tmp_path) is None
    assert resolve_log_path("", tmp_path) is None
    assert resolve_log_path("   ", tmp_path) is None
    # A relative path resolves against the repo root, so the same config value is stable
    # regardless of the shell's cwd.
    assert resolve_log_path("events.log", tmp_path) == tmp_path / "events.log"
    assert resolve_log_path("logs/a.log", tmp_path) == tmp_path / "logs" / "a.log"
    assert resolve_log_path("/abs/x.log", tmp_path) == Path("/abs/x.log")
    assert resolve_log_path("~/x.log", tmp_path) == Path.home() / "x.log"


def test_event_log_disabled_is_a_noop():
    # A None path makes emit a no-op — callers never branch on whether logging is on.
    EventLog(None).emit("commit", sha="abc")  # must not raise


def test_event_log_format_quotes_and_drops_none(tmp_path):
    log = tmp_path / "events.log"
    el = EventLog(log)
    el.emit("commit", sha="deadbeef1234", type="agent", subject="Add validation to parse()")
    el.emit("ai-change-detected", backend="claude", session=None)  # None fields dropped
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    # <iso-timestamp> event k=v … ; whitespace/quotes force quoting; a value with none stays bare.
    assert lines[0].split(" ", 1)[1] == 'commit sha=deadbeef1234 type=agent subject="Add validation to parse()"'
    assert lines[1].endswith("ai-change-detected backend=claude")  # session=None omitted


def test_event_log_flattens_newlines(tmp_path):
    log = tmp_path / "events.log"
    EventLog(log).emit("note", text="line one\nline two")
    assert len(log.read_text().splitlines()) == 1  # one event is always one line


def test_event_log_survives_unwritable_path(tmp_path):
    # Best-effort: an unwritable path (a directory where the parent is a file) is swallowed.
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    EventLog(blocker / "sub" / "events.log").emit("commit", sha="abc")  # must not raise
