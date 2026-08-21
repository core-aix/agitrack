"""Which remote-tracking branches still EXIST on the remote, for the dashboard's selector.

Git never removes a remote-tracking ref on a plain ``git fetch`` — only ``--prune`` does, and
most people never pass it — so ``refs/remotes/origin/*`` accumulates every branch that was ever
pushed, including the ones merged and deleted months ago. Listing those refs therefore lists
branches that no longer exist anywhere but in this clone, which is what the branch selector
started doing the moment it learned to offer fetched branches at all.

Only the remote can answer "is this branch still there", so this asks it — once per remote per
:data:`_TTL_SECONDS`, on a BACKGROUND thread, exactly like the GitHub login resolution the
dashboard already does (:mod:`agitrack.metrics.github`). A page render never waits on it: the
first paint shows what the clone has, and the stale entries drop off a poll or two later.

**Not knowing is never a reason to hide a branch.** A remote that is unreachable, needs a
credential we do not have, or simply is not configured leaves that remote UNCHECKED, and an
unchecked remote's branches are all shown. Hiding a branch because the network was down would be
a worse failure than showing one that has been deleted.

And nothing here ever PRUNES. Removing the user's refs is a change to their repository, which a
page that exists to show them things has no business making; the refs stay exactly as git left
them and only the display is filtered.
"""

from __future__ import annotations

import threading
import time

from agitrack.git import GitRepo

# How long a remote's branch list is trusted. One `ls-remote` is a single round trip with a small
# payload, but it IS network on a page that polls, so it is cached as long as the GitHub login
# crawl beside it.
_TTL_SECONDS = 300.0
_LS_REMOTE_TIMEOUT = 20.0

# (repo path, remote) -> (monotonic stamp, branch names or None when the remote could not be asked)
_CACHE: dict[tuple[str, str], tuple[float, set[str] | None]] = {}
# Remotes with a refresh already in flight, so a polling dashboard spawns at most one per remote.
_INFLIGHT: set[tuple[str, str]] = set()
_LOCK = threading.Lock()


def live_branches(repo: GitRepo, remotes: list[str]) -> dict[str, set[str]]:
    """Branch names each remote still has, for the remotes that could be ASKED.

    Non-blocking: returns whatever is cached now (empty when cold) and refreshes stale or missing
    entries in the background. A remote absent from the result is one nothing is known about —
    the caller must leave its branches alone.
    """
    known: dict[str, set[str]] = {}
    now = time.monotonic()
    for remote in remotes:
        key = (str(repo.repo), remote)
        cached = _CACHE.get(key)
        if cached is None or now - cached[0] >= _TTL_SECONDS:
            _refresh_async(repo, key)
        if cached is not None and cached[1] is not None:
            known[remote] = cached[1]
    return known


def prune_stale(names: list[str], live: dict[str, set[str]]) -> list[str]:
    """*names* (``origin/feature-x``) with the ones their remote no longer has removed.

    Matching is against the actual remote names rather than "everything before the first slash",
    so a branch whose own name contains slashes (``origin/release/2.1``) is resolved correctly.
    """
    if not live:
        return list(names)
    kept: list[str] = []
    for name in names:
        stale = False
        for remote, branches in live.items():
            prefix = f"{remote}/"
            if name.startswith(prefix) and name[len(prefix) :] not in branches:
                stale = True
                break
        if not stale:
            kept.append(name)
    return kept


def _refresh_async(repo: GitRepo, key: tuple[str, str]) -> None:
    with _LOCK:
        if key in _INFLIGHT:
            return
        _INFLIGHT.add(key)

    def worker() -> None:
        try:
            names = repo.remote_head_branches(key[1], timeout=_LS_REMOTE_TIMEOUT)
            # Cached even when None: an unreachable remote must not be re-dialled on every poll.
            _CACHE[key] = (time.monotonic(), names)
        except Exception:
            _CACHE[key] = (time.monotonic(), None)
        finally:
            with _LOCK:
                _INFLIGHT.discard(key)

    threading.Thread(target=worker, daemon=True, name="agit-remote-branches").start()


def _reset_cache_for_tests() -> None:
    _CACHE.clear()
    with _LOCK:
        _INFLIGHT.clear()
