"""HTTP server for the live aGiTrack dashboard (#54).

`agitrack --dashboard` serves the HTML dashboard and opens it in the browser.
The page polls ``/data`` on an interval and re-renders, so the dashboard
reflects new commits as they land — useful for watching an agent work.
Everything is recomputed from ``git log`` on each request: read-only, no state,
identical on every clone.

Where it binds depends on where aGiTrack is running. On your own machine it
stays on loopback; in a **remote shell** (SSH/Mosh) loopback would be reachable
only from the remote box itself, so it binds all interfaces instead and
advertises the remote's own IP — open it directly if the firewall allows, or
copy-paste the printed `ssh -L` command if it doesn't. Set
``AGITRACK_DASHBOARD_HOST`` to pin a bind address (``127.0.0.1`` opts back out).
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import urllib.parse
import webbrowser
from typing import Any, Callable, TypeVar

from agitrack.git import GitRepo
from agitrack.metrics import learn as learn_page
from agitrack.metrics.collect import Dashboard, build_dashboard
from agitrack.metrics.files import FileBrowser, git_browser
from agitrack.metrics.insights import build_insights, context_from_browser
from agitrack.metrics.github import cached_logins
from agitrack.metrics.web import (
    _filter_stats,
    aggregates_payload,
    commit_diff,
    log_page,
    shared_sessions_for,
    shell_html,
)

DEFAULT_HOST = "127.0.0.1"
# Bind address used when aGiTrack runs in a remote shell: loopback there is reachable
# only from the remote box, which is never where the user's browser is.
ALL_INTERFACES = "0.0.0.0"
# Overrides the automatic choice, e.g. AGITRACK_DASHBOARD_HOST=127.0.0.1 to keep the
# dashboard off the network even over SSH, or a single NIC address to narrow it.
BIND_HOST_ENV = "AGITRACK_DASHBOARD_HOST"

DEFAULT_PORT = 8765
# Ports are handed out CONSECUTIVELY from the preferred one (8765, 8766, 8767, …) rather
# than falling straight to an OS-assigned ephemeral port: with several dashboards/backtraces
# up at once the URLs stay predictable and adjacent, so one `ssh -L` block covers them.
PORT_SCAN_SPAN = 32

_ServerT = TypeVar("_ServerT", bound=http.server.HTTPServer)


def _str(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key)
    return values[0] if values else ""


def _int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = _str(query, key)
    return int(raw) if raw.lstrip("-").isdigit() else default


class _DashboardHandler(http.server.BaseHTTPRequestHandler):
    repo: GitRepo  # set on the per-server subclass
    email_logins: dict[str, str] = {}  # lowercased email → login hint (set on the subclass)
    # Per-server cache of the file browser, keyed by (ref, head sha): building it scans
    # `git log --numstat`, so it is rebuilt only when the branch's tip moves, not per poll.
    _browser_cache: dict[tuple[str, str], FileBrowser] = {}
    # Efficiency insights are scoped to the CURRENT FILTER (so a narrowed time range re-asks
    # the question for that slice), hence keyed by the filter too — not just the branch tip.
    # Bounded: cleared whenever the tip moves, and capped while a tip is current.
    _insights_cache: dict[tuple, list[dict]] = {}

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        try:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            author, backend, model = _str(query, "author"), _str(query, "backend"), _str(query, "model")
            frm, to = _int(query, "from", 0), _int(query, "to", 0)
            ref = self._ref(_str(query, "branch"))
            if parsed.path in ("/", "/index.html"):
                # Paint the page chrome instantly with no aggregates/log embedded, then
                # let the browser fetch /data and /log behind a loading animation — so a
                # repo with a huge history doesn't block the first paint on the git-log
                # crunch. Warming the login cache here (a background refresh) means the
                # resolved GitHub IDs are likely ready by the time the first /data poll
                # lands, so committers show as their IDs almost immediately.
                cached_logins(self.repo)
                html = shell_html(self.repo)
                self._respond("text/html; charset=utf-8", html.encode("utf-8"), cache_control="no-cache")
            elif parsed.path == "/data":
                payload = aggregates_payload(
                    self._dashboard(ref),
                    author=author,
                    backend=backend,
                    model=model,
                    frm=frm,
                    to=to,
                    granularity=_str(query, "granularity"),
                )
                payload["shared_sessions"] = shared_sessions_for(self.repo)
                payload["insights"] = self._insights(ref, author=author, backend=backend, model=model, frm=frm, to=to)
                self._respond("application/json", self._json(payload))
            elif parsed.path == "/log":
                page = log_page(
                    self._dashboard(ref),
                    repo=self.repo,
                    author=author,
                    backend=backend,
                    model=model,
                    frm=frm,
                    to=to,
                    offset=_int(query, "offset", 0),
                    limit=_int(query, "limit", 50),
                    sort=_str(query, "sort"),
                )
                self._respond("application/json", self._json(page))
            elif parsed.path == "/diff":
                # This commit's file diffs, straight from the local clone — so the dashboard
                # shows changes without GitHub. The sha is validated as a hex id in commit_diff.
                self._respond("application/json", self._json(commit_diff(self.repo, _str(query, "sha"))))
            elif parsed.path == "/files":
                # The file browser: every changed file with its per-file change history and the
                # conversation/tokens behind each change (same view as --backtrace, real commits).
                self._respond("application/json", self._json({"files": self._browser(ref).files_payload()}))
            elif parsed.path == "/filelog":
                self._respond("application/json", self._json(self._browser(ref).file_log_payload(_str(query, "path"))))
            elif parsed.path == "/filediff":
                self._respond(
                    "application/json",
                    self._json(self._browser(ref).file_diff(_str(query, "path"), _str(query, "sha"))),
                )
            elif parsed.path == "/learn":
                # The learning page: the backend agent coaches the user from their own
                # interaction traces (agitrack/metrics/learn.py). Chrome only; the page
                # fetches /learn/state after paint, like the dashboard shell.
                self._respond(
                    "text/html; charset=utf-8",
                    learn_page.learn_html(self.repo.repo).encode("utf-8"),
                    cache_control="no-cache",
                )
            elif parsed.path == "/learn/state":
                # ``ref`` honours a ?branch= param (validated in _ref): the trace lives in
                # commits, so the committer list and trace count are branch-dependent.
                payload = learn_page.learn_state(self.repo.repo, self.repo)
                dash = self._dashboard(ref)
                payload["committers"] = sorted({label for stat in dash.stats for label in dash.committers_of(stat)})
                payload["branches"] = dash.branches or self.repo.list_branches()
                payload["branch"] = ref if ref != "HEAD" else self.repo.current_branch()
                payload["trace_turns"] = sum(1 for stat in dash.stats if stat.kind in learn_page._AI_KINDS)
                self._respond("application/json", self._json(payload))
            elif parsed.path == "/learn/models":
                self._respond("application/json", self._json(learn_page.model_options(_str(query, "backend"))))
            else:
                self.send_error(404, "not found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The browser closed the connection mid-response — a poll superseded
            # by the next one, a refresh, or a closed tab. Harmless; don't let
            # http.server dump a traceback to the console aGiTrack is running in.
            pass

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        # All POST endpoints belong to the learning page. Bodies are JSON; a beacon
        # flush (navigator.sendBeacon) may arrive without an application/json header,
        # so the body is parsed regardless of content type. Every handler returns a
        # JSON payload; agent failures come back as {"error": …} rather than a 500 so
        # the page can show them in place.
        try:
            parsed = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if 0 < length <= 1_000_000 else b""
            try:
                body = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except json.JSONDecodeError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            payload = learn_page.handle_learn_post(
                parsed.path, body, root=self.repo.repo, repo=self.repo, view=self._learn_view
            )
            if payload is None:
                self.send_error(404, "not found")
                return
            self._respond("application/json", self._json(payload))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _learn_view(self, author: str, frm: int, to: int, branch: str) -> tuple[list, list[dict], list[dict]]:
        """The filtered stats + insights + file rows the learning agent's digest is built
        from: exactly the same slice the dashboard would show for this filter. ``branch``
        picks the ref the trace is read from (validated like the dashboard's selector)."""
        ref = self._ref(branch)
        dash = self._dashboard(ref)
        stats = _filter_stats(dash, author=author, backend="", model="", frm=frm, to=to)
        insights = self._insights(ref, author=author, frm=frm, to=to)
        return stats, insights, self._browser(ref).files_payload()

    @staticmethod
    def _json(payload: dict) -> bytes:
        return json.dumps(payload).encode("utf-8")

    def _ref(self, branch: str) -> str:
        # Only an actual local branch may be viewed: an unchecked value would be
        # interpolated straight into ``git log <ref>``, so anything not in the
        # branch list (an option string, a bogus name, "") falls back to HEAD.
        return branch if branch and branch in self.repo.list_branches() else "HEAD"

    def _dashboard(self, ref: str = "HEAD") -> Dashboard:
        # cached_logins never blocks: it returns the cached GitHub identities (or {}
        # when cold) and refreshes them in the background, so polls stay fast. Resolved
        # logins appear on a later poll. {} when gh is absent. The initial / paint warms
        # this cache so the IDs are usually ready by the first /data poll.
        logins = cached_logins(self.repo)
        return build_dashboard(self.repo, ref, sha_logins=logins, email_logins=self.email_logins)

    def _browser(self, ref: str = "HEAD") -> FileBrowser:
        # Build (and cache) the file browser for this ref. Keyed by the branch tip so a poll
        # that finds no new commits reuses it; only a new commit rebuilds the numstat index.
        dash = self._dashboard(ref)
        head = dash.stats[-1].sha if dash.stats else ""
        key = (ref, head)
        cache = type(self)._browser_cache
        hit = cache.get(key)
        if hit is None:
            hit = git_browser(self.repo, dash.stats, ref)
            cache.clear()  # keep only the latest tip's browser — bounded memory
            cache[key] = hit
        return hit

    _INSIGHTS_CACHE_MAX = 16

    def _insights(
        self, ref: str = "HEAD", *, author: str = "", backend: str = "", model: str = "", frm: int = 0, to: int = 0
    ) -> list[dict]:
        # Insights for the FILTERED view: the same commits the rest of the page is showing.
        # Cached per (tip, filter) — a poll with unchanged filters reuses the result, and a new
        # commit invalidates every entry.
        dash = self._dashboard(ref)
        head = dash.stats[-1].sha if dash.stats else ""
        key = (ref, head, author, backend, model, frm, to)
        cache = type(self)._insights_cache
        hit = cache.get(key)
        if hit is None:
            if cache and next(iter(cache))[:2] != (ref, head):
                cache.clear()  # the tip moved: every cached slice is stale
            elif len(cache) >= self._INSIGHTS_CACHE_MAX:
                cache.pop(next(iter(cache)))  # bound the per-tip filter variants
            stats = _filter_stats(dash, author=author, backend=backend, model=model, frm=frm, to=to)
            files, sha_paths = context_from_browser(self._browser(ref), stats)
            hit = build_insights(stats, files, sha_paths)
            cache[key] = hit
        return hit

    def _respond(self, content_type: str, body: bytes, *, cache_control: str = "no-store") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Data endpoints are always recomputed; never let the browser cache them. HTML
        # pages pass "no-cache" instead: still revalidated on a normal load, but eligible
        # for the browser's back/forward cache — "no-store" disables bfcache, which made
        # returning from /learn to the dashboard a full blank-page reload (#learn).
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Stay quiet: the dashboard is a foreground tool, not a web log."""


class _DashboardServer(http.server.ThreadingHTTPServer):
    # Threaded so one slow request (e.g. the first gh lookup) never blocks the
    # page; daemon threads so Ctrl-C exits immediately without joining them.
    daemon_threads = True

    # A client that vanished mid-write surfaces as BrokenPipeError here too;
    # swallow it so the server doesn't print a traceback per dropped poll.
    def handle_error(self, request: Any, client_address: Any) -> None:
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            super().handle_error(request, client_address)


def is_remote_session() -> bool:
    """Whether aGiTrack is running in a shell on a *remote* machine (SSH/Mosh).

    Only the SSH environment is trusted here: it is set by sshd itself for the login
    session and inherited by everything started from it, so it means "the terminal the
    user typed into lives elsewhere". A headless local box is deliberately NOT treated
    as remote — that would put the dashboard on the network for someone sitting at a
    console, which they never asked for."""
    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT") or os.environ.get("SSH_TTY"))


def default_bind_host() -> str:
    """The address the dashboard should listen on.

    Loopback locally; all interfaces in a remote shell, so the user can reach it from
    their own machine at the remote's IP (subject to firewall rules — see
    :func:`remote_access_help` for the SSH-forwarding fallback). ``AGITRACK_DASHBOARD_HOST``
    overrides both."""
    override = os.environ.get(BIND_HOST_ENV, "").strip()
    if override:
        return override
    return ALL_INTERFACES if is_remote_session() else DEFAULT_HOST


def _ssh_server_ip() -> str:
    """The address of THIS machine that the SSH client connected to.

    ``SSH_CONNECTION`` is ``<client-ip> <client-port> <server-ip> <server-port>``, so its
    third field is the one address we know is routable from the user's machine — better
    than guessing among several NICs."""
    parts = os.environ.get("SSH_CONNECTION", "").split()
    return parts[2] if len(parts) >= 4 else ""


def _primary_ip() -> str:
    """This host's IP on the interface that reaches the default route. The UDP socket is
    never connected to anything (no packet leaves the machine); it just asks the kernel
    which local address routing would pick."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(("198.51.100.1", 9))  # TEST-NET-3: reserved, never routed anywhere
            return str(probe.getsockname()[0])
    except OSError:
        return ""


