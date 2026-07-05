"""Resolve commit authors to GitHub logins via the ``gh`` CLI (#54).

``git log`` only carries a name and an email, and the same person commits under
several of each. The GitHub API maps every commit to its author's login — the
one stable identity — so the dashboard can label committers by GitHub ID.

This is strictly best-effort: ``gh`` may be missing, unauthenticated, offline,
or the repo may have no GitHub remote. In every such case we return ``{}`` and
the caller falls back to the email/login heuristic in
:func:`agitrack.metrics.collect.resolve_committers`. Results are cached per repo
with a short TTL so the live dashboard's frequent refreshes don't re-hit the
API.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time

from agitrack.git import GitRepo
from agitrack.proc import console_isolation_kwargs

# git@github.com:owner/repo.git  or  https://github.com/owner/repo(.git)
_REMOTE_RE = re.compile(r"github\.com[:/]+(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")

_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_TTL_SECONDS = 300.0
_TIMEOUT_SECONDS = 20.0
# Repos with an in-flight background login refresh, so a cold/stale cache spawns at
# most one gh crawl at a time (the live dashboard polls concurrently).
_INFLIGHT: set[str] = set()
_INFLIGHT_LOCK = threading.Lock()


def gh_status() -> str:
    """Whether the ``gh`` CLI is usable: ``"ok"`` (installed and authenticated),
    ``"missing"`` (not installed), or ``"unauthenticated"`` (installed but not
    logged in, or the auth check failed/timed out)."""
    if shutil.which("gh") is None:
        return "missing"
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            **console_isolation_kwargs(),  # keep gh off a console on Windows (proc.py)
        )
    except (OSError, subprocess.SubprocessError):
        return "unauthenticated"
    return "ok" if result.returncode == 0 else "unauthenticated"


def gh_available() -> bool:
    """True when the ``gh`` CLI is installed and authenticated."""
    return gh_status() == "ok"


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
    # not retry on every dashboard refresh.
    _CACHE[key] = (now, logins)
    return logins


def cached_logins(repo: GitRepo) -> dict[str, str]:
    """Non-blocking variant for the live dashboard's hot path: return whatever logins
    are cached right now (``{}`` when cold), and refresh the cache in the BACKGROUND
    when it is cold or stale. A page render therefore never waits on the paginated,
    networked ``gh`` crawl — the resolved logins simply appear on a later poll. The
    first paint labels committers by the email heuristic until then."""
    key = str(repo.repo)
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached is None or now - cached[0] >= _TTL_SECONDS:
        _refresh_logins_async(repo, key)
    return cached[1] if cached is not None else {}


def _refresh_logins_async(repo: GitRepo, key: str) -> None:
    with _INFLIGHT_LOCK:
        if key in _INFLIGHT:
            return  # a refresh is already running for this repo
        _INFLIGHT.add(key)

    def worker() -> None:
        try:
            logins = _fetch_logins(repo)
            _CACHE[key] = (time.monotonic(), logins)
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(key)

    threading.Thread(target=worker, daemon=True, name="agit-gh-logins").start()


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
            **console_isolation_kwargs(),  # keep gh off a console on Windows (proc.py)
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
    with _INFLIGHT_LOCK:
        _INFLIGHT.clear()
