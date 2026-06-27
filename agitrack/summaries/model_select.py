from __future__ import annotations

import subprocess

from agitrack.proc import console_isolation_kwargs, resolve_subprocess_command


def list_available_models(backend_name: str) -> list[str]:
    """The models the summarizer can use for the given backend (smallest tier first
    where we know the ordering, i.e. Claude). Empty when the backend's CLI can't be
    queried — callers then fall back to free-text model entry."""
    if backend_name == "opencode":
        return _list_opencode_models()
    if backend_name == "claude":
        return _list_claude_models()
    return []


def smallest_model(backend_name: str, models: list[str]) -> str | None:
    """The smallest / cheapest model to default the summarizer to. For Claude that's
    the Haiku tier; for other backends we don't presume a size ordering, so there is
    no recommended default."""
    if backend_name == "claude":
        for model in models:
            if "haiku" in model.lower():
                return model
    return None


def _list_opencode_models() -> list[str]:
    try:
        result = subprocess.run(
            resolve_subprocess_command(["opencode", "models"]),
            text=True,
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


def _list_claude_models() -> list[str]:
    try:
        result = subprocess.run(
            resolve_subprocess_command(["claude", "--help"]),
            text=True,
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
