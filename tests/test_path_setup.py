"""Tests for agitrack/path_setup.py — making an installed agent CLI usable outside aGiTrack.

aGiTrack's own process PATH is patched the moment a backend is installed, so the agent runs
in THIS session; the user's other terminals knew nothing about it and reported "command not
found" until they found the right rc file themselves. These cover the offer that fixes that,
and — just as important — the failure path, which must be told to the user and acknowledged
rather than scrolling past.

No real profile is touched: every test writes into tmp_path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import agitrack.path_setup as ps


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway home directory, so profile writes never touch the real one."""
    monkeypatch.setattr(ps, "_home", lambda: tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Which file a new shell would actually read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shell,platform,expected",
    [
        ("/bin/zsh", "darwin", ".zshrc"),
        ("/usr/bin/fish", "linux", "config.fish"),
        ("/bin/bash", "darwin", ".bash_profile"),  # macOS terminals start LOGIN shells
        ("/bin/bash", "linux", ".bashrc"),
        ("/bin/ksh", "linux", ".profile"),  # unknown shell → the file every POSIX shell reads
        ("", "linux", ".profile"),
    ],
)
def test_profile_follows_the_users_login_shell(home, shell, platform, expected):
    profile = ps.shell_profile_path(environ={"SHELL": shell}, platform=platform)
    assert profile.name == expected
    assert profile.is_relative_to(home)


def test_the_written_line_matches_the_shell_it_is_written_for(home):
    directory = str(home / ".local" / "bin")
    # fish has no `export`; every other shell gets a PATH assignment, home-relative so the
    # line survives the home directory moving.
    assert ps.profile_line(directory, profile=home / ".zshrc") == 'export PATH="$HOME/.local/bin:$PATH"'
    assert ps.profile_line(directory, profile=home / "config.fish") == 'fish_add_path "$HOME/.local/bin"'
    outside = ps.profile_line("/opt/agents/bin", profile=home / ".zshrc")
    assert outside == 'export PATH="/opt/agents/bin:$PATH"'


# ---------------------------------------------------------------------------
# Writing the entry
# ---------------------------------------------------------------------------


# These call the profile writer directly rather than persist_path_entry: on Windows the
# dispatch goes to the registry branch, and a test must never edit the machine's real user
# PATH (on CI it did exactly that before this split). The dispatch itself is covered below.


def test_persist_appends_a_marked_line_and_creates_a_missing_profile(home):
    profile = home / ".zshrc"
    ok, detail = ps._persist_profile(str(home / ".local" / "bin"), profile)
    assert ok is True and detail == str(profile)
    written = profile.read_text(encoding="utf-8")
    assert ps.MARKER in written  # the user can see who wrote it, and so can the next install
    assert 'export PATH="$HOME/.local/bin:$PATH"' in written


def test_persist_does_not_disturb_what_was_already_in_the_profile(home):
    profile = home / ".zshrc"
    profile.write_text("alias ll='ls -l'", encoding="utf-8")  # note: no trailing newline
    ps._persist_profile(str(home / ".local" / "bin"), profile)
    written = profile.read_text(encoding="utf-8")
    assert written.startswith("alias ll='ls -l'\n")  # the user's last line stays its own line
    assert written.endswith("\n")


def test_persist_reports_a_failure_instead_of_raising(home):
    # An unwritable profile must come back as a reported failure — the caller turns that into
    # instructions the user acknowledges. The path is blocked by a FILE where a directory
    # would have to be, which fails the same way on every OS (permissions do not).
    (home / "blocked").write_text("not a directory", encoding="utf-8")
    ok, detail = ps._persist_profile(str(home / ".local" / "bin"), home / "blocked" / ".zshrc")
    assert ok is False
    assert "could not write" in detail


def test_persist_writes_the_profile_on_posix_and_the_registry_on_windows(monkeypatch, home):
    calls: list[str] = []
    monkeypatch.setattr(ps, "_persist_profile", lambda directory, target: calls.append("profile") or (True, "p"))
    monkeypatch.setattr(ps, "_persist_windows", lambda directory: calls.append("registry") or (True, "r"))

    monkeypatch.setattr(ps.os, "name", "posix")
    ps.persist_path_entry(str(home / "bin"), profile=home / ".zshrc")
    monkeypatch.setattr(ps.os, "name", "nt")
    ps.persist_path_entry(str(home / "bin"))
    assert calls == ["profile", "registry"]


