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

import hashlib
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
# Read-only MIRROR of the remote refs, used by the dashboard's listing fetch. The
# listing path fetches the remote into these instead of force-overwriting the canonical
# local refs above — so a remote that's momentarily behind (a share whose push lagged or
# failed) can never rewind your own freshly-shared session out of the local ref. The
# listing then unions the local refs with these mirrors, newest copy winning (see
# ``listing_entries``).
REMOTE_MIRROR = "refs/agitrack/shared-sessions-remote"
LEGACY_MIRROR = "refs/agit/shared-sessions-remote"
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
    merged: int = 0  # rows folded in from a concurrent contributor's diverged copy


def count_transcript_rows(text: str) -> int:
    """Number of non-empty lines in a JSONL transcript — a monotonic proxy for
    conversation length. Redaction is line-preserving, so a redacted shared copy
    and a raw local copy of the same conversation have the same row count, which
    makes them directly comparable for "which is newer/longer"."""
    return sum(1 for line in text.splitlines() if line.strip())


def _row_id(raw: str) -> str | None:
    """The stable id of one JSONL transcript row (Claude rows carry a ``uuid``), or
    None when the line isn't a JSON object with an id — which also marks a transcript
    that can't be line-merged (e.g. OpenCode's single-object export)."""
    try:
        row = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(row, dict):
        rid = row.get("uuid")
        return rid if isinstance(rid, str) and rid else None
    return None


def merge_transcripts(new: str, existing: str) -> str:
    """Union two divergent copies of the SAME conversation so neither side's turns
    are lost, rather than one overwriting the other (parallel shares).

    Claude transcripts are append-only with a shared head, so when two people each
    add different turns the copies share a common prefix and diverge only in the
    tail. This keeps the shared prefix, then appends the rows unique to each side —
    ``new`` first, then ``existing`` — deduplicated by row id. The result is a
    superset of both: lossless and order-stable (and idempotent: merging a copy that
    already contains the other's rows changes nothing).

    Only line-oriented JSONL with per-row ids can be merged this way. When lineage
    can't be established — the first rows have no id or differ (a different
    conversation, or OpenCode's single-object export) — it falls back to ``new``
    (last-write-wins), the prior behaviour."""
    new_rows = [line for line in new.splitlines() if line.strip()]
    old_rows = [line for line in existing.splitlines() if line.strip()]
    if not old_rows:
        return new
    if not new_rows:
        return existing
    head = _row_id(new_rows[0])
    if head is None or head != _row_id(old_rows[0]):
        return new  # not the same line-mergeable conversation → last-write-wins
    # Longest common prefix (rows identical by id, in order).
    prefix_len = 0
    for a, b in zip(new_rows, old_rows):
        a_id = _row_id(a)
        if a_id is not None and a_id == _row_id(b):
            prefix_len += 1
        else:
            break
    merged = list(new_rows[:prefix_len])
    seen = {rid for row in merged if (rid := _row_id(row)) is not None}
    for row in new_rows[prefix_len:] + old_rows[prefix_len:]:
        rid = _row_id(row)
        key = rid if rid is not None else row
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return "\n".join(merged) + "\n"


