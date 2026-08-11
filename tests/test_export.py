"""`agitrack -d export`: the server-free static demo copy of the dashboard.

The export must be complete (every fetch the live page can make has a pre-rendered
file behind it) and honestly degraded (a demo banner on both pages, filters disabled,
agent-driven learn actions answered with an install hint instead of silently failing).
"""

import json
from pathlib import Path

from agitrack import cli
from agitrack.metrics.export import export_static_demo
from agitrack.metrics.collect import build_dashboard
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
    story = (out / "story" / "index.html").read_text(encoding="utf-8")
    from agitrack.metrics.export import _DEMO_NOTE

    for page in (index, learn, story):
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


def test_export_ships_the_storyline_page(tmp_path, monkeypatch):
    """The third page: its own directory, its state baked from the store, generation
    disabled, and the cross-links rewritten for a directory layout."""
    out = _export(tmp_path, monkeypatch)
    story = (out / "story" / "index.html").read_text(encoding="utf-8")
    state = json.loads((out / "demo" / "story.json").read_text(encoding="utf-8"))

    # The state a live /story/state would return: the outline is always there (it needs no
    # agent), and the engine note is fixed rather than resolved on the export machine.
    assert state["outline"] and state["meta"]["commits"] > 0
    assert state["building"] is None
    assert state["engine"]["backend"] == "claude"
    assert state["branches"] == []  # one branch is shipped; a picker would lie

    assert 'if (name === "story/state") return file("story.json"' in _shim_of(story)
    # The whole studio answers with the shared demo toast, not just its last button.
    assert 'lockPanel("studio", ".zpart, #e-save"' in story
    assert "demoflash" in story  # ...as the same fixed bottom toast the dashboard uses
    # Cross-links: on the live server each page is a sibling path, in the demo a directory.
    assert 'relink("backlink", "../"); relink("learnlink", "../learn/")' in story
    learn = (out / "learn" / "index.html").read_text(encoding="utf-8")
    assert 'relink("storylink", "../story/")' in learn
    # And the dashboard points at it in both places.
    index = (out / "index.html").read_text(encoding="utf-8")
    assert 'id="storylink"' in index and 'id="storycta"' in index


def test_the_demo_answers_every_unavailable_action_the_same_way(tmp_path, monkeypatch):
    """One note, one box, one colour, on all three pages. "Tell this part closer" was drawn
    by the page after load, so no listener covered it: the click reached the blocked POST and
    the page reported a RED failure in the middle of the document, while every other
    unavailable control showed a calm amber note at the bottom."""
    out = _export(tmp_path, monkeypatch)
    story = (out / "story" / "index.html").read_text(encoding="utf-8")

    # Style, days, the note box and every button: all of it, by selector, so controls the
    # page draws later are covered too.
    assert 'lockPanel("studio", ".zpart, #e-save"' in story
    # A select opens on mousedown, before any click lands...
    assert 'document.addEventListener("mousedown"' in story
    # ...and a text box would otherwise take a caret and typing.
    assert "field.readOnly = true" in story
    # The note goes to the page's OWN toast where there is one, so two boxes never stack.
    assert 'if (typeof window.flash === "function") {' in story
    # And the safety net: whatever still reaches the page's error path is shown as a notice.
    assert "if ((LEARN || STORY) && typeof window.flash === " in story
    assert 'html.replace(/class="error"/g, \'class="notice"\')' in story


def test_the_note_never_names_a_page_it_might_not_be_on(tmp_path, monkeypatch):
    """All three pages show the same note, so it cannot describe one of them: a reader who met
    it on the dashboard was being told about "the learn page's agent-driven features"."""
    from agitrack.metrics.export import _DEMO_NOTE

    for word in ("learn", "dashboard", "story", "storyline"):
        assert word not in _DEMO_NOTE.lower(), f"the shared note names the {word} page"


def test_the_learn_pages_generation_controls_are_locked_too(tmp_path, monkeypatch):
    """Whose sessions, which branch, which period, how much time, how you feel, the note: all of
    it feeds a lesson that only a live agent can write. Leaving those live let a reader set every
    one of them, watch nothing change, and find out why only at the last button."""
    out = _export(tmp_path, monkeypatch)
    learn = (out / "learn" / "index.html").read_text(encoding="utf-8")

    # The same helper that locks the story page's studio, pointed at the check-in panel.
    assert 'lockPanel("checkin", ""' in learn
    assert "var lockPanel = function(panel, alsoMatching, why)" in learn
    # ...which covers clicks, a select's mousedown, and the note box taking a caret.
    assert 'document.addEventListener("mousedown"' in learn
    assert "field.readOnly = true" in learn
    assert 'box.style.cursor = "not-allowed"' in learn
    # The panel holds every control a lesson is asked for with.
    for control in ('id="f-source"', 'id="f-branch"', 'id="f-period"', 'id="time-chips"', 'id="mood-chips"', 'id="go"'):
        assert control in learn, control


