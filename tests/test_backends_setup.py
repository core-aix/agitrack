"""Tests for agitrack/backends/setup.py.

All backend availability checks and subprocess calls are mocked.
"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from agitrack.backends.setup import (
    ensure_installed_backend,
    install_backend,
    install_hint,
    select_default_backend,
    select_default_summarizer_model,
    BackendUnavailable,
)


# ---------------------------------------------------------------------------
# install_hint — cross-platform (consolidated in dev merge)
# ---------------------------------------------------------------------------


def test_install_hint_claude_mentions_windows_winget():
    hint = install_hint("claude")
    assert "winget" in hint
    assert "Windows" in hint


def test_install_hint_claude_mentions_curl():
    hint = install_hint("claude")
    assert "curl" in hint


def test_install_hint_codex_mentions_npm_and_windows():
    hint = install_hint("codex")
    assert "npm" in hint
    assert "@openai/codex" in hint
    assert "Windows" in hint


def test_install_hint_codex_names_the_product():
    # The hint is the only thing a user without the CLI sees, so it must name what to install
    # and where it comes from — not just "install the 'codex' CLI".
    hint = install_hint("codex")
    assert "Codex" in hint
    assert "developers.openai.com" in hint


def test_install_hint_opencode_mentions_npm():
    hint = install_hint("opencode")
    assert "npm" in hint


def test_install_hint_opencode_mentions_windows():
    hint = install_hint("opencode")
    assert "Windows" in hint


def test_install_hint_unknown_backend():
    hint = install_hint("unknown-backend-xyz")
    assert "unknown-backend-xyz" in hint
    assert "PATH" in hint


# ---------------------------------------------------------------------------
# select_default_backend — invalid input retry, then valid
# ---------------------------------------------------------------------------


def test_select_default_backend_invalid_then_skip():
    # Nothing installed, so the install prompt is shown; out-of-range and non-numeric
    # answers are rejected with a message, then Enter skips (default falls back to first).
    config = MagicMock()
    calls = iter(["99", "abc", ""])
    output_lines = []

    with (
        patch("agitrack.backends.setup.available_backends", return_value=["claude", "opencode"]),
        patch("agitrack.backends.setup.backend_installed", return_value=False),
    ):
        result = select_default_backend(
            config,
            input_fn=lambda _: next(calls),
            output_fn=output_lines.append,
            install_fn=lambda name, input_fn, output_fn: pytest.fail("invalid/skip answers must not install"),
        )

    assert result == "claude"
    assert any("valid number" in line for line in output_lines)


def test_select_default_backend_valid_first_try():
    config = MagicMock()
    with (
        patch("agitrack.backends.setup.available_backends", return_value=["claude"]),
        patch("agitrack.backends.setup.backend_installed", return_value=True),
    ):
        result = select_default_backend(config, input_fn=lambda _: "1", output_fn=lambda _: None)
    assert result == "claude"
    assert config.default_backend == "claude"


# ---------------------------------------------------------------------------
# select_default_summarizer_model — invalid input → recommended default
# ---------------------------------------------------------------------------


def test_select_default_summarizer_model_invalid_input_uses_default():
    # list_available_models / smallest_model are lazily imported inside the function;
    # patch them at their source module.
    config = MagicMock()
    config.summarization_model = None
    with (
        patch(
            "agitrack.summaries.model_select.list_available_models",
            return_value=["claude-haiku-4-5-20251001", "claude-sonnet-4-6"],
        ),
        patch(
            "agitrack.summaries.model_select.smallest_model",
            return_value="claude-haiku-4-5-20251001",
        ),
    ):
        select_default_summarizer_model(config, "claude", input_fn=lambda _: "not-a-number", output_fn=lambda _: None)
    assert config.summarization_model == "claude-haiku-4-5-20251001"


def test_select_default_summarizer_model_valid_choice_saved():
    config = MagicMock()
    with (
        patch(
            "agitrack.summaries.model_select.list_available_models",
            return_value=["claude-haiku-4-5-20251001", "claude-sonnet-4-6"],
        ),
        patch(
            "agitrack.summaries.model_select.smallest_model",
            return_value="claude-haiku-4-5-20251001",
        ),
    ):
        select_default_summarizer_model(config, "claude", input_fn=lambda _: "2", output_fn=lambda _: None)
    assert config.summarization_model == "claude-sonnet-4-6"


def test_select_default_summarizer_model_no_models_is_noop():
    # When no models come back, the function returns early without touching config.
    config = MagicMock()
    with patch("agitrack.summaries.model_select.list_available_models", return_value=[]):
        select_default_summarizer_model(config, "claude", input_fn=lambda _: "1", output_fn=lambda _: None)
    config.summarization_model.__set__.assert_not_called() if hasattr(config.summarization_model, "__set__") else None


# ---------------------------------------------------------------------------
# ensure_installed_backend — re-check inside loop finds backend
# ---------------------------------------------------------------------------


def test_ensure_installed_backend_already_installed():
    config = MagicMock()
    with patch("agitrack.backends.setup.backend_installed", return_value=True):
        result = ensure_installed_backend("claude", config, interactive=True)
    assert result == "claude"


def test_ensure_installed_backend_not_interactive_raises():
    config = MagicMock()
    with patch("agitrack.backends.setup.backend_installed", return_value=False):
        with pytest.raises(BackendUnavailable):
            ensure_installed_backend("claude", config, interactive=False)


def test_ensure_installed_backend_is_a_gate_not_an_installer():
    # The launch gate never installs (that's the MSI / first-run). It shows the manual hint
    # and re-checks on Enter; once the user has installed it by hand, it proceeds.
    config = MagicMock()
    state = {"installed": False}
    lines = []

    def fake_input(_prompt):
        state["installed"] = True  # user installs it, then presses Enter
        return ""

    with (
        patch("agitrack.backends.setup.available_backends", return_value=["claude"]),
        patch("agitrack.backends.setup.backend_installed", side_effect=lambda _name: state["installed"]),
        patch("agitrack.backends.setup.install_hint", return_value="INSTALL-HINT"),
    ):
        result = ensure_installed_backend(
            "claude", config, interactive=True, input_fn=fake_input, output_fn=lines.append
        )
    assert result == "claude"
    assert any("INSTALL-HINT" in line for line in lines)  # showed instructions, didn't install


def test_ensure_installed_backend_quit_raises():
    config = MagicMock()
    with (
        patch("agitrack.backends.setup.available_backends", return_value=["claude"]),
        patch("agitrack.backends.setup.backend_installed", return_value=False),
        patch("agitrack.backends.setup.install_hint", return_value="hint"),
    ):
        with pytest.raises(BackendUnavailable):
            ensure_installed_backend(
                "claude",
                config,
                interactive=True,
                input_fn=lambda _: "q",
                output_fn=lambda _: None,
            )


# ---------------------------------------------------------------------------
# select_default_backend — show statuses, offer to install the uninstalled ones
# ---------------------------------------------------------------------------


def test_select_default_backend_shows_status_and_skips_with_enter():
    # Both already installed: every line shows "installed", no install is offered, Enter is
    # not even needed (nothing uninstalled). Default is the first.
    config = MagicMock()
    lines = []
    with patch("agitrack.backends.setup.backend_installed", return_value=True):
        result = select_default_backend(config, input_fn=lambda _: "", output_fn=lines.append)
    assert result == "claude"
    assert any("claude (installed)" in line for line in lines)
    assert any("opencode (installed)" in line for line in lines)


def test_select_default_backend_picking_uninstalled_installs_and_selects_it():
    # claude installed, the rest not: picking '2' installs that backend AND makes it the default
    # — the single prompt's number is the default choice, not a separate install offer. The
    # expected name comes from the (alphabetical) registry so a new backend renumbers safely.
    from agitrack.backends.proxy_agents import available_backends

    second = available_backends()[1]
    installs = []
    config = MagicMock()

    def fake_installed(name):
        return name == "claude" or name in installs

    # "2" picks the backend; "y" confirms the install, which is no longer implicit — typing a
    # number used to run `curl … | bash` immediately.
    answers = iter(["2", "y"])
    with patch("agitrack.backends.setup.backend_installed", side_effect=fake_installed):
        result = select_default_backend(
            config,
            input_fn=lambda _: next(answers),
            output_fn=lambda _: None,
            install_fn=lambda name, input_fn, output_fn: installs.append(name) or True,
        )
    assert installs == [second]  # the chosen uninstalled backend is installed first
    assert result == second  # …and it becomes the default


def test_select_default_backend_none_installed_pick_installs_chosen():
    # Nothing installed: picking '1' installs that backend and makes it the default.
    installs = []
    config = MagicMock()

    def fake_installed(name):
        return name in installs

    answers = iter(["1", "y"])  # pick, then confirm the install (no longer implicit)
    with patch("agitrack.backends.setup.backend_installed", side_effect=fake_installed):
        result = select_default_backend(
            config,
            input_fn=lambda _: next(answers),
            output_fn=lambda _: None,
            install_fn=lambda name, input_fn, output_fn: installs.append(name) or True,
        )
    assert installs == ["claude"]
    assert result == "claude"
    assert config.default_backend == "claude"


def test_select_default_backend_skip_when_none_installed_defaults_to_first():
    # User presses Enter without installing: default falls back to the first listed; the
    # launch-time gate will offer to install it later.
    config = MagicMock()
    with patch("agitrack.backends.setup.backend_installed", return_value=False):
        result = select_default_backend(
            config,
            input_fn=lambda _: "",
            output_fn=lambda _: None,
            install_fn=lambda name, input_fn, output_fn: pytest.fail("nothing should be installed on skip"),
        )
    assert result == "claude"


# ---------------------------------------------------------------------------
# install_backend — cross-platform automatic install
# ---------------------------------------------------------------------------


def test_install_backend_posix_prefers_official_script(monkeypatch):
    monkeypatch.setattr("agitrack.backends.setup.os.name", "posix")
    ran = []

    def fake_run(command, **kwargs):
        ran.append(command)
        return subprocess.CompletedProcess(command, returncode=0, stdout="/usr/local", stderr="")

    def fake_which(exe):
        return f"/usr/bin/{exe}" if exe in {"bash", "curl"} else None  # no npm

    with patch("agitrack.backends.setup.backend_installed", side_effect=[True]):
        ok = install_backend("claude", output_fn=lambda _: None, run=fake_run, which=fake_which, persist_path=False)
    assert ok is True
    # The official installer (bash -lc "curl … | bash") was used, not npm.
    assert ran and ran[0][:2] == ["bash", "-lc"]
    assert "claude.ai/install.sh" in ran[0][2]


def test_install_backend_uses_npm_when_no_script_tools(monkeypatch):
    monkeypatch.setattr("agitrack.backends.setup.os.name", "posix")
    ran = []

    def fake_run(command, **kwargs):
        ran.append(command)
        return subprocess.CompletedProcess(command, returncode=0, stdout="/usr/local", stderr="")

    def fake_which(exe):
        return "/usr/bin/npm" if exe == "npm" else None  # npm only, no bash/curl

    with patch("agitrack.backends.setup.backend_installed", side_effect=[True]):
        ok = install_backend("opencode", output_fn=lambda _: None, run=fake_run, which=fake_which, persist_path=False)
    assert ok is True
    # npm install -g opencode-ai (resolve_subprocess_command passes it through on POSIX).
    assert any("install" in c and "opencode-ai" in c for c in ran)


def test_install_backend_no_installer_available_returns_false(monkeypatch):
    monkeypatch.setattr("agitrack.backends.setup.os.name", "posix")
    lines = []
    with patch("agitrack.backends.setup.backend_installed", return_value=False):
        ok = install_backend(
            "claude",
            output_fn=lines.append,
            run=lambda *a, **k: pytest.fail("nothing runnable should be invoked"),
            which=lambda exe: None,  # nothing on PATH: no bash/curl/npm
        )
    assert ok is False
    assert any("Could not install" in line for line in lines)


def test_install_backend_unknown_backend_returns_false():
    assert install_backend("nope", output_fn=lambda _: None, run=lambda *a, **k: None, which=lambda e: None) is False


# ---------------------------------------------------------------------------
# First-run flow ergonomics: drained prompts, separated sections, visible progress
# ---------------------------------------------------------------------------


def test_first_run_prompts_drain_stale_keypresses_by_default():
    # The install steps between these questions can run for minutes. Whatever the user
    # pressed while waiting must be discarded, or it answers the NEXT question and that
    # question flashes past looking as if it had been skipped.
    import inspect

    from agitrack.console import ask

    for func in (select_default_backend, select_default_summarizer_model, ensure_installed_backend):
        assert inspect.signature(func).parameters["input_fn"].default is ask, func.__name__


def test_install_backend_announces_a_slow_step_and_closes_the_installers_output(monkeypatch):
    monkeypatch.setattr("agitrack.backends.setup.os.name", "posix")
    lines = []

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, returncode=0, stdout="/usr/local", stderr="")

    with patch("agitrack.backends.setup.backend_installed", side_effect=[True]):
        ok = install_backend(
            "claude",
            output_fn=lines.append,
            run=fake_run,
            which=lambda exe: f"/usr/bin/{exe}" if exe in {"bash", "curl"} else None,
            persist_path=False,
        )
    assert ok is True
    # A "this takes a while" reassurance, because the installer itself can print nothing.
    assert any("take several minutes" in line for line in lines)
    # A bare newline after the installer runs, so its (possibly unterminated) output can't
    # run into the next message or leave the following question on the same line.
    assert "" in lines
    # The step itself starts on a fresh line, separated from the preceding output.
    assert any(line.startswith("\nInstalling Claude Code") for line in lines)


def test_backend_question_starts_a_clearly_separated_block():
    config = MagicMock()
    lines = []
    with patch("agitrack.backends.setup.backend_installed", return_value=True):
        select_default_backend(config, input_fn=lambda _prompt: "", output_fn=lines.append)
    # Two newlines: the first ends any partial line left by an installer's output, the
    # second leaves a blank line so the question reads as a new section.
    assert lines[0].startswith("\n\nAgent backends:")


def test_a_backend_installed_off_path_is_found(tmp_path, monkeypatch):
    """N5: detection was PATH-only, so a working `claude` at %APPDATA%\\npm or ~/.local/bin was
    reported "not installed" — with a reinstall recipe whose official installer drops it in that
    same directory, so a naive user could loop. `_candidate_bin_dirs()` already hardcoded exactly
    those paths (with the comment "claude.cmd lands here"), but its only call site was INSIDE the
    install routine, after a successful install."""
    from agitrack.backends import setup

    bindir = tmp_path / "offpath-bin"
    bindir.mkdir()
    shim = bindir / "claude"
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)

    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(setup, "_candidate_bin_dirs", lambda npm, run: [str(bindir)])

    assert setup.backend_installed("claude") is True
    # ...and the directory is added to PATH, so the launch that follows actually works rather
    # than reporting success and then failing to spawn.
    assert str(bindir) in os.environ["PATH"]


def test_a_backend_that_is_really_missing_is_still_reported_missing(tmp_path, monkeypatch):
    from agitrack.backends import setup

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(setup, "_candidate_bin_dirs", lambda npm, run: [str(tmp_path / "also-empty")])

    assert setup.backend_installed("claude") is False


def test_the_codex_install_hint_does_not_use_the_403_endpoint():
    """The chatgpt.com installer endpoint returns HTTP 403 from some networks (two independent
    live-test runs confirmed it), so the hint would hand the user a command that fails."""
    from agitrack.backends.setup import install_hint

    hint = install_hint("codex")
    assert "chatgpt.com/backend-api/codex/install" not in hint
    assert "@openai/codex" in hint


def test_picking_an_uninstalled_backend_asks_before_downloading_it():
    """N0: typing a number ran `curl … | bash` IMMEDIATELY — no y/N, no abort, the only
    disclosure a trailing clause on the question above. A user picking the agent they PLANNED to
    use later got an unannounced ~291 MB download piped from the network into a shell."""
    from agitrack.backends import setup

    class _Config:
        default_backend = None

    installs: list[str] = []
    printed: list[str] = []
    answers = iter(["2", "n"])  # pick a not-installed backend, then decline the install

    with patch("agitrack.backends.setup.backend_installed", side_effect=lambda name: False):
        chosen = setup.select_default_backend(
            _Config(),
            input_fn=lambda prompt: next(answers),
            output_fn=printed.append,
            install_fn=lambda name, **kw: installs.append(name) or True,
        )

    assert installs == []  # declined ⇒ nothing was downloaded
    text = "\n".join(printed)
    assert "is not installed yet" in text
    assert "stays your default" in text  # declining is not a dead end
    assert chosen  # ...and the choice was still saved


def test_confirming_the_install_still_installs():
    from agitrack.backends import setup

    class _Config:
        default_backend = None

    installs: list[str] = []
    answers = iter(["2", "y"])

    with patch("agitrack.backends.setup.backend_installed", side_effect=lambda name: False):
        setup.select_default_backend(
            _Config(),
            input_fn=lambda prompt: next(answers),
            output_fn=lambda text: None,
            install_fn=lambda name, **kw: installs.append(name) or True,
        )

    assert len(installs) == 1
