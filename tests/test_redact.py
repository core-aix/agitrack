"""`agitrack redact`: take interaction traces back out of history (agitrack/redact.py).

Every test runs against a REAL temp git repository: the command rewrites commit objects, and a
mock of `git` could not tell us whether the trees, the refs, the notes and the working tree come
out the way the docstring promises.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from agitrack import redact
from agitrack.commits.message import build_agent_commit_message, build_manual_squash_trailer
from agitrack.git import GitRepo

SECRET = "hunter2-the-password-i-pasted-by-mistake"
T0 = 1_790_000_000  # epoch seconds; the turns below are placed relative to it


def _init_repo(path: Path) -> GitRepo:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return GitRepo(path)


def _git(repo: GitRepo, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo.repo), *args], capture_output=True, text=True, check=True).stdout


def _agent_body(prompt: str, reply: str, *, started: int, ended: int) -> str:
    return build_agent_commit_message(
        latest_prompt=prompt,
        trace=[{"role": "user", "content": prompt}, {"role": "agent", "content": reply}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="m",
        token_usage={"input": 10, "output": 5},
        started_at=started,
        ended_at=ended,
    )


def _commit(repo: GitRepo, message: str, *, file: str, content: str) -> str:
    (repo.repo / file).write_text(content, encoding="utf-8")
    _git(repo, "add", file)
    subprocess.run(
        ["git", "-C", str(repo.repo), "commit", "-q", "--cleanup=verbatim", "-F", "-"],
        input=message,
        text=True,
        check=True,
    )
    return _git(repo, "rev-parse", "HEAD").strip()


def _full_log(repo: GitRepo) -> str:
    return _git(repo, "log", "--format=%B", "HEAD")


def test_a_commit_is_redacted_and_everything_but_its_messages_is_left_as_it_was(tmp_path):
    repo = _init_repo(tmp_path)
    leaky = _commit(
        repo,
        _agent_body(f"deploy it, the password is {SECRET}", "Deployed.", started=T0, ended=T0 + 60),
        file="b.txt",
        content="b\n",
    )
    after = _commit(
        repo, _agent_body("now add tests", "Added.", started=T0 + 120, ended=T0 + 180), file="c.txt", content="c\n"
    )
    trees_before = _git(repo, "log", "--format=%T", "HEAD").split()
    (repo.repo / "a.txt").write_text("uncommitted edit\n", encoding="utf-8")  # a dirty tree survives
    assert SECRET in _full_log(repo)

    assert redact.run(repo, commits=[leaky], since=None, until=None, assume_yes=True) == 0

    log = _full_log(repo)
    assert SECRET not in log
    assert "removed with `agitrack redact`" in log
    assert "now add tests" in log  # the later turn's trace is untouched
    # Same trees all the way down: no file changed, only messages and ids.
    assert _git(repo, "log", "--format=%T", "HEAD").split() == trees_before
    assert _git(repo, "rev-parse", "HEAD").strip() != after  # the descendant was re-parented
    assert (repo.repo / "a.txt").read_text(encoding="utf-8") == "uncommitted edit\n"
    # The subject was written from the trace (it IS the prompt here), so it went too; the
    # metadata stayed, so the token counts the dashboard reads are unchanged.
    redacted = _git(repo, "log", "-1", "--format=%B", "HEAD~1")
    assert redacted.startswith(redact.REMOVED_SUBJECT)
    assert "tokens_since_last_commit_output: 5" in redacted
    assert "trace_removed:" in redacted


def test_a_window_redacts_only_the_turns_inside_it_even_within_one_folded_commit(tmp_path):
    """A manual-mode commit folds several turns into the user's own commit. A window takes out
    only the turns it covers, and never the user's own subject: they wrote that."""
    repo = _init_repo(tmp_path)
    inside = _agent_body(f"use {SECRET}", "ok", started=T0, ended=T0 + 30)
    outside = _agent_body("refactor the parser", "done", started=T0 + 7200, ended=T0 + 7260)
    trailer = build_manual_squash_trailer(agitrack_session_id="agit-1", latent_bodies=[inside, outside])
    _commit(repo, "My own commit message\n\n" + trailer, file="b.txt", content="b\n")

    since = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(T0 - 60))
    until = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(T0 + 60))
    assert redact.run(repo, commits=None, since=since, until=until, assume_yes=True) == 0

    message = _git(repo, "log", "-1", "--format=%B")
    assert message.startswith("My own commit message")
    assert SECRET not in message
    assert "refactor the parser" in message
    assert message.count("trace_removed:") == 1


def test_running_it_twice_changes_nothing_the_second_time(tmp_path):
    repo = _init_repo(tmp_path)
    leaky = _commit(repo, _agent_body(SECRET, "ok", started=T0, ended=T0 + 5), file="b.txt", content="b\n")
    assert redact.run(repo, commits=[leaky], since=None, until=None, assume_yes=True) == 0
    head = _git(repo, "rev-parse", "HEAD").strip()

    assert redact.run(repo, commits=["HEAD"], since=None, until=None, assume_yes=True) == 0
    assert _git(repo, "rev-parse", "HEAD").strip() == head


