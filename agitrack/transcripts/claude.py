from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from agitrack.backends.base import TokenUsage
from agitrack.transcripts.types import ExportedSession, SessionRef, SessionTurn, turns_after

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
    "import_shared_session",
    "prepare_resume",
    "link_session",
    "session_cwd",
    "retarget_session_cwd",
    "parse_rows",
]

# The "model" Claude Code stamps on synthetic (non-LLM) assistant messages —
# compaction notices, interrupt/"no response" markers. It names no real model, so
# the turn parser must not treat it as the conversation's model.
SYNTHETIC_MODEL = "<synthetic>"

# User messages whose text is purely a slash-command/tool artifact are not real
# prompts and should be excluded from the interaction trace.
_COMMAND_TAGS = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<user-prompt-submit-hook>",
    "<task-notification>",
)

# A typed slash command is recorded as a synthetic user row carrying a
# <command-name>/foo</command-name> artifact (see `_slash_command_name`). For
# commands that DO real work — most importantly /init, which writes CLAUDE.md —
# Claude Code then injects the command's expanded instructions as a separate
# `isMeta` user row, and the assistant's file-changing work follows. Capturing
# the command lets that expansion open a real turn so its work is committed.
_COMMAND_NAME_RE = re.compile(r"<command-name>\s*(/[^<]*?)\s*</command-name>")


def _projects_root() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
    return base / "projects"


def _encode_repo(repo: Path) -> str:
    # Claude Code names a project directory by replacing every non-alphanumeric
    # character of the absolute working directory with a dash.
    return re.sub(r"[^a-zA-Z0-9]", "-", str(repo.resolve()))


def _project_dir(repo: Path) -> Path:
    return _projects_root() / _encode_repo(repo)


def _session_path(repo: Path, session_id: str) -> Path:
    return _project_dir(repo) / f"{session_id}.jsonl"


def latest_session_id(repo: Path) -> str | None:
    refs = list_sessions(repo)
    # Prefer the newest conversation that actually has a user prompt. Claude mints
    # a fresh, EMPTY session id whenever a conversation is resumed or opened from
    # its session picker; that empty transcript is newest by mtime but has nothing
    # to resume. Treating it as "latest" makes aGiTrack adopt/resume it and drop the
    # user into a blank session on the next start — and only the start after that
    # recovers (the "first restart starts fresh, second restart resumes it"
    # off-by-one). A ref's label is its first real user prompt, so `label` is None
    # exactly when the transcript has no real turn. Fall back to raw recency only
    # if nothing has content yet (e.g. a brand-new, not-yet-used first session).
    resumable = [ref for ref in refs if ref.label]
    pool = resumable or refs
    if not pool:
        return None
    return max(pool, key=lambda ref: ref.updated).id


def _refs_in_project_dir(project_dir: Path) -> list[SessionRef]:
    if not project_dir.is_dir():
        return []
    refs = []
    for path in project_dir.glob("*.jsonl"):
        if not path.is_file():
            continue
        try:
            updated = path.stat().st_mtime
        except OSError:
            continue
        refs.append(SessionRef(id=path.stem, updated=updated, label=_session_label(path)))
    return refs


def list_sessions(repo: Path) -> list[SessionRef]:
    return _refs_in_project_dir(_project_dir(repo))


def list_worktree_sessions(worktrees_root: Path) -> list[tuple[str, SessionRef]]:
    """Every Claude conversation recorded under any aGiTrack worktree of this repo,
    newest first, paired with the worktree key needed to recreate it. Includes
    conversations whose worktree has since been deleted (Claude keeps the
    transcript keyed by the worktree path), so they stay resumable."""
    root = _projects_root()
    if not root.is_dir():
        return []
    prefix = _encode_repo(worktrees_root) + "-"
    out: list[tuple[str, SessionRef]] = []
    for project_dir in root.iterdir():
        if not project_dir.is_dir() or not project_dir.name.startswith(prefix):
            continue
        worktree_key = project_dir.name[len(prefix) :]
        if not worktree_key:
            continue
        for ref in _refs_in_project_dir(project_dir):
            out.append((worktree_key, ref))
    out.sort(key=lambda item: item[1].updated, reverse=True)
    return out


def _session_label(path: Path, *, line_limit: int = 100) -> str | None:
    # The first real user prompt makes a readable label; it is near the top of
    # the transcript, so reading only the head keeps listing cheap.
    try:
        with path.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index >= line_limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") == "user":
                    prompt = _user_prompt(row)
                    if prompt:
                        return prompt.splitlines()[0]
    except OSError:
        return None
    return None


