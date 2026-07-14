"""Focused unit tests for agitrack.proxy.CommitEngine (#29, P4).

These tests construct CommitEngine directly — no ProxyRunner.__new__ required —
verifying the core commit pipeline, parse-result consumption, parse-worker
launch, and the simple state helpers.
"""

from __future__ import annotations

import threading
import types


from agitrack.backends.base import TokenUsage
from agitrack.transcripts.opencode import SessionTurn
from agitrack.proxy.commit_engine import CommitEngine
from agitrack.proxy.session import Session
from agitrack.transcripts import ExportedSession
from agitrack.config import AgitrackState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Repo:
    """Minimal GitRepo stand-in."""

    def __init__(self, *, staged: bool = True):
        self._staged = staged
        self.message: str | None = None
        self.staged_paths: list[str] = []
        self.untracked: list[str] = []

    def add_tracked(self) -> None:
        pass

    def has_staged_changes(self) -> bool:
        return self._staged

    def commit(self, message: str) -> str:
        self.message = message
        return "dead1234"

    def untracked_files(self) -> list[str]:
        return list(self.untracked)

    def stage_paths(self, paths: list[str]) -> None:
        self.staged_paths.extend(paths)


def _noop_stage(repo, state) -> None:
    """No-op stage_untracked_fn for tests that don't care about untracked."""


def _engine(tmp_path, *, staged: bool = True) -> tuple[CommitEngine, _Repo, AgitrackState]:
    repo = _Repo(staged=staged)
    state = AgitrackState(tmp_path)
    engine = CommitEngine(repo, state)
    return engine, repo, state


def _turn(
    prompt: str,
    response: str,
    *,
    total: int = 1,
    output: int = 1,
    reasoning_effort: str | None = None,
    assistant_id: str = "aid",
) -> SessionTurn:
    return SessionTurn(
        "uid",
        assistant_id,
        prompt,
        response,
        TokenUsage(total=total, output=output, input=total - output),
        None,
        complete=True,
        reasoning_effort=reasoning_effort,
    )


