"""Unattended self-update: aGiTrack keeps itself current without asking.

Updating the installation is not a decision the user should have to make over and over,
so any aGiTrack process — the interactive TUI or one of the daemons — may install a newer
version on its own. It is ON by default; the global ``self_update`` setting turns it off
for a machine that must stay pinned, and aGiTrack then only REPORTS that a newer version
exists (the dashboards' "install it yourself" notice) instead of installing it.

Three rules shape how updating is done safely:

**One updater at a time.** Several aGiTrack processes usually run at once (a TUI, a
dashboard daemon, a backtrace daemon, background trackers in other repos). Two of them
upgrading the same installation concurrently is how a half-replaced install happens, so an
OS file lock in the global config dir admits exactly one; everyone else skips the attempt
and tries again on their next check. The kernel drops the lock when its holder dies, so a
crash mid-update cannot wedge every future attempt.

**Only what can finish unattended is attempted.** :class:`~agitrack.update.updater.Updater`
knows the install mode, and the modes differ in what "apply" even means: a source checkout
fast-forwards, a pip/pipx venv upgrades in place, but a Windows MSI needs elevation and a
Windows pip install cannot replace the running ``agitrack.exe`` at all — both only stage
work for after this process exits. Homebrew is the user's package manager, not ours. Those
modes are recorded as "needs you", never half-attempted; :func:`auto_update_plan` is the
single place that decides, so no caller can mix one mode's mechanism into another's.

**Nothing restarts the user's session.** A finished update leaves the running process on
the old code; the TUI keeps showing its existing restart reminder and the user restarts
when it suits them. Daemons restart themselves (they have no conversation to interrupt) —
that is :mod:`agitrack.update.restart`'s job, driven by the fingerprint on disk changing.

The outcome of the last attempt is recorded globally (the installation is global, not
per-repo) so the dashboards can tell the user when self-updating did not work and they
have to do it themselves.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

# Outcomes recorded in the state file.
STATE_OK = "ok"  # nothing to do, or an update was installed
STATE_MANUAL = "manual"  # a newer version exists but this install mode cannot self-update
STATE_FAILED = "failed"  # self-update was attempted and did not work
# An update exists but was deliberately NOT applied for a condition that is the user's own and
# clears itself — a source checkout with uncommitted changes. Deliberately NOT `needs_user`:
# there is nothing to install and no action to take beyond the work already in progress, and on
# the aGiTrack checkout itself this is the ordinary state for most of the day, so treating it as
# an action item put a permanent-looking "install it yourself" banner on the developer's own
# dashboard that came and went with the working tree.
STATE_DEFERRED = "deferred"

_STATE_FILE = "self-update.json"
_LOCK_FILE = "self-update.lock"


def _config_dir() -> Path:
    """The global aGiTrack directory, honouring AGITRACK_CONFIG_DIR like every other
    global-state path (so tests and sandboxes stay isolated)."""
    from agitrack.env import getenv_compat

    configured = getenv_compat("CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".agitrack"


def state_path() -> Path:
    return _config_dir() / _STATE_FILE


def lock_path() -> Path:
    return _config_dir() / _LOCK_FILE


@dataclass
class SelfUpdateRecord:
    """What the last self-update attempt concluded, as the dashboards read it."""

    state: str = STATE_OK
    current: str = ""
    latest: str = ""
    method: str = ""  # the install mode, so the reminder can be specific
    error: str = ""
    instructions: str = ""
    at: float = 0.0

    @property
    def needs_user(self) -> bool:
        """Whether the user has to update by hand — a newer version exists and aGiTrack
        could not install it."""
        return self.state in (STATE_MANUAL, STATE_FAILED) and bool(self.latest)


def read_state() -> SelfUpdateRecord:
    """The recorded outcome (best-effort: a missing or corrupt file reads as "fine")."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return SelfUpdateRecord()
    if not isinstance(data, dict):
        return SelfUpdateRecord()
    return SelfUpdateRecord(
        state=str(data.get("state") or STATE_OK),
        current=str(data.get("current") or ""),
        latest=str(data.get("latest") or ""),
        method=str(data.get("method") or ""),
        error=str(data.get("error") or ""),
        instructions=str(data.get("instructions") or ""),
        at=float(data.get("at") or 0.0),
    )


