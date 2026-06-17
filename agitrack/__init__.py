"""aGiTrack: agent + git tracking."""

import os
import re
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _windows_unsupported_message() -> str:
    """Instructions shown when aGiTrack is launched on native Windows.

    aGiTrack drives the coding agent through a POSIX pseudo-terminal — it hard-imports
    ``pty``/``termios``/``fcntl`` at load with no fallbacks, so native Windows Python
    cannot even import the package (``agitrack --help`` would otherwise die with a bare
    ``ModuleNotFoundError: No module named 'termios'`` traceback). The supported Windows
    path is WSL2, which is just Linux to aGiTrack, so point the user there explicitly."""
    return (
        "aGiTrack does not run on native Windows (PowerShell, CMD, or Windows Terminal's\n"
        "default Windows shell).\n"
        "\n"
        "It drives the coding agent through a POSIX pseudo-terminal (pty/termios), which\n"
        "native Windows Python does not provide. Run it inside Windows Subsystem for Linux\n"
        "(WSL2) instead — that is the supported Windows path and behaves like Linux.\n"
        "\n"
        "Set it up:\n"
        "  1. In PowerShell (as Administrator):   wsl --install\n"
        "  2. Reboot if prompted, then open your Linux distro (e.g. Ubuntu) from the\n"
        "     Start menu or a new Windows Terminal tab.\n"
        "  3. Install aGiTrack inside that Linux shell:   pipx install agitrack\n"
        "     (or:  pip install agitrack)\n"
        "  4. Run aGiTrack from the WSL shell — not from PowerShell or CMD.\n"
        "\n"
        "More on WSL2: https://learn.microsoft.com/windows/wsl/install"
    )


def _require_supported_platform(os_name: str = os.name) -> None:
    """Refuse to run on native Windows with actionable instructions instead of a traceback.

    Called at package import so it fires before the POSIX-only submodules (``agitrack.cli``
    and its transitive ``pty``/``termios`` imports) are loaded — on native Windows that
    import chain raises ``ModuleNotFoundError`` first, burying the real problem. ``os.name``
    is ``"nt"`` only on native Windows; WSL2, Linux and macOS all report ``"posix"`` and
    pass through untouched."""
    if os_name == "nt":
        print(_windows_unsupported_message(), file=sys.stderr)
        raise SystemExit(1)


_require_supported_platform()


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