def session_belongs_to_repo(repo: Path, session_id: str) -> bool:
    return _session_path(repo, session_id).is_file()


def prepare_resume(worktree: Path, session_id: str) -> bool:
    """Ensure ``claude --resume <session_id>`` works when run in ``worktree``.

    Claude looks up a session's transcript in the project dir of its current
    working directory, so a conversation recorded elsewhere (the repo root before
    aGiTrack ran, or a different worktree) is invisible from a fresh worktree. Link the
    transcript into the worktree's project dir so the resume finds it. We hardlink
    (one inode, two names) rather than copy, so turns aGiTrack appends from the worktree
    stay visible to a plain `claude` run in the original directory, and vice-versa
    — the conversation does not fork. Falls back to a copy only across filesystems
    (where hardlinks aren't possible). Returns True if the transcript is in place."""
    if not session_id:
        return False
    worktree = Path(worktree)
    target_dir = _project_dir(worktree)
    target = target_dir / f"{session_id}.jsonl"
    if target.is_file():
        return True
    source = _find_session_file(session_id)
    if source is None:
        return False
    if source.resolve() == target.resolve():
        return True
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        os.link(source, target)  # share one inode so new turns flow both ways
    except FileExistsError:
        return True
    except OSError:
        try:
            shutil.copy2(source, target)  # different filesystem: copy instead
        except OSError:
            return False
    return True


def link_session(session_id: str, src_repo: Path, dst_repo: Path) -> bool:
    """Hardlink a session's transcript from ``src_repo``'s project dir into
    ``dst_repo``'s, so the conversation is also visible/continuable from
    ``dst_repo`` — e.g. surfacing an aGiTrack worktree session in the repo root so a
    plain ``claude`` run there can resume it. One inode, two names, so later turns
    stay shared. No-op if the source isn't recorded yet or a transcript already
    sits at the destination."""
    if not session_id:
        return False
    src = _session_path(Path(src_repo), session_id)
    if not src.is_file():
        return False
    dst_dir = _project_dir(Path(dst_repo))
    dst = dst_dir / f"{session_id}.jsonl"
    if dst.exists():
        return True
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        os.link(src, dst)
    except FileExistsError:
        return True
    except OSError:
        return False
    return True


def export_session_raw(repo: Path, session_id: str) -> str | None:
    """The full transcript file's text for ``session_id`` under ``repo``'s project
    dir — the portable artifact shared with collaborators (issue #55). None when
    the session isn't recorded for this repo."""
    if not session_id:
        return None
    path: Path | None = _session_path(Path(repo), session_id)
    if path is None or not path.is_file():
        # A session recorded under a (possibly removed) worktree still has its
        # transcript keyed by path elsewhere — find it so dormant sessions can be
        # shared / refreshed too.
        path = _find_session_file(session_id)
    if path is None or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def session_transcript_size(repo: Path, session_id: str) -> int | None:
    """Byte size of a session's transcript file (a cheap ``stat``, no read) — used
    to tell at a glance whether the local conversation has grown past the shared
    copy without re-reading/redacting it. None when the transcript isn't found."""
    if not session_id:
        return None
    path: Path | None = _session_path(Path(repo), session_id)
    if path is None or not path.is_file():
        path = _find_session_file(session_id)
    if path is None:
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None


def has_imported_session(repo: Path, session_id: str) -> bool:
    """Whether ``repo``'s Claude project dir already holds this session's transcript
    (so resuming would otherwise keep the local copy rather than the shared one)."""
    return bool(session_id) and _session_path(Path(repo), session_id).is_file()


