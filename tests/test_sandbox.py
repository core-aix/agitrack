import shutil
import subprocess
import sys

import pytest

from agitrack.proxy import sandbox


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
def test_sandbox_exec_blocks_base_and_siblings_allows_self(tmp_path):
    base = tmp_path / "repo"
    (base / ".git").mkdir(parents=True)
    s1 = base / ".agitrack" / "worktrees" / "s1"
    s2 = base / ".agitrack" / "worktrees" / "s2"
    s1.mkdir(parents=True)
    s2.mkdir(parents=True)
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


@pytest.mark.skipif(not sandbox._bwrap_works(), reason="bubblewrap unavailable / unprivileged userns blocked")
def test_bwrap_blocks_base_and_siblings_allows_self(tmp_path):
    base = tmp_path / "repo"
    (base / ".git").mkdir(parents=True)
    s1 = base / ".agitrack" / "worktrees" / "s1"
    s2 = base / ".agitrack" / "worktrees" / "s2"
    s1.mkdir(parents=True)
    s2.mkdir(parents=True)
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
