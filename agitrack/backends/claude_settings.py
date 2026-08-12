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

# The flags whose presence identifies OUR hook entries. Matching on the command string rather
# than on a marker key of our own keeps each entry to the shape Claude Code documents — an
# unknown key risks being rejected by its settings validation, which would take the user's
# whole settings file down with it.
HOOK_FLAG = "--claude-session-note"
AUTOSTART_FLAG = "--autostart-on-change"

# The two hooks, each as (event, flag). They have DIFFERENT lifetimes, which is the whole
# reason they are separate entries rather than one:
#   SessionStart/--claude-session-note  — the commit-guidance note. True only while a tracker
#       is running, so the daemon removes it on teardown.
#   Stop/--autostart-on-change          — starts the tracker when a turn leaves changes behind.
#       Must OUTLIVE the daemon (that is its whole job), so it is persistent and only
#       `-b stop` / `--remove-hooks` take it away.
SESSION_NOTE_HOOK = ("SessionStart", HOOK_FLAG)
AUTOSTART_HOOK = ("Stop", AUTOSTART_FLAG)

SETTINGS_RELPATH = Path(".claude") / "settings.local.json"


def hook_command(flag: str = HOOK_FLAG) -> str:
    """The shell command Claude Code runs for one of aGiTrack's hooks.

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

    return " ".join(f'"{part}"' for part in [*agitrack_invocation(), flag])


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


def _entries(data: dict[str, Any], event: str) -> list:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    entries = hooks.get(event)
    return entries if isinstance(entries, list) else []


def _is_ours(entry: Any, flag: str) -> bool:
    if not isinstance(entry, dict):
        return False
    return any(isinstance(hook, dict) and flag in str(hook.get("command", "")) for hook in entry.get("hooks") or [])


def install_hook(repo: Path, hook: tuple[str, str], *, debug=None) -> bool:
    """Register one of aGiTrack's hooks in ``<repo>/.claude/settings.local.json``.

    Idempotent: an existing aGiTrack entry for this event is refreshed in place (the command
    carries an absolute path, which a reinstall of aGiTrack can move), and everything else in
    the file is preserved byte-for-byte in meaning. Returns True when the file ends up
    carrying the hook."""
    event, flag = hook
    path = Path(repo) / SETTINGS_RELPATH
    data = _load(path)
    if data is None:
        if debug:
            debug(f"claude settings at {path} are not usable JSON; leaving them alone")
        return False
    entry = {"hooks": [{"type": "command", "command": hook_command(flag)}]}
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return False
    kept = [item for item in _entries(data, event) if not _is_ours(item, flag)]
    hooks[event] = [*kept, entry]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        if debug:
            debug(f"could not write claude settings at {path}: {error!r}")
        return False
    return True


def remove_hook(repo: Path, hook: tuple[str, str], *, debug=None) -> bool:
    """Take one of our hooks back out, leaving any settings the user has of their own.

    Empty containers are pruned so stopping the tracker leaves no trace: a lingering
    ``"hooks": {"SessionStart": []}`` in a file aGiTrack created would be litter in the
    user's repo. Returns True when something was removed."""
    event, flag = hook
    path = Path(repo) / SETTINGS_RELPATH
    data = _load(path)
    if not data:
        return False
    entries = _entries(data, event)
    kept = [item for item in entries if not _is_ours(item, flag)]
    if len(kept) == len(entries):
        return False
    hooks = data["hooks"]
    if kept:
        hooks[event] = kept
    else:
        hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    try:
        if data:
            # Put the user's OWN FORMATTING back when what remains is their file again.
            #
            # aGiTrack rewrites this file with its own canonical `json.dumps(indent=2)`. On a repo
            # that TRACKS settings.local.json that reformatting is a real, permanent diff: after
            # `-b stop` removed every aGiTrack entry, `git status` still showed
            # ` M .claude/settings.local.json` — the file re-indented and never restored — so the
            # user was left holding a change they never made, forever. Measured on this exact
            # case: bytes differ, JSON identical.
            if not _restore_committed_bytes(path, data):
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


