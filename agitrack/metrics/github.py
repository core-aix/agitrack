"""Resolve commit authors to GitHub logins via the ``gh`` CLI (#54).

``git log`` only carries a name and an email, and the same person commits under
several of each. The GitHub API maps every commit to its author's login — the
one stable identity — so the dashboard can label committers by GitHub ID.

Resolution is per REF, not per repo. ``repos/{owner}/{repo}/commits`` lists the DEFAULT
BRANCH's commits and nothing else, so a commit that lives only on another branch got no
login at all: on a branch someone had just pulled, every contributor whose work was not
also on the default branch fell back to their raw git name. The same person then carried
two different labels depending on which branch you were looking at, and filtering by their
GitHub ID dropped their branch-only commits entirely — they read as missing. So the crawl
is also run for the ref being viewed (``?sha=<tip>``), and the two maps are merged.

This is strictly best-effort: ``gh`` may be missing, unauthenticated, offline,
or the repo may have no GitHub remote. In every such case we return ``{}`` and
the caller falls back to the email/login heuristic in
:func:`agitrack.metrics.collect.resolve_committers`. Results are cached per repo
with a short TTL so the live dashboard's frequent refreshes don't re-hit the
API.

What a crawl learned is also KEPT, in ``.agitrack/github-logins.json``, and only ever grows. A
commit's author never changes, and the label a person gets depends on it: without a login they
are shown by their bare git name, with one as ``login (Name)``. Held in memory alone, the map
was empty after every dashboard start (and every update restart) until the crawl landed, and a
crawl that timed out or ran offline replaced a good map with an empty one for five minutes, so
the same person flipped between the two forms from one visit to the next.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import json
import threading
import time
from pathlib import Path

from agitrack.fileio import STATE_DIRNAME, atomic_write_text, read_json_object
from agitrack.git import GitRepo
from agitrack.proc import UTF8_TEXT, console_isolation_kwargs

# git@github.com:owner/repo.git  or  https://github.com/owner/repo(.git)
_REMOTE_RE = re.compile(r"github\.com[:/]+(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")

# Keyed by (repo path, ref tip) — see the module header for why the ref matters. The default
# branch's crawl is stored under an empty tip, and every ref's map is merged over it.
_CACHE: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}
_TTL_SECONDS = 300.0
_TIMEOUT_SECONDS = 20.0
# The paginated crawl reads every commit of a ref, a page per 100: on a long history it outlasts
# the 20 s that suits a one-shot `gh auth status`, and a timeout learns nothing at all.
_CRAWL_TIMEOUT_SECONDS = 120.0
# Every login learned so far, per repo: sha -> login, loaded from and saved to the repo's
# `.agitrack/github-logins.json` (see the module header).
_KNOWN: dict[str, dict[str, str]] = {}
_KNOWN_LOCK = threading.Lock()
_KNOWN_FILE = "github-logins.json"
# Repos with an in-flight background login refresh, so a cold/stale cache spawns at
# most one gh crawl at a time (the live dashboard polls concurrently).
_INFLIGHT: set[tuple[str, str]] = set()
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
            **UTF8_TEXT,
            timeout=_TIMEOUT_SECONDS,
            **console_isolation_kwargs(),  # keep gh off a console on Windows (proc.py)
        )
    except (OSError, subprocess.SubprocessError):
        return "unauthenticated"
    return "ok" if result.returncode == 0 else "unauthenticated"


def gh_available() -> bool:
    """True when the ``gh`` CLI is installed and authenticated."""
    return gh_status() == "ok"


def _ref_tip(repo: GitRepo, ref: str) -> str:
    """The commit ``ref`` names, or ``""`` when there is nothing to ask GitHub about.

    A SHA rather than the branch name because the ref may be a remote-tracking one
    (``origin/feature-x``), which means nothing to the API — and because a sha is exactly what
    ``?sha=`` wants. An unpushed commit simply gets a 422 and an empty map, which is the same
    "fall back to the email heuristic" path as having no GitHub remote at all."""
    if not ref or ref == "HEAD":
        return ""
    try:
        return repo.rev_parse(ref)
    except Exception:
        return ""


def resolve_logins(repo: GitRepo, *, refresh: bool = False, ref: str = "") -> dict[str, str]:
    """Map full commit SHA → GitHub login for the repo's commits.

    The default branch's commits always; ``ref``'s as well when one is given (merged over the
    default branch's, so a commit on both keeps one answer). Returns ``{}`` (never raises) when
    ``gh`` cannot answer, so callers can always fall back. Cached per (repo, ref) for
    :data:`_TTL_SECONDS`."""
    tip = _ref_tip(repo, ref)
    _cached_or_fetch(repo, "", refresh=refresh)
    if tip:
        _cached_or_fetch(repo, tip, refresh=refresh)
    return _known(repo)


def _cached_or_fetch(repo: GitRepo, tip: str, *, refresh: bool) -> dict[str, str]:
    key = (str(repo.repo), tip)
    now = time.monotonic()
    if not refresh:
        cached = _CACHE.get(key)
        if cached is not None and now - cached[0] < _TTL_SECONDS:
            return cached[1]
    _store(repo, key, _fetch_logins(repo, tip))
    return _CACHE[key][1]


def cached_logins(repo: GitRepo, ref: str = "") -> dict[str, str]:
    """Non-blocking variant for the live dashboard's hot path: return whatever logins
    are cached right now (``{}`` when cold), and refresh the cache in the BACKGROUND
    when it is cold or stale. A page render therefore never waits on the paginated,
    networked ``gh`` crawl — the resolved logins simply appear on a later poll. The
    first paint labels committers by the email heuristic until then.

    ``ref`` is the branch being VIEWED. Its commits are crawled alongside the default branch's,
    because the default branch's crawl is all there used to be and it left every branch-only
    contributor unidentified (see the module header)."""
    tip = _ref_tip(repo, ref)
    known = _known(repo)  # what is known NOW, before any refresh below starts
    _cached_now(repo, "")
    if tip:
        _cached_now(repo, tip)
    return known


def _cached_now(repo: GitRepo, tip: str) -> None:
    key = (str(repo.repo), tip)
    cached = _CACHE.get(key)
    if cached is None or time.monotonic() - cached[0] >= _TTL_SECONDS:
        _refresh_logins_async(repo, key)


def _known_path(repo: GitRepo) -> Path:
    return Path(repo.repo) / STATE_DIRNAME / _KNOWN_FILE


def _known(repo: GitRepo) -> dict[str, str]:
    """Every login learned for this repo so far, read from disk the first time it is asked.
    The cached crawls are all folded into it, so this is the one map callers are handed."""
    key = str(repo.repo)
    with _KNOWN_LOCK:
        known = _KNOWN.get(key)
        if known is None:
            raw = read_json_object(_known_path(repo)).get("logins")
            known = {str(sha): str(who) for sha, who in raw.items() if sha and who} if isinstance(raw, dict) else {}
            _KNOWN[key] = known
        for (repo_key, _tip), (_at, logins) in list(_CACHE.items()):  # crawls land on other threads
            if repo_key == key:
                known.update(logins)
        return dict(known)


def _store(repo: GitRepo, key: tuple[str, str], logins: dict[str, str] | None) -> None:
    """Record one crawl's answer. A FAILED crawl (None) keeps what this ref already had, only
    restarting its TTL so a broken `gh` is not retried on every poll; an answer adds to the kept
    map and is saved when it taught us something new."""
    previous = _CACHE.get(key)
    if logins is None:
        _CACHE[key] = (time.monotonic(), previous[1] if previous is not None else {})
        return
    known = _known(repo)  # before this answer is cached, which would fold it in unsaved
    _CACHE[key] = (time.monotonic(), logins)
    if not logins or all(known.get(sha) == login for sha, login in logins.items()):
        return
    known.update(logins)
    with _KNOWN_LOCK:
        _KNOWN[str(repo.repo)] = known
    try:
        atomic_write_text(_known_path(repo), json.dumps({"logins": known}, sort_keys=True))
    except OSError:
        pass  # a read-only repo still gets the in-memory map; the next start just crawls again


def _refresh_logins_async(repo: GitRepo, key: tuple[str, str]) -> None:
    with _INFLIGHT_LOCK:
        if key in _INFLIGHT:
            return  # a refresh is already running for this repo/ref
        _INFLIGHT.add(key)

    def worker() -> None:
        try:
            _store(repo, key, _fetch_logins(repo, key[1]))
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(key)

    threading.Thread(target=worker, daemon=True, name="agit-gh-logins").start()


def _fetch_logins(repo: GitRepo, tip: str = "") -> dict[str, str] | None:
    """Crawl one ref's commits. ``tip`` empty means the repo's DEFAULT branch (the API's own
    default); a sha narrows the walk to that commit and its ancestors. None when the crawl
    could not run or failed (no `gh`, offline, a timeout, an unpushed ref), which is not the
    same as an answer: it must not wipe out what an earlier crawl learned."""
    if shutil.which("gh") is None:
        return None
    # The sha comes from `git rev-parse`, so it is a hex object id and not user text; the
    # endpoint is built by format, never by shell.
    endpoint = "repos/{owner}/{repo}/commits?per_page=100"
    if tip:
        endpoint += f"&sha={tip}"
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                endpoint,
                "--jq",
                ".[] | select(.author.login != null) | [.sha, .author.login] | @tsv",
            ],
            cwd=str(repo.repo),
            capture_output=True,
            **UTF8_TEXT,
            timeout=_CRAWL_TIMEOUT_SECONDS,
            **console_isolation_kwargs(),  # keep gh off a console on Windows (proc.py)
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
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
    with _KNOWN_LOCK:
        _KNOWN.clear()
    with _INFLIGHT_LOCK:
        _INFLIGHT.clear()
