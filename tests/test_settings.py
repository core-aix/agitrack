"""Settings: the repo-local overlay on GlobalConfig and the Ctrl-G settings menu.

The overlay lets a setting be written for a single repository (its
``.agitrack/config.json``) and take precedence over the global file. The menu edits
any config option as a PENDING change (each picking repo-local vs global), writing them
only when the user confirms "save" on close. Esc goes up one level at every step.
"""

from __future__ import annotations

import json
import sys

import pytest

from agitrack.config.settings import GlobalConfig

from proxy_helpers import make_runner

# Tests that use POSIX-style paths ("/x:/y" with ":" separator) are skipped
# on Windows where os.pathsep is ";" and drive letters also contain ":".
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX path separator only")


def _config(tmp_path):
    gc = GlobalConfig(path=tmp_path / "global" / "config.json")
    gc.load_repo_overlay(tmp_path / "repo")
    return gc


# --- seed defaults ----------------------------------------------------------


def test_seed_defaults_writes_all_knobs_and_is_idempotent(tmp_path):
    from agitrack.sessions.share_cap import DEFAULT_MAX_SHARED_BYTES

    gc = GlobalConfig(path=tmp_path / "global" / "config.json")
    assert not gc.path.exists()  # nothing written yet

    assert gc.seed_defaults() is True  # first seed writes the file
    written = json.loads(gc.path.read_text())
    # Every user-facing knob is present with its default, so the file is self-documenting.
    assert written["sandbox"] is True
    assert written["use_worktrees"] is True
    assert written["manual_commits"] is False
    assert written["background"] is False
    assert written["commit_guidance"] is True
    assert written["summarization_enabled"] is True
    assert written["check_for_updates"] is True
    assert written["default_backend"] is None
    assert written["allowed_edit_paths"] == []
    assert written["menu_key"] == "ctrl-g"
    assert written["share_max_transcript_bytes"] == DEFAULT_MAX_SHARED_BYTES
    assert written["timings"]["base_poll_seconds"] == 3.0
    # Transient/state-like keys are NOT written as settings.
    assert "pending_manual_update" not in written
    assert "session_sharing" not in written

    # Idempotent: a second seed changes nothing and does not rewrite.
    assert gc.seed_defaults() is False


def test_seed_defaults_never_overwrites_user_values(tmp_path):
    gc = GlobalConfig(path=tmp_path / "global" / "config.json")
    gc.set("sandbox", False, scope="global")  # user turned sandbox off

    assert gc.seed_defaults() is True  # fills the OTHER missing keys
    written = json.loads(gc.path.read_text())
    assert written["sandbox"] is False  # user value preserved
    assert written["use_worktrees"] is True  # gap filled with default


def test_seed_defaults_adds_only_new_keys_after_upgrade(tmp_path):
    # Simulate a config written before "background" existed: seeding adds just that key.
    gc = GlobalConfig(path=tmp_path / "global" / "config.json")
    gc.data = {k: v for k, v in gc._default_config().items() if k != "background"}
    gc.save()

    assert gc.seed_defaults() is True
    assert json.loads(gc.path.read_text())["background"] is False


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


def test_share_size_cap_default_configure_and_hard_clamp(tmp_path):
    from agitrack.sessions.share_cap import DEFAULT_MAX_SHARED_BYTES, HARD_MAX_SHARED_BYTES

    gc = _config(tmp_path)
    # Default when unset.
    assert gc.share_max_transcript_bytes == DEFAULT_MAX_SHARED_BYTES
    # A sensible configured value is honored (repo overlay wins, like any setting).
    gc.set("share_max_transcript_bytes", 50 * 1024 * 1024, scope="repo")
    assert gc.share_max_transcript_bytes == 50 * 1024 * 1024
    # A nonsensical value falls back to the default (a typo can't break sharing).
    gc.set("share_max_transcript_bytes", "lots", scope="global")
    gc.unset("share_max_transcript_bytes", scope="repo")
    assert gc.share_max_transcript_bytes == DEFAULT_MAX_SHARED_BYTES
    # Even if an over-limit value slips in, the effective value is clamped to the hard ceiling.
    gc.set("share_max_transcript_bytes", 500 * 1024 * 1024, scope="repo")
    assert gc.share_max_transcript_bytes == HARD_MAX_SHARED_BYTES


def test_share_config_error_flags_only_over_the_hard_limit(tmp_path):
    from agitrack.sessions.share_cap import HARD_MAX_SHARED_BYTES

    gc = _config(tmp_path)
    assert gc.share_config_error() is None  # unset → fine
    gc.set("share_max_transcript_bytes", HARD_MAX_SHARED_BYTES, scope="global")
    assert gc.share_config_error() is None  # exactly at the limit → fine
    gc.set("share_max_transcript_bytes", HARD_MAX_SHARED_BYTES + 1, scope="global")
    err = gc.share_config_error()
    assert err is not None and "share_max_transcript_bytes" in err and "100 MiB" in err


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


