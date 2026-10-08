"""Watching a working tree for changes, shared by the interactive session and the background
tracker. An event-driven watch costs nothing while nothing changes, so it is what decides
whether there is anything to look at before git is asked."""

from __future__ import annotations

import os
import threading

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - exercised only without optional dependency
    FileSystemEvent = None  # type: ignore[misc, assignment]
    FileSystemEventHandler = object  # type: ignore[misc, assignment]
    Observer = None  # type: ignore[misc, assignment]


class RepoChangeHandler(FileSystemEventHandler):
    IGNORED_PARTS = {".agitrack", ".git", ".pytest_cache", ".venv", "__pycache__"}

    # Event types that mean "somebody READ a file", not "the worktree changed". watchdog's
    # inotify backend reports IN_OPEN and IN_CLOSE_NOWRITE as these; macOS FSEvents reports
    # only real modifications, which is why this was a Linux-only failure.
    #
    # Counting a read as a change is catastrophic here, not merely noisy: `_last_change_at` is
    # reset on every one, so `worktree_settled` (now - _last_change_at >= FILE_STABLE_SECONDS)
    # never becomes true, the commit gate never opens, and aGiTrack stops committing entirely
    # while looking perfectly healthy. Measured on a real repo with NOTHING being written:
    # 1536 opened + 1536 closed_no_write events in 25s — about 123 phantom "changes" a second,
    # from ordinary reads of files like .gitignore and AGENTS.md.
    #
    # `closed` (IN_CLOSE_WRITE) is deliberately NOT in this set: it follows an actual write.
    READ_ONLY_EVENT_TYPES = {"opened", "closed_no_write"}

    def __init__(self, repo_path, changed: threading.Event, wake: "threading.Event | None" = None) -> None:
        self.repo_path = repo_path
        self.changed = changed
        # The git worker sleeps on `wake`; setting it lets a real worktree write
        # wake the worker at once instead of waiting for its poll timeout.
        self.wake = wake

    def on_any_event(self, event: FileSystemEvent) -> None:
        # Reads are not changes. Checked by event_type STRING rather than by class so this
        # works across watchdog versions (older ones simply never emit these types).
        if getattr(event, "event_type", "") in self.READ_ONLY_EVENT_TYPES:
            return
        # A directory "modified" only says an entry inside it was added or removed, and that
        # entry has an event of its own, which is judged on its own path. Taken alone, creating
        # `.agitrack/` read as a change to the repository root.
        if getattr(event, "is_directory", False) and getattr(event, "event_type", "") == "modified":
            return
        # watchdog reports src_path as str or bytes depending on how the watch was
        # set up; normalise to str so the IGNORED_PARTS check is uniform.
        src_path = os.fsdecode(event.src_path)
        try:
            relative = os.path.relpath(src_path, self.repo_path)
        except ValueError:
            relative = src_path
        if relative == os.path.join(".agitrack", "flush-request"):
            # A `git commit` is waiting on us to record the conversation so far (see
            # ProxyRunner._service_commit_flush_request). Not a worktree change, so only the git
            # worker is woken — at once, rather than after its idle poll.
            if self.wake is not None:
                self.wake.set()
            return
        if any(part in self.IGNORED_PARTS for part in relative.split(os.sep)):
            return
        self.changed.set()
        if self.wake is not None:
            self.wake.set()


def start_watching(path, changed: threading.Event, wake: threading.Event | None = None):
    """A started observer that sets ``changed`` on any real change under ``path``, or None when
    no watcher is available here (the caller then polls, as it did before)."""
    if Observer is None:
        return None
    try:
        observer = Observer()
        observer.schedule(RepoChangeHandler(path, changed, wake), str(path), recursive=True)
        observer.daemon = True
        observer.start()
        return observer
    except Exception:
        return None


def stop_watching(observer) -> None:
    if observer is None:
        return
    try:
        observer.stop()
        observer.join(timeout=2.0)
    except Exception:
        pass
