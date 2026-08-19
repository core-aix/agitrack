"""One dashboard, one port, every repository — switched by URL path.

Each repository used to get its own dashboard daemon on its own port, and each *view* of it got
another: the live dashboard on 8765, its backtrace on 8766, the next project on 8767, and so on
upward. That is a port to remember per project, a separate `ssh -L` line for each, a bookmark that
goes stale the moment the daemons come up in a different order, and no way at all to get from one
project's dashboard to another's except by finding the terminal you started it from.

So there is one hub instead. It binds a single port and mounts every repository under a path:

    /                     the repository you used last, in the view that fits it
    /r/<slug>/            a repository's LIVE dashboard (aGiTrack's own tracking)
    /b/<slug>/            the same repository's BACKTRACE (reconstructed from transcripts)
    /repos                what the repo switcher in the page header is built from

``<slug>`` comes from :func:`agitrack.repos.slug_for`, so it is derived from the path rather than
handed out: every process computes the same URL for the same repository without asking the hub.

The trailing slash on a mount is load-bearing. Every page fetches its data with RELATIVE URLs
(``data``, ``log``, ``learn/state``), which is what lets the exact same page code serve from the
root of a standalone daemon and from a mount point here; ``/r/<slug>`` without the slash would
resolve them against ``/r/``. Requests that arrive without it are redirected rather than fixed up,
so the browser's address bar and the page's own relative links never disagree.

Scopes are built lazily and kept: mounting a repository costs nothing until someone looks at it,
and a backtrace reconstruction (the expensive one) is built on first request into that view.
"""

from __future__ import annotations

import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from agitrack import repos as repo_registry
from agitrack.git import GitError, GitRepo
from agitrack.metrics.routing import Response, html_response, json_response, redirect
from agitrack.proc import detach_kwargs, pid_alive, terminate_pid

# Mount prefixes. Short on purpose: they are typed by hand more often than any other part of the
# URL, and they read as what they are once you know the two dashboards ("r" for the repository as
# aGiTrack tracked it, "b" for the backtrace it reconstructed).
ACTIVE_PREFIX = "r"
BACKTRACE_PREFIX = "b"
# "Take me to this repository, in whichever view suits it" — the prefix the repo switcher uses.
# It is a REDIRECT, not a third view: the page you land on is always /r/ or /b/, so the address
# bar, the toggle and a bookmark all agree about what you are looking at.
CHOOSE_PREFIX = "go"

_PREFIX_VIEW = {ACTIVE_PREFIX: repo_registry.ACTIVE, BACKTRACE_PREFIX: repo_registry.BACKTRACE}
_VIEW_PREFIX = {view: prefix for prefix, view in _PREFIX_VIEW.items()}


def mount_path(slug: str, view: str = repo_registry.ACTIVE, subpath: str = "") -> str:
    """The URL path a repository's view is served at, always with its trailing slash."""
    prefix = _VIEW_PREFIX.get(view, ACTIVE_PREFIX)
    return f"/{prefix}/{slug}/{subpath.lstrip('/')}"


def choose_path(slug: str, subpath: str = "") -> str:
    """The URL that opens a repository in whichever view suits it (see :func:`preferred_view`)."""
    return f"/{CHOOSE_PREFIX}/{slug}/{subpath.lstrip('/')}"


def split_choose(path: str) -> tuple[str, str] | None:
    """``(slug, subpath)`` for a ``/go/`` path, or None when it is not one."""
    parts = path.split("/")
    if len(parts) < 3 or parts[0] != "" or parts[1] != CHOOSE_PREFIX or not parts[2]:
        return None
    return parts[2], "/".join(parts[3:])


def split_mount(path: str) -> tuple[str, str, str] | None:
    """``(view, slug, subpath)`` for a mounted path, or None when it is not one.

    ``subpath`` is what the scope sees, and it always begins with ``/`` so a scope's routing table
    is identical whether it is mounted here or serving at the root of its own daemon."""
    parts = path.split("/")
    if len(parts) < 3 or parts[0] != "":
        return None
    view = _PREFIX_VIEW.get(parts[1])
    if view is None or not parts[2]:
        return None
    subpath = "/" + "/".join(parts[3:])
    return view, parts[2], subpath


# --------------------------------------------------------------------------- which view to show


