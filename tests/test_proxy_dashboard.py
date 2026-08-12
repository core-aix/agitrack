from agitrack.metrics.server import browser_is_local, open_dashboard_in_browser
from agitrack.proxy.runner import ProxyInput
from tests.proxy_helpers import make_runner


def test_dashboard_is_in_the_ctrl_g_command_palette():
    assert "dashboard" in ProxyInput.COMMANDS


def _repo():
    """A stand-in repo object: the dashboard handler only needs its path."""
    return type("R", (), {"repo": "/tmp/proj"})()


def _hub(monkeypatch, *, url, view="active", running_before=False, record=None):
    """Stand in for the dashboard hub: what it would return, without a socket or a daemon."""
    calls: dict = {"ensure": [], "opened": []}
    state = {"running": running_before}
    monkeypatch.setattr(
        "agitrack.metrics.hub.ensure_hub_for",
        lambda directory, **kw: (state.__setitem__("running", True), calls["ensure"].append(directory), (url, view))[
            -1
        ],
    )
    monkeypatch.setattr(
        "agitrack.metrics.hub.running_hub",
        lambda: (record or {"pid": 4242, "url": "http://127.0.0.1:8765/", "port": 8765}) if state["running"] else None,
    )
    monkeypatch.setattr("agitrack.metrics.open_dashboard_in_browser", lambda u: calls["opened"].append(u) or True)
    return calls


def test_dashboard_command_opens_this_repo_on_the_hub(monkeypatch):
    # One hub serves every repository, so the Ctrl-G dashboard no longer spawns anything of its
    # own: it asks the hub for this repo's URL and opens it.
    calls = _hub(monkeypatch, url="http://127.0.0.1:8765/r/proj-abc123/")

    runner = make_runner(base_repo=_repo())
    monkeypatch.setattr(runner, "_render", lambda: None)
    popups: list[tuple] = []
    monkeypatch.setattr(runner, "_select_popup", lambda title, options, **kw: popups.append((title, kw)) or "ok")

    runner._handle_dashboard_command()

    assert runner._dashboard_url == "http://127.0.0.1:8765/r/proj-abc123/"
    assert calls["opened"] == ["http://127.0.0.1:8765/r/proj-abc123/"]
    # A popup tells the user the daemon outlives aGiTrack, how to stop it, and that one
    # dashboard covers every repository.
    detail = " ".join(popups[0][1]["detail"])
    assert "KEEPS RUNNING" in detail
    assert "agitrack -d stop" in detail
    assert "every repository" in detail

    # A second invocation just reopens it: the hub was already running, so no popup this time.
    runner._handle_dashboard_command()
    assert calls["opened"] == ["http://127.0.0.1:8765/r/proj-abc123/"] * 2
    assert len(popups) == 1  # the persistence popup shows only on a fresh start


def test_dashboard_command_opens_the_backtrace_view_when_nothing_is_tracked(monkeypatch):
    # Which view opens is the hub's decision. A repo with nothing tracked yet gets the
    # reconstruction, and the popup explains why this is not the dashboard they asked for.
    calls = _hub(monkeypatch, url="http://127.0.0.1:8765/b/proj-abc123/", view="backtrace")

    runner = make_runner(base_repo=_repo())
    monkeypatch.setattr(runner, "_render", lambda: None)
    popups: list[tuple] = []
    monkeypatch.setattr(runner, "_select_popup", lambda title, options, **kw: popups.append((title, kw)) or "ok")

    runner._handle_dashboard_command()

    assert calls["opened"] == ["http://127.0.0.1:8765/b/proj-abc123/"]
    assert popups and popups[0][0].startswith("Backtrace view live at")
    assert "no recorded AI work yet" in " ".join(popups[0][1]["detail"])


def test_dashboard_command_reports_when_the_hub_fails_to_start(monkeypatch):
    monkeypatch.setattr("agitrack.metrics.hub.ensure_hub_for", lambda directory, **kw: ("", "active"))
    monkeypatch.setattr("agitrack.metrics.hub.running_hub", lambda: None)
    monkeypatch.setattr("agitrack.metrics.hub.log_path", lambda: "/tmp/dashboard.log")

    runner = make_runner(base_repo=_repo())
    monkeypatch.setattr(runner, "_render", lambda: None)
    messages: list[str] = []
    monkeypatch.setattr(runner, "_set_message", lambda text: messages.append(text))

    runner._handle_dashboard_command()

    # The user is pointed at the log rather than left with a dashboard that silently did not open.
    assert messages and "/tmp/dashboard.log" in messages[0]


def test_dashboard_is_never_stopped_by_agitrack_exit():
    # The Ctrl-G dashboard is terminal-owned: aGiTrack's teardown must not kill it
    # (the old _stop_dashboard exit hook is gone entirely).
    runner = make_runner(base_repo=object())
    assert not hasattr(runner, "_stop_dashboard")


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
