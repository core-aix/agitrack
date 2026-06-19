"""Settings: the repo-local overlay on GlobalConfig and the Ctrl-G settings menu.

The overlay lets a setting be written for a single repository (its
``.agitrack/config.json``) and take precedence over the global file. The menu edits
any config option, asks repo-local vs global before saving, and loops so several can
be changed in one visit (backward navigation).
"""

from __future__ import annotations

import json

from agitrack.config.settings import GlobalConfig

from proxy_helpers import make_runner


def _config(tmp_path):
    gc = GlobalConfig(path=tmp_path / "global" / "config.json")
    gc.load_repo_overlay(tmp_path / "repo")
    return gc


# --- repo-local overlay -----------------------------------------------------


def test_repo_overlay_overrides_global(tmp_path):
    gc = _config(tmp_path)
    gc.set("sandbox", False, scope="global")
    assert gc.sandbox is False and gc.source("sandbox") == "global"
    gc.set("sandbox", True, scope="repo")  # repo wins
    assert gc.sandbox is True and gc.source("sandbox") == "repo"
    # The repo value lives in the repo file; the global file keeps its own.
    assert json.loads((tmp_path / "repo" / ".agitrack" / "config.json").read_text())["sandbox"] is True
    assert json.loads((tmp_path / "global" / "config.json").read_text())["sandbox"] is False


def test_unset_repo_reveals_global(tmp_path):
    gc = _config(tmp_path)
    gc.set("use_worktrees", False, scope="global")
    gc.set("use_worktrees", True, scope="repo")
    assert gc.use_worktrees is True
    gc.unset("use_worktrees", scope="repo")
    assert gc.use_worktrees is False and gc.source("use_worktrees") == "global"


def test_save_repo_preserves_other_keys(tmp_path):
    # The repo config.json is shared with AgitrackState (summarization etc.); writing a
    # setting must not clobber unrelated keys already in the file.
    repo_cfg = tmp_path / "repo" / ".agitrack" / "config.json"
    repo_cfg.parent.mkdir(parents=True)
    repo_cfg.write_text(json.dumps({"summarization_enabled": False, "trace_turn_limit": 9}))
    gc = _config(tmp_path)
    gc.set("sandbox", False, scope="repo")
    data = json.loads(repo_cfg.read_text())
    assert data == {"summarization_enabled": False, "trace_turn_limit": 9, "sandbox": False}


def test_allowed_edit_paths_parsing(tmp_path):
    gc = _config(tmp_path)
    gc.set("allowed_edit_paths", ["/a", "/b"], scope="repo")
    assert gc.allowed_edit_paths == ["/a", "/b"]
    # A hand-written ":"-joined string is tolerated.
    gc.set("allowed_edit_paths", "/x:/y", scope="global")
    gc.unset("allowed_edit_paths", scope="repo")
    assert gc.allowed_edit_paths == ["/x", "/y"]


# --- settings menu ----------------------------------------------------------


def _settings_runner(tmp_path):
    runner = make_runner()
    runner.global_config = _config(tmp_path)
    runner._set_message = lambda *a, **k: None
    runner._render = lambda *a, **k: None
    return runner


def _drive(runner, steps):
    """Drive _select_popup/_prompt_popup with a scripted list of (title-substring, fn)."""
    it = iter(steps)

    def select(title, options, **k):
        key, fn = next(it)
        assert key in title, f"expected step '{key}', got popup '{title}'"
        return fn(options) if callable(fn) else fn

    def prompt(title, body, *, default=""):
        key, fn = next(it)
        assert key in title, f"expected step '{key}', got prompt '{title}'"
        return fn(default) if callable(fn) else fn

    runner._select_popup = select
    runner._prompt_popup = prompt


def test_settings_menu_saves_bool_to_repo(tmp_path):
    runner = _settings_runner(tmp_path)
    assert runner.global_config.commit_guidance is True
    _drive(
        runner,
        [
            ("Settings", lambda opts: next(o for o in opts if o.startswith("Tell the agent"))),
            ("Tell the agent", "Turn OFF"),
            ("Save", lambda opts: next(o for o in opts if o.startswith("This repository"))),
            ("Settings", "← Done"),  # loop back, then close
        ],
    )
    runner._settings_menu()
    assert runner.global_config.commit_guidance is False
    assert runner.global_config.source("commit_guidance") == "repo"


def test_settings_menu_saves_choice_to_global(tmp_path):
    runner = _settings_runner(tmp_path)
    _drive(
        runner,
        [
            ("Settings", lambda opts: next(o for o in opts if o.startswith("Default backend"))),
            ("Default backend", "claude"),
            ("Save", lambda opts: next(o for o in opts if o.startswith("Global"))),
            ("Settings", "← Done"),
        ],
    )
    runner._settings_menu()
    assert runner.global_config.default_backend == "claude"
    assert runner.global_config.source("default_backend") == "global"


def test_settings_menu_back_navigation_does_not_save(tmp_path):
    runner = _settings_runner(tmp_path)
    _drive(
        runner,
        [
            ("Settings", lambda opts: next(o for o in opts if o.startswith("Sandbox"))),
            ("Sandbox", "← Back"),  # back from the editor → no save, returns to list
            ("Settings", "← Done"),
        ],
    )
    runner._settings_menu()
    assert runner.global_config.source("sandbox") == "default"  # never written


def test_settings_menu_edits_allowed_paths_and_updates_runtime(tmp_path):
    runner = _settings_runner(tmp_path)
    runner._allowed_edit_paths = []
    _drive(
        runner,
        [
            ("Settings", lambda opts: next(o for o in opts if o.startswith("Extra sandbox-writable"))),
            ("Extra sandbox-writable", lambda default: "/data/shared:/srv/x"),
            ("Save", lambda opts: next(o for o in opts if o.startswith("This repository"))),
            ("Settings", "← Done"),
        ],
    )
    runner._settings_menu()
    assert runner.global_config.allowed_edit_paths == ["/data/shared", "/srv/x"]
    assert runner._allowed_edit_paths == ["/data/shared", "/srv/x"]  # live runtime value too


def test_settings_timings_submenu_saves(tmp_path):
    runner = _settings_runner(tmp_path)
    _drive(
        runner,
        [
            ("Timings", lambda opts: next(o for o in opts if o.startswith("file_stable_seconds"))),
            ("file_stable_seconds", lambda default: "12"),
            ("Save timing", lambda opts: next(o for o in opts if o.startswith("Global"))),
            ("Timings", "← Back"),
        ],
    )
    runner._settings_timings_menu()
    assert runner.global_config.timings["file_stable_seconds"] == 12.0