def import_shared_session(
    repo: Path, session_id: str, transcript: str, *, overwrite: bool = False, as_id: str | None = None
) -> bool:
    """Write a shared transcript into ``repo``'s Claude project dir as
    ``<session_id>.jsonl`` so a subsequent ``claude --resume <session_id>`` finds
    it (the normal resume path then links it into the session worktree). The
    transcript's ``cwd`` fields are retargeted to ``repo`` so Claude doesn't try to
    restore the original author's working directory.

    By default an existing local copy is kept (no clobber). With ``overwrite`` —
    the "pull the latest shared version" path for syncing your own session between
    machines — the local copy is *replaced*; it is unlinked first so a hardlink to
    a live worktree copy is broken rather than stomped.

    With ``as_id`` the conversation is installed under a NEW id instead (its
    ``sessionId`` fields are rewritten), so it can be resumed as a SEPARATE local
    session alongside an existing copy of the same conversation — the "keep both"
    path for an id that already exists locally. Returns True when in place."""
    if not session_id or not transcript:
        return False
    repo = Path(repo)
    effective_id = as_id or session_id
    target_dir = _project_dir(repo)
    target = target_dir / f"{effective_id}.jsonl"
    if target.is_file() and not overwrite and as_id is None:
        return True  # already have this conversation locally — don't clobber it
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)  # break any hardlink before replacing
        body = _retarget_rows(transcript, cwd=str(repo.resolve()), new_session_id=as_id)
        target.write_text(body, encoding="utf-8")
    except OSError:
        return False
    return True


def _retarget_rows(transcript: str, *, cwd: str, new_session_id: str | None = None) -> str:
    """Rewrite every row's ``cwd`` (and, when ``new_session_id`` is given, its
    ``sessionId``), leaving non-JSON lines untouched."""
    out: list[str] = []
    for line in transcript.split("\n"):
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            out.append(line)
            continue
        if isinstance(row, dict) and ("cwd" in row or (new_session_id and "sessionId" in row)):
            if "cwd" in row:
                row["cwd"] = cwd
            if new_session_id and "sessionId" in row:
                row["sessionId"] = new_session_id
            out.append(json.dumps(row))
        else:
            out.append(line)
    return "\n".join(out)


def retarget_session_cwd(repo: Path, session_id: str, cwd: str) -> bool:
    """Rewrite the ``cwd`` recorded in ``repo``'s copy of ``session_id``'s transcript
    to ``cwd``, so a resumed Claude session runs in ``cwd`` instead of a directory the
    conversation recorded earlier.

    Claude's ``--resume`` restores the working directory stored in the transcript, so
    a session first run inside a worktree keeps pointing at that worktree even when
    aGiTrack later launches it elsewhere (e.g. ``--no-worktree`` on the repo root). This
    aligns the transcript with the launch dir. Any hardlink to another copy (the
    original worktree's transcript) is broken first via ``unlink`` so ONLY this repo's
    copy is retargeted — the two then diverge, which is correct: they now run in
    different directories. No-op (and cheap) when the transcript is absent or already
    points at ``cwd``. Returns True only when a rewrite actually happened."""
    if not session_id or not cwd:
        return False
    path = _session_path(Path(repo), session_id)
    if not path.is_file():
        return False
    try:
        original = path.read_text(encoding="utf-8")
    except OSError:
        return False
    retargeted = _retarget_rows(original, cwd=cwd)
    if retargeted == original:
        return False  # already at this cwd — leave the (possibly hardlinked) file alone
    try:
        path.unlink(missing_ok=True)  # break any hardlink before replacing
        path.write_text(retargeted, encoding="utf-8")
    except OSError:
        return False
    return True


def session_cwd(session_id: str, *, since: float | None = None) -> str | None:
    """The working directory Claude most recently recorded for a session. Claude
    writes its `cwd` into (almost) every transcript line, so this reads the last
    one that has it from the newest transcript file. Used to detect a resume that
    restored the session's old cwd instead of the worktree it was launched in.

    When ``since`` (an epoch timestamp) is given, only rows whose `timestamp` is
    at or after it are considered, so a *stale* cwd recorded before the current
    launch is ignored — only a directory a post-launch turn actually ran in
    counts as drift. Returns None when no qualifying row exists yet (the caller
    then re-checks later instead of latching a premature, false warning)."""
    if not session_id:
        return None
    path = _find_session_file(session_id)
    if path is None:
        return None
    found: str | None = None
    cutoff = int(since) if since is not None else None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or '"cwd"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cutoff is not None:
                    stamp = _row_timestamp(row)
                    if stamp is None or stamp < cutoff:
                        continue  # stale (pre-launch) or undatable row — skip
                cwd = row.get("cwd")
                if isinstance(cwd, str) and cwd:
                    found = cwd  # keep the last one
    except OSError:
        return None
    return found


def _find_session_file(session_id: str) -> Path | None:
    # The transcript for a session id may live under any project dir (the repo
    # root, a worktree). Return the most recent match.
    root = _projects_root()
    if not root.is_dir():
        return None
    newest: tuple[float, Path] | None = None
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if not candidate.is_file():
            continue
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, candidate)
    return newest[1] if newest else None


