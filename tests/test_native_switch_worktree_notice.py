"""A conversation switched with the BACKEND's own command is offered its own worktree.

`/clear`, `/resume` and OpenCode's picker start a new conversation inside the process aGiTrack
already spawned, whose cwd is this session's worktree. Left alone, that conversation shares the
worktree, the turn branch and the merge target of the conversation before it — while the status
bar shows a new name and id, so it reads exactly like a session aGiTrack started itself.

aGiTrack can give it a worktree of its own: create one and respawn the backend there with
``--resume``, so the conversation survives and only the process moves. That costs a restart, so
it is offered rather than assumed.

The failure this replaces was worse than the missing isolation. Confirmed in a live `/clear` run:
the session was RENAMED to the new conversation's name while its directory kept the old one, so
``self.name`` and ``self.worktree.name`` diverged — turn branches were filed under a name no
directory had, and at exit ``_remember_session_for_backend`` overwrote the chosen name with the
directory's, leaving two conversations sharing one name and the typed name gone. So when the
offer is declined the session now keeps its name, and only the tracked conversation id moves.

Real git worktrees, both backends.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from agitrack.config import AgitrackState, GlobalConfig
from agitrack.git import GitRepo
from agitrack.git.worktree import WorktreeManager
from agitrack.proxy.session import Session
from agitrack.transcripts.types import SessionRef
from proxy_helpers import make_runner
from agitrack.backends.proxy_agents import available_backends

# Every registered backend, so a newly added one is covered the moment it is registered
# (the pattern test_backend_parity.py already uses).
BACKENDS = available_backends()

SWITCHED = [
    SessionRef(id="old-session", updated=100.0, label="old"),
    SessionRef(id="new-session", updated=200.0, label="new"),
]

YES = "Yes"
NO = "No"

# An activity value meaning "this conversation's turn is running RIGHT NOW", resolved when the
# code under test asks rather than when the test set it up. `time.time()` frozen into the map
# at construction made these tests a race against their own setup: the runner build does real
# git work, and if it outran the idle window the "running" turn was already stale by the first
# assertion. That is exactly how it failed — green everywhere for months, then red once on a
# loaded windows-latest runner (see AGENTS.md, "Timing-dependent tests").
LIVE = "live"


def _init_repo(path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


def _runner(tmp_path, backend_name, *, refs, worktree: bool, answer=NO, activity=None):
    """A runner tracking ``old-session``, either inside a real worktree or on the base tree
    (the --no-worktree/manual shape, where there are no worktrees to hand out).

    ``answer`` is what the user picks at the own-worktree offer: the YES/NO prefix of an option,
    or None for Esc. ``activity`` maps a conversation id to the epoch of its last backend
    activity, so a test can hold a conversation's turn OPEN (a recent mtime) — the switch is
    only handled once the conversation switched TO has gone quiet.
    """
    base = _init_repo(tmp_path)
    if worktree:
        info = WorktreeManager(base).create("alpha", base="main")
        session_repo = GitRepo(info.path)
    else:
        info, session_repo = None, base
    state = AgitrackState(session_repo.repo, default_backend=backend_name)
    state.backend_session_id = "old-session"

    runner = make_runner(repo=session_repo, state=state)
    runner.base_repo = base
    runner.global_config = GlobalConfig(path=tmp_path / "global.json")
    runner.worktree = info
    runner.name = "alpha" if worktree else "main"
    runner._use_worktrees = worktree
    runner.state.data["backend"] = backend_name

    forgotten: list[tuple[str, str]] = []

    class _Backend:
        name = backend_name

        def list_sessions(self, _repo):
            return refs

        def session_activity_mtime(self, session_id):
            # None = "unknown", which is how a backend with no such signal answers.
            value = (activity or {}).get(session_id)
            return time.time() if value is LIVE else value

        def forget_session_in(self, repo, session_id):
            forgotten.append((str(repo), session_id))
            return True

    runner.backend = _Backend()
    runner.forgotten = forgotten
    runner._session_watch_at = 0.0
    runner.agent_in_flight = False
    runner.running = True
    runner._initialize_session_baseline = lambda: None
    runner._render = lambda *a, **k: None

    popups: list[tuple[str, list, str]] = []

    def fake_popup(title, options, *, detail=None):
        popups.append((title, list(options), "\n".join(detail or [])))
        if answer is None:
            return None
        # An acknowledgment popup (a lone "ok") has no YES/NO to pick.
        return next((option for option in options if option.startswith(answer)), options[0])

    runner._select_popup = fake_popup
    runner.popups = popups

    # Relocation is driven end-to-end elsewhere; here we record that it was requested.
    relocations: list[tuple[str, str]] = []
    runner.relocations = relocations
    runner._name_for_switched_conversation = lambda sid: "gamma"
    commits: list[str | None] = []
    runner.commits = commits
    runner._commit_latest_turn_sync = lambda: commits.append(runner.state.backend_session_id)
    runner._stop_file_watcher = lambda: None
    runner._teardown_child = lambda: None
    # A clean outgoing worktree unless a test dirties it: relocation refuses to walk away
    # from uncommitted changes.
    runner.repo.has_changes = lambda: False
    # Every conversation in SWITCHED existed before launch unless a test says otherwise.
    runner._pre_spawn_sessions = {ref.id: ref.updated for ref in refs}

    def fake_new_session(name, *, resume_session_id=None, **kw):
        relocations.append((name, resume_session_id))
        runner.sessions.append(runner.active)  # what a successful _new_session leaves behind

    runner._new_session = fake_new_session
    return runner


# ------------------------------------------------------------------ the offer


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_native_switch_offers_the_new_conversation_its_own_worktree(tmp_path, backend_name):
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=YES)

    runner._service_native_session_switch()

    assert len(runner.popups) == 1
    title, options, detail = runner.popups[0]
    assert "own worktree" in title
    assert any(option.startswith("Yes") for option in options)
    assert any(option.startswith("No") for option in options)
    assert ".agitrack/worktrees/alpha" in detail  # names the worktree it would otherwise share
    assert backend_name in detail


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_yes_moves_the_conversation_into_a_new_session(tmp_path, backend_name):
    # The relocation resumes THIS conversation in the new worktree — the conversation is
    # preserved, only the process moves.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=YES)

    runner._service_native_session_switch()

    assert runner.relocations == [("gamma", "new-session")]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_the_finished_turn_is_committed_before_the_offer_is_raised(tmp_path, backend_name):
    # A conversation is only visible to the watcher once it HAS CONTENT, so its first turn has
    # already run by the time the switch is noticed. The offer is a modal, and a modal blocks
    # the reactor's commit pass for as long as the user takes to answer — so the commit has to
    # happen first, or a `/clear` + one turn leaves a written file with no commit anywhere.
    # Seen live before this ordering existed.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=YES)
    order: list[str] = []
    runner._commit_latest_turn_sync = lambda: order.append(f"commit:{runner.state.backend_session_id}")
    runner._select_popup = lambda title, options, **kw: order.append("offer") or options[0]
    runner._teardown_child = lambda: order.append("teardown")
    original_new_session = runner._new_session

    def spy(name, **kw):
        order.append("new_session")
        original_new_session(name, **kw)

    runner._new_session = spy

    runner._service_native_session_switch()

    # Committed against the NEW conversation — the one that asked for the work — and before the
    # dialog that can block for minutes or move the session out from under the changes.
    assert order == ["commit:new-session", "offer", "teardown", "new_session"]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_declining_still_commits_the_finished_turn(tmp_path, backend_name):
    # The commit must not be contingent on the answer: staying put loses the turn just as
    # thoroughly if nothing records it.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=NO)

    runner._service_native_session_switch()

    assert runner.commits == ["new-session"]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_conversation_new_since_launch_is_committed_from_its_very_first_turn(tmp_path, backend_name):
    # initialize_session_baseline marks the newest complete turn as "already accounted for",
    # which is right for a startup resume and exactly wrong for a conversation the user just
    # started: nothing of it has ever been committed, so writing it off strands that turn.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=NO)
    runner._pre_spawn_sessions = {"old-session": 100.0}  # the new one did not exist at launch
    runner._initialize_session_baseline = lambda: setattr(
        runner.state, "last_backend_message_id", "msg-of-the-turn-just-run"
    )

    runner._service_native_session_switch()

    assert runner.state.last_backend_message_id is None


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_resuming_an_older_conversation_keeps_its_computed_baseline(tmp_path, backend_name):
    # The mirror image: /resume onto a conversation that predates this run. Its earlier turns
    # WERE committed by a previous run, and clearing the baseline would re-commit its whole
    # history as one enormous turn.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=NO)
    runner._pre_spawn_sessions = {"old-session": 100.0, "new-session": 150.0}
    runner._initialize_session_baseline = lambda: setattr(runner.state, "last_backend_message_id", "msg-already-done")

    runner._service_native_session_switch()

    assert runner.state.last_backend_message_id == "msg-already-done"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_no_keeps_the_conversation_here_and_keeps_the_session_name(tmp_path, backend_name):
    # The corruption this replaces: declining used to rename the session, splitting the name
    # from the worktree directory it must match.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=NO)

    runner._service_native_session_switch()

    assert runner.relocations == []
    assert runner.state.backend_session_id == "new-session"  # tracking still follows the switch
    assert runner.name == "alpha"  # ...but the session keeps its name
    assert runner.worktree.name == "alpha"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_declining_links_the_new_conversation_to_this_sessions_name(tmp_path, backend_name):
    # Both conversations now live in one worktree, so both must resolve to that worktree's name.
    # Leaving the new one unnamed is what let the startup fallback pick a name from elsewhere.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=NO)

    runner._service_native_session_switch()

    root = AgitrackState(runner.base_repo.repo, default_backend=backend_name)
    assert root.session_name_for("new-session") == "alpha"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_escape_is_treated_as_no(tmp_path, backend_name):
    # Esc must not relocate: a restart of the backend is not something to do on an ambiguous key.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=None)

    runner._service_native_session_switch()

    assert runner.relocations == []
    assert runner.state.backend_session_id == "new-session"
    assert runner.name == "alpha"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_cancelling_the_name_prompt_leaves_the_conversation_where_it_is(tmp_path, backend_name):
    # Backing out of the name prompt is still a decision not to move.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=YES)
    runner._name_for_switched_conversation = lambda sid: None

    runner._service_native_session_switch()

    assert runner.relocations == []
    assert runner.state.backend_session_id == "new-session"
    assert runner.name == "alpha"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_failed_relocation_restores_the_outgoing_session(tmp_path, backend_name):
    # _new_session leaves a BARE session behind when it bails (e.g. the worktree can't be
    # created). Stranding the runner on it would leave no screen and no PTY — aGiTrack dead.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=YES)
    outgoing = runner.active
    runner._new_session = lambda name, **kw: None  # bails without appending a session

    runner._service_native_session_switch()

    assert runner.active is outgoing
    assert runner.state.backend_session_id == "new-session"  # falls back to staying put
    assert runner.name == "alpha"


def _relocating_runner(tmp_path, backend_name):
    """A runner whose ``_new_session`` really does what relocation depends on: swaps the active
    session for a NEW one, in its own worktree, with its own state file."""
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=YES)
    runner.state.last_backend_message_id = "msg-old"

    def new_session(name, *, resume_session_id=None, **kw):
        info = WorktreeManager(runner.base_repo).create(name, base="main")
        session = Session.bare()
        session.name = name
        session.worktree = info
        session.repo = GitRepo(info.path)
        session.state = AgitrackState(info.path, default_backend=backend_name)
        session.state.backend_session_id = resume_session_id
        runner.relocations.append((name, resume_session_id))
        runner.active = session
        runner.sessions.append(session)

    runner._new_session = new_session
    return runner


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_relocation_gives_the_outgoing_worktree_its_own_conversation_back(tmp_path, backend_name):
    # THE restart mix-up. Detecting the switch necessarily re-points the still-active session at
    # the new conversation (the turn that just ran belongs to it and is committed there). If
    # relocation then carries that conversation off without putting the old session back, the
    # worktree it left is still recorded as running it — two worktrees, one conversation — and
    # the next start resolves the moved conversation's name from whichever worktree claims it.
    runner = _relocating_runner(tmp_path, backend_name)
    outgoing = runner.active

    runner._service_native_session_switch()

    assert runner.relocations == [("gamma", "new-session")]
    assert outgoing.state.backend_session_id == "old-session"
    assert outgoing.state.last_backend_message_id == "msg-old"
    assert runner.active.state.backend_session_id == "new-session"
    # …and durably, not just in memory: the next run reads these files.
    outgoing_state = AgitrackState(outgoing.worktree.path, default_backend=backend_name)
    assert outgoing_state.backend_session_id == "old-session"
    assert AgitrackState(runner.active.worktree.path).backend_session_id == "new-session"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_relocation_lands_the_moved_turn_on_the_base_before_cutting_the_worktree(tmp_path, backend_name):
    # The turn belongs to the conversation being moved but was committed in the worktree being
    # left. Cut the new worktree from a base branch that doesn't have it yet and the resumed
    # agent opens without the files it just wrote — live, it reported its own b.txt missing.
    runner = _relocating_runner(tmp_path, backend_name)
    order: list[str] = []
    runner._integrate_committed_turn_before_new_turn = lambda: order.append("integrate")
    inner = runner._new_session
    runner._new_session = lambda name, **kw: (order.append("new session"), inner(name, **kw))[1]

    runner._service_native_session_switch()

    assert order == ["integrate", "new session"]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_relocation_stops_the_old_worktree_claiming_the_moved_conversation(tmp_path, backend_name):
    # The other half of the same claim: Claude files a transcript per directory, so the moved
    # conversation leaves a copy in the worktree it ran in, and that copy is what the resume
    # list and `latest_session_id` read. The backend is asked to forget it there.
    runner = _relocating_runner(tmp_path, backend_name)
    outgoing = runner.active

    runner._service_native_session_switch()

    assert runner.forgotten == [(str(outgoing.worktree.path), "new-session")]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_declining_leaves_the_outgoing_session_tracking_the_new_conversation(tmp_path, backend_name):
    # The restore belongs to relocation ALONE. Answering No means this session keeps the new
    # conversation — putting the old id back there would point the session at a conversation
    # the user has left.
    runner = _relocating_runner(tmp_path, backend_name)
    runner.popups.clear()
    runner._select_popup = lambda title, options, **kw: next(
        (option for option in options if option.startswith(NO)), options[0]
    )

    runner._service_native_session_switch()

    assert runner.relocations == []
    assert runner.state.backend_session_id == "new-session"
    assert runner.forgotten == []


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_name_already_held_by_a_live_session_is_re_asked(tmp_path, backend_name):
    # A session and its worktree are 1:1, so accepting a name another live session holds would
    # aim two backends at one directory. Ask again rather than silently renaming.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=YES)
    del runner._name_for_switched_conversation  # exercise the real one
    runner._live_session_name_taken = lambda name: name == "taken"
    runner._next_session_name = lambda: "suggested"
    asked: list[str] = []
    answers = iter(["taken", "free"])

    def prompt(title, **kw):
        asked.append(title)
        return next(answers)

    runner._prompt_session_name = prompt

    runner._service_native_session_switch()

    assert runner.relocations == [("free", "new-session")]
    assert len(asked) == 2
    assert "already open" in asked[1]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_relocation_refuses_while_the_outgoing_worktree_is_dirty(tmp_path, backend_name):
    # Seen live: the pre-offer commit did not capture a `/clear` turn that CREATED a file, and
    # relocating on top of that left it untracked in a worktree the user had walked away from.
    # Refusing keeps it in view, and the ordinary commit pass picks it up once the reactor runs.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=YES)
    runner.repo.has_changes = lambda: True

    runner._service_native_session_switch()

    assert runner.relocations == []  # did not move
    assert runner.state.backend_session_id == "new-session"  # tracking still follows the switch
    assert runner.name == "alpha"
    assert any("isn't committed yet" in title for title, _o, _d in runner.popups)


# ------------------------------------------------------------------ guards


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_no_worktree_mode_is_never_offered_a_worktree(tmp_path, backend_name):
    # --no-worktree and --manual-commits run on the base tree by design; there is nothing to
    # hand out, and the naming flow there is unchanged.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=False, answer=YES)
    runner._restore_or_ask_session_name = lambda *a, **k: None

    runner._service_native_session_switch()

    assert runner.popups == []
    assert runner.relocations == []
    assert runner.state.backend_session_id == "new-session"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_staying_on_the_same_conversation_offers_nothing(tmp_path, backend_name):
    # The watcher runs on every reactor tick; without the switch guard it would prompt forever.
    runner = _runner(
        tmp_path,
        backend_name,
        refs=[SessionRef(id="old-session", updated=100.0, label="old")],
        worktree=True,
        answer=YES,
    )

    runner._service_native_session_switch()

    assert runner.popups == []
    assert runner.relocations == []
    assert runner.state.backend_session_id == "old-session"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_the_switch_waits_for_the_new_conversations_turn_to_finish(tmp_path, backend_name):
    # A conversation becomes visible the moment the user's PROMPT is written, not when the agent
    # has answered it. Every "is the agent busy" signal is keyed to the conversation just left,
    # which `/clear` froze — so acting immediately committed a turn that had not happened
    # ("committing finished turn with no final text response", live) and opened a modal over a
    # working agent, which on Yes walked the session away from files the turn had yet to write.
    runner = _runner(
        tmp_path,
        backend_name,
        refs=SWITCHED,
        worktree=True,
        answer=YES,
        activity={"new-session": LIVE},  # its turn is running right now
    )

    runner._service_native_session_switch()

    assert runner.popups == []
    assert runner.commits == []
    assert runner.relocations == []
    assert runner.state.backend_session_id == "old-session"  # tracking has not moved yet


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_the_switch_is_handled_once_the_new_conversation_goes_quiet(tmp_path, backend_name):
    # The deferral is a wait, not a refusal: the very next pass, with the turn finished, does
    # the whole thing — commit first, then offer.
    runner = _runner(
        tmp_path,
        backend_name,
        refs=SWITCHED,
        worktree=True,
        answer=YES,
        activity={"new-session": LIVE},
    )

    runner._service_native_session_switch()
    assert runner.relocations == []

    runner.backend.session_activity_mtime = lambda sid: time.time() - 3600  # turn finished
    runner._session_watch_at = 0.0
    runner._service_native_session_switch()

    assert runner.commits == ["new-session"]
    assert runner.relocations == [("gamma", "new-session")]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_merely_touched_transcript_does_not_read_as_a_switch(tmp_path, backend_name):
    # The listing ranks by FILE mtime, and aGiTrack's own writes move it: staging a resume,
    # mirroring a conversation into the base repo, retargeting a recorded cwd. Live, that made a
    # conversation abandoned with `/clear` hours earlier the newest file in the directory — it
    # was adopted as the session's own and took the session's name with it.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=YES)
    messages = {"old-session": 2_000.0, "new-session": 1_000.0}  # ours has the newer MESSAGES
    runner.backend.session_last_activity = lambda sid: messages.get(sid)

    runner._service_native_session_switch()

    assert runner.popups == []
    assert runner.commits == []
    assert runner.state.backend_session_id == "old-session"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_fresh_session_is_never_dragged_onto_a_conversation_from_a_PREVIOUS_run(tmp_path, backend_name):
    # Found live under --no-worktree --new-session. A fresh session has an id but no transcript
    # until its first prompt, so every conversation in the directory looks newer than "ours" —
    # and the previous run's conversation was adopted as a switch: status bar and state file said
    # the OLD conversation while the backend really was running the new one, and the name given
    # at the startup prompt seconds earlier was asked for a second time.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=False, answer=YES)
    runner.state.backend_session_id = "fresh-session"  # minted by --new-session; no transcript yet
    # SWITCHED is entirely pre-launch (the harness snapshots it as such), i.e. history.

    runner._service_native_session_switch()

    assert runner.popups == []  # no "New conversation detected — name this session?"
    assert runner.state.backend_session_id == "fresh-session"  # still tracking what we spawned


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_an_unbound_session_still_adopts_a_conversation_WRITTEN_since_launch(tmp_path, backend_name):
    # The other half: the guard must not stop a fresh session from picking up the conversation
    # its own backend just wrote (that is how a blank session gets bound at all).
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=False, answer=NO)
    runner.state.backend_session_id = "fresh-session"
    runner._pre_spawn_sessions = {"old-session": 100.0}  # new-session did not exist at launch
    runner._prompt_popup = lambda *a, **k: "beta"  # the no-worktree naming path, answered

    runner._service_native_session_switch()

    assert runner.state.backend_session_id == "new-session"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_real_switch_is_still_adopted_when_its_messages_are_newer(tmp_path, backend_name):
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=NO)
    messages = {"old-session": 1_000.0, "new-session": 2_000.0}
    runner.backend.session_last_activity = lambda sid: messages.get(sid)

    runner._service_native_session_switch()

    assert runner.state.backend_session_id == "new-session"
    assert runner.commits == ["new-session"]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_stale_sibling_conversation_does_not_trigger_the_offer(tmp_path, backend_name):
    # An older conversation in the same directory must never pull tracking off the live one,
    # and certainly must not prompt to restart the backend for it.
    runner = _runner(
        tmp_path,
        backend_name,
        refs=[
            SessionRef(id="old-session", updated=500.0, label="live"),
            SessionRef(id="stale", updated=100.0, label="stale"),
        ],
        worktree=True,
        answer=YES,
    )

    runner._service_native_session_switch()

    assert runner.popups == []
    assert runner.state.backend_session_id == "old-session"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_backing_out_of_the_name_prompt_says_what_happened_and_renames_nothing(tmp_path, backend_name):
    # Esc (or quitting) at the name prompt used to be silent: nothing moved, and the user was
    # left to work out why. The conversation stays — and keeps running in this session, so it is
    # still linked to it — but the SESSION is not renamed, and going back to its earlier
    # conversation no longer demands a new name (see test_worktree_conversation_ownership).
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=YES)
    runner._name_for_switched_conversation = lambda sid: None  # backed out
    messages: list[str] = []
    runner._set_message = lambda text, **k: messages.append(text)

    runner._service_native_session_switch()

    assert runner.relocations == []
    assert runner.name == "alpha"  # the session keeps its own name
    assert runner.state.backend_session_id == "new-session"  # still tracking what the backend runs
    assert any("Not moved" in text and "alpha" in text for text in messages)


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_deciding_to_keep_it_here_does_link_the_new_conversation_to_this_session(tmp_path, backend_name):
    # The counterpart: "No — keep it here" IS a decision, so the conversation joins this session.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=NO)
    named: list[str | None] = []
    runner._persist_session_name = lambda sid: named.append(sid)

    runner._service_native_session_switch()

    assert named == ["new-session"]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_the_naming_dialog_does_not_ask_for_a_name_twice(tmp_path, backend_name):
    # The popup shows a title AND a field label; both saying "Name for the new session…" read as
    # the same sentence twice, in slightly different words.
    runner = _runner(tmp_path, backend_name, refs=SWITCHED, worktree=True, answer=YES)
    seen: list[tuple[str, str]] = []

    def fake_prompt(title, prompt, *, default="", detail=None):
        seen.append((title, prompt))
        return "gamma"

    runner._prompt_popup = fake_prompt
    runner._session_name_taken = lambda name: False
    runner._live_session_name_taken = lambda name: False
    del runner._name_for_switched_conversation  # exercise the real naming flow

    runner._service_native_session_switch()

    assert len(seen) == 1
    title, prompt = seen[0]
    assert "name" in (title + prompt).lower()  # it does ask
    assert not (title.lower().startswith("name for") and prompt.lower().startswith("name for"))
    assert prompt.lower().count("name") == 1