def _fake_winreg(existing: str | None):
    """Stand-in for the winreg module, so the Windows branch is testable on any OS — and so a
    test can never touch a real HKCU\\Environment."""

    class Key:
        def __init__(self, store):
            self.store = store

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    store: dict[str, tuple] = {}
    if existing is not None:
        store["Path"] = (existing, 2)

    class FakeWinreg:
        HKEY_CURRENT_USER = object()
        KEY_READ = 1
        KEY_SET_VALUE = 2
        REG_EXPAND_SZ = 2

        @staticmethod
        def CreateKeyEx(root, path, reserved, access):
            return Key(store)

        @staticmethod
        def QueryValueEx(key, name):
            if name not in key.store:
                raise OSError("no such value")
            return key.store[name]

        @staticmethod
        def SetValueEx(key, name, reserved, value_type, value):
            key.store[name] = (value, value_type)

    return FakeWinreg, store


def test_windows_appends_to_the_user_path_without_rewriting_what_is_there(monkeypatch):
    import sys as _sys

    fake, store = _fake_winreg(r"C:\Windows;C:\Windows\System32")
    monkeypatch.setitem(_sys.modules, "winreg", fake)
    monkeypatch.setattr(ps.os, "name", "nt")

    ok, detail = ps._persist_windows(r"C:\Users\u\AppData\Roaming\npm")
    assert ok is True and "user PATH" in detail
    value = store["Path"][0]
    assert value.startswith(r"C:\Windows;C:\Windows\System32")  # the existing PATH is preserved
    assert value.endswith(r"C:\Users\u\AppData\Roaming\npm")


def test_windows_does_not_add_a_directory_that_is_already_listed(monkeypatch):
    import sys as _sys

    fake, store = _fake_winreg(r"C:\Windows;C:\Users\u\AppData\Roaming\npm")
    monkeypatch.setitem(_sys.modules, "winreg", fake)
    monkeypatch.setattr(ps.os, "name", "nt")

    ok, detail = ps._persist_windows(r"C:\Users\u\AppData\Roaming\NPM")  # same dir, different case
    assert ok is True and "already listed" in detail
    assert store["Path"][0] == r"C:\Windows;C:\Users\u\AppData\Roaming\npm"  # untouched


def test_already_in_profile_recognises_both_spellings(home):
    profile = home / ".zshrc"
    directory = str(home / ".local" / "bin")
    assert ps.already_in_profile(directory, profile=profile) is False  # no file yet
    profile.write_text('export PATH="$HOME/.local/bin:$PATH"\n', encoding="utf-8")
    assert ps.already_in_profile(directory, profile=profile) is True  # $HOME-relative
    profile.write_text(f'export PATH="{directory}:$PATH"\n', encoding="utf-8")
    assert ps.already_in_profile(directory, profile=profile) is True  # absolute


def test_on_inherited_path_ignores_what_only_this_process_added():
    # THE distinction the whole module rests on: aGiTrack prepends the new directory to its
    # own os.environ so the agent runs now, which must not be mistaken for the user's shells
    # having it too.
    inherited = os.pathsep.join(["/usr/bin", "/bin"])
    assert ps.on_inherited_path("/usr/bin", inherited=inherited) is True
    assert ps.on_inherited_path("/usr/bin/", inherited=inherited) is True  # trailing separator
    assert ps.on_inherited_path("/home/u/.local/bin", inherited=inherited) is False


# ---------------------------------------------------------------------------
# The offer, end to end
# ---------------------------------------------------------------------------


def _offer(home, monkeypatch, *, answers, persist_fn, inherited="/usr/bin"):
    monkeypatch.setattr(ps, "INHERITED_PATH", inherited)
    monkeypatch.setattr(ps.os.environ, "get", lambda key, default="": {"SHELL": "/bin/zsh"}.get(key, default))
    asked: list[str] = []
    lines: list[str] = []
    replies = iter(answers)

    def input_fn(prompt: str) -> str:
        asked.append(prompt)
        return next(replies, "")

    result = ps.offer_to_persist_path(
        str(home / ".local" / "bin"),
        label="Claude Code",
        command="claude",
        input_fn=input_fn,
        output_fn=lines.append,
        persist_fn=persist_fn,
    )
    return result, asked, "\n".join(lines)


