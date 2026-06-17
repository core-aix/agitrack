import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agitrack import cli
from agitrack.config import DEFAULT_TIMINGS, GlobalConfig
from agitrack.update import KIND_PACKAGE, KIND_SOURCE, UpdateStatus, Updater
from agitrack.update.updater import _version_tuple
from proxy_helpers import make_runner


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], path)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "user.name", "t"], path)


def _commit(path: Path, name: str, content: str, message: str) -> None:
    (path / name).write_text(content)
    _git(["add", name], path)
    _git(["commit", "-qm", message], path)


@pytest.fixture
def source_clone(tmp_path: Path):
    """A 'remote' repo and a clone of it whose `main` tracks `origin/main` —
    the shape of a source-linked aGiTrack install. Returns (remote, clone)."""
    remote = tmp_path / "remote"
    _init_repo(remote)
    _commit(remote, "agit.py", "v1\n", "first")
    clone = tmp_path / "clone"
    _git(["clone", "-q", str(remote), str(clone)], tmp_path)
    _git(["config", "user.email", "t@t"], clone)
    _git(["config", "user.name", "t"], clone)
    return remote, clone


# --- version comparison (pure) ---------------------------------------------


def test_version_tuple_orders_numerically():
    assert _version_tuple("1.2.3") == (1, 2, 3)
    assert _version_tuple("10.0") > _version_tuple("9.9")
    # Non-numeric trailers fall back to the leading integer.
    assert _version_tuple("1.2.3rc1") == (1, 2, 3)
    assert _version_tuple("2.0.0") > _version_tuple("1.9.9")


# --- install-kind detection -------------------------------------------------


def test_kind_is_source_when_source_repo_present(source_clone):
    _, clone = source_clone
    assert Updater(source_repo=clone).kind == KIND_SOURCE


def test_kind_is_package_without_source_repo():
    assert Updater(source_repo=None).kind == KIND_PACKAGE


# --- source check -----------------------------------------------------------


def test_source_check_reports_no_update_when_in_sync(source_clone):
    _, clone = source_clone
    status = Updater(source_repo=clone).check()
    assert status.ok
    assert status.available is False
    assert status.behind == 0


def test_source_check_detects_upstream_commits(source_clone):
    remote, clone = source_clone
    # Advance the remote by two commits; the clone is now behind.
    _commit(remote, "agit.py", "v2\n", "second")
    _commit(remote, "agit.py", "v3\n", "third")
    status = Updater(source_repo=clone).check()
    assert status.ok
    assert status.available is True
    assert status.behind == 2
    assert "2 new commits" in status.message


def test_source_check_restart_when_checkout_updated_under_running_process(source_clone):
    # The checkout was fast-forwarded (a prior self-update, or a manual pull) while
    # this process kept running the old code. The check must see the running copy
    # is stale and offer a restart, even though HEAD is now in sync with upstream.
    remote, clone = source_clone
    updater = Updater(source_repo=clone)
    assert updater.check().available is False  # first check snapshots the running HEAD (in sync)

    _commit(remote, "agit.py", "v2\n", "second")
    Updater(source_repo=clone).apply()  # a separate actor fast-forwards the checkout

    status = updater.check()  # same process: its running code is now older than disk
    assert status.available is True
    assert status.restart_only is True
    assert "restart" in status.message.lower()


def test_source_check_detects_local_update_even_when_offline(source_clone, monkeypatch):
    # The running process must learn the checkout advanced under it even when the
    # network fetch fails (offline) — local staleness is detectable with no remote.
    remote, clone = source_clone
    updater = Updater(source_repo=clone)
    assert updater.check().available is False  # snapshots the running HEAD (in sync)

    # Advance the LOCAL checkout directly (no remote push), then make every fetch fail.
    _commit(clone, "agit.py", "v2\n", "local second")
    real_run = subprocess.run

    def fail_fetch(args, **kwargs):
        if "fetch" in args:
            return subprocess.CompletedProcess(args, 1, "", "could not resolve host")
        return real_run(args, **kwargs)

    monkeypatch.setattr("agitrack.update.updater.subprocess.run", fail_fetch)

    status = updater.check()
    assert status.available is True
    assert status.restart_only is True  # the running copy is older than disk
    assert "restart" in status.message.lower()


