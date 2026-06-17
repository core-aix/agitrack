"""Native-Windows platform guard.

aGiTrack hard-imports POSIX-only modules (``pty``/``termios``/``fcntl``) at load with no
fallback, so on native Windows the package can't import — ``agitrack --help`` would die
with a bare ``ModuleNotFoundError: No module named 'termios'`` traceback. The guard in
``agitrack/__init__.py`` runs at package import (before that doomed chain) and replaces the
traceback with actionable WSL2 setup instructions. WSL2/Linux/macOS all report
``os.name == "posix"`` and must pass through untouched.
"""

from __future__ import annotations

import pytest

import agitrack


def test_native_windows_exits_with_instructions(capsys):
    # os.name == "nt" only on native Windows: refuse, but with a clean non-zero exit.
    with pytest.raises(SystemExit) as exc:
        agitrack._require_supported_platform(os_name="nt")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "does not run on native Windows" in err
    assert "WSL" in err  # points the user at the supported Windows path
    assert "wsl --install" in err  # and gives the concrete command


@pytest.mark.parametrize("os_name", ["posix"])
def test_posix_platforms_pass_through(os_name, capsys):
    # WSL2, Linux and macOS all report "posix" — the guard must be a no-op and print nothing.
    agitrack._require_supported_platform(os_name=os_name)
    assert capsys.readouterr().err == ""


def test_message_is_actionable():
    # The instructions must name both how to install and where to run it.
    message = agitrack._windows_unsupported_message()
    assert "pipx install agitrack" in message or "pip install agitrack" in message
    assert "from the WSL shell" in message