@posix_only
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


# Edits are PENDING until the user confirms "save" when closing. Each edit picks its own
# scope (repo / global) via the "Apply '<label>' to:" prompt; closing with unsaved changes
# asks Yes/No/Keep editing. Esc goes up one level (value→list, scope→value, list→close).


def test_settings_menu_saves_bool_to_repo(tmp_path):
    runner = _settings_runner(tmp_path)
    assert runner.global_config.commit_guidance is True
    _drive(
        runner,
        [
            ("Settings", lambda opts: next(o for o in opts if o.startswith("Ask the agent"))),
            ("Ask the agent", "Turn OFF"),
            ("Apply", lambda opts: next(o for o in opts if o.startswith("This repository"))),
            ("Settings", lambda opts: next(o for o in opts if o.startswith("← Close"))),  # close → save prompt
            ("unsaved settings change", "Yes, save them"),
            ("Restart aGiTrack now", "Not now"),  # restart-only setting → offered a restart
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
            ("Settings", lambda opts: next(o for o in opts if o.startswith("Default coding agent"))),
            ("Default coding agent", "claude"),
            ("Apply", lambda opts: next(o for o in opts if o.startswith("Global"))),
            ("Settings", lambda opts: next(o for o in opts if o.startswith("← Close"))),
            ("unsaved settings change", "Yes, save them"),
            ("Restart aGiTrack now", "Not now"),  # default backend is read at launch → restart offered
        ],
    )
    runner._settings_menu()
    assert runner.global_config.default_backend == "claude"
    assert runner.global_config.source("default_backend") == "global"


def test_settings_menu_discard_on_close_writes_nothing(tmp_path):
    # Change a setting, then choose "No, discard" at the close prompt → nothing persists.
    runner = _settings_runner(tmp_path)
    _drive(
        runner,
        [
            ("Settings", lambda opts: next(o for o in opts if o.startswith("Sandbox"))),
            ("Sandbox", "Turn OFF"),
            ("Apply", lambda opts: next(o for o in opts if o.startswith("Global"))),
            ("Settings", lambda opts: next(o for o in opts if o.startswith("← Close"))),
            ("unsaved settings change", "No, discard them"),
        ],
    )
    runner._settings_menu()
    assert runner.global_config.source("sandbox") == "default"  # nothing written


def test_settings_menu_esc_in_editor_returns_to_list(tmp_path):
    # Esc in the value editor goes up ONE level (back to the list), it does NOT quit the menu.
    runner = _settings_runner(tmp_path)
    lists_shown = []

    def select(title, options, **k):
        if "Settings" in title:
            lists_shown.append(True)
            if len(lists_shown) == 1:
                return next(o for o in options if o.startswith("Sandbox"))  # open editor
            return next(o for o in options if o.startswith("← Close"))  # second visit → close
        return None  # Esc in the editor (and the empty close → no pending, just closes)

    runner._select_popup = select
    runner._settings_menu()
    assert lists_shown == [True, True]  # list re-shown after Esc in the editor
    assert runner.global_config.source("sandbox") == "default"  # nothing changed


def test_settings_menu_esc_in_scope_returns_to_value(tmp_path):
    # Esc at the scope prompt goes up one level — back to the value editor, not the list.
    runner = _settings_runner(tmp_path)
    value_prompts = []

    def select(title, options, **k):
        if "Settings" in title:
            if value_prompts:  # second visit to the list → close (no pending kept)
                return next(o for o in options if o.startswith("← Close"))
            return next(o for o in options if o.startswith("Default coding agent"))
        if "Apply" in title:  # the scope prompt embeds the label, so check it first
            return None  # Esc at scope → re-edit the value
        if "Default coding agent" in title:
            value_prompts.append(True)
            return "claude" if len(value_prompts) == 1 else "← Back"  # re-offered after scope Esc
        return None

    runner._select_popup = select
    runner._settings_menu()
    assert len(value_prompts) == 2  # value editor shown again after Esc at the scope prompt
    assert runner.global_config.source("default_backend") == "default"  # never saved


@posix_only
def test_settings_menu_edits_allowed_paths(tmp_path):
    runner = _settings_runner(tmp_path)
    _drive(
        runner,
        [
            ("Settings", lambda opts: next(o for o in opts if o.startswith("Folders/files"))),
            ("Folders/files", lambda default: "/data/shared:/srv/x"),
            ("Apply", lambda opts: next(o for o in opts if o.startswith("This repository"))),
            ("Settings", lambda opts: next(o for o in opts if o.startswith("← Close"))),
            ("unsaved settings change", "Yes, save them"),
            ("Restart aGiTrack now", "Not now"),  # allowed-paths is read at launch → restart offered
        ],
    )
    runner._settings_menu()
    # Saved to config; it takes effect on the next launch (no live runtime mutation).
    assert runner.global_config.allowed_edit_paths == ["/data/shared", "/srv/x"]