def preferred_view(directory: Path, *, starting_tracking: bool = False) -> str:
    """The view a repository should open in.

    Rules, in order:

    0. **Starting a tracking mode shows the tracking.** `agitrack -b` and `agitrack -i` are the
       user saying "record this repository from now on", and the reconstruction is emphatically
       not that: it is the inferred history of what happened BEFORE. A remembered preference for
       the backtrace was still winning here, so starting a tracker on a fully tracked repository
       opened the one view that could not show the work about to be recorded. Asking for the
       reconstruction has its own command, `--backtrace`. The rules below still apply when there
       is nothing tracked yet, so a first-ever run on an old project still gets its history.
    1. A repository with **nothing tracked** but reconstructable sessions opens on the BACKTRACE.
       An empty live dashboard is the worst possible answer when the history is sitting in the
       backends' own transcripts.
    2. The **first time** it does have tracked commits carrying token counts, it flips to the live
       dashboard, once. That transition is the moment the recorded history becomes better than the
       inferred one, and it is worth showing unasked — but only the once, which is what
       ``tracked_seen`` in the repo registry remembers.
    3. After that, whatever view the user last chose stands. Toggling views is a decision, and a
       page that keeps overriding it is a page arguing with its reader.
    """
    entry = repo_registry.entry_for(directory)
    remembered = entry.view if entry else ""
    if _has_tracked_tokens(directory):
        if starting_tracking or (entry is not None and not entry.tracked_seen):
            repo_registry.mark_tracked_seen(directory)
            repo_registry.set_view(directory, repo_registry.ACTIVE)
            return repo_registry.ACTIVE
        return remembered or repo_registry.ACTIVE
    if remembered:
        return remembered
    return repo_registry.BACKTRACE if _has_backtrace(directory) else repo_registry.ACTIVE


def _has_tracked_tokens(directory: Path) -> bool:
    from agitrack.metrics.suggest import has_tracked_tokens

    try:
        repo = GitRepo.discover(directory)
    except (GitError, OSError):
        return False  # not a repository at all: there is genuinely nothing tracked here
    try:
        return has_tracked_tokens(repo)
    except Exception:
        # Anything else is a probe that did not work, and `has_tracked_tokens` is careful never
        # to divert on one. A blanket `except: return False` here quietly threw that away and
        # could send a fully tracked repository to the reconstruction.
        return True


def _has_backtrace(directory: Path) -> bool:
    from agitrack.metrics.suggest import has_backtrace_history

    return has_backtrace_history(directory)


# --------------------------------------------------------------------------- mounted scopes


