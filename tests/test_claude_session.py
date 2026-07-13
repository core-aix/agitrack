import json
import sys

import pytest

from agitrack.transcripts import claude as claude_session
from agitrack.transcripts import turns_after
from agitrack.transcripts.claude import (
    export_session,
    latest_session_id,
    list_sessions,
    parse_rows,
    session_belongs_to_repo,
)


def test_session_cwd_reads_last_recorded_cwd(monkeypatch, tmp_path):
    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    proj = config / "projects" / claude_session._encode_repo(tmp_path / "wt")
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(
        '{"type":"user","cwd":"/old/dir"}\n'
        '{"type":"assistant"}\n'  # a line without cwd is skipped
        '{"type":"user","cwd":"/new/dir"}\n',
        encoding="utf-8",
    )
    assert claude_session.session_cwd("s") == "/new/dir"  # last cwd wins
    assert claude_session.session_cwd("missing") is None


def test_session_transcript_path_and_mtime(monkeypatch, tmp_path):
    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    proj = config / "projects" / claude_session._encode_repo(tmp_path / "wt")
    proj.mkdir(parents=True)
    transcript = proj / "sess.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")

    assert claude_session.session_transcript_path("sess") == transcript
    assert claude_session.session_transcript_mtime("sess") == pytest.approx(transcript.stat().st_mtime)
    # Unknown / empty ids resolve to nothing rather than raising.
    assert claude_session.session_transcript_path("missing") is None
    assert claude_session.session_transcript_mtime("missing") is None
    assert claude_session.session_transcript_mtime("") is None


def test_retarget_session_cwd_rewrites_recorded_cwd(monkeypatch, tmp_path):
    # Resuming under a new launch dir (e.g. --no-worktree on the repo root) must
    # rewrite the transcript's recorded cwd so Claude's --resume doesn't restore an
    # old worktree directory.
    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    repo = tmp_path / "repo"
    proj = config / "projects" / claude_session._encode_repo(repo)
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(
        '{"type":"user","cwd":"/old/worktree","sessionId":"s"}\n{"type":"assistant","cwd":"/old/worktree"}\n',
        encoding="utf-8",
    )

    assert claude_session.retarget_session_cwd(repo, "s", str(repo)) is True
    body = (proj / "s.jsonl").read_text(encoding="utf-8")
    assert "/old/worktree" not in body
    # Parse JSON to compare paths — raw text has JSON-escaped backslashes on Windows.
    assert any(json.loads(ln).get("cwd") == str(repo) for ln in body.splitlines() if ln)
    assert claude_session.session_cwd("s") == str(repo)
    # Idempotent: a second call (already aligned) makes no change.
    assert claude_session.retarget_session_cwd(repo, "s", str(repo)) is False
    # Missing transcript → no-op.
    assert claude_session.retarget_session_cwd(repo, "missing", str(repo)) is False


def test_retarget_session_cwd_breaks_hardlink_to_other_copy(monkeypatch, tmp_path):
    # Retargeting must not mutate another (hardlinked) copy of the same transcript —
    # the two diverge because they now run in different directories.
    import os

    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    repo = tmp_path / "repo"
    other = tmp_path / "worktree"
    repo_proj = config / "projects" / claude_session._encode_repo(repo)
    other_proj = config / "projects" / claude_session._encode_repo(other)
    repo_proj.mkdir(parents=True)
    other_proj.mkdir(parents=True)
    (other_proj / "s.jsonl").write_text('{"type":"user","cwd":"/old/worktree"}\n', encoding="utf-8")
    os.link(other_proj / "s.jsonl", repo_proj / "s.jsonl")  # share one inode

    assert claude_session.retarget_session_cwd(repo, "s", str(repo)) is True
    # Parse JSON to compare paths — raw text has JSON-escaped backslashes on Windows.
    rewritten = (repo_proj / "s.jsonl").read_text(encoding="utf-8")
    assert any(json.loads(ln).get("cwd") == str(repo) for ln in rewritten.splitlines() if ln)
    # The other copy is untouched (hardlink was broken before the rewrite).
    assert (other_proj / "s.jsonl").read_text(encoding="utf-8") == '{"type":"user","cwd":"/old/worktree"}\n'


def test_session_discovery_is_strictly_repo_scoped(monkeypatch, tmp_path):
    # SAFETY: a session driven in one repo must NEVER be visible to another repo's tracker, or a
    # commit in repo A could pick up repo B's conversation and tokens. Claude keys transcripts by
    # the cwd-encoded project dir, so repoA's discovery only ever sees repoA's sessions.
    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    repo_a = tmp_path / "repoA"
    repo_b = tmp_path / "repoB"
    proj_a = config / "projects" / claude_session._encode_repo(repo_a)
    proj_b = config / "projects" / claude_session._encode_repo(repo_b)
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)

    def _session(prompt: str) -> str:
        return json.dumps({"type": "user", "message": {"role": "user", "content": prompt}, "cwd": "/x"}) + "\n"

    (proj_a / "sa.jsonl").write_text(_session("ALPHA work"), encoding="utf-8")
    (proj_b / "sb.jsonl").write_text(_session("BRAVO work"), encoding="utf-8")

    # Each repo discovers ONLY its own session.
    assert claude_session.latest_session_id(repo_a) == "sa"
    assert claude_session.latest_session_id(repo_b) == "sb"
    assert [r.id for r in claude_session.list_sessions(repo_a)] == ["sa"]
    assert [r.id for r in claude_session.list_sessions(repo_b)] == ["sb"]
    # repoB's session id resolves to NOTHING under repoA's project dir (no cross-repo read path).
    assert not claude_session._session_path(repo_a, "sb").exists()
    assert claude_session._session_path(repo_b, "sb").exists()  # it lives only under repoB


