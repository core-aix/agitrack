import subprocess

import pytest

from agitrack import cli
from agitrack.git import GitRepo


def _has_git() -> bool:
    return subprocess.run(["git", "--version"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not available")


def test_git_init_seeds_usable_repo(tmp_path):
    (tmp_path / "file.txt").write_text("hello\n", encoding="utf-8")
    repo = GitRepo.init(tmp_path)

    # Valid HEAD (the seed commit) so worktree setup won't choke on an unborn branch.
    assert repo.current_branch() not in ("", "HEAD")
    # The user's pre-existing file is left untracked for aGiTrack's user-commit flow.
    assert "file.txt" in repo.status_short()


def test_git_init_repo_has_born_head(tmp_path):
    repo = GitRepo.init(tmp_path)
    assert repo.has_commits()


def test_ensure_born_seeds_unborn_repo_and_is_idempotent(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    repo = GitRepo.discover(tmp_path)
    assert not repo.has_commits()  # fresh `git init`: unborn HEAD

    assert repo.ensure_born() is True  # seeds an initial commit
    assert repo.has_commits()
    assert repo.current_branch() not in ("", "HEAD")  # worktree-usable HEAD

    assert repo.ensure_born() is False  # already born: no-op


def test_discover_or_init_seeds_empty_initialized_repo(tmp_path, capsys):
    # A user who ran `git init` themselves (unborn HEAD) must start cleanly,
    # leaving their own files untracked for aGiTrack's user-commit flow.
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "existing.txt").write_text("mine\n", encoding="utf-8")

    repo = cli._discover_or_init(tmp_path)

    assert repo is not None
    assert repo.has_commits()
    assert repo.current_branch() not in ("", "HEAD")
    assert "existing.txt" in repo.untracked_files()
    assert "Seeded an initial commit" in capsys.readouterr().out


def test_discover_or_init_returns_existing_repo(tmp_path, monkeypatch):
    GitRepo.init(tmp_path)
    asked = []
    monkeypatch.setattr("builtins.input", lambda *a: asked.append(1) or "n")
    repo = cli._discover_or_init(tmp_path)
    assert repo is not None
    assert asked == []  # an existing repo is never prompted about


def _force_tty(monkeypatch, stdin: bool, stdout: bool = True):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: stdin, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: stdout, raising=False)


def test_discover_or_init_initializes_when_user_agrees(tmp_path, monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    repo = cli._discover_or_init(tmp_path)

    assert repo is not None
    assert repo.current_branch() not in ("", "HEAD")  # initialized + seeded


def test_discover_or_init_stops_when_user_declines(tmp_path, monkeypatch, capsys):
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: "")  # default = no

    repo = cli._discover_or_init(tmp_path)

    assert repo is None  # caller exits; aGiTrack can't run outside a git repo
    assert "cannot run outside a Git repository" in capsys.readouterr().out
    assert not (tmp_path / ".git").exists()  # nothing was created


def test_discover_or_init_non_interactive_does_not_prompt(tmp_path, monkeypatch):
    _force_tty(monkeypatch, stdin=False)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))

    assert cli._discover_or_init(tmp_path) is None


# --- backend passthrough args (#32) -----------------------------------------


def _stub_repo_and_free_lock(monkeypatch):
    """Stub repo discovery to a lightweight object with a ``.repo`` path and the
    single-instance pre-check to "free", so cli.main reaches the launch surface."""
    import pathlib
    from types import SimpleNamespace

    monkeypatch.setattr(cli, "_discover_or_init", lambda p: SimpleNamespace(repo=pathlib.Path("/tmp/agit-test")))

    class _FreeLock:
        def __init__(self, _path):
            pass

        def probe_owner(self):
            return None

    monkeypatch.setattr(cli, "RepoLock", _FreeLock)


def _stub_launch(monkeypatch, *, use_worktrees: bool = True, commit_guidance: bool = True):
    """Stub the launch surface so cli.main only exercises arg routing.
    Returns the dict the fake runner/shell records its kwargs into."""
    captured: dict = {}

    class Fake:
        def __init__(self, repo, **kw):
            captured.update(kw)

        def run(self):
            return 0

    monkeypatch.setattr(cli, "ProxyRunner", Fake)
    monkeypatch.setattr(cli, "AgitrackShell", Fake)
    _stub_repo_and_free_lock(monkeypatch)

    class Config:
        def has_default_backend(self):
            return True

        default_backend = "opencode"

    Config.use_worktrees = use_worktrees
    Config.commit_guidance = commit_guidance
    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())
    return captured


