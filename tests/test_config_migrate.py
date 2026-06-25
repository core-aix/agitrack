"""Tests for agitrack/config/migrate.py.

All tests use tmp_path — no real git repo or network needed.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

from agitrack.config.migrate import (
    LEGACY_STATE_DIRNAME,
    STATE_DIRNAME,
    migrate_global_config,
    migrate_repo_state,
)


# ---------------------------------------------------------------------------
# migrate_repo_state
# ---------------------------------------------------------------------------


def _fake_repo(root: Path):
    repo = MagicMock()
    repo.repo = root
    return repo


def test_migrate_repo_state_no_op_when_new_dir_already_exists(tmp_path):
    (tmp_path / STATE_DIRNAME).mkdir()
    (tmp_path / LEGACY_STATE_DIRNAME).mkdir()
    assert migrate_repo_state(_fake_repo(tmp_path)) is False


def test_migrate_repo_state_no_op_when_no_legacy_dir(tmp_path):
    assert migrate_repo_state(_fake_repo(tmp_path)) is False


def test_migrate_repo_state_renames_legacy_to_current(tmp_path):
    legacy = tmp_path / LEGACY_STATE_DIRNAME
    legacy.mkdir()
    (legacy / "state.json").write_text("{}")
    repo = _fake_repo(tmp_path)
    assert migrate_repo_state(repo) is True
    assert (tmp_path / STATE_DIRNAME).exists()
    assert not legacy.exists()


def test_migrate_repo_state_calls_repair_worktrees(tmp_path):
    legacy = tmp_path / LEGACY_STATE_DIRNAME
    legacy.mkdir()
    worktrees_dir = legacy / "worktrees" / "sess-1"
    worktrees_dir.mkdir(parents=True)
    repo = _fake_repo(tmp_path)
    migrate_repo_state(repo)
    repo.repair_worktrees.assert_called_once()
    args = repo.repair_worktrees.call_args[0]
    assert any("sess-1" in a for a in args)


def test_migrate_repo_state_rename_oserror_returns_false(tmp_path):
    legacy = tmp_path / LEGACY_STATE_DIRNAME
    legacy.mkdir()
    repo = _fake_repo(tmp_path)
    with patch.object(Path, "rename", side_effect=OSError("cross-device")):
        result = migrate_repo_state(repo)
    assert result is False
    assert not (tmp_path / STATE_DIRNAME).exists()


def test_migrate_repo_state_repair_exception_swallowed(tmp_path):
    legacy = tmp_path / LEGACY_STATE_DIRNAME
    legacy.mkdir()
    repo = _fake_repo(tmp_path)
    repo.repair_worktrees.side_effect = RuntimeError("repair failed")
    result = migrate_repo_state(repo)
    # Migration itself still returns True — the repair failure is silent
    assert result is True
    assert (tmp_path / STATE_DIRNAME).exists()


def test_migrate_repo_state_no_worktrees_dir_ok(tmp_path):
    legacy = tmp_path / LEGACY_STATE_DIRNAME
    legacy.mkdir()
    repo = _fake_repo(tmp_path)
    result = migrate_repo_state(repo)
    assert result is True
    repo.repair_worktrees.assert_called_once_with()  # called with no paths


# ---------------------------------------------------------------------------
# migrate_global_config
# ---------------------------------------------------------------------------


def test_migrate_global_config_no_op_when_new_dir_exists(tmp_path):
    new_dir = tmp_path / ".agitrack"
    new_dir.mkdir()
    assert migrate_global_config(new_dir) is False


def test_migrate_global_config_no_op_when_no_legacy(tmp_path):
    new_dir = tmp_path / ".agitrack"
    assert migrate_global_config(new_dir) is False


def test_migrate_global_config_copies_legacy(tmp_path):
    legacy = tmp_path / LEGACY_STATE_DIRNAME
    legacy.mkdir()
    (legacy / "config.json").write_text('{"k": "v"}')
    new_dir = tmp_path / STATE_DIRNAME
    assert migrate_global_config(new_dir) is True
    assert new_dir.exists()
    assert (new_dir / "config.json").read_text() == '{"k": "v"}'
    # Legacy is NOT removed — it's a copy
    assert legacy.exists()


def test_migrate_global_config_oserror_returns_false(tmp_path):
    legacy = tmp_path / LEGACY_STATE_DIRNAME
    legacy.mkdir()
    new_dir = tmp_path / STATE_DIRNAME
    with patch("agitrack.config.migrate.shutil.copytree", side_effect=OSError("permission")):
        result = migrate_global_config(new_dir)
    assert result is False
    assert not new_dir.exists()
