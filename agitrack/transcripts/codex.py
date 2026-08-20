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
from agitrack.fileio import safe_is_dir
from agitrack.transcripts import capabilities as caps
from agitrack.transcripts.edits import merge_edits_by_path, tracked_edit
from agitrack.transcripts.shell_edits import edits_from_shell
from agitrack.transcripts.types import (
    SUBAGENT_LIVE_HORIZON_SECONDS,
    ExportedSession,
    FileEdit,
    SessionRef,
    SessionTurn,
)

# A rollout file name ends with the session uuid, so the id can be recovered from the path
# alone. That is what makes the filesystem fallback possible when the SQLite index is
# missing, locked, or on a schema this version doesn't understand.
_ROLLOUT_RE = re.compile(r"^rollout-.*?-([0-9a-fA-F-]{36})\.jsonl$")

# Codex ships a new numbered state db when it migrates schema (``state_5.sqlite`` today). Glob
# rather than pin: a pinned name silently stops resolving sessions the day Codex bumps it, and
# the symptom (aGiTrack "loses" every session) is far worse than the cost of a directory listing.
_STATE_DB_GLOB = "state_*.sqlite"

# Codex's sub-agent tools (the ``multi_agent`` feature, stable-on in 0.147.0). A spawned agent
# runs as its own THREAD with its own rollout file, so its tokens are absent from the parent's
# counts — the same shape as OpenCode's `task` tool and Claude's sidechains.
#
# The two names do different jobs and only one of them means "a sub-agent ran": ``spawn_agent``
# starts one, ``wait_agent`` merely blocks on ids already spawned. Counting ``wait_agent`` as a
# spawn would report two sub-agents for one, so only ``spawn_agent`` marks usage — but
# ``wait_agent``'s ``targets`` argument is the ONLY place the child's thread id appears in the
# parent's rollout (``spawn_agent``'s arguments carry just the message), so it is still read for
# ids. Verified against a live run: spawn_agent{"message":…,"fork_context":true} then
# wait_agent{"targets":["019fe8e3-7a9d-…"]}.
_SUBAGENT_SPAWN_TOOL = "spawn_agent"
_SUBAGENT_WAIT_TOOL = "wait_agent"

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


def watch_roots() -> list[Path]:
    """Directories whose mtime changes when a NEW Codex conversation appears.

    The backtrace daemon decides a fresh discovery pass is owed by stat-ing directories
    (``metrics.backtrace._watch_signature``). Watching only the sessions root is not enough
    here, because Codex files rollouts under ``sessions/YYYY/MM/DD/`` — so the root's own mtime
    never changes for a new conversation, and the daemon never noticed one until something else
    forced a rediscovery.

    Four cases have to be covered, and the chain from the root down to the NEWEST existing day
    directory covers all of them: a new rollout today changes the day dir; the first rollout of
    a new day changes the month dir; a new month changes the year dir; a new year changes the
    root. That is a handful of stats per poll, rather than the ``rglob`` over every rollout ever
    written that a "newest file" signature would need.

    Names are zero-padded numerals, so lexicographic order IS chronological.
    """
    root = _sessions_root()
    roots = [root]
    current = root
    for _ in range(3):  # YYYY / MM / DD
        try:
            children = sorted(child for child in current.iterdir() if safe_is_dir(child))
        except OSError:
            break
        if not children:
            break
        current = children[-1]
        roots.append(current)
    return roots


def _schema_version(path: Path) -> int:
    """The migration number in ``state_<n>.sqlite``; -1 when it has none."""
    digits = path.stem.rpartition("_")[2]
    return int(digits) if digits.isdigit() else -1


def _state_dbs() -> list[Path]:
    """Codex state databases, newest schema first — see ``_STATE_DB_GLOB``.

    Sorted NUMERICALLY, not lexicographically: the day Codex ships ``state_10.sqlite``, a string
    sort puts ``state_5`` first and every id → rollout_path / model / nickname lookup would
    silently answer from the stale pre-migration database.
    """
    try:
        return sorted(_codex_home().glob(_STATE_DB_GLOB), key=_schema_version, reverse=True)
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
    if not safe_is_dir(root):
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


def _content_text(payload: dict) -> str:
    """The text of a ``response_item`` message, whose ``content`` is a list of typed parts.

    This is the ONLY place the interactive TUI records the conversation. ``codex exec`` writes
    both ``event_msg`` (``user_message`` / ``agent_message``) *and* a mirrored
    ``response_item``; the TUI writes only the ``response_item``. Parsing just the events —
    which is what the exec-derived rollouts taught — produced turns with an EMPTY user prompt
    for every interactive session, i.e. every real aGiTrack run.
    """
    content = payload.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "".join(parts).strip()


