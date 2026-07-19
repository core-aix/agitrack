"""The dashboard's routing panel (``/routing``).

Where the learn page coaches the user from their interaction traces, the
routing panel exposes the model router's learned preferences and accepts
explicit ratings. The JSON endpoints are:

* ``GET  /routing``        — HTML page
* ``GET  /routing/state``  — current profile + sync status + the user's
  recent events (for the panel's event feed)
* ``POST /routing/rate``   — record a 1-5 rating for the current model
* ``POST /routing/sync``   — toggle the prefs-sync switch (mirror of
  :mod:`agitrack.routing.store.set_sync`)

The panel shares the same per-user keying as the learn page (the user's
GitHub id, with the git user.name fallback) and reuses the routing
store's orphan-ref sync.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from agitrack.git import GitRepo
from agitrack.routing.store import (
    EVENT_KIND_RATING,
    load_events,
    load_profile,
    maybe_sync,
    record_event,
    restore_prefs_from_ref,
    set_sync as store_set_sync,
    sync_info,
    user_id,
)

# Same per-process restore guard the learn page uses, so a fresh user with
# nothing on the ref doesn't pay a network fetch on every page load.
_restore_checked: set[str] = set()
_restore_lock = threading.Lock()


def _check_and_restore(root: Path, repo: GitRepo | None, gid: str) -> bool:
    """Restore the user's profile from the orphan ref when the local store
    is empty (so prefs follow the user across machines). Idempotent per
    (root, gid)."""
    if repo is None:
        return False
    with _restore_lock:
        key = f"{root}::{gid}"
        if key in _restore_checked:
            return False
    profile = load_profile(root, gid)
    if not _profile_is_empty(profile):
        with _restore_lock:
            _restore_checked.add(key)
        return False
    restored = False
    try:
        restored = restore_prefs_from_ref(root, repo, gid)
    except Exception:
        restored = False
    with _restore_lock:
        _restore_checked.add(key)
    return restored


def _profile_is_empty(profile: dict[str, Any]) -> bool:
    models = profile.get("models")
    events = profile.get("events")
    has_models = isinstance(models, dict) and bool(models)
    has_events = isinstance(events, list) and bool(events)
    return not (has_models or has_events)


def routing_state(root: Path, repo: GitRepo | None) -> dict[str, Any]:
    """The JSON payload the panel fetches on load. Includes the current
    profile, a recent-events tail (for the event feed), the sync status,
    and a ``me`` echo so the page can greet the user by id."""
    gid = user_id(root, repo)
    restored = _check_and_restore(root, repo, gid)
    profile = load_profile(root, gid)
    return {
        "me": gid,
        "profile": profile,
        "restored": restored,
        "events": load_events(root, gid, limit=50),
        "sync": sync_info(root, repo),
    }


def post_rate(
    root: Path,
    repo: GitRepo | None,
    *,
    rating: int,
    model: str | None = None,
    task_class: str | None = None,
    complexity: str | None = None,
) -> dict[str, Any]:
    """Record a 1-5 rating and return the refreshed state. Bad ratings
    (out of range) are rejected with an explicit error."""
    try:
        rating_int = int(rating)
    except (TypeError, ValueError):
        return {"error": "rating must be an integer 1-5"}
    if not 1 <= rating_int <= 5:
        return {"error": "rating must be an integer 1-5"}
    gid = user_id(root, repo)
    from agitrack.routing.store import SignalEvent

    record_event(
        root,
        gid,
        SignalEvent(
            kind=EVENT_KIND_RATING,
            model=model,
            backend=model.split("/", 1)[0] if model and "/" in model else None,
            task_class=task_class,
            complexity=complexity,
            value=rating_int,
        ),
    )
    if repo is not None:
        try:
            maybe_sync(root, repo)
        except Exception:
            pass
    return {"state": routing_state(root, repo)}


def post_sync(root: Path, repo: GitRepo | None, *, enabled: bool) -> dict[str, Any]:
    """Flip the prefs-sync toggle. Reuses :func:`agitrack.routing.store.set_sync`."""
    return store_set_sync(root, repo, bool(enabled))


# HTML page ---------------------------------------------------------------------


def routing_html(root: Path, *, banner_html: str = "") -> str:
    """The /routing page: per-model acceptance rates, the event feed, and a
    rating widget. Static HTML; the data is fetched on paint via
    ``/routing/state``, like the learn page."""
    del root  # the page itself doesn't need it
    banner = banner_html or ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>aGiTrack — Routing</title>"
        "<style>" + _ROUTING_CSS + "</style>"
        "</head><body>"
        f"<header class='routing-header'>{banner}<h1>Model routing</h1>"
        "<p>Per-model quality signals aGiTrack has learned from your sessions. "
        "Use the rating widget to teach the router which model to prefer for each kind of task.</p>"
        "</header>" + _ROUTING_BODY + "</body></html>"
    )


# The body is intentionally a small set of containers: the per-model list
# (left), the event feed (right), and a rating widget at the bottom. The
# page fetches ``/routing/state`` on load, paints, and re-fetches after
# each rate action. There is no live agent call on the routing page — the
# signals are recorded by the runner, the page just displays them.
_ROUTING_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 1.5rem; color: #1a1a1a; background: #fafafa; }
.routing-header { max-width: 960px; margin: 0 auto 1.5rem; }
.routing-header h1 { margin: 0 0 0.5rem; font-size: 1.6rem; }
.routing-header p { margin: 0; color: #555; }
.routing-main { max-width: 960px; margin: 0 auto; display: flex; flex-direction: column; gap: 1.5rem; }
.routing-section { background: #fff; border: 1px solid #e1e1e1; border-radius: 8px; padding: 1.25rem 1.5rem; }
.routing-section h2 { margin: 0 0 0.75rem; font-size: 1.15rem; }
.routing-subtitle { color: #777; margin: 0 0 0.5rem; font-size: 0.9rem; }
.routing-table { width: 100%; border-collapse: collapse; }
.routing-table th, .routing-table td { padding: 0.5rem 0.75rem; text-align: left; border-bottom: 1px solid #eee; }
.routing-table th { font-size: 0.85rem; color: #555; text-transform: uppercase; }
.routing-events { list-style: none; padding: 0; margin: 0; max-height: 320px; overflow: auto; }
.routing-events li { padding: 0.4rem 0; border-bottom: 1px solid #f0f0f0; font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: 0.85rem; color: #333; }
.routing-empty { color: #888; font-style: italic; }
.routing-rate-buttons { display: flex; gap: 0.5rem; margin: 0.75rem 0 0.5rem; }
.routing-rate-buttons button { background: #fff; border: 1px solid #ccc; border-radius: 4px; padding: 0.5rem 1rem; font-size: 0.9rem; cursor: pointer; }
.routing-rate-buttons button:hover { background: #f5f5f5; border-color: #888; }
.routing-status { color: #555; font-size: 0.9rem; }
.routing-sync-info { color: #555; font-size: 0.9rem; }
#routing-sync-toggle { background: #fff; border: 1px solid #ccc; border-radius: 4px; padding: 0.4rem 0.9rem; cursor: pointer; }
.updatebanner { background: #fff8e1; border: 1px solid #ffe082; padding: 0.5rem 0.75rem; border-radius: 4px; margin-bottom: 1rem; font-size: 0.9rem; color: #5d4037; }
"""

