"""git commit message encoding (agitrack/git/repo.py).

aGiTrack commit messages carry the agent interaction trace, which routinely contains
box-drawing characters, em-dashes, curly quotes and emoji. On Windows, ``subprocess``
text mode defaults to the ANSI code page (cp1252), which can't encode those — so feeding
such a message to ``git commit -F -`` via ``input=`` raised ``UnicodeEncodeError`` before
git even ran, and EVERY agent-turn commit failed (aGiTrack "stopped committing" on Windows).
``GitRepo`` must force UTF-8 for git I/O. These tests run on the Windows CI job too, where
the bug actually reproduced."""

from pathlib import Path

from agitrack.git import GitRepo


def test_commit_message_with_non_ascii_does_not_raise(tmp_path: Path) -> None:
    repo = GitRepo.init(tmp_path)
    (tmp_path / "f.txt").write_text("hi", encoding="utf-8")
    repo.stage_paths(["f.txt"])
    # Box-drawing │, tree └─, em-dash —, curly quote ’, emoji 🎮 — all non-cp1252.
    message = "Add feature\n\n## Agent\ncurly ’ dash — box │ tree └─ emoji \U0001f3ae\n"
    repo.commit(message)  # must NOT raise UnicodeEncodeError
    body = repo._run(["git", "log", "-1", "--pretty=%B"]).stdout
    assert "│" in body  # box-drawing round-trips
    assert "—" in body  # em-dash round-trips
    assert "\U0001f3ae" in body  # emoji round-trips
