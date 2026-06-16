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


try:
    # Single source of truth: the installed distribution's metadata, built from
    # the `version` in pyproject.toml. Reading it here (rather than hardcoding a
    # second copy) guarantees the version stamped into commit metadata is exactly
    # the aGiTrack the user has installed.
    __version__ = version("agitrack")
except PackageNotFoundError:
    # No distribution metadata: fall back to the source checkout's pyproject.toml,
    # and only then to 0.0.0 (an unreleased working tree; matches scripts/publish.sh).
    __version__ = _source_version() or "0.0.0"