def write_state(record: SelfUpdateRecord) -> None:
    """Record an attempt's outcome (best-effort; never raises)."""
    try:
        from agitrack.fileio import atomic_write_text

        atomic_write_text(
            state_path(),
            json.dumps(
                {
                    "state": record.state,
                    "current": record.current,
                    "latest": record.latest,
                    "method": record.method,
                    "error": record.error,
                    "instructions": record.instructions,
                    "at": record.at or time.time(),
                }
            ),
        )
    except OSError:
        pass


def auto_update_plan(updater) -> tuple[bool, str]:
    """``(can_self_update, why_not)`` for the install mode *updater* detected.

    The single authority on which modes aGiTrack may update by itself, so a caller can
    never drive one mode's mechanism through another's path:

    * **source checkout** — yes: a fast-forward of the clone aGiTrack runs from. A dirty
      or diverged checkout is refused by the updater itself, which then reports the reason.
    * **pip / pipx (POSIX)** — yes: the venv is upgraded in place and the next process
      start picks it up.
    * **pip / pipx (Windows)** — no: the OS locks the running ``agitrack.exe``, so the
      upgrade can only run from a helper after this process exits.
    * **Homebrew** — no: brew owns that installation; upgrading it behind the user's back
      would fight their package manager.
    * **MSI** — no: installing needs elevation and replaces the running executable.
    """
    import sys

    from agitrack.update.updater import KIND_SOURCE, METHOD_BREW_PYTHON, METHOD_MSI

    if updater.kind == KIND_SOURCE:
        return True, ""
    method = updater._install_method()
    if method == METHOD_MSI:
        return False, "the Windows installer needs elevation, so aGiTrack cannot install it for you"
    if method == METHOD_BREW_PYTHON:
        return False, "this install belongs to Homebrew — update it with `brew upgrade agitrack`"
    if sys.platform == "win32":
        return False, "Windows locks the running agitrack.exe, so the upgrade has to run after it exits"
    return True, ""


def attempt_self_update(*, debug=None, timeout: int | None = None, on_status=None) -> SelfUpdateRecord:
    """Check for a newer aGiTrack and install it when this install mode allows.

    Safe to call from anywhere and from several processes at once: only the lock holder
    does any work, and the result is recorded for the dashboards either way. Never raises
    — an update problem must not take down a session or a daemon.

    ``on_status`` receives the :class:`~agitrack.update.updater.UpdateStatus` this call
    obtained, so a caller that also wants to show the check's own result (the TUI does)
    gets it without paying for a second network round-trip.
    """

    def _log(message: str) -> None:
        if debug:
            try:
                debug(message)
            except Exception:
                pass

    if not self_update_enabled():
        # The user pinned this machine to the installed version. Still CHECK, so the
        # dashboards and the TUI can say an update exists — they just won't install it.
        _log("self-update: disabled by config (self_update = false); reporting only")
        return _report_only(on_status=on_status, log=_log)

    from agitrack.git.lock import RepoLock

    lock = RepoLock(lock_path())
    if not lock.acquire():
        _log("self-update: another aGiTrack instance holds the update lock; skipping this round")
        return read_state()  # someone else is on it; report what we last knew
    try:
        from agitrack.update.updater import Updater

        updater = Updater()
        status = updater.check() if timeout is None else updater.check(timeout=timeout)
        if on_status is not None:
            try:
                on_status(status)
            except Exception:
                pass
        if not status.ok:
            _log(f"self-update: check failed: {status.error}")
            return read_state()  # a failed CHECK is not a failed update — say nothing new
        if not status.available:
            record = SelfUpdateRecord(state=STATE_OK, current=status.current, at=time.time())
            write_state(record)
            return record
        can_auto, why_not = auto_update_plan(updater)
        if not can_auto:
            _log(f"self-update: {status.latest} available but this install must be updated by hand: {why_not}")
            record = SelfUpdateRecord(
                state=STATE_MANUAL,
                current=status.current,
                latest=status.latest,
                method=_method_label(updater),
                error=why_not,
                instructions=_instructions(updater),
                at=time.time(),
            )
            write_state(record)
            return record
        _log(f"self-update: installing {status.current} → {status.latest}")
        result = updater.apply()
        if not result.ok:
            deferred = bool(getattr(result, "deferred", False))
            _log(f"self-update: apply {'deferred' if deferred else 'failed'}: {result.error}")
            record = SelfUpdateRecord(
                state=STATE_DEFERRED if deferred else STATE_FAILED,
                current=status.current,
                latest=status.latest,
                method=_method_label(updater),
                error=result.error or "the update did not complete",
                instructions=_instructions(updater),
                at=time.time(),
            )
            write_state(record)
            return record
        _log(f"self-update: installed {status.latest}")
        record = SelfUpdateRecord(state=STATE_OK, current=status.latest or status.current, at=time.time())
        write_state(record)
        return record
    except Exception as error:  # a broken update path must never break the caller
        _log(f"self-update: unexpected failure: {error!r}")
        return read_state()
    finally:
        lock.release()


