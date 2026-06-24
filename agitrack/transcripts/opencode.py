from __future__ import annotations

import json
import os
from agitrack.env import getenv_compat
import pty
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from agitrack.backends.base import TokenUsage
from agitrack.transcripts.types import ExportedSession, SessionRef, SessionTurn, turns_after

# Every `opencode` subprocess aGiTrack runs synchronously (often on the main reactor/menu
# thread — session list/export/import) is capped: the CLI talks to a TTY and can hang
# (interactive fallback, a server-side stall), and an unbounded wait would FREEZE the whole
# TUI. On timeout the call returns empty/non-zero, which every caller already treats as "no
# data", rather than deadlocking. Generous enough for a real call, short enough to recover.
_OPENCODE_CALL_TIMEOUT = 30.0

__all__ = [
    "ExportedSession",
    "SessionRef",
    "SessionTurn",
    "turns_after",
    "latest_session_id",
    "list_sessions",
    "list_worktree_sessions",
    "session_belongs_to_repo",
    "export_session",
    "export_session_raw",
    "session_transcript_size",
    "has_imported_session",
    "import_shared_session",
    "retarget_session_dir",
    "parse_exported_session",
    "looks_like_event_blob",
]

# Keys an assistant message's `info` may carry the reasoning effort / model
# variant under across OpenCode versions. Best-effort — searched recursively and
# only used when present; absence falls back to the reasoning-token signal.
_REASONING_EFFORT_KEYS = {"reasoningEffort", "reasoning_effort", "effort", "variant"}


