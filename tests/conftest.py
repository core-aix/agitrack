"""Shared test fixtures for the agit test suite (#29, P7).

The ``make_runner`` factory lives in ``tests/proxy_helpers.py`` (importable
as ``proxy_helpers`` on the pytest sys.path); this file also re-exports it
for convenience.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Put the REPO ROOT on sys.path so `from tests.test_dashboard import ...` resolves.
# pytest only adds `tests/` itself (the first dir without an __init__.py), so several modules
# that share fixtures via `tests.<module>` imports were relying on the repo root arriving by
# accident — through whichever editable-install layout the environment happened to use. That
# is not a property of this repo, so reinstalling the package (a plain `uv sync`) silently
# broke collection of five test modules. Anchoring it here makes the suite independent of how
# aGiTrack is installed.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from proxy_helpers import make_runner as _make_runner  # noqa: E402,F401 – re-exported below


@pytest.fixture(scope="session", autouse=True)
def _keep_console_windows_off_the_desktop():
    """Windows: stop the SUITE's own subprocesses from flashing console windows.

    These tests are real-git by policy, so a full run spawns git a few thousand times. Under a
    console-less parent — a CI runner, or any harness that pipes the output — Windows gives
    every one of those children a console window of its own, and running the tests carpet-bombs
    the desktop with windows that appear and vanish. Product code avoids this with
    ``proc.console_isolation_kwargs()``; 200-odd direct spawns across the suite's own helpers
    cannot each be relied on to remember, so it is patched in once, here, at the one place they
    all pass through.

    This does NOT paper over a missing flag in PRODUCT code: that is enforced separately and
    statically by ``tests/test_console_isolation.py``, which reads the source rather than the
    runtime, so a product spawn that forgets still fails the suite.
    """
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name != "nt" or not no_window:
        yield
        return
    original_init = subprocess.Popen.__init__

    def _init(self, *args, **kwargs):
        # Only when the caller expressed no preference: a spawn that sets creationflags or
        # startupinfo (detach_kwargs, the console-sharing self-update re-exec) means it.
        if not kwargs.get("creationflags") and not kwargs.get("startupinfo"):
            kwargs["creationflags"] = no_window
        return original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _init
    try:
        yield
    finally:
        subprocess.Popen.__init__ = original_init


@pytest.fixture(autouse=True)
def _isolate_global_config(tmp_path_factory, monkeypatch):
    """Point aGiTrack's global config at an isolated empty dir for every test.

    Otherwise tests read the developer's real ``~/.agitrack/config.json`` — whose
    ``default_backend`` masks tests that should fail without one — so the suite
    passed locally but broke in CI (empty config, no default backend). Isolating it
    makes the suite behave the same on every machine and in CI. Tests that need a
    specific config still set ``AGITRACK_CONFIG_DIR`` themselves (that wins, as it
    runs after this fixture)."""
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path_factory.mktemp("agitrack-config")))


@pytest.fixture(autouse=True)
def _never_touch_real_daemons(monkeypatch):
    """No test may see (or signal!) the developer's real daemons.

    ``daemons.list_running`` combines the registry (isolated via AGITRACK_CONFIG_DIR
    above) with an OS PROCESS-TABLE SCAN — so a test that reaches ``restart_all()``
    (e.g. through ``restart_agitrack``) would SIGTERM the developer's live dashboards
    on every run, exactly as it silently did before this guard. Tests of the scan
    itself bind the real function at import time (see test_daemons.py)."""
    from agitrack import daemons

    monkeypatch.setattr(daemons, "_scan_daemon_processes", lambda: [])


@pytest.fixture(autouse=True)
def _never_really_self_update(monkeypatch):
    """No test may install an aGiTrack update for real.

    ``attempt_self_update`` is a genuine side effect: on a source install it fetches and
    MERGES the checkout aGiTrack is running from. A test that started the daemon's watcher
    thread did exactly that in CI — the merge pulled the release commit into the job's own
    checkout mid-run, so ``pyproject.toml`` said 0.5.18 while the already-imported
    ``agitrack.__version__`` still said 0.5.17, and the version test failed. The watcher
    now opts in rather than defaulting on; this is the second line of defence, so no future
    caller can reach the real updater from a test. Tests of the self-updater itself stub
    ``Updater`` and call the module directly."""
    from agitrack.update import selfupdate

    monkeypatch.setattr(
        selfupdate, "attempt_self_update", lambda **kwargs: selfupdate.SelfUpdateRecord(), raising=False
    )


@pytest.fixture
def runner_factory():
    """Pytest fixture providing the make_runner factory."""
    return _make_runner


# Re-export so callers can do: from conftest import make_runner
make_runner = _make_runner
