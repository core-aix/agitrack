"""Windows platform-layer regression tests that run on POSIX too (#118).

The factory dispatch, the socketpair waker, the POSIX host adapter, and the pure
command/env resolvers don't need Windows to run — exercising them here keeps the Windows
code paths covered on the normal Linux/macOS gate. The genuinely Windows-only pieces (ConPTY
spawn, Win32 console) live in ``test_windows_conpty`` (skipped off Windows)."""

import select
import sys
import types

import pytest

import agitrack.cli as cli
import agitrack.proxy.platform as platform
from agitrack.proxy.platform.nt import NtWaker, _env_block, _resolve_windows_command

_posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-path behaviour only")


# --- reactor waker (socket-bridge) -------------------------------------------------


def test_make_waker_is_selectable_and_wakes():
    waker = platform.make_waker()
    try:
        assert waker.wake_fileno() not in select.select([waker.wake_fileno()], [], [], 0)[0]
        waker.wake()
        assert waker.wake_fileno() in select.select([waker.wake_fileno()], [], [], 0.5)[0]
        waker.drain()
        assert waker.wake_fileno() not in select.select([waker.wake_fileno()], [], [], 0)[0]
    finally:
        waker.close()


def test_nt_waker_socketpair_works_on_any_platform():
    # NtWaker is socketpair-based (Windows ``select`` accepts only sockets) — but the
    # socketpair primitive is cross-platform, so it's directly testable here.
    waker = NtWaker()
    try:
        waker.wake()
        assert waker.wake_fileno() in select.select([waker.wake_fileno()], [], [], 0.5)[0]
    finally:
        waker.close()


# --- host terminal factory ---------------------------------------------------------


@_posix_only
def test_make_host_terminal_posix_adapter():
    runner = types.SimpleNamespace(old_attrs=None)
    host = platform.make_host_terminal(runner)
    # POSIX learns of resizes via SIGWINCH, so the polled flag is always False; start/stop
    # are no-ops there (the Windows impl starts/stops the reader + resize threads).
    assert host.consume_resize_pending() is False
    host.start()
    host.stop()  # must not raise
    # The POSIX adapter forwards stdin reads to fd 0 (not asserted directly — pytest
    # replaces sys.stdin with a pseudo-file that has no fileno()).
    assert type(host).__name__ == "PosixHostTerminal"


# --- child-process factory dispatch ------------------------------------------------


@_posix_only
def test_make_child_process_dispatches_to_posix_spawn(monkeypatch):
    import agitrack.proxy.process as process

    seen = {}
    monkeypatch.setattr(
        process.BackendProcess,
        "spawn",
        classmethod(lambda cls, command, cwd, extra_env=None: seen.setdefault("args", (command, cwd, extra_env))),
    )
    platform.make_child_process(["claude", "--x"], "/repo", {"K": "V"})
    assert seen["args"] == (["claude", "--x"], "/repo", {"K": "V"})


# --- Windows command resolution (pure, runs anywhere) ------------------------------


def test_resolve_windows_command_wraps_cmd_shims(tmp_path):
    # A .cmd backend shim (npm's claude.cmd) must run through cmd.exe /c — the only way
    # ConPTY's CreateProcess executes a batch file. Returns the command-LINE string, with the
    # inner command wrapped in protective quotes; args are preserved after the script.
    script = tmp_path / "be.cmd"
    script.write_text("@echo off\n")
    appname, cmdline = _resolve_windows_command([str(script), "run", "--flag"])
    assert appname.lower().endswith("cmd.exe")
    assert cmdline.startswith('/c "') and cmdline.endswith('"')
    assert str(script) in cmdline
    assert "run" in cmdline and "--flag" in cmdline


def test_resolve_windows_command_cmd_shim_under_spaced_path(tmp_path):
    # A .cmd shim under a path with spaces (e.g. a username "First Last") must be wrapped so
    # cmd.exe's quote-strip after /c doesn't split it at the space — the whole inner command,
    # including the quoted shim path and a quoted spaced arg, gets an extra outer quote pair.
    d = tmp_path / "First Last" / "npm"
    d.mkdir(parents=True)
    script = d / "claude.cmd"
    script.write_text("@echo off\n")
    appname, cmdline = _resolve_windows_command([str(script), "--flag", "arg with space"])
    assert appname.lower().endswith("cmd.exe")
    assert cmdline.startswith('/c "') and cmdline.endswith('"')
    assert f'"{script}"' in cmdline  # the spaced shim path is quoted
    assert '"arg with space"' in cmdline  # the spaced arg is quoted


