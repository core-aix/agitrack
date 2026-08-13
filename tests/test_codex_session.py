"""The Codex rollout parser (``agitrack.transcripts.codex``).

Every record shape here was captured from real rollout files written by codex-cli 0.147.0 —
``session_meta`` / ``turn_context`` / ``task_started`` / ``user_message`` / ``agent_message`` /
``function_call`` / ``custom_tool_call`` / ``patch_apply_end`` / ``token_count`` /
``task_complete`` — so a Codex release that renames a field breaks these tests rather than
silently emptying every commit's metadata.

``CODEX_HOME`` is redirected at the fixture level so no test can read (or write) the
developer's real ``~/.codex`` session store.
"""

from __future__ import annotations

import json

import pytest

from agitrack.transcripts import codex
from agitrack.transcripts.types import turns_after

SESSION = "019fe8dc-ca6c-7951-9225-73513aadf083"


@pytest.fixture(autouse=True)
def codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return tmp_path / "codex"


def _row(kind, payload, stamp="2026-08-09T23:30:12.005Z"):
    return {"timestamp": stamp, "type": kind, "payload": payload}


def _meta(cwd="/repo", session_id=SESSION, thread_source="user"):
    return _row(
        "session_meta",
        {"session_id": session_id, "id": session_id, "cwd": cwd, "source": "cli", "thread_source": thread_source},
    )


def _usage(inp, cached=0, out=0, reasoning=0, write=0):
    return _row(
        "event_msg",
        {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": inp,
                    "cached_input_tokens": cached,
                    "cache_write_input_tokens": write,
                    "output_tokens": out,
                    "reasoning_output_tokens": reasoning,
                    "total_tokens": inp + out,
                }
            },
        },
    )


def _turn(turn_id, prompt, reply, *, model="gpt-5.4-mini", complete=True, extra=()):
    rows = [
        _row("event_msg", {"type": "task_started", "turn_id": turn_id}),
        _row("turn_context", {"turn_id": turn_id, "model": model, "cwd": "/repo"}),
        _row("event_msg", {"type": "user_message", "message": prompt}),
        *extra,
        _row("event_msg", {"type": "agent_message", "message": reply, "phase": "final"}),
    ]
    if complete:
        rows.append(_row("event_msg", {"type": "task_complete", "turn_id": turn_id, "last_agent_message": reply}))
    return rows


def _write(home, rows, session_id=SESSION):
    directory = home / "sessions" / "2026" / "08" / "10"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-08-10T00-30-11-{session_id}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


# --- turn assembly -----------------------------------------------------------


def test_each_task_started_to_task_complete_is_one_turn(codex_home):
    path = _write(
        codex_home,
        [_meta(), *_turn("t1", "add subtract", "Added subtract."), *_turn("t2", "add multiply", "Added multiply.")],
    )

    session = codex.export_session_at(path)

    assert [t.user_prompt for t in session.turns] == ["add subtract", "add multiply"]
    assert [t.final_response for t in session.turns] == ["Added subtract.", "Added multiply."]
    assert all(t.complete for t in session.turns)
    assert all(t.model == "gpt-5.4-mini" for t in session.turns)


def test_a_turn_without_task_complete_is_incomplete(codex_home):
    # The commit engine must not split one prompt across two commits, so a turn still in flight
    # has to report itself unfinished rather than looking done.
    path = _write(codex_home, [_meta(), *_turn("t1", "still working", "partial", complete=False)])

    session = codex.export_session_at(path)

    assert len(session.turns) == 1
    assert session.turns[0].complete is False


def test_an_interrupted_turn_is_marked_and_closed(codex_home):
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "user_message", "message": "do a big thing"}),
        _row("event_msg", {"type": "turn_aborted", "turn_id": "t1"}),
        *_turn("t2", "next prompt", "done"),
    ]
    path = _write(codex_home, rows)

    session = codex.export_session_at(path)

    assert session.turns[0].interrupted is True
    assert session.turns[0].complete is False
    assert session.turns[1].user_prompt == "next prompt"  # the next turn is NOT merged into it