def _opencode_session_list(cwd: Path, max_count: int) -> list[dict]:
    """`opencode session list --format json`, BOUNDED by a timeout. The CLI talks to a TTY and
    can hang; without the cap this (run on the menu/main thread) freezes the whole TUI. Returns
    the parsed session dicts, or [] on timeout/spawn-failure/non-zero/bad-JSON — every caller
    already treats an empty list as 'no sessions'."""
    _debug(cwd, f"opencode session list starting (max_count={max_count})")
    try:
        process = subprocess.run(
            ["opencode", "session", "list", "--format", "json", "--max-count", str(max_count)],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_OPENCODE_CALL_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        _debug(cwd, f"opencode session list timed out/failed: {error!r}")
        return []
    _debug(cwd, f"opencode session list finished returncode={process.returncode} stdout_bytes={len(process.stdout)}")
    if process.returncode != 0:
        return []
    try:
        sessions = json.loads(process.stdout)
    except json.JSONDecodeError:
        return []
    return sessions if isinstance(sessions, list) else []


def _fetch_sessions(repo: Path, max_count: int) -> list[dict]:
    sessions = _opencode_session_list(repo, max_count)
    if not sessions:
        return []
    resolved = repo.resolve()
    matching = [session for session in sessions if _same_repo(session.get("directory"), resolved) and session.get("id")]
    if matching:
        return matching
    # No session recorded for this directory. Fall back to the unfiltered list
    # ONLY when the output carries no `directory` fields at all (an OpenCode
    # version that doesn't report it) — otherwise an empty result here would
    # adopt and resume the globally newest session from an unrelated project.
    if any("directory" in session for session in sessions):
        return []
    return [session for session in sessions if session.get("id")]


def _to_seconds(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    # OpenCode reports millisecond timestamps; normalise to seconds.
    return number / 1000.0 if number > 1e12 else number


def list_sessions(repo: Path) -> list[SessionRef]:
    refs = []
    for session in _fetch_sessions(repo, 50):
        updated = session.get("updated") or session.get("created") or 0
        title = session.get("title")
        refs.append(
            SessionRef(
                id=str(session["id"]), updated=_to_seconds(updated), label=title if isinstance(title, str) else None
            )
        )
    return refs


def latest_session_id(repo: Path) -> str | None:
    refs = list_sessions(repo)
    if not refs:
        return None
    return max(refs, key=lambda ref: ref.updated).id


def list_worktree_sessions(worktrees_root: Path) -> list[tuple[str, SessionRef]]:
    """Every OpenCode conversation recorded under any aGiTrack worktree of this repo,
    newest first, paired with the worktree key needed to recreate it. OpenCode
    records each session's ``directory``, so conversations whose worktree has
    since been removed are still listed (and stay resumable)."""
    root = worktrees_root.resolve()
    cwd = next((p for p in [root, *root.parents] if p.is_dir()), Path.home())
    sessions = _opencode_session_list(cwd, 200)
    out: list[tuple[str, SessionRef]] = []
    for session in sessions:
        sid = session.get("id")
        directory = session.get("directory")
        if not sid or not isinstance(directory, str):
            continue
        try:
            dpath = Path(directory).resolve()
        except OSError:
            continue
        if dpath.parent != root:  # only sessions that ran in a worktree of this repo
            continue
        updated = session.get("updated") or session.get("created") or 0
        title = session.get("title")
        ref = SessionRef(id=str(sid), updated=_to_seconds(updated), label=title if isinstance(title, str) else None)
        out.append((dpath.name, ref))
    out.sort(key=lambda item: item[1].updated, reverse=True)
    return out


def session_belongs_to_repo(repo: Path, session_id: str) -> bool:
    sessions = _opencode_session_list(repo, 50)
    resolved = repo.resolve()
    return any(
        session.get("id") == session_id and _same_repo(session.get("directory"), resolved) for session in sessions
    )


def _same_repo(directory: object, repo: Path) -> bool:
    if not isinstance(directory, str) or not directory:
        return False
    try:
        return Path(directory).resolve() == repo
    except OSError:
        return directory == str(repo)


def export_session(repo: Path, session_id: str) -> ExportedSession | None:
    data = _export_data(repo, session_id)
    if data is None:
        return None
    # Sub-agents OpenCode spawns via the `task` tool run in their OWN child sessions,
    # which are absent from this export and hidden from `session list`. Export each (by
    # the child id the parent's task part records) and fold its tokens into the turn that
    # launched it, so sub-agent consumption is fully accounted (issue: subagent tokens).
    subagent_tokens = _collect_subagent_tokens(repo, session_id, data)
    return parse_exported_session(data, subagent_tokens=subagent_tokens)


def _export_data(repo: Path, session_id: str) -> dict | None:
    """Run ``opencode export <session_id>`` and return the parsed JSON object (the
    ``{info, messages}`` structure), or None on any failure."""
    _debug(repo, f"opencode export starting session_id={session_id}")
    output, returncode = _run_export_pty(repo, session_id)
    _debug(
        repo,
        f"opencode export finished session_id={session_id} returncode={returncode} output_bytes={len(output.encode(errors='replace'))}",
    )
    if returncode != 0:
        return None
    json_text = _extract_json_object(output)
    if not json_text:
        return None
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _collect_subagent_tokens(repo: Path, session_id: str, data: dict) -> dict[str | None, TokenUsage]:
    """Token usage of every sub-agent the session spawned, keyed by the DIRECT child
    session id the parent's ``task`` part references, so `parse_exported_session` can
    attribute each to the turn that launched it. A child's total rolls up its own
    consumption plus any nested sub-agents'. Empty (and no extra exports) when the
    session used no sub-agents — the common case has zero overhead."""
    out: dict[str | None, TokenUsage] = {}
    visited: set[str] = {session_id}
    for message in _as_list(data.get("messages")):
        for child_id in _task_child_session_ids(message.get("parts")):
            out.setdefault(child_id, TokenUsage()).add(_subagent_tokens_for_session(repo, child_id, visited))
    return out


def _subagent_tokens_for_session(repo: Path, child_id: str, visited: set[str]) -> TokenUsage:
    # Sum a sub-agent child session's own assistant token usage PLUS all of its nested
    # sub-agents', as sub-agent buckets. `visited` guards against cycles / re-exporting.
    usage = TokenUsage()
    if not child_id or child_id in visited:
        return usage
    visited.add(child_id)
    data = _export_data(repo, child_id)
    if data is None:
        return usage
    for message in _as_list(data.get("messages")):
        info = _as_dict(message.get("info"))
        if info.get("role") == "assistant":
            usage.add(_subagent_message_tokens(info, message.get("parts")))
        for grand_id in _task_child_session_ids(message.get("parts")):
            usage.add(_subagent_tokens_for_session(repo, grand_id, visited))
    return usage


def _task_child_session_ids(parts: object) -> set[str]:
    # Child session ids referenced by `task` sub-agent tool parts. A sub-agent part
    # records both the child `sessionId` and its `parentSessionId` in `state.metadata`;
    # require both so an ordinary tool that merely carries a sessionId is ignored.
    out: set[str] = set()
    if not isinstance(parts, list):
        return out
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "tool":
            continue
        meta = _as_dict(_as_dict(part.get("state")).get("metadata"))
        child = meta.get("sessionId")
        if isinstance(child, str) and child and meta.get("parentSessionId"):
            out.add(child)
    return out


def _subagent_message_tokens(info: dict, parts: object) -> TokenUsage:
    # A sub-agent assistant message's tokens, remapped into the sub-agent buckets. Its
    # context size isn't the main turn's, so `context` is left untouched (None).
    main = _tokens(info, parts)
    return TokenUsage(
        total=main.total,
        subagent_input=main.input,
        subagent_output=main.output,
        subagent_reasoning=main.reasoning,
        subagent_cache_read=main.cache_read,
        subagent_cache_write=main.cache_write,
    )


def export_session_raw(repo: Path, session_id: str) -> str | None:
    """The full session serialised as JSON text — the portable artifact shared
    with collaborators (issue #55). Produced by ``opencode export --sanitize``
    (OpenCode redacts transcript/file data at the source; aGiTrack masks secrets and
    home paths on top). The id is preserved, so a later ``opencode import`` of
    this text round-trips to the same session. None when it can't be exported."""
    if not session_id:
        return None
    _debug(repo, f"opencode export(raw) starting session_id={session_id}")
    output, returncode = _run_export_pty(repo, session_id, sanitize=True)
    _debug(repo, f"opencode export(raw) finished session_id={session_id} returncode={returncode}")
    if returncode != 0:
        return None
    json_text = _extract_json_object(output)
    if not json_text:
        return None
    try:
        json.loads(json_text)  # only share text OpenCode can import back
    except json.JSONDecodeError:
        return None
    return json_text


def retarget_session_dir(repo: Path, session_id: str, cwd: str) -> bool:
    """Point an OpenCode session's recorded ``directory`` at ``cwd`` so resuming it opens in
    THIS worktree, not the (possibly stale or deleted) directory it last ran in.

    OpenCode resumes by id from its global store and restores that recorded directory,
    ignoring the launch path — so without this a resumed session keeps the wrong working
    directory. A no-op when the session already belongs to ``cwd``. Otherwise re-exports the
    session (UNSANITIZED — this is a local move, not a share, so content must be preserved)
    and re-imports it with ``cwd`` as the import cwd, which is how OpenCode retargets a
    session's directory. Best-effort: returns True only when it was actually moved."""
    if not session_id:
        return False
    cwd_path = Path(cwd)
    if session_belongs_to_repo(cwd_path, session_id):
        return False  # already recorded against this directory — nothing to do
    output, returncode = _run_export_pty(repo, session_id, sanitize=False)
    if returncode != 0:
        return False
    transcript = _extract_json_object(output)
    if not transcript:
        return False
    return import_shared_session(cwd_path, session_id, transcript, overwrite=True)


def session_transcript_size(repo: Path, session_id: str) -> int | None:
    # OpenCode keeps sessions in a SQLite store with no per-session file to stat,
    # and exporting purely to measure size would make the manage-shared menu slow
    # (one spawn per entry). Skip the cheap "is the shared copy current?" hint for
    # OpenCode — auto-update is content-hash gated and a manual update always pushes.
    return None


def has_imported_session(repo: Path, session_id: str) -> bool:
    """Whether ``repo`` already holds this session locally. OpenCode preserves the
    id across import and retargets the session's directory to the import cwd, so a
    session recorded against this repo means resuming would keep the local copy
    unless explicitly overwritten."""
    return bool(session_id) and session_belongs_to_repo(repo, session_id)


def import_shared_session(
    repo: Path, session_id: str, transcript: str, *, overwrite: bool = False, as_id: str | None = None
) -> bool:
    """Install a shared session via ``opencode import`` so it can be resumed in
    ``repo`` (issue #55). OpenCode preserves the session id and retargets the
    session's directory to the import cwd, so running this with ``repo`` as cwd
    makes the session belong to — and resume in — this repo.

    By default an existing local copy is kept (no clobber). With ``overwrite`` —
    the "pull the latest shared version" path for syncing your own session between
    machines — the import re-runs and replaces the local copy. With ``as_id`` the
    conversation is re-id'd before import (every occurrence of its id token swapped
    for the new one), so it imports as a SEPARATE local session alongside an
    existing copy ("keep both"). Returns True when the session is in place."""
    if not session_id or not transcript:
        return False
    repo = Path(repo)
    if as_id:
        # The session id is a unique "ses_"-prefixed token; swapping every
        # occurrence re-ids the whole session (info.id and message refs) so
        # OpenCode imports it as a brand-new session.
        transcript = transcript.replace(session_id, as_id)
        session_id = as_id
    elif not overwrite and has_imported_session(repo, session_id):
        return True  # already have this conversation locally — don't clobber it
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            handle.write(transcript)
            tmp_path = handle.name
    except OSError:
        return False
    try:
        _debug(repo, f"opencode import starting session_id={session_id}")
        output, returncode = _run_opencode_pty(repo, ["opencode", "import", tmp_path])
        _debug(repo, f"opencode import finished session_id={session_id} returncode={returncode}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    # `opencode import` of a *missing* file exits 0 but prints "File not found",
    # so a clean exit alone isn't proof; require the success line too.
    return returncode == 0 and "Imported session" in output


def _debug(repo: Path, message: str) -> None:
    if (getenv_compat("DEBUG_PROXY") or "").strip().lower() not in {"1", "true", "yes"}:
        return
    try:
        path = repo / ".agitrack" / "proxy-debug.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def _run_export_pty(repo: Path, session_id: str, *, sanitize: bool = False) -> tuple[str, int]:
    args = ["opencode", "export", session_id]
    if sanitize:
        # OpenCode's own redaction of transcript/file data at the source. aGiTrack
        # masks secrets/home paths on top before sharing (issue #55).
        args.append("--sanitize")
    return _run_opencode_pty(repo, args)


def _run_opencode_pty(repo: Path, args: list[str]) -> tuple[str, int]:
    """Run ``opencode`` under a pty in ``repo`` (it talks to a TTY) and return
    its combined output and exit code. A pty is needed because the CLI writes
    framed/colour output to a terminal, not a plain pipe."""
    pid, fd = pty.fork()
    if pid == 0:
        # Never let the child survive a failed exec — it would keep running
        # aGiTrack's own Python code from the fork point as a duplicate process.
        try:
            os.chdir(repo)
            os.execvp(args[0], args)
        except BaseException:
            os._exit(127)

    # Watchdog: a hung `opencode` (interactive TTY fallback, server stall) would otherwise
    # block the os.read loop forever and FREEZE aGiTrack. SIGKILL it on timeout so the read
    # hits EOF and we return what we have with a non-zero exit (callers treat that as no data).
    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    watchdog = threading.Timer(_OPENCODE_CALL_TIMEOUT, _kill_on_timeout)
    watchdog.daemon = True
    watchdog.start()
    chunks: list[bytes] = []
    try:
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        watchdog.cancel()
    os.close(fd)
    _done, status = os.waitpid(pid, 0)
    exit_code = os.waitstatus_to_exitcode(status)
    if timed_out.is_set():
        exit_code = exit_code or 124  # killed → ensure a non-zero "unusable" code
    return b"".join(chunks).decode(errors="replace"), exit_code


def parse_exported_session(
    data: dict, *, subagent_tokens: "dict[str | None, TokenUsage] | None" = None
) -> ExportedSession:
    # `subagent_tokens` maps a sub-agent child session id -> its token usage (in the
    # sub-agent buckets); each is added to the turn whose `task` part launched that child
    # (see `_collect_subagent_tokens`). The None key, and any child unmatched to a turn,
    # is attributed to the latest turn so sub-agent tokens are never dropped.
    info = _as_dict(data.get("info"))
    session_id = str(info.get("id") or "")
    updated = (info.get("time") or {}).get("updated") if isinstance(info.get("time"), dict) else None
    model = _model_name(info)
    messages = _as_list(data.get("messages"))
    current_user: dict | None = None
    assistant_group: list[dict] = []
    turns: list[SessionTurn] = []
    child_ids_per_turn: list[set[str]] = []
    # Context compactions OpenCode performed within the current turn (see below).
    compactions = 0

    def flush() -> None:
        nonlocal compactions
        if current_user is None or not assistant_group:
            return
        turn = _build_turn(current_user, assistant_group, model)
        if turn:
            turn.compaction_count = compactions
            turns.append(turn)
            child_ids: set[str] = set()
            for assistant in assistant_group:
                child_ids |= _task_child_session_ids(assistant.get("parts"))
            child_ids_per_turn.append(child_ids)

    for message in messages:
        msg_info = _as_dict(message.get("info"))
        role = msg_info.get("role")
        if role == "user":
            flush()
            current_user = message
            assistant_group = []
            compactions = 0
        elif role == "assistant" and current_user is not None:
            # OpenCode injects its conversation summary as an assistant message
            # marked `summary: true` (mode/agent "compaction"). It is bookkeeping,
            # not a real response, so keep it out of the turn's final response and
            # the interaction trace — but tally it, since a compaction resets the
            # turn's context and so bears on its token counts. (User messages carry
            # an unrelated `summary` struct of file diffs, which is why this guard
            # is assistant-only.)
            if msg_info.get("summary") is True or msg_info.get("mode") == "compaction":
                compactions += 1
                continue
            assistant_group.append(message)
    flush()
    _attribute_subagent_tokens(turns, child_ids_per_turn, subagent_tokens)
    return ExportedSession(session_id=session_id, model=model, updated=updated, turns=turns)


def _attribute_subagent_tokens(
    turns: list[SessionTurn],
    child_ids_per_turn: list[set[str]],
    subagent_tokens: "dict[str | None, TokenUsage] | None",
) -> None:
    # Add each sub-agent's token usage to the turn that launched it (matched by child
    # session id). A child matching no turn — or the None key — is attributed to the
    # latest turn, so its tokens are never dropped.
    if not subagent_tokens or not turns:
        return
    for child_id, usage in subagent_tokens.items():
        index: int | None = None
        if child_id is not None:
            index = next((i for i, ids in enumerate(child_ids_per_turn) if child_id in ids), None)
        if index is None:
            index = len(turns) - 1
        turns[index].tokens.add(usage)


def _build_turn(user_message: dict, assistants: list[dict], session_model: str | None) -> SessionTurn | None:
    user_info = _as_dict(user_message.get("info"))
    user_id = str(user_info.get("id") or "")
    if not user_id:
        return None

    final_response = ""
    final_assistant: dict | None = None
    agent_messages: list[str] = []
    tokens = TokenUsage()
    model = session_model
    effort: str | None = None
    last_assistant = assistants[-1] if assistants else None
    for assistant in assistants:
        assistant_info = _as_dict(assistant.get("info"))
        tokens.add(_tokens(assistant_info, assistant.get("parts")))
        model = _model_name(assistant_info) or model
        effort = effort or _find_value(assistant_info, _REASONING_EFFORT_KEYS)
        response = _final_response(assistant.get("parts"), finish=assistant_info.get("finish"))
        if response:
            final_response = response
            final_assistant = assistant
            # Each assistant message's user-facing reply, in order, so the opt-in
            # full trace can include every message rather than only the last.
            agent_messages.append(response)

    final_info = (final_assistant or last_assistant or {}).get("info", {})
    assistant_id = str(final_info.get("id") or "")
    return SessionTurn(
        user_message_id=user_id,
        assistant_message_id=assistant_id,
        user_prompt=_parts_text(user_message.get("parts")),
        final_response=final_response,
        tokens=tokens,
        model=model,
        # A named effort/variant when the export records one, otherwise "on" when
        # the turn spent reasoning tokens — the only reasoning signal OpenCode
        # reliably exposes (the configured level is not in the export).
        reasoning_effort=effort or ("on" if tokens.reasoning > 0 else None),
        started_at=_message_time(user_info),
        ended_at=_message_time(_as_dict(final_info)) or _message_time(user_info),
        agent_messages=agent_messages,
    )


def _message_time(info: dict) -> int | None:
    """Epoch seconds a message was created, from OpenCode's `time` block."""
    time_block = info.get("time")
    if not isinstance(time_block, dict):
        return None
    stamp = time_block.get("created") or time_block.get("updated")
    seconds = _to_seconds(stamp) if stamp is not None else 0.0
    return int(seconds) or None


def _extract_json_object(output: str) -> str | None:
    start = output.find("{")
    end = output.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    return output[start : end + 1]


def _parts_text(parts: object) -> str:
    if not isinstance(parts, list):
        return ""
    texts = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            text = part["text"]
            if _looks_like_event_blob(text):
                continue
            texts.append(text)
    return "".join(texts).strip()


def _final_response(parts: object, *, finish: object = None) -> str:
    if not isinstance(parts, list):
        return ""
    texts = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
            continue
        metadata = part.get("metadata")
        phase = _find_value(metadata, {"phase"}) if isinstance(metadata, dict) else None
        if phase == "final_answer" or (finish == "stop" and part.get("type") == "text"):
            text = part["text"]
            texts.append(_final_text_from_event_blob(text) if _looks_like_event_blob(text) else text)
    return "".join(texts).strip()


def looks_like_event_blob(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    event_lines = 0
    for line in lines[:5]:
        if line.startswith("{") and '"type"' in line and ('"sessionID"' in line or '"part"' in line):
            event_lines += 1
    return event_lines >= min(len(lines), 2)


_looks_like_event_blob = looks_like_event_blob


def _final_text_from_event_blob(text: str) -> str:
    final_parts: list[str] = []
    fallback_parts: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = _as_dict(event.get("part"))
        part_text = part.get("text") if isinstance(part.get("text"), str) else event.get("text")
        if not isinstance(part_text, str) or not part_text.strip():
            continue
        metadata = part.get("metadata")
        phase = _find_value(metadata, {"phase"}) if isinstance(metadata, dict) else None
        if phase == "final_answer" or str(event.get("type", "")).lower() in {"final", "complete", "done"}:
            final_parts.append(part_text)
        elif str(event.get("type", "")).lower() == "text" or str(part.get("type", "")).lower() == "text":
            fallback_parts.append(part_text)
    return "".join(final_parts or fallback_parts).strip()


def _tokens(info: dict, parts: object) -> TokenUsage:
    usage = TokenUsage()
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict):
                usage.add(_token_usage(part.get("tokens")))
    return usage if usage.total else _token_usage(info.get("tokens"))


def _token_usage(tokens: object) -> TokenUsage:
    if not isinstance(tokens, dict):
        return TokenUsage()
    cache = _as_dict(tokens.get("cache"))
    input_tokens = _int(tokens.get("input"))
    output_tokens = _int(tokens.get("output"))
    reasoning_tokens = _int(tokens.get("reasoning"))
    return TokenUsage(
        context=input_tokens or None,
        total=output_tokens + reasoning_tokens,
        input=input_tokens,
        output=output_tokens,
        reasoning=reasoning_tokens,
        cache_read=_int(cache.get("read")),
        cache_write=_int(cache.get("write")),
    )


def _model_name(info: dict) -> str | None:
    model = info.get("model")
    if isinstance(model, dict):
        provider = model.get("providerID")
        model_id = model.get("modelID") or model.get("id")
        if provider and model_id:
            return f"{provider}/{model_id}"
        return str(model_id) if model_id else None
    provider = info.get("providerID")
    model_id = info.get("modelID")
    if provider and model_id:
        return f"{provider}/{model_id}"
    return str(model_id) if model_id else None


def _find_value(value: object, keys: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, str) and item.strip():
                return item.strip()
            found = _find_value(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_value(item, keys)
            if found:
                return found
    return None


def _as_dict(value: object) -> dict:
    """Narrow an arbitrary JSON value to a dict (empty if it isn't one). Using a
    single call keeps mypy's isinstance-narrowing intact, unlike the inline
    `x.get(k) if isinstance(x.get(k), dict) else {}` idiom."""
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0
