from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from agitrack import paths
from agitrack.backends.base import TokenUsage
from agitrack.fileio import safe_is_dir
from agitrack.sessions.share_cap import select_kept_indices
from agitrack.transcripts import capabilities
from agitrack.transcripts.edits import content_from_read_output, seed_file_state, tracked_edit
from agitrack.transcripts.types import (
    SUBAGENT_LIVE_HORIZON_SECONDS,
    ExportedSession,
    FileEdit,
    SessionRef,
    SessionTurn,
    turns_after,
)

__all__ = [
    "ExportedSession",
    "SessionRef",
    "SessionTurn",
    "turns_after",
    "latest_session_id",
    "list_sessions",
    "list_worktree_sessions",
    "sessions_under",
    "session_belongs_to_repo",
    "export_session",
    "export_session_at",
    "export_session_raw",
    "session_transcript_size",
    "import_shared_session",
    "prepare_resume",
    "link_session",
    "forget_session_in",
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

# Labels for turns the agent ran in response to a harness `<task-notification>` rather than a
# user prompt. Two kinds (see `_task_notification_kind`): a TERMINAL notification (the
# backgrounded task finished or failed; carries a `<status>` tag) and an INTERMEDIATE monitor
# update (a Monitor streaming an `<event>` while the task keeps running). The distinction
# matters downstream: the commit engine defers monitor-update-only turns so a long-running
# monitor doesn't flood the history with one commit per tick.
_BACKGROUND_TURN_LABEL = "(background task completed)"
MONITOR_UPDATE_LABEL = "(background monitor update)"
BACKGROUND_PROMPT_LABELS = (_BACKGROUND_TURN_LABEL, MONITOR_UPDATE_LABEL)

# How recently a background task must have streamed a monitor event to still count as
# LIVE (see parse_rows' liveness bookkeeping). Generous enough for slow-ticking
# monitors; small enough that a task that died without a terminal notification stops
# suppressing the user-commit dialog within the hour.
_BACKGROUND_LIVE_HORIZON_SECONDS = 3600

# The tool result Claude Code returns when the agent launches an ASYNC sub-agent: the call
# returns immediately with the sub-agent's id, and the work continues in the background
# until a terminal `<task-notification>` (keyed by that same id) reports it back. A
# SYNCHRONOUS Agent/Task call returns the sub-agent's finished report instead and matches
# neither pattern — it is already over when the turn ends, so it is not tracked here.
_ASYNC_AGENT_LAUNCH_MARKER = "Async agent launched successfully"
_ASYNC_AGENT_ID_RE = re.compile(r"\bagentId:\s*([0-9a-zA-Z_-]+)")

# A typed slash command is recorded as a synthetic user row carrying a
# <command-name>/foo</command-name> artifact (see `_slash_command_name`). For
# commands that DO real work — most importantly /init, which writes CLAUDE.md —
# Claude Code then injects the command's expanded instructions as a separate
# `isMeta` user row, and the assistant's file-changing work follows. Capturing
# the command lets that expansion open a real turn so its work is committed.
_COMMAND_NAME_RE = re.compile(r"<command-name>\s*(/[^<]*?)\s*</command-name>")

# The arguments the user typed after the command, e.g.
#   <command-args>Improve the paper and record an experimentation plan.</command-args>
_COMMAND_ARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL)

# Slash commands are a moving target — Claude Code keeps adding them (`/goal` and `/loop` were
# the ones that exposed this, but the set is not knowable in advance and grows without warning),
# and user-defined skills are invoked the same way. So whether a command's arguments are a USER
# INSTRUCTION is decided from the arguments themselves, never from an allow-list of names:
#
#     a slash command whose arguments are PROSE (more than one word) carries a user
#     instruction, and belongs in the trace exactly like a typed prompt.
#
# Without this the instruction was lost entirely. Such a command has no `isMeta` expansion row
# (unlike `/init`), so `pending_command` was set, never consumed, and silently dropped — a
# commit produced by "/goal <a paragraph of requirements>" recorded no prompt at all, and the
# work looked unmotivated in both the history and the dashboard.
#
# Why prose, and why that is the whole test:
#
#   * It must NOT also require the agent to have responded. A `/goal` typed while the agent is
#     mid-tool-call, or as the last thing in a transcript, has no response to wait for yet —
#     and those are the ordinary ways these commands get used (steering work in progress). An
#     instruction is an instruction the moment it is typed; the reply comes later, and the turn
#     completes then, exactly as it does for a typed prompt.
#   * Something must still exclude configuration, because an unanswered turn is not free: it
#     stays incomplete and defers commits (the same reason `/compact` is excluded below). One
#     bare token — `/model sonnet`, `/status verbose`, `/goal clear` — is a parameter or a
#     control word, not something anyone asked the agent to do. Prose is.
#
# Erring is asymmetric and this errs the right way: losing a paragraph of requirements makes the
# resulting commits unexplainable, whereas a missed single-word directive costs a label.
_INSTRUCTION_WORDS = 2  # a command's args are an instruction from this many words up

# Values Claude stamps on a conversation's opening user row when the prompt came from a
# program rather than a person — see `_is_programmatic_row`.
_SDK_PROMPT_SOURCES = {"sdk"}
_SDK_ENTRYPOINTS = {"sdk", "sdk-cli"}


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


# How much of a transcript's tail to read when asking when it last had a message. Generous
# enough to span several rows of a long turn, small enough to be free on a 150 MB session.
_TAIL_BYTES = 65536


def _content_updated(path: Path, fallback: float) -> float:
    """When this conversation last had a MESSAGE, read from the tail of its transcript.

    The file's mtime cannot answer this. aGiTrack itself touches transcripts — hardlinking one
    into a worktree for a resume, mirroring it to the base repo, rewriting a recorded cwd — none
    of which adds a message, all of which bump the mtime. Observed live: a conversation
    abandoned by `/clear` hours earlier was linked into a worktree, became the newest file
    there, and was adopted as that session's conversation, taking the session's NAME with it —
    so the next start opened the session on the dead conversation and the real one came back
    under a fresh name.

    A transcript is append-only JSONL, so the newest message timestamp is at the end: only the
    tail is read, whatever the file's size. ``fallback`` (the mtime) is used when the tail
    carries no timestamp at all."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - _TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return fallback
    for raw in reversed(_TIMESTAMP_RE.findall(tail)):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return fallback


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
    # Ranked by CONTENT recency, not the file's mtime — see _content_updated for what mtime
    # ranking cost here (a dead conversation aGiTrack had merely touched being adopted as the
    # session's own).
    project = _project_dir(repo)
    return max(pool, key=lambda ref: _content_updated(project / f"{ref.id}.jsonl", ref.updated)).id


def _refs_in_project_dir(project_dir: Path) -> list[SessionRef]:
    if not safe_is_dir(project_dir):
        return []
    refs = []
    for path in project_dir.glob("*.jsonl"):
        if not path.is_file():
            continue
        try:
            updated = path.stat().st_mtime
        except OSError:
            continue
        label, programmatic = _scan_session_head(path)
        refs.append(SessionRef(id=path.stem, updated=updated, label=label, programmatic=programmatic))
    return refs


def list_sessions(repo: Path) -> list[SessionRef]:
    """Every conversation in this repository that a HUMAN is driving, newest-first ordering
    left to the caller.

    Programmatic transcripts (Agent SDK, ``claude -p``) are excluded. They are work an agent
    fanned out, not conversations to track, resume, or offer in a picker: an agent that farms
    a task out to one SDK worker per item writes one transcript per worker into this very
    directory, and adopting them would make aGiTrack commit each worker's run as if it were
    the user's next turn. `sessions_under` (used by ``--backtrace``) deliberately keeps them —
    reconstructing what happened in a directory should still see that work.
    """
    return [ref for ref in _refs_in_project_dir(_project_dir(repo)) if not ref.programmatic]


def list_worktree_sessions(worktrees_root: Path) -> list[tuple[str, SessionRef]]:
    """Every Claude conversation recorded under any aGiTrack worktree of this repo,
    newest first, paired with the worktree key needed to recreate it. Includes
    conversations whose worktree has since been deleted (Claude keeps the
    transcript keyed by the worktree path), so they stay resumable."""
    root = _projects_root()
    if not safe_is_dir(root):
        return []
    prefix = _encode_repo(worktrees_root) + "-"
    out: list[tuple[str, SessionRef]] = []
    for project_dir in root.iterdir():
        if not safe_is_dir(project_dir) or not project_dir.name.startswith(prefix):
            continue
        worktree_key = project_dir.name[len(prefix) :]
        if not worktree_key:
            continue
        for ref in _refs_in_project_dir(project_dir):
            out.append((worktree_key, ref))
    out.sort(key=lambda item: item[1].updated, reverse=True)
    return out


def _first_cwd(path: Path, *, line_limit: int = 200) -> str | None:
    """The first working directory a transcript file records (Claude stamps ``cwd`` on
    almost every row). Reads only the head of the file — enough to confirm which directory
    a session ran in without loading the whole transcript."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            for _, line in zip(range(line_limit), handle):
                if '"cwd"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = row.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def _within(directory: Path, cwd: str) -> bool:
    """Whether ``cwd`` is ``directory`` itself or a path beneath it (so a session that ran
    in a subdirectory or an ``.agitrack`` worktree of ``directory`` counts as having touched
    it)."""
    try:
        candidate = Path(cwd).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return candidate == directory or directory in candidate.parents