class _Mounts:
    """The scopes the hub is serving, built on demand and kept.

    A mount is created the first time someone asks for it, not when a repository is remembered:
    the registry can name a dozen projects, and building a dashboard (let alone a reconstruction)
    for one nobody opened would spend the user's CPU on a page that does not exist yet.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, Any] = {}
        self._backtrace: dict[str, Any] = {}
        # Per-directory live reconstruction state, so a backtrace mount keeps up with new sessions
        # exactly as the standalone daemon does.
        self._backtrace_live: dict[str, dict] = {}

    def directory_for(self, slug: str) -> Path | None:
        entry = repo_registry.find(slug)
        return entry.directory if entry is not None else None

    def active(self, slug: str):
        with self._lock:
            scope = self._active.get(slug)
            if scope is not None:
                return scope
        directory = self.directory_for(slug)
        if directory is None:
            return None
        try:
            repo = GitRepo.discover(directory)
        except (GitError, OSError):
            return None
        from agitrack.metrics.server import RepoScope

        # The live dashboard's "the backtrace can see work you have not committed" notice is
        # exact when the reconstruction for this repo is already built, and the cheap estimate
        # when it is not. Passed as a callable rather than a value because which of the two
        # applies changes the moment someone opens the backtrace view.
        scope = RepoScope(repo, pending_source=lambda: self.pending_for(slug, directory))
        with self._lock:
            return self._active.setdefault(slug, scope)

    def pending_for(self, slug: str, directory: Path):
        """What the backtrace holds for this repo that its tracked commits do not.

        Never BUILDS the reconstruction: spending minutes of the user's CPU to decorate a
        dashboard they did not ask to have decorated is exactly the wrong trade. It uses one only
        if it is already there."""
        from agitrack.metrics.pending import pending_work

        live = self._backtrace_live.get(slug)
        return pending_work(directory, (live or {}).get("view"))

    def backtrace(self, slug: str):
        with self._lock:
            scope = self._backtrace.get(slug)
            if scope is not None:
                return scope
        directory = self.directory_for(slug)
        if directory is None:
            return None
        # Built OUTSIDE the lock: a reconstruction can take a while on a long history, and holding
        # the mount lock through it would stall every other repository's requests.
        scope = self._build_backtrace(slug, directory)
        with self._lock:
            return self._backtrace.setdefault(slug, scope)

    def _build_backtrace(self, slug: str, directory: Path):
        from agitrack.metrics.backtrace import (
            BacktraceScope,
            _clear_progress,
            _discover,
            _watch_transcripts,
            _write_progress,
            build_backtrace,
        )

        sources = _discover(directory)
        memo: dict[str, tuple[tuple, dict]] = {}
        view = build_backtrace(
            directory,
            sources=sources,
            memo=memo,
            progress=lambda done, total, phase: _write_progress(directory, done, total, phase),
        )
        _clear_progress(directory)
        live: dict = {"view": view, "sources": sources}
        self._backtrace_live[slug] = live
        stop = threading.Event()
        threading.Thread(
            target=_watch_transcripts,
            args=(directory, live, memo, stop),
            daemon=True,
            name=f"agitrack-backtrace-watch-{slug}",
        ).start()
        return BacktraceScope(lambda: live["view"])

    def scope(self, view: str, slug: str):
        return self.active(slug) if view == repo_registry.ACTIVE else self.backtrace(slug)

    def drop(self, slug: str) -> None:
        with self._lock:
            self._active.pop(slug, None)
            self._backtrace.pop(slug, None)
            self._backtrace_live.pop(slug, None)


# --------------------------------------------------------------------------- open pages
#
# Every mode opens the dashboard when it starts, and on a machine where you work in several
# repositories that meant a new browser tab each time, all showing the same dashboard on the same
# port. The hub cannot see the browser, so the PAGES tell it they exist: each one pings while it
# is open, and a launcher that wants to show a repository asks an already-open dashboard tab to
# go there instead of opening another.

# How long a page may go without pinging before it is presumed closed. Comfortably more than the
# ping interval, so one slow frame never evicts a live tab.
_CLIENT_TTL_SECONDS = 15.0
# How long a launcher waits for an open tab to pick up a navigation before giving up and opening
# a browser itself. Long enough for one ping to land, short enough not to be felt.
_NAVIGATE_WAIT_SECONDS = 4.0


class _Clients:
    """The dashboard pages currently open, as far as they have told us.

    Deliberately in-memory and best-effort: this exists to avoid a redundant browser tab, and the
    worst outcome of getting it wrong is the extra tab we have today. Nothing depends on it being
    right, so nothing is persisted and nothing is locked beyond this dict."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, dict] = {}
        # Which ping came last, counted rather than timed. `time.monotonic()` is ~15.6ms coarse on
        # Windows, so two tabs pinging inside one tick get an identical timestamp and "the most
        # recent tab" then fell through to comparing client IDS — steering the alphabetically-last
        # tab instead of the one you were just looking at. A counter cannot tie.
        self._order = 0

    def ping(self, client_id: str, path: str, page: str, browser: str = "") -> str:
        """Record that this page is open; return a URL it has been asked to navigate to, if any."""
        if not client_id:
            return ""
        with self._lock:
            self._expire()
            self._order += 1
            record = self._seen.setdefault(client_id, {})
            record.update(path=path, page=page, seen=time.monotonic(), order=self._order)
            if browser:
                record["browser"] = browser
            target = str(record.pop("navigate", "") or "")
            if target:
                waiter = record.pop("waiter", None)
                if waiter is not None:
                    waiter.set()
            return target

    def close(self, client_id: str) -> None:
        """A page said it is going away (a closed tab, a navigation). Drop it now rather than
        waiting out the TTL, so a navigation is never handed to a tab that has gone."""
        with self._lock:
            self._seen.pop(client_id, None)

    def navigate(self, url: str, *, timeout: float = _NAVIGATE_WAIT_SECONDS) -> str | None:
        """Ask an open DASHBOARD tab to go to ``url``. True once one has taken it.

        Only a dashboard page qualifies. A tab showing the story or the learn page is somewhere
        the reader chose to be, about a repository they chose; steering it to another project's
        dashboard would take that away, so those get a new tab instead.

        Waits for the page to actually pick the navigation up. A tab closed a second ago still
        looks open here, and queueing into that would leave the user with no window at all.

        Returns the steered tab's BROWSER FAMILY (possibly ``""`` if it did not say) once one has
        taken the navigation, or None when nothing did. The caller needs it to raise the right
        window: a page cannot raise itself."""
        waiter = threading.Event()
        with self._lock:
            self._expire()
            target = self._pick_dashboard()
            if target is None:
                return None
            self._seen[target]["navigate"] = url
            self._seen[target]["waiter"] = waiter
        if waiter.wait(timeout):
            with self._lock:
                return str((self._seen.get(target) or {}).get("browser") or "")
        with self._lock:  # nobody took it: drop it so it cannot fire later, out of context
            record = self._seen.get(target)
            if record is not None:
                record.pop("navigate", None)
                record.pop("waiter", None)
        return None

    def _pick_dashboard(self) -> str | None:
        """The most recently active open dashboard page, or None.

        Ordered by the ping COUNTER, not the clock: see ``_order``. The timestamp is still what
        decides whether a page is alive at all, which coarse resolution cannot get wrong."""
        candidates = [(record.get("order", 0), key) for key, record in self._seen.items() if not record.get("page")]
        return max(candidates)[1] if candidates else None

    def _expire(self) -> None:
        cutoff = time.monotonic() - _CLIENT_TTL_SECONDS
        for key in [k for k, record in self._seen.items() if record.get("seen", 0) < cutoff]:
            self._seen.pop(key, None)

    def snapshot(self) -> list[dict]:
        with self._lock:
            self._expire()
            return [{"page": r.get("page", ""), "path": r.get("path", "")} for r in self._seen.values()]


