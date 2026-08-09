"""Read the Codex CLI's session transcripts into aGiTrack turns.

Codex stores each conversation TWICE, and this module deliberately treats the two
sources asymmetrically:

* A **rollout JSONL** under ``$CODEX_HOME/sessions/<YYYY>/<MM>/<DD>/rollout-<stamp>-<id>.jsonl``
  is the source of truth. It is one append-only file per conversation — a resume appends to
  the SAME file and keeps the SAME id (verified live against codex-cli 0.147.0), exactly like
  Claude's per-session ``.jsonl``. Everything a commit needs (prompts, replies, tools, token
  counts, file patches) is in there.
* A **SQLite index** (``$CODEX_HOME/state_<n>.sqlite``, a ``threads`` table) maps id → rollout
  path, cwd, model and last-activity. It is only ever used as a fast INDEX; every fact that
  ends up in a commit is re-read from the rollout. The db is opened read-only through an
  ``immutable=1`` URI so aGiTrack can never lock or corrupt the store the user's own Codex
  process is actively writing — and every query falls back to a filesystem scan, so a schema
  bump (the file is already at ``state_5``) degrades to "slower", never to "broken".

The record shapes below were read off real rollouts produced by this module's own live tests,
not guessed from documentation.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

from agitrack.backends.base import TokenUsage
from agitrack.transcripts import capabilities as caps
from agitrack.transcripts.edits import merge_edits_by_path, tracked_edit
from agitrack.transcripts.types import ExportedSession, FileEdit, SessionRef, SessionTurn

# A rollout file name ends with the session uuid, so the id can be recovered from the path
# alone. That is what makes the filesystem fallback possible when the SQLite index is
# missing, locked, or on a schema this version doesn't understand.
_ROLLOUT_RE = re.compile(r"^rollout-.*?-([0-9a-fA-F-]{36})\.jsonl$")

# Codex ships a new numbered state db when it migrates schema (``state_5.sqlite`` today). Glob
# rather than pin: a pinned name silently stops resolving sessions the day Codex bumps it, and
# the symptom (aGiTrack "loses" every session) is far worse than the cost of a directory listing.
_STATE_DB_GLOB = "state_*.sqlite"

# Tool names Codex uses for its own built-in file editing. Recorded so ``_edits_from_turn``
# knows which calls carry file changes; everything else is an ordinary tool call.
_PATCH_TOOL = "apply_patch"

# Codex's sub-agent tool (the ``multi_agent`` feature, stable-on in 0.147.0). A spawned agent
# runs as its own THREAD with its own rollout file, so its tokens are absent from the parent's
# counts — the same shape as OpenCode's `task` tool and Claude's sidechains.
_SUBAGENT_TOOLS = ("spawn_agent", "agent", "run_agent", "task")

# Codex's skill invocation tool. Skills live under ``$CODEX_HOME/skills`` and a plugin-supplied
# skill is namespaced ``<plugin>:<skill>``, which is what ``capabilities.plugins_from_skills``
# lifts the plugin name out of.
_SKILL_TOOLS = ("skill", "use_skill", "run_skill")


# --- locating the store ------------------------------------------------------


def _codex_home() -> Path:
    """Codex's data root. ``CODEX_HOME`` overrides it (Codex's own convention, and what the
    tests set to point at a fixture tree instead of the developer's real sessions)."""
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


def _sessions_root() -> Path:
    return _codex_home() / "sessions"


def _state_dbs() -> list[Path]:
    """Codex state databases, newest schema first — see ``_STATE_DB_GLOB``."""
    try:
        return sorted(_codex_home().glob(_STATE_DB_GLOB), reverse=True)
    except OSError:
        return []


