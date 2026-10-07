"""A conversation's turns go to the repositories they edited (agitrack/routing.py).

One agent conversation routinely edits the repository it was started in, a repository nested
inside it, and a sibling checkout. Each repository's commits record the turns that changed it:
its own conversations' turns, minus those that only edited OTHER tracked repositories, plus the
turns of conversations started elsewhere that edited it.

Real git repositories throughout, and real-shaped Claude transcripts read through the real
parser (``CLAUDE_CONFIG_DIR`` points at the test's own directory).
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agitrack import routing
from agitrack.backends.base import TokenUsage
from agitrack.backends.proxy_agents import make_proxy_agent
from agitrack.config import AgitrackState
from agitrack.config.settings import GlobalConfig
from agitrack.git import GitRepo
from agitrack.proxy.background import BackgroundRunner
from agitrack.transcripts import claude
from agitrack.transcripts.types import FileEdit, SessionTurn


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "README.md").write_text(f"{path.name}\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return path.resolve()


def _turn(prompt: str, *edited: Path, started: float | None = None) -> SessionTurn:
    turn = SessionTurn("u-" + prompt, "a-" + prompt, prompt, "done", TokenUsage(total=3, output=1), "m")
    turn.edits = [FileEdit(path=str(path), insertions=1, deletions=0) for path in edited]
    turn.started_at = started if started is not None else time.time()
    return turn


@pytest.fixture(autouse=True)
def _only_this_tests_conversations(tmp_path, monkeypatch):
    """Every backend's store points at this test's own (empty) directories, so discovering
    "conversations started elsewhere" can never reach the developer's real ones."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("AGITRACK_XDG_DATA_HOME", str(tmp_path / "xdg-data"))


@pytest.fixture
def layout(tmp_path):
    """parent/ (a repo) holding inner/ (its own repo), and a sibling repo beside it."""
    parent = _repo(tmp_path / "parent")
    inner = _repo(parent / "inner")
    sibling = _repo(tmp_path / "sibling")
    scratch = (tmp_path / "scratch").resolve()
    scratch.mkdir()
    return parent, inner, sibling, scratch


def test_a_file_belongs_to_the_nearest_repository(layout):
    parent, inner, sibling, scratch = layout
    assert routing.repo_root_of(str(parent / "a.txt")) == str(parent)
    assert routing.repo_root_of(str(inner / "deep" / "b.txt")) == str(inner)
    assert routing.repo_root_of(str(sibling / "c.txt")) == str(sibling)
    assert routing.repo_root_of(str(scratch / "notes.md")) is None


def test_home_keeps_its_own_work_talk_and_anything_it_cannot_hand_over(layout, monkeypatch):
    parent, inner, sibling, scratch = layout
    monkeypatch.setattr(routing, "is_tracked", lambda root: root == str(inner))
    keep = routing.Router(str(parent)).home_filter(str(parent))

    assert keep(_turn("its own edit", parent / "a.txt"))
    assert keep(_turn("a question, no edits"))
    assert keep(_turn("a scratch file outside any repository", scratch / "x"))
    assert keep(_turn("both here and in the nested repo", parent / "a.txt", inner / "b.txt"))
    # Only the nested repo, which is tracked: it records this turn, so the parent does not.
    assert not keep(_turn("only the nested repo", inner / "b.txt"))
    # The sibling is NOT tracked: nobody else would record the turn, so the parent keeps it
    # rather than let the trace fall on the floor.
    assert keep(_turn("only the untracked sibling", sibling / "c.txt"))


def test_a_running_turn_is_not_placed_until_it_finishes(layout, monkeypatch):
    parent, inner, _sibling, _scratch = layout
    monkeypatch.setattr(routing, "is_tracked", lambda root: True)
    running = _turn("still going", inner / "b.txt")
    running.complete = False
    assert routing.Router(str(parent)).home_filter(str(parent))(running)


def test_a_repository_takes_exactly_the_turns_that_edited_it_from_elsewhere(layout):
    parent, inner, sibling, _scratch = layout
    floor = time.time() - 60
    keep = routing.Router(str(inner)).foreign_filter(str(parent), floor=floor)

    assert keep(_turn("edits the nested repo", inner / "b.txt"))
    assert not keep(_turn("edits only the parent", parent / "a.txt"))
    assert not keep(_turn("a question"))
    # History from before routing began here is never claimed retroactively.
    assert not keep(_turn("old work", inner / "b.txt", started=floor - 3600))
    # A relative path is resolved against the conversation's own folder.
    relative = SessionTurn("u", "a", "relative", "done", TokenUsage(), "m")
    relative.edits = [FileEdit(path="inner/b.txt", insertions=1, deletions=0)]
    relative.started_at = time.time()
    assert keep(relative)


