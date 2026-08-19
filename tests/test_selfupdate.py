"""Unattended self-update: aGiTrack keeps its own installation current.

Updating is not a decision worth interrupting anyone for, so any aGiTrack process may
install a newer version. What must hold: only one process updates at a time, only install
modes that can finish unattended are attempted (never mixing one mode's mechanism into
another's), the running session is never restarted from under the user, and whatever is
left for the user to do reaches them on the dashboards.
"""

import json
import os
import sys
import types

from agitrack.update import selfupdate
from agitrack.update.updater import KIND_PACKAGE, KIND_SOURCE, METHOD_BREW_PYTHON, METHOD_MSI, METHOD_PIP

# The suite-wide guard in conftest stubs attempt_self_update so no test can ever install a
# real update (it fetches and merges the checkout aGiTrack runs from). These tests ARE the
# updater's tests, so they bind the real function here at import time and drive it against
# a stubbed Updater — the same pattern test_daemons.py uses for the process scan.
_attempt_self_update = selfupdate.attempt_self_update


def _updater(kind, method=METHOD_PIP):
    return types.SimpleNamespace(kind=kind, _install_method=lambda: method)


def test_only_install_modes_that_can_finish_unattended_are_attempted():
    # The modes differ in what "apply" even means; auto_update_plan is the single authority
    # so no caller can drive one mode's mechanism through another's path.
    assert selfupdate.auto_update_plan(_updater(KIND_SOURCE))[0] is True  # git fast-forward
    # pip/pipx upgrades the venv in place and the next process start picks it up — except on
    # Windows, which locks the running agitrack.exe, so that mode is deliberately refused
    # there and left to a helper that runs after this process exits.
    can, why = selfupdate.auto_update_plan(_updater(KIND_PACKAGE, METHOD_PIP))
    if sys.platform == "win32":
        assert can is False and "locks the running agitrack.exe" in why
    else:
        assert can is True

    can, why = selfupdate.auto_update_plan(_updater(KIND_PACKAGE, METHOD_MSI))
    assert can is False and "elevation" in why  # MSI replaces the running exe
    can, why = selfupdate.auto_update_plan(_updater(KIND_PACKAGE, METHOD_BREW_PYTHON))
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
        _attempt_self_update()  # another instance is mid-update
        assert checks == []  # …so this one does not even check, let alone install
    finally:
        held.release()

    _attempt_self_update()  # lock free again
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

    record = _attempt_self_update()
    assert record.state == selfupdate.STATE_FAILED
    assert record.needs_user is True and record.latest == "1.1.0"
    assert "local changes" in record.error
    # …and it is on disk for a dashboard in another process to read.
    assert selfupdate.read_state().needs_user is True
    assert json.loads(selfupdate.state_path().read_text())["latest"] == "1.1.0"


def test_the_reminder_is_re_checked_against_the_agitrack_running_now():
    """The state file is a global cache and what it describes moves underneath it, so a record
    is not trusted as written.

    Both halves are measured, not imagined. A dashboard kept showing "0.6.13 is available and
    has to be installed by you" on a machine ALREADY running 0.6.13: a failed attempt's record
    survives, and whoever later succeeds writes a fresh file rather than correcting the old one.
    The second half is subtler — several aGiTrack processes of different vintages share this one
    file. A background tracker whose venv wheel had been replaced by an editable install
    underneath it never saw its own install change again, so it stayed on 0.6.10 for days and
    kept publishing that version's "install it by hand" verdict, which the current install then
    displayed as its own problem.

    A source install is exempt from both: its fields are commit shas, and whether a checkout is
    behind is git's answer, not a version comparison's."""
    import agitrack

    def _record(**kwargs):
        return selfupdate.SelfUpdateRecord(state=selfupdate.STATE_FAILED, **kwargs)

    running = agitrack.__version__
    assert _record(current=running, latest=running, method="pip").needs_user is False
    assert _record(current="0.6.10", latest="99.0.0", method="pip").needs_user is False
    # …while this install's own record about a genuinely newer release still asks for action.
    assert _record(current=running, latest="99.0.0", method="pip").needs_user is True
    # A sha is not a version: parsed as one it comes back as (0,), which read as "already newer
    # than that" and silently suppressed every source checkout's notice.
    assert _record(current="a1b2c3d", latest="e4f5a6b", method="source").needs_user is True


