from __future__ import annotations

import subprocess

from agitrack.proc import UTF8_TEXT, console_isolation_kwargs, resolve_subprocess_command


def list_available_models(backend_name: str) -> list[str]:
    """The models the summarizer can use for the given backend (smallest tier first
    where we know the ordering, i.e. Claude). Empty when the backend's CLI can't be
    queried — callers then fall back to free-text model entry."""
    if backend_name == "opencode":
        return _list_opencode_models()
    if backend_name == "claude":
        return _list_claude_models()
    if backend_name == "codex":
        return _list_codex_models()
    return []


def compatible_summarization_model(backend_name: str, model: str | None) -> str | None:
    """The configured summarization model to actually hand this backend — or None to let the
    backend pick its own default when the configured id belongs to a DIFFERENT backend.

    ``summarization_model`` is a single global setting, but a model id is provider-specific:
    OpenCode addresses models as ``provider/model`` (e.g. ``anthropic/claude-haiku-4-5``), while
    the Claude CLI uses bare ids (``claude-haiku-4-5-20251001``, ``haiku``). A session running
    the OpenCode backend therefore can't use a Claude model id: ``opencode run --model
    claude-haiku-4-5-20251001`` exits non-zero and the summary fails outright. When the coding
    backend and the configured model don't match, drop the model and fall back to the backend's
    default rather than failing every summary. (Cross-backend summarization is impossible anyway —
    the summarizer always runs the SAME backend as the session, so a Claude id under OpenCode is
    simply misconfiguration, not a request to use Claude.)"""
    if not model:
        return None
    has_provider = "/" in model
    if backend_name == "opencode":
        # OpenCode needs a provider-qualified id; a bare (Claude-style) id is not its own.
        return model if has_provider else None
    if backend_name == "claude":
        # The Claude CLI needs a bare id; a provider/model id is an OpenCode-style id.
        return model if not has_provider else None
    if backend_name == "codex":
        # Codex also needs a bare id, so the provider check alone cannot separate it from
        # Claude — both spell their models the same way. Without a second test, a global
        # ``summarization_model`` of ``claude-haiku-4-5-20251001`` was handed straight to
        # ``codex -m``, which rejects it, and EVERY summary failed after a backend switch.
        # Rejecting Claude-shaped ids specifically (rather than allow-listing Codex's, which
        # would break the moment OpenAI ships a new model family) keeps an unknown-but-valid
        # Codex id working while catching the one realistic misconfiguration.
        if has_provider or _looks_like_a_claude_model(model):
            return None
        return model
    return model


# Claude's CLI accepts both full ids (``claude-haiku-4-5-20251001``) and the short tier aliases,
# so both spellings have to be recognized as "not a Codex model".
_CLAUDE_MODEL_MARKERS = ("claude", "haiku", "sonnet", "opus")


def _looks_like_a_claude_model(model: str) -> bool:
    return any(marker in model.lower() for marker in _CLAUDE_MODEL_MARKERS)


def smallest_model(backend_name: str, models: list[str]) -> str | None:
    """The smallest / cheapest model to default the summarizer to.

    For Claude that's the Haiku tier and for Codex the ``-mini`` tier — both name their small
    model in the id, so the ordering is readable rather than guessed. OpenCode fronts arbitrary
    providers whose ids carry no size convention, so no default is presumed there."""
    if backend_name == "claude":
        for model in models:
            if "haiku" in model.lower():
                return model
    if backend_name == "codex":
        for model in models:
            if model.lower().endswith("-mini"):
                return model
    return None


def _list_opencode_models() -> list[str]:
    try:
        result = subprocess.run(
            resolve_subprocess_command(["opencode", "models"]),
            **UTF8_TEXT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
            **console_isolation_kwargs(),  # keep the backend CLI off the host console (proc.py)
        )
        if result.returncode != 0:
            return []
        models = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("-"):
                models.append(line.split()[0] if " " in line else line)
        return models
    except (subprocess.TimeoutExpired, OSError):
        return []


def _list_codex_models() -> list[str]:
    """Codex's available models, smallest tier first.

    Read from the model roster Codex itself caches (``$CODEX_HOME/models_cache.json``) rather
    than shelled out for: Codex's CLI has no `models` subcommand to ask, and the cache is the
    same list its own picker shows.

    Filtered on the roster's OWN flags — ``visibility == "list"`` and ``supported_in_api`` — not
    on a guess about the id. Matching "review" in the name looked like it dropped the
    non-selectable entries but missed them: on a real roster it still offered ``gpt-5.6-sol-wm``
    (``visibility: "hide"``, ``supported_in_api: false``), and picking it fails every summary.
    """
    import json
    import os
    from pathlib import Path

    home = os.environ.get("CODEX_HOME")
    cache = (Path(home).expanduser() if home else Path.home() / ".codex") / "models_cache.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    names = []
    for entry in models:
        if not isinstance(entry, dict):
            continue
        if entry.get("visibility") not in (None, "list"):
            continue
        if entry.get("supported_in_api") is False:
            continue
        model_id = entry.get("id") or entry.get("slug")
        if isinstance(model_id, str) and model_id.strip():
            names.append(model_id.strip())
    # Smallest first, matching the Claude list's contract (the caller labels [0] "recommended"
    # only via smallest_model, but an ordered list keeps the menu sensible either way).
    return sorted(names, key=lambda name: (not name.endswith("-mini"), name))


def _list_claude_models() -> list[str]:
    try:
        result = subprocess.run(
            resolve_subprocess_command(["claude", "--help"]),
            **UTF8_TEXT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
            **console_isolation_kwargs(),  # keep the backend CLI off the host console (proc.py)
        )
        if result.returncode != 0:
            return []
        # aGiTrack's curated Claude tiers, smallest (Haiku) → largest (Opus).
        return ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-8"]
    except (subprocess.TimeoutExpired, OSError):
        return []
