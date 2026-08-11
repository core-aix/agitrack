"""`agitrack --daemons stop`: stop every aGiTrack daemon, anywhere.

The escape hatch for "I have strays across several repos and cannot find them all". Deliberately
never force-kills and never stops the session it is run from.
"""

from __future__ import annotations

import pytest

from agitrack import daemons


class _Info:
    def __init__(self, pid: int, function: str = "repo dashboard", repo_name: str = "proj"):
        self.pid, self.function, self.repo_name = pid, function, repo_name
        self.repo, self.url, self.cmd = f"/tmp/{repo_name}", "", []


def _fake_registry(monkeypatch, infos, *, alive_after: set[int] = frozenset()):
    """Registry of *infos*; pids in *alive_after* ignore SIGTERM."""
    signalled: list[int] = []
    deregistered: list[int] = []
    monkeypatch.setattr(daemons, "list_running", lambda **kw: list(infos))
    monkeypatch.setattr(daemons, "_signal_targets", lambda infos: infos)
    monkeypatch.setattr(daemons, "terminate_pid", lambda pid: signalled.append(pid))
    monkeypatch.setattr(daemons, "pid_alive", lambda pid: pid in alive_after)
    monkeypatch.setattr(daemons, "deregister", lambda pid=None: deregistered.append(pid))
    return signalled, deregistered


def test_stop_all_stops_every_daemon(monkeypatch):
    signalled, deregistered = _fake_registry(monkeypatch, [_Info(11), _Info(22, "backtrace dashboard")])

    stopped, survivors = daemons.stop_all(exclude_pid=999)

    assert (stopped, survivors) == (2, [])
    assert signalled == [11, 22] and deregistered == [11, 22]


def test_stop_all_never_stops_the_session_it_runs_from(monkeypatch):
    """Running it inside a live aGiTrack session must not terminate that session."""
    signalled, _ = _fake_registry(monkeypatch, [_Info(11), _Info(4242, "background mode")])

    stopped, _survivors = daemons.stop_all(exclude_pid=4242)

    assert stopped == 1 and signalled == [11]  # the caller's own pid was skipped


def test_a_daemon_that_ignores_sigterm_is_reported_not_force_killed(monkeypatch):
    """A wedged dashboard is far less bad than one killed mid-write of its handshake, so a
    survivor is NAMED for the user rather than escalated to SIGKILL."""
    signalled, deregistered = _fake_registry(monkeypatch, [_Info(11), _Info(22)], alive_after={22})

    stopped, survivors = daemons.stop_all(exclude_pid=999)

    assert stopped == 1
    assert len(survivors) == 1 and "22" in survivors[0]
    assert 22 not in deregistered  # still running ⇒ still registered, so it stays findable


def test_stop_all_with_nothing_running(monkeypatch):
    _fake_registry(monkeypatch, [])
    assert daemons.stop_all(exclude_pid=999) == (0, [])


def _cli_registry(monkeypatch, infos, *, tty: bool, answer: str = ""):
    """CLI-level fake: registry contents, whether stdin is a terminal, and the typed answer."""
    monkeypatch.setattr("agitrack.daemons.list_running", lambda **kw: list(infos))
    monkeypatch.setattr("sys.stdin.isatty", lambda: tty, raising=False)

    def _input(prompt: str = "") -> str:
        print(prompt, end="")  # real input() echoes its prompt; keep it in the captured stream
        return answer

    monkeypatch.setattr("builtins.input", _input)


def test_cli_reports_what_it_stopped(monkeypatch, capsys):
    from agitrack import cli

    _cli_registry(monkeypatch, [_Info(11), _Info(22), _Info(33)], tty=False)
    monkeypatch.setattr("agitrack.daemons.stop_all", lambda **kw: (3, []))
    assert cli.main(["--daemons", "stop", "--yes"]) == 0
    assert "Stopped 3" in capsys.readouterr().out


def test_cli_exits_nonzero_when_a_daemon_survives(monkeypatch, capsys):
    """A partial result must not look like success — the user still has a stray to deal with."""
    from agitrack import cli

    _cli_registry(monkeypatch, [_Info(22)], tty=False)
    monkeypatch.setattr("agitrack.daemons.stop_all", lambda **kw: (1, ["repo dashboard for proj (pid 22)"]))
    assert cli.main(["--daemons", "stop", "--yes"]) == 1
    assert "Could not stop" in capsys.readouterr().out