def test_a_mode_that_cannot_self_update_is_recorded_without_attempting(tmp_path, monkeypatch):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path))
    applied: list[str] = []

    class _Updater:
        kind = KIND_PACKAGE

        def _install_method(self):
            return METHOD_BREW_PYTHON

        def check(self, **kwargs):
            return types.SimpleNamespace(ok=True, available=True, current="1.0.0", latest="1.1.0", error="")

        def apply(self):
            applied.append("apply")  # must never happen for a brew install
            return types.SimpleNamespace(ok=True, error="", message="")

        def manual_update_instructions(self):
            return "Run: brew upgrade agitrack"

    monkeypatch.setattr("agitrack.update.updater.Updater", _Updater)

    record = _attempt_self_update()
    assert applied == []  # brew's install is not ours to touch
    assert record.state == selfupdate.STATE_MANUAL and record.method == METHOD_BREW_PYTHON
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

    record = _attempt_self_update()
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


def test_a_repo_dashboard_only_shows_the_install_notice_of_its_own_instance(tmp_path, monkeypatch):
    """The state file is global; the instances on the machine are not one install.

    A repo tracked from a source checkout updates itself silently, yet its dashboard carried a
    "this Python is externally managed, upgrade it yourself" banner — written by an unrelated
    tracker running a wheel in another project's venv, because whoever checks last owns the one
    global file. On a repo dashboard the subject is that repo's instance, and its fingerprint in
    the repo lock says which install that is."""
    import agitrack

    from agitrack.git.lock import RepoLock
    from agitrack.metrics.web import _update_banner_html

    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "config"))
    repo_root = tmp_path / "repo"
    (repo_root / ".agitrack").mkdir(parents=True)
    repo = types.SimpleNamespace(repo=repo_root)
    selfupdate.write_state(
        selfupdate.SelfUpdateRecord(
            state=selfupdate.STATE_FAILED,
            method="pipx",
            current=agitrack.__version__,
            latest="99.0.0",
            error="pipx could not upgrade it",
        )
    )

    # No instance on this repo (a dashboard-only repo): the global installation is the subject.
    assert "99.0.0" in _update_banner_html(repo)

    lock = RepoLock(repo_root / ".agitrack" / "lock")
    for fingerprint, expected in (
        ("commit:abc123", ""),  # tracked from a source checkout — the wheel's verdict is not its own
        (f"version:{agitrack.__version__}", "99.0.0"),  # the very install the record was written by
    ):
        monkeypatch.setattr("agitrack.update.restart.RUNNING_FINGERPRINT", fingerprint)
        # Same on disk, so the OTHER notice ("restart your session") stays out of the way.
        monkeypatch.setattr("agitrack.update.restart.disk_fingerprint", lambda f=fingerprint: f)
        assert lock.acquire() is True
        try:
            banner = _update_banner_html(repo)
        finally:
            lock.release()
        assert (expected in banner) if expected else banner == ""


def test_the_install_notice_speaks_versions_for_a_package_and_commits_for_a_source_checkout(tmp_path, monkeypatch):
    # A source checkout and a package install are updated in different units. `current`/`latest`
    # for a source install are short COMMIT HASHES (Updater._check_source), so the one-size
    # sentence rendered "aGiTrack a1b2c3d is available" — which reads like a version and says
    # nothing about how far behind the checkout is.
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path))
    from agitrack.metrics.web import _update_banner_html

    selfupdate.write_state(
        selfupdate.SelfUpdateRecord(
            state=selfupdate.STATE_MANUAL,
            method="source",
            current="a1b2c3d",
            latest="e4f5a6b",
            instructions="update manually by running `git pull` in the aGiTrack source checkout",
        )
    )
    source = _update_banner_html()
    assert "commit is available" in source and "a1b2c3d" in source and "e4f5a6b" in source
    assert "git pull" in source
    assert "installed by you" not in source  # a checkout is pulled, not installed

    import agitrack

    # `current` is the RUNNING version deliberately: a package record naming any other version
    # was written by a different install and is ignored on purpose (see the re-check test).
    selfupdate.write_state(
        selfupdate.SelfUpdateRecord(
            state=selfupdate.STATE_MANUAL, method="pipx", current=agitrack.__version__, latest="0.7.0"
        )
    )
    package = _update_banner_html()
    assert "aGiTrack 0.7.0 is available" in package and "commit" not in package


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
            error="pip could not upgrade it",
            instructions="update it with `pipx upgrade agitrack`",
        )
    )
    installed = _update_banner_html(repo)
    assert "has to be installed by you" in installed and "pipx upgrade agitrack" in installed
    assert "restart aGiTrack" not in installed

    selfupdate.write_state(selfupdate.SelfUpdateRecord(state=selfupdate.STATE_OK))
    monkeypatch.setattr("agitrack.update.selfupdate.running_session_is_stale", lambda root: True)
    restart = _update_banner_html(repo)
    assert "still on the old version" in restart and "has to be installed by you" not in restart

    # Both at once render as two separate notices, not one merged line.
    selfupdate.write_state(selfupdate.SelfUpdateRecord(state=selfupdate.STATE_FAILED, latest="1.2.0"))
    both = _update_banner_html(repo)
    assert both.count('class="updatebanner"') == 2


