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

import pytest

from agitrack.git import GitRepo
from agitrack.metrics import backtrace as bt
from agitrack.sessions import SharedSessionStore
from agitrack.sessions.bulk import discover_local_sessions, share_all
from agitrack.transcripts import codex


@pytest.fixture(autouse=True)
def codex_home(tmp_path, monkeypatch):
    """Redirect Codex's store, as tests/test_codex_session.py does.

    Both the local discovery pass and the shared-transcript exporter read ``$CODEX_HOME``
    (defaulting to ``~/.codex``), so without this a test that merely built a backtrace of a temp
    repo scanned — and reconstructed from — the developer's own Codex history.
    """
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


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


def _codex_transcript(cwd: str, *, file_path: str, text: str, msg_id: str) -> str:
    """A Codex rollout — the transcript Codex shares, verbatim (its own on-disk JSONL).

    Same shape as the records tests/test_codex_session.py captured from codex-cli 0.147.0: the
    ``session_meta`` header carries the cwd a reconstruction must relativize against, and the
    applied patch is what the turn's edits are recovered from.
    """
    rows = [
        {
            "timestamp": "2026-08-06T09:00:00.000Z",
            "type": "session_meta",
            "payload": {"session_id": msg_id, "cwd": cwd, "source": "cli", "thread_source": "user"},
        },
        {
            "timestamp": "2026-08-06T09:00:01.000Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "t1"},
        },
        {
            "timestamp": "2026-08-06T09:00:02.000Z",
            "type": "turn_context",
            "payload": {"turn_id": "t1", "model": "gpt-5.4-codex", "cwd": cwd},
        },
        {
            "timestamp": "2026-08-06T09:00:03.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "write the doc"},
        },
        {
            "timestamp": "2026-08-06T09:00:30.000Z",
            "type": "event_msg",
            "payload": {
                "type": "patch_apply_end",
                "success": True,
                "changes": {file_path: {"type": "add", "content": text}},
            },
        },
        {
            "timestamp": "2026-08-06T09:00:40.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 10, "output_tokens": 5}},
            },
        },
        {
            "timestamp": "2026-08-06T09:00:45.000Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "t1", "last_agent_message": "done"},
        },
    ]
    return "".join(json.dumps(row) + "\n" for row in rows)


# The transcript each backend shares, and an id shaped the way that backend issues them (Codex
# resolves a conversation by the UUID in its rollout file name, so a shared Codex session must
# carry a real UUID or nothing on the receiving side can file it).
_SHARED = {
    "claude": (_claude_transcript, "remote-claude-1"),
    "codex": (_codex_transcript, "019fe8dc-ca6c-7951-9225-73513aadf083"),
}


def _publish(
    repo: GitRepo, *, owner: str, name: str, session_id: str, transcript: str, backend: str = "claude"
) -> None:
    store = SharedSessionStore(repo)
    store.publish(
        github_id=owner,
        name=name,
        transcript=transcript,
        manifest={
            "github_id": owner,
            "name": name,
            "contributors": [owner],
            "backend": backend,
            "session_id": session_id,
            "updated": int(time.time()),
            "transcript_rows": transcript.count("\n"),
        },
    )


@pytest.mark.parametrize("backend", sorted(_SHARED))
def test_a_shared_session_is_reconstructed_and_attributed_to_its_sharer(tmp_path, monkeypatch, backend):
    repo = _repo(tmp_path)
    make_transcript, session_id = _SHARED[backend]
    _publish(
        repo,
        owner="dana-eng",
        name="contributing-docs",
        session_id=session_id,
        backend=backend,
        transcript=make_transcript(
            "/home/dana/proj", file_path="/home/dana/proj/CONTRIBUTING.md", text="a\nb\nc\n", msg_id=session_id
        ),
    )
    monkeypatch.setattr(bt, "_state_dir", lambda: tmp_path / "state")

    sources = bt._remote_sources(Path(repo.repo), local_ids=set())

    assert [s.ref_id for s in sources] == [session_id]
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