def sessions_under(directory: Path) -> list[tuple[SessionRef, Path]]:
    """Every Claude session whose recorded working directory is ``directory`` or a path
    beneath it, paired with its transcript file — the sessions the ``--backtrace`` feature
    reconstructs. Git is never consulted, so this works in a directory that was never a repo.

    Claude names each project directory by substituting the cwd's non-alphanumerics with
    dashes, so a session run under ``directory`` lives in a project dir whose name starts
    with ``directory``'s encoding; the recorded cwd is then re-read to reject a same-prefix
    sibling (``/a/b`` vs ``/a/b-c``)."""
    root = _projects_root()
    if not safe_is_dir(root):
        return []
    directory = directory.resolve()
    encoded = _encode_repo(directory)
    out: list[tuple[SessionRef, Path]] = []
    for project_dir in root.iterdir():
        if not safe_is_dir(project_dir):
            continue
        name = project_dir.name
        if name != encoded and not name.startswith(encoded + "-"):
            continue
        for ref in _refs_in_project_dir(project_dir):
            path = project_dir / f"{ref.id}.jsonl"
            cwd = _first_cwd(path)
            if cwd is not None and _within(directory, cwd):
                out.append((ref, path))
    out.sort(key=lambda item: item[0].updated, reverse=True)
    return out


def _is_programmatic_row(row: dict) -> bool:
    """Whether a conversation-opening user row came from a program rather than a person.

    Claude stamps where a prompt originated: an interactive TUI session records
    ``entrypoint: "cli"`` with ``promptSource: "typed"``, while a run driven by the Agent
    SDK or ``claude -p`` records ``sdk-cli``/``sdk``. Only POSITIVE evidence counts as
    programmatic — a transcript from an older Claude Code that stamps neither field stays
    adoptable, so this can never cost aGiTrack a session it used to track.
    """
    source = row.get("promptSource")
    if isinstance(source, str) and source.strip().lower() in _SDK_PROMPT_SOURCES:
        return True
    entrypoint = row.get("entrypoint")
    return isinstance(entrypoint, str) and entrypoint.strip().lower() in _SDK_ENTRYPOINTS


def _scan_session_head(path: Path, *, line_limit: int = 100) -> tuple[str | None, bool]:
    """``(label, programmatic)`` for a transcript, from one bounded read of its head.

    The label is the session's first real user prompt. Both answers live near the top of
    the file, so they are collected in a single pass to keep listing cheap.
    """
    label: str | None = None
    programmatic = False
    origin_seen = False
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
                if row.get("type") != "user":
                    continue
                if not origin_seen and not row.get("isSidechain"):
                    # The conversation's OWN opening prompt. Sidechain rows belong to a
                    # sub-agent running inside it and carry no origin fields, so they say
                    # nothing about who is driving the conversation.
                    origin_seen = True
                    programmatic = _is_programmatic_row(row)
                if label is None:
                    prompt = _user_prompt(row)
                    if prompt:
                        label = prompt.splitlines()[0]
                if label is not None and origin_seen:
                    break
    except OSError:
        return None, False
    return label, programmatic


def _session_label(path: Path, *, line_limit: int = 100) -> str | None:
    return _scan_session_head(path, line_limit=line_limit)[0]


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
    source = _find_session_file(session_id)  # newest copy of this id across all project dirs
    if source is None:
        return target.is_file()  # nothing better to stage; keep whatever is already there
    if source.resolve() == target.resolve():
        return True  # the target already IS the freshest copy (or hardlinked to it)
    # A copy may already sit at the target but be STALE: a prior resume hardlinked it, then
    # cwd-retargeting broke the hardlink, freezing it while the live copy elsewhere kept growing.
    # Returning early on mere existence would resume that OLDER frozen snapshot. Keep the existing
    # target only when it is at least as fresh (mtime AND size) as the newest source; otherwise
    # replace it so the resume gets the FULL, current conversation, not an older state.
    if target.is_file():
        try:
            src_stat, dst_stat = source.stat(), target.stat()
            if dst_stat.st_mtime >= src_stat.st_mtime and dst_stat.st_size >= src_stat.st_size:
                return True
            target.unlink()  # stale -> re-stage the newest copy below
        except OSError:
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


def forget_session_in(repo: Path, session_id: str) -> bool:
    """Drop ``repo``'s copy of a conversation's transcript — the conversation itself lives on.

    Claude files a transcript per working directory, so a conversation that RAN in one directory
    and then moved to another (aGiTrack relocating a `/clear`-started conversation into its own
    worktree) leaves a copy behind in the directory it left. That copy is not harmless: it is
    what ``list_worktree_sessions`` and ``latest_session_id`` read, so the old worktree goes on
    claiming a conversation that is now somebody else's — which is how, after a restart, two
    worktrees end up mixed up.

    Refuses unless the conversation is recorded somewhere ELSE, so this can only ever remove a
    duplicate, never the last copy. Returns True when a copy was removed."""
    if not session_id:
        return False
    target = _session_path(Path(repo), session_id)
    if not target.is_file():
        return False
    elsewhere = [
        path
        for path in _projects_root().glob(f"*/{session_id}.jsonl")
        if path.is_file() and path.resolve() != target.resolve()
    ]
    if not elsewhere:
        return False  # the only copy — dropping it would lose the conversation
    try:
        target.unlink()
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


def _is_resume_boundary(line: str) -> bool:
    """A row the trimmed tail can validly BEGIN at so ``claude --resume`` reconstructs the
    conversation: a ``user`` message whose content is plain text — a real prompt OR the
    compaction summary (both are ``type:"user"`` with a *string* content). A ``user`` row whose
    content is a LIST is a tool_result, which must not start a conversation (it would orphan the
    tool_use it answers — Claude then reports no prior context). Anchoring here keeps the tail
    user-first and reconstructible; verified against a real ``claude --resume``."""
    stripped = line.strip()
    if not stripped or "user" not in stripped:
        return False
    try:
        row = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    if not isinstance(row, dict) or row.get("type") != "user":
        return False
    message = row.get("message")
    return isinstance(message, dict) and isinstance(message.get("content"), str)


