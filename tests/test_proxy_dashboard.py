import threading
import time

from agitrack.metrics.server import browser_is_local, open_dashboard_in_browser
from agitrack.proxy.runner import ProxyInput
from tests.proxy_helpers import make_runner


class _FakeServer:
    def __init__(self):
        self.server_address = ("127.0.0.1", 12345)
        self.shutdown_called = False
        self.closed = False
        self.started = threading.Event()

    def serve_forever(self):
        self.started.set()
        while not self.shutdown_called:
            time.sleep(0.005)

    def shutdown(self):
        self.shutdown_called = True

    def server_close(self):
        self.closed = True


def test_dashboard_is_in_the_ctrl_g_command_palette():
    assert "dashboard" in ProxyInput.COMMANDS


def test_dashboard_command_serves_and_opens_browser(monkeypatch):
    fake = _FakeServer()
    opened: list[str] = []
    monkeypatch.setattr("agitrack.metrics.build_server", lambda repo, **kw: fake)
    # The handler routes the browser through open_dashboard_in_browser (which skips
    # opening on a remote/headless host); force "opened locally" for the test.
    monkeypatch.setattr("agitrack.metrics.open_dashboard_in_browser", lambda url: opened.append(url) or True)

    runner = make_runner(base_repo=object())
    monkeypatch.setattr(runner, "_render", lambda: None)

    runner._handle_dashboard_command()

    assert runner._dashboard_server is fake
    assert runner._dashboard_url == "http://127.0.0.1:12345/"
    assert opened == ["http://127.0.0.1:12345/"]
    assert fake.started.wait(timeout=2.0)  # the server thread actually started

    # A second invocation reuses the running server (no new one), just reopens it.
    monkeypatch.setattr("agitrack.metrics.build_server", lambda repo, **kw: _FakeServer())
    runner._handle_dashboard_command()
    assert runner._dashboard_server is fake  # unchanged
    assert opened == ["http://127.0.0.1:12345/", "http://127.0.0.1:12345/"]

    # Exit shuts the dashboard down.
    runner._stop_dashboard()
    assert fake.shutdown_called and fake.closed
    assert runner._dashboard_server is None


def test_stop_dashboard_is_a_noop_when_none_running():
    runner = make_runner(base_repo=object())
    runner._stop_dashboard()  # must not raise
    assert runner._dashboard_server is None


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