def test_the_stale_notice_says_something_different_about_a_daemon(monkeypatch, tmp_path):
    """A background tracker and an interactive session need OPPOSITE things said about them.

    The tracker restarts itself a minute or two after the update lands, so "restart aGiTrack when
    convenient" asks for work already under way — and that is what the page said, next to its own
    "reloaded because aGiTrack was updated" notice, which read as a contradiction. An interactive
    session is genuinely left alone (restarting it would interrupt the conversation), so there the
    user really is the only one who can act.
    """
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path))
    import json as _json

    from agitrack import daemons
    from agitrack.metrics.web import _update_banner_html

    repo = types.SimpleNamespace(repo=tmp_path / "repo")
    (repo.repo / ".agitrack").mkdir(parents=True)
    (repo.repo / ".agitrack" / "lock").write_text(_json.dumps({"pid": os.getpid()}), encoding="utf-8")
    selfupdate.write_state(selfupdate.SelfUpdateRecord(state=selfupdate.STATE_OK))
    monkeypatch.setattr("agitrack.update.selfupdate.running_session_is_stale", lambda root: True)

    monkeypatch.setattr(
        daemons,
        "list_running",
        lambda **kwargs: [daemons.DaemonInfo(pid=os.getpid(), kind="background", repo=str(repo.repo))],
    )
    daemon_banner = _update_banner_html(repo)
    assert "will load the new one by itself" in daemon_banner
    assert "restart aGiTrack when convenient" not in daemon_banner

    monkeypatch.setattr(daemons, "list_running", lambda **kwargs: [])
    session_banner = _update_banner_html(repo)
    assert "restart aGiTrack when convenient" in session_banner


def test_daemon_watcher_also_installs_updates(monkeypatch):
    """Watching alone only reacts to someone ELSE updating, so a machine whose only
    aGiTrack is a dashboard (or backtrace) daemon would never update. The watcher thread
    that restarts the daemon also drives the self-update — when asked to.

    Installing is OPT-IN (``self_update=True``, which both daemons pass) rather than the
    default: it fetches and merges the checkout aGiTrack runs from, and defaulting it on
    meant an unrelated watcher test did that to CI's own checkout mid-run."""
    import threading

    from agitrack.update import restart as update_restart

    attempts: list[str] = []
    monkeypatch.setattr("agitrack.update.selfupdate.attempt_self_update", lambda *a, **k: attempts.append("tried"))
    stop = threading.Event()
    seen: list[str] = []
    thread = update_restart.watch_for_update(
        stop, seen.append, interval=0.01, read_version=lambda: None, self_update=True, self_update_interval=999.0
    )
    for _ in range(200):
        if attempts:
            break
        threading.Event().wait(0.01)
    stop.set()
    thread.join(timeout=2)
    assert attempts, "a watching daemon must also install updates"

    # …and a watcher that did NOT ask for it never touches the updater.
    attempts.clear()
    stop2 = threading.Event()
    thread2 = update_restart.watch_for_update(stop2, seen.append, interval=0.01, read_version=lambda: None)
    threading.Event().wait(0.2)
    stop2.set()
    thread2.join(timeout=2)
    assert attempts == [], "self-update must be opt-in, never a side effect of watching"


def test_self_update_is_on_by_default_but_can_be_turned_off(tmp_path, monkeypatch):
    """Self-updating is the default — the user is not asked, ever. A machine that must stay
    pinned turns off the global ``self_update`` setting, and aGiTrack then only REPORTS the
    newer version instead of installing it, so the dashboards still say what to do."""
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path))
    from agitrack.config.settings import GlobalConfig

    assert GlobalConfig().self_update is True  # on unless the user says otherwise
    applied: list[str] = []

    class _Updater:
        kind = KIND_SOURCE

        def _install_method(self):
            return METHOD_PIP

        def check(self, **kwargs):
            return types.SimpleNamespace(ok=True, available=True, current="1.0.0", latest="1.1.0", error="")

        def apply(self):
            applied.append("applied")
            return types.SimpleNamespace(ok=True, error="", message="Updated.")

        def manual_update_instructions(self):
            return "Run: pip install --upgrade agitrack"

    monkeypatch.setattr("agitrack.update.updater.Updater", _Updater)

    assert _attempt_self_update().state == selfupdate.STATE_OK
    assert applied == ["applied"]  # default: installs it

    applied.clear()
    config = GlobalConfig()
    config.self_update = False
    record = _attempt_self_update()
    assert applied == []  # turned off: never installs
    # …but the user is still told, with the command that does it.
    assert record.state == selfupdate.STATE_MANUAL and record.needs_user is True
    assert record.latest == "1.1.0" and "turned off" in record.error
    assert "pip install --upgrade agitrack" in record.instructions
