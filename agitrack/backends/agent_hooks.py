"""The one place that knows which session hooks each backend gets, and how to take them back.

Background mode installs up to two per-backend hooks into the repository, both fired by the
backend itself when a session opens:

* **the commit-guidance note** — tells the agent aGiTrack is committing for it, so it does not
  commit its own work. Lifetime: only while a tracker runs, so the daemon removes it on
  teardown. Claude Code and Codex both take it (their ``SessionStart`` protocols are wire-
  compatible); OpenCode has no channel that does not rewrite the user's own message, so it
  does not (see ``opencode_settings``).
* **the start-on-session-open hook** — starts the tracker when an agent opens a session in the
  repo, instead of waiting for a turn to end with changes or for a commit. Lifetime: PERSISTENT,
  because running when aGiTrack is not is its entire job. All three backends take it.

Routing lives here rather than at each call site because the answer is per backend, changes as
the backends gain features (Codex could carry neither before 0.147), and is needed from four
places: the daemon's start-up and teardown, ``agitrack stop`` / ``--remove-hooks``, and the
interactive TUI, which displaces the persistent hook while it owns the repository.

REMOVAL IS NEVER BACKEND-SCOPED. A repository's backend changes — the same repo can be driven
by Claude today and Codex tomorrow — so a stop that only cleaned up the CURRENT backend would
leave the previous one's hook armed to start a tracker the user just asked to stop. Every
removal therefore sweeps all three. Installation stays scoped to the backend actually in use:
writing hooks for backends the user does not run would litter their repo with config for tools
that are not there.
"""

from __future__ import annotations

from pathlib import Path

from agitrack.backends import claude_settings, codex_settings, opencode_settings

# The backends that can carry the commit-guidance note, and the module that installs it.
_GUIDANCE_BACKENDS = {"claude": claude_settings, "codex": codex_settings}
# The backends that can start the tracker when a session opens.
_AUTOSTART_BACKENDS = {"claude": claude_settings, "codex": codex_settings, "opencode": opencode_settings}


def install_commit_guidance(repo: Path, backend: str | None, *, debug=None) -> bool:
    module = _GUIDANCE_BACKENDS.get(backend or "")
    if module is None:
        return False
    return bool(module.install_commit_guidance_hook(repo, debug=debug))


def remove_commit_guidance(repo: Path, *, debug=None) -> bool:
    removed = False
    for module in _GUIDANCE_BACKENDS.values():
        try:
            removed |= bool(module.remove_commit_guidance_hook(repo, debug=debug))
        except Exception:  # best-effort: one backend's failure must not strand the others
            pass
    return removed


def install_agent_autostart(repo: Path, backend: str | None, *, debug=None) -> bool:
    module = _AUTOSTART_BACKENDS.get(backend or "")
    if module is None:
        return False
    return bool(module.install_agent_autostart_hook(repo, debug=debug))


def remove_agent_autostart(repo: Path, *, debug=None) -> bool:
    removed = False
    for module in _AUTOSTART_BACKENDS.values():
        try:
            removed |= bool(module.remove_agent_autostart_hook(repo, debug=debug))
        except Exception:
            pass
    return removed


def installed_autostart_backends(repo: Path) -> list[str]:
    """Which backends' start-on-session-open hooks are armed for this repo right now.

    The interactive TUI asks BEFORE displacing them and restores exactly this list on exit, so a
    session started in a repo that never had the hook does not come back to one that does:
    "restore what was there" has to mean exactly that, and "which backends" is part of what was
    there."""
    armed: list[str] = []
    try:
        if claude_settings.hook_is_installed(repo, claude_settings.AGENT_AUTOSTART_HOOK):
            armed.append("claude")
        if codex_settings.hook_is_installed(repo, codex_settings.AGENT_AUTOSTART_HOOK):
            armed.append("codex")
        if opencode_settings.hook_is_installed(repo):
            armed.append("opencode")
    except Exception:
        return armed
    return armed


def startup_notice(repo: Path, backend: str | None) -> str | None:
    """Anything the user must do themselves for these hooks to work, or None.

    Only Codex has such a step (it requires a person to review a new hook before it runs), and
    only when its hook is actually installed — see ``codex_settings.trust_reminder``."""
    if backend != "codex":
        return None
    try:
        return codex_settings.trust_reminder(repo)
    except Exception:
        return None