def export_session(repo: Path, session_id: str) -> ExportedSession | None:
    path = _session_path(repo, session_id)
    if not path.is_file():
        return None
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None
    return parse_rows(session_id, rows, subagent_tokens=_subagent_token_map(path))


def _subagent_token_map(session_path: Path) -> dict[str | None, TokenUsage]:
    """Sub-agent token usage for a Claude session, keyed by the parent Task tool_use id.

    Newer Claude Code records each Task/Agent sub-agent in its OWN transcript file under
    ``<session>/subagents/agent-*.jsonl`` (with a sibling ``*.meta.json`` naming the
    ``toolUseId`` of the Task tool that spawned it), separate from the main transcript —
    so their tokens are invisible to a plain read of ``<session>.jsonl``. Sum each
    sub-agent's assistant usage into the sub-agent buckets, keyed by that tool id so the
    caller (`parse_rows`) can attribute it to the turn that launched it. A sub-agent file
    with no readable tool id is keyed under None (attributed to the latest turn rather
    than dropped). Returns an empty map when the session has no sub-agents."""
    subdir = session_path.with_suffix("") / "subagents"
    if not subdir.is_dir():
        return {}
    out: dict[str | None, TokenUsage] = {}
    try:
        agent_files = sorted(subdir.glob("agent-*.jsonl"))
    except OSError:
        return {}
    for agent_path in agent_files:
        out.setdefault(_subagent_tool_use_id(agent_path), TokenUsage()).add(_subagent_file_tokens(agent_path))
    return out