def test_retarget_session_cwd_repoints_worktree_file_paths(monkeypatch, tmp_path):
    # Regression (--no-worktree): retargeting must repoint not just the cwd field but every
    # absolute path under the old WORKTREE — tool file_path args, command output, mentions — so a
    # resumed agent edits the launch dir, not the worktree it sees throughout its history.
    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    repo = tmp_path / "repo"
    wt = repo / ".agitrack" / "worktrees" / "feature"
    proj = config / "projects" / claude_session._encode_repo(repo)
    proj.mkdir(parents=True)
    rows = [
        {"type": "user", "cwd": str(wt), "message": {"role": "user", "content": "edit it"}},
        {
            "type": "assistant",
            "cwd": str(wt),
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": f"{wt}/app.py"}},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": f"{wt}/sub/b.py"}},
                ]
            },
        },
    ]
    (proj / "s.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    assert claude_session.retarget_session_cwd(repo, "s", str(repo)) is True
    parsed = [json.loads(ln) for ln in (proj / "s.jsonl").read_text(encoding="utf-8").splitlines() if ln]
    assert all(r.get("cwd") == str(repo) for r in parsed)  # cwd repointed to base
    args = [b["input"]["file_path"] for b in parsed[1]["message"]["content"]]
    # The tool file_path args (what actually drives edits/reads) point at the base repo now.
    assert args == [f"{repo}/app.py", f"{repo}/sub/b.py"]


def test_retarget_session_cwd_leaves_unrelated_absolute_paths(monkeypatch, tmp_path):
    # An imported session whose old cwd is NOT an aGiTrack worktree: align its cwd field, but do
    # NOT rewrite unrelated absolute paths in content (those files don't exist in this repo).
    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    repo = tmp_path / "repo"
    proj = config / "projects" / claude_session._encode_repo(repo)
    proj.mkdir(parents=True)
    row = {
        "type": "assistant",
        "cwd": "/some/other/checkout",
        "message": {"content": [{"type": "text", "text": "saw /some/other/checkout/x.py"}]},
    }
    (proj / "s.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    assert claude_session.retarget_session_cwd(repo, "s", str(repo)) is True
    parsed = json.loads((proj / "s.jsonl").read_text(encoding="utf-8").strip())
    assert parsed["cwd"] == str(repo)  # cwd field aligned...
    assert parsed["message"]["content"][0]["text"] == "saw /some/other/checkout/x.py"  # ...content left alone


def test_session_cwd_since_ignores_stale_pre_launch_rows(monkeypatch, tmp_path):
    # #72: with `since`, only rows recorded at/after the current launch count, so
    # a stale cwd left by a resume/import doesn't read as drift.
    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    proj = config / "projects" / claude_session._encode_repo(tmp_path / "wt")
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(
        '{"type":"user","cwd":"/stale/base","timestamp":"2026-06-14T10:00:00Z"}\n'
        '{"type":"user","cwd":"/the/worktree","timestamp":"2026-06-14T10:00:30Z"}\n',
        encoding="utf-8",
    )
    from datetime import datetime

    launch = datetime.fromisoformat("2026-06-14T10:00:15+00:00").timestamp()  # between the two rows
    # Only the post-launch row (the worktree) qualifies; the stale one is ignored.
    assert claude_session.session_cwd("s", since=launch) == "/the/worktree"
    # Before any post-launch row exists, nothing qualifies → None (caller waits).
    assert claude_session.session_cwd("s", since=launch + 3600) is None
    # Without `since`, the last row still wins (unchanged behavior).
    assert claude_session.session_cwd("s") == "/the/worktree"


def test_prepare_resume_stages_transcript_into_worktree(monkeypatch, tmp_path):
    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    repo_root = tmp_path / "repo"
    worktree = repo_root / ".agitrack" / "worktrees" / "session-1"
    repo_root.mkdir()
    worktree.mkdir(parents=True)

    # A conversation recorded under the repo root (e.g. a plain `claude` run).
    root_proj = config / "projects" / claude_session._encode_repo(repo_root)
    root_proj.mkdir(parents=True)
    (root_proj / "abc.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")

    assert claude_session.prepare_resume(worktree, "abc") is True
    staged = config / "projects" / claude_session._encode_repo(worktree) / "abc.jsonl"
    assert staged.is_file()

    # Hardlinked (one inode), so a turn appended from the worktree is visible in
    # the original directory's transcript too — the conversation does not fork.
    with staged.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant"}\n')
    assert (root_proj / "abc.jsonl").read_text(encoding="utf-8").count("\n") == 2

    # Idempotent and id-specific.
    assert claude_session.prepare_resume(worktree, "abc") is True
    assert claude_session.prepare_resume(worktree, "missing") is False


def test_prepare_resume_refreshes_stale_staged_copy(monkeypatch, tmp_path):
    # Regression: a prior resume staged the transcript into the target dir, then cwd-retargeting
    # broke the hardlink — freezing that staged copy while the live copy elsewhere kept growing.
    # prepare_resume must REPLACE the stale snapshot with the newest copy, or --no-worktree
    # resumes an OLDER state of the conversation ("an older session opened").
    import os
    import time

    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    base = tmp_path / "repo"
    worktree = base / ".agitrack" / "worktrees" / "candle"
    base.mkdir()
    worktree.mkdir(parents=True)

    base_proj = config / "projects" / claude_session._encode_repo(base)
    wt_proj = config / "projects" / claude_session._encode_repo(worktree)
    base_proj.mkdir(parents=True)
    wt_proj.mkdir(parents=True)

    # The live (worktree) copy: the full, current conversation, newest mtime.
    live = wt_proj / "s.jsonl"
    live.write_text('{"type":"user"}\n{"type":"assistant"}\n{"type":"user"}\n', encoding="utf-8")
    # A STALE staged copy at the base target: fewer lines, OLDER mtime, separate inode.
    stale = base_proj / "s.jsonl"
    stale.write_text('{"type":"user"}\n', encoding="utf-8")
    old = time.time() - 10000
    os.utime(stale, (old, old))

    assert claude_session.prepare_resume(base, "s") is True
    # The base copy is refreshed to the full conversation, not left at the 1-line snapshot.
    assert (base_proj / "s.jsonl").read_text(encoding="utf-8").count("\n") == 3


def test_prepare_resume_keeps_fresh_staged_copy(monkeypatch, tmp_path):
    # The inverse: a staged copy that is already as fresh as (or fresher than) the source is
    # left untouched — no needless re-staging.
    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    base = tmp_path / "repo"
    base.mkdir()
    base_proj = config / "projects" / claude_session._encode_repo(base)
    base_proj.mkdir(parents=True)
    (base_proj / "s.jsonl").write_text('{"type":"user"}\n{"type":"assistant"}\n', encoding="utf-8")
    before = (base_proj / "s.jsonl").stat().st_ino

    assert claude_session.prepare_resume(base, "s") is True
    assert (base_proj / "s.jsonl").stat().st_ino == before  # untouched (no source is newer)


def test_link_session_surfaces_worktree_conversation_in_base(monkeypatch, tmp_path):
    config = tmp_path / "config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    repo_root = tmp_path / "repo"
    worktree = repo_root / ".agitrack" / "worktrees" / "session-1"
    repo_root.mkdir()
    worktree.mkdir(parents=True)

    # A conversation born inside aGiTrack (recorded under the worktree project dir).
    wt_proj = config / "projects" / claude_session._encode_repo(worktree)
    wt_proj.mkdir(parents=True)
    (wt_proj / "born.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")

    assert claude_session.link_session("born", worktree, repo_root) is True
    base = config / "projects" / claude_session._encode_repo(repo_root) / "born.jsonl"
    assert base.is_file()

    # Hardlinked: a later turn from the worktree is visible from the repo root too.
    with (wt_proj / "born.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant"}\n')
    assert base.read_text(encoding="utf-8").count("\n") == 2

    # Idempotent, and a no-op when the source isn't recorded.
    assert claude_session.link_session("born", worktree, repo_root) is True
    assert claude_session.link_session("missing", worktree, repo_root) is False


def _user(uuid, text, **extra):
    row = {"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}}
    row.update(extra)
    return row


def _assistant(msg_id, text, *, model="claude-opus-4-8", usage=None, content=None, stop_reason=None):
    blocks = content if content is not None else [{"type": "text", "text": text}]
    message = {"id": msg_id, "role": "assistant", "model": model, "content": blocks, "usage": usage or {}}
    if stop_reason is not None:
        message["stop_reason"] = stop_reason
    return {"type": "assistant", "message": message}


def test_parse_rows_groups_turns_with_final_response_and_tokens():
    rows = [
        _user("u1", "first prompt"),
        _assistant(
            "m0", "", content=[{"type": "thinking", "thinking": "..."}], usage={"input_tokens": 10, "output_tokens": 5}
        ),
        _assistant(
            "m1",
            "final answer one",
            usage={
                "input_tokens": 20,
                "output_tokens": 100,
                "cache_read_input_tokens": 8000,
                "cache_creation_input_tokens": 200,
            },
        ),
        _user("u2", "second prompt"),
        _assistant("m2", "final answer two", usage={"input_tokens": 30, "output_tokens": 50}),
    ]

    session = parse_rows("sess-1", rows)

    assert session.session_id == "sess-1"
    assert session.model == "claude-opus-4-8"
    assert len(session.turns) == 2

    turn1 = session.turns[0]
    assert turn1.user_prompt == "first prompt"
    assert turn1.final_response == "final answer one"
    assert turn1.assistant_message_id == "m1"
    # output summed across the turn's assistant messages
    assert turn1.tokens.output == 105
    assert turn1.tokens.total == 105
    # context taken from the last assistant message (input + cache read + cache write)
    assert turn1.tokens.context == 20 + 8000 + 200
    assert turn1.tokens.cache_read == 8000

    assert session.turns[1].user_prompt == "second prompt"
    assert session.turns[1].final_response == "final answer two"


def test_parse_rows_records_reasoning_effort_from_thinking_blocks():
    rows = [
        _user("u1", "thought prompt"),
        _assistant("m0", "", content=[{"type": "thinking", "thinking": "..."}]),
        _assistant("m1", "answer", usage={"input_tokens": 1, "output_tokens": 1}),
        _user("u2", "plain prompt"),
        _assistant("m2", "answer", usage={"input_tokens": 1, "output_tokens": 1}),
    ]

    session = parse_rows("sess-1", rows)

    # A thinking block means extended thinking was active for that turn; a turn
    # without one reveals nothing about reasoning, so it stays None (never "off").
    assert session.turns[0].reasoning_effort == "on"
    assert session.turns[1].reasoning_effort is None


def test_parse_rows_ignores_synthetic_model_marker():
    # Claude Code stamps synthetic (non-LLM) assistant messages — compaction notices,
    # interrupt markers — with the literal model "<synthetic>". It names no real model,
    # so it must not overwrite the turn's actual model.
    rows = [
        _user("u1", "prompt"),
        _assistant("m1", "real answer", usage={"input_tokens": 5, "output_tokens": 7}),
        _assistant("m2", "[interrupted]", model="<synthetic>", usage={"input_tokens": 0, "output_tokens": 0}),
    ]

    session = parse_rows("sess-syn", rows)

    # The real model sticks for both the turn and the session, never "<synthetic>".
    assert session.turns[0].model == "claude-opus-4-8"
    assert session.model == "claude-opus-4-8"


def test_parse_rows_collects_all_agent_messages_in_order():
    # A turn can interleave several user-facing replies with tool calls; the parser
    # keeps each text message (in order) on agent_messages, while final_response
    # stays the last one. Tool calls contribute no message.
    rows = [
        _user("u1", "do the thing"),
        _assistant(
            "m1",
            "On it.",
            content=[
                {"type": "text", "text": "On it."},
                {"type": "tool_use", "id": "t1", "name": "Edit", "input": {}},
            ],
        ),
        _assistant("m2", "", content=[{"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}]),
        _assistant("m3", "Done — all set."),
    ]

    turn = parse_rows("sess-multi", rows).turns[0]

    assert turn.agent_messages == ["On it.", "Done — all set."]
    assert turn.final_response == "Done — all set."


def _queued(prompt, *, mode="prompt", origin="human"):
    # How Claude Code records a message the user QUEUES while the agent is working: a
    # `type:"attachment"` row (attachment.type == "queued_command"), NOT a `type:"user"` row.
    return {
        "type": "attachment",
        "attachment": {"type": "queued_command", "prompt": prompt, "commandMode": mode, "origin": {"kind": origin}},
    }


def test_parse_rows_captures_queued_followup_messages_in_the_turn():
    # A follow-up the user sends WHILE the agent works must appear in the interaction trace —
    # it's threaded into the same turn, and was being dropped entirely (issue: follow-ups vanish).
    rows = [
        _user("u1", "Fix the messy commit format."),
        _assistant(
            "m1", "", content=[{"type": "tool_use", "id": "t1", "name": "Edit", "input": {}}], stop_reason="tool_use"
        ),
        _queued("The subject should still be summarized."),  # queued mid-work
        _assistant(
            "m2", "", content=[{"type": "tool_use", "id": "t2", "name": "Edit", "input": {}}], stop_reason="tool_use"
        ),
        _queued("Also add a mode matrix to the webpage."),  # a second queued follow-up
        _assistant("m3", "Done — all three addressed."),
    ]

    turn = parse_rows("sess-q", rows).turns[0]

    # The base prompt stays user_prompt; each queued follow-up is a DISTINCT message (its own
    # ## User heading later), captured in queued_followups — not merged into user_prompt.
    assert turn.user_prompt == "Fix the messy commit format."
    assert turn.queued_followups == [
        "The subject should still be summarized.",
        "Also add a mode matrix to the webpage.",
    ]
    assert turn.final_response == "Done — all three addressed."
    # Exactly one turn (the queued messages extend it, they don't each open a new turn).
    assert len(parse_rows("sess-q", rows).turns) == 1


def test_parse_rows_ignores_non_human_or_slash_queued_attachments():
    # Only genuine human prompts are captured; a queued slash directive or non-human origin isn't.
    rows = [
        _user("u1", "do the thing"),
        _queued("/compact"),  # a slash directive kept out of the trace
        _queued("noise", origin="system"),  # non-human origin
        _queued("bg", mode="bash"),  # not a typed prompt
        _assistant("m1", "done"),
    ]
    turn = parse_rows("sess-q2", rows).turns[0]
    assert turn.user_prompt == "do the thing"
    assert turn.queued_followups == []  # none of the excluded attachments were added


def test_parse_rows_marks_turn_incomplete_while_last_message_is_tool_use():
    # A prompt whose latest assistant message is a tool call is still mid-flight:
    # the agent paused between writing code and writing tests. aGiTrack must see this
    # turn as incomplete so it doesn't commit now and split the prompt in two.
    rows = [
        _user("u1", "fix the bug and add tests"),
        _assistant(
            "m1",
            "Let me add a sanitizer.",
            content=[
                {"type": "text", "text": "Let me add a sanitizer."},
                {"type": "tool_use", "id": "t1", "name": "Edit", "input": {}},
            ],
            stop_reason="tool_use",
        ),
    ]

    turn = parse_rows("sess-mid", rows).turns[0]

    assert turn.final_response == "Let me add a sanitizer."
    assert turn.complete is False


def test_parse_rows_marks_turn_complete_when_last_message_ends_the_turn():
    rows = [
        _user("u1", "fix the bug and add tests"),
        _assistant(
            "m1",
            "Working on it.",
            content=[
                {"type": "text", "text": "Working on it."},
                {"type": "tool_use", "id": "t1", "name": "Edit", "input": {}},
            ],
            stop_reason="tool_use",
        ),
        _assistant("m2", "Done — code and tests are in.", stop_reason="end_turn"),
    ]

    turn = parse_rows("sess-done", rows).turns[0]

    assert turn.final_response == "Done — code and tests are in."
    assert turn.complete is True


def test_parse_rows_turn_complete_when_stop_reason_absent():
    # Older transcripts (or other backends) may omit the stop reason; default to
    # complete so the commit loop is never stalled.
    rows = [_user("u1", "hello"), _assistant("m1", "hi")]
    assert parse_rows("sess-old", rows).turns[0].complete is True


def test_parse_rows_excludes_meta_sidechain_tool_results_and_commands():
    rows = [
        _user("c", "<local-command-caveat>noise</local-command-caveat>", isMeta=True),
        _user("s", "<command-name>/model</command-name>"),
        # Harness background-task completion notices are not real prompts either.
        _user("tn", "<task-notification>\n<task-id>abc</task-id>\n</task-notification>"),
        _user("side", "subagent prompt", isSidechain=True),
        {
            "type": "user",
            "uuid": "tr",
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]},
        },
        _user("real", "the real prompt"),
        _assistant("m1", "response"),
        # sidechain assistant output must not be attributed to the turn
        {
            "type": "assistant",
            "isSidechain": True,
            "message": {"id": "sx", "content": [{"type": "text", "text": "side"}], "usage": {}},
        },
    ]

    session = parse_rows("sess-2", rows)

    assert len(session.turns) == 1
    assert session.turns[0].user_prompt == "the real prompt"
    assert session.turns[0].final_response == "response"


def test_session_last_activity_uses_content_timestamp_not_file_mtime(tmp_path, monkeypatch):
    # Recency must come from message timestamps, not the file's mtime: aGiTrack rewrites a
    # transcript (staging / cwd-retarget) and bumps the file mtime without adding a message, so
    # mtime ranking can make an older conversation look newest. session_last_activity reads the
    # newest message timestamp, which doesn't move when aGiTrack touches the file.
    import os
    import time

    proj = tmp_path / "projects" / "encoded-repo"
    proj.mkdir(parents=True)
    monkeypatch.setattr(claude_session, "_projects_root", lambda: tmp_path / "projects")
    tx = proj / "sess-1.jsonl"
    tx.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "hi"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "uuid": "a1",
                "timestamp": "2026-01-02T03:04:05Z",
                "message": {"id": "m1", "content": [{"type": "text", "text": "ok"}], "usage": {}},
            }
        )
        + "\n"
    )
    os.utime(tx, (time.time() + 99999, time.time() + 99999))  # bump file mtime far into the future

    ts = claude_session.session_last_activity("sess-1")
    from datetime import datetime, timezone

    expected = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp()
    assert ts == expected  # content timestamp, NOT the bumped file mtime
    assert claude_session.session_last_activity("nonexistent") is None


def test_parse_rows_background_task_work_opens_its_own_turn():
    # The agent backgrounds a task; the turn finishes (end_turn) and is committed. Later the
    # task completes — the harness injects a <task-notification> user row — and the agent acts
    # on the result. That work must form its OWN turn rather than extend the prior (already
    # committed) turn: otherwise it overwrites that turn's assistant id, breaking the commit
    # watermark (turns_after would then re-export everything), and mis-attributes the work.
    rows = [
        _user("u1", "run the tests in the background"),
        _assistant("m1", "Started the tests in the background.", stop_reason="end_turn"),
        _user("tn", "<task-notification>\n<task-id>abc</task-id>\n</task-notification>"),
        _assistant("m2", "Tests passed; I fixed the failing case.", stop_reason="end_turn"),
    ]

    session = parse_rows("sess-bg", rows)

    assert len(session.turns) == 2
    assert session.turns[0].user_prompt == "run the tests in the background"
    assert session.turns[0].assistant_message_id == "m1"
    assert session.turns[1].user_prompt == "(background task completed)"
    assert session.turns[1].assistant_message_id == "m2"
    assert session.turns[1].final_response == "Tests passed; I fixed the failing case."
    # The first turn's id is preserved, so the watermark still matches it and only the new
    # background-driven turn is exported next.
    assert turns_after(session, "m1") == session.turns[1:]


def test_parse_rows_background_notification_mid_turn_does_not_split():
    # A background task completing WHILE a turn is still in flight (the agent is mid-tool) must
    # not split the turn — the continued work is part of the ongoing turn, not a new one.
    rows = [
        _user("u1", "do a thing"),
        _assistant(
            "m1",
            "",
            content=[{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
            stop_reason="tool_use",
        ),
        _user("tn", "<task-notification>\n<task-id>abc</task-id>\n</task-notification>"),
        _assistant("m2", "Done.", stop_reason="end_turn"),
    ]

    session = parse_rows("sess-bg2", rows)

    assert len(session.turns) == 1
    assert session.turns[0].user_prompt == "do a thing"
    assert session.turns[0].final_response == "Done."


def test_parse_rows_background_notification_then_real_prompt_is_not_a_turn():
    # A background notification followed by a genuine user prompt (the user typed before the
    # agent acted) must NOT create a phantom "(background task completed)" turn — the real
    # prompt supersedes it.
    rows = [
        _user("u1", "first"),
        _assistant("m1", "ok", stop_reason="end_turn"),
        _user("tn", "<task-notification>\n<task-id>abc</task-id>\n</task-notification>"),
        _user("u2", "second real prompt"),
        _assistant("m2", "done second", stop_reason="end_turn"),
    ]

    session = parse_rows("sess-bg3", rows)

    assert [t.user_prompt for t in session.turns] == ["first", "second real prompt"]


def test_parse_rows_opens_a_turn_for_a_slash_command_expansion():
    # Regression: `/init` (and other work-doing commands) write CLAUDE.md, but Claude
    # records the command as a <command-name> artifact and injects its instructions as a
    # separate isMeta user row. Both are filtered as non-prompts, so previously NO turn
    # opened and the file-creating assistant work was dropped — aGiTrack never committed
    # or merged it. The command's expansion must open a turn labelled with the command.
    rows = [
        _user("cmd", "<command-message>init</command-message>\n<command-name>/init</command-name>"),
        _user("exp", "Please analyze this codebase and create a CLAUDE.md file.", isMeta=True),
        _assistant("m1", "I've created CLAUDE.md.", stop_reason="end_turn"),
    ]

    session = parse_rows("sess-init", rows)

    assert len(session.turns) == 1
    turn = session.turns[0]
    assert turn.user_prompt == "/init"
    assert turn.final_response == "I've created CLAUDE.md."
    assert turn.complete is True


def test_parse_rows_command_without_expansion_opens_no_turn():
    # A command that injects no expanded prompt (e.g. /model, /clear) does no work, so it
    # must NOT open a spurious turn just because it was remembered — only an expansion does.
    rows = [
        _user("cmd", "<command-name>/model</command-name>"),
        _assistant("m1", "switched model", stop_reason="end_turn"),
    ]

    session = parse_rows("sess-model", rows)

    # No turn: the lone assistant row has no opening prompt to attach to.
    assert session.turns == []


def test_parse_rows_excludes_compaction_summary():
    rows = [
        _user(
            "summary",
            "This session is being continued from a previous conversation...",
            isCompactSummary=True,
            isVisibleInTranscriptOnly=True,
        ),
        _user("real", "the real prompt"),
        _assistant("m1", "response"),
    ]

    session = parse_rows("sess-compact", rows)

    assert len(session.turns) == 1
    assert session.turns[0].user_prompt == "the real prompt"


def test_parse_rows_attributes_sidechain_tokens_to_subagent_buckets():
    rows = [
        _user("real", "the real prompt"),
        _assistant("m1", "response", usage={"input_tokens": 30, "output_tokens": 50}),
        {
            "type": "assistant",
            "isSidechain": True,
            "message": {
                "id": "sx",
                "content": [{"type": "text", "text": "side"}],
                "usage": {"input_tokens": 40, "output_tokens": 60, "cache_read_input_tokens": 700},
            },
        },
    ]

    turn = parse_rows("sess-3", rows).turns[0]

    # Main-line counters reflect only the non-sidechain assistant message.
    assert turn.tokens.output == 50
    assert turn.tokens.input == 30
    # Sidechain consumption is recorded separately, not folded into the main count.
    assert turn.tokens.subagent_input == 40
    assert turn.tokens.subagent_output == 60
    assert turn.tokens.subagent_cache_read == 700
    # The sub-agent's context size never overrides the main turn's context.
    assert turn.tokens.context == 30


def test_parse_rows_counts_split_message_usage_exactly_once():
    # Claude splits ONE assistant API response (same message id, same usage) across
    # several rows — one per content block (thinking/text/tool_use). Its usage must be
    # counted ONCE, never multiplied by the number of rows (this over-counted output by
    # ~95% on real transcripts before the fix).
    usage = {"input_tokens": 100, "output_tokens": 40, "cache_read_input_tokens": 9, "cache_creation_input_tokens": 3}
    rows = [
        _user("u1", "do something"),
        _assistant("m1", "", usage=usage, content=[{"type": "thinking", "thinking": "hmm"}]),
        _assistant("m1", "", usage=usage, content=[{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]),
        _assistant("m1", "final answer", usage=usage, stop_reason="end_turn"),
    ]
    turn = parse_rows("s", rows).turns[0]
    assert turn.tokens.output == 40  # counted once, not 120
    assert turn.tokens.input == 100  # not 300
    assert turn.tokens.cache_read == 9
    assert turn.tokens.cache_write == 3
    assert turn.tokens.total == 40
    assert turn.final_response == "final answer"


def test_parse_rows_distinct_messages_still_sum():
    # Genuinely distinct assistant messages (a multi-step tool turn) DO sum — each id is
    # counted once, but different ids accumulate.
    rows = [
        _user("u1", "go"),
        _assistant("m1", "", usage={"output_tokens": 10, "input_tokens": 50}, stop_reason="tool_use"),
        _assistant("m2", "done", usage={"output_tokens": 20, "input_tokens": 5}, stop_reason="end_turn"),
    ]
    turn = parse_rows("s", rows).turns[0]
    assert turn.tokens.output == 30  # 10 + 20
    assert turn.tokens.input == 55  # 50 + 5


def test_parse_rows_counts_later_row_usage_when_first_row_usage_empty():
    # Robustness: if a split's first row carries no usage and a later row of the SAME id
    # carries it, the usage is still counted (not lost to a premature "already counted").
    rows = [
        _user("u1", "go"),
        _assistant("m1", "", usage={}, content=[{"type": "thinking", "thinking": "x"}]),
        _assistant("m1", "done", usage={"output_tokens": 7}, stop_reason="end_turn"),
    ]
    turn = parse_rows("s", rows).turns[0]
    assert turn.tokens.output == 7


def test_parse_rows_attributes_compaction_to_following_turn():
    # Claude injects the compaction summary as an `isCompactSummary` user row that sits
    # BETWEEN turns — after the prior turn's last message, before the next real prompt.
    # It must not become a turn of its own, and it is attributed to the NEXT turn (whose
    # context it shrank), not the prior one.
    rows = [
        _user("u1", "first task"),
        _assistant("m1", "first answer", usage={"output_tokens": 5}, stop_reason="end_turn"),
        _user("c1", "<conversation summary>", isCompactSummary=True),
        _user("u2", "second task"),
        _assistant("m2", "second answer", usage={"output_tokens": 6}, stop_reason="end_turn"),
    ]
    turns = parse_rows("s", rows).turns
    assert [t.user_prompt for t in turns] == ["first task", "second task"]  # no phantom turn
    assert [t.compaction_count for t in turns] == [0, 1]


def test_parse_rows_compaction_with_no_following_turn_is_unrecorded():
    # A compaction at the very end influenced no subsequent work, so it is not pinned to
    # the prior turn (which ran against the pre-compaction context).
    rows = [
        _user("u1", "task"),
        _assistant("m1", "answer", usage={"output_tokens": 5}, stop_reason="end_turn"),
        _user("c1", "<conversation summary>", isCompactSummary=True),
    ]
    turns = parse_rows("s", rows).turns
    assert len(turns) == 1
    assert turns[0].compaction_count == 0


def test_parse_rows_attributes_separate_file_subagent_tokens_by_tool_use_id():
    # Newer Claude Code records sub-agents in their own files; `export_session` reads
    # those and passes their tokens (keyed by the Task tool_use id) to parse_rows, which
    # must add them to the turn whose assistant message launched that tool.
    from agitrack.backends.base import TokenUsage

    rows = [
        _user("u1", "first prompt"),
        _assistant("m1", "done one", usage={"input_tokens": 10, "output_tokens": 20}),
        _user("u2", "fan out a subagent"),
        _assistant(
            "m2",
            "spawned",
            usage={"input_tokens": 11, "output_tokens": 22},
            content=[{"type": "tool_use", "id": "toolu_ABC", "name": "Agent", "input": {}}],
        ),
        _assistant("m2b", "all set", usage={"input_tokens": 5, "output_tokens": 7}),
    ]
    sub = TokenUsage(total=99, subagent_input=100, subagent_output=99, subagent_cache_read=300)
    session = parse_rows("sess", rows, subagent_tokens={"toolu_ABC": sub})

    first, second = session.turns
    # The first turn launched no sub-agent — untouched.
    assert first.tokens.subagent_output == 0
    # The second turn owns toolu_ABC, so the sub-agent tokens land there.
    assert second.tokens.subagent_output == 99
    assert second.tokens.subagent_input == 100
    assert second.tokens.subagent_cache_read == 300
    # The sub-agent's generated tokens roll into the turn's grand total too.
    assert second.tokens.total == 22 + 7 + 99


def test_parse_rows_subagent_with_no_tool_id_falls_back_to_latest_turn():
    from agitrack.backends.base import TokenUsage

    rows = [_user("u1", "only prompt"), _assistant("m1", "answer", usage={"output_tokens": 5})]
    session = parse_rows("sess", rows, subagent_tokens={None: TokenUsage(total=12, subagent_output=12)})
    assert session.turns[-1].tokens.subagent_output == 12  # never dropped


def test_parse_rows_unmatched_subagent_attributed_by_mtime_not_latest():
    # An id-less sub-agent (missing/unreadable meta.json) is attributed to the turn that was
    # ACTIVE at its file mtime — not always the latest turn. Attributing it to a stable,
    # earlier turn is what lets the commit watermark trim it after it is counted once,
    # instead of re-attaching (and re-counting) it onto each new turn on every re-parse.
    from datetime import datetime

    from agitrack.backends.base import TokenUsage

    def _u(uuid, text, ts):
        return {"type": "user", "uuid": uuid, "timestamp": ts, "message": {"role": "user", "content": text}}

    def _a(msg_id, text, ts, out):
        return {
            "type": "assistant",
            "timestamp": ts,
            "message": {
                "id": msg_id,
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": text}],
                "usage": {"output_tokens": out},
            },
        }

    rows = [
        _u("u1", "first", "2026-01-01T00:00:00Z"),
        _a("m1", "one", "2026-01-01T00:10:00Z", 5),
        _u("u2", "second", "2026-01-01T01:00:00Z"),
        _a("m2", "two", "2026-01-01T01:10:00Z", 5),
    ]
    when = int(datetime.fromisoformat("2026-01-01T00:05:00+00:00").timestamp())  # inside turn 1's span
    session = parse_rows(
        "sess",
        rows,
        subagent_tokens={None: TokenUsage(total=12, subagent_output=12)},
        unmatched_subagent_time=when,
    )
    first, second = session.turns
    assert first.tokens.subagent_output == 12  # attributed to the turn it ran during
    assert second.tokens.subagent_output == 0  # NOT the latest turn


def test_subagent_token_map_reads_separate_agent_files(tmp_path):
    # The on-disk layout newer Claude Code uses: <session>.jsonl plus a sibling
    # <session>/subagents/agent-*.jsonl (+ .meta.json naming the parent toolUseId).
    session_path = tmp_path / "sess.jsonl"
    session_path.write_text("{}\n", encoding="utf-8")
    sub_dir = tmp_path / "sess" / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-1.meta.json").write_text(json.dumps({"toolUseId": "toolu_ABC"}), encoding="utf-8")
    (sub_dir / "agent-1.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"type": "user", "isSidechain": True, "message": {"content": "go"}},
                {
                    "type": "assistant",
                    "isSidechain": True,
                    "message": {"usage": {"input_tokens": 40, "output_tokens": 60, "cache_read_input_tokens": 9}},
                },
                {
                    "type": "assistant",
                    "isSidechain": True,
                    "message": {"usage": {"input_tokens": 1, "output_tokens": 3}},
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # A second sub-agent whose meta is missing → keyed under None (still counted).
    (sub_dir / "agent-2.jsonl").write_text(
        json.dumps({"type": "assistant", "isSidechain": True, "message": {"usage": {"output_tokens": 4}}}) + "\n",
        encoding="utf-8",
    )

    token_map = claude_session._subagent_token_map(session_path)
    assert token_map["toolu_ABC"].subagent_output == 63  # 60 + 3
    assert token_map["toolu_ABC"].subagent_input == 41  # 40 + 1
    assert token_map["toolu_ABC"].subagent_cache_read == 9
    assert token_map[None].subagent_output == 4  # the meta-less agent


def test_export_session_accounts_separate_file_subagents_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    repo = tmp_path / "repo"
    repo.mkdir()
    project_dir = claude_session._project_dir(repo)
    project_dir.mkdir(parents=True)
    rows = [
        _user("u1", "fan out a subagent"),
        _assistant(
            "m1",
            "delegating",
            usage={"input_tokens": 10, "output_tokens": 20},
            content=[{"type": "tool_use", "id": "toolu_XYZ", "name": "Agent", "input": {}}],
        ),
        _assistant("m1b", "all done", usage={"output_tokens": 5}, stop_reason="end_turn"),
    ]
    (project_dir / "abc.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    sub_dir = project_dir / "abc" / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-1.meta.json").write_text(json.dumps({"toolUseId": "toolu_XYZ"}), encoding="utf-8")
    (sub_dir / "agent-1.jsonl").write_text(
        json.dumps(
            {"type": "assistant", "isSidechain": True, "message": {"usage": {"input_tokens": 70, "output_tokens": 88}}}
        )
        + "\n",
        encoding="utf-8",
    )

    session = export_session(repo, "abc")
    assert session is not None
    turn = session.turns[0]
    assert turn.tokens.subagent_output == 88  # the separate sub-agent file is read and attributed
    assert turn.tokens.subagent_input == 70
    assert turn.tokens.output == 25  # main-line output unaffected (20 + 5)


def test_subagent_tokens_since_counts_only_new_files(tmp_path, monkeypatch):
    # The headless run() path snapshots sub-agent files before a turn and counts only the
    # ones the turn adds, so a resumed session's prior sub-agents aren't double-counted.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    repo = tmp_path / "repo"
    repo.mkdir()
    sub_dir = claude_session._project_dir(repo) / "abc" / "subagents"
    sub_dir.mkdir(parents=True)

    def write_agent(name, output):
        (sub_dir / f"{name}.jsonl").write_text(
            json.dumps({"type": "assistant", "isSidechain": True, "message": {"usage": {"output_tokens": output}}})
            + "\n",
            encoding="utf-8",
        )

    write_agent("agent-old", 5)
    prior = claude_session.subagent_agent_files(repo, "abc")
    assert prior == {"agent-old.jsonl"}

    # The turn spawns two new sub-agents.
    write_agent("agent-new1", 11)
    write_agent("agent-new2", 22)
    usage = claude_session.subagent_tokens_since(repo, "abc", prior)
    assert usage.subagent_output == 33  # only the two new files, not the old one
    # With no prior snapshot, every file counts.
    assert claude_session.subagent_tokens_since(repo, "abc", set()).subagent_output == 38


def test_export_session_reads_jsonl_from_project_dir(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    project_dir = claude_session._project_dir(repo)
    project_dir.mkdir(parents=True)
    rows = [_user("u1", "hello"), _assistant("m1", "hi there", usage={"output_tokens": 3})]
    (project_dir / "abc.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    assert session_belongs_to_repo(repo, "abc")
    assert latest_session_id(repo) == "abc"
    session = export_session(repo, "abc")
    assert session is not None
    assert session.turns[0].user_prompt == "hello"
    assert session.turns[0].final_response == "hi there"


def test_list_sessions_returns_refs_with_labels(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    project_dir = claude_session._project_dir(repo)
    project_dir.mkdir(parents=True)
    (project_dir / "s1.jsonl").write_text(json.dumps(_user("u1", "first session prompt")) + "\n")
    (project_dir / "s2.jsonl").write_text(json.dumps(_user("u2", "second session prompt")) + "\n")

    refs = list_sessions(repo)
    by_id = {ref.id: ref for ref in refs}
    assert set(by_id) == {"s1", "s2"}
    assert by_id["s1"].label == "first session prompt"
    assert by_id["s2"].updated > 0
    assert latest_session_id(repo) in {"s1", "s2"}


def test_latest_session_id_skips_empty_resumed_sessions(tmp_path, monkeypatch):
    import os
    import time

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    project_dir = claude_session._project_dir(repo)
    project_dir.mkdir(parents=True)

    real = project_dir / "real.jsonl"
    real.write_text(json.dumps(_user("u1", "real prompt")) + "\n" + json.dumps(_assistant("m1", "ok")) + "\n")
    # Claude's resume/picker artifact: a session with no real user prompt.
    empty = project_dir / "empty.jsonl"
    empty.write_text(json.dumps(_assistant("m0", "")) + "\n")

    now = time.time()
    os.utime(real, (now - 100, now - 100))
    os.utime(empty, (now, now))  # the EMPTY one is newest by mtime

    # Newest by mtime is empty (nothing to resume) → pick the real conversation.
    assert latest_session_id(repo) == "real"


def test_latest_session_id_falls_back_to_recency_when_all_empty(tmp_path, monkeypatch):
    import os
    import time

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    project_dir = claude_session._project_dir(repo)
    project_dir.mkdir(parents=True)
    older = project_dir / "older.jsonl"
    older.write_text(json.dumps(_assistant("m0", "")) + "\n")
    newer = project_dir / "newer.jsonl"
    newer.write_text(json.dumps(_assistant("m1", "")) + "\n")

    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))

    # Nothing has content yet → fall back to plain recency.
    assert latest_session_id(repo) == "newer"


def test_list_worktree_sessions_aggregates_by_recency(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    worktrees_root = tmp_path / "repo" / ".agitrack" / "worktrees"
    worktrees_root.mkdir(parents=True)

    # Two worktree paths -> two encoded project dirs under Claude's projects root.
    alpha_dir = claude_session._project_dir(worktrees_root / "alpha")
    beta_dir = claude_session._project_dir(worktrees_root / "beta")
    alpha_dir.mkdir(parents=True)
    beta_dir.mkdir(parents=True)
    (alpha_dir / "sess-a.jsonl").write_text(json.dumps(_user("u1", "hello from alpha")) + "\n")
    (beta_dir / "sess-b.jsonl").write_text(json.dumps(_user("u2", "hello from beta")) + "\n")
    os.utime(beta_dir / "sess-b.jsonl", (time.time() + 10, time.time() + 10))  # beta is newer

    result = claude_session.list_worktree_sessions(worktrees_root)
    ids = [ref.id for _, ref in result]
    assert ids == ["sess-b", "sess-a"]  # newest first

    # The returned worktree key recreates the same project dir, so resuming from
    # the recreated worktree path finds the transcript again.
    key_by_id = {ref.id: key for key, ref in result}
    assert claude_session._project_dir(worktrees_root / key_by_id["sess-b"]) == beta_dir
    assert key_by_id["sess-b"] == "beta"


@pytest.mark.skipif(sys.platform == "win32", reason="Path('/...') on Windows includes drive letter, changing encoding")
def test_encode_repo_matches_claude_naming():
    # Claude names the project directory by replacing every non-alphanumeric
    # character of the absolute working directory with a dash.
    from pathlib import Path

    assert claude_session._encode_repo(Path("/a.b/c_d")) == "-a-b-c-d"


def test_parse_rows_records_session_updated_from_row_timestamps():
    # Issue #26 cleanup: `updated` was declared but never assigned, so every
    # Claude ExportedSession reported None. It now reflects the newest row.
    rows = [
        _user("u1", "first prompt"),
        _assistant("m1", "answer", usage={"input_tokens": 1, "output_tokens": 1}),
    ]
    rows[0]["timestamp"] = "2026-06-10T10:00:00.000Z"
    rows[1]["timestamp"] = "2026-06-10T10:05:30.000Z"

    session = parse_rows("sess-1", rows)

    from datetime import datetime, timezone

    expected = int(datetime(2026, 6, 10, 10, 5, 30, tzinfo=timezone.utc).timestamp())
    assert session.updated == expected


def test_parse_rows_updated_none_without_timestamps():
    rows = [_user("u1", "p"), _assistant("m1", "a", usage={})]
    assert parse_rows("sess-1", rows).updated is None


def test_parse_rows_records_turn_conversation_span():
    from datetime import datetime, timezone

    rows = [
        _user("u1", "first prompt"),
        _assistant("m1", "answer one", usage={"input_tokens": 1, "output_tokens": 1}),
        _user("u2", "second prompt"),
        _assistant("m2", "answer two", usage={"input_tokens": 1, "output_tokens": 1}),
    ]
    rows[0]["timestamp"] = "2026-06-10T10:00:00.000Z"
    rows[1]["timestamp"] = "2026-06-10T10:02:00.000Z"
    rows[2]["timestamp"] = "2026-06-10T10:05:00.000Z"
    rows[3]["timestamp"] = "2026-06-10T10:06:30.000Z"

    turns = parse_rows("sess-1", rows).turns
    epoch = lambda *a: int(datetime(*a, tzinfo=timezone.utc).timestamp())  # noqa: E731
    # Each turn spans its user prompt to its last assistant message.
    assert turns[0].started_at == epoch(2026, 6, 10, 10, 0, 0)
    assert turns[0].ended_at == epoch(2026, 6, 10, 10, 2, 0)
    assert turns[1].started_at == epoch(2026, 6, 10, 10, 5, 0)
    assert turns[1].ended_at == epoch(2026, 6, 10, 10, 6, 30)


# --- Esc interrupts: the turn completes and the marker is not a prompt ----------


def test_parse_rows_interrupt_marker_completes_turn_and_is_not_a_prompt():
    # Esc mid-tool-use leaves stop_reason=tool_use followed by a user row
    # "[Request interrupted by user]". The turn will never get more messages,
    # so it must parse as complete (or the commit gate defers forever), be
    # flagged interrupted (queued prompts were discarded), and the marker must
    # not become a turn of its own or pollute the subject/trace.
    rows = [
        _user("u1", "fix the parser"),
        _assistant(
            "m1", "Let me look at the file.", usage={"input_tokens": 5, "output_tokens": 5}, stop_reason="tool_use"
        ),
        _user("int-1", "[Request interrupted by user]"),
    ]

    session = parse_rows("sess-1", rows)

    assert len(session.turns) == 1
    turn = session.turns[0]
    assert turn.user_prompt == "fix the parser"
    assert turn.complete is True
    assert turn.interrupted is True


def test_parse_rows_tool_use_interrupt_variant_is_recognized():
    rows = [
        _user("u1", "do it"),
        _assistant("m1", "working", usage={}, stop_reason="tool_use"),
        _user("int-1", "[Request interrupted by user for tool use]"),
        _user("u2", "actually do something else"),
        _assistant("m2", "done", usage={}, stop_reason="end_turn"),
    ]

    session = parse_rows("sess-1", rows)

    assert [turn.user_prompt for turn in session.turns] == ["do it", "actually do something else"]
    assert session.turns[0].interrupted is True
    assert session.turns[0].complete is True
    assert session.turns[1].interrupted is False


def test_parse_rows_superseded_tool_use_turn_is_complete():
    # A turn flushed because a NEW prompt began can never receive more
    # messages — only the transcript's last (dangling) turn may be in-flight.
    rows = [
        _user("u1", "first"),
        _assistant("m1", "starting first", usage={}, stop_reason="tool_use"),
        _user("u2", "second"),
        _assistant("m2", "answering second", usage={}, stop_reason="tool_use"),
    ]

    session = parse_rows("sess-1", rows)

    assert session.turns[0].complete is True  # superseded: finished for good
    assert session.turns[1].complete is False  # dangling tool_use: still mid-flight
