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
