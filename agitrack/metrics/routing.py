"""What a dashboard request answers with, independent of who is serving it.

aGiTrack has two dashboards (the live one built from tracked commits, and the backtrace
reconstruction built from agent transcripts) and they can now be served two ways: on their own,
or side by side under one hub that switches repositories by URL path. That is four combinations of
"who owns the socket" over the same request logic, and the logic had been written twice inside
``BaseHTTPRequestHandler`` subclasses that wrote to ``self.wfile`` directly, so it could not be
mounted anywhere else.

So request handling is expressed as a plain function of ``(path, query) -> Response`` on a *scope*
object (one repository, in one view). Nothing in a scope knows about sockets, which is what lets
the same scope serve a single-repo daemon, a hub route, or a test that never opens a port.
``None`` means "not mine" and becomes a 404 at whatever edge is holding the connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Response:
    """One HTTP answer: a status, a body, and the few headers these pages need."""

    content_type: str = "application/json"
    body: bytes = b""
    # Data endpoints are recomputed on every request and must never be cached. HTML pages pass
    # "no-cache" instead: still revalidated on a normal load, but eligible for the browser's
    # back/forward cache, without which returning from /learn to the dashboard is a blank reload.
    cache_control: str = "no-store"
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)


def json_response(payload: object, *, status: int = 200) -> Response:
    import json

    return Response(
        content_type="application/json",
        body=json.dumps(payload).encode("utf-8"),
        status=status,
    )


def html_response(html: str, *, status: int = 200) -> Response:
    return Response(
        content_type="text/html; charset=utf-8",
        body=html.encode("utf-8") if isinstance(html, str) else html,
        cache_control="no-cache",
        status=status,
    )


def redirect(location: str) -> Response:
    """A 302 to ``location``.

    302 rather than 301: where a repository's dashboard should land is a *current* decision (it
    depends on whether anything is tracked yet), and a permanent redirect would be cached by the
    browser long after the answer changed."""
    return Response(
        content_type="text/html; charset=utf-8",
        body=b"",
        status=302,
        headers={"Location": location},
    )
