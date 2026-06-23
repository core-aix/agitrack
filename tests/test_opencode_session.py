import json
import subprocess

from agitrack.transcripts.opencode import (
    latest_session_id,
    parse_exported_session,
    session_belongs_to_repo,
    turns_after,
)


def test_parse_exported_session_turns_model_and_tokens():
    session = parse_exported_session(
        {
            "info": {
                "id": "ses-1",
                "model": {"providerID": "openai", "id": "gpt-5.5"},
                "time": {"updated": 123},
            },
            "messages": [
                {
                    "info": {"role": "user", "id": "u1"},
                    "parts": [{"type": "text", "text": "fix it"}],
                },
                {
                    "info": {
                        "role": "assistant",
                        "id": "a1",
                        "parentID": "u1",
                        "providerID": "openai",
                        "modelID": "gpt-5.5",
                        "tokens": {"total": 99, "input": 90, "output": 9},
                    },
                    "parts": [
                        {
                            "type": "text",
                            "text": "fixed",
                            "metadata": {"openai": {"phase": "final_answer"}},
                        }
                    ],
                },
            ],
        }
    )

    assert session.session_id == "ses-1"
    assert session.model == "openai/gpt-5.5"
    assert session.updated == 123
    assert len(session.turns) == 1
    assert session.turns[0].user_prompt == "fix it"
    assert session.turns[0].final_response == "fixed"
    assert session.turns[0].model == "openai/gpt-5.5"
    assert session.turns[0].tokens.total == 9


def test_parse_exported_session_groups_multiple_assistants_until_next_user():
    session = parse_exported_session(
        {
            "info": {"id": "ses-1", "model": {"providerID": "ollama", "id": "qwen"}},
            "messages": [
                {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "add timer"}]},
                {
                    "info": {
                        "role": "assistant",
                        "id": "a-tools",
                        "parentID": "u1",
                        "finish": "tool-calls",
                        "tokens": {"total": 5},
                    },
                    "parts": [{"type": "tool", "tool": "edit"}],
                },
                {
                    "info": {
                        "role": "assistant",
                        "id": "a-final",
                        "parentID": "u1",
                        "finish": "stop",
                        "tokens": {"total": 7, "input": 5, "output": 2},
                    },
                    "parts": [{"type": "text", "text": "Added countdown timer."}],
                },
            ],
        }
    )

    assert len(session.turns) == 1
    assert session.turns[0].assistant_message_id == "a-final"
    assert session.turns[0].final_response == "Added countdown timer."
    assert session.turns[0].tokens.total == 2
    assert session.turns[0].tokens.input == 5
    # The single text-bearing assistant message is the turn's only agent message.
    assert session.turns[0].agent_messages == ["Added countdown timer."]


def test_parse_exported_session_collects_all_agent_messages_in_order():
    # Two assistant messages each emit a user-facing reply (around a tool call);
    # both are kept on agent_messages, with final_response the last.
    session = parse_exported_session(
        {
            "info": {"id": "ses-1", "model": {"providerID": "ollama", "id": "qwen"}},
            "messages": [
                {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "do it"}]},
                {
                    "info": {"role": "assistant", "id": "a1", "parentID": "u1", "finish": "stop"},
                    "parts": [{"type": "text", "text": "On it."}],
                },
                {
                    "info": {"role": "assistant", "id": "a-tool", "parentID": "u1", "finish": "tool-calls"},
                    "parts": [{"type": "tool", "tool": "edit"}],
                },
                {
                    "info": {"role": "assistant", "id": "a2", "parentID": "u1", "finish": "stop"},
                    "parts": [{"type": "text", "text": "Done."}],
                },
            ],
        }
    )

    assert session.turns[0].agent_messages == ["On it.", "Done."]
    assert session.turns[0].final_response == "Done."