def test_a_second_task_started_closes_the_previous_turn_as_incomplete(codex_home):
    # A crash mid-turn leaves no task_complete. The next prompt must still be its own turn.
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "user_message", "message": "first"}),
        *_turn("t2", "second", "done"),
    ]
    path = _write(codex_home, rows)

    session = codex.export_session_at(path)

    assert [t.user_prompt for t in session.turns] == ["first", "second"]
    assert session.turns[0].complete is False


def test_a_prompt_queued_mid_turn_becomes_a_followup_not_a_new_turn(codex_home):
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "user_message", "message": "first ask"}),
        _row("event_msg", {"type": "user_message", "message": "also do this"}),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "both done"}),
    ]
    path = _write(codex_home, rows)

    turn = codex.export_session_at(path).turns[0]

    assert turn.user_prompt == "first ask"
    assert turn.queued_followups == ["also do this"]


def test_a_turn_with_only_tool_calls_and_no_text_still_parses(codex_home):
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "user_message", "message": "just run it"}),
        _row("response_item", {"type": "function_call", "name": "exec_command", "arguments": "{}"}),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1"}),
    ]
    path = _write(codex_home, rows)

    turn = codex.export_session_at(path).turns[0]

    assert turn.user_prompt == "just run it"
    assert turn.final_response == ""
    assert turn.assistant_message_id == ""  # nothing to anchor a committed-watermark on yet


def test_compaction_before_a_turn_is_recorded(codex_home):
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "compacted"}),
        _row("event_msg", {"type": "user_message", "message": "after compaction"}),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "ok"}),
    ]
    path = _write(codex_home, rows)

    assert codex.export_session_at(path).turns[0].compaction_count == 1


def test_emoji_and_non_ascii_survive_the_parse(codex_home):
    path = _write(codex_home, [_meta(), *_turn("t1", "add 🎉 support — naïve", "Done 🎉 — café")])

    turn = codex.export_session_at(path).turns[0]

    assert turn.user_prompt == "add 🎉 support — naïve"
    assert turn.final_response == "Done 🎉 — café"


# --- damage tolerance --------------------------------------------------------


def test_a_half_written_trailing_line_is_skipped(codex_home):
    # The rollout is being APPENDED TO by a live Codex process, so the last line is routinely a
    # partial record. Every mid-turn poll would fail if that raised.
    path = _write(codex_home, [_meta(), *_turn("t1", "hello", "hi")])
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"timestamp":"2026-08-09T23:31:00Z","type":"event_ms')

    session = codex.export_session_at(path)

    assert len(session.turns) == 1
    assert session.turns[0].final_response == "hi"


def test_a_missing_transcript_file_yields_none_not_an_exception(codex_home):
    assert codex.export_session(codex_home, "does-not-exist") is None
    assert codex.session_transcript_path("does-not-exist") is None
    assert codex.session_activity_mtime("does-not-exist") is None
    assert codex.session_last_activity("") is None


def test_an_empty_transcript_yields_none(codex_home):
    path = _write(codex_home, [])

    assert codex.export_session_at(path) is None


@pytest.mark.parametrize(
    "rows,label",
    [
        ([{"not": "a rollout row"}], "rows with no type at all"),
        ([{"type": "event_msg"}], "an event with no payload"),
        ([{"type": "event_msg", "payload": "not-a-dict"}], "a payload of the wrong type"),
        ([{"type": "event_msg", "payload": {"type": "task_complete"}}], "a complete with no start"),
    ],
)
def test_structurally_damaged_rows_never_raise(codex_home, rows, label):
    path = _write(codex_home, [_meta(), *rows])

    session = codex.export_session_at(path)

    assert session is not None, label
    assert session.turns == []


# --- tokens ------------------------------------------------------------------


def test_a_turn_sums_its_token_counts_and_keeps_the_last_context(codex_home):
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "user_message", "message": "go"}),
        _usage(11532, cached=8064, out=245, reasoning=20),
        _usage(12153, cached=11648, out=82, reasoning=14),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "done"}),
    ]
    path = _write(codex_home, rows)

    tokens = codex.export_session_at(path).turns[0].tokens

    assert tokens.input == (11532 - 8064) + (12153 - 11648)  # fresh input, cache never counted twice
    assert tokens.cache_read == 8064 + 11648
    assert tokens.output == (245 - 20) + (82 - 14)
    assert tokens.reasoning == 20 + 14
    assert tokens.context == 12153  # last wins: how full the window ended up


