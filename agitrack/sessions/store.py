"""The shared-session store: a history-free git ref that keeps only the latest
copy of each contributor's shared sessions, scoped to this repo.

Layout under ``refs/agitrack/shared-sessions``::

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

from agitrack.git import GitRepo
from agitrack.sessions.identity import slug

REF = "refs/agitrack/shared-sessions"
# Sessions shared by a peer still running pre-rename aGiT land under the old ref.
# Reads merge both (new wins); writes only ever touch the new ref.
LEGACY_REF = "refs/agit/shared-sessions"
DEFAULT_KEEP = 5  # most-recent shared sessions retained per contributor
# Throttle remote fetches when the dashboard polls. Your OWN shared sessions land
# in the local ref directly (no fetch needed); this only pulls collaborators'
# newly-shared sessions, so a long interval is fine and keeps load off the remote.
_FETCH_TTL = 300.0
_fetch_at: dict[str, float] = {}


@dataclass
class SharedEntry:
    github_id: str  # the lineage origin owner = the ref path's owner component
    name: str
    manifest: dict
    source_ref: str = REF  # the ref this entry was read from (legacy entries differ)

    @property
    def contributors(self) -> list[str]:
        """Every github id that has shared this session, sorted (order-independent).
        Falls back to just the origin owner for an entry shared by an older client
        that didn't record the set."""
        raw = self.manifest.get("contributors")
        ids = sorted({str(c) for c in raw if c}) if isinstance(raw, list) else []
        return ids or [self.github_id]

    @property
    def display(self) -> str:
        # `<id1>+<id2>/<name>`: the contributor set (sorted, so order never matters)
        # before the name. One logical session shows as one entry no matter how many
        # machines/people have shared it.
        return f"{'+'.join(self.contributors)}/{self.name}"


@dataclass
class PublishResult:
    remote: bool  # whether a git remote exists at all
    pushed: bool  # whether the push succeeded
    pruned: int = 0  # how many of the contributor's stale sessions were removed
    error: str = ""
    behind: bool = False  # refused: the shared copy already has newer turns than this one


