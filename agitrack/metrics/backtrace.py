"""``agitrack --backtrace``: reconstruct how PAST coding-agent conversations changed a
directory, from local transcripts alone — no git history, and no prior aGiTrack use.

The dashboard is normally computed from ``git log`` (aGiTrack's own commit metadata).
Backtrace instead reads the local Claude and OpenCode session transcripts for the current
directory, recovers each turn's file edits from the tool-call inputs, and maps every turn
onto the SAME :class:`~agitrack.metrics.collect.Dashboard` / :class:`CommitStat` model the
web dashboard already renders. So a first-time user — even in a folder that was never a git
repo — can immediately see the value: every prompt, the model that answered it, the tokens
it burned, the lines it changed, and the full user↔agent trace behind each change.

It is a HISTORICAL RECONSTRUCTION, made explicit by a banner in the view, and is built
ONCE and cached: re-exporting transcripts on every dashboard poll (OpenCode's export shells
out to its CLI) would be far too slow, and the history does not change under us.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Callable

from agitrack.commits import METADATA_HEADER
from agitrack.commits.message import _token_metadata_lines, render_interaction_trace
from agitrack.git import GitRepo
from agitrack.metrics.collect import CommitStat, Dashboard, _abbreviate_home, _display_repo
from agitrack.transcripts import claude, opencode
from agitrack.transcripts.edits import combine_patches, merge_edits_by_path, total_lines
from agitrack.transcripts.types import ExportedSession, FileEdit, SessionRef, SessionTurn

# Cap on how many sessions a single backtrace reconstructs, newest first. Exporting a
# session is real work (OpenCode shells out per session), so an unbounded scan of a machine
# with thousands of conversations could take minutes; the cap keeps the view responsive and
# the dropped count is surfaced in the banner (never silently truncated).
MAX_SESSIONS = 200

# Cap on a single turn's reconstructed patch, so one enormous refactor can't bloat the
# ``/diff`` payload. The line COUNTS are always exact (they are summed before this cap);
# only the shown patch text is trimmed, with a marker.
_MAX_PATCH_CHARS = 200_000


@dataclass
class BacktraceView:
    """A built backtrace: the dashboard to render plus the per-turn diffs its ``/diff``
    view serves, and the counts the banner reports."""

    directory: str  # home-abbreviated, for display
    dashboard: Dashboard
    root: Path | None = None  # the resolved directory on disk, to list only files that still exist
    diffs: dict[str, str] = field(default_factory=dict)  # virtual sha -> combined unified patch
    file_edits: dict[str, list[FileEdit]] = field(default_factory=dict)  # virtual sha -> per-file edits
    session_count: int = 0  # sessions included in the view
    edited_sessions: int = 0  # of those, how many actually changed files
    backends: list[str] = field(default_factory=list)  # backends that contributed
    dropped_sessions: int = 0  # sessions beyond MAX_SESSIONS that were not read

    @property
    def is_empty(self) -> bool:
        return not self.dashboard.stats

    def banner_text(self) -> str:
        """The plain-text notice that this view is a reconstruction, with the counts."""
        backends = ", ".join(self.backends) if self.backends else "no"
        parts = [
            f"BACKTRACE — reconstructed {self.dashboard.total_commits} agent turn(s) from "
            f"{self.session_count} local session(s) ({backends}) in {self.directory}.",
            "A historical view of how past coding-agent conversations changed this directory — "
            "not aGiTrack's live repo tracking.",
        ]
        if self.dropped_sessions:
            parts.append(f"Older sessions beyond the most recent {MAX_SESSIONS} were not included.")
        parts.append(
            "Tip: run 'agitrack --backtrace commit' to bake this history into your git commit "
            "messages, then launch your coding agent through 'agitrack' and everything is fully "
            "tracked going forward."
        )
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Building the view
# ---------------------------------------------------------------------------


@dataclass
class _Source:
    backend: str
    ref_id: str
    updated: float
    base_dir: str  # the directory the session recorded (for making edit paths relative)
    export: Callable[[], ExportedSession | None]
    # The transcript on disk (a file for Claude, a directory for OpenCode). Statting these
    # is how the daemon notices new turns without reading or parsing anything — see
    # _watch_signature.
    watch: tuple[Path, ...] = ()


def _discover(directory: Path) -> list[_Source]:
    """Every Claude and OpenCode session that ran in ``directory`` or beneath it, newest
    first — the sessions to reconstruct. Each backend's discovery is best-effort: a failure
    in one (e.g. the OpenCode CLI missing) never blocks the other."""
    sources: list[_Source] = []
    try:
        # One conversation can be recorded under several project dirs — the aGiTrack worktree it
        # ran in AND the base repo it was resumed from (Claude keys transcripts by cwd). They share
        # a session id but differ in completeness, so keep only the largest (most complete) copy per
        # id; counting both would double every turn and emit duplicate virtual shas.
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
            base = claude._first_cwd(path) or str(directory)
            export: Callable[[], ExportedSession | None] = partial(claude.export_session_at, path, collect_edits=True)
            sources.append(_Source("claude", ref.id, ref.updated, base, export, watch=(path,)))
    except Exception:
        pass
    try:
        seen_opencode: set[str] = set()
        for ref, sdir in opencode.sessions_under(directory):
            if ref.id in seen_opencode:
                continue
            seen_opencode.add(ref.id)
            export = partial(opencode.export_session, Path(sdir), ref.id, collect_edits=True)
            sources.append(_Source("opencode", ref.id, ref.updated, sdir, export, watch=(Path(sdir),)))
    except Exception:
        pass
    sources.sort(key=lambda s: s.updated, reverse=True)
    return sources


# How the served view keeps up with new agent work, without burning CPU to do it.
#
# Rebuilding is expensive (discovery alone reads the head of every transcript; processing a
# long session parses megabytes), so nothing is rebuilt speculatively. Instead three tiers
# guard the work, cheapest first:
#
# 1. A stat-only signature of the transcripts (mtime+size of each session, mtime of the
#    directories that would hold a NEW one). Taken every _WATCH_POLL_SECONDS; when it is
#    unchanged — the overwhelmingly common case — the poll costs a handful of stat() calls
#    and nothing else happens.
# 2. Only when a DIRECTORY changed can a session have appeared or vanished, so only then is
#    discovery re-run. A signature change confined to known files skips it entirely.
# 3. The rebuild reuses the processed result of every session whose file is byte-identical
#    to the last pass, so an active conversation re-processes exactly one session.
#
# _WATCH_MIN_REBUILD_SECONDS then floors how often a rebuild may happen at all: while an
# agent is working its transcript changes every few seconds, and a reconstruction that is
# a minute behind is perfectly useful.
#
# The reuse memo lives in this process only, keyed by the file's identity. It is NOT the
# persisted cache that was removed: that one survived aGiTrack upgrades and served output
# built by superseded code (a subject truncation fix never reached the page). This memo
# dies with the daemon, and the daemon restarts itself whenever aGiTrack is updated, so no
# entry can outlive the code that produced it.
_WATCH_POLL_SECONDS = 15.0
_WATCH_MIN_REBUILD_SECONDS = 60.0


def _stat_signature(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _watch_signature(sources: list[_Source]) -> tuple[dict[str, tuple], dict[str, tuple]]:
    """``(files, dirs)`` stat signatures for everything that could change the view.

    ``files`` covers the known transcripts: a change means an existing session gained turns.
    ``dirs`` covers the directories holding them (and the backends' roots): a change there
    means a session may have been added or removed, which is the only case that needs a
    fresh discovery pass."""
    files: dict[str, tuple] = {}
    dirs: dict[str, tuple] = {}
    watched_dirs: set[Path] = set()
    try:
        watched_dirs.add(claude._projects_root())
    except Exception:
        pass
    for source in sources:
        for path in source.watch:
            signature = _stat_signature(path)
            if signature is not None:
                files[str(path)] = signature
            watched_dirs.add(path.parent)
    for directory in watched_dirs:
        signature = _stat_signature(directory)
        if signature is not None:
            dirs[str(directory)] = signature
    return files, dirs


def build_backtrace(
    directory: Path,
    *,
    max_sessions: int = MAX_SESSIONS,
    progress=None,
    sources: list[_Source] | None = None,
    memo: dict[str, tuple[tuple, dict]] | None = None,
) -> BacktraceView:
    """Reconstruct the backtrace dashboard for ``directory`` from local transcripts.

    ``progress`` (optional) is called ``progress(done, total, phase)`` as work proceeds — during
    discovery (``total`` still 0) and before each session is exported — so a caller can show a
    progress bar. Exporting is the slow part (OpenCode shells out to its CLI per session).

    ``sources`` skips discovery when the caller already has an up-to-date list (the daemon's
    watcher re-discovers only when a directory changed). ``memo``, when given, carries
    processed sessions between rebuilds keyed by the transcript's stat identity, so a rebuild
    re-reads only what actually changed; see the watch notes above for why this in-process
    memo is not the persisted cache that was removed.

    Nothing is cached across processes: a fresh build always reprocesses every session from
    the transcripts, so a processing fix can never be masked by an older result."""
    directory = directory.resolve()
    if progress:
        progress(0, 0, "discovering")
    if sources is None:
        sources = _discover(directory)
    dropped = max(0, len(sources) - max_sessions)
    sources = sources[:max_sessions]
    total = len(sources)

    fresh_sessions: dict[str, dict] = {}
    stats: list[CommitStat] = []
    diffs: dict[str, str] = {}
    file_edits: dict[str, list[FileEdit]] = {}
    backends: set[str] = set()
    edited_sessions = 0
    included_sessions = 0
    # Resuming or rewinding a conversation forks it into a NEW session id that replays the whole
    # earlier transcript, so one real turn shows up in several sessions. The assistant message id is
    # the turn's true identity (it comes from the API), so keep each turn once — otherwise its lines
    # and tokens are counted once per fork. Sessions are walked newest-first, so the newest copy wins.
    seen_turns: set[str] = set()
    assistant_id_by_sha: dict[str, str] = {}

    for index, source in enumerate(sources):
        if progress:
            progress(index, total, "exporting")
        key = f"{source.backend}:{source.ref_id}"
        identity = tuple(_stat_signature(path) for path in source.watch)
        entry = None
        if memo is not None:
            remembered = memo.get(key)
            # Reuse only when the transcript is byte-for-byte what was processed before;
            # any append (a new turn) changes mtime+size and forces a re-read.
            if remembered is not None and remembered[0] == identity and all(identity):
                entry = remembered[1]
        if entry is None:
            entry = _process_source(directory, source)
            if memo is not None and all(identity):
                memo[key] = (identity, entry)
        fresh_sessions[key] = entry
        if not entry.get("stats"):
            continue
        entry_diffs = entry.get("diffs") or {}
        entry_edits = entry.get("file_edits") or {}
        backend = str(entry.get("backend") or source.backend)
        assistant_ids = {str(ref["sha"]): str(ref.get("assistant_id") or "") for ref in entry.get("turn_refs") or []}
        kept = 0
        for stat_dict in entry["stats"]:
            sha = str(stat_dict.get("sha") or "")
            # A message id is only unique WITHIN a backend, so scope the turn's identity by backend.
            assistant_id = _turn_key(backend, assistant_ids.get(sha, ""))
            if assistant_id:
                if assistant_id in seen_turns:
                    continue  # this turn was already taken from a newer fork of the conversation
                seen_turns.add(assistant_id)
            assistant_id_by_sha[sha] = assistant_id
            stats.append(_stat_from_dict(stat_dict))
            if sha in entry_diffs:
                diffs[sha] = entry_diffs[sha]
            if sha in entry_edits:
                file_edits[sha] = [_edit_from_dict(edit) for edit in entry_edits[sha]]
            kept += 1
        if not kept:
            continue  # a pure fork: every one of its turns already came from another session
        included_sessions += 1
        backends.add(str(entry.get("backend") or source.backend))
        if entry.get("edited"):
            edited_sessions += 1
    if progress:
        progress(total, total, "done")

    # Tag turns already committed to git with aGiTrack metadata, so the log shows what is tracked
    # vs. what `--backtrace commit` would still add. (No-op when the directory isn't a git repo.)
    _mark_tracked(directory, fresh_sessions, stats, assistant_id_by_sha)

    stats.sort(key=lambda stat: (stat.timestamp, stat.sha))  # oldest first, like git log order
    dashboard = Dashboard(
        repo=_display_repo(str(directory)),
        branch="",
        stats=stats,
        commit_base="",  # no git remote — the virtual shas are not real commits
        branches=[],
    )
    return BacktraceView(
        directory=_display_repo(str(directory)),
        dashboard=dashboard,
        root=directory,
        diffs=diffs,
        file_edits=file_edits,
        session_count=included_sessions,
        edited_sessions=edited_sessions,
        backends=sorted(backends),
        dropped_sessions=dropped,
    )


def _session_to_stats(
    source: _Source,
    session_id: str,
    turns: list[SessionTurn],
    *,
    start_index: int,
    updated: float,
    bases: list[str],
) -> tuple[list[CommitStat], dict[str, str], dict[str, list[FileEdit]], bool, list[dict], str]:
    """Map a slice of a session's turns (``turns``, whose first is turn number ``start_index`` in
    the whole session) onto virtual :class:`CommitStat`s. Returns ``(stats, combined_diffs,
    per_turn_file_edits, session_changed_files, turn_refs, last_message_id)`` — ``turn_refs`` records
    each turn's sha/message-id/index (for tracked-status and the resume watermark), and
    ``last_message_id`` is the id to resume after next time (incremental re-processing)."""
    stats: list[CommitStat] = []
    diffs: dict[str, str] = {}
    file_edits: dict[str, list[FileEdit]] = {}
    session_changed = False
    turn_refs: list[dict] = []
    last_message_id = ""
    for offset, turn in enumerate(turns):
        index = start_index + offset
        # The resume watermark: the last message id we processed, even for an empty turn, so next
        # time ``turns_after`` can pick up exactly where we left off.
        last_message_id = turn.assistant_message_id or turn.user_message_id or last_message_id
        # Merge AFTER relativizing: two absolute paths (a worktree's and the repo's) can collapse
        # onto the same repo-relative file, and a turn's repeated edits to one file are one change.
        edits = merge_edits_by_path(
            [rel for rel in (_relativize(edit, bases) for edit in turn.edits) if rel is not None]
        )
        has_content = bool(turn.user_prompt.strip() or turn.final_response.strip() or turn.agent_messages or edits)
        if not has_content:
            continue
        sha = _virtual_sha(source.backend, session_id, index, turn.assistant_message_id)
        insertions, deletions = total_lines(edits)
        if edits:
            session_changed = True
            file_edits[sha] = edits
            patch = combine_patches(edits)
            if len(patch) > _MAX_PATCH_CHARS:
                patch = patch[:_MAX_PATCH_CHARS] + "\n… (diff truncated)\n"
            diffs[sha] = patch
        timestamp = turn.ended_at or turn.started_at or int(updated or 0)
        prompts = [p for p in (turn.user_prompt, *turn.queued_followups) if p.strip()]
        stats.append(
            CommitStat(
                sha=sha,
                # No committer exists for a reconstructed turn — the transcript records no
                # git author — so it is left blank (the view hides committer chrome entirely).
                author="",
                email="",
                subject=_subject(turn),
                kind="agent",
                timestamp=timestamp,
                started_at=_iso(turn.started_at),
                ended_at=_iso(turn.ended_at),
                backend=source.backend,
                model=turn.model,
                tokens=_tokens_dict(turn),
                insertions=insertions,
                deletions=deletions,
                prompt=turn.user_prompt,
                user_prompts=prompts,
                message=_message(source, session_id, turn),
            )
        )
        turn_refs.append({"sha": sha, "assistant_id": turn.assistant_message_id or "", "index": index})
    return stats, diffs, file_edits, session_changed, turn_refs, last_message_id


# ---------------------------------------------------------------------------
# Turn -> CommitStat helpers
# ---------------------------------------------------------------------------


def _virtual_sha(backend: str, session_id: str, index: int, assistant_id: str) -> str:
    """A stable, unique 40-hex id for a turn — used as the dashboard row key and the
    ``/diff`` lookup key. It looks like a git sha (so the front-end treats it as one and
    offers the diff button) but is a hash of the turn's identity, never a real object."""
    raw = f"{backend}:{session_id}:{index}:{assistant_id}".encode()
    return hashlib.sha1(raw).hexdigest()