# --- capabilities ------------------------------------------------------------


def test_a_spawned_subagent_is_recorded_and_its_tokens_folded_in(codex_home):
    # A Codex sub-agent runs as its OWN thread with its own rollout, so its consumption is
    # absent from the parent's counters entirely — it has to be found and added, or every
    # multi-agent turn under-reports.
    child = "019fe8e3-7a9d-7d12-8e2b-da1425a793ce"
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "user_message", "message": "delegate it"}),
        _row(
            "response_item",
            {"type": "function_call", "name": "spawn_agent", "arguments": '{"message":"go","fork_context":true}'},
        ),
        _row(
            "response_item",
            {"type": "function_call", "name": "wait_agent", "arguments": json.dumps({"targets": [child]})},
        ),
        _usage(100, out=10),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "delegated"}),
    ]
    path = _write(codex_home, rows)
    _write(
        codex_home,
        [
            _meta(session_id=child, thread_source="subagent"),
            _usage(500, cached=100, out=60, reasoning=5),
            _patch_end("/repo/haiku.txt", {"type": "add", "content": "one\ntwo\nthree\n"}),
        ],
        session_id=child,
    )

    turn = codex.export_session_at(path, session_id=SESSION, collect_edits=True).turns[0]

    # The sub-agent's thread is deliberately excluded from the session listings, so nothing else
    # would ever read it — its file changes have to be attributed to the turn that delegated them
    # or they disappear from --backtrace entirely.
    assert [(e.path, e.insertions) for e in turn.edits] == [("/repo/haiku.txt", 3)]

    assert turn.subagents == [child]  # no nickname available without the state db → the id
    assert turn.tokens.subagent_input == 400
    assert turn.tokens.subagent_output == 55
    assert turn.tokens.subagent_reasoning == 5
    assert turn.tokens.input == 100  # the parent's own counters are untouched
    assert turn.tokens.context == 100  # a sub-agent has its own window; it must not move this


def test_a_spawn_without_a_wait_still_reports_that_a_subagent_ran(codex_home):
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "user_message", "message": "delegate"}),
        _row("response_item", {"type": "function_call", "name": "spawn_agent", "arguments": '{"message":"go"}'}),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "ok"}),
    ]
    path = _write(codex_home, rows)

    assert codex.export_session_at(path).turns[0].subagents == ["spawn_agent"]


def test_a_built_in_tool_is_never_mistaken_for_an_mcp_tool(codex_home):
    # apply_patch/exec_command are underscore-separated exactly like an MCP tool, so a
    # shape-based guess would invent a server named "apply" in the commit's provenance.
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "user_message", "message": "edit it"}),
        _row("response_item", {"type": "custom_tool_call", "name": "apply_patch", "input": "*** Begin Patch"}),
        _row("response_item", {"type": "function_call", "name": "exec_command", "arguments": "{}"}),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "ok"}),
    ]
    path = _write(codex_home, rows)

    turn = codex.export_session_at(path).turns[0]

    assert turn.mcp_servers == []
    assert turn.mcp_tools == []


def test_a_configured_mcp_server_is_matched_in_tool_names(codex_home, tmp_path):
    (codex_home).mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text('[mcp_servers.lore]\ncommand = "x"\n', encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    rows = [
        _meta(cwd=str(repo)),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "user_message", "message": "look it up"}),
        _row("response_item", {"type": "function_call", "name": "lore__lucky_number", "arguments": "{}"}),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "ok"}),
    ]
    path = _write(codex_home, rows)

    turn = codex.export_session_at(path, repo=repo).turns[0]

    assert turn.mcp_servers == ["lore"]
    assert turn.mcp_tools == ["lore/lucky_number"]


def test_a_plugin_supplied_skill_names_its_plugin(codex_home):
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "user_message", "message": "use the skill"}),
        _row(
            "response_item", {"type": "function_call", "name": "skill", "arguments": '{"skill":"lorepack:pack-lore"}'}
        ),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "ok"}),
    ]
    path = _write(codex_home, rows)

    turn = codex.export_session_at(path).turns[0]

    assert turn.skills == ["lorepack:pack-lore"]
    assert turn.plugins == ["lorepack"]


