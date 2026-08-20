"""Daemons restart themselves after aGiTrack is updated on disk (agitrack/update/restart.py).

aGiTrack still never INSTALLS updates; but once pip/pipx/brew/git-pull has swapped the
code underneath, every daemon (background tracker, dashboard, backtrace) must replace
itself with the new version instead of silently running stale modules.
"""

import threading
import time
from pathlib import Path

import pytest

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


def test_a_view_daemon_holds_its_restart_until_the_trackers_have_taken_the_update():
    """The dashboard, hub and backtrace daemons restart LAST.

    All the daemons watch the same fingerprint, so one update wakes them all at once — and the
    view daemons, with the least to do on the way up, kept winning the race. The reader then got a
    page that reloaded to announce the new version and, right beside it, a warning that this
    repo's session was still on the old one and should be restarted "when convenient": a warning
    about a tracker that was already restarting itself, gone by the next poll.
    """
    waiting = iter([["/repo/a"], ["/repo/a"], []])
    notes: list[str] = []
    fired: list[str] = []
    stop = threading.Event()

    thread = update_restart.watch_for_update(
        stop,
        fired.append,
        interval=0.01,
        read_version=lambda: "2.2.2",
        defer_while=lambda: next(waiting, []),
        log=notes.append,
    )
    thread.join(timeout=5)

    assert fired == ["2.2.2"]  # once the trackers are current, and not before
    assert any("holding this restart" in note for note in notes)
    # The confirmed fingerprint is HELD across the wait, not thrown away: re-running the
    # two-sightings confirmation each round would push the restart back indefinitely on a busy
    # machine, which is the opposite of what the ordering is for.
    assert len(fired) == 1


def test_the_wait_for_the_trackers_is_bounded():
    # A tracker that never comes back — wedged, or stopped between the two readings — must not
    # leave the dashboard serving old code forever. It says so, and goes.
    notes: list[str] = []
    fired: list[str] = []
    stop = threading.Event()

    thread = update_restart.watch_for_update(
        stop,
        fired.append,
        interval=0.01,
        read_version=lambda: "2.2.2",
        defer_while=lambda: ["/repo/stuck"],
        defer_limit=0.0,
        log=notes.append,
    )
    thread.join(timeout=5)

    assert fired == ["2.2.2"]
    assert any("did not pick the update up in time" in note for note in notes)


def test_a_gate_that_raises_never_blocks_the_restart():
    # The ordering is a courtesy to the reader; it is never a reason a daemon cannot get onto
    # the new code.
    def _explode() -> list[str]:
        raise RuntimeError("registry unreadable")

    fired: list[str] = []
    stop = threading.Event()

    thread = update_restart.watch_for_update(
        stop, fired.append, interval=0.01, read_version=lambda: "2.2.2", defer_while=_explode
    )
    thread.join(timeout=5)

    assert fired == ["2.2.2"]


