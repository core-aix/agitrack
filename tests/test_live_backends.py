"""Opt-in smoke tests against the REAL backend CLIs. Deselected by default.

Every other test in this suite mocks the backend. That is right for logic, and it is blind to
the one thing mocks cannot see: **the backend CLI changing its output format under us.** A new
Claude Code release renaming a JSON field, or a Codex/OpenCode version changing its event shape,
breaks aGiTrack for every user of that backend while the whole mocked suite stays green — the
mocks assert the shape we *believed* was true when we wrote them.

Run them deliberately::

    pytest -m live                 # every backend, whichever are installed
    pytest -m live -k codex        # one of them

They are excluded from the default run (and therefore from CI) because they need the backend
installed and authenticated, cost real tokens, and take seconds to tens of seconds. Each is
skipped when its binary is absent, so `-m live` on a machine with only one backend runs only
that one.

Deliberately minimal: these prove the CONTRACT aGiTrack depends on — that a bare run returns
text, an exit code, a model name and non-zero token counts, in the fields the parser reads.
Behaviour beyond that belongs in the mocked suite, where it is fast and deterministic.
"""

from __future__ import annotations

import shutil

import pytest

from agitrack.backends.claude import ClaudeBackend
from agitrack.backends.codex import CodexBackend
from agitrack.backends.opencode import OpenCodeBackend

pytestmark = pytest.mark.live

_BACKENDS = {"claude": ClaudeBackend, "codex": CodexBackend, "opencode": OpenCodeBackend}

# Cheapest tier per backend, so a smoke test never bills a frontier model. None = the CLI's own
# default (OpenCode fronts arbitrary providers, so there is no id that is valid everywhere).
_SMOKE_MODELS = {"claude": "claude-haiku-4-5-20251001", "codex": "gpt-5.4-mini", "opencode": None}


def _backend_or_skip(name, tmp_path):
    if shutil.which(name) is None:
        pytest.skip(f"{name} is not installed on this machine")
    return _BACKENDS[name](tmp_path)


@pytest.mark.parametrize("backend_name", sorted(_BACKENDS))
def test_a_bare_run_returns_usable_text(backend_name, tmp_path):
    # The summarizer's contract. A bare run must come back with a non-empty answer and a clean
    # exit — this is the call every commit summary is built on.
    backend = _backend_or_skip(backend_name, tmp_path)

    result = backend.run(
        "Reply with exactly the word: pineapple",
        model=_SMOKE_MODELS[backend_name],
        session_id=None,
        bare=True,
        system_prompt="Follow the instruction exactly. Output only what is asked, no preamble.",
        timeout_seconds=120,
    )

    assert result.exit_code == 0, f"{backend_name} bare run failed: {result!r}"
    assert result.final_response.strip(), "a bare run returned no text at all"
    assert "pineapple" in result.final_response.lower()


@pytest.mark.parametrize("backend_name", sorted(_BACKENDS))
def test_a_bare_run_reports_its_model_and_token_usage(backend_name, tmp_path):
    # aGiTrack records the model and per-turn token counts on every commit. If the CLI renames
    # or restructures these fields, the commits silently start carrying zeros — which is
    # exactly the kind of drift only a live call can catch.
    backend = _backend_or_skip(backend_name, tmp_path)

    result = backend.run(
        "Say OK.",
        model=_SMOKE_MODELS[backend_name],
        session_id=None,
        bare=True,
        system_prompt="Reply with just: OK",
        timeout_seconds=120,
    )

    assert result.exit_code == 0
    assert result.backend == backend_name
    assert result.model, "no model reported — the commit metadata would say 'unknown'"
    assert result.tokens.output > 0, "no output tokens reported — per-turn accounting would be zero"
    assert result.tokens.input > 0, "no input tokens reported"


@pytest.mark.parametrize("backend_name", sorted(_BACKENDS))
def test_the_update_command_names_the_real_binary(backend_name, tmp_path):
    # aGiTrack offers to update the backend in place. Naming the wrong binary would run an
    # unrelated program on the user's machine, so pin the head of the command.
    backend = _backend_or_skip(backend_name, tmp_path)

    command = backend.update_command()

    assert command and command[0] == backend_name