# --------------------------------------------------------------------------- routing


class HubRouter:
    """Turns a request path into the right scope's answer.

    Kept separate from the HTTP handler so the whole routing surface can be exercised without
    opening a socket, and so the hub's own pages (the picker, ``/repos``) sit next to the mounts
    they link to rather than inside a request handler."""

    def __init__(self) -> None:
        self.mounts = _Mounts()
        self.clients = _Clients()

    def get(self, path: str, query: dict[str, list[str]]) -> Response | None:
        if path in ("", "/"):
            return self._root()
        if path == "/repos":
            return json_response({"repos": self.repo_list()})
        if path == "/clients":
            return json_response({"clients": self.clients.snapshot()})
        chosen = split_choose(path)
        if chosen is not None:
            # Switching repository must not carry the CURRENT view across: the view that suits one
            # project is not the view that suits another, and arriving on a project's empty
            # tracked dashboard because the last one happened to be tracked is exactly the empty
            # page the whole view-selection rule exists to avoid.
            slug, subpath = chosen
            entry = repo_registry.find(slug)
            if entry is None:
                return self._unknown_repo(slug)
            return redirect(mount_path(slug, preferred_view(entry.directory), subpath))
        mount = split_mount(path)
        if mount is None:
            return None
        view, slug, subpath = mount
        entry = repo_registry.find(slug)
        if entry is None:
            return self._unknown_repo(slug)
        if subpath == "/" and not path.endswith("/"):
            # `/r/<slug>` with no trailing slash: every relative fetch on the page would resolve
            # one level too high. Redirect rather than serve, so the address bar agrees with the
            # links the page draws.
            return redirect(mount_path(slug, view))
        scope = self.mounts.scope(view, slug)
        if scope is None:
            return self._unknown_repo(slug)
        if subpath in ("/", "/index.html"):
            self._remember_view(entry, view)
        return scope.get(subpath, query)

    def post(self, path: str, body: dict) -> Response | None:
        if path == "/clients":
            # A page saying "I am open" (and being told where to go, if anywhere).
            if body.get("closing"):
                self.clients.close(str(body.get("id") or ""))
                return json_response({"ok": True})
            target = self.clients.ping(
                str(body.get("id") or ""),
                str(body.get("path") or ""),
                str(body.get("page") or ""),
                str(body.get("browser") or ""),
            )
            return json_response({"navigate": target})
        if path == "/navigate":
            # A launcher asking an already-open dashboard tab to show a repository. The answer
            # carries which browser took it, so the launcher can raise that window.
            steered = self.clients.navigate(str(body.get("url") or ""))
            return json_response({"navigated": steered is not None, "browser": steered or ""})
        mount = split_mount(path)
        if mount is None:
            return None
        view, slug, subpath = mount
        scope = self.mounts.scope(view, slug)
        return None if scope is None else scope.post(subpath, body)

    # ------------------------------------------------------------------ hub pages

    def _root(self) -> Response:
        """Send the visitor to the repository they used last, in the view that fits it."""
        entries = repo_registry.list_repos()
        if not entries:
            return html_response(_no_repos_html())
        entry = entries[0]
        return redirect(mount_path(entry.slug, preferred_view(entry.directory)))

    def repo_list(self) -> list[dict]:
        """Every repository the hub can switch to, most recently used first (see
        :func:`_served_repos` for what "every" means).

        Deliberately cheap: no dashboard is built to answer it. The page header polls this to fill
        its switcher, and a hub with ten projects mounted must not walk ten histories to draw a
        dropdown. The per-repo tracking state costs a handshake file and a pid check each, which
        is on the same order as listing the repositories at all — and it is what turns the
        switcher from a list of names into an answer to "where am I actually tracking?"."""
        from agitrack.proxy.background import running_mode_for

        out = []
        for entry in _served_repos():
            state = running_mode_for(entry.directory)
            out.append(
                {
                    "slug": entry.slug,
                    "name": entry.name,
                    "path": _display(entry.path),
                    "active_url": mount_path(entry.slug, repo_registry.ACTIVE),
                    "backtrace_url": mount_path(entry.slug, repo_registry.BACKTRACE),
                    # What the switcher navigates to: the server picks the view on arrival.
                    "go_url": choose_path(entry.slug),
                    "last_seen": entry.last_seen,
                    "running": bool(state.get("running")),
                    # The mode in as few words as a dropdown row can carry; the full sentence is
                    # the row's tooltip.
                    "state": _short_state(state),
                    "state_detail": str(state.get("detail") or ""),
                }
            )
        return out

    def _unknown_repo(self, slug: str) -> Response:
        return html_response(_unknown_repo_html(slug), status=404)

    @staticmethod
    def _remember_view(entry, view: str) -> None:
        # Only on a change: this runs on every page load, and rewriting the registry file for an
        # answer that did not move is pure IO on the hot path.
        if entry.view != view:
            repo_registry.set_view(entry.path, view)