def _restore_committed_bytes(path: Path, data: dict) -> bool:
    """Put ``path`` back to its COMMITTED content when ``data`` is semantically identical to it.
    Returns True when that happened.

    This is what makes hook removal a true no-op on a repo that tracks the file: aGiTrack's own
    formatting is undone, not merely its entries. Deliberately conservative — the file is
    restored ONLY when the remaining JSON parses equal to the committed JSON, so a user edit made
    since that commit is never reverted (in that case the caller falls back to writing our own
    formatting, which is the best we can do).

    ``git checkout`` does the writing, rather than us writing the blob's bytes ourselves, because
    the blob is not what belongs in the working tree. Under ``core.autocrlf`` / ``core.eol`` /
    an ``eol=`` attribute — the default on Git for Windows — git stores LF and checks out CRLF,
    so writing the blob verbatim leaves a file whose CONTENT hashes identically to HEAD (``git
    diff`` is empty) while ``git status`` still reports ` M`, because the bytes are not the ones
    a checkout would produce. That is the very "a diff the user never made" this function exists
    to prevent, just moved one layer down. Letting git write it applies whatever conversion this
    repo is configured for, on every platform. The byte-writing fallback is kept for the case
    where checkout itself fails."""
    committed = _committed_text(path)
    if committed is None:
        return False
    try:
        if json.loads(committed) != data:
            return False
    except (json.JSONDecodeError, ValueError):
        return False
    if not _git_checkout_file(path):
        path.write_text(committed, encoding="utf-8", newline="")
    return True


def _git_checkout_file(path: Path) -> bool:
    """``git checkout HEAD -- <path>``: restore the file exactly as a checkout would write it.
    Safe here only because the caller has already proved the remaining JSON equals the committed
    JSON, so there is nothing of the user's left to discard."""
    import subprocess

    from agitrack.proc import UTF8_TEXT, console_isolation_kwargs

    try:
        result = subprocess.run(
            ["git", "checkout", "HEAD", "--", f"./{path.name}"],
            cwd=path.parent,
            capture_output=True,
            **UTF8_TEXT,
            **console_isolation_kwargs(),
        )
    except OSError:
        return False
    return result.returncode == 0


def _committed_text(path: Path) -> str | None:
    """``git show HEAD:<path>`` for this file, or None when it is untracked/unreadable."""
    import subprocess

    from agitrack.proc import UTF8_TEXT, console_isolation_kwargs

    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:./{path.name}"],
            cwd=path.parent,
            capture_output=True,
            **UTF8_TEXT,
            **console_isolation_kwargs(),
        )
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


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


def hook_is_installed(repo: Path, hook: tuple[str, str] = SESSION_NOTE_HOOK) -> bool:
    event, flag = hook
    data = _load(Path(repo) / SETTINGS_RELPATH)
    if not data:
        return False
    return any(_is_ours(entry, flag) for entry in _entries(data, event))


def install_commit_guidance_hook(repo: Path, *, debug=None) -> bool:
    return install_hook(repo, SESSION_NOTE_HOOK, debug=debug)


def remove_commit_guidance_hook(repo: Path, *, debug=None) -> bool:
    return remove_hook(repo, SESSION_NOTE_HOOK, debug=debug)


def install_autostart_hook(repo: Path, *, debug=None) -> bool:
    """The Stop hook that starts the tracker once a turn has left changes behind.

    PERSISTENT, unlike the guidance hook: its entire purpose is to run when aGiTrack is not.
    Only `-b stop` and `--remove-hooks` take it away."""
    return install_hook(repo, AUTOSTART_HOOK, debug=debug)


def remove_autostart_hook(repo: Path, *, debug=None) -> bool:
    return remove_hook(repo, AUTOSTART_HOOK, debug=debug)


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
