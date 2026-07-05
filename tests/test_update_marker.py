"""The shared update-available marker + its surfaces (background status, dashboard banner).
Installing an update can't be automated, so aGiTrack only RECORDS and REMINDS."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agitrack.git import GitRepo
from agitrack.update.marker import (
    clear_update_marker,
    read_update_marker,
    update_reminder_line,
    write_update_marker,
)


def _repo(path: Path) -> GitRepo:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return GitRepo(path)


def test_marker_write_read_clear_and_reminder(tmp_path):
    assert read_update_marker(tmp_path) is None
    assert update_reminder_line(tmp_path) is None
    write_update_marker(tmp_path, current="0.1.16", latest="0.2.0", message="update available")
    info = read_update_marker(tmp_path)
    assert info["current"] == "0.1.16" and info["latest"] == "0.2.0"
    line = update_reminder_line(tmp_path)
    assert "0.1.16" in line and "0.2.0" in line and "update" in line.lower()
    clear_update_marker(tmp_path)
    assert read_update_marker(tmp_path) is None


def test_marker_ignores_corrupt_or_empty(tmp_path):
    (tmp_path / ".agitrack").mkdir()
    (tmp_path / ".agitrack" / "update-available.json").write_text("{ not json")
    assert read_update_marker(tmp_path) is None  # corrupt ⇒ no update
    write_update_marker(tmp_path, current="a", latest="", message="")  # no latest ⇒ invalid
    assert read_update_marker(tmp_path) is None


def test_dashboard_banner_reflects_marker(tmp_path):
    from agitrack.metrics.web import _update_banner_html, shell_html

    repo = _repo(tmp_path)
    assert _update_banner_html(repo) == ""  # no marker ⇒ no banner
    assert "__UPDATE_BANNER__" not in shell_html(repo)  # placeholder always substituted
    write_update_marker(tmp_path, current="0.1.16", latest="0.2.0", message="u")
    banner = _update_banner_html(repo)
    assert "0.1.16" in banner and "0.2.0" in banner and "updatebanner" in banner
    assert "updatebanner" in shell_html(repo)
