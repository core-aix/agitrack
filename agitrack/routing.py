"""Which repository a conversation turn belongs to.

A coding agent is not confined to the folder it was started in. One conversation routinely
edits the repository it runs in, a repository nested inside it (a submodule, a clone in a
subfolder), a sibling checkout, and a scratch file in ``/tmp`` — sometimes all in one turn. A
commit records the conversation that produced its changes, so the conversation has to be routed
to wherever its changes landed, not to wherever the agent happened to be started:

* A tracker records the turns of the conversations started in its own repository (as always),
  **and** the turns of conversations started anywhere else that edited files inside it. A
  repository nested inside another is its own destination: an edit under ``parent/sub/`` belongs
  to ``sub`` when ``sub`` is a git repository, never to ``parent``.
* A turn that edited nothing in its own repository, and edited only repositories that aGiTrack
  tracks (a tracker runs there, or one will start on its next commit or agent session), is left
  to those repositories. Its home keeps everything else: its own edits, a turn that only talked,
  a turn whose edits could not be placed, and a turn whose destination nobody is tracking, so a
  trace is never dropped on the floor just because it was meant for somewhere else.

A turn's edits are what the transcript shows the agent writing (the same recovery
``--backtrace`` uses: editing tools, shell idioms, sub-agents). Repositories are found from the
filesystem alone (a ``.git`` entry on the way up), because this runs on every poll.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agitrack import paths
from agitrack.transcripts.types import ExportedSession, SessionTurn

# How far back a tracker looks for OTHER folders' conversations that may have edited it. A
# conversation idle for longer than this has nothing new to route.
RECENT_SECONDS = 6 * 3600
# Bytes read from the end of a transcript the first time it is checked for a mention of this
# repository's path (later checks read only what was appended since).
_FIRST_MENTION_SCAN = 4 * 1024 * 1024


def _real(path: str | os.PathLike) -> str:
    try:
        return os.path.realpath(os.fspath(path))
    except (OSError, ValueError):
        return os.path.abspath(os.fspath(path))


_ROOT_CACHE: dict[str, str | None] = {}


def repo_root_of(path: str) -> str | None:
    """The git repository a (possibly deleted) file lives in, or None outside any repository.
    The NEAREST one: a file in ``parent/sub/x`` belongs to ``sub`` when ``sub`` is a repository."""
    directory = os.path.dirname(_real(path))
    walked: list[str] = []
    found: str | None = None
    while True:
        if directory in _ROOT_CACHE:
            found = _ROOT_CACHE[directory]
            break
        walked.append(directory)
        if os.path.exists(os.path.join(directory, ".git")):
            found = directory
            break
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    for each in walked:
        _ROOT_CACHE[each] = found
    return found


def _turn_paths(turn: SessionTurn, cwd: str | None) -> list[str]:
    out = []
    for edit in getattr(turn, "edits", None) or []:
        path = edit.path
        if not path:
            continue
        if not paths.is_absolute(path):
            if not cwd:
                continue
            path = os.path.join(cwd, path)
        out.append(_real(path))
    return out


def destinations(turn: SessionTurn, cwd: str | None) -> set[str]:
    """The repositories a turn edited (outside any repository: not a destination)."""
    roots = {repo_root_of(path) for path in _turn_paths(turn, cwd)}
    return {root for root in roots if root}


def is_tracked(root: str) -> bool:
    """Whether something will record this repository's share of a conversation started
    elsewhere: a background tracker running there, an interactive session there that routes
    turns (no worktree), or an auto-start hook that will start a tracker on its next commit.
    A worktree session does not count: its agent is confined to the worktree, and it reads no
    other folder's conversations."""
    from agitrack.proxy import background

    try:
        mode = background.running_mode_for(Path(root))
    except Exception:
        return False
    if mode.get("kind") == "interactive":
        try:
            status = json.loads((Path(root) / ".agitrack" / "session.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return bool(status.get("commit_flush"))  # written only by a no-worktree session
    return bool(mode.get("running") or mode.get("armed"))


def _finished(turn: SessionTurn) -> bool:
    from agitrack.proxy.commit_engine import turn_is_finished

    try:
        return bool(turn_is_finished(turn))
    except Exception:
        return True


@dataclass
class Router:
    """Routing decisions for ONE repository."""

    root: str

    def __post_init__(self) -> None:
        self.root = _real(self.root)

    def home_filter(self, cwd: str | None) -> Callable[[SessionTurn], bool]:
        """Which turns of a conversation started in this repository it records."""
        tracked: dict[str, bool] = {}

        def keep(turn: SessionTurn) -> bool:
            if not _finished(turn):
                return True  # placed once it is finished; its edits so far may not be all
            targets = destinations(turn, cwd or self.root)
            if not targets or self.root in targets:
                return True
            for target in targets:
                if target not in tracked:
                    tracked[target] = is_tracked(target)
            # Left to the repositories it edited only when every one of them will record it.
            return not all(tracked[target] for target in targets)

        return keep

    def foreign_filter(self, cwd: str | None, *, floor: float) -> Callable[[SessionTurn], bool]:
        """Which turns of a conversation started ELSEWHERE this repository records: exactly the
        ones that edited it, prompted after routing began here (``floor``), so history from
        before is never claimed retroactively."""

        def keep(turn: SessionTurn) -> bool:
            started = getattr(turn, "started_at", None)
            if started:
                started = started / 1000 if started > 1e11 else started
                if started < floor:
                    return False
            return self.root in destinations(turn, cwd)

        return keep


# ---------------------------------------------------------------------------
# Finding conversations started elsewhere that may have edited this repository
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    backend: str
    session_id: str
    cwd: str
    path: Path | None  # the transcript file, for backends that keep one
    updated: float = 0.0  # when the conversation last changed


_MENTION_OFFSETS: dict[str, int] = {}
_MENTIONED: set[tuple[str, str]] = set()


def _mentions(transcript: Path, root: str) -> bool:
    """Whether the transcript names this repository's path, reading only what was appended
    since the last check. Sticky: once a conversation has named the repository it stays a
    candidate (later turns may edit it by a relative path)."""
    key = (str(transcript), root)
    if key in _MENTIONED:
        return True
    try:
        size = transcript.stat().st_size
    except OSError:
        return False
    offset = _MENTION_OFFSETS.get(str(transcript))
    if offset is None or offset > size:
        offset = max(0, size - _FIRST_MENTION_SCAN)
    if size == offset:
        return False
    needles = {root.encode("utf-8"), root.replace("\\", "\\\\").encode("utf-8")}
    try:
        with transcript.open("rb") as handle:
            handle.seek(max(0, offset - 512))  # overlap, so a path split across reads is found
            data = handle.read(size - offset + 512)
    except OSError:
        return False
    _MENTION_OFFSETS[str(transcript)] = size
    if any(needle in data for needle in needles):
        _MENTIONED.add(key)
        return True
    return False


def _related(cwd: str, root: str) -> bool:
    """A conversation whose folder is an ANCESTOR of this repository (it can reach in by a
    relative path) or a folder INSIDE it other than the repository itself."""
    real = _real(cwd)
    if real == root:
        return False
    return paths.under(root, real) or paths.under(real, root)


def candidates(root: str, *, since: float, exclude_cwd: str | None = None) -> list[Candidate]:
    """Conversations started outside this repository, active since ``since``, that may have
    edited it. Cheap filters only; whether a turn really did is decided by :class:`Router`."""
    root = _real(root)
    found: list[Candidate] = []
    try:
        from agitrack.transcripts import claude

        for ref, cwd, path in claude.recent_sessions(since):
            if _real(cwd) != root and (_related(cwd, root) or _mentions(path, root)):
                found.append(Candidate("claude", ref.id, cwd, path, ref.updated))
    except Exception:
        pass
    try:
        from agitrack.transcripts import codex

        for ref, cwd, path in codex.recent_sessions(since):
            if _real(cwd) != root and (_related(cwd, root) or _mentions(path, root)):
                found.append(Candidate("codex", ref.id, cwd, path, ref.updated))
    except Exception:
        pass
    try:
        from agitrack.transcripts import opencode

        for ref, directory in opencode.recent_sessions(since):
            # OpenCode keeps no file to scan for a mention, so every recently active
            # conversation elsewhere is a candidate and its edits decide.
            if _real(directory) != root:
                found.append(Candidate("opencode", ref.id, directory, None, ref.updated))
    except Exception:
        pass
    return found


def export_candidate(candidate: Candidate) -> ExportedSession | None:
    """The conversation with each turn's edits recovered."""
    try:
        if candidate.backend == "claude" and candidate.path is not None:
            from agitrack.transcripts import claude

            return claude.export_session_at(candidate.path, collect_edits=True)
        if candidate.backend == "codex":
            from agitrack.transcripts import codex

            return codex.export_session(Path(candidate.cwd), candidate.session_id, collect_edits=True)
        if candidate.backend == "opencode":
            from agitrack.transcripts import opencode

            return opencode.export_session(Path(candidate.cwd), candidate.session_id, collect_edits=True)
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Recording another folder's conversation in this repository
# ---------------------------------------------------------------------------

# The state fields a conversation's parse rewrites as "the conversation being tracked".
# Recording another folder's conversation must not leave THIS repository tracking it.
_TRACKED_CONVERSATION_KEYS = ("backend_session_id", "backend_session_repo", "model", "last_backend_message_id")


def routing_floor(state) -> float:
    """When this repository began taking turns from conversations started elsewhere. Turns
    prompted before it are never claimed: that is history, not something to re-attribute."""
    value = state.data.get("routing_since")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    now = time.time()
    state.data["routing_since"] = now
    state.save()
    return now


def record_elsewhere(
    repo,
    state,
    *,
    commit_fn: Callable,
    debug_fn: Callable[[str], None],
    require_complete: bool = True,
    at_commit: bool = False,
) -> bool:
    """Record, in ``repo``, the turns of conversations started in ANOTHER folder that edited
    it. Each conversation is read with its edits recovered and run through the ordinary commit
    pipeline under its OWN watermark, keeping only the turns that touched this repository.
    Shared by the background tracker and the interactive session, so both route the same way.
    Returns whether anything was recorded."""
    from agitrack.backends.proxy_agents import make_proxy_agent
    from agitrack.proxy.commit_engine import CommitEngine
    from agitrack.proxy.session import Session

    root = str(repo.repo)
    floor = routing_floor(state)
    router = Router(root)
    recorded = False
    for candidate in candidates(root, since=max(floor, time.time() - RECENT_SECONDS)):
        # A conversation unchanged since this repository last went through it has nothing new
        # to offer, and reading it again can cost a CLI call (OpenCode exports by subprocess).
        seen_key = (root, candidate.backend, candidate.session_id)
        if candidate.updated and _PROCESSED.get(seen_key) == candidate.updated:
            continue
        exported = export_candidate(candidate)
        if exported is None or not exported.turns:
            continue
        # Session sets its per-session fields dynamically, so it is used untyped here (as the
        # background runner's _bare_session does).
        session: Any = Session.bare()
        session.repo = repo
        session.state = state
        try:
            session.backend = make_proxy_agent(candidate.backend)
        except Exception:
            continue
        session.worktree = None
        session.name = None
        session.agent_parse_thread = None
        session.agent_parse_result = (
            candidate.session_id,
            exported,
            state.backend_message_id_for(candidate.session_id),
            state,
        )
        saved = {key: state.data.get(key) for key in _TRACKED_CONVERSATION_KEYS}
        try:
            committed, _ = CommitEngine(repo, state, debug_fn=debug_fn).finish_parse_if_ready(
                session=session,
                quiet=True,
                prompt_untracked=False,
                require_complete=require_complete,
                awaited_followups=[],
                agent_is_active_fn=lambda: False,
                debug_fn=debug_fn,
                note_session_change_fn=lambda _sid: None,
                mirror_fn=lambda _sid: None,
                commit_fn=commit_fn,
                at_commit=at_commit,
                turn_filter=router.foreign_filter(candidate.cwd, floor=floor),
            )
        except Exception as error:
            debug_fn(f"recording a conversation from {candidate.cwd} failed: {error!r}")
            committed = False
        finally:
            state.data.update(saved)
            state.save()
        if committed:
            debug_fn(f"recorded turns from a {candidate.backend} conversation started in {candidate.cwd}")
            recorded = True
        if not any(not _finished(turn) for turn in exported.turns):
            # Only once nothing in it is still running: a running turn is placed when it ends,
            # and that end is itself a change, so the conversation is read again then.
            _PROCESSED[seen_key] = candidate.updated
    return recorded


_PROCESSED: dict[tuple[str, str, str], float] = {}
