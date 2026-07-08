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
from agitrack.metrics.collect import CommitStat, Dashboard, _abbreviate_home
from agitrack.transcripts import claude, opencode
from agitrack.transcripts.edits import combine_patches, total_lines
from agitrack.transcripts.types import ExportedSession, FileEdit, SessionTurn, turns_after

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


def _discover(directory: Path) -> list[_Source]:
    """Every Claude and OpenCode session that ran in ``directory`` or beneath it, newest
    first — the sessions to reconstruct. Each backend's discovery is best-effort: a failure
    in one (e.g. the OpenCode CLI missing) never blocks the other."""
    sources: list[_Source] = []
    try:
        for ref, path in claude.sessions_under(directory):
            base = claude._first_cwd(path) or str(directory)
            export: Callable[[], ExportedSession | None] = partial(claude.export_session_at, path, collect_edits=True)
            sources.append(_Source("claude", ref.id, ref.updated, base, export))
    except Exception:
        pass
    try:
        for ref, sdir in opencode.sessions_under(directory):
            export = partial(opencode.export_session, Path(sdir), ref.id, collect_edits=True)
            sources.append(_Source("opencode", ref.id, ref.updated, sdir, export))
    except Exception:
        pass
    sources.sort(key=lambda s: s.updated, reverse=True)
    return sources


