import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    match = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    assert match, "pyproject.toml has no version"
    return match.group(1)


def test_vscode_extension_version_matches_pyproject():
    # The VSCode extension launches this exact CLI, so it must ship the SAME version.
    # scripts/sync_vscode_version.py (run by scripts/publish.sh) keeps them equal;
    # this guards against drift.
    pkg = json.loads((ROOT / "editors" / "vscode" / "package.json").read_text(encoding="utf-8"))
    assert pkg["version"] == _pyproject_version(), (
        "VSCode extension version must equal aGiTrack's pyproject version; "
        "run `python3 scripts/sync_vscode_version.py`."
    )


def test_sync_script_reports_no_drift_in_check_mode():
    import importlib.util

    spec = importlib.util.spec_from_file_location("sync_vscode_version", ROOT / "scripts" / "sync_vscode_version.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.pyproject_version() == _pyproject_version()
    assert module.sync(check=True) == 0  # repo is in sync