def _subagent_tool_use_id(agent_path: Path) -> str | None:
    # The Task tool_use id that spawned this sub-agent, read from its sibling
    # `agent-*.meta.json`. None when the meta is missing/unreadable.
    meta_path = agent_path.with_name(agent_path.name[: -len(".jsonl")] + ".meta.json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    tool_id = meta.get("toolUseId") if isinstance(meta, dict) else None
    return tool_id if isinstance(tool_id, str) and tool_id else None


def _subagent_file_tokens(agent_path: Path) -> TokenUsage:
    # Sum a sub-agent transcript's assistant token usage into the sub-agent buckets,
    # counting each message id once (the same row-splitting applies to sub-agent files).
    usage = TokenUsage()
    counted_ids: set[str] = set()
    try:
        with agent_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") == "assistant":
                    message = _as_dict(row.get("message"))
                    usage.add(_usage_once(message, counted_ids, sidechain=True))
    except OSError:
        pass
    return usage


def subagent_agent_files(repo: Path, session_id: str) -> set[str]:
    """Names of the sub-agent transcript files currently recorded for a session — a cheap
    snapshot the headless ``run()`` takes BEFORE a turn, so only the files that turn ADDS
    are counted afterwards (a resumed session already has prior sub-agents on disk)."""
    subdir = _subagents_dir(repo, session_id)
    if subdir is None or not subdir.is_dir():
        return set()
    try:
        return {path.name for path in subdir.glob("agent-*.jsonl")}
    except OSError:
        return set()


def subagent_tokens_since(repo: Path, session_id: str, prior_files: set[str]) -> TokenUsage:
    """Sub-agent token usage from transcript files NOT in ``prior_files`` — i.e. the
    sub-agents a just-finished headless turn spawned. Lets ``run()`` fold sub-agent
    consumption into its result even though Claude records each sub-agent in its own file,
    separate from the ``--output-format json`` usage (which covers only the main agent)."""
    usage = TokenUsage()
    subdir = _subagents_dir(repo, session_id)
    if subdir is None or not subdir.is_dir():
        return usage
    try:
        agent_files = sorted(subdir.glob("agent-*.jsonl"))
    except OSError:
        return usage
    for agent_path in agent_files:
        if agent_path.name not in prior_files:
            usage.add(_subagent_file_tokens(agent_path))
    return usage


def _subagents_dir(repo: Path, session_id: str) -> Path | None:
    if not session_id:
        return None
    return _session_path(Path(repo), session_id).with_suffix("") / "subagents"


def parse_rows(
    session_id: str,
    rows: list[dict],
    *,
    subagent_tokens: "dict[str | None, TokenUsage] | None" = None,
) -> ExportedSession:
    # `subagent_tokens` maps a Task tool_use id -> the sub-agent's token usage (in the
    # sub-agent buckets), for newer Claude Code where each sub-agent is recorded in its
    # OWN transcript file rather than inline in `rows` (see `_subagent_token_map`). Each
    # is added to the turn that launched that tool; the None key (a sub-agent with no
    # recoverable tool id) is attributed to the latest turn so its tokens are never lost.
    turns: list[SessionTurn] = []
    tool_ids_per_turn: list[set[str]] = []
    current: dict | None = None
    model: str | None = None
    updated: int | None = None
    # Claude splits one assistant API response (one message.id, one usage) across several
    # rows — one per content block — each carrying the FULL identical usage. Count each
    # message id's usage ONCE so tokens aren't multiplied by the block count (issue: the
    # per-row sum over-counted output by ~95% on real transcripts).
    counted_ids: set[str] = set()
    # Context compactions seen since the last turn began. Claude injects the compaction
    # summary as an `isCompactSummary` user row that sits BETWEEN turns (after the prior
    # turn's last message, before the next real prompt), so each is attributed to the
    # NEXT turn — the one whose context it shrank. A compaction with no following turn
    # influenced no work and is left unrecorded.
    pending_compactions = 0
    # The slash command (e.g. "/init") whose invocation row we just saw, awaiting its
    # expanded-instructions row to open a turn. Cleared once a turn opens (from the
    # expansion or the next real prompt). See `_slash_command_name`.
    pending_command: str | None = None

    def flush(*, dangling: bool = False) -> None:
        nonlocal current
        if current is not None:
            turns.append(_finalize_turn(current, dangling=dangling))
            tool_ids_per_turn.append(current.get("tool_ids") or set())
            current = None

    for row in rows:
        stamp = _row_timestamp(row)
        if stamp is not None:
            updated = stamp if updated is None else max(updated, stamp)
        row_type = row.get("type")
        if row_type == "user":
            if _is_interrupt_marker(row):
                # Esc: the turn is finished as far as commits are concerned —
                # it will never receive more messages — and Claude discarded
                # any queued prompts. The marker itself is not a user prompt.
                if current is not None:
                    current["interrupted"] = True
                continue
            if row.get("isCompactSummary"):
                # The summary Claude injects when it compacts the conversation: not a
                # prompt, but a token-affecting event. Tally it for the next turn.
                pending_compactions += 1
                continue
            command = _slash_command_name(row)
            if command is not None:
                # A typed slash command invocation. Remember it: a command that does
                # real work (e.g. /init) injects its expanded instructions as the next
                # isMeta user row, which then opens the turn. Commands with no expansion
                # (/model, /clear) leave this set but harmlessly unused.
                pending_command = command
                continue
            prompt = _user_prompt(row)
            if prompt is None:
                # The expanded instructions of a slash command arrive as an isMeta user
                # row. Right after a command invocation this row drives the turn (e.g.
                # /init writing CLAUDE.md), so open a turn labelled with the command;
                # otherwise meta rows stay excluded as before.
                if pending_command is None or _command_expansion_text(row) is None:
                    continue
                prompt = pending_command
            pending_command = None
            flush()
            current = {
                "user_id": str(row.get("uuid") or ""),
                "prompt": prompt,
                "final": "",
                "assistant_id": "",
                "model": model,
                "tokens": TokenUsage(),
                "stop_reason": None,
                "started_at": stamp,
                "ended_at": stamp,
                "tool_ids": set(),
                "compactions": pending_compactions,
                "reasoning_effort": None,
                "messages": [],
            }
            pending_compactions = 0
        elif row_type == "assistant" and current is not None and row.get("isSidechain"):
            # Sub-agent (sidechain) turns are not part of the main interaction
            # trace, but their tokens are still consumed — record them under the
            # turn's sub-agent buckets instead of dropping them.
            message = _as_dict(row.get("message"))
            current["tokens"].add(_usage_once(message, counted_ids, sidechain=True))
        elif row_type == "assistant" and current is not None:
            message = _as_dict(row.get("message"))
            if stamp is not None:
                current["ended_at"] = stamp
            current["tokens"].add(_usage_once(message, counted_ids))
            # Claude Code stamps synthetic (non-LLM) assistant messages — compaction
            # notices, interrupt/"no response" markers — with the literal model
            # "<synthetic>". That names no real model, so it must not overwrite the
            # turn's actual model (otherwise the commit, and the dashboard's by-model
            # breakdown, records "<synthetic>" instead of e.g. claude-opus-4-8).
            message_model = message.get("model")
            if isinstance(message_model, str) and message_model and message_model != SYNTHETIC_MODEL:
                current["model"] = message_model
                model = message_model
            # Track the most recent assistant message's stop reason; `tool_use`
            # means the turn is still mid-flight (more messages will follow the
            # tool result), anything else (end_turn/stop_sequence/max_tokens) is a
            # finished response.
            current["stop_reason"] = message.get("stop_reason")
            # Claude Code emits a `thinking` content block whenever extended
            # thinking is enabled, so its presence is the only signal the transcript
            # gives that reasoning was active (the budget itself is never recorded).
            if current["reasoning_effort"] is None and _has_thinking(message):
                current["reasoning_effort"] = "on"
            _collect_tool_use_ids(message, current["tool_ids"])
            text = _assistant_text(message)
            if text:
                current["final"] = text
                current["assistant_id"] = str(message.get("id") or "")
                # Each assistant message with user-facing text is a separate reply
                # (tool calls sit between them); keep them all in order so the
                # opt-in full trace can show every message, not just the last.
                current["messages"].append(text)
    flush(dangling=True)
    _attribute_subagent_tokens(turns, tool_ids_per_turn, subagent_tokens)
    return ExportedSession(session_id=session_id, model=model, updated=updated, turns=turns)


def _usage_once(message: dict, counted_ids: set[str], *, sidechain: bool = False) -> TokenUsage:
    # The token usage for an assistant message, counted exactly once across the several
    # rows Claude splits it into (each row shares the message id and the FULL usage). A row
    # whose `usage` is absent does NOT mark the id counted, so a later row of the same id
    # that DOES carry usage is still counted. Messages with no id can't be de-duplicated
    # and are counted as-is (Claude always assigns ids, so this is just a safe fallback).
    msg_id = message.get("id")
    msg_id = msg_id if isinstance(msg_id, str) and msg_id else None
    if msg_id is not None and msg_id in counted_ids:
        return TokenUsage()
    usage = message.get("usage")
    # Mark the id counted only once a NON-EMPTY usage is seen, so a split whose first row
    # carries no usage still has the later usage-bearing row of the same id counted.
    if msg_id is not None and isinstance(usage, dict) and usage:
        counted_ids.add(msg_id)
    return _message_tokens(usage, sidechain=sidechain)


def _collect_tool_use_ids(message: dict, sink: set[str]) -> None:
    # Record the ids of `tool_use` blocks in an assistant message, so a sub-agent
    # transcript (keyed by the Task tool_use id that spawned it) can be attributed to the
    # turn that launched it.
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_id = block.get("id")
            if isinstance(tool_id, str) and tool_id:
                sink.add(tool_id)


def _attribute_subagent_tokens(
    turns: list[SessionTurn],
    tool_ids_per_turn: list[set[str]],
    subagent_tokens: "dict[str | None, TokenUsage] | None",
) -> None:
    # Add each sub-agent's token usage to the turn that launched it (matched by Task
    # tool_use id). A sub-agent whose id matches no turn — or that had none recorded
    # (the None key) — is attributed to the latest turn, so its tokens are never dropped.
    if not subagent_tokens or not turns:
        return
    for tool_id, usage in subagent_tokens.items():
        index: int | None = None
        if tool_id is not None:
            index = next((i for i, ids in enumerate(tool_ids_per_turn) if tool_id in ids), None)
        if index is None:
            index = len(turns) - 1
        turns[index].tokens.add(usage)


def _row_timestamp(row: dict) -> int | None:
    # Transcript rows carry an ISO-8601 `timestamp`; the newest one is the
    # session's last-updated time.
    value = row.get("timestamp")
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _finalize_turn(turn: dict, *, dangling: bool = False) -> SessionTurn:
    interrupted = bool(turn.get("interrupted"))
    # Only the transcript's LAST (dangling) turn can still be mid-flight, and
    # only when it ends in `tool_use` (the one non-terminal stop reason; a
    # missing reason in older transcripts counts as complete). A turn flushed
    # because a new prompt began — or one the user interrupted — can never
    # receive more messages, so treating it as in-progress would stall the
    # commit loop forever.
    in_flight = dangling and not interrupted and turn.get("stop_reason") == "tool_use"
    return SessionTurn(
        user_message_id=turn["user_id"],
        assistant_message_id=turn["assistant_id"],
        user_prompt=turn["prompt"],
        final_response=turn["final"],
        tokens=turn["tokens"],
        model=turn["model"],
        complete=not in_flight,
        interrupted=interrupted,
        started_at=turn.get("started_at"),
        ended_at=turn.get("ended_at"),
        compaction_count=int(turn.get("compactions") or 0),
        reasoning_effort=turn.get("reasoning_effort"),
        agent_messages=list(turn.get("messages") or []),
    )


def _has_thinking(message: dict) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "thinking" for block in content)


