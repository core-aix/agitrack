import subprocess
import sys

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


# --- startup gh-availability check ------------------------------------------------


def _stub_gh(monkeypatch, *, status: str, has_remote: bool = True):
    """Stub the gh status + GitHub-remote probes the startup check reads."""
    import agitrack.metrics.github as ghmod

    monkeypatch.setattr(ghmod, "gh_status", lambda: status)
    monkeypatch.setattr(ghmod, "commit_url_base", lambda repo: "https://x/commit/" if has_remote else "")
    monkeypatch.setattr(cli, "_drain_terminal_input", lambda: None)


_FAKE_REPO = object()


def test_gh_check_silent_when_authenticated(monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    _stub_gh(monkeypatch, status="ok")
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))
    assert cli._check_gh_availability(_FAKE_REPO) == (True, False)


def test_gh_check_silent_without_a_github_remote(monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    _stub_gh(monkeypatch, status="unauthenticated", has_remote=False)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))
    assert cli._check_gh_availability(_FAKE_REPO) == (True, False)


def test_gh_check_non_interactive_does_not_prompt(monkeypatch):
    _force_tty(monkeypatch, stdin=False)
    _stub_gh(monkeypatch, status="unauthenticated")
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))
    assert cli._check_gh_availability(_FAKE_REPO) == (True, False)


def test_gh_check_unauthenticated_continue(monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    _stub_gh(monkeypatch, status="unauthenticated")
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert cli._check_gh_availability(_FAKE_REPO) == (True, True)


def test_gh_check_quit_aborts_startup(monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    _stub_gh(monkeypatch, status="unauthenticated")
    monkeypatch.setattr("builtins.input", lambda *a: "q")
    assert cli._check_gh_availability(_FAKE_REPO) == (False, True)


def test_gh_check_login_runs_gh_auth_login(monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    _stub_gh(monkeypatch, status="unauthenticated")
    monkeypatch.setattr("builtins.input", lambda *a: "l")
    ran = []
    monkeypatch.setattr(cli, "_run_gh_login", lambda: ran.append(True))
    assert cli._check_gh_availability(_FAKE_REPO) == (True, True)
    assert ran == [True]


def test_gh_check_missing_does_not_offer_login(monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    _stub_gh(monkeypatch, status="missing")
    monkeypatch.setattr("builtins.input", lambda *a: "l")  # 'l' is meaningless when gh isn't installed
    ran = []
    monkeypatch.setattr(cli, "_run_gh_login", lambda: ran.append(True))
    assert cli._check_gh_availability(_FAKE_REPO) == (True, True)
    assert ran == []  # no login attempted — gh isn't installed


# --- startup menu-key conflict check ----------------------------------------------


def _menu_config(tmp_path, **data):
    from agitrack.config import GlobalConfig

    config = GlobalConfig(tmp_path / "config.json")
    config.data.update(data)
    return config


def test_menu_key_check_silent_without_conflict(tmp_path, monkeypatch):
    # No known host conflict (not VS Code) → never prompts.
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr(cli.os, "environ", {"TERM_PROGRAM": "iTerm.app"})
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))
    assert cli._verify_menu_key(_menu_config(tmp_path)) is True


def test_menu_key_check_non_interactive_does_not_prompt(tmp_path, monkeypatch):
    _force_tty(monkeypatch, stdin=False)
    monkeypatch.setattr(cli.os, "environ", {"TERM_PROGRAM": "vscode"})
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))
    assert cli._verify_menu_key(_menu_config(tmp_path)) is True


def test_menu_key_check_keep_records_acknowledgement(tmp_path, monkeypatch):
    # VS Code + Ctrl-G conflicts; pressing Enter keeps it and records the ack so the next
    # launch stays quiet.
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr(cli.os, "environ", {"TERM_PROGRAM": "vscode"})
    monkeypatch.setattr("builtins.input", lambda *a: "")  # keep
    config = _menu_config(tmp_path)
    assert cli._verify_menu_key(config) is True
    assert config._raw("menu_key_acknowledged") == "ctrl-g"
    # Second launch: already acknowledged → no prompt.
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))
    assert cli._verify_menu_key(config) is True