def test_only_background_trackers_are_waited_for(monkeypatch, tmp_path):
    """An interactive session holds the same repo lock and is deliberately never restarted, so
    waiting on one would keep the dashboard on old code for as long as the conversation lasts."""
    from agitrack import daemons
    from agitrack.update import selfupdate

    infos = [
        daemons.DaemonInfo(pid=1, kind="background", repo="/repo/stale"),
        daemons.DaemonInfo(pid=2, kind="background", repo="/repo/current"),
        daemons.DaemonInfo(pid=3, kind="session", repo="/repo/interactive"),
        daemons.DaemonInfo(pid=4, kind="dashboard", repo="/repo/stale"),
    ]
    fingerprints = {
        "/repo/stale": "version:1.0.0",
        "/repo/current": "version:2.0.0",
        "/repo/interactive": "version:1.0.0",
    }
    monkeypatch.setattr(daemons, "list_running", lambda **kwargs: infos)
    # Keyed through `as_posix`, not `str`: the gate looks each repo up as a Path, and on Windows
    # `str(Path("/repo/stale"))` is `\\repo\\stale` — so a `str`-keyed double answered "" for
    # every repo, the gate found nothing stale, and the test failed on Windows alone.
    monkeypatch.setattr(selfupdate, "instance_fingerprint", lambda root: fingerprints.get(Path(root).as_posix(), ""))
    monkeypatch.setattr(update_restart, "disk_fingerprint", lambda: "version:2.0.0")

    assert update_restart.stale_background_trackers() == ["/repo/stale"]


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
    # Restart = SPAWN-AND-VERIFY: the old daemon exits only once the replacement has
    # provably bound (handshake correlated on the child pid) — never a blind exec that
    # a broken update could crash out of.
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

    spawned: list[dict] = []
    child = type("Child", (), {"pid": 4243, "poll": lambda self: 0, "terminate": lambda self: None})()
    monkeypatch.setattr(update_restart, "watch_for_update", fake_watch)
    monkeypatch.setattr(daemon, "spawn_dashboard_daemon", lambda r, **kw: spawned.append(kw) or child)
    verified: list[int] = []
    monkeypatch.setattr(daemon, "wait_for_handshake", lambda r, *, pid, timeout: verified.append(pid) or {"pid": pid})

    assert _daemon_thread(lambda: daemon.run_dashboard_daemon(repo)) == 0
    # Cleanup ran (socket closed, handshake cleared), the replacement was spawned with
    # the bound port pinned, and its handshake was verified before the old daemon left.
    assert fakes[0].shutdown_called and fakes[0].closed
    assert daemon.read_handshake(repo) is None
    assert spawned == [{"port": 12345, "email_logins": None}]
    assert verified == [4243]


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

    spawned: list[dict] = []
    child = type("Child", (), {"pid": 4243, "poll": lambda self: None, "terminate": lambda self: None})()
    monkeypatch.setattr(update_restart, "watch_for_update", fake_watch)
    monkeypatch.setattr(daemon, "spawn_dashboard_daemon", lambda r, **kw: spawned.append(kw) or child)
    # First replacement never handshakes (it crashed on the broken update); second works.
    monkeypatch.setattr(
        daemon,
        "wait_for_handshake",
        lambda r, *, pid, timeout: {"pid": pid} if len(spawned) > 1 else None,
    )

    assert _daemon_thread(lambda: daemon.run_dashboard_daemon(repo)) == 0
    assert len(spawned) == 2  # failed verification once, retried
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

    spawned: list[dict] = []
    child = type("Child", (), {"pid": 4243, "poll": lambda self: None, "terminate": lambda self: None})()
    monkeypatch.setattr(update_restart, "watch_for_update", fake_watch)
    monkeypatch.setattr(daemon, "spawn_dashboard_daemon", lambda r, **kw: spawned.append(kw) or child)
    monkeypatch.setattr(daemon, "wait_for_handshake", lambda r, *, pid, timeout: None)  # verification fails

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
    assert len(spawned) == 1  # the failed attempt; no further restart after the stop
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

    def fake_exec(cmd, log=print, **kwargs):  # **kwargs: the spawn-and-verify handoff passes verify=
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

    def fake_exec(cmd, log=print, **kwargs):  # **kwargs: the spawn-and-verify handoff passes verify=
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


# --- the Windows handoff: never exit until the successor provably took over -------------


class _FakeChild:
    """Stand-in for the spawned replacement's Popen handle."""

    def __init__(self, pid=4242, exits_with=None):
        self.pid = pid
        self._exits_with = exits_with
        self.returncode = None
        self.terminated = False

    def poll(self):
        self.returncode = self._exits_with
        return self._exits_with

    def terminate(self):
        self.terminated = True


def test_a_replacement_that_never_comes_up_leaves_the_old_daemon_running(monkeypatch):
    """The Windows restart was `Popen(...)` then `os._exit(0)` — fire-and-forget. `Popen`
    returns once the process is CREATED, which says nothing about whether it went on to track
    anything, so the documented contract ("returning means the restart failed, resume on the
    current code") could never hold there. A successor that died on the way up left the repo
    with NO tracker, exit code 0, and nothing in the log.

    Measured cause: the successor lost the race for the repo lock its dying predecessor still
    held, concluded a live tracker was already running, and exited 0."""
    monkeypatch.setattr(update_restart.os, "name", "nt")
    child = _FakeChild(exits_with=1)  # started, then died during startup
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: child)
    exited: list = []
    monkeypatch.setattr(update_restart.os, "_exit", lambda code: exited.append(code))
    lines: list[str] = []

    update_restart.exec_replacement(["agitrack"], log=lines.append, verify=lambda pid: False, verify_seconds=2.0)

    assert exited == [], "the old daemon must NOT exit when the replacement did not take over"
    assert any("exited during startup" in line for line in lines)
    assert any("keeping the current version running" in line for line in lines)


