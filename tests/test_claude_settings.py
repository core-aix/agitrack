"""The Claude Code SessionStart hook that carries aGiTrack's commit guidance in -b mode.

Background mode never spawns the agent, so the ``--append-system-prompt`` the proxy uses is
not available: the note travels through ``.claude/settings.local.json`` instead. These tests
are about not damaging a file that belongs to the user — every one of them starts from
settings that already have content worth keeping.
"""

from __future__ import annotations

import json

from agitrack.backends import claude_settings


def _settings(repo):
    return repo / ".claude" / "settings.local.json"


def _write(repo, data):
    path = _settings(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_install_creates_the_hook_in_a_repo_with_no_claude_settings(tmp_path):
    assert claude_settings.install_commit_guidance_hook(tmp_path) is True

    data = json.loads(_settings(tmp_path).read_text(encoding="utf-8"))
    (entry,) = data["hooks"]["SessionStart"]
    assert entry["hooks"][0]["type"] == "command"
    assert claude_settings.HOOK_FLAG in entry["hooks"][0]["command"]
    assert claude_settings.hook_is_installed(tmp_path) is True


def test_install_keeps_every_other_setting_and_hook(tmp_path):
    # The file is the user's: permissions, MCP servers and their own hooks all live here,
    # and an aGiTrack that flattened it would be doing real damage to their setup.
    mine = {"hooks": [{"type": "command", "command": "make lint"}]}
    _write(tmp_path, {"permissions": {"allow": ["Bash(git *)"]}, "hooks": {"SessionStart": [mine], "Stop": [mine]}})

    claude_settings.install_commit_guidance_hook(tmp_path)

    data = json.loads(_settings(tmp_path).read_text(encoding="utf-8"))
    assert data["permissions"] == {"allow": ["Bash(git *)"]}
    assert data["hooks"]["Stop"] == [mine]
    assert data["hooks"]["SessionStart"][0] == mine  # theirs first, ours appended
    assert len(data["hooks"]["SessionStart"]) == 2


def test_install_is_idempotent_and_refreshes_a_stale_command(tmp_path):
    # The command carries an absolute interpreter path, which a reinstall or a venv change
    # moves. Repeated installs must update it in place rather than stack up copies.
    _write(
        tmp_path,
        {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": f"/old/python {claude_settings.HOOK_FLAG}"}]}
                ]
            }
        },
    )

    claude_settings.install_commit_guidance_hook(tmp_path)
    claude_settings.install_commit_guidance_hook(tmp_path)

    entries = json.loads(_settings(tmp_path).read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    assert len(entries) == 1
    assert entries[0]["hooks"][0]["command"] == claude_settings.hook_command()


def test_remove_takes_ours_out_and_leaves_theirs(tmp_path):
    mine = {"hooks": [{"type": "command", "command": "make lint"}]}
    _write(tmp_path, {"hooks": {"SessionStart": [mine]}})
    claude_settings.install_commit_guidance_hook(tmp_path)

    assert claude_settings.remove_commit_guidance_hook(tmp_path) is True

    data = json.loads(_settings(tmp_path).read_text(encoding="utf-8"))
    assert data["hooks"]["SessionStart"] == [mine]
    assert claude_settings.hook_is_installed(tmp_path) is False


def test_remove_leaves_no_litter_in_a_repo_that_had_no_claude_dir(tmp_path):
    # aGiTrack created both the file and the directory, so stopping the tracker must leave
    # the repo exactly as it found it — not an empty settings file the user now has to
    # wonder about (and, in a repo that tracks .claude/, would have to decide about).
    claude_settings.install_commit_guidance_hook(tmp_path)

    claude_settings.remove_commit_guidance_hook(tmp_path)

    assert not _settings(tmp_path).exists()
    assert not (tmp_path / ".claude").exists()


def test_a_settings_file_that_is_not_json_is_never_touched(tmp_path):
    # Half-edited JSON is a normal state for a file a human is editing. Rewriting it would
    # destroy work in progress; refusing to install costs only the note.
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"permissions": {"allow": [', encoding="utf-8")

    assert claude_settings.install_commit_guidance_hook(tmp_path) is False
    assert claude_settings.remove_commit_guidance_hook(tmp_path) is False
    assert path.read_text(encoding="utf-8") == '{"permissions": {"allow": ['


def test_remove_reports_nothing_to_do_when_the_hook_was_never_installed(tmp_path):
    assert claude_settings.remove_commit_guidance_hook(tmp_path) is False


def test_every_part_of_the_hook_command_is_quoted(monkeypatch):
    """A Windows path must survive the shell Claude Code runs hooks through.

    That shell is POSIX-style, so an unquoted ``C:\\Users\\dev\\...\\python.exe`` loses its
    backslashes to escape processing and becomes a command that does not exist. The hook then
    fails silently and the note never arrives — which is exactly what a live session showed
    before this: unquoted answered "NO", quoted answered "YES". Quoting parts only when they
    contain spaces is therefore not enough, and this is the test that says so.
    """
    monkeypatch.setattr(
        "agitrack.proc.agitrack_invocation",
        lambda: [r"C:\Users\dev\agitrack\.venv\Scripts\python.exe", "-m", "agitrack"],
    )

    command = claude_settings.hook_command()

    assert command == (r'"C:\Users\dev\agitrack\.venv\Scripts\python.exe" "-m" "agitrack" "--claude-session-note"')


def _repo(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def test_the_hook_says_nothing_while_no_tracker_is_running(tmp_path, monkeypatch, capsys):
    """A hook outlives its daemon whenever aGiTrack does not control the exit — a crash, a
    reboot, or a Windows `-b stop`, which terminates the process rather than signalling it.
    The note claims aGiTrack is committing the agent's work; with no tracker that is false,
    and a false note is worse than none: the agent stops committing and nothing else starts.
    """
    monkeypatch.setattr("agitrack.proxy.background.background_tracker_is_running", lambda repo: False)

    assert claude_settings.print_session_note(cwd=_repo(tmp_path)) == 0

    assert capsys.readouterr().out == ""


def test_a_directory_that_is_not_a_repo_is_not_an_error(tmp_path, capsys):
    # Claude Code runs this on EVERY session start, including in directories aGiTrack has
    # never heard of. A traceback there would be noise about a feature the user never asked
    # for, so it exits quietly.
    assert claude_settings.print_session_note(cwd=tmp_path) == 0
    assert capsys.readouterr().out == ""


def test_the_hook_prints_the_note_in_the_documented_envelope(tmp_path, monkeypatch, capsys):
    from agitrack.backends.proxy_agents import agent_system_note

    monkeypatch.setattr("agitrack.proxy.background.background_tracker_is_running", lambda repo: True)

    assert claude_settings.print_session_note(cwd=_repo(tmp_path)) == 0

    payload = json.loads(capsys.readouterr().out)
    specific = payload["hookSpecificOutput"]
    assert specific["hookEventName"] == "SessionStart"
    assert specific["additionalContext"] == agent_system_note(use_worktrees=False)
    # The note background mode needs is the NO-WORKTREE one: -b always runs on the current
    # branch, and the worktree variant would send the agent looking for a directory that
    # does not exist.
    assert "no separate worktree" in specific["additionalContext"]
    assert "Do NOT create git commits yourself" in specific["additionalContext"]


def test_a_settings_file_the_repo_TRACKS_is_never_deleted(tmp_path):
    """Most repos git-ignore settings.local.json — that is what the `.local` is for — but
    some commit it. Emptying our hook out of a tracked file must not delete it: that would
    turn stopping the tracker into a staged-able deletion of a file in the user's history.
    """
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    path = _write(tmp_path, {})
    subprocess.run(["git", "add", "-f", str(path)], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "keep my claude settings"], cwd=tmp_path, check=True)
    claude_settings.install_commit_guidance_hook(tmp_path)

    claude_settings.remove_commit_guidance_hook(tmp_path)

    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == {}
    # Still in the index — the point of the test. (Its bytes can differ from the committed
    # ones by our formatting; what must never happen is a DELETION showing up in git status.)
    assert (
        "D"
        not in subprocess.run(
            ["git", "status", "--porcelain", "--", str(path)], cwd=tmp_path, capture_output=True, text=True
        ).stdout
    )


def test_the_two_hooks_are_independent(tmp_path):
    """They have different LIFETIMES, which is why they are separate entries.

    The session note is true only while a tracker runs, so the daemon takes it away on
    teardown. The auto-start hook exists to run when aGiTrack is NOT running, so it has to
    survive that same teardown — removing one must never take the other with it.
    """
    claude_settings.install_commit_guidance_hook(tmp_path)
    claude_settings.install_autostart_hook(tmp_path)

    claude_settings.remove_commit_guidance_hook(tmp_path)

    assert claude_settings.hook_is_installed(tmp_path, claude_settings.SESSION_NOTE_HOOK) is False
    assert claude_settings.hook_is_installed(tmp_path, claude_settings.AUTOSTART_HOOK) is True
    data = json.loads(_settings(tmp_path).read_text(encoding="utf-8"))
    assert "SessionStart" not in data["hooks"] and data["hooks"]["Stop"]


def test_the_autostart_hook_command_is_quoted_too(monkeypatch):
    monkeypatch.setattr("agitrack.proc.agitrack_invocation", lambda: [r"C:\Users\dev\python.exe", "-m", "agitrack"])

    command = claude_settings.hook_command(claude_settings.AUTOSTART_FLAG)

    assert command == r'"C:\Users\dev\python.exe" "-m" "agitrack" "--autostart-on-change"'


def test_removing_our_hook_restores_the_users_own_formatting(tmp_path):
    """D3 (second half): aGiTrack rewrites this file with its own canonical
    `json.dumps(indent=2)`. On a repo that TRACKS settings.local.json that reformatting is a
    real, permanent diff — after `-b stop` removed every aGiTrack entry, `git status` still
    showed ` M .claude/settings.local.json`, the file re-indented and never restored, so the user
    was left holding a change they never made. Measured live: bytes differ, JSON identical."""
    import subprocess

    from agitrack.backends import claude_settings

    repo = tmp_path / "proj"
    (repo / ".claude").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    settings = repo / ".claude" / "settings.local.json"
    # The user's own formatting: compact, nested on one line — nothing json.dumps would produce.
    original = '{\n  "permissions": { "allow": ["Bash(ls:*)"] },\n  "hooks": { "Stop": [ { "hooks": [] } ] }\n}\n'
    settings.write_text(original, encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".claude/settings.local.json"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "team settings"], cwd=repo, check=True)

    assert claude_settings.install_autostart_hook(repo)
    assert settings.read_text(encoding="utf-8") != original  # ours is in there now
    assert claude_settings.remove_autostart_hook(repo)

    assert settings.read_text(encoding="utf-8") == original
    porcelain = subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert porcelain == "", f"left a diff the user never made: {porcelain!r}"


def test_a_user_edit_since_the_commit_is_never_reverted(tmp_path):
    """The restore is deliberately conservative: committed bytes go back ONLY when what remains
    parses equal to the committed JSON. An edit the user made since that commit must survive."""
    import json
    import subprocess

    from agitrack.backends import claude_settings

    repo = tmp_path / "proj"
    (repo / ".claude").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    settings = repo / ".claude" / "settings.local.json"
    settings.write_text('{"permissions": {"allow": []}}\n', encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".claude/settings.local.json"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "settings"], cwd=repo, check=True)

    claude_settings.install_autostart_hook(repo)
    # The user adds a permission of their own AFTER the commit.
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["permissions"]["allow"].append("Bash(git:*)")
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    claude_settings.remove_autostart_hook(repo)

    kept = json.loads(settings.read_text(encoding="utf-8"))
    assert kept["permissions"]["allow"] == ["Bash(git:*)"]  # their edit survived the removal
