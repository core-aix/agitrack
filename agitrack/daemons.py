"""Global registry of the long-running aGiTrack daemons, across every repository.

aGiTrack can leave three kinds of detached process running: the **repo dashboard** (`-d`), the
**backtrace dashboard** (`--backtrace`), and the **background tracker** (`-b`). Each lives in its
own repo/temp dir, so there was no single place to see (or stop) them all. Every daemon now writes
a tiny ``<pid>.json`` into a shared directory when it starts serving and removes it when it exits;
this module reads that directory to:

* list every running daemon with its function, repo, and PID (`agitrack --daemons`), so a user can
  kill a stray one by hand; and
* after a self-update, gracefully stop and re-spawn them all so they reload the new version
  (:func:`restart_all`, called from the update restart path).

Entries whose process is gone are pruned on read, so a crashed daemon never lingers in the list.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from agitrack import __version__
from agitrack.proc import detach_kwargs, pid_alive, terminate_pid

# Human-readable name for each daemon kind, shown in `--daemons`.
KIND_LABELS = {
    "dashboard": "repo dashboard",
    "backtrace": "backtrace dashboard",
    "background": "background mode",
}


def _registry_dir() -> Path:
    return Path.home() / ".agitrack" / "daemons"


def _entry_path(pid: int) -> Path:
    return _registry_dir() / f"{pid}.json"


def _daemon_command() -> list[str]:
    """The command that would re-launch THIS daemon process — its own argv. A frozen build runs
    the exe directly; a normal install re-invokes ``python -m agitrack``."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *sys.argv[1:]]
    return [sys.executable, "-m", "agitrack", *sys.argv[1:]]


@dataclass
class DaemonInfo:
    pid: int
    kind: str
    repo: str
    url: str = ""
    version: str = ""
    cmd: list[str] = field(default_factory=list)
    started: int = 0

    @property
    def function(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def repo_name(self) -> str:
        name = Path(self.repo).name if self.repo else ""
        return name or (self.repo or "?")


def register(kind: str, repo: str | os.PathLike[str], *, url: str = "", cmd: list[str] | None = None) -> None:
    """Record THIS process as a running daemon of ``kind`` for ``repo``. Best-effort — a failure
    to write the registry entry never breaks the daemon itself."""
    try:
        directory = _registry_dir()
        directory.mkdir(parents=True, exist_ok=True)
        record = {
            "pid": os.getpid(),
            "kind": kind,
            "repo": str(repo),
            "url": url,
            "version": __version__,
            "cmd": cmd or _daemon_command(),
            "started": int(time.time()),
        }
        path = _entry_path(os.getpid())
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def deregister(pid: int | None = None) -> None:
    """Remove a daemon's registry entry (defaults to this process). Best-effort."""
    try:
        _entry_path(pid if pid is not None else os.getpid()).unlink()
    except OSError:
        pass


def list_running() -> list[DaemonInfo]:
    """Every aGiTrack daemon currently alive, pruning entries whose process has exited."""
    out: list[DaemonInfo] = []
    try:
        entries = sorted(_registry_dir().glob("*.json"))
    except OSError:
        return []
    for path in entries:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _safe_unlink(path)
            continue
        pid = record.get("pid")
        if not isinstance(pid, int) or not pid_alive(pid):
            _safe_unlink(path)  # stale entry from a crashed/killed daemon
            continue
        out.append(
            DaemonInfo(
                pid=pid,
                kind=str(record.get("kind", "?")),
                repo=str(record.get("repo", "")),
                url=str(record.get("url", "")),
                version=str(record.get("version", "?")),
                cmd=list(record.get("cmd") or []),
                started=int(record.get("started", 0)),
            )
        )
    out.sort(key=lambda info: (info.kind, info.repo))
    return out


def restart_all(*, exclude_pid: int | None = None, log=lambda message: None) -> int:
    """Gracefully stop and re-spawn every running daemon, so they reload freshly updated code.

    Called from the update restart path. The current process (``exclude_pid``, this pid by default)
    is skipped — it restarts itself via the caller's own re-exec. Each daemon is SIGTERM'd (its
    handler shuts it down and deregisters), then re-launched from its recorded command. Re-spawned
    from the home dir with ``PYTHONSAFEPATH`` so ``python -m agitrack`` can never pick up a stray
    ``agitrack`` package in some directory. Best-effort and independent per daemon."""
    skip = exclude_pid if exclude_pid is not None else os.getpid()
    env = {**os.environ, "PYTHONSAFEPATH": "1"}
    home = str(Path.home())
    restarted = 0
    for info in list_running():
        if info.pid == skip or not info.cmd:
            continue
        try:
            terminate_pid(info.pid)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and pid_alive(info.pid):
                time.sleep(0.05)
            deregister(info.pid)
            subprocess.Popen(
                info.cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=home,
                env=env,
                **detach_kwargs(),
            )
            restarted += 1
            log(f"restarted {info.function} for {info.repo_name} (was pid {info.pid})")
        except Exception:
            pass
    return restarted


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