def test_menu_key_check_quit_aborts(tmp_path, monkeypatch):
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr(cli.os, "environ", {"TERM_PROGRAM": "vscode"})
    monkeypatch.setattr("builtins.input", lambda *a: "q")
    assert cli._verify_menu_key(_menu_config(tmp_path)) is False


def test_menu_key_check_test_then_keep(tmp_path, monkeypatch):
    # 't' runs the key test (stubbed), then Enter keeps the key.
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr(cli.os, "environ", {"TERM_PROGRAM": "vscode"})
    answers = iter(["t", ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    tested = []
    monkeypatch.setattr(cli, "_run_menu_key_test", lambda key: tested.append(key) or True)
    assert cli._verify_menu_key(_menu_config(tmp_path)) is True
    assert tested == ["ctrl-g"]


def _fake_msvcrt(monkeypatch, presses):
    # A stand-in msvcrt for the native-Windows menu-key probe so it's testable on POSIX
    # (the real path has no termios; #118). getch pops the queued keypresses in order.
    import sys
    import types

    queue = list(presses)
    monkeypatch.setitem(
        sys.modules, "msvcrt", types.SimpleNamespace(kbhit=lambda: bool(queue), getch=lambda: queue.pop(0))
    )


def test_read_menu_key_press_windows_detects_expected(monkeypatch):
    # Ctrl-G arriving as 0x07 means the key reached aGiTrack and would open the TUI menu.
    _fake_msvcrt(monkeypatch, [b"\x07"])
    assert cli._read_menu_key_press_windows(b"\x07", shift=False, timeout=1.0) is True


def test_read_menu_key_press_windows_times_out_when_intercepted(monkeypatch):
    # The host (VS Code) swallowed the key — nothing ever arrives, so the probe reports False.
    _fake_msvcrt(monkeypatch, [])
    assert cli._read_menu_key_press_windows(b"\x07", shift=False, timeout=0.05) is False


def test_read_menu_key_press_windows_ctrl_c_cancels(monkeypatch):
    _fake_msvcrt(monkeypatch, [b"\x03"])  # Ctrl-C
    assert cli._read_menu_key_press_windows(b"\x07", shift=False, timeout=1.0) is None


def test_read_menu_key_press_windows_skips_function_key_scancodes(monkeypatch):
    # A function/arrow key (lead byte 0xe0 + scancode) is consumed, not matched; a real
    # Ctrl-G after it still registers.
    _fake_msvcrt(monkeypatch, [b"\xe0", b"H", b"\x07"])
    assert cli._read_menu_key_press_windows(b"\x07", shift=False, timeout=1.0) is True


def test_menu_key_check_change_persists_new_key(tmp_path, monkeypatch):
    # 'c' to change → enter a non-conflicting key → it's persisted as menu_key.
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr(cli.os, "environ", {"TERM_PROGRAM": "vscode"})
    answers = iter(["c", "ctrl-o", "n"])  # choose; new key; skip the test
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    config = _menu_config(tmp_path)
    assert cli._verify_menu_key(config) is True
    assert config.menu_key == "ctrl-o"
    assert config._raw("menu_key_acknowledged") == "ctrl-o"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows msvcrt path returns False on timeout, not None")
def test_read_menu_key_press_returns_none_without_tty():
    # No real tty under pytest → the raw-mode test can't run and reports "unavailable".
    assert cli._read_menu_key_press(b"\x07", shift=False) is None


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

        def acquire(self):
            return True  # nobody else holds it — we take it

        def release(self):
            pass

        def owner_pid(self):
            return None

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
    monkeypatch.setattr(cli, "BackgroundRunner", Fake)
    monkeypatch.setattr(cli, "AgitrackShell", Fake)
    # These tests exercise main()'s arg routing, not the pre-TUI startup checks; neutralize
    # them so the minimal stub Config/repo below need no extra surface — and so the checks
    # don't behave differently by environment (e.g. the menu-key check firing because the
    # suite runs inside VS Code, or the gh check shelling out to `gh` on the stub repo,
    # which has no `_run` and broke CI where gh is unauthenticated).
    monkeypatch.setattr(cli, "_verify_menu_key", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_check_gh_availability", lambda *a, **k: (True, False))
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

        def acquire(self):
            return False  # another instance holds it — refuse

        def owner_pid(self):
            return 4321

        def release(self):
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


def test_background_refused_when_another_instance_holds_the_repo(monkeypatch):
    # Only ONE aGiTrack per repo: a background tracker must be refused (never launched) when the
    # single-writer repo lock is already held — by ANY mode — so two never race over commits.
    import pathlib
    from types import SimpleNamespace

    launched: list = []
    monkeypatch.setattr(cli, "_discover_or_init", lambda p: SimpleNamespace(repo=pathlib.Path("/tmp/x")))
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    monkeypatch.setattr(cli, "BackgroundRunner", lambda *a, **k: launched.append(k))

    class _HeldLock:
        def __init__(self, _path):
            pass

        def acquire(self):
            return False  # another instance already holds the repo

        def owner_pid(self):
            return 999

        def release(self):
            pass

    monkeypatch.setattr(cli, "RepoLock", _HeldLock)
    monkeypatch.setattr(cli, "already_running_message", lambda pid: "running")

    class Config:
        check_for_updates = False
        background = False

        def has_default_backend(self):
            return True

        default_backend = "claude"

        def load_repo_overlay(self, _root):
            pass

        def seed_defaults(self):
            return False

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--background", "--backend", "claude"])

    assert rc == 1
    assert launched == []  # the tracker was never constructed


def test_no_backend_configured_non_interactive_errors(monkeypatch, capsys):
    # No --backend and no configured default, run non-interactively: aGiTrack must
    # fail clearly rather than silently fall back to a hardcoded backend (the old
    # behaviour that produced surprise OpenCode sessions).
    _force_tty(monkeypatch, stdin=False)
    _stub_repo_and_free_lock(monkeypatch)
    launched: list = []

    class Fake:
        def __init__(self, repo, **kw):
            launched.append(kw)

        def run(self):
            return 0

    monkeypatch.setattr(cli, "ProxyRunner", Fake)
    monkeypatch.setattr(cli, "AgitrackShell", Fake)

    class Config:
        use_worktrees = True

        def has_default_backend(self):
            return False

        default_backend = None

        def load_repo_overlay(self, _root):
            pass

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main([])

    assert rc == 1
    assert launched == []  # never launched a backend
    assert "No coding agent backend is configured" in capsys.readouterr().out


def test_explicit_backend_flag_launches_without_configured_default(monkeypatch):
    # An explicit --backend works even with no configured default (no fallback needed).
    _force_tty(monkeypatch, stdin=False)
    _stub_repo_and_free_lock(monkeypatch)
    captured: dict = {}

    class Fake:
        def __init__(self, repo, **kw):
            captured.update(kw)

        def run(self):
            return 0

    monkeypatch.setattr(cli, "ProxyRunner", Fake)
    monkeypatch.setattr(cli, "AgitrackShell", Fake)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)

    class Config:
        use_worktrees = True

        def has_default_backend(self):
            return False

        default_backend = None

        def load_repo_overlay(self, _root):
            pass

    monkeypatch.setattr(cli, "GlobalConfig", lambda: Config())

    rc = cli.main(["--backend", "claude"])

    assert rc == 0
    assert captured.get("backend") == "claude"


# --- --no-worktree (#9) -----------------------------------------------------


def test_no_worktree_flag_disables_worktrees(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main(["--no-worktree"])
    assert captured["use_worktrees"] is False


def test_default_uses_config_use_worktrees(monkeypatch):
    captured = _stub_launch(monkeypatch, use_worktrees=False)  # config opt-out, no flag
    cli.main([])
    assert captured["use_worktrees"] is False


def test_manual_commits_flag_forces_no_worktree(monkeypatch):
    # --manual-commits always runs without a worktree, even though config has worktrees on.
    captured = _stub_launch(monkeypatch, use_worktrees=True)
    cli.main(["--manual-commits"])
    assert captured["manual_commits"] is True
    assert captured["use_worktrees"] is False


def test_manual_commits_short_flag(monkeypatch):
    # -m is the short alias and behaves identically.
    captured = _stub_launch(monkeypatch, use_worktrees=True)
    cli.main(["-m"])
    assert captured["manual_commits"] is True
    assert captured["use_worktrees"] is False


def test_manual_commits_off_by_default(monkeypatch):
    captured = _stub_launch(monkeypatch)
    cli.main([])
    assert captured["manual_commits"] is False
    assert captured["use_worktrees"] is True  # worktrees stay on when manual mode is off


# --- --background / -b -------------------------------------------------------


def _autostart_config(tmp_path):
    from agitrack.config.settings import GlobalConfig

    cfg = GlobalConfig(path=tmp_path / "global.json")
    cfg.load_repo_overlay(tmp_path)  # repo overlay at tmp_path/.agitrack/config.json
    return cfg


def test_background_hook_prompt_enable_off_and_asks_once(tmp_path, monkeypatch, capsys):
    # `agitrack -b` explains the auto-start hook and records the repo-scoped choice once.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def run(answer):
        cfg = _autostart_config(tmp_path / (answer or "default"))  # a fresh repo per case
        monkeypatch.setattr("builtins.input", lambda *a: answer)
        cli._maybe_prompt_background_hook(cfg, scripted=False)
        return cfg

    assert run("").autotrack_hook == "auto"  # default (Enter) enables auto-start
    assert run("y").autotrack_hook == "auto"
    assert run("n").autotrack_hook == "off"
    assert "--remove-hooks" in capsys.readouterr().out  # tells the user how to cancel it

    # Asked once per repo: a second call does not re-prompt.
    keep = run("y")
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("re-prompted")))
    cli._maybe_prompt_background_hook(keep, scripted=False)


def test_background_hook_prompt_skipped_when_scripted(tmp_path, monkeypatch):
    cfg = _autostart_config(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("should not prompt")))
    cli._maybe_prompt_background_hook(cfg, scripted=True)
    assert cfg.source("autotrack_hook") != "repo"  # nothing recorded (default 'auto' stays implicit)