def _transcript_is_readable(text: str, backend: str | None) -> bool:
    """Whether a transcript still parses into a usable session for its backend.

    Used to guard a union merge before it's uploaded: a merge that combined two
    diverged copies must not produce a transcript the backend can no longer load.
    True only when the backend's OWN parser yields at least one turn; False on a
    parse failure or an empty result. Supported for both backends — Claude
    (line-oriented JSONL via ``parse_rows``) and OpenCode (a single export object via
    ``parse_exported_session``). An unknown/unspecified backend has no parser to
    check, so it is treated as readable (the merge logic only runs for line-mergeable
    Claude transcripts anyway)."""
    if not text.strip():
        return False
    try:
        if backend == "claude":
            from agitrack.transcripts.claude import parse_rows

            rows: list[dict] = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # mirror the real reader: skip a stray unparsable line
            return bool(parse_rows("readability-check", rows).turns)
        if backend == "opencode":
            from agitrack.transcripts.opencode import parse_exported_session

            return bool(parse_exported_session(json.loads(text)).turns)
    except (json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError):
        return False
    return True


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

        Unions four sources, keeping the NEWEST copy of each session: the canonical local
        ref — which always holds YOUR own freshly-shared sessions — its legacy counterpart
        (a pre-rename peer's shares), and the two remote MIRRORS the listing fetch keeps
        (collaborators' sessions, plus possibly a stale copy of your own). Newest-wins —
        rather than "local wins" — is what keeps a remote that's momentarily behind from
        making your just-shared session look old: the listing fetch (:meth:`fetch`) writes
        only the mirrors, never the local ref, so your fresh local copy is never rewound and
        wins the tie against a stale mirror. The local ref is read unconditionally (it always
        exists once you've shared); the others are skipped until they exist."""
        prefix = self._prefix()
        best: dict[tuple[str, str], SharedEntry] = {}
        for ref in (self.ref, REMOTE_MIRROR, LEGACY_REF, LEGACY_MIRROR):
            if ref != self.ref and not self.repo.ref_exists(ref):
                continue
            for path in self.repo.read_tree_paths(ref):
                if not path.startswith(prefix):
                    continue
                rest = path[len(prefix) :].split("/")
                if len(rest) < 3:
                    continue
                key = (rest[0], rest[1])
                if key in best and rest[2] != "manifest.json":
                    continue  # the manifest path decides the winner; skip the transcript path
                manifest = self._manifest(*key, ref=ref)
                current = best.get(key)
                if current is None or manifest.get("updated", 0) > current.manifest.get("updated", 0):
                    best[key] = SharedEntry(github_id=key[0], name=key[1], manifest=manifest, source_ref=ref)
        return sorted(best.values(), key=lambda e: e.manifest.get("updated", 0), reverse=True)

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
        # When the chosen entry came from a remote MIRROR, its transcript lives on the
        # remote's CANONICAL ref (the mirror name doesn't exist on the remote), so fetch
        # that into the mirror; for a local/legacy entry the source ref IS the remote ref.
        remote_ref = {REMOTE_MIRROR: REF, LEGACY_MIRROR: LEGACY_REF}.get(ref, ref)
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
            self.repo.fetch_ref(f"+{remote_ref}:{ref}", timeout=timeout, cancel=cancel)
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
        """Pull the latest shared refs from the remote into the read-only MIRROR refs, for
        LISTING (best-effort).

        Fetching into the mirrors — NOT the canonical local refs — is what keeps a listing
        refresh from rewinding your own freshly-shared session: the local ref (which holds
        your shares) is left untouched, and :meth:`entries` unions it with the mirrors,
        newest copy winning. So a remote that's momentarily behind can never make your
        just-shared session show an old "shared" time. Fetches only the small manifests (a
        blob-size filter skips the large transcripts) so listing is fast; a chosen session's
        transcript is fetched on demand by :meth:`read_transcript`, falling back to a full
        fetch when the remote doesn't support partial fetch. ``timeout`` bounds each git
        fetch; ``cancel`` stops it at once. (Internal write syncs use ``_fetch_current``,
        which still force-updates the local ref for the push lease.)"""
        if not self.repo.remote_exists():
            return False
        if cancel is not None and cancel.is_set():
            return False  # already cancelled: don't even start
        ok = False
        # Mirror the current ref and the legacy ref (a pre-rename peer's shares); the legacy
        # ref may not exist on the remote at all (best-effort, ignore failure).
        for src, dst in ((self.ref, REMOTE_MIRROR), (LEGACY_REF, LEGACY_MIRROR)):
            if cancel is not None and cancel.is_set():
                break
            fetched = self.repo.fetch_ref(
                f"+{src}:{dst}", filter_blobs="blob:limit=16k", timeout=timeout, cancel=cancel
            )
            if not fetched and not (cancel is not None and cancel.is_set()):
                fetched = self.repo.fetch_ref(f"+{src}:{dst}", timeout=timeout, cancel=cancel)
            ok = ok or fetched
        return ok

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

    def unshare(self, github_id: str, name: str, *, timeout: float | None = None) -> PublishResult:
        """Remove one of the contributor's own shared sessions and push the removal.
        Sync-then-rewrite-then-push, like :meth:`publish`. ``timeout`` bounds each network
        fetch/push so a stalled remote can't hang the (background) unshare thread.

        The entry is removed from BOTH the current ref and the legacy
        ``refs/agit/shared-sessions`` — a session shared before the aGiT → aGiTrack
        rename lives only in the legacy ref, so rewriting the current ref alone would
        leave it visible (it would keep surfacing through :meth:`entries`)."""
        gid, nm = slug(github_id), slug(name)
        remote = self.repo.remote_exists()
        pushed_any = False
        errors: list[str] = []
        for ref in (self.ref, LEGACY_REF):
            if ref != self.ref and not self.repo.ref_exists(ref):
                continue
            ok, err = self._unshare_one_ref(ref, gid, nm, remote, timeout)
            if ok is None:
                continue  # this ref doesn't hold the entry
            if remote and not ok and _is_stale_lease(err):
                # A concurrent push (this session's own auto-share, or another machine) moved
                # the remote ref between our fetch and our push, so --force-with-lease rejected
                # it. Re-sync onto the current tip and retry once — exactly what publish() does.
                # Without this the unshare reliably fails on an active repo and the removal
                # never lands (the user sees "the push was rejected").
                ok, err = self._unshare_one_ref(ref, gid, nm, remote, timeout)
            pushed_any = pushed_any or bool(ok)
            if not ok and err.strip():
                errors.append(err.strip())
        if not remote:
            return PublishResult(remote=False, pushed=False)
        return PublishResult(remote=True, pushed=pushed_any, error="; ".join(errors))

    def _unshare_one_ref(
        self, ref: str, gid: str, nm: str, remote: bool, timeout: float | None = None
    ) -> tuple[bool | None, str]:
        """Drop the ``gid/nm`` entry from *ref* and (with a remote) push the removal. Returns
        ``(None, "")`` when *ref* doesn't hold the entry; otherwise ``(pushed_ok, error)`` — a
        remote-less repo reports ``(True, "")`` once the local ref is rewritten. Re-fetches the
        ref each call so the retry above rebuilds the removal onto the moved remote tip."""
        base = f"{self._prefix()}{gid}/{nm}/"
        if remote:
            # Filtered sync is enough: rewriting only re-references existing blob SHAs
            # (already on the remote), so the large transcripts need not be local.
            self.repo.fetch_ref(f"+{ref}:{ref}", filter_blobs="blob:limit=16k", timeout=timeout)
        paths = self.repo.read_tree_paths(ref)
        if not any(k.startswith(base) for k in paths):
            return None, ""  # this ref doesn't hold the entry
        old = self.repo.ref_sha(ref)
        kept = {k: v for k, v in paths.items() if not k.startswith(base)}
        self._commit(kept, f"agitrack: unshare session {gid}/{nm}", ref=ref)
        if not remote:
            return True, ""  # local-only: the rewrite IS the removal
        lease = f"{ref}:{old}" if old else None
        return self.repo.push_ref(f"{ref}:{ref}", force_with_lease=lease, timeout=timeout)

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
        overwrite: bool = False,
    ) -> PublishResult:
        """Share one session: add it, prune the contributor's stale ones, and push.
        The local copy is always saved (so it can be pushed later); the push is
        best-effort and reports its outcome.

        ``overwrite`` resolves a conflict where the SHARED copy is already newer (more
        turns) than this one — normally refused with ``behind=True`` so a stale machine
        can't rewind everyone's copy. It first syncs the current remote (so the
        replacement is against its latest tip), then replaces the shared copy with this
        session wholesale, skipping both the union merge and the recency guard. Default
        off: an ordinary share (and every auto-share) stays conservative and refuses to
        regress. (Divergent copies that CAN be combined are union-merged automatically
        during a normal publish, so they never reach a ``behind`` refusal — ``overwrite``
        is for the non-mergeable case, where replace-or-keep is the only real choice.)

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
            if not overwrite and self._would_regress(gid, nm, transcript):
                return PublishResult(remote=False, pushed=False, behind=True)
            self._add_session(gid, nm, transcript, manifest)
            pruned = self.prune_own_stale(pgid, keep=keep)
            return PublishResult(remote=False, pushed=False, pruned=pruned)
        # Overwrite: sync the current remote FIRST so we replace its latest tip cleanly
        # (the lease then matches), instead of pushing optimistically only to lose the
        # lease and retry.
        if overwrite:
            self._fetch_current(timeout=timeout, cancel=cancel)
        result = self._add_and_push(
            gid, nm, transcript, manifest, keep, timeout, cancel, prune_gid=pgid, overwrite=overwrite
        )
        if result.pushed or not _is_stale_lease(result.error):
            return result
        if cancel is not None and cancel.is_set():
            return result  # the user cancelled the push: don't fetch+retry behind their back
        # Lost the race (or our orphan ref diverged from a remote one we'd never
        # fetched): sync onto the current remote tip and try once more.
        self._fetch_current(timeout=timeout, cancel=cancel)
        return self._add_and_push(
            gid, nm, transcript, manifest, keep, timeout, cancel, prune_gid=pgid, overwrite=overwrite
        )

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
        overwrite: bool = False,
    ) -> PublishResult:
        # Best-effort union merge: if a copy already sits in the local ref and it has
        # DIVERGED from ours — e.g. a concurrent contributor's version we just fetched
        # after losing the push race — combine both sides' turns so neither's work is
        # dropped, instead of overwriting one with the other. A no-op for the common
        # cases: our copy is a superset of what's there (append-only re-share), or the
        # transcript isn't line-mergeable (OpenCode), in which case last-write-wins and
        # the recency guard below still applies. Skipped entirely on ``overwrite``: the
        # caller chose to REPLACE the shared copy with this session, so its turns must
        # not be folded back in.
        merged_rows = 0
        existing = self.repo.read_ref_blob(self.ref, f"{self._prefix()}{gid}/{nm}/transcript.jsonl")
        if existing and not overwrite:
            combined = merge_transcripts(transcript, existing)
            # Only accept the merge if its result is still readable by the backend —
            # the guard that combining two diverged copies didn't corrupt the session.
            # An unreadable merge falls back to last-write-wins (our own transcript,
            # which is whole), so a broken merge is never uploaded.
            if combined != transcript and _transcript_is_readable(combined, manifest.get("backend")):
                merged_rows = count_transcript_rows(combined) - count_transcript_rows(transcript)
                transcript = combined
                manifest = {
                    **manifest,
                    "content_hash": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
                    "transcript_rows": count_transcript_rows(combined),
                    "transcript_bytes": len(combined.encode("utf-8")),
                }
        # Recency guard: never replace a longer shared copy with a shorter (older) one.
        # Runs on the retry too — after a stale-lease fetch the local ref equals the
        # remote, so a machine that's behind is caught here and refuses rather than
        # rewinding everyone's shared session to its older state. (After a merge above
        # the transcript is a superset, so this never trips for a genuine divergence.)
        # ``overwrite`` deliberately bypasses it: the user chose to replace the newer
        # shared copy with this one.
        if not overwrite and self._would_regress(gid, nm, transcript):
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
        return PublishResult(remote=True, pushed=ok, pruned=pruned, error="" if ok else err.strip(), merged=merged_rows)
