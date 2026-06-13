"""aGiT: agent + git."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the installed distribution's metadata, built from
    # the `version` in pyproject.toml. Reading it here (rather than hardcoding a
    # second copy) guarantees the version stamped into commit metadata is exactly
    # the aGiT the user has installed.
    __version__ = version("agit-ai")
except PackageNotFoundError:
    # A source tree that was never installed has no distribution metadata.
    # 0.0.0 marks an unreleased working tree (matches scripts/publish.sh).
    __version__ = "0.0.0"
