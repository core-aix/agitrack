from __future__ import annotations

import uuid
from pathlib import Path
from typing import Protocol

from agitrack.transcripts import claude as claude_session, opencode as opencode_session
from agitrack.transcripts import ExportedSession, SessionRef


# Appended to the coding agent's own system prompt when the backend CLI supports it (e.g.
# Claude's --append-system-prompt). aGiTrack already creates a git commit for each turn, so
# an agent committing on its own duplicates work and breaks the per-turn token/line
# accounting the commits carry. The note keeps the agent's normal behaviour but stops it
# from self-committing — unless the user explicitly asks it to.
AGENT_SYSTEM_NOTE = (
    "This coding session runs inside aGiTrack, which automatically creates a git commit for "
    "your changes after each turn. Do NOT create git commits yourself (do not run `git commit`) "
    "unless the user explicitly asks you to commit — aGiTrack handles version control for this "
    "session. Otherwise work exactly as normal."
)


class ProxyAgent(Protocol):
    """A coding-agent CLI driven through aGiTrack's proxy (native TUI) mode.

    Each backend launches its own interactive TUI and records its transcript in
    a backend-specific place; these methods give the proxy a uniform way to
    spawn the CLI and recover the turns/responses/tokens aGiTrack commits.
    """

    name: str
    # Whether this backend has a portable transcript that can be shared and
    # resumed across machines (issue #55). Both Claude (per-session .jsonl) and
    # OpenCode (export/import CLI) do.
    supports_session_sharing: bool

    def export_session_raw(self, repo: Path, session_id: str) -> str | None:
        """The full transcript text to share, or None if unavailable/unsupported."""

    def transcript_size(self, repo: Path, session_id: str) -> int | None:
        """Byte size of the session transcript (a cheap stat), or None — for a fast
        'is the shared copy current?' check without reading the whole file."""

    def has_local_session(self, repo: Path, session_id: str) -> bool:
        """Whether ``repo`` already holds this session locally (so resuming it would
        keep the local copy unless explicitly overwritten)."""

    def import_shared_session(
        self, repo: Path, session_id: str, transcript: str, *, overwrite: bool = False, as_id: str | None = None
    ) -> bool:
        """Install a shared transcript so the session can be resumed in ``repo``.
        With ``overwrite`` it replaces an existing local copy (pull-latest). With
        ``as_id`` it installs the conversation under a NEW id (the "keep both" path
        for an id that already exists locally). Returns True on success; False if
        unsupported."""

    def new_session_id(self) -> str | None:
        """A session id to start a fresh session with, or None to let the
        backend choose one that aGiTrack will discover afterwards."""

    def new_import_id(self) -> str | None:
        """A fresh id to re-import a shared conversation under, so it can live
        alongside an existing local copy of the same id ("keep both"). None when
        the backend can't re-id an imported session."""

    def spawn_command(self, repo: Path, *, session_id: str | None, resume: bool) -> list[str]: ...

    def session_belongs_to_repo(self, repo: Path, session_id: str) -> bool: ...

    def ensure_resumable(self, repo: Path, session_id: str) -> bool:
        """Make sure spawning the resume command in ``repo`` (as cwd) will find
        this conversation, staging its transcript there if the backend stores
        transcripts per directory. Returns True if it can be resumed."""

    def mirror_to_base(self, base_repo: Path, worktree: Path, session_id: str) -> bool:
        """Make a conversation running in ``worktree`` also visible/continuable
        from ``base_repo`` (e.g. a plain CLI run in the repo root). Returns True
        if mirrored. No-op for backends without per-directory transcript files."""

    def recorded_working_dir(self, session_id: str, *, since: float | None = None) -> str | None:
        """The working directory the backend most recently recorded for a session,
        or None if it doesn't record one. Used to detect a resume that drifted the
        cwd away from the worktree it was launched in. ``since`` (an epoch
        timestamp) restricts the answer to turns recorded at or after the current
        launch, so a stale pre-launch cwd isn't mistaken for drift."""

    def latest_session_id(self, repo: Path) -> str | None: ...

    def list_sessions(self, repo: Path) -> list[SessionRef]:
        """Every session recorded for this repository, for listing/switching."""

    def list_worktree_sessions(self, worktrees_root: Path) -> list[tuple[str, SessionRef]]:
        """Every conversation recorded under an aGiTrack worktree of this repo, paired
        with the worktree directory name (the session's name). Includes ones whose
        worktree has since been removed, so named sessions stay resumable."""

    def export_session(self, repo: Path, session_id: str) -> ExportedSession | None: ...

    def is_event_blob(self, content: str) -> bool:
        """Whether a trace entry is a raw backend event dump that should be
        dropped rather than committed."""