# --- file edits --------------------------------------------------------------


def _patch_end(path, change):
    return _row("event_msg", {"type": "patch_apply_end", "success": True, "changes": {path: change}})


def test_an_added_file_records_its_whole_content(codex_home):
    # Codex describes an ADD with `content` and an UPDATE with `unified_diff`. Reading only the
    # diff (the update shape) silently recorded zero edits for every newly-created file.
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "user_message", "message": "make it"}),
        _patch_end("/repo/haiku.txt", {"type": "add", "content": "one\ntwo\n"}),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "ok"}),
    ]
    path = _write(codex_home, rows)

    edits = codex.export_session_at(path, collect_edits=True).turns[0].edits

    assert [(e.path, e.insertions, e.deletions) for e in edits] == [("/repo/haiku.txt", 2, 0)]
    assert "new file mode" in edits[0].patch


def test_an_updated_file_records_the_hunk_as_an_incremental_edit(codex_home):
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "user_message", "message": "add subtract"}),
        _patch_end(
            "/repo/calc.py",
            {
                "type": "update",
                "unified_diff": "@@ -2 +2,5 @@\n     return a + b\n+\n+\n+def subtract(a, b):\n+    return a - b\n",
            },
        ),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "ok"}),
    ]
    path = _write(codex_home, rows)

    edits = codex.export_session_at(path, collect_edits=True).turns[0].edits

    assert [(e.path, e.insertions, e.deletions) for e in edits] == [("/repo/calc.py", 4, 0)]


def test_a_failed_patch_records_no_edit(codex_home):
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "user_message", "message": "try it"}),
        _row(
            "event_msg",
            {
                "type": "patch_apply_end",
                "success": False,
                "changes": {"/repo/x.py": {"type": "add", "content": "nope\n"}},
            },
        ),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "failed"}),
    ]
    path = _write(codex_home, rows)

    assert codex.export_session_at(path, collect_edits=True).turns[0].edits == []


def test_edits_are_not_collected_unless_asked_for(codex_home):
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "user_message", "message": "make it"}),
        _patch_end("/repo/a.txt", {"type": "add", "content": "x\n"}),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "ok"}),
    ]
    path = _write(codex_home, rows)

    assert codex.export_session_at(path).turns[0].edits == []


# --- session discovery -------------------------------------------------------


def test_sessions_are_scoped_to_the_repo_they_recorded(codex_home, tmp_path):
    repo, other = tmp_path / "repo", tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    _write(codex_home, [_meta(cwd=str(repo)), *_turn("t1", "a", "b")])

    assert codex.session_belongs_to_repo(repo, SESSION) is True
    assert codex.session_belongs_to_repo(other, SESSION) is False
    assert [ref.id for ref in codex.list_sessions(repo)] == [SESSION]
    assert codex.list_sessions(other) == []
    assert codex.latest_session_id(repo) == SESSION


def test_a_subagent_thread_is_never_listed_as_a_resumable_session(codex_home, tmp_path):
    # A sub-agent's rollout sits in the same directory, against the same cwd, and is usually the
    # NEWEST file — so without the filter it would be adopted as "the session" on restart and
    # silently switch the user onto a sub-agent's thread.
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(codex_home, [_meta(cwd=str(repo)), *_turn("t1", "a", "b")])
    _write(codex_home, [_meta(cwd=str(repo), session_id="child-1", thread_source="subagent")], session_id="child-1")

    assert [ref.id for ref in codex.list_sessions(repo)] == [SESSION]
    assert [ref.id for ref, _p in codex.sessions_under(repo)] == [SESSION]


def test_a_headless_exec_session_is_marked_programmatic(codex_home, tmp_path):
    # aGiTrack's OWN summarizer calls look exactly like a `codex exec` thread; they must not be
    # offered as conversations to resume.
    repo = tmp_path / "repo"
    repo.mkdir()
    rows = [
        _row("session_meta", {"session_id": SESSION, "cwd": str(repo), "source": "exec", "thread_source": "user"}),
        *_turn("t1", "a", "b"),
    ]
    _write(codex_home, rows)

    assert [ref.programmatic for ref in codex.list_sessions(repo)] == [True]
    assert codex.latest_session_id(repo) is None


