"""Back-compatibility for the aGiT → aGiTrack rename.

These guard the read/migration shims that must keep working after the rename:
existing on-disk state, historical commit metadata (old ``# aGiT Metadata`` /
``<aGiT>`` subjects still classify), and the old ``AGIT_*`` env vars. The ACTIVE
legacy machinery — the ``agit/`` branch prefix and the ``refs/agit/shared-sessions``
ref — has been removed; aGiTrack only writes/recognises ``agitrack/`` going forward.
"""

import subprocess
from importlib.metadata import entry_points

from agitrack.config import AgitrackState
from agitrack.config.migrate import migrate_global_config, migrate_repo_state
from agitrack.env import getenv_compat
from agitrack.git import GitRepo, is_managed_branch
from agitrack.metrics.collect import _parse_commit


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


# --- commit-metadata back-compat -------------------------------------------


def test_legacy_metadata_header_and_subject_are_still_tracked():
    # A commit written by the old aGiT carries `# aGiT Metadata` and a `<aGiT> `
    # subject; the dashboard must still classify it as agent work, not untracked.
    body = "<aGiT> fix the parser\n\n# aGiT Metadata\ncommit_type: agent\nbackend: claude\nmodel: opus\n"
    stat = _parse_commit("sha1", "me", "me@x", "0", body)
    assert stat.kind == "agent"
    assert stat.backend == "claude"
    assert stat.prompt == "fix the parser"  # the legacy subject prefix is stripped


def test_legacy_merge_branch_subject_classifies_as_ops():
    # An old auto-merge subject naming an `agit/...` turn branch is aGiTrack ops.
    body = "Merge branch 'main' into agit/claude/session-1/t2\n"
    assert _parse_commit("sha2", "me", "me@x", "0", body).kind == "agitrack-ops"


# --- branch-prefix back-compat ---------------------------------------------


def test_is_managed_branch_recognises_only_agitrack_prefix():
    # The legacy ``agit/`` active recognition was removed: aGiTrack manages only its own
    # ``agitrack/`` turn branches now. A historical ``agit/`` merge SUBJECT still classifies
    # as ops in the dashboard (a read shim, see above), but a live ``agit/`` branch is no
    # longer treated as managed for integration/cleanup.
    assert is_managed_branch("agitrack/claude/session-1/t1")
    assert not is_managed_branch("agit/claude/session-1/t1")
    assert not is_managed_branch("feature/x")
    assert not is_managed_branch("main")


# --- on-disk state migration -----------------------------------------------


def test_migrate_repo_state_moves_dir_and_repairs_worktree(tmp_path):
    repo = _init_repo(tmp_path)
    legacy = tmp_path / ".agit" / "worktrees" / "session-1"
    legacy.parent.mkdir(parents=True)
    # A real worktree registered under the legacy .agit path.
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(legacy)], cwd=tmp_path, check=True)
    (tmp_path / ".agit" / "state.json").write_text('{"backend": "claude"}', encoding="utf-8")

    assert migrate_repo_state(repo) is True
    assert (tmp_path / ".agitrack" / "state.json").exists()
    assert not (tmp_path / ".agit").exists()
    # The moved worktree still resolves (repair fixed the admin back-link).
    listing = subprocess.run(["git", "worktree", "list"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert ".agitrack/worktrees/session-1" in listing
    assert ".agit/worktrees" not in listing
    # Idempotent: a second run is a no-op.
    assert migrate_repo_state(repo) is False


def test_migrate_repo_state_no_op_when_new_dir_exists(tmp_path):
    repo = _init_repo(tmp_path)
    (tmp_path / ".agit").mkdir()
    (tmp_path / ".agitrack").mkdir()
    assert migrate_repo_state(repo) is False
    assert (tmp_path / ".agit").exists()  # left untouched


def test_migrate_global_config_copies_legacy(tmp_path):
    legacy = tmp_path / ".agit"
    legacy.mkdir()
    (legacy / "config.json").write_text('{"default_backend": "opencode"}', encoding="utf-8")
    new_dir = tmp_path / ".agitrack"

    assert migrate_global_config(new_dir) is True
    assert (new_dir / "config.json").read_text(encoding="utf-8") == '{"default_backend": "opencode"}'
    assert legacy.exists()  # copy, not move — old aGiT keeps working


def test_state_carries_legacy_session_id_key(tmp_path):
    # A pre-rename state.json keyed the id as `agit_session_id`; the value must be
    # kept (not regenerated) so the session's worktree/branches aren't orphaned.
    (tmp_path / ".agitrack").mkdir()
    (tmp_path / ".agitrack" / "state.json").write_text(
        '{"agit_session_id": "agit-keep-me", "backend": "claude"}', encoding="utf-8"
    )
    state = AgitrackState(tmp_path)
    assert state.session_id == "agit-keep-me"


# --- env-var back-compat ----------------------------------------------------


def test_getenv_compat_prefers_new_then_legacy(monkeypatch):
    monkeypatch.delenv("AGITRACK_SANDBOX", raising=False)
    monkeypatch.delenv("AGIT_SANDBOX", raising=False)
    assert getenv_compat("SANDBOX") is None

    monkeypatch.setenv("AGIT_SANDBOX", "off")  # legacy only
    assert getenv_compat("SANDBOX") == "off"

    monkeypatch.setenv("AGITRACK_SANDBOX", "on")  # new wins
    assert getenv_compat("SANDBOX") == "on"


# --- packaging back-compat --------------------------------------------------


def test_console_script_is_agitrack_only():
    # aGiTrack installs a single command, `agitrack`. The legacy `agit` alias has been
    # removed, so it must NOT be registered as a console script.
    scripts = {e.name: e.value for e in entry_points(group="console_scripts")}
    assert scripts.get("agitrack") == "agitrack.cli:main"
    assert "agit" not in scripts
