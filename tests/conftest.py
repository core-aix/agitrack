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


@pytest.fixture
def runner_factory():
    """Pytest fixture providing the make_runner factory."""
    return _make_runner


# Re-export so callers can do: from conftest import make_runner
make_runner = _make_runner