def test_what_will_be_stopped_is_shown_BEFORE_anything_is_stopped(monkeypatch, capsys):
    """This reaches across every repository the user has. Someone typing it while thinking about
    one repo must see the other four listed while they can still say no."""
    from agitrack import cli

    _cli_registry(
        monkeypatch,
        [_Info(11, repo_name="thinking-of-this-one"), _Info(22, repo_name="but-also-this-one")],
        tty=True,
        answer="y",
    )
    monkeypatch.setattr("agitrack.daemons.stop_all", lambda **kw: (2, []))
    assert cli.main(["--daemons", "stop"]) == 0

    out = capsys.readouterr().out
    listing, _, aftermath = out.partition("Stop all of these?")
    assert "but-also-this-one" in listing  # named before the prompt, not after the kill
    assert "Stopped 2" in aftermath


def test_declining_the_confirmation_stops_nothing(monkeypatch, capsys):
    from agitrack import cli

    _cli_registry(monkeypatch, [_Info(11)], tty=True, answer="n")
    monkeypatch.setattr("agitrack.daemons.stop_all", lambda **kw: pytest.fail("declined ⇒ must not stop"))
    assert cli.main(["--daemons", "stop"]) == 0
    assert "Cancelled" in capsys.readouterr().out


def test_an_unanswered_confirmation_stops_nothing(monkeypatch):
    """Ctrl-C / EOF at the prompt is not consent."""
    from agitrack import cli

    _cli_registry(monkeypatch, [_Info(11)], tty=True)
    monkeypatch.setattr("builtins.input", lambda _p="": (_ for _ in ()).throw(KeyboardInterrupt))
    monkeypatch.setattr("agitrack.daemons.stop_all", lambda **kw: pytest.fail("unanswered ⇒ must not stop"))
    assert cli.main(["--daemons", "stop"]) == 0


def test_no_confirmation_is_asked_when_nothing_is_running(monkeypatch, capsys):
    from agitrack import cli

    _cli_registry(monkeypatch, [], tty=True)
    monkeypatch.setattr("builtins.input", lambda _p="": pytest.fail("nothing to confirm"))
    monkeypatch.setattr("agitrack.daemons.stop_all", lambda **kw: pytest.fail("nothing to stop"))
    assert cli.main(["--daemons", "stop"]) == 0
    assert "No aGiTrack daemons" in capsys.readouterr().out


def test_bare_daemons_flag_still_lists(monkeypatch, capsys):
    """`--daemons` on its own keeps its old read-only meaning — no surprise shutdowns."""
    from agitrack import cli

    monkeypatch.setattr("agitrack.daemons.list_running", lambda **kw: [])
    monkeypatch.setattr(
        "agitrack.daemons.stop_all", lambda **kw: pytest.fail("bare --daemons must never stop anything")
    )
    assert cli.main(["--daemons"]) == 0
    assert "No aGiTrack daemons" in capsys.readouterr().out


def test_the_listing_says_how_to_stop_them_all(monkeypatch, capsys):
    """Whoever is reading `--daemons` is usually there BECAUSE they have strays they cannot place,
    and the listing only ever explained how to stop them one at a time — each needing its own
    `--repo`. It now names the one command that ends all of them, with the count, and says that
    it asks first so nobody has to try it to find out."""
    from agitrack import cli

    monkeypatch.setattr("agitrack.daemons.list_running", lambda **kw: [_Info(11), _Info(22), _Info(33)])
    assert cli.main(["--daemons"]) == 0

    out = capsys.readouterr().out
    assert "agitrack --daemons stop" in out
    assert "all 3 of them" in out  # the count, so the reach is concrete
    assert "in every repository" in out  # ...and that it is not scoped to this project
    assert "asks first" in out


