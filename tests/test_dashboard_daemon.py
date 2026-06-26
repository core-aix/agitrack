"""The out-of-process dashboard daemon (#110): handshake, lifecycle, owner watchdog."""

import subprocess
import sys
import threading
import types

import pytest

from agitrack.metrics import daemon


def _repo(tmp_path):
    """A minimal stand-in: the daemon only needs ``repo.repo`` (a writable dir)."""
    return types.SimpleNamespace(repo=tmp_path)


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows keeps process handles open after wait(); PID appears alive"
)
def test_pid_alive_distinguishes_live_and_dead_processes():
    import os

    assert daemon.pid_alive(os.getpid()) is True
    # A finished, reaped child's pid is dead.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert daemon.pid_alive(proc.pid) is False


def test_handshake_roundtrip_and_clear(tmp_path):
    repo = _repo(tmp_path)
    assert daemon.read_handshake(repo) is None
    daemon._write_handshake(repo, {"pid": 1, "url": "http://x/", "port": 8765})
    assert daemon.read_handshake(repo) == {"pid": 1, "url": "http://x/", "port": 8765}
    daemon.clear_handshake(repo)
    assert daemon.read_handshake(repo) is None
    daemon.clear_handshake(repo)  # idempotent, never raises


def test_running_handshake_clears_a_stale_record(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    daemon._write_handshake(repo, {"pid": 4242, "url": "http://x/"})

    monkeypatch.setattr(daemon, "pid_alive", lambda pid: True)
    assert daemon.running_handshake(repo) == {"pid": 4242, "url": "http://x/"}

    # Once the recorded pid is dead, the record is stale and gets cleared.
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: False)
    assert daemon.running_handshake(repo) is None
    assert daemon.read_handshake(repo) is None


def test_wait_for_handshake_matches_pid_and_times_out(tmp_path):
    repo = _repo(tmp_path)
    # A stale record for a different pid must not satisfy the wait.
    daemon._write_handshake(repo, {"pid": 1, "url": "http://old/"})
    assert daemon.wait_for_handshake(repo, pid=2, timeout=0.2) is None

    daemon._write_handshake(repo, {"pid": 2, "url": "http://new/"})
    assert daemon.wait_for_handshake(repo, pid=2, timeout=0.2) == {"pid": 2, "url": "http://new/"}


def test_start_reuses_a_running_daemon(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        daemon, "running_handshake", lambda r: {"pid": 7, "url": "http://127.0.0.1:8765/", "port": 8765}
    )
    monkeypatch.setattr(
        daemon, "spawn_dashboard_daemon", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not respawn"))
    )
    monkeypatch.setattr(daemon, "open_dashboard_in_browser", lambda url: True)

    assert daemon.start_dashboard_daemon(repo, owner_pid=999) == 0
    assert "already running" in capsys.readouterr().out


