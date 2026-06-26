"""Tests for agitrack/system_tools.py and the cli git/gh prerequisite helpers.

All package-manager and git calls are mocked — no real installs, no real git config.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import agitrack.cli as cli
import agitrack.system_tools as st


def _ok(stdout: str = ""):
    return subprocess.CompletedProcess([], returncode=0, stdout=stdout, stderr="")


# ---------------------------------------------------------------------------
# can_install_tool — picks the right manager per OS
# ---------------------------------------------------------------------------


def test_can_install_tool_windows_uses_winget(monkeypatch):
    monkeypatch.setattr(st.os, "name", "nt")
    assert st.can_install_tool("git", which=lambda e: r"C:\winget.exe" if e == "winget" else None) is True


def test_can_install_tool_macos_uses_brew(monkeypatch):
    monkeypatch.setattr(st.os, "name", "posix")
    monkeypatch.setattr(st.sys, "platform", "darwin")
    assert st.can_install_tool("gh", which=lambda e: "/opt/homebrew/bin/brew" if e == "brew" else None) is True


def test_can_install_tool_linux_uses_distro_manager(monkeypatch):
    monkeypatch.setattr(st.os, "name", "posix")
    monkeypatch.setattr(st.sys, "platform", "linux")
    assert st.can_install_tool("git", which=lambda e: "/usr/bin/apt-get" if e == "apt-get" else None) is True


def test_can_install_tool_false_without_a_manager(monkeypatch):
    monkeypatch.setattr(st.os, "name", "posix")
    monkeypatch.setattr(st.sys, "platform", "linux")
    assert st.can_install_tool("git", which=lambda e: None) is False


def test_can_install_tool_unknown_tool_is_false():
    assert st.can_install_tool("ripgrep", which=lambda e: "/usr/bin/anything") is False


# ---------------------------------------------------------------------------
# install_system_tool
# ---------------------------------------------------------------------------


def test_install_system_tool_windows_runs_winget(monkeypatch):
    monkeypatch.setattr(st.os, "name", "nt")
    ran = []

    def fake_which(exe):
        return rf"C:\{exe}.exe"  # winget present, and git resolves after install

    def fake_run(command, **kwargs):
        ran.append(command)
        return _ok()

    ok = st.install_system_tool("git", output_fn=lambda _: None, run=fake_run, which=fake_which)
    assert ok is True
    assert ran and "install" in ran[0] and "Git.Git" in ran[0]


def test_install_system_tool_linux_uses_sudo_apt(monkeypatch):
    monkeypatch.setattr(st.os, "name", "posix")
    monkeypatch.setattr(st.sys, "platform", "linux")
    ran = []

    def fake_which(exe):
        return f"/usr/bin/{exe}" if exe in {"apt-get", "gh"} else None

    def fake_run(command, **kwargs):
        ran.append(command)
        return _ok()

    ok = st.install_system_tool("gh", output_fn=lambda _: None, run=fake_run, which=fake_which)
    assert ok is True
    assert ran[0][:3] == ["sudo", "apt-get", "install"]
    assert ran[0][-1] == "gh"


def test_install_system_tool_no_manager_returns_false(monkeypatch):
    monkeypatch.setattr(st.os, "name", "posix")
    monkeypatch.setattr(st.sys, "platform", "linux")
    lines = []
    ok = st.install_system_tool(
        "git",
        output_fn=lines.append,
        run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
        which=lambda e: None,
    )
    assert ok is False
    assert any("Could not install" in line for line in lines)


def test_install_system_tool_nonzero_returncode_returns_false(monkeypatch):
    monkeypatch.setattr(st.os, "name", "nt")
    ok = st.install_system_tool(
        "gh",
        output_fn=lambda _: None,
        run=lambda *a, **k: subprocess.CompletedProcess([], returncode=1),
        which=lambda e: rf"C:\{e}.exe",
    )
    assert ok is False


# ---------------------------------------------------------------------------
# cli._ensure_git_identity
# ---------------------------------------------------------------------------


def test_ensure_git_identity_noop_when_already_set():
    with patch("agitrack.cli._git_config_global", side_effect=["Ada", "ada@x.io"]) as m:
        cli._ensure_git_identity()
    # Two reads (name, email); no writes.
    assert m.call_count == 2


def test_ensure_git_identity_prompts_and_sets_both():
    # Reads: name="", email="" → prompt both → re-reads return the entered values.
    reads = iter(["", "", "Ada Lovelace", "ada@x.io"])
    writes = []

    def fake_cfg(args):
        if args[0] == "--get":
            return next(reads)
        writes.append(tuple(args))
        return ""

    inputs = iter(["Ada Lovelace", "ada@x.io"])
    with (
        patch("agitrack.cli._git_config_global", side_effect=fake_cfg),
        patch("builtins.input", lambda _prompt: next(inputs)),
    ):
        cli._ensure_git_identity()
    assert ("user.name", "Ada Lovelace") in writes
    assert ("user.email", "ada@x.io") in writes


# ---------------------------------------------------------------------------
# cli._maybe_install_tool — gated on TTY + an available manager
# ---------------------------------------------------------------------------


def test_maybe_install_tool_non_tty_returns_false(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    assert cli._maybe_install_tool("git", required=True) is False


def test_maybe_install_tool_declined_returns_false(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    with (
        patch("agitrack.system_tools.can_install_tool", return_value=True),
        patch("agitrack.system_tools.install_system_tool", side_effect=AssertionError("must not install")),
        patch("builtins.input", lambda _prompt: "n"),
    ):
        assert cli._maybe_install_tool("git", required=True) is False


def test_maybe_install_tool_accepts_and_installs(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    with (
        patch("agitrack.system_tools.can_install_tool", return_value=True),
        patch("agitrack.system_tools.install_system_tool", return_value=True) as install,
        patch("builtins.input", lambda _prompt: ""),  # Enter → default yes
    ):
        assert cli._maybe_install_tool("gh", required=False) is True
    install.assert_called_once_with("gh")
