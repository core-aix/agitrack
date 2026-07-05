"""Localhost server for the live aGiTrack dashboard (#54).

`agitrack --dashboard` serves the HTML dashboard on localhost and opens it in the
browser. The page polls ``/data`` on an interval and re-renders, so the
dashboard reflects new commits as they land — useful for watching an agent
work. Everything is recomputed from ``git log`` on each request: read-only, no
state, identical on every clone.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import urllib.parse
import webbrowser
from typing import Any

from agitrack.git import GitRepo
from agitrack.metrics.collect import Dashboard, build_dashboard
from agitrack.metrics.github import cached_logins
from agitrack.metrics.web import aggregates_payload, commit_diff, log_page, shared_sessions_for, shell_html

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _str(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key)
    return values[0] if values else ""


def _int(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = _str(query, key)
    return int(raw) if raw.lstrip("-").isdigit() else default


class _DashboardHandler(http.server.BaseHTTPRequestHandler):
    repo: GitRepo  # set on the per-server subclass
    email_logins: dict[str, str] = {}  # lowercased email → login hint (set on the subclass)

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
                self._respond("text/html; charset=utf-8", html.encode("utf-8"))
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
            else:
                self.send_error(404, "not found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The browser closed the connection mid-response — a poll superseded
            # by the next one, a refresh, or a closed tab. Harmless; don't let
            # http.server dump a traceback to the console aGiTrack is running in.
            pass

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

    def _respond(self, content_type: str, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Always recompute; never let the browser cache /data.
        self.send_header("Cache-Control", "no-store")
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


def build_server(
    repo: GitRepo,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    email_logins: dict[str, str] | None = None,
) -> http.server.HTTPServer:
    """An HTTP server bound to ``host:port`` serving the dashboard for ``repo``.
    Falls back to an OS-assigned free port if the preferred one is taken.

    ``email_logins`` (lowercased author email → GitHub login) supplements ``gh`` for
    commits not yet on the remote — e.g. fresh session commits — so the current user's
    local work still shows their GitHub ID."""
    handler = type(
        "DashboardHandler",
        (_DashboardHandler,),
        {"repo": repo, "email_logins": {k.lower(): v for k, v in (email_logins or {}).items()}},
    )
    try:
        return _DashboardServer((host, port), handler)
    except OSError:
        return _DashboardServer((host, 0), handler)


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


def remote_browser_hint(url: str, port: int) -> str:
    """A one-line hint for opening the dashboard from the user's own machine when
    aGiTrack runs on a remote host."""
    return (
        f"Open {url} from your own machine. If aGiTrack is on a remote host, your editor's "
        f"automatic port forwarding should make the link work; over plain SSH, forward the "
        f"port first, e.g. `ssh -L {port}:127.0.0.1:{port} <host>`."
    )


def serve_dashboard(
    repo: GitRepo,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> int:
    server = build_server(repo, host=host, port=port)
    bound_port = server.server_address[1]
    url = f"http://{host}:{bound_port}/"
    print(f"aGiTrack dashboard live at {url}\nRecomputed from commit metadata; auto-refreshes. Press Ctrl-C to stop.")
    if open_browser and not open_dashboard_in_browser(url):
        print(remote_browser_hint(url, bound_port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping the dashboard.")
    finally:
        server.server_close()
    return 0
