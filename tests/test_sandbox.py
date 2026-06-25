import os
import shutil
import subprocess
import sys

import pytest

from agitrack.proxy import sandbox

_posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="sandbox-exec is macOS-only; select.select on pipes is POSIX-only"
)


def test_wrap_command_disabled_via_env(monkeypatch):
    monkeypatch.setenv("AGITRACK_SANDBOX", "0")
    command = ["claude", "--resume", "x"]
    assert sandbox.wrap_command(command, base="/repo", worktree="/repo/.agitrack/worktrees/s1") is command


def test_wrap_command_noop_when_worktree_is_base(monkeypatch, tmp_path):
    monkeypatch.delenv("AGITRACK_SANDBOX", raising=False)
    monkeypatch.setattr(sandbox, "_have_sandbox_exec", lambda: True)
    command = ["claude"]
    assert sandbox.wrap_command(command, base=str(tmp_path), worktree=str(tmp_path)) == command


def test_wrap_command_noop_when_no_mechanism(monkeypatch, tmp_path):
    # bwrap/sandbox-exec both unavailable -> caller falls back to warn-on-edit.
    monkeypatch.delenv("AGITRACK_SANDBOX", raising=False)
    monkeypatch.setattr(sandbox, "_have_sandbox_exec", lambda: False)
    monkeypatch.setattr(sandbox, "_have_bwrap", lambda: False)
    base = tmp_path / "repo"
    wt = base / ".agitrack" / "worktrees" / "s1"
    wt.mkdir(parents=True)
    command = ["claude", "-r", "x"]
    assert sandbox.wrap_command(command, base=str(base), worktree=str(wt)) is command


@_posix_only
def test_wrap_command_wraps_with_sandbox_exec(monkeypatch, tmp_path):
    monkeypatch.delenv("AGITRACK_SANDBOX", raising=False)
    monkeypatch.setattr(sandbox, "_have_sandbox_exec", lambda: True)
    base = tmp_path / "repo"
    wt = base / ".agitrack" / "worktrees" / "s1"
    wt.mkdir(parents=True)
    wrapped = sandbox.wrap_command(["claude", "-r", "x"], base=str(base), worktree=str(wt))
    assert wrapped[0] == "sandbox-exec" and wrapped[1] == "-p"
    assert wrapped[-3:] == ["claude", "-r", "x"]
    profile = wrapped[2]
    assert "(allow default)" in profile
    assert "(deny file-write*" in profile and str(wt.resolve()) in profile


def test_wrap_command_wraps_with_bwrap(monkeypatch, tmp_path):
    # No sandbox-exec (Linux), bwrap usable -> bubblewrap prefix.
    monkeypatch.delenv("AGITRACK_SANDBOX", raising=False)
    monkeypatch.setattr(sandbox, "_have_sandbox_exec", lambda: False)
    monkeypatch.setattr(sandbox, "_have_bwrap", lambda: True)
    base = tmp_path / "repo"
    (base / ".git").mkdir(parents=True)
    wt = base / ".agitrack" / "worktrees" / "s1"
    wt.mkdir(parents=True)
    wrapped = sandbox.wrap_command(["claude", "-r", "x"], base=str(base), worktree=str(wt))
    assert wrapped[0] == "bwrap"
    assert wrapped[-3:] == ["claude", "-r", "x"]
    assert wrapped[wrapped.index("--") + 1 :] == ["claude", "-r", "x"]


@_posix_only
def test_build_profile_denies_siblings_allows_this_worktree(tmp_path):
    base = tmp_path / "repo"
    root = base / ".agitrack" / "worktrees"
    (root / "s1").mkdir(parents=True)
    profile = sandbox.build_profile(str(base), str(root / "s1"))
    lines = profile.splitlines()
    # Both the base and the worktrees root are denied; .git and *this* worktree
    # are re-allowed (after the denies, so they win).
    assert any("deny" in ln and str(root.resolve()) in ln for ln in lines)
    deny_root = next(i for i, ln in enumerate(lines) if "deny" in ln and str(root.resolve()) in ln)
    allow_wt = next(i for i, ln in enumerate(lines) if "allow" in ln and str((root / "s1").resolve()) in ln)
    assert allow_wt > deny_root  # later rule wins