# Codex injects harness context as ordinary ``role: "user"`` messages, each one a single XML-ish
# element (``<environment_context>…</environment_context>``, ``<skills_instructions>…``). They are
# not what the human typed, and committing one would put a multi-kilobyte context block in the
# trace as the user's prompt. Matched structurally — a message that is ENTIRELY one wrapping tag —
# rather than by a fixed tag list, so a new injection Codex adds later is excluded too.
_WRAPPED_CONTEXT_RE = re.compile(r"^<([A-Za-z_][\w-]*)>[\s\S]*?</\1>\s*")


def _is_harness_injection(text: str) -> bool:
    """Whether a ``role: "user"`` message is Codex scaffolding rather than something a human typed.

    Two conditions, both required.

    1. The message is ENTIRELY complete ``<tag>…</tag>`` blocks. Several are concatenated into one
       message in practice — a live turn carried
       ``<recommended_plugins>…</recommended_plugins><environment_context>…</environment_context>``
       as a single 3,230-character "prompt" — so blocks are peeled one at a time and it only
       counts if nothing is left over. Requiring a SINGLE wrapping tag missed exactly that case
       and put the whole context dump into the commit as the user's prompt.
    2. At least one tag name is ``snake_case``. Every injection Codex emits is
       (``environment_context``, ``recommended_plugins``, ``skills_instructions``, …), while
       markup a human might paste as their whole prompt is not (``<div>…</div>``,
       ``<Component>…</Component>``, ``<html>…</html>``). Without this a pure-markup prompt was
       classified as scaffolding and the turn committed with an EMPTY user prompt — dropping a
       real prompt, which is the worse of the two failures. Keyed on the naming convention
       rather than a fixed tag list so an injection Codex adds later is still caught.
    """
    remaining = text.strip()
    if not remaining.startswith("<"):
        return False
    saw_snake_case_tag = False
    while remaining:
        match = _WRAPPED_CONTEXT_RE.match(remaining)
        if not match:
            return False  # real prose left over: a human wrote this
        saw_snake_case_tag = saw_snake_case_tag or "_" in match.group(1)
        remaining = remaining[match.end() :]
    return saw_snake_case_tag


def _tool_name(payload: dict) -> str:
    name = payload.get("name")
    return name.strip() if isinstance(name, str) else ""


# Codex spells "run a shell command" three ways, and only the first is a plain tool call:
# `exec_command` (a function_call whose JSON arguments carry `cmd`/`workdir`), the older
# `shell`/`local_shell` (whose `command` is an argv LIST, e.g. ["bash","-lc","sed -i ..."]),
# and `exec` — a custom_tool_call whose input is JAVASCRIPT that calls
# `tools.exec_command({cmd:"...", workdir:"..."})`. The last is the shape the current sandbox
# uses, so reading only the first two recovered nothing from a modern rollout.
_JS_EXEC_CALL = re.compile(r"tools\.exec_command\s*\(\s*\{(?P<body>.*?)\}\s*\)", re.S)
_JS_FIELD = re.compile(
    r"""["']?(?P<key>cmd|command|workdir)["']?\s*:\s*(?P<value>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')"""
)
_SHELL_TOOLS = frozenset({"exec_command", "shell", "local_shell", "container.exec"})


def _shell_commands(name: str, payload: dict) -> list[tuple[str, str]]:
    """Every ``(command, workdir)`` a tool call would run in a shell.

    A list rather than one pair because a single ``exec`` script may drive several commands.
    Empty for any tool that is not a shell, and for a call whose arguments cannot be read —
    an unreadable command is recovered as nothing, never guessed at.
    """
    if name in _SHELL_TOOLS:
        arguments = payload.get("arguments")
        if not isinstance(arguments, str):
            return []
        try:
            parsed = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(parsed, dict):
            return []
        command = parsed.get("cmd") or parsed.get("command") or ""
        if isinstance(command, list):
            # An argv list: the script is the last word of a `bash -lc <script>` style call,
            # and a bare argv (["ls","-la"]) rejoins to the same text a shell would have run.
            command = (
                str(command[-1])
                if len(command) > 2 and command[0] in ("bash", "sh", "zsh")
                else " ".join(str(word) for word in command)
            )
        workdir = parsed.get("workdir") or parsed.get("cwd") or ""
        return [(str(command), str(workdir) if isinstance(workdir, str) else "")]
    if name != "exec":
        return []
    source = payload.get("input")
    if not isinstance(source, str):
        return []
    out: list[tuple[str, str]] = []
    for call in _JS_EXEC_CALL.finditer(source):
        fields = {match.group("key"): match.group("value") for match in _JS_FIELD.finditer(call.group("body"))}
        raw = fields.get("cmd") or fields.get("command")
        if not raw:
            continue
        command = _js_string(raw)
        if command is None:
            continue
        workdir = _js_string(fields.get("workdir") or '""') or ""
        out.append((command, workdir))
    return out


