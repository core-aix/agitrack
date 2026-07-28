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

from tests.test_dashboard import _demo_repo, _write_lines


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
    from agitrack.metrics.export import _DEMO_NOTE

    for page in (index, learn):
        assert "STATIC DEMO" in page
        assert _DEMO_NOTE in page  # the unsupported-action note is wired into both shims
        assert "window.fetch = function" in page  # the shim is installed

    # Every granularity the chart selector offers has a baked /data response.
    for granularity in GRANULARITIES:
        data = json.loads((out / "demo" / f"data-{granularity}.json").read_text(encoding="utf-8"))
        assert "agg" in data and "insights" in data and "timeseries" in data

    # Every log page for every sort order, covering the demo's last-30-days window.
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


def test_export_scopes_the_demo_to_the_last_30_days(tmp_path, monkeypatch):
    """The demo ships a last-30-days view (anchored to the newest commit), not all time:
    an ancient commit stays out of the log pages, the embedded first paint, and the baked
    diffs — and the page says so (banner text + the disabled range dropdown's value)."""
    _no_network_identity(monkeypatch)
    repo = _demo_repo(tmp_path / "repo")
    _write_lines(repo, "ancient.txt", 3)
    old = "2001-02-03T04:05:06"
    repo._run(["git", "add", "-A"])
    repo._run(
        ["git", "commit", "-m", "ancient work"],
        env={"GIT_AUTHOR_DATE": old, "GIT_COMMITTER_DATE": old},
    )
    ancient_sha = repo.rev_parse("HEAD")
    out = tmp_path / "site"
    export_static_demo(repo, out)

    first = json.loads((out / "demo" / "log-date-0.json").read_text(encoding="utf-8"))
    assert first["total"] == len(first["entries"])  # one in-window page, ancient not counted
    assert "ancient work" not in [entry["subject"] for entry in first["entries"]]
    assert not (out / "demo" / "diff" / f"{ancient_sha}.json").exists()  # only in-scope diffs baked

    index = (out / "index.html").read_text(encoding="utf-8")
    assert "30-day period" in index  # the banner names the scope (dateless: no build stamp)
    assert "UTC" not in index.split("</div>", 1)[0]  # …and carries no timestamp
    assert 'period.value = "30"' in index  # the disabled range dropdown shows the real scope


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
    # Tapping a disabled filter (clicks fall through to its wrapper) or the reset button
    # answers with the learn page's demo note as a fixed toast, not silence.
    assert "demoflash" in index
    assert 'el.style.pointerEvents = "none"' in index
    assert "stopImmediatePropagation" in index  # reset intercepted ahead of the page handler
    # The #trace deep link parks the Interaction Trace HEADING under the sticky chrome. It
    # used to scroll two things — the window to the entry, and the message's own max-height
    # box to the heading; the message now flows with the page, so one scroll does it.
    assert 'location.hash === "#trace"' in index
    assert "box.scrollTop" not in index  # no inner scroll region left to chase
    assert "target.getBoundingClientRect().top - inset()" in index
    # The inset counts only chrome that is ACTUALLY stuck: the filter bar is sticky on a
    # desktop but scrolls away on a phone, where reserving its height parked the entry a
    # screenful too low. And corrections scroll INSTANTLY — with the page's default smooth
    # behavior each correction measured a mid-animation position and visibly overshot.
    assert 'pos !== "sticky" && pos !== "fixed"' in index
    assert 'root.style.scrollBehavior = "auto"' in index
    # EVERY flash on the demo learn page is the same amber notice: a snapshot has no
    # actionable failure, and matching the note's text would miss the page's escaped
    # rendering of it ("doesn't" -> "doesn&#39;t"), leaving some notes red and some amber.
    learn = (out / "learn" / "index.html").read_text(encoding="utf-8")
    assert 'html.replace(/class="error"/g, \'class="notice"\')' in learn
    # Controls that would show the note inline (engine save, sync toggle) or swallow it
    # silently (start over) are intercepted to the same toast instead.
    assert '["e-save", "sync-toggle", "reset-suggest"]' in learn
    learn = (out / "learn" / "index.html").read_text(encoding="utf-8")
    # Agent-driven POSTs answer with the install hint; suggest re-serves the profile.
    assert "static demo" in learn
    assert "learn/suggest" in learn
    # Demo navigation: the logo leads to the main webpage from both pages, and the learn
    # page's dashboard link points one level up (it is a directory in the demo).
    assert '"../"' in index and "homelink" in index
    assert '"../../"' in learn and "homelink" in learn
    assert 'back.href = "../"' in learn
    # The banner links back to the main page's install section instead of spelling
    # out install commands.
    assert 'href="../#install"' in index
    assert 'href="../../#install"' in learn
    # …and the link renders GREEN on BOTH pages, standing out from the amber banner text.
    # Each banner styles its links explicitly because the two pages' global anchor colors
    # differ, which is what made the same banner render differently on each.
    assert ".backtracebanner a{color:var(--phosphor)" in index
    assert ".btbanner a{color:var(--phosphor)" in learn


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
