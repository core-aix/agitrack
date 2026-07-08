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

import getpass
import hashlib
import http.server
import json
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Callable

from agitrack.commits.message import render_interaction_trace
from agitrack.metrics.collect import CommitStat, Dashboard, _abbreviate_home
from agitrack.transcripts import claude, opencode
from agitrack.transcripts.edits import combine_patches, total_lines
from agitrack.transcripts.types import ExportedSession, FileEdit, SessionTurn

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
    diffs: dict[str, str] = field(default_factory=dict)  # virtual sha -> unified patch
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


def build_backtrace(directory: Path, *, max_sessions: int = MAX_SESSIONS) -> BacktraceView:
    """Reconstruct the backtrace dashboard for ``directory`` from local transcripts."""
    directory = directory.resolve()
    sources = _discover(directory)
    dropped = max(0, len(sources) - max_sessions)
    sources = sources[:max_sessions]

    author = _local_author()
    stats: list[CommitStat] = []
    diffs: dict[str, str] = {}
    backends: set[str] = set()
    edited_sessions = 0
    included_sessions = 0

    for source in sources:
        try:
            exported = source.export()
        except Exception:
            exported = None
        if exported is None:
            continue
        bases = _relativize_bases(directory, source.base_dir)
        session_stats, session_diffs, session_edited = _session_to_stats(source, exported, author=author, bases=bases)
        if not session_stats:
            continue
        included_sessions += 1
        backends.add(source.backend)
        if session_edited:
            edited_sessions += 1
        stats.extend(session_stats)
        diffs.update(session_diffs)

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
        diffs=diffs,
        session_count=included_sessions,
        edited_sessions=edited_sessions,
        backends=sorted(backends),
        dropped_sessions=dropped,
    )


def _session_to_stats(
    source: _Source, exported: ExportedSession, *, author: str, bases: list[str]
) -> tuple[list[CommitStat], dict[str, str], bool]:
    """Map one session's turns onto virtual :class:`CommitStat`s (plus their diffs). Returns
    ``(stats, diffs, session_changed_files)``."""
    stats: list[CommitStat] = []
    diffs: dict[str, str] = {}
    session_changed = False
    for index, turn in enumerate(exported.turns):
        edits = [_relativize(edit, bases) for edit in turn.edits]
        has_content = bool(turn.user_prompt.strip() or turn.final_response.strip() or turn.agent_messages or edits)
        if not has_content:
            continue
        sha = _virtual_sha(source.backend, exported.session_id, index, turn.assistant_message_id)
        insertions, deletions = total_lines(edits)
        if edits:
            session_changed = True
            patch = combine_patches(edits)
            if len(patch) > _MAX_PATCH_CHARS:
                patch = patch[:_MAX_PATCH_CHARS] + "\n… (diff truncated)\n"
            diffs[sha] = patch
        timestamp = turn.ended_at or turn.started_at or int(exported.updated or 0)
        prompts = [p for p in (turn.user_prompt, *turn.queued_followups) if p.strip()]
        stats.append(
            CommitStat(
                sha=sha,
                author=author,
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
                message=_message(turn),
            )
        )
    return stats, diffs, session_changed


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


def _message(turn: SessionTurn) -> str:
    """The turn's detail-view text: the subject followed by a ``# Interaction Trace`` of the
    user↔agent conversation, rendered exactly as an aGiTrack commit renders its trace (secret
    masking, heading nesting, ``## User`` / ``## Agent`` roles) so the dashboard's log section
    shows the full conversation behind the change."""
    trace: list[dict] = []
    if turn.user_prompt.strip():
        trace.append({"role": "user", "content": turn.user_prompt})
    for followup in turn.queued_followups:
        if followup.strip():
            trace.append({"role": "user", "content": followup})
    agent_texts = turn.agent_messages or ([turn.final_response] if turn.final_response.strip() else [])
    for text in agent_texts:
        if text.strip():
            trace.append({"role": "agent", "content": text})
    body = render_interaction_trace(trace, trace_turn_limit=len(trace) + 1)
    header = _subject(turn)
    if not body:
        return header
    return f"{header}\n\n# Interaction Trace\n\n{body}\n"


def _iso(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_author() -> str:
    try:
        return getpass.getuser() or "you"
    except Exception:
        return "you"


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
    from agitrack.metrics.web import aggregates_payload, format_html, log_page

    banner = _banner_html(view)
    page = format_html(view.dashboard, banner_html=banner, backtrace=True).encode("utf-8")

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

    return f'<div class="updatebanner">⏪ {_escape(view.banner_text())}</div>'


def serve_backtrace(
    directory: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    """Build the backtrace for ``directory`` and serve it on localhost until Ctrl-C."""
    from agitrack.metrics.server import (
        _DashboardServer,
        open_dashboard_in_browser,
        remote_browser_hint,
    )

    print(f"Scanning local coding-agent transcripts for {_abbreviate_home(str(directory))} …")
    view = build_backtrace(directory)
    if view.is_empty:
        print(_empty_message(directory))
        return 0

    handler = _make_handler(view)
    try:
        server = _DashboardServer((host, port), handler)
    except OSError:
        server = _DashboardServer((host, 0), handler)
    bound_port = server.server_address[1]
    url = f"http://{host}:{bound_port}/"
    print(view.banner_text())
    print(
        f"\naGiTrack backtrace live at {url}\nHistorical reconstruction from local transcripts. Press Ctrl-C to stop."
    )
    if open_browser and not open_dashboard_in_browser(url):
        print(remote_browser_hint(url, bound_port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping the backtrace dashboard.")
    finally:
        server.server_close()
    return 0


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