def _js_string(literal: str) -> str | None:
    """A JavaScript string literal as its value. JSON decodes double-quoted ones exactly; a
    single-quoted one is re-quoted first, since JS allows what JSON does not."""
    try:
        if literal.startswith("'"):
            literal = '"' + literal[1:-1].replace('"', '\\"') + '"'
        value = json.loads(literal)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, str) else None


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
        # Codex describes a change one of two ways, and which one depends on the kind: an ADD
        # carries the whole new file in ``content``, while an UPDATE carries only a
        # ``unified_diff``. Reading only the diff (the shape an update taught us to expect)
        # silently produced zero edits for every newly-created file — verified against a live
        # sub-agent run that created haiku.txt with ``{"type": "add", "content": ...}``.
        if kind == "delete":
            edit = tracked_edit(file_state, str(path), write="")
        elif isinstance(change.get("content"), str):
            edit = tracked_edit(file_state, str(path), write=change["content"])
        else:
            # An UPDATE names no whole-file content, and the file predates the session, so there
            # is no baseline to apply the patch to. Hand the hunks over as (old, new) snippet
            # pairs instead — the same shape Claude's/OpenCode's Edit tool produces — which
            # ``tracked_edit`` replays against tracked content when it has it and diffs directly
            # when it doesn't. An earlier version tried to APPLY the diff to an empty baseline;
            # every hunk landed past the end of a zero-line file and was skipped, so a real
            # `subtract()` edit was recorded as no change at all.
            edit = tracked_edit(file_state, str(path), subedits=_hunk_subedits(str(change.get("unified_diff") or "")))
        if edit is not None:
            edits.append(edit)
    return edits


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


def _hunk_subedits(diff: str) -> list[tuple[str, str]]:
    """A unified diff as ``(old_text, new_text)`` replacement pairs, one per ``@@`` hunk.

    Each hunk's context lines are kept in BOTH sides so the pair is a real, anchored
    replacement rather than a bag of changed lines — that anchoring is what lets
    ``tracked_edit`` apply it to content it is already tracking (so a second edit to the same
    file diffs incrementally) instead of only ever diffing the snippets against each other.
    """
    pairs: list[tuple[str, str]] = []
    old: list[str] = []
    new: list[str] = []

    def flush() -> None:
        if old or new:
            pairs.append(("\n".join(old), "\n".join(new)))

    started = False
    for line in diff.split("\n"):
        if _HUNK_RE.match(line):
            if started:
                flush()
            old, new = [], []
            started = True
            continue
        if not started:
            continue
        if line.startswith("+"):
            new.append(line[1:])
        elif line.startswith("-"):
            old.append(line[1:])
        elif line.startswith("\\"):
            continue  # "\ No newline at end of file" is a marker, not content
        else:
            text = line[1:] if line.startswith(" ") else line
            old.append(text)
            new.append(text)
    if started:
        flush()
    return pairs


