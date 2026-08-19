"""The Claude Code SessionStart hook that carries aGiTrack's commit guidance in -b mode.

Background mode never spawns the agent, so the ``--append-system-prompt`` the proxy uses is
not available: the note travels through ``.claude/settings.local.json`` instead. These tests
are about not damaging a file that belongs to the user — every one of them starts from
settings that already have content worth keeping.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

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
    assert specific["additionalContext"].startswith(agent_system_note(use_worktrees=False))
    # The note background mode needs is the NO-WORKTREE one: -b always runs on the current
    # branch, and the worktree variant would send the agent looking for a directory that
    # does not exist.
    assert "no separate worktree" in specific["additionalContext"]
    assert "Do NOT create git commits yourself" in specific["additionalContext"]


def test_every_injection_of_the_note_is_a_text_claude_has_not_seen_before(tmp_path, monkeypatch, capsys):
    """Claude Code DROPS a SessionStart injection whose text is already in the conversation.

    Measured on 2.1.235: the hook fires on `/resume` and prints, and the resumed session
    records no injection at all when it already carries an identical note — while the same
    hook printing into a session without it records the note in full. So a switched-to
    conversation kept only the copy from its own first start, which in a long or
    compaction-forked session is far behind the live context, and the agent went back to
    committing its own work. Two injections must therefore never be byte-identical.
    """
    monkeypatch.setattr("agitrack.proxy.background.background_tracker_is_running", lambda repo: True)
    repo = _repo(tmp_path)
    stamps = iter(
        [
            datetime(2026, 8, 19, 9, 30, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 19, 13, 4, 10, tzinfo=timezone.utc),
        ]
    )
    stamped = claude_settings.issued_at_clause
    monkeypatch.setattr(claude_settings, "issued_at_clause", lambda: stamped(next(stamps)))

    claude_settings.print_session_note(cwd=repo)
    claude_settings.print_session_note(cwd=repo)

    lines = capsys.readouterr().out.splitlines()
    first, second = [json.loads(line)["hookSpecificOutput"]["additionalContext"] for line in lines]
    assert first != second
    # ...and the stamp is the ONLY difference: the guidance itself must not drift between
    # injections, or the agent is told something new every time it resumes.
    assert first.split(" (aGiTrack tracker confirmed")[0] == second.split(" (aGiTrack tracker confirmed")[0]
    assert "2026-08-19T13:04:10Z" in second


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


def test_removal_gives_an_UNTRACKED_settings_file_its_own_bytes_back(tmp_path):
    """The half `git checkout` cannot reach.

    ``_restore_committed_bytes`` restores a file git has a commit of, which is the case that
    shows up in `git status`. But `.local` says most repos ignore this file, and aGiTrack is
    just as much a guest in one git will never restore — there its reformatting simply stays
    forever. The install-time snapshot covers those, and this is a directory with no git at all.
    """
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = '{"permissions": {"allow": ["Write", "Edit"], "defaultMode": "acceptEdits"}}\n'
    path.write_text(original, encoding="utf-8")

    claude_settings.install_autostart_hook(tmp_path)
    assert path.read_text(encoding="utf-8") != original  # the hook really did go in
    claude_settings.remove_autostart_hook(tmp_path)

    assert path.read_text(encoding="utf-8") == original
    assert not (tmp_path / claude_settings.ORIGINAL_RELPATH).exists()


def test_the_bytes_come_back_only_after_the_LAST_hook_is_removed(tmp_path):
    """The two hooks have different lifetimes; the daemon takes the session note away on
    teardown while the auto-start hook stays. Restoring the original then would delete a
    hook that is supposed to outlive the daemon."""
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = '{"permissions": {"allow": ["Write"]}}\n'
    path.write_text(original, encoding="utf-8")
    claude_settings.install_autostart_hook(tmp_path)
    claude_settings.install_commit_guidance_hook(tmp_path)

    claude_settings.remove_commit_guidance_hook(tmp_path)

    assert claude_settings.hook_is_installed(tmp_path, claude_settings.AUTOSTART_HOOK) is True
    assert path.read_text(encoding="utf-8") != original

    claude_settings.remove_autostart_hook(tmp_path)

    assert path.read_text(encoding="utf-8") == original


def test_an_edit_the_user_made_meanwhile_wins_over_the_snapshot(tmp_path):
    """The snapshot is how the file LOOKED when aGiTrack arrived, not a licence to roll the
    user back. A setting added while the tracker ran must survive stopping it."""
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"permissions": {"allow": ["Write"]}}\n', encoding="utf-8")
    claude_settings.install_autostart_hook(tmp_path)

    data = json.loads(path.read_text(encoding="utf-8"))
    data["permissions"]["allow"].append("Edit")
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    claude_settings.remove_autostart_hook(tmp_path)

    assert json.loads(path.read_text(encoding="utf-8")) == {"permissions": {"allow": ["Write", "Edit"]}}


def test_the_snapshot_lives_in_the_self_ignoring_state_dir(tmp_path):
    """It is aGiTrack's own working state, so it must land in `.agitrack/` — which carries a
    `.gitignore` of `*` — and never as a stray file in the user's tree."""
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")

    claude_settings.install_autostart_hook(tmp_path)

    assert (tmp_path / claude_settings.ORIGINAL_RELPATH).exists()
    assert (tmp_path / ".agitrack" / ".gitignore").read_text(encoding="utf-8").endswith("*\n")


def test_a_settings_file_agitrack_created_itself_leaves_no_snapshot(tmp_path):
    """Nothing to restore, so nothing to remember — and the file is deleted outright."""
    claude_settings.install_autostart_hook(tmp_path)

    assert not (tmp_path / claude_settings.ORIGINAL_RELPATH).exists()

    claude_settings.remove_autostart_hook(tmp_path)

    assert not _settings(tmp_path).exists()


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


def test_removing_our_hook_is_clean_under_crlf_conversion(tmp_path):
    """The same guarantee on a repo whose line endings git converts — the default on Git for
    Windows (`core.autocrlf`), where LF is stored and CRLF is checked out.

    Restoring the COMMITTED BYTES is not the same as restoring the FILE. Writing the blob
    verbatim leaves a working tree whose content hashes identically to HEAD — `git diff` empty,
    `git hash-object` matching — while `git status` still reports ` M`, because those are not the
    bytes a checkout would produce and the index's stat entry never got refreshed. Exactly the
    "diff the user never made" this is supposed to prevent, one layer down. Only reproduced on
    Windows, where autocrlf is on by default; this pins it everywhere by setting it explicitly."""
    import subprocess

    from agitrack.backends import claude_settings

    repo = tmp_path / "proj"
    (repo / ".claude").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "core.autocrlf", "true"], cwd=repo, check=True)
    settings = repo / ".claude" / "settings.local.json"
    # CRLF in the working tree, as a Windows editor writes it; git stores LF.
    original = '{\r\n  "permissions": { "allow": ["Bash(ls:*)"] }\r\n}\r\n'
    settings.write_bytes(original.encode("utf-8"))
    subprocess.run(["git", "add", "-f", ".claude/settings.local.json"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "team settings"], cwd=repo, check=True)

    assert claude_settings.install_autostart_hook(repo)
    assert claude_settings.remove_autostart_hook(repo)

    porcelain = subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert porcelain == "", f"left a diff the user never made: {porcelain!r}"
    # And the file really is back to what a checkout produces, not merely hash-equal.
    assert subprocess.run(["git", "update-index", "--refresh"], cwd=repo, capture_output=True).returncode == 0


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