_SUBJECT_MAX = 100


def _subject(turn: SessionTurn) -> str:
    """A one-line label for the turn: the first non-empty line of its prompt, trimmed.
    A long line is cut at a WORD end and marked with an ellipsis — never mid-word
    (mirroring the dashboard's client-side truncSubject; a hard cut remains only when
    a single word fills more than half the cap)."""
    for line in turn.user_prompt.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) <= _SUBJECT_MAX:
            return line
        cut = line[: _SUBJECT_MAX - 1]
        space = cut.rfind(" ")
        if space > _SUBJECT_MAX / 2:
            cut = cut[:space]
        return cut.rstrip() + "…"
    return "(agent turn)"


# Token keys the dashboard expects on a stat (exactly what a real commit's metadata carries):
# the per-bucket counts, never the derived ``total`` or the ``context`` gauge — including
# those would put keys on the stat that the token panel never accounts for.
_TOKEN_KEYS = (
    "input",
    "output",
    "reasoning",
    "cache_read",
    "cache_write",
    "subagent_input",
    "subagent_output",
    "subagent_reasoning",
    "subagent_cache_read",
    "subagent_cache_write",
)


def _tokens_dict(turn: SessionTurn) -> dict[str, int]:
    """The turn's token usage as the dashboard's ``tokens`` dict — the same per-bucket keys a
    real aGiTrack commit records (input/output/reasoning/cache_read/cache_write and their
    subagent_* counterparts), dropping zeros and the derived ``total``/``context`` fields.

    The issue-#14 input convention applies here exactly as in commit metadata
    (commits/message.py): cache-creation tokens ARE fresh input, processed once and written
    to the cache, so ``input`` counts uncached + cache_write. Raw API buckets made a
    cached-heavy turn show an impossible handful of input tokens (fewer than the prompt's
    words) and put cache_write above input, which the convention forbids."""
    data = turn.tokens.to_dict()
    tokens = {key: value for key in _TOKEN_KEYS if isinstance((value := data.get(key)), int) and value > 0}
    if tokens.get("cache_write"):
        tokens["input"] = tokens.get("input", 0) + tokens["cache_write"]
    if tokens.get("subagent_cache_write"):
        tokens["subagent_input"] = tokens.get("subagent_input", 0) + tokens["subagent_cache_write"]
    return tokens


