"""Tests for agitrack/update/updater.py.

All subprocess and filesystem calls are mocked — no real git, pip, or network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from agitrack.update.updater import (
    KIND_PACKAGE,
    KIND_SOURCE,
    METHOD_HOMEBREW,
    Updater,
    _version_tuple,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode=returncode, stdout=stdout, stderr=stderr)


def _make_updater_package() -> Updater:
    """Return an Updater forced into package mode (no source repo)."""
    return Updater(source_repo=None)


def _make_updater_source(tmp_path: Path) -> Updater:
    """Return an Updater forced into source mode with a mock git HEAD."""
    with patch("agitrack.update.updater._git") as mock_git:
        mock_git.return_value = _completed(0, stdout="abc1234\n")
        updater = Updater(source_repo=tmp_path)
    return updater


# ---------------------------------------------------------------------------
# kind / source_repo properties
# ---------------------------------------------------------------------------


def test_kind_is_package_when_no_source():
    assert _make_updater_package().kind == KIND_PACKAGE


def test_kind_is_source_when_source_repo_provided(tmp_path):
    updater = _make_updater_source(tmp_path)
    assert updater.kind == KIND_SOURCE


def test_source_repo_returns_injected_path(tmp_path):
    updater = _make_updater_source(tmp_path)
    assert updater.source_repo == tmp_path


def test_source_repo_is_none_for_package():
    assert _make_updater_package().source_repo is None


# ---------------------------------------------------------------------------
# __init__: HEAD snapshot
# ---------------------------------------------------------------------------


def test_init_source_snapshots_running_rev(tmp_path):
    with patch("agitrack.update.updater._git") as mock_git:
        mock_git.return_value = _completed(0, stdout="deadbeef\n")
        updater = Updater(source_repo=tmp_path)
    assert updater._running_rev == "deadbeef"


def test_init_source_head_fails_running_rev_none(tmp_path):
    with patch("agitrack.update.updater._git") as mock_git:
        mock_git.return_value = _completed(1)
        updater = Updater(source_repo=tmp_path)
    assert updater._running_rev is None


# ---------------------------------------------------------------------------
# check() dispatch
# ---------------------------------------------------------------------------


def test_check_dispatches_to_check_package_when_no_source():
    updater = _make_updater_package()
    with patch.object(updater, "_check_package", return_value=MagicMock()) as mock_check:
        updater.check()
        mock_check.assert_called_once()


# ---------------------------------------------------------------------------
# _has_module_pip
# ---------------------------------------------------------------------------


def test_has_module_pip_returns_true_on_returncode_zero():
    updater = _make_updater_package()
    with patch("agitrack.update.updater.subprocess.run") as mock_run:
        mock_run.return_value = _completed(0)
        assert updater._has_module_pip("python") is True


def test_has_module_pip_returns_false_on_nonzero():
    updater = _make_updater_package()
    with patch("agitrack.update.updater.subprocess.run") as mock_run:
        mock_run.return_value = _completed(1)
        assert updater._has_module_pip("python") is False


def test_has_module_pip_oserror_returns_false():
    updater = _make_updater_package()
    with patch("agitrack.update.updater.subprocess.run", side_effect=OSError):
        assert updater._has_module_pip("python") is False


def test_has_module_pip_subprocess_error_returns_false():
    updater = _make_updater_package()
    with patch(
        "agitrack.update.updater.subprocess.run",
        side_effect=subprocess.SubprocessError,
    ):
        assert updater._has_module_pip("python") is False


# ---------------------------------------------------------------------------
# _pip_invocation
# ---------------------------------------------------------------------------


def test_pip_invocation_uses_current_python_when_pip_available():
    import sys
    updater = _make_updater_package()
    with patch.object(updater, "_has_module_pip", return_value=True):
        result = updater._pip_invocation()
    assert result == [sys.executable, "-m", "pip"]


def test_pip_invocation_falls_back_to_pip3():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_has_module_pip", return_value=False),
        patch("agitrack.update.updater.shutil.which", side_effect=lambda n: "/usr/bin/pip3" if n == "pip3" else None),
    ):
        result = updater._pip_invocation()
    assert result == ["/usr/bin/pip3"]


def test_pip_invocation_falls_back_to_pip_when_no_pip3():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_has_module_pip", return_value=False),
        patch(
            "agitrack.update.updater.shutil.which",
            side_effect=lambda n: "/usr/bin/pip" if n == "pip" else None,
        ),
    ):
        result = updater._pip_invocation()
    assert result == ["/usr/bin/pip"]


def test_pip_invocation_returns_none_when_nothing_found():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_has_module_pip", return_value=False),
        patch("agitrack.update.updater.shutil.which", return_value=None),
    ):
        assert updater._pip_invocation() is None


# ---------------------------------------------------------------------------
# _installed_version
# ---------------------------------------------------------------------------


def test_installed_version_returns_string():
    # The function either returns from importlib.metadata or the __version__ fallback;
    # either way it must return a non-empty string without raising.
    updater = _make_updater_package()
    result = updater._installed_version()
    assert isinstance(result, str) and result


def test_installed_version_fallback_on_metadata_error():
    updater = _make_updater_package()
    with patch("importlib.metadata.version", side_effect=Exception("not found")):
        result = updater._installed_version()
    # Must return something string-like without raising
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _latest_package_version
# ---------------------------------------------------------------------------


def test_latest_package_version_parses_latest_line():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_pip_invocation", return_value=["pip"]),
        patch("agitrack.update.updater.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed(0, stdout="LATEST: 1.5.0\nagitrack (1.4.0)\n")
        result = updater._latest_package_version()
    assert result == "1.5.0"


def test_latest_package_version_parses_parenthetical_line():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_pip_invocation", return_value=["pip"]),
        patch("agitrack.update.updater.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed(0, stdout="agitrack (2.0.1)\n")
        result = updater._latest_package_version()
    assert result == "2.0.1"


def test_latest_package_version_nonzero_returncode_returns_none():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_pip_invocation", return_value=["pip"]),
        patch("agitrack.update.updater.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed(1)
        assert updater._latest_package_version() is None


def test_latest_package_version_timeout_returns_none():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_pip_invocation", return_value=["pip"]),
        patch(
            "agitrack.update.updater.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["pip"], 5),
        ),
    ):
        assert updater._latest_package_version() is None


def test_latest_package_version_no_pip_returns_none():
    updater = _make_updater_package()
    with patch.object(updater, "_pip_invocation", return_value=None):
        assert updater._latest_package_version() is None


def test_latest_package_version_unrecognised_output_returns_none():
    updater = _make_updater_package()
    with (
        patch.object(updater, "_pip_invocation", return_value=["pip"]),
        patch("agitrack.update.updater.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed(0, stdout="nothing useful here\n")
        assert updater._latest_package_version() is None


# ---------------------------------------------------------------------------
# _apply_package: pip failure (non-PEP 668)
# ---------------------------------------------------------------------------


def test_apply_package_pip_non_pep668_failure_sets_error():
    updater = _make_updater_package()
    pip_result = _completed(1, stdout="", stderr="some unrelated error\nfailed to upgrade")
    with (
        patch.object(updater, "_pip_invocation", return_value=["pip"]),
        patch("agitrack.update.updater.subprocess.run", return_value=pip_result),
    ):
        status = updater._apply_package()
    assert status.error is not None
    assert "upgrade" in status.error or "error" in status.error.lower() or status.error


# ---------------------------------------------------------------------------
# _apply_package: brew upgrade path
# ---------------------------------------------------------------------------


def test_apply_package_brew_upgrade_called_on_homebrew_install():
    updater = _make_updater_package()
    # Simulate pip refusing with PEP 668, then brew succeeding.
    pip_result = _completed(1, stderr="externally-managed-environment error")
    brew_result = _completed(0)
    call_results = iter([pip_result, brew_result])
    with (
        patch.object(updater, "_pip_invocation", return_value=["pip"]),
        patch.object(updater, "_install_method", return_value=METHOD_HOMEBREW),
        patch("agitrack.update.updater.shutil.which", return_value="/usr/local/bin/brew"),
        patch("agitrack.update.updater.subprocess.run", side_effect=lambda *a, **k: next(call_results)),
        patch.object(updater, "_installed_version", return_value="1.9.0"),
    ):
        status = updater._apply_package()
    assert status.error is None
    assert status.current == "1.9.0"


# ---------------------------------------------------------------------------
# _version_tuple helper
# ---------------------------------------------------------------------------


def test_version_tuple_basic():
    assert _version_tuple("1.2.3") == (1, 2, 3)


def test_version_tuple_single():
    assert _version_tuple("5") == (5,)


def test_version_tuple_with_suffix():
    # Non-numeric suffix is truncated at the first non-digit character
    t = _version_tuple("1.2.3rc1")
    assert t[0] == 1
    assert t[1] == 2


def test_version_tuple_empty_string_returns_zero():
    # "".split(".") is [""], so the single empty chunk maps to 0.
    assert _version_tuple("") == (0,)
