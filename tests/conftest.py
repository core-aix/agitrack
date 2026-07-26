"""Shared test fixtures for the agit test suite (#29, P7).

The ``make_runner`` factory lives in ``tests/proxy_helpers.py`` (importable
as ``proxy_helpers`` on the pytest sys.path); this file also re-exports it
for convenience.
"""

from __future__ import annotations

import pytest

from proxy_helpers import make_runner as _make_runner  # noqa: F401 – re-exported below


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


@pytest.fixture
def runner_factory():
    """Pytest fixture providing the make_runner factory."""
    return _make_runner


# Re-export so callers can do: from conftest import make_runner
make_runner = _make_runner
