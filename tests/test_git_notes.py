import pytest
from pathlib import Path
from agit.git import GitRepo


def test_notes_add_and_show(tmp_path: Path) -> None:
    repo = GitRepo.init(tmp_path)
    repo.commit("initial commit")
    sha = repo.rev_parse("HEAD")
    repo.notes_add(sha, "This is a test note", namespace="test")
    note = repo.notes_show(sha, namespace="test")
    assert note is not None
    assert "This is a test note" in note


def test_notes_show_no_note(tmp_path: Path) -> None:
    repo = GitRepo.init(tmp_path)
    repo.commit("initial commit")
    sha = repo.rev_parse("HEAD")
    note = repo.notes_show(sha, namespace="test")
    assert note is None


def test_notes_list(tmp_path: Path) -> None:
    repo = GitRepo.init(tmp_path)
    repo.commit("first commit")
    sha1 = repo.rev_parse("HEAD")
    repo.notes_add(sha1, "Note 1", namespace="test")
    repo.commit("second commit")
    sha2 = repo.rev_parse("HEAD")
    repo.notes_add(sha2, "Note 2", namespace="test")
    notes = repo.notes_list(namespace="test")
    assert len(notes) == 2
    shas = [sha for sha, _ in notes]
    assert sha1 in shas or sha1[:7] in shas
    assert sha2 in shas or sha2[:7] in shas


def test_notes_list_empty(tmp_path: Path) -> None:
    repo = GitRepo.init(tmp_path)
    notes = repo.notes_list(namespace="test")
    assert notes == []


def test_notes_add_overwrite(tmp_path: Path) -> None:
    repo = GitRepo.init(tmp_path)
    repo.commit("initial commit")
    sha = repo.rev_parse("HEAD")
    repo.notes_add(sha, "First note", namespace="test")
    repo.notes_add(sha, "Second note", namespace="test")
    note = repo.notes_show(sha, namespace="test")
    assert note is not None
    assert "Second note" in note
    assert "First note" not in note
