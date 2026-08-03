"""Lifecycle of the `agitrack --backtrace` daemon: start, report, stop, and recover.

`metrics/backtrace.py` was the second-least-covered user-facing module (66%), with its whole
daemon lifecycle untested. The failures here are the ones that make a feature look broken with
no explanation: a `stop` that reports success while the process lives on, a `status` that names
a daemon which died days ago, or — worst — a stale handshake file that makes every future
`--backtrace` believe one is already running, so the user can never start one again.

The handshake file is the daemon's whole notion of "am I running": a JSON record naming a pid
and a URL. Everything below is about that record staying honest about reality.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from agitrack.metrics import backtrace as bt

# Binds real TCP ports via the shared server helpers. Pinned to a single xdist worker with the
# other port-binding suites — see the `xdist_group` note in pyproject.toml.
pytestmark = pytest.mark.xdist_group("net")


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Point the daemon's handshake/log directory at a temp dir.

    Without this the tests read and WRITE the developer's real backtrace state, so a `stop`
    test would kill their live daemon — the same class of hazard the suite already guards
    against for the dashboard daemons.
    """
    state = tmp_path / "backtrace-state"
    state.mkdir()
    monkeypatch.setattr(bt, "_state_dir", lambda: state)
    return state


def _record(pid: int, url: str = "http://127.0.0.1:8765") -> dict:
    return {"pid": pid, "url": url}


# --- status -----------------------------------------------------------------


def test_status_reports_nothing_when_no_daemon_has_run(tmp_path, capsys):
    assert bt.backtrace_daemon_status(tmp_path) == 0
    assert "No backtrace daemon is running" in capsys.readouterr().out


def test_status_names_the_url_and_pid_of_a_live_daemon(tmp_path, capsys):
    # The URL is the entire point of the command — it is how the user opens the page.
    bt._write_handshake(tmp_path, _record(os.getpid(), "http://127.0.0.1:9999"))

    assert bt.backtrace_daemon_status(tmp_path) == 0

    out = capsys.readouterr().out
    assert "http://127.0.0.1:9999" in out and str(os.getpid()) in out


def test_status_does_not_report_a_daemon_whose_process_is_gone(tmp_path, capsys):
    # A crashed daemon leaves its handshake behind. Reporting it would send the user to a URL
    # that answers nothing.
    bt._write_handshake(tmp_path, _record(_dead_pid()))

    assert bt.backtrace_daemon_status(tmp_path) == 0
    assert "No backtrace daemon is running" in capsys.readouterr().out


# --- stale-record recovery --------------------------------------------------


def _dead_pid() -> int:
    """A pid that has certainly exited — a real child, waited on."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


def test_a_stale_handshake_is_cleared_so_a_new_daemon_can_start(tmp_path):
    """The one that bricks the feature.

    If a crashed daemon's record is treated as live, every later `--backtrace` believes one is
    already running and refuses to start — permanently, because nothing ever removes the file.
    Reading it must both report "not running" AND clear it.
    """
    bt._write_handshake(tmp_path, _record(_dead_pid()))
    assert bt._handshake_path(tmp_path).exists()

    assert bt._running_handshake(tmp_path) is None
    assert not bt._handshake_path(tmp_path).exists(), "the stale record was left to block future starts"


def test_a_corrupt_handshake_is_treated_as_no_daemon(tmp_path):
    # The file is written atomically, but it can still be truncated by a full disk, or written
    # by an older version with a different shape. Neither may raise.
    bt._handshake_path(tmp_path).write_text("{not json", encoding="utf-8")

    assert bt._read_handshake(tmp_path) is None
    assert bt._running_handshake(tmp_path) is None


def test_a_handshake_that_is_not_an_object_is_rejected(tmp_path):
    bt._handshake_path(tmp_path).write_text(json.dumps(["not", "a", "record"]), encoding="utf-8")
    assert bt._read_handshake(tmp_path) is None


def test_a_handshake_without_a_pid_is_not_treated_as_live(tmp_path):
    # A partially-written or older-format record must never read as a running daemon.
    bt._write_handshake(tmp_path, {"url": "http://127.0.0.1:8765"})
    assert bt._running_handshake(tmp_path) is None


# --- stop -------------------------------------------------------------------


def test_stop_reports_plainly_when_nothing_is_running(tmp_path, capsys):
    assert bt.stop_backtrace_daemon(tmp_path) == 0
    assert "No backtrace daemon is running" in capsys.readouterr().out


def test_stop_clears_a_stale_record_rather_than_claiming_to_stop_it(tmp_path, capsys):
    # Telling the user "Stopped (pid N)" for a process that died last week is a lie that hides
    # the real state; and leaving the record blocks the next start.
    bt._write_handshake(tmp_path, _record(_dead_pid()))

    assert bt.stop_backtrace_daemon(tmp_path) == 0

    assert "No backtrace daemon is running" in capsys.readouterr().out
    assert not bt._handshake_path(tmp_path).exists()


def test_stop_terminates_a_real_process_and_clears_its_record(tmp_path, capsys):
    """End to end against a REAL child process, because that is the claim: `stop` must actually
    stop it. A `stop` that returns 0 while the daemon keeps serving leaves the port held and
    the user with no way to reclaim it short of finding the pid themselves."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        bt._write_handshake(tmp_path, _record(child.pid))

        assert bt.stop_backtrace_daemon(tmp_path) == 0

        assert child.wait(timeout=30) is not None  # really gone, not just reported gone
        assert not bt._handshake_path(tmp_path).exists()
        assert str(child.pid) in capsys.readouterr().out
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


# Port scanning is NOT tested here. `bind_scanning` is shared with the dashboard and is already
# covered by tests/test_dashboard_network.py, which knows something this file got wrong: a
# stand-in listener must bind the way the real server does (`bind_exclusively`), because plain
# SO_REUSEADDR on Windows means "bind even if someone else already has this port" — so a naive
# blocker blocks nothing and the assertion passes vacuously on POSIX and fails on Windows.


# --- the log the daemon leaves behind ---------------------------------------


def test_the_daemon_log_path_is_scoped_to_the_directory(tmp_path):
    # Two directories tracked at once must not share a log or a handshake, or stopping one
    # reports on the other.
    other = tmp_path / "other"
    other.mkdir()

    assert bt._handshake_path(tmp_path) != bt._handshake_path(other)
    assert bt._log_path(tmp_path) != bt._log_path(other)


def test_two_directories_track_their_daemons_independently(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    bt._write_handshake(tmp_path, _record(os.getpid()))

    assert bt._running_handshake(tmp_path) is not None
    assert bt._running_handshake(other) is None