def advertised_host(bind_host: str) -> str:
    """A host component someone can actually type, given what the server bound to.

    A wildcard bind ("listening everywhere") is not an address — printing
    ``http://0.0.0.0:8765/`` gives the user nothing to click. Resolve it to the IP the
    SSH client already used, else this host's primary IP, else the hostname."""
    if bind_host and bind_host not in ("0.0.0.0", "::", "*"):
        return bind_host
    for candidate in (_ssh_server_ip(), _primary_ip()):
        if candidate:
            return candidate
    try:
        return socket.gethostname() or DEFAULT_HOST
    except OSError:
        return DEFAULT_HOST


def dashboard_url(bind_host: str, port: int) -> str:
    """The URL to show the user for a server bound to ``bind_host:port``."""
    return f"http://{advertised_host(bind_host)}:{port}/"


def bind_scanning(
    factory: Callable[[tuple[str, int]], _ServerT], host: str, port: int, *, span: int = PORT_SCAN_SPAN
) -> _ServerT:
    """Bind ``factory`` to the first free port at or after ``port``.

    Consecutive allocation is the point: a second dashboard/backtrace on the same box
    lands on 8766, a third on 8767, instead of a random ephemeral port that the user has
    to look up every time (and that no pre-arranged SSH forward can cover). ``port=0``
    keeps its usual meaning — let the OS pick — and an exhausted span falls back to that
    rather than failing to serve at all."""
    if port <= 0:
        return factory((host, 0))
    for candidate in range(port, min(port + max(span, 1), 65536)):
        try:
            return factory((host, candidate))
        except OSError:
            continue
    return factory((host, 0))


