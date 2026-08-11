"""`--verbose` has to be visible (C13).

It produced ZERO extra on-screen output — the verbose and non-verbose transcripts differed only
in the session name, and the status bar carried no marker. Everything went to
`.agitrack/proxy-debug-<stamp>.log`, whose path was never printed anywhere, so the flag read as
broken and the file it wrote was undiscoverable.
"""

from __future__ import annotations

from pathlib import Path

from agitrack.proxy.runner import ProxyRunner


def _runner(tmp_path, *, verbose: bool, raw: bool = False):
    from types import SimpleNamespace

    from agitrack.proxy.session import Session

    runner = ProxyRunner.__new__(ProxyRunner)
    runner.__dict__["_active_session"] = Session()  # `repo` is a per-session field
    runner.base_repo = SimpleNamespace(repo=Path(tmp_path))
    runner.repo = runner.base_repo
    runner.debug_proxy = verbose or raw
    runner.raw_capture = raw
    runner._diag_run = "20260811-120000"
    return runner


def test_verbose_prints_the_path_of_the_log_it_writes(tmp_path, capsys):
    _runner(tmp_path, verbose=True)._announce_diagnostics()

    out = capsys.readouterr().out
    assert "proxy-debug-20260811-120000.log" in out
    assert str(tmp_path) in out
    # The file records every keystroke and grows while idle — say so before it is left on.
    assert "keystrokes" in out


def test_raw_capture_names_its_own_file_too(tmp_path, capsys):
    _runner(tmp_path, verbose=True, raw=True)._announce_diagnostics()

    out = capsys.readouterr().out
    assert "proxy-raw-20260811-120000.log" in out


def test_without_verbose_nothing_is_printed(tmp_path, capsys):
    _runner(tmp_path, verbose=False)._announce_diagnostics()

    assert capsys.readouterr().out == ""