def test_build_bwrap_command_orders_binds(tmp_path):
    base = tmp_path / "repo"
    (base / ".git").mkdir(parents=True)
    wt = base / ".agitrack" / "worktrees" / "s1"
    wt.mkdir(parents=True)
    args = sandbox.build_bwrap_command(str(base), str(wt))
    assert args[0] == "bwrap"
    assert args[-1] == "--"
    # The base read-only bind must precede the .git/worktree read-write re-binds,
    # otherwise the read-only base would shadow them.
    ro_base = args.index(str(base.resolve()))  # first occurrence: the --ro-bind src
    bind_wt = args.index(str(wt.resolve()))
    assert args[ro_base - 1] == "--ro-bind"
    assert ro_base < bind_wt
    # .git is re-bound read-write, and the sandbox chdirs into the worktree.
    assert "--bind" in args and str((base / ".git").resolve()) in args
    assert args[args.index("--chdir") + 1] == str(wt.resolve())


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("sandbox-exec"), reason="sandbox-exec is macOS-only")
def test_sandbox_exec_blocks_base_and_siblings_allows_self(tmp_path, monkeypatch):
    base = tmp_path / "repo"
    (base / ".git").mkdir(parents=True)
    s1 = base / ".agitrack" / "worktrees" / "s1"
    s2 = base / ".agitrack" / "worktrees" / "s2"
    s1.mkdir(parents=True)
    s2.mkdir(parents=True)
    agent_dir = base / "vendor" / ".claude"  # a backend installed under the repo
    agent_dir.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "agent_writable_dirs", lambda: [str(agent_dir.resolve())])
    profile = sandbox.build_profile(str(base), str(s1))

    # Skip when we can't create a nested sandbox (e.g. the suite is itself running
    # inside agit's own confinement — macOS forbids nesting, so even an allow-all
    # profile fails). That's an environment limit, not a behaviour under test.
    probe = base / "probe"
    if (
        subprocess.run(
            ["sandbox-exec", "-p", "(version 1)(allow default)", "/bin/sh", "-c", f"echo ok > {probe}"],
            capture_output=True,
        ).returncode
        != 0
    ):
        pytest.skip("nested sandbox-exec unavailable (already sandboxed)")

    def write(path) -> int:
        return subprocess.run(
            ["sandbox-exec", "-p", profile, "/bin/sh", "-c", f"echo hi > {path}"],
            capture_output=True,
        ).returncode

    assert write(base / "proxy.py") != 0  # base source: denied
    assert write(s2 / "edit.py") != 0  # another session's worktree: denied
    assert write(s1 / "edit.py") == 0  # this session's worktree: allowed
    assert write(base / ".git" / "x") == 0  # git internals: allowed
    assert write(agent_dir / "update") == 0  # backend self-update under the repo: allowed


@pytest.mark.skipif(not sandbox._bwrap_works(), reason="bubblewrap unavailable / unprivileged userns blocked")
def test_bwrap_blocks_base_and_siblings_allows_self(tmp_path, monkeypatch):
    base = tmp_path / "repo"
    (base / ".git").mkdir(parents=True)
    s1 = base / ".agitrack" / "worktrees" / "s1"
    s2 = base / ".agitrack" / "worktrees" / "s2"
    s1.mkdir(parents=True)
    s2.mkdir(parents=True)
    agent_dir = base / "vendor" / ".claude"  # a backend installed under the repo
    agent_dir.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "agent_writable_dirs", lambda: [str(agent_dir.resolve())])
    prefix = sandbox.build_bwrap_command(str(base), str(s1))

    def write(path) -> int:
        return subprocess.run(
            [*prefix, "/bin/sh", "-c", f"echo hi > {path}"],
            capture_output=True,
        ).returncode

    assert write(base / "proxy.py") != 0  # base source: denied (read-only)
    assert write(s2 / "edit.py") != 0  # another session's worktree: denied
    assert write(s1 / "edit.py") == 0  # this session's worktree: allowed
    assert write(base / ".git" / "x") == 0  # git internals: allowed
    assert write(agent_dir / "update") == 0  # backend self-update under the repo: allowed


# ---------------------------------------------------------------------------
# Backend agent self-update carve-out
# ---------------------------------------------------------------------------


def test_agent_writable_dirs_covers_known_roots(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    # Windows uses USERPROFILE (not HOME) for Path.home() / os.path.expanduser("~").
    if sys.platform == "win32":
        monkeypatch.setenv("USERPROFILE", str(home))
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)

    dirs = sandbox.agent_writable_dirs()
    # Both backends' install/config/state roots are present (realpath-resolved).
    assert str((home / ".claude").resolve()) in dirs
    assert str(home / ".opencode") in dirs
    assert str(home / ".local" / "share" / "claude") in dirs
    assert str(home / ".local" / "state" / "opencode") in dirs
    assert str(home / ".local" / "bin") in dirs  # native launcher symlink dir
    # No bare "/" ever, and no duplicates.
    assert os.sep not in [d for d in dirs if d == os.sep]
    assert len(dirs) == len(set(dirs))


