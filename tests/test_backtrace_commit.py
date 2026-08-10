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


@pytest.fixture(autouse=True)
def codex_home(tmp_path, monkeypatch):
    """Redirect Codex's store, as tests/test_codex_session.py does.

    Reconstruction walks every backend, so without this each test read the developer's (or the
    CI runner's) real ``~/.codex`` history — planting whatever conversations happened to be
    there into the repo the test had just built."""
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


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


def _plant_codex_session(codex_home, repo, session_id, *, prompt, path, content, when):
    """A Codex rollout for ``repo``, in the record shapes tests/test_codex_session.py captured
    from codex-cli 0.147.0. Unlike Claude, Codex files rollouts by DATE and links them to a
    directory only through the ``cwd`` in the header — so that header is what scopes it here."""
    rows = [
        {
            "timestamp": f"{when}.000Z",
            "type": "session_meta",
            "payload": {"session_id": session_id, "cwd": str(repo), "source": "cli", "thread_source": "user"},
        },
        {"timestamp": f"{when}.100Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
        {
            "timestamp": f"{when}.200Z",
            "type": "turn_context",
            "payload": {"turn_id": "t1", "model": "gpt-5.4-codex", "model_reasoning_effort": "medium"},
        },
        {"timestamp": f"{when}.300Z", "type": "event_msg", "payload": {"type": "user_message", "message": prompt}},
        {
            "timestamp": f"{when}.400Z",
            "type": "event_msg",
            "payload": {
                "type": "patch_apply_end",
                "success": True,
                "changes": {str(repo / path): {"type": "add", "content": content}},
            },
        },
        {
            "timestamp": f"{when}.500Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 40}},
            },
        },
        {
            "timestamp": f"{when}.600Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "t1", "last_agent_message": f"Done: {prompt}"},
        },
    ]
    directory = codex_home / "sessions" / "2026" / "07" / "02"
    directory.mkdir(parents=True, exist_ok=True)
    rollout = directory / f"rollout-2026-07-02T09-00-00-{session_id}.jsonl"
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return rollout


def test_a_codex_made_commit_is_annotated_from_its_rollout(tmp_path, monkeypatch, codex_home):
    """The same reconstruction, for a repo whose agent was Codex.

    Nothing here is Claude-shaped: the turn is bounded by Codex's own task_started/task_complete
    events, its file changes come from an applied patch rather than tool-call arguments, and the
    conversation is located by the cwd its rollout header records. A commit whose files that turn
    produced must still gain the same metadata and trace.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    from agitrack.metrics import backtrace as bt

    monkeypatch.setattr(bt.opencode, "sessions_under", lambda d: [])

    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("# Proj\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial: add README", date="2026-07-01T10:00:00+00:00")

    session_id = "019fe8dc-ca6c-7951-9225-73513aadf083"
    _plant_codex_session(
        codex_home,
        repo,
        session_id,
        prompt="Create calc.py",
        path="calc.py",
        content="def add(a, b):\n    return a + b\n",
        when="2026-07-02T09:00:00",
    )
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add calc module", date="2026-07-02T10:00:00+00:00")

    assert backtrace_commit(repo, "agitrack-history", _input=lambda _p: "y") == 0

    body = _git(repo, "log", "-1", "--format=%B", "agitrack-history").stdout
    assert "# aGiTrack Metadata" in body and "commit_type: agent" in body
    assert "backend: codex" in body and "model: gpt-5.4-codex" in body
    assert f"backend_session_id: {session_id}" in body
    assert "reasoning_effort: medium" in body
    assert "tokens_since_last_commit_output: 40" in body
    assert "# Interaction Trace" in body and "Create calc.py" in body
    assert body.splitlines()[0] == "add calc module"  # the user's own subject survives


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


def test_backtrace_commit_with_no_stdin_cancels_instead_of_crashing(repo_with_history, capsys):
    # Run from a script or CI (`agitrack --backtrace commit --branch x < /dev/null`) the
    # confirmation `input()` raises EOFError. It escaped as a raw Python traceback instead of a
    # message; and since the "yes" answer REWRITES history, no answer must mean no.
    def _eof(_prompt):
        raise EOFError

    before = _git(repo_with_history, "branch", "--format=%(refname:short)").stdout.split()
    rc = backtrace_commit(repo_with_history, "unattended", _input=_eof)

    assert rc == 0
    assert "Aborted" in capsys.readouterr().out
    assert _git(repo_with_history, "branch", "--format=%(refname:short)").stdout.split() == before


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


def _user_attribution_block() -> str:
    """The metadata aGiTrack stamps on a plain USER commit: attribution only — no turn behind it,
    no interaction trace, no token counts."""
    return (
        "\n\n# aGiTrack Metadata\n"
        "commit_type: user\n"
        "backend: agit\n"
        "agitrack_session_id: agitrack-23c1ae28\n"
        "system: macOS 15.7.3\n"
        "agitrack_version: 0.6.5\n"
    )


@pytest.fixture
def repo_ai_commit_at_head(tmp_path, monkeypatch):
    """A repo whose HEAD is the AI-made commit, so a test can re-stamp THAT commit with `--amend`.

    ``repo_with_history`` ends on a trailing user commit, and amending there silently rewrites the
    wrong commit — a stamp the reconstruction never reads, so the test passes without exercising
    anything. Keeping the AI commit at HEAD makes the stamp land where the assertion looks.
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
    return repo