def build_server(
    repo: GitRepo,
    *,
    host: str | None = None,
    port: int = DEFAULT_PORT,
    email_logins: dict[str, str] | None = None,
) -> http.server.HTTPServer:
    """An HTTP server serving the dashboard for ``repo``, bound to ``host`` (defaulting to
    :func:`default_bind_host`) on the first free port at or after ``port``.

    ``email_logins`` (lowercased author email → GitHub login) supplements ``gh`` for
    commits not yet on the remote — e.g. fresh session commits — so the current user's
    local work still shows their GitHub ID."""
    handler = type(
        "DashboardHandler",
        (_DashboardHandler,),
        {
            "repo": repo,
            "email_logins": {k.lower(): v for k, v in (email_logins or {}).items()},
            # A fresh per-server cache so two servers (different repos) never share a browser.
            "_browser_cache": {},
        },
    )
    bind_host = default_bind_host() if host is None else host
    return bind_scanning(lambda address: _DashboardServer(address, handler), bind_host, port)


def browser_is_local() -> bool:
    """Whether a browser opened here would land on the user's *current* machine.

    The dashboard binds to localhost on whatever host aGiTrack runs on. When that
    host is a remote one — a Remote-SSH / WSL / container shell, or a plain SSH/Mosh
    session — calling ``webbrowser.open`` would try to launch a browser on the remote
    (which is usually headless, so it fails or opens the wrong screen). In that case we
    must NOT open it here and instead let the user reach the forwarded URL from their
    own machine.

    An explicit ``$BROWSER`` is always honored — editors that forward a local browser
    set it, and a user can point it at their own tunnel — so respecting it routes to the
    current machine. Otherwise a remote shell (``SSH_*``) or a headless Linux box (no
    ``DISPLAY``/``WAYLAND_DISPLAY``) is treated as not-local."""
    if os.environ.get("BROWSER"):
        return True
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return True


