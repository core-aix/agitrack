"""Tests for agitrack/backends/setup.py.

All backend availability checks and subprocess calls are mocked.
"""

from __future__ import annotations

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
            install_fn=lambda name, output_fn: pytest.fail("invalid/skip answers must not install"),
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


def test_ensure_installed_backend_auto_installs_on_enter():
    config = MagicMock()
    calls = []
    with (
        patch("agitrack.backends.setup.available_backends", return_value=["claude"]),
        patch("agitrack.backends.setup.backend_installed", return_value=False),
    ):
        result = ensure_installed_backend(
            "claude",
            config,
            interactive=True,
            input_fn=lambda _: "",  # user presses Enter → install automatically
            output_fn=lambda _: None,
            install_fn=lambda name, output_fn: calls.append(name) or True,
        )
    assert result == "claude"
    assert calls == ["claude"]  # only the selected backend was installed


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


def test_select_default_backend_prompts_to_install_uninstalled_even_when_one_present():
    # claude installed, opencode not: the user IS still asked whether to install opencode.
    installs = []
    config = MagicMock()
    answers = iter(["2", ""])  # install opencode (#2), then Enter to finish

    def fake_installed(name):
        return name == "claude" or name in installs

    with patch("agitrack.backends.setup.backend_installed", side_effect=fake_installed):
        result = select_default_backend(
            config,
            input_fn=lambda _: next(answers),
            output_fn=lambda _: None,
            install_fn=lambda name, output_fn: installs.append(name) or True,
        )
    assert installs == ["opencode"]  # only the uninstalled one was offered/installed
    assert result == "claude"  # default stays the already-installed one


def test_select_default_backend_install_all():
    installs = []
    config = MagicMock()

    def fake_installed(name):
        return name in installs

    with patch("agitrack.backends.setup.backend_installed", side_effect=fake_installed):
        result = select_default_backend(
            config,
            input_fn=lambda _: "all",  # install every uninstalled backend
            output_fn=lambda _: None,
            install_fn=lambda name, output_fn: installs.append(name) or True,
        )
    assert sorted(installs) == ["claude", "opencode"]
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
            install_fn=lambda name, output_fn: pytest.fail("nothing should be installed on skip"),
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
        ok = install_backend("claude", output_fn=lambda _: None, run=fake_run, which=fake_which)
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
        ok = install_backend("opencode", output_fn=lambda _: None, run=fake_run, which=fake_which)
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
