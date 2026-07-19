from unittest.mock import Mock
from agitrack.summaries import Summarizer
from agitrack.backends.base import AgentResult, TokenUsage
from agitrack.transcripts.types import SessionTurn, ExportedSession

# A small interaction trace (the only input the summarizer is now given), shaped
# like the "## User"/"## Agent" body appended to an aGiTrack commit.
_TRACE = "## User\n\ndo the task\n\n## Agent\n\nDid the task and added tests."


def test_summarize_commit() -> None:
    backend = Mock()
    backend.run.return_value = AgentResult(
        backend="test",
        session_id=None,
        model="test-model",
        final_response="This is a commit summary.",
        exit_code=0,
        tokens=TokenUsage(),
    )
    summarizer = Summarizer(backend, model="test-model")
    summary = summarizer.summarize_commit(trace=_TRACE)
    assert summary == "This is a commit summary."
    backend.run.assert_called_once()
    call_args = backend.run.call_args
    assert call_args[1]["model"] == "test-model"
    assert call_args[1]["session_id"] is None
    # The summarizer reads only its instruction + trace: the backend is run "bare" (no
    # agent system prompt, tools, or project memory), so it isn't charged thousands of
    # input tokens the summary never uses.
    assert call_args[1]["bare"] is True
    # The instruction rides in the SYSTEM prompt (not the user message), so a small
    # model summarizes the trace instead of completing/echoing an instruction-shaped
    # user prompt (the ~75%-echo regression). The user message is just the trace.
    assert call_args[1]["system_prompt"].startswith("You are a technical summarizer")
    user_prompt = call_args[0][0]
    assert user_prompt.startswith("Interaction trace:")
    assert "you are a technical summarizer" not in user_prompt.lower()


def test_summarizer_input_includes_cache_creation_tokens() -> None:
    # Regression for the "summary input token is always 20" bug: a cache-served summary
    # reports a tiny `input_tokens` while the real input sits in cache-creation. Fresh
    # input must count input + cache_write (matching the main commit line), and cache
    # reads are tracked separately — not folded in (that would double-count).
    backend = Mock()
    backend.run.return_value = AgentResult(
        backend="test",
        session_id=None,
        model="test-model",
        final_response="A concise summary of the change.",
        exit_code=0,
        tokens=TokenUsage(input=20, output=300, cache_write=4500, cache_read=12000),
    )
    summarizer = Summarizer(backend, model="test-model")
    summarizer.summarize_commit(trace=_TRACE)
    assert summarizer.tokens_input == 4520  # 20 uncached + 4500 cache-creation, not 20
    assert summarizer.tokens_output == 300
    assert summarizer.tokens_cache_read == 12000  # separate, never added into input


def test_summarizer_token_counts_accumulate_across_calls() -> None:
    backend = Mock()
    backend.run.return_value = AgentResult(
        backend="test",
        session_id=None,
        model="m",
        final_response="ok summary",
        exit_code=0,
        tokens=TokenUsage(input=10, output=5, cache_write=100, cache_read=7),
    )
    summarizer = Summarizer(backend, model="m")
    summarizer.summarize_commit(trace=_TRACE)
    summarizer.update_session_summary(current_summary=None, trace=_TRACE, commit_summary="ok summary")
    assert summarizer.tokens_input == 220  # (10 + 100) * 2
    assert summarizer.tokens_output == 10
    assert summarizer.tokens_cache_read == 14