def open_dashboard_in_browser(url: str) -> bool:
    """Open ``url`` in the user's browser when it is on this machine; return whether a
    browser was launched. On a remote/headless host it does nothing (the caller should
    tell the user to open the forwarded URL from their own machine)."""
    if not browser_is_local():
        return False
    try:
        return webbrowser.open(url)
    except (webbrowser.Error, OSError):
        return False


def ssh_target() -> str:
    """``user@host`` for SSH-ing back into this machine, for a copy-pasteable command.

    The login name is this process's user (whoever is running aGiTrack is who the user
    would log in as again) and the host is the address their SSH client already reached
    us on, so the command works verbatim with nothing to fill in."""
    try:
        import getpass

        user = getpass.getuser()
    except Exception:  # no password entry / no USER: fall back to a placeholder
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or "<user>"
    host = _ssh_server_ip() or _primary_ip() or socket.gethostname() or "<remote-host>"
    return f"{user}@{host}"


def ssh_forward_command(port: int, *, target: str = "") -> str:
    """The exact ``ssh`` command to run **on the user's own machine** to forward ``port``.

    ``-N`` (no remote command) keeps it a pure tunnel the user can Ctrl-C when done."""
    return f"ssh -N -L {port}:localhost:{port} {target or ssh_target()}"