def test_already_running_refused_before_privacy_prompt(monkeypatch, capsys):
    # A second instance must be turned away BEFORE the privacy acknowledgement, so
    # the user isn't asked to acknowledge anything only to be refused.
    import pathlib
    from types import SimpleNamespace

    events: list[str] = []
    monkeypatch.setattr(cli, "_discover_or_init", lambda p: SimpleNamespace(repo=pathlib.Path("/tmp/x")))

    class _HeldLock:
        def __init__(self, _path):
            pass

        def probe_owner(self):
            return 4321  # another instance holds it

    monkeypatch.setattr(cli, "RepoLock", _HeldLock)
    monkeypatch.setattr(cli, "already_running_message", lambda pid: events.append(f"refused:{pid}") or "running")
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: events.append("privacy") or True)

    class Config:
        check_for_updates = False
        use_worktrees = True

        def has_default_backend(self):
            return True

        default_backend = "opencode"

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--backend", "opencode"])

    assert rc == 1
    assert events == ["refused:4321"]  # refused, and the privacy prompt never ran


# --- --no-worktree (#9) -----------------------------------------------------


def test_no_worktree_flag_disables_worktrees(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--no-worktree"])
    assert captured["use_worktrees"] is False


def test_default_uses_config_use_worktrees(monkeypatch):
    captured = _stub_launch(monkeypatch, use_worktrees=False)  # config opt-out, no flag
    cli.main([])
    assert captured["use_worktrees"] is False


# --- --no-sandbox / --allowed-edit-paths ------------------------------------


def test_sandbox_on_by_default(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "opencode"])
    assert captured["sandbox"] is True


def test_no_sandbox_flag_disables_sandbox(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "opencode", "--no-sandbox"])
    assert captured["sandbox"] is False


def test_allowed_edit_paths_flag_splits_on_pathsep(monkeypatch):
    import os

    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "opencode", "--allowed-edit-paths", os.pathsep.join(["/data", "/srv/x"])])
    assert captured["allowed_edit_paths"] == ["/data", "/srv/x"]


def test_allowed_edit_paths_default_empty(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "opencode"])
    assert captured["allowed_edit_paths"] == []


# --- --no-commit-guidance ---------------------------------------------------


