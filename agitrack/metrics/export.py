"""Static demo export of the metrics dashboard (``agitrack -d export``).

Writes a self-contained, server-free copy of the dashboard (and the learn page) that any
static host can serve: GitHub Pages, Netlify, a plain file server. Every number in the
dashboard derives from commit metadata alone, so a snapshot generated from a clone is the
real dashboard, frozen at the exporting commit.

Layout of the exported directory::

    index.html            the dashboard, full payload embedded + the demo fetch shim
    learn/index.html      the learn page + its shim
    demo/                 the pre-rendered responses the shims serve:
      data-<granularity>.json     /data for hour/day/week/month (last-30-days scope)
      log-<sort>-<offset>.json    every /log page for every sort order (same scope)
      diff/<sha>.json             every commit's /diff
      files.json                  /files
      filelog/<n>.json            /filelog per file (n = index in files.json order)
      filediff/<n>-<sha12>.json   /filediff per (file, change)
      state.json                  /learn/state (profile, committers, engine info)

A ``fetch`` shim injected into both pages maps the endpoints the live page calls to those
files. The demo ships the LAST 30 DAYS of history (anchored to the newest commit, so a
rebuild from a quiet repo never exports an empty page) rather than all time: recent
activity is what sells the dashboard, and it keeps the baked diff set small. What can't be
static degrades explicitly: the filter dropdowns are disabled with an explanatory tooltip
(the range dropdown showing the "last 30 days" it actually serves), and the learn page's
agent-driven actions (new lessons, chat, exercise review) return a notice pointing at a
real install. A frozen top banner on both pages says what this is and how to run the real
thing.

The learn profile comes from the repo's learning store (``.agitrack/learning.json``): the
exporting user's profile, or the store's single profile when the exporting identity has
none (CI exports a checked-in fixture that way).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from agitrack.git import GitRepo
from agitrack.metrics import learn as learn_page
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

# How much history the demo ships: the dashboard's "last 30 days" range, not all time.
_DEMO_WINDOW_DAYS = 30

# What the shims answer for anything the snapshot cannot serve (an unbaked diff, a learn
# action that needs the live coach). Rendered in place by the page's normal error paths.
_DEMO_NOTE = "This static demo doesn't support this action. Install and run aGiTrack on your own repo to see the live dashboard and the learn page's agent-driven features."


def _banner_html(generated: str, css_class: str, site_root: str) -> str:
    """The frozen top strip both exported pages carry. ``css_class`` is the page's own
    sticky-banner class (the dashboard styles ``backtracebanner``, the learn page
    ``btbanner``), so the demo notice inherits the exact banner treatment of its page.
    ``site_root`` is the relative path back to the main webpage, whose install section
    the banner links to."""
    text = (
        "STATIC DEMO: snapshot of the real aGiTrack dashboard, with "
        f"aGiTrack's own history over a 30-day period. "
    )
    return (
        f'<div class="{css_class}">🧪 '
        + _esc(text)
        + f'<a href="{site_root}#install">Get it for your own repo &rarr;</a></div>'
    )


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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


def _shim(*, base: str, files_index: dict[str, int], learn: bool, site_root: str) -> str:
    """The <script> block injected into an exported page: reroutes the page's relative
    fetches to the pre-rendered files and disables what has no static equivalent.

    Placed before the page's main script so the override is installed first. ``base`` is
    the demo/ directory relative to the page. GET endpoints map to files; a miss degrades
    the way the live page already handles errors (a 503 keeps the last-loaded data, an
    ``{"error": …}`` renders in place). Learn POSTs are canned: suggest re-serves the
    shipped profile so the check-in button works; agent actions return the demo notice."""
    manifest = json.dumps(files_index, separators=(",", ":")) if files_index else "{}"
    lock_ids = "[]" if learn else '["f-author","f-backend","f-model","f-period","f-branch"]'
    return f"""<script>