_ROUTING_BODY = """
<main class="routing-main">
  <section class="routing-section">
    <h2>Models</h2>
    <p class="routing-subtitle" id="routing-me"></p>
    <table class="routing-table" id="routing-models">
      <thead>
        <tr>
          <th>Model</th>
          <th>Attempts</th>
          <th>Quality</th>
          <th>Ratings</th>
          <th>Corrections</th>
          <th>Discards</th>
          <th>Reverts</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </section>
  <section class="routing-section">
    <h2>Recent events</h2>
    <ul class="routing-events" id="routing-events"></ul>
  </section>
  <section class="routing-section routing-rate">
    <h2>Rate the last turn</h2>
    <p>Records a 1-5 rating in the routing store so the router can learn
       from it. Disabled when the router is off.</p>
    <div class="routing-rate-buttons">
      <button data-rating="1">★ 1</button>
      <button data-rating="2">★★ 2</button>
      <button data-rating="3">★★★ 3</button>
      <button data-rating="4">★★★★ 4</button>
      <button data-rating="5">★★★★★ 5</button>
    </div>
    <p class="routing-status" id="routing-status"></p>
  </section>
  <section class="routing-section routing-sync">
    <h2>Sync</h2>
    <p>When on, your routing preferences sync to the orphan ref
       <code>refs/agitrack/routing-prefs</code> so they follow you across machines.</p>
    <button id="routing-sync-toggle">Toggle sync</button>
    <p class="routing-sync-info" id="routing-sync-info"></p>
  </section>
</main>
<script>
(function() {
  function renderModels(profile) {
    var tbody = document.querySelector('#routing-models tbody');
    tbody.innerHTML = '';
    var models = (profile && profile.models) || {};
    var names = Object.keys(models);
    if (!names.length) {
      tbody.innerHTML = '<tr><td colspan="7">No model data yet. The router will learn as you use aGiTrack.</td></tr>';
      return;
    }
    names.sort(function(a, b) { return (models[b].quality_ema || 0) - (models[a].quality_ema || 0); });
    names.forEach(function(name) {
      var r = models[name] || {};
      var tr = document.createElement('tr');
      tr.innerHTML = '<td>' + name + '</td>' +
        '<td>' + (r.attempts || 0) + '</td>' +
        '<td>' + ((r.quality_ema == null) ? '—' : (r.quality_ema.toFixed(2))) + '</td>' +
        '<td>' + (r.rating_count || 0) + '</td>' +
        '<td>' + (r.judge_corrections || 0) + '</td>' +
        '<td>' + (r.discards || 0) + '</td>' +
        '<td>' + (r.reverts || 0) + '</td>';
      tbody.appendChild(tr);
    });
  }
  function renderEvents(events) {
    var ul = document.querySelector('#routing-events');
    ul.innerHTML = '';
    if (!events || !events.length) {
      ul.innerHTML = '<li class="routing-empty">No events yet.</li>';
      return;
    }
    events.slice().reverse().forEach(function(ev) {
      var li = document.createElement('li');
      var ts = ev.ts ? new Date(ev.ts * 1000).toISOString() : '';
      var detail = ev.value != null ? (' — ' + JSON.stringify(ev.value)) : '';
      li.textContent = '[' + ts + '] ' + (ev.kind || '?') + ' · ' + (ev.model || '?') + detail;
      ul.appendChild(li);
    });
  }
  function renderSync(sync) {
    var info = document.querySelector('#routing-sync-info');
    if (!sync) { info.textContent = 'No sync info.'; return; }
    if (!sync.available) { info.textContent = 'Sync needs a git remote.'; return; }
    info.textContent = 'Sync ' + (sync.enabled ? 'ON' : 'OFF') + '.';
  }
  function refresh() {
    return fetch('/routing/state', {cache: 'no-store'}).then(function(r) { return r.json(); }).then(function(payload) {
      document.querySelector('#routing-me').textContent = 'Signed in as ' + (payload.me || 'unknown') + '.';
      renderModels(payload.profile);
      renderEvents(payload.events);
      renderSync(payload.sync);
    });
  }
  document.querySelectorAll('.routing-rate-buttons button').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var rating = btn.getAttribute('data-rating');
      var status = document.querySelector('#routing-status');
      status.textContent = 'Recording...';
      fetch('/routing/rate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({rating: parseInt(rating, 10)}),
      }).then(function(r) { return r.json(); }).then(function(payload) {
        if (payload.error) { status.textContent = 'Error: ' + payload.error; return; }
        status.textContent = 'Recorded ' + rating + '-star rating.';
        return refresh();
      });
    });
  });
  document.querySelector('#routing-sync-toggle').addEventListener('click', function() {
    var info = document.querySelector('#routing-sync-info');
    info.textContent = 'Toggling...';
    fetch('/routing/sync', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({})}).then(function(r) { return r.json(); }).then(function(payload) {
      if (payload.error) { info.textContent = 'Error: ' + payload.error; return; }
      return refresh();
    });
  });
  refresh();
})();
</script>
"""


__all__ = [
    "routing_html",
    "routing_state",
    "post_rate",
    "post_sync",
]