def test_worktree_sessions_are_paired_with_their_worktree_name(codex_home, tmp_path):
    root = tmp_path / "worktrees"
    (root / "feature-x").mkdir(parents=True)
    _write(codex_home, [_meta(cwd=str(root / "feature-x")), *_turn("t1", "a", "b")])

    assert [(name, ref.id) for name, ref in codex.list_worktree_sessions(root)] == [("feature-x", SESSION)]


def test_retargeting_moves_the_recorded_working_directory(codex_home, tmp_path):
    # Codex restores a resumed thread's RECORDED cwd rather than the launch dir, so a session
    # whose worktree moved would reopen in a stale (possibly deleted) directory.
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    _write(codex_home, [_meta(cwd=str(old)), *_turn("t1", "a", "b")])

    assert codex.retarget_session_cwd(new, SESSION, str(new)) is True
    assert codex.session_cwd(SESSION) == str(new)
    assert codex.retarget_session_cwd(new, SESSION, str(new)) is False  # already aligned: a no-op


# --- sharing -----------------------------------------------------------------


def test_a_shared_transcript_round_trips_through_import(codex_home, tmp_path):
    repo, elsewhere = tmp_path / "repo", tmp_path / "elsewhere"
    repo.mkdir()
    elsewhere.mkdir()
    _write(codex_home, [_meta(cwd=str(repo)), *_turn("t1", "hello", "hi there")])
    raw = codex.export_session_raw(repo, SESSION)

    # A re-import id must be a real UUID: Codex resolves a session by the uuid in its rollout
    # FILE NAME, so anything else writes a file neither Codex nor aGiTrack can find again.
    # `new_import_id()` is what production uses, so use it here too.
    new_id = codex.new_import_id()
    assert raw and codex.import_shared_session(elsewhere, SESSION, raw, as_id=new_id) is True
    imported = codex.export_session(elsewhere, new_id)

    assert imported is not None
    assert imported.turns[0].final_response == "hi there"
    # The cwd is rewritten on the way in, or Codex would resume pointing at a directory that
    # does not exist on this machine.
    assert codex.session_cwd(new_id) == str(elsewhere.resolve())


def test_capping_keeps_the_header_and_a_whole_trailing_turn(codex_home):
    # Git rejects an oversized blob, so a long conversation must be trimmed. Without the
    # session_meta header Codex cannot rebuild the thread and the share imports unresumable.
    rows = [_meta()] + [r for i in range(12) for r in _turn(f"t{i}", f"prompt {i}", f"reply {i}")]
    transcript = "".join(json.dumps(row) + "\n" for row in rows)

    capped = codex.cap_shared_transcript(transcript, len(transcript.encode()) // 3)

    assert len(capped.encode()) <= len(transcript.encode()) // 3
    lines = [json.loads(line) for line in capped.splitlines() if line.strip()]
    assert lines[0]["type"] == "session_meta"  # header preserved
    assert lines[1]["payload"]["type"] == "task_started"  # tail starts on a clean boundary
    parsed = codex.parse_exported_session(capped)
    assert parsed.turns and parsed.turns[-1].final_response == "reply 11"  # newest turns kept


def test_capping_leaves_a_small_transcript_untouched(codex_home):
    transcript = "".join(json.dumps(row) + "\n" for row in [_meta(), *_turn("t1", "a", "b")])

    assert codex.cap_shared_transcript(transcript, 10_000_000) == transcript


# --- misc --------------------------------------------------------------------


def test_turns_after_watermark_uses_codex_turn_ids(codex_home):
    path = _write(codex_home, [_meta(), *_turn("t1", "first", "one"), *_turn("t2", "second", "two")])
    session = codex.export_session_at(path)

    remaining = turns_after(session, session.turns[0].assistant_message_id)

    assert [t.user_prompt for t in remaining] == ["second"]


def test_a_raw_rollout_row_is_recognised_as_an_event_blob():
    # A rollout row that leaks into the trace is noise in a commit message.
    assert codex.looks_like_event_blob(json.dumps(_row("event_msg", {"type": "agent_message"}))) is True
    assert codex.looks_like_event_blob("Added subtract(a, b) to calc.py.") is False
    assert codex.looks_like_event_blob('{"unrelated": "json"}') is False


def test_trust_is_only_propagated_from_an_already_trusted_base_repo(codex_home, tmp_path):
    base = tmp_path / "repo"
    worktree = base / ".agitrack" / "worktrees" / "s1"
    worktree.mkdir(parents=True)
    codex_home.mkdir(parents=True, exist_ok=True)

    # Base repo not trusted → Codex must still ask, in full.
    assert codex.trust_args(worktree, base) == []

    (codex_home / "config.toml").write_text(
        f'[projects."{base.resolve()}"]\ntrust_level = "trusted"\n', encoding="utf-8"
    )

    args = codex.trust_args(worktree, base)
    assert args[0] == "-c"
    assert str(worktree.resolve()) in args[1] and "trusted" in args[1]
    assert codex.trust_args(base, base) == []  # not a worktree: nothing to propagate


# --- the interactive TUI's record shape --------------------------------------
#
# `codex exec` and the interactive TUI write DIFFERENT records for the same conversation, and
# the TUI's is the one every real aGiTrack session produces. Parsing only the exec shape gave
# every interactive turn an empty user prompt and zero file edits. These tests pin the TUI shape.


def _tui_message(role, text):
    return _row("response_item", {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]})