_INTERRUPT_MARKER = "[Request interrupted by user"


def _is_interrupt_marker(row: dict) -> bool:
    # Esc leaves a user row whose text is "[Request interrupted by user]" (or
    # the "... for tool use" variant); it marks the abort, it is not a prompt.
    message = _as_dict(row.get("message"))
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        text = "".join(
            block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
    else:
        return False
    return text.startswith(_INTERRUPT_MARKER)


def _user_prompt(row: dict) -> str | None:
    # `isCompactSummary` marks the summary Claude injects as a user message when
    # it compacts a conversation (it also sets `isVisibleInTranscriptOnly`). It
    # is not a real prompt, so keep it out of the interaction trace and subject.
    if row.get("isMeta") or row.get("isSidechain") or row.get("isCompactSummary"):
        return None
    message = _as_dict(row.get("message"))
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"]
        if not parts:
            return None  # tool_result-only messages are not user prompts
        text = "".join(parts).strip()
    else:
        return None
    if not text or text.startswith(_COMMAND_TAGS) or text.startswith(_INTERRUPT_MARKER):
        return None
    return text


def _slash_command_name(row: dict) -> str | None:
    """The slash command a user row invokes (e.g. ``/init``), or None.

    Claude Code records a typed slash command as a synthetic user row carrying a
    ``<command-name>`` artifact rather than a normal prompt, so `_user_prompt`
    rightly drops it. We surface the command name separately so that — for a
    command whose expansion drives real work — the following expansion row can
    open a turn attributed to the command (see `parse_rows`)."""
    message = _as_dict(row.get("message"))
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        return None
    match = _COMMAND_NAME_RE.search(text)
    return match.group(1) if match else None


def _command_expansion_text(row: dict) -> str | None:
    """The expanded instructions a slash command injects, or None.

    Commands like ``/init`` substitute their body as a following ``isMeta`` user
    row (e.g. "analyze this codebase and create a CLAUDE.md"). Meta rows are not
    normally prompts, but right after a command invocation this row IS the turn's
    driver, so `parse_rows` opens a turn for it. Returns the row's prompt text
    when it is a meta row carrying real text (not another command artifact)."""
    if not row.get("isMeta"):
        return None
    message = _as_dict(row.get("message"))
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"]
        text = "".join(parts).strip()
    else:
        return None
    if not text or text.startswith(_COMMAND_TAGS):
        return None
    return text


def _assistant_text(message: dict) -> str:
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    texts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip()
    ]
    return "".join(texts).strip()