def test_parse_exported_session_excludes_compaction_summary():
    session = parse_exported_session(
        {
            "info": {"id": "ses-1"},
            "messages": [
                {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "do the work"}]},
                {
                    "info": {
                        "role": "assistant",
                        "id": "a1",
                        "parentID": "u1",
                        "finish": "stop",
                        "tokens": {"total": 4, "output": 4},
                    },
                    "parts": [{"type": "text", "text": "real response"}],
                },
                {
                    # The compaction summary must not overwrite the turn's response.
                    "info": {
                        "role": "assistant",
                        "id": "sum",
                        "parentID": "u1",
                        "mode": "compaction",
                        "agent": "compaction",
                        "summary": True,
                        "finish": "stop",
                        "tokens": {"total": 0},
                    },
                    "parts": [{"type": "text", "text": "This is a summary of the conversation so far..."}],
                },
            ],
        }
    )

    assert len(session.turns) == 1
    assert session.turns[0].final_response == "real response"
    assert session.turns[0].assistant_message_id == "a1"
    assert session.turns[0].tokens.total == 4
    # The compaction is excluded from the response/tokens but recorded as an event.
    assert session.turns[0].compaction_count == 1


def test_parse_exported_session_compaction_count_resets_per_turn():
    session = parse_exported_session(
        {
            "info": {"id": "ses-1"},
            "messages": [
                {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "first"}]},
                {
                    "info": {"role": "assistant", "id": "c1", "parentID": "u1", "mode": "compaction", "summary": True},
                    "parts": [{"type": "text", "text": "summary"}],
                },
                {
                    "info": {
                        "role": "assistant",
                        "id": "a1",
                        "parentID": "u1",
                        "finish": "stop",
                        "tokens": {"output": 2},
                    },
                    "parts": [{"type": "text", "text": "answer one"}],
                },
                {"info": {"role": "user", "id": "u2"}, "parts": [{"type": "text", "text": "second"}]},
                {
                    "info": {
                        "role": "assistant",
                        "id": "a2",
                        "parentID": "u2",
                        "finish": "stop",
                        "tokens": {"output": 3},
                    },
                    "parts": [{"type": "text", "text": "answer two"}],
                },
            ],
        }
    )

    assert [t.compaction_count for t in session.turns] == [1, 0]


def test_parse_exported_session_counts_reasoning_part_tokens():
    session = parse_exported_session(
        {
            "info": {"id": "ses-1"},
            "messages": [
                {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "think"}]},
                {
                    "info": {"role": "assistant", "id": "a1", "finish": "stop"},
                    "parts": [
                        {
                            "type": "reasoning",
                            "tokens": {"input": 10, "output": 0, "reasoning": 6, "cache": {"read": 4}},
                        },
                        {"type": "text", "text": "done", "tokens": {"input": 12, "output": 2, "reasoning": 0}},
                    ],
                },
            ],
        }
    )

    assert session.turns[0].tokens.context == 12
    assert session.turns[0].tokens.input == 22
    assert session.turns[0].tokens.total == 8
    assert session.turns[0].tokens.reasoning == 6
    assert session.turns[0].tokens.cache_read == 4
    # Reasoning tokens were spent, so the effort signal reads "on".
    assert session.turns[0].reasoning_effort == "on"


def test_parse_exported_session_prefers_named_reasoning_effort():
    session = parse_exported_session(
        {
            "info": {"id": "ses-1"},
            "messages": [
                {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "hi"}]},
                {
                    "info": {"role": "assistant", "id": "a1", "finish": "stop", "variant": "high"},
                    "parts": [
                        {"type": "text", "text": "done", "tokens": {"input": 1, "output": 1, "reasoning": 3}},
                    ],
                },
            ],
        }
    )

    # An explicit effort/variant in the export wins over the bare "on" fallback.
    assert session.turns[0].reasoning_effort == "high"