def test_a_tui_turn_recovers_the_prompt_from_role_messages(codex_home):
    # The TUI emits NO user_message/agent_message events at all — only response_item messages.
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _tui_message("developer", "<skills_instructions>\nA skill is...\n</skills_instructions>"),
        _tui_message("user", "<environment_context>\n  <cwd>/repo</cwd>\n</environment_context>"),
        _row("turn_context", {"turn_id": "t1", "model": "gpt-5.4-mini"}),
        _tui_message("user", "Add a subtract(a, b) function to calc.py."),
        _tui_message("assistant", "I'm checking calc.py first."),
        _tui_message("assistant", "DONE_SUBTRACT"),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1"}),
    ]
    path = _write(codex_home, rows)

    turn = codex.export_session_at(path).turns[0]

    assert turn.user_prompt == "Add a subtract(a, b) function to calc.py."
    assert turn.final_response == "DONE_SUBTRACT"
    assert turn.agent_messages == ["I'm checking calc.py first.", "DONE_SUBTRACT"]
    assert turn.queued_followups == []  # the environment_context block is not a queued prompt


def test_concatenated_context_blocks_are_not_the_users_prompt(codex_home):
    # A live turn carried <recommended_plugins>…</recommended_plugins><environment_context>…
    # </environment_context> as one 3,230-character "prompt". Requiring a SINGLE wrapping tag
    # missed it and the whole context dump became the commit's user prompt.
    injected = "<recommended_plugins>\nAirtable\n</recommended_plugins>\n<environment_context>\n<cwd>/repo</cwd>\n</environment_context>"
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _tui_message("user", injected),
        _tui_message("user", "the real prompt"),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "ok"}),
    ]
    path = _write(codex_home, rows)

    turn = codex.export_session_at(path).turns[0]

    assert turn.user_prompt == "the real prompt"
    assert turn.queued_followups == []


def test_prose_that_merely_contains_a_tag_is_still_a_prompt(codex_home):
    # The injection rule must not eat a genuine prompt that happens to mention markup.
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _tui_message("user", "<div>hello</div> — please wrap this in a component"),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "ok"}),
    ]
    path = _write(codex_home, rows)

    assert codex.export_session_at(path).turns[0].user_prompt.endswith("wrap this in a component")


def test_the_same_message_in_both_record_forms_is_counted_once(codex_home):
    # `codex exec` writes BOTH an event_msg and a mirrored response_item for each message.
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "user_message", "message": "one prompt"}),
        _tui_message("user", "one prompt"),
        _row("event_msg", {"type": "agent_message", "message": "one reply"}),
        _tui_message("assistant", "one reply"),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "one reply"}),
    ]
    path = _write(codex_home, rows)

    turn = codex.export_session_at(path).turns[0]

    assert turn.user_prompt == "one prompt"
    assert turn.queued_followups == []  # NOT treated as a second, queued prompt
    assert turn.agent_messages == ["one reply"]


