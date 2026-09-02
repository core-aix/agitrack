"""Deliver aGiTrack's session-start hooks to Codex, the way ``claude_settings`` does for Claude.

WHAT CHANGED. Codex used to have no channel for any of this: its only prompt input was
``experimental_instructions_file`` (which REPLACES its base instructions instead of appending)
and ``AGENTS.md`` (a TRACKED file in the user's repo), so background mode could not tell a Codex
agent that aGiTrack commits for it. Codex 0.147 added real lifecycle hooks, and they are wire-
compatible with Claude Code's: the same ``SessionStart`` input on stdin (``cwd``, ``source``,
``session_id``, ``transcript_path``) and the same ``{"hookSpecificOutput": {"hookEventName":
"SessionStart", "additionalContext": ...}}`` output — verified live, the context arrives as a
``developer`` message in the session's rollout. So both hooks aGiTrack installs for Claude work
here unchanged, and the only per-backend part is WHERE they are written.

WHERE. ``<repo>/.codex/hooks.json``, the project layer's hook config. ``.codex/`` is in
``git/repo.py``'s ``_NEVER_STAGE_PREFIXES``, so nothing written here can reach a commit.

TWO THINGS ARE LOAD-BEARING, both measured against 0.147 rather than assumed:

* **The project layer is only read for a repository Codex TRUSTS**, and trust means a persisted
  ``[projects."<path>"] trust_level = "trusted"`` in the user's ``config.toml`` — the same entry
  Codex writes when the user answers its "do you trust this folder?" prompt. Until then the file
  is ignored in silence. See :func:`project_is_trusted`.
* **A newly written hook does not run until the user reviews it.** Codex holds hook trust per
  hook identity (``[hooks.state]`` in the user's config) and skips untrusted hooks with no
  warning at all in ``codex exec``; the user trusts them from the TUI's ``/hooks`` screen.
  aGiTrack does NOT write that trust itself — forging the user's answer to a security prompt is
  not aGiTrack's to make — so it says what is needed instead (see :func:`trust_reminder`).

Everything is best-effort: an unreadable or non-JSON ``hooks.json`` belongs to the user and is
left strictly alone, minus the hooks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agitrack.backends.claude_settings import (
    AGENT_AUTOSTART_FLAG,
    HOOK_FLAG,
    hook_command,
)

# Codex names its events in PascalCase, exactly as Claude Code does, and nests them under a
# top-level "hooks" object beside an optional "description" — the two fields its parser accepts
# (`unknown field 'session_start', expected 'description' or 'hooks'`, its own error message).
# Each event holds a list of matcher groups, each with a list of {type, command} hooks: the same
# shape as `.claude/settings.local.json`, which is why the entries themselves are built by the
# shared `hook_command`.
SESSION_START_EVENT = "SessionStart"
SESSION_NOTE_HOOK = (SESSION_START_EVENT, HOOK_FLAG)
AGENT_AUTOSTART_HOOK = (SESSION_START_EVENT, AGENT_AUTOSTART_FLAG)

HOOKS_RELPATH = Path(".codex") / "hooks.json"
_DESCRIPTION = "aGiTrack session hooks (managed by aGiTrack; removed when it stops)."


def _load(path: Path) -> dict[str, Any] | None:
    """The hooks file as a dict — None when it exists but is not usable.

    A MISSING file is ours to create; an unparsable one belongs to the user and must be left
    alone rather than overwritten, exactly as in ``claude_settings``."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _events(data: dict[str, Any]) -> dict[str, Any]:
    hooks = data.get("hooks")
    return hooks if isinstance(hooks, dict) else {}


def _entries(data: dict[str, Any], event: str) -> list:
    entries = _events(data).get(event)
    return entries if isinstance(entries, list) else []


def _is_ours(entry: Any, flag: str) -> bool:
    if not isinstance(entry, dict):
        return False
    return any(isinstance(hook, dict) and flag in str(hook.get("command", "")) for hook in entry.get("hooks") or [])


def _write(path: Path, data: dict[str, Any], *, debug=None) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        if debug:
            debug(f"could not write codex hooks at {path}: {error!r}")
        return False
    return True


def install_hook(repo: Path, hook: tuple[str, str], *, debug=None) -> bool:
    """Register one of aGiTrack's hooks in ``<repo>/.codex/hooks.json``.

    Idempotent: an existing aGiTrack entry for this event/flag is refreshed in place (the command
    carries an absolute path, which reinstalling aGiTrack can move) and every other entry — the
    user's own, and aGiTrack's other hook — is preserved."""
    event, flag = hook
    path = Path(repo) / HOOKS_RELPATH
    data = _load(path)
    if data is None:
        if debug:
            debug(f"codex hooks at {path} are not usable JSON; leaving them alone")
        return False
    entry = {"hooks": [{"type": "command", "command": hook_command(flag)}]}
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return False
    data.setdefault("description", _DESCRIPTION)
    kept = [item for item in _entries(data, event) if not _is_ours(item, flag)]
    hooks[event] = [*kept, entry]
    return _write(path, data, debug=debug)