def _reroot_dangling_rows(lines: list[str]) -> list[str]:
    """After trimming, a kept row may reference a ``parentUuid`` that was dropped. Claude
    resumes by walking ``parentUuid`` from the newest message back to a root (``parentUuid:
    null``); if it hits a MISSING parent instead it reconstructs NOTHING ("no prior context").
    So rewrite every dangling ``parentUuid`` (parent not among the kept rows) to ``null``,
    turning the trimmed tail into a self-rooted, resumable conversation. Verified against a
    real ``claude --resume`` (kept tail resumes; a dangling parent does not)."""
    kept_uuids: set[str] = set()
    for line in lines:
        s = line.strip()
        if not s:
            continue
        try:
            row = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("uuid"):
            kept_uuids.add(row["uuid"])
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            out.append(line)
            continue
        try:
            row = json.loads(s)
        except json.JSONDecodeError:
            out.append(line)
            continue
        if isinstance(row, dict) and row.get("parentUuid") and row["parentUuid"] not in kept_uuids:
            row["parentUuid"] = None
            out.append(json.dumps(row))
        else:
            out.append(line)
    return out


def cap_shared_transcript(transcript: str, max_bytes: int) -> str:
    """Bound a Claude ``.jsonl`` transcript to ``max_bytes`` for sharing, keeping whole rows.
    Keeps the most recent turns as a CONTIGUOUS tail (anchored at a compaction summary, whose
    recap carries the dropped earlier context), then re-roots the chain so Claude can resume it.
    Returns ``transcript`` unchanged when it already fits.

    A disconnected "head" is deliberately NOT kept: empirically it leaves Claude unable to
    reconstruct the conversation on resume (it reports no prior context). The system prompt is
    re-applied by Claude at runtime and the compaction summary recaps persistent context, so a
    contiguous tail loses nothing needed while staying resumable."""
    if len(transcript.encode("utf-8")) <= max_bytes:
        return transcript
    lines = transcript.split("\n")
    sizes = [len(line.encode("utf-8")) for line in lines]
    boundary = [_is_resume_boundary(line) for line in lines]
    kept = select_kept_indices(sizes, boundary, max_bytes, sep_bytes=1)
    if kept is None:
        return transcript
    return "\n".join(_reroot_dangling_rows([lines[i] for i in kept]))


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


def _rewrite_path_prefixes(value, prefixes: tuple[str, ...], new: str):
    """Recursively rewrite any string under ``value`` that IS one of ``prefixes`` or sits under
    it so its prefix becomes ``new``. Used to repoint a resumed session's absolute file paths —
    tool ``file_path`` args, command output, mentions in text — from the old worktree it ran in
    to the launch dir, so the agent edits there and not the old worktree.

    Matching is by path SHAPE (:mod:`agitrack.paths`): a transcript mixes separators freely
    (``C:\\repo\\.agitrack\\worktrees\\x/app.py`` is one real example), and comparing raw
    strings simply left every such path pointing at the worktree."""
    if isinstance(value, str):
        for prefix in prefixes:
            tail = paths.relative_to(value, prefix)
            if tail is None:
                continue
            return new + ("/" + tail if tail else "")
        return value
    if isinstance(value, list):
        return [_rewrite_path_prefixes(item, prefixes, new) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite_path_prefixes(val, prefixes, new) for key, val in value.items()}
    return value


def _recorded_cwds(transcript: str) -> set[str]:
    """The distinct ``cwd`` directories a transcript records (the dirs the session has run in)."""
    found: set[str] = set()
    for line in transcript.split("\n"):
        stripped = line.strip()
        if not stripped or '"cwd"' not in stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        cwd = row.get("cwd") if isinstance(row, dict) else None
        if isinstance(cwd, str) and cwd:
            found.add(cwd)
    return found


def _retarget_rows(
    transcript: str,
    *,
    cwd: str,
    new_session_id: str | None = None,
    rewrite_prefixes: tuple[str, ...] = (),
    git_branch: str | None = None,
) -> str:
    """Rewrite every row's ``cwd`` (and, when ``new_session_id`` is given, its ``sessionId``).
    When ``rewrite_prefixes`` is given, also repoint any absolute path under those prefixes (a
    worktree the session previously ran in) to ``cwd`` — so a resumed agent edits the launch dir,
    not the old worktree it sees throughout its history.

    When ``git_branch`` is given, a row whose ``cwd`` is being MOVED to ``cwd`` (i.e. it was
    recorded somewhere else) also has its ``gitBranch`` retargeted to ``git_branch``. This is the
    last worktree fingerprint: after a session made in a worktree is resumed under ``--no-worktree``
    on the base repo, leaving every row stamped with the old ``agitrack/…`` worktree branch makes
    the resumed agent still read its whole history as "in a worktree." The rewrite is deliberately
    gated on the cwd actually moving, so a normal in-worktree resume (cwd unchanged, only the branch
    advanced a turn) is left byte-for-byte identical and its shared hardlink is preserved.

    Non-JSON lines, and rows nothing applies to, are left byte-for-byte unchanged."""
    prefixes = tuple(p for p in rewrite_prefixes if p)
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
        if not isinstance(row, dict):
            out.append(line)
            continue
        # Whether this row is being relocated is decided from its ORIGINAL cwd, before the
        # prefix rewrite below can already drag a worktree-prefixed cwd onto the target.
        cwd_moved = "cwd" in row and row.get("cwd") != cwd
        rewritten = _rewrite_path_prefixes(row, prefixes, cwd) if prefixes else row
        changed = rewritten != row
        row = rewritten
        if cwd_moved:
            row["cwd"] = cwd
            changed = True
        # Only move the branch on a row whose cwd we just relocated — the session is being
        # taken out of its worktree, so its worktree branch no longer describes where it runs.
        if git_branch and cwd_moved and row.get("gitBranch") not in (None, git_branch):
            row["gitBranch"] = git_branch
            changed = True
        if new_session_id and "sessionId" in row and row.get("sessionId") != new_session_id:
            row["sessionId"] = new_session_id
            changed = True
        out.append(json.dumps(row) if changed else line)
    return "\n".join(out)


def retarget_session_cwd(repo: Path, session_id: str, cwd: str, *, git_branch: str | None = None) -> bool:
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
    points at ``cwd``. Returns True only when a rewrite actually happened.

    ``git_branch`` (the launch dir's current branch) additionally retargets the ``gitBranch``
    of any row whose cwd is being moved, so a worktree session resumed on the base repo no
    longer carries the old ``agitrack/…`` worktree branch throughout its history — the final
    worktree fingerprint that otherwise makes the resumed agent read itself as still in a
    worktree. Gated on the cwd actually moving, so a plain in-worktree resume is untouched."""
    if not session_id or not cwd:
        return False
    path = _session_path(Path(repo), session_id)
    if not path.is_file():
        return False
    try:
        original = path.read_text(encoding="utf-8")
    except OSError:
        return False
    # Old aGiTrack worktrees this conversation ran in: repoint not just the `cwd` field but every
    # absolute path under them to the launch dir, so a resumed agent edits there rather than the
    # old worktree it sees throughout its history (tool file_path args, command output, mentions).
    # Scoped to our own ``.agitrack/worktrees/`` dirs so an imported session's unrelated absolute
    # paths (which don't exist in this repo anyway) are left alone — only its cwd field is aligned.
    worktree_prefixes = tuple(
        d for d in (_recorded_cwds(original) - {cwd}) if paths.contains_segments(d, "/.agitrack/worktrees/")
    )
    retargeted = _retarget_rows(original, cwd=cwd, rewrite_prefixes=worktree_prefixes, git_branch=git_branch)
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
    if not safe_is_dir(root):
        return None
    newest: tuple[float, Path] | None = None
    for project_dir in root.iterdir():
        if not safe_is_dir(project_dir):
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


def session_transcript_path(session_id: str) -> Path | None:
    """The path to a session's live transcript ``.jsonl`` (the newest match across project
    dirs), or None if not found. A caller can cache this and ``stat`` it repeatedly as a cheap
    liveness signal, instead of re-scanning the project dirs each time."""
    return _find_session_file(session_id) if session_id else None


def session_transcript_mtime(session_id: str) -> float | None:
    """The mtime (epoch seconds) of a session's transcript file, or None if not found.

    A CHEAP liveness signal (a single ``stat``, no read): Claude appends each message to the
    ``.jsonl`` as it happens — including a sub-agent's sidechain messages — so a turn that is
    working but printing nothing to the terminal (the main agent waiting on a sub-agent) still
    advances this. It lets aGiTrack tell "the turn is still running" from "the terminal is just
    quiet", so it doesn't decide the turn ended and try to commit mid-turn."""
    path = session_transcript_path(session_id)
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