def test_the_tui_records_file_edits_as_item_completed_filechange(codex_home):
    # The TUI has no patch_apply_end; it reports the applied patch as item_completed/FileChange.
    # Without that branch --backtrace reconstructed zero edits for every interactive session.
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _tui_message("user", "add subtract"),
        _row(
            "event_msg",
            {
                "type": "item_completed",
                "item": {
                    "type": "FileChange",
                    "changes": {
                        "/repo/calc.py": {
                            "type": "update",
                            "unified_diff": "@@ -2 +2,3 @@\n     return a + b\n+\n+def subtract(a, b):\n",
                        }
                    },
                },
            },
        ),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "done"}),
    ]
    path = _write(codex_home, rows)

    edits = codex.export_session_at(path, collect_edits=True).turns[0].edits

    assert [(e.path, e.insertions, e.deletions) for e in edits] == [("/repo/calc.py", 2, 0)]


def test_a_prompt_that_is_nothing_but_markup_is_still_a_prompt(codex_home):
    # The injection rule keys on Codex's snake_case tag convention, so a human pasting a whole
    # markup document as their entire prompt is not mistaken for scaffolding. Dropping a real
    # prompt (an empty user_prompt in the commit) is the worse of the two failure modes.
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _tui_message("user", '<Component>\n  <Button label="go" />\n</Component>'),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "ok"}),
    ]
    path = _write(codex_home, rows)

    assert codex.export_session_at(path).turns[0].user_prompt.startswith("<Component>")


def test_the_state_db_is_chosen_by_schema_NUMBER_not_string_order(codex_home):
    # The day Codex ships state_10.sqlite, a lexicographic sort puts state_5 first and every
    # id -> rollout_path / model / nickname lookup silently answers from the stale database.
    codex_home.mkdir(parents=True, exist_ok=True)
    for name in ("state_5.sqlite", "state_10.sqlite", "state_9.sqlite"):
        (codex_home / name).write_bytes(b"")

    assert [p.name for p in codex._state_dbs()] == ["state_10.sqlite", "state_9.sqlite", "state_5.sqlite"]


def test_a_repeated_wait_on_one_child_counts_it_once(codex_home):
    child = "019fe8e3-7a9d-7d12-8e2b-da1425a793ce"
    wait = _row(
        "response_item",
        {"type": "function_call", "name": "wait_agent", "arguments": json.dumps({"targets": [child]})},
    )
    rows = [
        _meta(),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _row("event_msg", {"type": "user_message", "message": "delegate"}),
        wait,
        wait,  # re-waiting after a partial return must not double-count the child
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "ok"}),
    ]
    path = _write(codex_home, rows)
    _write(codex_home, [_meta(session_id=child, thread_source="subagent"), _usage(500, out=60)], session_id=child)

    turn = codex.export_session_at(path, session_id=SESSION).turns[0]

    assert turn.tokens.subagent_input == 500
    assert turn.subagents == [child]


def test_the_session_label_comes_from_the_tui_record_shape_too(codex_home, tmp_path):
    # Reading only user_message events left every TUI session (i.e. every real aGiTrack session)
    # unlabeled in the picker.
    repo = tmp_path / "repo"
    repo.mkdir()
    rows = [
        _meta(cwd=str(repo)),
        _row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        _tui_message("user", "<environment_context>\n<cwd>/repo</cwd>\n</environment_context>"),
        _tui_message("user", "Refactor the parser"),
        _row("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "ok"}),
    ]
    _write(codex_home, rows)

    assert [ref.label for ref in codex.list_sessions(repo)] == ["Refactor the parser"]


def test_recorded_cwd_reads_the_header_without_parsing_the_whole_file(codex_home):
    # --backtrace needs the session's recorded cwd to relativize its (absolute) edit paths; the
    # rollout's own location under ~/.codex/sessions/ relativizes nothing.
    path = _write(codex_home, [_meta(cwd="/somewhere/repo"), *_turn("t1", "a", "b")])

    assert codex.recorded_cwd(path) == "/somewhere/repo"
    assert codex.recorded_cwd(codex_home / "nope.jsonl") is None