def test_resolve_windows_command_passes_through_plain_exe(tmp_path):
    exe = tmp_path / "be.exe"
    exe.write_text("")  # not executable on POSIX, so shutil.which returns None → use as-is
    appname, cmdline = _resolve_windows_command([str(exe), "go"])
    assert appname == str(exe)
    assert cmdline == "go"


def test_resolve_windows_command_ignores_half_installed_npm_shim(monkeypatch):
    # A backend whose npm install left only an extensionless shell script / .ps1 (no .cmd)
    # must NOT resolve to that non-runnable file — which_executable returns None, so the
    # command falls back to the bare name (which then fails loudly rather than launching a
    # file ConPTY can't execute and looping on "exited repeatedly").
    import agitrack.proc as proc

    present = {"claude": r"C:\npm\claude", "claude.ps1": r"C:\npm\claude.ps1"}
    monkeypatch.setattr(proc, "_IS_WINDOWS", True)
    monkeypatch.setattr(proc.shutil, "which", lambda name: present.get(name))
    appname, cmdline = _resolve_windows_command(["claude", "--resume"])
    assert appname == "claude"  # not C:\npm\claude (the bare shell script)
    assert cmdline == "--resume"


def test_env_block_is_null_separated_or_none():
    assert _env_block(None) is None  # inherit the environment unchanged
    block = _env_block({"AGIT_X": "1"})
    assert block is not None
    assert "AGIT_X=1\0" in block
    assert block.endswith("\0")


# --- --backend-command parsing keeps Windows backslashes ---------------------------


def test_resolve_backend_command_keeps_windows_backslashes(monkeypatch):
    # shlex defaults to POSIX mode (backslash = escape), which would mangle C:\tools\x.exe.
    monkeypatch.setattr(cli.os, "name", "nt")
    tokens, err = cli._resolve_backend_command(r"C:\tools\wrap.exe claude", config=None, backend="claude")  # type: ignore[arg-type]
    assert err is None
    assert tokens == [r"C:\tools\wrap.exe", "claude"]


def test_resolve_backend_command_posix_splits_normally(monkeypatch):
    monkeypatch.setattr(cli.os, "name", "posix")
    tokens, err = cli._resolve_backend_command("wrapper claude", config=None, backend="claude")  # type: ignore[arg-type]
    assert err is None
    assert tokens == ["wrapper", "claude"]


# --- a user-supplied launch command bypasses the backend install gate --------------


def test_custom_launch_command_bypasses_install_gate(monkeypatch):
    from tests.proxy_helpers import make_runner

    import agitrack.proxy.runner as runner_mod

    runner = make_runner()
    runner._launch_command = lambda: ["wrapper", "claude"]
    monkeypatch.setattr(
        runner_mod,
        "ensure_installed_backend",
        lambda *a, **k: pytest.fail("the install gate must be skipped when a launch command is set"),
    )
    assert runner._ensure_backend_available() is True


# --- install guidance covers macOS, Linux, AND Windows in one message -----------------


def test_backend_install_hint_covers_all_platforms():
    from agitrack.backends import setup

    hint = setup.install_hint("opencode")
    assert "npm install -g opencode-ai" in hint  # works on any OS (with Node)
    assert "winget install OpenJS.NodeJS" in hint  # Windows Node
    assert "brew install node" in hint  # macOS Node
    assert "package manager" in hint  # Linux
    assert "opencode.ai" in hint


def test_git_install_hint_covers_all_platforms():
    hint = cli._git_install_hint()
    assert "brew install git" in hint  # macOS
    assert "apt install git" in hint  # Linux
    assert "winget install Git.Git" in hint  # Windows
    assert "\n\n" in hint  # parts separated by a blank line for legibility


def test_gh_install_hint_covers_all_platforms():
    hint = cli._gh_install_hint()
    assert "brew install gh" in hint  # macOS
    assert "apt install gh" in hint  # Linux
    assert "winget install GitHub.cli" in hint  # Windows
    assert "gh auth login" in hint  # how to sign in after installing
    assert "\n\n" in hint  # parts separated by a blank line for legibility


def test_backend_install_hint_parts_are_blank_line_separated():
    from agitrack.backends import setup

    # Each instruction part is its own block so the options are easy to tell apart on screen.
    assert "\n\n" in setup.install_hint("claude")


def test_cli_gives_clear_message_when_git_missing(monkeypatch, capsys):
    # The VS Code extension can install the aGiTrack CLI without git being present; that must
    # produce an actionable message, not a raw FileNotFoundError from repo discovery.
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "git" else f"/usr/bin/{name}")
    rc = cli.main(["--repo", "."])
    assert rc == 1
    assert "git is not installed" in capsys.readouterr().out
