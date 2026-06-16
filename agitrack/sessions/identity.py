"""Resolve the contributor's identity for labelling shared sessions.

Prefers the GitHub login (the stable identity teammates recognise) via ``gh``;
falls back to a slug of the git ``user.name`` when ``gh`` is unavailable. Always
returns a filesystem/ref-safe slug, never raises.
"""

from __future__ import annotations

import re
import shutil
import subprocess

from agitrack.git import GitRepo

_TIMEOUT_SECONDS = 20.0
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(value: str) -> str:
    """A safe path/ref segment: alnum, dot, dash, underscore; no traversal."""
    cleaned = _SLUG_RE.sub("-", (value or "").strip()).strip("-.")
    cleaned = cleaned.replace("..", "-")
    return cleaned or "anonymous"


def _gh_login() -> str | None:
    if shutil.which("gh") is None:
        return None
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    login = result.stdout.strip()
    return login if result.returncode == 0 and login else None


def _git_name(repo: GitRepo | None) -> str | None:
    if repo is None:
        return None
    name = repo._run(["git", "config", "user.name"], check=False).stdout.strip()
    return name or None


def github_login(repo: GitRepo | None = None) -> str:
    """Best-effort GitHub login (slugged); falls back to the git user name, then
    ``anonymous``. Never raises."""
    return slug(_gh_login() or _git_name(repo) or "anonymous")