def _restamp_head(repo, message: str) -> None:
    """Replace HEAD's message, leaving its tree and parents alone."""
    _git(repo, "commit", "--amend", "-m", message, "--only")
    assert message.splitlines()[0] in _git(repo, "log", "-1", "--format=%s").stdout


def test_a_plain_user_commit_does_not_make_the_repo_look_already_tracked(repo_ai_commit_at_head, capsys):
    """The reported dead end: someone runs aGiTrack, commits the agent's work THEMSELVES, and
    `--backtrace commit` then refuses with "every agent-made commit here is already tracked" —
    on the strength of an attribution block carrying no turn, no trace and no tokens. That is
    precisely the new user this command exists for, so it must annotate, not decline.
    """
    repo = repo_ai_commit_at_head
    _restamp_head(repo, "add calc module" + _user_attribution_block())

    assert backtrace_commit(repo, "recon", _input=lambda _p: "y") == 0

    out = capsys.readouterr().out
    assert "Nothing to add" not in out
    assert "1 agent commit(s) NOT yet tracked will gain aGiTrack metadata" in out
    body = _git(repo, "log", "recon", "--format=%B", "-1").stdout
    assert "# Interaction Trace" in body and "Create calc.py" in body
    assert "tokens_since_last_commit_output:" in body


def test_the_hollow_user_block_is_replaced_not_left_beside_the_real_record(repo_ai_commit_at_head):
    """Two metadata blocks would read as a SQUASH aggregate whose user constituent carries
    nothing, so the attribution-only block is dropped when the real record goes in."""
    repo = repo_ai_commit_at_head
    _restamp_head(repo, "add calc module" + _user_attribution_block())

    assert backtrace_commit(repo, "recon", _input=lambda _p: "y") == 0

    body = _git(repo, "log", "recon", "--format=%B", "-1").stdout
    assert body.count("# aGiTrack Metadata") == 1
    assert "commit_type: agent" in body and "commit_type: user" not in body
    assert body.lstrip().startswith("add calc module")  # the user's own subject survives


def test_a_genuinely_tracked_agent_commit_is_still_left_alone(repo_ai_commit_at_head, capsys):
    """The relaxation must not go the other way. A commit that already RECORDS AI work (trace and
    real token counts) stays untouched — re-annotating it would print the same conversation twice
    and double-count its tokens. The anchor differs from the planted turn's, so the turn-level
    dedup can't be what spares it: only `carries_ai_history` can.
    """
    repo = repo_ai_commit_at_head
    _restamp_head(repo, _agitrack_commit_message("add calc module", session_id="other", anchor="other", output=416))

    assert backtrace_commit(repo, "recon", _input=lambda _p: "y") == 0

    # "Nothing to add" is still reachable — for the case it was always meant for. Nothing is
    # rewritten, so the existing record cannot be doubled.
    assert "Nothing to add: every agent-made commit here is already tracked" in capsys.readouterr().out
    assert "recon" not in _git(repo, "branch", "--format=%(refname:short)").stdout.split()
    assert _git(repo, "log", "-1", "--format=%B").stdout.count("tokens_since_last_commit_output:") == 1
