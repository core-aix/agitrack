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
    # A real process standing in for a leaked daemon. It has to CARRY `--background-serve`:
    # the reaper now demands proof from the process table that a pid really is an aGiTrack
    # daemon before signalling it (the registry alone is not identity — see reap_daemon_pids),
    # so a stand-in that does not look like one is correctly left alone. The flag is inert for
    # `python -c` beyond landing in sys.argv, which is exactly where the scan reads it.
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "--background-serve"])
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


def test_the_reaper_never_kills_a_pid_that_is_not_a_daemon(tmp_path):
    """A registry entry outlives the process that wrote it, and Windows reassigns PIDs briskly,
    so "the registry says this number was a daemon" is not evidence that it still is.

    This is not hypothetical here: under xdist each worker runs this teardown when IT finishes
    while its siblings are still working, so an unverified kill reaches another worker. That is
    what `[gw0] node down: Not properly terminated` at 97% of a run was — the session ended with
    no summary, and the coverage total for the whole run went with it."""
    registry = tmp_path / "daemons"
    registry.mkdir()
    # An innocent process whose pid a stale entry claims. Nothing about it says "aGiTrack".
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        (registry / f"{bystander.pid}.json").write_text(
            json.dumps({"pid": bystander.pid, "kind": "background", "repo": str(tmp_path)}), encoding="utf-8"
        )

        reap_daemon_pids(collect_registered_daemons(tmp_path))

        time.sleep(0.3)
        assert bystander.poll() is None, "the reaper killed a process that was never a daemon"
    finally:
        bystander.kill()
        bystander.wait(timeout=10)
