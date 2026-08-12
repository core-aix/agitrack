"""Deliver aGiTrack's commit guidance to Claude Code when aGiTrack does not launch it.

WHY THIS EXISTS. In proxy (TUI) mode aGiTrack spawns the agent itself and appends the note
to its system prompt with ``--append-system-prompt`` (see ``proxy_agents.agent_system_note``),
so the agent knows aGiTrack is committing for it and does not commit on its own. In
background mode (``-b``) there is no spawn to attach a flag to: the user drives the agent
from its own CLI, an IDE extension, or anything else, and aGiTrack only watches the
transcript. The agent then behaves as it does everywhere else and commits its own work,
which is the duplicate-commit problem this module removes.

Claude Code is the only backend where this can be done without touching the user's source
tree. It reads ``.claude/settings.local.json`` — the per-machine, not-committed settings
file — and its ``SessionStart`` hook injects the hook command's output into the session's
context. ``.claude/`` is in ``git/repo.py``'s ``_NEVER_STAGE_PREFIXES``, so nothing aGiTrack
writes there can end up in a commit.

The other two backends have no equivalent, which is why this module is Claude-specific:

* **Codex** exposes only ``experimental_instructions_file``, which REPLACES the base
  instructions rather than appending, so using it would delete Codex's own coding prompt.
  Its other channel is ``AGENTS.md`` — a TRACKED file in the user's repo.
* **OpenCode**'s CLI has no system-prompt flag at all (verified against ``opencode --help``
  and ``opencode run --help``); ``--agent`` selects a whole replacement agent, and its
  config lives in a repo-root ``opencode.json`` or the user's global config — again either
  a tracked file or a setting that would leak into sessions aGiTrack is not running.

Everything here is best-effort: a repo whose settings file is unreadable, unwritable or
not JSON keeps working exactly as before, minus the note.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# The flag whose presence identifies OUR hook entry. Matching on the command string rather
# than on a marker key of our own keeps the entry to the shape Claude Code documents — an
# unknown key risks being rejected by its settings validation, which would take the user's
# whole settings file down with it.
HOOK_FLAG = "--claude-session-note"

SETTINGS_RELPATH = Path(".claude") / "settings.local.json"


def hook_command() -> str:
    """The shell command Claude Code runs at session start to obtain the note.

    Built from ``agitrack_invocation()`` — the same self-reference the git hooks use — rather
    than the bare name ``agitrack``: the hook runs in whatever environment the user's editor
    or shell happens to have, which is not necessarily one where aGiTrack's scripts directory
    is on PATH, and the interpreter path keeps working across a self-update.

    EVERY PART IS QUOTED, including parts with no spaces, and that is not cosmetic. Claude
    Code runs hook commands through a POSIX-style shell, which on Windows eats the
    backslashes in ``C:\\Users\\...\\python.exe`` as escape characters: the command silently
    becomes ``C:Usersdev...`` and does not exist. The hook then fails with no message anyone
    sees and the note never arrives — measured, not theorised (an unquoted path produced "NO"
    from a live session where the quoted one produced "YES"). Inside double quotes a
    backslash is literal, so quoting is what makes a Windows path survive the shell."""
    from agitrack.proc import agitrack_invocation

    return " ".join(f'"{part}"' for part in [*agitrack_invocation(), HOOK_FLAG])


def session_note_payload(note: str) -> str:
    """What the hook prints: Claude Code's documented SessionStart envelope.

    ``additionalContext`` is the explicit channel for "add this to the session's context",
    rather than relying on bare stdout being adopted."""
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": note}},
        ensure_ascii=False,
    )


def _load(path: Path) -> dict[str, Any] | None:
    """The settings file as a dict — None when it exists but is not usable.

    The distinction matters: a MISSING file is ours to create, whereas an unparsable one
    belongs to the user and must be left strictly alone. Overwriting it would destroy
    permissions, MCP servers and hooks that have nothing to do with aGiTrack."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _entries(data: dict[str, Any]) -> list:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    entries = hooks.get("SessionStart")
    return entries if isinstance(entries, list) else []