def test_parse_exported_session_omits_reasoning_effort_when_absent():
    session = parse_exported_session(
        {
            "info": {"id": "ses-1"},
            "messages": [
                {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "hi"}]},
                {
                    "info": {"role": "assistant", "id": "a1", "finish": "stop"},
                    "parts": [{"type": "text", "text": "done", "tokens": {"input": 1, "output": 1, "reasoning": 0}}],
                },
            ],
        }
    )

    assert session.turns[0].reasoning_effort is None


def test_parse_exported_session_extracts_final_text_from_event_blob():
    event_blob = "\n".join(
        [
            '{"type":"step_start","timestamp":1,"sessionID":"ses-1","part":{"type":"step-start"}}',
            '{"type":"text","timestamp":2,"sessionID":"ses-1","part":{"type":"text","text":"Hi.","metadata":{"openai":{"phase":"final_answer"}}}}',
            '{"type":"step_finish","timestamp":3,"sessionID":"ses-1","part":{"type":"step-finish","tokens":{"total":9529,"input":9523,"output":6,"reasoning":0}}}',
        ]
    )
    session = parse_exported_session(
        {
            "info": {"id": "ses-1"},
            "messages": [
                {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "hi"}]},
                {
                    "info": {"role": "assistant", "id": "a1", "finish": "stop"},
                    "parts": [{"type": "text", "text": event_blob}],
                },
            ],
        }
    )

    assert session.turns[0].final_response == "Hi."


def test_turns_after_last_message_id():
    session = parse_exported_session(
        {
            "info": {"id": "ses-1"},
            "messages": [
                {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "one"}]},
                {
                    "info": {"role": "assistant", "id": "a1", "parentID": "u1"},
                    "parts": [{"type": "text", "text": "done"}],
                },
                {"info": {"role": "user", "id": "u2"}, "parts": [{"type": "text", "text": "two"}]},
                {
                    "info": {"role": "assistant", "id": "a2", "parentID": "u2"},
                    "parts": [{"type": "text", "text": "done"}],
                },
            ],
        }
    )

    assert [turn.assistant_message_id for turn in turns_after(session, "a1")] == ["a2"]


def test_latest_session_id_prefers_most_recent_matching_repo(monkeypatch, tmp_path):
    old = tmp_path / "old"
    old.mkdir()

    sessions = [
        {"id": "other", "directory": str(old), "updated": 300},
        {"id": "older", "directory": str(tmp_path), "updated": 100},
        {"id": "newer", "directory": str(tmp_path), "updated": 200},
    ]

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps(sessions), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert latest_session_id(tmp_path) == "newer"


def test_session_belongs_to_repo(monkeypatch, tmp_path):
    sessions = [{"id": "ses-1", "directory": str(tmp_path), "updated": 1}]

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps(sessions), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert session_belongs_to_repo(tmp_path, "ses-1") is True
    assert session_belongs_to_repo(tmp_path, "other") is False


def test_list_worktree_sessions_filters_to_worktrees_newest_first(monkeypatch, tmp_path):
    from agitrack.transcripts.opencode import list_worktree_sessions

    worktrees_root = tmp_path / ".agitrack" / "worktrees"
    (worktrees_root / "session-1").mkdir(parents=True)
    (worktrees_root / "session-2").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    sessions = [
        {"id": "a", "directory": str(worktrees_root / "session-1"), "updated": 1000, "title": "first"},
        {"id": "b", "directory": str(worktrees_root / "session-2"), "updated": 3000, "title": "second"},
        {"id": "c", "directory": str(elsewhere), "updated": 9000, "title": "another repo"},
        {"id": "d", "directory": str(worktrees_root / "a" / "deep"), "updated": 5000},  # not an immediate child
    ]

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps(sessions), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = list_worktree_sessions(worktrees_root)
    # Only the immediate worktree children of this repo, paired with the worktree
    # name, newest first.
    assert [(key, ref.id) for key, ref in result] == [("session-2", "b"), ("session-1", "a")]
    assert result[0][1].label == "second"


# --- issue #19: never fall back to other repos' sessions ------------------------