def test_accepting_the_offer_writes_the_entry(home, monkeypatch):
    written: list[tuple] = []
    result, asked, output = _offer(
        home,
        monkeypatch,
        answers=[""],
        persist_fn=lambda directory, **kw: written.append((directory, kw)) or (True, "~/.zshrc"),
    )
    assert result is True
    assert len(written) == 1
    assert asked and asked[0].strip().startswith("Add it to your PATH")  # Enter accepts
    assert "not on your PATH" in output  # the user is told WHY they're being asked
    assert "Open a NEW terminal" in output


def test_declining_leaves_the_profile_alone_but_says_how_to_do_it(home, monkeypatch):
    result, asked, output = _offer(
        home, monkeypatch, answers=["n"], persist_fn=lambda *a, **k: pytest.fail("must not write on a decline")
    )
    assert result is False
    assert len(asked) == 1  # declining is a decision, not something to acknowledge again
    assert "Leaving your PATH unchanged" in output
    assert "add this directory to your PATH" in output


def test_a_failed_write_is_reported_and_must_be_acknowledged(home, monkeypatch):
    # The one outcome that leaves the CLI aGiTrack-only. It must not scroll past unread.
    result, asked, output = _offer(
        home, monkeypatch, answers=["y", ""], persist_fn=lambda *a, **k: (False, "could not write /x/.zshrc: denied")
    )
    assert result is False
    assert len(asked) == 2
    assert "acknowledge" in asked[1].lower()
    assert "Could not update your PATH automatically" in output
    assert "could not write /x/.zshrc: denied" in output  # the actual reason, not a shrug
    assert "Then open a NEW terminal" in output


def test_nothing_is_asked_when_the_users_shells_already_find_it(home, monkeypatch):
    directory = str(home / ".local" / "bin")
    result, asked, output = _offer(
        home,
        monkeypatch,
        answers=[],
        persist_fn=lambda *a, **k: pytest.fail("nothing to persist"),
        inherited=os.pathsep.join(["/usr/bin", directory]),
    )
    assert result is True
    assert asked == [] and output == ""


def test_an_entry_written_by_an_earlier_run_is_not_duplicated(home, monkeypatch):
    (home / ".zshrc").write_text('export PATH="$HOME/.local/bin:$PATH"\n', encoding="utf-8")
    result, asked, output = _offer(
        home, monkeypatch, answers=[], persist_fn=lambda *a, **k: pytest.fail("already there")
    )
    assert result is True
    assert asked == []
    assert "already adds to your PATH" in output


def test_without_a_terminal_the_instructions_are_printed_and_nothing_is_asked(home, monkeypatch):
    monkeypatch.setattr(ps, "INHERITED_PATH", "/usr/bin")
    lines: list[str] = []
    result = ps.offer_to_persist_path(
        str(home / ".local" / "bin"),
        label="Claude Code",
        command="claude",
        input_fn=lambda prompt: pytest.fail("must not prompt without a terminal"),
        output_fn=lines.append,
        persist_fn=lambda *a, **k: pytest.fail("must not write without consent"),
        interactive=False,
    )
    assert result is False
    assert any("add this directory to your PATH" in line for line in lines)


def test_manual_instructions_cover_every_platform(home):
    text = ps.manual_path_instructions(str(home / ".local" / "bin"), profile=home / ".zshrc")
    assert "macOS / Linux" in text and "Windows" in text
    assert "\n\n" in text  # blocks separated by a blank line, like the other install hints


# ---------------------------------------------------------------------------
# The installer wires it up
# ---------------------------------------------------------------------------


def test_install_backend_offers_the_path_entry_after_a_successful_install(monkeypatch, tmp_path):
    import subprocess
    from unittest.mock import patch

    from agitrack.backends.setup import install_backend

    monkeypatch.setattr("agitrack.backends.setup.os.name", "posix")
    offered: list[dict] = []
    monkeypatch.setattr("agitrack.backends.setup._installed_bin_dir", lambda name: str(tmp_path / ".local" / "bin"))
    monkeypatch.setattr(
        "agitrack.path_setup.offer_to_persist_path",
        lambda directory, **kwargs: offered.append({"directory": directory, **kwargs}) or True,
    )

    with patch("agitrack.backends.setup.backend_installed", side_effect=[True]):
        ok = install_backend(
            "claude",
            input_fn=lambda prompt: "",
            output_fn=lambda _: None,
            run=lambda command, **kw: subprocess.CompletedProcess(command, returncode=0, stdout="", stderr=""),
            which=lambda exe: f"/usr/bin/{exe}" if exe in {"bash", "curl"} else None,
        )

    assert ok is True
    assert offered and offered[0]["directory"] == str(tmp_path / ".local" / "bin")
    assert offered[0]["command"] == "claude"  # what the user would type in another terminal
    assert offered[0]["label"] == "Claude Code"