def test_start_spawns_waits_and_reports(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    spawned: dict[str, object] = {}
    monkeypatch.setattr(daemon, "running_handshake", lambda r: None)
    monkeypatch.setattr(
        daemon,
        "spawn_dashboard_daemon",
        lambda r, **kw: spawned.update(kw) or types.SimpleNamespace(pid=4242),
    )
    monkeypatch.setattr(
        daemon, "wait_for_handshake", lambda r, **kw: {"pid": 4242, "url": "http://127.0.0.1:8765/", "port": 8765}
    )
    opened: list[str] = []
    monkeypatch.setattr(daemon, "open_dashboard_in_browser", lambda url: opened.append(url) or True)

    assert daemon.start_dashboard_daemon(repo, owner_pid=555) == 0
    assert spawned["owner_pid"] == 555
    out = capsys.readouterr().out
    assert "daemon live at http://127.0.0.1:8765/" in out
    assert "stops when this terminal closes" in out
    assert opened == ["http://127.0.0.1:8765/"]


def test_start_reports_failure_when_daemon_never_binds(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(daemon, "running_handshake", lambda r: None)
    monkeypatch.setattr(daemon, "spawn_dashboard_daemon", lambda r, **kw: types.SimpleNamespace(pid=4242))
    monkeypatch.setattr(daemon, "wait_for_handshake", lambda r, **kw: None)

    assert daemon.start_dashboard_daemon(repo, owner_pid=555) == 1
    assert "did not start" in capsys.readouterr().out


def test_stop_reports_when_nothing_running(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(daemon, "running_handshake", lambda r: None)
    assert daemon.stop_dashboard_daemon(repo) == 0
    assert "No dashboard daemon is running" in capsys.readouterr().out


def test_stop_signals_and_clears(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    daemon._write_handshake(repo, {"pid": 4242, "url": "http://x/"})
    monkeypatch.setattr(daemon, "running_handshake", lambda r: {"pid": 4242, "url": "http://x/"})
    killed: list[int] = []
    # stop uses the cross-platform terminate_pid (SIGTERM / TerminateProcess), imported
    # into the daemon module's namespace.
    monkeypatch.setattr(daemon, "terminate_pid", lambda pid: killed.append(pid))
    # Report the process gone immediately so the wait loop returns at once.
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: False)

    assert daemon.stop_dashboard_daemon(repo) == 0
    assert killed == [4242]
    assert daemon.read_handshake(repo) is None
    assert "Stopped the dashboard daemon (pid 4242)" in capsys.readouterr().out


def test_status_reports_running_and_stopped(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(daemon, "running_handshake", lambda r: {"pid": 9, "url": "http://127.0.0.1:8765/"})
    assert daemon.dashboard_daemon_status(repo) == 0
    assert "running at http://127.0.0.1:8765/ (pid 9)" in capsys.readouterr().out

    monkeypatch.setattr(daemon, "running_handshake", lambda r: None)
    assert daemon.dashboard_daemon_status(repo) == 0
    assert "No dashboard daemon is running" in capsys.readouterr().out


class _FakeServer:
    def __init__(self):
        self.server_address = ("127.0.0.1", 12345)
        self._stop = threading.Event()
        self.shutdown_called = False
        self.closed = False

    def serve_forever(self):
        self._stop.wait(5)

    def shutdown(self):
        self.shutdown_called = True
        self._stop.set()

    def server_close(self):
        self.closed = True


def test_run_dashboard_daemon_shuts_down_when_owner_dies(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    fake = _FakeServer()
    monkeypatch.setattr(daemon, "build_server", lambda r, **kw: fake)
    # signal.signal only works on the main thread; the serve loop runs in a worker here.
    monkeypatch.setattr(daemon.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_OWNER_POLL_SECONDS", 0.02)
    # Owner already gone → the watchdog should request shutdown on its first check.
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: False)

    box: dict[str, int] = {}
    thread = threading.Thread(target=lambda: box.update(rc=daemon.run_dashboard_daemon(repo, owner_pid=999999)))
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert box.get("rc") == 0
    assert fake.shutdown_called and fake.closed
    # The handshake is published while serving, then cleared on shutdown.
    assert daemon.read_handshake(repo) is None


def test_run_dashboard_daemon_publishes_handshake_then_serves(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    seen: dict[str, object] = {}

    class _CapturingServer(_FakeServer):
        def serve_forever(self):
            # Capture the handshake that was published before serving started.
            seen["handshake"] = daemon.read_handshake(repo)
            super().serve_forever()

    capturing = _CapturingServer()
    monkeypatch.setattr(daemon, "build_server", lambda r, **kw: capturing)
    monkeypatch.setattr(daemon.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_OWNER_POLL_SECONDS", 0.02)
    monkeypatch.setattr(daemon, "pid_alive", lambda pid: False)

    thread = threading.Thread(target=lambda: daemon.run_dashboard_daemon(repo, owner_pid=999999))
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    record = seen["handshake"]
    assert record is not None
    assert record["url"] == "http://127.0.0.1:12345/"
    assert record["port"] == 12345
