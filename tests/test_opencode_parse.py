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
    assert tokens.context == 8869
    assert tokens.total == 14
    assert tokens.input == 8869
    assert tokens.output == 14
    assert tokens.cache_write == 3
    assert tokens.cache_read == 2