def build_backtrace(
    directory: Path, *, max_sessions: int = MAX_SESSIONS, progress=None, use_cache: bool = True
) -> BacktraceView:
    """Reconstruct the backtrace dashboard for ``directory`` from local transcripts.

    ``progress`` (optional) is called ``progress(done, total, phase)`` as work proceeds — during
    discovery (``total`` still 0) and before each session is exported — so a caller can show a
    progress bar. Exporting is the slow part (OpenCode shells out to its CLI per session).

    ``use_cache`` (default on) persists each session's PROCESSED result and reuses it next time when
    the transcript hasn't changed, so only new/changed sessions are re-exported — a repeat run on a
    directory with many sessions is near-instant instead of rebuilding everything."""
    directory = directory.resolve()
    if progress:
        progress(0, 0, "discovering")
    sources = _discover(directory)
    dropped = max(0, len(sources) - max_sessions)
    sources = sources[:max_sessions]
    total = len(sources)

    cached_sessions = _load_cache(directory)["sessions"] if use_cache else {}
    fresh_sessions: dict[str, dict] = {}
    stats: list[CommitStat] = []
    diffs: dict[str, str] = {}
    file_edits: dict[str, list[FileEdit]] = {}
    backends: set[str] = set()
    edited_sessions = 0
    included_sessions = 0

    for index, source in enumerate(sources):
        if progress:
            progress(index, total, "exporting")
        key = f"{source.backend}:{source.ref_id}"
        cached = cached_sessions.get(key)
        # Reuse the cached processed result when the transcript hasn't changed since (its recorded
        # last-updated time didn't advance); otherwise re-export and re-process this one session.
        if isinstance(cached, dict) and float(cached.get("updated", -1)) >= source.updated:
            entry = cached
        else:
            # Changed (or new): re-export, but reuse the cached prefix and process only new turns.
            entry = _process_source(directory, source, cached=cached if isinstance(cached, dict) else None)
        fresh_sessions[key] = entry
        if not entry.get("stats"):
            continue
        included_sessions += 1
        backends.add(str(entry.get("backend") or source.backend))
        if entry.get("edited"):
            edited_sessions += 1
        stats.extend(_stat_from_dict(d) for d in entry["stats"])
        diffs.update(entry.get("diffs") or {})
        for sha, edit_dicts in (entry.get("file_edits") or {}).items():
            file_edits[sha] = [_edit_from_dict(e) for e in edit_dicts]
    if progress:
        progress(total, total, "done")
    if use_cache:
        _save_cache(directory, {"version": _CACHE_VERSION, "sessions": fresh_sessions})

    # Tag turns already committed to git with aGiTrack metadata, so the log shows what is tracked
    # vs. what `--backtrace commit` would still add. (No-op when the directory isn't a git repo.)
    _mark_tracked(directory, fresh_sessions, stats)

    stats.sort(key=lambda stat: (stat.timestamp, stat.sha))  # oldest first, like git log order
    dashboard = Dashboard(
        repo=_abbreviate_home(str(directory)),
        branch="",
        stats=stats,
        commit_base="",  # no git remote — the virtual shas are not real commits
        branches=[],
    )
    return BacktraceView(
        directory=_abbreviate_home(str(directory)),
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
        edits = [_relativize(edit, bases) for edit in turn.edits]
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


def _subject(turn: SessionTurn) -> str:
    """A one-line label for the turn: the first non-empty line of its prompt, trimmed."""
    for line in turn.user_prompt.splitlines():
        line = line.strip()
        if line:
            return line[:100]
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
    subagent_* counterparts), dropping zeros and the derived ``total``/``context`` fields."""
    data = turn.tokens.to_dict()
    return {key: value for key in _TOKEN_KEYS if isinstance((value := data.get(key)), int) and value > 0}


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
    bases = [str(directory)]
    if session_dir and session_dir not in bases:
        bases.append(session_dir)
    return bases


def _relativize(edit: FileEdit, bases: list[str]) -> FileEdit:
    """Rewrite an edit's absolute path to one relative to the directory (or the session's
    recorded dir), and rewrite its patch headers to match, so the diff view shows
    repo-relative paths instead of leaking absolute/home paths."""
    display = _display_path(edit.path, bases)
    if display == edit.path:
        return edit
    return FileEdit(
        path=display,
        insertions=edit.insertions,
        deletions=edit.deletions,
        patch=edit.patch.replace(edit.path, display) if edit.patch else edit.patch,
    )


def _display_path(path: str, bases: list[str]) -> str:
    for base in bases:
        base = base.rstrip("/")
        if base and (path == base or path.startswith(base + "/")):
            return path[len(base) + 1 :] or path
    # A shared/sanitized session keeps a worktree-style absolute path (e.g.
    # /Users/user/Code/x/.agitrack/worktrees/foo/pkg/mod.py) that matches no base; show the
    # path after the worktree segment so it still reads as repo-relative.
    marker = "/worktrees/"
    if marker in path:
        tail = path.split(marker, 1)[1]
        parts = tail.split("/", 1)
        if len(parts) == 2 and parts[1]:
            return parts[1]
    return _abbreviate_home(path)


# ---------------------------------------------------------------------------
# Serving the backtrace HTML (reuses the live dashboard's renderer/endpoints)
# ---------------------------------------------------------------------------


def _int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = (query.get(key) or [""])[0]
    return int(raw) if raw.lstrip("-").isdigit() else default


def _str(query: dict[str, list[str]], key: str) -> str:
    return (query.get(key) or [""])[0]


def _make_handler(view: BacktraceView) -> type[http.server.BaseHTTPRequestHandler]:
    from agitrack.metrics.files import backtrace_browser
    from agitrack.metrics.web import aggregates_payload, format_html, log_page

    banner = _banner_html(view)
    page = format_html(view.dashboard, banner_html=banner, backtrace=True).encode("utf-8")
    browser = backtrace_browser(view.dashboard.stats, view.file_edits, directory=view.root)

    class _BacktraceHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            try:
                parsed = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path in ("/", "/index.html"):
                    self._respond("text/html; charset=utf-8", page)
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
                else:
                    self.send_error(404, "not found")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def _respond(self, content_type: str, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
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
    path = _handshake_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(record, handle)
    import os

    os.replace(tmp, path)


def _clear_handshake(directory: Path) -> None:
    try:
        _handshake_path(directory).unlink()
    except OSError:
        pass


def _progress_path(directory: Path) -> Path:
    return _state_dir() / f"{_dir_key(directory)}.progress.json"


def _write_progress(directory: Path, done: int, total: int, phase: str) -> None:
    """The building child records its progress here; the launching parent polls it to draw a bar."""
    import os
    import time

    try:
        path = _progress_path(directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"done": done, "total": total, "phase": phase, "t": int(time.time())}))
        os.replace(tmp, path)
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
# Incremental cache: the processed per-session result is saved so a later run only re-exports the
# transcripts that actually changed (OpenCode exports are slow), instead of rebuilding everything.
# ---------------------------------------------------------------------------

# Bump whenever the per-session processing changes shape/logic so a stale cache is ignored.
_CACHE_VERSION = 1


def _cache_path(directory: Path) -> Path:
    return _state_dir() / f"{_dir_key(directory)}.cache.json"


def _load_cache(directory: Path) -> dict:
    try:
        data = json.loads(_cache_path(directory).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict) and data.get("version") == _CACHE_VERSION and isinstance(data.get("sessions"), dict):
        return data
    return {"version": _CACHE_VERSION, "sessions": {}}


def _save_cache(directory: Path, cache: dict) -> None:
    import os

    try:
        path = _cache_path(directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


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


def _process_source(directory: Path, source: _Source, cached: dict | None = None) -> dict:
    """Export and process ONE session into a JSON-serializable cache entry (its stats, diffs, and
    per-file edits). This is the slow step the cache lets a repeat run skip.

    When ``cached`` (a prior entry for this SAME session that has since grown) is given and its
    watermark is still present in the transcript, only the turns AFTER the watermark are processed
    and the cached prefix is reused — so a large, actively-growing session isn't re-synthesized from
    turn 1 each run (intra-session incremental)."""
    try:
        exported = source.export()
    except Exception:
        exported = None
    if exported is None:
        return _empty_entry(source)
    bases = _relativize_bases(directory, source.base_dir)
    all_turns = exported.turns

    reuse: dict | None = None
    turns = all_turns
    start_index = 0
    if isinstance(cached, dict) and cached.get("last_message_id") and cached.get("stats") is not None:
        after = turns_after(exported, str(cached["last_message_id"]))
        if len(after) < len(all_turns):  # watermark found — resume just after it
            reuse = cached
            turns = after
            start_index = len(all_turns) - len(after)

    stats, diffs, file_edits, changed, turn_refs, last_id = _session_to_stats(
        source,
        exported.session_id,
        turns,
        start_index=start_index,
        updated=float(exported.updated or source.updated),
        bases=bases,
    )
    stat_dicts = [_stat_to_dict(stat) for stat in stats]
    fedit_dicts = {sha: [_edit_to_dict(edit) for edit in edits] for sha, edits in file_edits.items()}
    if reuse is not None:  # prepend the reused prefix (already serialized)
        stat_dicts = list(reuse.get("stats") or []) + stat_dicts
        diffs = {**(reuse.get("diffs") or {}), **diffs}
        fedit_dicts = {**(reuse.get("file_edits") or {}), **fedit_dicts}
        turn_refs = list(reuse.get("turn_refs") or []) + turn_refs
        changed = changed or bool(reuse.get("edited"))
        last_id = last_id or str(reuse.get("last_message_id") or "")
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


def _mark_tracked(directory: Path, sessions: dict, stats: list[CommitStat]) -> None:
    """Set ``stat.tracked`` on the reconstructed turns already committed with aGiTrack metadata.

    aGiTrack commits record ``backend_session_id`` and ``conversation_anchor`` (the last message id
    the commit covered), so within a session every turn up to the LATEST committed anchor is already
    tracked. Matches reconstructed turns (by session + message id + order via the cached turn_refs)
    against those anchors. A no-op when the directory is not a git repo."""
    anchors = _committed_anchors(directory)
    if not anchors:
        return
    tracked_shas: set[str] = set()
    for key, entry in sessions.items():
        session_id = key.partition(":")[2]
        session_anchors = anchors.get(session_id)
        refs = entry.get("turn_refs") or []
        if not session_anchors or not refs:
            continue
        anchored_indices = [int(ref["index"]) for ref in refs if ref.get("assistant_id") in session_anchors]
        if not anchored_indices:
            continue
        cutoff = max(anchored_indices)  # every turn at/before the latest committed anchor is tracked
        tracked_shas.update(str(ref["sha"]) for ref in refs if int(ref["index"]) <= cutoff)
    if tracked_shas:
        for stat in stats:
            if stat.sha in tracked_shas:
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


def start_backtrace_daemon(directory: Path, *, owner_pid: int, open_browser: bool = True, timeout: float = 90.0) -> int:
    """`agitrack --backtrace` (html): start — or reuse — the background backtrace daemon for
    ``directory``, then return to the shell. The daemon dies when the launching terminal
    closes (owner-pid watchdog) or via `agitrack --backtrace stop`.

    ``timeout`` is the STALL tolerance, not a total deadline: the first build scans and exports
    every local session (OpenCode shells out per session) and can take minutes, so a progress bar
    is shown and the wait continues as long as progress is being made, giving up only if the child
    makes none for ``timeout`` seconds."""
    import subprocess
    import sys
    import os

    from agitrack.proc import detach_kwargs

    running = _running_handshake(directory)
    if running is not None:
        url = str(running.get("url", ""))
        print(f"aGiTrack backtrace already running at {url} (pid {running.get('pid')}).")
        _maybe_open(url, running, open_browser)
        return 0

    cmd = [
        sys.executable,
        "-m",
        "agitrack",
        "--repo",
        str(directory),
        "--backtrace-serve",
        "--dashboard-owner-pid",
        str(owner_pid),
    ]
    # The child must load the INSTALLED aGiTrack, never a stray ``agitrack/`` package in the
    # target directory: the backtraced directory can itself be the aGiTrack source checkout, and
    # ``python -m agitrack`` would otherwise import that (older) copy from cwd. So run the child
    # from a neutral state dir and set PYTHONSAFEPATH to keep cwd off ``sys.path``.
    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONSAFEPATH"] = "1"
    log = _open_log(directory)
    print(f"Scanning local coding-agent transcripts for {_abbreviate_home(str(directory))} … (this can take a moment)")
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=str(state_dir), env=env, **detach_kwargs()
        )
    finally:
        if log is not subprocess.DEVNULL:
            log.close()

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
    url = str(record.get("url", ""))
    print(
        f"aGiTrack backtrace daemon live at {url} (pid {record.get('pid')}).\n"
        + record.get("banner", "")
        + "\nRuns in the background; stops when this terminal closes or `agitrack --backtrace stop`."
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
    directory: Path, *, owner_pid: int | None = None, host: str = "127.0.0.1", port: int = 8765
) -> int:
    """The detached child: build the reconstruction once, then serve it until told to stop
    (SIGTERM/SIGINT) or until the owner pid disappears. Publishes a handshake so the launcher
    can show the URL; if there is nothing to show, records that and exits."""
    import os
    import signal
    import threading
    import time

    from agitrack.metrics.daemon import _watch_owner
    from agitrack.metrics.server import _DashboardServer

    # Report build progress to a file the launching parent polls to draw a progress bar; clear it
    # once the (potentially slow) reconstruction is done.
    view = build_backtrace(
        directory, progress=lambda done, total, phase: _write_progress(directory, done, total, phase)
    )
    _clear_progress(directory)
    if view.is_empty:
        _write_handshake(directory, {"pid": os.getpid(), "empty": True})
        return 0

    handler = _make_handler(view)
    try:
        server = _DashboardServer((host, port), handler)
    except OSError:
        server = _DashboardServer((host, 0), handler)
    bound_port = server.server_address[1]
    url = f"http://{host}:{bound_port}/"
    _write_handshake(
        directory,
        {
            "pid": os.getpid(),
            "host": host,
            "port": bound_port,
            "url": url,
            "banner": view.banner_text(),
            "started": int(time.time()),
        },
    )
    from agitrack import daemons

    daemons.register("backtrace", directory, url=url)

    stop = threading.Event()

    def _request_shutdown(*_: object) -> None:
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)
    if owner_pid:
        threading.Thread(
            target=_watch_owner,
            args=(owner_pid, stop, _request_shutdown),
            daemon=True,
            name="agitrack-backtrace-owner-watch",
        ).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        _clear_handshake(directory)
        from agitrack import daemons

        daemons.deregister()
    return 0


def _open_log(directory: Path):
    import subprocess

    try:
        path = _log_path(directory)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("ab")
    except OSError:
        return subprocess.DEVNULL


def _maybe_open(url: str, record: dict, open_browser: bool) -> None:
    from agitrack.metrics.server import open_dashboard_in_browser, remote_browser_hint

    if not (open_browser and url):
        return
    if not open_dashboard_in_browser(url):
        port = record.get("port", 8765)
        print(remote_browser_hint(url, int(port) if isinstance(port, int) else 8765))


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