def _served_repos() -> list:
    """The repositories the switcher offers: every one aGiTrack is RUNNING on right now, plus
    every one it remembers having worked on (which is what keeps a repo listed after its tracker
    stops, and what lists a repo opened with `-d` alone).

    The remembered list is the primary source; live daemons are unioned in because a repository
    aGiTrack is demonstrably tracking must never be missing from the switcher — and it was.
    `repos.remember` was reachable only through `ensure_hub_for`, so a tracker that opened no
    dashboard (the autotrack hook, a scripted or non-TTY start, `open_dashboard_on_start` off)
    never got an entry; a live example ran for hours while its repo was absent from the dropdown.
    The union also covers an entry lost to `repos.json`'s deliberately unlocked write.

    A repo found only by its daemon is REMEMBERED as it is added, so the repair happens once and
    the repo then behaves like any other — it survives its tracker stopping, and it keeps the
    view the user last chose. Writes only on the first sighting, so the polling this endpoint
    serves stays a read.

    A repo the user stopped with `agitrack stop` is NOT resurrected, even if a daemon for it is
    somehow still alive: `served=False` is an explicit decision, and a switcher that keeps
    offering a project the user just stopped is ignoring them. Only a repo with no entry at all
    is adopted. (`agitrack stop` stops the daemons before clearing the flag, so the two states
    do not normally coexist; this makes the outcome independent of that ordering.)"""
    entries = repo_registry.list_repos()
    known = {str(Path(entry.path).expanduser()) for entry in entries}
    try:
        from agitrack import daemons

        live = daemons.running_repos()
    except Exception:
        return entries  # the switcher must draw even if the daemon registry cannot be read
    added = False
    for path in live:
        if path in known or not Path(path).is_dir():
            continue
        if repo_registry.entry_for(path) is not None:
            continue  # known but unserved: the user stopped it, so leave it stopped
        try:
            repo_registry.remember(path)
            added = True
        except Exception:
            continue  # a repo we cannot record is still not worth dropping from the list
    if not added:
        return entries
    # Re-read rather than splicing: `remember` decides slug/name/order, and the list the switcher
    # draws must be in the same order every caller sees.
    return repo_registry.list_repos()


def _short_state(state: dict) -> str:
    """A tracking state short enough for a dropdown row.

    ``running_mode``'s label reads well on its own line in the header ("tracking · background ·
    auto commits") and is far too long next to twenty repository names, so the row keeps the part
    that differs between repositories and drops the word they would all share."""
    if not state.get("running"):
        return "off"
    kind = str(state.get("kind") or "")
    if kind == "background":
        return "background" + (" · manual" if "manual" in str(state.get("label") or "") else "")
    if kind == "interactive":
        return "interactive" + (" · manual" if "manual" in str(state.get("label") or "") else "")
    return "tracking"


def _display(path: str) -> str:
    from agitrack.metrics.collect import _abbreviate_home

    return _abbreviate_home(path)


def _page(title: str, body: str) -> str:
    from agitrack.metrics import ui

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>aGiTrack - {title}</title><style>{ui.TOKENS}{ui.BASE_CSS}"
        "body{background:var(--ink);color:var(--fg);font-family:var(--mono);margin:0;padding:48px 24px}"
        ".box{max-width:640px;margin:0 auto;background:var(--panel);border:1px solid var(--line);padding:28px}"
        "h1{font-family:var(--display);color:var(--phosphor);font-size:26px;margin:0 0 14px}"
        "p{line-height:1.6}code{background:var(--panel2);padding:1px 6px;color:var(--phosphor)}"
        "</style></head><body><div class='box'>" + body + "</div></body></html>"
    )


def _no_repos_html() -> str:
    return _page(
        "Dashboard",
        "<h1>Nothing to show yet</h1>"
        "<p>aGiTrack has not been pointed at any repository on this machine, so there is no "
        "dashboard to open.</p>"
        "<p>Run <code>agitrack</code> in a project and pick a mode, or <code>agitrack -d</code> "
        "to open that project's dashboard directly. It will appear here, and in the repository "
        "switcher at the top of every page.</p>",
    )


def _unknown_repo_html(slug: str) -> str:
    return _page(
        "Dashboard",
        "<h1>No such repository</h1>"
        f"<p>This dashboard is not serving <code>{slug}</code>. It may have been stopped with "
        "<code>agitrack stop</code>, or its directory may be gone.</p>"
        "<p><a href='/'>Back to the dashboard</a></p>",
    )


