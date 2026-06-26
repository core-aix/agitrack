"""Tests for agitrack/summaries/model_select.py.

All subprocess calls are mocked — no real backends are needed.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from agitrack.summaries.model_select import (
    _list_claude_models,
    _list_opencode_models,
    list_available_models,
    smallest_model,
)


# ---------------------------------------------------------------------------
# list_available_models — dispatch
# ---------------------------------------------------------------------------


def test_list_available_models_unknown_backend_returns_empty():
    assert list_available_models("foo") == []
    assert list_available_models("") == []


def test_list_available_models_claude_dispatches():
    with patch("agitrack.summaries.model_select.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = list_available_models("claude")
    assert result == ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-8"]


def test_list_available_models_opencode_dispatches():
    stdout = "gpt-4o  GPT 4o\ngpt-4-mini  GPT 4 mini\n"
    with patch("agitrack.summaries.model_select.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")
        result = list_available_models("opencode")
    assert result == ["gpt-4o", "gpt-4-mini"]


# ---------------------------------------------------------------------------
# _list_claude_models
# ---------------------------------------------------------------------------


def test_list_claude_models_success():
    with patch("agitrack.summaries.model_select.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="Usage: claude ...", stderr="")
        result = _list_claude_models()
    assert "claude-haiku-4-5-20251001" in result
    assert len(result) == 3


def test_list_claude_models_nonzero_returncode():
    with patch("agitrack.summaries.model_select.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        assert _list_claude_models() == []


def test_list_claude_models_oserror():
    with patch("agitrack.summaries.model_select.subprocess.run", side_effect=OSError("no claude")):
        assert _list_claude_models() == []


def test_list_claude_models_timeout():
    with patch(
        "agitrack.summaries.model_select.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["claude"], 5),
    ):
        assert _list_claude_models() == []


# ---------------------------------------------------------------------------
# _list_opencode_models
# ---------------------------------------------------------------------------


def test_list_opencode_models_parses_lines():
    stdout = (
        "# Available models\n"
        "-----------------\n"
        "gpt-4o   GPT 4 Omni\n"
        "gpt-4-mini\n"
        "\n"
        "# comment line\n"
        "claude-sonnet  Claude Sonnet\n"
    )
    with patch("agitrack.summaries.model_select.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")
        result = _list_opencode_models()
    assert result == ["gpt-4o", "gpt-4-mini", "claude-sonnet"]


def test_list_opencode_models_nonzero_returncode():
    with patch("agitrack.summaries.model_select.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="err")
        assert _list_opencode_models() == []


def test_list_opencode_models_oserror():
    with patch("agitrack.summaries.model_select.subprocess.run", side_effect=OSError):
        assert _list_opencode_models() == []


def test_list_opencode_models_timeout():
    with patch(
        "agitrack.summaries.model_select.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["opencode"], 5),
    ):
        assert _list_opencode_models() == []


def test_list_opencode_models_empty_stdout():
    with patch("agitrack.summaries.model_select.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        assert _list_opencode_models() == []


# ---------------------------------------------------------------------------
# smallest_model
# ---------------------------------------------------------------------------


def test_smallest_model_returns_haiku_for_claude():
    models = ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-8"]
    assert smallest_model("claude", models) == "claude-haiku-4-5-20251001"


def test_smallest_model_case_insensitive():
    assert smallest_model("claude", ["Claude-Haiku-X"]) == "Claude-Haiku-X"


def test_smallest_model_non_claude_returns_none():
    assert smallest_model("opencode", ["gpt-4o", "gpt-4-mini"]) is None


def test_smallest_model_no_haiku_in_list_returns_none():
    assert smallest_model("claude", ["claude-sonnet-4-6", "claude-opus-4-8"]) is None


def test_smallest_model_empty_list_returns_none():
    assert smallest_model("claude", []) is None