def _query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a read-only query against Codex's state db, returning [] on ANY failure.

    Opened ``immutable=1``: Codex may be writing this very database from another process, and
    aGiTrack must never take a lock that could stall or corrupt the user's live session. An
    immutable open also skips WAL recovery, so a query can't block behind a busy writer — the
    cost is that rows still only in the -wal file are invisible, which is exactly why every
    caller has a filesystem fallback rather than trusting an empty result.
    """
    for db in _state_dbs():
        try:
            connection = sqlite3.connect(f"file:{db}?immutable=1", uri=True, timeout=1.0)
        except sqlite3.Error:
            continue
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(sql, params).fetchall()
            if rows:
                return [dict(row) for row in rows]
        except sqlite3.Error:
            # A schema bump can remove a column this query names. Fall through to the next db
            # (and ultimately to the caller's filesystem scan) rather than raising.
            continue
        finally:
            connection.close()
    return []


def _rollout_files() -> list[Path]:
    """Every rollout file on disk, newest mtime first. The fallback index."""
    root = _sessions_root()
    if not root.is_dir():
        return []
    try:
        files = [path for path in root.rglob("rollout-*.jsonl") if path.is_file()]
    except OSError:
        return []
    return sorted(files, key=lambda path: _mtime(path), reverse=True)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _id_from_path(path: Path) -> str | None:
    match = _ROLLOUT_RE.match(path.name)
    return match.group(1) if match else None


def session_transcript_path(session_id: str) -> Path | None:
    """The session's rollout file. Codex keeps ONE append-only file per conversation, so this
    doubles as the runner's turn-end liveness signal (its mtime advances while the agent works
    even when the TUI prints nothing)."""
    if not session_id:
        return None
    for row in _query("SELECT rollout_path FROM threads WHERE id = ?", (session_id,)):
        candidate = Path(str(row.get("rollout_path") or ""))
        if candidate.name and candidate.is_file():
            return candidate
    # The db had no usable answer (missing, migrated, or the row is still in the WAL): the id is
    # in the file name, so scan for it.
    for path in _rollout_files():
        if _id_from_path(path) == session_id:
            return path
    return None


def session_transcript_size(repo: Path, session_id: str) -> int | None:
    path = session_transcript_path(session_id)
    if path is None:
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None


# --- reading rollout rows ----------------------------------------------------


def _read_rows(path: Path) -> list[dict]:
    """Parse a rollout file, skipping unreadable lines.

    Tolerant on purpose: the file is being APPENDED TO by a live Codex process, so the last
    line is routinely a half-written record. Dropping it and keeping the rest is what lets a
    turn be committed while the session is still running; raising would make every mid-turn
    poll fail.
    """
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _payload(row: dict) -> dict:
    payload = row.get("payload")
    return payload if isinstance(payload, dict) else {}


def _row_kind(row: dict) -> tuple[str, str]:
    """``(record type, payload type)`` — the two-level discriminator every rollout row carries."""
    return str(row.get("type") or ""), str(_payload(row).get("type") or "")


def _row_time(row: dict) -> int | None:
    """Epoch seconds for a rollout row. Codex stamps every record with an ISO-8601 ``timestamp``."""
    stamp = row.get("timestamp")
    if not isinstance(stamp, str) or not stamp:
        return None
    text = stamp.replace("Z", "+00:00")
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(text).timestamp())
    except (ValueError, TypeError):
        return None


def _session_meta(rows: list[dict]) -> dict:
    for row in rows:
        if row.get("type") == "session_meta":
            return _payload(row)
    return {}


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


# --- tokens ------------------------------------------------------------------


def _turn_tokens(usage: dict, *, subagent: bool = False) -> TokenUsage:
    """One Codex ``last_token_usage`` block mapped onto aGiTrack's categories.

    Two Codex-specific conversions, both verified against live counters:

    * ``input_tokens`` INCLUDES ``cached_input_tokens`` (a live turn read 11532 input of which
      8064 were cached, and ``total_tokens`` was 11532+245=11777 — so input is the whole prompt,
      not the uncached remainder). aGiTrack's ``input`` means FRESH input only, with cache reads
      tracked separately and never rolled in, so the cached part is subtracted out here.
      Reporting Codex's raw ``input_tokens`` as ``input`` would double-count the cache on every
      turn — on that same turn it would have claimed 11532 fresh tokens against a true 3468.
    * ``output_tokens`` likewise INCLUDES ``reasoning_output_tokens`` as a subset. aGiTrack
      requires the generated categories NOT to overlap (``output + reasoning`` must be the true
      total), so reasoning is subtracted out of ``output`` and reported in its own bucket — the
      OpenCode shape rather than the Claude one, because Codex does report reasoning separately.
    """
    input_tokens = _int(usage.get("input_tokens"))
    cache_read = _int(usage.get("cached_input_tokens"))
    cache_write = _int(usage.get("cache_write_input_tokens"))
    output_tokens = _int(usage.get("output_tokens"))
    reasoning = _int(usage.get("reasoning_output_tokens"))
    # max(0, ...) guards the invariant rather than trusting it: a provider that ever reports
    # a cached count larger than the input would otherwise produce negative token totals,
    # which propagate into commit metadata and the dashboard as nonsense.
    fresh_input = max(0, input_tokens - cache_read)
    generated = max(0, output_tokens - reasoning)
    if subagent:
        # A spawned agent has its OWN context window, so only its consumption counts — the
        # parent turn's ``context`` gauge must not be moved by it (same rule as Claude
        # sidechains and OpenCode `task` children).
        return TokenUsage(
            total=output_tokens,
            subagent_input=fresh_input,
            subagent_output=generated,
            subagent_reasoning=reasoning,
            subagent_cache_read=cache_read,
            subagent_cache_write=cache_write,
        )
    return TokenUsage(
        # How full the window got: Codex's ``input_tokens`` is already the WHOLE prompt the
        # model read (cache included), so unlike Claude/OpenCode there is nothing to add back.
        context=input_tokens or None,
        total=output_tokens,
        input=fresh_input,
        output=generated,
        reasoning=reasoning,
        cache_read=cache_read,
        cache_write=cache_write,
    )


# --- turn assembly -----------------------------------------------------------


def _agent_message(payload: dict) -> str:
    text = payload.get("message")
    return text.strip() if isinstance(text, str) else ""


def _tool_name(payload: dict) -> str:
    name = payload.get("name")
    return name.strip() if isinstance(name, str) else ""


def _patch_edits(payload: dict, file_state: dict[str, str]) -> list[FileEdit]:
    """File edits from a ``patch_apply_end`` event.

    Codex hands over the applied change directly — ``changes`` maps an absolute path to a
    ``unified_diff`` — which is strictly better evidence than the tool-call ARGUMENTS the other
    backends' parsers have to replay: this is what actually landed on disk, after any rejection.
    The diff is re-derived through ``tracked_edit`` anyway so the patch text, line counts and
    incremental-diff behaviour are byte-identical to the other backends' output.
    """
    if not payload.get("success", True):
        return []  # a failed patch changed nothing; recording it would invent an edit
    edits: list[FileEdit] = []
    for path, change in _as_dict(payload.get("changes")).items():
        change = _as_dict(change)
        kind = str(change.get("type") or "update")
        after = _apply_unified_diff(file_state.get(str(path), ""), str(change.get("unified_diff") or ""))
        if kind == "delete":
            edit = tracked_edit(file_state, str(path), write="")
        else:
            edit = tracked_edit(file_state, str(path), write=after)
        if edit is not None:
            edits.append(edit)
    return edits


def _apply_unified_diff(before: str, diff: str) -> str:
    """``before`` with ``diff``'s hunks applied, best-effort.

    Codex's ``unified_diff`` is a normal ``@@`` patch. Applying it (rather than storing the raw
    diff) keeps ``file_state`` a real file image, which is what makes a SECOND edit to the same
    file in a later turn diff incrementally instead of re-reporting the whole file — the
    accumulating-diff bug ``tracked_edit`` exists to prevent. A hunk that doesn't line up is
    skipped rather than guessed at: a wrong file image is worse than a coarse one.
    """
    if not diff:
        return before
    lines = before.split("\n") if before else []
    out: list[str] = []
    cursor = 0
    for hunk_header, body in _hunks(diff):
        start = hunk_header - 1 if hunk_header > 0 else 0
        if start < cursor or start > len(lines):
            continue  # out of order or past the end — can't be applied safely
        out.extend(lines[cursor:start])
        cursor = start
        for line in body:
            if line.startswith("+"):
                out.append(line[1:])
            elif line.startswith("-"):
                if cursor < len(lines):
                    cursor += 1
            else:
                if cursor < len(lines):
                    out.append(lines[cursor])
                    cursor += 1
                else:
                    out.append(line[1:] if line.startswith(" ") else line)
    out.extend(lines[cursor:])
    text = "\n".join(out)
    if before.endswith("\n") and not text.endswith("\n"):
        text += "\n"
    return text


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _hunks(diff: str):
    """``(old_start, body_lines)`` for each ``@@`` hunk in a unified diff."""
    current: list[str] | None = None
    start = 0
    for line in diff.split("\n"):
        match = _HUNK_RE.match(line)
        if match:
            if current is not None:
                yield start, current
            start = int(match.group(1))
            current = []
            continue
        if current is not None:
            current.append(line)
    if current is not None:
        yield start, current


def _new_turn() -> dict:
    return {
        "turn_id": "",
        "user_prompt": "",
        "agent_messages": [],
        "tool_names": [],
        "skills": [],
        "subagents": [],
        "child_thread_ids": [],
        "tokens": TokenUsage(),
        "model": None,
        "started_at": None,
        "ended_at": None,
        "complete": False,
        "interrupted": False,
        "compaction_count": 0,
        "queued_followups": [],
        "edits": [],
        "last_message_index": 0,
    }


def parse_rows(
    rows: list[dict],
    *,
    collect_edits: bool = False,
    mcp_servers=(),
    subagent_tokens: "dict[str, TokenUsage] | None" = None,
) -> list[SessionTurn]:
    """Codex rollout records → :class:`SessionTurn` objects, one per user prompt.

    Turn boundaries come from Codex's own ``task_started`` / ``task_complete`` events, which
    carry a shared ``turn_id``. That is a real boundary the backend declares, so — unlike the
    heuristics the other two parsers need — a turn is known to be finished rather than inferred
    from idleness. ``complete`` is False for a trailing turn with no ``task_complete``, which is
    exactly the mid-flight state the commit engine must not split a commit across.
    """
    turns: list[SessionTurn] = []
    file_state: dict[str, str] = {}
    current: dict | None = None
    # Ids of message records already counted, so a rollout that repeats a record (Codex writes
    # both an `event_msg` and a mirrored `response_item` for each assistant message) never
    # double-counts the reply text.
    seen_messages: set[str] = set()

    def close(turn: dict, *, complete: bool) -> None:
        turn["complete"] = complete
        turns.append(_finalize(turn, mcp_servers=mcp_servers, subagent_tokens=subagent_tokens or {}))

    for row in rows:
        kind, payload_kind = _row_kind(row)
        payload = _payload(row)
        stamp = _row_time(row)

        if payload_kind == "task_started":
            if current is not None:
                # A new task with the previous one still open means the previous turn never
                # completed — an interrupt (Esc) or a crash. Close it as INCOMPLETE rather than
                # merging the two prompts, so the trace keeps one turn per prompt.
                close(current, complete=False)
            current = _new_turn()
            current["turn_id"] = str(payload.get("turn_id") or "")
            current["started_at"] = stamp
            continue

        if current is None:
            continue

        if kind == "turn_context":
            model = payload.get("model")
            if isinstance(model, str) and model.strip():
                current["model"] = model.strip()
            effort = payload.get("model_reasoning_effort") or payload.get("reasoning_effort")
            if isinstance(effort, str) and effort.strip():
                current["reasoning_effort"] = effort.strip()
            continue

        if payload_kind == "user_message":
            text = _agent_message(payload)
            if not text:
                continue
            if current["user_prompt"]:
                # A second user_message inside one task is a prompt the user QUEUED while the
                # agent was working; it belongs to this turn but is its own message.
                current["queued_followups"].append(text)
            else:
                current["user_prompt"] = text
            continue

        if payload_kind == "agent_message":
            text = _agent_message(payload)
            key = f"{current['turn_id']}:{len(current['agent_messages'])}:{text[:80]}"
            if text and key not in seen_messages:
                seen_messages.add(key)
                current["agent_messages"].append(text)
            continue

        if payload_kind in ("function_call", "custom_tool_call"):
            name = _tool_name(payload)
            if name:
                current["tool_names"].append(name)
                _classify_tool(name, payload, current)
            continue

        if payload_kind == "patch_apply_end":
            if collect_edits:
                current["edits"].extend(_patch_edits(payload, file_state))
            continue

        if payload_kind == "token_count":
            info = _as_dict(payload.get("info"))
            usage = _as_dict(info.get("last_token_usage"))
            if usage:
                current["tokens"].add(_turn_tokens(usage))
            continue

        if payload_kind in ("compacted", "compaction", "auto_compact_begin"):
            # A compaction resets what "context" means for everything after it, so the turn it
            # precedes records that it happened (see SessionTurn.compaction_count).
            current["compaction_count"] += 1
            continue

        if payload_kind in ("turn_aborted", "task_aborted", "turn_interrupted"):
            current["interrupted"] = True
            current["ended_at"] = stamp or current["ended_at"]
            close(current, complete=False)
            current = None
            continue

        if payload_kind == "task_complete":
            last = payload.get("last_agent_message")
            if isinstance(last, str) and last.strip() and last.strip() not in current["agent_messages"]:
                current["agent_messages"].append(last.strip())
            current["ended_at"] = stamp or current["ended_at"]
            close(current, complete=True)
            current = None
            continue

    if current is not None:
        close(current, complete=False)
    return turns


def _classify_tool(name: str, payload: dict, turn: dict) -> None:
    """Record a tool call's capability meaning (skill / sub-agent) alongside its raw name.

    Deliberately conservative: only tools whose names Codex actually uses for these concepts
    are classified, and the invoked skill/agent name is read from the call's arguments. Anything
    unrecognized stays a plain tool name, which at worst means a capability goes UNREPORTED —
    never that a commit claims a capability that was not used.
    """
    lowered = name.lower()
    arguments = payload.get("arguments")
    if not isinstance(arguments, str):
        arguments = payload.get("input") if isinstance(payload.get("input"), str) else ""
    parsed: dict = {}
    try:
        candidate = json.loads(arguments) if arguments else {}
        parsed = candidate if isinstance(candidate, dict) else {}
    except (json.JSONDecodeError, ValueError):
        parsed = {}
    if lowered in _SKILL_TOOLS:
        skill = parsed.get("skill") or parsed.get("name") or parsed.get("skill_name")
        if isinstance(skill, str) and skill.strip():
            turn["skills"].append(skill.strip())
    elif lowered in _SUBAGENT_TOOLS:
        agent = parsed.get("agent") or parsed.get("agent_type") or parsed.get("name") or parsed.get("role")
        turn["subagents"].append(agent.strip() if isinstance(agent, str) and agent.strip() else lowered)
        thread_id = parsed.get("thread_id") or parsed.get("child_thread_id")
        if isinstance(thread_id, str) and thread_id.strip():
            turn["child_thread_ids"].append(thread_id.strip())


def _finalize(turn: dict, *, mcp_servers=(), subagent_tokens: dict[str, TokenUsage]) -> SessionTurn:
    messages = [text for text in turn["agent_messages"] if text]
    tokens = turn["tokens"]
    for child in turn["child_thread_ids"]:
        child_usage = subagent_tokens.get(child)
        if child_usage is not None:
            tokens.add(child_usage)
    capability = caps.collect(
        tool_names=turn["tool_names"],
        skills=turn["skills"],
        subagents=turn["subagents"],
        mcp_servers=mcp_servers,
    )
    # Codex's own turn id is the stable identity for BOTH ends of the turn. The other backends
    # have distinct per-message ids; here the pair is derived from the one id Codex guarantees,
    # with a positional suffix so a turn that is force-committed before the reply arrives (the
    # user-id watermark path in ``turns_after``) still gets a different assistant id afterwards.
    turn_id = turn["turn_id"] or f"turn-{turn['started_at'] or 0}"
    return SessionTurn(
        user_message_id=f"{turn_id}:user",
        assistant_message_id=f"{turn_id}:assistant" if messages else "",
        user_prompt=turn["user_prompt"],
        final_response=messages[-1] if messages else "",
        agent_messages=messages,
        tokens=tokens,
        model=turn["model"],
        complete=turn["complete"],
        interrupted=turn["interrupted"],
        started_at=turn["started_at"],
        ended_at=turn["ended_at"],
        reasoning_effort=turn.get("reasoning_effort"),
        compaction_count=turn["compaction_count"],
        queued_followups=turn["queued_followups"],
        mcp_servers=capability.mcp_servers,
        mcp_tools=capability.mcp_tools,
        skills=capability.skills,
        subagents=capability.subagents,
        plugins=capability.plugins,
        edits=merge_edits_by_path(turn["edits"]),
    )


# --- sub-agent tokens --------------------------------------------------------


def _spawned_thread_ids(session_id: str) -> list[str]:
    """Child thread ids Codex spawned from this conversation.

    Codex's ``multi_agent`` feature runs a sub-agent as its OWN thread with its own rollout
    file, so its tokens are entirely absent from the parent's counters — the same problem
    OpenCode's `task` tool has. The parent→child mapping lives in the state db's
    ``thread_spawn_edges`` table; without it a sub-agent's consumption is silently dropped.
    """
    rows = _query("SELECT child_thread_id FROM thread_spawn_edges WHERE parent_thread_id = ?", (session_id,))
    return [str(row.get("child_thread_id") or "") for row in rows if row.get("child_thread_id")]


def _subagent_tokens(session_id: str, visited: set[str] | None = None) -> dict[str, TokenUsage]:
    """``child thread id -> its total consumption``, recursing through nested spawns.

    ``visited`` breaks cycles: a corrupt or self-referential edge would otherwise recurse until
    the stack blew, taking down the commit that was merely trying to count tokens.
    """
    visited = visited if visited is not None else set()
    totals: dict[str, TokenUsage] = {}
    for child in _spawned_thread_ids(session_id):
        if child in visited:
            continue
        visited.add(child)
        usage = TokenUsage()
        path = session_transcript_path(child)
        if path is not None:
            for row in _read_rows(path):
                _, payload_kind = _row_kind(row)
                if payload_kind != "token_count":
                    continue
                info = _as_dict(_payload(row).get("info"))
                block = _as_dict(info.get("last_token_usage"))
                if block:
                    usage.add(_turn_tokens(block, subagent=True))
        for nested in _subagent_tokens(child, visited).values():
            usage.add(nested)
        totals[child] = usage
    return totals


# --- session discovery -------------------------------------------------------


def _first_cwd(rows: list[dict]) -> str | None:
    cwd = _session_meta(rows).get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def _same_repo(directory: object, repo: Path) -> bool:
    if not isinstance(directory, str) or not directory:
        return False
    try:
        return os.path.realpath(directory) == os.path.realpath(repo)
    except OSError:
        return False


def _label(rows: list[dict]) -> str | None:
    """A short human label for the session picker — Codex's own thread title when it set one,
    otherwise the first user prompt (which is what Codex titles a thread with anyway)."""
    for row in rows:
        _, payload_kind = _row_kind(row)
        if payload_kind == "user_message":
            text = _agent_message(_payload(row))
            if text:
                return text.splitlines()[0][:120]
    return None


def _refs_for(paths: list[Path]) -> list[SessionRef]:
    refs: list[SessionRef] = []
    for path in paths:
        session_id = _id_from_path(path)
        if not session_id:
            continue
        rows = _read_rows(path)
        refs.append(
            SessionRef(
                id=session_id,
                updated=session_last_activity(session_id) or _mtime(path),
                label=_label(rows),
                # Codex records how a thread was created. An ``exec`` thread is a headless
                # `codex exec` run — aGiTrack's OWN summarizer calls look exactly like that —
                # so it is marked programmatic and kept out of the resume/switch lists, the
                # same guard Claude's parser applies to `claude -p` transcripts.
                programmatic=str(_session_meta(rows).get("source") or "") == "exec",
            )
        )
    return refs


def _repo_rollouts(repo: Path) -> list[Path]:
    """Rollout files whose recorded cwd is ``repo``, newest first.

    Asks the SQLite index first (one query beats reading every rollout head), then verifies
    each hit against the file itself — the db can lag behind a retarget, and a session listed
    under the wrong repo is how a switch opens the wrong conversation.
    """
    paths: list[Path] = []
    seen: set[str] = set()
    for row in _query("SELECT id, rollout_path, cwd FROM threads ORDER BY updated_at DESC"):
        if not _same_repo(row.get("cwd"), repo):
            continue
        path = Path(str(row.get("rollout_path") or ""))
        if path.is_file() and str(path) not in seen:
            seen.add(str(path))
            paths.append(path)
    if paths:
        return paths
    for path in _rollout_files():
        if _same_repo(_first_cwd(_read_rows(path)), repo):
            paths.append(path)
    return paths


def list_sessions(repo: Path) -> list[SessionRef]:
    return _refs_for(_repo_rollouts(repo))


def latest_session_id(repo: Path) -> str | None:
    for ref in list_sessions(repo):
        if not ref.programmatic:
            return ref.id
    return None


def sessions_under(directory: Path) -> list[tuple[SessionRef, str]]:
    """Every session recorded anywhere under ``directory``, paired with its rollout path —
    the discovery pass behind ``--backtrace`` and ``--share-sessions``."""
    root = os.path.realpath(directory)
    found: list[tuple[SessionRef, str]] = []
    for path in _rollout_files():
        rows = _read_rows(path)
        cwd = _first_cwd(rows)
        if not cwd:
            continue
        try:
            resolved = os.path.realpath(cwd)
        except OSError:
            continue
        if resolved != root and not resolved.startswith(root + os.sep):
            continue
        session_id = _id_from_path(path)
        if not session_id:
            continue
        found.append(
            (
                SessionRef(
                    id=session_id,
                    updated=session_last_activity(session_id) or _mtime(path),
                    label=_label(rows),
                    programmatic=str(_session_meta(rows).get("source") or "") == "exec",
                ),
                str(path),
            )
        )
    return found


def list_worktree_sessions(worktrees_root: Path) -> list[tuple[str, SessionRef]]:
    """Conversations recorded under an aGiTrack worktree, paired with the worktree's directory
    name (which is the aGiTrack session name). Includes worktrees that have since been removed,
    so a named session stays resumable after its worktree is cleaned up."""
    root = os.path.realpath(worktrees_root)
    out: list[tuple[str, SessionRef]] = []
    for path in _rollout_files():
        rows = _read_rows(path)
        cwd = _first_cwd(rows)
        if not cwd:
            continue
        try:
            resolved = os.path.realpath(cwd)
        except OSError:
            continue
        if not resolved.startswith(root + os.sep):
            continue
        name = resolved[len(root) + 1 :].split(os.sep)[0]
        session_id = _id_from_path(path)
        if not name or not session_id:
            continue
        out.append(
            (
                name,
                SessionRef(
                    id=session_id,
                    updated=session_last_activity(session_id) or _mtime(path),
                    label=_label(rows),
                    programmatic=str(_session_meta(rows).get("source") or "") == "exec",
                ),
            )
        )
    return out


def session_belongs_to_repo(repo: Path, session_id: str) -> bool:
    path = session_transcript_path(session_id)
    if path is None:
        return False
    return _same_repo(_first_cwd(_read_rows(path)), repo)


def session_cwd(session_id: str, *, since: float | None = None) -> str | None:
    """The working directory Codex most recently recorded for this session.

    ``since`` restricts the answer to records at/after the current launch, so a stale cwd from
    before aGiTrack started the session is never mistaken for the agent drifting out of its
    worktree — the check the runner makes on every poll.
    """
    path = session_transcript_path(session_id)
    if path is None:
        return None
    latest: str | None = None
    for row in _read_rows(path):
        kind, _ = _row_kind(row)
        payload = _payload(row)
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            continue
        if kind not in ("session_meta", "turn_context"):
            continue
        if since is not None:
            stamp = _row_time(row)
            if stamp is None or stamp < int(since):
                continue
        latest = cwd
    return latest


def session_last_activity(session_id: str) -> float | None:
    """Epoch seconds of the session's newest MESSAGE.

    Distinct from the file mtime on purpose: aGiTrack's own staging and retargeting rewrite the
    rollout and bump its mtime without adding a message, so ranking conversations by mtime
    would promote a session nobody talked to. Reads the db's ``updated_at`` (cheap) and falls
    back to the last timestamped row.
    """
    if not session_id:
        return None
    for row in _query("SELECT updated_at, updated_at_ms FROM threads WHERE id = ?", (session_id,)):
        millis = row.get("updated_at_ms")
        if isinstance(millis, int) and millis > 0:
            return millis / 1000.0
        seconds = row.get("updated_at")
        if isinstance(seconds, int) and seconds > 0:
            return float(seconds)
    path = session_transcript_path(session_id)
    if path is None:
        return None
    for row in reversed(_read_rows(path)):
        stamp = _row_time(row)
        if stamp is not None:
            return float(stamp)
    return None


def session_activity_mtime(session_id: str) -> float | None:
    """The rollout file's mtime — the runner's turn-end liveness signal.

    MUST stay session-scoped (a shared, always-advancing clock would read as "this turn is
    still running" forever and the session would never commit). Codex gives one append-only
    file per conversation, so the mtime is exactly this session's last write.
    """
    path = session_transcript_path(session_id)
    return _mtime(path) if path is not None else None


def session_model(session_id: str) -> str | None:
    """The model this session last ran under, for a run that didn't pin one explicitly."""
    for row in _query("SELECT model FROM threads WHERE id = ?", (session_id,)):
        model = row.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    path = session_transcript_path(session_id)
    if path is None:
        return None
    for row in reversed(_read_rows(path)):
        if row.get("type") == "turn_context":
            model = _payload(row).get("model")
            if isinstance(model, str) and model.strip():
                return model.strip()
    return None


# --- export ------------------------------------------------------------------


def export_session(repo: Path, session_id: str, *, collect_edits: bool = False) -> ExportedSession | None:
    path = session_transcript_path(session_id)
    if path is None:
        return None
    return export_session_at(path, collect_edits=collect_edits, repo=repo, session_id=session_id)


def export_session_at(
    path: Path,
    *,
    collect_edits: bool = False,
    repo: Path | None = None,
    session_id: str | None = None,
) -> ExportedSession | None:
    rows = _read_rows(path)
    if not rows:
        return None
    resolved_id = session_id or _id_from_path(path) or str(_session_meta(rows).get("session_id") or "")
    turns = parse_rows(
        rows,
        collect_edits=collect_edits,
        mcp_servers=configured_mcp_servers(repo) if repo is not None else (),
        subagent_tokens=_subagent_tokens(resolved_id) if resolved_id else {},
    )
    return ExportedSession(
        session_id=resolved_id,
        model=session_model(resolved_id) if resolved_id else None,
        updated=int(_mtime(path)) or None,
        turns=turns,
    )


def parse_exported_session(data: object, *, collect_edits: bool = False) -> ExportedSession:
    """Parse a SHARED Codex transcript (the rollout's JSONL text, or its already-split rows).

    Accepts text or a list so the same function serves the sharing path (which carries the raw
    file) and tests (which build rows directly). Never raises on damaged input — a shared
    transcript arrives over git from another machine and may be truncated by the size cap, so
    the parser must yield whatever turns are intact instead of failing the whole import.
    """
    rows: list[dict] = []
    if isinstance(data, str):
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(row, dict):
                rows.append(row)
    elif isinstance(data, list):
        rows = [row for row in data if isinstance(row, dict)]
    elif isinstance(data, dict):
        rows = [data]
    meta = _session_meta(rows)
    return ExportedSession(
        session_id=str(meta.get("session_id") or meta.get("id") or ""),
        model=None,
        updated=None,
        turns=parse_rows(rows, collect_edits=collect_edits),
    )


def export_session_raw(repo: Path, session_id: str) -> str | None:
    """The transcript text to share. Codex's rollout is plain JSONL — line-mergeable exactly
    like Claude's, so the session store's line-union merge applies unchanged."""
    path = session_transcript_path(session_id)
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _is_resume_boundary(line: str) -> bool:
    """Whether a rollout line can START a shared transcript that Codex will still resume.

    A capped transcript must begin at a point Codex can rebuild state from. ``session_meta`` is
    the only true header, so it is always kept; a ``task_started`` is the next-best anchor
    because everything a turn needs is re-emitted after it (``turn_context`` carries the model
    and cwd again).
    """
    try:
        row = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(row, dict):
        return False
    kind, payload_kind = _row_kind(row)
    return kind == "session_meta" or payload_kind == "task_started"


def cap_shared_transcript(transcript: str, max_bytes: int) -> str:
    """Bound a shared transcript to ``max_bytes``, keeping the most recent turns.

    Git rejects an oversized blob outright, so a long conversation must be trimmed rather than
    left unshareable. The ``session_meta`` header is ALWAYS preserved and re-attached on top of
    the kept tail: without it Codex cannot rebuild the thread and the shared session imports as
    unresumable, which is the whole point of sharing it.
    """
    encoded = transcript.encode("utf-8")
    if len(encoded) <= max_bytes:
        return transcript
    lines = transcript.splitlines(keepends=True)
    header = ""
    for line in lines:
        if line.strip() and _is_resume_boundary(line):
            try:
                if _row_kind(json.loads(line))[0] == "session_meta":
                    header = line
            except (json.JSONDecodeError, ValueError):
                pass
            break
    budget = max_bytes - len(header.encode("utf-8"))
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        encoded_line = len(line.encode("utf-8"))
        if size + encoded_line > budget:
            break
        kept.append(line)
        size += encoded_line
    kept.reverse()
    # Trim forward to the first line that is a clean turn boundary, so the tail never starts
    # mid-turn (half a turn parses into a prompt with no reply, or a reply with no prompt).
    for index, line in enumerate(kept):
        if _is_resume_boundary(line):
            kept = kept[index:]
            break
    else:
        kept = []
    return header + "".join(kept)


# --- sharing / import --------------------------------------------------------


def _import_path(session_id: str) -> Path:
    """Where an imported transcript is filed. Codex partitions by date, so an import lands
    under today's directory — the id in the file name is what actually resolves it, and the
    date directory is only an index Codex itself also rebuilds by scanning."""
    stamp = time.strftime("%Y/%m/%d")
    directory = _sessions_root() / stamp
    directory.mkdir(parents=True, exist_ok=True)
    name = f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"
    return directory / name


def has_imported_session(repo: Path, session_id: str) -> bool:
    return session_transcript_path(session_id) is not None


def import_shared_session(
    repo: Path, session_id: str, transcript: str, *, overwrite: bool = False, as_id: str | None = None
) -> bool:
    """Install a shared transcript so ``codex resume <id>`` finds it in ``repo``.

    Rewrites the session id and the recorded cwd on the way in: the transcript came from
    another machine, where both the id (when re-importing under a new id for "keep both") and
    the absolute repo path are different. Without the cwd rewrite Codex would resume the
    conversation pointing at a directory that does not exist here.
    """
    target_id = as_id or session_id
    existing = session_transcript_path(target_id)
    if existing is not None and not overwrite and as_id is None:
        return False
    rows: list[dict] = []
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    if not rows:
        return False
    target_cwd = str(Path(repo).resolve())
    for row in rows:
        payload = _payload(row)
        if not payload:
            continue
        for key in ("session_id", "id"):
            if isinstance(payload.get(key), str) and payload.get(key) == session_id:
                payload[key] = target_id
        if isinstance(payload.get("cwd"), str):
            payload["cwd"] = target_cwd
        roots = payload.get("workspace_roots")
        if isinstance(roots, list):
            payload["workspace_roots"] = [target_cwd for _ in roots]
    destination = existing if (existing is not None and overwrite and as_id is None) else _import_path(target_id)
    try:
        destination.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    except OSError:
        return False
    return True


def retarget_session_cwd(repo: Path, session_id: str, cwd: str, *, git_branch: str | None = None) -> bool:
    """Move the session's RECORDED working directory to ``cwd``.

    Codex restores a resumed thread's recorded cwd rather than using the launch directory, so a
    session resumed after its worktree moved (or after a switch to ``--no-worktree``) would open
    in a stale — possibly deleted — directory. Rewriting the rollout's recorded cwd is what
    keeps a resume landing where aGiTrack actually launched it.

    ``git_branch`` is accepted for a uniform signature across backends. Codex stamps its git
    branch only in the SQLite index, which aGiTrack never writes to (see ``_query``: the db is
    opened immutable so the user's live Codex process can't be disturbed), so it is ignored
    here — the recorded cwd alone is what drives the resume.
    """
    path = session_transcript_path(session_id)
    if path is None:
        return False
    rows = _read_rows(path)
    if not rows:
        return False
    target = str(Path(cwd).resolve())
    changed = False
    for row in rows:
        payload = _payload(row)
        if not payload:
            continue
        if isinstance(payload.get("cwd"), str) and payload["cwd"] != target:
            payload["cwd"] = target
            changed = True
        roots = payload.get("workspace_roots")
        if isinstance(roots, list) and roots and any(root != target for root in roots):
            payload["workspace_roots"] = [target for _ in roots]
            changed = True
    if not changed:
        return False
    try:
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    except OSError:
        return False
    return True


def prepare_resume(worktree: Path, session_id: str) -> bool:
    """Whether spawning the resume command in ``worktree`` will find this conversation.

    Codex resolves a session by id from its global store regardless of cwd, so there is nothing
    to stage — unlike Claude, which files transcripts per directory and needs the transcript
    copied into the worktree's project dir first.
    """
    return bool(session_id) and session_transcript_path(session_id) is not None


def forget_session_in(repo: Path, session_id: str) -> bool:
    """Nothing to forget: Codex records ONE cwd per conversation and ``retarget_session_cwd``
    moves that record rather than leaving a copy behind in the directory the session left."""
    return False


def new_import_id() -> str:
    """A fresh id to re-import a shared conversation under ("keep both").

    Codex ids are UUIDs (v7 in practice — time-ordered — but it resolves any UUID by exact
    match from the file name, so a v4 is fine as a re-import id)."""
    return str(uuid.uuid4())


# --- configuration -----------------------------------------------------------

# Codex reads MCP servers from its own config, layered: the user's `$CODEX_HOME/config.toml`
# plus a per-project `.codex/config.toml`. Both are consulted because a repo-local server is
# exactly the kind of provenance a commit should record.
_MCP_CONFIG_PATHS = ((".codex", "config.toml"),)
_MCP_SECTION_RE = re.compile(r"^\s*\[+\s*mcp_servers\s*\.\s*([^\]\.]+?)\s*\]+\s*$")


def configured_mcp_servers(repo: Path) -> frozenset[str]:
    """MCP server names configured for ``repo``.

    Needed because Codex names an MCP tool ``<server>__<tool>`` — which, like OpenCode's
    ``<server>_<tool>``, is indistinguishable BY SHAPE from a built-in such as ``apply_patch``.
    Matching only against servers that are actually configured is what stops a commit inventing
    a server name in its provenance (see ``capabilities.split_mcp_names``).

    Parsed with a regex rather than a TOML library because the only thing needed is the set of
    ``[mcp_servers.<name>]`` table headers, and Codex's config is otherwise full of keys whose
    types this module has no business validating.
    """
    names: set[str] = set()
    candidates = [_codex_home() / "config.toml"]
    for parts in _MCP_CONFIG_PATHS:
        candidates.append(Path(repo).joinpath(*parts))
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            match = _MCP_SECTION_RE.match(line)
            if match:
                names.add(match.group(1).strip().strip('"').strip("'"))
    return frozenset(name for name in names if name)


def looks_like_event_blob(text: str) -> bool:
    """Whether a trace entry is a raw Codex event dump rather than something a human wrote.

    A rollout row that leaks into the trace (a malformed parse, a pasted debug line) is noise
    in a commit message. Recognized by the two-key shape every rollout record has.
    """
    stripped = (text or "").strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        return False
    try:
        row = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(row, dict):
        return False
    return row.get("type") in ("session_meta", "event_msg", "response_item", "turn_context", "world_state")
