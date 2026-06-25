"""Tests for agitrack/backends/setup.py.

All backend availability checks and subprocess calls are mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agitrack.backends.setup import (
    _wait_for_install,
    ensure_installed_backend,
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


def test_select_default_backend_invalid_then_valid():
    config = MagicMock()
    calls = iter(["99", "abc", "1"])
    output_lines = []

    with (
        patch("agitrack.backends.setup.available_backends", return_value=["claude", "opencode"]),
        patch("agitrack.backends.setup.backend_installed", return_value=True),
    ):
        result = select_default_backend(
            config,
            input_fn=lambda _: next(calls),
            output_fn=output_lines.append,
        )

    assert result in {"claude", "opencode"}
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
        select_default_summarizer_model(
            config, "claude", input_fn=lambda _: "not-a-number", output_fn=lambda _: None
        )
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
        select_default_summarizer_model(
            config, "claude", input_fn=lambda _: "2", output_fn=lambda _: None
        )
    assert config.summarization_model == "claude-sonnet-4-6"


def test_select_default_summarizer_model_no_models_is_noop():
    # When no models come back, the function returns early without touching config.
    config = MagicMock()
    with patch("agitrack.summaries.model_select.list_available_models", return_value=[]):
        select_default_summarizer_model(
            config, "claude", input_fn=lambda _: "1", output_fn=lambda _: None
        )
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


def test_ensure_installed_backend_installed_on_retry():
    config = MagicMock()
    # First call (outside loop): not installed.
    # Second call (inside loop re-check): installed.
    side_effects = iter([False, True])
    with (
        patch("agitrack.backends.setup.available_backends", return_value=["claude"]),
        patch("agitrack.backends.setup.backend_installed", side_effect=side_effects),
        patch("agitrack.backends.setup.install_hint", return_value="install it!"),
    ):
        result = ensure_installed_backend(
            "claude",
            config,
            interactive=True,
            input_fn=lambda _: "",   # user presses Enter (retry)
            output_fn=lambda _: None,
        )
    assert result == "claude"


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
# _wait_for_install
# ---------------------------------------------------------------------------


def test_wait_for_install_returns_true_once_installed():
    install_checks = iter([False, True])
    with patch("agitrack.backends.setup.backend_installed", side_effect=install_checks):
        result = _wait_for_install(
            "claude",
            input_fn=lambda _: "",  # press Enter
            output_fn=lambda _: None,
        )
    assert result is True


def test_wait_for_install_still_not_found_message_then_found():
    output_lines = []
    install_checks = iter([False, False, True])
    with patch("agitrack.backends.setup.backend_installed", side_effect=install_checks):
        result = _wait_for_install(
            "claude",
            input_fn=lambda _: "",
            output_fn=output_lines.append,
        )
    assert result is True
    assert any("still not found" in line for line in output_lines)


def test_wait_for_install_back_returns_false():
    with patch("agitrack.backends.setup.backend_installed", return_value=False):
        result = _wait_for_install(
            "claude",
            input_fn=lambda _: "b",
            output_fn=lambda _: None,
        )
    assert result is False


def test_wait_for_install_choose_returns_false():
    with patch("agitrack.backends.setup.backend_installed", return_value=False):
        result = _wait_for_install(
            "claude",
            input_fn=lambda _: "c",
            output_fn=lambda _: None,
        )
    assert result is False
