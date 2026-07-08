"""Tests for ``agitrack --backtrace``: reconstructing past agent conversations (and the
file changes they made) from local Claude/OpenCode transcripts, with no git history.

The collector merges both backends, so most tests drive it through monkeypatched discovery
(no real filesystem/CLI); one end-to-end test plants a real Claude transcript in a plain,
non-git temp directory to prove the git-independence the feature promises.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from agitrack.backends.base import TokenUsage
from agitrack.metrics import backtrace as bt
from agitrack.metrics.web import aggregates_payload, format_html, log_page
from agitrack.transcripts import claude, opencode
from agitrack.transcripts.edits import make_edit
from agitrack.transcripts.types import ExportedSession, SessionRef, SessionTurn


# --------------------------------------------------------------------------- edits math


def test_make_edit_modified_counts_and_patch():
    edit = make_edit("pkg/mod.py", "a\nb\nc\n", "a\nB\nc\nd\n")
    assert edit is not None
    assert edit.path == "pkg/mod.py"
    # one line changed (b->B) is one insertion + one deletion; one appended line is +1.
    assert edit.insertions == 2 and edit.deletions == 1
    assert edit.patch.startswith("diff --git a/pkg/mod.py b/pkg/mod.py")
    assert "@@" in edit.patch


def test_make_edit_new_file_is_all_insertions():
    edit = make_edit("new.txt", "", "one\ntwo\n", status="added")
    assert edit is not None
    assert edit.insertions == 2 and edit.deletions == 0
    assert "new file mode" in edit.patch and "--- /dev/null" in edit.patch


def test_make_edit_noop_returns_none():
    assert make_edit("x.py", "same\n", "same\n") is None
    assert make_edit("", "a", "b") is None


# --------------------------------------------------------------------------- per-backend extraction


def test_claude_parse_rows_collects_edits():
    rows = [
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-07-08T10:00:00Z",
            "message": {"role": "user", "content": "edit it"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2026-07-08T10:00:05Z",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "claude-opus-4-8",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {"file_path": "/repo/a.py", "content": "x\ny\n"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Edit",
                        "input": {"file_path": "/repo/b.py", "old_string": "old\n", "new_string": "new\nmore\n"},
                    },
                    {"type": "text", "text": "done"},
                ],
            },
        },
    ]
    exported = claude.parse_rows("sess", rows, collect_edits=True)
    assert len(exported.turns) == 1
    edits = exported.turns[0].edits
    assert {e.path for e in edits} == {"/repo/a.py", "/repo/b.py"}
    assert sum(e.insertions for e in edits) == 4  # 2 (write) + 2 (edit adds new+more)

    # Without collect_edits the edits are NOT gathered (the hot path is unaffected).
    assert claude.parse_rows("sess", rows).turns[0].edits == []


def test_opencode_parse_exported_session_collects_edits():
    data = {
        "info": {"id": "ses_1", "time": {"updated": 1_700_000_000_000}},
        "messages": [
            {
                "info": {"id": "u1", "role": "user", "time": {"created": 1_700_000_000_000}},
                "parts": [{"type": "text", "text": "go"}],
            },
            {
                "info": {
                    "id": "a1",
                    "role": "assistant",
                    "time": {"created": 1_700_000_001_000},
                    "finish": "stop",
                    "model": {"providerID": "anthropic", "modelID": "claude"},
                },
                "parts": [
                    {
                        "type": "tool",
                        "tool": "write",
                        "state": {"input": {"filePath": "/repo/x.py", "content": "1\n2\n3\n"}},
                    },
                    {
                        "type": "tool",
                        "tool": "edit",
                        "state": {"input": {"filePath": "/repo/y.py", "oldString": "a\n", "newString": "b\n"}},
                    },
                    {"type": "text", "text": "done", "metadata": {"phase": "final_answer"}},
                ],
            },
        ],
    }
    exported = opencode.parse_exported_session(data, collect_edits=True)
    assert len(exported.turns) == 1
    edits = exported.turns[0].edits
    assert {e.path for e in edits} == {"/repo/x.py", "/repo/y.py"}
    assert sum(e.insertions for e in edits) == 4 and sum(e.deletions for e in edits) == 1
    assert opencode.parse_exported_session(data).turns[0].edits == []


# --------------------------------------------------------------------------- collector helpers


def _turn(
    prompt: str,
    *,
    edits=(),
    tokens=None,
    agent="done",
    assistant_id="a1",
    started=1000,
    ended=1005,
    model="claude-opus-4-8",
) -> SessionTurn:
    return SessionTurn(
        user_message_id="u",
        assistant_message_id=assistant_id,
        user_prompt=prompt,
        final_response=agent,
        tokens=tokens or TokenUsage(input=100, output=50),
        model=model,
        started_at=started,
        ended_at=ended,
        agent_messages=[agent] if agent else [],
        edits=list(edits),
    )


def _patch_discovery(monkeypatch, *, claude_sessions=None, opencode_sessions=None):
    """Make build_backtrace read from in-memory synthetic sessions instead of the real
    filesystem/OpenCode CLI. ``*_sessions`` map session id -> ExportedSession."""
    claude_sessions = claude_sessions or {}
    opencode_sessions = opencode_sessions or {}

    monkeypatch.setattr(
        claude,
        "sessions_under",
        lambda d: [
            (SessionRef(id=sid, updated=float(i)), Path(f"/fake/{sid}.jsonl")) for i, sid in enumerate(claude_sessions)
        ],
    )
    monkeypatch.setattr(claude, "_first_cwd", lambda p: "/repo")
    monkeypatch.setattr(
        claude, "export_session_at", lambda path, collect_edits=False: claude_sessions.get(Path(path).stem)
    )
    monkeypatch.setattr(
        opencode,
        "sessions_under",
        lambda d: [(SessionRef(id=sid, updated=float(i)), "/repo") for i, sid in enumerate(opencode_sessions)],
    )
    monkeypatch.setattr(opencode, "export_session", lambda repo, sid, collect_edits=False: opencode_sessions.get(sid))


def test_build_backtrace_merges_both_backends(monkeypatch, tmp_path):
    edit = make_edit("/repo/a.py", "", "x\ny\n", status="added")
    claude_es = ExportedSession(
        session_id="c1", model="claude-opus-4-8", updated=2000, turns=[_turn("do a claude thing", edits=[edit])]
    )
    oc_es = ExportedSession(
        session_id="o1", model="gpt-5.5", updated=2000, turns=[_turn("do an opencode thing", model="gpt-5.5")]
    )
    _patch_discovery(monkeypatch, claude_sessions={"c1": claude_es}, opencode_sessions={"o1": oc_es})

    view = bt.build_backtrace(tmp_path)
    assert not view.is_empty
    assert view.session_count == 2 and view.edited_sessions == 1
    assert view.backends == ["claude", "opencode"]
    assert view.dashboard.total_commits == 2
    assert set(view.dashboard.by_backend) == {"claude", "opencode"}
    # the claude turn's edit shows up as tracked-AI lines and a diff entry
    assert view.dashboard.ai_lines == (2, 0)
    edited = next(s for s in view.dashboard.stats if s.insertions)
    assert edited.sha in view.diffs and view.diffs[edited.sha].startswith("diff --git a/a.py b/a.py")


def test_backtrace_tokens_exclude_total_and_context():
    turn = _turn("p", tokens=TokenUsage(input=10, output=20, cache_read=3))
    tokens = bt._tokens_dict(turn)
    assert tokens == {"input": 10, "output": 20, "cache_read": 3}
    assert "total" not in tokens and "context" not in tokens


def test_backtrace_message_carries_interaction_trace():
    turn = _turn("please refactor the parser", agent="I refactored it.")
    message = bt._message(turn)
    assert "# Interaction Trace" in message
    assert "## User" in message and "please refactor the parser" in message
    assert "## Agent" in message and "I refactored it." in message


def test_backtrace_relativizes_edit_paths():
    edit = make_edit("/repo/pkg/mod.py", "a\n", "b\n")
    rel = bt._relativize(edit, ["/repo"])
    assert rel.path == "pkg/mod.py"
    assert "a/pkg/mod.py" in rel.patch and "/repo/pkg" not in rel.patch


def test_backtrace_empty_directory(monkeypatch, tmp_path):
    _patch_discovery(monkeypatch)  # no sessions on either backend
    view = bt.build_backtrace(tmp_path)
    assert view.is_empty
    text = bt.render_backtrace_text(tmp_path)
    assert "No local coding-agent history found" in text


# --------------------------------------------------------------------------- serving surfaces


def test_backtrace_html_and_endpoints(monkeypatch, tmp_path):
    edit = make_edit("/repo/a.py", "", "x\ny\n", status="added")
    es = ExportedSession(
        session_id="c1", model="claude-opus-4-8", updated=2000, turns=[_turn("build the thing", edits=[edit])]
    )
    _patch_discovery(monkeypatch, claude_sessions={"c1": es})
    view = bt.build_backtrace(tmp_path)

    html = format_html(view.dashboard, banner_html=bt._banner_html(view), backtrace=True)
    assert "BACKTRACE" in html and "updatebanner" in html
    assert "__UPDATE_BANNER__" not in html and "__DATA__" not in html

    data = aggregates_payload(view.dashboard)
    assert data["agg"]["total"] == 1
    page = log_page(view.dashboard, offset=0, limit=10)
    entry = page["entries"][0]
    # the virtual sha is a hex object-id-looking string so the front-end offers the diff button
    assert re.fullmatch(r"[0-9a-f]{40}", entry["sha"])
    assert entry["message"] and "# Interaction Trace" in entry["message"]
    assert view.diffs[entry["sha"]].startswith("diff --git a/a.py b/a.py")


# --------------------------------------------------------------------------- end-to-end, non-git


def test_backtrace_end_to_end_non_git_claude(monkeypatch, tmp_path):
    """Plant a real Claude transcript for a plain (non-git) directory and reconstruct it —
    proving the feature works with no git repo and no prior aGiTrack use."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    # OpenCode discovery would shell out to its CLI; skip it in this filesystem-only test.
    monkeypatch.setattr(opencode, "sessions_under", lambda d: [])

    workdir = tmp_path / "myproject"  # NOT a git repo
    workdir.mkdir()
    project = home / ".claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(workdir.resolve()))
    project.mkdir(parents=True)
    rows = [
        {
            "type": "user",
            "uuid": "u1",
            "cwd": str(workdir),
            "timestamp": "2026-07-08T10:00:00Z",
            "message": {"role": "user", "content": "Create hello.py"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "cwd": str(workdir),
            "timestamp": "2026-07-08T10:00:05Z",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "claude-opus-4-8",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 120, "output_tokens": 60},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {"file_path": str(workdir / "hello.py"), "content": 'print("hi")\n'},
                    },
                    {"type": "text", "text": "Created hello.py."},
                ],
            },
        },
    ]
    (project / "abc123.jsonl").write_text("\n".join(json.dumps(r) for r in rows))

    view = bt.build_backtrace(workdir)
    assert not view.is_empty
    assert view.session_count == 1 and view.edited_sessions == 1
    assert view.dashboard.total_commits == 1
    stat = view.dashboard.stats[0]
    assert stat.backend == "claude" and stat.model == "claude-opus-4-8"
    assert stat.insertions == 1 and stat.deletions == 0
    assert stat.tokens.get("input") == 120 and stat.tokens.get("output") == 60
    # path relativized to the directory, trace present, diff served
    assert view.diffs[stat.sha].startswith("diff --git a/hello.py b/hello.py")
    assert "Create hello.py" in stat.message and "## Agent" in stat.message