def test_each_exported_page_can_be_posted_on_its_own(tmp_path, monkeypatch):
    """Any one of the three may be linked somewhere else, so each carries its OWN card: a
    title that says what the thing IS (the served page titles itself for the person looking at
    THEIR repo), a description, and a preview image. Absolute URLs throughout — a crawler
    fetching /dashboard/story/ cannot resolve a relative image, and a broken card is worse than
    no card."""
    out = _export(tmp_path, monkeypatch)
    pages = {
        "dash": (out / "index.html").read_text(encoding="utf-8"),
        "learn": (out / "learn" / "index.html").read_text(encoding="utf-8"),
        "story": (out / "story" / "index.html").read_text(encoding="utf-8"),
    }
    from agitrack.metrics.export import _SITE, _SOCIAL

    seen_titles, seen_images = set(), set()
    for name, html in pages.items():
        card = _SOCIAL[name]
        assert f"<title>{card['title']}</title>" in html, f"{name} keeps its in-repo title"
        for tag in ("og:title", "og:description", "og:image", "og:url", "twitter:card", "twitter:image"):
            assert tag in html, f"{name} is missing {tag}"
        assert f'<meta name="description" content="{card["description"]}">' in html
        assert f'<meta property="og:url" content="{_SITE}{card["path"]}">' in html
        assert f'<link rel="canonical" href="{_SITE}{card["path"]}">' in html
        assert card["image"].startswith("https://"), "a relative preview image cannot be crawled"
        seen_titles.add(card["title"])
        seen_images.add(card["image"])
    # Three pages, three cards: posting the story must not show the dashboard's screenshot.
    assert len(seen_titles) == 3 and len(seen_images) == 3


def test_the_demo_never_offers_to_add_commits_it_cannot_add(tmp_path, monkeypatch):
    """A frozen snapshot has nothing to add, so the page must not offer it. The button was also
    claiming EVERY commit in the repo ("add the 789 new commits"): the shipped fixture is
    written on ONE branch and CI exports whatever it checked out, so the per-branch lookup
    misses and the story arrives through the fallback — by which point the meta had already
    been computed for "no story at all"."""
    from agitrack.metrics import story as story_page

    _no_network_identity(monkeypatch)
    repo = _demo_repo(tmp_path / "repo")
    told = story_page.story_stats(build_dashboard(repo).stats)
    story_page.StoryStore(repo.repo).put(
        "some-other-branch",  # deliberately NOT the branch being exported
        {
            "title": "A told story",
            "moments": [
                {
                    "id": "m1",
                    "title": "It happened",
                    "summary": "s",
                    "from": told[0].timestamp,
                    "to": told[-1].timestamp,
                    "shas": [stat.sha for stat in told],
                    "commits": [{"sha": stat.sha, "short": stat.short, "subject": stat.subject} for stat in told],
                }
            ],
            "covered_shas": [stat.sha for stat in told],
        },
    )
    out = tmp_path / "site"
    export_static_demo(repo, out)

    state = json.loads((out / "demo" / "story.json").read_text(encoding="utf-8"))
    assert state["story"], "a story does ship"
    assert state["meta"]["uncovered"] == 0
    # The page hides the button on exactly that (renderStudio: !has || !m.uncovered).
    story = (out / "story" / "index.html").read_text(encoding="utf-8")
    assert '$("extend").hidden = !has || !m.uncovered;' in story


def test_the_preview_images_the_cards_point_at_are_in_the_repo():
    """A card naming an image the site does not serve is a broken card everywhere it is shared."""
    from agitrack.metrics.export import _SITE, _SOCIAL

    docs = Path(__file__).resolve().parent.parent / "docs"
    for name, card in _SOCIAL.items():
        relative = card["image"][len(_SITE) :].lstrip("/")
        assert (docs / relative).is_file(), f"{name}'s preview image ({relative}) is not in docs/"


def _shim_of(html: str) -> str:
    return html