def test_source_check_snapshots_running_rev_at_construction(source_clone, monkeypatch):
    # The running rev is captured when the Updater is built, not on the first check —
    # so a local update that lands before any successful check is still seen as stale.
    remote, clone = source_clone
    updater = Updater(source_repo=clone)  # snapshot taken here (HEAD == v1)

    # The checkout advances before the very first check() ever runs.
    _commit(clone, "agit.py", "v2\n", "local second")

    status = updater.check(fetch=False)  # no network at all
    assert status.available is True
    assert status.restart_only is True


def test_source_check_errors_without_upstream(tmp_path: Path):
    repo = tmp_path / "solo"
    _init_repo(repo)
    _commit(repo, "agit.py", "v1\n", "first")  # main, no upstream and no remote
    status = Updater(source_repo=repo).check()
    assert not status.ok
    assert status.available is False
    assert "upstream" in (status.error or "")


def test_source_check_detects_update_on_branch_without_upstream(source_clone):
    # aGiTrack usually runs on a session worktree branch (`agit/...`) that tracks no
    # upstream of its own. The check must still find updates by comparing against
    # origin's default branch, not silently report "up to date".
    remote, clone = source_clone
    _git(["checkout", "-q", "-b", "agit/session-1"], clone)  # branch with no upstream
    _commit(remote, "agit.py", "v2\n", "second")  # origin's default branch advances
    status = Updater(source_repo=clone).check()
    assert status.ok
    assert status.available is True
    assert status.behind == 1


def test_source_apply_merges_default_branch_without_upstream(source_clone):
    # The same no-upstream branch must actually update by merging origin's default
    # branch when the user applies the update.
    remote, clone = source_clone
    _git(["checkout", "-q", "-b", "agit/session-1"], clone)
    _commit(remote, "agit.py", "v2\n", "second")
    result = Updater(source_repo=clone).apply()
    assert result.ok, result.error
    assert (clone / "agit.py").read_text() == "v2\n"  # upstream default branch merged in


# --- source apply -----------------------------------------------------------


def test_source_apply_fast_forwards(source_clone):
    remote, clone = source_clone
    _commit(remote, "agit.py", "v2\n", "second")
    updater = Updater(source_repo=clone)
    assert updater.check().available
    result = updater.apply()
    assert result.ok, result.error
    # The clone now carries the remote's content and is back in sync.
    assert (clone / "agit.py").read_text() == "v2\n"
    # The checkout is current, but THIS process is still running the pre-update
    # code, so the next check asks for a restart (not another download). In a real
    # run apply() is immediately followed by a re-exec, so this is never observed.
    after = updater.check()
    assert after.available is True and after.restart_only is True


def test_source_apply_refuses_dirty_tree(source_clone):
    remote, clone = source_clone
    _commit(remote, "agit.py", "v2\n", "second")
    (clone / "agit.py").write_text("local edit\n")  # uncommitted local change
    result = Updater(source_repo=clone).apply()
    assert not result.ok
    assert "uncommitted" in (result.error or "")
    assert (clone / "agit.py").read_text() == "local edit\n"  # untouched


def test_source_apply_merges_diverged_branch_cleanly(source_clone):
    # aGiTrack runs on session branches that accumulate the user's own commits, so the
    # checkout is routinely diverged from upstream. A divergence with NO conflicting
    # edits must merge cleanly — pulling in upstream while preserving local work —
    # rather than being refused.
    remote, clone = source_clone
    _commit(remote, "remote.py", "r\n", "remote change")  # upstream touches a new file
    _commit(clone, "local.py", "l\n", "local change")  # local touches a different file
    result = Updater(source_repo=clone).apply()
    assert result.ok, result.error
    assert (clone / "remote.py").read_text() == "r\n"  # upstream change pulled in
    assert (clone / "local.py").read_text() == "l\n"  # local work preserved


