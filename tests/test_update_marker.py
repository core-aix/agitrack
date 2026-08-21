"""The shared update-available marker + its surfaces (background status, dashboard banner).
Installing an update can't be automated, so aGiTrack only RECORDS and REMINDS."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from agitrack.git import GitRepo
from agitrack.update.marker import (
    _MARKER_MAX_AGE_SECONDS,
    clear_update_marker,
    marker_path,
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
    write_update_marker(
        tmp_path, current="0.1.16", latest="0.2.0", message="aGiTrack update available: 0.1.16 → 0.2.0."
    )
    info = read_update_marker(tmp_path)
    assert info["current"] == "0.1.16" and info["latest"] == "0.2.0"
    line = update_reminder_line(tmp_path)
    assert "0.1.16" in line and "0.2.0" in line and "update" in line.lower()
    clear_update_marker(tmp_path)
    assert read_update_marker(tmp_path) is None


def test_the_reminder_says_what_the_check_said(tmp_path):
    """Two different findings write a marker and they ask for different things. Rendering both as
    "an update is available, install it" told people to reinstall an aGiTrack that was already up
    to date and only needed its session restarted."""
    write_update_marker(
        tmp_path,
        current="96ccb20",
        latest="ab9e4d0",
        message="aGiTrack was updated on disk but the running copy is older (96ccb20 → ab9e4d0); restart to load it.",
    )

    line = update_reminder_line(tmp_path)
    assert line is not None
    assert "restart to load it" in line
    assert "pip" not in line and "choose 'update'" not in line


def test_a_marker_nobody_refreshed_expires(tmp_path):
    """A repository is only re-checked while a tracker runs ON IT, so a project nobody has tracked
    since keeps its last answer forever: the dashboard was still repeating a six-week-old
    comparison of two commits nobody had been running for a month."""
    write_update_marker(tmp_path, current="6d826a3", latest="a860d2a", message="aGiTrack update available.")
    path = marker_path(tmp_path)
    record = json.loads(path.read_text())
    record["written"] = time.time() - _MARKER_MAX_AGE_SECONDS - 1
    path.write_text(json.dumps(record))

    assert read_update_marker(tmp_path) is None
    assert update_reminder_line(tmp_path) is None
    # Dropped, not merely skipped: the thing that would otherwise refresh it is not running here.
    assert not path.exists()


def test_an_unstamped_marker_is_judged_by_its_file(tmp_path):
    # Written by an aGiTrack that predates the stamp, which is the population most likely to be
    # stale. Its mtime is a real age, so it is used rather than trusting the record forever.
    write_update_marker(tmp_path, current="a", latest="b", message="aGiTrack update available.")
    path = marker_path(tmp_path)
    path.write_text(json.dumps({"current": "a", "latest": "b", "message": "aGiTrack update available."}))
    old = time.time() - _MARKER_MAX_AGE_SECONDS - 1
    import os

    os.utime(path, (old, old))

    assert read_update_marker(tmp_path) is None


def test_a_fresh_marker_survives_a_clock_that_moved(tmp_path):
    # A record from the future (a clock that jumped, a copied tree) reads as brand new: refusing
    # to show a real update is the worse of the two mistakes.
    write_update_marker(tmp_path, current="a", latest="b", message="aGiTrack update available.")
    path = marker_path(tmp_path)
    record = json.loads(path.read_text())
    record["written"] = time.time() + 3600
    path.write_text(json.dumps(record))

    assert read_update_marker(tmp_path) is not None


def test_marker_ignores_corrupt_or_empty(tmp_path):
    (tmp_path / ".agitrack").mkdir()
    (tmp_path / ".agitrack" / "update-available.json").write_text("{ not json")
    assert read_update_marker(tmp_path) is None  # corrupt ⇒ no update
    write_update_marker(tmp_path, current="a", latest="", message="")  # no latest ⇒ invalid
    assert read_update_marker(tmp_path) is None


def test_dashboard_banner_reflects_marker(tmp_path):
    from agitrack.metrics.web import _update_banner_html, shell_html, update_notices

    repo = _repo(tmp_path)
    assert update_notices(repo) == []  # no marker ⇒ nothing to say
    assert 'class="updatebanner"' not in _update_banner_html(repo)
    assert "__UPDATE_BANNER__" not in shell_html(repo)  # placeholder always substituted
    write_update_marker(
        tmp_path, current="0.1.16", latest="0.2.0", message="aGiTrack update available: 0.1.16 → 0.2.0."
    )
    banner = _update_banner_html(repo)
    assert "0.1.16" in banner and "0.2.0" in banner and 'class="updatebanner"' in banner
    assert 'class="updatebanner"' in shell_html(repo)


def test_the_banner_has_a_container_the_page_can_re_render_into(tmp_path):
    """The notice was rendered into the HTML and never touched again, so a tab open since the
    morning kept announcing an update that had since been installed. The page re-renders the
    banner from its own polling, which needs somewhere to render INTO whether or not there was
    anything to say when the page was served."""
    from agitrack.metrics.web import _update_banner_html

    repo = _repo(tmp_path)

    assert 'id="updatebanners"' in _update_banner_html(repo)


def test_the_state_poll_carries_the_notices_the_page_renders(tmp_path):
    # Same source as the served HTML, so the poll can only ever agree with it or correct it.
    from agitrack.metrics.server import RepoScope
    from agitrack.metrics.web import update_notices

    repo = _repo(tmp_path)
    write_update_marker(
        tmp_path, current="0.1.16", latest="0.2.0", message="aGiTrack update available: 0.1.16 → 0.2.0."
    )
    state = json.loads(RepoScope(repo).get("/state", {}).body.decode("utf-8"))

    assert state["update_notices"] == update_notices(repo)
    assert "0.2.0" in state["update_notices"][0]

    # ...and once the marker is gone, the next poll withdraws it without a reload.
    clear_update_marker(tmp_path)
    state = json.loads(RepoScope(repo).get("/state", {}).body.decode("utf-8"))
    assert state["update_notices"] == []