def _message(source: _Source, session_id: str, turn: SessionTurn) -> str:
    """The turn's detail-view text: the subject, a ``# Interaction Trace`` of the user↔agent
    conversation (rendered exactly as an aGiTrack commit renders its trace — secret masking,
    heading nesting, ``## User`` / ``## Agent`` roles), and a ``# aGiTrack Metadata`` block.

    The metadata block carries ONLY what the transcript actually records — backend, model,
    reasoning effort, the backend session id, the conversation's start/end, and the turn's
    token usage — so the log shows the same metadata an aGiTrack commit would, with nothing
    invented (no synthetic session name, committer, or commit hash)."""
    # Build the trace EXACTLY as a default aGiTrack commit does (commit_engine._agent_messages_for):
    # the user prompt, each mid-turn queued follow-up as its own ## User, and ONLY the agent's
    # FINAL response as the ## Agent block. Intermediate agent chatter, internal thinking, tool
    # calls and results are never in a commit, so they must not appear here either.
    trace: list[dict] = []
    if turn.user_prompt.strip():
        trace.append({"role": "user", "content": turn.user_prompt})
    for followup in turn.queued_followups:
        if followup.strip():
            trace.append({"role": "user", "content": followup})
    if turn.final_response.strip():
        trace.append({"role": "agent", "content": turn.final_response})
    body = render_interaction_trace(trace, trace_turn_limit=len(trace) + 1)

    lines = [_subject(turn), ""]
    if body:
        lines += ["# Interaction Trace", "", body, ""]
    lines += _metadata_lines(source, session_id, turn)
    return "\n".join(lines).rstrip() + "\n"


def _metadata_lines(source: _Source, session_id: str, turn: SessionTurn) -> list[str]:
    """The ``# aGiTrack Metadata`` block for a reconstructed turn — real transcript fields only."""
    lines = [
        METADATA_HEADER,
        "commit_type: agent",
        f"backend: {source.backend}",
        f"model: {turn.model or 'unknown'}",
    ]
    if turn.reasoning_effort:
        lines.append(f"reasoning_effort: {turn.reasoning_effort}")
    if session_id:
        lines.append(f"backend_session_id: {session_id}")
    if turn.compaction_count:
        lines.append(f"context_compactions: {turn.compaction_count}")
    if turn.started_at:
        lines.append(f"agent_started_at: {_iso(turn.started_at)}")
    if turn.ended_at:
        lines.append(f"agent_ended_at: {_iso(turn.ended_at)}")
    # Only the per-turn token counters (never the derived context/total lines the commit
    # writer also emits, which aren't a real per-turn figure here).
    lines += [
        line for line in _token_metadata_lines(turn.tokens.to_dict()) if line.startswith("tokens_since_last_commit_")
    ]
    return lines