def test_agent_writable_dirs_follows_resolved_executable(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # claude resolved like the native install: a launcher symlink -> versioned binary.
    install = home / "tools" / "claude" / "versions" / "9.9.9"
    install.parent.mkdir(parents=True)
    install.write_text("#!/bin/sh\n")
    launcher = home / ".local" / "bin" / "claude"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(install)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: str(launcher) if name == "claude" else None)

    dirs = sandbox.agent_writable_dirs()
    assert str(launcher.parent.resolve()) in dirs  # the launcher dir
    assert str(install.parent.resolve()) in dirs  # the dir the launcher resolves into


@_posix_only
def test_build_profile_allows_agent_update_dir_under_repo(monkeypatch, tmp_path):
    # An agent installed *under* the base repo must still be writable for updates:
    # its allow rule has to come after the base deny so it wins.
    base = tmp_path / "repo"
    wt = base / ".agitrack" / "worktrees" / "s1"
    wt.mkdir(parents=True)
    agent_dir = base / "vendor" / ".claude"
    agent_dir.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "agent_writable_dirs", lambda: [str(agent_dir.resolve())])

    lines = sandbox.build_profile(str(base), str(wt)).splitlines()
    deny_base = next(i for i, ln in enumerate(lines) if "deny" in ln and str(base.resolve()) in ln)
    allow_agent = next(i for i, ln in enumerate(lines) if "allow" in ln and str(agent_dir.resolve()) in ln)
    assert allow_agent > deny_base  # later rule wins -> updates allowed


@_posix_only
def test_build_profile_allows_user_allowed_paths(tmp_path):
    # A user-specified allowed path is re-allowed for writes after the base deny, even
    # if it doesn't exist yet (subpath rules cover not-yet-created files).
    base = tmp_path / "repo"
    wt = base / ".agitrack" / "worktrees" / "s1"
    wt.mkdir(parents=True)
    allowed = base / "data" / "shared"  # under the base (the interesting case), not created
    lines = sandbox.build_profile(str(base), str(wt), [str(allowed)]).splitlines()
    deny_base = next(i for i, ln in enumerate(lines) if "deny" in ln and str(base.resolve()) in ln)
    allow_user = next(i for i, ln in enumerate(lines) if "allow" in ln and str(os.path.realpath(allowed)) in ln)
    assert allow_user > deny_base  # later rule wins → writes allowed


def test_build_bwrap_command_binds_existing_allowed_path_under_base(tmp_path):
    base = tmp_path / "repo"
    (base / ".git").mkdir(parents=True)
    wt = base / ".agitrack" / "worktrees" / "s1"
    wt.mkdir(parents=True)
    allowed = base / "data"
    allowed.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    args = sandbox.build_bwrap_command(str(base), str(wt), [str(allowed), str(outside)])
    # Under-base allowed dir is re-bound read-write; an outside one is already RW (not bound).
    ro_base = args.index(str(base.resolve()))
    bind_allowed = args.index(str(allowed.resolve()))
    assert args[bind_allowed - 1] == "--bind" and bind_allowed > ro_base
    assert str(outside.resolve()) not in args


def test_build_bwrap_command_rebinds_agent_dir_under_base(monkeypatch, tmp_path):
    base = tmp_path / "repo"
    (base / ".git").mkdir(parents=True)
    wt = base / ".agitrack" / "worktrees" / "s1"
    wt.mkdir(parents=True)
    under = base / "vendor" / ".claude"
    under.mkdir(parents=True)
    outside = tmp_path / "home" / ".opencode"
    outside.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "agent_writable_dirs", lambda: [str(under.resolve()), str(outside.resolve())])

    args = sandbox.build_bwrap_command(str(base), str(wt))
    # The under-base agent dir is re-bound read-write after the read-only base bind.
    ro_base = args.index(str(base.resolve()))
    bind_under = args.index(str(under.resolve()))
    assert args[bind_under - 1] == "--bind" and bind_under > ro_base
    # The outside-base dir is already read-write via --dev-bind / / -> not re-bound.
    assert str(outside.resolve()) not in args


def test_build_bwrap_command_skips_missing_agent_dir(monkeypatch, tmp_path):
    base = tmp_path / "repo"
    (base / ".git").mkdir(parents=True)
    wt = base / ".agitrack" / "worktrees" / "s1"
    wt.mkdir(parents=True)
    missing = base / "vendor" / ".claude"  # under base but does not exist
    monkeypatch.setattr(sandbox, "agent_writable_dirs", lambda: [str(missing)])
    # bwrap errors on a missing bind source, so a non-existent agent dir is skipped.
    args = sandbox.build_bwrap_command(str(base), str(wt))
    assert str(missing) not in args
