"""What the three web pages must have in common (`agitrack/metrics/ui.py`).

The dashboard, the learn page and the storyline are three self-contained documents on
purpose: each is served alone, works from a file:// snapshot, and carries its own script.
They are NOT three designs, and every time a shared detail lived in three places it got
fixed in one or two of them: the same commit message rendered in different colours, hidden
boxes stayed on screen where only some pages had the rule, a diff wrapped on one page and
ran off the edge on another, one backtrace banner had three looks.

These tests are the contract that keeps them together. They assert against the RENDERED
page, because that is what a browser gets, and a rule that lives in the shared module would
be invisible in a raw template.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from agitrack.git import GitRepo
from agitrack.metrics import learn, story, ui
from agitrack.metrics.web import shell_html


@pytest.fixture(scope="module")
def pages(tmp_path_factory) -> dict[str, str]:
    """Every page, rendered."""
    root = tmp_path_factory.mktemp("repo")
    repo = GitRepo.init(root)
    return {
        "dashboard": shell_html(repo),
        "learn": learn.learn_html(root),
        "story": story.story_html(root),
    }


def test_no_page_ships_an_unsubstituted_placeholder(pages):
    for name, html in pages.items():
        leftovers = re.findall(r"__[A-Z][A-Z0-9_]+__", html)
        assert not leftovers, f"{name} still carries {sorted(set(leftovers))}"


def test_every_page_uses_the_same_palette(pages):
    """One set of colours under one set of names. The dashboard used to call a colour
    --panel-2 while the others called it --panel2, so a rule copied between pages rendered
    in no colour at all."""
    for name, html in pages.items():
        assert ui.TOKENS in html, f"{name} does not use the shared palette"
    for token in ("--phosphor:", "--amber:", "--fg-dim:", "--panel2:", "--bad:", "--ops:", "--mono:"):
        assert token in ui.TOKENS


def test_hidden_actually_hides_on_every_page(pages):
    """`hidden` is a UA-level display:none, which any author `display` outranks: without this
    rule a hidden flex box stays on screen (which is exactly what the dashboard's file filter
    did in the commits view)."""
    for name, html in pages.items():
        assert "[hidden]{display:none !important}" in html, name


def test_the_top_strip_looks_the_same_on_every_page(pages):
    """The backtrace notice, the static-demo notice and the update notice are one strip with
    one look, wherever the reader meets it."""
    for name, html in pages.items():
        assert ui.BANNER_CSS in html, f"{name} styles the banner itself"
    # ...and its call-to-action is green against the amber text, in one rule for all of them.
    assert ".backtracebanner a,.btbanner a,.updatebanner a{color:var(--phosphor)" in ui.BANNER_CSS


def test_a_commit_renders_identically_wherever_it_is_opened(pages):
    """A commit opened in the dashboard's log and one opened inside a story moment are the
    same commit: same renderer, same colours, same wrapping."""
    for name in ("dashboard", "story"):
        html = pages[name]
        assert ui.COMMIT_CSS in html, f"{name} styles commits itself"
        assert ui.COMMIT_JS in html, f"{name} renders commits itself"
    # The rules that matter most, spelled out: no scroll box, and long lines wrap.
    assert "white-space:pre-wrap;overflow-wrap:anywhere" in ui.COMMIT_CSS
    assert "max-height" not in ui.COMMIT_CSS


def test_the_button_that_flips_a_commit_looks_the_same_in_all_three_places(pages):
    """The dashboard's log detail, its file browser and a story moment each have a button that
    swaps a commit's message for its file changes. They are one control under three names, and
    the log's one carried no rule at all: it rendered as the browser's default grey chrome in
    the middle of a black terminal page."""
    for name in ("dashboard", "story"):
        assert ".diffbtn,.fdifftoggle,.cflip{" in pages[name], f"{name} does not use the shared toggle"
    assert ".diffbtn:hover,.fdifftoggle:hover,.cflip:hover" in ui.COMMIT_CSS
    # Every name the pages actually render is covered by that one selector.
    for name, rendered in (
        ("dashboard", 'class="diffbtn"'),
        ("dashboard", 'class="fdifftoggle"'),
        ("story", 'class="cflip"'),
    ):
        assert rendered in pages[name], f"{name} no longer renders {rendered}"


def test_both_pages_pick_a_date_range_the_same_way(pages):
    """The dashboard filters what it SHOWS with this control and the story page scopes what it
    TELLS, but it is one control: same presets, same custom popup, same UTC dates, one wiring
    helper. That helper also fixes what a plain `change` listener cannot - picking "custom
    range…" when it is already the choice fires no event, so the popup never reopened and a
    reader could not go from one custom range to another."""
    for name in ("dashboard", "story"):
        html = pages[name]
        assert ui.RANGE_CSS in html, f"{name} styles the range control itself"
        assert ui.RANGE_JS in html, f"{name} wires the range control itself"
        assert ui.RANGE_OPTIONS in html, f"{name} offers its own presets"
        assert "bindRangeControl(" in html, f"{name} does not use the shared wiring"
        for control in ('id="f-period"', 'id="f-from"', 'id="f-to"', 'id="dr-done"', 'id="daterange"'):
            assert control in html, f"{name} is missing {control}"
    assert 'period.addEventListener("click", () => { if (period.value === "custom") open(true); });' in ui.RANGE_JS
    assert 'Date.parse(value + "T00:00:00Z")' in ui.RANGE_JS  # one timezone, both pages


def test_a_page_says_what_went_wrong_in_one_place(pages):
    """Notices and errors float as ONE fixed toast at the bottom. The story page used to
    render its errors inline in the middle of the document while the learn page showed the
    same message as a quiet toast, so the demo's "this needs a live install" note looked
    like a failure on one page and an explanation on the other."""
    for name in ("learn", "story"):
        assert ui.FLASH_CSS in pages[name], f"{name} styles its notices itself"
    assert "#flash{position:fixed" in ui.FLASH_CSS and "bottom:18px" in ui.FLASH_CSS
    assert "click to dismiss" in ui.FLASH_CSS
    # Amber for "this is how it is", red for "this broke" - one meaning per colour.
    assert ".notice{border:1px solid var(--warn)" in ui.FLASH_CSS
    assert ".error{border:1px solid var(--bad)" in ui.FLASH_CSS


def test_the_pages_that_pick_a_model_have_the_same_settings_panel(pages):
    """The coach engine and the storyteller are the same repo setting, so they are the same
    panel: same box, same summary, same gear."""
    for name in ("learn", "story"):
        assert ui.ENGINE_CSS in pages[name], f"{name} styles its settings panel itself"
        assert "<summary>" in pages[name]
    assert '.engine summary::before{content:"\\2699\\FE0F  "}' in ui.ENGINE_CSS
    # ...and neither page draws its own icon in the markup, which is how they came to differ.
    for name in ("learn", "story"):
        assert 'class="gear"' not in pages[name], f"{name} carries its own settings icon"


def test_the_generation_overlay_is_shared_by_the_pages_that_generate(pages):
    for name in ("learn", "story"):
        assert ui.OVERLAY_CSS in pages[name], f"{name} styles the overlay itself"
    assert ".ov-engine" in ui.OVERLAY_CSS  # which model is working, on both


def test_every_page_has_the_same_helpers(pages):
    for name, html in pages.items():
        assert ui.DOM_JS in html, f"{name} defines its own helpers"
    for helper in ("const $ =", "const esc =", "async function postJson("):
        assert helper in ui.DOM_JS


def test_no_page_redefines_a_shared_helper(pages):
    """A second definition would shadow the shared one and drift from it."""
    for name, html in pages.items():
        script = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        for helper in ("function commitMd(", "function renderDiff(", "function reflowParagraphs(", "const $ ="):
            assert script.count(helper) <= 1, f"{name} defines {helper} more than once"


def test_every_page_script_runs(pages, tmp_path):
    """The whole page is one inline script: anything that throws at its top level takes the
    page with it and looks exactly like a server that never answered."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node to evaluate the page scripts")
    from tests.test_story import _DOM_STUB

    import json as _json

    for name, html in pages.items():
        source = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
        script = tmp_path / f"{name}.js"
        script.write_text("const SOURCE = " + _json.dumps(source) + ";\n" + _DOM_STUB, encoding="utf-8")
        result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, f"{name}: {result.stdout.strip() or result.stderr.strip()}"