def test_settings_timings_submenu_saves(tmp_path):
    runner = _settings_runner(tmp_path)
    # Drive the whole menu so the close → save prompt actually writes the pending timing.
    _drive(
        runner,
        [
            ("Settings", lambda opts: next(o for o in opts if o.startswith("Polling & debounce"))),
            ("Timings", lambda opts: next(o for o in opts if o.startswith("file_stable_seconds"))),
            ("file_stable_seconds", lambda default: "12"),
            ("Apply", lambda opts: next(o for o in opts if o.startswith("Global"))),
            ("Timings", "← Back"),  # back to the settings list
            ("Settings", lambda opts: next(o for o in opts if o.startswith("← Close"))),
            ("unsaved settings change", "Yes, save them"),
            ("Restart aGiTrack now", "Not now"),  # timings are read at launch → restart offered
        ],
    )
    runner._settings_menu()
    assert runner.global_config.timings["file_stable_seconds"] == 12.0


def test_settings_restart_setting_offers_restart(tmp_path):
    # A restart-only setting (sandbox) offers a restart after saving; declining keeps running
    # and notes the change applies next launch.
    runner = _settings_runner(tmp_path)
    msgs: list[str] = []
    runner._set_message = lambda m, **k: msgs.append(m)
    restarted: list = []
    runner._restart_now = lambda msg: restarted.append(msg)
    _drive(
        runner,
        [
            ("Settings", lambda opts: next(o for o in opts if o.startswith("Sandbox"))),  # sandbox (restart)
            ("Sandbox", "Turn OFF"),
            ("Apply", lambda opts: next(o for o in opts if o.startswith("Global"))),
            ("Settings", lambda opts: next(o for o in opts if o.startswith("← Close"))),
            ("unsaved settings change", "Yes, save them"),
            ("Restart aGiTrack now", "Not now"),
        ],
    )
    runner._settings_menu()
    assert runner.global_config.sandbox is False  # the change was saved
    assert restarted == []  # declined → kept running
    assert any("next time you start aGiTrack" in m for m in msgs)


def test_settings_restart_setting_can_restart_now(tmp_path):
    # Accepting the restart offer triggers the re-exec path.
    runner = _settings_runner(tmp_path)
    restarted: list = []
    runner._restart_now = lambda msg: restarted.append(msg)
    _drive(
        runner,
        [
            ("Settings", lambda opts: next(o for o in opts if o.startswith("Sandbox"))),
            ("Sandbox", "Turn OFF"),
            ("Apply", lambda opts: next(o for o in opts if o.startswith("Global"))),
            ("Settings", lambda opts: next(o for o in opts if o.startswith("← Close"))),
            ("unsaved settings change", "Yes, save them"),
            ("Restart aGiTrack now", "Yes, restart now"),
        ],
    )
    runner._settings_menu()
    assert restarted  # _restart_now was invoked


def test_settings_live_setting_has_no_restart_warning(tmp_path):
    runner = _settings_runner(tmp_path)
    msgs: list[str] = []
    runner._set_message = lambda m, **k: msgs.append(m)
    _drive(
        runner,
        [
            # check_for_updates is read live, so changing it takes effect immediately.
            ("Settings", lambda opts: next(o for o in opts if o.startswith("Automatically check"))),
            ("Automatically check", "Turn OFF"),
            ("Apply", lambda opts: next(o for o in opts if o.startswith("Global"))),
            ("Settings", lambda opts: next(o for o in opts if o.startswith("← Close"))),
            ("unsaved settings change", "Yes, save them"),
        ],
    )
    runner._settings_menu()
    assert any("Saved" in m for m in msgs)
    assert not any("restart" in m.lower() for m in msgs)


def test_settings_menu_esc_on_list_with_pending_prompts_save(tmp_path):
    # Esc on the top list (rather than picking "← Close") still triggers the save prompt.
    runner = _settings_runner(tmp_path)

    def select(title, options, **k):
        if "Settings" in title and runner._pending_count() == 0:
            return next(o for o in options if o.startswith("Sandbox"))
        if "Sandbox" in title:
            return "Turn OFF"
        if "Apply" in title:
            return next(o for o in options if o.startswith("Global"))
        if "Settings" in title:  # back on the list with a pending change → press Esc
            return None
        if "unsaved settings change" in title:
            return "Yes, save them"
        return None

    runner._select_popup = select
    runner._settings_menu()
    assert runner.global_config.sandbox is False
    assert runner.global_config.source("sandbox") == "global"