# --------------------------------------------------------------------------- end to end


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _write_claude_session(config: Path, cwd: Path, session_id: str, turns: list[tuple[str, Path | None]]) -> Path:
    project = config / "projects" / claude._encode_repo(cwd)
    project.mkdir(parents=True, exist_ok=True)
    rows = []
    base = time.time() - 30
    for index, (prompt, target) in enumerate(turns):
        at = base + index * 5
        rows.append(
            {
                "type": "user",
                "uuid": f"u{index}",
                "parentUuid": f"a{index - 1}" if index else None,
                "sessionId": session_id,
                "cwd": str(cwd),
                "timestamp": _iso(at),
                "message": {"role": "user", "content": prompt},
            }
        )
        content: list[dict] = []
        if target is not None:
            content.append(
                {
                    "type": "tool_use",
                    "id": f"t{index}",
                    "name": "Write",
                    "input": {"file_path": str(target), "content": f"{prompt}\n"},
                }
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"{prompt}\n", encoding="utf-8")  # the edit really landed
        content.append({"type": "text", "text": f"Done: {prompt}"})
        rows.append(
            {
                "type": "assistant",
                "uuid": f"a{index}",
                "parentUuid": f"u{index}",
                "sessionId": session_id,
                "cwd": str(cwd),
                "timestamp": _iso(at + 2),
                "message": {
                    "id": f"msg{index}",
                    "role": "assistant",
                    "model": "claude-opus-5",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "content": content,
                },
            }
        )
    path = project / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _tracker(root: Path, tmp_path: Path) -> BackgroundRunner:
    state = AgitrackState(root, default_backend="claude")
    state.data["routing_since"] = time.time() - 3600  # routing began before these turns
    runner = BackgroundRunner(
        GitRepo(root),
        manual_commits=True,
        _global_config=GlobalConfig(path=tmp_path / f"{root.name}.json"),
        _state=state,
    )
    runner.backend = make_proxy_agent("claude")
    runner._make_summarizer = lambda: None
    runner._summarization_enabled = lambda: False
    runner._manual.setup()
    return runner


def _pending(runner: BackgroundRunner) -> str:
    return "\n".join(runner._manual.pending_bodies())


@pytest.mark.routing
def test_one_conversation_is_split_between_the_parent_and_the_nested_repo(layout, tmp_path, monkeypatch):
    """The reported case: Claude, started in the parent, edits the parent AND a repository
    nested inside it in the same conversation. Each repository's record carries exactly the
    turns that changed it."""
    parent, inner, _sibling, _scratch = layout
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    claude._EXPORTS.clear()
    claude._HEAD_CACHE.clear()
    _write_claude_session(
        config,
        parent,
        "11111111-1111-1111-1111-111111111111",
        [
            ("update the parent readme", parent / "notes.md"),
            ("fix the nested library", inner / "lib.py"),
            ("and what does that change?", None),
        ],
    )
    monkeypatch.setattr(routing, "is_tracked", lambda root: root in {str(parent), str(inner)})

    inner_tracker = _tracker(inner, tmp_path)
    parent_tracker = _tracker(parent, tmp_path)
    assert inner_tracker._process_once(require_complete=False, at_commit=True)
    assert parent_tracker._process_once(require_complete=False, at_commit=True)

    in_inner, in_parent = _pending(inner_tracker), _pending(parent_tracker)
    assert "fix the nested library" in in_inner
    assert "update the parent readme" not in in_inner
    assert "update the parent readme" in in_parent
    assert "fix the nested library" not in in_parent
    # The nested repo's record says where the conversation came from.
    assert "backend_session_id: 11111111-1111-1111-1111-111111111111" in in_inner

    # Polling again records nothing twice: each repository keeps its own watermark.
    inner_count = inner_tracker._manual.pending_count()
    routing._PROCESSED.clear()  # make the second poll really re-read the conversation
    inner_tracker._process_once(require_complete=False, at_commit=True)
    assert inner_tracker._manual.pending_count() == inner_count
    # ...and the nested repo still tracks no conversation of its own.
    assert inner_tracker.state.backend_session_id is None


@pytest.mark.routing
def test_a_sibling_repository_takes_the_turns_that_edited_it(layout, tmp_path, monkeypatch):
    """A conversation started in one checkout editing ANOTHER (not nested, not a parent): the
    other repository finds it because the conversation names its path."""
    parent, _inner, sibling, _scratch = layout
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    claude._EXPORTS.clear()
    claude._HEAD_CACHE.clear()
    _write_claude_session(
        config,
        parent,
        "22222222-2222-2222-2222-222222222222",
        [("port the helper to the sibling", sibling / "helper.py"), ("tidy the parent", parent / "x.md")],
    )
    monkeypatch.setattr(routing, "is_tracked", lambda root: True)

    sibling_tracker = _tracker(sibling, tmp_path)
    assert sibling_tracker._process_once(require_complete=False, at_commit=True)

    recorded = _pending(sibling_tracker)
    assert "port the helper to the sibling" in recorded
    assert "tidy the parent" not in recorded