(function(){{
  var BASE = {json.dumps(base)};
  var FILES = {manifest};
  var NOTE = {json.dumps(_DEMO_NOTE)};
  var LEARN = {json.dumps(learn)};
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
    // The baked data is scoped to the last 30 days — make the (disabled) range
    // dropdown say so instead of claiming "all time".
    if (!LEARN) {{
      var period = document.getElementById("f-period");
      if (period) period.value = "{_DEMO_WINDOW_DAYS}";
    }}
    // The learn page's "back to dashboard" link is written for the live server, where
    // the page lives at /learn and "./" is the dashboard. In the demo the page is a
    // directory (/dashboard/learn/), so point the link one level up explicitly.
    if (LEARN) {{
      var back = document.getElementById("backlink");
      if (back) back.href = "../";
    }}
    // The learn page surfaces the demo note through its ERROR path (agent actions return
    // {{error: NOTE}}), which flashes red — but the dashboard shows the same note as an
    // amber notice. Restyle demo-note flashes as notices so the two pages match; real
    // errors keep the red treatment.
    if (LEARN && typeof window.flash === "function") {{
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
          // Only chrome that is ACTUALLY pinned to the top can cover the entry. The filter
          // bar is sticky on a desktop but scrolls away on a phone, where reserving its
          // (tall, wrapped) height parked the entry a screenful too low — with earlier
          // commits filling the gap. So measure the stuck elements' bottom edge rather
          // than blindly summing heights.
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
          // Two scrolls, not one: the commit message lives in its own scrollable box
          // (.dmsg, max-height-capped), so the WINDOW pins the expanded ENTRY right
          // below the sticky chrome (the commit fills the view, no earlier entries
          // showing) while the BOX scrolls internally so the trace starts at its top.
          var box = target.closest ? target.closest(".dmsg") : null;
          // The page scrolls smoothly by default (html{{scroll-behavior:smooth}}), which
          // makes every correction animate — each next one then measures a position
          // mid-flight and overshoots, so the view visibly ran down past the commit and
          // crept back up. Corrections must be instant; restore the page default after.
          var root = document.documentElement, priorBehavior = root.style.scrollBehavior;
          root.style.scrollBehavior = "auto";
          var done = function(){{ clearInterval(pin); root.style.scrollBehavior = priorBehavior; }};
          var settled = 0, corrections = 0;
          var pin = setInterval(function(){{
            if (box) box.scrollTop += Math.round(target.getBoundingClientRect().top - box.getBoundingClientRect().top);
            var drift = entry.getBoundingClientRect().top - inset();
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


def export_static_demo(repo: GitRepo, out_dir: Path) -> Path:
    """Write the static demo site for ``repo`` into ``out_dir`` (replaced if present).
    Returns ``out_dir``."""
    dash = build_dashboard(repo, sha_logins=cached_logins(repo))
    browser = git_browser(repo, dash.stats, "HEAD")
    files, sha_paths = context_from_browser(browser, dash.stats)
    insights = build_insights(dash.stats, files, sha_paths)
    shared = shared_sessions_for(repo)
    generated = aggregates_payload(dash)["generated_at"]
    # The demo's scope: the last 30 days, anchored to the newest commit rather than the
    # export clock so a rebuild from a briefly quiet repo never bakes an empty demo.
    newest = max((stat.timestamp for stat in dash.stats if stat.timestamp), default=0)
    frm = max(0, newest - _DEMO_WINDOW_DAYS * 86400)
    demo_stats = [stat for stat in dash.stats if stat.timestamp and stat.timestamp >= frm]

    if out_dir.exists():
        shutil.rmtree(out_dir)
    demo = out_dir / "demo"
    demo.mkdir(parents=True)

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

    page = format_html(
        dash,
        shared_sessions=shared,
        banner_html=_banner_html(generated, "backtracebanner", "../"),
        insights=insights,
        frm=frm,
    )
    (out_dir / "index.html").write_text(
        _inject_shim(page, _shim(base="demo/", files_index=files_index, learn=False, site_root="../")),
        encoding="utf-8",
    )
    learn_html = learn_page.learn_html(repo.repo, banner_html=_banner_html(generated, "btbanner", "../../"))
    learn_dir = out_dir / "learn"
    learn_dir.mkdir()
    (learn_dir / "index.html").write_text(
        _inject_shim(learn_html, _shim(base="../demo/", files_index={}, learn=True, site_root="../../")),
        encoding="utf-8",
    )
    return out_dir
