"""Static demo export of the metrics dashboard (``agitrack -d export``).

Writes a self-contained, server-free copy of the dashboard (and the learn page) that any
static host can serve: GitHub Pages, Netlify, a plain file server. Every number in the
dashboard derives from commit metadata alone, so a snapshot generated from a clone is the
real dashboard, frozen at the exporting commit.

Layout of the exported directory::

    index.html            the dashboard, full payload embedded + the demo fetch shim
    learn/index.html      the learn page + its shim
    demo/                 the pre-rendered responses the shims serve:
      data-<granularity>.json     /data for hour/day/week/month (default filter)
      log-<sort>-<offset>.json    every /log page for every sort order
      diff/<sha>.json             every commit's /diff
      files.json                  /files
      filelog/<n>.json            /filelog per file (n = index in files.json order)
      filediff/<n>-<sha12>.json   /filediff per (file, change)
      state.json                  /learn/state (profile, committers, engine info)

A ``fetch`` shim injected into both pages maps the endpoints the live page calls to those
files. What can't be static degrades explicitly: the filter dropdowns are disabled with an
explanatory tooltip, and the learn page's agent-driven actions (new lessons, chat, exercise
review) return a notice pointing at a real install. A frozen top banner on both pages says
what this is and how to run the real thing.

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

INSTALL_HINT = "pip install agitrack, then run: agitrack -d"
_REPO_URL = "https://github.com/core-aix/agitrack"

# What the shims answer for anything the snapshot cannot serve (an unbaked diff, a learn
# action that needs the live coach). Rendered in place by the page's normal error paths.
_DEMO_NOTE = (
    "This is a static demo, so this action needs a live install. Run aGiTrack on your own repo: " + INSTALL_HINT
)


def _banner_html(generated: str, css_class: str) -> str:
    """The frozen top strip both exported pages carry. ``css_class`` is the page's own
    sticky-banner class (the dashboard styles ``backtracebanner``, the learn page
    ``btbanner``), so the demo notice inherits the exact banner treatment of its page."""
    text = (
        "STATIC DEMO: the real aGiTrack dashboard, generated from the aGiTrack repository's "
        f"own git history ({generated}). Filters and the live coach are off in this snapshot. "
    )
    return (
        f'<div class="{css_class}">🧪 '
        + _esc(text)
        + f"Get it for your repo: <code>{_esc(INSTALL_HINT)}</code> · "
        + f'<a href="{_REPO_URL}">github.com/core-aix/agitrack</a></div>'
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
    {lock_ids}.forEach(function(id){{
      var el = document.getElementById(id);
      if (el) {{ el.disabled = true; el.title = "Filters are off in this static demo. " + NOTE; }}
    }});
    // The learn page's "back to dashboard" link is written for the live server, where
    // the page lives at /learn and "./" is the dashboard. In the demo the page is a
    // directory (/dashboard/learn/), so point the link one level up explicitly.
    if (LEARN) {{
      var back = document.getElementById("backlink");
      if (back) back.href = "../";
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
    generated = "updated " + (aggregates_payload(dash)["generated_at"])

    if out_dir.exists():
        shutil.rmtree(out_dir)
    demo = out_dir / "demo"
    demo.mkdir(parents=True)

    # /data for each chart granularity (default filter — the only filter the demo serves).
    for granularity in GRANULARITIES:
        payload = aggregates_payload(dash, granularity=granularity)
        payload["shared_sessions"] = shared
        payload["insights"] = insights
        _write_json(demo / f"data-{granularity}.json", payload)

    # Every /log page for every sort order, so paging and re-sorting work in the demo.
    total = len(dash.stats)
    for sort in LOG_SORTS:
        for offset in range(0, max(total, 1), PAGE_SIZE):
            _write_json(demo / f"log-{sort}-{offset}.json", log_page(dash, repo=repo, offset=offset, sort=sort))

    # Every commit's diff, and the whole file browser with each change's file diff.
    for stat in dash.stats:
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
        dash, shared_sessions=shared, banner_html=_banner_html(generated, "backtracebanner"), insights=insights
    )
    (out_dir / "index.html").write_text(
        _inject_shim(page, _shim(base="demo/", files_index=files_index, learn=False, site_root="../")),
        encoding="utf-8",
    )
    learn_html = learn_page.learn_html(repo.repo, banner_html=_banner_html(generated, "btbanner"))
    learn_dir = out_dir / "learn"
    learn_dir.mkdir()
    (learn_dir / "index.html").write_text(
        _inject_shim(learn_html, _shim(base="../demo/", files_index={}, learn=True, site_root="../../")),
        encoding="utf-8",
    )
    return out_dir
