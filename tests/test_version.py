"""Version-resolution tests.

The version stamped into commit metadata (``agitrack_version:``) must be the version of
the aGiTrack *actually running*. Reading installed distribution metadata alone stamped a
stale version whenever an older aGiTrack was also installed in the environment (real
commits recorded ``0.0.4`` while ``pyproject.toml`` said ``0.0.6``). A source checkout is
authoritative for itself, so its ``pyproject.toml`` is preferred over installed metadata.
"""

from __future__ import annotations

import agitrack
from agitrack.update import selfupdate


def test_source_version_matches_pyproject():
    # The running source tree's version is parsed from its own pyproject.toml.
    source = agitrack._source_version()
    assert source is not None and source.count(".") >= 2  # e.g. "0.0.6"
    # The message matters more than the assertion. `__version__` is read once at import while
    # `source` is re-read now, so the ONLY way these differ is that pyproject.toml changed
    # underneath the running suite — which in CI means a self-update merged the release commit
    # into the job's own checkout. Bare, this failed as `assert '0.6.19' == '0.6.20'`, which
    # reads like a packaging mistake in the commit under test and sent three separate
    # investigations looking in the wrong place. Say what actually happened instead.
    assert agitrack.__version__ == source, (  # what gets stamped into commits
        f"agitrack.__version__ is {agitrack.__version__!r} but pyproject.toml now reads "
        f"{source!r}. Nothing in a commit can cause that: the file changed while the suite was "
        "running, so a self-update escaped the guards and merged this checkout mid-run. Check "
        f"that {selfupdate.NO_SELF_UPDATE_ENV} is set for the whole run (conftest's "
        "_never_really_self_update) and that self_update_enabled() still honours it."
    )


def test_resolve_version_prefers_source_over_installed(monkeypatch):
    # A source checkout (pyproject present) wins over installed metadata, so a stale or
    # mismatched installed dist can't stamp the wrong version (the 0.0.4-vs-0.0.6 bug).
    monkeypatch.setattr(agitrack, "_source_version", lambda: "9.9.9")
    monkeypatch.setattr(agitrack, "_installed_version", lambda: "0.0.4")
    assert agitrack._resolve_version() == "9.9.9"


def test_resolve_version_falls_back_to_installed_metadata(monkeypatch):
    # An installed wheel has no pyproject beside the package, so it uses its own metadata.
    monkeypatch.setattr(agitrack, "_source_version", lambda: None)
    monkeypatch.setattr(agitrack, "_installed_version", lambda: "1.2.3")
    assert agitrack._resolve_version() == "1.2.3"


def test_resolve_version_falls_back_to_placeholder(monkeypatch):
    # Neither a source tree nor installed metadata: the unreleased-tree placeholder.
    monkeypatch.setattr(agitrack, "_source_version", lambda: None)
    monkeypatch.setattr(agitrack, "_installed_version", lambda: None)
    assert agitrack._resolve_version() == "0.0.0"


def test_every_version_indicator_says_the_same_thing(monkeypatch, tmp_path):
    """One install must not answer "which version is this?" differently depending on where it is
    asked. It did: `agitrack --version` named the commit on a source checkout while the dashboard
    served the bare release version, and the dashboard's copy is not cosmetic — the page compares
    it across polls to notice that the daemon serving it has been replaced, so between two
    releases (every commit reporting the same version) it could not notice at all."""
    from agitrack import versioning
    from agitrack.cli import _version_line
    from agitrack.metrics.web import _agitrack_version
    from agitrack.proxy.crash import _version

    class _Source:
        def __init__(self, root):
            self.repo = root

        def short_sha(self, ref="HEAD"):
            return "feedface"

        def status_short(self):
            return ""

    monkeypatch.setattr("agitrack.update.updater.detect_source_repo", lambda: tmp_path)
    monkeypatch.setattr("agitrack.git.GitRepo", _Source)
    versioning.source_suffix.cache_clear()

    expected = versioning.version_line()

    assert "(source feedface)" in expected
    assert _version_line() == expected  # agitrack --version
    assert _agitrack_version() == expected  # the dashboard page and its /state poll
    assert _version() == expected  # a crash report


def test_the_suite_can_never_self_update_the_checkout_it_runs_from():
    """The guard behind ``test_source_version_matches_pyproject``.

    A test that starts a real background tracker starts a real update watcher with it, and on a
    source checkout a self-update MERGES the checkout aGiTrack is running from — in CI, the job's
    own working tree. That merge pulled the release commit in mid-run, so ``pyproject.toml``
    advanced while the already-imported ``agitrack.__version__`` stayed put, and the test above
    failed with the two exactly one release apart (0.6.12/0.6.13, 0.6.13/0.6.14, 0.6.14/0.6.15 on
    three consecutive Windows runs, then 0.6.19/0.6.20 on macOS).

    The macOS recurrence is the reason this asserts what it does. Two earlier guards were the
    wrong shape: monkeypatching ``attempt_self_update`` cannot reach the SUBPROCESS doing the
    merging, and ``check_for_updates: false`` in the isolated config stops only the tracker's own
    periodic check — the restart watcher's ``self_update=True`` reaches the updater by another
    route that never reads that key, and any config key is lost anyway the moment a test repoints
    ``AGITRACK_CONFIG_DIR``. What survives both is the ENVIRONMENT, honoured at the one function
    every install path goes through. So that is what is asserted here: not a particular caller
    being polite, but the choke point saying no.
    """
    import os

    from agitrack.config.settings import GlobalConfig

    # Set for this process, and therefore inherited by every child a test can start.
    assert os.environ.get(selfupdate.NO_SELF_UPDATE_ENV) == "1"
    # …and actually honoured where it counts, whatever the config says.
    assert selfupdate.self_update_suppressed_by_env() is True
    assert selfupdate.self_update_enabled() is False
    # The config guard remains, to keep spawned trackers from polling for updates all suite.
    assert GlobalConfig().check_for_updates is False