_TIMESTAMP_RE = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')


def session_last_activity(session_id: str) -> float | None:
    """Last-activity time (epoch seconds) of a session, read from its transcript's CONTENT —
    the newest message ``timestamp`` — rather than the file's mtime. aGiTrack's own staging /
    cwd-retargeting rewrites a transcript (bumping the FILE mtime) without adding any message,
    so mtime is an unreliable "most recent conversation" signal — it can make an older session
    look newest after aGiTrack touches it. The message timestamps don't move, so they rank the
    conversations by genuine user activity. None if the transcript isn't found or has no stamp."""
    if not session_id:
        return None
    path = _find_session_file(session_id)
    if path is None:
        return None
    # The tail first: this is asked on a timer (the conversation-switch watcher), and a session's
    # transcript runs to hundreds of megabytes, so reading the whole file to find its LAST
    # timestamp would put a multi-megabyte read on every tick. Append-only JSONL puts the newest
    # timestamp at the end. Only a transcript whose tail somehow carries none is read in full.
    tail = _content_updated(path, float("nan"))
    if tail == tail:  # not NaN: the tail answered
        return tail
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    latest: float | None = None
    for match in _TIMESTAMP_RE.finditer(data):
        try:
            ts = datetime.fromisoformat(match.group(1).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def export_session(repo: Path, session_id: str, *, collect_edits: bool = False) -> ExportedSession | None:
    return export_session_at(_session_path(repo, session_id), collect_edits=collect_edits)


# The last export, keyed by the file's identity. A long session's transcript is large (this
# repo's own is ~150 MB) and re-reading it costs a fifth of a second EVERY time, which the
# user waits out with their prompt held ("checking existing git changes..."). An append-only
# JSONL that has not grown or been touched since the last read cannot have changed, so the
# previous result stands. One entry only, and dropped after a couple of minutes: this exists
# to serve the same file twice in a row, not to hold a 150 MB session in memory all day.
_LAST_EXPORT: tuple[tuple, "ExportedSession | None", float] | None = None
_EXPORT_MEMO_SECONDS = 120.0


def export_session_at(path: Path, *, collect_edits: bool = False) -> ExportedSession | None:
    """Export the session recorded in the transcript file at ``path`` (the session id is
    its filename stem). Reads a specific file rather than encoding a repo path, so the
    backtrace scanner can export sessions it discovered under any project directory —
    including ones whose recorded cwd is a subdirectory or a deleted worktree.

    ``collect_edits`` also recovers each turn's file edits from the tool-call inputs (see
    :func:`_edits_from_message`); it is off for ordinary exports.

    Repeated calls for an UNCHANGED file (same size and mtime) reuse the previous result."""
    global _LAST_EXPORT
    if not path.is_file():
        return None
    try:
        stamp = path.stat()
        key = (str(path), stamp.st_size, stamp.st_mtime_ns, collect_edits)
    except OSError:
        key = None
    if _LAST_EXPORT is not None:
        cached_key, cached, stored_at = _LAST_EXPORT
        if time.monotonic() - stored_at > _EXPORT_MEMO_SECONDS:
            _LAST_EXPORT = None  # let a big session go rather than hold it for a caller who left
        elif key is not None and cached_key == key:
            return cached
    rows: list[dict] = []
    try:
        # errors="replace", NEVER strict: a transcript is appended to by the backend while we
        # read it, and one undecodable byte — a torn multi-byte character mid-write, or a lone
        # surrogate that reached the transcript from pasted/mis-decoded input — would otherwise
        # raise here and make the WHOLE session unparseable. That is not a degraded parse, it
        # is no commits at all for that session, silently. Per-line JSON errors are already
        # tolerated a few lines below for the same reason; this is the byte-level equivalent.
        with path.open("r", encoding="utf-8", errors="replace") as handle:
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
    exported = parse_rows(
        path.stem,
        rows,
        subagent_tokens=_subagent_token_map(path),
        unmatched_subagent_time=_subagent_unmatched_mtime(path),
        subagent_activity=_subagent_activity(path),
        collect_edits=collect_edits,
    )
    if key is not None:
        _LAST_EXPORT = (key, exported, time.monotonic())
    return exported


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
    if not safe_is_dir(subdir):
        return {}
    out: dict[str | None, TokenUsage] = {}
    try:
        agent_files = sorted(subdir.glob("agent-*.jsonl"))
    except OSError:
        return {}
    for agent_path in agent_files:
        out.setdefault(_subagent_tool_use_id(agent_path), TokenUsage()).add(_subagent_file_tokens(agent_path))
    return out


def _subagent_unmatched_mtime(session_path: Path) -> int | None:
    """The newest mtime (epoch seconds) among sub-agent files with NO readable parent tool
    id — the ones keyed under ``None`` in :func:`_subagent_token_map`. Lets ``parse_rows``
    attribute those id-less sub-agents to the turn active when they ran, instead of always
    the latest turn (which re-attaches, and double-counts, them onto each new turn on every
    re-parse). ``None`` when there are no id-less sub-agents or none has a readable mtime."""
    subdir = session_path.with_suffix("") / "subagents"
    if not safe_is_dir(subdir):
        return None
    try:
        agent_files = sorted(subdir.glob("agent-*.jsonl"))
    except OSError:
        return None
    newest: int | None = None
    for agent_path in agent_files:
        if _subagent_tool_use_id(agent_path) is not None:
            continue
        try:
            mtime = int(agent_path.stat().st_mtime)
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def _subagent_activity(session_path: Path) -> dict[str, int]:
    """``agent id -> mtime`` (epoch seconds) for each sub-agent transcript of this session.

    Claude Code names the file after the sub-agent itself (``agent-<agentId>.jsonl``) — the
    very id its launch tool result reported and its terminal `<task-notification>` carries —
    so the mtime is a direct read of when that sub-agent last produced anything. `parse_rows`
    uses it as the liveness evidence behind ``live_subagent_ids``: a sub-agent still writing
    is still working, and one silent past the horizon is presumed dead so it cannot defer
    commits forever. Empty when the session has no sub-agents (or an older Claude Code that
    keeps them inline, in which case liveness falls back to the launch time)."""
    subdir = session_path.with_suffix("") / "subagents"
    if not safe_is_dir(subdir):
        return {}
    out: dict[str, int] = {}
    try:
        agent_files = sorted(subdir.glob("agent-*.jsonl"))
    except OSError:
        return {}
    for agent_path in agent_files:
        agent_id = agent_path.name[len("agent-") : -len(".jsonl")]
        if not agent_id:
            continue
        try:
            out[agent_id] = int(agent_path.stat().st_mtime)
        except OSError:
            continue
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
    if subdir is None or not safe_is_dir(subdir):
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
    if subdir is None or not safe_is_dir(subdir):
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


def _queued_human_prompt(row: dict) -> str | None:
    """A message the user QUEUED while the agent was still working. Claude Code records such a
    message as a ``type:"attachment"`` row (``attachment.type == "queued_command"``) — NOT a
    ``type:"user"`` row — so the normal user-prompt path never sees it, and without this it is
    dropped from the interaction trace (the user's follow-up instructions vanish from the commit).

    Returns the prompt text for a genuine human prompt (``commandMode == "prompt"``, human origin,
    not a slash directive), else None. The queued text belongs to the turn in flight when it was
    sent — Claude threads it into the same response rather than opening a new ``user`` row."""
    if row.get("type") != "attachment":
        return None
    att = row.get("attachment")
    if not isinstance(att, dict) or att.get("type") != "queued_command":
        return None
    if att.get("commandMode") != "prompt":
        return None  # a queued slash/bash directive, not a typed prompt
    origin = att.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None  # only genuine human input, never a tool/system-injected queue entry
    prompt = att.get("prompt")
    if not isinstance(prompt, str):
        return None
    text = prompt.strip()
    if not text or text.startswith("/"):
        return None  # empty, or a slash command kept out of the trace like any other
    return text


def _superseded_prompt_ids(rows: list[dict]) -> set[str]:
    """Ids of ``user`` rows the user REWOUND past — edited and re-sent before the agent
    acted on them.

    Editing a prompt does not rewrite its row: Claude Code branches, recording the revised
    text as a NEW user row hanging off the SAME ``parentUuid`` and continuing the
    conversation from that one. The abandoned row stays in the transcript with nothing
    descending from it, so replaying the file linearly showed the discarded draft in the
    interaction trace next to the real prompt — the same message twice, once as the user
    first wrote it.

    Siblings are the signal: among ``user`` rows sharing a parent, only the last is live.
    A genuine mid-turn follow-up is NOT a sibling (Claude threads those into the running
    turn as ``attachment`` rows), so this cannot swallow one. Rows with no parent are left
    alone — every conversation start shares "no parent" without being a rewind.
    """
    seen: dict[str, str] = {}
    superseded: set[str] = set()
    for row in rows:
        if row.get("type") != "user" or row.get("isSidechain"):
            continue
        parent = str(row.get("parentUuid") or "")
        uuid = str(row.get("uuid") or "")
        if not parent or not uuid:
            continue
        previous = seen.get(parent)
        if previous is not None:
            superseded.add(previous)  # an earlier draft off the same parent: abandoned
        seen[parent] = uuid
    return superseded


def parse_rows(
    session_id: str,
    rows: list[dict],
    *,
    subagent_tokens: "dict[str | None, TokenUsage] | None" = None,
    unmatched_subagent_time: int | None = None,
    subagent_activity: dict[str, int] | None = None,
    collect_edits: bool = False,
) -> ExportedSession:
    # `subagent_tokens` maps a Task tool_use id -> the sub-agent's token usage (in the
    # sub-agent buckets), for newer Claude Code where each sub-agent is recorded in its
    # OWN transcript file rather than inline in `rows` (see `_subagent_token_map`). Each
    # is added to the turn that launched that tool; the None key (a sub-agent with no
    # recoverable tool id) is attributed to the turn that was active at
    # `unmatched_subagent_time` (its file mtime), or the latest turn if that is unknown —
    # so its tokens are never lost, and are attributed to a STABLE turn that the commit
    # watermark can trim, instead of being re-attributed onto each new turn every re-parse.
    #
    # `subagent_activity` maps an async sub-agent's id -> the mtime of its own transcript
    # file, the freshest evidence available that it is still working (see
    # `_subagent_activity`). Injected rather than read here so parse_rows stays a pure
    # function of `rows`.
    turns: list[SessionTurn] = []
    tool_ids_per_turn: list[set[str]] = []
    # Prompts the user edited before the agent answered: their rows are still in the file
    # but nothing descends from them (see _superseded_prompt_ids).
    superseded = _superseded_prompt_ids(rows)
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
    # Set when a `<task-notification>` row arrived ("completed" for a terminal notification,
    # "update" for an intermediate monitor event; None otherwise). It is not a prompt, but if
    # the agent then does work off the back of it — with the prior turn already finished and no
    # new user prompt in between — that work opens its own turn rather than being merged into
    # the previous (already-committed) turn. See `_task_notification_kind`.
    pending_background: str | None = None
    # Liveness of background tasks, judged from the NOTIFICATION stream (launch-based
    # counting overcounts badly: a task finishing while the agent is mid-turn delivers
    # its result without a terminal notification). A task that has streamed an
    # intermediate monitor event RECENTLY and has no terminal notification after it is
    # treated as still running and still writing; the recency horizon stops a monitor
    # that died notification-less from suppressing the user-commit dialog forever.
    background_last_event: dict[str, int] = {}
    background_terminated: set[str] = set()
    # Async sub-agents this session launched -> the launch timestamp. A sub-agent leaves the
    # set the moment a terminal `<task-notification>` names it (`background_terminated`); the
    # ones still in it after the horizon check are `live_subagent_ids`, and while any exist the
    # launching turn's closing message is not the end of the work it describes.
    spawned_subagents: dict[str, int] = {}
    # Per-session running content of each edited file, so a Write/Edit's diff is the incremental
    # change vs the previous turn, not the whole file every time (only used when collect_edits).
    file_state: dict[str, str] = {}
    # Read tool_use id -> file path, for whole-file reads still awaiting their tool_result. The
    # result carries the file's pre-existing content, which seeds `file_state` so a later Write
    # diffs against it instead of counting the whole (already existing) file as newly added.
    pending_reads: dict[str, str] = {}
    # The skills currently loaded into this session's context. Loading a skill is not a one-turn
    # action: it injects standing instructions that keep shaping every later turn, so a skill joins
    # this roster when it loads and every turn opened afterwards inherits it. Crediting only the
    # loading turn lost the skill entirely in the common case — a bare `/<skill>` edits nothing, so
    # that turn never produced a commit, and the whole session's provenance came out empty.
    # Cleared whenever the context holding the instructions is discarded (compaction, /clear):
    # past that point the transcript no longer shows the skill in play, and aGiTrack does not claim
    # capabilities it cannot prove.
    active_skills: list[str] = []

    def flush(*, dangling: bool = False) -> None:
        nonlocal current
        if current is not None:
            turns.append(_finalize_turn(current, dangling=dangling))
            tool_ids_per_turn.append(current.get("tool_ids") or set())
            current = None

    def note_notification(row: dict) -> str | None:
        """Record what a `<task-notification>` row says about the task it names, and return
        its kind (or None when the row is not a notification). Called for BOTH row shapes —
        the delivered ``user`` message and the queued ``attachment`` — because a task that
        reports back while the agent is busy is only ever recorded as the latter, and that is
        precisely the case that has to end a sub-agent's liveness."""
        kind = _task_notification_kind(row)
        if kind is None:
            return None
        task_id = _notification_task_id(row)
        if task_id:
            if kind == "completed":
                background_terminated.add(task_id)
                background_last_event.pop(task_id, None)
                # An async sub-agent reports back through the same channel, keyed by the same
                # id: its work is done, so it stops holding the launching turn's commit.
                spawned_subagents.pop(task_id, None)
            else:
                background_last_event[task_id] = _row_timestamp(row) or 0
        return kind

    for row in rows:
        stamp = _row_timestamp(row)
        if stamp is not None:
            updated = stamp if updated is None else max(updated, stamp)
        row_type = row.get("type")
        if row_type == "user":
            for agent_id in _launched_async_agent_ids(row):
                # The agent spawned a sub-agent that keeps working after this turn ends.
                # Recorded FIRST, before any branch below can `continue` past this row: the
                # launch acknowledgment is a tool_result, which every one of them discards.
                spawned_subagents.setdefault(agent_id, stamp or 0)
            if str(row.get("uuid") or "") in superseded:
                # A draft the user edited before the agent saw it: nothing in the
                # conversation descends from this row, so it is not part of the history.
                continue
            if collect_edits and pending_reads:
                # A Read's result: seed the file's pre-existing content before any later Write.
                _seed_reads_from_result(_as_dict(row.get("message")), pending_reads, file_state)
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
                # Compaction replaces the conversation with a summary, so a loaded skill's
                # verbatim instructions are gone from the context. Whether the summary carried
                # their gist forward is not something the transcript says, so stop claiming them.
                active_skills.clear()
                continue
            notification_kind = note_notification(row)
            if notification_kind is not None:
                # A backgrounded task reported back. Not a prompt; defer to the assistant
                # branch, which opens a NEW turn for any work the agent does in response (so
                # it is committed and attributed on its own, not folded into the prior turn).
                pending_background = notification_kind
                continue
            hot_loaded = _hot_loaded_skill(row)
            if hot_loaded is not None:
                _remember_skills(active_skills, (hot_loaded,))
                # `/skill do the thing` — the args are an instruction, so the command row already
                # opened the turn and the body follows it; add the skill to that turn directly.
                # A bare `/skill` has no turn yet (`current` is still the PREVIOUS turn, which
                # must not be credited): the roster seeds the turn this body is about to open.
                if pending_command is None and current is not None:
                    _remember_skills(current.setdefault("skills", []), (hot_loaded,))
            command = _slash_command_name(row)
            if command == "/clear":
                # The context is wiped, so nothing loaded before it is in effect any more.
                active_skills.clear()
            if command is not None:
                # A command carrying a user INSTRUCTION (`/goal …`, `/loop …`, a skill
                # invocation) IS a prompt — fall straight through and open its turn now, exactly
                # as a typed prompt does. Not deferred until the agent replies: a directive
                # typed while the agent is mid-tool-call, or as the last row in the transcript,
                # has no reply to wait for yet, and those are the ordinary ways these commands
                # are used. The turn simply stays in flight until the reply lands, like any other.
                directive = _slash_command_directive(row)
                if directive is None:
                    # Everything else. Remember the invocation: a command that does real work
                    # (e.g. /init) injects its expanded instructions as the next isMeta user row,
                    # which then opens the turn. Commands with no expansion (/model, /clear)
                    # leave this set but harmlessly unused.
                    pending_command = command
                    continue
                prompt: str | None = directive
            else:
                prompt = _user_prompt(row)
            if prompt is None:
                # The expanded instructions of a slash command arrive as an isMeta user
                # row. Right after a command invocation this row drives the turn (e.g.
                # /init writing CLAUDE.md), so open a turn labelled with the command;
                # otherwise meta rows stay excluded as before.
                if pending_command is None or _command_expansion_text(row) is None:
                    continue
                prompt = pending_command
            if prompt == "/compact" or prompt.startswith("/compact "):
                # Newer Claude Code records a typed /compact as a PLAIN user row (no
                # <command-name> artifact). It is a local command driving the compaction
                # recorded right after it (compact_boundary + isCompactSummary rows),
                # never receives an assistant reply, and must not open a turn: an
                # unanswered "/compact" turn would ride every later commit's trace.
                continue
            pending_command = None
            pending_background = None  # a real prompt supersedes a pending background-task turn
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
                "tool_names": [],
                # Every skill the session has loaded and not discarded is still shaping this turn.
                "skills": list(active_skills),
                "subagents": [],
                "compactions": pending_compactions,
                "reasoning_effort": None,
                "messages": [],
            }
            pending_compactions = 0
        elif row_type == "attachment":
            notification_kind = note_notification(row)
            if notification_kind is not None:
                # A task that reported back while the agent was still working: the harness
                # QUEUES the notification, so it lands as an attachment rather than a user
                # row. Same bookkeeping, same hand-off to the assistant branch.
                pending_background = notification_kind
                continue
            queued = _queued_human_prompt(row)
            if queued is not None:
                if current is not None:
                    # A message the user queued while the agent was working: Claude threads it into
                    # the SAME response (no separate `user` row), so it belongs to the turn in flight.
                    # Keep it as a DISTINCT message (its own ## User heading in the trace) rather than
                    # merging it into the base prompt — the user sent it after the agent had already
                    # said something.
                    current.setdefault("queued_followups", []).append(queued)
                else:
                    # Queued before any turn opened in this parse window — open one for it.
                    flush()
                    current = {
                        "user_id": str(row.get("uuid") or ""),
                        "prompt": queued,
                        "final": "",
                        "assistant_id": "",
                        "model": model,
                        "tokens": TokenUsage(),
                        "stop_reason": None,
                        "started_at": stamp,
                        "ended_at": stamp,
                        "tool_ids": set(),
                        "tool_names": [],
                        "skills": list(active_skills),
                        "subagents": [],
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
            if pending_background and current.get("stop_reason") not in (None, "tool_use"):
                # The agent is acting on a completed background task, and the current turn has
                # already finished (a real stop reason, not mid-tool) with no new user prompt
                # since. Open a fresh turn so this background-driven work is committed and
                # attributed on its own — not merged into the prior, already-committed turn
                # (which would also overwrite its assistant id and break the commit watermark).
                new_prompt = MONITOR_UPDATE_LABEL if pending_background == "update" else _BACKGROUND_TURN_LABEL
                flush()
                current = {
                    "user_id": str(row.get("uuid") or ""),
                    "prompt": new_prompt,
                    "final": "",
                    "assistant_id": "",
                    "model": model,
                    "tokens": TokenUsage(),
                    "stop_reason": None,
                    "started_at": stamp,
                    "ended_at": stamp,
                    "tool_ids": set(),
                    "tool_names": [],
                    "skills": list(active_skills),
                    "subagents": [],
                    "compactions": pending_compactions,
                    "reasoning_effort": None,
                    "messages": [],
                }
                pending_compactions = 0
            pending_background = None
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
            if _assistant_text(message) == "No response requested.":
                # Claude Code's synthetic filler for an aborted/crashed request. It must not
                # contribute ANYTHING to the turn: not a final message (it isn't one), and
                # not its stop_reason either — taking the filler's "end_turn" made a crashed
                # turn look complete-but-answerless, which the no-final-text fallback then
                # committed with a trace ending in a bare user message. Leaving the
                # stop_reason unset keeps the dangling turn in-flight (see
                # _finalize_turn) until the restarted process produces a real reply.
                continue
            current["stop_reason"] = message.get("stop_reason")
            # Claude Code emits a `thinking` content block whenever extended
            # thinking is enabled, so its presence is the only signal the transcript
            # gives that reasoning was active (the budget itself is never recorded).
            if current["reasoning_effort"] is None and _has_thinking(message):
                current["reasoning_effort"] = "on"
            _collect_tool_use_ids(message, current["tool_ids"])
            _collect_capabilities(message, current)
            # A `Skill` call loads its instructions into the same context a `/<skill>` does, so it
            # joins the roster too. Done HERE, as the call is read, rather than when the turn is
            # finalized: a compaction later in the transcript must be able to clear a skill loaded
            # before it, and turns are finalized only once the NEXT prompt arrives.
            _remember_skills(active_skills, current.get("skills") or ())
            if collect_edits:
                # Reconstruct this turn's file edits from the tool-call inputs (opt-in; the
                # backtrace exporter is the only caller). Attributed to the turn in flight,
                # so they land on the same SessionTurn the conversation trace does.
                current.setdefault("edits", []).extend(_edits_from_message(message, file_state))
                pending_reads.update(_whole_file_reads(message))
            text = _assistant_text(message)
            if text:
                current["final"] = text
                current["assistant_id"] = str(message.get("id") or "")
                # Each assistant message with user-facing text is a separate reply
                # (tool calls sit between them); keep them all in order so the
                # opt-in full trace can show every message, not just the last.
                current["messages"].append(text)
    flush(dangling=True)
    _attribute_subagent_tokens(turns, tool_ids_per_turn, subagent_tokens, unmatched_subagent_time)
    horizon = time.time() - _BACKGROUND_LIVE_HORIZON_SECONDS
    live_ids = sorted(
        task_id
        for task_id, last_event in background_last_event.items()
        if task_id not in background_terminated and last_event >= horizon
    )
    # Async sub-agents launched and never reported back. Each one's freshest sign of life is
    # the later of its launch and the last write to its own transcript; past the horizon it is
    # presumed dead (killed process, crashed harness) rather than allowed to defer this repo's
    # commits indefinitely.
    activity = subagent_activity or {}
    subagent_horizon = time.time() - SUBAGENT_LIVE_HORIZON_SECONDS
    live_subagents = sorted(
        agent_id
        for agent_id, launched_at in spawned_subagents.items()
        if agent_id not in background_terminated and max(launched_at, activity.get(agent_id, 0)) >= subagent_horizon
    )
    return ExportedSession(
        session_id=session_id,
        model=model,
        updated=updated,
        turns=turns,
        live_background_task_ids=live_ids,
        live_subagent_ids=live_subagents,
    )


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


_NOTIFICATION_TASK_ID_RE = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")


def _notification_task_id(row: dict) -> str | None:
    """The harness task id a `<task-notification>` row refers to, if present. (The
    ``<task-id>`` tag appears in BOTH shapes; ``<tool-use-id>`` only in terminal ones,
    so liveness bookkeeping keys on the task id.)

    An async sub-agent's task id IS its ``agentId`` — the same string its launch tool
    result reported and its own transcript file is named after — so the launch and the
    report-back match up without any extra bookkeeping."""
    match = _NOTIFICATION_TASK_ID_RE.search(_notification_row_text(row))
    return match.group(1) if match else None


def _launched_async_agent_ids(row: dict) -> list[str]:
    """Ids of async sub-agents whose LAUNCH this user row acknowledges.

    An async ``Agent``/``Task`` call returns immediately with a tool result that names the
    spawned sub-agent (``agentId: …``) and says the work continues in the background; the
    sub-agent then keeps editing files long after the launching turn has posted what looks
    like a final answer. Recording the launch is what lets `parse_rows` know that turn is
    not really over (see ``live_subagent_ids``). A synchronous call returns the sub-agent's
    finished report instead and produces no such marker."""
    message = _as_dict(row.get("message"))
    content = message.get("content")
    if not isinstance(content, list):
        return []
    found: list[str] = []
    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
            continue
        result = block.get("content")
        if isinstance(result, str):
            text = result
        elif isinstance(result, list):
            text = "".join(part.get("text", "") for part in result if isinstance(part, dict))
        else:
            continue
        if _ASYNC_AGENT_LAUNCH_MARKER not in text:
            continue
        match = _ASYNC_AGENT_ID_RE.search(text)
        if match:
            found.append(match.group(1))
    return found


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


# Tool calls whose INPUT names the capability being used, rather than the tool name itself:
# `Skill` invokes a named skill (a plugin's shows as "plugin:skill"), `Agent`/`Task` spawns a
# named sub-agent type. Recorded so the commit says WHICH skill/sub-agent ran, not just that one did.
_SKILL_TOOLS = {"Skill"}
_SUBAGENT_TOOLS = {"Agent", "Task"}


def _collect_capabilities(message: dict, turn: dict) -> None:
    """Record the tool names this assistant message called, plus the skill / sub-agent each
    capability-invoking call names, onto the turn in flight. Split into MCP servers/tools later
    (see :mod:`agitrack.transcripts.capabilities`) so the naming convention lives in one place."""
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "tool_use"):
            continue
        name = str(block.get("name") or "")
        if not name:
            continue
        turn.setdefault("tool_names", []).append(name)
        payload = block.get("input")
        payload = payload if isinstance(payload, dict) else {}
        if name in _SKILL_TOOLS:
            skill = payload.get("skill") or payload.get("name")
            if isinstance(skill, str) and skill.strip():
                turn.setdefault("skills", []).append(skill.strip())
        elif name in _SUBAGENT_TOOLS:
            # `subagent_type` is optional — an unnamed Agent call runs the default agent, which is
            # still worth recording as a sub-agent (it is a separate context that did work).
            agent_type = payload.get("subagent_type") or "default"
            if isinstance(agent_type, str) and agent_type.strip():
                turn.setdefault("subagents", []).append(agent_type.strip())


