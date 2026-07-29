"""Atomic writes must survive CONCURRENT writers (agitrack/fileio.py).

The old fixed ``<file>.tmp`` sidecar crashed with FileNotFoundError the moment two
aGiTrack processes saved the same file at once (an interactive session plus an
export/dashboard/tracker process on the same repo — exactly the "developing aGiTrack
under aGiTrack" setup). These tests pin the unique-tmp behavior.
"""

import json
import os
import subprocess
import sys

import pytest

from agitrack.fileio import atomic_write_text

_HAMMER = """
import json, sys
from pathlib import Path
from agitrack.fileio import atomic_write_text
path = Path(sys.argv[1])
for i in range(300):
    atomic_write_text(path, json.dumps({"writer": sys.argv[2], "i": i}) + "\\n")
"""


def test_concurrent_writers_do_not_crash_or_corrupt(tmp_path):
    target = tmp_path / "state.json"
    procs = [
        subprocess.Popen([sys.executable, "-c", _HAMMER, str(target), name], stderr=subprocess.PIPE)
        for name in ("a", "b")
    ]
    for proc in procs:
        _, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err.decode()
    # Whatever won last, the file is complete valid JSON — never truncated or missing.
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["writer"] in ("a", "b") and data["i"] == 299
    # No unique tmp files left behind on the happy path.
    assert not list(tmp_path.glob("state.json.*"))


def test_a_rename_refused_for_a_moment_is_waited_out(tmp_path, monkeypatch):
    """Windows refuses a replace while any other handle to the destination is open without
    delete sharing — the other writer mid-replace, a reader loading the file, the virus
    scanner that woke when the tmp file appeared. Nothing is wrong and it clears in
    milliseconds, so the save must not crash the caller. (The concurrent-writers test above
    hit this for real: 600 hammered writes on Windows, one PermissionError, one dead process.)
    """
    import agitrack.fileio as fileio

    target = tmp_path / "state.json"
    real_replace, refusals = os.replace, {"left": 3}

    def refuse_then_allow(src, dst):
        if refusals["left"]:
            refusals["left"] -= 1
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(fileio.os, "replace", refuse_then_allow)
    monkeypatch.setattr(fileio, "_REPLACE_BACKOFF_SECONDS", 0.001)  # don't actually wait in a test

    atomic_write_text(target, "written anyway")
    assert target.read_text(encoding="utf-8") == "written anyway"
    assert refusals["left"] == 0  # it really did have to retry
    assert not list(tmp_path.glob("state.json.*"))  # and cleaned up after itself


def test_a_rename_that_is_never_allowed_still_reports(tmp_path, monkeypatch):
    """The retry must not swallow a permission problem that is real — a save that cannot
    happen has to say so rather than being silently dropped."""
    import agitrack.fileio as fileio

    def always_refuse(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(fileio.os, "replace", always_refuse)
    monkeypatch.setattr(fileio, "_REPLACE_BACKOFF_SECONDS", 0.0)

    with pytest.raises(PermissionError):
        atomic_write_text(tmp_path / "state.json", "never lands")
    assert not list(tmp_path.glob("state.json.*"))  # the tmp file is still cleaned up


def test_atomic_write_creates_parents_and_replaces(tmp_path):
    target = tmp_path / "deep" / "nested" / "file.json"
    atomic_write_text(target, "one")
    atomic_write_text(target, "two")
    assert target.read_text(encoding="utf-8") == "two"
    assert not list(target.parent.glob("file.json.*"))
