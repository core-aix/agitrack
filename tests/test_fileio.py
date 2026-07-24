"""Atomic writes must survive CONCURRENT writers (agitrack/fileio.py).

The old fixed ``<file>.tmp`` sidecar crashed with FileNotFoundError the moment two
aGiTrack processes saved the same file at once (an interactive session plus an
export/dashboard/tracker process on the same repo — exactly the "developing aGiTrack
under aGiTrack" setup). These tests pin the unique-tmp behavior.
"""

import json
import subprocess
import sys

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


def test_atomic_write_creates_parents_and_replaces(tmp_path):
    target = tmp_path / "deep" / "nested" / "file.json"
    atomic_write_text(target, "one")
    atomic_write_text(target, "two")
    assert target.read_text(encoding="utf-8") == "two"
    assert not list(target.parent.glob("file.json.*"))
