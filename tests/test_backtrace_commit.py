"""Tests for ``agitrack --backtrace commit``: replaying a repo's history onto a new branch and
annotating the AI-made commits with reconstructed aGiTrack metadata, all from local transcripts.

The happy path builds a real git repo plus a planted Claude transcript and checks that only the
AI-made commit is annotated (trace + metadata), user commits are byte-for-byte unchanged, and the
trees/authors are preserved. The rest cover the safety guards.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest

from agitrack.metrics.backtrace_commit import backtrace_commit


def _git(repo, *args, date=None):
    env = {
        "GIT_AUTHOR_NAME": "Dev",
        "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "Dev",
        "GIT_COMMITTER_EMAIL": "dev@example.com",
    }
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    import os

    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, env={**os.environ, **env})


def _plant_claude_session(claude_home, repo, session_id, rows):
    project = claude_home / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(repo.resolve()))
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{session_id}.jsonl").write_text("\n".join(json.dumps(r) for r in rows))


def _write_edit_turn(repo, prompt, path, before, after, when, *, uid, mid):
    tool = (
        {"name": "Write", "input": {"file_path": str(repo / path), "content": after}}
        if before is None
        else {"name": "Edit", "input": {"file_path": str(repo / path), "old_string": before, "new_string": after}}
    )
    return [
        {
            "type": "user",
            "uuid": f"u{uid}",
            "cwd": str(repo),
            "timestamp": f"{when}.000Z",
            "message": {"role": "user", "content": prompt},
        },
        {
            "type": "assistant",
            "uuid": f"a{uid}",
            "cwd": str(repo),
            "timestamp": f"{when}.500Z",
            "message": {
                "id": mid,
                "role": "assistant",
                "model": "claude-opus-4-8",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 40},
                "content": [{"type": "tool_use", "id": f"t{uid}", **tool}, {"type": "text", "text": f"Done: {prompt}"}],
            },
        },
    ]


@pytest.fixture
def repo_with_history(tmp_path, monkeypatch):
    """A git repo with a user commit, an AI-made commit (backed by a planted transcript), and a
    second user commit — plus an isolated CLAUDE_CONFIG_DIR."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    # OpenCode discovery would shell out; keep this test filesystem-only.
    from agitrack.metrics import backtrace as bt

    monkeypatch.setattr(bt.opencode, "sessions_under", lambda d: [])

    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("# Proj\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial: add README", date="2026-07-01T10:00:00+00:00")

    rows = _write_edit_turn(
        repo,
        "Create calc.py",
        "calc.py",
        None,
        "def add(a, b):\n    return a + b\n",
        "2026-07-02T09:00:00",
        uid=1,
        mid="m1",
    )
    _plant_claude_session(home / ".claude", repo, "sess-aaa", rows)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add calc module", date="2026-07-02T10:00:00+00:00")

    (repo / "README.md").write_text("# Proj\n\nA calculator.\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "docs: describe", date="2026-07-03T08:00:00+00:00")
    return repo


def test_backtrace_commit_annotates_only_ai_commit(repo_with_history):
    repo = repo_with_history
    rc = backtrace_commit(repo, "agitrack-history", _input=lambda p: "y")
    assert rc == 0

    # A new branch exists, current branch switched to it, main untouched.
    branches = _git(repo, "branch", "--format=%(refname:short)").stdout.split()
    assert "agitrack-history" in branches and "main" in branches
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "agitrack-history"

    entries = {
        s.split(" ", 1)[1]: s.split(" ", 1)[0]
        for s in _git(repo, "log", "--format=%H %s", "agitrack-history").stdout.splitlines()
    }
    calc_msg = _git(repo, "log", "-1", "--format=%B", entries["add calc module"]).stdout

    # The AI commit gains the full aGiTrack metadata + trace, reconstructed from the transcript.
    assert "# aGiTrack Metadata" in calc_msg and "commit_type: agent" in calc_msg
    assert "backend: claude" in calc_msg and "model: claude-opus-4-8" in calc_msg
    assert "backend_session_id: sess-aaa" in calc_msg
    assert "tokens_since_last_commit_output: 40" in calc_msg
    assert "# Interaction Trace" in calc_msg and "Create calc.py" in calc_msg
    # ...and keeps its original subject line.
    assert calc_msg.splitlines()[0] == "add calc module"

    # User commits are byte-for-byte unchanged (no metadata added).
    assert _git(repo, "log", "-1", "--format=%B", entries["docs: describe"]).stdout.strip() == "docs: describe"
    assert "aGiTrack" not in _git(repo, "log", "-1", "--format=%B", entries["initial: add README"]).stdout

    # Content is preserved: the reconstructed tip has the same tree as the original branch tip.
    assert (
        _git(repo, "rev-parse", "agitrack-history^{tree}").stdout.strip()
        == _git(repo, "rev-parse", "main^{tree}").stdout.strip()
    )
    # Author identity + date preserved on the rewritten commit.
    assert "2026-07-02T10:00:00" in _git(repo, "log", "-1", "--format=%aI", entries["add calc module"]).stdout


def test_backtrace_commit_dashboard_sees_the_ai_commit(repo_with_history):
    from agitrack.git import GitRepo
    from agitrack.metrics.collect import build_dashboard

    backtrace_commit(repo_with_history, "tracked", _input=lambda p: "y")
    dash = build_dashboard(GitRepo.discover(repo_with_history), "tracked")
    assert dash.count("agent") == 1
    assert "claude-opus-4-8" in dash.by_model


def test_backtrace_commit_requires_git_repo(tmp_path, capsys):
    rc = backtrace_commit(tmp_path, "newb", _input=lambda p: "y")
    assert rc == 1
    assert "not a git repository" in capsys.readouterr().out


def test_backtrace_commit_requires_clean_tree(repo_with_history, capsys):
    (repo_with_history / "calc.py").write_text("dirty\n")
    rc = backtrace_commit(repo_with_history, "newb", _input=lambda p: "y")
    assert rc == 1
    assert "uncommitted changes" in capsys.readouterr().out


def test_a_missing_branch_name_is_ASKED_for_not_a_dead_end(repo_with_history):
    """Printing a flag and exiting was a trap: `parse_known_args` funnels an unknown option to
    the BACKEND rather than erroring, so a user who mistyped the flag (or copied the `--branch`
    spelling the message itself printed, which did not exist) got the identical message back
    forever with nothing to say why. The command is interactive anyway, so it asks."""
    answers = iter(["reconstructed", "y"])  # branch name, then the rewrite confirmation
    rc = backtrace_commit(repo_with_history, "", _input=lambda _p: next(answers))

    assert rc == 0
    branches = _git(repo_with_history, "branch", "--format=%(refname:short)").stdout.split()
    assert "reconstructed" in branches  # the name typed at the prompt was used


def test_cancelling_the_branch_prompt_changes_nothing(repo_with_history, capsys):
    before = _git(repo_with_history, "branch", "--format=%(refname:short)").stdout.split()

    rc = backtrace_commit(repo_with_history, "", _input=lambda _p: "")  # blank = cancel

    assert rc == 1
    assert "Cancelled" in capsys.readouterr().out
    assert _git(repo_with_history, "branch", "--format=%(refname:short)").stdout.split() == before


def test_backtrace_commit_rejects_existing_branch(repo_with_history, capsys):
    _git(repo_with_history, "branch", "taken")
    rc = backtrace_commit(repo_with_history, "taken", _input=lambda p: "y")
    assert rc == 1
    assert "already exists" in capsys.readouterr().out


def test_backtrace_commit_declined_makes_no_changes(repo_with_history, capsys):
    before = _git(repo_with_history, "branch", "--format=%(refname:short)").stdout.split()
    rc = backtrace_commit(repo_with_history, "declined", _input=lambda prompt: "n")
    assert rc == 0
    assert "Aborted" in capsys.readouterr().out
    after = _git(repo_with_history, "branch", "--format=%(refname:short)").stdout.split()
    assert before == after  # no branch created


def test_backtrace_commit_no_ai_history(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    from agitrack.metrics import backtrace as bt

    monkeypatch.setattr(bt.opencode, "sessions_under", lambda d: [])
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("hi\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    rc = backtrace_commit(repo, "newb", _input=lambda p: "y")
    assert rc == 0
    assert "No AI-made file changes" in capsys.readouterr().out


def _agitrack_commit_message(subject: str, *, session_id: str, anchor: str, output: int) -> str:
    """A commit message shaped like one aGiTrack itself wrote — trace plus the metadata that
    records which session and which turn it covered."""
    return (
        f"<aGiTrack> {subject}\n\n"
        "# Interaction Trace\n\n## User\n\nCreate calc.py\n\n## Agent\n\nDone.\n\n"
        "# aGiTrack Metadata\n"
        "commit_type: agent\n"
        "backend: claude\n"
        f"backend_session_id: {session_id}\n"
        f"conversation_anchor: {anchor}\n"
        f"tokens_since_last_commit_output: {output}\n"
    )


def test_turns_already_committed_by_agitrack_are_not_counted_again(tmp_path, monkeypatch, capsys):
    """The repo this command exists for is one that used aGiTrack for PART of its life.

    A turn aGiTrack already committed carries its trace and its token counts in that commit.
    Re-attributing it here would print the same conversation twice in one history and count
    the same tokens twice — and file overlap alone will happily land it on a later, untracked
    commit, so "the tracked commit is skipped" is not enough on its own.
    """
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    from agitrack.metrics import backtrace as bt

    monkeypatch.setattr(bt.opencode, "sessions_under", lambda d: [])

    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("# Proj\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial", date="2026-07-01T10:00:00+00:00")

    # Turn 1 (m1) was committed BY aGiTrack; turn 2 (m2) touches the same file and was not.
    rows = _write_edit_turn(
        repo,
        "Create calc.py",
        "calc.py",
        None,
        "def add(a, b):\n    return a + b\n",
        "2026-07-02T09:00:00",
        uid=1,
        mid="m1",
    ) + _write_edit_turn(
        repo,
        "Add subtract",
        "calc.py",
        "def add(a, b):\n    return a + b\n",
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
        "2026-07-03T09:00:00",
        uid=2,
        mid="m2",
    )
    _plant_claude_session(home / ".claude", repo, "sess-aaa", rows)

    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(repo, "add", "-A")
    _git(
        repo,
        "commit",
        "-m",
        _agitrack_commit_message("add calc", session_id="sess-aaa", anchor="m1", output=40),
        date="2026-07-02T10:00:00+00:00",
    )
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add subtract", date="2026-07-03T10:00:00+00:00")

    assert backtrace_commit(repo, "reconstructed", _input=lambda _p: "y") == 0

    bodies = subprocess.run(
        ["git", "log", "--format=%B%x00", "reconstructed"], cwd=repo, text=True, capture_output=True
    ).stdout
    annotated = subprocess.run(
        ["git", "log", "-1", "--format=%B", "reconstructed"], cwd=repo, text=True, capture_output=True
    ).stdout
    # The untracked turn is annotated onto its own commit…
    assert "## User\n\nAdd subtract" in annotated
    # …and the turn aGiTrack already committed is NOT dragged along with it.
    assert "Create calc.py" not in annotated
    # Across the whole rewritten history each turn is traced exactly once: the aGiTrack
    # commit keeps its own trace, the new annotation adds only the untracked one.
    assert bodies.count("# Interaction Trace") == 2
    assert bodies.count("## User") == 2
    # And each turn's tokens are recorded once — the tracked commit's count is untouched.
    assert bodies.count("tokens_since_last_commit_output:") == 2


def test_a_forked_conversation_contributes_each_turn_once(tmp_path, monkeypatch):
    """Resuming or rewinding replays the earlier turns into a NEW session id, so the same
    turn is read from several transcripts. Keyed by the backend's own message id, only one
    copy survives — otherwise a trace would be appended several times over and its tokens
    summed once per copy."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    from agitrack.metrics import backtrace as bt

    monkeypatch.setattr(bt.opencode, "sessions_under", lambda d: [])

    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("# Proj\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial", date="2026-07-01T10:00:00+00:00")

    shared = _write_edit_turn(
        repo,
        "Create calc.py",
        "calc.py",
        None,
        "def add(a, b):\n    return a + b\n",
        "2026-07-02T09:00:00",
        uid=1,
        mid="m1",
    )
    _plant_claude_session(home / ".claude", repo, "sess-old", shared)
    _plant_claude_session(home / ".claude", repo, "sess-new", shared)  # the resumed fork replays it

    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add calc module", date="2026-07-02T10:00:00+00:00")

    assert backtrace_commit(repo, "reconstructed", _input=lambda _p: "y") == 0

    body = subprocess.run(
        ["git", "log", "-1", "--format=%B", "reconstructed"], cwd=repo, text=True, capture_output=True
    ).stdout
    assert body.count("# Interaction Trace") == 1
    assert body.count("## User") == 1  # one copy of the turn, not one per fork
    assert body.count("tokens_since_last_commit_output:") == 1
    assert "tokens_since_last_commit_output: 40" in body  # 40, not 80


# --- the CLI wiring: where the branch name actually got lost -----------------------------------


def _run_cli(argv, monkeypatch):
    """Invoke `agitrack …` through main(), capturing what reached backtrace_commit."""
    from agitrack import cli

    seen: dict = {}

    def fake(directory, branch, **kw):
        seen["directory"], seen["branch"] = directory, branch
        return 0

    monkeypatch.setattr("agitrack.metrics.backtrace_commit.backtrace_commit", fake)
    rc = cli.main(argv)
    return rc, seen


def test_the_branch_flag_actually_reaches_backtrace_commit(repo_with_history, monkeypatch):
    """THE regression. `parse_known_args` funnels unknown options to the BACKEND instead of
    erroring, so a branch flag that did not match silently vanished and `backtrace_commit` was
    called with "" — printing "give me a branch name" no matter what the user typed. Nothing
    covered the CLI wiring, so the whole feature was unusable from the command line."""
    rc, seen = _run_cli(
        ["--backtrace", "commit", "--backtrace-branch", "recon", "--repo", str(repo_with_history)], monkeypatch
    )

    assert rc == 0
    assert seen["branch"] == "recon"  # ...not ""


def test_the_legacy_branch_spelling_still_works(repo_with_history, monkeypatch):
    """`--branch` is what earlier guidance printed. It stays as an undocumented alias precisely
    because an unknown option here does NOT error — it would silently do nothing again."""
    rc, seen = _run_cli(["--backtrace", "commit", "--branch", "recon", "--repo", str(repo_with_history)], monkeypatch)

    assert rc == 0 and seen["branch"] == "recon"


def test_an_unrecognized_option_is_reported_not_swallowed(repo_with_history, monkeypatch, capsys):
    """The failure mode that made this unfixable from the outside: a mistyped option was collected
    as backend passthrough, so the user saw no error — just the same message forever."""
    _run_cli(
        ["--backtrace", "commit", "--backtrace-branch", "recon", "--brunch", "x", "--repo", str(repo_with_history)],
        monkeypatch,
    )

    assert "--brunch" in capsys.readouterr().out


def test_reconstructed_history_carries_tokens_so_the_dashboard_has_something(repo_with_history):
    """New-user payoff, end to end: after reconstructing, the repo is no longer "tracked but
    tokenless" — which is the exact condition that sends someone to the backtrace view."""
    from agitrack.git import GitRepo
    from agitrack.metrics import suggest

    repo = GitRepo.discover(repo_with_history)
    assert suggest.has_tracked_tokens(repo) is False  # nothing recorded yet

    assert backtrace_commit(repo_with_history, "recon", _input=lambda _p: "y") == 0

    assert suggest.has_tracked_tokens(repo) is True  # the reconstruction put real numbers in
