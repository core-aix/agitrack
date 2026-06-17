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
