"""Daemons restart themselves after aGiTrack is updated on disk (agitrack/update/restart.py).

aGiTrack still never INSTALLS updates; but once pip/pipx/brew/git-pull has swapped the
code underneath, every daemon (background tracker, dashboard, backtrace) must replace
itself with the new version instead of silently running stale modules.
"""

import threading
import time

from agitrack.update import restart as update_restart

from tests.test_background import _runner
from tests.test_dashboard_daemon import _FakeServer, _repo


def test_updated_disk_version_only_reports_a_real_change(monkeypatch):
    monkeypatch.setattr("agitrack._resolve_version", lambda: update_restart.RUNNING_VERSION)
    assert update_restart.updated_disk_version() is None
    monkeypatch.setattr("agitrack._resolve_version", lambda: "0.0.0")  # "could not resolve"
    assert update_restart.updated_disk_version() is None
    monkeypatch.setattr("agitrack._resolve_version", lambda: "9.9.9")
    assert update_restart.updated_disk_version() == "9.9.9"

    def boom() -> str:
        raise OSError("disk unhappy")

    monkeypatch.setattr("agitrack._resolve_version", boom)
    assert update_restart.updated_disk_version() is None


def test_watch_for_update_requires_two_consecutive_sightings():
    # A half-copied upgrade may flap: the watcher must fire only once the SAME new
    # version is seen twice in a row, and then exactly once.
    readings = iter([None, "1.1.1", "2.2.2", "2.2.2", "3.3.3"])
    fired: list[str] = []
    stop = threading.Event()
    thread = update_restart.watch_for_update(
        stop, fired.append, interval=0.01, read_version=lambda: next(readings, None)
    )
    thread.join(timeout=5)
    assert fired == ["2.2.2"]


def test_watch_for_update_stops_with_the_daemon():
    stop = threading.Event()
    thread = update_restart.watch_for_update(stop, lambda v: None, interval=30, read_version=lambda: None)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_restart_command_appends_the_port_flag_only_when_missing(monkeypatch):
    monkeypatch.setattr("sys.argv", ["agitrack", "--repo", "/x", "--dashboard-serve"])
    cmd = update_restart.restart_command(["--dashboard-port", "8770"])
    assert cmd[-2:] == ["--dashboard-port", "8770"]
    assert "--dashboard-serve" in cmd
    monkeypatch.setattr("sys.argv", ["agitrack", "--repo", "/x", "--dashboard-serve", "--dashboard-port", "8766"])
    cmd = update_restart.restart_command(["--dashboard-port", "8766"])
    assert cmd.count("--dashboard-port") == 1


def test_dashboard_daemon_restarts_itself_after_an_update(tmp_path, monkeypatch):
    from agitrack.metrics import daemon

    repo = _repo(tmp_path)
    fake = _FakeServer()
    monkeypatch.setattr(daemon, "build_server", lambda r, **kw: fake)
    monkeypatch.setattr(daemon.signal, "signal", lambda *a, **k: None)  # worker thread

    def fake_watch(stop, on_update, **kw):
        return threading.Thread(target=lambda: (time.sleep(0.05), on_update("9.9.9")), daemon=True).start()

    executed: list[list[str]] = []
    monkeypatch.setattr(update_restart, "watch_for_update", fake_watch)
    monkeypatch.setattr(update_restart, "exec_replacement", lambda cmd, log=print: executed.append(cmd))

    box: dict[str, int] = {}
    thread = threading.Thread(target=lambda: box.update(rc=daemon.run_dashboard_daemon(repo)))
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive() and box.get("rc") == 0
    # Cleanup ran first (socket closed, handshake cleared), THEN the exec — with the
    # bound port pinned so the user's open URL survives the swap.
    assert fake.shutdown_called and fake.closed
    assert daemon.read_handshake(repo) is None
    assert len(executed) == 1
    assert "--dashboard-port" in executed[0]


def test_background_restart_leaves_in_flight_turns_for_the_replacement(tmp_path, monkeypatch):
    # On an update restart the tracker must NOT force-capture an in-flight turn at the
    # swap: the replacement process resumes tracking and commits it when it truly ends.
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    calls: list[str] = []
    monkeypatch.setattr(runner, "_process_once", lambda **kw: calls.append(f"process:{kw}"))
    monkeypatch.setattr(runner, "_auto_fold_pending", lambda **kw: calls.append("fold"))

    runner._teardown(restarting=True)
    assert calls == []  # no force-capture, no fold

    runner._teardown()  # a REAL stop still captures the final turn
    assert any(call.startswith("process:") for call in calls) and "fold" in calls


def test_background_run_execs_replacement_after_update(tmp_path, monkeypatch):
    runner, repo, state, backend = _runner(tmp_path, manual=False)

    def fake_watch(stop, on_update, **kw):
        return threading.Thread(target=lambda: (time.sleep(0.05), on_update("9.9.9")), daemon=True).start()

    executed: list[list[str]] = []
    monkeypatch.setattr(update_restart, "watch_for_update", fake_watch)
    monkeypatch.setattr(update_restart, "exec_replacement", lambda cmd, log=print: executed.append(cmd))

    assert runner.run() == 0
    assert len(executed) == 1  # tore down, then replaced itself with the new code
