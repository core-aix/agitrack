from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Single-writer locking primitive, chosen per platform. POSIX uses an advisory
# ``flock`` on the open file description; native Windows has no ``fcntl``, so it uses a
# mandatory ``msvcrt.locking`` byte-range lock. Both are released the instant the owning
# handle/process dies, which is the property RepoLock relies on (no stale-file reclaim).
# The Windows lock is taken on a single byte FAR past any data we store, so a reader
# (e.g. the VS Code extension) can still read the pid JSON at offset 0 — a mandatory lock
# over offset 0 would block those reads. Gated on ``sys.platform`` (not ``os.name``) so
# mypy platform-narrows and skips the Windows-only ``msvcrt`` branch when checking on POSIX.
if sys.platform == "win32":  # pragma: no cover - exercised only on native Windows
    import msvcrt

    _WIN_LOCK_OFFSET = 0x4000_0000

    def _try_lock(fd: int) -> bool:
        try:
            os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def already_running_message(pid: int | None) -> str:
    """Refusal shown when a second aGiTrack is started on a repo that already has one
    running. aGiTrack auto-commits and merges as the agent works, so two instances on
    the same repo would race over commits and branches; we allow only one. Names
    the holding process so the user can find (and stop) it."""
    owner = f"already running on this repo (PID {pid})" if pid else "already running on this repo"
    return (
        f"Another aGiTrack instance is {owner}.\n"
        "Stop it before starting a new one.\n"
        "\n"
        "aGiTrack manages your git commits as the agent works. Running two instances on "
        "the same repo would let them fight over commits and branches, so only one is "
        "allowed at a time."
    )


class RepoLock:
    """Advisory single-writer lock for a working tree.

    Only one aGiTrack process should auto-commit/merge in a given working tree at a
    time. The authority is an OS ``flock`` held on a long-lived fd: the kernel
    releases it the instant the owner dies, so there is no stale-file reclaim
    (and its delete-a-live-lock race) and no PID-liveness guessing that PID
    reuse could fool. The file itself carries no authority — it just records
    the owner's PID for the "already running" message; it persists across
    releases (never unlinked, so two processes can never end up holding flocks
    on two different inodes of the same path) and is truncated on release.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Try to take the lock. Returns True on success, False if another live
        process already holds it."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            return False
        if not _try_lock(fd):
            os.close(fd)
            return False
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)  # the lock may have left the fd seeked elsewhere (Windows)
            os.write(fd, json.dumps({"pid": os.getpid(), "started_at": time.time()}).encode())
        except OSError:
            pass  # informational only; the lock is what guards, not the file content
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            os.ftruncate(self._fd, 0)  # leave no stale-looking owner info behind
        except OSError:
            pass
        _unlock(self._fd)
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def owner_pid(self) -> int | None:
        pid = self._read_info().get("pid")
        return pid if isinstance(pid, int) else None

    def probe_owner(self) -> int | None:
        """Non-destructively check whether another live process holds this lock.

        Returns the holder's PID (or None if unknown) when the lock is held by
        someone else, and None when it is free. Used for an early "already running"
        check *before* the authoritative :meth:`acquire`. There is a tiny window
        between this probe and ``acquire`` in which another process could take the
        lock, but ``acquire`` stays the real guard — so the worst case is the
        refusal appears a moment later, never a false start of two instances."""
        if self._fd is not None:
            return None  # we already hold it
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            # No lock file / dir yet ⇒ nobody is running; let acquire() be authority.
            return None
        if not _try_lock(fd):
            os.close(fd)
            return self.owner_pid()  # held by another live process
        # Free: we momentarily grabbed it — release at once so acquire() can take it.
        try:
            _unlock(fd)
        finally:
            os.close(fd)
        return None

    def is_held_by_self(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> "RepoLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

    def _read_info(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return {}
