"""The session-start hooks background mode installs into a repo, one per backend.

They answer the question the older auto-start triggers could not: a reboot (or any other way a
tracker dies) leaves a repo armed but untracked, and until now the first thing to bring tracking
back was a finished turn that changed something, or a commit — so the FIRST turn after the
restart was always written with nothing watching it. An agent OPENING a session is the earliest
honest signal that work is about to happen here, and all three backends expose it.

These tests are about the two things that make that safe: what is written into the user's repo
(and taken back out), and never starting a daemon beside an aGiTrack that already owns the repo.
"""

from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path

from agitrack.backends import agent_hooks, claude_settings, codex_settings, opencode_settings
from agitrack.config import AgitrackState, GlobalConfig
from agitrack.git import GitRepo


def _init_repo(path: Path) -> GitRepo:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


# --- what lands in the repo, per backend ------------------------------------


def test_claude_gets_a_session_start_hook_beside_the_note(tmp_path):
    """Two SessionStart entries, not one merged entry, because their LIFETIMES differ: the note
    is true only while a tracker runs and goes away with it, while auto-start's whole job is to
    run when aGiTrack is not."""
    claude_settings.install_commit_guidance_hook(tmp_path)
    claude_settings.install_agent_autostart_hook(tmp_path)

    data = json.loads((tmp_path / claude_settings.SETTINGS_RELPATH).read_text(encoding="utf-8"))
    commands = [h["command"] for entry in data["hooks"]["SessionStart"] for h in entry["hooks"]]
    assert any("--autostart-on-agent" in c for c in commands)
    assert any("--claude-session-note" in c for c in commands)

    assert claude_settings.remove_agent_autostart_hook(tmp_path) is True
    data = json.loads((tmp_path / claude_settings.SETTINGS_RELPATH).read_text(encoding="utf-8"))
    remaining = [h["command"] for entry in data["hooks"]["SessionStart"] for h in entry["hooks"]]
    # Removing one must not take the other: they are disarmed by different events.
    assert remaining and all("--autostart-on-agent" not in c for c in remaining)


def test_codex_hooks_use_the_shape_codex_actually_parses(tmp_path):
    """Measured against Codex 0.147, whose own error messages named every part of this: the file
    takes `description` or `hooks`, the event is PascalCase `SessionStart`, and each entry is a
    matcher group holding `{type, command}` hooks — the same shape as Claude Code's."""
    codex_settings.install_agent_autostart_hook(tmp_path)
    codex_settings.install_commit_guidance_hook(tmp_path)

    data = json.loads((tmp_path / codex_settings.HOOKS_RELPATH).read_text(encoding="utf-8"))
    assert set(data) <= {"description", "hooks"}
    entries = data["hooks"]["SessionStart"]
    assert len(entries) == 2
    for entry in entries:
        assert list(entry) == ["hooks"]
        assert entry["hooks"][0]["type"] == "command"


def test_a_users_own_codex_hooks_survive_ours_coming_and_going(tmp_path):
    theirs = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "their-linter"}]}]}}
    (tmp_path / ".codex").mkdir()
    (tmp_path / codex_settings.HOOKS_RELPATH).write_text(json.dumps(theirs), encoding="utf-8")

    codex_settings.install_agent_autostart_hook(tmp_path)
    codex_settings.remove_agent_autostart_hook(tmp_path)

    data = json.loads((tmp_path / codex_settings.HOOKS_RELPATH).read_text(encoding="utf-8"))
    assert data["hooks"]["PreToolUse"] == theirs["hooks"]["PreToolUse"]
    assert "SessionStart" not in data["hooks"]


def test_a_codex_hooks_file_that_was_ours_alone_is_deleted_outright(tmp_path):
    codex_settings.install_agent_autostart_hook(tmp_path)

    codex_settings.remove_agent_autostart_hook(tmp_path)

    # Stopping the tracker leaves no trace: neither the file nor the directory it needed.
    assert not (tmp_path / codex_settings.HOOKS_RELPATH).exists()
    assert not (tmp_path / ".codex").exists()


def test_opencode_gets_a_plugin_and_never_loses_the_users_own(tmp_path):
    opencode_settings.install_agent_autostart_hook(tmp_path)
    plugin = tmp_path / opencode_settings.PLUGIN_RELPATH
    source = plugin.read_text(encoding="utf-8")
    assert opencode_settings.MARKER in source
    assert "--autostart-on-agent" in source
    # The invocation is embedded as JSON so a Windows interpreter path's backslashes stay data
    # instead of becoming escapes in the JavaScript.
    assert "const COMMAND = [" in source

    plugin.write_text("// mine, actually\n", encoding="utf-8")
    assert opencode_settings.remove_agent_autostart_hook(tmp_path) is False
    assert plugin.exists()


# --- the routing, and what a stop has to sweep ------------------------------


def test_removal_sweeps_every_backend_not_just_the_current_one(tmp_path):
    """A repo's backend changes between runs. A stop that only cleaned up the backend in use
    today would leave yesterday's hook armed to start a tracker the user just asked to stop."""
    for backend in ("claude", "codex", "opencode"):
        agent_hooks.install_agent_autostart(tmp_path, backend)
    assert agent_hooks.installed_autostart_backends(tmp_path) == ["claude", "codex", "opencode"]

    assert agent_hooks.remove_agent_autostart(tmp_path) is True

    assert agent_hooks.installed_autostart_backends(tmp_path) == []


