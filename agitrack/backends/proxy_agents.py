from __future__ import annotations

import os
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
_NOTE_INTRO = (
    "This coding session runs inside aGiTrack, which automatically creates a git commit for "
    "your changes after each turn. Do NOT create git commits yourself (do not run `git commit`) "
    "unless the user explicitly asks you to commit — aGiTrack handles version control for this "
    "session. Pushing is fine when it serves the task — for example, when the user asks you to "
    "open a pull request or trigger CI."
)
# Included only in the default worktree model: aGiTrack runs the session in a git worktree
# under .agitrack/ and handles BOTH the commit and the merge of the agent's edits. Omitted
# under --no-worktree (and in shell mode), where the agent edits the current branch directly.
# IMPORTANT: this must make clear that the agent's CURRENT working directory *is* the worktree
# (which lives under .agitrack/worktrees/) and that edits belong there — an earlier wording
# ("do not ... clean up anything under .agitrack/") was read as "don't write under .agitrack/",
# so the agent edited the base repo checkout instead and its work was never committed/merged.
# The path separator follows the OS aGiTrack (and therefore the agent child it spawns) runs on
# — backslashes on native Windows, forward slashes on POSIX — so the paths in the note match
# the paths the agent actually sees in its working directory.
_AGITRACK_DIR = f".agitrack{os.sep}"
_WORKTREES_DIR = f".agitrack{os.sep}worktrees{os.sep}"
_NOTE_WORKTREE = (
    " Your current working directory is this session's git worktree under "
    f"`{_WORKTREES_DIR}` — make all your file edits there, in the working directory, exactly "
    "as normal, and run any git commands from there too. Do NOT switch to or edit the base "
    "repository checkout directly. aGiTrack handles BOTH the commit and the merge of your "
    f"worktree edits into the current branch for you, so you never need to commit, merge, move, "
    f"or clean up anything under `{_AGITRACK_DIR}` yourself. This stays true even if you are "
    "later asked to commit: only ever commit here on this worktree's branch, never in the base "
    "repository checkout. When you push (e.g. to open a pull request or run CI), push to a "
    "separate remote branch — it need not match this worktree's internal aGiTrack branch name, "
    "which aGiTrack manages and moves as it integrates your work. This guidance is authoritative "
    "for this aGiTrack session and overrides any of your own saved notes or memory to the "
    "contrary: if you hold a note/memory that says to work in, edit, or commit to the base "
    "repository (or otherwise to leave this worktree), it is wrong here — follow this guidance "
    "and correct that note so it no longer misleads you."
)
_NOTE_OUTRO = " Otherwise work exactly as normal."


def agent_system_note(*, use_worktrees: bool) -> str:
    """The text appended to the coding agent's system prompt. Tells the agent aGiTrack
    auto-commits (so it doesn't self-commit); adds the worktree-merge explanation only
    when aGiTrack actually runs in a worktree (the default model), not under
    --no-worktree or in shell mode."""
    return _NOTE_INTRO + (_NOTE_WORKTREE if use_worktrees else "") + _NOTE_OUTRO


