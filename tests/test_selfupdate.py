"""Unattended self-update: aGiTrack keeps its own installation current.

Updating is not a decision worth interrupting anyone for, so any aGiTrack process may
install a newer version. What must hold: only one process updates at a time, only install
modes that can finish unattended are attempted (never mixing one mode's mechanism into
another's), the running session is never restarted from under the user, and whatever is
left for the user to do reaches them on the dashboards.
"""

import json
import types

from agitrack.update import selfupdate
from agitrack.update.updater import KIND_PACKAGE, KIND_SOURCE, METHOD_HOMEBREW, METHOD_MSI, METHOD_PIP


def _updater(kind, method=METHOD_PIP):
    return types.SimpleNamespace(kind=kind, _install_method=lambda: method)


def test_only_install_modes_that_can_finish_unattended_are_attempted():
    # The modes differ in what "apply" even means; auto_update_plan is the single authority
    # so no caller can drive one mode's mechanism through another's path.
    assert selfupdate.auto_update_plan(_updater(KIND_SOURCE))[0] is True  # git fast-forward
    assert selfupdate.auto_update_plan(_updater(KIND_PACKAGE, METHOD_PIP))[0] is True  # pip venv

    can, why = selfupdate.auto_update_plan(_updater(KIND_PACKAGE, METHOD_MSI))
    assert can is False and "elevation" in why  # MSI replaces the running exe
    can, why = selfupdate.auto_update_plan(_updater(KIND_PACKAGE, METHOD_HOMEBREW))
    assert can is False and "brew upgrade" in why  # brew owns that install


def test_windows_package_installs_are_left_to_the_post_exit_helper(monkeypatch):
    # Windows locks the running agitrack.exe, so pip cannot replace it in place — the
    # upgrade only runs from a helper after the process exits, never unattended here.
    monkeypatch.setattr(selfupdate.sys if hasattr(selfupdate, "sys") else __import__("sys"), "platform", "win32")
    can, why = selfupdate.auto_update_plan(_updater(KIND_PACKAGE, METHOD_PIP))
    assert can is False and "agitrack.exe" in why


def test_only_one_instance_may_self_update_at_a_time(tmp_path, monkeypatch):
    """Several aGiTrack processes normally run at once; two of them upgrading the same
    installation is how a half-replaced install happens. The second caller must do nothing
    while the first holds the lock."""
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path))
    checks: list[str] = []

    class _Updater:
        kind = KIND_SOURCE

        def _install_method(self):
            return METHOD_PIP

        def check(self, **kwargs):
            checks.append("checked")
            return types.SimpleNamespace(ok=True, available=False, current="1.0.0", latest="", error="")

    monkeypatch.setattr("agitrack.update.updater.Updater", _Updater)

    from agitrack.git.lock import RepoLock

    held = RepoLock(selfupdate.lock_path())
    assert held.acquire() is True
    try:
        selfupdate.attempt_self_update()  # another instance is mid-update
        assert checks == []  # …so this one does not even check, let alone install
    finally:
        held.release()

    selfupdate.attempt_self_update()  # lock free again
    assert checks == ["checked"]


def test_a_failed_self_update_is_recorded_for_the_dashboards(tmp_path, monkeypatch):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path))

    class _Updater:
        kind = KIND_SOURCE

        def _install_method(self):
            return METHOD_PIP

        def check(self, **kwargs):
            return types.SimpleNamespace(ok=True, available=True, current="1.0.0", latest="1.1.0", error="")

        def apply(self):
            return types.SimpleNamespace(ok=False, error="the working tree has local changes", message="")

        def manual_update_instructions(self):
            return "Run: pip install --upgrade agitrack"

    monkeypatch.setattr("agitrack.update.updater.Updater", _Updater)

    record = selfupdate.attempt_self_update()
    assert record.state == selfupdate.STATE_FAILED
    assert record.needs_user is True and record.latest == "1.1.0"
    assert "local changes" in record.error
    # …and it is on disk for a dashboard in another process to read.
    assert selfupdate.read_state().needs_user is True
    assert json.loads(selfupdate.state_path().read_text())["latest"] == "1.1.0"


def test_a_mode_that_cannot_self_update_is_recorded_without_attempting(tmp_path, monkeypatch):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path))
    applied: list[str] = []

    class _Updater:
        kind = KIND_PACKAGE

        def _install_method(self):
            return METHOD_HOMEBREW

        def check(self, **kwargs):
            return types.SimpleNamespace(ok=True, available=True, current="1.0.0", latest="1.1.0", error="")

        def apply(self):
            applied.append("apply")  # must never happen for a brew install
            return types.SimpleNamespace(ok=True, error="", message="")

        def manual_update_instructions(self):
            return "Run: brew upgrade agitrack"

    monkeypatch.setattr("agitrack.update.updater.Updater", _Updater)

    record = selfupdate.attempt_self_update()
    assert applied == []  # brew's install is not ours to touch
    assert record.state == selfupdate.STATE_MANUAL and record.method == METHOD_HOMEBREW
    assert "brew upgrade" in record.instructions