def remove_hook(repo: Path, hook: tuple[str, str], *, debug=None) -> bool:
    """Take one of our hooks back out, leaving anything of the user's. Returns True when
    something was removed.

    Empty containers are pruned, and a file that held nothing but aGiTrack's hooks is deleted
    outright (with its directory, when that was ours alone) — stopping the tracker should leave
    no trace in the user's repo. A file that git TRACKS is emptied rather than deleted, for the
    same reason as in ``claude_settings``: a deletion of a file in the user's history is a worse
    trace to leave than an empty object."""
    event, flag = hook
    path = Path(repo) / HOOKS_RELPATH
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
        if data.get("description") == _DESCRIPTION:
            data.pop("description", None)  # ours too; do not leave it behind alone
    if data:
        return _write(path, data, debug=debug)
    try:
        if _is_tracked_by_git(path):
            path.write_text("{}\n", encoding="utf-8")
            return True
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass  # the user has other .codex/ content; leave it
    except OSError as error:
        if debug:
            debug(f"could not update codex hooks at {path}: {error!r}")
        return False
    return True


def _is_tracked_by_git(path: Path) -> bool:
    """Whether git has this file committed. Any failure answers "tracked": the cautious
    direction is to leave a file in place rather than remove one that mattered."""
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


def hook_is_installed(repo: Path, hook: tuple[str, str] = AGENT_AUTOSTART_HOOK) -> bool:
    event, flag = hook
    data = _load(Path(repo) / HOOKS_RELPATH)
    if not data:
        return False
    return any(_is_ours(entry, flag) for entry in _entries(data, event))


def install_commit_guidance_hook(repo: Path, *, debug=None) -> bool:
    return install_hook(repo, SESSION_NOTE_HOOK, debug=debug)


def remove_commit_guidance_hook(repo: Path, *, debug=None) -> bool:
    return remove_hook(repo, SESSION_NOTE_HOOK, debug=debug)


def install_agent_autostart_hook(repo: Path, *, debug=None) -> bool:
    return install_hook(repo, AGENT_AUTOSTART_HOOK, debug=debug)


def remove_agent_autostart_hook(repo: Path, *, debug=None) -> bool:
    return remove_hook(repo, AGENT_AUTOSTART_HOOK, debug=debug)


def project_is_trusted(repo: Path) -> bool:
    """Whether Codex will read this repository's project config layer at all.

    Codex loads ``.codex/hooks.json`` only for a repo the user has TRUSTED, and trust lives as a
    persisted ``[projects."<path>"] trust_level = "trusted"`` entry in ``$CODEX_HOME/config.toml``
    — measured: with the same file in place, a session in an untrusted directory never ran the
    hook and printed nothing, while the identical run under a persisted trust entry ran it. A
    per-invocation ``-c projects…trust_level`` override does NOT count, so this reads the file.

    Answering False is what lets the caller say "Codex will ignore this until you trust the
    folder" instead of installing a hook that silently does nothing. Any failure answers True:
    a guess must not turn into a scary message about a repo that is in fact fine."""
    import os

    home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    config = home / "config.toml"
    try:
        text = config.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return True
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        return True
    try:
        data = tomllib.loads(text)
    except ValueError:
        return True
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return False
    resolved = os.path.realpath(Path(repo))
    for key, value in projects.items():
        if not isinstance(value, dict) or value.get("trust_level") != "trusted":
            continue
        try:
            if os.path.realpath(key) == resolved:
                return True
        except OSError:  # pragma: no cover - a config path that cannot be resolved
            continue
    return False


def codex_is_installed() -> bool:
    """Whether this machine has a Codex CLI at all — the gate on saying anything about it.

    The hooks are installed for every backend, so a repo tracked on Claude carries Codex's hook
    too, and its trust reminder used to print for a user who has never installed Codex: a chore
    about a prompt they will never see, in a tool they do not have. Best-effort by design — if
    the lookup itself fails, answering True keeps the reminder for a user who may well need it
    rather than silently dropping the one step aGiTrack cannot take for them."""
    try:
        from agitrack.backends.setup import backend_installed

        return bool(backend_installed("codex"))
    except Exception:
        return True


def trust_reminder(repo: Path) -> str | None:
    """The one thing the user has to do themselves, or None when there is nothing to say.

    Codex requires every new or changed hook to be REVIEWED before it runs, and skips untrusted
    ones silently — no warning, no output, the hook simply never fires. aGiTrack will not write
    that trust on the user's behalf: it is a security decision about running a command, and the
    whole point of the review is that a person makes it. So it says what to do once, and the
    message names the specific thing that is missing rather than reciting both possibilities.

    Two conditions gate it, and both are about not handing someone a chore that is not theirs:
    the hook has to be on disk (:func:`hook_is_installed`), and Codex has to be installed at all
    (:func:`codex_is_installed`). Even then the reminder opens with "If you use the Codex
    backend" — an installed Codex is not a used one, and a Claude user should be able to stop
    reading at the first clause."""
    if not hook_is_installed(repo, AGENT_AUTOSTART_HOOK):
        return None
    if not codex_is_installed():
        return None
    if not project_is_trusted(repo):
        return (
            "If you use the Codex backend: Codex reads a project's hooks only for a folder it "
            "trusts, and this one is not trusted yet — answer 'yes' to Codex's trust prompt for "
            "this folder, then run `/hooks` in Codex and trust aGiTrack's two session hooks. "
            "Until then, tracking still starts on your next commit."
        )
    return (
        "If you use the Codex backend: Codex runs a new hook only after you review it — run "
        "`/hooks` in Codex once and trust aGiTrack's two session hooks. Until then, tracking "
        "still starts on your next commit."
    )
