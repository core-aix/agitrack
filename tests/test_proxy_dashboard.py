import os

from agitrack.metrics.server import browser_is_local, open_dashboard_in_browser
from agitrack.proxy.runner import ProxyInput
from tests.proxy_helpers import make_runner


class _FakeProc:
    """Stand-in for the detached dashboard child's Popen handle."""

    def __init__(self, pid=4242):
        self.pid = pid
        self._alive = True
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True
        self._alive = False


def test_dashboard_is_in_the_ctrl_g_command_palette():
    assert "dashboard" in ProxyInput.COMMANDS


def test_dashboard_command_spawns_process_and_opens_browser(monkeypatch):
    proc = _FakeProc()
    spawned: list[dict] = []
    opened: list[str] = []
    monkeypatch.setattr("agitrack.metrics.running_handshake", lambda repo: None)  # none running yet
    monkeypatch.setattr("agitrack.metrics.clear_handshake", lambda repo: None)
    monkeypatch.setattr("agitrack.metrics.spawn_dashboard_daemon", lambda repo, **kw: spawned.append(kw) or proc)
    monkeypatch.setattr(
        "agitrack.metrics.wait_for_handshake",
        lambda repo, **kw: {"pid": proc.pid, "url": "http://127.0.0.1:12345/", "port": 12345},
    )
    # The handler routes the browser through open_dashboard_in_browser (which skips
    # opening on a remote/headless host); force "opened locally" for the test.
    monkeypatch.setattr("agitrack.metrics.open_dashboard_in_browser", lambda url: opened.append(url) or True)

    runner = make_runner(base_repo=object())
    monkeypatch.setattr(runner, "_render", lambda: None)
    monkeypatch.setattr(runner, "_dashboard_email_logins", lambda: {})

    runner._handle_dashboard_command()

    assert runner._dashboard_proc is proc
    assert runner._dashboard_url == "http://127.0.0.1:12345/"
    assert opened == ["http://127.0.0.1:12345/"]
    # The child is owned by THIS aGiTrack process, so it dies when the TUI exits.
    assert spawned and spawned[0]["owner_pid"] == os.getpid()

    # A second invocation reuses the running process (never respawns), just reopens it.
    monkeypatch.setattr(
        "agitrack.metrics.spawn_dashboard_daemon",
        lambda repo, **kw: (_ for _ in ()).throw(AssertionError("must not respawn a live daemon")),
    )
    runner._handle_dashboard_command()
    assert runner._dashboard_proc is proc  # unchanged
    assert opened == ["http://127.0.0.1:12345/", "http://127.0.0.1:12345/"]

    # Exit kills the dashboard process.
    monkeypatch.setattr("agitrack.metrics.clear_handshake", lambda repo: None)
    runner._stop_dashboard()
    assert proc.terminated
    assert runner._dashboard_proc is None


def test_dashboard_command_reports_when_daemon_fails_to_start(monkeypatch):
    proc = _FakeProc()
    monkeypatch.setattr("agitrack.metrics.running_handshake", lambda repo: None)  # none running yet
    monkeypatch.setattr("agitrack.metrics.clear_handshake", lambda repo: None)
    monkeypatch.setattr("agitrack.metrics.spawn_dashboard_daemon", lambda repo, **kw: proc)
    monkeypatch.setattr("agitrack.metrics.wait_for_handshake", lambda repo, **kw: None)  # never binds
    monkeypatch.setattr("agitrack.metrics.log_path", lambda repo: "/tmp/dashboard.log")

    runner = make_runner(base_repo=object())
    monkeypatch.setattr(runner, "_render", lambda: None)
    monkeypatch.setattr(runner, "_dashboard_email_logins", lambda: {})

    runner._handle_dashboard_command()

    assert runner._dashboard_proc is None  # not adopted
    assert proc.terminated  # the stillborn child was reaped, not orphaned