def test_a_broken_path_check_never_fails_the_install(monkeypatch):
    import subprocess
    from unittest.mock import patch

    from agitrack.backends.setup import install_backend

    monkeypatch.setattr("agitrack.backends.setup.os.name", "posix")
    monkeypatch.setattr(
        "agitrack.backends.setup._installed_bin_dir",
        lambda name: (_ for _ in ()).throw(OSError("no idea where it went")),
    )
    lines: list[str] = []
    with patch("agitrack.backends.setup.backend_installed", side_effect=[True]):
        ok = install_backend(
            "claude",
            input_fn=lambda prompt: "",
            output_fn=lines.append,
            run=lambda command, **kw: subprocess.CompletedProcess(command, returncode=0, stdout="", stderr=""),
            which=lambda exe: f"/usr/bin/{exe}" if exe in {"bash", "curl"} else None,
        )
    assert ok is True  # the backend runs under aGiTrack; a PATH convenience must not undo that
    assert any("could not check your PATH" in line for line in lines)


def test_the_path_offer_uses_the_same_drained_input_as_the_rest_of_first_run():
    import inspect

    from agitrack.backends.setup import install_backend
    from agitrack.console import ask

    assert inspect.signature(install_backend).parameters["input_fn"].default is ask


def test_installed_bin_dir_is_read_from_the_executable_itself(monkeypatch):
    from agitrack.backends import setup

    monkeypatch.setattr(setup, "which_executable", lambda exe: str(Path("/home/u/.local/bin") / exe))
    assert setup._installed_bin_dir("claude") == str(Path("/home/u/.local/bin"))
    monkeypatch.setattr(setup, "which_executable", lambda exe: None)
    assert setup._installed_bin_dir("claude") is None


def test_a_piped_run_prints_instructions_instead_of_prompting(monkeypatch, tmp_path):
    # Consent can't be given without a terminal, so a scripted install must not block on a
    # question nobody can answer — it says what to do and moves on.
    import subprocess
    from unittest.mock import patch

    from agitrack.backends.setup import install_backend

    monkeypatch.setattr("agitrack.backends.setup.os.name", "posix")
    monkeypatch.setattr("agitrack.backends.setup.sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)}))
    monkeypatch.setattr("agitrack.backends.setup._installed_bin_dir", lambda name: str(tmp_path / "bin"))
    seen: list[bool] = []
    monkeypatch.setattr(
        "agitrack.path_setup.offer_to_persist_path",
        lambda directory, **kwargs: seen.append(kwargs["interactive"]) or False,
    )
    with patch("agitrack.backends.setup.backend_installed", side_effect=[True]):
        install_backend(
            "claude",
            output_fn=lambda _: None,
            run=lambda command, **kw: subprocess.CompletedProcess(command, returncode=0, stdout="", stderr=""),
            which=lambda exe: f"/usr/bin/{exe}" if exe in {"bash", "curl"} else None,
        )
    assert seen == [False]


def test_the_windows_fallback_never_hands_the_user_setx():
    """A7: the fallback printed `setx PATH "$env:PATH;<dir>"` — on the non-TTY path, on decline,
    and on write failure, i.e. the "press Enter to acknowledge" path aGiTrack deliberately makes
    unmissable. This file's OWN docstring documents setx as the thing to avoid ("it truncates the
    value at 1024 characters, which silently destroys a long PATH"), and it is wrong in scope
    too: in PowerShell `$env:PATH` is the combined machine+user value but setx writes the USER
    value, so every machine entry is permanently copied into the user PATH."""
    from agitrack.path_setup import manual_path_instructions

    text = manual_path_instructions(r"C:\Users\dev\AppData\Roaming\npm")

    assert "setx" not in text
    assert "$env:PATH" not in text  # the combined machine+user value must never be written back
    assert "GetEnvironmentVariable('Path', 'User')" in text
    assert "SetEnvironmentVariable('Path'" in text
    assert "'User'" in text
    assert r"C:\Users\dev\AppData\Roaming\npm" in text