def test_commit_turns_records_latest_reasoning_effort(tmp_path):
    engine, repo, state = _engine(tmp_path)
    engine.commit_turns(
        # The most recent turn that recorded a level wins; a later None doesn't erase it.
        turns=[
            _turn("a", "done", reasoning_effort="on"),
            _turn("b", "done", reasoning_effort="high"),
            _turn("c", "done"),
        ],
        backend="opencode",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    assert "reasoning_effort: high" in repo.message


def test_commit_turns_omits_reasoning_effort_when_no_turn_records_it(tmp_path):
    engine, repo, state = _engine(tmp_path)
    engine.commit_turns(
        turns=[_turn("a", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    assert "reasoning_effort:" not in repo.message


def test_commit_turns_records_only_final_agent_message_by_default(tmp_path):
    engine, repo, state = _engine(tmp_path)
    turn = _turn("do it", "Done.")
    turn.agent_messages = ["On it.", "Done."]
    engine.commit_turns(
        turns=[turn],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    # Default: only the final reply lands in the trace.
    assert "Done." in repo.message
    assert "On it." not in repo.message


def test_commit_turns_records_all_agent_messages_when_option_on(tmp_path):
    engine, repo, state = _engine(tmp_path)
    state.full_agent_messages = True
    turn = _turn("do it", "Done.")
    turn.agent_messages = ["On it.", "Done."]
    engine.commit_turns(
        turns=[turn],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    # Every user-facing message appears, each as its own "## Agent" block.
    assert "On it." in repo.message
    assert "Done." in repo.message
    assert repo.message.count("## Agent") == 2


def test_commit_turns_full_agent_messages_override_forces_on(tmp_path):
    # The per-run override (e.g. --full-agent-messages) forces all messages even
    # when the per-repo config is off.
    repo = _Repo(staged=True)
    state = AgitrackState(tmp_path)
    assert state.full_agent_messages is False
    engine = CommitEngine(repo, state, full_agent_messages=True)
    turn = _turn("do it", "Done.")
    turn.agent_messages = ["On it.", "Done."]
    engine.commit_turns(
        turns=[turn],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    assert "On it." in repo.message
    assert repo.message.count("## Agent") == 2


def test_commit_turns_full_agent_messages_override_none_defers_to_config(tmp_path):
    # With no override (None), the per-repo config decides — here it's on.
    repo = _Repo(staged=True)
    state = AgitrackState(tmp_path)
    state.full_agent_messages = True
    engine = CommitEngine(repo, state, full_agent_messages=None)
    turn = _turn("do it", "Done.")
    turn.agent_messages = ["On it.", "Done."]
    engine.commit_turns(
        turns=[turn],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    assert "On it." in repo.message


def test_commit_turns_records_conversation_anchor_of_last_turn(tmp_path):
    engine, repo, state = _engine(tmp_path)
    engine.commit_turns(
        # The anchor links to the last covered turn's backend message id.
        turns=[
            _turn("a", "done", assistant_id="msg-1"),
            _turn("b", "done", assistant_id="msg-2"),
        ],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    assert "conversation_anchor: msg-2" in repo.message


def test_commit_turns_omits_conversation_anchor_when_no_message_id(tmp_path):
    engine, repo, state = _engine(tmp_path)
    engine.commit_turns(
        turns=[_turn("a", "done", assistant_id="")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert repo.message is not None
    assert "conversation_anchor:" not in repo.message


# ---------------------------------------------------------------------------
# CommitEngine.commit_turns — proxy mode (accumulate_trace_only_on_commit=False)
# ---------------------------------------------------------------------------


def test_commit_turns_surfaces_compactions_and_clears_origin_event(tmp_path):
    # The commit message records the compaction count (summed across the committed
    # turns) and the session's fork/copy origin; the origin event is one-shot, cleared
    # once surfaced so later commits don't keep re-announcing the lineage.
    engine, repo, state = _engine(tmp_path)
    state.set_session_origin_event(kind="fork", source="ses_parent", source_name="main")
    turns = [_turn("first", "a"), _turn("second", "b")]
    turns[0].compaction_count = 1
    turns[1].compaction_count = 2

    assert (
        engine.commit_turns(
            turns=turns,
            backend="claude",
            backend_session_id="s1",
            model="m",
            stage_untracked_fn=_noop_stage,
        )
        is True
    )
    assert "context_compactions: 3" in repo.message
    assert "forked_from: ses_parent (main)" in repo.message
    assert "forked from 'main'" in repo.message  # the trace note
    # One-shot: the event is gone after the commit that surfaced it.
    assert state.session_origin_event() is None


def test_commit_turns_returns_false_for_empty_turns(tmp_path):
    engine, repo, state = _engine(tmp_path)
    assert (
        engine.commit_turns(
            turns=[],
            backend="claude",
            backend_session_id="s1",
            model="m",
            stage_untracked_fn=_noop_stage,
        )
        is False
    )
    assert repo.message is None


def test_commit_turns_returns_false_when_nothing_staged(tmp_path):
    engine, repo, state = _engine(tmp_path, staged=False)
    assert (
        engine.commit_turns(
            turns=[_turn("fix it", "done")],
            backend="claude",
            backend_session_id="s1",
            model="m",
            stage_untracked_fn=_noop_stage,
        )
        is False
    )


def test_commit_turns_creates_commit_when_staged(tmp_path):
    engine, repo, state = _engine(tmp_path)
    assert (
        engine.commit_turns(
            turns=[_turn("fix the bug", "done")],
            backend="claude",
            backend_session_id="s1",
            model="m",
            stage_untracked_fn=_noop_stage,
        )
        is True
    )
    assert repo.message is not None
    assert repo.message.startswith("<aGiTrack> fix the bug")


def test_commit_turns_token_not_counted_on_failed_attempt(tmp_path):
    engine, repo, state = _engine(tmp_path, staged=False)
    engine.commit_turns(
        turns=[_turn("fix it", "done", total=100, output=10)],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert state.pending_token_usage()["input"] == 0


def test_commit_turns_calls_pre_commit_fn(tmp_path):
    engine, repo, state = _engine(tmp_path)
    called = []
    engine.commit_turns(
        turns=[_turn("do it", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
        pre_commit_fn=lambda: called.append(True),
    )
    assert called == [True]


def test_commit_turns_calls_on_commit_fn_with_sha_and_trace(tmp_path):
    engine, repo, state = _engine(tmp_path)
    received = []
    engine.commit_turns(
        turns=[_turn("do it", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
        on_commit_fn=lambda sha, trace, is_cover: received.append((sha, trace, is_cover)),
    )
    assert len(received) == 1
    sha, trace, is_cover = received[0]
    assert sha == "dead1234"
    assert is_cover is False  # a plain commit (no backend commits to cover)
    # The trace handed to the callback is the real, rebuilt interaction trace
    # (the summarizer's input), not an empty/stale one.
    assert "## User" in trace and "do it" in trace
    assert "## Agent" in trace and "done" in trace


def test_commit_turns_subject_joins_multiple_prompts(tmp_path):
    engine, repo, state = _engine(tmp_path)
    engine.commit_turns(
        turns=[
            _turn("add parser", "done1"),
            _turn("add tests", "done2"),
        ],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    subject = repo.message.splitlines()[0]
    assert subject == "<aGiTrack> add parser / add tests"


def test_commit_turns_pending_trace_cleared_after_commit(tmp_path):
    engine, repo, state = _engine(tmp_path)
    state.append_trace("user", "prior prompt")
    engine.commit_turns(
        turns=[_turn("new prompt", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert state.pending_trace() == []


def test_commit_turns_preserves_pending_users_not_in_turns(tmp_path):
    # A user prompt recorded in state that never appears as a turn's user_prompt
    # (e.g. from an earlier incomplete parse) is carried into the commit.
    engine, repo, state = _engine(tmp_path)
    state.append_trace("user", "initial orphan prompt")
    engine.commit_turns(
        turns=[_turn("second prompt", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    assert "initial orphan prompt" in repo.message
    assert "second prompt" in repo.message


def test_garbled_and_empty_leftovers_do_not_pollute_the_trace(tmp_path):
    # The proxy reconstructs raw typed bytes, so a follow-up typed while the agent was busy can be
    # captured garbled ("ontinue" for "Continue") or empty. Such captures used to leak into the
    # trace as spurious extra ## User headings (the empty one rendered as a bare "## User").
    engine, repo, state = _engine(tmp_path)
    state.append_trace("user", "ontinue")  # a dropped-leading-char capture of "Continue"
    state.append_trace("user", "   ")  # a blank capture
    engine.commit_turns(
        turns=[_turn("Continue", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    trace = repo.message.split("# Interaction Trace", 1)[1].split("# aGiTrack Metadata", 1)[0]
    # Exactly one ## User (the clean "Continue") — the garbled and empty captures are dropped,
    # not rendered as extra (or bare) headings.
    assert trace.count("## User") == 1 and "Continue" in trace


def test_leftover_user_message_precedes_multi_message_agent_response(tmp_path):
    # A message the user sent mid-turn (a pending "leftover" prompt) must appear BEFORE the
    # agent's response in the trace — even when the turn emitted several agent messages. The
    # agent's reply covers everything the user said, so the leftover belongs right after the
    # turn's prompt, not wedged between (or after) the agent's messages. The old logic
    # inserted before the LAST agent message, which (with full_agent_messages on) dropped
    # the leftover after the agent's earlier replies — the Claude trace-ordering bug.
    engine, repo, state = _engine(tmp_path)
    engine._full_agent_messages = True  # each agent message becomes its own ## Agent block
    state.append_trace("user", "wait, also handle the edge case")  # typed while agent worked
    turn = _turn("add the feature", "all done")
    turn.agent_messages = ["starting now", "still working", "all done"]
    engine.commit_turns(
        turns=[turn],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    msg = repo.message
    assert msg is not None
    # rindex skips the leftover's appearance in the subject line and finds its ## User block
    # in the trace body — which must precede every ## Agent block of the turn.
    leftover_pos = msg.rindex("wait, also handle the edge case")
    assert leftover_pos < msg.index("starting now")
    assert msg.index("starting now") < msg.index("still working") < msg.index("all done")


def test_queued_followups_render_as_separate_user_headings_without_duplication(tmp_path):
    # A message the user QUEUES mid-turn belongs to the turn but is a DISTINCT message: it gets its
    # OWN ## User heading (sent after the agent already responded), not merged into the base prompt.
    # The submit-time capture still records the base separately — it must dedup against the turn's
    # base (not re-added), and tokens are counted once per TURN regardless of the trace text.
    engine, repo, state = _engine(tmp_path)
    base = "Please fix the parser and make sure the tests pass"
    state.append_trace("user", base)  # submit-time capture of the base prompt
    turn = _turn(base, "done", total=100, output=40)
    turn.queued_followups = ["Also add a status command.", "Also verify the token counts are correct."]
    engine.commit_turns(
        turns=[turn],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
    )
    msg = repo.message
    assert msg is not None
    body = msg.split("# Interaction Trace", 1)[1]  # ignore the subject line
    # Three DISTINCT ## User headings: the base + the two queued follow-ups. The base is NOT
    # duplicated by the submit-time leftover.
    assert body.count("## User") == 3
    assert "Also add a status command." in body and "Also verify the token counts are correct." in body
    # Tokens reflect the ONE turn, not doubled by the extra trace entries.
    assert "tokens_since_last_commit_output: 40" in msg
    assert "tokens_since_last_commit_output: 80" not in msg


def test_commit_turns_stage_untracked_fn_receives_repo_and_state(tmp_path):
    engine, repo, state = _engine(tmp_path)
    calls = []

    def capture_stage(r, s):
        calls.append((r, s))

    engine.commit_turns(
        turns=[_turn("do it", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=capture_stage,
    )
    assert calls == [(repo, state)]


def test_commit_turns_auto_stages_untracked_excluding_declined(tmp_path):
    # When stage_untracked_fn does the auto-staging (non-interactive / proxy
    # exit), it must skip files the user intentionally declined.
    engine, repo, state = _engine(tmp_path)
    repo.untracked = ["new_file.py", "ignored.log"]
    state.add_declined(["ignored.log"])

    def stage_untracked_fn(r, s):
        declined = set(s.declined_untracked())
        r.stage_paths([p for p in r.untracked_files() if p not in declined])

    engine.commit_turns(
        turns=[_turn("add file", "done")],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=stage_untracked_fn,
    )
    assert "new_file.py" in repo.staged_paths
    assert "ignored.log" not in repo.staged_paths


# ---------------------------------------------------------------------------
# CommitEngine.commit_turns — actions mode (accumulate_trace_only_on_commit=True)
# ---------------------------------------------------------------------------


def test_commit_turns_actions_mode_no_trace_on_failed_attempt(tmp_path):
    # Bug #14 (d041d10): a failed attempt must leave state pristine.
    engine, repo, state = _engine(tmp_path, staged=False)
    engine.commit_turns(
        turns=[_turn("fix it", "done", total=140, output=10)],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
        accumulate_trace_only_on_commit=True,
    )
    assert state.pending_trace() == []
    assert state.pending_token_usage()["input"] == 0


def test_commit_turns_actions_mode_succeeds_with_clean_state(tmp_path):
    engine, repo, state = _engine(tmp_path)
    result = engine.commit_turns(
        turns=[_turn("fix it", "done", total=50, output=10)],
        backend="claude",
        backend_session_id="s1",
        model="m",
        stage_untracked_fn=_noop_stage,
        accumulate_trace_only_on_commit=True,
    )
    assert result is True
    assert repo.message is not None
    assert state.pending_trace() == []


# ---------------------------------------------------------------------------
# CommitEngine.record_user_prompt / await_followup
# ---------------------------------------------------------------------------


def test_record_user_prompt_appends_to_trace(tmp_path):
    engine, _, state = _engine(tmp_path)
    engine.record_user_prompt("hello world")
    trace = state.pending_trace()
    assert len(trace) == 1
    assert trace[0]["role"] == "user"
    assert trace[0]["content"] == "hello world"


def test_record_user_prompt_noop_on_empty(tmp_path):
    engine, _, state = _engine(tmp_path)
    engine.record_user_prompt("")
    assert state.pending_trace() == []


def test_record_user_prompt_skips_slash_commands(tmp_path):
    # A bare slash command (e.g. the user typing /compact in the TUI) is a backend
    # directive, not a prompt. It must not be recorded into the trace, or it surfaces
    # in the commit as a stray '## User /comp' entry — redundant with the compaction
    # lead-in note the trace already carries.
    engine, _, state = _engine(tmp_path)
    for command in ("/compact", "/comp", "  /model opus  ", "/clear"):
        engine.record_user_prompt(command)
    assert state.pending_trace() == []  # none recorded
    # A real prompt that merely mentions a slash mid-sentence is still recorded.
    engine.record_user_prompt("run /compact after you finish")
    assert [item["content"] for item in state.pending_trace()] == ["run /compact after you finish"]


def test_await_followup_appends_normalized(tmp_path):
    engine, _, _ = _engine(tmp_path)
    result = engine.await_followup("  fix   the bug  ", [])
    assert result == ["fix the bug"]


def test_await_followup_skips_slash_commands(tmp_path):
    engine, _, _ = _engine(tmp_path)
    result = engine.await_followup("/compact", [])
    assert result == []


def test_await_followup_skips_empty(tmp_path):
    engine, _, _ = _engine(tmp_path)
    assert engine.await_followup("", []) == []


# ---------------------------------------------------------------------------
# CommitEngine.sanitize_state_trace
# ---------------------------------------------------------------------------


def test_sanitize_state_trace_removes_event_blobs(tmp_path):
    engine, _, state = _engine(tmp_path)
    blob = '{"type":"event","payload":"..."}'

    class FakeBackend:
        def is_event_blob(self, content):
            return content.startswith('{"type":"event"')

    state.append_trace("user", "my prompt")
    state.append_trace("agent", blob)
    state.append_trace("agent", "a normal response")

    engine.sanitize_state_trace(FakeBackend())

    items = state.pending_trace()
    assert len(items) == 2
    assert all(item["content"] != blob for item in items)


def test_sanitize_state_trace_noop_when_clean(tmp_path):
    engine, _, state = _engine(tmp_path)

    class FakeBackend:
        def is_event_blob(self, content):
            return False

    state.append_trace("user", "hello")
    state.append_trace("agent", "world")
    engine.sanitize_state_trace(FakeBackend())
    assert len(state.pending_trace()) == 2


# ---------------------------------------------------------------------------
# CommitEngine.start_parse / parse worker
# ---------------------------------------------------------------------------


def test_start_parse_sets_parse_thread_on_session(tmp_path):
    state = AgitrackState(tmp_path)
    session = Session.bare()
    session.state = state
    session.worktree = None
    session.backend = types.SimpleNamespace(
        name="claude",
        latest_session_id=lambda repo: None,
        export_session=lambda repo, sid: None,
    )
    session.repo = types.SimpleNamespace(repo=tmp_path)

    engine = CommitEngine(None, state)
    started = engine.start_parse(
        session=session,
        discover_session_id_fn=lambda: None,
        debug_fn=lambda *a, **k: None,
    )
    assert started is True
    assert session.agent_parse_thread is not None
    session.agent_parse_thread.join(timeout=5)


def test_start_parse_returns_false_when_already_running(tmp_path):
    state = AgitrackState(tmp_path)
    session = Session.bare()
    session.state = state
    session.worktree = None

    ready = threading.Event()

    class SlowBackend:
        name = "claude"

        def latest_session_id(self, repo):
            return None

        def export_session(self, repo, sid):
            ready.wait(timeout=5)
            return None

    session.backend = SlowBackend()
    session.repo = types.SimpleNamespace(repo=tmp_path)

    engine = CommitEngine(None, state)
    assert (
        engine.start_parse(
            session=session,
            discover_session_id_fn=lambda: None,
            debug_fn=lambda *a, **k: None,
        )
        is True
    )
    # Second launch while thread is alive
    assert (
        engine.start_parse(
            session=session,
            discover_session_id_fn=lambda: None,
            debug_fn=lambda *a, **k: None,
        )
        is False
    )
    ready.set()
    session.agent_parse_thread.join(timeout=5)


def test_start_parse_writes_result_to_owning_session(tmp_path):
    state = AgitrackState(tmp_path)
    session = Session.bare()
    session.state = state
    # Use a worktree so the worker calls backend.latest_session_id to find the id.
    session.worktree = object()

    exported = ExportedSession("ses-1", "m", None, [])

    class Backend:
        name = "claude"

        def latest_session_id(self, repo):
            return "ses-1"

        def export_session(self, repo, sid):
            return exported

    session.backend = Backend()
    session.repo = types.SimpleNamespace(repo=tmp_path)

    engine = CommitEngine(None, state)
    engine.start_parse(
        session=session,
        discover_session_id_fn=lambda: None,
        debug_fn=lambda *a, **k: None,
    )
    session.agent_parse_thread.join(timeout=5)
    assert session.agent_parse_result is not None
    sid, exp, last_id, owner_state = session.agent_parse_result
    assert sid == "ses-1"
    assert exp is exported
    assert owner_state is state


def _noworktree_session(tmp_path, state, *, export_sink):
    session = Session.bare()
    session.state = state
    session.worktree = None

    class Backend:
        name = "claude"

        def latest_session_id(self, repo):
            return None  # unused in no-worktree mode

        def export_session(self, repo, sid):
            export_sink["sid"] = sid
            return ExportedSession(sid, "m", None, [])

    session.backend = Backend()
    session.repo = types.SimpleNamespace(repo=tmp_path)
    return session


def test_start_parse_no_worktree_follows_switched_session(tmp_path):
    # No-worktree mode prefers snapshot-based discovery (a session that appeared AFTER
    # launch — a Claude /resume or a new conversation started inside the backend) over the
    # pinned id, so all modes follow an in-backend session switch.
    state = AgitrackState(tmp_path)
    state.backend_session_id = "pinned"
    seen: dict = {}
    session = _noworktree_session(tmp_path, state, export_sink=seen)

    engine = CommitEngine(None, state)
    engine.start_parse(session=session, discover_session_id_fn=lambda: "switched", debug_fn=lambda *a, **k: None)
    session.agent_parse_thread.join(timeout=5)

    assert seen["sid"] == "switched"  # followed the switch, not the pinned id
    assert session.agent_parse_result[0] == "switched"


def test_start_parse_no_worktree_falls_back_to_pinned_when_no_switch(tmp_path):
    # When discovery finds no post-launch switch (returns None) the worker keeps the pinned
    # id — so a normal continuation is unaffected.
    state = AgitrackState(tmp_path)
    state.backend_session_id = "pinned"
    seen: dict = {}
    session = _noworktree_session(tmp_path, state, export_sink=seen)

    engine = CommitEngine(None, state)
    engine.start_parse(session=session, discover_session_id_fn=lambda: None, debug_fn=lambda *a, **k: None)
    session.agent_parse_thread.join(timeout=5)

    assert seen["sid"] == "pinned"


def test_start_parse_reads_per_conversation_watermark(tmp_path):
    # The parse worker reads the watermark for the conversation actually being exported —
    # so after a switch it uses that conversation's own committed mark, not the last one.
    state = AgitrackState(tmp_path)
    state.backend_session_id = "A"
    state.set_backend_message_id("A", "a-hi")  # A's committed high-water mark
    state.data["backend_message_ids"] = {"A": "a-hi", "B": "b-hi"}  # B was tracked before
    seen: dict = {}
    session = _noworktree_session(tmp_path, state, export_sink=seen)

    engine = CommitEngine(None, state)
    engine.start_parse(session=session, discover_session_id_fn=lambda: "B", debug_fn=lambda *a, **k: None)
    session.agent_parse_thread.join(timeout=5)

    sid, _exp, last_id, _owner = session.agent_parse_result
    assert sid == "B"
    assert last_id == "b-hi"  # B's own watermark, not A's (a-hi)


# ---------------------------------------------------------------------------
# CommitEngine.finish_parse_if_ready
# ---------------------------------------------------------------------------


def _make_finish_helpers(tmp_path, session, exported_session, *, last_message_id=None):
    """Set up a CommitEngine and helper stubs for finish_parse_if_ready tests."""
    state = AgitrackState(tmp_path)
    session.state = state
    session.backend = types.SimpleNamespace(name="claude")
    session.agent_parse_result = (exported_session.session_id, exported_session, last_message_id, state)
    session.agent_parse_thread = None

    repo = _Repo(staged=True)
    engine = CommitEngine(repo, state)

    commits = []

    def commit_fn(**kwargs):
        commits.append(kwargs)
        return True

    return engine, state, commits, commit_fn


def test_finish_parse_returns_none_when_no_result(tmp_path):
    state = AgitrackState(tmp_path)
    session = Session.bare()
    session.state = state
    session.agent_parse_thread = None
    session.agent_parse_result = None

    engine = CommitEngine(None, state)
    result, awaited = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=lambda **k: True,
    )
    assert result is None


def test_finish_parse_defers_incomplete_turn(tmp_path):
    session = Session.bare()
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        [
            SessionTurn("u1", "a1", "do it", "partial", TokenUsage(total=1, output=1), None, complete=False),
        ],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: True,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is None
    assert commits == []


def test_finish_parse_commits_complete_turn(tmp_path):
    session = Session.bare()
    # SessionTurn(user_message_id, assistant_message_id, user_prompt, final_response,
    #             tokens, model, complete)
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        [
            SessionTurn("u1", "msg-1", "fix it", "done", TokenUsage(total=1, output=1), None, complete=True),
        ],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True
    assert len(commits) == 1
    # Watermark advanced to the assistant_message_id of the last complete turn
    assert state.last_backend_message_id == "msg-1"


def test_finish_parse_defers_awaited_followup_while_active(tmp_path):
    session = Session.bare()
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        [
            SessionTurn("u1", "a1", "prompt one", "done", TokenUsage(total=1, output=1), None, complete=True),
        ],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, new_awaited = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=["queued follow-up"],
        agent_is_active_fn=lambda: True,  # agent still running
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is None
    assert commits == []
    assert "queued follow-up" in new_awaited


def test_finish_parse_clears_awaited_on_interrupt(tmp_path):
    # An Esc interrupt clears the awaited list so commits are not deferred forever.
    session = Session.bare()
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        [
            SessionTurn(
                "u1", "a1", "fix", "partial", TokenUsage(total=1, output=1), None, complete=True, interrupted=True
            ),
        ],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, new_awaited = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=["a prompt that was discarded"],
        agent_is_active_fn=lambda: True,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True
    assert new_awaited == []


def _cancelled_exported():
    # A turn the user interrupted before any committable response: interrupted,
    # empty final_response (so it is not in complete_turns).
    return ExportedSession(
        "ses-1",
        "m",
        None,
        [
            SessionTurn("u1", "a1", "build it", "", TokenUsage(total=1, output=1), None, interrupted=True),
        ],
    )


def test_finish_parse_invokes_cancel_handler_and_advances_watermark(tmp_path):
    # A cancelled turn with no committable response routes to on_cancelled_fn; when
    # it reports it handled the leftover changes, the watermark advances past the
    # turn so it isn't reconsidered, and no normal commit happens.
    session = Session.bare()
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, _cancelled_exported())
    seen = []

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
        on_cancelled_fn=lambda turns: seen.append(turns) or True,
    )
    assert result is False
    assert commits == []  # no normal commit for a response-less turn
    assert len(seen) == 1
    assert state.last_backend_message_id == "a1"


def test_finish_parse_cancel_handler_keep_does_not_advance_watermark(tmp_path):
    # If the handler declines (user kept the changes to decide later), the watermark
    # is left untouched so the turn is still the current tail.
    session = Session.bare()
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, _cancelled_exported())

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
        on_cancelled_fn=lambda turns: False,
    )
    assert result is False
    assert state.last_backend_message_id is None


def test_finish_parse_commits_finished_turn_with_no_final_text_response(tmp_path):
    # A turn that FINISHED (complete, not interrupted) but emitted no final TEXT response —
    # e.g. its last action was a file edit and the agent stopped without a closing message —
    # must still be committed if it changed files. Dropping it would leave the work uncommitted
    # forever (the live loop re-parses and the exit finalize hits the same gate). It is NOT a
    # cancellation, so the cancel handler is never consulted.
    session = Session.bare()
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        [SessionTurn("u1", "a1", "build it", "", TokenUsage(total=1, output=1), None, complete=True)],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)
    called = []

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
        on_cancelled_fn=lambda turns: called.append(turns) or True,
    )
    assert result is True  # the edits are committed, not dropped
    assert len(commits) == 1
    assert called == []  # not a cancellation — the handler is never called
    # Watermark advances past the turn (its assistant id) so it isn't re-committed.
    assert state.last_backend_message_id == "a1"


def test_finish_parse_no_text_turn_watermark_falls_back_to_user_id(tmp_path):
    # A no-text finished turn never recorded an assistant id (that is only set from a text
    # message). The watermark must still advance — off the user id — so the turn isn't
    # reconsidered and re-committed on the next parse.
    session = Session.bare()
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        [SessionTurn("u1", "", "build it", "", TokenUsage(total=1, output=1), None, complete=True)],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True
    assert state.last_backend_message_id == "u1"


def test_finish_parse_defers_a_genuinely_mid_flight_turn(tmp_path):
    # The mid-flight guard is upstream: a turn still in progress (complete=False) with
    # require_complete=True is DEFERRED (None), never reaching the no-response commit path.
    session = Session.bare()
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        [SessionTurn("u1", "a1", "build it", "", TokenUsage(total=1, output=1), None, complete=False)],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is None  # deferred, not committed
    assert commits == []


def test_finish_parse_exit_commits_dangling_no_text_turn(tmp_path):
    # The exit / sync finalize path (require_complete=False) must commit a finished-but-dangling
    # no-text turn's edits — this is exactly the "commit not made even on exit" case.
    session = Session.bare()
    exported = ExportedSession(
        "ses-1",
        "m",
        None,
        # complete=False (ended on a tool_use) but the process is gone, so on exit we commit it.
        [SessionTurn("u1", "a1", "apply the change", "", TokenUsage(total=1, output=1), None, complete=False)],
    )
    engine, state, commits, commit_fn = _make_finish_helpers(tmp_path, session, exported)

    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=False,  # exit / sync finalize
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=commit_fn,
    )
    assert result is True
    assert len(commits) == 1
    assert state.last_backend_message_id == "a1"


def test_finish_parse_discards_stale_session_result(tmp_path):
    # A result whose owner_state differs from self.state is silently discarded.
    state_a = AgitrackState(tmp_path / "a")
    state_b = AgitrackState(tmp_path / "b")

    session = Session.bare()
    session.state = state_a
    # Result is tagged with state_b (a stale worker from before a session switch)
    session.agent_parse_result = ("ses-b", None, None, state_b)
    session.agent_parse_thread = None

    engine = CommitEngine(None, state_a)
    result, _ = engine.finish_parse_if_ready(
        session=session,
        quiet=True,
        prompt_untracked=False,
        require_complete=True,
        awaited_followups=[],
        agent_is_active_fn=lambda: False,
        debug_fn=lambda *a, **k: None,
        note_session_change_fn=lambda sid: None,
        mirror_fn=lambda sid: None,
        commit_fn=lambda **k: True,
    )
    assert result is None  # discarded, not committed


# ---------------------------------------------------------------------------
# CommitEngine.recover_nonempty_session / initialize_session_baseline
# ---------------------------------------------------------------------------


def test_recover_nonempty_session_returns_none_on_exception(tmp_path):
    state = AgitrackState(tmp_path)
    engine = CommitEngine(None, state)

    class FailBackend:
        def latest_session_id(self, repo):
            raise RuntimeError("network error")

    result = engine.recover_nonempty_session(
        FailBackend(),
        types.SimpleNamespace(repo=tmp_path),
        lambda sid: None,
    )
    assert result is None


def test_recover_nonempty_session_returns_none_when_same_id(tmp_path):
    state = AgitrackState(tmp_path)
    state.backend_session_id = "ses-1"
    engine = CommitEngine(None, state)

    class SameBackend:
        def latest_session_id(self, repo):
            return "ses-1"

    result = engine.recover_nonempty_session(
        SameBackend(),
        types.SimpleNamespace(repo=tmp_path),
        lambda sid: None,
    )
    assert result is None


def test_initialize_session_baseline_clears_when_not_continue(tmp_path):
    state = AgitrackState(tmp_path)
    state.backend_session_id = "old-ses"
    state.last_backend_message_id = "old-msg"
    engine = CommitEngine(None, state)

    engine.initialize_session_baseline(
        None,
        None,
        should_continue_fn=lambda: False,
        stage_backend_resume_fn=lambda sid: None,
    )
    assert state.backend_session_id is None
    assert state.last_backend_message_id is None