def _whole_file_reads(message: dict) -> dict[str, str]:
    """``tool_use id -> file path`` for each Read of a file's FULL content in this assistant
    message. A ranged read (``offset``/``limit``) is skipped: its result is only a slice, so it
    can't stand in for the file's prior content."""
    content = message.get("content")
    if not isinstance(content, list):
        return {}
    reads: dict[str, str] = {}
    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Read"):
            continue
        raw_input = block.get("input")
        inp = raw_input if isinstance(raw_input, dict) else {}
        if inp.get("offset") or inp.get("limit"):
            continue
        path, tool_id = inp.get("file_path"), block.get("id")
        if isinstance(path, str) and path and isinstance(tool_id, str) and tool_id:
            reads[tool_id] = path
    return reads


def _seed_reads_from_result(message: dict, pending_reads: dict[str, str], file_state: dict[str, str]) -> None:
    """Consume the ``tool_result`` blocks answering earlier Reads, recording each file's content as
    the baseline for later edits in this session."""
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
            continue
        path = pending_reads.pop(str(block.get("tool_use_id") or ""), "")
        if not path:
            continue
        body = block.get("content")
        if isinstance(body, list):  # some results arrive as a list of text blocks
            body = "".join(b.get("text") or "" for b in body if isinstance(b, dict) and b.get("type") == "text")
        if isinstance(body, str):
            seed_file_state(file_state, path, content_from_read_output(body))