def test_commit_summary_prompt_is_self_contained() -> None:
    # A commit summary must describe ONLY this commit (its interaction trace). It
    # must not be seeded with any prior/rolling session summary — that contaminated the
    # commit message with earlier, unrelated work and made the model respond with
    # "the summary you provided is already complete".
    backend = Mock()
    backend.run.return_value = AgentResult(
        backend="test",
        session_id=None,
        model="test-model",
        final_response="Fixed the bug in the parser.",
        exit_code=0,
        tokens=TokenUsage(),
    )
    summarizer = Summarizer(backend, model="test-model")
    # The rolling summary that used to leak in — it must not reach the backend.
    summarizer.summarize_commit(trace="## User\n\nAdd a new feature\n\n## Agent\n\nDone.")
    prompt = backend.run.call_args[0][0]
    assert "Add a new feature" in prompt  # the trace's own content is present
    assert "Previous session" not in prompt
    assert "Current session context" not in prompt  # the removed injection label
    # summarize_commit no longer accepts a session_summary argument at all.
    import inspect

    assert "session_summary" not in inspect.signature(summarizer.summarize_commit).parameters


def test_update_session_summary() -> None:
    backend = Mock()
    backend.run.return_value = AgentResult(
        backend="test",
        session_id=None,
        model="test-model",
        final_response="Updated session summary.",
        exit_code=0,
        tokens=TokenUsage(),
    )
    summarizer = Summarizer(backend, model="test-model")
    summary = summarizer.update_session_summary(
        current_summary="Initial summary.",
        trace=_TRACE,
        commit_summary="Added tests for new feature.",
    )
    assert summary == "Updated session summary."
    call_args = backend.run.call_args
    prompt = call_args[0][0]
    assert "Initial summary." in prompt
    assert "Added tests for new feature." in prompt


def test_update_session_summary_initial() -> None:
    backend = Mock()
    backend.run.return_value = AgentResult(
        backend="test",
        session_id=None,
        model="test-model",
        final_response="Initial session summary.",
        exit_code=0,
        tokens=TokenUsage(),
    )
    summarizer = Summarizer(backend, model="test-model")
    summary = summarizer.update_session_summary(
        current_summary=None,
        trace=_TRACE,
        commit_summary="Started the project.",
    )
    assert summary == "Initial session summary."
    call_args = backend.run.call_args
    prompt = call_args[0][0]
    assert "No previous session summary exists" in prompt


def test_summarize_pre_compaction() -> None:
    backend = Mock()
    backend.run.return_value = AgentResult(
        backend="test",
        session_id=None,
        model="test-model",
        final_response="Comprehensive session summary before compaction.",
        exit_code=0,
        tokens=TokenUsage(),
    )
    summarizer = Summarizer(backend, model="test-model")
    exported = ExportedSession(
        session_id="test-session",
        model="test-model",
        updated=0,
        turns=[
            SessionTurn(
                user_message_id="1",
                assistant_message_id="2",
                user_prompt="First task",
                final_response="Done.",
                tokens=TokenUsage(),
                model="test-model",
                complete=True,
                interrupted=False,
            ),
            SessionTurn(
                user_message_id="3",
                assistant_message_id="4",
                user_prompt="Second task",
                final_response="Also done.",
                tokens=TokenUsage(),
                model="test-model",
                complete=True,
                interrupted=False,
            ),
        ],
    )
    summary = summarizer.summarize_pre_compaction(
        exported_session=exported,
        current_summary="Current summary.",
    )
    assert summary == "Comprehensive session summary before compaction."
    call_args = backend.run.call_args
    prompt = call_args[0][0]
    assert "First task" in prompt
    assert "Second task" in prompt
    assert "Current summary." in prompt


def test_every_summary_call_is_stateless() -> None:
    # No summary task may continue a previous backend session — every call must
    # pass session_id=None so the backend starts fresh (no cross-request leak).
    # This is backend-agnostic: it guards both Claude and OpenCode, which only
    # resume when handed a session id.
    backend = Mock()
    backend.run.return_value = _result("A summary.")
    summarizer = Summarizer(backend)
    exported = ExportedSession(session_id="s", model="m", updated=0, turns=[_turn()])

    summarizer.summarize_commit(trace=_TRACE)
    summarizer.update_session_summary(current_summary="prev", trace=_TRACE, commit_summary="c")
    summarizer.summarize_pre_compaction(exported_session=exported, current_summary="prev")

    assert backend.run.call_count == 3
    for call in backend.run.call_args_list:
        assert call.kwargs["session_id"] is None