def _stub_bg_daemon(monkeypatch):
    """`agitrack -b` now spawns a DETACHED daemon: the launcher calls start_background_daemon
    with the flags forwarded to the child. Capture those to assert the resolved commit mode."""
    captured: dict = {}

    def fake_start(repo, *, extra_args, **kw):
        captured["extra_args"] = extra_args
        return 0

    monkeypatch.setattr("agitrack.proxy.background.start_background_daemon", fake_start)
    return captured


def test_background_flag_forces_no_worktree_and_auto_default(monkeypatch):
    # --background always runs without a worktree and defaults to AUTO commits (like the TUI).
    _stub_launch(monkeypatch, use_worktrees=True)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    captured = _stub_bg_daemon(monkeypatch)
    cli.main(["--background"])
    # The launcher forwards the resolved commit mode to the detached daemon child.
    assert "--auto-commit" in captured["extra_args"]  # auto by default
    assert "--manual-commits" not in captured["extra_args"]


def test_background_short_flag(monkeypatch):
    _stub_launch(monkeypatch)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    captured = _stub_bg_daemon(monkeypatch)
    cli.main(["-b"])
    assert "--auto-commit" in captured["extra_args"]  # auto by default


def test_background_manual_commits_opts_into_manual(monkeypatch):
    _stub_launch(monkeypatch)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    captured = _stub_bg_daemon(monkeypatch)
    cli.main(["-b", "-m"])
    assert "--manual-commits" in captured["extra_args"]  # -m opts into user-triggered commits