# --------------------------------------------------------------------------- HTTP


def build_handler(router: HubRouter) -> type[http.server.BaseHTTPRequestHandler]:
    from agitrack.metrics.server import read_json_body, write_response

    class _HubHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            try:
                parsed = urllib.parse.urlparse(self.path)
                write_response(self, router.get(parsed.path, urllib.parse.parse_qs(parsed.query)))
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            try:
                parsed = urllib.parse.urlparse(self.path)
                body = read_json_body(self)
                if parsed.path == "/clients" and not body.get("browser"):
                    # The page says which browser it is in, but a tab loaded before it learned to
                    # says nothing — and it is still the tab that has to be raised. The header is
                    # on every request either way, so it fills the gap without a reload.
                    from agitrack.metrics.server import browser_family_from_user_agent

                    body["browser"] = browser_family_from_user_agent(self.headers.get("User-Agent", ""))
                write_response(self, router.post(parsed.path, body))
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args: object) -> None:
            """Stay quiet: the dashboard is a tool, not a web log."""

    return _HubHandler


# --------------------------------------------------------------------------- daemon lifecycle


def handshake_path() -> Path:
    """Where the hub records its pid and URL. GLOBAL, not per repo: there is one hub."""
    from agitrack.env import getenv_compat

    config_dir = getenv_compat("CONFIG_DIR")
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".agitrack"
    return base / "dashboard.json"


def log_path() -> Path:
    return handshake_path().with_name("dashboard.log")


def read_handshake() -> dict[str, Any] | None:
    try:
        with handshake_path().open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def clear_handshake() -> None:
    try:
        handshake_path().unlink()
    except OSError:
        pass


def _write_handshake(record: dict[str, Any]) -> None:
    from agitrack.fileio import atomic_write_text

    path = handshake_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(record))
    except OSError:
        pass


def running_hub() -> dict[str, Any] | None:
    """The handshake of a hub that is actually alive, else None (pruning a stale record)."""
    record = read_handshake()
    if record is None:
        return None
    pid = record.get("pid")
    if isinstance(pid, int) and pid_alive(pid):
        return record
    clear_handshake()
    return None


def hub_url() -> str:
    record = running_hub()
    return str(record.get("url") or "") if record else ""


def _spawn_hub() -> subprocess.Popen[bytes] | None:
    """Start the hub as a detached child, freeing whatever terminal asked for it."""
    # Built explicitly rather than from this process's argv: the hub takes no arguments beyond its
    # own flag, and inheriting the launcher's (say `--backtrace`) would start the wrong thing.
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--hub-serve"]
    else:
        command = [sys.executable, "-m", "agitrack", "--hub-serve"]
    try:
        log_path().parent.mkdir(parents=True, exist_ok=True)
        handle = log_path().open("ab")
    except OSError:
        handle = subprocess.DEVNULL  # type: ignore[assignment]
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            **detach_kwargs(),
        )
    except OSError:
        return None


def start_hub(*, timeout: float = 20.0) -> dict[str, Any] | None:
    """Ensure the hub is running and return its handshake record.

    Idempotent and race-tolerant: two repositories opening a dashboard at the same instant both
    call this, and the second finds the first's handshake rather than binding a second port."""
    record = running_hub()
    if record is not None:
        return record
    child = _spawn_hub()
    if child is None:
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = running_hub()
        if record is not None:
            return record
        if child.poll() is not None:
            # The child died before publishing. Another process may have won the race and be
            # serving already, so check once more before giving up.
            return running_hub()
        time.sleep(0.05)
    return None


def ensure_hub_for(directory: Path, *, view: str = "", starting_tracking: bool = False) -> tuple[str, str]:
    """Remember ``directory``, make sure the hub is up, and return ``(url, view)`` for it.

    This is the single entry point every mode uses: `-d`, `--backtrace`, the TUI's dashboard
    shortcut and the background tracker all want the same thing, which is "the page for this
    repository, wherever the hub happens to be"."""
    entry = repo_registry.remember(directory)
    chosen = view or preferred_view(directory, starting_tracking=starting_tracking)
    record = start_hub()
    if record is None:
        return "", chosen
    base = str(record.get("url") or "").rstrip("/")
    return base + mount_path(entry.slug, chosen), chosen