# --- unsuccessful summaries are rejected, not used as commit subjects (#8) ----


def _result(text: str, exit_code: int = 0) -> AgentResult:
    return AgentResult(
        backend="test",
        session_id=None,
        model="test-model",
        final_response=text,
        exit_code=exit_code,
        tokens=TokenUsage(),
    )


def _turn() -> SessionTurn:
    return SessionTurn(
        user_message_id="1",
        assistant_message_id="2",
        user_prompt="Add a new feature",
        final_response="I've added the feature.",
        tokens=TokenUsage(),
        model="test-model",
        complete=True,
        interrupted=False,
    )


def test_summarizer_raises_on_session_limit_error_text() -> None:
    import pytest
    from agitrack.summaries import UnusableSummaryError

    backend = Mock()
    backend.run.return_value = _result("You've hit your session limit. Your limit will reset at 3pm.")
    summarizer = Summarizer(backend)
    with pytest.raises(UnusableSummaryError):
        summarizer.summarize_commit(trace=_TRACE)


def test_summarizer_raises_on_refusal_text() -> None:
    # Refusals observed verbatim as commit SUBJECTS in a real repo whose trace held only
    # background-task markers: the model explained it had nothing to summarize, and the
    # explanation sailed through the guards into the commit message. A summary that opens
    # in first person about its own inability is never usable.
    import pytest
    from agitrack.summaries import UnusableSummaryError

    refusals = [
        "I don't have any coding session turns or code diff to analyze. The trace shows only placeholders.",
        "I cannot provide a meaningful summary of code changes because no code diff was provided.",
        "I don't have sufficient information to write a summary.",
        "I cannot write a meaningful summary without access to the coding session turns.",
        "I am unable to summarize this session.",
        "I can't generate a summary from this interaction trace.",
    ]
    for refusal in refusals:
        backend = Mock()
        backend.run.return_value = _result(refusal)
        with pytest.raises(UnusableSummaryError):
            Summarizer(backend).summarize_commit(trace=_TRACE)


def test_summary_first_person_content_is_still_usable() -> None:
    # The refusal guard keys on the OPENING of the first line; a genuine summary that
    # mentions inability mid-sentence (or uses first person later) must still pass.
    from agitrack.summaries.summarizer import summary_is_usable

    assert summary_is_usable("Fixed the parser so it no longer says it cannot handle empty files.")
    assert summary_is_usable("Refactored retries; I don't have to special-case timeouts anymore.".capitalize())


def test_summarizer_raises_on_nonzero_exit_code() -> None:
    import pytest
    from agitrack.summaries import UnusableSummaryError

    backend = Mock()
    backend.run.return_value = _result("Looks like a fine summary.", exit_code=1)
    summarizer = Summarizer(backend)
    with pytest.raises(UnusableSummaryError):
        summarizer.summarize_commit(trace=_TRACE)


def test_summarizer_raises_on_empty_response() -> None:
    import pytest
    from agitrack.summaries import UnusableSummaryError

    backend = Mock()
    backend.run.return_value = _result("   \n  ")
    summarizer = Summarizer(backend)
    with pytest.raises(UnusableSummaryError):
        summarizer.summarize_commit(trace=_TRACE)


def test_summary_is_usable_detects_error_shapes() -> None:
    from agitrack.summaries import summary_is_usable

    for error in [
        "You've hit your session limit. It resets at 3pm.",
        "you have hit your usage limit",
        "Claude usage limit reached|1760000000",
        "API Error: 429 rate_limit_error",
        "Credit balance is too low",
        "Please run /login to authenticate",
        "Invalid API key. Please check your configuration.",
    ]:
        assert summary_is_usable(error) is False, error