def test_a_dry_run_changes_nothing(tmp_path):
    repo = _init_repo(tmp_path)
    leaky = _commit(repo, _agent_body(SECRET, "ok", started=T0, ended=T0 + 5), file="b.txt", content="b\n")

    assert redact.run(repo, commits=[leaky], since=None, until=None, dry_run=True) == 0
    assert _git(repo, "rev-parse", "HEAD").strip() == leaky


def test_every_local_branch_holding_the_commit_is_rewritten_and_a_summary_note_is_dropped(tmp_path):
    repo = _init_repo(tmp_path)
    leaky = _commit(repo, _agent_body(SECRET, "ok", started=T0, ended=T0 + 5), file="b.txt", content="b\n")
    _git(repo, "notes", "--ref", "agitrack/commit-summary", "add", "-m", f"summary quoting {SECRET}", leaky)
    _git(repo, "branch", "feature")
    keeper = _commit(
        repo, _agent_body("ordinary", "fine", started=T0 + 99, ended=T0 + 100), file="c.txt", content="c\n"
    )
    _git(repo, "notes", "--ref", "agitrack/commit-summary", "add", "-m", "an unrelated summary", keeper)

    assert redact.run(repo, commits=[leaky], since=None, until=None, assume_yes=True) == 0

    for branch in ("HEAD", "feature"):
        assert SECRET not in _git(repo, "log", "--format=%B", branch)
    notes = _git(repo, "log", "--format=%N", "--notes=agitrack/commit-summary", "HEAD")
    assert SECRET not in notes
    assert "an unrelated summary" in notes  # a descendant's note follows its new id


def test_pending_latent_turns_are_rewritten_too(tmp_path):
    # A turn recorded latently but not yet folded into a commit lives on refs/agitrack/manual/*;
    # the next commit would fold the ORIGINAL text in, so the latent chain is rewritten as well.
    repo = _init_repo(tmp_path)
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    body = _agent_body(SECRET, "ok", started=T0, ended=T0 + 5)
    latent = subprocess.run(
        ["git", "-C", str(repo.repo), "commit-tree", tree, "-p", "HEAD", "-F", "-"],
        input=body,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    _git(repo, "update-ref", "refs/agitrack/manual/agit-1", latent)

    since = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(T0 - 1))
    assert redact.run(repo, commits=None, since=since, until="now", assume_yes=True) == 0

    assert SECRET not in _git(repo, "log", "--format=%B", "refs/agitrack/manual/agit-1")


def test_a_window_is_remembered_for_turns_that_are_not_committed_yet(tmp_path):
    repo = _init_repo(tmp_path)

    assert redact.run(repo, commits=None, since="2026-10-06 14:00", until="2026-10-06 15:00", assume_yes=True) == 0

    windows = redact.redacted_windows(repo.repo)
    assert len(windows) == 1
    inside = redact.parse_when("2026-10-06 14:30")
    assert redact.in_windows(inside, windows)
    assert not redact.in_windows(redact.parse_when("2026-10-06 16:00"), windows)


def test_a_bare_date_as_the_end_of_a_window_means_the_whole_day():
    assert redact.parse_when("2026-10-06", end=True) - redact.parse_when("2026-10-06") == 86400


def test_nothing_is_rewritten_without_a_selection(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    assert redact.run(repo, commits=None, since=None, until=None) == 2
    assert "--commit" in capsys.readouterr().out


def test_a_metadata_block_quoted_inside_a_trace_is_not_mistaken_for_a_turn():
    quoted = "Here is the format:\n\n```\n# aGiTrack Metadata\ncommit_type: agent\n```"
    message = _agent_body(SECRET, quoted, started=T0, ended=T0 + 5)

    new, count = redact.redact_message(message, window=None, committed_at=None)

    assert count == 1
    assert SECRET not in new and "Here is the format" not in new


def test_a_commit_no_branch_contains_is_reported_not_claimed_as_removed(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    orphan = _commit(
        repo, _agent_body(f"the password is {SECRET}", "Ok.", started=T0, ended=T0 + 60), file="b.txt", content="b\n"
    )
    _git(repo, "reset", "-q", "--hard", "HEAD~1")  # the commit is now on no branch at all

    code = redact.run(repo, commits=[orphan], since=None, until=None, assume_yes=True)

    out = capsys.readouterr().out
    assert code == 1
    assert "No branch contains " + orphan[:10] in out
    assert "Removed the interaction trace" not in out


def test_a_commit_on_a_detached_head_is_rewritten(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "--detach")
    leaky = _commit(
        repo, _agent_body(f"the password is {SECRET}", "Ok.", started=T0, ended=T0 + 60), file="b.txt", content="b\n"
    )

    assert redact.run(repo, commits=[leaky], since=None, until=None, assume_yes=True) == 0

    assert SECRET not in _full_log(repo)
    assert _git(repo, "rev-parse", "HEAD").strip() != leaky
    assert "across 1 commit(s)" in capsys.readouterr().out