class OpenCodeProxyAgent:
    name = "opencode"
    # OpenCode's `export`/`import` CLI serialises a whole session to JSON (id
    # preserved, directory retargeted to the import cwd), so its sessions are
    # portable and shareable like Claude's (issue #55).
    supports_session_sharing = True

    def export_session_raw(self, repo: Path, session_id: str) -> str | None:
        return opencode_session.export_session_raw(repo, session_id)

    def transcript_size(self, repo: Path, session_id: str) -> int | None:
        return opencode_session.session_transcript_size(repo, session_id)

    def has_local_session(self, repo: Path, session_id: str) -> bool:
        return opencode_session.has_imported_session(repo, session_id)

    def import_shared_session(
        self, repo: Path, session_id: str, transcript: str, *, overwrite: bool = False, as_id: str | None = None
    ) -> bool:
        return opencode_session.import_shared_session(repo, session_id, transcript, overwrite=overwrite, as_id=as_id)

    def new_session_id(self) -> str | None:
        # OpenCode assigns its own session id; aGiTrack discovers it after the run.
        return None

    def new_import_id(self) -> str | None:
        # OpenCode ids are "ses_"-prefixed tokens; mint one so a shared session can
        # be re-imported alongside an existing local copy ("keep both").
        return "ses_" + uuid.uuid4().hex

    def spawn_command(self, repo: Path, *, session_id: str | None, resume: bool) -> list[str]:
        command = ["opencode"]
        if resume and session_id:
            command.extend(["--session", session_id])
        command.append(str(repo))
        return command

    def session_belongs_to_repo(self, repo: Path, session_id: str) -> bool:
        return opencode_session.session_belongs_to_repo(repo, session_id)

    def ensure_resumable(self, repo: Path, session_id: str) -> bool:
        # OpenCode resumes by id from its own global store, regardless of cwd.
        return bool(session_id)

    def mirror_to_base(self, base_repo: Path, worktree: Path, session_id: str) -> bool:
        # OpenCode keeps sessions in a global store keyed by id (resumable from
        # anywhere); there's no per-directory transcript file to link.
        return False

    def recorded_working_dir(self, session_id: str, *, since: float | None = None) -> str | None:
        return None  # not tracked for OpenCode

    def latest_session_id(self, repo: Path) -> str | None:
        return opencode_session.latest_session_id(repo)

    def list_sessions(self, repo: Path) -> list[SessionRef]:
        return opencode_session.list_sessions(repo)

    def list_worktree_sessions(self, worktrees_root: Path) -> list[tuple[str, SessionRef]]:
        return opencode_session.list_worktree_sessions(worktrees_root)

    def export_session(self, repo: Path, session_id: str) -> ExportedSession | None:
        return opencode_session.export_session(repo, session_id)

    def is_event_blob(self, content: str) -> bool:
        return opencode_session.looks_like_event_blob(content)


class ClaudeProxyAgent:
    name = "claude"
    supports_session_sharing = True

    def export_session_raw(self, repo: Path, session_id: str) -> str | None:
        return claude_session.export_session_raw(repo, session_id)

    def transcript_size(self, repo: Path, session_id: str) -> int | None:
        return claude_session.session_transcript_size(repo, session_id)

    def has_local_session(self, repo: Path, session_id: str) -> bool:
        return claude_session.has_imported_session(repo, session_id)

    def import_shared_session(
        self, repo: Path, session_id: str, transcript: str, *, overwrite: bool = False, as_id: str | None = None
    ) -> bool:
        return claude_session.import_shared_session(repo, session_id, transcript, overwrite=overwrite, as_id=as_id)

    def new_session_id(self) -> str | None:
        # Claude accepts an explicit session id, so aGiTrack picks one up front and
        # knows exactly which transcript to read.
        return str(uuid.uuid4())

    def new_import_id(self) -> str | None:
        # A fresh uuid to re-import a shared conversation under ("keep both").
        return str(uuid.uuid4())

    def spawn_command(self, repo: Path, *, session_id: str | None, resume: bool) -> list[str]:
        if resume and session_id:
            command = ["claude", "--resume", session_id]
        elif session_id:
            command = ["claude", "--session-id", session_id]
        else:
            command = ["claude"]
        # Tell the coding agent that aGiTrack auto-commits, so it doesn't self-commit
        # (Claude supports appending to its system prompt; OpenCode's TUI has no such flag).
        command.extend(["--append-system-prompt", AGENT_SYSTEM_NOTE])
        return command

    def session_belongs_to_repo(self, repo: Path, session_id: str) -> bool:
        return claude_session.session_belongs_to_repo(repo, session_id)

    def ensure_resumable(self, repo: Path, session_id: str) -> bool:
        return claude_session.prepare_resume(repo, session_id)

    def mirror_to_base(self, base_repo: Path, worktree: Path, session_id: str) -> bool:
        return claude_session.link_session(session_id, worktree, base_repo)

    def recorded_working_dir(self, session_id: str, *, since: float | None = None) -> str | None:
        return claude_session.session_cwd(session_id, since=since)

    def latest_session_id(self, repo: Path) -> str | None:
        return claude_session.latest_session_id(repo)

    def list_sessions(self, repo: Path) -> list[SessionRef]:
        return claude_session.list_sessions(repo)

    def list_worktree_sessions(self, worktrees_root: Path) -> list[tuple[str, SessionRef]]:
        return claude_session.list_worktree_sessions(worktrees_root)

    def export_session(self, repo: Path, session_id: str) -> ExportedSession | None:
        return claude_session.export_session(repo, session_id)

    def is_event_blob(self, content: str) -> bool:
        return False


_AGENTS: dict[str, type] = {
    OpenCodeProxyAgent.name: OpenCodeProxyAgent,
    ClaudeProxyAgent.name: ClaudeProxyAgent,
}


def available_backends() -> list[str]:
    return sorted(_AGENTS)


def make_proxy_agent(name: str) -> ProxyAgent:
    # Raise on an unknown backend rather than silently substituting one: a stale
    # or mistyped backend name must surface, not quietly launch the wrong agent.
    try:
        agent_class = _AGENTS[name]
    except KeyError:
        raise ValueError(f"Unknown backend {name!r}; choose one of {', '.join(available_backends())}.") from None
    return agent_class()
