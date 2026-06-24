"""Cross-platform graceful-shutdown sentinel (#118).

The VS Code extension can't deliver a catchable signal to aGiTrack on native Windows, so it
writes ``<repo>/.agitrack/shutdown``; the reactor polls it and exits gracefully. Exercised on
POSIX here (the mechanism is platform-agnostic)."""

import time
import types

from tests.proxy_helpers import make_runner


def test_shutdown_sentinel_finalizes_and_stops(tmp_path):
    runner = make_runner(base_repo=types.SimpleNamespace(repo=tmp_path))
    runner.running = True
    runner._last_shutdown_check = 0.0
    finalized: list[bool] = []
    runner._finalize_pending_work = lambda: finalized.append(True)
    runner._debug = lambda *a, **k: None

    # No sentinel ⇒ no-op.
    runner._check_shutdown_request()
    assert runner.running is True and finalized == []

    # Sentinel present ⇒ finalize the turn, consume the file, and stop the loop.
    agit = tmp_path / ".agitrack"
    agit.mkdir()
    (agit / "shutdown").write_text("")
    runner._last_shutdown_check = 0.0  # bypass the poll rate-limit
    runner._check_shutdown_request()
    assert runner.running is False
    assert finalized == [True]
    assert not (agit / "shutdown").exists()  # consumed so it can't re-fire


def test_shutdown_check_is_rate_limited(tmp_path):
    runner = make_runner(base_repo=types.SimpleNamespace(repo=tmp_path))
    runner.running = True
    runner._finalize_pending_work = lambda: None
    runner._debug = lambda *a, **k: None
    agit = tmp_path / ".agitrack"
    agit.mkdir()
    (agit / "shutdown").write_text("")

    runner._last_shutdown_check = time.monotonic()  # checked just now
    runner._check_shutdown_request()
    assert runner.running is True  # within the rate-limit window: didn't act yet
    assert (agit / "shutdown").exists()
