"""Consolidating a project's sessions from several machines into one backtrace.

A backtrace used to reconstruct only what it could find on the machine running it, and had no
notion of WHO did any of it — a transcript records no git author, so every turn was attributed
to nobody. That makes it a view of one laptop, not of a project.

Two pieces close that. `agitrack --share-sessions` pushes every local session to `origin` in one
go (:mod:`agitrack.sessions.bulk`), and the backtrace pulls the shared ones back, attributing
each to the GitHub id it was shared under. The end state, verified live against a private GitHub
repo: a fresh clone with no local transcripts at all reconstructs the whole project's history,
and both machines agree on who did what.

The rules that keep the merge honest are what these tests pin:

* A session present BOTH locally and on origin is counted once, and the local copy wins — it is
  never capped for transport, and its turns emit the same virtual shas, so a duplicate would
  show as repeated rows.
* A shared session's edits are relative to the SHARER's checkout root, not the viewer's. Against
  the wrong root every path fails to relativize and is silently dropped, which read as a
  collaborator having contributed no lines to files they had plainly written.
* Local sessions are attributed to the viewer only when there is someone else in the view;
  a single-machine backtrace shows no authors and must not pay for a `gh` lookup to say so.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from agitrack.git import GitRepo
from agitrack.metrics import backtrace as bt
from agitrack.sessions import SharedSessionStore
from agitrack.sessions.bulk import discover_local_sessions, share_all


def _repo(tmp_path: Path) -> GitRepo:
    import subprocess

    root = tmp_path / "proj"
    root.mkdir()
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "dev@example.com"],
        ["git", "config", "user.name", "dev"],
    ):
        subprocess.run(cmd, cwd=root, check=True)
    (root / "README.md").write_text("# proj\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return GitRepo(root)


def _claude_transcript(cwd: str, *, file_path: str, text: str, msg_id: str) -> str:
    rows = [
        {
            "type": "user",
            "uuid": "u1",
            "cwd": cwd,
            "message": {"role": "user", "content": "write the doc"},
            "timestamp": "2026-08-06T09:00:00.000Z",
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "message": {
                "role": "assistant",
                "id": msg_id,
                "model": "claude-opus-5",
                "stop_reason": "end_turn",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {"file_path": file_path, "content": text},
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
            "timestamp": "2026-08-06T09:00:30.000Z",
        },
        {
            "type": "assistant",
            "uuid": "a2",
            "parentUuid": "a1",
            "message": {
                "role": "assistant",
                "id": msg_id + "-final",
                "model": "claude-opus-5",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
            "timestamp": "2026-08-06T09:00:45.000Z",
        },
    ]
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def _publish(repo: GitRepo, *, owner: str, name: str, session_id: str, transcript: str) -> None:
    store = SharedSessionStore(repo)
    store.publish(
        github_id=owner,
        name=name,
        transcript=transcript,
        manifest={
            "github_id": owner,
            "name": name,
            "contributors": [owner],
            "backend": "claude",
            "session_id": session_id,
            "updated": int(time.time()),
            "transcript_rows": transcript.count("\n"),
        },
    )


def test_a_shared_session_is_reconstructed_and_attributed_to_its_sharer(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _publish(
        repo,
        owner="dana-eng",
        name="contributing-docs",
        session_id="remote-1",
        transcript=_claude_transcript(
            "/home/dana/proj", file_path="/home/dana/proj/CONTRIBUTING.md", text="a\nb\nc\n", msg_id="msg_dana"
        ),
    )
    monkeypatch.setattr(bt, "_state_dir", lambda: tmp_path / "state")

    sources = bt._remote_sources(Path(repo.repo), local_ids=set())

    assert [s.ref_id for s in sources] == ["remote-1"]
    assert sources[0].owner == "dana-eng"
    assert sources[0].shared is True
    # The sharer's own checkout root, so their absolute edit paths still relativize.
    assert sources[0].base_dir == "/home/dana/proj"


def test_a_session_present_locally_is_not_pulled_again_from_origin(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _publish(
        repo,
        owner="dana-eng",
        name="dupe",
        session_id="both-1",
        transcript=_claude_transcript("/home/dana/proj", file_path="/home/dana/proj/x.md", text="x\n", msg_id="m"),
    )
    monkeypatch.setattr(bt, "_state_dir", lambda: tmp_path / "state")

    assert bt._remote_sources(Path(repo.repo), local_ids={"both-1"}) == []


def test_a_shared_session_contributes_its_edits_and_its_author(tmp_path, monkeypatch):
    # End to end through the view: the collaborator's lines are counted (they relativize against
    # THEIR root) and the turn carries their id as its author.
    repo = _repo(tmp_path)
    _publish(
        repo,
        owner="dana-eng",
        name="docs",
        session_id="remote-2",
        transcript=_claude_transcript(
            "/home/dana/proj", file_path="/home/dana/proj/CONTRIBUTING.md", text="one\ntwo\nthree\n", msg_id="msg_d2"
        ),
    )
    monkeypatch.setattr(bt, "_state_dir", lambda: tmp_path / "state")

    view = bt.build_backtrace(Path(repo.repo))

    assert view.shared_sessions == 1
    assert view.contributors == ["dana-eng"]
    assert [stat.author for stat in view.dashboard.stats] == ["dana-eng"]
    assert sum(stat.insertions for stat in view.dashboard.stats) == 3


def test_a_single_machine_backtrace_names_nobody(tmp_path, monkeypatch):
    # No shared sessions ⇒ no author chrome, and no identity lookup to produce one.
    repo = _repo(tmp_path)
    monkeypatch.setattr(bt, "_state_dir", lambda: tmp_path / "state")
    called: list = []
    monkeypatch.setattr(bt, "_local_owner", lambda directory: called.append(directory) or "should-not-be-used")

    view = bt.build_backtrace(Path(repo.repo))

    assert view.shared_sessions == 0
    assert view.contributors == []
    assert called == [], "resolved a GitHub login for a backtrace that shows no authors"


def test_bulk_share_publishes_every_local_session_then_skips_unchanged(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    root = Path(repo.repo)
    from agitrack.transcripts.types import SessionRef

    made = {
        "s-one": _claude_transcript(str(root), file_path=str(root / "a.md"), text="a\n", msg_id="m1"),
        "s-two": _claude_transcript(str(root), file_path=str(root / "b.md"), text="b\n", msg_id="m2"),
    }
    store_dir = tmp_path / "transcripts"
    store_dir.mkdir()
    listed = []
    for sid, text in made.items():
        path = store_dir / f"{sid}.jsonl"
        path.write_text(text)
        listed.append((SessionRef(id=sid, updated=100.0, label="work"), path))
    monkeypatch.setattr("agitrack.transcripts.claude.sessions_under", lambda directory: listed)
    monkeypatch.setattr("agitrack.transcripts.opencode.sessions_under", lambda directory: [])
    monkeypatch.setattr("agitrack.sessions.bulk.github_login", lambda repo=None: "shiqiangw")

    assert {c.session_id for c in discover_local_sessions(root)} == {"s-one", "s-two"}

    first = share_all(repo)
    assert [o.status for o in first.outcomes] == ["shared", "shared"]

    # Re-running must be cheap and idempotent, not two more pushes of identical blobs.
    second = share_all(repo)
    assert [o.status for o in second.outcomes] == ["unchanged", "unchanged"]
    assert second.shared == []