def test_dashboard_command_reuses_an_externally_running_daemon(monkeypatch):
    # A dashboard daemon already running for this repo (e.g. from `agitrack -d`, or a
    # prior session) is reused: the browser opens at its URL and no duplicate is spawned,
    # nor is its handshake cleared.
    opened: list[str] = []
    monkeypatch.setattr(
        "agitrack.metrics.running_handshake",
        lambda repo: {"pid": 777, "url": "http://127.0.0.1:9999/", "port": 9999},
    )
    monkeypatch.setattr(
        "agitrack.metrics.spawn_dashboard_daemon",
        lambda repo, **kw: (_ for _ in ()).throw(AssertionError("must not spawn when one is already running")),
    )
    monkeypatch.setattr(
        "agitrack.metrics.clear_handshake",
        lambda repo: (_ for _ in ()).throw(AssertionError("must not clear another daemon's handshake")),
    )
    monkeypatch.setattr("agitrack.metrics.open_dashboard_in_browser", lambda url: opened.append(url) or True)

    runner = make_runner(base_repo=object())
    monkeypatch.setattr(runner, "_render", lambda: None)

    runner._handle_dashboard_command()

    assert opened == ["http://127.0.0.1:9999/"]
    assert runner._dashboard_url == "http://127.0.0.1:9999/"
    # We don't own it, so we don't track it as our proc and won't stop it on exit.
    assert runner._dashboard_proc is None
    runner._stop_dashboard()  # must not raise, must not kill the reused daemon


def test_stop_dashboard_is_a_noop_when_none_running():
    runner = make_runner(base_repo=object())
    runner._stop_dashboard()  # must not raise
    assert runner._dashboard_proc is None


# --- browser routing: open locally, never on a remote/headless host -------------


def _clear_browser_env(monkeypatch):
    for var in ("BROWSER", "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(var, raising=False)


def test_browser_not_local_over_ssh(monkeypatch):
    _clear_browser_env(monkeypatch)
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    assert browser_is_local() is False


def test_browser_not_local_on_headless_linux(monkeypatch):
    _clear_browser_env(monkeypatch)
    monkeypatch.setattr("sys.platform", "linux")
    assert browser_is_local() is False


def test_browser_local_when_browser_env_set_even_over_ssh(monkeypatch):
    # An explicit $BROWSER (e.g. an editor's local-browser helper) is always honored.
    _clear_browser_env(monkeypatch)
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    monkeypatch.setenv("BROWSER", "open")
    assert browser_is_local() is True


def test_open_dashboard_in_browser_skips_when_not_local(monkeypatch):
    _clear_browser_env(monkeypatch)
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    called: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: called.append(url) or True)
    assert open_dashboard_in_browser("http://127.0.0.1:8765/") is False
    assert called == []  # never opened a browser on the remote host


# --- graceful exit on terminal close (SIGHUP/SIGTERM) ---------------------------


def test_exit_signal_finalizes_pending_work_then_exits(monkeypatch):
    runner = make_runner(base_repo=object())
    events: list[str] = []
    runner._finalize_pending_work = lambda: events.append("finalize")
    runner._disable_host_terminal_modes = lambda: events.append("disable-modes")
    runner._cleanup_child = lambda: events.append("cleanup")
    runner._restore_terminal = lambda: events.append("restore")

    import pytest

    with pytest.raises(SystemExit):
        runner._handle_exit_signal(1, None)  # SIGHUP

    # Pending work is finalized BEFORE teardown, so a just-finished turn isn't stranded.
    assert events[0] == "finalize"
    assert "cleanup" in events and "restore" in events
    assert runner.running is False
    assert runner.screen is None  # rendering suppressed during signal finalize


def test_exit_signal_still_exits_when_finalize_raises(monkeypatch):
    runner = make_runner(base_repo=object())

    def boom():
        raise RuntimeError("finalize failed")

    runner._finalize_pending_work = boom
    runner._disable_host_terminal_modes = lambda: None
    runner._cleanup_child = lambda: None
    runner._restore_terminal = lambda: None

    import pytest

    with pytest.raises(SystemExit):
        runner._handle_exit_signal(15, None)  # SIGTERM — a failing finalize can't block exit