def _is_loopback(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


def remote_access_help(url: str, port: int, *, bind_host: str = "") -> str:
    """The copy-paste block printed when the dashboard can't be opened here — i.e. aGiTrack
    is on a remote host and the browser is on the user's machine.

    Every route is spelled out in full so nothing has to be understood or adapted. Which
    routes exist depends on what the server bound: a network bind can be opened directly
    when the firewall allows it, with SSH forwarding as the fallback, whereas a loopback
    bind is reachable ONLY through the tunnel — offering the direct URL there would send
    the user to a page that cannot load. ``bind_host`` defaults to reading the URL, which
    is right for both (an advertised loopback URL means a loopback bind)."""
    tunnel = (
        f"Run this ON YOUR OWN MACHINE (in a new terminal; leave it running):\n"
        f"    {ssh_forward_command(port)}\n"
        f"then open:\n"
        f"    http://localhost:{port}/"
    )
    host = bind_host or urllib.parse.urlparse(url).hostname or ""
    if _is_loopback(host):
        return f"The dashboard is bound to this machine's loopback, so reach it over an SSH tunnel.\n{tunnel}"
    return (
        f"Open this from your own machine (works if the firewall allows port {port}):\n"
        f"    {url}\n"
        f"If that does not load, forward the port instead. {tunnel}"
    )


def exposure_note(bind_host: str) -> str:
    """A line (blank when irrelevant) saying the dashboard is reachable beyond this machine.

    Binding all interfaces is what makes a remote dashboard usable at all, but it also
    means anyone who can route to this host and past its firewall can read the repo's
    commits and diffs — so say so, and say how to undo it."""
    if bind_host not in ("0.0.0.0", "::", "*"):
        return ""
    return (
        "Listening on all network interfaces so you can reach it from your own machine; "
        f"anyone able to reach this host on that port can view it. Set {BIND_HOST_ENV}=127.0.0.1 "
        "to keep it loopback-only.\n"
    )


def remote_browser_hint(url: str, port: int) -> str:
    """A compact one-line variant of :func:`remote_access_help`, for the TUI status popup."""
    return f"Open {url} from your own machine, or forward the port: `{ssh_forward_command(port)}`"


def serve_dashboard(
    repo: GitRepo,
    *,
    host: str | None = None,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> int:
    bind_host = default_bind_host() if host is None else host
    server = build_server(repo, host=bind_host, port=port)
    bound_port = int(server.server_address[1])
    url = dashboard_url(bind_host, bound_port)
    print(f"aGiTrack dashboard live at {url}\nRecomputed from commit metadata; auto-refreshes. Press Ctrl-C to stop.")
    if open_browser and not open_dashboard_in_browser(url):
        print(remote_access_help(url, bound_port, bind_host=bind_host))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping the dashboard.")
    finally:
        server.server_close()
    return 0
