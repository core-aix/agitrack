"""The SUITE must not leak background daemons.

A full run left 60 alive on Windows and several on macOS — real detached processes still polling
long-deleted `pytest-of-…/pytest-NNN` temp dirs, indefinitely, with `--daemons list` giving no
signal (age, parent gone) to tell them from wanted ones. Two runs during this issue's own
re-test reproduced it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from conftest import collect_registered_daemons, reap_daemon_pids


def test_a_registered_daemon_is_killed_and_deregistered(tmp_path):
    registry = tmp_path / "daemons"
    registry.mkdir()
    # A real process standing in for a leaked daemon.
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    (registry / f"{victim.pid}.json").write_text(
        json.dumps({"pid": victim.pid, "kind": "background", "repo": str(tmp_path)}), encoding="utf-8"
    )

    pids = collect_registered_daemons(tmp_path)
    assert pids == [victim.pid]
    assert reap_daemon_pids(pids) >= 1

    for _ in range(100):
        if victim.poll() is not None:
            break
        time.sleep(0.05)
    victim.wait(timeout=10)
    assert victim.poll() is not None, "the leaked daemon survived"
    assert list(registry.glob("*.json")) == []  # and its entry is gone


def test_reaping_never_touches_this_process(tmp_path):
    """Scoped by pid as well as by config dir: a registry entry naming the test runner itself
    must never be signalled."""
    registry = tmp_path / "daemons"
    registry.mkdir()
    (registry / f"{os.getpid()}.json").write_text(
        json.dumps({"pid": os.getpid(), "kind": "background", "repo": str(tmp_path)}), encoding="utf-8"
    )

    # The runner's own pid is never collected, so it can never be signalled.
    assert collect_registered_daemons(tmp_path) == []

    assert list(registry.glob("*.json")) == []


def test_an_empty_or_absent_registry_is_fine(tmp_path):
    assert collect_registered_daemons(tmp_path) == []
    (tmp_path / "daemons").mkdir()
    assert collect_registered_daemons(tmp_path) == []
    assert reap_daemon_pids([]) == 0


def test_reaping_happens_once_at_session_end_not_after_every_test():
    """Killing during the run means signalling a process while another test may still be inside
    a call that waits on it — doing it per-test hung a full run at 98%, with a reader thread
    blocked forever on a pipe the dead daemon's children still held open. The leak this exists
    to fix is daemons surviving the whole RUN, so session end is both sufficient and safe."""
    import conftest

    # The per-test fixture only NOTES pids; the one that kills is session-scoped.
    assert conftest._note_daemons_a_test_started._fixture_function_marker.scope == "function"
    assert conftest._kill_leaked_daemons_at_the_end._fixture_function_marker.scope == "session"
