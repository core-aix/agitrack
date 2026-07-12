"""Run the metrics dashboard server in its own background process (#110).

`agitrack -d` used to serve the dashboard in the *foreground*, tying up the
terminal until Ctrl-C. Now it spawns the HTTP server as a detached background
process and returns to the shell prompt — you keep using the terminal while the
dashboard keeps serving. Recomputing metrics from ``git log`` on every poll is
real CPU/IO, so hosting it out-of-process also keeps that work (and its energy
use) off whatever launched it.

The daemon is **bound to whatever launched it** and dies with it — it is never
left orphaned:

* it watches an *owner* pid (the launching shell for ``agitrack -d``, the TUI
  process for the Ctrl-G dashboard) and shuts down once that pid goes away —
  e.g. you close the terminal, or aGiTrack is SIGKILLed with no chance to stop
  us first; and
* callers that own it can also stop it explicitly — ``agitrack -d stop`` for the
  CLI daemon, ``_stop_dashboard`` on the TUI's teardown path.

The child publishes a small handshake file (``<repo>/.agitrack/dashboard.json``)
recording the port it actually bound, so the launcher can show the URL and open a
browser without racing the child's startup.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from agitrack.git import GitRepo
from agitrack.metrics.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    build_server,
    open_dashboard_in_browser,
    remote_browser_hint,
)
from agitrack.proc import detach_kwargs, pid_alive, terminate_pid

# How often the child re-checks that its owner is still alive. The check
# (``os.kill(pid, 0)``) is essentially free, so a couple of seconds keeps
# shutdown prompt without any meaningful idle wakeups.
_OWNER_POLL_SECONDS = 2.0

# Internal env var carrying the launcher's email→GitHub-login hints to the child
# (so the current user's fresh, unpushed commits still show a GitHub ID).
EMAIL_LOGINS_ENV = "AGITRACK_DASHBOARD_EMAIL_LOGINS"


def handshake_path(repo: GitRepo) -> Path:
    """Where the child records its pid and bound URL for the launcher."""
    return repo.repo / ".agitrack" / "dashboard.json"


def log_path(repo: GitRepo) -> Path:
    """Where the detached child's stdout/stderr go (startup errors land here)."""
    return repo.repo / ".agitrack" / "dashboard.log"


def read_handshake(repo: GitRepo) -> dict[str, Any] | None:
    """The child's published ``{pid, host, port, url, started}`` record, or None."""
    try:
        with handshake_path(repo).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def clear_handshake(repo: GitRepo) -> None:
    """Best-effort removal of the handshake file."""
    try:
        handshake_path(repo).unlink()
    except OSError:
        pass


def _write_handshake(repo: GitRepo, record: dict[str, Any]) -> None:
    path = handshake_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(record, handle)
    os.replace(tmp, path)  # atomic: the launcher never sees a half-written record


def running_handshake(repo: GitRepo) -> dict[str, Any] | None:
    """The handshake of a daemon that is actually still running, else None.

    A record whose pid is dead is stale (a crash, or a kill that couldn't clean
    up) — drop it so the next start spawns a fresh daemon rather than reusing a
    corpse.
    """
    record = read_handshake(repo)
    if record is None:
        return None
    pid = record.get("pid")
    if isinstance(pid, int) and pid_alive(pid):
        return record
    clear_handshake(repo)
    return None