def open_dashboard(
    directory: Path,
    *,
    view: str = "",
    open_browser: bool = True,
    quiet: bool = False,
    starting_tracking: bool = False,
) -> int:
    """`agitrack -d` and `agitrack --backtrace`: show this repository's dashboard.

    Both commands do the same three things now — remember the repository, make sure the one hub is
    up, open the right path on it — and differ only in which view they ask for. The hub survives
    this terminal and keeps serving every other repository too, so it is stopped by
    ``agitrack stop`` (this repository) or ``agitrack -d stop`` (all of them), never by closing
    the window."""
    from agitrack.metrics.server import exposure_note, open_dashboard_in_browser, remote_access_help

    was_running = running_hub() is not None
    url, chosen = ensure_hub_for(directory, view=view, starting_tracking=starting_tracking)
    if not url:
        if not quiet:
            # Two failures reach here and they need different answers: a port nobody can bind,
            # and a child that died on startup. The log has the actual reason either way, so it
            # is named first, followed by the one knob that fixes the common case.
            print(
                "The aGiTrack dashboard did not start.\n"
                f"  Why: see {log_path()} (the last lines are the child's own output).\n"
                "  If something else already holds the port, aGiTrack tries the next few; if all\n"
                "  of them are taken, free one or set AGITRACK_DASHBOARD_HOST to another address.\n"
                "  Everything else still works: tracking does not depend on the dashboard."
            )
        return 1
    record = running_hub() or {}
    if not quiet:
        served = len(repo_registry.list_repos())
        started = "already running" if was_running else f"live at {str(record.get('url', '')).rstrip('/')}"
        others = "" if served <= 1 else f" It also serves {served - 1} other repositor{'y' if served == 2 else 'ies'}."
        print(
            f"aGiTrack dashboard {started} (pid {record.get('pid')}).\n"
            f"  {'backtrace' if chosen == repo_registry.BACKTRACE else 'dashboard'} for this repo: {url}\n"
            + exposure_note(str(record.get("host", "")))
            + f"One dashboard serves every repository, switchable from the page header.{others}\n"
            "It runs in the background (surviving this terminal) until `agitrack -d stop`."
        )
    # Before opening anything: is a dashboard tab already open? Steering it beats stacking up a
    # tab per repository, all of them the same dashboard on the same port.
    if open_browser and _steer_open_tab(record, url):
        if not quiet:
            print("  (switched the dashboard tab you already had open)")
        return 0
    if open_browser and not open_dashboard_in_browser(url) and not quiet:
        help_text = remote_access_help(url, int(record.get("port", 0) or 0), bind_host=str(record.get("host", "")))
        if help_text:
            print(help_text)
    return 0


