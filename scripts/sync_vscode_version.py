#!/usr/bin/env python3
"""Stamp the VSCode extension's version from aGiTrack's version.

``pyproject.toml`` is aGiTrack's single source of truth for the version. The VSCode
extension in ``editors/vscode/`` is a launcher for *this exact CLI*, so it must always
ship the SAME version — its ``package.json`` version is derived from here rather than
maintained by hand.

Run by ``scripts/publish.sh`` on every release; equality is enforced in CI by
``tests/test_version_sync.py``. Use ``--check`` to verify without writing (non-zero
exit on drift), or no argument to rewrite ``package.json`` in place.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE_JSON = ROOT / "editors" / "vscode" / "package.json"
PACKAGE_LOCK = ROOT / "editors" / "vscode" / "package-lock.json"


def pyproject_version() -> str:
    match = re.search(r'^version = "([^"]+)"', PYPROJECT.read_text(encoding="utf-8"), re.M)
    if not match:
        raise SystemExit("could not find `version` in pyproject.toml")
    return match.group(1)


def package_json_version() -> str:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["version"]


def _sync_package_lock(want: str) -> None:
    """Keep package-lock.json's version in step so `npm ci` doesn't reject a drift.
    Best-effort: the lock file is optional and gate-checked only via package.json."""
    if not PACKAGE_LOCK.is_file():
        return
    data = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
    changed = False
    if data.get("version") != want:
        data["version"] = want
        changed = True
    root_pkg = data.get("packages", {}).get("")
    if isinstance(root_pkg, dict) and root_pkg.get("version") != want:
        root_pkg["version"] = want
        changed = True
    if changed:
        PACKAGE_LOCK.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def sync(*, check: bool = False) -> int:
    want = pyproject_version()
    text = PACKAGE_JSON.read_text(encoding="utf-8")
    have = json.loads(text)["version"]
    if check:
        if have != want:
            print(
                f"version drift: pyproject.toml is {want} but editors/vscode/package.json is {have};\n"
                "run `python3 scripts/sync_vscode_version.py` to fix.",
                file=sys.stderr,
            )
            return 1
        return 0
    if have != want:
        # Rewrite only the first top-level "version": "…" (the extension's own
        # version), preserving the file's formatting otherwise.
        updated = re.sub(r'("version":\s*")[^"]+(")', rf"\g<1>{want}\g<2>", text, count=1)
        PACKAGE_JSON.write_text(updated, encoding="utf-8")
        print(f"synced editors/vscode/package.json version -> {want}")
    _sync_package_lock(want)  # always keep the lock aligned so `npm ci` is happy
    return 0


if __name__ == "__main__":
    raise SystemExit(sync(check="--check" in sys.argv[1:]))