def _edits_from_message(message: dict, file_state: dict[str, str]) -> list[FileEdit]:
    """The file edits in an assistant message's ``tool_use`` blocks (Edit / Write /
    MultiEdit), as :class:`FileEdit`s — used by the backtrace exporter to reconstruct
    how the conversation changed files. Non-editing tools (Read, Bash, …) are ignored;
    a tool call that produced no net change contributes nothing. ``file_state`` (per
    session, mutated) tracks each file's current content so every edit's diff is the
    INCREMENTAL change, not the whole file each time it is rewritten."""
    content = message.get("content")
    if not isinstance(content, list):
        return []
    edits: list[FileEdit] = []
    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "tool_use"):
            continue
        name = block.get("name")
        raw_input = block.get("input")
        inp = raw_input if isinstance(raw_input, dict) else {}
        path = inp.get("file_path") or inp.get("filePath") or ""
        if not isinstance(path, str):
            continue
        if name == "Write":
            edit = tracked_edit(file_state, path, write=str(inp.get("content") or ""))
        elif name == "Edit":
            edit = tracked_edit(
                file_state, path, subedits=[(str(inp.get("old_string") or ""), str(inp.get("new_string") or ""))]
            )
        elif name == "MultiEdit":
            edit = tracked_edit(
                file_state,
                path,
                subedits=[
                    (str(sub.get("old_string") or ""), str(sub.get("new_string") or ""))
                    for sub in inp.get("edits") or []
                    if isinstance(sub, dict)
                ],
            )
        else:
            continue
        if edit is not None:
            edits.append(edit)
    return edits