def _new_turn() -> dict:
    return {
        "turn_id": "",
        "user_prompt": "",
        "agent_messages": [],
        "tool_names": [],
        "skills": [],
        "child_thread_ids": [],
        "spawn_count": 0,
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
    subagent_work: "dict[str, tuple[TokenUsage, list[FileEdit]]] | None" = None,
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
    # The directory the rollout ran in, from its `session_meta` header. Codex's `apply_patch`
    # names absolute paths, but a shell command's are relative to this, and the two must land on
    # the SAME key in `file_state` or one file is tracked (and counted) twice.
    rollout_cwd = str(_session_meta(rows).get("cwd") or "")
    current: dict | None = None
    # Ids of message records already counted, so a rollout that repeats a record (Codex writes
    # both an `event_msg` and a mirrored `response_item` for each assistant message) never
    # double-counts the reply text.
    seen_messages: set[str] = set()

    def close(turn: dict, *, complete: bool) -> None:
        turn["complete"] = complete
        turns.append(_finalize(turn, mcp_servers=mcp_servers, subagent_work=subagent_work or {}))

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
            _add_user_message(current, _agent_message(payload), seen_messages)
            continue

        if payload_kind == "agent_message":
            _add_agent_message(current, _agent_message(payload), seen_messages)
            continue

        if kind == "response_item" and payload_kind == "message":
            # The TUI's only record of the conversation (see _content_text). In an `exec`
            # rollout this DUPLICATES the event_msg above, so both paths dedupe on the text.
            role = str(payload.get("role") or "")
            if role == "user":
                _add_user_message(current, _content_text(payload), seen_messages)
            elif role == "assistant":
                _add_agent_message(current, _content_text(payload), seen_messages)
            # `developer` / `system` roles are harness scaffolding, never conversation.
            continue

        if payload_kind in ("function_call", "custom_tool_call"):
            name = _tool_name(payload)
            if name:
                current["tool_names"].append(name)
                _classify_tool(name, payload, current)
            if collect_edits:
                # Codex edits through the shell as readily as through `apply_patch`, and a
                # `sed -i` or `cat` heredoc leaves no FileChange record — so without this the
                # backtrace showed only the patches (see transcripts.shell_edits).
                for command, workdir in _shell_commands(name, payload):
                    current["edits"].extend(edits_from_shell(file_state, command, cwd=workdir or rollout_cwd))
            continue

        if payload_kind == "patch_apply_end":
            if collect_edits:
                current["edits"].extend(_patch_edits(payload, file_state))
            continue

        if payload_kind == "item_completed":
            # The interactive TUI reports an applied patch as an item_completed/FileChange
            # instead of the patch_apply_end the exec path emits — same ``changes`` map. Without
            # this branch --backtrace reconstructed ZERO file edits for every TUI session, which
            # is every real aGiTrack run. Only FileChange is read here: the message and command
            # items duplicate records already handled above.
            item = _as_dict(payload.get("item"))
            if collect_edits and str(item.get("type") or "") == "FileChange":
                current["edits"].extend(_patch_edits(item, file_state))
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


def _add_user_message(turn: dict, text: str, seen: set[str]) -> None:
    """Record a human prompt on the turn, ignoring harness injections and duplicates."""
    if not text or _is_harness_injection(text):
        return
    key = f"{turn['turn_id']}:u:{text}"
    if key in seen:
        return  # the same message arriving as both an event_msg and a response_item
    seen.add(key)
    if turn["user_prompt"]:
        # A second user message inside one task is a prompt the user QUEUED while the agent was
        # already working; it belongs to this turn but is its own message.
        turn["queued_followups"].append(text)
    else:
        turn["user_prompt"] = text


def _add_agent_message(turn: dict, text: str, seen: set[str]) -> None:
    if not text:
        return
    key = f"{turn['turn_id']}:a:{text}"
    if key in seen:
        return
    seen.add(key)
    turn["agent_messages"].append(text)


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
    elif lowered == _SUBAGENT_SPAWN_TOOL:
        # A spawn call names no agent — Codex assigns the child a nickname ("Ohm") that lives
        # only in its thread row — so the id is resolved first and the name looked up in
        # ``_finalize``. The tool name is the fallback so a spawn is never reported as nothing.
        turn["spawn_count"] += 1
    elif lowered == _SUBAGENT_WAIT_TOOL:
        for target in parsed.get("targets") or []:
            if isinstance(target, str) and target.strip():
                turn["child_thread_ids"].append(target.strip())


def _finalize(
    turn: dict, *, mcp_servers=(), subagent_work: dict[str, tuple[TokenUsage, list[FileEdit]]]
) -> SessionTurn:
    messages = [text for text in turn["agent_messages"] if text]
    tokens = turn["tokens"]
    # Sub-agents are named by resolving the child THREAD ids, not collected during the scan:
    # a spawn call carries no agent name, only the child thread does (see _agent_nickname).
    subagents: list[str] = []
    # De-duplicated: the model can call `wait_agent` more than once on the same child (re-waiting
    # after a partial return), and each repeat would otherwise add that sub-agent's whole
    # TokenUsage again and list it twice in the turn's provenance.
    for child in dict.fromkeys(turn["child_thread_ids"]):
        work = subagent_work.get(child)
        if work is not None:
            tokens.add(work[0])
            turn["edits"].extend(work[1])  # the delegated file changes belong to this turn
        subagents.append(_agent_nickname(child) or child)
    if turn["spawn_count"] and not turn["child_thread_ids"]:
        # The turn demonstrably spawned an agent but never waited on it, so no child id is
        # recoverable from this rollout. Record the capability under the tool's own name rather
        # than dropping it: "a sub-agent ran" is true and provable, only its identity is not.
        subagents.append(_SUBAGENT_SPAWN_TOOL)
    capability = caps.collect(
        tool_names=turn["tool_names"],
        skills=turn["skills"],
        subagents=subagents,
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


def _agent_nickname(thread_id: str) -> str | None:
    """The name Codex gave a spawned sub-agent ("Ohm").

    Codex names an agent in its THREAD ROW, never in the parent's rollout, so this is the only
    way a commit can say which sub-agent ran rather than printing a bare uuid.
    """
    for row in _query("SELECT agent_nickname FROM threads WHERE id = ?", (thread_id,)):
        nickname = row.get("agent_nickname")
        if isinstance(nickname, str) and nickname.strip():
            return nickname.strip()
    # The child's own rollout header repeats it under source.subagent.thread_spawn, so the name
    # survives a missing/migrated state db exactly like every other fact this module reads.
    path = session_transcript_path(thread_id)
    if path is None:
        return None
    spawn = _as_dict(
        _as_dict(_as_dict(_session_meta(_read_rows(path)).get("source")).get("subagent")).get("thread_spawn")
    )
    nickname = spawn.get("agent_nickname")
    return nickname.strip() if isinstance(nickname, str) and nickname.strip() else None


def _child_ids_in_rows(rows: list[dict]) -> list[str]:
    """Child thread ids named by the conversation's own ``wait_agent`` calls.

    The rollout is the authoritative source here, and it must be read as well as the state db:
    the db's ``thread_spawn_edges`` is the only place a spawn that was never waited on is
    recorded, but the db is also the part of Codex's store aGiTrack cannot see into when it is
    missing, mid-migration, or holding the rows in an un-checkpointed WAL. Taking the union
    means a sub-agent's tokens survive either source being unavailable.
    """
    found: list[str] = []
    for row in rows:
        _, payload_kind = _row_kind(row)
        if payload_kind != "function_call":
            continue
        payload = _payload(row)
        if _tool_name(payload).lower() != _SUBAGENT_WAIT_TOOL:
            continue
        try:
            parsed = json.loads(payload.get("arguments") or "{}")
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        for target in parsed.get("targets") or []:
            if isinstance(target, str) and target.strip():
                found.append(target.strip())
    return found


# A child thread's rollout brackets every turn it runs: ``task_started`` opens one and exactly
# one of these closes it. A rollout whose last such event is a ``task_started`` is a sub-agent
# that is still working right now. Read off real rollouts from this module's live runs — a
# completed sub-agent ends on ``task_complete``, one killed with its parent on ``turn_aborted``.
_TURN_TERMINAL_EVENTS = frozenset({"task_complete", "turn_aborted"})

# ``spawn_agent`` returns the id of the thread it started, and the harness later feeds the parent
# a ``<subagent_notification>`` naming that same id when the sub-agent reports back. Both land in
# the PARENT'S OWN ROLLOUT, which is what makes async sub-agents trackable at all: the state db's
# ``thread_spawn_edges`` is invisible mid-session (aGiTrack opens it ``immutable=1``, so rows
# still in the un-checkpointed WAL are simply not there — verified live: a running sub-agent's
# edge read back as no rows at all), and mid-session is the only time the answer matters.
#
# The spawn result reaches the rollout in two shapes depending on how the model called the tool —
# a plain ``function_call_output`` for a direct ``spawn_agent`` call, or a
# ``custom_tool_call_output`` when the model drives the tool from the ``exec`` JS sandbox — but
# the payload it prints is the tool's own return value either way, so one pattern reads both.
# Both shapes were captured from live runs of this module's own tests.
_SPAWNED_AGENT_ID_RE = re.compile(r'"agent_id"\s*:\s*"([^"]+)"')
_SUBAGENT_NOTIFICATION_TAG = "<subagent_notification>"
_NOTIFIED_AGENT_ID_RE = re.compile(r'"agent_path"\s*:\s*"([^"]+)"')


def _thread_is_running(path: Path) -> bool:
    """Whether the thread recorded at *path* has a turn open right now."""
    running = False
    for row in _read_rows(path):
        record_kind, payload_kind = _row_kind(row)
        if record_kind != "event_msg":
            continue
        if payload_kind == "task_started":
            running = True
        elif payload_kind in _TURN_TERMINAL_EVENTS:
            running = False
    return running


def _flat_text(value: object) -> str:
    """A rollout field's text, whether Codex stored it as a bare string or as the
    ``[{"type": "input_text", "text": …}, …]`` block list it uses elsewhere. Concatenated
    rather than JSON-dumped: a dump escapes the inner quotes of an embedded JSON payload, so a
    pattern written against what the tool actually printed would never match."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(part.get("text", "") for part in value if isinstance(part, dict))
    return ""


def _spawned_agent_ids_in_rows(rows: list[dict]) -> list[str]:
    """Child thread ids this conversation's ``spawn_agent`` calls RETURNED (see the regex note)."""
    found: list[str] = []
    for row in rows:
        _, payload_kind = _row_kind(row)
        if payload_kind not in ("function_call_output", "custom_tool_call_output"):
            continue
        for match in _SPAWNED_AGENT_ID_RE.finditer(_flat_text(_payload(row).get("output"))):
            found.append(match.group(1))
    return found


def _notified_agent_ids_in_rows(rows: list[dict]) -> set[str]:
    """Child thread ids that have REPORTED BACK — the harness injects a
    ``<subagent_notification>`` user message naming the sub-agent whose work is over."""
    found: set[str] = set()
    for row in rows:
        _, payload_kind = _row_kind(row)
        if payload_kind != "message":
            continue
        text = _flat_text(_payload(row).get("content"))
        if _SUBAGENT_NOTIFICATION_TAG not in text:
            continue
        found.update(match.group(1) for match in _NOTIFIED_AGENT_ID_RE.finditer(text))
    return found


def live_subagent_ids(session_id: str, rows: list[dict], *, extra_children: list[str] | None = None) -> list[str]:
    """Ids of this conversation's sub-agent threads that are STILL WORKING.

    Codex's ``spawn_agent`` is asynchronous and ``wait_agent`` is optional, so the main agent can
    spawn a sub-agent, post what reads as a final answer, and leave the sub-agent editing the tree
    for minutes afterwards. Committing there records a finished-sounding trace over half-done
    work, which is what ``ExportedSession.live_subagent_ids`` exists to prevent.

    A candidate is dropped as soon as its ``<subagent_notification>`` arrives; the ones left are
    confirmed against the CHILD'S OWN rollout (an unclosed ``task_started``) rather than the state
    db's ``thread_spawn_edges.status``, which read ``open`` both for a sub-agent that had already
    finished and for one that had been aborted — it records only that the spawn happened. The
    mtime horizon is the safety valve for a sub-agent killed so hard it never recorded its own end
    (see SUBAGENT_LIVE_HORIZON_SECONDS).

    Nested spawns count too — a grandchild still writing is still writing — and ``visited`` keeps
    a corrupt or self-referential edge from recursing forever, exactly as ``_subagent_work`` does.
    """
    reported = _notified_agent_ids_in_rows(rows)
    queue = list(
        dict.fromkeys(
            [
                *_spawned_agent_ids_in_rows(rows),
                *(extra_children or []),
                *_spawned_thread_ids(session_id),
            ]
        )
    )
    live: list[str] = []
    visited: set[str] = set()
    horizon = time.time() - SUBAGENT_LIVE_HORIZON_SECONDS
    while queue:
        child = queue.pop(0)
        if child in visited:
            continue
        visited.add(child)
        queue.extend(_spawned_thread_ids(child))
        if child in reported:
            continue  # already reported back: its work is in the conversation, not still coming
        path = session_transcript_path(child)
        if path is None:
            continue  # no rollout to judge by: claiming it is running would defer on nothing
        if _mtime(path) >= horizon and _thread_is_running(path):
            live.append(child)
    return sorted(live)


def _spawned_thread_ids(session_id: str) -> list[str]:
    """Child thread ids Codex spawned from this conversation.

    Codex's ``multi_agent`` feature runs a sub-agent as its OWN thread with its own rollout
    file, so its tokens are entirely absent from the parent's counters — the same problem
    OpenCode's `task` tool has. The parent→child mapping lives in the state db's
    ``thread_spawn_edges`` table; without it a sub-agent's consumption is silently dropped.
    """
    rows = _query("SELECT child_thread_id FROM thread_spawn_edges WHERE parent_thread_id = ?", (session_id,))
    return [str(row.get("child_thread_id") or "") for row in rows if row.get("child_thread_id")]


def _subagent_work(
    session_id: str,
    visited: set[str] | None = None,
    *,
    extra_children: list[str] | None = None,
    collect_edits: bool = False,
) -> dict[str, tuple[TokenUsage, list[FileEdit]]]:
    """``child thread id -> (its total consumption, the files it changed)``, recursing through
    nested spawns.

    Edits are collected as well as tokens because a sub-agent's thread is excluded from the
    session listings (it is not a conversation the user can resume), so nothing else would ever
    read it — and the files it wrote would vanish from ``--backtrace`` entirely. Verified on a
    live run: the parent turn recorded no edits at all while its sub-agent's own rollout carried
    ``haiku.txt (+3)``. The work belongs to the parent turn that delegated it.

    ``visited`` breaks cycles: a corrupt or self-referential edge would otherwise recurse until
    the stack blew, taking down the commit that was merely trying to count tokens.
    """
    visited = visited if visited is not None else set()
    totals: dict[str, tuple[TokenUsage, list[FileEdit]]] = {}
    children = list(dict.fromkeys([*(extra_children or []), *_spawned_thread_ids(session_id)]))
    for child in children:
        if child in visited:
            continue
        visited.add(child)
        usage = TokenUsage()
        edits: list[FileEdit] = []
        file_state: dict[str, str] = {}
        path = session_transcript_path(child)
        if path is not None:
            for row in _read_rows(path):
                _, payload_kind = _row_kind(row)
                payload = _payload(row)
                if payload_kind == "token_count":
                    block = _as_dict(_as_dict(payload.get("info")).get("last_token_usage"))
                    if block:
                        usage.add(_turn_tokens(block, subagent=True))
                elif collect_edits and payload_kind == "patch_apply_end":
                    edits.extend(_patch_edits(payload, file_state))
                elif collect_edits and payload_kind == "item_completed":
                    item = _as_dict(payload.get("item"))
                    if str(item.get("type") or "") == "FileChange":
                        edits.extend(_patch_edits(item, file_state))
        for nested_usage, nested_edits in _subagent_work(child, visited, collect_edits=collect_edits).values():
            usage.add(nested_usage)
            edits.extend(nested_edits)
        totals[child] = (usage, edits)
    return totals


# --- session discovery -------------------------------------------------------


def _read_header(path: Path) -> dict:
    """The rollout's ``session_meta`` payload, read WITHOUT parsing the whole file.

    ``session_meta`` is always the first record, and the discovery passes only need three fields
    from it (cwd, source, thread_source). Fully parsing every rollout in ``$CODEX_HOME`` made each
    ``--backtrace`` / daemon discovery O(the user's entire Codex history) in both I/O and CPU —
    Claude's equivalent reads only a bounded head for exactly this reason. A few lines are scanned
    rather than one in case a future version emits a banner before the header.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(5):
                line = handle.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(row, dict) and row.get("type") == "session_meta":
                    return _payload(row)
    except OSError:
        return {}
    return {}


def recorded_cwd(path: Path) -> str | None:
    """The working directory a rollout recorded, read from its header alone.

    Public because ``--backtrace`` needs it to relativize a session's (absolute) edit paths:
    the rollout file's own location under ``~/.codex/sessions/`` relativizes nothing, so using
    it dropped every reconstructed edit.
    """
    cwd = _read_header(path).get("cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def _same_repo(directory: object, repo: Path) -> bool:
    if not isinstance(directory, str) or not directory:
        return False
    try:
        return os.path.realpath(directory) == os.path.realpath(repo)
    except OSError:
        return False


def _is_agent_thread(header: dict) -> bool:
    """Whether a rollout header describes a SUB-AGENT's thread rather than a user conversation.

    A spawned agent gets its own rollout file in the same directory, recorded against the same
    cwd as its parent. Without this filter every sub-agent would appear in the session picker as
    a resumable conversation and — being the newest file — could be adopted as "the session" on
    restart, silently switching the user onto a sub-agent's thread. Its tokens are still counted:
    its tokens AND its file edits are folded into the PARENT turn that spawned it (see
    ``_subagent_work``).
    """
    return str(header.get("thread_source") or "") == "subagent"


def _label(rows: list[dict]) -> str | None:
    """A short human label for the session picker: the session's first real user prompt.

    Must read BOTH record forms for the same reason ``parse_rows`` does — the interactive TUI
    writes only ``response_item`` messages, so reading just ``user_message`` events left every
    TUI session (i.e. every real aGiTrack session) unlabeled in the picker. Harness injections
    are skipped or the label would be a slab of ``<environment_context>``.
    """
    for row in rows:
        kind, payload_kind = _row_kind(row)
        payload = _payload(row)
        if payload_kind == "user_message":
            text = _agent_message(payload)
        elif kind == "response_item" and payload_kind == "message" and payload.get("role") == "user":
            text = _content_text(payload)
        else:
            continue
        if text and not _is_harness_injection(text):
            return text.splitlines()[0][:120]
    return None


def _refs_for(paths: list[Path]) -> list[SessionRef]:
    refs: list[SessionRef] = []
    for path in paths:
        session_id = _id_from_path(path)
        if not session_id:
            continue
        header = _read_header(path)
        if _is_agent_thread(header):
            continue
        refs.append(
            SessionRef(
                id=session_id,
                updated=session_last_activity(session_id) or _mtime(path),
                label=_label(_read_rows(path)),
                # Codex records how a thread was created. An ``exec`` thread is a headless
                # `codex exec` run — aGiTrack's OWN summarizer calls look exactly like that —
                # so it is marked programmatic and kept out of the resume/switch lists, the
                # same guard Claude's parser applies to `claude -p` transcripts.
                programmatic=str(header.get("source") or "") == "exec",
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
        if _same_repo(recorded_cwd(path), repo):
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
        # Cheap header check first: most rollouts belong to other directories, and parsing them
        # in full to find that out is what made this pass scale with the whole Codex history.
        header = _read_header(path)
        cwd = header.get("cwd")
        if not isinstance(cwd, str) or not cwd or _is_agent_thread(header):
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
                    label=_label(_read_rows(path)),
                    programmatic=str(header.get("source") or "") == "exec",
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
        header = _read_header(path)  # header-only filter, as in sessions_under
        cwd = header.get("cwd")
        if not isinstance(cwd, str) or not cwd or _is_agent_thread(header):
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
                    label=_label(_read_rows(path)),
                    programmatic=str(header.get("source") or "") == "exec",
                ),
            )
        )
    return out


def session_belongs_to_repo(repo: Path, session_id: str) -> bool:
    path = session_transcript_path(session_id)
    if path is None:
        return False
    return _same_repo(recorded_cwd(path), repo)


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
        subagent_work=(
            _subagent_work(resolved_id, extra_children=_child_ids_in_rows(rows), collect_edits=collect_edits)
            if resolved_id
            else {}
        ),
    )
    return ExportedSession(
        session_id=resolved_id,
        model=session_model(resolved_id) if resolved_id else None,
        updated=int(_mtime(path)) or None,
        turns=turns,
        live_subagent_ids=live_subagent_ids(resolved_id, rows, extra_children=_child_ids_in_rows(rows)),
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


_TRUST_SECTION_RE = re.compile(r'^\s*\[\s*projects\s*\.\s*"(.+?)"\s*\]\s*$')
_TRUST_LEVEL_RE = re.compile(r'^\s*trust_level\s*=\s*"(.+?)"\s*$')


def trusted_projects() -> set[str]:
    """Directories the user has told Codex to trust, from ``$CODEX_HOME/config.toml``.

    Codex asks "Do you trust the contents of this directory?" the first time it runs anywhere
    new, and records the answer as ``[projects."<path>"] trust_level = "trusted"``. aGiTrack
    runs each session in a FRESH worktree, which is a new directory every time — so without
    this the user would face that prompt on every single session, in a directory they never
    chose. Read (never written): granting trust stays the user's decision, made once for the
    repository, and ``codex_trust_args`` only ever propagates a grant that already exists.
    """
    trusted: set[str] = set()
    try:
        text = (_codex_home() / "config.toml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return trusted
    current: str | None = None
    for line in text.splitlines():
        section = _TRUST_SECTION_RE.match(line)
        if section:
            current = section.group(1)
            continue
        if line.lstrip().startswith("["):
            current = None  # any other table ends the projects.<path> block
            continue
        level = _TRUST_LEVEL_RE.match(line)
        if level and current and level.group(1).strip() == "trusted":
            trusted.add(current)
    return trusted


def trust_args(repo: Path, base_repo: Path | None) -> list[str]:
    """``-c projects."<repo>".trust_level="trusted"`` when — and only when — the user has
    already trusted the base repository this worktree belongs to.

    Returns [] when the base repo is untrusted (or unknown), so Codex still asks, in full, for
    a repository the user has never approved. The propagation is narrow on purpose: the
    worktree is aGiTrack's own checkout of the very repository the user already trusted, and
    its contents are that repository's contents.
    """
    if base_repo is None:
        return []
    try:
        target = os.path.realpath(repo)
        base = os.path.realpath(base_repo)
    except OSError:
        return []
    if target == base:
        return []  # not a worktree; Codex's own recorded trust for this path already applies
    trusted = {os.path.realpath(path) for path in trusted_projects()}
    if base not in trusted:
        return []
    return ["-c", f'projects."{target}".trust_level="trusted"']


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