def test_source_apply_reports_merge_conflict(source_clone):
    # When upstream and local edit the SAME lines, an automatic merge can't resolve
    # it: report a clear "merge conflict" message and abort, leaving the running
    # source clean (no conflict markers, local work intact) instead of half-merged.
    remote, clone = source_clone
    _commit(remote, "agit.py", "remote v2\n", "remote change")  # same file...
    _commit(clone, "agit.py", "local v2\n", "local change")  # ...edited differently
    result = Updater(source_repo=clone).apply()
    assert not result.ok
    assert "merge conflict" in (result.error or "").lower()
    assert (clone / "agit.py").read_text() == "local v2\n"  # local kept, merge aborted
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(clone), text=True, stdout=subprocess.PIPE
    ).stdout
    assert porcelain.strip() == ""  # no in-progress merge / conflict state left behind


# --- package path (mocked index) -------------------------------------------


def test_package_check_available(monkeypatch):
    updater = Updater(source_repo=None)
    monkeypatch.setattr(updater, "_installed_version", lambda: "1.0.0")
    monkeypatch.setattr(updater, "_latest_package_version", lambda **k: "1.2.0")
    status = updater.check()
    assert status.kind == KIND_PACKAGE
    assert status.available is True
    assert status.current == "1.0.0" and status.latest == "1.2.0"


def test_package_check_up_to_date(monkeypatch):
    updater = Updater(source_repo=None)
    monkeypatch.setattr(updater, "_installed_version", lambda: "1.2.0")
    monkeypatch.setattr(updater, "_running_version", lambda: "1.2.0")  # running == installed
    monkeypatch.setattr(updater, "_latest_package_version", lambda **k: "1.2.0")
    assert updater.check().available is False


def test_package_check_restart_when_running_is_stale(monkeypatch):
    # The package on disk was already upgraded, but this process still runs the
    # old version — the check must offer a restart, not report "up to date".
    updater = Updater(source_repo=None)
    monkeypatch.setattr(updater, "_installed_version", lambda: "1.3.0")
    monkeypatch.setattr(updater, "_running_version", lambda: "1.2.0")
    monkeypatch.setattr(updater, "_latest_package_version", lambda **k: "1.3.0")  # index == installed
    status = updater.check()
    assert status.available is True
    assert status.restart_only is True
    assert "restart" in status.message.lower()


def test_package_check_errors_when_index_unreachable(monkeypatch):
    updater = Updater(source_repo=None)
    monkeypatch.setattr(updater, "_installed_version", lambda: "1.0.0")
    monkeypatch.setattr(updater, "_latest_package_version", lambda **k: None)
    status = updater.check()
    assert not status.ok


# --- package upgrade: pip / pipx / homebrew routing -------------------------

import sys

import agitrack
from agitrack.update.updater import METHOD_HOMEBREW, METHOD_PIP, METHOD_PIPX


def _detect_method(monkeypatch, *, install_path: str, prefix: str = "/usr") -> str:
    # Drive _install_method purely off where the code "lives": the resolved
    # agitrack package path plus sys.prefix as a backstop. Pin sys.prefix to a
    # neutral path so a brew-Python test runner doesn't leak a /Cellar/ marker in.
    monkeypatch.setattr(agitrack, "__file__", install_path)
    monkeypatch.setattr(sys, "prefix", prefix)
    return Updater(source_repo=None)._install_method()


def test_install_method_detects_pipx(monkeypatch):
    path = "/home/u/.local/pipx/venvs/agitrack/lib/python3.12/site-packages/agitrack/__init__.py"
    assert _detect_method(monkeypatch, install_path=path) == METHOD_PIPX