def test_commit_guidance_on_by_default(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main([])
    assert captured["commit_guidance"] is True


def test_no_commit_guidance_flag_disables_it(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--no-commit-guidance"])
    assert captured["commit_guidance"] is False


def test_default_uses_config_commit_guidance(monkeypatch):
    captured = _stub_launch(monkeypatch, commit_guidance=False)  # config opt-out, no flag
    cli.main([])
    assert captured["commit_guidance"] is False


def test_delay_merge_flag_passed_to_runner(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--delay-merge"])
    assert captured["delay_merge"] is True


def test_delay_merge_off_by_default(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main([])
    assert captured["delay_merge"] is False


def test_version_flag_prints_version_and_exits(monkeypatch, capsys):
    # `agitrack --version` is cheap and side-effect-free: no repo discovery, no
    # privacy prompt. The VSCode extension reads it to detect a self-updated CLI.
    import agitrack

    called = {"discover": False}
    monkeypatch.setattr(cli, "_discover_or_init", lambda p: called.__setitem__("discover", True))
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == agitrack.__version__
    assert called["discover"] is False  # exits before touching the repo


def test_startup_message_printed_for_interactive_proxy(monkeypatch, capsys):
    # Entering aGiTrack prints immediate feedback so the terminal isn't silent while the
    # TUI comes up — shown however it was launched (terminal or VSCode).
    _stub_launch(monkeypatch)
    cli.main([])
    assert "aGiTrack is starting..." in capsys.readouterr().out


def test_startup_message_suppressed_in_json_mode(monkeypatch, capsys):
    # json/bridge output is machine-readable; the human "starting" line must not leak in.
    _stub_launch(monkeypatch)
    cli.main(["--prompt", ":status"])
    assert "aGiTrack is starting..." not in capsys.readouterr().out


def test_recover_flag_finalizes_and_exits(tmp_path, monkeypatch, capsys):
    # `agitrack --recover` runs headless recovery and exits — no privacy prompt,
    # no TUI, no "starting" line. With no session worktrees there is nothing to do.
    from agitrack.git import GitRepo

    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "cfg"))
    GitRepo.init(tmp_path / "repo")
    rc = cli.main(["--repo", str(tmp_path / "repo"), "--recover"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Nothing to recover." in out
    assert "aGiTrack is starting" not in out  # recovery is not an interactive launch


def test_update_check_runs_under_a_tty(monkeypatch):
    # The startup self-update offer is gated only on a TTY (+ config) — NOT on any
    # editor/environment signal — so it runs inside VSCode's integrated terminal,
    # which is a real PTY, exactly as in a standalone terminal.
    captured = _stub_launch(monkeypatch)
    _force_tty(monkeypatch, stdin=True, stdout=True)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    ran = {"checked": False}
    monkeypatch.setattr(cli, "_check_for_update_at_startup", lambda config: ran.__setitem__("checked", True))

    cli.main([])  # plain interactive proxy launch

    assert ran["checked"] is True
    assert captured  # launch still proceeded


def test_update_check_skipped_without_a_tty(monkeypatch):
    _stub_launch(monkeypatch)
    _force_tty(monkeypatch, stdin=False, stdout=False)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    ran = {"checked": False}
    monkeypatch.setattr(cli, "_check_for_update_at_startup", lambda config: ran.__setitem__("checked", True))

    cli.main([])

    assert ran["checked"] is False  # no way to answer a prompt without a TTY


def test_ui_bridge_flag_passed_to_shell_and_forces_json_mode(monkeypatch):
    # --ui-bridge is a json-mode transport: it must reach the shell and select json
    # mode even without an explicit --mode json (the VSCode extension relies on this).
    captured = _stub_launch(monkeypatch)
    cli.main(["--ui-bridge"])
    assert captured["ui_bridge"] is True


def test_ui_bridge_off_by_default(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--mode", "json", "--prompt", "hi"])
    assert captured["ui_bridge"] is False


def test_json_events_flag_passed_to_shell(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--mode", "json", "--json-events", "--prompt", "hi"])
    assert captured["json_events"] is True


def test_json_events_off_by_default(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--mode", "json", "--prompt", "hi"])
    assert captured["json_events"] is False


def test_full_agent_messages_off_by_default(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main([])
    assert captured["full_agent_messages"] is False


def test_full_agent_messages_flag_enables_it(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--full-agent-messages"])
    assert captured["full_agent_messages"] is True


def test_full_agent_messages_flag_not_forwarded_to_backend(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--full-agent-messages"])
    assert "--full-agent-messages" not in captured["backend_args"]


def test_unknown_args_forwarded_to_backend(monkeypatch):
    captured = _stub_launch(monkeypatch)
    rc = cli.main(["--backend", "opencode", "--port", "12345"])
    assert rc == 0
    assert captured["backend_args"] == ["--port", "12345"]


def test_double_dash_forwards_agitrack_defined_flags_and_prompt(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "claude", "--", "--verbose", "fix the bug"])
    # everything after -- goes to the backend, including a flag aGiTrack also owns
    assert captured["backend_args"] == ["--verbose", "fix the bug"]


def test_agitrack_flags_still_bind_before_separator(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--verbose", "--backend", "claude", "--", "--model", "opus"])
    # --verbose before -- is aGiTrack's; only post-separator args pass through
    assert captured["backend_args"] == ["--model", "opus"]


def test_no_passthrough_args_is_empty_list(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "opencode"])
    assert captured["backend_args"] == []


def test_reserved_passthrough_flag_warns_but_forwards(monkeypatch, capsys):
    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "claude", "--resume", "abc123"])
    out = capsys.readouterr().out
    assert "--resume" in out and "session" in out.lower()
    assert captured["backend_args"] == ["--resume", "abc123"]  # still forwarded


def test_backend_command_flag_passed_to_runner(monkeypatch):
    captured = _stub_launch(monkeypatch)
    rc = cli.main(["--backend-command", "somewrapper claude"])
    assert rc == 0
    assert captured["backend_command"] == ["somewrapper", "claude"]


def test_backend_command_absent_resolves_from_config(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--backend", "opencode"])
    # No flag and the stub config has no backend_command ⇒ launch the binary directly.
    assert captured["backend_command"] == []


def test_backend_command_invalid_value_fails_fast(monkeypatch, capsys):
    _stub_launch(monkeypatch)
    rc = cli.main(["--backend-command", 'wrap "unbalanced'])
    assert rc == 1
    assert "backend-command" in capsys.readouterr().out.lower()


def test_proxy_runner_stores_backend_command(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    from agitrack.proxy.runner import ProxyRunner

    runner = ProxyRunner(GitRepo(tmp_path), backend_command=["somewrapper", "opencode"])
    assert runner._backend_command == ["somewrapper", "opencode"]
    # The launch command flows into the spawned command's executable head.
    assert runner._launch_command() == ["somewrapper", "opencode"]


def test_proxy_runner_stores_backend_args(tmp_path):
    # Build a runner through the real __init__ (with a tmp git repo) and confirm
    # passthrough args are stored for _spawn to append.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    from agitrack.proxy.runner import ProxyRunner

    runner = ProxyRunner(GitRepo(tmp_path), backend_args=["--port", "9999"])
    assert runner._backend_args == ["--port", "9999"]
    # _spawn appends them after spawn_command; verify that composition directly.
    base = ["opencode", str(tmp_path)]
    assert base + runner._backend_args == ["opencode", str(tmp_path), "--port", "9999"]


def test_json_backends_append_backend_args():
    from agitrack.backends.claude import ClaudeBackend
    from agitrack.backends.opencode import OpenCodeBackend

    claude = ClaudeBackend("/repo", backend_args=["--max-budget-usd", "5"])
    assert claude.backend_args == ["--max-budget-usd", "5"]

    oc = OpenCodeBackend("/repo", backend_args=["--port", "0"])
    assert oc.backend_args == ["--port", "0"]


# --- combined help (#32) ----------------------------------------------------


def _no_backend_spawn(monkeypatch):
    """Record any backend-CLI invocation so a help test can assert none happened."""
    calls: list = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: calls.append(args) or None)
    monkeypatch.setattr(cli.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    return calls


def test_help_shows_only_agitrack_options(monkeypatch, capsys):
    # `agitrack --help` shows aGiTrack's OWN options only — never the backend's help.
    calls = _no_backend_spawn(monkeypatch)

    class Config:
        def has_default_backend(self):
            return True

        default_backend = "opencode"

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--help"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Interactive agent + git commit orchestration" in out
    assert "--backend" in out and "--no-commit-guidance" in out  # aGiTrack's own options
    assert "Backend help" not in out  # NOT the backend's help section
    assert calls == []  # the backend CLI was never invoked for help


def test_help_short_flag_shows_only_agitrack_options(monkeypatch, capsys):
    calls = _no_backend_spawn(monkeypatch)

    class Config:
        def has_default_backend(self):
            return True

        default_backend = "claude"

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["-h"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Interactive agent + git commit orchestration" in out
    assert "Backend help" not in out
    assert calls == []


def test_help_with_explicit_backend_still_omits_backend_help(monkeypatch, capsys):
    # Even with --backend, `--help` shows only aGiTrack's options.
    calls = _no_backend_spawn(monkeypatch)

    class Config:
        def has_default_backend(self):
            return False

        default_backend = None

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--backend", "opencode", "--help"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "--backend" in out
    assert "Backend help" not in out
    assert calls == []


def test_help_works_with_no_backend_selected(monkeypatch, capsys):
    # Help is backend-independent now: it shows even when no backend is chosen yet, and
    # no longer prints the old "No backend selected yet" combined-help note.
    _no_backend_spawn(monkeypatch)

    class Config:
        def has_default_backend(self):
            return False

        default_backend = None

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--help"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Interactive agent + git commit orchestration" in out
    assert "No backend selected yet" not in out


def test_backend_help_via_double_dash_runs_directly(monkeypatch):
    """Test that --backend X -- --help runs backend help directly, not combined help."""
    monkeypatch.setattr(
        cli, "_discover_or_init", lambda p: (_ for _ in ()).throw(AssertionError("TUI should not launch"))
    )
    monkeypatch.setattr(cli.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    class Config:
        def has_default_backend(self):
            return True

        default_backend = "opencode"

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    class FakeResult:
        returncode = 0

    run_calls = []

    def fake_run(*args, **kwargs):
        run_calls.append(args[0])
        return FakeResult()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    rc = cli.main(["--backend", "opencode", "--", "--help"])
    assert rc == 0
    assert run_calls == [["opencode", "--help"]]


def test_backend_help_runs_directly_without_tui(monkeypatch):
    monkeypatch.setattr(
        cli, "_discover_or_init", lambda p: (_ for _ in ()).throw(AssertionError("TUI should not launch"))
    )
    monkeypatch.setattr(cli.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    class Config:
        def has_default_backend(self):
            return True

        default_backend = "opencode"

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    class FakeResult:
        returncode = 0

    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: FakeResult())

    rc = cli.main(["--backend", "opencode", "--", "--help"])
    assert rc == 0


def test_backend_help_no_backend_selected(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_discover_or_init", lambda p: object())

    class Config:
        def has_default_backend(self):
            return False

        default_backend = None

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--", "--help"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "No backend selected" in out


def test_backend_other_args_still_launch_tui(monkeypatch):
    captured = _stub_launch(monkeypatch)
    rc = cli.main(["--backend", "opencode", "--", "--port", "12345"])
    assert rc == 0
    assert captured["backend_args"] == ["--port", "12345"]


# --- scripted prompts: --prompt (#53) -----------------------------------------


def test_prompt_flag_implies_json_mode_and_passes_prompts(monkeypatch):
    captured: dict = {}

    class FakeShell:
        def __init__(self, repo, **kw):
            captured.update(kw)

        def run(self):
            return None

    monkeypatch.setattr(cli, "AgitrackShell", FakeShell)
    monkeypatch.setattr(
        cli, "ProxyRunner", lambda *a, **k: (_ for _ in ()).throw(AssertionError("proxy must not launch"))
    )
    _stub_repo_and_free_lock(monkeypatch)

    class Config:
        def has_default_backend(self):
            return True

        default_backend = "claude"
        use_worktrees = True

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--prompt", "build it", "--prompt", ":status"])

    assert rc == 0
    assert captured["prompts"] == ["build it", ":status"]


def test_prompt_flag_never_blocks_on_input_even_with_a_tty(monkeypatch):
    # A scripted run must sail past the privacy acknowledgment and the
    # first-run backend selection — both would otherwise input() on a TTY.
    captured: dict = {}

    class FakeShell:
        def __init__(self, repo, **kw):
            captured.update(kw)

        def run(self):
            return None

    monkeypatch.setattr(cli, "AgitrackShell", FakeShell)
    _stub_repo_and_free_lock(monkeypatch)

    class Config:
        def has_default_backend(self):
            return False  # would trigger the interactive first-run selection

        default_backend = None
        use_worktrees = True

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr(
        "builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("scripted run must not prompt"))
    )

    rc = cli.main(["--backend", "claude", "--prompt", "build it"])

    assert rc == 0
    assert captured["prompts"] == ["build it"]


# --- startup privacy warning --------------------------------------------------


def test_privacy_warning_acknowledged_with_enter(monkeypatch, capsys):
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: "")

    assert cli._acknowledge_privacy_warning() is True
    out = capsys.readouterr().out
    # The warning explains what is logged and what not to enter.
    assert "git commit" in out
    assert "passwords, API keys" in out


def test_privacy_warning_drains_stdin_before_reading(monkeypatch):
    # A stray newline injected into the terminal (e.g. by an editor's shell integration)
    # must not auto-acknowledge: pending input is flushed BEFORE the prompt reads, so the
    # acknowledgment stays a deliberate keypress.
    events: list[str] = []
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr(cli, "_drain_terminal_input", lambda: events.append("drain"))
    monkeypatch.setattr("builtins.input", lambda *a: events.append("input") or "")

    assert cli._acknowledge_privacy_warning() is True
    assert events == ["drain", "input"]  # drained first, then read


def test_drain_terminal_input_never_raises():
    cli._drain_terminal_input()  # no real tty under pytest; must be a safe no-op


def test_privacy_warning_quit_aborts(monkeypatch, capsys):
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: "q")

    assert cli._acknowledge_privacy_warning() is False
    assert "not started" in capsys.readouterr().out


def test_privacy_warning_interrupt_aborts(monkeypatch, capsys):
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))

    assert cli._acknowledge_privacy_warning() is False


def test_privacy_warning_non_interactive_prints_and_continues(monkeypatch, capsys):
    # No TTY = no way to acknowledge: print the warning, never block automation.
    _force_tty(monkeypatch, stdin=False, stdout=False)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))

    assert cli._acknowledge_privacy_warning() is True
    assert "passwords, API keys" in capsys.readouterr().out


def test_privacy_warning_skipped_does_not_print_or_prompt(monkeypatch, capsys):
    # A menu-update restart passes skip=True: no warning, no prompt, just continue.
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))

    assert cli._acknowledge_privacy_warning(skip=True) is True
    assert capsys.readouterr().out == ""


def test_main_stops_when_privacy_warning_declined(monkeypatch):
    captured = _stub_launch(monkeypatch)
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: "q")

    rc = cli.main([])

    assert rc == 1
    assert captured == {}  # neither the proxy nor the shell was launched