def _attribute_subagent_tokens(
    turns: list[SessionTurn],
    tool_ids_per_turn: list[set[str]],
    subagent_tokens: "dict[str | None, TokenUsage] | None",
    unmatched_subagent_time: int | None = None,
) -> None:
    # Add each sub-agent's token usage to the turn that launched it (matched by Task
    # tool_use id). A sub-agent whose id matches no turn — or that had none recorded (the
    # None key) — is attributed to the turn that was active at ``unmatched_subagent_time``
    # (its file mtime) when that is known, else the latest turn, so its tokens are never
    # dropped. Attributing an id-less sub-agent to the turn it actually ran during (a
    # STABLE choice) — rather than always "the latest turn" — is what keeps the commit
    # watermark able to trim it after it is counted once: otherwise, on each re-parse it
    # would re-attach to the newest turn and be committed (and counted) again (double-count).
    if not subagent_tokens or not turns:
        return
    for tool_id, usage in subagent_tokens.items():
        index: int | None = None
        if tool_id is not None:
            index = next((i for i, ids in enumerate(tool_ids_per_turn) if tool_id in ids), None)
        if index is None and unmatched_subagent_time is not None:
            index = _turn_index_at_time(turns, unmatched_subagent_time)
        if index is None:
            index = len(turns) - 1
        turns[index].tokens.add(usage)