def _message_tokens(usage: object, *, sidechain: bool = False) -> TokenUsage:
    if not isinstance(usage, dict):
        return TokenUsage()
    input_tokens = _int(usage.get("input_tokens"))
    output_tokens = _int(usage.get("output_tokens"))
    cache_read = _int(usage.get("cache_read_input_tokens"))
    cache_write = _int(usage.get("cache_creation_input_tokens"))
    # Claude folds extended-thinking and tool-call tokens into output_tokens, so
    # there is no separate reasoning figure to record here.
    if sidechain:
        # A sub-agent has its own context window; only its consumption counts,
        # not its context size, so context is left untouched for the main turn.
        return TokenUsage(
            total=output_tokens,
            subagent_input=input_tokens,
            subagent_output=output_tokens,
            subagent_cache_read=cache_read,
            subagent_cache_write=cache_write,
        )
    return TokenUsage(
        context=(input_tokens + cache_read + cache_write) or None,
        total=output_tokens,
        input=input_tokens,
        output=output_tokens,
        reasoning=0,
        cache_read=cache_read,
        cache_write=cache_write,
    )


def _as_dict(value: object) -> dict:
    """Narrow an arbitrary JSON value to a dict (empty if it isn't one). Using a
    single call keeps mypy's isinstance-narrowing intact, unlike the inline
    `x.get(k) if isinstance(x.get(k), dict) else {}` idiom."""
    return value if isinstance(value, dict) else {}


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0
