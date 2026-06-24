import shutil
import subprocess

import pytest

from agitrack.commits import AgitrackActions
from agitrack.git import GitRepo
from agitrack.config import AgitrackState
from agitrack.git import WorktreeManager, _sanitize_name
from proxy_helpers import make_runner


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


def _commit(repo, name, content, message):
    (repo.repo / name).write_text(content)
    repo.stage_paths([name])
    repo.commit(message)


def _make_session(main, name, base, *, backend="test", turn=1):
    # Create a session worktree (detached at base) and put it on its first turn
    # branch, exactly as the app does lazily on the first commit via
    # _ensure_turn_branch. Returns (WorktreeInfo, worktree GitRepo).
    wm = WorktreeManager(main)
    info = wm.create(name, base=base)
    work = GitRepo.discover(info.path)
    work.switch(wm.turn_branch(name, turn, backend=backend), create=True)
    return info, work


def test_discard_all_changes_resets_tracked_and_removes_untracked(tmp_path):
    repo = _init_repo(tmp_path)
    # Edit a tracked file and create an untracked one — the leftovers a cancelled
    # agent turn might leave behind.
    (tmp_path / "f.txt").write_text("agent edit\n")
    (tmp_path / "new.txt").write_text("agent new file\n")
    repo.stage_paths(["f.txt"])  # one staged, plus an untracked file
    assert repo.has_changes()

    repo.discard_all_changes()

    assert not repo.has_changes()
    assert (tmp_path / "f.txt").read_text() == "base\n"  # tracked edit reverted
    assert not (tmp_path / "new.txt").exists()  # untracked file removed


# --- naming (pure) ---


def test_naming_helpers(tmp_path):
    repo = _init_repo(tmp_path)
    wm = WorktreeManager(repo)
    assert wm.worktree_path("feat x").name == "feat-x"
    # Branches are namespaced by backend then session: agit/<backend>/<name>/tN.
    assert wm.turn_branch("feat x", 2, backend="open code") == "agitrack/open-code/feat-x/t2"
    assert wm.is_agitrack_branch("agitrack/claude/feat-x/t0") is True
    assert wm.is_agitrack_branch("main") is False
    assert _sanitize_name("  ") == "session"


# --- worktree lifecycle against real git ---


def test_create_list_remove_worktree(tmp_path):
    repo = _init_repo(tmp_path)
    wm = WorktreeManager(repo)
    info = wm.create("feat", base="HEAD")
    assert info.path.is_dir()
    assert info.branch == ""  # detached at base; no branch until the first commit
    listed = wm.list()
    assert [w.name for w in listed] == ["feat"]

    # A turn branch appears only once the session commits.
    work = GitRepo.discover(info.path)
    work.switch(wm.turn_branch("feat", 1, backend="claude"), create=True)
    assert "agitrack/claude/feat/t1" in repo.list_branches("agitrack/")

    wm.remove("feat")
    assert not info.path.exists()
    assert "agitrack/claude/feat/t1" not in repo.list_branches("agitrack/")


def test_turn_branches_coexist_without_df_conflict(tmp_path):
    repo = _init_repo(tmp_path)
    wm = WorktreeManager(repo)
    info = wm.create("feat", base="HEAD")
    work = GitRepo.discover(info.path)
    # Successive turn branches must coexist under agit/<backend>/<name>/.
    work.switch(wm.turn_branch("feat", 1, backend="claude"), create=True, base="HEAD")
    work.switch(wm.turn_branch("feat", 2, backend="claude"), create=True, base="HEAD")
    assert "agitrack/claude/feat/t1" in repo.list_branches("agitrack/")
    assert "agitrack/claude/feat/t2" in repo.list_branches("agitrack/")


# --- merge behaviour against real git ---


def test_merge_clean(tmp_path):
    repo = _init_repo(tmp_path)
    base = repo.current_branch()
    repo.create_branch("topic", base)
    repo.switch("topic")
    _commit(repo, "new.txt", "hello\n", "add new")
    repo.switch(base)
    assert repo.merge("topic") is True
    assert (repo.repo / "new.txt").exists()


def test_merge_conflict_reports_and_aborts(tmp_path):
    repo = _init_repo(tmp_path)
    base = repo.current_branch()
    repo.create_branch("topic", base)
    repo.switch("topic")
    _commit(repo, "f.txt", "topic change\n", "topic edit")
    repo.switch(base)
    _commit(repo, "f.txt", "base change\n", "base edit")
    assert repo.merge("topic") is False
    assert "f.txt" in repo.unmerged_paths()
    repo.merge_abort()
    assert repo.unmerged_paths() == []


def _integration_runner(main_repo, worktree_repo, base_branch, name):
    runner = make_runner(
        repo=worktree_repo,
        base_repo=main_repo,
        _base_branch=base_branch,
        name=name,
        worktree=object(),  # non-None marks this as a worktree session
        turn=0,
        merge_ctx=None,
        master_fd=None,
        agent_in_flight=False,
    )
    runner.worktree_manager = WorktreeManager(main_repo)
    runner.backend = type("B", (), {"name": "test"})()
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._debug = lambda *a, **k: None
    runner._exiting = False
    # On a non-fast-forward integration aGiTrack surfaces a resolve options box;
    # default to "Merge automatically" so these tests exercise the agent path.
    runner._select_popup = lambda title, options: options[0]
    return runner


def test_integrate_clean_merge_advances_base(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "session-1", base)
    _commit(work, "agent.txt", "agent work\n", "<aGiTrack> work")

    runner = _integration_runner(main, work, base, "session-1")
    runner._integrate_session_turn()

    # Base fast-forwarded to include the agent's work.
    assert (main.repo / "agent.txt").exists()
    # The transient turn branch is gone and the worktree is left detached at base,
    # so a fully-merged session leaves no branch behind.
    assert main.list_branches("agitrack/") == []
    assert work.is_detached()
    # A fresh, backend-namespaced turn branch is created only when it next commits.
    runner._ensure_turn_branch()
    assert work.current_branch() == "agitrack/test/session-1/t1"
    assert runner.turn == 1


def test_integrate_conflict_prompts_then_starts_agent_merge(tmp_path):
    main = _init_repo(tmp_path)  # f.txt == "base\n"
    base = main.current_branch()
    info, work = _make_session(main, "s1", base)
    _commit(work, "f.txt", "worktree change\n", "wt change")
    _commit(main, "f.txt", "base change\n", "base change")  # conflicting base advance
    base_head = main.rev_parse(base)

    runner = _integration_runner(main, work, base, "s1")
    runner._integrate_session_turn()  # conflict -> options box -> "Merge automatically"

    # Base untouched; after choosing auto-resolve the merge is in progress.
    assert main.rev_parse(base) == base_head
    assert work.merge_in_progress() is True
    assert work.unmerged_paths()  # conflict present
    assert runner.merge_ctx is not None and runner.merge_ctx["source_branch"] == "agitrack/test/s1/t1"


