"""Which confinement mode is actually in effect has to be reportable (C11).

On stock Ubuntu 24.04 (`kernel.apparmor_restrict_unprivileged_userns=1`) bwrap cannot create a
sandbox, aGiTrack silently falls back to the advisory path, and NOTHING in the TUI, in `-s`, or
in the status bar said so — a whole run looked confined while a `!`-bash write into the base
checkout succeeded. What looked like enforcement was the agent choosing to comply.
"""

from __future__ import annotations

from agitrack.proxy import sandbox


def test_an_enforced_sandbox_says_which_mechanism(monkeypatch):
    monkeypatch.setattr(sandbox, "is_enabled", lambda: True)
    monkeypatch.setattr(sandbox, "is_available", lambda: True)
    monkeypatch.setattr(sandbox, "_have_sandbox_exec", lambda: True)

    line = sandbox.status_line()

    assert "enforced" in line
    assert "sandbox-exec" in line


def test_a_blocked_bwrap_says_confinement_is_only_advisory(monkeypatch):
    monkeypatch.setattr(sandbox, "is_enabled", lambda: True)
    monkeypatch.setattr(sandbox, "is_available", lambda: False)
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)

    line = sandbox.status_line()

    assert "ADVISORY only" in line
    assert "unprivileged user namespaces are denied" in line
    # ...and it is honest about what protection DOES remain.
    assert "committing into the base repo" in line


def test_a_missing_bwrap_is_reported_differently_from_a_blocked_one(monkeypatch):
    monkeypatch.setattr(sandbox, "is_enabled", lambda: True)
    monkeypatch.setattr(sandbox, "is_available", lambda: False)
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)

    line = sandbox.status_line()

    assert "not installed" in line


def test_confinement_turned_off_says_so(monkeypatch):
    monkeypatch.setattr(sandbox, "is_enabled", lambda: False)

    assert "off" in sandbox.status_line()


def test_status_reports_the_confinement_mode(tmp_path, monkeypatch, capsys):
    """The line has to reach the user, not just exist."""
    import subprocess

    from agitrack.git import GitRepo
    from agitrack.proxy.background import repo_status

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(cfg))
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    repo_status(GitRepo.discover(root))

    assert "Confinement:" in capsys.readouterr().out
