"""Tests for the global daemon registry (`agitrack --daemons` and restart-on-update)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from agitrack import daemons


def test_register_list_deregister(monkeypatch, tmp_path):
    monkeypatch.setattr(daemons, "_registry_dir", lambda: tmp_path / "daemons")
    daemons.register("dashboard", "/home/me/myrepo", url="http://127.0.0.1:8765/")
    infos = [i for i in daemons.list_running() if i.pid == os.getpid()]
    assert len(infos) == 1
    assert infos[0].function == "repo dashboard"
    assert infos[0].repo_name == "myrepo"
    assert infos[0].url == "http://127.0.0.1:8765/"

    daemons.deregister()
    assert not any(i.pid == os.getpid() for i in daemons.list_running())


def test_list_running_prunes_dead_entries(monkeypatch, tmp_path):
    directory = tmp_path / "daemons"
    monkeypatch.setattr(daemons, "_registry_dir", lambda: directory)
    directory.mkdir()
    dead_pid = 2**31 - 1  # a pid that cannot be running
    entry = directory / f"{dead_pid}.json"
    entry.write_text(json.dumps({"pid": dead_pid, "kind": "background", "repo": "/r"}), encoding="utf-8")

    assert daemons.list_running() == []  # the dead entry is filtered out...
    assert not entry.exists()  # ...and pruned from disk


def test_restart_all_terminates_and_respawns(monkeypatch, tmp_path):
    directory = tmp_path / "daemons"
    monkeypatch.setattr(daemons, "_registry_dir", lambda: directory)
    directory.mkdir()
    marker = tmp_path / "respawned.txt"

    # A real, alive process stands in for a running daemon; its recorded command touches a marker.
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    (directory / f"{sleeper.pid}.json").write_text(
        json.dumps(
            {
                "pid": sleeper.pid,
                "kind": "dashboard",
                "repo": "/r/one",
                "cmd": [sys.executable, "-c", f"open(r'{marker}', 'w').write('x')"],
            }
        ),
        encoding="utf-8",
    )

    # Terminate = actually kill + reap the sleeper, so the wait loop sees it exit promptly.
    def fake_terminate(pid):
        if pid == sleeper.pid:
            sleeper.kill()
            sleeper.wait()

    monkeypatch.setattr(daemons, "terminate_pid", fake_terminate)

    restarted = daemons.restart_all(exclude_pid=os.getpid())
    assert restarted == 1
    assert not (directory / f"{sleeper.pid}.json").exists()  # old entry removed

    for _ in range(100):  # the re-spawn is detached; wait for its marker
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists()


def test_restart_all_skips_current_process(monkeypatch, tmp_path):
    monkeypatch.setattr(daemons, "_registry_dir", lambda: tmp_path / "daemons")
    daemons.register("background", "/r/self")  # this process registers itself
    # Excluding self (the default) must not terminate this very test process.
    assert daemons.restart_all() == 0
    daemons.deregister()


def test_daemons_cli_empty(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(daemons, "_registry_dir", lambda: tmp_path / "none")
    from agitrack.cli import main

    assert main(["--daemons"]) == 0
    assert "No aGiTrack daemons are currently running." in capsys.readouterr().out