def _is_ours(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    return any(
        isinstance(hook, dict) and HOOK_FLAG in str(hook.get("command", "")) for hook in entry.get("hooks") or []
    )


def install_commit_guidance_hook(repo: Path, *, debug=None) -> bool:
    """Register the SessionStart hook in ``<repo>/.claude/settings.local.json``.

    Idempotent: an existing aGiTrack entry is refreshed in place (the command carries an
    absolute path, which a reinstall of aGiTrack can move), and everything else in the file
    is preserved byte-for-byte in meaning. Returns True when the file ends up carrying the
    hook."""
    path = Path(repo) / SETTINGS_RELPATH
    data = _load(path)
    if data is None:
        if debug:
            debug(f"claude settings at {path} are not usable JSON; leaving them alone")
        return False
    entry = {"hooks": [{"type": "command", "command": hook_command()}]}
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return False
    kept = [item for item in _entries(data) if not _is_ours(item)]
    hooks["SessionStart"] = [*kept, entry]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        if debug:
            debug(f"could not write claude settings at {path}: {error!r}")
        return False
    return True


def remove_commit_guidance_hook(repo: Path, *, debug=None) -> bool:
    """Take our hook back out, leaving any settings the user has of their own.

    Empty containers are pruned so stopping the tracker leaves no trace: a lingering
    ``"hooks": {"SessionStart": []}`` in a file aGiTrack created would be litter in the
    user's repo. Returns True when something was removed."""
    path = Path(repo) / SETTINGS_RELPATH
    data = _load(path)
    if not data:
        return False
    entries = _entries(data)
    kept = [item for item in entries if not _is_ours(item)]
    if len(kept) == len(entries):
        return False
    hooks = data["hooks"]
    if kept:
        hooks["SessionStart"] = kept
    else:
        hooks.pop("SessionStart", None)
    if not hooks:
        data.pop("hooks", None)
    try:
        if data:
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        elif _is_tracked_by_git(path):
            # Emptied but COMMITTED: some repos do track this file. Deleting it would show up
            # as a staged-able deletion of the user's own file, which is a far worse trace to
            # leave than an empty object.
            path.write_text("{}\n", encoding="utf-8")
        else:
            # The file existed only to carry our hook: remove it, and the directory too when
            # it was ours alone. Claude Code recreates either on demand.
            path.unlink()
            try:
                path.parent.rmdir()
            except OSError:
                pass  # the user has other .claude/ content; leave it
    except OSError as error:
        if debug:
            debug(f"could not update claude settings at {path}: {error!r}")
        return False
    return True


def _is_tracked_by_git(path: Path) -> bool:
    """Whether git has this file committed. Most repos git-ignore ``settings.local.json``
    (that is what the ``.local`` is for), but some track it, and aGiTrack must not delete a
    file the user's history contains. Any failure answers "tracked": the cautious direction
    is to leave a file in place rather than remove one that mattered."""
    import subprocess

    from agitrack.proc import console_isolation_kwargs

    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", path.name],
            cwd=path.parent,
            capture_output=True,
            **console_isolation_kwargs(),
        )
    except OSError:
        return True
    return result.returncode == 0


def hook_is_installed(repo: Path) -> bool:
    data = _load(Path(repo) / SETTINGS_RELPATH)
    if not data:
        return False
    return any(_is_ours(entry) for entry in _entries(data))


def print_session_note(cwd: Path | None = None) -> int:
    """Entry point of the hook itself (``agitrack --claude-session-note``).

    SILENT WHEN NO TRACKER IS RUNNING, and that is the important part. The note asserts that
    aGiTrack is committing the agent's work, which is only true while a background tracker is
    watching this repo. A hook outlives its daemon in every case aGiTrack does not control —
    a killed process (Windows ``-b stop`` terminates rather than signals, so teardown never
    runs), a crash, a reboot — and a note that lies in those cases is worse than no note: the
    agent stops committing and nothing else starts. Printing nothing is a no-op for Claude
    Code, so a stale hook simply does nothing until a tracker returns.

    Always exits 0. A hook that fails is noise in the user's session about a feature they did
    not ask for."""
    from agitrack.backends.proxy_agents import agent_system_note

    try:
        from agitrack.git import GitRepo
        from agitrack.proxy.background import background_tracker_is_running

        if not background_tracker_is_running(GitRepo.discover(Path(cwd) if cwd else Path.cwd())):
            return 0
    except Exception:
        return 0
    # Always the no-worktree note: background mode runs on the current branch, and the
    # worktree variant would send the agent looking for a directory that does not exist.
    print(session_note_payload(agent_system_note(use_worktrees=False)))
    return 0