def test_the_stop_all_hint_is_not_shown_when_nothing_is_running(monkeypatch, capsys):
    from agitrack import cli

    monkeypatch.setattr("agitrack.daemons.list_running", lambda **kw: [])
    assert cli.main(["--daemons"]) == 0

    out = capsys.readouterr().out
    assert "No aGiTrack daemons" in out
    assert "--daemons stop" not in out  # nothing to stop, so no instructions for stopping it


def test_an_unattended_stop_all_refuses_instead_of_killing_everything(monkeypatch, capsys):
    """The confirmation used to be gated on `sys.stdin.isatty()` and simply SKIPPED without a
    terminal — so a script, a CI job, or an agent harness silently killed every daemon on the
    machine, including the developer's own live session, while `--daemons list` still advertised
    "(lists them and asks first)". Unattended destruction has to be asked for explicitly."""
    from agitrack import cli

    _cli_registry(monkeypatch, [_Info(11), _Info(22)], tty=False)
    monkeypatch.setattr("agitrack.daemons.stop_all", lambda **kw: pytest.fail("no tty, no --yes ⇒ must not stop"))

    assert cli.main(["--daemons", "stop"]) == 1

    out = capsys.readouterr().out
    assert "no terminal to confirm on" in out
    assert "--yes" in out
    assert "--repo" in out  # the scoped alternative, offered where the reach is being explained


def test_daemons_stop_can_be_scoped_to_one_repo(monkeypatch, capsys):
    """Stopping five daemons one `--repo` at a time was the only scoped option, and the confirmed
    blast radius of the unscoped form reached across other repositories mid-run."""
    from agitrack import cli

    seen: dict = {}
    monkeypatch.setattr("agitrack.daemons.list_running", lambda **kw: seen.setdefault("list", kw) and [] or [_Info(11)])
    monkeypatch.setattr("agitrack.daemons.stop_all", lambda **kw: (seen.update(stop=kw), (1, []))[1])
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)

    assert cli.main(["--repo", "/tmp/proj", "--daemons", "stop", "--yes"]) == 0

    assert seen["list"]["repo"].endswith("proj")
    assert seen["stop"]["repo"].endswith("proj")


def test_the_listing_offers_the_scoped_stop(monkeypatch, capsys):
    from agitrack import cli

    monkeypatch.setattr("agitrack.daemons.list_running", lambda **kw: [_Info(11), _Info(22)])
    assert cli.main(["--daemons"]) == 0

    out = capsys.readouterr().out
    assert "--repo <path> --daemons stop" in out


def test_a_reused_pid_is_never_signalled(monkeypatch):
    """`pid_alive()` is not identity. A registry entry survives a crash and, after a reboot, the
    OS reassigns that number to something else entirely — an editor, a browser, a build. Stop-all
    must require proof the process is actually an aGiTrack daemon before signalling it."""
    signalled: list[int] = []
    deregistered: list[int] = []
    monkeypatch.setattr(daemons, "list_running", lambda **kw: [_Info(11), _Info(22)])
    monkeypatch.setattr(daemons, "terminate_pid", lambda pid: signalled.append(pid))
    monkeypatch.setattr(daemons, "pid_alive", lambda pid: False)
    monkeypatch.setattr(daemons, "deregister", lambda pid=None: deregistered.append(pid))
    # 22's number now belongs to something that is not ours.
    monkeypatch.setattr(
        daemons, "_process_command_lines", lambda: ["11 python -m agitrack --dashboard-serve", "22 vim"]
    )

    stopped, survivors = daemons.stop_all(exclude_pid=999)

    assert signalled == [11]
    assert stopped == 1 and survivors == []
    assert 22 in deregistered  # the stale entry is reaped rather than left to mislead again


def test_the_process_scan_stands_down_under_an_isolated_config_dir(monkeypatch, tmp_path):
    """`AGITRACK_CONFIG_DIR` is how every caller asks for a private aGiTrack universe — the test
    suite, a CI job, one slot of a parallel live-test run. The registry honoured it; the process
    scan did not, so `--daemons stop` under isolation still found and killed every daemon on the
    machine. Isolation one half ignores is not isolation."""
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(
        daemons, "_process_command_lines", lambda: ["4242 python -m agitrack --dashboard-serve --repo /somebody/else"]
    )

    assert daemons._scan_daemon_processes() == []
    assert daemons.list_running() == []
