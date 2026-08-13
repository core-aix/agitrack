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


def test_the_debug_log_never_INVENTS_a_directory(tmp_path, monkeypatch):
    # Found live: with AGITRACK_DEBUG_PROXY=1 an OpenCode run grew a phantom
    # `.agitrack/worktrees/.agitrack/proxy-debug.log`, because `list_worktree_sessions` logs
    # against the worktrees ROOT and the logger created whatever parents it needed. Anything
    # enumerating worktrees then sees a directory that looks like a session.
    from agitrack.transcripts.opencode import _debug

    monkeypatch.setenv("AGITRACK_DEBUG_PROXY", "1")
    not_a_repo = tmp_path / "worktrees"
    not_a_repo.mkdir()

    _debug(not_a_repo, "scanning")

    assert not (not_a_repo / ".agitrack").exists()  # nothing conjured

    # A real repo (its .agitrack already exists) still gets the log.
    repo = tmp_path / "repo"
    (repo / ".agitrack").mkdir(parents=True)
    _debug(repo, "scanning")
    assert "scanning" in (repo / ".agitrack" / "proxy-debug.log").read_text(encoding="utf-8")


def test_apply_patch_edits_are_counted(tmp_path):
    """C31: `_edits_from_parts` matched only write/edit/patch and dropped `apply_patch`, whose
    payload key is `patchText`. OpenAI-family models under OpenCode reach for it, so those
    sessions reported +0/-0 lines and `--backtrace commit` exited 128 with "every agent-made
    commit here is user commits with no agent-made edits to attribute" — seconds after
    `--backtrace text` had reported the same commit as `agent 1 … 1/1 (100.0%)`."""
    from agitrack.transcripts.opencode import _edits_from_parts

    parts = [
        {
            "type": "tool",
            "tool": "apply_patch",
            "state": {
                "input": {"patchText": ("*** Begin Patch\n*** Add File: hello.txt\n+hello\n+world\n*** End Patch\n")}
            },
        }
    ]
    edits = _edits_from_parts(parts, {})

    assert [e.path for e in edits] == ["hello.txt"]
    assert edits[0].insertions == 2 and edits[0].deletions == 0


def test_apply_patch_update_diffs_incrementally():
    from agitrack.transcripts.edits import edits_from_apply_patch

    state = {"a.py": "one\ntwo\nthree\n"}
    patch = "*** Begin Patch\n*** Update File: a.py\n@@\n one\n-two\n+TWO\n three\n*** End Patch\n"

    edits = edits_from_apply_patch(state, patch)

    assert [(e.path, e.insertions, e.deletions) for e in edits] == [("a.py", 1, 1)]
    assert state["a.py"] == "one\nTWO\nthree\n"


def test_context_tokens_are_the_peak_not_the_last_step():
    """D7: OpenCode records usage per STEP and `TokenUsage.add` overwrites `context` with
    whatever came last, so the final — often tiny — step's count replaced the real total:
    349 / 357 / 187 reported against an actual ~6.5 K."""
    from agitrack.transcripts.opencode import _tokens

    parts = [
        {"tokens": {"input": 6500, "output": 40, "cache": {"read": 0, "write": 0}}},
        {"tokens": {"input": 187, "output": 5, "cache": {"read": 0, "write": 0}}},
    ]

    usage = _tokens({}, parts)

    assert usage.context == 6500
    assert usage.output == 45  # the summed fields are unaffected


def test_a_session_agitrack_drove_itself_is_not_offered_as_a_conversation(tmp_path, monkeypatch):
    """Parity: Claude filters `promptSource: sdk` and Codex filters `source == "exec"`, but
    OpenCode's `session list` exposes no such field — so a scripted run was adopted as the
    user's conversation. aGiTrack cannot see into someone else's script, but it must always
    account for its own."""
    from agitrack.transcripts import opencode as oc

    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(
        oc,
        "_fetch_sessions",
        lambda repo, n: [
            {"id": "ses_real", "updated": 1000, "title": "my chat", "directory": str(tmp_path)},
            {"id": "ses_bot", "updated": 2000, "title": "summary", "directory": str(tmp_path)},
        ],
    )

    assert oc.latest_session_id(tmp_path) == "ses_bot"  # newest wins while nothing is marked

    oc.mark_programmatic("ses_bot")

    assert oc.latest_session_id(tmp_path) == "ses_real"
    assert {r.id: r.programmatic for r in oc.list_sessions(tmp_path)} == {"ses_real": False, "ses_bot": True}