def test_export_bakes_the_diffs_a_shipped_story_points_at(tmp_path, monkeypatch):
    """A story is mostly about older history, so its commits usually fall outside the demo's
    30-day window: their diffs must be baked anyway or every "show diff" in the demo fails."""
    from agitrack.metrics import story as story_page

    _no_network_identity(monkeypatch)
    repo = _demo_repo(tmp_path / "repo")
    old = "2001-02-03T04:05:06"
    _write_lines(repo, "ancient.txt", 3)
    repo._run(["git", "add", "-A"])
    repo._run(["git", "commit", "-m", "ancient work"], env={"GIT_AUTHOR_DATE": old, "GIT_COMMITTER_DATE": old})
    ancient = repo.rev_parse("HEAD")
    store = story_page.StoryStore(repo.repo)
    store.put(
        "main",
        {
            "title": "A told story",
            "moments": [
                {
                    "id": "c1",
                    "title": "The ancient moment",
                    "summary": "s",
                    "shas": [ancient],
                    "commits": [{"sha": ancient, "short": ancient[:7], "subject": "ancient work"}],
                }
            ],
            "covered_shas": [ancient],
        },
    )
    out = tmp_path / "site"
    export_static_demo(repo, out)

    state = json.loads((out / "demo" / "story.json").read_text(encoding="utf-8"))
    assert state["story"]["title"] == "A told story"  # shipped even though it names another branch
    assert (out / "demo" / "diff" / f"{ancient}.json").exists()


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
    assert 'relink("backlink", "../")' in learn
    # The banner links back to the main page's install section instead of spelling
    # out install commands.
    assert 'href="../#install"' in index
    assert 'href="../../#install"' in learn
    # …and the link renders GREEN on every page, standing out from the amber banner text.
    # One rule, in agitrack/metrics/ui.py, so the strip cannot drift apart again.
    shared = ".backtracebanner a,.btbanner a,.updatebanner a{color:var(--phosphor)"
    assert shared in index and shared in learn


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


def test_export_refuses_a_directory_it_did_not_write(tmp_path, monkeypatch, capsys):
    """A mistyped `--export-dir ~/Documents` must not be an unrecoverable rmtree.

    The export REPLACES its output directory, so it may only delete a directory it can prove
    is its own: empty, absent, or carrying the export marker."""
    _no_network_identity(monkeypatch)
    repo = _demo_repo(tmp_path / "repo")
    precious = tmp_path / "documents"
    (precious / "taxes").mkdir(parents=True)
    (precious / "README.txt").write_text("do not delete", encoding="utf-8")
    (precious / "taxes" / "2025.txt").write_text("keep", encoding="utf-8")

    code = cli.main(["-d", "export", "--repo", str(repo.repo), "--export-dir", str(precious)])

    assert code == 1
    assert (precious / "README.txt").read_text(encoding="utf-8") == "do not delete"
    assert (precious / "taxes" / "2025.txt").exists()
    out = capsys.readouterr().out
    assert "Refusing to replace" in out
    assert "--force" in out


def test_export_replaces_its_own_previous_output(tmp_path, monkeypatch):
    """Re-exporting into a previous export is the normal case and must keep working —
    including cleaning up files the new export no longer produces."""
    _no_network_identity(monkeypatch)
    repo = _demo_repo(tmp_path / "repo")
    out = tmp_path / "site"
    export_static_demo(repo, out)
    from agitrack.metrics.export import EXPORT_MARKER

    assert (out / EXPORT_MARKER).exists()
    (out / "stale.json").write_text("{}", encoding="utf-8")

    export_static_demo(repo, out)

    assert not (out / "stale.json").exists()
    assert (out / "index.html").exists()


def test_export_force_overrides_the_refusal(tmp_path, monkeypatch):
    _no_network_identity(monkeypatch)
    repo = _demo_repo(tmp_path / "repo")
    out = tmp_path / "site"
    out.mkdir()
    (out / "someone-elses.txt").write_text("bye", encoding="utf-8")

    export_static_demo(repo, out, force=True)

    assert not (out / "someone-elses.txt").exists()
    assert (out / "index.html").exists()


def test_export_accepts_an_empty_directory(tmp_path, monkeypatch):
    _no_network_identity(monkeypatch)
    repo = _demo_repo(tmp_path / "repo")
    out = tmp_path / "site"
    out.mkdir()
    export_static_demo(repo, out)
    assert (out / "index.html").exists()