def count_transcript_rows(text: str) -> int:
    """Number of non-empty lines in a JSONL transcript — a monotonic proxy for
    conversation length. Redaction is line-preserving, so a redacted shared copy
    and a raw local copy of the same conversation have the same row count, which
    makes them directly comparable for "which is newer/longer"."""
    return sum(1 for line in text.splitlines() if line.strip())


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
        """Shared sessions for *this* repo, newest first (by manifest ``updated``).

        Merges the current ref with the legacy ``refs/agitrack/shared-sessions`` so
        sessions shared by a peer still on pre-rename aGiT remain visible. The
        current ref is read first and wins on a name collision."""
        prefix = self._prefix()
        seen: dict[tuple[str, str], SharedEntry] = {}
        for ref in (self.ref, LEGACY_REF):
            if ref != self.ref and not self.repo.ref_exists(ref):
                continue
            for path in self.repo.read_tree_paths(ref):
                if not path.startswith(prefix):
                    continue
                rest = path[len(prefix) :].split("/")
                if len(rest) < 3:
                    continue
                key = (rest[0], rest[1])
                if key in seen:
                    continue
                seen[key] = SharedEntry(
                    github_id=key[0], name=key[1], manifest=self._manifest(*key, ref=ref), source_ref=ref
                )
        return sorted(seen.values(), key=lambda e: e.manifest.get("updated", 0), reverse=True)

    def _manifest(self, github_id: str, name: str, *, ref: str | None = None) -> dict:
        raw = self.repo.read_ref_blob(ref or self.ref, f"{self._prefix()}{github_id}/{name}/manifest.json")
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    def _entry_rows(self, gid: str, nm: str) -> int | None:
        """Row count of the transcript currently stored for ``gid/nm`` in the local
        ref, or None when there's no such entry yet. Authoritative (reads the blob)
        so it doesn't depend on a manifest field that an older client may omit."""
        raw = self.repo.read_ref_blob(self.ref, f"{self._prefix()}{gid}/{nm}/transcript.jsonl")
        return count_transcript_rows(raw) if raw else None

    def _would_regress(self, gid: str, nm: str, transcript: str) -> bool:
        """Whether writing ``transcript`` would REPLACE a longer shared copy with a
        shorter one — i.e. this machine is behind. Claude/OpenCode transcripts are
        append-only, so fewer rows means an older conversation. Refusing this is what
        stops a stale machine (or its auto-share, which fires on every commit) from
        rewinding everyone's shared copy to an earlier state."""
        existing = self._entry_rows(gid, nm)
        return existing is not None and count_transcript_rows(transcript) < existing

    def read_transcript(
        self, entry: SharedEntry, *, timeout: float | None = None, cancel: "threading.Event | None" = None
    ) -> str | None:
        ref = entry.source_ref
        path = f"{self._prefix()}{entry.github_id}/{entry.name}/transcript.jsonl"
        # Resuming a SHARED session must reflect the LATEST shared state — so sync the
        # full ref from the remote FIRST, then read. Reading the local ref blind would
        # return a stale copy whenever one is already present locally: the listing
        # fetch pulls small transcripts (and only the manifests for large ones), and a
        # prior resume may have left an older transcript in the object store. The full
        # fetch force-updates the ref to the remote tip, so the read below is current.
        # ``timeout`` bounds this (potentially large) fetch; ``cancel`` kills it (the
        # git process is terminated, not left running). Offline (fetch fails) we fall
        # back to whatever is local — the best available.
        if self.repo.remote_exists() and not (cancel is not None and cancel.is_set()):
            self.repo.fetch_ref(f"+{ref}:{ref}", timeout=timeout, cancel=cancel)
        return self.repo.read_ref_blob(ref, path)

    # --- writing -----------------------------------------------------------

    def _commit(
        self, entries: dict[str, str], message: str, *, reclaim: bool = True, ref: str | None = None
    ) -> str | None:
        """Rewrite ``ref`` (default the current ref) to a new orphan commit of
        *entries*. Returns the PREVIOUS ref sha (the now-orphaned snapshot), so a
        caller that pushes can keep it as the push's delta base and reclaim it
        afterwards.

        ``reclaim`` (default) deletes the orphaned previous snapshot's objects right
        away — large transcript blobs shouldn't linger until git's auto-gc (#55). Pass
        ``reclaim=False`` when the previous version must survive a little longer (a
        publish keeps it as the ``git push`` delta base, then reclaims it post-push)."""
        ref = ref or self.ref
        old = self.repo.ref_sha(ref)
        tree = self.repo.write_tree_from(entries)
        sha = self.repo.commit_tree_orphan(tree, message)
        self.repo.update_ref(ref, sha)
        if reclaim and old and old != sha:
            self.repo.delete_orphaned_objects(old)
        return old if old != sha else None

    def _add_session(
        self, github_id: str, name: str, transcript: str, manifest: dict, *, reclaim: bool = True
    ) -> str | None:
        base = f"{self._prefix()}{github_id}/{name}/"
        entries = {k: v for k, v in self.repo.read_tree_paths(self.ref).items() if not k.startswith(base)}
        entries[base + "transcript.jsonl"] = self.repo.write_blob(transcript)
        entries[base + "manifest.json"] = self.repo.write_blob(json.dumps(manifest, indent=2, sort_keys=True))
        return self._commit(entries, f"agit: share session {github_id}/{name}", reclaim=reclaim)

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
        if cancel is not None and cancel.is_set():
            return False  # already cancelled: don't even start
        # Pull the legacy ref too (best-effort, ignore failure) so sessions shared
        # by a pre-rename peer still list. The remote may not have it at all. Only
        # the *listing* path needs this; internal syncs use ``_fetch_current``.
        self.repo.fetch_ref(
            f"+{LEGACY_REF}:{LEGACY_REF}", filter_blobs="blob:limit=16k", timeout=timeout, cancel=cancel
        )
        return self._fetch_current(timeout=timeout, cancel=cancel)

    def _fetch_current(self, *, timeout: float | None = None, cancel: "threading.Event | None" = None) -> bool:
        """Sync only the current ref from the remote (no legacy ref). Used by the
        write paths (publish retry, cleanup, unshare) that touch the new ref only."""
        if not self.repo.remote_exists():
            return False
        if cancel is not None and cancel.is_set():
            return False
        refspec = f"+{self.ref}:{self.ref}"
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
                self._fetch_current()
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
        """Best-effort fetch at most once per TTL, in the BACKGROUND — for pollers (the
        dashboard) that want others' newly-shared sessions without hammering the remote
        or blocking the request on a network round trip. Your OWN shared sessions are
        already in the local ref, so the page renders from it immediately and a
        teammate's newly-shared session appears on a later poll."""
        key = str(self.repo.repo)
        now = time.monotonic()
        if now - _fetch_at.get(key, 0.0) < _FETCH_TTL:
            return
        _fetch_at[key] = now  # claim the window up front so concurrent polls don't pile on

        def worker() -> None:
            try:
                self.fetch()
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True, name="agit-shared-fetch-bg").start()

    def unshare(self, github_id: str, name: str) -> PublishResult:
        """Remove one of the contributor's own shared sessions and push the removal.
        Sync-then-rewrite-then-push, like :meth:`publish`.

        The entry is removed from BOTH the current ref and the legacy
        ``refs/agit/shared-sessions`` — a session shared before the aGiT → aGiTrack
        rename lives only in the legacy ref, so rewriting the current ref alone would
        leave it visible (it would keep surfacing through :meth:`entries`)."""
        gid, nm = slug(github_id), slug(name)
        base = f"{self._prefix()}{gid}/{nm}/"
        remote = self.repo.remote_exists()
        pushed_any = False
        errors: list[str] = []
        for ref in (self.ref, LEGACY_REF):
            if ref != self.ref and not self.repo.ref_exists(ref):
                continue
            if remote:
                # Filtered sync is enough: rewriting only re-references existing blob
                # SHAs (already on the remote), so the large transcripts need not be local.
                self.repo.fetch_ref(f"+{ref}:{ref}", filter_blobs="blob:limit=16k")
            paths = self.repo.read_tree_paths(ref)
            if not any(k.startswith(base) for k in paths):
                continue  # this ref doesn't hold the entry
            old = self.repo.ref_sha(ref)
            kept = {k: v for k, v in paths.items() if not k.startswith(base)}
            self._commit(kept, f"agitrack: unshare session {gid}/{nm}", ref=ref)
            if remote:
                lease = f"{ref}:{old}" if old else None
                ok, err = self.repo.push_ref(f"{ref}:{ref}", force_with_lease=lease)
                pushed_any = pushed_any or ok
                if not ok and err.strip():
                    errors.append(err.strip())
        if not remote:
            return PublishResult(remote=False, pushed=False)
        return PublishResult(remote=True, pushed=pushed_any, error="; ".join(errors))

    def publish(
        self,
        *,
        github_id: str,
        name: str,
        transcript: str,
        manifest: dict,
        prune_gid: str | None = None,
        keep: int = DEFAULT_KEEP,
        timeout: float | None = None,
        cancel: "threading.Event | None" = None,
    ) -> PublishResult:
        """Share one session: add it, prune the contributor's stale ones, and push.
        The local copy is always saved (so it can be pushed later); the push is
        best-effort and reports its outcome.

        ``github_id``/``name`` are the entry's LINEAGE ORIGIN (the path it lives at) —
        a re-share of an imported session writes under the original owner+name so it
        stays one entry. ``prune_gid`` is the ACTUAL sharer's id, whose own stale
        sessions are pruned; it defaults to ``github_id`` for a session originated
        here. (Pruning the origin owner when a different contributor re-shares would
        let them prune someone else's sessions — hence the split.)

        Push-first: build on the local tip and push optimistically, leasing against
        the tip we last knew. This avoids a fetch round trip in the common case —
        nobody else pushed since our last sync — so a share is a single network hop.
        ``--force-with-lease`` still guards against clobbering a concurrent
        contributor: a stale lease fails cleanly, and only then do we fetch their
        work, rebuild our entry onto the new tip, and retry. Net effect: one hop
        normally, two only on a genuine race."""
        gid, nm = slug(github_id), slug(name)
        pgid = slug(prune_gid) if prune_gid else gid
        if not self.repo.remote_exists():
            if self._would_regress(gid, nm, transcript):
                return PublishResult(remote=False, pushed=False, behind=True)
            self._add_session(gid, nm, transcript, manifest)
            pruned = self.prune_own_stale(pgid, keep=keep)
            return PublishResult(remote=False, pushed=False, pruned=pruned)
        result = self._add_and_push(gid, nm, transcript, manifest, keep, timeout, cancel, prune_gid=pgid)
        if result.pushed or not _is_stale_lease(result.error):
            return result
        if cancel is not None and cancel.is_set():
            return result  # the user cancelled the push: don't fetch+retry behind their back
        # Lost the race (or our orphan ref diverged from a remote one we'd never
        # fetched): sync onto the current remote tip and try once more.
        self._fetch_current(timeout=timeout, cancel=cancel)
        return self._add_and_push(gid, nm, transcript, manifest, keep, timeout, cancel, prune_gid=pgid)

    def _add_and_push(
        self,
        gid: str,
        nm: str,
        transcript: str,
        manifest: dict,
        keep: int,
        timeout: float | None = None,
        cancel: "threading.Event | None" = None,
        *,
        prune_gid: str | None = None,
    ) -> PublishResult:
        # Recency guard: never replace a longer shared copy with a shorter (older) one.
        # Runs on the retry too — after a stale-lease fetch the local ref equals the
        # remote, so a machine that's behind is caught here and refuses rather than
        # rewinding everyone's shared session to its older state.
        if self._would_regress(gid, nm, transcript):
            return PublishResult(remote=True, pushed=False, behind=True)
        old = self.repo.ref_sha(self.ref)  # tip we believe the remote is at = the delta base
        # Keep the PREVIOUS snapshot's objects (reclaim=False) so they survive until the
        # push below: ``git push`` deltifies the new transcript against the version the
        # remote already has (the same blob is still local), so an append-heavy session
        # re-shared (or auto-shared on every commit) transmits only the new turns, not a
        # fresh full copy each time. Without this, deleting `old` before the push left no
        # local delta base, forcing git to send the whole transcript every share.
        self._add_session(gid, nm, transcript, manifest, reclaim=False)
        pruned = self.prune_own_stale(prune_gid or gid, keep=keep)
        lease = f"{self.ref}:{old}" if old else None  # None ⇒ creating the ref
        ok, err = self.repo.push_ref(f"{self.ref}:{self.ref}", force_with_lease=lease, timeout=timeout, cancel=cancel)
        # The previous version has served as the push's delta base; reclaim it now so
        # only the current snapshot stays local (bounded storage; nothing for unshare to
        # miss). Re-fetchable if a retry needs it. The ref still has no history, so an
        # unshare that rewrites the orphan commit still fully removes the session.
        if old:
            self.repo.delete_orphaned_objects(old)
        return PublishResult(remote=True, pushed=ok, pruned=pruned, error="" if ok else err.strip())
