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
    monkeypatch.setattr(daemons, "_scan_daemon_processes", lambda: [])  # isolate from real OS processes
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
    monkeypatch.setattr(daemons, "_scan_daemon_processes", lambda: [])  # isolate from real OS processes
    directory.mkdir()
    dead_pid = 2**31 - 1  # a pid that cannot be running
    entry = directory / f"{dead_pid}.json"
    entry.write_text(json.dumps({"pid": dead_pid, "kind": "background", "repo": "/r"}), encoding="utf-8")

    assert daemons.list_running() == []  # the dead entry is filtered out...
    assert not entry.exists()  # ...and pruned from disk


def test_restart_all_terminates_and_respawns(monkeypatch, tmp_path):
    directory = tmp_path / "daemons"
    monkeypatch.setattr(daemons, "_registry_dir", lambda: directory)
    monkeypatch.setattr(daemons, "_scan_daemon_processes", lambda: [])  # isolate from real OS processes
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
    monkeypatch.setattr(daemons, "_scan_daemon_processes", lambda: [])  # isolate from real OS processes
    daemons.register("background", "/r/self")  # this process registers itself
    # Excluding self (the default) must not terminate this very test process.
    assert daemons.restart_all() == 0
    daemons.deregister()


def test_scan_daemon_processes_parses_ps(monkeypatch):
    canned = (
        "  501 /usr/bin/python3 -m agitrack --repo /home/me/proj --dashboard-serve --dashboard-owner-pid 42\n"
        "  777 /usr/bin/python3 -m agitrack --repo /home/me/other --backtrace-serve --dashboard-owner-pid 9\n"
        "  888 /usr/bin/python3 -m agitrack --repo /home/me/bg --background --background-serve\n"
        "  999 -zsh\n"
        " 1000 /usr/bin/python3 -m agitrack --dashboard\n"  # interactive dashboard launcher, NOT a daemon
    )

    class _R:
        stdout = canned

    monkeypatch.setattr(daemons.subprocess, "run", lambda *a, **k: _R())
    by_kind = {i.kind: i for i in daemons._scan_daemon_processes()}
    assert set(by_kind) == {"dashboard", "backtrace", "background"}  # the no-serve-flag lines are ignored
    assert by_kind["dashboard"].pid == 501 and by_kind["dashboard"].repo == "/home/me/proj"
    assert by_kind["backtrace"].repo == "/home/me/other"
    assert by_kind["background"].repo == "/home/me/bg"
    assert "--dashboard-serve" in by_kind["dashboard"].cmd  # its argv can re-launch it


def test_list_running_finds_unregistered_daemon_via_ps(monkeypatch, tmp_path):
    """A daemon with NO registry entry (e.g. started before the registry existed) is still found."""
    monkeypatch.setattr(daemons, "_registry_dir", lambda: tmp_path / "empty")  # no registry entries

    class _R:
        stdout = "  501 /usr/bin/python3 -m agitrack --repo /r/one --dashboard-serve --dashboard-owner-pid 1\n"

    # This test exercises the REAL scan (parsing a mocked `_process_command_lines`), so it must not
    # stub _scan_daemon_processes.
    monkeypatch.setattr(daemons, "_process_command_lines", lambda: _R().stdout.splitlines())
    infos = daemons.list_running()
    assert any(i.pid == 501 and i.kind == "dashboard" and i.repo == "/r/one" for i in infos)


def test_ps_scan_failure_is_graceful(monkeypatch):
    def boom(*a, **k):
        raise OSError("ps not available")

    monkeypatch.setattr(daemons.subprocess, "run", boom)
    assert daemons._scan_daemon_processes() == []  # never raises where ps is missing (e.g. Windows)


def test_daemons_cli_empty(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(daemons, "_registry_dir", lambda: tmp_path / "none")
    monkeypatch.setattr(daemons, "_scan_daemon_processes", lambda: [])  # isolate from real OS processes
    from agitrack.cli import main

    assert main(["--daemons"]) == 0
    assert "No aGiTrack daemons are currently running." in capsys.readouterr().out
