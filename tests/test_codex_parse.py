"""The Codex event-stream reader and command construction.

Every event shape asserted here was captured from a real ``codex exec --json`` run against
codex-cli 0.147.0; the token numbers in the usage tests are the actual counters that run
reported. Mirrors ``test_opencode_parse.py`` for the Codex backend.
"""

import io
from pathlib import Path

from agitrack.backends.codex import CodexBackend


def _events(*lines):
    return io.StringIO("\n".join(lines) + "\n")


def test_final_response_is_the_last_agent_message_not_the_narration():
    # Codex narrates while it works ("I'm checking the repository state…") as ordinary
    # agent_message items. Joining them would put the narration into the commit summary; only
    # the LAST one is the answer.
    backend = CodexBackend(Path("."))

    final, session_id, _tokens = backend._read_events(
        _events(
            '{"type":"thread.started","thread_id":"019fe8dc-ca6c-7951-9225-73513aadf083"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"I am checking calc.py first."}}',
            '{"type":"item.completed","item":{"id":"i1","type":"command_execution","command":"ls"}}',
            '{"type":"item.completed","item":{"id":"i2","type":"agent_message","text":"Added subtract(a, b)."}}',
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}',
        ),
        stream_console=False,
    )

    assert final == "Added subtract(a, b)."
    assert session_id == "019fe8dc-ca6c-7951-9225-73513aadf083"


def test_non_json_banner_lines_are_ignored():
    # `codex exec` prints a plain "Reading additional input from stdin..." banner (and any panic
    # text) on the same stream. Parsing it would make a diagnostic the commit's final response.
    backend = CodexBackend(Path("."))

    final, _sid, _tokens = backend._read_events(
        _events(
            "Reading additional input from stdin...",
            '{"type":"item.completed","item":{"type":"agent_message","text":"real answer"}}',
        ),
        stream_console=False,
    )

    assert final == "real answer"


def test_usage_splits_cached_input_and_reasoning_out_of_their_totals():
    # The two Codex-specific conversions. Its input_tokens INCLUDES cached_input_tokens and its
    # output_tokens INCLUDES reasoning_output_tokens; aGiTrack requires `input` to be FRESH
    # input and the generated categories not to overlap. Numbers are a real turn's counters.
    backend = CodexBackend(Path("."))

    _final, _sid, tokens = backend._read_events(
        _events(
            '{"type":"turn.completed","usage":{"input_tokens":11532,"cached_input_tokens":8064,'
            '"cache_write_input_tokens":0,"output_tokens":245,"reasoning_output_tokens":20}}'
        ),
        stream_console=False,
    )

    assert tokens.input == 11532 - 8064  # fresh input only — the cache is not counted twice
    assert tokens.cache_read == 8064
    assert tokens.output == 245 - 20  # reasoning lives in its own bucket
    assert tokens.reasoning == 20
    assert tokens.output + tokens.reasoning == 245  # …and together they are the real total
    assert tokens.context == 11532  # the whole prompt the model read, cache included


def test_usage_never_goes_negative_on_impossible_counters():
    # A provider reporting more cached input than input at all would otherwise produce negative
    # token totals, which propagate into commit metadata and the dashboard as nonsense.
    backend = CodexBackend(Path("."))

    _final, _sid, tokens = backend._read_events(
        _events(
            '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":99,"output_tokens":1,"reasoning_output_tokens":50}}'
        ),
        stream_console=False,
    )

    assert tokens.input == 0
    assert tokens.output == 0


def test_read_events_does_not_echo_to_stdout_when_streaming_off(capsys):
    # A bare (summarizer) run must be SILENT: aGiTrack's stdout is the host terminal, so echoing
    # the streamed agent text there leaks the summary next to the user's input box.
    backend = CodexBackend(Path("."))

    final, _sid, _tokens = backend._read_events(
        _events('{"type":"item.completed","item":{"type":"agent_message","text":"a one line summary"}}'),
        stream_console=False,
    )

    assert final == "a one line summary"
    assert capsys.readouterr().out == ""


def test_read_events_streams_to_stdout_for_foreground_runs(capsys):
    backend = CodexBackend(Path("."))

    backend._read_events(
        _events('{"type":"item.completed","item":{"type":"agent_message","text":"hello there"}}'),
        stream_console=True,
    )

    assert "hello there" in capsys.readouterr().out


