"""Tests for the proxy runner's routing menu dispatch and judge wiring.

These tests focus on the runner's surface: command palette, settings menu,
status-bar hint, and the run_command dispatch. The full integration with
real backend CLIs is exercised in test_routing_integration.py and the
backends-and-config tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch


from agitrack.config import GlobalConfig
from agitrack.config.state import AgitrackState
from agitrack.git import GitRepo
from proxy_helpers import make_runner


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


def _store(tmp_path: Path) -> tuple[Path, GitRepo]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    return repo, GitRepo.discover(repo)


# ---------------------------------------------------------------------------
# palette + command dispatch
# ---------------------------------------------------------------------------


def test_palette_contains_model_and_rate() -> None:
    """The Ctrl-G palette lists 'model' and 'rate' so the user can find
    the new commands by typing 'm' or 'r' (the prefix-by-letter shortcut
    the palette uses)."""
    from agitrack.proxy.runner import ProxyInput

    pi = ProxyInput()
    names = pi.matches()
    assert "model" in names
    assert "rate" in names


def test_palette_first_letter_shortcuts_work() -> None:
    """Pressing 'm' + Enter or 'r' + Enter in the palette jumps straight to
    the matching command (the same UX as the existing 'sessions' shortcut)."""
    from agitrack.proxy.runner import ProxyInput

    pi = ProxyInput()
    pi.buffer.extend(b"m")
    matches = pi.matches()
    assert matches[0] == "model"
    pi.buffer.clear()
    pi.buffer.extend(b"r")
    matches = pi.matches()
    assert matches[0] == "rate"


# ---------------------------------------------------------------------------
# runner helpers
# ---------------------------------------------------------------------------


def test_routing_helpers_return_false_when_disabled(tmp_path: Path) -> None:
    """When ``routing_mode`` is "off", the runner's routing helpers must
    short-circuit: no judge call, no router construction, no work."""
    repo, _git = _store(tmp_path)
    state = AgitrackState(repo)
    state.data["backend"] = "claude"
    state.data["model"] = "claude-opus-4-8"
    state.save()
    runner = make_runner(state=state, repo=_git, global_config=GlobalConfig())
    # Default config is routing_mode="off" (per the new defaults).
    assert runner._routing_enabled() is False
    assert runner._routing_judge_enabled() is True  # judge toggle is independent


def test_routing_enabled_in_suggest_mode(tmp_path: Path) -> None:
    repo, _git = _store(tmp_path)
    state = AgitrackState(repo)
    state.data["backend"] = "claude"
    state.data["model"] = "claude-haiku-4-5"
    state.save()
    config = GlobalConfig()
    config.routing_mode = "suggest"
    runner = make_runner(state=state, repo=_git, global_config=config)
    assert runner._routing_enabled() is True
    # With a real repo, the router facade should be constructible.
    router = runner._routing_router()
    assert router is not None


def test_routing_judge_can_be_disabled(tmp_path: Path) -> None:
    repo, _git = _store(tmp_path)
    state = AgitrackState(repo)
    state.data["backend"] = "claude"
    state.save()
    config = GlobalConfig()
    config.routing_judge = False
    runner = make_runner(state=state, repo=_git, global_config=config)
    assert runner._routing_judge_enabled() is False


def test_routing_router_uses_configured_pool(tmp_path: Path) -> None:
    """When the user has set a routing_pool, the router builds it from
    the config rather than auto-seeding."""
    repo, _git = _store(tmp_path)
    state = AgitrackState(repo)
    state.data["backend"] = "claude"
    state.data["model"] = "claude-haiku-4-5"
    state.save()
    config = GlobalConfig()
    config.routing_mode = "auto"
    config.routing_pool = [
        {"label": "haiku", "model": "claude-haiku-4-5", "tier": 1},
        {"label": "opus", "model": "claude-opus-4-8", "tier": 3},
    ]
    config.routing_exploration = 0.0
    runner = make_runner(state=state, repo=_git, global_config=config)
    router = runner._routing_router()
    assert router is not None
    pool = router.build_pool(available_models=[], current_model="claude-haiku-4-5")
    # The user-defined pool is the source of truth.
    models = {entry.model for entry in pool}
    assert "claude-haiku-4-5" in models
    assert "claude-opus-4-8" in models


# ---------------------------------------------------------------------------
# settings menu
# ---------------------------------------------------------------------------


def test_settings_specs_include_routing_keys() -> None:
    """The settings menu exposes every routing config key so the user can
    tune the router without editing JSON by hand."""
    repo_path = Path("/tmp/agitrack-routing-specs-test")
    repo_path.mkdir(exist_ok=True)
    runner = make_runner()
    specs = runner._settings_specs()
    keys = {spec["key"] for spec in specs}
    assert "routing_mode" in keys
    assert "routing_pool" in keys
    assert "routing_allow_cloud" in keys
    assert "routing_judge" in keys
    assert "routing_sync" in keys


def test_setting_value_display_routing_pool_friendly_summary(tmp_path: Path) -> None:
    """The pool's display is a one-line summary, not the raw JSON — a long
    pool would blow up the settings menu otherwise."""
    repo, _git = _store(tmp_path)
    state = AgitrackState(repo)
    state.data["backend"] = "claude"
    state.save()
    config = GlobalConfig()
    config.routing_pool = [
        {"label": "haiku", "model": "claude-haiku-4-5", "tier": 1},
        {"label": "opus", "model": "claude-opus-4-8", "tier": 3},
    ]
    runner = make_runner(state=state, repo=_git, global_config=config)
    spec = next(s for s in runner._settings_specs() if s["key"] == "routing_pool")
    display = runner._setting_value_display(spec)
    assert "haiku" in display
    assert "opus" in display
    # Tiers should appear as "t1" / "t3" so the user can read it at a glance.
    assert "t1" in display
    assert "t3" in display


# ---------------------------------------------------------------------------
# implicit signals
# ---------------------------------------------------------------------------


def test_record_routing_discard_swallows_errors(tmp_path: Path) -> None:
    """A failure inside the router (e.g. corrupt store) must NOT propagate
    out — the runner's contract is that routing never blocks a turn."""
    repo, _git = _store(tmp_path)
    state = AgitrackState(repo)
    state.data["backend"] = "claude"
    state.save()
    runner = make_runner(state=state, repo=_git, global_config=GlobalConfig())
    # Force the router to raise.
    with patch.object(runner, "_routing_router", return_value=None):
        # The helper should silently no-op when no router is available.
        runner._record_routing_discard()

    # And: a router that raises must also be tolerated.
    class _RaisingRouter:
        def record_discard(self, **_):
            raise RuntimeError("store corrupted")

    with patch.object(runner, "_routing_router", return_value=_RaisingRouter()):
        runner._record_routing_discard()  # no exception propagates


# ---------------------------------------------------------------------------
# model menu command
# ---------------------------------------------------------------------------


def test_model_command_dispatches_without_error(tmp_path: Path) -> None:
    """The Ctrl-G "model" command must dispatch cleanly even when the user
    cancels the popup (returns _MENU_UP). Verifies the dispatch path is
    wired end-to-end and doesn't raise on Esc."""
    repo, _git = _store(tmp_path)
    state = AgitrackState(repo)
    state.data["backend"] = "claude"
    state.data["model"] = "claude-opus-4-8"
    state.save()
    config = GlobalConfig()
    config.routing_mode = "auto"
    config.routing_pool = [
        {"label": "haiku", "model": "claude-haiku-4-5", "tier": 1},
        {"label": "opus", "model": "claude-opus-4-8", "tier": 3},
    ]
    runner = make_runner(state=state, repo=_git, global_config=config)
    # The user pressed Esc on the picker.
    with patch.object(runner, "_select_popup", return_value=None):
        result = runner._routing_model_menu()
    assert result == runner._MENU_UP
    # State.model should not have changed.
    assert state.model == "claude-opus-4-8"