def _iso(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Edit-path relativization: transcripts record absolute (sometimes sanitized)
# paths; show them relative to the directory so the diff reads like a repo diff.
# ---------------------------------------------------------------------------


def _relativize_bases(directory: Path, session_dir: str) -> list[str]:
    # Longest first: a session that ran in an aGiTrack worktree records a base_dir BENEATH the
    # directory, and the deeper base must win so its edits become repo-relative
    # ("pkg/mod.py") rather than worktree-prefixed (".agitrack/worktrees/foo/pkg/mod.py").
    bases = [str(directory)]
    if session_dir and session_dir not in bases:
        bases.append(session_dir)
    return sorted(bases, key=len, reverse=True)


def _strip_worktree_prefix(rel: str) -> str:
    """``.agitrack/worktrees/<name>/pkg/mod.py`` -> ``pkg/mod.py``. Work an agent did inside an
    aGiTrack worktree is work on the repo (the worktree is merged back), so it must collapse onto
    the same file the repo knows rather than showing up as a separate phantom path."""
    parts = rel.split("/")
    if len(parts) > 3 and parts[0] == ".agitrack" and parts[1] == "worktrees":
        return "/".join(parts[3:])
    return rel


def _relativize(edit: FileEdit, bases: list[str]) -> FileEdit | None:
    """Rewrite an edit's absolute path to one relative to the directory (or the session's
    recorded dir), and rewrite its patch headers to match, so the diff view shows
    repo-relative paths instead of leaking absolute/home paths.

    None when the edit is OUTSIDE the directory (a scratch file in /tmp, a plan under
    ~/.claude): the agent touched it, but it is not a change to this repo, so it must not be
    counted in the repo's AI lines."""
    display = _display_path(edit.path, bases)
    if display is None:
        return None
    if display == edit.path:
        return edit
    return FileEdit(
        path=display,
        insertions=edit.insertions,
        deletions=edit.deletions,
        patch=edit.patch.replace(edit.path, display) if edit.patch else edit.patch,
    )


def _display_path(path: str, bases: list[str]) -> str | None:
    """``path`` as a directory-relative display path, or None when it lies outside the directory."""
    if not path.startswith("/") and not path.startswith("~"):
        return _strip_worktree_prefix(path)  # already relative
    for base in bases:  # longest (most specific) base first
        base = base.rstrip("/")
        if base and (path == base or path.startswith(base + "/")):
            return _strip_worktree_prefix(path[len(base) + 1 :] or path)
    # A shared/sanitized session keeps a worktree-style absolute path (e.g.
    # /Users/user/Code/x/.agitrack/worktrees/foo/pkg/mod.py) that matches no base; show the
    # path after the worktree segment so it still reads as repo-relative.
    marker = "/worktrees/"
    if marker in path:
        tail = path.split(marker, 1)[1]
        parts = tail.split("/", 1)
        if len(parts) == 2 and parts[1]:
            return parts[1]
    return None


# ---------------------------------------------------------------------------
# Serving the backtrace HTML (reuses the live dashboard's renderer/endpoints)
# ---------------------------------------------------------------------------


def _int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = (query.get(key) or [""])[0]
    return int(raw) if raw.lstrip("-").isdigit() else default


def _str(query: dict[str, list[str]], key: str) -> str:
    return (query.get(key) or [""])[0]


def _make_handler(view_source) -> type[http.server.BaseHTTPRequestHandler]:
    """Serve a backtrace view. ``view_source`` is either a fixed :class:`BacktraceView` or a
    zero-argument callable returning the CURRENT one, which is how the daemon serves a
    reconstruction that keeps up with new sessions.

    Everything a request needs beyond the view itself — the rendered page, the file browser,
    the insight cache — is derived once per view and rebuilt lazily the first time a request
    sees a newer one. So the watcher thread only does transcript work; rendering happens on
    demand, and a rebuild nobody looks at costs nothing extra."""
    import threading as _threading

    from agitrack.metrics import learn as learn_page
    from agitrack.metrics.files import backtrace_browser
    from agitrack.metrics.insights import build_insights, context_from_browser
    from agitrack.metrics.web import _filter_stats, aggregates_payload, format_html, log_page

    get_view = view_source if callable(view_source) else (lambda: view_source)

    class _Serving:
        """The derived artefacts for one built view."""

        def __init__(self, view: BacktraceView) -> None:
            self.view = view
            self.browser = backtrace_browser(view.dashboard.stats, view.file_edits, directory=view.root)
            self.insight_cache: dict[tuple, list[dict]] = {}
            self.page = format_html(
                view.dashboard,
                banner_html=_banner_html(view),
                backtrace=True,
                insights=self.insights_for("", "", "", 0, 0),
            ).encode("utf-8")

        def insights_for(self, author: str, backend: str, model: str, frm: int, to: int) -> list[dict]:
            # Scoped to the current filter, exactly as the live dashboard does, so narrowing the
            # time range re-asks the question for that slice. A built view never changes, so each
            # distinct filter is computed once and memoized (bounded).
            key = (author, backend, model, frm, to)
            hit = self.insight_cache.get(key)
            if hit is None:
                stats = _filter_stats(self.view.dashboard, author=author, backend=backend, model=model, frm=frm, to=to)
                files, sha_paths = context_from_browser(self.browser, stats)
                hit = build_insights(stats, files, sha_paths)
                if len(self.insight_cache) >= 16:
                    self.insight_cache.pop(next(iter(self.insight_cache)))
                self.insight_cache[key] = hit
            return hit

    state: dict = {"view": None, "serving": None}
    build_lock = _threading.Lock()

    def serving() -> "_Serving":
        current = get_view()
        cached = state["serving"]
        if cached is not None and state["view"] is current:
            return cached
        with build_lock:
            if state["serving"] is None or state["view"] is not current:
                state["view"] = current
                state["serving"] = _Serving(current)
            return state["serving"]

    # The learning page works over the reconstruction too: same traces, same coach. The
    # served directory may not be a git repo at all — learn then runs with repo=None
    # (identity falls back to the gh login, progress sync reports unavailable, everything
    # else works; the progress log lives in <dir>/.agitrack/learning.json either way).
    learn_root = get_view().root or Path.cwd()
    try:
        learn_repo: GitRepo | None = GitRepo.discover(learn_root)
    except Exception:
        learn_repo = None

    def learn_view(source: str, frm: int, to: int, branch: str) -> tuple[list, list[dict], list[dict]]:
        # ``branch`` is ignored: the reconstruction has no git refs to switch between.
        active = serving()
        stats = _filter_stats(active.view.dashboard, author=source, backend="", model="", frm=frm, to=to)
        return stats, active.insights_for(source, "", "", frm, to), active.browser.files_payload()

    class _BacktraceHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            try:
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                active = serving()  # the newest built view, rendered on first sight
                view, browser, page = active.view, active.browser, active.page
                insights_for = active.insights_for
                if parsed.path in ("/", "/index.html"):
                    self._respond("text/html; charset=utf-8", page, cache_control="no-cache")
                elif parsed.path == "/data":
                    payload = aggregates_payload(
                        view.dashboard,
                        author=_str(query, "author"),
                        backend=_str(query, "backend"),
                        model=_str(query, "model"),
                        frm=_int(query, "from", 0),
                        to=_int(query, "to", 0),
                        granularity=_str(query, "granularity"),
                    )
                    payload["shared_sessions"] = []
                    payload["insights"] = insights_for(
                        _str(query, "author"),
                        _str(query, "backend"),
                        _str(query, "model"),
                        _int(query, "from", 0),
                        _int(query, "to", 0),
                    )
                    self._respond("application/json", json.dumps(payload).encode("utf-8"))
                elif parsed.path == "/log":
                    page_data = log_page(
                        view.dashboard,
                        author=_str(query, "author"),
                        backend=_str(query, "backend"),
                        model=_str(query, "model"),
                        frm=_int(query, "from", 0),
                        to=_int(query, "to", 0),
                        offset=_int(query, "offset", 0),
                        limit=_int(query, "limit", 50),
                        sort=_str(query, "sort"),
                    )
                    self._respond("application/json", json.dumps(page_data).encode("utf-8"))
                elif parsed.path == "/diff":
                    sha = _str(query, "sha")
                    self._respond(
                        "application/json",
                        json.dumps({"sha": sha, "diff": view.diffs.get(sha, "")}).encode("utf-8"),
                    )
                elif parsed.path == "/files":
                    self._respond("application/json", json.dumps({"files": browser.files_payload()}).encode("utf-8"))
                elif parsed.path == "/filelog":
                    self._respond(
                        "application/json", json.dumps(browser.file_log_payload(_str(query, "path"))).encode("utf-8")
                    )
                elif parsed.path == "/filediff":
                    self._respond(
                        "application/json",
                        json.dumps(browser.file_diff(_str(query, "path"), _str(query, "sha"))).encode("utf-8"),
                    )
                elif parsed.path == "/learn":
                    self._respond(
                        "text/html; charset=utf-8",
                        learn_page.learn_html(
                            learn_root, banner_html=learn_page.learn_backtrace_banner(view.directory)
                        ).encode("utf-8"),
                        cache_control="no-cache",
                    )
                elif parsed.path == "/learn/state":
                    payload = learn_page.learn_state(learn_root, learn_repo)
                    # Reconstructed turns carry no committers, so the trace-source select
                    # usually only offers "entire team" here; harmless when some exist.
                    try:
                        payload["committers"] = sorted(
                            {label for stat in view.dashboard.stats for label in view.dashboard.committers_of(stat)}
                        )
                    except Exception:
                        payload["committers"] = []
                    # No git refs in a reconstruction: the page hides its branch selector.
                    payload["branches"] = []
                    payload["branch"] = ""
                    payload["trace_turns"] = sum(
                        1 for stat in view.dashboard.stats if stat.kind in learn_page._AI_KINDS
                    )
                    self._respond("application/json", json.dumps(payload).encode("utf-8"))
                elif parsed.path == "/learn/models":
                    self._respond(
                        "application/json", json.dumps(learn_page.model_options(_str(query, "backend"))).encode("utf-8")
                    )
                else:
                    self.send_error(404, "not found")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            # The learning page's POST endpoints, shared with the live server (see
            # learn.handle_learn_post). Bodies are JSON; a beacon flush may arrive
            # without an application/json header, so parse regardless of content type.
            try:
                parsed = urllib.parse.urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if 0 < length <= 1_000_000 else b""
                try:
                    body = json.loads(raw.decode("utf-8", errors="replace") or "{}")
                except json.JSONDecodeError:
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                payload = learn_page.handle_learn_post(
                    parsed.path, body, root=learn_root, repo=learn_repo, view=learn_view
                )
                if payload is None:
                    self.send_error(404, "not found")
                    return
                self._respond("application/json", json.dumps(payload).encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def _respond(self, content_type: str, body: bytes, *, cache_control: str = "no-store") -> None:
            from agitrack.metrics.server import maybe_gzip

            # Same compression as the live server: the backtrace page is the same ~90 KB of
            # text, and it is usually being read over the connection that made it slow.
            body, encoding = maybe_gzip(body, self.headers.get("Accept-Encoding", ""))
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if encoding:
                self.send_header("Content-Encoding", encoding)
            self.send_header("Content-Length", str(len(body)))
            # HTML pages use "no-cache" so the browser's back/forward cache can restore
            # them instantly (see the live server's _respond); data stays "no-store".
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            """Stay quiet — this is a foreground tool, not a web log."""

    return _BacktraceHandler


def _banner_html(view: BacktraceView) -> str:
    from agitrack.metrics.web import _escape

    # `backtracebanner` (not `updatebanner`) so it renders as a frozen top strip — the CSS pins
    # it like the filter bar, and the JS offsets the filters below it (see the template).
    return f'<div class="backtracebanner">⏪ {_escape(view.banner_text())}</div>'


# ---------------------------------------------------------------------------
# Background daemon — same lifecycle model as `agitrack -d` (#110): a detached child
# serves the reconstruction and dies with the shell that launched it. The handshake lives
# in a per-directory temp file (NOT under the directory), so it works in a directory that is
# not a git repo and never collides with the live dashboard's own handshake.
# ---------------------------------------------------------------------------


def _state_dir() -> Path:
    import tempfile

    return Path(tempfile.gettempdir()) / "agitrack-backtrace"


def _dir_key(directory: Path) -> str:
    return hashlib.sha1(str(directory.resolve()).encode()).hexdigest()[:16]


def _handshake_path(directory: Path) -> Path:
    return _state_dir() / f"{_dir_key(directory)}.json"


def _log_path(directory: Path) -> Path:
    return _state_dir() / f"{_dir_key(directory)}.log"


def _read_handshake(directory: Path) -> dict | None:
    try:
        with _handshake_path(directory).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_handshake(directory: Path, record: dict) -> None:
    from agitrack.fileio import atomic_write_text

    atomic_write_text(_handshake_path(directory), json.dumps(record))


def _clear_handshake(directory: Path) -> None:
    try:
        _handshake_path(directory).unlink()
    except OSError:
        pass


def _progress_path(directory: Path) -> Path:
    return _state_dir() / f"{_dir_key(directory)}.progress.json"


def _write_progress(directory: Path, done: int, total: int, phase: str) -> None:
    """The building child records its progress here; the launching parent polls it to draw a bar."""
    import time

    try:
        from agitrack.fileio import atomic_write_text

        atomic_write_text(
            _progress_path(directory),
            json.dumps({"done": done, "total": total, "phase": phase, "t": int(time.time())}),
        )
    except OSError:
        pass


def _read_progress(directory: Path) -> dict | None:
    try:
        return json.loads(_progress_path(directory).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _clear_progress(directory: Path) -> None:
    try:
        _progress_path(directory).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
def _empty_entry(source: _Source) -> dict:
    return {
        "updated": source.updated,
        "backend": source.backend,
        "edited": False,
        "stats": [],
        "diffs": {},
        "file_edits": {},
        "turn_refs": [],
        "last_message_id": "",
        "turn_count": 0,
    }


def _process_source(directory: Path, source: _Source) -> dict:
    """Export and process ONE session into a JSON-serializable entry (its stats, diffs,
    and per-file edits) — the slow step of a build. Always processes the WHOLE session:
    the persisted cache (and its intra-session incremental resume) was removed after it
    served stale processed output across a processing fix."""
    try:
        exported = source.export()
    except Exception:
        exported = None
    if exported is None:
        return _empty_entry(source)
    bases = _relativize_bases(directory, source.base_dir)
    all_turns = exported.turns

    stats, diffs, file_edits, changed, turn_refs, last_id = _session_to_stats(
        source,
        exported.session_id,
        all_turns,
        start_index=0,
        updated=float(exported.updated or source.updated),
        bases=bases,
    )
    stat_dicts = [_stat_to_dict(stat) for stat in stats]
    fedit_dicts = {sha: [_edit_to_dict(edit) for edit in edits] for sha, edits in file_edits.items()}
    return {
        "updated": source.updated,
        "backend": source.backend,
        "edited": changed,
        "stats": stat_dicts,
        "diffs": diffs,
        "file_edits": fedit_dicts,
        "turn_refs": turn_refs,
        "last_message_id": last_id,
        "turn_count": len(all_turns),
    }


def _stat_to_dict(stat: CommitStat) -> dict:
    return {
        "sha": stat.sha,
        "author": stat.author,
        "email": stat.email,
        "subject": stat.subject,
        "kind": stat.kind,
        "timestamp": stat.timestamp,
        "started_at": stat.started_at,
        "ended_at": stat.ended_at,
        "backend": stat.backend,
        "model": stat.model,
        "tokens": stat.tokens,
        "insertions": stat.insertions,
        "deletions": stat.deletions,
        "prompt": stat.prompt,
        "user_prompts": stat.user_prompts,
        "message": stat.message,
    }


def _stat_from_dict(data: dict) -> CommitStat:
    return CommitStat(
        sha=data["sha"],
        author=data["author"],
        email=data["email"],
        subject=data["subject"],
        kind=data["kind"],
        timestamp=data["timestamp"],
        started_at=data["started_at"],
        ended_at=data["ended_at"],
        backend=data["backend"],
        model=data["model"],
        tokens=data["tokens"],
        insertions=data["insertions"],
        deletions=data["deletions"],
        prompt=data["prompt"],
        user_prompts=data["user_prompts"],
        message=data["message"],
    )


def _edit_to_dict(edit: FileEdit) -> dict:
    return {"path": edit.path, "insertions": edit.insertions, "deletions": edit.deletions, "patch": edit.patch}


def _edit_from_dict(data: dict) -> FileEdit:
    return FileEdit(path=data["path"], insertions=data["insertions"], deletions=data["deletions"], patch=data["patch"])


# ---------------------------------------------------------------------------
# Tracked status: which reconstructed turns are already committed to git with aGiTrack metadata.
# ---------------------------------------------------------------------------


def _turn_key(backend: str, assistant_id: str) -> str:
    """A turn's identity across forked sessions: its assistant message id, scoped by backend (the
    id is only unique within one backend). Empty when the turn carries no message id."""
    return f"{backend}:{assistant_id}" if assistant_id else ""


def _mark_tracked(
    directory: Path, sessions: dict, stats: list[CommitStat], assistant_id_by_sha: dict[str, str]
) -> None:
    """Set ``stat.tracked`` on the reconstructed turns already committed with aGiTrack metadata.

    aGiTrack commits record ``backend_session_id`` and ``conversation_anchor`` (the last message id
    the commit covered), so within a session every turn up to the LATEST committed anchor is already
    tracked. Matches reconstructed turns (by session + message id + order via the cached turn_refs)
    against those anchors. A no-op when the directory is not a git repo.

    Also matches on the assistant message id, not just the virtual sha: forked conversations share
    turns, and the copy kept in ``stats`` may come from a different session id than the one the
    commit recorded — the message id is what identifies the turn across forks."""
    anchors = _committed_anchors(directory)
    if not anchors:
        return
    tracked_shas: set[str] = set()
    tracked_assistant_ids: set[str] = set()
    for key, entry in sessions.items():
        backend, _, session_id = key.partition(":")
        session_anchors = anchors.get(session_id)
        refs = entry.get("turn_refs") or []
        if not session_anchors or not refs:
            continue
        anchored_indices = [int(ref["index"]) for ref in refs if ref.get("assistant_id") in session_anchors]
        if not anchored_indices:
            continue
        cutoff = max(anchored_indices)  # every turn at/before the latest committed anchor is tracked
        for ref in refs:
            if int(ref["index"]) <= cutoff:
                tracked_shas.add(str(ref["sha"]))
                turn_key = _turn_key(str(entry.get("backend") or backend), str(ref.get("assistant_id") or ""))
                if turn_key:
                    tracked_assistant_ids.add(turn_key)
    if not tracked_shas and not tracked_assistant_ids:
        return
    for stat in stats:
        assistant_id = assistant_id_by_sha.get(stat.sha, "")
        if stat.sha in tracked_shas or (assistant_id and assistant_id in tracked_assistant_ids):
            stat.tracked = True


def _committed_anchors(directory: Path) -> dict[str, set[str]]:
    """``backend_session_id -> {conversation_anchor message ids}`` from the repo's aGiTrack commits,
    or ``{}`` when ``directory`` is not a git repo. These are the committed watermarks per session."""
    try:
        from agitrack.git import GitRepo

        repo = GitRepo.discover(directory)
    except Exception:
        return {}
    out: dict[str, set[str]] = {}
    body = repo._run(["git", "log", "--format=%B%x00", "HEAD", "--"], check=False).stdout
    for chunk in body.split("\x00"):
        session_id = anchor = None
        for line in chunk.splitlines():
            if line.startswith("backend_session_id:"):
                session_id = line.split(":", 1)[1].strip()
            elif line.startswith("conversation_anchor:"):
                anchor = line.split(":", 1)[1].strip()
        if session_id and anchor:
            out.setdefault(session_id, set()).add(anchor)
    return out


def _running_handshake(directory: Path) -> dict | None:
    """The handshake of a backtrace daemon that is still alive for ``directory``, else None
    (a stale record from a crashed daemon is cleared)."""
    from agitrack.proc import pid_alive

    record = _read_handshake(directory)
    if record is None:
        return None
    pid = record.get("pid")
    if isinstance(pid, int) and pid_alive(pid):
        return record
    _clear_handshake(directory)
    return None


def _spawn_backtrace_child(directory: Path, *, owner_pid: int | None = None, port: int | None = None):
    """Launch the detached backtrace child (shared by the CLI start and the update-restart
    handoff). The child must load the INSTALLED aGiTrack, never a stray ``agitrack/``
    package in the target directory: the backtraced directory can itself be the aGiTrack
    source checkout, and ``python -m agitrack`` would otherwise import that (older) copy
    from cwd — so it runs from a neutral state dir with PYTHONSAFEPATH keeping cwd off
    ``sys.path``."""
    import os
    import subprocess
    import sys

    from agitrack.proc import detach_kwargs

    cmd = [
        sys.executable,
        "-m",
        "agitrack",
        "--repo",
        str(directory),
        "--backtrace-serve",
    ]
    if owner_pid is not None:
        cmd += ["--dashboard-owner-pid", str(owner_pid)]
    if port is not None:
        cmd += ["--dashboard-port", str(port)]
    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONSAFEPATH"] = "1"
    log = _open_log(directory)
    try:
        return subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=str(state_dir), env=env, **detach_kwargs()
        )
    finally:
        if log is not subprocess.DEVNULL:
            log.close()


def start_backtrace_daemon(
    directory: Path, *, owner_pid: int | None = None, open_browser: bool = True, timeout: float = 90.0
) -> int:
    """`agitrack --backtrace` (html): start the background backtrace daemon for ``directory``,
    then return to the shell. The daemon is NOT bound to the launching terminal: it keeps
    serving until `agitrack --backtrace stop` (and restarts itself after aGiTrack updates).

    Like ``agitrack -d``, re-running while a daemon is already up RESTARTS it — the old one is
    stopped and a fresh one started on the same port, so the URL is unchanged and a re-run
    (e.g. after an aGiTrack update, or to pick up new sessions) always serves current code.

    ``timeout`` is the STALL tolerance, not a total deadline: the first build scans and exports
    every local session (OpenCode shells out per session) and can take minutes, so a progress bar
    is shown and the wait continues as long as progress is being made, giving up only if the child
    makes none for ``timeout`` seconds."""

    from agitrack.metrics.daemon import _terminate_and_wait

    running = _running_handshake(directory)
    reuse_port: int | None = None
    if running is not None:
        old_pid = int(running["pid"])
        raw_port = running.get("port")
        reuse_port = int(raw_port) if isinstance(raw_port, int) else None
        # Stop the old daemon and wait for the socket to be released so the replacement can
        # bind the SAME port. If it lingers, the child's port fallback still yields a working
        # (just different) URL rather than a failure.
        _terminate_and_wait(old_pid, timeout=5.0)
        _clear_handshake(directory)
        print(f"Restarting the backtrace daemon (was pid {old_pid}).")

    print(f"Scanning local coding-agent transcripts for {_abbreviate_home(str(directory))} … (this can take a moment)")
    proc = _spawn_backtrace_child(directory, owner_pid=owner_pid, port=reuse_port)

    # Wait for the child to finish reconstructing (it publishes the handshake when ready), showing
    # a live progress bar. The wait is STALL-based, not a fixed deadline: a big directory can take
    # minutes to export, so as long as the child keeps making progress we keep waiting; we only
    # give up if it makes no progress for `timeout` seconds or dies.
    record = _wait_for_backtrace(directory, proc, stall_seconds=timeout)
    if record is None:
        print(f"The backtrace daemon did not start (or stalled). See {_log_path(directory)} for details.")
        return 1
    if record.get("empty"):
        _clear_handshake(directory)
        print(_empty_message(directory))
        return 0
    from agitrack.metrics.server import exposure_note

    url = str(record.get("url", ""))
    print(
        f"aGiTrack backtrace daemon live at {url} (pid {record.get('pid')}).\n"
        + record.get("banner", "")
        + "\n"
        + exposure_note(str(record.get("host", "")))
        + "Runs in the background (surviving this terminal) until `agitrack --backtrace stop`."
    )
    _maybe_open(url, record, open_browser)
    return 0


def _wait_for_backtrace(directory: Path, proc, *, stall_seconds: float) -> dict | None:
    """Wait for the detached child to publish its handshake, drawing a progress bar (on a TTY) from
    the progress file the child writes. There is NO overall deadline — a large repo can take as
    long as it needs — so this keeps waiting as long as the child is alive AND making progress. It
    returns None only if the child exits without a handshake, or truly hangs (no progress at all for
    ``stall_seconds``; a single session export is bounded well under that)."""
    import sys
    import time

    _clear_progress(directory)  # start from a clean slate — ignore a previous run's leftover
    tty = sys.stdout.isatty()
    last_seen: dict | None = None
    last_change = time.monotonic()
    try:
        while True:
            record = _read_handshake(directory)
            if record is not None and record.get("pid") == proc.pid:
                return record
            if proc.poll() is not None:
                # The child exited — it may have written the handshake (incl. the empty case) just
                # before exiting, so re-check once before giving up.
                record = _read_handshake(directory)
                return record if record is not None and record.get("pid") == proc.pid else None
            prog = _read_progress(directory)
            if prog != last_seen:
                last_seen = prog
                last_change = time.monotonic()
            if tty:
                _render_progress(prog)
            if time.monotonic() - last_change > stall_seconds:
                return None  # the child is alive but hasn't advanced at all — treat as hung
            time.sleep(0.2)
    finally:
        if tty:
            sys.stdout.write("\r" + " " * 72 + "\r")
            sys.stdout.flush()


def _watch_transcripts(directory: Path, live: dict, memo: dict, stop) -> None:
    """Keep ``live["view"]`` current as agent sessions come and go.

    Runs the three-tier guard described above the constants: a stat-only signature every
    poll, discovery only when a directory changed, and a memoized rebuild that re-reads
    only the transcripts that actually moved. Rebuilds are floored at
    ``_WATCH_MIN_REBUILD_SECONDS`` so a conversation in progress — whose transcript grows
    every few seconds — cannot spin this thread.

    Never raises: the daemon must keep serving the view it already has, whatever a
    transcript does.
    """
    import time as _time

    files, dirs = _watch_signature(live["sources"])
    last_rebuild = _time.monotonic()
    while not stop.wait(_WATCH_POLL_SECONDS):
        try:
            sources = live["sources"]
            new_files, new_dirs = _watch_signature(sources)
            if new_files == files and new_dirs == dirs:
                continue  # nothing on disk moved: the poll cost a few stat() calls
            if _time.monotonic() - last_rebuild < _WATCH_MIN_REBUILD_SECONDS:
                continue  # changed, but too soon to spend a rebuild on it; caught next poll
            rediscover = new_dirs != dirs  # a session may have appeared or been removed
            files, dirs = new_files, new_dirs
            if rediscover:
                sources = _discover(directory)
            view = build_backtrace(directory, sources=sources, memo=memo)
            if view.is_empty:
                continue  # keep serving what we have rather than an empty page
            live["sources"] = sources
            live["view"] = view
            last_rebuild = _time.monotonic()
            # Re-signature AFTER the rebuild: processing takes seconds during which the
            # transcript may have grown again, and that growth belongs to the next pass.
            files, dirs = _watch_signature(sources)
            # Drop memo entries for sessions that are gone, so a long-lived daemon cannot
            # accumulate the processed output of transcripts that no longer exist.
            keys = {f"{source.backend}:{source.ref_id}" for source in sources}
            for stale in [key for key in memo if key not in keys]:
                memo.pop(stale, None)
        except Exception:
            continue


def _render_progress(prog: dict | None) -> None:
    """Draw the reconstruction progress on one rewritten terminal line."""
    import sys

    if not prog:
        message = "  Discovering local coding-agent sessions…"
    else:
        done, total = int(prog.get("done", 0)), int(prog.get("total", 0))
        if total > 0:
            width = 24
            filled = min(width, int(width * done / total))
            bar = "█" * filled + "░" * (width - filled)
            message = f"  Reconstructing  [{bar}]  {done}/{total} sessions"
        else:
            message = "  Reconstructing…"
    sys.stdout.write("\r" + message.ljust(72))
    sys.stdout.flush()


def stop_backtrace_daemon(directory: Path) -> int:
    """`agitrack --backtrace stop`: stop the background backtrace daemon for ``directory``."""
    import time

    from agitrack.proc import pid_alive, terminate_pid

    record = _running_handshake(directory)
    if record is None:
        _clear_handshake(directory)
        print("No backtrace daemon is running for this directory.")
        return 0
    pid = int(record["pid"])
    terminate_pid(pid)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.05)
    _clear_handshake(directory)
    print(f"Stopped the backtrace daemon (pid {pid}).")
    return 0


def backtrace_daemon_status(directory: Path) -> int:
    """`agitrack --backtrace status`: report whether a backtrace daemon is running."""
    record = _running_handshake(directory)
    if record is None:
        print("No backtrace daemon is running for this directory.")
        return 0
    print(f"aGiTrack backtrace daemon running at {record.get('url', '')} (pid {record.get('pid')}).")
    return 0


def run_backtrace_daemon(
    directory: Path, *, owner_pid: int | None = None, host: str | None = None, port: int | None = None
) -> int:
    """The detached child: build the reconstruction once, then serve it until told to stop
    (SIGTERM/SIGINT) or until the owner pid disappears. Publishes a handshake so the launcher
    can show the URL; if there is nothing to show, records that and exits."""
    import os
    import signal
    import threading
    import time

    from agitrack.metrics.daemon import _watch_owner
    from agitrack.metrics.server import (
        DEFAULT_PORT,
        _DashboardServer,
        bind_scanning,
        dashboard_url,
        default_bind_host,
    )

    bind_host = default_bind_host() if host is None else host
    preferred_port = DEFAULT_PORT if port is None else port

    # Report build progress to a file the launching parent polls to draw a progress bar; clear it
    # once the (potentially slow) reconstruction is done.
    sources = _discover(directory)
    memo: dict[str, tuple[tuple, dict]] = {}
    view = build_backtrace(
        directory,
        sources=sources,
        memo=memo,
        progress=lambda done, total, phase: _write_progress(directory, done, total, phase),
    )
    _clear_progress(directory)
    if view.is_empty:
        _write_handshake(directory, {"pid": os.getpid(), "empty": True})
        return 0

    # The served view is swapped in place as new agent work lands, so the page's own 30s
    # refresh shows turns and tokens from sessions that ran after the daemon started.
    live: dict = {"view": view, "sources": sources}
    watch_stop = threading.Event()
    threading.Thread(target=_watch_transcripts, args=(directory, live, memo, watch_stop), daemon=True).start()

    handler = _make_handler(lambda: live["view"])
    from agitrack.update import restart as update_restart

    # Mutable view of the CURRENT serve cycle for the signal handlers (installed once):
    # an explicit stop must always work, including between a failed update-restart and
    # the next cycle's bind (see the retry loop below).
    current: dict = {"server": None, "stop": None}
    explicit_stop = threading.Event()

    def _request_shutdown(*_: object) -> None:
        explicit_stop.set()
        stop = current.get("stop")
        server = current.get("server")
        if stop is not None:
            stop.set()
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    while True:
        # Consecutive ports (8765, 8766, …) so a backtrace started alongside a dashboard — or a
        # second backtrace — gets a predictable neighbouring URL instead of a random one.
        server = bind_scanning(lambda address: _DashboardServer(address, handler), bind_host, preferred_port)
        bound_port = int(server.server_address[1])
        url = dashboard_url(bind_host, bound_port)
        _write_handshake(
            directory,
            {
                "pid": os.getpid(),
                "host": bind_host,
                "port": bound_port,
                "url": url,
                "banner": view.banner_text(),
                "started": int(time.time()),
            },
        )
        from agitrack import daemons

        daemons.register("backtrace", directory, url=url)

        stop = threading.Event()
        current.update(server=server, stop=stop)
        if explicit_stop.is_set():  # a stop landed while we were between cycles
            stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        if owner_pid:
            threading.Thread(
                target=_watch_owner,
                args=(owner_pid, stop, _request_shutdown),
                daemon=True,
                name="agitrack-backtrace-owner-watch",
            ).start()

        restart_wanted = threading.Event()

        def _restart_for_update(_version: str, *, _stop=stop, _server=server) -> None:
            restart_wanted.set()
            _stop.set()
            threading.Thread(target=_server.shutdown, daemon=True).start()

        update_restart.watch_for_update(stop, _restart_for_update)

        try:
            server.serve_forever()
        finally:
            server.server_close()
            _clear_handshake(directory)
            daemons.deregister()
        if explicit_stop.is_set() or not restart_wanted.is_set():
            return 0
        # Restart by SPAWN-AND-VERIFY, not exec: a replacement running a broken update
        # could crash on startup, and an exec'd-over process has nothing left to retry
        # with. The old daemon only exits once the replacement provably serves (its own
        # handshake, correlated on its pid); otherwise it reaps the corpse and serves
        # on, retrying on the next detection — until the user stops it.
        try:
            child = _spawn_backtrace_child(directory, port=bound_port)
            record = _wait_for_backtrace(directory, child, stall_seconds=90.0)
        except Exception:
            child, record = None, None
        if record is not None:
            print(f"restarted onto the updated aGiTrack (pid {record.get('pid')}).", flush=True)
            return 0
        if child is not None and child.poll() is None:
            child.terminate()
        print("update restart failed; still serving on the current version and retrying.", flush=True)
        preferred_port = bound_port


def _open_log(directory: Path):
    import subprocess

    try:
        path = _log_path(directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("ab")
    except OSError:
        return subprocess.DEVNULL


def _maybe_open(url: str, record: dict, open_browser: bool) -> None:
    from agitrack.metrics.server import DEFAULT_PORT, open_dashboard_in_browser, remote_access_help

    if not (open_browser and url):
        return
    if not open_dashboard_in_browser(url):
        port = record.get("port", DEFAULT_PORT)
        print(
            remote_access_help(
                url,
                int(port) if isinstance(port, int) else DEFAULT_PORT,
                bind_host=str(record.get("host", "")),
            )
        )


def render_backtrace_text(directory: Path) -> str:
    """A one-shot text backtrace report for ``directory`` (the ``--backtrace text`` output)."""
    from agitrack.metrics.render import format_dashboard

    view = build_backtrace(directory)
    if view.is_empty:
        return _empty_message(directory)
    return f"{view.banner_text()}\n\n{format_dashboard(view.dashboard)}"


def _empty_message(directory: Path) -> str:
    return (
        f"No local coding-agent history found for {_abbreviate_home(str(directory))}.\n"
        "Backtrace reconstructs past Claude or OpenCode sessions that ran in this directory "
        "(or a subdirectory) and changed files — none were found here."
    )