def test_every_page_is_readable_on_a_phone(pages):
    """Each page has to reflow, and nothing may push the document wider than the screen."""
    for name, html in pages.items():
        assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html, name
        assert re.search(r"@media \(max-width:\s*\d+px\)", html), f"{name} has no narrow-screen rules"
        assert "overflow-x:hidden" in html, name


def test_every_page_links_to_the_others(pages):
    """The three pages are one product: from any of them you can reach the other two."""
    assert 'id="storylink"' in pages["dashboard"] and 'id="learnlink"' in pages["dashboard"]
    assert 'id="backlink"' in pages["learn"] and 'id="storylink"' in pages["learn"]
    assert 'id="backlink"' in pages["story"] and 'id="learnlink"' in pages["story"]
    # A "dashboard" link goes to the dashboard, never back to wherever the reader came from.
    for name in ("learn", "story"):
        script = "\n".join(re.findall(r"<script>(.*?)</script>", pages[name], re.S))
        assert "history.back()" not in script, f"{name}'s dashboard link goes back instead of home"


def test_a_page_with_its_own_hover_treatment_drops_the_shared_underline(pages):
    """The shared rule underlines a hovered link, which is right for a link sitting in a
    sentence. The dashboard instead INVERTS one — phosphor block, ink text — and the two
    together drew a line through the middle of that block, most visibly on the story and
    learn entries in its header. The page that defines its own affordance turns the other off;
    the dashboard's footer links, which have no invert, opt back in."""
    dashboard = pages["dashboard"]
    assert "a:hover{color:var(--ink);background:var(--phosphor);text-decoration:none}" in dashboard
    assert "footer .flink:hover{text-decoration:underline}" in dashboard
    # The shared default is still what the other two pages get.
    assert "a:hover{text-decoration:underline}" in ui.BASE_CSS


def test_the_pages_agree_on_their_width(pages):
    widths = {name: re.search(r"\.wrap\{max-width:(\d+)px", html).group(1) for name, html in pages.items()}
    assert len(set(widths.values())) == 1, f"the pages disagree on their width: {widths}"