def test_no_matching_directory_returns_no_sessions(monkeypatch, tmp_path):
    # All sessions belong to OTHER directories: returning them would make aGiTrack
    # adopt and resume an unrelated project's conversation at startup.
    other = tmp_path / "other-project"
    other.mkdir()
    sessions = [
        {"id": "foreign-1", "directory": str(other), "updated": 300},
        {"id": "foreign-2", "directory": str(other), "updated": 200},
    ]

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps(sessions), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    from agitrack.transcripts.opencode import list_sessions

    assert list_sessions(tmp_path) == []
    assert latest_session_id(tmp_path) is None


def test_directoryless_output_still_falls_back(monkeypatch, tmp_path):
    # Older OpenCode versions don't report `directory` at all; only then is the
    # unfiltered list acceptable (there is nothing to filter on).
    sessions = [
        {"id": "ses-old-format", "updated": 100},
        {"id": "ses-newer", "updated": 200},
    ]

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps(sessions), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert latest_session_id(tmp_path) == "ses-newer"


def test_mixed_output_with_any_directory_field_does_not_fall_back(monkeypatch, tmp_path):
    # If even one session carries `directory`, the format clearly supports it —
    # an empty match means this repo truly has no sessions.
    other = tmp_path / "other-project"
    other.mkdir()
    sessions = [
        {"id": "foreign", "directory": str(other), "updated": 300},
        {"id": "directoryless", "updated": 400},
    ]

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps(sessions), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert latest_session_id(tmp_path) is None


def test_run_export_pty_failed_exec_exits_child(tmp_path):
    # Issue #20: a failed chdir/exec in the forked export child must terminate
    # it (127), not let it keep executing the test suite as a duplicate process.
    from agitrack.transcripts.opencode import _run_export_pty

    output, exit_code = _run_export_pty(tmp_path / "missing-dir", "ses-x")

    assert exit_code == 127


def _task_part(parent_id, child_id):
    # The shape OpenCode records for a `task` sub-agent tool call: its state.metadata
    # carries the child session id and the parent session id.
    return {
        "type": "tool",
        "tool": "task",
        "state": {"metadata": {"parentSessionId": parent_id, "sessionId": child_id, "truncated": False}},
    }


def test_task_child_session_ids_requires_parent_and_child():
    from agitrack.transcripts.opencode import _task_child_session_ids

    assert _task_child_session_ids([_task_part("P", "C")]) == {"C"}
    # An ordinary tool that merely carries a sessionId (no parentSessionId) is ignored.
    assert _task_child_session_ids([{"type": "tool", "state": {"metadata": {"sessionId": "C"}}}]) == set()
    assert _task_child_session_ids([{"type": "text", "text": "hi"}]) == set()


def test_parse_exported_session_attributes_subagent_tokens_to_launching_turn():
    from agitrack.backends.base import TokenUsage

    data = {
        "info": {"id": "P", "model": {"providerID": "openai", "id": "gpt-5.5"}},
        "messages": [
            {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "plain"}]},
            {
                "info": {"role": "assistant", "id": "a1", "tokens": {"total": 9, "input": 90, "output": 9}},
                "parts": [{"type": "text", "text": "ok", "metadata": {"openai": {"phase": "final_answer"}}}],
            },
            {"info": {"role": "user", "id": "u2"}, "parts": [{"type": "text", "text": "delegate"}]},
            {
                "info": {"role": "assistant", "id": "a2", "tokens": {"total": 12, "input": 100, "output": 12}},
                "parts": [
                    _task_part("P", "C"),
                    {"type": "text", "text": "done", "metadata": {"openai": {"phase": "final_answer"}}},
                ],
            },
        ],
    }
    sub = TokenUsage(total=180, subagent_input=6131, subagent_output=180, subagent_cache_read=9728)
    session = parse_exported_session(data, subagent_tokens={"C": sub})

    first, second = session.turns
    assert first.tokens.subagent_output == 0  # the plain turn launched no sub-agent
    assert second.tokens.subagent_output == 180  # attributed to the turn that ran the task
    assert second.tokens.subagent_input == 6131
    assert second.tokens.subagent_cache_read == 9728
    assert second.tokens.total == 12 + 180  # sub-agent generated tokens roll into the total