def test_a_successful_self_update_clears_the_reminder(tmp_path, monkeypatch):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path))
    selfupdate.write_state(selfupdate.SelfUpdateRecord(state=selfupdate.STATE_FAILED, latest="1.1.0"))

    class _Updater:
        kind = KIND_SOURCE

        def _install_method(self):
            return METHOD_PIP

        def check(self, **kwargs):
            return types.SimpleNamespace(ok=True, available=True, current="1.0.0", latest="1.1.0", error="")

        def apply(self):
            return types.SimpleNamespace(ok=True, error="", message="Updated.")

        def manual_update_instructions(self):
            return ""

    monkeypatch.setattr("agitrack.update.updater.Updater", _Updater)

    record = selfupdate.attempt_self_update()
    assert record.state == selfupdate.STATE_OK and record.needs_user is False
    assert selfupdate.read_state().needs_user is False


def test_a_session_running_older_code_is_detectable(tmp_path, monkeypatch):
    """A self-update deliberately leaves a running session on its old code. The dashboard
    can say so only if it can tell that apart from "no session" and "already current"."""
    from agitrack.git.lock import RepoLock

    repo_root = tmp_path / "repo"
    (repo_root / ".agitrack").mkdir(parents=True)
    lock = RepoLock(repo_root / ".agitrack" / "lock")

    monkeypatch.setattr("agitrack.update.restart.disk_fingerprint", lambda: "commit:new")
    assert selfupdate.running_session_is_stale(repo_root) is False  # no session at all

    monkeypatch.setattr("agitrack.update.restart.RUNNING_FINGERPRINT", "commit:old")
    assert lock.acquire() is True
    try:
        # The lock records the code the holder loaded, so "stale" is a file read.
        assert selfupdate.running_session_is_stale(repo_root) is True
        monkeypatch.setattr("agitrack.update.restart.disk_fingerprint", lambda: "commit:old")
        assert selfupdate.running_session_is_stale(repo_root) is False  # session already current
    finally:
        lock.release()
    monkeypatch.setattr("agitrack.update.restart.disk_fingerprint", lambda: "commit:new")
    assert selfupdate.running_session_is_stale(repo_root) is False  # released: nothing running


def test_dashboards_show_the_two_notices_separately(tmp_path, monkeypatch):
    """The live dashboard distinguishes "install it yourself" (global, the installation)
    from "restart your session" (this repo) — they call for different actions. Backtrace
    has no session of its own, so it only ever shows the first."""
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path))
    from agitrack.metrics.web import _update_banner_html

    repo = types.SimpleNamespace(repo=tmp_path / "repo")
    (repo.repo / ".agitrack").mkdir(parents=True)

    monkeypatch.setattr("agitrack.update.selfupdate.running_session_is_stale", lambda root: False)
    assert _update_banner_html(repo) == ""  # nothing to say

    selfupdate.write_state(
        selfupdate.SelfUpdateRecord(
            state=selfupdate.STATE_MANUAL,
            latest="1.2.0",
            error="belongs to Homebrew",
            instructions="Run: brew upgrade agitrack",
        )
    )
    installed = _update_banner_html(repo)
    assert "has to be installed by you" in installed and "brew upgrade agitrack" in installed
    assert "restart aGiTrack" not in installed

    selfupdate.write_state(selfupdate.SelfUpdateRecord(state=selfupdate.STATE_OK))
    monkeypatch.setattr("agitrack.update.selfupdate.running_session_is_stale", lambda root: True)
    restart = _update_banner_html(repo)
    assert "still on the old version" in restart and "has to be installed by you" not in restart

    # Both at once render as two separate notices, not one merged line.
    selfupdate.write_state(selfupdate.SelfUpdateRecord(state=selfupdate.STATE_FAILED, latest="1.2.0"))
    both = _update_banner_html(repo)
    assert both.count('class="updatebanner"') == 2


def test_daemon_watcher_also_installs_updates(monkeypatch):
    """Watching alone only reacts to someone ELSE updating, so a machine whose only
    aGiTrack is a dashboard (or backtrace) daemon would never update. The watcher thread
    that restarts the daemon also drives the self-update."""
    import threading

    from agitrack.update import restart as update_restart

    attempts: list[str] = []
    monkeypatch.setattr("agitrack.update.selfupdate.attempt_self_update", lambda *a, **k: attempts.append("tried"))
    stop = threading.Event()
    seen: list[str] = []
    thread = update_restart.watch_for_update(
        stop, seen.append, interval=0.01, read_version=lambda: None, self_update_interval=999.0
    )
    for _ in range(200):
        if attempts:
            break
        threading.Event().wait(0.01)
    stop.set()
    thread.join(timeout=2)
    assert attempts, "a watching daemon must also install updates"
