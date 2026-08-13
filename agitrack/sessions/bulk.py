"""Share every local session for a repo in one go.

Session sharing has always been per-session and interactive: pick one from the menu, push it.
That is the wrong shape for the thing people actually want at the end of a stretch of work —
"put everything I did on this project where the rest of the team (and my other machines) can
see it" — and it is the prerequisite for a backtrace that reconstructs a project's history
across MACHINES rather than only the transcripts sitting on this one.

So this module does the same thing the menu does, for all of them, without a UI: discover the
conversations recorded under the repo (both backends), redact and cap each transcript exactly
as an interactive share would, and publish them under the sharer's GitHub id. Everything
downstream — the ref layout, the lineage/contributor rules, the push-with-lease retry — is
:mod:`agitrack.sessions.store`'s, unchanged: this is a different way to invoke sharing, not a
second implementation of it.

Two rules keep a bulk run from being destructive, both inherited deliberately:

* A session whose SHARED copy already has more turns than the local one is refused
  (``PublishResult.behind``), never overwritten. A bulk push from a machine that has been
  offline for a week must not rewind everyone else's copy of a session it happens to hold an
  older snapshot of. Those are reported as skipped, with a reason.
* A session already shared with identical content is skipped without a network round trip, so
  re-running the command is cheap and idempotent rather than N pushes of unchanged blobs.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path

from agitrack.config.state import AgitrackState
from agitrack.git import GitRepo
from agitrack.sessions.identity import github_login, slug
from agitrack.sessions.redact import redact_transcript
from agitrack.sessions.store import SharedSessionStore
from agitrack.transcripts.types import SessionRef

# The same ceiling an interactive share uses when the repo config sets none. Git refuses very
# large blobs and forges reject large pushes, so a transcript is capped to a resumable recent
# tail rather than failing the whole run.
DEFAULT_MAX_TRANSCRIPT_BYTES = 40 * 1024 * 1024


@dataclass
class SessionCandidate:
    """One local conversation that could be shared."""

    backend: str
    session_id: str
    name: str
    updated: float
    raw: str


@dataclass
class ShareOutcome:
    session_id: str
    backend: str
    name: str
    status: str  # "shared" | "unchanged" | "behind" | "failed" | "unreadable"
    detail: str = ""


@dataclass
class BulkResult:
    outcomes: list[ShareOutcome] = field(default_factory=list)
    login: str = ""

    @property
    def shared(self) -> list[ShareOutcome]:
        return [o for o in self.outcomes if o.status == "shared"]

    @property
    def skipped(self) -> list[ShareOutcome]:
        return [o for o in self.outcomes if o.status != "shared"]

    def summary(self) -> str:
        if not self.outcomes:
            return "No local sessions found for this repository."
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        parts = [f"{count} {status}" for status, count in sorted(counts.items())]
        return f"{len(self.outcomes)} session(s): " + ", ".join(parts)


def discover_local_sessions(directory: Path) -> list[SessionCandidate]:
    """Every conversation recorded under ``directory``, newest first, with its raw transcript.

    "Under" rather than "in": a session may have run in an aGiTrack worktree beneath the repo,
    and those are as much part of the project's history as ones run in the base checkout. One
    conversation recorded under several project directories (Claude keys transcripts by cwd)
    is kept ONCE, at its largest — the most complete copy — matching how the backtrace
    reconstructs the same sessions.
    """
    from agitrack.transcripts import claude, codex, opencode

    state = AgitrackState(directory)
    candidates: dict[str, SessionCandidate] = {}

    try:
        best: dict[str, tuple[int, SessionRef, Path]] = {}
        for ref, path in claude.sessions_under(directory):
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            current = best.get(ref.id)
            if current is None or size > current[0]:
                best[ref.id] = (size, ref, path)
        for _size, ref, path in best.values():
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not raw.strip():
                continue
            candidates[ref.id] = SessionCandidate(
                backend="claude",
                session_id=ref.id,
                name=_session_name(state, ref),
                updated=float(getattr(ref, "updated", 0.0) or 0.0),
                raw=raw,
            )
    except Exception:
        pass  # one backend's discovery failing must never block the other

    try:
        # `rollout` (not `path`): the Claude loop above binds `path` as a Path, and codex's
        # sessions_under yields the rollout location as a str — reusing the name is a type clash.
        for ref, rollout in codex.sessions_under(directory):
            if ref.id in candidates:
                continue
            try:
                raw = Path(rollout).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not raw.strip():
                continue
            candidates[ref.id] = SessionCandidate(
                backend="codex",
                session_id=ref.id,
                name=_session_name(state, ref),
                updated=float(getattr(ref, "updated", 0.0) or 0.0),
                raw=raw,
            )
    except Exception:
        pass  # one backend's discovery failing must never block the others

    try:
        for ref, sdir in opencode.sessions_under(directory):
            if ref.id in candidates:
                continue
            exported = opencode.export_session_raw(Path(sdir), ref.id)
            if not exported or not exported.strip():
                continue
            raw = exported
            candidates[ref.id] = SessionCandidate(
                backend="opencode",
                session_id=ref.id,
                name=_session_name(state, ref),
                updated=float(getattr(ref, "updated", 0.0) or 0.0),
                raw=raw,
            )
    except Exception:
        pass

    return sorted(candidates.values(), key=lambda c: c.updated, reverse=True)


def _session_name(state: AgitrackState, ref) -> str:
    """The name to file a conversation under.

    The user-given session name when there is one — that is what the rest of aGiTrack (the
    sessions menu, the resume list, commit metadata) already calls this conversation, and a
    bulk share must not invent a second vocabulary for it. Otherwise a stable, readable
    fallback derived from the id, so an unnamed conversation is still shareable and still
    lands at a predictable ref path.
    """
    named = state.session_name_for(ref.id)
    if named:
        return named
    short = str(ref.id).replace("-", "")[:8]
    return f"session-{short}" if short else "session"


def share_all(
    repo: GitRepo,
    *,
    login: str | None = None,
    max_bytes: int = DEFAULT_MAX_TRANSCRIPT_BYTES,
    timeout: float | None = None,
    progress=None,
    on_result=None,
    overwrite: bool = False,
) -> BulkResult:
    """Share every local session for ``repo``. Returns what happened to each.

    ``progress(index, total, candidate)`` is called BEFORE each push and ``on_result(outcome)``
    right after it, so a CLI can report a session and its outcome together as it goes — a bulk
    share is one network round trip each, and a silent minute reads as a hang.
    """

    def record(outcome: ShareOutcome) -> None:
        result.outcomes.append(outcome)
        if on_result is not None:
            on_result(outcome)

    directory = Path(repo.repo)
    store = SharedSessionStore(repo)
    resolved = login or github_login(repo)
    result = BulkResult(login=resolved)
    candidates = discover_local_sessions(directory)
    # Fetch ONCE up front rather than per session: every publish would otherwise re-sync the
    # same ref, turning a bulk share into N redundant round trips.
    try:
        store.fetch(timeout=timeout)
    except Exception:
        pass
    existing = {}
    try:
        existing = {(entry.github_id, entry.name): entry for entry in store.entries()}
    except Exception:
        pass

    for index, candidate in enumerate(candidates, start=1):
        if progress is not None:
            progress(index, len(candidates), candidate)
        shared_text = _redact_and_cap(candidate, max_bytes)
        digest = hashlib.sha256(shared_text.encode("utf-8")).hexdigest()
        prior = existing.get((slug(resolved), slug(candidate.name)))
        if prior is not None and prior.manifest.get("content_hash") == digest:
            record(ShareOutcome(candidate.session_id, candidate.backend, candidate.name, "unchanged"))
            continue
        manifest = {
            "github_id": resolved,
            "name": candidate.name,
            "contributors": [resolved],
            "backend": candidate.backend,
            "session_id": candidate.session_id,
            "updated": int(time.time()),
            "content_hash": digest,
            "transcript_bytes": len(candidate.raw.encode("utf-8")),
            "transcript_rows": shared_text.count("\n") + 1 if shared_text else 0,
            "truncated": len(shared_text) != len(redact_transcript(candidate.raw)),
        }
        try:
            published = store.publish(
                github_id=resolved,
                name=candidate.name,
                transcript=shared_text,
                manifest=manifest,
                timeout=timeout,
                overwrite=overwrite,
            )
        except Exception as error:
            record(ShareOutcome(candidate.session_id, candidate.backend, candidate.name, "failed", str(error)))
            continue
        if published.behind:
            record(
                ShareOutcome(
                    candidate.session_id,
                    candidate.backend,
                    candidate.name,
                    "behind",
                    # NAME THE REAL FLAG. `--overwrite` does not exist; it only ever resolved because
                    # argparse prefix-matches, and it would break the moment any other
                    # --overwrite* option is added.
                    "the shared copy already has newer turns; resume it first, or re-run with --overwrite-shared",
                )
            )
        elif published.remote and not published.pushed:
            record(
                ShareOutcome(
                    candidate.session_id, candidate.backend, candidate.name, "failed", published.error or "push refused"
                )
            )
        else:
            detail = "" if published.pushed else "saved locally (no 'origin' remote to push to)"
            record(ShareOutcome(candidate.session_id, candidate.backend, candidate.name, "shared", detail))
    return result


def _redact_and_cap(candidate: SessionCandidate, max_bytes: int) -> str:
    """Redact secrets and bound the transcript, exactly as an interactive share does.

    The cap is per backend because the transcript formats differ (Claude's JSONL rows vs
    OpenCode's export), and each module knows how to drop whole middle turns while leaving a
    resumable recent tail.
    """
    from agitrack.transcripts import claude, codex, opencode

    redacted = redact_transcript(candidate.raw)
    # Keyed by name rather than a two-way ternary: with three backends the ternary silently
    # handed Codex's JSONL to OpenCode's single-JSON-object capper, which would have truncated
    # every shared Codex session to nothing.
    cappers = {
        "claude": claude.cap_shared_transcript,
        "codex": codex.cap_shared_transcript,
        "opencode": opencode.cap_shared_transcript,
    }
    cap = cappers.get(candidate.backend, claude.cap_shared_transcript)
    try:
        return cap(redacted, max_bytes)
    except Exception:
        return redacted