def test_summary_is_usable_keeps_topical_mentions_of_limits_and_errors() -> None:
    from agitrack.summaries import summary_is_usable

    # Legitimate summaries that merely talk about limits/errors must pass.
    for summary in [
        "Add rate limiting to the API client",
        "Fix API error handling in the retry loop",
        "Enforce a session limit on concurrent worktrees",
        "Handle the usage limits page in the settings UI",
    ]:
        assert summary_is_usable(summary) is True, summary


def test_summary_is_usable_rejects_echoed_prompt() -> None:
    from agitrack.summaries import summary_is_usable
    from agitrack.summaries.prompts import COMMIT_SUMMARY_SYSTEM, PRE_COMPACTION_SYSTEM, SESSION_UPDATE_SYSTEM

    # The exact bug: the backend echoed its own system prompt, which then became
    # the commit subject/body. Every system prompt and the scaffolding must be
    # recognised as not-a-summary.
    assert summary_is_usable(COMMIT_SUMMARY_SYSTEM) is False
    assert summary_is_usable(SESSION_UPDATE_SYSTEM) is False
    assert summary_is_usable(PRE_COMPACTION_SYSTEM) is False
    assert summary_is_usable("Recent conversation turns:\nUser: hi\nAgent: hello") is False
    # A real summary that happens to mention conversations still passes.
    assert summary_is_usable("Refactored the conversation parser and added tests.") is True


def test_summarizer_raises_when_backend_echoes_the_prompt() -> None:
    import pytest
    from agitrack.summaries import UnusableSummaryError

    # Simulate the backend echoing the SYSTEM instruction it was given (the observed
    # failure mode with a small model) — the summarizer must reject it so the commit
    # keeps its prompt-led message rather than a prompt-dump.
    captured: dict[str, str] = {}

    def echo_run(prompt, *, model=None, session_id=None, bare=False, system_prompt=None, commit_guidance=True):
        captured["user"] = prompt
        captured["system"] = system_prompt
        return _result(system_prompt)  # echo the instruction back as the response

    backend = Mock()
    backend.run.side_effect = echo_run
    summarizer = Summarizer(backend)
    with pytest.raises(UnusableSummaryError):
        summarizer.summarize_commit(trace=_TRACE)
    # The instruction rides in the SYSTEM prompt; the user message is just the trace.
    assert captured["system"].startswith("You are a technical summarizer")
    assert captured["user"].startswith("Interaction trace:")
    assert "you are a technical summarizer" not in captured["user"].lower()


def test_looks_like_prompt_echo_is_marker_independent() -> None:
    from agitrack.summaries.summarizer import _looks_like_prompt_echo

    # The general (marker-independent) check: a response that restates the prompt
    # from the top is an echo even if no fixed marker matches.
    prompt = "Please produce a one-line summary of the following changes in plain prose."
    assert _looks_like_prompt_echo(prompt, prompt) is True
    assert _looks_like_prompt_echo(prompt, prompt + " extra trailing text") is True
    assert _looks_like_prompt_echo(prompt, "A concise, unrelated summary of the work.") is False
    # Short responses can't be confused with an echo of a long prompt.
    assert _looks_like_prompt_echo(prompt, "Done.") is False


def test_session_update_rejects_echoed_prompt() -> None:
    import pytest
    from agitrack.summaries import UnusableSummaryError

    # The session-update path goes through the same _run guard; echoing the received
    # USER message (the trace) must be rejected too (the marker-independent check).
    def echo_run(prompt, *, model=None, session_id=None, bare=False, system_prompt=None, commit_guidance=True):
        return _result(prompt)

    backend = Mock()
    backend.run.side_effect = echo_run
    summarizer = Summarizer(backend)
    with pytest.raises(UnusableSummaryError):
        summarizer.update_session_summary(current_summary=None, trace=_TRACE, commit_summary="x")