def test_a_failed_turn_yields_no_text_rather_than_the_error():
    # turn.failed carries the provider's error JSON. Returning it would make an API error the
    # commit subject; the empty response is what makes the summarizer reject the run.
    backend = CodexBackend(Path("."))

    final, _sid, _tokens = backend._read_events(
        _events(
            '{"type":"error","message":"{\\"status\\":400}"}',
            '{"type":"turn.failed","error":{"message":"boom"}}',
        ),
        stream_console=False,
    )

    assert final == ""


def test_update_command_names_the_codex_binary():
    assert CodexBackend(Path(".")).update_command() == ["codex", "update"]


def test_update_command_honours_a_launch_wrapper():
    backend = CodexBackend(Path("."), launch_command=["somewrapper", "codex"])

    assert backend.update_command() == ["somewrapper", "codex", "update"]


# --- command construction -----------------------------------------------------
#
# Each assertion here encodes a failure seen against the real CLI, so a well-meant refactor of
# the argv can't silently reintroduce one.


def _captured_command(monkeypatch, **kwargs):
    import subprocess

    seen = {}

    class _FakeProc:
        def __init__(self):
            self.stdout = io.StringIO('{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n')

        def wait(self):
            return 0

        def kill(self):  # the bare-run watchdog holds a reference to this
            pass

    def fake_popen(command, **popen_kwargs):
        seen["command"] = command
        seen["kwargs"] = popen_kwargs
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    CodexBackend(Path(".")).run("a prompt", model=None, session_id=None, **kwargs)
    return seen


def test_a_bare_run_is_ephemeral_read_only_and_tool_free(monkeypatch):
    seen = _captured_command(monkeypatch, bare=True)
    command = seen["command"]

    # --ephemeral writes NO session file, so a summarizer conversation cannot be listed,
    # adopted or resumed as the user's session (the failure #8/#56 describe on other backends).
    assert "--ephemeral" in command
    assert command[command.index("-s") + 1] == "read-only"
    # Leaving the agentic tools on made a live summarizer shell out ~20 times before replying.
    assert "shell_tool" in command
    # `minimal` effort is rejected by the API whenever web_search is attached (HTTP 400).
    assert 'model_reasoning_effort="low"' in command


def test_a_bare_run_passes_the_system_prompt_as_an_instructions_FILE(monkeypatch, tmp_path):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path))
    seen = _captured_command(monkeypatch, bare=True, system_prompt="You summarize commits.")

    override = next(a for a in seen["command"] if a.startswith("experimental_instructions_file="))
    path = Path(override.split("=", 1)[1].strip('"'))

    # It has to be a file — Codex takes a replacement system prompt no other way, and
    # `base_instructions` is not a settable key at all.
    assert path.read_text(encoding="utf-8") == "You summarize commits."


def test_a_coding_run_gets_workspace_write_and_no_instructions_override(monkeypatch):
    seen = _captured_command(monkeypatch, bare=False)
    command = seen["command"]

    assert command[command.index("-s") + 1] == "workspace-write"
    assert "--ephemeral" not in command
    assert not any(a.startswith("experimental_instructions_file=") for a in command)


def test_resume_is_a_subcommand_emitted_after_every_option(monkeypatch):
    # `codex exec resume -s workspace-write <id>` is rejected ("unexpected argument '-s'").
    import subprocess

    seen = {}

    class _FakeProc:
        def __init__(self):
            self.stdout = io.StringIO("")

        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda command, **kw: (seen.update(command=command), _FakeProc())[1])
    CodexBackend(Path(".")).run("go on", model="gpt-5.4-mini", session_id="019f-abc")
    command = seen["command"]

    assert command[-3:] == ["resume", "019f-abc", "go on"]
    assert command.index("-s") < command.index("resume")


def test_stdin_is_closed_so_codex_cannot_read_the_hosts_terminal(monkeypatch):
    # `codex exec` folds a piped stdin into the prompt as a <stdin> block. aGiTrack's stdin is
    # the user's terminal, so inheriting it lets the agent eat the host's keystrokes — and a
    # background summarizer blocks forever waiting for EOF.
    import subprocess

    seen = _captured_command(monkeypatch, bare=True)

    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL
