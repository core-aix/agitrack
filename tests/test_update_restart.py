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


def test_updated_fingerprint_only_reports_a_settled_change(monkeypatch):
    monkeypatch.setattr(update_restart, "RUNNING_FINGERPRINT", "commit:aaa")
    monkeypatch.setattr(update_restart, "disk_fingerprint", lambda: "commit:aaa")
    assert update_restart.updated_fingerprint() is None  # same install
    monkeypatch.setattr(update_restart, "disk_fingerprint", lambda: None)  # update in progress
    assert update_restart.updated_fingerprint() is None
    monkeypatch.setattr(update_restart, "disk_fingerprint", lambda: "commit:bbb")
    assert update_restart.updated_fingerprint() == "commit:bbb"
    # Unknown at daemon start: never act on guesses for the whole process lifetime.
    monkeypatch.setattr(update_restart, "RUNNING_FINGERPRINT", None)
    assert update_restart.updated_fingerprint() is None


def test_source_fingerprint_is_the_head_commit_and_waits_for_index_lock(tmp_path, monkeypatch):
    # Source install: "a full update completed" means a NEW COMMIT LANDED. While a
    # pull/checkout is mid-flight (index.lock held) the reading is unsettled — a daemon
    # restarting into a half-updated tree would just crash on import.
    from agitrack.git import GitRepo

    repo = GitRepo.init(tmp_path)
    repo._run(["git", "commit", "--allow-empty", "-m", "seed"])
    monkeypatch.setattr(update_restart, "_source_root", lambda: tmp_path)

    head = repo.rev_parse("HEAD")
    assert update_restart.disk_fingerprint() == f"commit:{head}"

    (tmp_path / ".git" / "index.lock").write_text("", encoding="utf-8")  # pull in progress
    assert update_restart.disk_fingerprint() is None

    (tmp_path / ".git" / "index.lock").unlink()
    repo._run(["git", "commit", "--allow-empty", "-m", "the update landed"])
    assert update_restart.disk_fingerprint() == "commit:" + repo.rev_parse("HEAD")


def test_wheel_fingerprint_is_the_installed_version(monkeypatch):
    # Wheel install: pip rewrites dist-info as the LAST step, so readable metadata means
    # the reinstall finished; missing metadata means it is still running (unsettled).
    monkeypatch.setattr(update_restart, "_source_root", lambda: None)
    monkeypatch.setattr("agitrack._installed_version", lambda: "1.2.3")
    assert update_restart.disk_fingerprint() == "version:1.2.3"
    monkeypatch.setattr("agitrack._installed_version", lambda: None)
    assert update_restart.disk_fingerprint() is None


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


class _Execed(BaseException):
    """Raised by test doubles of exec_replacement to model a SUCCESSFUL exec: the real
    one never returns (the process image is replaced). BaseException so the daemon's
    own exception handling can't swallow it."""


def _daemon_thread(target):
    box: dict[str, object] = {}

    def _run():
        try:
            box["rc"] = target()
        except _Execed:
            box["rc"] = "execed"

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    return box["rc"]


def test_dashboard_daemon_restarts_itself_after_an_update(tmp_path, monkeypatch):
    from agitrack.metrics import daemon

    repo = _repo(tmp_path)
    fakes: list[_FakeServer] = []

    def make_server(r, **kw):
        fakes.append(_FakeServer())
        return fakes[-1]

    monkeypatch.setattr(daemon, "build_server", make_server)
    monkeypatch.setattr(daemon.signal, "signal", lambda *a, **k: None)  # worker thread

    def fake_watch(stop, on_update, **kw):
        threading.Thread(target=lambda: (time.sleep(0.05), on_update("commit:bbb")), daemon=True).start()

    executed: list[list[str]] = []

    def fake_exec(cmd, log=print):
        executed.append(cmd)
        raise _Execed()  # the real exec never returns

    monkeypatch.setattr(update_restart, "watch_for_update", fake_watch)
    monkeypatch.setattr(update_restart, "exec_replacement", fake_exec)

    assert _daemon_thread(lambda: daemon.run_dashboard_daemon(repo)) == "execed"
    # Cleanup ran first (socket closed, handshake cleared), THEN the exec — with the
    # bound port pinned so the user's open URL survives the swap.
    assert fakes[0].shutdown_called and fakes[0].closed
    assert daemon.read_handshake(repo) is None
    assert len(executed) == 1
    assert "--dashboard-port" in executed[0]


def test_dashboard_daemon_retries_after_a_failed_restart(tmp_path, monkeypatch):
    # A failed exec must NOT strand a dead daemon: it binds and serves again on the
    # current code — handshake re-published, --daemons registry re-entered — and
    # retries on the next detection, until the exec succeeds or the user stops it.
    from agitrack import daemons as registry
    from agitrack.metrics import daemon

    repo = _repo(tmp_path)
    fakes: list[_FakeServer] = []

    def make_server(r, **kw):
        fakes.append(_FakeServer())
        return fakes[-1]

    monkeypatch.setattr(daemon, "build_server", make_server)
    monkeypatch.setattr(daemon.signal, "signal", lambda *a, **k: None)

    registered: list[str] = []
    monkeypatch.setattr(registry, "register", lambda kind, path, **kw: registered.append(kind))

    def fake_watch(stop, on_update, **kw):
        threading.Thread(target=lambda: (time.sleep(0.05), on_update("commit:bbb")), daemon=True).start()

    executed: list[list[str]] = []

    def fake_exec(cmd, log=print):
        executed.append(cmd)
        if len(executed) == 1:
            return  # first attempt FAILS (exec_replacement returns on failure)
        raise _Execed()  # second attempt succeeds

    monkeypatch.setattr(update_restart, "watch_for_update", fake_watch)
    monkeypatch.setattr(update_restart, "exec_replacement", fake_exec)

    assert _daemon_thread(lambda: daemon.run_dashboard_daemon(repo)) == "execed"
    assert len(executed) == 2  # failed once, retried
    assert len(fakes) == 2  # served a full second cycle between the attempts
    assert registered.count("dashboard") == 2  # visible in --daemons throughout


def test_dashboard_explicit_stop_wins_over_retry(tmp_path, monkeypatch):
    # After a failed restart the daemon keeps serving — and `agitrack -d stop`
    # (SIGTERM) must still end it cleanly instead of being eaten by the retry loop.
    from agitrack.metrics import daemon

    repo = _repo(tmp_path)
    fakes: list[_FakeServer] = []

    def make_server(r, **kw):
        fakes.append(_FakeServer())
        return fakes[-1]

    monkeypatch.setattr(daemon, "build_server", make_server)
    handlers: list = []
    monkeypatch.setattr(daemon.signal, "signal", lambda sig, fn: handlers.append(fn))

    armed: list[int] = []

    def fake_watch(stop, on_update, **kw):
        armed.append(1)
        if len(armed) == 1:  # only the FIRST cycle sees an update
            threading.Thread(target=lambda: (time.sleep(0.05), on_update("commit:bbb")), daemon=True).start()

    executed: list[list[str]] = []
    monkeypatch.setattr(update_restart, "watch_for_update", fake_watch)
    monkeypatch.setattr(update_restart, "exec_replacement", lambda cmd, log=print: executed.append(cmd))

    box: dict[str, object] = {}
    thread = threading.Thread(target=lambda: box.update(rc=daemon.run_dashboard_daemon(repo)))
    thread.start()
    for _ in range(200):  # wait for the post-failure second cycle to be serving
        if len(fakes) == 2 and len(armed) == 2:
            break
        time.sleep(0.02)
    assert handlers, "signal handlers installed"
    handlers[0]()  # the user's `agitrack -d stop` (SIGTERM)
    thread.join(timeout=10)

    assert not thread.is_alive() and box.get("rc") == 0
    assert len(executed) == 1  # the failed attempt; no further restart after the stop
    assert daemon.read_handshake(repo) is None  # clean shutdown


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
    # CI has no real backend CLI on PATH; run() must get past its install gate.
    monkeypatch.setattr("agitrack.backends.setup.backend_installed", lambda name: True)

    def fake_watch(stop, on_update, **kw):
        threading.Thread(target=lambda: (time.sleep(0.05), on_update("commit:bbb")), daemon=True).start()

    executed: list[list[str]] = []

    def fake_exec(cmd, log=print):
        executed.append(cmd)
        raise _Execed()

    monkeypatch.setattr(update_restart, "watch_for_update", fake_watch)
    monkeypatch.setattr(update_restart, "exec_replacement", fake_exec)

    assert _daemon_thread(runner.run) == "execed"
    assert len(executed) == 1  # tore down, then replaced itself with the new code


def test_background_retries_and_restores_tracking_after_a_failed_restart(tmp_path, monkeypatch):
    # A failed exec resumes tracking on the current code: handshake and hooks come
    # back (stop/status/--daemons still work), and the restart is retried.
    runner, repo, state, backend = _runner(tmp_path, manual=False)
    monkeypatch.setattr("agitrack.backends.setup.backend_installed", lambda name: True)
    setups: list[str] = []
    monkeypatch.setattr(runner._manual, "setup", lambda: setups.append("manual"))
    monkeypatch.setattr(runner, "_install_autotrack_hook", lambda: setups.append("hook"))
    real_write = runner._write_handshake
    monkeypatch.setattr(runner, "_write_handshake", lambda: setups.append("handshake") or real_write())

    def fake_watch(stop, on_update, **kw):
        threading.Thread(target=lambda: (time.sleep(0.05), on_update("commit:bbb")), daemon=True).start()

    executed: list[list[str]] = []

    def fake_exec(cmd, log=print):
        executed.append(cmd)
        if len(executed) == 1:
            return  # first attempt fails
        raise _Execed()

    monkeypatch.setattr(update_restart, "watch_for_update", fake_watch)
    monkeypatch.setattr(update_restart, "exec_replacement", fake_exec)

    assert _daemon_thread(runner.run) == "execed"
    assert len(executed) == 2  # failed, resumed, retried
    # Between the attempts the tracker restored its visibility and hooks
    # (initial setup + the post-failure re-setup).
    assert setups.count("handshake") >= 2
    assert setups.count("manual") >= 2 and setups.count("hook") >= 2
