import io
from pathlib import Path

from agitrack.backends.opencode import OpenCodeBackend


def test_opencode_parse_prefers_final_response():
    backend = OpenCodeBackend(Path("."))
    output = "\n".join(
        [
            '{"type":"message","content":"partial"}',
            '{"type":"thinking","content":"secret"}',
            '{"type":"final","content":"done","sessionID":"ses-1","model":"m"}',
        ]
    )

    final, session_id, model, _tokens = backend._read_events(io.StringIO(output))

    assert final == "done"
    assert session_id == "ses-1"
    assert model == "m"


def test_read_events_does_not_echo_to_stdout_when_streaming_off(capsys):
    # A bare (summarizer) run must be SILENT: aGiTrack's stdout is the host terminal, so echoing
    # the streamed agent text there leaks the summary next to the user's input box (the bug).
    backend = OpenCodeBackend(Path("."))
    output = "\n".join(
        [
            '{"type":"text","sessionID":"ses-1","part":{"type":"text","text":"working on it…"}}',
            '{"type":"text","sessionID":"ses-1","part":{"type":"text","text":"final summary","metadata":{"openai":{"phase":"final_answer"}}}}',
        ]
    )

    final, _sid, _model, _tokens = backend._read_events(io.StringIO(output), stream_console=False)

    assert final == "working on it…final summary"  # still parsed correctly
    assert capsys.readouterr().out == ""  # …but nothing printed to the terminal


def test_read_events_streams_to_stdout_for_foreground_runs(capsys):
    # Non-bare (shell) runs still stream progress to the console — that's the point there.
    backend = OpenCodeBackend(Path("."))
    output = '{"type":"text","sessionID":"ses-1","part":{"type":"text","text":"hello there"}}'

    backend._read_events(io.StringIO(output), stream_console=True)

    assert "hello there" in capsys.readouterr().out


def test_bare_run_reads_events_silently(monkeypatch, capsys):
    # End-to-end through run(bare=True): the summarizer path must not print the streamed text.
    import subprocess

    backend = OpenCodeBackend(Path("."))
    events = '{"type":"text","sessionID":"ses-1","part":{"type":"text","text":"a one line summary","metadata":{"openai":{"phase":"final_answer"}}}}\n'

    class _FakeProc:
        def __init__(self):
            self.stdout = io.StringIO(events)
            self.stdin = None

        def wait(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())
    result = backend.run("summarize this", model=None, session_id=None, bare=True, system_prompt="You summarize.")

    assert result.final_response == "a one line summary"
    assert capsys.readouterr().out == ""  # no leak to the terminal


def test_opencode_parse_nested_text_part():
    backend = OpenCodeBackend(Path("."))
    output = "\n".join(
        [
            '{"type":"step_start","sessionID":"ses-1","part":{"type":"step-start"}}',
            '{"type":"text","sessionID":"ses-1","part":{"type":"text","text":"Hi. What would you like to work on?","metadata":{"openai":{"phase":"final_answer"}}}}',
            '{"type":"step_finish","sessionID":"ses-1","part":{"type":"step-finish"}}',
        ]
    )

    final, session_id, model, _tokens = backend._read_events(io.StringIO(output))

    assert final == "Hi. What would you like to work on?"
    assert session_id == "ses-1"
    assert model is None


def test_opencode_parse_token_usage():
    backend = OpenCodeBackend(Path("."))
    parsed = backend._parse_event_line(
        '{"type":"step_finish","sessionID":"ses-1","part":{"type":"step-finish","tokens":{"total":8883,"input":8869,"output":14,"reasoning":0,"cache":{"write":3,"read":2}}}}'
    )

    assert parsed is not None
    _display, _final, session_id, _model, tokens = parsed
    assert session_id == "ses-1"
    # `context` is how FULL the window is, so it counts every token the model read — the cached
    # prefix included. This used to assert `input` alone (8869), which under-reported it by the
    # whole cache: measured on a live OpenCode turn, input=727 against cache.read=5632, so the
    # gauge read 727 when the true context was 6365. Claude has always summed the three, so this
    # is also what makes the number mean the same thing on both backends.
    # See tests/test_token_accounting.py for the full semantics, pinned against real output.
    assert tokens.context == 8869 + 2 + 3
    assert tokens.total == 14
    assert tokens.input == 8869
    assert tokens.output == 14
    assert tokens.cache_write == 3
    assert tokens.cache_read == 2


# --- turn completeness ------------------------------------------------------
#
# `SessionTurn.complete` is how every caller asks "has the agent finished this turn?" —
# `CommitEngine.finish_parse_if_ready` finds the running turn via `not turns[-1].complete`, and
# the background tracker refuses to commit a turn that is still being written. OpenCode's
# parser never set it, so every turn defaulted to complete=True and the whole mid-turn
# machinery was INERT on this backend: an agent commit made mid-turn got no in-flight
# attribution, and the daemon would commit a half-written turn. Claude computes the same thing.


def _export(finish, *, with_assistant=True):
    messages = [{"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "fix it"}]}]
    if with_assistant:
        info = {"role": "assistant", "id": "a1", "time": {"completed": 5}}
        if finish is not None:
            info["finish"] = finish
        messages.append({"info": info, "parts": [{"type": "text", "text": "working"}]})
    from agitrack.transcripts.opencode import parse_exported_session

    return parse_exported_session({"info": {"id": "ses-1", "time": {"updated": 1}}, "messages": messages})


def test_a_finished_opencode_turn_is_complete():
    # The real shape, verified against a live `opencode export`: a finished assistant message
    # records finish: "stop".
    session = _export("stop")
    assert session.turns[-1].complete is True


def test_a_streaming_opencode_turn_is_not_complete():
    # Still being written: no terminal reason yet. Reporting this as complete is what let the
    # tracker commit a turn mid-write and suppressed in-flight attribution entirely.
    session = _export(None)
    assert session.turns[-1].complete is False


def test_an_unanswered_opencode_prompt_is_not_complete():
    # The user sent a prompt and the agent has not started: there is no turn to commit.
    session = _export(None, with_assistant=False)
    assert session.turns == [] or session.turns[-1].complete is False


def test_opencode_completeness_matches_claudes_meaning():
    # Parity, stated directly: the same situation must answer the same on both backends, or
    # every caller that branches on `complete` behaves differently depending on the agent.
    from agitrack.transcripts.claude import parse_rows

    rows = [
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "go"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "claude-opus-5",
                "stop_reason": "tool_use",  # mid-tool-call: still running
                "content": [{"type": "text", "text": "working"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    ]
    claude_turn = parse_rows("s", rows).turns[-1]
    opencode_turn = _export(None).turns[-1]

    assert claude_turn.complete is False and opencode_turn.complete is False
