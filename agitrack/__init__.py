"""aGiTrack: agent + git tracking."""

import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _source_version() -> str | None:
    """The version declared in the source checkout's ``pyproject.toml``.

    A fallback for when distribution metadata is unavailable — e.g. a source tree
    that was never installed, or an editable install whose ``.dist-info`` went
    missing (a reinstall that failed mid-build leaves no metadata, so
    ``version("agitrack")`` then raises ``PackageNotFoundError``). pyproject.toml is
    the single source of truth for the version (see scripts/publish.sh); parse it
    with the same regex the publisher uses so a source checkout still stamps the
    real release version into commit metadata instead of the 0.0.0 placeholder.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'^version = "([^"]+)"', text, re.M)
    return match.group(1) if match else None


def _installed_version() -> str | None:
    """The version recorded in this distribution's installed metadata, or None when no
    metadata is present (a bare source tree that was never installed)."""
    try:
        return version("agitrack")
    except PackageNotFoundError:
        return None


def _resolve_version() -> str:
    """The version of the aGiTrack *actually running*, stamped into commit metadata.

    A source checkout (the package sitting next to a ``pyproject.toml``) is authoritative
    for itself: its ``pyproject.toml`` is the live single source of truth. Prefer it over
    installed distribution metadata, because the two can disagree and the metadata then
    stamps the WRONG version — e.g. an older aGiTrack also installed in the same
    environment, or an editable install whose ``.dist-info`` froze the version at install
    time while the source has since been bumped. Commits stamping a stale ``0.0.4`` while
    ``pyproject.toml`` said ``0.0.6`` were exactly this: ``version("agitrack")`` returned the
    old installed metadata instead of the running source.

    A pip-installed wheel has no ``pyproject.toml`` beside the package, so it falls through
    to its own distribution metadata — the right version for that install. Only with
    neither available does it fall back to ``0.0.0`` (matches scripts/publish.sh)."""
    return _source_version() or _installed_version() or "0.0.0"


__version__ = _resolve_version()
