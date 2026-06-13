"""Resolve commit authors to GitHub logins via the ``gh`` CLI (#54).

``git log`` only carries a name and an email, and the same person commits under
several of each. The GitHub API maps every commit to its author's login — the
one stable identity — so the dashboard can label committers by GitHub ID.

This is strictly best-effort: ``gh`` may be missing, unauthenticated, offline,
or the repo may have no GitHub remote. In every such case we return ``{}`` and
the caller falls back to the email/login heuristic in
:func:`agit.metrics.collect.resolve_committers`. Results are cached per repo
with a short TTL so the live dashboard's frequent refreshes don't re-hit the
API.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time

from agit.git import GitRepo

# git@github.com:owner/repo.git  or  https://github.com/owner/repo(.git)
_REMOTE_RE = re.compile(r"github\.com[:/]+(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")

_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_TTL_SECONDS = 300.0
_TIMEOUT_SECONDS = 20.0


def gh_available() -> bool:
    """True when the ``gh`` CLI is installed and authenticated."""
    if shutil.which("gh") is None:
        return False
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def resolve_logins(repo: GitRepo, *, refresh: bool = False) -> dict[str, str]:
    """Map full commit SHA → GitHub login for the repo's commits.

    Returns ``{}`` (never raises) when ``gh`` cannot answer, so callers can
    always fall back. Cached per repo for :data:`_TTL_SECONDS`."""
    key = str(repo.repo)
    now = time.monotonic()
    if not refresh:
        cached = _CACHE.get(key)
        if cached is not None and now - cached[0] < _TTL_SECONDS:
            return cached[1]

    logins = _fetch_logins(repo)
    # Cache even an empty result: it means gh is unavailable here, and we should
    # not retry on every 5-second dashboard refresh.
    _CACHE[key] = (now, logins)
    return logins


def _fetch_logins(repo: GitRepo) -> dict[str, str]:
    if shutil.which("gh") is None:
        return {}
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                "repos/{owner}/{repo}/commits?per_page=100",
                "--jq",
                ".[] | select(.author.login != null) | [.sha, .author.login] | @tsv",
            ],
            cwd=str(repo.repo),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    logins: dict[str, str] = {}
    for line in result.stdout.splitlines():
        sha, _, login = line.partition("\t")
        if sha and login:
            logins[sha.strip()] = login.strip()
    return logins


def commit_url_base(repo: GitRepo) -> str:
    """``https://github.com/<owner>/<repo>/commit/`` for the origin remote, or
    ``""`` when there is no GitHub remote — so dashboard log entries can link to
    the canonical commit on GitHub."""
    try:
        url = repo._run(["git", "remote", "get-url", "origin"], check=False).stdout.strip()
    except OSError:
        return ""
    match = _REMOTE_RE.search(url)
    if not match:
        return ""
    return f"https://github.com/{match.group('owner')}/{match.group('repo')}/commit/"


def _reset_cache_for_tests() -> None:
    _CACHE.clear()
