from pathlib import Path

from agit.backends.opencode import OpenCodeBackend


def test_opencode_parse_prefers_final_response():
    backend = OpenCodeBackend(Path("."))
    output = '\n'.join([
        '{"type":"message","content":"partial"}',
        '{"type":"thinking","content":"secret"}',
        '{"type":"final","content":"done","sessionID":"ses-1","model":"m"}',
    ])

    final, session_id, model = backend._parse_output(output)

    assert final == "done"
    assert session_id == "ses-1"
    assert model == "m"


def test_opencode_parse_nested_text_part():
    backend = OpenCodeBackend(Path("."))
    output = '\n'.join([
        '{"type":"step_start","sessionID":"ses-1","part":{"type":"step-start"}}',
        '{"type":"text","sessionID":"ses-1","part":{"type":"text","text":"Hi. What would you like to work on?","metadata":{"openai":{"phase":"final_answer"}}}}',
        '{"type":"step_finish","sessionID":"ses-1","part":{"type":"step-finish"}}',
    ])

    final, session_id, model = backend._parse_output(output)

    assert final == "Hi. What would you like to work on?"
    assert session_id == "ses-1"
    assert model is None