def test_opencode_gets_no_commit_guidance_hook(tmp_path):
    """OpenCode's only place to add text to a turn is the user's own message parts, so the note
    would end up inside the prompt aGiTrack then commits as the user's words. Until there is a
    channel that does not rewrite what the user said, it goes without."""
    assert agent_hooks.install_commit_guidance(tmp_path, "opencode") is False
    assert not (tmp_path / ".opencode").exists()
    assert not (tmp_path / ".claude").exists()


def test_codex_now_gets_the_note_it_could_never_have(tmp_path):
    """Codex 0.147's SessionStart hook is wire-compatible with Claude Code's — verified live:
    the additionalContext arrives as a `developer` message in the session's rollout."""
    assert agent_hooks.install_commit_guidance(tmp_path, "codex") is True

    data = json.loads((tmp_path / codex_settings.HOOKS_RELPATH).read_text(encoding="utf-8"))
    commands = [h["command"] for entry in data["hooks"]["SessionStart"] for h in entry["hooks"]]
    assert any("--claude-session-note" in c for c in commands)


# --- starting the tracker ---------------------------------------------------


def test_opening_a_session_starts_the_tracker_and_tells_that_session(tmp_path, monkeypatch, capsys):
    """The two halves are one feature. A tracker started here is invisible to the note hook that
    ran beside it — it checked before the daemon existed — so this session would be the one
    session that never learns aGiTrack is committing for it, and would commit its own work until
    the next restart."""
    from agitrack.proxy import background as background_module

    repo = _init_repo(tmp_path)
    AgitrackState(tmp_path, default_backend="claude").save()
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        background_module,
        "spawn_background_daemon",
        lambda repo, *, extra_args: spawned.append(extra_args) or types.SimpleNamespace(pid=4321),
    )
    monkeypatch.setattr(background_module, "wait_for_handshake", lambda repo, *, pid, timeout: {"pid": pid})

    assert background_module.autostart_on_agent_session(repo) == 0

    assert spawned and "--backend" in spawned[0]
    payload = json.loads(capsys.readouterr().out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Do NOT create git commits yourself" in payload["hookSpecificOutput"]["additionalContext"]


def test_a_session_opening_beside_a_running_agitrack_starts_nothing(tmp_path, monkeypatch, capsys):
    """The rule the whole design rests on: one git-editing aGiTrack per repo. An interactive TUI
    holds the single-writer lock, and this hook fires for the very session that TUI spawned."""
    from agitrack.git import RepoLock
    from agitrack.proxy import background as background_module

    repo = _init_repo(tmp_path)
    AgitrackState(tmp_path, default_backend="claude").save()
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        background_module, "spawn_background_daemon", lambda repo, *, extra_args: spawned.append(extra_args)
    )
    holder = RepoLock(tmp_path / ".agitrack" / "lock")
    assert holder.acquire()
    try:
        background_module.autostart_on_agent_session(repo)
    finally:
        holder.release()

    assert spawned == []
    # And it stays silent: the note asserts a tracker is committing, which here is not true.
    assert capsys.readouterr().out == ""


def test_a_session_opening_respects_the_opt_out(tmp_path, monkeypatch):
    from agitrack.proxy import background as background_module

    repo = _init_repo(tmp_path)
    AgitrackState(tmp_path, default_backend="claude").save()
    config = GlobalConfig()
    config.load_repo_overlay(tmp_path)
    config.set("autotrack_hook", "off", scope="repo")
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        background_module, "spawn_background_daemon", lambda repo, *, extra_args: spawned.append(extra_args)
    )

    background_module.autostart_on_agent_session(repo)

    assert spawned == []


# --- the interactive session's borrow --------------------------------------


def _runner_stub(repo: GitRepo):
    """The two attributes the displace/restore pair touches. Exercising the REAL methods on a
    stub keeps this a test of the shipped code rather than of a re-description of it; building a
    whole ProxyRunner would need a PTY, a backend and a worktree to test six lines."""
    from agitrack.proxy.runner import ProxyRunner

    runner = types.SimpleNamespace(base_repo=repo, _debug=lambda message: None, _displaced_agent_autostart=[])
    return runner, ProxyRunner


def test_an_interactive_session_borrows_the_hooks_and_gives_them_back(tmp_path):
    """Interactive mode spawns the agent ITSELF and is the single writer while it runs, so the
    hook that would start a daemon for that very session has to be out of the way. It must come
    back on exit, though: these hooks outlive aGiTrack by design, and a TUI session must not be
    what silently uninstalls them."""
    repo = _init_repo(tmp_path)
    agent_hooks.install_agent_autostart(tmp_path, "claude")
    agent_hooks.install_agent_autostart(tmp_path, "codex")
    runner, ProxyRunner = _runner_stub(repo)

    ProxyRunner._displace_background_autostart(runner)
    assert agent_hooks.installed_autostart_backends(tmp_path) == []

    ProxyRunner._restore_background_autostart(runner)
    assert agent_hooks.installed_autostart_backends(tmp_path) == ["claude", "codex"]


def test_a_repo_that_never_had_them_does_not_come_back_with_them(tmp_path):
    """ "Restore what was there" has to mean exactly that. Turning auto-start ON for a repo the
    user never armed — because they happened to run an interactive session in it — would be
    aGiTrack deciding a standing preference on their behalf."""
    repo = _init_repo(tmp_path)
    runner, ProxyRunner = _runner_stub(repo)

    ProxyRunner._displace_background_autostart(runner)
    ProxyRunner._restore_background_autostart(runner)

    assert agent_hooks.installed_autostart_backends(tmp_path) == []
    assert not (tmp_path / ".claude").exists()