def test_a_replacement_that_takes_over_ends_the_old_daemon(monkeypatch):
    monkeypatch.setattr(update_restart.os, "name", "nt")
    child = _FakeChild(pid=777)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: child)
    exited: list = []

    def _exit(code):
        # The real os._exit NEVER returns; a double that does would let the function run on
        # into the give-up path and terminate a successor that had already taken over.
        exited.append(code)
        raise _Execed()

    monkeypatch.setattr(update_restart.os, "_exit", _exit)

    with pytest.raises(_Execed):
        update_restart.exec_replacement(
            ["agitrack"], log=lambda _: None, verify=lambda pid: pid == 777, verify_seconds=5.0
        )

    assert exited == [0]  # handed over, so this process is done
    assert not child.terminated


def test_a_replacement_that_hangs_is_abandoned_not_left_racing(monkeypatch):
    """A successor still alive at the deadline is terminated: it must not come up LATER and
    fight the daemon that is about to resume tracking."""
    monkeypatch.setattr(update_restart.os, "name", "nt")
    child = _FakeChild()  # alive, but verify never succeeds
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: child)
    monkeypatch.setattr(update_restart.os, "_exit", lambda code: pytest.fail("must not exit"))
    lines: list[str] = []

    update_restart.exec_replacement(["agitrack"], log=lines.append, verify=lambda pid: False, verify_seconds=0.5)

    assert child.terminated
    assert any("did not come up within" in line for line in lines)


# --- POSIX gets the same handoff, for the same reason ------------------------------------------


def test_posix_does_not_exec_into_an_update_it_has_not_verified(monkeypatch):
    """POSIX used to ``os.execv`` here, and that is the one thing that cannot keep the promise
    this module makes. exec replaces the process image: a successor that dies on import leaves
    NOTHING to fall back to, so "a failed restart never strands a dead daemon" was true of the
    dashboard and false of the background tracker.

    Measured on a real pip install with a broken upgrade staged: the tracker logged "restarting
    on the new version", exec'd, the successor died on a SyntaxError, and the repository was left
    with no tracker at all — while the hub in the same experiment survived, because it verified.
    """
    monkeypatch.setattr(update_restart.os, "name", "posix")
    monkeypatch.setattr(
        update_restart.os, "execv", lambda *a: pytest.fail("os.execv cannot be undone by a failed successor")
    )
    child = _FakeChild(exits_with=1)  # the broken update: started, then died on the way up
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: child)
    monkeypatch.setattr(update_restart.os, "_exit", lambda code: pytest.fail("must not exit on a failed restart"))
    lines: list[str] = []

    update_restart.exec_replacement(["agitrack"], log=lines.append, verify=lambda pid: False, verify_seconds=2.0)

    # Returning IS the contract: the caller resumes on the code it already has.
    assert any("exited during startup" in line for line in lines)
    assert any("keeping the current version running" in line for line in lines)


def test_posix_hands_over_once_the_successor_proves_it_is_up(monkeypatch):
    monkeypatch.setattr(update_restart.os, "name", "posix")
    monkeypatch.setattr(update_restart.os, "execv", lambda *a: pytest.fail("no exec on this path"))
    child = _FakeChild(pid=909)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: child)
    exited: list = []

    def _exit(code):
        exited.append(code)
        raise _Execed()  # the real os._exit never returns

    monkeypatch.setattr(update_restart.os, "_exit", _exit)

    with pytest.raises(_Execed):
        update_restart.exec_replacement(
            ["agitrack"], log=lambda _: None, verify=lambda pid: pid == 909, verify_seconds=5.0
        )

    assert exited == [0]
    assert not child.terminated


def test_the_posix_successor_is_detached_so_it_outlives_us(monkeypatch):
    """We exit the moment the successor proves it is up. A child left in this process's session
    would go down with the terminal that started us, which is the same "repo left untracked"
    outcome by another route."""
    from agitrack.proc import detach_kwargs

    monkeypatch.setattr(update_restart.os, "name", "posix")
    seen: dict = {}

    def fake_popen(command, **kwargs):
        seen.update(kwargs)
        return _FakeChild(pid=5150)

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr(update_restart.os, "_exit", lambda code: (_ for _ in ()).throw(_Execed()))

    with pytest.raises(_Execed):
        update_restart.exec_replacement(["agitrack"], log=lambda _: None, verify=lambda pid: True)

    for key, value in detach_kwargs().items():
        assert seen.get(key) == value, f"the successor must be spawned detached ({key})"