def self_update_enabled() -> bool:
    """Whether aGiTrack may install updates for itself — the global ``self_update``
    setting, on unless the user turned it off. Read fresh each attempt so flipping it
    takes effect without restarting anything."""
    try:
        from agitrack.config.settings import GlobalConfig

        return bool(GlobalConfig().self_update)
    except Exception:
        return True  # a broken/absent config must not silently stop updates


def _report_only(*, on_status, log) -> SelfUpdateRecord:
    """Check without installing (self-update turned off): record what the user would have
    to install themselves, so the dashboards' "install it yourself" notice still appears."""
    try:
        from agitrack.update.updater import Updater

        updater = Updater()
        status = updater.check()
        if on_status is not None:
            try:
                on_status(status)
            except Exception:
                pass
        if not status.ok:
            return read_state()
        if not status.available:
            record = SelfUpdateRecord(state=STATE_OK, current=status.current, at=time.time())
            write_state(record)
            return record
        record = SelfUpdateRecord(
            state=STATE_MANUAL,
            current=status.current,
            latest=status.latest,
            method=_method_label(updater),
            error="automatic updates are turned off for this machine",
            instructions=_instructions(updater),
            at=time.time(),
        )
        write_state(record)
        return record
    except Exception as error:
        log(f"self-update: report-only check failed: {error!r}")
        return read_state()


def _method_label(updater) -> str:
    from agitrack.update.updater import KIND_SOURCE

    if updater.kind == KIND_SOURCE:
        return "source"
    try:
        return str(updater._install_method())
    except Exception:
        return "package"


def _instructions(updater) -> str:
    try:
        return str(updater.manual_update_instructions())
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# "Is a stale aGiTrack still running here?"
# ---------------------------------------------------------------------------


def running_session_is_stale(repo_root: Path) -> bool:
    """Whether an aGiTrack session is live on ``repo_root`` running OLDER code than what
    is installed now.

    A self-update leaves the running TUI on the code it started with — deliberately, since
    restarting it would interrupt a conversation. The dashboard says so, which is only
    honest if it can tell "a session is running and it is stale" from "no session" and from
    "a session already on the new code". The session records the fingerprint it loaded
    (commit for a source install, version for a wheel) in the repo lock it already holds,
    so this is a file read and a comparison — no process probing.
    """
    try:
        from agitrack.update.restart import disk_fingerprint

        record = _lock_record(repo_root / ".agitrack" / "lock")
        running = str((record or {}).get("fingerprint") or "")
        if not running:
            return False  # no live session, or one from before this was recorded
        current = disk_fingerprint()
        return bool(current) and running != current
    except Exception:
        return False


def _lock_record(path: Path) -> dict | None:
    """The JSON the lock holder wrote, or None when the lock is free (the file is
    truncated on release) or unreadable."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if isinstance(pid, int) and pid > 0:
        from agitrack.proc import pid_alive

        if not pid_alive(pid):
            return None  # leftovers from a holder that died without truncating
    # Deliberately NOT excluding this process: if the caller itself is the stale session,
    # "restart to load the new version" is exactly the right thing to tell its reader.
    return data