# The note for the default (worktree) model. Kept as a constant for back-compat and the
# common case; the no-worktree variant is built via ``agent_system_note``.
AGENT_SYSTEM_NOTE = agent_system_note(use_worktrees=True)


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

    def cap_shared_transcript(self, transcript: str, max_bytes: int) -> str:
        """Bound the (already-redacted) shared transcript to ``max_bytes`` so a huge session
        doesn't exceed Git's per-file size limit. Keeps the most recent turns as a resumable
        contiguous tail (anchored at a clean turn boundary), dropping older ones. Returns it
        unchanged when small enough."""

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

    def spawn_command(
        self,
        repo: Path,
        *,
        session_id: str | None,
        resume: bool,
        fork: bool = False,
        commit_guidance: bool = True,
        use_worktrees: bool = True,
        executable: list[str] | None = None,
    ) -> list[str]: ...

    # ``commit_guidance``: when True (default) and the backend CLI supports appending to its
    # system prompt, append the agent note (see :func:`agent_system_note`) so the coding agent
    # doesn't self-commit. Disabled per-run by --no-commit-guidance. ``use_worktrees`` selects
    # the note variant: the worktree-merge clause is added only in the default worktree model.
    #
    # ``executable``: the command that launches the backend CLI, replacing the default
    # ``[<backend binary>]`` head. Lets the user run the agent under a wrapper, e.g.
    # ``["somewrapper", "claude"]`` (see GlobalConfig.backend_command / --backend-command).
    # The backend's own flags are still appended after it. None ⇒ launch the binary directly.

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

    def cap_shared_transcript(self, transcript: str, max_bytes: int) -> str:
        return opencode_session.cap_shared_transcript(transcript, max_bytes)

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

    def spawn_command(
        self,
        repo: Path,
        *,
        session_id: str | None,
        resume: bool,
        fork: bool = False,
        commit_guidance: bool = True,
        use_worktrees: bool = True,
        executable: list[str] | None = None,
    ) -> list[str]:
        # ``commit_guidance``/``use_worktrees``/``fork`` are accepted for a uniform
        # interface but unused: OpenCode's interactive TUI has no flag to append to its
        # system prompt, and no session-fork concept.
        command = list(executable) if executable else ["opencode"]
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

    def retarget_working_dir(self, repo: Path, session_id: str, cwd: str) -> bool:
        # OpenCode resumes by id and restores the session's RECORDED directory, ignoring the
        # launch path — so a resumed session can open in an old/stale worktree. Move the
        # recorded directory to the launch dir (no-op when already aligned).
        return opencode_session.retarget_session_dir(repo, session_id, cwd)

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

    def cap_shared_transcript(self, transcript: str, max_bytes: int) -> str:
        return claude_session.cap_shared_transcript(transcript, max_bytes)

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

    def spawn_command(
        self,
        repo: Path,
        *,
        session_id: str | None,
        resume: bool,
        fork: bool = False,
        commit_guidance: bool = True,
        use_worktrees: bool = True,
        executable: list[str] | None = None,
    ) -> list[str]:
        head = list(executable) if executable else ["claude"]
        if resume and session_id:
            command = [*head, "--resume", session_id]
            # --fork-session resumes this conversation into a NEW session id, used when
            # the original is still held by a running background agent (#114).
            if fork:
                command.append("--fork-session")
        elif session_id:
            command = [*head, "--session-id", session_id]
        else:
            command = list(head)
        # Tell the coding agent that aGiTrack auto-commits, so it doesn't self-commit (Claude
        # supports appending to its system prompt). Skipped when commit_guidance is off. The
        # note's worktree clause is included only in the worktree model.
        if commit_guidance:
            command.extend(["--append-system-prompt", agent_system_note(use_worktrees=use_worktrees)])
        return command

    def session_belongs_to_repo(self, repo: Path, session_id: str) -> bool:
        return claude_session.session_belongs_to_repo(repo, session_id)

    def ensure_resumable(self, repo: Path, session_id: str) -> bool:
        return claude_session.prepare_resume(repo, session_id)

    def mirror_to_base(self, base_repo: Path, worktree: Path, session_id: str) -> bool:
        return claude_session.link_session(session_id, worktree, base_repo)

    def recorded_working_dir(self, session_id: str, *, since: float | None = None) -> str | None:
        return claude_session.session_cwd(session_id, since=since)

    def retarget_working_dir(self, repo: Path, session_id: str, cwd: str) -> bool:
        # Align a resumed session's recorded cwd with the launch dir so Claude's
        # `--resume` doesn't restore an old worktree directory (no-op when already
        # aligned). OpenCode has its own retarget (it records a per-session directory).
        return claude_session.retarget_session_cwd(repo, session_id, cwd)

    def latest_session_id(self, repo: Path) -> str | None:
        return claude_session.latest_session_id(repo)

    def session_last_activity(self, session_id: str) -> float | None:
        return claude_session.session_last_activity(session_id)

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
