from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as _wt
    import msvcrt as _msvcrt

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.LockFileEx.restype = _wt.BOOL
    _kernel32.LockFileEx.argtypes = [_wt.HANDLE, _wt.DWORD, _wt.DWORD, _wt.DWORD, _wt.DWORD, ctypes.c_void_p]
    _kernel32.UnlockFileEx.restype = _wt.BOOL
    _kernel32.UnlockFileEx.argtypes = [_wt.HANDLE, _wt.DWORD, _wt.DWORD, _wt.DWORD, ctypes.c_void_p]

    _kernel32.CreateFileW.restype = _wt.HANDLE
    _kernel32.CreateFileW.argtypes = [
        _wt.LPCWSTR, _wt.DWORD, _wt.DWORD, ctypes.c_void_p, _wt.DWORD, _wt.DWORD, _wt.HANDLE,
    ]

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_ALWAYS = 4
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001

    # Lock at a high offset so the PID JSON at byte 0 is never inside the locked range.
    # Windows byte-range locks block reads of the locked range from other processes,
    # unlike POSIX flock which is purely advisory.  By locking a byte far beyond any
    # real file content we get OS-auto-release-on-death semantics without preventing
    # other processes from reading the PID info.
    _LOCK_BYTE = 1_000_000

    def _make_overlapped(offset: int) -> ctypes.Array:
        """Return a zeroed 32-byte OVERLAPPED with Offset set to *offset*."""
        ov = (ctypes.c_byte * 32)()
        # On 64-bit Windows: Internal(8) + InternalHigh(8) + Offset(4) at byte 16.
        ctypes.c_uint32.from_buffer(ov, 16).value = offset
        return ov

    def _open_lock_file(path: str) -> int:
        """Open/create the lock file with FILE_SHARE_READ|WRITE so other processes
        can always read the PID info at byte 0 regardless of our lock state."""
        handle = _kernel32.CreateFileW(
            path,
            _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_ALWAYS,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        invalid = ctypes.cast(ctypes.c_void_p(-1), _wt.HANDLE).value
        if handle == invalid or handle is None:
            raise OSError(ctypes.get_last_error(), f"CreateFileW failed: {path}")
        return _msvcrt.open_osfhandle(handle, os.O_RDWR)

    def _lock_exclusive_nb(fd: int) -> None:
        handle = _msvcrt.get_osfhandle(fd)
        ov = _make_overlapped(_LOCK_BYTE)
        flags = _LOCKFILE_EXCLUSIVE_LOCK | _LOCKFILE_FAIL_IMMEDIATELY
        if not _kernel32.LockFileEx(handle, flags, 0, 1, 0, ov):
            raise OSError(ctypes.get_last_error(), "LockFileEx failed")

    def _unlock(fd: int) -> None:
        handle = _msvcrt.get_osfhandle(fd)
        ov = _make_overlapped(_LOCK_BYTE)
        _kernel32.UnlockFileEx(handle, 0, 1, 0, ov)

else:
    import fcntl as _fcntl

    def _open_lock_file(path: str) -> int:  # type: ignore[misc]
        return os.open(path, os.O_CREAT | os.O_RDWR, 0o644)

    def _lock_exclusive_nb(fd: int) -> None:  # type: ignore[misc]
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:  # type: ignore[misc]
        _fcntl.flock(fd, _fcntl.LOCK_UN)


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
    time. The authority is an OS file-lock held on a long-lived fd: the kernel
    releases it the instant the owner dies, so there is no stale-file reclaim
    (and its delete-a-live-lock race) and no PID-liveness guessing that PID
    reuse could fool. The file itself carries no authority — it just records
    the owner's PID for the "already running" message; it persists across
    releases (never unlinked, so two processes can never end up holding locks
    on two different inodes of the same path) and is truncated on release.

    POSIX: uses fcntl.flock (LOCK_EX | LOCK_NB).
    Windows: uses LockFileEx at a high byte offset (well beyond any PID data)
             so that other processes can always read the PID info at byte 0.
             The file is opened with FILE_SHARE_READ|WRITE for the same reason.
             Both are released automatically when the process exits.
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
            fd = _open_lock_file(str(self.path))
        except OSError:
            return False
        try:
            _lock_exclusive_nb(fd)
        except OSError:
            os.close(fd)
            return False
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, json.dumps({"pid": os.getpid(), "started_at": time.time()}).encode())
        except OSError:
            pass  # informational only; the lock is what locks
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            os.ftruncate(self._fd, 0)  # leave no stale-looking owner info behind
        except OSError:
            pass
        try:
            _unlock(self._fd)
        except OSError:
            pass
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
            fd = _open_lock_file(str(self.path))
        except OSError:
            # No lock file / dir yet ⇒ nobody is running; let acquire() be authority.
            return None
        try:
            _lock_exclusive_nb(fd)
        except OSError:
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