def test_integrate_conflict_leave_for_later_keeps_work_unintegrated(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "s1", base)
    _commit(work, "f.txt", "worktree change\n", "wt change")
    _commit(main, "f.txt", "base change\n", "base change")
    base_head = main.rev_parse(base)

    runner = _integration_runner(main, work, base, "s1")
    # Pick the last option, "Leave for later".
    runner._select_popup = lambda title, options: options[-1]
    runner._integrate_session_turn()

    # Nothing merged: base untouched, no merge in progress, work still on the branch.
    assert main.rev_parse(base) == base_head
    assert work.merge_in_progress() is False
    assert runner.merge_ctx is None
    assert "agitrack/test/s1/t1" in main.list_branches("agitrack/")


def test_finalize_agent_merge_commits_and_advances(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "s1", base, backend="claude")
    _commit(work, "f.txt", "worktree change\n", "wt change")
    _commit(main, "f.txt", "base change\n", "base change")

    runner = _integration_runner(main, work, base, "s1")
    runner.state = AgitrackState(info.path)
    runner.backend = type("B", (), {"name": "claude"})()
    runner._integrate_session_turn()  # -> conflict, merge in progress
    assert runner.merge_ctx is not None

    # Simulate the agent resolving the conflict in the worktree.
    (info.path / "f.txt").write_text("resolved: base + worktree\n")

    assert runner._finalize_agent_merge() is True
    # Base advanced to include the resolved merge; the worktree is left detached
    # at base with its turn branch deleted.
    assert (main.repo / "f.txt").read_text() == "resolved: base + worktree\n"
    assert work.is_detached()
    assert main.list_branches("agitrack/") == []
    assert runner.merge_ctx is None
    # The merge commit is tagged for an agent-resolved merge.
    assert "<aGiTrack-merge>" in main._run(["git", "log", "-1", "--format=%s"]).stdout


def test_turn_from_branch():
    runner = make_runner()
    assert runner._turn_from_branch("agitrack/session-1/t0") == 0
    assert runner._turn_from_branch("agitrack/feature/t5") == 5
    assert runner._turn_from_branch("main") == 0


def test_open_session_worktree_creates_then_reuses(tmp_path):
    main = _init_repo(tmp_path)
    runner = make_runner(
        base_repo=main,
        _base_branch=main.current_branch(),
    )
    runner.worktree_manager = WorktreeManager(main)

    info1, repo1 = runner._open_session_worktree("s1")
    assert info1.path.is_dir()
    assert repo1.is_detached()  # created detached at base, no branch yet

    # A second call reuses the same worktree (resume across runs) rather than failing.
    info2, repo2 = runner._open_session_worktree("s1")
    assert info2.path == info1.path


def test_worktree_manager_move_renames_directory(tmp_path):
    main = _init_repo(tmp_path)
    wm = WorktreeManager(main)
    info = wm.create("old-name", base=main.current_branch())
    (info.path / "work.txt").write_text("hi\n")  # some content to carry across the move

    new_info = wm.move("old-name", "new-name")

    assert new_info.name == "new-name"
    assert new_info.path.is_dir() and new_info.path.name == "new-name"
    assert (new_info.path / "work.txt").read_text() == "hi\n"  # content moved with it
    assert not info.path.exists()  # old directory is gone
    assert GitRepo(new_info.path).current_branch()  # still a valid worktree


