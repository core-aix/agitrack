"""`agitrack -d export`: the server-free static demo copy of the dashboard.

The export must be complete (every fetch the live page can make has a pre-rendered
file behind it) and honestly degraded (a demo banner on both pages, filters disabled,
agent-driven learn actions answered with an install hint instead of silently failing).
"""

import json
from pathlib import Path

from agitrack import cli
from agitrack.metrics.export import export_static_demo
from agitrack.metrics.web import GRANULARITIES, LOG_SORTS, PAGE_SIZE

from tests.test_dashboard import _demo_repo


def _no_network_identity(monkeypatch):
    monkeypatch.setattr("agitrack.metrics.learn.learner_id", lambda root, repo: "someone-else")


def _export(tmp_path, monkeypatch) -> Path:
    _no_network_identity(monkeypatch)
    repo = _demo_repo(tmp_path / "repo")
    out = tmp_path / "site"
    export_static_demo(repo, out)
    return out


def test_export_writes_a_complete_static_site(tmp_path, monkeypatch):
    out = _export(tmp_path, monkeypatch)

    index = (out / "index.html").read_text(encoding="utf-8")
    learn = (out / "learn" / "index.html").read_text(encoding="utf-8")
    for page in (index, learn):
        assert "STATIC DEMO" in page
        assert "pip install agitrack" in page
        assert "window.fetch = function" in page  # the shim is installed

    # Every granularity the chart selector offers has a baked /data response.
    for granularity in GRANULARITIES:
        data = json.loads((out / "demo" / f"data-{granularity}.json").read_text(encoding="utf-8"))
        assert "agg" in data and "insights" in data and "timeseries" in data

    # Every log page for every sort order, covering the full history.
    first = json.loads((out / "demo" / "log-date-0.json").read_text(encoding="utf-8"))
    total = first["total"]
    assert total > 0
    for sort in LOG_SORTS:
        for offset in range(0, total, PAGE_SIZE):
            assert (out / "demo" / f"log-{sort}-{offset}.json").exists()

    # Every commit on the first page has its diff baked under the sha the page fetches.
    for entry in first["entries"]:
        assert (out / "demo" / "diff" / f"{entry['sha']}.json").exists()

    # The file browser: every listed file has a filelog, and every change a filediff.
    files = json.loads((out / "demo" / "files.json").read_text(encoding="utf-8"))["files"]
    assert files
    for i, row in enumerate(files):
        assert row["path"]
        changes = json.loads((out / "demo" / "filelog" / f"{i}.json").read_text(encoding="utf-8"))["changes"]
        for change in changes:
            sha = str(change.get("sha") or "")
            if sha:
                assert (out / "demo" / "filediff" / f"{i}-{sha[:12]}.json").exists()


def test_export_shim_installs_before_the_page_script(tmp_path, monkeypatch):
    out = _export(tmp_path, monkeypatch)
    index = (out / "index.html").read_text(encoding="utf-8")
    # The fetch override must be parsed before any page script can call fetch.
    assert index.index("window.fetch = function") < index.index("const INIT")


def test_export_disables_filters_and_cans_learn_actions(tmp_path, monkeypatch):
    out = _export(tmp_path, monkeypatch)
    index = (out / "index.html").read_text(encoding="utf-8")
    for control in ("f-author", "f-backend", "f-model", "f-period", "f-branch"):
        assert control in index
    assert "el.disabled = true" in index
    learn = (out / "learn" / "index.html").read_text(encoding="utf-8")
    # Agent-driven POSTs answer with the install hint; suggest re-serves the profile.
    assert "static demo" in learn
    assert "learn/suggest" in learn


def test_export_learn_state_falls_back_to_the_single_store_profile(tmp_path, monkeypatch):
    """CI seeds a fixture profile under a name that never matches the exporting identity;
    the export must still ship it (the store's only non-empty profile)."""
    _no_network_identity(monkeypatch)
    repo = _demo_repo(tmp_path / "repo")
    profile = {"assessment": "sharp", "gaps": [], "suggestions": [], "lessons": [{"id": "l1", "title": "t"}]}
    store = repo.repo / ".agitrack"
    store.mkdir(exist_ok=True)
    (store / "learning.json").write_text(json.dumps({"profiles": {"maintainer": profile}}), encoding="utf-8")
    out = tmp_path / "site"
    export_static_demo(repo, out)
    state = json.loads((out / "demo" / "state.json").read_text(encoding="utf-8"))
    assert state["me"] == "maintainer"
    assert state["profile"]["assessment"] == "sharp"
    assert state["committers"]
    assert state["trace_turns"] > 0


def test_cli_export_writes_the_site(tmp_path, monkeypatch, capsys):
    _no_network_identity(monkeypatch)
    repo = _demo_repo(tmp_path / "repo")
    out = tmp_path / "site"
    code = cli.main(["-d", "export", "--repo", str(repo.repo), "--export-dir", str(out)])
    assert code == 0
    assert (out / "index.html").exists()
    assert "Static demo dashboard written to" in capsys.readouterr().out
