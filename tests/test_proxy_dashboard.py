import threading
import time

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
    monkeypatch.setattr("agitrack.metrics.build_server", lambda repo: fake)
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    runner = make_runner(base_repo=object())
    monkeypatch.setattr(runner, "_render", lambda: None)

    runner._handle_dashboard_command()

    assert runner._dashboard_server is fake
    assert runner._dashboard_url == "http://127.0.0.1:12345/"
    assert opened == ["http://127.0.0.1:12345/"]
    assert fake.started.wait(timeout=2.0)  # the server thread actually started

    # A second invocation reuses the running server (no new one), just reopens it.
    monkeypatch.setattr("agitrack.metrics.build_server", lambda repo: _FakeServer())
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