def _turn_index_at_time(turns: list[SessionTurn], when: int) -> int | None:
    # The index of the turn whose recorded span [started_at, ended_at] contains epoch
    # second ``when``; else the latest turn that had already ended by then; else None
    # (no turn carries usable timestamps → caller falls back to the latest turn). This
    # gives an id-less sub-agent a STABLE home turn across re-parses so it is counted once.
    best: int | None = None
    for i, turn in enumerate(turns):
        started = turn.started_at
        ended = turn.ended_at
        if started is not None and started <= when and (ended is None or when <= ended):
            return i
        if ended is not None and ended <= when:
            best = i
    return best


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
    if dangling and not interrupted and turn.get("stop_reason") is None and not turn["final"]:
        # No real assistant row has been processed yet: a just-typed prompt the agent
        # is still thinking about (its first assistant row can lag the user row by many
        # seconds), or a crashed turn whose only "reply" was the skipped filler. The
        # missing-reason default below is for OLD transcripts whose assistant rows
        # carry no stop_reason — those still set a final text. A prompt-only turn must
        # NOT count as complete: the background tracker polls mid-window and would
        # commit a trace that is just the user's message with no agent response.
        in_flight = True
    used = capabilities.collect(
        tool_names=turn.get("tool_names") or [],
        skills=turn.get("skills") or [],
        subagents=turn.get("subagents") or [],
    )
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
        queued_followups=list(turn.get("queued_followups") or []),
        mcp_servers=used.mcp_servers,
        mcp_tools=used.mcp_tools,
        skills=used.skills,
        subagents=used.subagents,
        plugins=used.plugins,
        edits=list(turn.get("edits") or []),
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


def _notification_row_text(row: dict) -> str:
    """The `<task-notification>` text carried by *row*, or ``""`` when it carries none.

    The harness delivers a notification in one of TWO row shapes, and both occur in the
    same transcript: as a ``user`` message once the agent is fed it, and as a
    ``type:"attachment"`` row with ``commandMode == "task-notification"`` when it is
    QUEUED behind a turn already in flight. Reading only the user shape (which is all this
    did originally) missed every notification a task delivered while the agent was busy —
    including, on newer Claude Code, most terminal ones."""
    if row.get("type") == "attachment":
        att = row.get("attachment")
        if not isinstance(att, dict) or att.get("commandMode") != "task-notification":
            return ""
        prompt = att.get("prompt")
        text = prompt.strip() if isinstance(prompt, str) else ""
    else:
        message = _as_dict(row.get("message"))
        content = message.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            text = "".join(
                block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
        else:
            return ""
    return text if text.startswith("<task-notification>") else ""


def _task_notification_kind(row: dict) -> str | None:
    """``"completed"``/``"update"`` for a harness `<task-notification>` row, else None.

    The harness injects these rows when a task the agent backgrounded reports back. Two
    shapes exist: a TERMINAL notification (the task completed or failed) carries a
    ``<status>…</status>`` tag, while an INTERMEDIATE monitor update (a Monitor streaming
    events from a still-running job) carries an ``<event>`` payload and no status. Neither
    is a prompt, but the agent usually ACTS on them — so parse_rows opens a fresh turn for
    that work instead of merging it into the prior, already-committed turn; the kind decides
    the turn's synthetic prompt label (and thereby the commit engine's defer-vs-commit
    choice for monitor ticks)."""
    text = _notification_row_text(row)
    if not text:
        return None
    # Only an explicit ``<event>`` payload marks an intermediate monitor tick; anything
    # else (a ``<status>`` completion/failure, or an unknown shape from another harness
    # version) is treated as terminal — the conservative default, since deferring a
    # commit is the riskier misclassification.
    return "update" if "<event>" in text else "completed"


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


def _slash_command_directive(row: dict) -> str | None:
    """``"/cmd <args>"`` when a slash-command row carries a user INSTRUCTION, else None.

    The arguments are the interesting part: ``/goal <what to achieve>``,
    ``/loop <what to repeat>``, ``/some-skill <what to do>``. The command names the mode and
    the args say what was asked, so both are kept.

    None when there are no arguments at all (``/clear``), or when they are a single bare token
    (``/model sonnet``, ``/goal clear``) — a parameter or a control word, not an instruction.
    See the note by :data:`_INSTRUCTION_WORDS` for why prose is the whole test.
    """
    name = _slash_command_name(row)
    if name is None:
        return None
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
    match = _COMMAND_ARGS_RE.search(text)
    args = match.group(1).strip() if match else ""
    if len(args.split()) < _INSTRUCTION_WORDS:
        return None
    return f"{name} {args}"


# A skill invoked as `/<skill>` is HOT-LOADED: Claude Code injects the skill's own text as a meta
# user row and never calls the `Skill` tool, so `_collect_capabilities` (which reads tool_use
# blocks) sees nothing at all. That injected body opens with the skill's install directory, and
# that line is the only machine-readable marker the invocation leaves — without reading it, every
# skill the user runs from the command line is missing from the commit's provenance.
_SKILL_BASE_DIR_RE = re.compile(r"^Base directory for this skill:\s*(\S.*?)\s*$", re.MULTILINE)


def _remember_skills(roster: list[str], names) -> None:
    """Add *names* to a skill roster in place, first-seen order, no duplicates."""
    for name in names or ():
        if name and name not in roster:
            roster.append(name)


def _hot_loaded_skill(row: dict) -> str | None:
    """The name of a skill hot-loaded by a `/<skill>` slash command, or None.

    Taken from the last path segment of the announced base directory
    (``~/.claude/skills/<name>``), which is the skill's own directory name.
    """
    text = _command_expansion_text(row)
    if not text:
        return None
    match = _SKILL_BASE_DIR_RE.search(text)
    if match is None:
        return None
    name = match.group(1).replace("\\", "/").rstrip("/").rpartition("/")[2]
    return name or None


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