def test_worktree_has_pending_work(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, repo = _make_session(main, "s", base)
    runner = make_runner(
        base_repo=main,
        _base_branch=base,
    )

    assert runner._worktree_has_pending_work(repo, repo.current_branch()) is False
    _commit(repo, "a.txt", "x\n", "work")
    assert runner._worktree_has_pending_work(repo, repo.current_branch()) is True


def test_reconcile_integrates_and_deletes_stale_worktrees(tmp_path):

    main = _init_repo(tmp_path)
    base = main.current_branch()
    wm = WorktreeManager(main)
    active = wm.create("session-1", base=base)
    wm.create("merged-one", base=base)  # clean, detached, nothing ahead of base
    _, pending_work = _make_session(main, "pending-one", base)
    _commit(pending_work, "p.txt", "pending\n", "pending work")

    runner = make_runner(
        repo=GitRepo.discover(active.path),  # the active session's worktree
        base_repo=main,
        _base_branch=base,
        worktree=active,
        name="session-1",
    )
    runner.worktree_manager = wm
    messages = []
    runner._set_message = lambda message, **kw: messages.append(message)
    runner._debug = lambda *a, **k: None

    runner._reconcile_sessions_on_startup()

    names = {info.name for info in wm.list()}
    # Stale worktrees are cleaned up; only the active session's worktree remains.
    assert names == {"session-1"}
    # The pending work was integrated into the base before its worktree went away
    # (its Claude conversation persists and stays resumable).
    assert (main.repo / "p.txt").exists()
    # A clean cleanup needs no user attention.
    assert messages == []


def test_reconcile_flags_conflicting_stale_worktree(tmp_path):

    main = _init_repo(tmp_path)  # f.txt == "base\n"
    base = main.current_branch()
    wm = WorktreeManager(main)
    active = wm.create("session-1", base=base)
    stale_info, stale_work = _make_session(main, "conflict-one", base)
    stale = stale_info
    _commit(stale_work, "f.txt", "stale change\n", "stale edit")
    _commit(main, "f.txt", "base change\n", "base edit")  # diverges -> conflict

    runner = make_runner(
        repo=GitRepo.discover(active.path),
        base_repo=main,
        _base_branch=base,
        worktree=active,
        name="session-1",
    )
    runner.worktree_manager = wm
    messages = []
    runner._set_message = lambda message, **kw: messages.append(message)
    runner._debug = lambda *a, **k: None

    runner._reconcile_sessions_on_startup()

    names = {info.name for info in wm.list()}
    # The conflicting worktree is kept (no clean merge) and surfaced to the user.
    assert "conflict-one" in names
    assert messages and "conflict-one" in messages[0]
    # The stale merge attempt left no merge in progress behind.
    assert GitRepo.discover(stale.path).merge_in_progress() is False


def test_ensure_turn_branch_creates_branch_for_detached_session(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    # A freshly created session is detached at base with no turn branch.
    info = WorktreeManager(main).create("s1", base=base)
    work = GitRepo.discover(info.path)
    assert work.is_detached()
    assert main.list_branches("agitrack/") == []

    runner = _integration_runner(main, work, base, "s1")
    runner._ensure_turn_branch()  # a new prompt arrives -> its own backend-namespaced branch
    assert work.current_branch() == "agitrack/test/s1/t1"

    # Already on a turn branch: no extra branch is created.
    runner._ensure_turn_branch()
    assert work.current_branch() == "agitrack/test/s1/t1"


def test_integrate_session_on_exit_merges_and_deletes_branch(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "s1", base)
    _commit(work, "a.txt", "x\n", "<aGiTrack> work")  # committed but not integrated

    runner = _integration_runner(main, work, base, "s1")
    runner._exiting = True
    runner._integrate_session_on_exit()

    # Work integrated into base; the worktree is detached and its branch gone.
    assert (main.repo / "a.txt").exists()
    assert work.is_detached()
    assert main.list_branches("agitrack/") == []


def test_integrate_session_on_exit_drops_empty_branch(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info = WorktreeManager(main).create("s1", base=base)
    work = GitRepo.discover(info.path)  # on agit/s1/t0 with nothing ahead of base

    runner = _integration_runner(main, work, base, "s1")
    runner._exiting = True
    runner._integrate_session_on_exit()

    assert work.is_detached()
    assert main.list_branches("agitrack/") == []


def test_active_has_pending_reflects_unintegrated_commits(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "s1", base)

    runner = _integration_runner(main, work, base, "s1")
    assert runner._active_has_pending() is False
    _commit(work, "a.txt", "x\n", "<aGiTrack> work")
    assert runner._active_has_pending() is True


def test_integrate_active_session_clean_merge(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "s1", base)
    _commit(work, "a.txt", "x\n", "<aGiTrack> work")  # an unintegrated commit

    runner = _integration_runner(main, work, base, "s1")
    runner._select_popup = lambda *a, **k: "Merge manually (you resolve here, then Complete merge)"
    runner._integrate_active_session()

    # The clean commit integrated and the base advanced; nothing left pending and
    # no branch lingers (the worktree is detached at base).
    assert (main.repo / "a.txt").exists()
    assert runner._active_has_pending() is False
    assert work.is_detached()
    assert main.list_branches("agitrack/") == []


def test_session_unintegrated_detects_pending_commits(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "s1", base)

    runner = _integration_runner(main, work, base, "s1")
    assert runner._integration.session_unintegrated(work) is False
    _commit(work, "a.txt", "x\n", "<aGiTrack> work")
    assert runner._integration.session_unintegrated(work) is True
    # After integration the worktree is detached and merged -> nothing pending.
    runner._integrate_session_turn()
    assert runner._integration.session_unintegrated(work) is False


def test_session_unintegrated_flags_conflict_in_progress(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info = WorktreeManager(main).create("s1", base=base)
    work = GitRepo.discover(info.path)
    _commit(work, "f.txt", "worktree change\n", "wt change")
    _commit(main, "f.txt", "base change\n", "base change")

    runner = _integration_runner(main, work, base, "s1")
    # Leave a conflicting merge in progress in the worktree.
    assert work.merge(base) is False
    assert runner._integration.session_unintegrated(work) is True


def test_align_refuses_cross_branch_merge(tmp_path):
    # The guard: aGiTrack must NEVER merge a branch into a worktree other than the
    # one that worktree records as its own merge branch (the demo-video→dev bug).
    main = _init_repo(tmp_path)
    base = main.current_branch()
    main.create_branch("demo-video", base)
    info, work = _make_session(main, "s1", base)
    AgitrackState(info.path).merge_branch = base  # this worktree merges into `base`

    runner = _integration_runner(main, work, base, "s1")
    runner.worktree = info
    runner._base_branch = "demo-video"  # but the runner is (wrongly) on another branch
    aligned: list = []
    runner._integration.align_session_to_base = lambda r: aligned.append(r) or "repointed"
    warnings: list = []
    runner._set_message = lambda m, **k: warnings.append(m)

    runner._align_session_to_base(work)

    assert aligned == []  # the merge was REFUSED, not performed
    assert any("cross-branch" in w.lower() for w in warnings)  # the user is warned


def test_sync_idle_worktrees_aligns_each_session_to_its_own_base(tmp_path):
    # Two sessions on different merge branches: the idle one must be aligned to ITS
    # OWN base, never the active session's (which would merge across branches).
    from agitrack.proxy.session import Session

    main = _init_repo(tmp_path)
    base = main.current_branch()
    main.create_branch("demo-video", base)
    info1, work1 = _make_session(main, "s1", base)
    info2, work2 = _make_session(main, "s2", "demo-video", backend="test")

    runner = _integration_runner(main, work2, "demo-video", "s2")  # active session merges into demo-video
    runner.worktree = info2
    runner.agent_in_flight = False

    idle = Session.bare()
    idle.repo = work1
    idle.worktree = info1
    idle.name = "s1"
    idle._base_branch = base  # the idle session merges into `base`
    idle.agent_in_flight = False
    runner.sessions = [runner.active, idle]

    recorded: list = []
    runner._align_session_to_base = lambda repo: recorded.append((runner.name, runner._base_branch))

    runner._sync_idle_worktrees_to_base()

    # Each session was aligned with ITS OWN branch — never crossed.
    assert ("s2", "demo-video") in recorded
    assert ("s1", base) in recorded


def _real_session(main, name, base, *, backend="test"):
    # A real, idle concurrent session: its own worktree + first turn branch + the merge
    # branch recorded in its on-disk state (what the cross-branch guard reads).
    from agitrack.proxy.session import Session

    info, work = _make_session(main, name, base, backend=backend)
    state = AgitrackState(info.path)
    state.backend = backend
    state.merge_branch = base
    session = Session.bare()
    session.repo, session.worktree, session._base_branch = work, info, base
    session.state, session.name = state, name
    session.backend = type("B", (), {"name": backend})()
    session.turn, session.merge_ctx, session.agent_in_flight = 1, None, False
    return session, info, work


def _concurrent_runner(main, sessions):
    # A runner driving several real, concurrent worktree sessions (active = the first).
    runner = make_runner(
        base_repo=main,
        repo=sessions[0].repo,
        worktree=sessions[0].worktree,
        _base_branch=sessions[0]._base_branch,
        state=sessions[0].state,
        name=sessions[0].name,
        turn=1,
        merge_ctx=None,
        agent_in_flight=False,
    )
    runner.active = sessions[0]
    runner.sessions = list(sessions)
    runner.worktree_manager = WorktreeManager(main)
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._debug = lambda *a, **k: None
    runner._exiting = False
    runner._integration_paused = False
    runner._select_popup = lambda title, options: options[0]
    # The agent's own commit is made directly in these tests, so the commit/summary
    # workers (which need a live backend) are no-ops here.
    runner._commit_latest_turn_sync = lambda *a, **k: None
    runner._service_commit_summary = lambda *a, **k: None
    return runner


def test_concurrent_sessions_integrate_into_their_own_branches_no_crosstalk(tmp_path):
    # Two sessions merging into DIFFERENT branches each commit and integrate; each
    # branch must receive ONLY its own session's work (the demo-video→dev contamination
    # class of bug), end to end against real git.
    main = _init_repo(tmp_path)
    base = main.current_branch()
    main.create_branch("dev", base)
    a, _, aw = _real_session(main, "sessA", base)
    b, _, bw = _real_session(main, "sessB", "dev")
    runner = _concurrent_runner(main, [a, b])

    _commit(aw, "a.txt", "from A\n", "<aGiTrack> A work")
    _commit(bw, "b.txt", "from B\n", "<aGiTrack> B work")
    runner.active = a
    runner._integrate_session_turn()
    runner.active = b
    runner._integrate_session_turn()

    main_files = {p.name for p in main.repo.iterdir() if p.is_file()}
    main.switch("dev")
    dev_files = {p.name for p in main.repo.iterdir() if p.is_file()}
    main.switch(base)
    assert "a.txt" in main_files and "b.txt" not in main_files  # main got A only
    assert "b.txt" in dev_files and "a.txt" not in dev_files  # dev got B only


def test_concurrent_idle_sync_really_aligns_each_to_its_own_base(tmp_path):
    # The REAL _sync_idle_worktrees_to_base (not mocked): after both base branches gain
    # commits out of band, each idle worktree must pick up ITS OWN base's commit and
    # never the other's.
    main = _init_repo(tmp_path)
    base = main.current_branch()
    main.create_branch("dev", base)
    a, _, aw = _real_session(main, "sessA", base)
    b, _, bw = _real_session(main, "sessB", "dev")
    # Both sessions are idle and detached at their base (no own work yet).
    aw.switch_detach(base)
    bw.switch_detach("dev")
    runner = _concurrent_runner(main, [a, b])

    main.switch("dev")
    _commit(main, "dev2.txt", "d2\n", "dev advance")
    main.switch(base)
    _commit(main, "main2.txt", "m2\n", "main advance")

    runner.active = a
    runner._sync_idle_worktrees_to_base()

    assert (aw.repo / "main2.txt").exists() and not (aw.repo / "dev2.txt").exists()
    assert (bw.repo / "dev2.txt").exists() and not (bw.repo / "main2.txt").exists()


def test_concurrent_conflict_on_shared_base_detected_and_base_untouched(tmp_path):
    # Two sessions on the SAME base with conflicting edits: the first integrates cleanly,
    # the second is detected as a conflict and the base is left untouched (the merge is
    # backed out, worktree clean).
    main = _init_repo(tmp_path)
    base = main.current_branch()
    main.create_branch("dev", base)
    c, _, cw = _real_session(main, "sessC", "dev")
    d, _, dw = _real_session(main, "sessD", "dev")
    runner = _concurrent_runner(main, [c, d])

    _commit(cw, "shared.txt", "C version\n", "<aGiTrack> C")
    _commit(dw, "shared.txt", "D version\n", "<aGiTrack> D")
    runner.active = c
    assert runner._integrate_turn_or_conflict() == "integrated"
    dev_head = main.rev_parse("dev")

    runner.active = d
    assert runner._integrate_turn_or_conflict() == "conflict"
    assert main.rev_parse("dev") == dev_head  # base untouched by the conflicting turn
    assert not dw.merge_in_progress()  # the conflicted merge was backed out


def test_concurrent_retarget_flushes_old_base_then_future_work_lands_on_new(tmp_path):
    # Retargeting a session: its pending work flushes into the OLD branch, and a later
    # commit integrates into the NEW branch — never leaking across.
    main = _init_repo(tmp_path)
    base = main.current_branch()
    main.create_branch("feature", base)
    a, _, aw = _real_session(main, "sessA", base)
    runner = _concurrent_runner(main, [a])
    runner.active = a

    _commit(aw, "a.txt", "work\n", "<aGiTrack> A")
    assert runner._retarget_active_session("feature") is True
    assert runner._base_branch == "feature" and a.state.merge_branch == "feature"
    assert (main.repo / "a.txt").exists()  # pending work flushed into the OLD base

    # New work after the retarget integrates into feature, not main.
    aw.switch(WorktreeManager(main).turn_branch("sessA", 2, backend="test"), create=True)
    _commit(aw, "a2.txt", "feature work\n", "<aGiTrack> A more")
    runner._integrate_session_turn()
    main.switch("feature")
    feature_files = {p.name for p in main.repo.iterdir() if p.is_file()}
    main.switch(base)
    assert "a2.txt" in feature_files
    assert not (main.repo / "a2.txt").exists()  # never leaked back into main


def test_repoint_current_to_base_detaches_at_new_base(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info = WorktreeManager(main).create("s1", base=base)
    work = GitRepo.discover(info.path)
    # A new base branch that has diverged from the old one.
    main.create_branch("release", base)
    main.switch("release")
    _commit(main, "r.txt", "r\n", "release work")
    release_sha = main.rev_parse("release")

    runner = _integration_runner(main, work, base, "s1")
    runner._base_branch = "release"  # base has already been switched
    runner.turn = 3
    runner._repoint_current_to_base()

    # The session keeps its worktree but now sits (detached) on the new base, so
    # its next turn branches from there.
    assert work.is_detached()
    assert work.rev_parse("HEAD") == release_sha
    assert runner.turn == 0


def test_base_switch_candidates_excludes_agitrack_and_current(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    main.create_branch("feature", base)
    WorktreeManager(main).create("s1", base=base)  # creates an agit/s1/t0 branch

    runner = make_runner(
        base_repo=main,
        _base_branch=base,
    )

    candidates = runner._integration.base_switch_candidates()
    assert "feature" in candidates
    assert base not in candidates
    assert all(not name.startswith("agitrack/") for name in candidates)


def test_fast_forward_branch_advances_only_on_true_ff(tmp_path):
    from agitrack.git import GitError

    main = _init_repo(tmp_path)
    base = main.current_branch()
    base_sha = main.rev_parse("HEAD")
    # 'ahead' descends from base (a real fast-forward).
    main.create_branch("ahead", base)
    main.switch("ahead")
    _commit(main, "a.txt", "a\n", "ahead work")
    ahead_sha = main.rev_parse("HEAD")
    # 'diverged' starts from the same base commit but takes its own path.
    main.create_branch("diverged", base_sha)
    main.switch("diverged")
    _commit(main, "b.txt", "b\n", "diverged work")
    main.switch("ahead")  # leave base un-checked-out, like a drifted directory

    # `is_ancestor` reflects fast-forward-ability; base never moved.
    assert main.rev_parse(base) == base_sha
    assert main.is_ancestor(base, "ahead") is True
    assert main.is_ancestor("ahead", base) is False

    # Advancing base to its descendant is allowed (a true ff) and moves the ref.
    main.fast_forward_branch(base, "ahead")
    assert main.rev_parse(base) == ahead_sha

    # 'diverged' is not a descendant of 'ahead', so advancing would drop commits
    # → refused.
    with pytest.raises(GitError):
        main.fast_forward_branch("ahead", "diverged")


def test_retarget_active_session_changes_merge_target_per_session(tmp_path):
    # The active session's merge branch moves to the new target (its worktree
    # re-points there); the repo directory is NOT checked out, so a session can merge
    # into a branch other than the one in the directory.
    main = _init_repo(tmp_path)
    base = main.current_branch()
    main.create_branch("feature", base)
    info, work = _make_session(main, "s1", base)

    runner = _integration_runner(main, work, base, "s1")
    runner.worktree = info
    runner.sessions = [runner.active]
    runner._commit_latest_turn_sync = lambda: None  # no real backend to parse

    ok = runner._retarget_active_session("feature")

    assert ok is True
    assert runner._base_branch == "feature"  # this session now merges into feature
    assert runner._integration.base_branch == "feature"  # the service follows the active session
    assert main.current_branch() == base  # the repo directory was NOT switched


def test_each_session_has_its_own_merge_branch(tmp_path):
    # Concurrent sessions can merge into independent branches. The single
    # IntegrationService is re-pointed to whichever session is active, so each
    # integrates into its own branch.
    from agitrack.proxy.session import Session

    main = _init_repo(tmp_path)
    base = main.current_branch()
    main.create_branch("feature", base)
    info, work = _make_session(main, "s1", base)
    runner = _integration_runner(main, work, base, "s1")
    runner.sessions = [runner.active]
    assert runner._integration.base_branch == base  # service follows the active session

    s2 = Session.bare()
    s2._base_branch = "feature"  # a second session targeting another branch
    runner.sessions.append(s2)
    runner.active = s2
    assert runner._base_branch == "feature"
    assert runner._integration.base_branch == "feature"  # the service re-points on switch

    runner.active = runner.sessions[0]  # back to the first session
    assert runner._base_branch == base
    assert runner._integration.base_branch == base


def test_change_session_merge_branch_menu_retargets_chosen_session(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    main.create_branch("feature", base)
    info, work = _make_session(main, "s1", base)
    runner = _integration_runner(main, work, base, "s1")
    runner.worktree = info
    runner.sessions = [runner.active]

    retargeted: list = []
    runner._retarget_active_session = lambda target: retargeted.append(target) or True
    # Pick the (only) session, then pick "feature" as its new merge branch.
    picks = iter([lambda title, options: options[0], lambda title, options: "feature"])
    runner._select_popup = lambda title, options: next(picks)(title, options)

    runner._change_session_merge_branch_menu()

    assert retargeted == ["feature"]


def test_prompt_new_session_base_defaults_to_dir_branch_or_picks_another(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    main.create_branch("feature", base)
    info, work = _make_session(main, "s1", base)
    runner = _integration_runner(main, work, base, "s1")
    runner.worktree = info
    runner._use_worktrees = True

    # Choosing the first option keeps the directory's branch (the default base).
    runner._select_popup = lambda title, options: options[0]
    assert runner._prompt_new_session_base() == base

    # Choosing another branch returns it (the new session merges into it).
    runner._select_popup = lambda title, options: "feature"
    assert runner._prompt_new_session_base() == "feature"

    # Cancelling the picker returns None.
    runner._select_popup = lambda title, options: None
    assert runner._prompt_new_session_base() is None


def test_log_range_lists_commits(tmp_path):
    repo = _init_repo(tmp_path)
    base_sha = repo.rev_parse("HEAD")
    _commit(repo, "a.txt", "a\n", "add a")
    out = repo.log_range(base_sha, "HEAD", paths=["a.txt"])
    assert "add a" in out


def test_align_session_to_base_repoints_clean_worktree(tmp_path):
    main = _init_repo(tmp_path)
    main.create_branch("feature", main.current_branch())
    main.switch("feature")
    _commit(main, "feat.txt", "feature\n", "feature commit")
    base = main.current_branch()  # "feature"
    # A reused worktree sitting at an older base (the initial commit, not feature).
    info = WorktreeManager(main).create("session-1", base="HEAD~1")
    work = GitRepo.discover(info.path)
    assert work.rev_parse("HEAD") != main.rev_parse(base)

    runner = _integration_runner(main, work, base, "session-1")
    runner._align_session_to_base(work)

    # Re-pointed to the branch the user launched from.
    assert work.rev_parse("HEAD") == main.rev_parse(base)


def test_align_session_to_base_keeps_worktree_with_pending_work(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "session-1", base)
    _commit(work, "w.txt", "work\n", "work")  # committed, ahead of base
    head_before = work.rev_parse("HEAD")

    runner = _integration_runner(main, work, base, "session-1")
    runner._align_session_to_base(work)

    # Has unintegrated commits, so it is left untouched for integration.
    assert work.rev_parse("HEAD") == head_before
    assert work.current_branch() == "agitrack/test/session-1/t1"


def test_align_session_to_base_merges_new_base_commits_into_worktree(tmp_path):
    main = _init_repo(tmp_path)  # f.txt == "base\n"
    base = main.current_branch()
    info, work = _make_session(main, "session-1", base)
    _commit(work, "w.txt", "work\n", "session work")  # own work, ahead of base
    # The base gains a new, non-conflicting commit (another session integrated)
    # after this worktree branched off it.
    _commit(main, "newbase.txt", "from base\n", "base advanced")

    runner = _integration_runner(main, work, base, "session-1")
    runner._align_session_to_base(work)

    # The new base commit is pulled into the worktree, its own work is kept, and
    # it stays on its turn branch with no merge left in progress.
    assert (work.repo / "newbase.txt").exists()
    assert (work.repo / "w.txt").exists()
    assert work.current_branch() == "agitrack/test/session-1/t1"
    assert work.merge_in_progress() is False


def test_poll_base_advanced_detects_out_of_band_commits(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    runner = make_runner(
        base_repo=main,
        _base_branch=base,
        worktree=object(),
        _base_advanced=False,
        _last_base_head=None,
        _base_poll_at=0.0,
    )
    runner._debug = lambda *a, **k: None

    # First poll only records the baseline — it never triggers a sync on startup.
    runner._poll_base_advanced()
    assert runner._base_advanced is False
    assert runner._last_base_head == main.rev_parse(base)

    # The user commits straight to the base branch, outside aGiTrack.
    _commit(main, "user.txt", "by hand\n", "user commit")
    runner._base_poll_at = 0.0  # bypass the 3s throttle for the test
    runner._poll_base_advanced()

    # The moved base flags a sync so idle worktrees pick the new commit up.
    assert runner._base_advanced is True
    assert runner._last_base_head == main.rev_parse(base)


def test_poll_base_advanced_noop_without_worktree(tmp_path):
    main = _init_repo(tmp_path)
    runner = make_runner(
        base_repo=main,
        _base_branch=main.current_branch(),
        worktree=None,  # legacy / non-worktree session: nothing to sync
        _base_advanced=False,
        _last_base_head=None,
        _base_poll_at=0.0,
    )

    runner._poll_base_advanced()
    assert runner._base_advanced is False


def test_align_session_to_base_skips_conflicting_base(tmp_path):
    main = _init_repo(tmp_path)  # f.txt == "base\n"
    base = main.current_branch()
    info, work = _make_session(main, "session-1", base)
    _commit(work, "f.txt", "session change\n", "session edit")  # edits f.txt
    head_before = work.rev_parse("HEAD")
    _commit(main, "f.txt", "base change\n", "base edit")  # conflicting edit on base

    runner = _integration_runner(main, work, base, "session-1")
    runner._align_session_to_base(work)

    # A conflicting base is backed out, leaving the worktree branch untouched for
    # the session's own integration to resolve — never a half-merged tree.
    assert work.merge_in_progress() is False
    assert work.rev_parse("HEAD") == head_before
    assert (work.repo / "f.txt").read_text() == "session change\n"


def test_remove_prunes_orphaned_directory_and_deletes_branches(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "s1", base)
    _commit(work, "a.txt", "x\n", "work")
    branch = work.current_branch()
    assert branch in main.list_branches("agitrack/")

    # Simulate a half-broken state: the directory vanished out-of-band.
    shutil.rmtree(info.path)
    WorktreeManager(main).remove("s1")

    # The stale worktree entry is pruned and the branch cleaned up (kept in sync).
    assert not info.path.exists()
    assert branch not in main.list_branches("agitrack/")
    assert info.path.name not in [w.name for w in WorktreeManager(main).list()]


def test_create_recovers_from_prunable_stale_entry(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    wm = WorktreeManager(main)
    info = wm.create("session-1", base=base)
    # The directory vanishes out-of-band, leaving git with a stale "prunable"
    # registration for the path (reproduces a worktree dir deleted by hand/crash).
    shutil.rmtree(info.path)
    porcelain = main._run(["git", "worktree", "list", "--porcelain"]).stdout
    assert "prunable" in porcelain  # the stale entry is present

    # Re-creating the session must succeed (prune the stale entry, then add fresh)
    # rather than failing with "already registered" and falling back to no worktree.
    info2 = wm.create("session-1", base=base)
    assert info2.path.is_dir()
    assert GitRepo.discover(info2.path).rev_parse("HEAD") == main.rev_parse(base)


def test_remove_worktree_on_exit_drops_merged_extra_session(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "session-2", base)
    _commit(work, "a.txt", "x\n", "<aGiTrack> work")

    runner = _integration_runner(main, work, base, "session-2")
    runner.worktree = info  # real WorktreeInfo so removal can find the path
    runner._primary_worktree_name = "session-1"  # this is an extra session
    runner.child_pid = None
    runner.master_fd = None
    runner._integrate_session_on_exit()  # integrate -> detached at base, merged
    runner._remove_worktree_on_exit()

    # A fully-merged *extra* session's worktree directory is gone on exit.
    assert not info.path.exists()
    assert info.name not in [w.name for w in WorktreeManager(main).list()]
    assert runner.worktree is None


def test_remove_worktree_on_exit_persists_primary_record_then_removes(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "session-1", base)
    _commit(work, "a.txt", "x\n", "<aGiTrack> work")

    runner = _integration_runner(main, work, base, "session-1")
    runner.worktree = info
    runner._primary_worktree_name = "session-1"  # the auto-resume session
    runner.child_pid = None
    runner.master_fd = None
    runner.global_config = type("G", (), {"default_backend": "opencode"})()
    runner.state = AgitrackState(info.path)
    runner.state.backend = "opencode"
    runner.state.backend_session_id = "sess-xyz"

    runner._integrate_session_on_exit()
    runner._remove_worktree_on_exit()

    # The worktree (and its working state) are removed...
    assert not info.path.exists()
    assert runner.worktree is None
    # ...but the resume pointer was saved to the durable repo-root state, so the
    # conversation auto-resumes on the next start.
    root = AgitrackState(main.repo)
    assert root.backend_session_id == "sess-xyz"
    assert root.backend == "opencode"


def test_remove_worktree_on_exit_keeps_unintegrated_session(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "session-1", base)
    _commit(work, "f.txt", "wt change\n", "wt")
    _commit(main, "f.txt", "base change\n", "base")  # conflicts with base

    runner = _integration_runner(main, work, base, "session-1")
    runner.worktree = info
    runner.child_pid = None
    runner.master_fd = None
    runner._exiting = True
    runner._integrate_session_on_exit()  # conflict -> aborts, leaves the work
    runner._remove_worktree_on_exit()

    # A session that could not be integrated keeps its worktree for next startup.
    assert info.path.exists()


def test_remove_worktree_on_exit_removes_dirty_worktree_after_copy_offer(tmp_path):
    # Regression: a fully-integrated session whose worktree still holds uncommitted
    # leftover files (e.g. an untracked file the user declined / a copied-out file
    # whose source remains) must NOT linger forever. On an interactive exit the copy
    # offer runs, then the worktree is removed — no "open end".
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "session-1", base)
    _commit(work, "a.txt", "x\n", "<aGiTrack> work")

    runner = _integration_runner(main, work, base, "session-1")
    runner.worktree = info
    runner.child_pid = None
    runner.master_fd = None
    runner.screen = object()  # interactive exit: a UI exists to offer the copy
    runner._exit_aborted = False
    offered: list = []
    runner._offer_copy_unstaged_to_base = lambda **k: offered.append(k.get("context"))

    runner._integrate_session_on_exit()  # clean integrate -> nothing ahead of base
    (work.repo / "leftover.txt").write_text("stranded\n")  # uncommitted leftover file
    assert work.has_changes()  # the worktree is dirty...
    runner._remove_worktree_on_exit()

    # ...but it is still removed (after the copy offer), not stranded.
    assert offered == ["exit"]
    assert not info.path.exists()
    assert runner.worktree is None


def test_remove_worktree_on_signal_exit_keeps_dirty_worktree(tmp_path):
    # On a signal teardown (terminal/window closed: screen is None) there is no UI to
    # offer the copy, so a worktree with leftover files is KEPT rather than silently
    # discarding the user's uncommitted work — the next startup surfaces it.
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "session-1", base)
    _commit(work, "a.txt", "x\n", "<aGiTrack> work")

    runner = _integration_runner(main, work, base, "session-1")
    runner.worktree = info
    runner.child_pid = None
    runner.master_fd = None
    runner.screen = None  # signal teardown: no UI
    runner._offer_copy_unstaged_to_base = lambda **k: pytest.fail("no copy offer without a UI")

    runner._integrate_session_on_exit()
    (work.repo / "leftover.txt").write_text("stranded\n")
    runner._remove_worktree_on_exit()

    # Kept so the leftover file isn't lost.
    assert info.path.exists()


def test_integrate_active_session_fast_forward_does_not_prompt(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "session-1", base)
    _commit(work, "a.txt", "x\n", "<aGiTrack> work")  # base unchanged -> fast-forwardable

    runner = _integration_runner(main, work, base, "session-1")
    runner.worktree = info
    runner._select_popup = lambda *a, **k: pytest.fail("must not prompt for a clean fast-forward")
    runner._prompt_resolve_conflict = lambda *a, **k: pytest.fail("agent must not be involved in a FF")

    runner._integrate_active_session()

    # Integrated directly with no agent / no prompt.
    assert (main.repo / "a.txt").exists()
    assert work.is_detached()
    assert main.list_branches("agitrack/") == []


def test_integrate_active_session_conflict_prompts(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "session-1", base)
    _commit(work, "f.txt", "wt change\n", "wt")
    _commit(main, "f.txt", "base change\n", "base")  # diverges -> conflict

    runner = _integration_runner(main, work, base, "session-1")
    runner.worktree = info
    prompted = []
    runner._prompt_resolve_conflict = lambda src: prompted.append(src)

    runner._integrate_active_session()

    # Only a real conflict surfaces the resolve options.
    assert prompted == ["agitrack/test/session-1/t1"]


def test_remember_session_for_backend_persists_to_root(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "session-2", base)

    runner = _integration_runner(main, work, base, "session-2")
    runner.worktree = info
    runner.global_config = type("G", (), {"default_backend": "claude"})()
    runner.state = AgitrackState(info.path)
    runner.state.backend = "opencode"
    runner.state.backend_session_id = "sess-77"

    runner._remember_session_for_backend()

    # The opencode conversation is remembered in the durable repo-root state, keyed
    # by backend, with the worktree it ran in — so switching back resumes it.
    rec = AgitrackState(main.repo).recall_session("opencode")
    assert rec["id"] == "sess-77"
    assert rec["worktree"] == "session-2"


def test_advance_base_to_flags_base_advanced(tmp_path):
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "session-1", base)
    _commit(work, "a.txt", "x\n", "<aGiTrack> work")

    runner = _integration_runner(main, work, base, "session-1")
    runner._base_advanced = False
    runner._integrate_turn_or_conflict()  # clean fast-forward -> _advance_base_to

    assert runner._base_advanced is True


def test_sync_idle_worktree_fast_forwards_to_advanced_base(tmp_path):
    import types

    main = _init_repo(tmp_path)
    base = main.current_branch()
    # An idle session, detached at the base.
    info = WorktreeManager(main).create("session-2", base=base)
    work = GitRepo.discover(info.path)
    assert work.is_detached()
    base_before = main.rev_parse(base)
    # The base advances (as if another session integrated work).
    _commit(main, "new.txt", "x\n", "advance base")
    assert main.rev_parse(base) != base_before

    runner = _integration_runner(main, work, base, "session-2")
    runner.repo = work
    runner.agent_in_flight = False
    runner.active_index = 0
    runner.sessions = [types.SimpleNamespace(repo=work, agent_in_flight=False)]

    runner._sync_idle_worktrees_to_base()

    # The idle worktree fast-forwarded onto the advanced base.
    assert work.rev_parse("HEAD") == main.rev_parse(base)


# --- _ensure_worktree_alive ---


def test_ensure_worktree_alive_none_returns_early():
    runner = make_runner(worktree=None)
    runner._ensure_worktree_alive()
    assert runner.worktree is None


def test_ensure_worktree_alive_path_exists_returns_early(tmp_path):
    main = _init_repo(tmp_path)
    wm = WorktreeManager(main)
    info = wm.create("s1", base=main.current_branch())
    repo = GitRepo.discover(info.path)
    runner = make_runner(
        repo=repo,
        base_repo=main,
        worktree=info,
    )
    repo_before = runner.repo

    runner._ensure_worktree_alive()

    assert runner.repo is repo_before
    assert info.path.exists()


def test_ensure_worktree_alive_recreates_worktree(tmp_path):
    from agitrack.backends.proxy_agents import make_proxy_agent

    main = _init_repo(tmp_path)
    wm = WorktreeManager(main)
    info = wm.create("session-1", base=main.current_branch())
    work = GitRepo.discover(info.path)
    shutil.rmtree(info.path)
    assert not info.path.exists()

    state = AgitrackState(info.path, default_backend="opencode")
    runner = make_runner(
        repo=work,
        base_repo=main,
        worktree=info,
        name="session-1",
        _base_branch=main.current_branch(),
        state=state,
        backend=make_proxy_agent("opencode"),
        actions=AgitrackActions(work, state),
        child_pid=None,
        master_fd=None,
        file_observer=None,
        passthrough_prompt=bytearray(),
        agent_in_flight=False,
        turn=0,
        merge_ctx=None,
    )
    runner.worktree_manager = wm
    runner.global_config = type("G", (), {"default_backend": "opencode"})()
    runner.verbose = False
    runner.sessions = [None]
    runner.active_index = 0

    messages = []
    runner._set_message = lambda msg, **kw: messages.append(msg)
    runner._render = lambda: None
    runner._debug = lambda *a, **k: None
    runner._teardown_child = lambda: None
    runner._stop_file_watcher = lambda: None
    runner._init_screen = lambda: None
    runner._spawn = lambda: None
    runner._start_file_watcher = lambda: None
    runner._resize_child = lambda: None
    runner._enable_host_mouse = lambda: None

    runner._ensure_worktree_alive()

    assert info.path.exists()
    assert runner.worktree is not None
    assert runner.worktree.path == info.path
    assert runner.repo.repo == info.path
    assert any("recreated" in m for m in messages), f"messages={messages}"


def test_ensure_worktree_alive_falls_back_on_open_session_failure(tmp_path):
    from agitrack.backends.proxy_agents import make_proxy_agent

    main = _init_repo(tmp_path)
    wm = WorktreeManager(main)
    info = wm.create("session-1", base=main.current_branch())
    work = GitRepo.discover(info.path)
    shutil.rmtree(info.path)
    assert not info.path.exists()

    state2 = AgitrackState(info.path, default_backend="opencode")
    runner = make_runner(
        repo=work,
        base_repo=main,
        worktree=info,
        name="session-1",
        _base_branch=main.current_branch(),
        state=state2,
        backend=make_proxy_agent("opencode"),
        actions=AgitrackActions(work, state2),
        child_pid=None,
        master_fd=None,
        file_observer=None,
        passthrough_prompt=bytearray(),
        agent_in_flight=False,
        turn=0,
        merge_ctx=None,
    )
    runner.worktree_manager = wm
    runner.global_config = type("G", (), {"default_backend": "opencode"})()
    runner.verbose = False
    runner.sessions = [None]
    runner.active_index = 0

    messages = []
    runner._set_message = lambda msg, **kw: messages.append(msg)
    runner._render = lambda: None
    runner._debug = lambda *a, **k: None
    runner._teardown_child = lambda: None
    runner._stop_file_watcher = lambda: None
    runner._init_screen = lambda: None
    runner._spawn = lambda: None
    runner._start_file_watcher = lambda: None
    runner._resize_child = lambda: None
    runner._enable_host_mouse = lambda: None

    def _failing_open(name):
        raise RuntimeError("worktree add failed")

    runner._open_session_worktree = _failing_open

    runner._ensure_worktree_alive()

    assert runner.worktree is None
    assert runner.name == "main"
    assert runner.repo is runner.base_repo
    assert any("base repo" in m for m in messages), f"messages={messages}"


def test_finalize_agent_merge_refuses_unresolved_conflict_markers(tmp_path):
    # Issue #13: `add_all()` used to run before the marker check, which cleared
    # the unmerged index state and made `git diff --check` (worktree vs index)
    # blind — so a merge the agent FAILED to resolve was committed with raw
    # <<<<<<< / ======= / >>>>>>> markers and fast-forwarded into the base.
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "s1", base, backend="claude")
    _commit(work, "f.txt", "worktree change\n", "wt change")
    _commit(main, "f.txt", "base change\n", "base change")
    base_head = main.rev_parse(base)

    runner = _integration_runner(main, work, base, "s1")
    runner.state = AgitrackState(info.path)
    runner.backend = type("B", (), {"name": "claude"})()
    runner._integrate_session_turn()  # -> conflict, merge in progress
    assert runner.merge_ctx is not None
    assert "<<<<<<<" in (info.path / "f.txt").read_text()

    # The agent failed to resolve: markers are still in the file.
    messages = []
    runner._set_message = lambda text, **k: messages.append(text)

    assert runner._finalize_agent_merge() is False
    # No merge commit, base untouched, the merge stays open for resolution.
    assert main.rev_parse(base) == base_head
    assert work.merge_in_progress() is True
    assert runner.merge_ctx is not None
    assert any("Conflict markers remain" in message for message in messages)

    # Once the markers ARE resolved a retry finalizes — even though the failed
    # attempt's add_all() already staged the marker-ridden version.
    (info.path / "f.txt").write_text("resolved: base + worktree\n")
    assert runner._finalize_agent_merge() is True
    assert (main.repo / "f.txt").read_text() == "resolved: base + worktree\n"
    assert main.rev_parse(base) != base_head


def test_has_conflict_markers_sees_staged_markers(tmp_path):
    # `git add -A` must not blind the marker check (issue #13).
    repo = _init_repo(tmp_path)
    (repo.repo / "f.txt").write_text("a\n<<<<<<< HEAD\nb\n=======\nc\n>>>>>>> side\n")
    assert repo.has_conflict_markers() is True
    repo.add_all()
    assert repo.has_conflict_markers() is True


def test_ensure_turn_branch_never_resets_existing_branch_with_work(tmp_path):
    # Issue #16: recovery paths restart the turn counter (a recreated worktree
    # is detached at base, so _turn_from_branch yields 0) while an earlier turn
    # branch may still exist holding unintegrated commits. The next
    # _ensure_turn_branch must not reuse its name — `git switch -C` used to
    # silently reset the branch and destroy that work.
    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "s1", base, backend="claude")
    _commit(work, "kept.txt", "unintegrated work\n", "<aGiTrack> kept work")
    kept_head = work.rev_parse("HEAD")  # tip of agit/claude/s1/t1

    # Simulate recovery: detached at base again, turn counter back at 0.
    work.switch_detach(base)
    runner = _integration_runner(main, work, base, "s1")
    runner.backend = type("B", (), {"name": "claude"})()
    runner.turn = 0

    runner._ensure_turn_branch()

    # The old branch and its commit survive; the session took the next free
    # turn number instead of resetting t1.
    assert main.rev_parse("agitrack/claude/s1/t1") == kept_head
    assert work.current_branch() == "agitrack/claude/s1/t2"
    assert runner.turn == 2


def test_switch_create_refuses_to_reset_existing_branch(tmp_path):
    from agitrack.git import GitError

    repo = _init_repo(tmp_path)
    repo.create_branch("topic", "HEAD")
    _commit(repo, "x.txt", "x\n", "advance main past topic")
    with pytest.raises(GitError):
        repo.switch("topic", create=True)  # -c, not -C: never resets
    # The branch is untouched and still where it was created.
    assert repo.rev_parse("topic") != repo.rev_parse("HEAD")


def test_agent_made_commits_integrate_when_idle(tmp_path):
    # The agent ran `git commit` itself, leaving the worktree clean — the
    # auto-commit path never fires, so integration must happen on idle instead
    # of waiting for exit/restart ("agit stopped merging commits").
    import time as time_mod

    main = _init_repo(tmp_path)
    base = main.current_branch()
    info, work = _make_session(main, "s1", base)
    _commit(work, "agent.txt", "committed by the agent itself\n", "agent's own commit")
    base_head = main.rev_parse(base)

    runner = _integration_runner(main, work, base, "s1")
    runner._integration_paused = False
    runner.agent_parse_thread = None
    runner.last_child_output = 0.0
    runner.CHILD_IDLE_SECONDS = 4.0
    runner.BASE_POLL_SECONDS = 3.0
    runner._idle_integrate_at = 0.0
    # The parse pipeline gets a chance to amend the trace onto the agent's own
    # commit first (#35); "parse consumed, nothing to attach" must still
    # integrate as-is.
    runner._finish_agent_parse_if_ready = lambda **kw: False

    runner._integrate_agent_made_commits_if_idle(time_mod.monotonic())

    # Base fast-forwarded to the agent's manual commit; worktree detached at
    # base with the turn branch cleaned up, exactly like a normal integration.
    assert main.rev_parse(base) != base_head
    assert (main.repo / "agent.txt").read_text() == "committed by the agent itself\n"
    assert work.is_detached()
    assert main.list_branches("agitrack/") == []
