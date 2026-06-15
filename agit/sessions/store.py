"""The shared-session store: a history-free git ref that keeps only the latest
copy of each contributor's shared sessions, scoped to this repo.

Layout under ``refs/agit/shared-sessions``::

    <repo-fingerprint>/<github-id>/<session-name>/transcript.jsonl
    <repo-fingerprint>/<github-id>/<session-name>/manifest.json

``<repo-fingerprint>`` is the repo's root-commit SHA (clone-stable). Listing is
filtered to the current repo's fingerprint, so another repo's sessions can never
surface here even if the ref somehow accumulated them. Every write rebuilds the
whole tree into a single parent-less commit (see
:meth:`GitRepo.commit_tree_orphan`), so old copies become unreferenced and the
ref never grows a history.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field

from agit.git import GitRepo
from agit.sessions.identity import slug

REF = "refs/agit/shared-sessions"
DEFAULT_KEEP = 5  # most-recent shared sessions retained per contributor
# Throttle remote fetches when the dashboard polls. Your OWN shared sessions land
# in the local ref directly (no fetch needed); this only pulls collaborators'
# newly-shared sessions, so a long interval is fine and keeps load off the remote.
_FETCH_TTL = 300.0
_fetch_at: dict[str, float] = {}


@dataclass
class SharedEntry:
    github_id: str
    name: str
    manifest: dict

    @property
    def display(self) -> str:
        return f"{self.github_id}/{self.name}"


@dataclass
class PublishResult:
    remote: bool  # whether a git remote exists at all
    pushed: bool  # whether the push succeeded
    pruned: int = 0  # how many of the contributor's stale sessions were removed
    error: str = ""


def _is_stale_lease(error: str) -> bool:
    """Whether a failed push was *rejected because the remote moved* (a lost race
    or a never-fetched remote ref) — the only failure publish() retries after a
    sync. Auth/network errors don't match these markers, so they fail fast instead
    of looping. Git emits 'stale info' for a broken ``--force-with-lease`` and
    'fetch first'/'non-fast-forward'/'[rejected]' for an ordinary rejection."""
    text = error.lower()
    return any(marker in text for marker in ("stale info", "fetch first", "non-fast-forward", "rejected"))


@dataclass
class SharedSessionStore:
    repo: GitRepo  # the BASE repo (owns the remote and the shared object db)
    ref: str = REF
    _fingerprint: str | None = field(default=None, repr=False)

    def fingerprint(self) -> str:
        if self._fingerprint is None:
            self._fingerprint = self.repo.root_commit() or "no-root"
        return self._fingerprint

    def _prefix(self) -> str:
        return f"{self.fingerprint()}/"

    # --- reading -----------------------------------------------------------

    def entries(self) -> list[SharedEntry]:
        """Shared sessions for *this* repo, newest first (by manifest ``updated``)."""
        prefix = self._prefix()
        seen: dict[tuple[str, str], SharedEntry] = {}
        for path in self.repo.read_tree_paths(self.ref):
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix) :].split("/")
            if len(rest) < 3:
                continue
            key = (rest[0], rest[1])
            if key in seen:
                continue
            seen[key] = SharedEntry(github_id=key[0], name=key[1], manifest=self._manifest(*key))
        return sorted(seen.values(), key=lambda e: e.manifest.get("updated", 0), reverse=True)

    def _manifest(self, github_id: str, name: str) -> dict:
        raw = self.repo.read_ref_blob(self.ref, f"{self._prefix()}{github_id}/{name}/manifest.json")
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    def read_transcript(
        self, entry: SharedEntry, *, timeout: float | None = None, cancel: "threading.Event | None" = None
    ) -> str | None:
        path = f"{self._prefix()}{entry.github_id}/{entry.name}/transcript.jsonl"
        blob = self.repo.read_ref_blob(self.ref, path)
        if blob is None and self.repo.remote_exists():
            # The listing fetch pulls only the small manifests (see `fetch`), so a
            # large transcript may not be local yet — fetch the full ref now that
            # the user has actually chosen this session, then read it. ``timeout``
            # bounds this (potentially large) fetch; ``cancel`` lets the user stop it
            # outright (the git process is killed, not left running).
            self.repo.fetch_ref(f"+{self.ref}:{self.ref}", timeout=timeout, cancel=cancel)
            blob = self.repo.read_ref_blob(self.ref, path)
        return blob

    # --- writing -----------------------------------------------------------

    def _commit(self, entries: dict[str, str], message: str) -> None:
        old = self.repo.ref_sha(self.ref)
        tree = self.repo.write_tree_from(entries)
        sha = self.repo.commit_tree_orphan(tree, message)
        self.repo.update_ref(self.ref, sha)
        # Reclaim the just-orphaned previous snapshot's objects right away — large
        # transcript blobs shouldn't linger until git's auto-gc (issue #55).
        if old and old != sha:
            self.repo.delete_orphaned_objects(old)

    def _add_session(self, github_id: str, name: str, transcript: str, manifest: dict) -> None:
        base = f"{self._prefix()}{github_id}/{name}/"
        entries = {k: v for k, v in self.repo.read_tree_paths(self.ref).items() if not k.startswith(base)}
        entries[base + "transcript.jsonl"] = self.repo.write_blob(transcript)
        entries[base + "manifest.json"] = self.repo.write_blob(json.dumps(manifest, indent=2, sort_keys=True))
        self._commit(entries, f"agit: share session {github_id}/{name}")

    def prune_own_stale(self, github_id: str, *, keep: int = DEFAULT_KEEP) -> int:
        """Drop all but the most-recent ``keep`` sessions belonging to ``github_id``
        (a contributor only prunes their own). Returns the number removed."""
        gid = slug(github_id)
        mine = [e for e in self.entries() if e.github_id == gid]  # newest first
        stale = mine[keep:]
        if not stale:
            return 0
        prefix = self._prefix()
        drop_bases = [f"{prefix}{gid}/{e.name}/" for e in stale]
        kept = {
            path: blob
            for path, blob in self.repo.read_tree_paths(self.ref).items()
            if not any(path.startswith(base) for base in drop_bases)
        }
        self._commit(kept, f"agit: prune {len(stale)} stale session(s) for {gid}")
        return len(stale)

    # --- sync --------------------------------------------------------------

    def fetch(self, *, timeout: float | None = None, cancel: "threading.Event | None" = None) -> bool:
        """Pull the latest shared ref from the remote (best-effort).

        Fetches only the small manifests (a blob-size filter skips the large
        transcripts) so listing which sessions exist is fast; the transcript of a
        chosen session is fetched on demand by :meth:`read_transcript`. Falls back
        to a full fetch when the remote doesn't support partial fetch. An optional
        ``timeout`` bounds each underlying git fetch so a stalled network call on
        bad internet can't run unbounded; ``cancel`` (an Event) stops it at once."""
        if not self.repo.remote_exists():
            return False
        refspec = f"+{self.ref}:{self.ref}"
        if cancel is not None and cancel.is_set():
            return False  # already cancelled: don't even start
        if self.repo.fetch_ref(refspec, filter_blobs="blob:limit=16k", timeout=timeout, cancel=cancel):
            return True
        if cancel is not None and cancel.is_set():
            return False  # cancelled during the filtered fetch: don't retry
        return self.repo.fetch_ref(refspec, timeout=timeout, cancel=cancel)

    def _is_session_snapshot(self, commit_sha: str) -> bool:
        # A shared-session snapshot commit is parent-less (an orphan we wrote) and
        # its tree has the manifest+transcript shape. Both checks together avoid
        # ever matching an unrelated orphan commit (e.g. an abandoned turn branch).
        if self.repo.parents(commit_sha):
            return False
        paths = self.repo.read_tree_paths(commit_sha)
        return any(p.endswith("/manifest.json") for p in paths) and any(p.endswith("/transcript.jsonl") for p in paths)

    def cleanup_orphans(self, *, fetch: bool = True) -> int:
        """Delete every dangling *shared-session* snapshot left by past rewrites —
        immediately, rather than waiting for git's auto-gc. Only commits that are
        genuinely shared-session snapshots are touched (see ``_is_session_snapshot``);
        other unreachable objects are left alone. Returns the count of objects removed.
        Best-effort; never raises. ``fetch`` first (default) syncs the local ref to the
        remote so a snapshot that's still the remote's tip is never mistaken for junk;
        pass ``fetch=False`` to stay offline (e.g. on exit)."""
        try:
            if fetch:
                self.fetch()
            current = self.repo.ref_sha(self.ref)
            removed = 0
            for sha in self.repo.unreachable_commits():
                if sha == current or not self._is_session_snapshot(sha):
                    continue
                removed += self.repo.delete_orphaned_objects(sha)
            return removed
        except Exception:
            return 0

    def fetch_throttled(self) -> None:
        """Best-effort fetch at most once per TTL — for pollers (the dashboard)
        that want others' newly-shared sessions without hammering the remote."""
        key = str(self.repo.repo)
        now = time.monotonic()
        if now - _fetch_at.get(key, 0.0) < _FETCH_TTL:
            return
        _fetch_at[key] = now
        self.fetch()

    def unshare(self, github_id: str, name: str) -> PublishResult:
        """Remove one of the contributor's own shared sessions and push the
        removal. Sync-then-rewrite-then-push, like :meth:`publish`."""
        gid, nm = slug(github_id), slug(name)
        base = f"{self._prefix()}{gid}/{nm}/"
        remote = self.repo.remote_exists()
        if remote:
            self.fetch()
        old = self.repo.ref_sha(self.ref)
        kept = {k: v for k, v in self.repo.read_tree_paths(self.ref).items() if not k.startswith(base)}
        self._commit(kept, f"agit: unshare session {gid}/{nm}")
        if not remote:
            return PublishResult(remote=False, pushed=False)
        lease = f"{self.ref}:{old}" if old else None
        ok, err = self.repo.push_ref(f"{self.ref}:{self.ref}", force_with_lease=lease)
        return PublishResult(remote=True, pushed=ok, error="" if ok else err.strip())

    def publish(
        self, *, github_id: str, name: str, transcript: str, manifest: dict, keep: int = DEFAULT_KEEP
    ) -> PublishResult:
        """Share one session: add it, prune the contributor's stale ones, and push.
        The local copy is always saved (so it can be pushed later); the push is
        best-effort and reports its outcome.

        Push-first: build on the local tip and push optimistically, leasing against
        the tip we last knew. This avoids a fetch round trip in the common case —
        nobody else pushed since our last sync — so a share is a single network hop.
        ``--force-with-lease`` still guards against clobbering a concurrent
        contributor: a stale lease fails cleanly, and only then do we fetch their
        work, rebuild our entry onto the new tip, and retry. Net effect: one hop
        normally, two only on a genuine race."""
        gid, nm = slug(github_id), slug(name)
        if not self.repo.remote_exists():
            self._add_session(gid, nm, transcript, manifest)
            pruned = self.prune_own_stale(gid, keep=keep)
            return PublishResult(remote=False, pushed=False, pruned=pruned)
        result = self._add_and_push(gid, nm, transcript, manifest, keep)
        if result.pushed or not _is_stale_lease(result.error):
            return result
        # Lost the race (or our orphan ref diverged from a remote one we'd never
        # fetched): sync onto the current remote tip and try once more.
        self.fetch()
        return self._add_and_push(gid, nm, transcript, manifest, keep)

    def _add_and_push(self, gid: str, nm: str, transcript: str, manifest: dict, keep: int) -> PublishResult:
        old = self.repo.ref_sha(self.ref)  # tip we believe the remote is at
        self._add_session(gid, nm, transcript, manifest)
        pruned = self.prune_own_stale(gid, keep=keep)
        lease = f"{self.ref}:{old}" if old else None  # None ⇒ creating the ref
        ok, err = self.repo.push_ref(f"{self.ref}:{self.ref}", force_with_lease=lease)
        return PublishResult(remote=True, pushed=ok, pruned=pruned, error="" if ok else err.strip())