def test_install_method_detects_homebrew(monkeypatch):
    path = "/opt/homebrew/Cellar/python@3.12/3.12.4/lib/python3.12/site-packages/agitrack/__init__.py"
    assert _detect_method(monkeypatch, install_path=path) == METHOD_HOMEBREW


def test_install_method_defaults_to_pip(monkeypatch):
    path = "/home/u/venv/lib/python3.12/site-packages/agitrack/__init__.py"
    assert _detect_method(monkeypatch, install_path=path) == METHOD_PIP


def test_install_method_prefers_pipx_over_homebrew(monkeypatch):
    # A pipx venv created by a brew-installed pipx still upgrades via pipx, not brew.
    path = "/opt/homebrew/Cellar/pipx/1.0/libexec/pipx/venvs/agitrack/lib/python/site-packages/agitrack/__init__.py"
    assert _detect_method(monkeypatch, install_path=path) == METHOD_PIPX


def _record_run(monkeypatch, *, returncode=0, stdout="", stderr=""):
    """Stub subprocess.run in the updater, capturing each command it runs."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr("agitrack.update.updater.subprocess.run", fake_run)
    return calls


def test_apply_package_primary_path_is_running_interpreter_pip(monkeypatch):
    # The manager-independent path: the running interpreter's own pip, used for a
    # plain pip / venv / --user / pipx install alike — no pipx/brew shell-out.
    updater = Updater(source_repo=None)
    monkeypatch.setattr(updater, "_has_module_pip", lambda python: True)
    monkeypatch.setattr(updater, "_installed_version", lambda: "2.0.0")
    calls = _record_run(monkeypatch, returncode=0)
    status = updater.apply()
    assert status.ok and status.current == "2.0.0"
    assert calls == [[sys.executable, "-m", "pip", "install", "--upgrade", "agitrack"]]


def test_apply_package_pip_falls_back_to_pip3_on_path(monkeypatch):
    updater = Updater(source_repo=None)
    monkeypatch.setattr(updater, "_has_module_pip", lambda python: False)  # no `python -m pip`
    monkeypatch.setattr(
        "agitrack.update.updater.shutil.which", lambda name: "/usr/bin/pip3" if name == "pip3" else None
    )
    monkeypatch.setattr(updater, "_installed_version", lambda: "2.0.0")
    calls = _record_run(monkeypatch, returncode=0)
    assert updater.apply().ok
    assert calls == [["/usr/bin/pip3", "install", "--upgrade", "agitrack"]]


def test_apply_package_pip_failure_reports_last_line(monkeypatch):
    # A non-PEP668 pip failure surfaces the error tail and does NOT try a manager.
    updater = Updater(source_repo=None)
    monkeypatch.setattr(updater, "_has_module_pip", lambda python: True)
    calls = _record_run(monkeypatch, returncode=1, stderr="boom\nERROR: could not install")
    status = updater.apply()
    assert not status.ok
    assert status.error == "ERROR: could not install"
    assert len(calls) == 1  # no brew/second attempt


def test_apply_package_pep668_under_homebrew_defers_to_brew(monkeypatch):
    # Externally-managed (PEP 668) pip refusal + a Homebrew-owned install → brew upgrade.
    updater = Updater(source_repo=None)
    monkeypatch.setattr(updater, "_has_module_pip", lambda python: True)
    monkeypatch.setattr(updater, "_install_method", lambda: METHOD_HOMEBREW)
    monkeypatch.setattr("agitrack.update.updater.shutil.which", lambda name: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(updater, "_installed_version", lambda: "2.0.0")

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        # pip refuses (PEP 668); brew succeeds.
        if cmd[-2:] == ["--upgrade", "agitrack"] and "pip" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="error: externally-managed-environment")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("agitrack.update.updater.subprocess.run", fake_run)
    status = updater.apply()
    assert status.ok and status.current == "2.0.0"
    assert calls[-1] == ["/opt/homebrew/bin/brew", "upgrade", "agitrack"]


def test_apply_package_pep668_without_manager_enumerates_routes(monkeypatch):
    # PEP 668 refusal but not a recognisable Homebrew install → full enumeration.
    updater = Updater(source_repo=None)
    monkeypatch.setattr(updater, "_has_module_pip", lambda python: True)
    monkeypatch.setattr(updater, "_install_method", lambda: METHOD_PIP)
    _record_run(monkeypatch, returncode=1, stderr="error: externally-managed-environment")
    status = updater.apply()
    assert not status.ok
    error = status.error or ""
    assert "externally managed" in error
    # Every supported route is named.
    for token in (
        "pip install --upgrade agitrack",
        "pipx upgrade agitrack",
        "brew upgrade agitrack",
        "break-system-packages",
    ):
        assert token in error


def test_apply_package_no_pip_enumerates_routes(monkeypatch):
    # No pip reachable at all and no identifiable manager → enumerated guidance.
    updater = Updater(source_repo=None)
    monkeypatch.setattr(updater, "_pip_invocation", lambda: None)
    monkeypatch.setattr(updater, "_install_method", lambda: METHOD_PIP)
    status = updater.apply()
    assert not status.ok
    assert "could not upgrade aGiTrack automatically" in (status.error or "")


# --- config -----------------------------------------------------------------


def test_check_for_updates_defaults_on_and_persists(tmp_path: Path):
    config = GlobalConfig(path=tmp_path / "config.json")
    assert config.check_for_updates is True
    config.check_for_updates = False
    assert GlobalConfig(path=tmp_path / "config.json").check_for_updates is False


def test_update_check_seconds_timing_default():
    assert DEFAULT_TIMINGS["update_check_seconds"] == 300.0


# --- runner integration -----------------------------------------------------


class _FakeUpdater:
    def __init__(self, status: UpdateStatus):
        self._status = status
        self.applied = False

    def check(self) -> UpdateStatus:
        return self._status

    def apply(self) -> UpdateStatus:
        self.applied = True
        return UpdateStatus(kind=self._status.kind, message="updated", current="new")


def _available_status() -> UpdateStatus:
    return UpdateStatus(
        kind=KIND_SOURCE,
        available=True,
        current="aaaaaaa",
        latest="bbbbbbb",
        behind=1,
        message="aGiTrack update available: 1 new commit on origin/main (aaaaaaa → bbbbbbb).",
    )


def test_consume_update_result_notifies_once():
    runner = make_runner()
    runner._update_status = None
    runner._update_check_thread = None
    runner._update_worker_result = _available_status()

    class _DoneThread:
        def is_alive(self):
            return False

    runner._update_check_thread = _DoneThread()
    runner._consume_update_check_result()
    assert runner._update_status is not None and runner._update_status.available
    assert runner._update_offered is True
    assert "update available" in (runner.message or "")
    # A second consume with the same (already offered) status does not re-notify.
    runner.message = None
    runner._consume_update_check_result()
    assert runner.message is None


def test_ready_for_update_blocked_by_in_flight_agent():
    runner = make_runner()
    assert runner._ready_for_update() is True
    runner.active.agent_in_flight = True
    assert runner._ready_for_update() is False


def test_ready_for_update_blocked_by_active_merge():
    runner = make_runner()
    runner.merge_ctx = object()  # a conflict resolution is in progress
    assert runner._ready_for_update() is False


def test_ready_for_update_blocked_by_background_merge():
    runner = make_runner()
    runner.merge_ctx = None
    runner.sessions = [SimpleNamespace(merge_ctx=object())]  # a background session is merging
    assert runner._merge_session_active() is True
    assert runner._ready_for_update() is False


def test_no_update_prompt_during_merge_session():
    runner = make_runner()
    runner.merge_ctx = object()  # merge in progress
    runner._update_status = None
    runner._update_offered = False
    runner._update_worker_result = _available_status()
    runner._update_check_thread = SimpleNamespace(is_alive=lambda: False)

    runner._maybe_check_for_update()

    # The finished result is NOT consumed or surfaced while merging.
    assert runner._update_offered is False
    assert runner.message is None
    assert runner._update_status is None

    # Once the merge ends, the pending result surfaces on the next pass.
    runner.merge_ctx = None
    runner._maybe_check_for_update()
    assert runner._update_offered is True
    assert "update available" in (runner.message or "")


def test_handle_update_command_marks_pending_when_busy(monkeypatch):
    runner = make_runner()
    runner._update_status = _available_status()
    runner.active.agent_in_flight = True  # not ready -> should defer
    monkeypatch.setattr(runner, "_select_popup", lambda *a, **k: "Update when sessions finish")
    applied = []
    monkeypatch.setattr(runner, "_apply_update_and_restart", lambda: applied.append(True))
    runner._handle_update_command()
    assert runner._update_pending is True
    assert applied == []  # deferred, not applied while the agent is busy


def test_handle_update_command_applies_when_ready(monkeypatch):
    runner = make_runner()
    runner._update_status = _available_status()
    monkeypatch.setattr(runner, "_select_popup", lambda *a, **k: "Update when sessions finish")
    applied = []
    monkeypatch.setattr(runner, "_apply_update_and_restart", lambda: applied.append(True))
    runner._handle_update_command()
    assert runner._update_pending is True
    assert applied == [True]


def test_handle_update_command_runs_a_fresh_check(monkeypatch):
    # The menu must NOT trust the (up to 5-min stale) cached status: a fresh check
    # runs on demand, so a newer version that appeared since the last periodic check
    # is offered instead of wrongly reporting "up to date".
    runner = make_runner()
    runner._update_status = UpdateStatus(kind=KIND_SOURCE, available=False, message="aGiTrack is up to date.")
    fresh = _available_status()
    runner._updater = _FakeUpdater(fresh)
    monkeypatch.setattr(runner, "_select_popup", lambda *a, **k: "Not now")
    runner._handle_update_command()
    assert runner._update_status is fresh  # the cached "up to date" was replaced
    assert "update postponed" in (runner.message or "").lower()


def test_maybe_apply_pending_update_triggers_when_ready(monkeypatch):
    runner = make_runner()
    runner._update_pending = True
    runner._update_applying = False
    applied = []
    monkeypatch.setattr(runner, "_apply_update_and_restart", lambda: applied.append(True))
    runner._maybe_apply_pending_update()
    assert applied == [True]


class _StubUpdater:
    def __init__(self, status: UpdateStatus):
        self._status = status

    def apply(self) -> UpdateStatus:
        return self._status


def test_apply_update_failure_leaves_session_intact():
    # If apply() fails, aGiTrack must NOT tear the session down — the user keeps working
    # exactly where they were. (Regression: finalizing/removing the worktree first,
    # then discovering apply() failed, left the reactor on a deleted worktree and
    # the next `git status` crashed with FileNotFoundError.)
    runner = make_runner()
    runner._update_status = UpdateStatus(kind=KIND_SOURCE, available=True)
    runner._updater = _StubUpdater(UpdateStatus(kind=KIND_SOURCE, error="not a fast-forward"))
    runner.running = True
    runner._pending_restart = False
    finalized: list = []
    runner._finalize_pending_work = lambda: finalized.append(True)
    runner._exit_child = lambda: finalized.append("exit")
    runner._render = lambda: None

    runner._apply_update_and_restart()

    assert finalized == []  # nothing torn down
    assert runner.running is True  # still running
    assert runner._pending_restart is False  # no re-exec scheduled
    assert runner._update_applying is False  # reset so the user can retry
    assert "update failed" in (runner.message or "").lower()


def test_apply_update_success_finalizes_then_restarts():
    runner = make_runner()
    runner._update_status = UpdateStatus(kind=KIND_SOURCE, available=True)
    runner._updater = _StubUpdater(UpdateStatus(kind=KIND_SOURCE, message="Updated to abc123."))
    runner.running = True
    runner._pending_restart = False
    order: list = []
    runner._finalize_pending_work = lambda: order.append("finalize")
    runner._exit_child = lambda: order.append("exit_child")
    runner._render = lambda: None

    runner._apply_update_and_restart()

    assert order == ["finalize", "exit_child"]  # only torn down AFTER a successful apply
    assert runner._pending_restart is True
    assert runner.running is False


# --- restart re-exec args ---------------------------------------------------


def _capture_restart(monkeypatch, argv):
    from agitrack.update import updater as updater_mod

    monkeypatch.setattr(updater_mod.sys, "argv", argv)
    captured: list = []
    monkeypatch.setattr(updater_mod.os, "execv", lambda exe, args: captured.append(args))
    monkeypatch.setattr(updater_mod.sys.stdout, "flush", lambda: None)
    monkeypatch.setattr(updater_mod.sys.stderr, "flush", lambda: None)
    return captured


def test_restart_agitrack_appends_extra_args(monkeypatch):
    from agitrack.update import restart_agitrack

    captured = _capture_restart(monkeypatch, ["agit", "--backend", "claude"])
    restart_agitrack(["--skip-privacy-ack"])

    assert captured[0][1:] == ["-m", "agitrack", "--backend", "claude", "--skip-privacy-ack"]


def test_restart_agitrack_does_not_duplicate_existing_flag(monkeypatch):
    from agitrack.update import restart_agitrack

    captured = _capture_restart(monkeypatch, ["agit", "--skip-privacy-ack"])
    restart_agitrack(["--skip-privacy-ack"])

    # The flag is already present from a prior restart; don't accumulate it.
    assert captured[0].count("--skip-privacy-ack") == 1


def test_restart_agitrack_without_extra_args_preserves_argv(monkeypatch):
    # The startup-update path passes no extra args, so the restart re-shows the
    # privacy warning (no --skip-privacy-ack injected).
    from agitrack.update import restart_agitrack

    captured = _capture_restart(monkeypatch, ["agit", "--verbose"])
    restart_agitrack()

    assert captured[0][1:] == ["-m", "agitrack", "--verbose"]
    assert "--skip-privacy-ack" not in captured[0]


# --- CLI startup prompt -----------------------------------------------------


class _StartupUpdater:
    def __init__(self, status: UpdateStatus):
        self._status = status
        self.checked = False
        self.applied = False

    def check(self, *, fetch: bool = True, timeout: int = 20) -> UpdateStatus:
        self.checked = True
        self.checked_timeout = timeout  # the startup path passes a short bound
        return self._status

    def apply(self) -> UpdateStatus:
        self.applied = True
        return UpdateStatus(kind=self._status.kind, message="updated", current="new")


def test_startup_prompt_applies_and_restarts(monkeypatch, tmp_path: Path):
    config = GlobalConfig(path=tmp_path / "c.json")
    updater = _StartupUpdater(_available_status())
    restarted = []
    monkeypatch.setattr("agitrack.update.Updater", lambda *a, **k: updater)
    monkeypatch.setattr("agitrack.update.restart_agitrack", lambda: restarted.append(True))
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    cli._check_for_update_at_startup(config)
    assert updater.applied is True
    assert restarted == [True]


def test_startup_check_uses_short_timeout(monkeypatch, tmp_path: Path):
    # The launch-time check must be tightly bounded so an offline user isn't blocked
    # from starting aGiTrack — it passes the short STARTUP_NET_TIMEOUT, not the default.
    from agitrack.update import STARTUP_NET_TIMEOUT

    config = GlobalConfig(path=tmp_path / "c.json")
    updater = _StartupUpdater(UpdateStatus(kind=KIND_SOURCE, available=False))
    monkeypatch.setattr("agitrack.update.Updater", lambda *a, **k: updater)
    cli._check_for_update_at_startup(config)
    assert updater.checked_timeout == STARTUP_NET_TIMEOUT
    assert STARTUP_NET_TIMEOUT < 20  # meaningfully shorter than the in-session bound


def test_check_survives_network_timeout(monkeypatch, tmp_path: Path):
    # A git fetch that times out must NOT raise out of check(): the real _git
    # wrapper catches TimeoutExpired and reports a clean failure, so aGiTrack starts.
    repo = tmp_path / "src"
    repo.mkdir()
    updater = Updater(source_repo=repo)
    monkeypatch.setattr(updater, "_upstream_ref", lambda _r: "origin/main")

    def fake_run(args, **kwargs):
        if "fetch" in args:
            raise subprocess.TimeoutExpired(cmd="git fetch", timeout=kwargs.get("timeout", 1))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("agitrack.update.updater.subprocess.run", fake_run)
    status = updater.check(timeout=1)  # must not raise
    assert not status.ok  # reported as an error, gracefully


def test_startup_prompt_defaults_to_update_on_empty_enter(monkeypatch, tmp_path: Path):
    # A bare Enter (empty answer) takes the recommended path: update now.
    config = GlobalConfig(path=tmp_path / "c.json")
    updater = _StartupUpdater(_available_status())
    restarted = []
    monkeypatch.setattr("agitrack.update.Updater", lambda *a, **k: updater)
    monkeypatch.setattr("agitrack.update.restart_agitrack", lambda: restarted.append(True))
    monkeypatch.setattr("builtins.input", lambda *a: "")
    cli._check_for_update_at_startup(config)
    assert updater.applied is True
    assert restarted == [True]


def test_startup_prompt_skips_on_explicit_no(monkeypatch, tmp_path: Path):
    config = GlobalConfig(path=tmp_path / "c.json")
    updater = _StartupUpdater(_available_status())
    monkeypatch.setattr("agitrack.update.Updater", lambda *a, **k: updater)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    cli._check_for_update_at_startup(config)
    assert updater.applied is False
    assert config.check_for_updates is True  # "no" this time, but keep asking


def test_startup_prompt_never_disables_future_checks(monkeypatch, tmp_path: Path):
    config = GlobalConfig(path=tmp_path / "c.json")
    updater = _StartupUpdater(_available_status())
    monkeypatch.setattr("agitrack.update.Updater", lambda *a, **k: updater)
    monkeypatch.setattr("builtins.input", lambda *a: "never")
    cli._check_for_update_at_startup(config)
    assert config.check_for_updates is False
    assert updater.applied is False


def test_startup_prompt_skipped_when_up_to_date(monkeypatch, tmp_path: Path):
    config = GlobalConfig(path=tmp_path / "c.json")
    updater = _StartupUpdater(UpdateStatus(kind=KIND_SOURCE, available=False))
    prompted = []
    monkeypatch.setattr("agitrack.update.Updater", lambda *a, **k: updater)
    monkeypatch.setattr("builtins.input", lambda *a: prompted.append(True) or "y")
    cli._check_for_update_at_startup(config)
    assert prompted == []  # no prompt when there is nothing to install


def test_startup_check_skipped_when_disabled(monkeypatch, tmp_path: Path):
    config = GlobalConfig(path=tmp_path / "c.json")
    config.check_for_updates = False

    def boom(*a, **k):
        raise AssertionError("update check must not run when disabled")

    monkeypatch.setattr("agitrack.update.Updater", boom)
    cli._check_for_update_at_startup(config)  # returns without checking
