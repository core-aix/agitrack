"""Static demo export of the metrics dashboard (``agitrack -d export``).

Writes a self-contained, server-free copy of the dashboard (plus the learn page and the
storyline) that any
static host can serve: GitHub Pages, Netlify, a plain file server. Every number in the
dashboard derives from commit metadata alone, so a snapshot generated from a clone is the
real dashboard, frozen at the exporting commit.

Layout of the exported directory::

    index.html            the dashboard, full payload embedded + the demo fetch shim
    learn/index.html      the learn page + its shim
    story/index.html      the storyline + its shim
    demo/                 the pre-rendered responses the shims serve:
      data-<granularity>.json     /data for hour/day/week/month (last-30-days scope)
      log-<sort>-<offset>.json    every /log page for every sort order (same scope)
      diff/<sha>.json             every commit's /diff
      files.json                  /files
      filelog/<n>.json            /filelog per file (n = index in files.json order)
      filediff/<n>-<sha12>.json   /filediff per (file, change)
      state.json                  /learn/state (profile, committers, engine info)
      story.json                  /story/state (the stored story: eras, moments, outline)

A ``fetch`` shim injected into both pages maps the endpoints the live page calls to those
files. The demo ships the LAST 30 DAYS of history (anchored to the newest commit, so a
rebuild from a quiet repo never exports an empty page) rather than all time: recent
activity is what sells the dashboard, and it keeps the baked diff set small. What can't be
static degrades explicitly: the filter dropdowns are disabled with an explanatory tooltip
(the range dropdown showing the "last 30 days" it actually serves), and the learn page's
agent-driven actions (new lessons, chat, exercise review) return a notice pointing at a
real install; the storyline's generate/extend/forget buttons answer with the same fixed
toast the disabled filters use. A frozen top banner on every page says what this is and how
to run the real thing.

The learn profile comes from the repo's learning store (``.agitrack/learning.json``): the
exporting user's profile, or the store's single profile when the exporting identity has
none (CI exports a checked-in fixture that way). The storyline ships the same way from
``.agitrack/story.json``, and the diffs its moments point at are baked even when they fall
outside the 30-day window, since a story is mostly about older history.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from agitrack.git import GitRepo
from agitrack.metrics import learn as learn_page
from agitrack.metrics import story as story_page
from agitrack.metrics.collect import Dashboard, build_dashboard
from agitrack.metrics.files import git_browser
from agitrack.metrics.github import cached_logins
from agitrack.metrics.insights import build_insights, context_from_browser
from agitrack.metrics.web import (
    GRANULARITIES,
    LOG_SORTS,
    PAGE_SIZE,
    aggregates_payload,
    commit_diff,
    format_html,
    log_page,
    shared_sessions_for,
)

_REPO_URL = "https://github.com/core-aix/agitrack"

# Written at the root of every export. `-d export` REPLACES its output directory, so before
# deleting anything we require proof the directory is ours: this marker, or emptiness. Without
# it a mistyped `--export-dir ~/Documents` was an unrecoverable `shutil.rmtree` (no recycle bin,
# no confirmation, exit 0 with the ordinary success message).
EXPORT_MARKER = ".agitrack-export"


class ExportTargetError(Exception):
    """``--export-dir`` points at a directory that is not an aGiTrack export and is not empty."""


def _clear_target(out_dir: Path, *, force: bool) -> None:
    """Empty ``out_dir`` for a fresh export, refusing to delete anyone else's files.

    Deletion is allowed only when the directory is empty, carries our marker, or ``force`` is
    set. Anything else raises rather than destroying data we cannot identify as ours."""
    if not out_dir.exists():
        return
    if not out_dir.is_dir():
        raise ExportTargetError(f"--export-dir is not a directory: {out_dir}")
    if not force and any(out_dir.iterdir()) and not (out_dir / EXPORT_MARKER).exists():
        raise ExportTargetError(
            f"Refusing to replace {out_dir}: it is not empty and was not written by aGiTrack.\n"
            f"`-d export` REPLACES its output directory — everything in it would be deleted.\n"
            "Point --export-dir at a new or empty directory, or pass --force to delete this one anyway."
        )
    shutil.rmtree(out_dir)


# How much history the demo ships: the dashboard's "last 30 days" range, not all time.
_DEMO_WINDOW_DAYS = 30

# What the shims answer for anything the snapshot cannot serve: a filter, an unbaked diff, any
# action that needs a live agent. Deliberately says nothing about WHICH page it is on, because
# all three show it and a reader who met it on the dashboard was being told about the learn page.
_DEMO_NOTE = (
    "This action is not supported in this static snapshot demo. Install and run aGiTrack yourself to try this feature."
)


def _banner_html(css_class: str, site_root: str, text: str = "") -> str:
    """The frozen top strip both exported pages carry. ``css_class`` is the page's own
    sticky-banner class (the dashboard styles ``backtracebanner``, the learn page
    ``btbanner``), so the demo notice inherits the exact banner treatment of its page.
    ``site_root`` is the relative path back to the main webpage, whose install section
    the banner links to. ``text`` overrides the wording for a page whose scope differs (the
    storyline ships the whole history, not the dashboard's 30-day slice). Deliberately
    dateless: the strip is read on every visit and a build timestamp only crowds it (the
    page's own "updated" line still carries one)."""
    text = (
        text
        or "STATIC DEMO: snapshot of the real aGiTrack dashboard, with aGiTrack's own history over a 30-day period. "
    )
    return (
        f'<div class="{css_class}">🧪 '
        + _esc(text)
        + f'<a href="{site_root}#install">Get it for your own repo &rarr;</a></div>'
    )


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- social cards
# Each demo page is postable ON ITS OWN, so each carries its own title, description and
# preview image rather than inheriting the site's. Absolute URLs throughout: a crawler
# fetching /dashboard/story/ has no way to resolve a relative image, and a card with a broken
# image is worse than no card. Only the EXPORT gets these — a live dashboard is on localhost,
# where there is nothing to share and nothing to crawl.
_SITE = "https://agitrack.core-aix.org"

_SOCIAL = {
    "dash": {
        "title": "aGiTrack dashboard: what your coding agents actually did",
        "description": (
            "A live demo, built from aGiTrack's own history: every agent turn as a traceable commit, "
            "with the interaction trace, the model, the tokens it cost and the lines it moved. Browse the "
            "log, open any commit's real diff, and see where the time and the tokens went."
        ),
        "image": f"{_SITE}/images/dashboard-v6.png",
        "image_alt": "The aGiTrack dashboard: agent commits, token usage, and efficiency insights.",
        "path": "/dashboard/",
    },
    "learn": {
        "title": "aGiTrack learn: a coach that reads your own agent sessions",
        "description": (
            "A live demo of the learn page: it reads the transcripts of your own coding sessions and "
            "teaches you, from them, how to drive an agent and your codebase better. Lessons, exercises "
            "and a profile that gets more specific the more you work."
        ),
        "image": f"{_SITE}/images/learn-page-v2.png",
        "image_alt": "The aGiTrack learn page: personalised lessons drawn from your own agent sessions.",
        "path": "/dashboard/learn/",
    },
    "story": {
        "title": "aGiTrack storyline: a repository's history, told as a story",
        "description": (
            "A live demo: aGiTrack's own history read back as a story by its coding agent. The project "
            "in parts, the moments inside each one, and under every moment what the developer actually "
            "asked for at the time and the commits it produced."
        ),
        "image": f"{_SITE}/images/story-page.png",
        "image_alt": "The aGiTrack storyline: a repository's history told as parts and moments.",
        "path": "/dashboard/story/",
    },
}


def _social_head(page: str) -> str:
    """The Open Graph / Twitter block for one exported page."""
    card = _SOCIAL[page]
    tags = [
        f'<meta name="description" content="{_esc(card["description"])}">',
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="aGiTrack">',
        f'<meta property="og:title" content="{_esc(card["title"])}">',
        f'<meta property="og:description" content="{_esc(card["description"])}">',
        f'<meta property="og:url" content="{_SITE}{card["path"]}">',
        f'<meta property="og:image" content="{card["image"]}">',
        f'<meta property="og:image:alt" content="{_esc(card["image_alt"])}">',
        '<meta property="og:locale" content="en_US">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{_esc(card["title"])}">',
        f'<meta name="twitter:description" content="{_esc(card["description"])}">',
        f'<meta name="twitter:image" content="{card["image"]}">',
        f'<meta name="twitter:image:alt" content="{_esc(card["image_alt"])}">',
        f'<link rel="canonical" href="{_SITE}{card["path"]}">',
    ]
    return "\n".join(tags)


def _with_social(html: str, page: str) -> str:
    """Give an exported page its own social card and a title that reads outside this repo.

    The served pages title themselves for the person looking at THEIR repo ("story ·
    myproject · aGiTrack"); a link posted somewhere else needs to say what the thing IS."""
    card = _SOCIAL[page]
    html = re.sub(r"<title>.*?</title>", f"<title>{_esc(card['title'])}</title>", html, count=1, flags=re.S)
    return html.replace("</head>", _social_head(page) + "\n</head>", 1)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def _demo_profile(root: Path) -> tuple[str, dict]:
    """The learn profile to ship: the exporting user's, or the store's only profile when
    that identity has none (how CI exports the checked-in fixture profile)."""
    store = learn_page.LearnStore(root)
    data = store.load()
    profiles = data.get("profiles") or {}
    gid = ""
    try:
        gid = learn_page.learner_id(root, None)
    except Exception:
        gid = ""
    if gid and isinstance(profiles.get(gid), dict) and not learn_page._profile_is_empty(profiles[gid]):
        return gid, profiles[gid]
    for key, profile in profiles.items():
        if isinstance(profile, dict) and not learn_page._profile_is_empty(profile):
            return key, profile
    return gid or "demo", learn_page.LearnStore.profile(data, gid or "demo")


def _learn_state(dash: Dashboard, repo: GitRepo) -> dict:
    """The /learn/state payload, built deterministically (no gh lookups, no ref sync):
    the page must paint the same on any host."""
    me, profile = _demo_profile(repo.repo)
    return {
        "me": me,
        "profile": profile,
        "restored": False,
        # A plausible engine note instead of resolving a live backend on the export
        # machine (CI has none): the demo never generates content anyway.
        "backend_info": {"backend": "claude", "model": "", "backend_source": "session", "model_source": "auto"},
        "sync": {"available": True, "enabled": False, "last": None, "users": []},
        "committers": sorted({label for stat in dash.stats for label in dash.committers_of(stat)}),
        "branches": dash.branches or ([dash.branch] if dash.branch else []),
        "branch": dash.branch,
        "trace_turns": sum(1 for stat in dash.stats if stat.kind in learn_page._AI_KINDS),
    }


def _story_state(dash: Dashboard, repo: GitRepo, sha_paths: dict[str, set[str]]) -> dict:
    """The /story/state payload for the demo: the stored story (or the store's only story,
    the way the learn profile falls back, so CI can ship a checked-in fixture written on
    another branch), the outline, and a fixed engine note. No agent runs here."""
    state = story_page.story_state(
        repo.repo,
        dash.stats,
        sha_paths,
        branch=dash.branch,
        branches=[],  # the demo serves one branch; the picker would lie
        repo_name=str(repo.repo).rstrip("/").rsplit("/", 1)[-1],
    )
    if not state.get("story"):
        # The fixture is written on ONE branch and CI exports from whatever it checked out, so
        # the branch lookup above usually misses and this is the story that actually ships.
        state["story"] = story_page.StoryStore(repo.repo).any_story()
    state["building"] = None
    # Nothing can be added to a frozen snapshot, so the page must not offer to: "add the N new
    # commits" is an action the demo answers with the install note, and the number it carried
    # was every commit in the repo anyway (the meta above was computed before the fallback
    # story was found, i.e. for "no story at all").
    state["meta"]["uncovered"] = 0
    # The page renders chapters from their `commits` rows; the raw sha lists exist only so a
    # build can tell what it has already covered, which a snapshot never does. Dropping them
    # takes a sizeable bite out of what every visitor downloads.
    story = state.get("story")
    if isinstance(story, dict):
        story.pop("covered_shas", None)
        for moment in story.get("moments") or []:
            moment.pop("shas", None)
    # A plausible engine note rather than resolving a backend on the export machine (CI has
    # none): the demo never generates anything anyway.
    state["engine"] = {"backend": "claude", "model": "", "backend_source": "session", "model_source": "auto"}
    return state


def _story_shas(state: dict) -> list[str]:
    """Every commit the shipped story points at, so its diffs are baked even when they fall
    outside the demo's 30-day window (a story is mostly about older history)."""
    story = state.get("story") or {}
    shas: list[str] = []
    for moment in story.get("moments") or []:
        for row in moment.get("commits") or []:
            sha = str(row.get("sha") or "")
            if sha and sha not in shas:
                shas.append(sha)
    return shas


def _shim(*, base: str, files_index: dict[str, int], page: str, site_root: str) -> str:
    """The <script> block injected into an exported page: reroutes the page's relative
    fetches to the pre-rendered files and disables what has no static equivalent.

    Placed before the page's main script so the override is installed first. ``base`` is
    the demo/ directory relative to the page, and ``page`` which of the three pages this is
    (``dash``, ``learn`` or ``story``). GET endpoints map to files; a miss degrades the way
    the live page already handles errors (a 503 keeps the last-loaded data, an
    ``{"error": …}`` renders in place). Learn POSTs are canned: suggest re-serves the
    shipped profile so the check-in button works; agent actions return the demo notice."""
    manifest = json.dumps(files_index, separators=(",", ":")) if files_index else "{}"
    learn = page == "learn"
    lock_ids = {
        "dash": '["f-author","f-backend","f-model","f-period","f-branch","advbtn"]',
        "learn": "[]",
        "story": '["f-branch"]',
    }[page]
    return f"""<script>
(function(){{
  var BASE = {json.dumps(base)};
  var FILES = {manifest};
  var NOTE = {json.dumps(_DEMO_NOTE)};
  var LEARN = {json.dumps(learn)};
  var STORY = {json.dumps(page == "story")};
  // The pages ask this before offering anything that needs a live agent or a live repo. A
  // snapshot cannot write a moment's prose, but it CAN show everything already in the data,
  // and a page that offers neither is the failure this flag exists to prevent.
  window.AGITRACK_STATIC = true;
  var real = window.fetch.bind(window);
  var asJson = function(obj){{ return Promise.resolve(new Response(JSON.stringify(obj), {{headers: {{"Content-Type": "application/json"}}}})); }};
  var unavailable = function(){{ return Promise.resolve(new Response("", {{status: 503}})); }};
  var file = function(name, miss){{ return real(BASE + name, {{cache: "no-store"}}).then(function(r){{ return r.ok ? r : miss(); }}, miss); }};
  var STATE = null;
  if (LEARN) STATE = file("state.json", function(){{ return asJson({{}}); }}).then(function(r){{ return r.clone().json(); }});
  window.fetch = function(input, init){{
    var url = String(input && input.url || input);
    var parts = url.split("?");
    var name = parts[0].replace(/^\\.\\//, "").replace(/^\\//, "");
    var p = new URLSearchParams(parts[1] || "");
    if (init && String(init.method || "GET").toUpperCase() === "POST") {{
      if (name === "learn/suggest") return STATE ? STATE.then(function(s){{ return asJson({{profile: s.profile}}); }}) : asJson({{error: NOTE}});
      if (name === "learn/progress") return asJson({{ok: true}});
      return asJson({{error: NOTE}});
    }}
    if (name === "data") {{
      var g = p.get("granularity") || "day";
      if (["hour","day","week","month"].indexOf(g) < 0) g = "day";
      return file("data-" + g + ".json", unavailable);
    }}
    if (name === "log") {{
      var sort = p.get("sort") || "date";
      if (["date","lines","tokens"].indexOf(sort) < 0) sort = "date";
      var page = {PAGE_SIZE};
      var offset = Math.max(0, Math.floor((parseInt(p.get("offset") || "0", 10) || 0) / page) * page);
      return file("log-" + sort + "-" + offset + ".json", unavailable);
    }}
    if (name === "diff") return file("diff/" + (p.get("sha") || "").slice(0, 40) + ".json", function(){{ return asJson({{error: NOTE}}); }});
    if (name === "files") return file("files.json", function(){{ return asJson({{files: []}}); }});
    if (name === "filelog") {{
      var i = FILES[p.get("path") || ""];
      return i === undefined ? asJson({{changes: []}}) : file("filelog/" + i + ".json", function(){{ return asJson({{changes: []}}); }});
    }}
    if (name === "filediff") {{
      var fi = FILES[p.get("path") || ""];
      if (fi === undefined) return asJson({{error: NOTE}});
      return file("filediff/" + fi + "-" + (p.get("sha") || "").slice(0, 12) + ".json", function(){{ return asJson({{error: NOTE}}); }});
    }}
    if (name === "story/state") return file("story.json", unavailable);
    if (name === "learn/state") return file("state.json", unavailable);
    if (name === "learn/models") return asJson({{backend: p.get("backend") || "", models: []}});
    return real(input, init);
  }};
  document.addEventListener("DOMContentLoaded", function(){{
    // Tapping an unsupported control answers with the same fixed toast the learn page
    // uses for unavailable features — visible from anywhere (the top banner is short and
    // may be scrolled away, especially on a phone). Click dismisses.
    var flashBox = null;
    if (!LEARN) {{
      document.head.insertAdjacentHTML("beforeend", "<style>" +
        "#demoflash{{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;z-index:80;" +
          "width:min(680px,calc(100vw - 32px));pointer-events:none}}" +
        "#demoflash .notice{{pointer-events:auto;cursor:pointer;background:var(--panel);" +
          "border:1px solid var(--amber);color:var(--amber);padding:10px 14px;font-size:13px;" +
          "border-radius:6px;margin:6px 0;box-shadow:0 14px 44px rgba(0,0,0,.65)}}" +
        "#demoflash .notice::after{{content:\\" · click to dismiss\\";opacity:.6;font-size:11px}}" +
        "</style>");
      flashBox = document.createElement("div");
      flashBox.id = "demoflash";
      document.body.appendChild(flashBox);
      flashBox.onclick = function(){{ flashBox.innerHTML = ""; }};
    }}
    var showNote = function(){{
      // ONE note, in ONE place, for every unavailable feature on every page. Where the page
      // has its own bottom toast (learn, story) that is the box used, so a demo note and a
      // page notice can never stack at the bottom of the screen looking like two answers.
      if (typeof window.flash === "function") {{
        window.flash('<div class="notice">' + NOTE + "</div>");
        return;
      }}
      if (!flashBox) return;
      var note = document.createElement("div");
      note.className = "notice";
      note.textContent = NOTE;
      flashBox.innerHTML = "";
      flashBox.appendChild(note);
    }};
    {lock_ids}.forEach(function(id){{
      var el = document.getElementById(id);
      if (!el) return;
      el.disabled = true;
      el.title = "Filters are off in this static demo. " + NOTE;
      // A disabled control swallows clicks, so let them fall through to the wrapper,
      // which explains why the control is off.
      el.style.pointerEvents = "none";
      var wrap = el.parentElement;
      if (wrap) {{ wrap.style.cursor = "not-allowed"; wrap.addEventListener("click", showNote); }}
    }});
    // The reset button stays enabled-looking but resetting filters is meaningless here —
    // intercept it (capture phase beats the page's own handler) and explain instead.
    if (!LEARN) {{
      var reset = document.getElementById("reset");
      if (reset) reset.addEventListener("click", function(e){{
        e.preventDefault(); e.stopImmediatePropagation(); showNote();
      }}, true);
    }}
    // The DASHBOARD's baked data is scoped to the last 30 days — make its (disabled) range
    // dropdown say so instead of claiming "all time". The story page has the same control
    // for a different job (which days to TELL) and ships the whole history, so it keeps its
    // own default: forcing 30 there left the dropdown saying one thing and the page another.
    if (!LEARN && !STORY) {{
      var period = document.getElementById("f-period");
      if (period) period.value = "{_DEMO_WINDOW_DAYS}";
    }}
    // The learn and story pages' cross-links are written for the live server, where they
    // live at /learn and /story and a sibling page is one relative hop away. In the demo
    // each is a DIRECTORY (/dashboard/learn/, /dashboard/story/), so the same relative
    // hrefs would resolve one level too deep: rewrite them explicitly.
    var relink = function(id, href){{
      var el = document.getElementById(id);
      if (el) el.href = href;
    }};
    if (LEARN) {{ relink("backlink", "../"); relink("storylink", "../story/"); }}
    if (STORY) {{ relink("backlink", "../"); relink("learnlink", "../learn/"); }}
    // Generating content needs a live agent, so the WHOLE panel that asks for it is off in a
    // snapshot: the story page's studio (voice, days, extra instruction, every button that
    // would start or undo a telling) and the learn page's check-in (whose sessions, which
    // branch, which period, how much time, how you feel, the note). Half-live controls are
    // worse than none: a reader could set all of it, watch nothing change, and only find out
    // why at the last button. Delegated on the document (capture phase) so it covers controls
    // the page draws later too.
    var lockPanel = function(panel, alsoMatching, why){{
      var box = document.getElementById(panel);
      if (!box) return;
      var inside = "#" + panel + (alsoMatching ? ", " + alsoMatching : "");
      document.addEventListener("click", function(e){{
        if (!e.target.closest || !e.target.closest(inside)) return;
        e.preventDefault(); e.stopImmediatePropagation(); showNote();
      }}, true);
      // A select opens its menu on mousedown, before any click lands.
      document.addEventListener("mousedown", function(e){{
        if (!e.target.closest || !e.target.closest(inside)) return;
        e.preventDefault(); showNote();
      }}, true);
      // ...and a text box would still take a caret and typing.
      [].forEach.call(box.querySelectorAll("input[type=text], input:not([type]), textarea"), function(field){{
        field.readOnly = true;
        field.addEventListener("focus", function(){{ field.blur(); showNote(); }});
      }});
      // Say it before anything is clicked, too: the panel reads as unavailable.
      box.style.cursor = "not-allowed";
      box.title = why + " " + NOTE;
    }};
    if (STORY) {{
      lockPanel("studio", ".zpart, #e-save", "Telling a story needs a live install.");
      // The page disables its buttons while it thinks a build is running or the engine is
      // missing; here they must stay clickable so the toast can explain why nothing happens.
      var keepLive = function(){{
        ["write", "extend", "rewrite"].forEach(function(id){{
          var el = document.getElementById(id);
          if (el) el.disabled = false;
        }});
      }};
      keepLive();
      setInterval(keepLive, 500);
    }}
    if (LEARN) {{
      lockPanel("checkin", "", "Writing a lesson needs a live install.");
      var keepGoLive = function(){{
        var go = document.getElementById("go");
        if (go) go.disabled = false;
      }};
      keepGoLive();
      setInterval(keepGoLive, 500);
    }}
    // The learn and story pages surface the demo note through their ERROR path (agent
    // actions return {{error: NOTE}}), which flashes red — but the dashboard shows the same
    // note as an amber notice. Restyle every flash as a notice so all three pages match.
    // This is the safety net UNDER the interceptions above: a control nobody listed still
    // says the same thing, in the same box, in the same colour.
    if ((LEARN || STORY) && typeof window.flash === "function") {{
      var pageFlash = window.flash;
      window.flash = function(html){{
        // EVERY flash here is amber. In a frozen snapshot there is no actionable failure —
        // whatever the page was about to report in red means the same thing, "this needs a
        // live install" — so the bottom note must not alternate between red and amber.
        // (Matching the note's TEXT is not enough: the page HTML-escapes it, so "doesn't"
        // becomes "doesn&#39;t" and the raw note never matches.)
        if (typeof html === "string") html = html.replace(/class="error"/g, 'class="notice"');
        return pageFlash(html);
      }};
    }}
    // Controls whose handlers would show the demo note INLINE (engine save / sync toggle
    // write to their status spans) or swallow it silently (start-over only reacts to a
    // success payload): intercept ahead of the page handler and use the same fixed toast
    // as everything else, so every unavailable feature announces itself one way.
    if (LEARN) {{
      ["e-save", "sync-toggle", "reset-suggest"].forEach(function(id){{
        var el = document.getElementById(id);
        if (el) el.addEventListener("click", function(e){{
          e.preventDefault(); e.stopImmediatePropagation();
          if (typeof window.flash === "function") window.flash('<div class="notice">' + NOTE + '</div>');
        }}, true);
      }});
    }}
    // Deep link from the main page's "Turns become commits" card: #trace opens the
    // newest aGiTrack commit in the log and scrolls to its Interaction Trace heading.
    // The log arrives asynchronously, so poll: first click the entry open (its detail
    // renders a frame later). Then PIN the heading just below the sticky chrome and keep
    // correcting while late layout (the chart canvas, web fonts, other entries) shifts the
    // page — a single blind scroll left the heading stranded mid-viewport with earlier
    // commits still showing above it, instead of the expanded commit filling the view.
    if (!LEARN && location.hash === "#trace") {{
      var deadline = Date.now() + 15000;
      var tick = setInterval(function(){{
        if (Date.now() > deadline) {{ clearInterval(tick); return; }}
        var entry = document.querySelector("#commitlog .entry.ai");
        if (!entry) return;
        var detail = entry.querySelector(".detail");
        if (detail && detail.hidden) {{ entry.click(); return; }}
        var target = null;
        (detail ? detail.querySelectorAll(".md-h") : []).forEach(function(h){{
          if (!target && /interaction trace/i.test(h.textContent)) target = h;
        }});
        if (target) {{
          clearInterval(tick);
          // Only chrome that is ACTUALLY pinned to the top can cover the target. The filter
          // bar is sticky on a desktop but scrolls away on a phone, where reserving its
          // (tall, wrapped) height parked the content a screenful too low.
          var inset = function(){{
            var bottom = 0;
            [".backtracebanner", ".controls"].forEach(function(sel){{
              var el = document.querySelector(sel);
              if (!el) return;
              var pos = getComputedStyle(el).position;
              if (pos !== "sticky" && pos !== "fixed") return;
              bottom = Math.max(bottom, el.getBoundingClientRect().bottom);
            }});
            return bottom + 10;
          }};
          // Park the Interaction Trace HEADING itself under the sticky chrome. The message
          // used to sit in its own max-height box, so the heading could only be reached by
          // scrolling that box and the window separately; now the message flows with the
          // page, so one scroll puts the trace at the top with the rest of it below.
          // The page scrolls smoothly by default (html{{scroll-behavior:smooth}}), which made
          // every correction animate — each next one then measured a position mid-flight and
          // overshot, so the view visibly ran past and crept back. Corrections are instant;
          // the page default is restored after.
          var root = document.documentElement, priorBehavior = root.style.scrollBehavior;
          root.style.scrollBehavior = "auto";
          var done = function(){{ clearInterval(pin); root.style.scrollBehavior = priorBehavior; }};
          var settled = 0, corrections = 0;
          var pin = setInterval(function(){{
            var drift = target.getBoundingClientRect().top - inset();
            if (Math.abs(drift) <= 2) {{ if (++settled >= 4) done(); return; }}
            settled = 0;
            if (++corrections > 60) {{ done(); return; }}
            window.scrollBy(0, drift);
          }}, 100);
        }}
      }}, 150);
    }}
    // On the demo site the big aGiTrack logo always leads back to the main webpage.
    var brand = document.querySelector(".brand");
    if (brand) {{
      document.head.insertAdjacentHTML("beforeend",
        "<style>a.homelink,a.homelink:hover{{border-bottom:none;background:none;color:inherit}}</style>");
      var home = document.createElement("a");
      home.className = "homelink";
      home.href = {json.dumps(site_root)};
      home.title = "aGiTrack home";
      brand.parentNode.insertBefore(home, brand);
      home.appendChild(brand);
    }}
  }});
}})();
</script>"""


def _inject_shim(html: str, shim: str) -> str:
    """Place the shim before the page's first <script> so the fetch override is installed
    before any page code can run."""
    marker = "<script"
    at = html.find(marker)
    if at < 0:
        return shim + html
    return html[:at] + shim + "\n" + html[at:]


def export_static_demo(repo: GitRepo, out_dir: Path, *, force: bool = False) -> Path:
    """Write the static demo site for ``repo`` into ``out_dir``.

    ``out_dir`` is REPLACED, so it must be empty, absent, or a previous export (marked with
    ``EXPORT_MARKER``) unless ``force``. Raises ``ExportTargetError`` otherwise. Returns
    ``out_dir``."""
    _clear_target(out_dir, force=force)
    dash = build_dashboard(repo, sha_logins=cached_logins(repo))
    browser = git_browser(repo, dash.stats, "HEAD")
    files, sha_paths = context_from_browser(browser, dash.stats)
    insights = build_insights(dash.stats, files, sha_paths)
    shared = shared_sessions_for(repo)
    # The demo's scope: the last 30 days, anchored to the newest commit rather than the
    # export clock so a rebuild from a briefly quiet repo never bakes an empty demo.
    newest = max((stat.timestamp for stat in dash.stats if stat.timestamp), default=0)
    frm = max(0, newest - _DEMO_WINDOW_DAYS * 86400)
    demo_stats = [stat for stat in dash.stats if stat.timestamp and stat.timestamp >= frm]

    demo = out_dir / "demo"
    demo.mkdir(parents=True)
    # Written first, so an export interrupted half-way is still recognisable as ours and the
    # retry does not have to refuse it.
    (out_dir / EXPORT_MARKER).write_text(
        "This directory is an aGiTrack static dashboard export (`agitrack -d export`).\n"
        "Re-exporting here REPLACES its entire contents. Delete this file to protect it.\n",
        encoding="utf-8",
    )

    # /data for each chart granularity (last-30-days scope — the only filter the demo serves).
    for granularity in GRANULARITIES:
        payload = aggregates_payload(dash, frm=frm, granularity=granularity)
        payload["shared_sessions"] = shared
        payload["insights"] = insights
        _write_json(demo / f"data-{granularity}.json", payload)

    # Every /log page for every sort order, so paging and re-sorting work in the demo.
    total = len(demo_stats)
    for sort in LOG_SORTS:
        for offset in range(0, max(total, 1), PAGE_SIZE):
            _write_json(
                demo / f"log-{sort}-{offset}.json", log_page(dash, repo=repo, frm=frm, offset=offset, sort=sort)
            )

    # Every in-scope commit's diff (only they appear in the log), and the whole file
    # browser with each change's file diff (file history deliberately stays full-depth).
    for stat in demo_stats:
        _write_json(demo / "diff" / f"{stat.sha}.json", commit_diff(repo, stat.sha))
    files_payload = browser.files_payload()
    _write_json(demo / "files.json", {"files": files_payload})
    files_index = {row["path"]: i for i, row in enumerate(files_payload)}
    for path, i in files_index.items():
        log_payload = browser.file_log_payload(path)
        _write_json(demo / "filelog" / f"{i}.json", log_payload)
        for change in log_payload.get("changes", []):
            sha = str(change.get("sha") or "")
            if sha:
                _write_json(demo / "filediff" / f"{i}-{sha[:12]}.json", browser.file_diff(path, sha))

    _write_json(demo / "state.json", _learn_state(dash, repo))

    # The storyline, and the diffs its chapters point at: a story is mostly about older
    # history, so those commits are usually outside the demo's 30-day window.
    _files_all, sha_paths = context_from_browser(browser)
    story_state = _story_state(dash, repo, sha_paths)
    _write_json(demo / "story.json", story_state)
    baked = {stat.sha for stat in demo_stats}
    for sha in _story_shas(story_state):
        if sha not in baked:
            _write_json(demo / "diff" / f"{sha}.json", commit_diff(repo, sha))

    page = format_html(
        dash,
        shared_sessions=shared,
        banner_html=_banner_html("backtracebanner", "../"),
        insights=insights,
        frm=frm,
    )
    (out_dir / "index.html").write_text(
        _with_social(
            _inject_shim(page, _shim(base="demo/", files_index=files_index, page="dash", site_root="../")), "dash"
        ),
        encoding="utf-8",
    )
    learn_html = learn_page.learn_html(repo.repo, banner_html=_banner_html("btbanner", "../../"))
    learn_dir = out_dir / "learn"
    learn_dir.mkdir()
    (learn_dir / "index.html").write_text(
        _with_social(
            _inject_shim(learn_html, _shim(base="../demo/", files_index={}, page="learn", site_root="../../")), "learn"
        ),
        encoding="utf-8",
    )
    story_html = story_page.story_html(
        repo.repo,
        banner_html=_banner_html(
            "btbanner",
            "../../",
            "STATIC DEMO: aGiTrack's own story told by its coding agent. Writing a new one needs a live install. ",
        ),
    )
    story_dir = out_dir / "story"
    story_dir.mkdir()
    (story_dir / "index.html").write_text(
        _with_social(
            _inject_shim(story_html, _shim(base="../demo/", files_index={}, page="story", site_root="../../")), "story"
        ),
        encoding="utf-8",
    )
    return out_dir
