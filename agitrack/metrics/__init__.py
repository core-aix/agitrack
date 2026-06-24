"""Repository metrics extracted from aGiTrack commit metadata (#54)."""

from agitrack.metrics.collect import CommitStat, Dashboard, build_dashboard, collect_commit_stats
from agitrack.metrics.daemon import (
    clear_handshake,
    dashboard_daemon_status,
    handshake_path,
    log_path,
    read_handshake,
    run_dashboard_daemon,
    running_handshake,
    spawn_dashboard_daemon,
    start_dashboard_daemon,
    stop_dashboard_daemon,
    wait_for_handshake,
)
from agitrack.metrics.render import render_dashboard
from agitrack.metrics.server import (
    build_server,
    open_dashboard_in_browser,
    remote_browser_hint,
    serve_dashboard,
)
from agitrack.metrics.web import dashboard_data, render_html

__all__ = [
    "CommitStat",
    "Dashboard",
    "build_dashboard",
    "build_server",
    "clear_handshake",
    "collect_commit_stats",
    "dashboard_daemon_status",
    "dashboard_data",
    "handshake_path",
    "log_path",
    "open_dashboard_in_browser",
    "read_handshake",
    "remote_browser_hint",
    "render_dashboard",
    "render_html",
    "run_dashboard_daemon",
    "running_handshake",
    "serve_dashboard",
    "spawn_dashboard_daemon",
    "start_dashboard_daemon",
    "stop_dashboard_daemon",
    "wait_for_handshake",
]