def _steer_open_tab(record: dict, url: str) -> bool:
    """Ask the hub to send an already-open dashboard tab to ``url``. True once one has gone there.

    Best-effort by construction: any failure (no hub, an old hub without the endpoint, a browser
    with nothing open) just answers False and the caller opens a tab, which is what it did before
    this existed. The hub does the waiting, so this returns only once a real page has taken it."""
    base = str(record.get("url") or "").rstrip("/")
    if not base:
        return False
    import json as _json
    import urllib.error
    import urllib.request

    try:
        request = urllib.request.Request(
            f"{base}/navigate",
            data=_json.dumps({"url": url}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=_NAVIGATE_WAIT_SECONDS + 3.0) as response:
            answer = _json.loads(response.read().decode("utf-8"))
        if not answer.get("navigated"):
            return False
        # The tab now holds the right page, but it may be behind three other windows, and a page
        # that changed where nobody can see it is indistinguishable from nothing happening.
        from agitrack.metrics.server import raise_browser_window

        raise_browser_window(str(answer.get("browser") or ""))
        return True
    except Exception:
        return False


def unmount_repo(directory: Path | str) -> bool:
    """Stop serving ``directory``, and stop the hub if nothing is left to serve.

    What `agitrack stop` means for the dashboard. The repository stays in the registry (so its
    remembered view and the once-only switch survive), it is simply no longer offered: a hub that
    kept listing a project the user explicitly stopped would be ignoring them."""
    entry = repo_registry.entry_for(directory)
    if entry is None or not entry.served:
        return False
    repo_registry.set_served(directory, False)
    if not repo_registry.list_repos():
        stop_hub(quiet=True)
    return True


def stop_hub(*, quiet: bool = False) -> int:
    """Stop the hub daemon. Exit code, so it can be a command's whole implementation."""
    record = running_hub()
    if record is None:
        if not quiet:
            print("The aGiTrack dashboard is not running.")
        return 0
    pid = record.get("pid")
    if isinstance(pid, int):
        terminate_pid(pid)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and pid_alive(pid):
            time.sleep(0.05)
        if pid_alive(pid):
            if not quiet:
                print(f"The aGiTrack dashboard (PID {pid}) did not stop in time.")
            return 1
        from agitrack.daemons import deregister

        deregister(pid)
    clear_handshake()
    if not quiet:
        print("Stopped the aGiTrack dashboard.")
    return 0


def hub_status() -> int:
    record = running_hub()
    if record is None:
        print("The aGiTrack dashboard is not running. Start it with `agitrack -d`.")
        return 0
    print(f"The aGiTrack dashboard is running on {record.get('url')} (PID {record.get('pid')}).")
    served = repo_registry.list_repos()
    if served:
        print(f"Serving {len(served)} repositor{'y' if len(served) == 1 else 'ies'}:")
        for entry in served:
            print(
                f"  {_display(entry.path)}  {record.get('url', '').rstrip('/')}{mount_path(entry.slug, entry.view or repo_registry.ACTIVE)}"
            )
    return 0


def lock_path() -> Path:
    """The lock that makes the hub SINGLE. See :func:`run_hub_daemon`."""
    return handshake_path().with_name("dashboard.lock")


def run_hub_daemon(*, host: str | None = None, port: int | None = None) -> int:
    """The detached child: serve every remembered repository until told to stop.

    **Exactly one hub may exist**, and the lock below is what guarantees it. Checking the
    handshake before spawning cannot: two aGiTrack runs starting a second apart (which is
    ordinary — a repo opening its dashboard while another mode starts on another) both saw no
    handshake, both spawned, and the second bound the NEXT port because the port scan politely
    stepped around its twin. The result was two dashboards on two ports, each serving a different
    subset of what the other had cached, and a handshake naming whichever won the last write.

    An OS file lock held for the process's lifetime settles it in the one place that can see both
    processes at once. The loser exits immediately and silently: the winner is already serving, so
    there is nothing to report and nothing to retry.
    """
    from agitrack import daemons
    from agitrack.git import RepoLock
    from agitrack.metrics.server import DEFAULT_PORT, _DashboardServer, bind_scanning, dashboard_url, default_bind_host
    from agitrack.update import restart as update_restart

    lock = RepoLock(lock_path())
    # A short retry, not zero: the update-restart path spawns the replacement while the outgoing
    # hub is still tearing down, and that hand-off must not be mistaken for a rival.
    if not lock.acquire(retry_seconds=5.0):
        return 0

    bind_host = default_bind_host() if host is None else host
    preferred_port = DEFAULT_PORT if port is None else port
    router = HubRouter()
    handler = build_handler(router)

    current: dict = {"server": None, "stop": None}
    explicit_stop = threading.Event()

    def _request_shutdown(*_: object) -> None:
        explicit_stop.set()
        stop, server = current.get("stop"), current.get("server")
        if stop is not None:
            stop.set()
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    while True:
        server = bind_scanning(lambda address: _DashboardServer(address, handler), bind_host, preferred_port)
        bound_port = int(server.server_address[1])
        url = dashboard_url(bind_host, bound_port)
        _write_handshake(
            {"pid": os.getpid(), "host": bind_host, "port": bound_port, "url": url, "started": int(time.time())}
        )
        daemons.register("hub", "", url=url)

        stop = threading.Event()
        current.update(server=server, stop=stop)
        if explicit_stop.is_set():
            stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        restart_wanted = threading.Event()

        def _restart_for_update(_version: str, *, _stop=stop, _server=server) -> None:
            restart_wanted.set()
            _stop.set()
            threading.Thread(target=_server.shutdown, daemon=True).start()

        # Restart LAST, after the repos' background trackers have taken the update: a view that
        # reloads itself onto the new version while a tracker is still on the old one announces
        # the update and warns about a stale session in the same breath, for a tracker that is
        # already restarting itself. See restart.stale_background_trackers.
        update_restart.watch_for_update(
            stop,
            _restart_for_update,
            self_update=True,
            defer_while=update_restart.stale_background_trackers,
            log=lambda message: print(f"aGiTrack: {message}", flush=True),
        )

        try:
            server.serve_forever()
        finally:
            server.server_close()
            clear_handshake()
            daemons.deregister()
        if explicit_stop.is_set() or not restart_wanted.is_set():
            lock.release()
            return 0
        # Hand the lock over BEFORE spawning the replacement, or the new hub would spend its
        # whole retry window waiting on a process that is waiting for it.
        lock.release()
        # Restart by spawn-and-verify rather than exec: a replacement running a broken update
        # could crash on startup, and an exec'd-over process has nothing left to retry with.
        child = _spawn_hub()
        if child is None:
            continue  # could not spawn: keep serving on the old version rather than vanish
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            record = running_hub()
            if record is not None and record.get("pid") != os.getpid():
                return 0
            if child.poll() is not None:
                break
            time.sleep(0.1)
        # The replacement never came up. Reap it and serve on; the next update check retries.
        try:
            if child.poll() is None:
                terminate_pid(child.pid)
        except Exception:
            pass


__all__ = [
    "ACTIVE_PREFIX",
    "BACKTRACE_PREFIX",
    "HubRouter",
    "choose_path",
    "ensure_hub_for",
    "hub_status",
    "hub_url",
    "mount_path",
    "preferred_view",
    "run_hub_daemon",
    "split_choose",
    "split_mount",
    "start_hub",
    "stop_hub",
    "unmount_repo",
]
