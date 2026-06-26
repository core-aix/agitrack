"""Tests for the pure-logic parts of agitrack/recovery.py.

RecoveryReport.summary() and RecoveryReport.did_work() are pure data-class
methods; RecoveryService._summarization_enabled() is a pure attribute read.
These need no git repo, subprocess, or PTY.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agitrack.recovery import RecoveryReport, RecoveryService


# ---------------------------------------------------------------------------
# RecoveryReport — did_work / summary
# ---------------------------------------------------------------------------


def test_did_work_false_on_empty_report():
    assert RecoveryReport().did_work() is False


def test_did_work_true_when_recovered():
    assert RecoveryReport(recovered=["sess-1"]).did_work() is True


def test_did_work_true_when_integrated():
    assert RecoveryReport(integrated=["sess-2"]).did_work() is True


def test_did_work_true_when_flagged():
    assert RecoveryReport(flagged=["sess-3"]).did_work() is True


def test_did_work_false_when_only_skipped_busy():
    # skipped_busy alone doesn't mean work was done
    assert RecoveryReport(skipped_busy=True).did_work() is False


def test_summary_skipped_busy():
    msg = RecoveryReport(skipped_busy=True).summary()
    assert "already running" in msg
    assert "skipped" in msg


def test_summary_nothing_to_recover():
    assert RecoveryReport().summary() == "Nothing to recover."


def test_summary_recovered_only():
    msg = RecoveryReport(recovered=["alpha", "beta"]).summary()
    assert "committed 2 finished turn(s)" in msg
    assert "alpha" in msg
    assert "beta" in msg
    assert msg.startswith("Recovery:")


def test_summary_integrated_only():
    msg = RecoveryReport(integrated=["gamma"]).summary()
    assert "integrated 1 session(s)" in msg
    assert "gamma" in msg


def test_summary_flagged_only():
    msg = RecoveryReport(flagged=["delta"]).summary()
    assert "need attention" in msg
    assert "delta" in msg


def test_summary_all_fields():
    report = RecoveryReport(recovered=["r1"], integrated=["i1"], flagged=["f1"])
    msg = report.summary()
    assert "committed" in msg
    assert "integrated" in msg
    assert "attention" in msg
    assert msg.endswith(".")


# ---------------------------------------------------------------------------
# RecoveryService._summarization_enabled
# ---------------------------------------------------------------------------


def _make_service(gc_enabled=None, state_enabled=None):
    gc = MagicMock()
    gc.summarization_enabled = gc_enabled
    state = MagicMock()
    state.summarization_enabled = state_enabled
    repo = MagicMock()
    repo.repo = MagicMock()
    repo.repo.__truediv__ = lambda self, other: MagicMock()
    svc = RecoveryService.__new__(RecoveryService)
    svc.base_repo = repo
    svc.global_config = gc
    svc._debug = lambda *a, **k: None
    return svc, state


def test_summarization_enabled_global_config_true_overrides_state():
    svc, state = _make_service(gc_enabled=True, state_enabled=False)
    assert svc._summarization_enabled(state) is True


def test_summarization_enabled_global_config_false_overrides_state():
    svc, state = _make_service(gc_enabled=False, state_enabled=True)
    assert svc._summarization_enabled(state) is False


def test_summarization_enabled_falls_back_to_state_when_gc_none():
    svc, state = _make_service(gc_enabled=None, state_enabled=False)
    assert svc._summarization_enabled(state) is False


def test_summarization_enabled_defaults_to_true_when_both_none():
    svc, state = _make_service(gc_enabled=None, state_enabled=None)
    assert svc._summarization_enabled(state) is True


# ---------------------------------------------------------------------------
# RecoveryService.recover — lock not acquired → skipped_busy
# ---------------------------------------------------------------------------


def test_recover_returns_skipped_busy_when_lock_not_acquired(tmp_path):
    repo = MagicMock()
    repo.repo = tmp_path
    svc = RecoveryService(repo)
    with patch("agitrack.recovery.RepoLock.acquire", return_value=False):
        report = svc.recover()
    assert report.skipped_busy is True
    assert report.did_work() is False


# ---------------------------------------------------------------------------
# RecoveryService._recover_locked — worktree list failure
# ---------------------------------------------------------------------------


def test_recover_locked_handles_worktree_list_exception(tmp_path):
    """If WorktreeManager.list() raises, _recover_locked returns an empty (no-work)
    report without propagating the exception."""
    repo = MagicMock()
    repo.repo = tmp_path
    repo.current_branch.return_value = "main"
    svc = RecoveryService(repo)
    svc._debug = lambda *a, **k: None
    with (
        patch("agitrack.recovery.WorktreeManager") as MockManager,
        patch("agitrack.recovery.IntegrationService"),
    ):
        MockManager.return_value.list.side_effect = RuntimeError("git exploded")
        report = svc._recover_locked()
    assert not report.did_work()
    assert not report.skipped_busy


# ---------------------------------------------------------------------------
# RecoveryService._recover_locked — per-worktree exception → flagged
# ---------------------------------------------------------------------------


def test_recover_one_exception_adds_to_flagged_once(tmp_path):
    """If _recover_one raises for a worktree, its name is added to flagged exactly once."""
    repo = MagicMock()
    repo.repo = tmp_path
    repo.current_branch.return_value = "main"
    svc = RecoveryService(repo)
    svc._debug = lambda *a, **k: None
    info = MagicMock()
    info.name = "sess-broken"
    with (
        patch("agitrack.recovery.WorktreeManager") as MockManager,
        patch("agitrack.recovery.IntegrationService"),
        patch.object(svc, "_recover_one", side_effect=RuntimeError("boom")),
    ):
        MockManager.return_value.list.return_value = [info]
        report = svc._recover_locked()
    assert report.flagged == ["sess-broken"]


# ---------------------------------------------------------------------------
# RecoveryService._recover_one — merge in progress → flagged
# ---------------------------------------------------------------------------


def test_recover_one_flags_mid_merge_worktree(tmp_path):
    """A worktree mid-merge is flagged for manual attention, never committed."""
    repo_mock = MagicMock()
    repo_mock.merge_in_progress.return_value = True
    info = MagicMock()
    info.name = "sess-mid-merge"
    report = RecoveryReport()
    svc = RecoveryService.__new__(RecoveryService)
    svc._debug = lambda *a, **k: None
    with patch("agitrack.recovery.GitRepo", return_value=repo_mock):
        svc._recover_one(info, MagicMock(), MagicMock(), report)
    assert "sess-mid-merge" in report.flagged
    assert not report.recovered


# ---------------------------------------------------------------------------
# RecoveryService._commit_finished_turn — no session_id → False
# ---------------------------------------------------------------------------


def test_commit_finished_turn_returns_false_when_no_session_id(tmp_path):
    repo_mock = MagicMock()
    info = MagicMock()
    info.path = tmp_path
    state_mock = MagicMock()
    state_mock.backend_session_id = None

    svc = RecoveryService.__new__(RecoveryService)
    svc.global_config = MagicMock()
    svc._debug = lambda *a, **k: None

    with patch("agitrack.recovery.AgitrackState", return_value=state_mock):
        result = svc._commit_finished_turn(repo_mock, info, MagicMock(), MagicMock())
    assert result is False