def test_background_stop_and_status_do_not_launch(monkeypatch):
    # `-b stop` / `-b status` are handled early and never construct a runner.
    calls: dict = {}
    monkeypatch.setattr(cli, "GitRepo", type("R", (), {"discover": staticmethod(lambda p: object())}))

    def _stop(repo):
        calls["stop"] = True
        return 0

    def _status(repo):
        calls["status"] = True
        return 0

    monkeypatch.setattr("agitrack.proxy.background.stop_background", _stop)
    monkeypatch.setattr("agitrack.proxy.background.background_status", _status)

    assert cli.main(["-b", "stop"]) == 0
    assert cli.main(["-b", "status"]) == 0
    assert calls == {"stop": True, "status": True}


def test_background_off_by_default(monkeypatch):
    # No --background ⇒ the normal proxy path runs (captures use_worktrees), background inert.
    captured = _stub_launch(monkeypatch, use_worktrees=True)
    cli.main([])
    assert captured["use_worktrees"] is True


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


def test_backend_command_mismatch_warns(monkeypatch, capsys):
    captured = _stub_launch(monkeypatch)
    rc = cli.main(["--backend", "claude", "--backend-command", "wrap opencode"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Warning" in out and "opencode" in out and "claude" in out
    # The launch still goes through with exactly what the user asked for.
    assert captured["backend_command"] == ["wrap", "opencode"]


def test_backend_command_mismatch_aborts_when_declined(monkeypatch, capsys):
    # Interactive run: a mismatch must be explicitly confirmed; declining (anything but
    # y) aborts before the backend is ever launched.
    captured = _stub_launch(monkeypatch)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_drain_terminal_input", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    rc = cli.main(["--backend", "claude", "--backend-command", "wrap opencode"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Warning" in out and "not started" in out
    assert captured == {}  # the runner was never constructed


def test_backend_command_mismatch_proceeds_when_confirmed(monkeypatch):
    # Entering y proceeds with exactly the command the user asked for.
    captured = _stub_launch(monkeypatch)
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: True)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_drain_terminal_input", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    rc = cli.main(["--backend", "claude", "--backend-command", "wrap opencode"])
    assert rc == 0
    assert captured["backend_command"] == ["wrap", "opencode"]


def test_backend_command_naming_selected_backend_does_not_warn(monkeypatch, capsys):
    _stub_launch(monkeypatch)
    cli.main(["--backend", "claude", "--backend-command", "somewrapper claude"])
    assert "Warning" not in capsys.readouterr().out


def test_backend_command_opaque_wrapper_does_not_warn(monkeypatch, capsys):
    # A wrapper that doesn't name any known backend is left alone (no guessing).
    _stub_launch(monkeypatch)
    cli.main(["--backend", "claude", "--backend-command", "mylauncher --flag"])
    assert "Warning" not in capsys.readouterr().out


def test_proxy_runner_stores_backend_command(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    from agitrack.proxy.runner import ProxyRunner

    runner = ProxyRunner(GitRepo(tmp_path), backend="opencode", backend_command=["somewrapper", "opencode"])
    assert runner._backend_command == ["somewrapper", "opencode"]
    # The launch command flows into the spawned command's executable head.
    assert runner._launch_command() == ["somewrapper", "opencode"]


def test_proxy_runner_stores_backend_args(tmp_path):
    # Build a runner through the real __init__ (with a tmp git repo) and confirm
    # passthrough args are stored for _spawn to append.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    from agitrack.proxy.runner import ProxyRunner

    runner = ProxyRunner(GitRepo(tmp_path), backend="opencode", backend_args=["--port", "9999"])
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


def test_privacy_warning_wraps_to_terminal_width():
    # A narrow terminal must wrap the warning tighter (different break points) so it never
    # overflows, while a wide terminal keeps the authored wrapping.
    wide = cli._privacy_warning(100)
    narrow = cli._privacy_warning(34)
    for text in (wide, narrow):
        assert text.startswith("\n")  # leading blank line preserved
        assert "passwords, API keys" in text  # key phrase never split mid-wrap
    # Narrow re-wraps: more lines, and every line fits the width.
    narrow_lines = [line for line in narrow.splitlines() if line]
    assert len(narrow_lines) > len([line for line in wide.splitlines() if line])
    assert all(len(line) <= 34 for line in narrow_lines)


def test_privacy_warning_never_exceeds_authored_width_on_wide_terminal():
    # On a very wide terminal we cap at the authored width rather than stretching the text
    # across the whole screen.
    lines = [line for line in cli._privacy_warning(500).splitlines() if line]
    assert lines and all(len(line) <= cli._PRIVACY_WARNING_WIDTH for line in lines)


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
    # json/scripted startup acknowledges the privacy warning in cli.main (no pre-TUI config
    # steps precede it there). The interactive TUI path acks it inside the runner, AFTER the
    # gh-login / menu-key / backend-install steps — see test_proxy.test_run_*privacy*.
    captured = _stub_launch(monkeypatch)
    _force_tty(monkeypatch, stdin=True)
    monkeypatch.setattr("builtins.input", lambda *a: "q")

    rc = cli.main(["--mode", "json"])

    assert rc == 1
    assert captured == {}  # the shell was not launched