def test_collect_subagent_tokens_recurses_into_nested_subagents(monkeypatch, tmp_path):
    # A sub-agent that itself spawns a sub-agent: the grandchild's tokens roll up into
    # the direct child's bucket (and ultimately the launching turn), and a cycle is safe.
    from agitrack.transcripts import opencode as O

    child = {
        "info": {"id": "C"},
        "messages": [
            {
                "info": {"role": "assistant", "tokens": {"total": 50, "input": 5, "output": 50}},
                "parts": [_task_part("C", "G")],
            },
        ],
    }
    grand = {
        "info": {"id": "G"},
        "messages": [
            {
                "info": {"role": "assistant", "tokens": {"total": 7, "input": 2, "output": 7}},
                "parts": [_task_part("G", "C")],
            },  # cycle back to C
        ],
    }
    exports = {"C": child, "G": grand}
    monkeypatch.setattr(O, "_export_data", lambda repo, sid: exports.get(sid))

    parent_data = {"info": {"id": "P"}, "messages": [{"info": {"role": "assistant"}, "parts": [_task_part("P", "C")]}]}
    token_map = O._collect_subagent_tokens(tmp_path, "P", parent_data)

    assert set(token_map) == {"C"}
    # child output 50 + grandchild output 7, accounted once despite the C->G->C cycle.
    assert token_map["C"].subagent_output == 57
    assert token_map["C"].subagent_input == 7  # 5 + 2


def test_opencode_bare_run_is_watchdog_capped(monkeypatch):
    # A bare (summarizer) run arms a watchdog that kills a hung process so it can't block this
    # session's next summary; an interactive run is uncapped. A killed process yields a
    # non-zero exit the summarizer treats as unusable (falling back to the prompt message).
    import io
    from pathlib import Path

    from agitrack.backends import opencode as O
    from agitrack.backends.opencode import OpenCodeBackend

    timers: list = []

    class FakeTimer:
        def __init__(self, interval, func):
            self.interval = interval
            self.func = func
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

    class FakeProc:
        def __init__(self):
            self.stdout = io.StringIO("")

        def wait(self):
            return -9  # as if killed by the watchdog

        def kill(self):
            pass

    monkeypatch.setattr(O.threading, "Timer", FakeTimer)
    monkeypatch.setattr(O.subprocess, "Popen", lambda *a, **k: FakeProc())
    backend = OpenCodeBackend(repo=Path("."))

    result = backend.run("summarize", model=None, session_id="s", bare=True)
    assert len(timers) == 1 and timers[0].interval > 0
    assert result.exit_code != 0  # killed → unusable, prompt-based message is kept

    timers.clear()
    backend.run("real work", model=None, session_id="s", bare=False)
    assert timers == []  # interactive turns are not capped


def test_read_events_captures_child_session_ids_from_task_events():
    # The headless run() path captures sub-agent child session ids from the live event
    # stream (a `task` tool event carries the child sessionId in its part metadata), so it
    # can export each child and fold its tokens into the run's totals.
    import io

    from agitrack.backends.opencode import OpenCodeBackend

    backend = OpenCodeBackend(repo=__import__("pathlib").Path("."))
    task_event = json.dumps(
        {
            "type": "tool",
            "part": {
                "type": "tool",
                "tool": "task",
                "state": {"metadata": {"parentSessionId": "P", "sessionId": "ses_child"}},
            },
        }
    )
    other = json.dumps({"type": "text", "part": {"type": "text", "text": "hi"}})
    child_ids: set[str] = set()
    backend._read_events(io.StringIO(task_event + "\n" + other + "\n"), child_ids=child_ids)
    assert child_ids == {"ses_child"}