def wait_for_handshake(repo: GitRepo, *, pid: int, timeout: float) -> dict[str, Any] | None:
    """Poll for the handshake the child with ``pid`` writes once it binds its port.

    Correlating on the child's pid means a stale record from an earlier daemon is
    never mistaken for this launch. Returns the record, or None if the deadline
    passes without one appearing.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = read_handshake(repo)
        if record is not None and record.get("pid") == pid:
            return record
        time.sleep(0.05)
    return None


def spawn_dashboard_daemon(
    repo: GitRepo,
    *,
    owner_pid: int,
    email_logins: dict[str, str] | None = None,
    port: int | None = None,
) -> subprocess.Popen[bytes]:
    """Launch the detached dashboard child and return its Popen handle.

    The child is started in its own session (``start_new_session``) so it survives
    the launcher returning and is not hit by Ctrl-C in the launcher's terminal; the
    owner-pid watchdog is what ends it. stdout/stderr go to a log file so a startup
    failure is recoverable. Shared by the CLI (``agitrack -d``) and the TUI.

    ``port`` requests a specific port (used when restarting to keep the previous URL);
    the child still falls back to an OS-assigned port if it is taken.
    """
    cmd = [
        sys.executable,
        "-m",
        "agitrack",
        "--repo",
        str(repo.repo),
        "--dashboard-serve",
        "--dashboard-owner-pid",
        str(owner_pid),
    ]
    if port is not None:
        cmd += ["--dashboard-port", str(port)]
    env = dict(os.environ)
    if email_logins:
        env[EMAIL_LOGINS_ENV] = json.dumps(email_logins)
    else:
        env.pop(EMAIL_LOGINS_ENV, None)
    log = _open_log(repo)
    try:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=str(repo.repo),
            env=env,
            **detach_kwargs(),
        )
    finally:
        # The child holds its own dup of the log fd; close the launcher's copy.
        if log is not subprocess.DEVNULL:
            log.close()


def _open_log(repo: GitRepo) -> Any:
    try:
        path = log_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("ab")
    except OSError:
        return subprocess.DEVNULL


def start_dashboard_daemon(
    repo: GitRepo,
    *,
    owner_pid: int,
    open_browser: bool = True,
    timeout: float = 8.0,
) -> int:
    """`agitrack -d`: start (or reuse) the background dashboard daemon for ``repo``.

    Restarts a daemon already running for this repo rather than leaving the old one in
    place — so re-running ``agitrack -d`` (e.g. after an aGiTrack update) picks up the new
    code, reusing the previous port so the URL is unchanged. The launching shell's pid is
    the owner, so the daemon dies when that terminal closes.
    """
    running = running_handshake(repo)
    reuse_port: int | None = None
    if running is not None:
        old_pid = int(running["pid"])
        raw_port = running.get("port")
        reuse_port = int(raw_port) if isinstance(raw_port, int) else None
        # Stop the old daemon and wait for it to release the socket, so the replacement can
        # bind the SAME port. If it lingers, the child's port fallback still gives a working
        # (just different) URL rather than failing.
        _terminate_and_wait(old_pid, timeout=5.0)
        clear_handshake(repo)
        print(f"Restarting the dashboard daemon (was pid {old_pid}).")

    proc = spawn_dashboard_daemon(repo, owner_pid=owner_pid, port=reuse_port)
    record = wait_for_handshake(repo, pid=proc.pid, timeout=timeout)
    if record is None:
        print(f"The dashboard daemon did not start. See {log_path(repo)} for details.")
        return 1
    url = str(record.get("url", ""))
    print(
        f"aGiTrack dashboard daemon live at {url} (pid {record.get('pid')}).\n"
        "Runs in the background; stops when this terminal closes or `agitrack -d stop`."
    )
    _maybe_open(url, record, open_browser)
    return 0


def _terminate_and_wait(pid: int, *, timeout: float) -> None:
    """Signal ``pid`` to stop and block until it exits (or ``timeout`` elapses). Best-effort;
    on POSIX this is SIGTERM, on Windows TerminateProcess. Waiting matters when the caller then
    rebinds the daemon's port: a still-listening process would keep it in use."""
    terminate_pid(pid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.05)


def stop_dashboard_daemon(repo: GitRepo) -> int:
    """`agitrack -d stop`: stop the background dashboard daemon for ``repo``."""
    record = running_handshake(repo)
    if record is None:
        clear_handshake(repo)
        print("No dashboard daemon is running for this repository.")
        return 0
    pid = int(record["pid"])
    _terminate_and_wait(pid, timeout=5.0)
    clear_handshake(repo)
    print(f"Stopped the dashboard daemon (pid {pid}).")
    return 0


def dashboard_daemon_status(repo: GitRepo) -> int:
    """`agitrack -d status`: report whether a dashboard daemon is running."""
    record = running_handshake(repo)
    if record is None:
        print("No dashboard daemon is running for this repository.")
        return 0
    print(f"aGiTrack dashboard daemon running at {record.get('url', '')} (pid {record.get('pid')}).")
    return 0


def _maybe_open(url: str, record: dict[str, Any], open_browser: bool) -> None:
    if not (open_browser and url):
        return
    if not open_dashboard_in_browser(url):
        port = record.get("port", DEFAULT_PORT)
        print(remote_browser_hint(url, int(port) if isinstance(port, int) else DEFAULT_PORT))


def run_dashboard_daemon(
    repo: GitRepo,
    *,
    owner_pid: int | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    email_logins: dict[str, str] | None = None,
) -> int:
    """Serve the dashboard until told to stop — the entry point of the child process.

    Binds the server, publishes the handshake, then blocks in ``serve_forever``.
    Shuts down on SIGTERM/SIGINT (an explicit stop) or when the owner-pid watchdog
    sees the launcher disappear.
    """
    server = build_server(repo, host=host, port=port, email_logins=email_logins)
    bound_port = server.server_address[1]
    url = f"http://{host}:{bound_port}/"
    _write_handshake(
        repo,
        {"pid": os.getpid(), "host": host, "port": bound_port, "url": url, "started": int(time.time())},
    )
    from agitrack import daemons

    daemons.register("dashboard", repo.repo, url=url)

    stop = threading.Event()

    def _request_shutdown(*_: Any) -> None:
        # serve_forever() can't be stopped from within its own thread, so shutdown()
        # must run on another one; the Event also stops the watchdog loop.
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    if owner_pid:
        threading.Thread(
            target=_watch_owner,
            args=(owner_pid, stop, _request_shutdown),
            daemon=True,
            name="agitrack-dashboard-owner-watch",
        ).start()

    try:
        server.serve_forever()
    finally:
        server.server_close()
        clear_handshake(repo)
        from agitrack import daemons

        daemons.deregister()
    return 0


def _watch_owner(owner_pid: int, stop: threading.Event, request_shutdown: Any) -> None:
    """Shut the server down when the owner process (launcher) goes away.

    The owner is the launching shell for ``agitrack -d`` or the TUI process for the
    Ctrl-G dashboard. When it exits — including via SIGKILL, which gives it no
    chance to stop us first — the dashboard must not outlive it. ``pid_alive``
    detects that even though we were reparented to init the moment the launcher
    returned.
    """
    while not stop.wait(_OWNER_POLL_SECONDS):
        if not pid_alive(owner_pid):
            request_shutdown()
            return