@pytest.mark.parametrize("backend", sorted(_SHARED))
def test_a_shared_session_contributes_its_edits_and_its_author(tmp_path, monkeypatch, backend):
    # End to end through the view: the collaborator's lines are counted (they relativize against
    # THEIR root) and the turn carries their id as its author.
    repo = _repo(tmp_path)
    make_transcript, session_id = _SHARED[backend]
    _publish(
        repo,
        owner="dana-eng",
        name="docs",
        session_id=session_id,
        backend=backend,
        transcript=make_transcript(
            "/home/dana/proj",
            file_path="/home/dana/proj/CONTRIBUTING.md",
            text="one\ntwo\nthree\n",
            msg_id=session_id,
        ),
    )
    monkeypatch.setattr(bt, "_state_dir", lambda: tmp_path / "state")

    view = bt.build_backtrace(Path(repo.repo))

    assert view.shared_sessions == 1
    assert view.backends == [backend]
    assert view.contributors == ["dana-eng"]
    assert [stat.author for stat in view.dashboard.stats] == ["dana-eng"]
    assert sum(stat.insertions for stat in view.dashboard.stats) == 3
    # The shared session's id reaches the reconstructed metadata: it is what ties a turn back to
    # the conversation it came from, and what its virtual sha is derived from. (Each backend
    # recovers it from the cached transcript's PATH — Claude from the file stem, Codex from the
    # uuid in its rollout name — so the recorded id embeds, rather than equals, the shared id.)
    anchor = next(
        line for line in view.dashboard.stats[0].message.splitlines() if line.startswith("backend_session_id:")
    )
    assert session_id in anchor


def test_a_shared_codex_transcript_is_cached_under_a_name_codex_can_read(tmp_path):
    """A materialized Codex share must be named like the rollout it IS.

    ``codex.export_session_at`` takes the conversation's id off the rollout FILE NAME and only
    falls back to the ``session_meta`` header — so cached as ``<owner>--<id>.json`` the id rested
    entirely on that fallback. A transport-capped share (or a header a future Codex stops
    emitting) then exports with an EMPTY session id, which collides every one of its turns'
    virtual shas with the next such session's (a sha is ``backend:session:index:message``) and
    drops ``backend_session_id`` from the reconstructed metadata.
    """
    session_id = "019fe8dc-ca6c-7951-9225-73513aadf083"
    full = _codex_transcript("/home/dana/proj", file_path="/x.md", text="a\n", msg_id=session_id)
    # Dropped `session_meta` line: a transcript trimmed for transport can start after the header,
    # and that is precisely the case the fallback has no answer for.
    transcript = "".join(line + "\n" for line in full.splitlines()[1:])
    cache = tmp_path / "cache"
    cache.mkdir()

    legacy = cache / f"dana-eng--{session_id}.json"  # what the cache used to be named
    legacy.write_text(transcript, encoding="utf-8")
    assert codex.export_session_at(legacy).session_id == ""  # …and the id was simply lost

    class _Entry:
        github_id = "dana-eng"

    class _Store:
        def read_transcript(self, entry):
            return transcript

    path = bt._materialize_shared(_Store(), _Entry(), cache, "codex", session_id)

    assert path is not None and path.suffix == ".jsonl"  # it is a rollout, not a JSON export
    assert codex._id_from_path(path) == session_id  # recovered from the PATH alone
    exported = bt._shared_export("codex", path)()
    assert exported is not None and exported.session_id == session_id
    assert exported.turns[0].user_prompt == "write the doc"


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


def test_bulk_share_publishes_every_local_session_then_skips_unchanged(tmp_path, monkeypatch, codex_home):
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
    # A Codex conversation for the same repo, discovered the way Codex is really discovered:
    # from a rollout under $CODEX_HOME whose header records this repo as its cwd. A bulk share
    # that only walked the other backends would leave a machine's Codex work unshared, and the
    # backtraces built from origin would be missing it on every OTHER machine.
    codex_id = "019fe8dc-ca6c-7951-9225-73513aadf083"
    rollout_dir = codex_home / "sessions" / "2026" / "08" / "06"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / f"rollout-2026-08-06T09-00-00-{codex_id}.jsonl").write_text(
        _codex_transcript(str(root), file_path=str(root / "c.md"), text="c\n", msg_id=codex_id), encoding="utf-8"
    )
    monkeypatch.setattr("agitrack.transcripts.claude.sessions_under", lambda directory: listed)
    monkeypatch.setattr("agitrack.transcripts.opencode.sessions_under", lambda directory: [])
    monkeypatch.setattr("agitrack.sessions.bulk.github_login", lambda repo=None: "shiqiangw")

    found = {c.session_id: c.backend for c in discover_local_sessions(root)}
    assert found == {"s-one": "claude", "s-two": "claude", codex_id: "codex"}

    first = share_all(repo)
    assert [o.status for o in first.outcomes] == ["shared", "shared", "shared"]

    # Re-running must be cheap and idempotent, not three more pushes of identical blobs.
    second = share_all(repo)
    assert [o.status for o in second.outcomes] == ["unchanged", "unchanged", "unchanged"]
    assert second.shared == []