def test_strip_summary_preamble_removes_meta_lead_ins() -> None:
    from agitrack.summaries.summarizer import strip_summary_preamble

    # The exact observed failure: a meta-preamble before the real summary.
    bad = (
        "The summary has been written. No further action is needed from me here — "
        "the conversation turns and diff you provided were the input for "
        "summarization, and the summary follows below.\n\n"
        "Multiple committers are now first-class, filterable identities."
    )
    assert strip_summary_preamble(bad) == "Multiple committers are now first-class, filterable identities."

    # Other common preamble shapes the model emits despite the instruction.
    assert strip_summary_preamble("Here is the summary:\n\nAdded OpenCode sharing.") == "Added OpenCode sharing."
    assert (
        strip_summary_preamble("Here is a summary of the changes: Added OpenCode sharing.") == "Added OpenCode sharing."
    )
    assert strip_summary_preamble("Sure! Here is the summary.\n\nFixed the filter.") == "Fixed the filter."
    assert strip_summary_preamble("Below is the summary —\n\nRefactored resolution.") == "Refactored resolution."


def test_strip_summary_preamble_keeps_genuine_summaries() -> None:
    from agitrack.summaries.summarizer import strip_summary_preamble

    # Real topic sentences must never be mistaken for a preamble, even when they
    # contain words like "summary", "here", or "this".
    for text in [
        "Added a summary panel to the dashboard that shows token totals.",
        "The summarizer now strips meta-preambles before using the text.",
        "Here-document parsing was fixed in the shell backend.",
        "This refactor splits the parser into three modules.",
        "Multiple committers are now first-class, filterable identities.",
    ]:
        assert strip_summary_preamble(text) == text


def test_summarize_commit_strips_preamble_end_to_end() -> None:
    # The whole point: a preamble-led model response yields a clean topic-sentence
    # summary (which becomes the commit subject), not "The summary has been written".
    backend = Mock()
    backend.run.return_value = _result("Here is the summary:\n\nAdded the committer filter.")
    summary = Summarizer(backend).summarize_commit(trace=_TRACE)
    assert summary == "Added the committer filter."


def test_commit_prompt_is_only_the_trace_bounded_and_reminds_at_the_end() -> None:
    # The commit summary's sole input is the interaction trace (no diff). A huge
    # trace is capped, and the instruction is restated next to the generation cue
    # so the model stays in summarization mode.
    from agitrack.summaries.summarizer import _MAX_TRACE_CHARS

    backend = Mock()
    backend.run.return_value = _result("Bounded summary.")
    huge_trace = "## User\n\ndo it\n\n## Agent\n\n" + "X" * (_MAX_TRACE_CHARS * 3)
    Summarizer(backend).summarize_commit(trace=huge_trace)
    prompt = backend.run.call_args[0][0]

    assert "Interaction trace:" in prompt  # the trace is the input
    assert "Code changes (diff)" not in prompt  # the diff is NOT included
    assert "[truncated" in prompt  # the oversized trace was capped
    assert "X" * (_MAX_TRACE_CHARS + 1) not in prompt
    # The instruction is restated immediately before the generation cue.
    tail = prompt[-400:]
    assert "output only the summary" in tail and tail.rstrip().endswith("Summary:")


def test_turns_block_keeps_most_recent_within_budget() -> None:
    # Still used for pre-compaction (whole-session) summaries.
    from agitrack.summaries.summarizer import _turns_block

    def turn(tag: str) -> SessionTurn:
        return SessionTurn(
            user_message_id=tag,
            assistant_message_id=tag,
            user_prompt=f"prompt-{tag}",
            final_response="Y" * 5_000,
            tokens=TokenUsage(),
            model="m",
            complete=True,
            interrupted=False,
        )

    block = _turns_block([turn("old"), turn("mid"), turn("new")], budget=8_000)
    assert "prompt-new" in block  # most recent kept
    assert "prompt-old" not in block  # earliest dropped over budget
    assert "[earlier turns omitted]" in block
