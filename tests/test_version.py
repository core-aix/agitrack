"""Version-resolution tests.

The version stamped into commit metadata (``agitrack_version:``) must be the version of
the aGiTrack *actually running*. Reading installed distribution metadata alone stamped a
stale version whenever an older aGiTrack was also installed in the environment (real
commits recorded ``0.0.4`` while ``pyproject.toml`` said ``0.0.6``). A source checkout is
authoritative for itself, so its ``pyproject.toml`` is preferred over installed metadata.
"""

from __future__ import annotations

import agitrack


def test_source_version_matches_pyproject():
    # The running source tree's version is parsed from its own pyproject.toml.
    source = agitrack._source_version()
    assert source is not None and source.count(".") >= 2  # e.g. "0.0.6"
    assert agitrack.__version__ == source  # what gets stamped into commits


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
    three consecutive Windows runs). Monkeypatching ``attempt_self_update`` cannot stop it: the
    tracker doing the merging is a SUBPROCESS. What reaches a subprocess is the config dir, which
    is inherited through ``AGITRACK_CONFIG_DIR`` — so it is the config that must say no.
    """
    from agitrack.config.settings import GlobalConfig

    assert GlobalConfig().check_for_updates is False
