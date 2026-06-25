"""Sharing full agent sessions via git (issue #55).

Real temp repos exercise the history-free shared-session ref, redaction, identity
resolution, the Claude transcript import/export, and a push/fetch round-trip
through a local bare remote.
"""

import json
import random
import string
import subprocess
from pathlib import Path

from agitrack.git import GitRepo
from agitrack.sessions import SharedSessionStore, github_login, redact_transcript
from agitrack.sessions.identity import slug


def _fake_token(prefix: str, n: int, *, charset: str = string.ascii_letters + string.digits) -> str:
    """A secret-SHAPED string assembled at runtime. Keeping no full literal secret in this
    file is deliberate: a hard-coded dummy token (a real-looking ``AIza…``/``SG.…``) trips
    GitHub's own secret-scanning push protection and blocks the test file from being pushed —
    the very failure these tests guard against. Only the (non-secret) prefix is literal; the
    body is random, so every regex still matches but nothing here is a scannable secret."""
    return prefix + "".join(random.choice(charset) for _ in range(n))


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


def _manifest(name, *, session_id, updated, model="claude-opus-4-8"):
    return {"github_id": "alice", "name": name, "session_id": session_id, "updated": updated, "model": model}


def _drain_shared_resume(runner):
    # The transcript fetch + import run on a worker thread; the resume completes on
    # the main loop's _service_shared_resume(). Drain both for the test.
    if runner._shared_resume_thread is not None:
        runner._shared_resume_thread.join(timeout=10)
    runner._service_shared_resume()


def _row(uuid, **extra):
    return json.dumps({"uuid": uuid, **extra})


def test_merge_transcripts_unions_divergent_copies():
    from agitrack.sessions.store import merge_transcripts

    base = _row("a") + "\n" + _row("b") + "\n"
    mine = base + _row("c1") + "\n"  # I appended c1
    theirs = base + _row("c2") + "\n"  # a collaborator appended c2

    merged = merge_transcripts(mine, theirs)
    ids = [json.loads(r)["uuid"] for r in merged.splitlines() if r.strip()]
    assert ids == ["a", "b", "c1", "c2"]  # shared prefix, then mine's tail, then theirs

    # Idempotent: re-merging a copy that already has the other's rows changes nothing.
    assert merge_transcripts(mine, merged) == merged
    assert merge_transcripts(merged, theirs) == merged


def test_merge_transcripts_falls_back_when_not_mergeable():
    from agitrack.sessions.store import merge_transcripts

    # Different first-row id → a different conversation → last-write-wins.
    assert merge_transcripts(_row("x1") + "\n", _row("y1") + "\n") == _row("x1") + "\n"
    # No per-row id (OpenCode-style single object) → last-write-wins.
    assert merge_transcripts('{"info":{},"messages":[]}', '{"info":{"a":1}}') == '{"info":{},"messages":[]}'
    # Empty sides degrade to the non-empty one.
    assert merge_transcripts("x", "") == "x"
    assert merge_transcripts("", "y") == "y"


def _claude_user(uuid, text):
    return json.dumps({"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}})


def _claude_assistant(uuid, text):
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "message": {"id": uuid, "role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    )


def test_publish_merges_divergent_local_entry(tmp_path):
    # When the local ref already holds a diverged copy of the same session (e.g. a
    # collaborator's version fetched after losing the push race), the store folds both
    # sides' turns together instead of overwriting one with the other. Real Claude rows
    # so the merge passes the readability guard.
    from agitrack.sessions.store import DEFAULT_KEEP

    repo = _init_repo(tmp_path)
    store = SharedSessionStore(repo)
    base = _claude_user("u1", "hi") + "\n" + _claude_assistant("a1", "hello") + "\n"
    theirs = base + _claude_user("u3", "theirs") + "\n" + _claude_assistant("a3", "sure") + "\n"
    mine = base + _claude_user("u2", "mine") + "\n" + _claude_assistant("a2", "ok") + "\n"
    manifest = {"session_id": "sid", "content_hash": "h", "backend": "claude"}
    store._add_session("alice", "s", theirs, manifest)

    result = store._add_and_push("alice", "s", mine, manifest, DEFAULT_KEEP)

    stored = store.repo.read_ref_blob(store.ref, f"{store._prefix()}alice/s/transcript.jsonl")
    ids = [json.loads(r)["uuid"] for r in stored.splitlines() if r.strip()]
    assert ids == ["u1", "a1", "u2", "a2", "u3", "a3"]  # union, nothing lost
    assert result.merged == 2  # the collaborator's two rows folded in


def test_publish_skips_unreadable_merge(tmp_path, monkeypatch):
    # If a merge would produce a transcript the backend can't load, the store falls
    # back to last-write-wins rather than uploading a broken session.
    import agitrack.sessions.store as store_mod
    from agitrack.sessions.store import DEFAULT_KEEP

    repo = _init_repo(tmp_path)
    store = SharedSessionStore(repo)
    base = _claude_user("u1", "hi") + "\n" + _claude_assistant("a1", "hello") + "\n"
    theirs = base + _claude_user("u3", "theirs") + "\n"
    mine = base + _claude_user("u2", "mine") + "\n"
    manifest = {"session_id": "sid", "content_hash": "h", "backend": "claude"}
    store._add_session("alice", "s", theirs, manifest)
    monkeypatch.setattr(store_mod, "_transcript_is_readable", lambda text, backend: False)

    result = store._add_and_push("alice", "s", mine, manifest, DEFAULT_KEEP)

    stored = store.repo.read_ref_blob(store.ref, f"{store._prefix()}alice/s/transcript.jsonl")
    ids = [json.loads(r)["uuid"] for r in stored.splitlines() if r.strip()]
    assert ids == ["u1", "a1", "u2"]  # our own copy, not the union
    assert result.merged == 0


def test_transcript_is_readable_claude():
    from agitrack.sessions.store import _transcript_is_readable

    good = _claude_user("u1", "hi") + "\n" + _claude_assistant("a1", "hello") + "\n"
    assert _transcript_is_readable(good, "claude") is True
    # No real turns (assistant only / empty / garbage) → not readable.
    assert _transcript_is_readable(_claude_assistant("a1", "hello"), "claude") is False
    assert _transcript_is_readable("", "claude") is False
    assert _transcript_is_readable("not json\nstill not", "claude") is False


def test_transcript_is_readable_opencode():
    from agitrack.sessions.store import _transcript_is_readable

    good = json.dumps(
        {
            "info": {"id": "ses"},
            "messages": [
                {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "hi"}]},
                {
                    "info": {"role": "assistant", "id": "a1", "finish": "stop"},
                    "parts": [{"type": "text", "text": "ok"}],
                },
            ],
        }
    )
    assert _transcript_is_readable(good, "opencode") is True
    assert _transcript_is_readable("{bad json", "opencode") is False
    assert _transcript_is_readable(json.dumps({"info": {}, "messages": []}), "opencode") is False


# --- redaction --------------------------------------------------------------


def test_redact_masks_secrets_and_home_path_but_keeps_structure():
    sk = _fake_token("sk-", 18)
    ghp = _fake_token("ghp_", 36)
    line = f'{{"cwd":"/Users/alice/Code/x","t":"api_key={sk} token {ghp}"}}'
    out = redact_transcript(line)
    assert sk not in out and ghp not in out
    assert "[REDACTED]" in out
    assert "/Users/alice" not in out and "/Users/user/Code/x" in out  # username masked, path kept
    assert out.startswith('{"cwd"')  # JSON shape preserved


def test_redact_leaves_ordinary_text_untouched():
    assert redact_transcript("just a normal sentence\nsecond line") == "just a normal sentence\nsecond line"


def test_redact_covers_the_secret_shapes_github_push_protection_blocks():
    # A secret that slips through redaction is what gets the push declined by GitHub's secret
    # scanning. Redaction must cover at least the high-confidence shapes GitHub flags, so a
    # share never trips push protection.
    upper = string.ascii_uppercase + string.digits  # AWS key ids are upper+digits
    hexlower = string.digits + "abcdef"  # Twilio SID is hex
    samples = [
        _fake_token("ghp_", 36),  # GitHub classic PAT
        _fake_token("github_pat_", 60, charset=string.ascii_letters + string.digits + "_"),  # fine-grained PAT
        _fake_token("glpat-", 20),  # GitLab PAT
        _fake_token("xoxb-", 24),  # Slack bot token
        _fake_token("AKIA", 16, charset=upper),  # AWS long-term key id
        _fake_token("ASIA", 16, charset=upper),  # AWS temporary key id
        _fake_token("AIza", 35),  # Google API key
        _fake_token("ya29.", 30),  # Google OAuth token
        _fake_token("sk_live_", 24),  # Stripe secret key
        _fake_token("sk-ant-api03-", 24),  # Anthropic key
        _fake_token("npm_", 36),  # npm token
        _fake_token("pypi-", 24),  # PyPI token
        _fake_token("SG.", 22) + "." + _fake_token("", 43),  # SendGrid
        _fake_token("SK", 32, charset=hexlower),  # Twilio API key SID
        _fake_token("dop_v1_", 40),  # Doppler token
    ]
    for secret in samples:
        out = redact_transcript(f'{{"text":"using token {secret} now"}}')
        assert secret not in out, f"leaked: {secret}"
        assert "[REDACTED]" in out

    # A PEM private key embedded in a JSONL line (escaped newlines) is masked end-to-end.
    pem_body = _fake_token("", 40)
    pem = f"-----BEGIN RSA PRIVATE KEY-----\\n{pem_body}\\n-----END RSA PRIVATE KEY-----"
    out = redact_transcript(f'{{"key":"{pem}"}}')
    assert pem_body not in out and "BEGIN RSA PRIVATE KEY" not in out


def test_redact_does_not_mask_ordinary_hex_or_identifiers():
    # The added patterns must not be so greedy they mangle normal transcript content (commit
    # SHAs, UUIDs, plain words) — that would corrupt shared transcripts wholesale.
    benign = '{"sha":"a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0","id":"550e8400-e29b-41d4-a716-446655440000"}'
    assert redact_transcript(benign) == benign


# --- size cap (don't exceed Git's per-file limit) ---------------------------


def test_select_kept_indices_returns_none_when_already_small():
    from agitrack.sessions.share_cap import select_kept_indices

    assert select_kept_indices([100] * 5, [False] * 5, max_bytes=100_000, sep_bytes=0) is None


def test_select_kept_indices_keeps_head_and_drops_middle_anchoring_tail_at_compaction():
    from agitrack.sessions.share_cap import select_kept_indices

    sizes = [100] * 100
    compaction = [False] * 100
    compaction[80] = True  # a clean boundary at/after where the greedy tail begins
    kept = select_kept_indices(sizes, compaction, max_bytes=3000, sep_bytes=0, head_bytes=500)
    assert kept is not None
    assert kept[:5] == [0, 1, 2, 3, 4]  # the opening (head) is preserved
    assert 80 in kept and 79 not in kept  # tail anchored at the compaction, not mid-conversation
    assert kept[-1] == 99  # the most recent item is kept
    assert sum(sizes[i] for i in kept) <= 3000  # under budget


def test_claude_cap_bounds_size_preserving_head_and_recent_turns():
    from agitrack.transcripts.claude import cap_shared_transcript

    rows = [json.dumps({"type": "assistant", "uuid": f"u{i}", "cwd": "/x", "pad": "P" * 400}) for i in range(300)]
    rows[200] = json.dumps({"type": "user", "isCompactSummary": True, "uuid": "c", "summary": "S" * 400})
    raw = "\n".join(rows)
    max_bytes = 40 * 1024

    out = cap_shared_transcript(raw, max_bytes, head_bytes=8 * 1024)

    assert len(out.encode("utf-8")) <= max_bytes  # under Git's file-size limit
    kept = out.split("\n")
    assert kept[0] == rows[0]  # opening preserved (system/setup persists)
    assert kept[-1] == rows[-1]  # most recent turn preserved
    assert len(kept) < 300  # the old middle was dropped
    for line in kept:
        json.loads(line)  # every kept row is still valid JSON (resume-able .jsonl)
    assert cap_shared_transcript(raw, 10 * 1024 * 1024) == raw  # unchanged when it already fits


def test_opencode_cap_bounds_size_keeping_info_and_recent_messages():
    from agitrack.transcripts.opencode import cap_shared_transcript

    messages = []
    for i in range(300):
        info = {"id": f"m{i}", "role": "assistant"}
        if i == 200:
            info = {"id": "c", "role": "assistant", "summary": True}
        messages.append({"info": info, "parts": [{"type": "text", "text": "T" * 400}]})
    raw = json.dumps({"info": {"id": "ses_x", "title": "hello"}, "messages": messages})
    max_bytes = 40 * 1024

    out = cap_shared_transcript(raw, max_bytes, head_bytes=8 * 1024)

    assert len(out.encode("utf-8")) <= max_bytes
    parsed = json.loads(out)  # still a valid {info, messages} object opencode can import
    assert parsed["info"]["id"] == "ses_x"  # session info preserved
    ids = [m["info"]["id"] for m in parsed["messages"]]
    assert ids[0] == "m0"  # opening preserved
    assert ids[-1] == "m299"  # most recent preserved
    assert len(parsed["messages"]) < 300  # middle dropped
    assert cap_shared_transcript(raw, 10 * 1024 * 1024) == raw  # unchanged when it fits
    assert cap_shared_transcript("not json{", 1) == "not json{"  # unparseable → left as-is


def test_redact_and_cap_trims_oversized_and_flags_truncation():
    # The share helper every share path uses: redact, then bound the size so the push can't
    # trip Git's per-file limit — reporting whether anything was trimmed (for the user notice).
    from types import SimpleNamespace

    from agitrack.proxy.runner import _redact_and_cap
    from agitrack.transcripts.claude import cap_shared_transcript

    backend = SimpleNamespace(cap_shared_transcript=cap_shared_transcript)
    big = "\n".join(json.dumps({"type": "assistant", "uuid": f"u{i}", "pad": "P" * 900}) for i in range(400))

    text, truncated = _redact_and_cap(backend, big, 64 * 1024)
    assert truncated is True
    assert len(text.encode("utf-8")) <= 64 * 1024

    small, trimmed = _redact_and_cap(backend, '{"type":"user"}', 64 * 1024)
    assert trimmed is False and small == '{"type":"user"}'


# --- identity ---------------------------------------------------------------


def test_github_login_prefers_gh(monkeypatch):
    import agitrack.sessions.identity as identity

    monkeypatch.setattr(identity.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(
        identity.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="octocat\n", stderr=""),
    )
    assert github_login() == "octocat"


def test_github_login_falls_back_to_git_name(tmp_path, monkeypatch):
    import agitrack.sessions.identity as identity

    monkeypatch.setattr(identity.shutil, "which", lambda _: None)  # no gh
    repo = _init_repo(tmp_path)
    assert github_login(repo) == "Test-User"  # slug of "Test User"


def test_slug_is_safe():
    assert slug("a/b..c d") == "a-b-c-d"
    assert slug("") == "anonymous"


# --- store: share / list / read / rewrite / fingerprint / prune -------------


def test_share_lists_and_reads_back(tmp_path):
    store = SharedSessionStore(_init_repo(tmp_path))
    store.publish(
        github_id="alice",
        name="fix-parser",
        transcript="hello",
        manifest=_manifest("fix-parser", session_id="id1", updated=10),
    )
    entries = store.entries()
    assert [e.display for e in entries] == ["alice/fix-parser"]
    assert store.read_transcript(entries[0]) == "hello"
    assert entries[0].manifest["session_id"] == "id1"


def test_entries_prefers_fresh_local_over_stale_remote_mirror(tmp_path):
    # The listing must never show a stale "shared" time: when the canonical local ref holds
    # a fresher copy of your own session than the remote mirror (a share whose push
    # lagged/failed), the local copy wins — and a teammate's mirror-only session still lists.
    from agitrack.sessions.store import REMOTE_MIRROR

    store = SharedSessionStore(_init_repo(tmp_path))
    store.publish(
        github_id="alice",
        name="fix-parser",
        transcript="local-fresh",
        manifest=_manifest("fix-parser", session_id="id1", updated=2000),
    )
    # The remote mirror (what fetch() populates) holds a STALE copy of the same session plus
    # a teammate's session that only exists on the remote.
    prefix = store._prefix()
    mirror = {
        f"{prefix}alice/fix-parser/transcript.jsonl": store.repo.write_blob("remote-stale"),
        f"{prefix}alice/fix-parser/manifest.json": store.repo.write_blob(
            json.dumps(_manifest("fix-parser", session_id="id1", updated=1000))
        ),
        f"{prefix}bob/feature/transcript.jsonl": store.repo.write_blob("teammate"),
        f"{prefix}bob/feature/manifest.json": store.repo.write_blob(
            json.dumps({"github_id": "bob", "name": "feature", "updated": 1500})
        ),
    }
    store._commit(mirror, "mirror", ref=REMOTE_MIRROR)

    listed = store.entries()
    by_key = {(e.github_id, e.name): e.manifest.get("updated") for e in listed}
    assert by_key[("alice", "fix-parser")] == 2000  # fresh local beats the stale remote mirror
    assert by_key[("bob", "feature")] == 1500  # teammate's mirror-only session still appears
    assert [e.name for e in listed] == ["fix-parser", "feature"]  # newest (highest updated) first


def test_fetch_lists_with_filter_and_reads_transcript_on_demand():
    # Listing fetches only the small manifests (blob filter); a chosen session's
    # large transcript is fetched on demand the first time it's read.
    fetches: list = []
    blobs = {"abc/me/sess/manifest.json": '{"updated": 1}'}  # transcript not local yet

    class FakeRepo:
        def remote_exists(self, name="origin"):
            return True

        def ref_exists(self, ref):
            return False  # no legacy ref

        def root_commit(self):
            return "abc"

        def read_tree_paths(self, ref):
            return {"abc/me/sess/manifest.json": "m", "abc/me/sess/transcript.jsonl": "t"}

        def read_ref_blob(self, ref, path):
            return blobs.get(path)

        def fetch_ref(self, refspec, *, remote="origin", filter_blobs=None, timeout=None, cancel=None):
            fetches.append(filter_blobs)
            if filter_blobs is None:  # the on-demand full fetch brings the transcript in
                blobs["abc/me/sess/transcript.jsonl"] = "the transcript"
            return True

    store = SharedSessionStore(FakeRepo())  # type: ignore[arg-type]
    assert store.fetch() is True
    # The listing fetches both refs (legacy + current) with the size filter so a
    # session shared by a pre-rename peer still lists.
    assert fetches == ["blob:limit=16k", "blob:limit=16k"]
    entry = store.entries()[0]
    assert store.read_transcript(entry) == "the transcript"
    assert None in fetches  # a full fetch was triggered on demand for the transcript


def test_fetch_passes_timeout_through_to_git(tmp_path):
    # A bad-internet bound: store.fetch(timeout=...) reaches the underlying git
    # fetch so a stalled network call can't run unbounded.
    seen: list = []

    class FakeRepo:
        repo = tmp_path

        def remote_exists(self, name="origin"):
            return True

        def fetch_ref(self, refspec, *, remote="origin", filter_blobs=None, timeout=None, cancel=None):
            seen.append(timeout)
            return True

    store = SharedSessionStore(FakeRepo())  # type: ignore[arg-type]
    assert store.fetch(timeout=12.0) is True
    assert seen == [12.0, 12.0]  # the same timeout bounds both the legacy and current fetch


def test_run_bounded_cancel_kills_process_promptly(tmp_path):
    # A set cancel Event must terminate the subprocess at once (not wait it out),
    # so a user who cancels truly stops the work.
    import threading
    import time

    repo = _init_repo(tmp_path)
    cancel = threading.Event()
    cancel.set()  # already cancelled before we start
    started = time.monotonic()
    rc = repo._run_bounded(["sleep", "10"], cancel=cancel)
    assert rc == 124
    assert time.monotonic() - started < 2.0  # killed promptly, did not sleep 10s


def test_run_bounded_timeout_kills_process(tmp_path):
    import time

    repo = _init_repo(tmp_path)
    started = time.monotonic()
    rc = repo._run_bounded(["sleep", "10"], timeout=0.3)
    assert rc == 124
    assert time.monotonic() - started < 2.0


def test_run_bounded_io_cancel_kills_and_captures(tmp_path):
    # The cancellable push variant kills the subprocess promptly and still returns
    # (code, stderr) so the caller can report the outcome.
    import threading
    import time

    repo = _init_repo(tmp_path)
    cancel = threading.Event()
    cancel.set()
    started = time.monotonic()
    code, stderr = repo._run_bounded_io(["sleep", "10"], cancel=cancel)
    assert code == 124
    assert isinstance(stderr, str)
    assert time.monotonic() - started < 2.0


def test_run_bounded_io_captures_stderr_on_completion(tmp_path):
    repo = _init_repo(tmp_path)
    code, stderr = repo._run_bounded_io(["sh", "-c", "echo oops 1>&2; exit 3"], timeout=5)
    assert code == 3
    assert "oops" in stderr


def test_fetch_does_not_start_when_already_cancelled(tmp_path):
    # "Don't let anything run if the user has already confirmed to cancel."
    import threading

    seen: list = []

    class FakeRepo:
        repo = tmp_path

        def remote_exists(self, name="origin"):
            return True

        def fetch_ref(self, *a, **k):
            seen.append(1)
            return True

    store = SharedSessionStore(FakeRepo())  # type: ignore[arg-type]
    cancel = threading.Event()
    cancel.set()
    assert store.fetch(cancel=cancel) is False
    assert seen == []  # never even started a git fetch


def test_read_transcript_passes_timeout_to_on_demand_fetch(tmp_path):
    # The full-transcript fetch (slow, can be large) must be bounded so it can't
    # wait forever — read_transcript threads its timeout into the on-demand fetch.
    seen: list = []

    class FakeRepo:
        repo = tmp_path

        def root_commit(self):
            return "abc"

        def read_ref_blob(self, ref, path):
            return None if not seen else "the transcript"  # missing until the fetch runs

        def remote_exists(self, name="origin"):
            return True

        def fetch_ref(self, refspec, *, remote="origin", filter_blobs=None, timeout=None, cancel=None):
            seen.append(timeout)
            return True

    from agitrack.sessions import SharedEntry

    store = SharedSessionStore(FakeRepo())  # type: ignore[arg-type]
    entry = SharedEntry(github_id="me", name="sess", manifest={})
    assert store.read_transcript(entry, timeout=120.0) == "the transcript"
    assert seen == [120.0]


def test_read_transcript_refetches_latest_even_when_stale_blob_is_local():
    # Regression: resuming a shared session returned a STALE local copy when an older
    # transcript blob was already present (e.g. from a prior resume / the listing
    # fetch). read_transcript must sync the ref FIRST so the resume reflects the
    # latest shared state, not whatever happens to be local.
    fetched: list = []
    blobs = {"abc/me/sess/transcript.jsonl": "OLD local copy"}  # a stale copy is already here

    class FakeRepo:
        def remote_exists(self, name="origin"):
            return True

        def root_commit(self):
            return "abc"

        def read_ref_blob(self, ref, path):
            return blobs.get(path)

        def fetch_ref(self, refspec, *, remote="origin", filter_blobs=None, timeout=None, cancel=None):
            fetched.append(refspec)
            blobs["abc/me/sess/transcript.jsonl"] = "NEW shared latest"  # the remote tip
            return True

    from agitrack.sessions import SharedEntry

    store = SharedSessionStore(FakeRepo())  # type: ignore[arg-type]
    entry = SharedEntry(github_id="me", name="sess", manifest={})
    assert store.read_transcript(entry) == "NEW shared latest"
    assert fetched == ["+refs/agitrack/shared-sessions:refs/agitrack/shared-sessions"]  # synced before reading


def test_read_transcript_without_remote_reads_local_only():
    # Offline / no remote: never attempt a fetch; serve the local copy (best available).
    blobs = {"abc/me/sess/transcript.jsonl": "local only"}

    class FakeRepo:
        def remote_exists(self, name="origin"):
            return False

        def root_commit(self):
            return "abc"

        def read_ref_blob(self, ref, path):
            return blobs.get(path)

        def fetch_ref(self, *a, **k):
            raise AssertionError("must not fetch when there is no remote")

    from agitrack.sessions import SharedEntry

    store = SharedSessionStore(FakeRepo())  # type: ignore[arg-type]
    entry = SharedEntry(github_id="me", name="sess", manifest={})
    assert store.read_transcript(entry) == "local only"


def test_read_transcript_does_not_fetch_when_already_cancelled():
    import threading

    class FakeRepo:
        def remote_exists(self, name="origin"):
            return True

        def root_commit(self):
            return "abc"

        def read_ref_blob(self, ref, path):
            return "whatever is local"

        def fetch_ref(self, *a, **k):
            raise AssertionError("must not start a fetch once cancelled")

    from agitrack.sessions import SharedEntry

    store = SharedSessionStore(FakeRepo())  # type: ignore[arg-type]
    cancel = threading.Event()
    cancel.set()
    entry = SharedEntry(github_id="me", name="sess", manifest={})
    assert store.read_transcript(entry, cancel=cancel) == "whatever is local"


def test_finalize_on_exit_cancels_inflight_fetches(tmp_path, monkeypatch):
    # Choosing to exit must stop any unfinished session fetch immediately.
    backend = _StubBackend()
    runner, _repo = _runner_with_store(tmp_path, monkeypatch, backend)
    cancelled = []
    runner._cancel_inflight_shared_fetches = lambda: cancelled.append(True)
    # Neutralise the rest of the (heavy) finalize so the test stays a unit.
    runner.sessions = [runner.active]
    runner._commit_latest_turn_sync = lambda: None
    runner._auto_share_on_exit = lambda: None
    runner._finalize_summary_then_integrate_on_exit = lambda: None
    runner._delete_orphan_merged_branches = lambda: None
    runner._sweep_orphan_shared_sessions = lambda **k: None

    runner._finalize_pending_work()

    assert cancelled == [True]


def test_fetch_shared_with_cancel_fast_path_when_no_remote(tmp_path, monkeypatch):
    # No remote ⇒ nothing to fetch over the network: the helper runs the cheap
    # local call inline (no thread, no interactive wait) and reports completion.
    backend = _StubBackend()
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    store = runner._shared_store()
    assert store.repo.remote_exists() is False
    assert runner._fetch_shared_with_cancel(store, "Fetching…") is True


def test_shared_ref_is_history_free_and_keeps_only_latest(tmp_path):
    repo = _init_repo(tmp_path)
    store = SharedSessionStore(repo)
    store.publish(github_id="alice", name="s", transcript="v1", manifest=_manifest("s", session_id="id", updated=1))
    store.publish(github_id="alice", name="s", transcript="v2", manifest=_manifest("s", session_id="id", updated=2))
    # The ref is a single parent-less commit (no history), holding only the latest.
    assert repo.parents(store.ref) == []
    assert store.read_transcript(store.entries()[0]) == "v2"


def test_entries_are_scoped_to_this_repo_fingerprint(tmp_path):
    repo = _init_repo(tmp_path)
    store = SharedSessionStore(repo)
    store.publish(
        github_id="alice", name="mine", transcript="x", manifest=_manifest("mine", session_id="id", updated=1)
    )
    # Inject an entry under a DIFFERENT repo fingerprint directly into the ref.
    paths = repo.read_tree_paths(store.ref)
    paths["other-repo-root/bob/theirs/transcript.jsonl"] = repo.write_blob("foreign")
    paths["other-repo-root/bob/theirs/manifest.json"] = repo.write_blob("{}")
    repo.update_ref(store.ref, repo.commit_tree_orphan(repo.write_tree_from(paths), "inject"))
    # Only this repo's session surfaces; the foreign-fingerprint one is hidden.
    assert [e.display for e in store.entries()] == ["alice/mine"]


def test_prune_keeps_only_the_newest_k_per_contributor(tmp_path):
    store = SharedSessionStore(_init_repo(tmp_path))
    for i in range(7):
        store.publish(
            github_id="alice",
            name=f"s{i}",
            transcript=f"t{i}",
            manifest=_manifest(f"s{i}", session_id=f"id{i}", updated=100 + i),
            keep=3,
        )
    names = [e.name for e in store.entries()]
    assert names == ["s6", "s5", "s4"]  # newest 3 kept, older pruned


def test_prune_never_touches_other_contributors(tmp_path):
    store = SharedSessionStore(_init_repo(tmp_path))
    store.publish(
        github_id="bob",
        name="keep",
        transcript="b",
        manifest={"github_id": "bob", "name": "keep", "session_id": "b1", "updated": 1},
    )
    for i in range(5):
        store.publish(
            github_id="alice",
            name=f"s{i}",
            transcript="a",
            manifest=_manifest(f"s{i}", session_id=f"a{i}", updated=10 + i),
            keep=2,
        )
    displays = {e.display for e in store.entries()}
    assert "bob/keep" in displays  # bob's single session survives alice's pruning
    assert sum(1 for e in store.entries() if e.github_id == "alice") == 2


# --- push / fetch round-trip through a bare remote --------------------------


def test_publish_pushes_and_a_clone_can_fetch_and_resume(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    (tmp_path / "src").mkdir()
    src = _init_repo(tmp_path / "src")
    # Push the source's default branch (name varies with git's init.defaultBranch:
    # main vs master) and point the bare remote's HEAD at it, so the clone checks
    # it out and has a born HEAD — otherwise root_commit() (the fingerprint) is
    # unborn in CI where the default branch differs.
    branch = src.current_branch()
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=src.repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=src.repo, check=True)
    subprocess.run(["git", "-C", str(remote), "symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True)

    result = SharedSessionStore(src).publish(
        github_id="alice",
        name="shared",
        transcript="conversation",
        manifest=_manifest("shared", session_id="sid", updated=5),
    )
    assert result.remote and result.pushed

    subprocess.run(["git", "clone", "-q", str(remote), str(tmp_path / "clone")], check=True)
    clone_store = SharedSessionStore(GitRepo(tmp_path / "clone"))
    assert clone_store.fingerprint() == SharedSessionStore(src).fingerprint()  # clone-stable
    assert clone_store.fetch()
    entries = clone_store.entries()
    assert [e.display for e in entries] == ["alice/shared"]
    assert clone_store.read_transcript(entries[0]) == "conversation"


def test_publish_without_remote_saves_locally(tmp_path):
    result = SharedSessionStore(_init_repo(tmp_path)).publish(
        github_id="alice", name="s", transcript="t", manifest=_manifest("s", session_id="id", updated=1)
    )
    assert result.remote is False and result.pushed is False


def test_remote_publish_reclaims_previous_version_but_keeps_latest(tmp_path):
    # Deferred reclaim: the previous transcript blob survives the push (so git can
    # deltify the new transcript against it — append-only sessions transmit just the
    # new turns) and is reclaimed AFTER, so local storage stays bounded to the latest
    # version. A fresh clone still reads the latest. (Exercises the real git push.)
    import subprocess as sp

    remote = tmp_path / "remote.git"
    sp.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    (tmp_path / "src").mkdir()
    src = _init_repo(tmp_path / "src")
    branch = src.current_branch()
    sp.run(["git", "remote", "add", "origin", str(remote)], cwd=src.repo, check=True)
    sp.run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=src.repo, check=True)
    sp.run(["git", "-C", str(remote), "symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True)
    store = SharedSessionStore(src)

    def transcript_blob():
        for line in sp.run(
            ["git", "-C", str(src.repo), "rev-list", "--objects", store.ref], capture_output=True, text=True
        ).stdout.splitlines():
            if "transcript" in line:
                return line.split()[0]
        return None

    assert store.publish(
        github_id="a", name="s", transcript="v1\n", manifest=_manifest("s", session_id="id", updated=1)
    ).pushed
    old_blob = transcript_blob()
    assert store.publish(
        github_id="a", name="s", transcript="v1\nv2\n", manifest=_manifest("s", session_id="id", updated=2)
    ).pushed

    # The previous version's blob was reclaimed after the push (bounded local storage)…
    assert sp.run(["git", "-C", str(src.repo), "cat-file", "-e", old_blob], capture_output=True).returncode != 0
    # …and the ref stays history-free (a single orphan commit, no parents).
    parents = sp.run(
        ["git", "-C", str(src.repo), "rev-list", "--count", store.ref], capture_output=True, text=True
    ).stdout.strip()
    assert parents == "1"
    # …and a fresh clone reads the latest.
    sp.run(["git", "clone", "-q", str(remote), str(tmp_path / "clone")], check=True)
    clone_store = SharedSessionStore(GitRepo(tmp_path / "clone"))
    assert clone_store.fetch()  # custom refs aren't pulled by a plain clone
    assert clone_store.read_transcript(clone_store.entries()[0]) == "v1\nv2\n"


def test_count_transcript_rows_ignores_blank_lines():
    from agitrack.sessions import count_transcript_rows

    assert count_transcript_rows("a\nb\nc\n") == 3
    assert count_transcript_rows("a\n\n  \nb\n") == 2
    assert count_transcript_rows("") == 0


def test_publish_refuses_to_regress_to_a_shorter_transcript(tmp_path):
    # Recency guard: a machine that's behind (its transcript has FEWER append-only
    # turns) must not overwrite the longer shared copy — that was the "resume gives a
    # much older session" bug, where a stale machine (or its auto-share) rewound the
    # shared state. The refusal is flagged (behind) and the stored copy is unchanged.
    store = SharedSessionStore(_init_repo(tmp_path))  # no remote: writes the local ref
    assert (
        store.publish(
            github_id="a", name="s", transcript="t1\nt2\nt3\n", manifest=_manifest("s", session_id="id", updated=1)
        ).behind
        is False
    )
    behind = store.publish(
        github_id="a", name="s", transcript="t1\nt2\n", manifest=_manifest("s", session_id="id", updated=2)
    )
    assert behind.behind is True
    assert behind.pushed is False
    # The longer copy is still intact — the older push was rejected, not applied.
    assert store.read_transcript(store.entries()[0]) == "t1\nt2\nt3\n"


def test_publish_overwrite_replaces_a_newer_shared_copy(tmp_path):
    # overwrite=True is the explicit "share onto a newer shared copy anyway" path: it
    # bypasses the recency guard and replaces the stored copy with this (shorter) one,
    # instead of refusing. Used when the user picks "Overwrite" on a behind conflict.
    store = SharedSessionStore(_init_repo(tmp_path))  # no remote: writes the local ref
    store.publish(
        github_id="a", name="s", transcript="t1\nt2\nt3\n", manifest=_manifest("s", session_id="id", updated=1)
    )
    # Without overwrite this regresses and is refused...
    assert store.publish(
        github_id="a", name="s", transcript="t1\n", manifest=_manifest("s", session_id="id", updated=2)
    ).behind
    # ...with overwrite it is accepted and replaces the longer copy wholesale.
    result = store.publish(
        github_id="a", name="s", transcript="t1\n", manifest=_manifest("s", session_id="id", updated=3), overwrite=True
    )
    assert result.behind is False
    assert store.read_transcript(store.entries()[0]) == "t1\n"


def test_publish_overwrite_does_not_fold_in_the_existing_turns(tmp_path):
    # Overwrite must REPLACE, not union: a divergent mergeable copy is written exactly,
    # so the shared copy's turns are dropped (that's the point — the user chose to reset
    # it to this session), unlike a normal publish which would merge them in.
    store = SharedSessionStore(_init_repo(tmp_path))
    store.publish(
        github_id="a",
        name="s",
        transcript=_row("u1") + "\n" + _row("u2") + "\n",
        manifest=_manifest("s", session_id="id", updated=1),
    )
    only_first = _row("u1") + "\n"
    store.publish(
        github_id="a",
        name="s",
        transcript=only_first,
        manifest=_manifest("s", session_id="id", updated=2),
        overwrite=True,
    )
    assert store.read_transcript(store.entries()[0]) == only_first


def test_remote_publish_overwrite_replaces_newer_remote_copy(tmp_path):
    # End-to-end through a bare remote: a behind machine that chooses overwrite syncs the
    # remote, then force-replaces its newer copy with this one — the mirror image of the
    # recency-guard test, where the same machine WITHOUT overwrite is refused.
    import subprocess as sp

    remote = tmp_path / "remote.git"
    sp.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    (tmp_path / "src").mkdir()
    src = _init_repo(tmp_path / "src")
    branch = src.current_branch()
    sp.run(["git", "remote", "add", "origin", str(remote)], cwd=src.repo, check=True)
    sp.run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=src.repo, check=True)
    sp.run(["git", "-C", str(remote), "symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True)
    store = SharedSessionStore(src)

    assert store.publish(
        github_id="a", name="s", transcript="t1\nt2\nt3\n", manifest=_manifest("s", session_id="id", updated=1)
    ).pushed
    # Behind without overwrite (refused, remote intact)...
    assert store.publish(
        github_id="a", name="s", transcript="t1\n", manifest=_manifest("s", session_id="id", updated=2)
    ).behind
    # ...accepted and pushed with overwrite.
    overwritten = store.publish(
        github_id="a", name="s", transcript="t1\n", manifest=_manifest("s", session_id="id", updated=3), overwrite=True
    )
    assert overwritten.pushed is True and overwritten.behind is False
    sp.run(["git", "clone", "-q", str(remote), str(tmp_path / "clone")], check=True)
    clone_store = SharedSessionStore(GitRepo(tmp_path / "clone"))
    assert clone_store.fetch()
    assert clone_store.read_transcript(clone_store.entries()[0]) == "t1\n"  # remote replaced


def test_publish_allows_a_longer_transcript(tmp_path):
    # The normal append case: more rows than the shared copy is accepted (and a new
    # first share, with no existing entry, is never blocked).
    store = SharedSessionStore(_init_repo(tmp_path))
    assert (
        store.publish(
            github_id="a", name="s", transcript="t1\n", manifest=_manifest("s", session_id="id", updated=1)
        ).behind
        is False
    )
    grew = store.publish(
        github_id="a", name="s", transcript="t1\nt2\n", manifest=_manifest("s", session_id="id", updated=2)
    )
    assert grew.behind is False
    assert store.read_transcript(store.entries()[0]) == "t1\nt2\n"


def test_remote_publish_recency_guard_blocks_a_behind_push(tmp_path):
    # End-to-end through a bare remote: after the lease-fail fetch syncs the remote's
    # longer copy, a behind machine's retry is refused — the remote is not rewound.
    import subprocess as sp

    remote = tmp_path / "remote.git"
    sp.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    (tmp_path / "src").mkdir()
    src = _init_repo(tmp_path / "src")
    branch = src.current_branch()
    sp.run(["git", "remote", "add", "origin", str(remote)], cwd=src.repo, check=True)
    sp.run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=src.repo, check=True)
    sp.run(["git", "-C", str(remote), "symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True)
    store = SharedSessionStore(src)

    assert store.publish(
        github_id="a", name="s", transcript="t1\nt2\nt3\n", manifest=_manifest("s", session_id="id", updated=2)
    ).pushed
    # A would-be regression to a shorter transcript is refused and never pushed.
    behind = store.publish(
        github_id="a", name="s", transcript="t1\n", manifest=_manifest("s", session_id="id", updated=3)
    )
    assert behind.behind is True and behind.pushed is False
    sp.run(["git", "clone", "-q", str(remote), str(tmp_path / "clone")], check=True)
    clone_store = SharedSessionStore(GitRepo(tmp_path / "clone"))
    assert clone_store.fetch()
    assert clone_store.read_transcript(clone_store.entries()[0]) == "t1\nt2\nt3\n"  # remote intact


def test_shared_entry_display_uses_sorted_contributor_set():
    from agitrack.sessions.store import SharedEntry

    e = SharedEntry(github_id="alice", name="foo", manifest={"contributors": ["bob", "alice", "bob"]})
    assert e.contributors == ["alice", "bob"]  # de-duped and sorted (order never matters)
    assert e.display == "alice+bob/foo"
    # Back-compat: an entry with no contributor set shows just the origin owner.
    assert SharedEntry(github_id="alice", name="foo", manifest={}).display == "alice/foo"


def test_reshare_under_origin_stays_one_entry_and_accumulates_contributors(tmp_path):
    # The heart of the redesign: bob re-sharing alice's session (under alice's origin
    # path) updates the SAME entry and joins the contributor set — `alice+bob/foo` —
    # instead of spawning a separate `bob/foo`.
    store = SharedSessionStore(_init_repo(tmp_path))
    store.publish(
        github_id="alice",
        name="foo",
        transcript="t1\n",
        manifest={"github_id": "alice", "name": "foo", "session_id": "id", "contributors": ["alice"]},
    )
    store.publish(
        github_id="alice",  # origin owner (where it lives), not the sharer
        name="foo",
        transcript="t1\nt2\n",
        prune_gid="bob",  # the actual sharer
        manifest={"github_id": "alice", "name": "foo", "session_id": "id", "contributors": ["alice", "bob"]},
    )
    entries = store.entries()
    assert len(entries) == 1  # one logical session, not two
    assert entries[0].display == "alice+bob/foo"


def test_prune_gid_prunes_the_sharer_not_the_origin_owner(tmp_path):
    # A contributor re-sharing under someone else's origin must prune only THEIR own
    # stale sessions, never the origin owner's — otherwise bob could evict alice's work.
    store = SharedSessionStore(_init_repo(tmp_path))
    for n in ("a", "b"):
        store.publish(
            github_id="alice", name=n, transcript="x\n", manifest={"github_id": "alice", "name": n, "session_id": n}
        )
    # bob re-shares alice/a with keep=1. Pruning is bob's (who owns nothing), so alice's
    # other session survives — a prune keyed on the origin owner would have dropped it.
    store.publish(
        github_id="alice",
        name="a",
        transcript="x\ny\n",
        prune_gid="bob",
        keep=1,
        manifest={"github_id": "alice", "name": "a", "session_id": "a", "contributors": ["alice", "bob"]},
    )
    assert sorted(e.name for e in store.entries()) == ["a", "b"]


class _PublishFakeRepo:
    """Records fetch/push calls so the push-first publish path can be asserted.

    ``push_results`` is consumed one per push attempt (``(ok, stderr)``)."""

    def __init__(self, push_results):
        self.push_results = list(push_results)
        self.calls: list = []

    def remote_exists(self, name="origin"):
        return True

    def ref_exists(self, ref):
        return False  # no legacy ref present

    def root_commit(self):
        return "fp"

    def ref_sha(self, ref):
        return "localtip"

    def read_tree_paths(self, ref):
        return {}

    def read_ref_blob(self, ref, path):
        return ""  # no existing entry → the recency guard never blocks these tests

    def write_blob(self, content):
        return "blob"

    def write_tree_from(self, entries):
        return "tree"

    def commit_tree_orphan(self, tree, message):
        return "commit"

    def update_ref(self, ref, sha):
        pass

    def delete_orphaned_objects(self, old):
        self.calls.append("reclaim")  # must come AFTER push so the old blob is the delta base
        return 0

    def fetch_ref(self, refspec, *, remote="origin", filter_blobs=None, timeout=None, cancel=None):
        self.calls.append("fetch")
        return True

    def push_ref(self, refspec, *, remote="origin", force_with_lease=None, timeout=None, cancel=None):
        self.calls.append("push")
        return self.push_results.pop(0)


def test_publish_pushes_first_without_a_fetch_in_the_common_case(tmp_path):
    # Push-first: when the optimistic push lands, publish makes a single network
    # hop — no pre-fetch — so a share is fast on a good connection.
    repo = _PublishFakeRepo([(True, "")])
    result = SharedSessionStore(repo).publish(  # type: ignore[arg-type]
        github_id="a", name="s", transcript="t", manifest=_manifest("s", session_id="id", updated=1)
    )
    assert result.pushed is True
    # No fetch; and the previous snapshot is reclaimed only AFTER the push, so it can
    # serve as the push's delta base (append-only transcripts transmit just the diff).
    assert repo.calls == ["push", "reclaim"]


def test_publish_retries_after_stale_lease(tmp_path):
    # A concurrent contributor moved the remote: the optimistic push is rejected
    # with a stale lease, so publish syncs and retries exactly once.
    repo = _PublishFakeRepo([(False, "! [rejected] shared (stale info)"), (True, "")])
    result = SharedSessionStore(repo).publish(  # type: ignore[arg-type]
        github_id="a", name="s", transcript="t", manifest=_manifest("s", session_id="id", updated=1)
    )
    assert result.pushed is True
    # push-first (reclaim after the failed attempt), sync, retry, reclaim again.
    assert repo.calls == ["push", "reclaim", "fetch", "push", "reclaim"]


def test_publish_does_not_retry_on_auth_failure(tmp_path):
    # A non-race failure (auth) must fail fast: no fetch, no second push, so a
    # broken credential can't spin the publish into a retry loop.
    repo = _PublishFakeRepo([(False, "fatal: Authentication failed for 'origin'")])
    result = SharedSessionStore(repo).publish(  # type: ignore[arg-type]
        github_id="a", name="s", transcript="t", manifest=_manifest("s", session_id="id", updated=1)
    )
    assert result.pushed is False
    assert repo.calls == ["push", "reclaim"]  # failed fast — no fetch/retry (still reclaims locally)


def test_unshare_removes_only_that_entry(tmp_path):
    store = SharedSessionStore(_init_repo(tmp_path))
    store.publish(github_id="alice", name="keep", transcript="k", manifest=_manifest("keep", session_id="k", updated=1))
    store.publish(github_id="alice", name="drop", transcript="d", manifest=_manifest("drop", session_id="d", updated=2))
    store.unshare("alice", "drop")
    assert [e.name for e in store.entries()] == ["keep"]


def test_unshare_removes_a_session_living_in_the_legacy_ref(tmp_path):
    # A session shared before the aGiT -> aGiTrack rename lives only under the legacy
    # ref. Unsharing must remove it there too, or it keeps surfacing in entries().
    from agitrack.sessions.store import LEGACY_REF

    repo = _init_repo(tmp_path)
    # Seed an entry directly into the legacy ref (as an old aGiT client would have).
    legacy = SharedSessionStore(repo, ref=LEGACY_REF)
    legacy._add_session("alice", "old-sess", "t", _manifest("old-sess", session_id="o", updated=1))

    store = SharedSessionStore(repo)  # current-ref store, as the running app uses
    assert [e.name for e in store.entries()] == ["old-sess"]  # visible via the legacy merge

    store.unshare("alice", "old-sess")
    assert store.entries() == []  # gone from the legacy ref, not just the current one
    assert not any(k.startswith(store._prefix()) for k in repo.read_tree_paths(LEGACY_REF))


def test_update_deletes_old_version_objects_immediately(tmp_path):
    import subprocess as sp

    repo = _init_repo(tmp_path)
    store = SharedSessionStore(repo)
    store.publish(github_id="a", name="s", transcript="OLD", manifest=_manifest("s", session_id="id", updated=1))
    old_blob = next(
        line.split()[0]
        for line in sp.run(
            ["git", "-C", str(repo.repo), "rev-list", "--objects", store.ref], capture_output=True, text=True
        ).stdout.splitlines()
        if "transcript" in line
    )
    store.publish(github_id="a", name="s", transcript="NEW", manifest=_manifest("s", session_id="id", updated=2))
    gone = sp.run(["git", "-C", str(repo.repo), "cat-file", "-e", old_blob], capture_output=True).returncode != 0
    assert gone  # the previous version's blob is reclaimed right away, not left for auto-gc
    assert store.read_transcript(store.entries()[0]) == "NEW"


def test_update_one_session_keeps_other_sessions_intact(tmp_path):
    # Regression: the immediate-deletion must never remove objects the current ref
    # still references (a sibling session's blobs).
    repo = _init_repo(tmp_path)
    store = SharedSessionStore(repo)
    store.publish(github_id="a", name="s1", transcript="one", manifest=_manifest("s1", session_id="i1", updated=1))
    store.publish(github_id="b", name="s2", transcript="two", manifest=_manifest("s2", session_id="i2", updated=1))
    store.publish(github_id="a", name="s1", transcript="one-v2", manifest=_manifest("s1", session_id="i1", updated=2))
    got = {e.display: store.read_transcript(e) for e in store.entries()}
    assert got == {"a/s1": "one-v2", "b/s2": "two"}


def test_cleanup_orphans_removes_only_session_snapshots(tmp_path):
    import subprocess as sp

    repo = _init_repo(tmp_path)
    store = SharedSessionStore(repo)
    store.publish(github_id="a", name="s", transcript="t", manifest=_manifest("s", session_id="id", updated=1))
    fp = store.fingerprint()
    # A dangling SESSION snapshot (orphan commit with the manifest/transcript shape).
    sess_tree = repo.write_tree_from(
        {f"{fp}/a/old/transcript.jsonl": repo.write_blob("stale"), f"{fp}/a/old/manifest.json": repo.write_blob("{}")}
    )
    sess_orphan = repo.commit_tree_orphan(sess_tree, "old shared snapshot")
    # A NON-session orphan (normal source tree) — must be left untouched.
    other_blob = repo.write_blob("source code")
    other_orphan = repo.commit_tree_orphan(repo.write_tree_from({"src/main.py": other_blob}), "abandoned work")

    store.cleanup_orphans(fetch=False)

    def alive(sha):
        return sp.run(["git", "-C", str(repo.repo), "cat-file", "-e", sha], capture_output=True).returncode == 0

    assert not alive(sess_orphan)  # the session snapshot is reclaimed
    assert alive(other_orphan) and alive(other_blob)  # the non-session orphan is spared
    assert [e.display for e in store.entries()] == ["a/s"]  # the live session is unaffected


# --- pull-latest on resume (sync between machines) --------------------------


def test_import_overwrite_replaces_local(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    from agitrack.transcripts import claude

    repo = tmp_path / "repo"
    repo.mkdir()
    assert not claude.has_imported_session(repo, "sid")
    claude.import_shared_session(repo, "sid", '{"cwd":"/x","t":1}\n')
    assert claude.has_imported_session(repo, "sid")
    # Default keeps the local copy; overwrite replaces it (pull-latest).
    assert claude.import_shared_session(repo, "sid", '{"cwd":"/x","t":2}\n')  # no overwrite
    assert '"t": 1' in (claude._project_dir(repo) / "sid.jsonl").read_text()  # local kept
    assert claude.import_shared_session(repo, "sid", '{"cwd":"/x","t":2}\n', overwrite=True)
    assert '"t": 2' in (claude._project_dir(repo) / "sid.jsonl").read_text()  # replaced


def test_resume_shared_prompts_to_pull_when_local_exists(tmp_path, monkeypatch):
    backend = _StubBackend(transcript="bob's chat", has_local=True)  # we already have a local copy
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    SharedSessionStore(repo).publish(
        github_id="me",
        name="sess",
        transcript="bob's chat",
        manifest={"github_id": "me", "name": "sess", "session_id": "sid-x", "updated": 1},
    )
    runner.sessions = []  # not live
    runner._resume_conversation = lambda name, sid, **k: None
    runner._prompt_session_name = lambda title, *, default: default  # accept the local name (#71)
    # First popup selects the session; second is the conflict choice → option[0] = Replace.
    runner._select_popup = lambda title, options: options[0]

    runner._resume_shared_session_menu()
    _drain_shared_resume(runner)

    assert backend.imported == ("sid-x", "bob's chat", True)  # imported with overwrite (replaced local)


def test_resume_shared_keep_both_imports_under_new_id(tmp_path, monkeypatch):
    # When a local copy exists, "Keep both" re-imports the shared conversation
    # under a fresh id and resumes THAT, leaving the original untouched.
    from agitrack.config import AgitrackState

    backend = _StubBackend(transcript="bob's chat", has_local=True)
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    SharedSessionStore(repo).publish(
        github_id="me",
        name="sess",
        transcript="bob's chat",
        manifest={"github_id": "me", "name": "sess", "session_id": "sid-x", "updated": 1},
    )
    runner.sessions = []
    resumed: list = []
    runner._resume_conversation = lambda name, sid, **k: resumed.append((name, sid))
    runner._prompt_session_name = lambda title, *, default: default
    # First popup: pick the entry. Second (conflict): pick "Keep both".
    picks = iter([lambda opts: opts[0], lambda opts: next(o for o in opts if o.startswith("Keep both"))])
    runner._select_popup = lambda title, options: next(picks)(options)

    runner._resume_shared_session_menu()
    _drain_shared_resume(runner)

    assert backend.imported_as_id == "claude-copy-id"  # re-imported under the fresh id
    assert resumed == [("sess", "claude-copy-id")]  # and resumed that copy
    # A "Keep both" fork starts a SEPARATE lineage: no origin is recorded, so sharing
    # it later publishes a new `<you>/<name>` entry rather than updating the original.
    assert AgitrackState(repo.repo).shared_origin("claude-copy-id") is None


def test_resume_shared_defaults_to_local_when_shared_is_older(tmp_path, monkeypatch):
    # Guard against silent downgrade (the "resume gives a much older session" report):
    # when the shared copy has FEWER turns than the local one, the conflict prompt
    # leads with keeping the local (newer) copy, and taking that default resumes
    # locally WITHOUT importing the older shared version.
    backend = _StubBackend(transcript="a\nb\nc\n", has_local=True)  # local = 3 rows
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    SharedSessionStore(repo).publish(
        github_id="me",
        name="sess",
        transcript="a\n",  # shared = 1 row (older / shorter)
        manifest={"github_id": "me", "name": "sess", "session_id": "sid-x", "updated": 1, "transcript_rows": 1},
    )
    runner.sessions = []
    resumed: list = []
    runner._resume_conversation = lambda name, sid, **k: resumed.append((name, sid))
    runner._prompt_session_name = lambda title, *, default: default
    captured: dict = {}

    def conflict_pick(title, options):
        captured["title"] = title
        captured["options"] = list(options)
        return options[0]  # take the default (lead) option

    picks = iter([lambda t, o: o[0], conflict_pick])  # 1st popup picks the entry; 2nd is the conflict
    runner._select_popup = lambda title, options: next(picks)(title, options)

    runner._resume_shared_session_menu()
    _drain_shared_resume(runner)

    assert backend.imported is None  # the older shared copy was NOT imported
    assert resumed == [("sess", "sid-x")]  # resumed the local (newer) copy directly
    assert captured["options"][0].startswith("Keep my local copy")  # the newer copy leads
    assert "NEWER" in captured["title"]


def test_share_identity_uses_origin_for_imported_and_self_for_local(tmp_path, monkeypatch):
    backend = _StubBackend()
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    runner._session_name = lambda idx: "mylocal"
    # Originated here: owner is the sharer, contributors is just them.
    assert runner._share_identity("sid-local", "tester") == ("tester", "mylocal", ["tester"])
    # Imported (origin recorded): writes under the origin owner, sharer joins the set.
    runner._user_state().set_shared_origin("sid-imp", owner="alice", name="foo", contributors=["alice"])
    assert runner._share_identity("sid-imp", "tester") == ("alice", "foo", ["alice", "tester"])


def test_resume_records_full_lineage_origin(tmp_path, monkeypatch):
    # Resuming a shared session records its origin owner + name + contributors, so a
    # later re-share updates that same entry and keeps the contributor set.
    from agitrack.config import AgitrackState

    backend = _StubBackend(transcript="chat", has_local=False)
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    SharedSessionStore(repo).publish(
        github_id="alice",
        name="sess",
        transcript="chat",
        manifest={
            "github_id": "alice",
            "name": "sess",
            "session_id": "sid-x",
            "updated": 1,
            "contributors": ["alice", "carol"],
        },
    )
    runner.sessions = []
    runner._resume_conversation = lambda name, sid, **k: None
    runner._prompt_session_name = lambda title, *, default: default
    runner._select_popup = lambda title, options: options[0]

    runner._resume_shared_session_menu()
    _drain_shared_resume(runner)

    rec = AgitrackState(repo.repo).shared_origin("sid-x")
    assert rec == {"owner": "alice", "name": "sess", "contributors": ["alice", "carol"]}


# --- auto-share opt-in state ------------------------------------------------


def test_state_auto_share_opt_in(tmp_path):
    from agitrack.config import AgitrackState

    state = AgitrackState(tmp_path)
    assert state.auto_share_enabled("sid") is False
    state.set_auto_share("sid", True)
    assert state.auto_share_enabled("sid") is True
    assert "sid" in state.auto_share_session_ids()
    state.set_auto_share("sid", False)
    assert state.auto_share_enabled("sid") is False
    assert state.auto_share_enabled(None) is False


def test_state_shared_origin_round_trip_and_backcompat(tmp_path):
    from agitrack.config import AgitrackState

    st = AgitrackState(tmp_path)
    st.set_shared_origin("sid", owner="alice", name="foo", contributors=["bob", "alice", "bob"])
    assert st.shared_origin("sid") == {"owner": "alice", "name": "foo", "contributors": ["alice", "bob"]}
    assert st.shared_origin_name("sid") == "foo"  # legacy accessor still resolves the name
    assert AgitrackState(tmp_path).shared_origin("sid")["owner"] == "alice"  # persists across reload
    # A legacy name-only record (older client) still reads, with an empty owner/set.
    st.set_shared_origin_name("old", "bar")
    assert st.shared_origin("old") == {"owner": "", "name": "bar", "contributors": []}
    # Clearing removes it.
    st.set_shared_origin("sid", owner=None, name=None)
    assert st.shared_origin("sid") is None


def test_state_shared_session_lineage_chain(tmp_path):
    from agitrack.config import AgitrackState

    state = AgitrackState(tmp_path)
    assert state.session_lineage("a") == ["a"]
    # Two successive resume drifts: a -> b -> c.
    state.add_shared_session_alias("b", "a")
    state.add_shared_session_alias("c", "b")
    assert state.session_lineage("c") == ["c", "b", "a"]
    assert state.session_lineage("b") == ["b", "a"]
    # Persists to base state and survives reload.
    assert AgitrackState(tmp_path).session_lineage("c") == ["c", "b", "a"]
    # Defensive: a corrupt self-referential alias never loops.
    state.add_shared_session_alias("d", "d")  # ignored (new == previous)
    assert state.session_lineage("d") == ["d"]


def test_runner_recognises_shared_session_after_id_drift(tmp_path):
    # #55: a session shared under id "old" that the backend resumes as "new" must
    # still be marked shared and keep auto-sharing, via the recorded lineage.
    from proxy_helpers import make_runner
    from agitrack.config import AgitrackState

    (tmp_path / "repo").mkdir()
    repo = _init_repo(tmp_path / "repo")
    runner = make_runner()
    runner.base_repo = repo
    runner._debug = lambda *a, **k: None
    base_state = AgitrackState(repo.repo)
    runner._user_state = lambda: AgitrackState(repo.repo)
    runner._my_shared_session_ids = lambda: {"old"}

    # Before drift: "old" is recognised directly.
    assert runner._session_is_shared("old", {"old"}) is True
    # Auto-share opted in under "old".
    base_state.set_auto_share("old", True)

    # The backend forks "old" -> "new" on resume.
    runner._record_shared_alias_on_drift("old", "new")

    assert runner._session_is_shared("new", runner._my_shared_session_ids()) is True
    assert runner._session_auto_shared("new") is True


def test_unshare_requires_confirmation():
    # Unsharing removes the session from origin for everyone, so the manage menu must
    # confirm before it runs.
    import types

    from proxy_helpers import make_runner

    runner = make_runner()
    runner._session_auto_shared = lambda sid: False
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    entry = types.SimpleNamespace(display="alice/foo", manifest={"session_id": "sid-1"})
    unshared: list = []
    runner._unshare_entry = lambda e: unshared.append(e)

    unshare_label = "✗ Unshare (remove for everyone)"

    # Pick "Unshare", then confirm "Yes, unshare" → unshare runs.
    answers = iter([unshare_label, "Yes, unshare"])
    runner._select_popup = lambda *a, **k: next(answers)
    runner._manage_one_shared_session(entry)
    assert unshared == [entry]

    # Pick "Unshare", then decline → unshare does NOT run.
    unshared.clear()
    answers = iter([unshare_label, "No, keep it"])
    runner._select_popup = lambda *a, **k: next(answers)
    runner._manage_one_shared_session(entry)
    assert unshared == []


# --- Claude transcript export / import --------------------------------------


def test_claude_export_and_import_retargets_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    from agitrack.transcripts import claude

    src = tmp_path / "srcrepo"
    src.mkdir()
    project = claude._project_dir(src)
    project.mkdir(parents=True)
    (project / "sid.jsonl").write_text('{"type":"user","cwd":"/Users/alice/old","x":1}\n{"noop":true}\n')

    raw = claude.export_session_raw(src, "sid")
    assert raw is not None and "/Users/alice/old" in raw

    dst = tmp_path / "dstrepo"
    dst.mkdir()
    assert claude.import_shared_session(dst, "sid", raw)
    imported = (claude._project_dir(dst) / "sid.jsonl").read_text()
    assert str(dst.resolve()) in imported and "/Users/alice/old" not in imported
    assert claude.session_belongs_to_repo(dst, "sid")
    # Re-importing must not clobber an existing local transcript.
    (claude._project_dir(dst) / "sid.jsonl").write_text("LOCAL")
    assert claude.import_shared_session(dst, "sid", raw)
    assert (claude._project_dir(dst) / "sid.jsonl").read_text() == "LOCAL"


def test_claude_import_as_id_keeps_both_under_a_new_id(tmp_path, monkeypatch):
    # "Keep both": re-import a shared conversation under a fresh id so it lives
    # alongside the existing local copy of the same id.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    from agitrack.transcripts import claude

    dst = tmp_path / "repo"
    dst.mkdir()
    raw = '{"type":"user","sessionId":"sid","cwd":"/old"}\n{"type":"assistant","sessionId":"sid"}\n'
    claude.import_shared_session(dst, "sid", raw)  # the original copy
    assert claude.import_shared_session(dst, "sid", raw, as_id="newid")

    # Both copies exist; the new one is re-id'd and cwd-retargeted.
    assert claude.session_belongs_to_repo(dst, "sid")
    assert claude.session_belongs_to_repo(dst, "newid")
    copy = (claude._project_dir(dst) / "newid.jsonl").read_text()
    assert '"sessionId": "newid"' in copy and '"sid"' not in copy
    assert str(dst.resolve()) in copy


# --- OpenCode transcript export / import ------------------------------------


def test_opencode_export_raw_sanitizes_and_validates_json(monkeypatch, tmp_path):
    from agitrack.transcripts import opencode

    seen: dict[str, object] = {}

    def fake_export(repo, session_id, *, sanitize=False):
        seen["sanitize"] = sanitize
        seen["session_id"] = session_id
        return ('noise\n{"info": {"id": "ses_1"}, "messages": []}\n', 0)

    monkeypatch.setattr(opencode, "_run_export_pty", fake_export)
    raw = opencode.export_session_raw(tmp_path, "ses_1")
    assert raw is not None and '"ses_1"' in raw
    assert seen["sanitize"] is True  # OpenCode's own redaction is requested
    # The surrounding pty noise is stripped to a single parseable JSON object.
    import json as _json

    assert _json.loads(raw)["info"]["id"] == "ses_1"


def test_opencode_export_raw_rejects_unparseable_output(monkeypatch, tmp_path):
    from agitrack.transcripts import opencode

    monkeypatch.setattr(opencode, "_run_export_pty", lambda repo, sid, *, sanitize=False: ("{not json", 0))
    assert opencode.export_session_raw(tmp_path, "ses_1") is None
    # A non-zero exit code is also treated as a failed export.
    monkeypatch.setattr(opencode, "_run_export_pty", lambda repo, sid, *, sanitize=False: ('{"info":{}}', 1))
    assert opencode.export_session_raw(tmp_path, "ses_1") is None


def test_opencode_import_runs_cli_and_checks_success(monkeypatch, tmp_path):
    from agitrack.transcripts import opencode

    monkeypatch.setattr(opencode, "has_imported_session", lambda repo, sid: False)
    captured: dict[str, object] = {}

    def fake_run(repo, args):
        # The transcript was written to a temp file passed to `opencode import`.
        captured["args"] = args
        captured["cwd"] = repo
        captured["content"] = Path(args[-1]).read_text()
        return ("Imported session: ses_1\n", 0)

    monkeypatch.setattr(opencode, "_run_opencode_pty", fake_run)
    assert opencode.import_shared_session(tmp_path, "ses_1", '{"info":{"id":"ses_1"}}') is True
    assert captured["args"][:2] == ["opencode", "import"]
    assert captured["cwd"] == tmp_path
    assert captured["content"] == '{"info":{"id":"ses_1"}}'
    # The temp file is cleaned up afterwards.
    assert not Path(captured["args"][-1]).exists()


def test_opencode_import_as_id_reids_for_keep_both(monkeypatch, tmp_path):
    # "Keep both" for OpenCode: every occurrence of the old id token is swapped
    # for the new one before import, so it lands as a separate session.
    from agitrack.transcripts import opencode

    captured: dict[str, object] = {}

    def fake_run(repo, args):
        captured["content"] = Path(args[-1]).read_text()
        return ("Imported session: ses_new\n", 0)

    monkeypatch.setattr(opencode, "_run_opencode_pty", fake_run)
    raw = '{"info":{"id":"ses_old"},"messages":[{"sessionID":"ses_old"}]}'
    assert opencode.import_shared_session(tmp_path, "ses_old", raw, as_id="ses_new") is True
    # The transcript handed to `opencode import` is fully re-id'd.
    assert "ses_old" not in captured["content"]
    assert captured["content"].count("ses_new") == 2


def test_opencode_import_failure_paths(monkeypatch, tmp_path):
    from agitrack.transcripts import opencode

    monkeypatch.setattr(opencode, "has_imported_session", lambda repo, sid: False)
    # A clean exit without the success line (e.g. "File not found") is a failure.
    monkeypatch.setattr(opencode, "_run_opencode_pty", lambda repo, args: ("File not found\n", 0))
    assert opencode.import_shared_session(tmp_path, "ses_1", "{}") is False
    # A non-zero exit is a failure too.
    monkeypatch.setattr(opencode, "_run_opencode_pty", lambda repo, args: ("boom\n", 1))
    assert opencode.import_shared_session(tmp_path, "ses_1", "{}") is False
    # Empty inputs short-circuit without spawning anything.
    assert opencode.import_shared_session(tmp_path, "", "{}") is False
    assert opencode.import_shared_session(tmp_path, "ses_1", "") is False


def test_opencode_import_keeps_local_copy_unless_overwrite(monkeypatch, tmp_path):
    from agitrack.transcripts import opencode

    monkeypatch.setattr(opencode, "has_imported_session", lambda repo, sid: True)
    ran = {"n": 0}

    def fake_run(repo, args):
        ran["n"] += 1
        return ("Imported session: ses_1\n", 0)

    monkeypatch.setattr(opencode, "_run_opencode_pty", fake_run)
    # Already have it locally and not overwriting → no import spawn, reported in place.
    assert opencode.import_shared_session(tmp_path, "ses_1", "{}") is True
    assert ran["n"] == 0
    # Overwrite (pull-latest) re-imports.
    assert opencode.import_shared_session(tmp_path, "ses_1", "{}", overwrite=True) is True
    assert ran["n"] == 1


def test_opencode_has_imported_session_uses_repo_membership(monkeypatch, tmp_path):
    from agitrack.transcripts import opencode

    monkeypatch.setattr(opencode, "session_belongs_to_repo", lambda repo, sid: sid == "mine")
    assert opencode.has_imported_session(tmp_path, "mine") is True
    assert opencode.has_imported_session(tmp_path, "other") is False
    assert opencode.has_imported_session(tmp_path, "") is False


def test_opencode_transcript_size_is_unavailable(tmp_path):
    from agitrack.transcripts import opencode

    # No cheap per-session stat exists (SQLite store), so size is intentionally None.
    assert opencode.session_transcript_size(tmp_path, "ses_1") is None


# --- runner glue: share + resume-shared through the session menu ------------


class _StubBackend:
    name = "claude"
    supports_session_sharing = True

    def __init__(self, transcript="conversation text", has_local=False):
        self._transcript = transcript
        self._has_local = has_local
        self.imported: tuple | None = None

    def session_belongs_to_repo(self, repo, session_id):
        return True

    def export_session_raw(self, repo, session_id):
        return self._transcript

    def cap_shared_transcript(self, transcript, max_bytes, head_bytes=0):
        return transcript  # stub transcripts are tiny; size-capping is unit-tested separately

    def transcript_size(self, repo, session_id):
        return len(self._transcript.encode("utf-8"))

    def has_local_session(self, repo, session_id):
        return self._has_local

    def import_shared_session(self, repo, session_id, transcript, *, overwrite=False, as_id=None):
        self.imported = (session_id, transcript, overwrite)
        self.imported_as_id = as_id
        return True

    def new_import_id(self):
        return "claude-copy-id"


def _runner_with_store(tmp_path, monkeypatch, backend):
    from proxy_helpers import make_runner
    from agitrack.config import AgitrackState, GlobalConfig

    (tmp_path / "repo").mkdir()
    repo = _init_repo(tmp_path / "repo")
    state = AgitrackState(tmp_path / "repo")
    runner = make_runner(repo=repo, state=state)
    runner.base_repo = repo
    runner.backend = backend
    runner.state.backend_session_id = "sid-123"
    runner.global_config = GlobalConfig(path=tmp_path / "config.json")
    runner.global_config.acknowledge_session_sharing()  # use the concise (already-seen) prompt
    runner.global_config.github_login = "tester"  # deterministic identity (no gh call)
    runner._render = lambda: None
    runner.messages = []
    runner._set_message = lambda msg, **k: runner.messages.append(msg)
    # Confirm any popup (the per-share consent, the keep-updated offer) with its first
    # option; tests that need other choices override this after construction.
    runner._select_popup = lambda title, options: options[0]
    return runner, repo


def test_warm_share_login_resolves_when_sharing_reachable(tmp_path):
    # Startup warms the login cache so auto-share never shells out mid-share.
    from types import SimpleNamespace
    from proxy_helpers import make_runner

    runner = make_runner()
    runner._debug = lambda *a, **k: None
    runner.backend = SimpleNamespace(supports_session_sharing=True)
    runner.global_config = SimpleNamespace(github_login="")
    runner.base_repo = SimpleNamespace(remote_exists=lambda: True)
    resolved: list = []
    runner._cached_or_resolve_login = lambda: resolved.append(True) or "tester"

    runner._warm_share_login()
    assert resolved == [True]


def test_warm_share_login_skips_when_no_remote_or_already_cached(tmp_path):
    from types import SimpleNamespace
    from proxy_helpers import make_runner

    runner = make_runner()
    runner._debug = lambda *a, **k: None
    runner.backend = SimpleNamespace(supports_session_sharing=True)
    resolved: list = []
    runner._cached_or_resolve_login = lambda: resolved.append(True) or "tester"

    # No remote ⇒ sharing can't reach anyone, so don't spend a `gh` call.
    runner.global_config = SimpleNamespace(github_login="")
    runner.base_repo = SimpleNamespace(remote_exists=lambda: False)
    runner._warm_share_login()
    # Already cached ⇒ nothing to resolve.
    runner.global_config = SimpleNamespace(github_login="tester")
    runner.base_repo = SimpleNamespace(remote_exists=lambda: True)
    runner._warm_share_login()
    assert resolved == []


def test_runner_share_session_publishes_and_redacts(tmp_path, monkeypatch):
    sk = _fake_token("sk-", 20)
    runner, repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend(transcript=f'{{"t":"token {sk}"}}'))

    runner._share_session()
    # The push runs in the background so the terminal never freezes; drain it.
    _drain_background_share_ops(runner)

    store = SharedSessionStore(repo)
    entries = store.entries()
    assert len(entries) == 1
    transcript = store.read_transcript(entries[0])
    assert sk not in transcript and "[REDACTED]" in transcript  # redacted
    assert entries[0].manifest["session_id"] == "sid-123"
    assert any(
        "Saved shared session" in n[0] or "Shared" in n[0] for n in runner._session_notices.values()
    )  # result notice


def test_share_behind_offers_overwrite_and_reshares(tmp_path, monkeypatch):
    # When the shared copy already has newer changes, the push is refused (behind). The
    # user is then asked, and choosing overwrite re-shares with overwrite=True.
    from agitrack.sessions import PublishResult

    runner, repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend(transcript="my session"))
    calls: list[dict] = []

    class FakeStore:
        def publish(self, **kw):
            calls.append(kw)
            if kw.get("overwrite"):
                return PublishResult(remote=True, pushed=True)
            return PublishResult(remote=True, pushed=False, behind=True)  # the shared copy is newer

    runner._shared_store = lambda: FakeStore()

    runner._share_session()  # consent + keep-updated popups answered with options[0]
    _drain_background_share_ops(runner)  # first publish → behind → conflict stashed
    assert runner._pending_share_conflicts, "a behind share queues a conflict to resolve"

    # The overwrite prompt's first option is "Overwrite…", so the default popup picks it.
    runner._service_share_conflicts()
    _drain_background_share_ops(runner)  # the overwrite publish

    assert len(calls) == 2
    assert not calls[0].get("overwrite")  # the initial share doesn't force
    assert calls[1]["overwrite"] is True  # the resolution does
    assert not runner._pending_share_conflicts
    assert any("Overwrote the shared copy" in n[0] for n in runner._session_notices.values())


def test_share_behind_cancel_leaves_shared_copy_untouched(tmp_path, monkeypatch):
    from agitrack.sessions import PublishResult

    runner, repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend(transcript="my session"))
    calls: list[dict] = []

    class FakeStore:
        def publish(self, **kw):
            calls.append(kw)
            return PublishResult(remote=True, pushed=False, behind=True)

    runner._shared_store = lambda: FakeStore()

    # Answer the conflict prompt by keeping the newer shared copy; other popups (consent,
    # keep-updated) still take their first option.
    def popup(title, options):
        if "already has newer changes" in title:
            return options[-1]  # "Keep the newer shared copy (cancel)"
        return options[0]

    runner._select_popup = popup

    runner._share_session()
    _drain_background_share_ops(runner)
    runner._service_share_conflicts()
    _drain_background_share_ops(runner)

    assert len(calls) == 1  # only the refused initial attempt — no overwrite was issued
    assert any("left the newer shared copy as is" in m for m in runner.messages)


def test_share_confirms_every_time_even_after_acknowledged(tmp_path, monkeypatch):
    # Each manual share uploads a fresh, possibly sensitive transcript, so the
    # sensitive-information confirmation must appear EVERY time — not only the first.
    backend = _StubBackend(transcript="hello")
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    runner.global_config.acknowledge_session_sharing()  # already acknowledged once
    prompts: list = []

    def popup(title, options):
        prompts.append(title)
        return "No, cancel"  # the user declines this time

    runner._select_popup = popup
    runner._share_session()

    assert prompts, "a confirmation popup is shown before pushing"
    assert "secret" in prompts[0].lower()  # it still warns about sensitive content
    assert SharedSessionStore(repo).entries() == []  # declined ⇒ nothing shared
    assert any("cancel" in m.lower() for m in runner.messages)


def test_share_runs_in_background_without_blocking(tmp_path, monkeypatch):
    # A manual share must not freeze the terminal on the push: it kicks the upload
    # onto a background thread (returning at once) and surfaces the result as a notice.
    import threading
    import time as _time

    from agitrack.sessions.store import PublishResult

    backend = _StubBackend(transcript="hello")
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    gate = threading.Event()

    class SlowStore:
        def publish(self, *, github_id, name, transcript, manifest, prune_gid=None, timeout=None, cancel=None):
            gate.wait(timeout=5)  # the network push is slow
            return PublishResult(remote=True, pushed=True)

    runner._shared_store = lambda: SlowStore()

    started = _time.monotonic()
    runner._share_session()
    assert _time.monotonic() - started < 1.0  # returned at once — did NOT wait on the push
    assert runner._background_share_ops  # the push is running in the background

    gate.set()
    _drain_background_share_ops(runner)
    assert any("Shared" in n[0] for n in runner._session_notices.values())  # result notice landed
    assert runner._background_share_ops == []  # cleared once finished


def test_runner_share_unsupported_backend_shows_message(tmp_path, monkeypatch):
    class _OpenCodeStub:
        name = "opencode"
        supports_session_sharing = False

    runner, repo = _runner_with_store(tmp_path, monkeypatch, _OpenCodeStub())

    runner._share_session()

    assert SharedSessionStore(repo).entries() == []  # nothing shared
    assert any("isn't supported" in m and "opencode" in m for m in runner.messages)


def test_runner_resume_shared_imports_and_resumes(tmp_path, monkeypatch):
    from agitrack.config import AgitrackState

    backend = _StubBackend()
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    # Seed a shared session as if a teammate published it.
    SharedSessionStore(repo).publish(
        github_id="bob",
        name="cool-fix",
        transcript="bob's chat",
        manifest={"github_id": "bob", "name": "cool-fix", "session_id": "bob-sid", "updated": 99},
    )
    resumed = []
    runner._resume_conversation = lambda name, sid, *, backend=None: resumed.append((name, sid, backend))
    runner._select_popup = lambda title, options: options[0]  # pick the first (only) entry
    runner._prompt_session_name = lambda title, *, default: default  # accept the offered local name (#71)

    runner._resume_shared_session_menu()
    _drain_shared_resume(runner)

    assert backend.imported == ("bob-sid", "bob's chat", False)  # imported, no overwrite (no local copy)
    # Resumed under the original share name (no sharer prefix, #55), pinned to the
    # entry's backend (defaults to the active backend when the manifest omits one).
    assert resumed == [("cool-fix", "bob-sid", "claude")]
    # The original share name is remembered for round-trip re-sharing.
    assert AgitrackState(repo.repo).shared_origin_name("bob-sid") == "cool-fix"


def test_runner_resume_shared_crosses_backends(tmp_path, monkeypatch):
    # Active backend is Claude, but the shared entry is an OpenCode session: it
    # must be imported and resumed by a freshly-built OpenCode agent, not Claude.
    from agitrack.proxy import runner as runner_module

    active = _StubBackend()  # name == "claude"
    runner, repo = _runner_with_store(tmp_path, monkeypatch, active)
    SharedSessionStore(repo).publish(
        github_id="bob",
        name="oc-fix",
        transcript='{"info":{"id":"ses_bob"}}',
        manifest={"github_id": "bob", "name": "oc-fix", "backend": "opencode", "session_id": "ses_bob", "updated": 7},
    )
    oc_agent = _StubBackend(transcript="oc")
    oc_agent.name = "opencode"
    built: list[str] = []

    def fake_make(name):
        built.append(name)
        return oc_agent

    monkeypatch.setattr(runner_module, "make_proxy_agent", fake_make)
    resumed: list = []
    runner._resume_conversation = lambda name, sid, *, backend=None: resumed.append((name, sid, backend))
    runner._select_popup = lambda title, options: options[0]
    runner._prompt_session_name = lambda title, *, default: default  # accept the offered local name (#71)

    runner._resume_shared_session_menu()
    _drain_shared_resume(runner)

    assert built == ["opencode"]  # a fresh OpenCode agent was constructed
    assert oc_agent.imported == ("ses_bob", '{"info":{"id":"ses_bob"}}', False)  # OpenCode did the import
    assert active.imported is None  # the active Claude agent was NOT used
    assert resumed == [("oc-fix", "ses_bob", "opencode")]  # resumed under the share name, pinned to opencode


def test_runner_auto_share_pushes_on_change_only(tmp_path, monkeypatch):
    # Auto-share is now triggered per commit (not a timer). It pushes the first
    # time, re-pushes when the transcript changes, and the worker's content-hash
    # gate skips a push when nothing changed — so it never hammers the remote.
    backend = _StubBackend(transcript="turn one")
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    runner.state.set_auto_share("sid-123", True)

    def fire_commit():
        runner._auto_share_thread = None
        runner._maybe_auto_share_active()
        if runner._auto_share_thread is not None:
            runner._auto_share_thread.join(timeout=10)

    def shared_transcript():
        store = SharedSessionStore(repo)
        return store.read_transcript(store.entries()[0])

    fire_commit()
    assert shared_transcript() == "turn one"  # first commit shares it

    backend._transcript = "turn one\nturn two"  # new turn arrived
    fire_commit()
    assert shared_transcript() == "turn one\nturn two"  # changed ⇒ re-pushed

    # Unchanged content: the worker's hash gate means no new push (a no-op).
    last_updated = SharedSessionStore(repo).entries()[0].manifest["updated"]
    monkeypatch.setattr("time.time", lambda: 10**10)  # would change `updated` IF it pushed
    fire_commit()
    assert SharedSessionStore(repo).entries()[0].manifest["updated"] == last_updated


def test_auto_share_on_exit_pushes_new_conversation(tmp_path, monkeypatch):
    # Quitting right after a turn (before the live, commit-fired auto-share thread
    # has pushed) must still share the latest conversation: the exit path pushes
    # synchronously so the final turns are not lost.
    backend = _StubBackend(transcript="turn one\nturn two")
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    runner.state.set_auto_share("sid-123", True)
    runner._sessions_with_activity.add(runner.state.session_id)  # a turn happened this run

    runner._auto_share_on_exit()

    store = SharedSessionStore(repo)
    assert store.read_transcript(store.entries()[0]) == "turn one\nturn two"


def test_auto_share_on_exit_skips_when_no_activity_this_run(tmp_path, monkeypatch):
    # Resuming an auto-shared session and quitting without typing anything must NOT
    # re-share: no committed turn this run ⇒ no push, no "Sharing…" message, instant
    # exit. This is robust to Claude's resume id-churn (which would otherwise make a
    # transcript-digest comparison see a spurious change).
    backend = _StubBackend(transcript="prior conversation, untouched")
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    runner.state.set_auto_share("sid-123", True)
    # No activity recorded this run (the user only resumed and quit).

    runner._auto_share_on_exit()

    assert SharedSessionStore(repo).entries() == []  # untouched ⇒ no share
    assert not any("before exit" in m for m in runner.messages)


def test_auto_share_on_exit_skipped_when_not_auto_shared(tmp_path, monkeypatch):
    backend = _StubBackend(transcript="private work")
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    runner._sessions_with_activity.add(runner.state.session_id)  # had activity, but not shared
    # auto-share NOT enabled for this session.

    runner._auto_share_on_exit()

    assert SharedSessionStore(repo).entries() == []  # nothing pushed on exit


def test_auto_share_on_exit_no_push_when_already_shared(tmp_path, monkeypatch):
    # A session that had a turn this run but whose content was already pushed (live)
    # is a no-op on exit — the content-hash gate avoids a redundant final push.
    backend = _StubBackend(transcript="all caught up")
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    runner.state.set_auto_share("sid-123", True)
    runner._sessions_with_activity.add(runner.state.session_id)

    runner._auto_share_on_exit()  # first push
    last_updated = SharedSessionStore(repo).entries()[0].manifest["updated"]

    monkeypatch.setattr("time.time", lambda: 10**10)  # would bump `updated` IF it pushed
    runner._auto_share_on_exit()  # unchanged ⇒ no push

    assert SharedSessionStore(repo).entries()[0].manifest["updated"] == last_updated


def test_auto_share_on_exit_times_out_without_hanging(tmp_path, monkeypatch):
    # A stalled push (offline / auth / unreachable remote) must never hang exit:
    # the push is bounded by EXIT_SHARE_TIMEOUT, after which exit continues with a
    # warning.
    import threading
    import time as _time

    backend = _StubBackend(transcript="brand new turns")
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    runner.state.set_auto_share("sid-123", True)
    runner._sessions_with_activity.add(runner.state.session_id)
    runner.EXIT_SHARE_TIMEOUT = 0.2

    started = threading.Event()

    class _HangStore:
        def entries(self):  # consulted by the no-edits gate; nothing published yet
            return []

        def publish(self, **kwargs):
            started.set()
            threading.Event().wait(5)  # block well past the timeout

    runner._shared_store = lambda: _HangStore()

    t0 = _time.monotonic()
    runner._auto_share_on_exit()
    elapsed = _time.monotonic() - t0

    assert started.is_set()  # the push was attempted
    assert elapsed < 2.0  # returned promptly — did not hang on the stalled push
    assert any("timed out" in m for m in runner.messages)


def test_auto_share_on_exit_warns_on_push_failure(tmp_path, monkeypatch):
    # A remote that exists but rejects the push: warn and continue (don't hang,
    # don't crash). Simulated with a store whose publish reports a failed push.
    from agitrack.sessions.store import PublishResult

    backend = _StubBackend(transcript="unpushed turns")
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    runner.state.set_auto_share("sid-123", True)
    runner._sessions_with_activity.add(runner.state.session_id)

    class _FailStore:
        def entries(self):
            return []

        def publish(self, **kwargs):
            return PublishResult(remote=True, pushed=False, error="rejected")

    runner._shared_store = lambda: _FailStore()

    runner._auto_share_on_exit()

    assert any("push failed" in m for m in runner.messages)


def test_finalize_on_exit_invokes_auto_share(tmp_path, monkeypatch):
    # The exit finalize wires the synchronous auto-share in for every session.
    backend = _StubBackend()
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    called = []
    runner._auto_share_on_exit = lambda: called.append(True)
    # Neutralise the rest of the (heavy) finalize so the test stays a unit.
    runner.sessions = [runner.active]
    runner._commit_latest_turn_sync = lambda: None
    runner._finalize_summary_then_integrate_on_exit = lambda: None
    runner._delete_orphan_merged_branches = lambda: None
    runner._sweep_orphan_shared_sessions = lambda **k: None

    runner._finalize_pending_work()

    assert called == [True]


def test_reshare_uses_origin_name_so_round_trip_updates_same_entry(tmp_path, monkeypatch):
    # A session imported from another machine re-shares under its ORIGINAL share
    # name, so sharing back and forth keeps updating the SAME entry instead of
    # prepending the sharer id (and growing the name) on every round-trip (#55).
    backend = _StubBackend(transcript="resumed work")
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    runner.state.set_shared_origin_name("sid-123", "feature")  # remembered when resumed
    runner.name = "feature-2"  # local name got deduped — must NOT drive the share name
    runner.state.set_auto_share("sid-123", True)

    runner._maybe_auto_share_active()
    if runner._auto_share_thread is not None:
        runner._auto_share_thread.join(timeout=10)

    entries = SharedSessionStore(repo).entries()
    assert [f"{e.github_id}/{e.name}" for e in entries] == ["tester/feature"]


def test_auto_share_main_thread_does_no_heavy_work(tmp_path, monkeypatch):
    # The reactor-thread part must never read/redact the transcript itself — that
    # happens in the worker. Prove it by making export_session_raw blow up if the
    # MAIN thread ever calls it; only the spawned worker may.
    import threading

    backend = _StubBackend()
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    main = threading.get_ident()
    calls = {"main": 0}
    real_export = backend.export_session_raw

    def guarded(repo_path, sid):
        if threading.get_ident() == main:
            calls["main"] += 1
        return real_export(repo_path, sid)

    backend.export_session_raw = guarded
    runner.state.set_auto_share("sid-123", True)

    runner._maybe_auto_share_active()
    if runner._auto_share_thread is not None:
        runner._auto_share_thread.join(timeout=10)

    assert calls["main"] == 0  # the transcript was only read on the worker thread
    assert SharedSessionStore(repo).entries()  # and the worker still shared it


def test_auto_share_optin_persists_in_base_repo_state(tmp_path, monkeypatch):
    # The opt-in must survive across aGiTrack runs: it has to live in the BASE repo
    # state, not the session worktree (which is removed on exit).
    from agitrack.config import AgitrackState

    runner, repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())
    (tmp_path / "worktree").mkdir()
    runner.state = AgitrackState(tmp_path / "worktree")  # session state lives in the (ephemeral) worktree

    runner._set_session_auto_share("sid-123", True)

    assert AgitrackState(repo.repo).auto_share_enabled("sid-123") is True  # base repo → persists
    assert AgitrackState(tmp_path / "worktree").auto_share_enabled("sid-123") is False  # not in the worktree
    assert runner._session_auto_shared("sid-123") is True


def test_my_shared_session_ids_lists_only_mine(tmp_path, monkeypatch):
    runner, repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())  # github_login = "tester"
    store = SharedSessionStore(repo)
    store.publish(
        github_id="tester",
        name="s1",
        transcript="t",
        manifest={"github_id": "tester", "name": "s1", "session_id": "sid-mine", "updated": 1},
    )
    store.publish(
        github_id="someoneelse",
        name="s2",
        transcript="t",
        manifest={"github_id": "someoneelse", "name": "s2", "session_id": "sid-theirs", "updated": 1},
    )
    assert runner._my_shared_session_ids() == {"sid-mine"}


def test_session_menu_marks_shared_sessions(tmp_path, monkeypatch):
    runner, repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())
    SharedSessionStore(repo).publish(
        github_id="tester",
        name="s1",
        transcript="t",
        manifest={"github_id": "tester", "name": "s1", "session_id": "sid-123", "updated": 1},
    )
    # Put the active session (backend_session_id == sid-123) in the list the menu
    # iterates, and stub the menu's heavier collaborators.
    runner.sessions = [runner.active]
    runner.merge_ctx = None
    runner._session_name = lambda i: "session-1"
    runner._session_status = lambda i: "running"
    runner._active_has_pending = lambda: False
    runner._dormant_worktrees = lambda names: []
    runner._resumable_sessions = lambda: []
    captured = {}
    runner._select_popup = lambda title, options: captured.update(options=options) or None

    runner._session_menu()

    assert any("⇪ shared" in opt for opt in captured["options"])  # the active session (sid-123) is marked


def test_runner_auto_share_skipped_when_not_opted_in(tmp_path, monkeypatch):
    runner, repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())
    runner._maybe_auto_share_active()  # session not opted in
    assert runner._auto_share_thread is None
    assert SharedSessionStore(repo).entries() == []


def test_runner_manage_unshare_removes_session(tmp_path, monkeypatch):
    backend = _StubBackend()
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    SharedSessionStore(repo).publish(
        github_id="tester",
        name="session-1",
        transcript="t",
        manifest={
            "github_id": "tester",
            "name": "session-1",
            "session_id": "sid-123",
            "updated": 1,
            "content_hash": "h",
        },
    )

    # First popup picks the (only) session; the "Manage" popup picks Unshare (3rd
    # action); the "Unshare …?" popup confirms. The list loops, so close it (Esc/None)
    # on the second visit.
    picks = {"n": 0}

    def _popup(title, options):
        if title.startswith("Manage"):
            return options[2]  # Unshare
        if title.startswith("Unshare"):
            return "Yes, unshare"  # confirm the destructive removal
        picks["n"] += 1
        return options[0] if picks["n"] == 1 else None  # pick once, then close

    runner._select_popup = _popup

    runner._manage_shared_sessions_menu()
    # Unsharing runs in the background so the session never freezes; drain it.
    _drain_background_share_ops(runner)

    assert SharedSessionStore(repo).entries() == []
    assert any("session-1" in n[0] for n in runner._session_notices.values())  # a result notice was shown


def _drain_background_share_ops(runner):
    # The unshare/etc. push runs on a daemon thread; join then service so the result
    # notice lands, mirroring the main loop's _service_background_share_ops().
    for op in list(runner._background_share_ops):
        op["thread"].join(timeout=10)
    runner._service_background_share_ops()


def test_unshare_is_non_blocking_with_progress_and_result_notices(tmp_path, monkeypatch):
    # The reported freeze: unshare pushed synchronously with no message. It must now
    # return immediately (session stays usable), show a progress notice, then a result.
    import threading
    import time as _time

    from agitrack.sessions import SharedEntry
    from agitrack.sessions.store import PublishResult

    backend = _StubBackend()
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    gate = threading.Event()

    class SlowStore:
        def unshare(self, github_id, name, *, timeout=None):
            gate.wait(timeout=5)  # the network removal is slow
            return PublishResult(remote=True, pushed=True)

    runner._shared_store = lambda: SlowStore()
    entry = SharedEntry(github_id="tester", name="session-1", manifest={"session_id": "sid-123"})

    started = _time.monotonic()
    runner._unshare_entry(entry)
    assert _time.monotonic() - started < 1.0  # returned at once — did NOT wait on the push
    assert runner._background_share_ops  # the removal is running in the background
    assert any("Unsharing" in n[0] for n in runner._session_notices.values())  # progress shown

    gate.set()
    _drain_background_share_ops(runner)
    assert any("Unshared" in n[0] for n in runner._session_notices.values())  # result shown
    assert runner._background_share_ops == []  # op cleared once finished


def test_manage_enabling_auto_update_syncs_immediately(tmp_path, monkeypatch):
    # Turning auto-update ON should push the latest right away, not wait for the
    # next commit — so the shared copy is current immediately.
    backend = _StubBackend(transcript="newest turns")
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    SharedSessionStore(repo).publish(
        github_id="tester",
        name="session-1",
        transcript="stale",
        manifest={"github_id": "tester", "name": "session-1", "session_id": "sid-123", "updated": 1},
    )
    # Pick the session, then "Turn ON auto-update" (2nd action); close the looping list next.
    picks = {"n": 0}

    def _popup(title, options):
        if title.startswith("Manage"):
            return options[1]
        picks["n"] += 1
        return options[0] if picks["n"] == 1 else None

    runner._select_popup = _popup

    runner._manage_shared_sessions_menu()
    # Enabling auto-update kicks the sync push onto a background thread (it must not
    # block the terminal); drain it to observe the result.
    _drain_background_share_ops(runner)

    assert runner._session_auto_shared("sid-123") is True  # opt-in persisted
    entry = SharedSessionStore(repo).entries()[0]
    assert SharedSessionStore(repo).read_transcript(entry) == "newest turns"  # pushed on enable


def test_manage_update_now_pushes_in_background_with_progress_notice(tmp_path, monkeypatch):
    # "Update now" must show a "pushing to origin…" progress notice and push in the
    # background — not freeze the terminal while the user waits.
    backend = _StubBackend(transcript="newest turns")
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    SharedSessionStore(repo).publish(
        github_id="tester",
        name="session-1",
        transcript="stale",
        manifest={
            "github_id": "tester",
            "name": "session-1",
            "session_id": "sid-123",
            "updated": 1,
            "transcript_bytes": 5,
        },
    )
    # Pick the (only) session, then "Update now" (1st action) — both are options[0];
    # then close the looping list (Esc/None) on the next visit.
    picks = {"n": 0}

    def _popup(title, options):
        if title.startswith("Manage"):
            return options[0]
        picks["n"] += 1
        return options[0] if picks["n"] == 1 else None

    runner._select_popup = _popup

    runner._manage_shared_sessions_menu()

    assert any("pushing to origin" in n[0].lower() for n in runner._session_notices.values())  # progress
    _drain_background_share_ops(runner)
    entry = SharedSessionStore(repo).entries()[0]
    assert SharedSessionStore(repo).read_transcript(entry) == "newest turns"  # pushed


def test_manage_menu_builds_list_without_reading_transcripts(tmp_path, monkeypatch):
    # The menu syncs the listing first (small manifests — see test_manage_menu_fetches_origin),
    # but must NEVER read/redact full transcripts to build the list (the "takes a few seconds"
    # bug). Here there's no remote, so the sync is an instant local no-op.
    backend = _StubBackend()
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    SharedSessionStore(repo).publish(
        github_id="tester",
        name="s1",
        transcript="t",
        manifest={"github_id": "tester", "name": "s1", "session_id": "sid-123", "updated": 1, "transcript_bytes": 5},
    )

    def boom_read(*a, **k):
        raise AssertionError("manage menu must not read/redact transcripts to build the list")

    backend.export_session_raw = boom_read
    captured = {}
    runner._select_popup = lambda title, options: captured.update(title=title, options=options) or None

    runner._manage_shared_sessions_menu()

    assert captured["title"].startswith("Your shared sessions")
    assert len(captured["options"]) == 1 and "s1" in captured["options"][0]


def test_shared_entry_status_is_size_based(tmp_path, monkeypatch):
    from agitrack.sessions import SharedEntry

    backend = _StubBackend(transcript="x" * 100)  # current transcript = 100 bytes
    runner, _repo = _runner_with_store(tmp_path, monkeypatch, backend)
    up_to_date = SharedEntry("tester", "s", {"session_id": "sid-123", "transcript_bytes": 100})
    grown = SharedEntry("tester", "s", {"session_id": "sid-123", "transcript_bytes": 50})
    unknown = SharedEntry("tester", "s", {"session_id": "sid-123"})  # no recorded size
    assert "up to date" in runner._shared_entry_status(up_to_date, "sid-123")
    assert "newer turns" in runner._shared_entry_status(grown, "sid-123")
    assert runner._shared_entry_status(unknown, "sid-123") == "shared"


def test_both_backends_flag_sharing_support():
    from agitrack.backends.proxy_agents import make_proxy_agent

    # Claude (per-session .jsonl) and OpenCode (export/import CLI) both have a
    # portable transcript, so both advertise session sharing (issue #55).
    assert make_proxy_agent("claude").supports_session_sharing is True
    assert make_proxy_agent("opencode").supports_session_sharing is True


def test_opencode_agent_delegates_sharing_to_transcript_module(tmp_path, monkeypatch):
    from agitrack.backends.proxy_agents import make_proxy_agent
    from agitrack.transcripts import opencode as opencode_session

    calls: dict[str, object] = {}

    def record(key, value):
        def fn(*args):
            calls[key] = args
            return value

        return fn

    monkeypatch.setattr(opencode_session, "export_session_raw", record("export", "{}"))
    monkeypatch.setattr(opencode_session, "session_transcript_size", record("size", None))
    monkeypatch.setattr(opencode_session, "has_imported_session", record("has", True))

    def fake_import(repo, sid, text, *, overwrite=False, as_id=None):
        calls["import"] = (repo, sid, text, overwrite)
        return True

    monkeypatch.setattr(opencode_session, "import_shared_session", fake_import)
    agent = make_proxy_agent("opencode")
    assert agent.export_session_raw(tmp_path, "ses_1") == "{}"
    assert agent.transcript_size(tmp_path, "ses_1") is None
    assert agent.has_local_session(tmp_path, "ses_1") is True
    assert agent.import_shared_session(tmp_path, "ses_1", "{}", overwrite=True) is True
    assert calls["export"] == (tmp_path, "ses_1")
    assert calls["import"] == (tmp_path, "ses_1", "{}", True)


def test_live_session_for_lineage_matches_by_origin_not_backend_id():
    # A multi-collaborator shared entry carries the LAST sharer's session id, so a
    # plain id match misses your own copy. _live_session_for_lineage matches by the
    # recorded shared lineage (origin owner + name) so resuming recognizes it and keeps
    # the existing session's name instead of minting a new one.
    from types import SimpleNamespace

    from proxy_helpers import make_runner

    runner = make_runner()
    runner.sessions = [
        SimpleNamespace(state=SimpleNamespace(backend_session_id="my-id")),
        SimpleNamespace(state=SimpleNamespace(backend_session_id="their-id")),
    ]
    origins = {
        "my-id": {"owner": "alice", "name": "feature", "contributors": ["alice", "bob"]},
    }
    runner._user_state = lambda: SimpleNamespace(shared_origin=lambda sid: origins.get(sid))

    assert runner._live_session_for_lineage("alice", "feature") == 0
    assert runner._live_session_for_lineage("alice", "other") is None
    assert runner._live_session_for_lineage("carol", "feature") is None  # different owner


# --- sharing-workflow fixes: menu exit, auto-share robustness, lineage unshare ---------


class _ResultStore:
    """A shared-session store stub that records publish kwargs and returns a fixed result
    for publish/unshare — for exercising the auto-share/menu paths without a real remote."""

    def __init__(self, result=None, entries=None):
        self.result = result
        self._entries = list(entries or [])
        self.publish_kwargs: dict | None = None

    def publish(self, **kwargs):
        self.publish_kwargs = kwargs
        return self.result

    def unshare(self, github_id, name, *, timeout=None):
        return self.result

    def entries(self):
        return list(self._entries)


def _fire_auto_share(runner):
    runner._auto_share_thread = None
    runner._maybe_auto_share_active()
    if runner._auto_share_thread is not None:
        runner._auto_share_thread.join(timeout=10)


def test_auto_share_does_not_cache_hash_on_failed_push(tmp_path, monkeypatch):
    # The bug: the content hash was cached BEFORE the push, so a silently-failing push left
    # the content marked "already shared" and it was never retried (shared copy went stale).
    from agitrack.sessions.store import PublishResult

    runner, _repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend(transcript="turns"))
    runner.state.set_auto_share("sid-123", True)
    runner._shared_store = lambda: _ResultStore(PublishResult(remote=True, pushed=False, error="rejected"))

    _fire_auto_share(runner)

    assert "sid-123" not in runner._auto_share_hash  # NOT cached — the next commit will retry
    assert runner._auto_share_outcome and "failed" in runner._auto_share_outcome  # recorded to surface


def test_auto_share_retries_unchanged_content_after_a_failure(tmp_path, monkeypatch):
    # Concretely: a failed push followed by a fire with the SAME content must push AGAIN
    # (not skip via the hash gate), and only then cache the hash.
    from agitrack.sessions.store import PublishResult

    runner, _repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend(transcript="turns"))
    runner.state.set_auto_share("sid-123", True)
    results = [
        PublishResult(remote=True, pushed=False, error="rejected"),
        PublishResult(remote=True, pushed=True),
    ]
    pushes = {"n": 0}

    class _FlakyStore:
        def entries(self):
            return []

        def publish(self, **kwargs):
            pushes["n"] += 1
            return results.pop(0)

    runner._shared_store = lambda: _FlakyStore()

    _fire_auto_share(runner)  # fails
    assert "sid-123" not in runner._auto_share_hash
    _fire_auto_share(runner)  # SAME content retried — must not be skipped

    assert pushes["n"] == 2  # retried the unchanged content rather than treating it as shared
    assert "sid-123" in runner._auto_share_hash  # cached only after the success


def test_auto_share_success_caches_hash_and_stays_silent(tmp_path, monkeypatch):
    from agitrack.sessions.store import PublishResult

    runner, _repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend(transcript="turns"))
    runner.state.set_auto_share("sid-123", True)
    runner._shared_store = lambda: _ResultStore(PublishResult(remote=True, pushed=True))

    _fire_auto_share(runner)
    runner._service_auto_share_outcome()

    assert "sid-123" in runner._auto_share_hash  # cached on a real success
    assert not any("failed" in n[0].lower() for n in runner._session_notices.values())  # silent on success


def test_auto_share_truncation_notice_shows_once_per_session(tmp_path, monkeypatch):
    # Auto-share fires every commit; a session that stays oversized is truncated every time. The
    # "we're trimming this" notice must appear ONCE, not on each auto-share (that would spam).
    runner, _repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())

    runner._auto_share_outcome = {"ok": True, "truncated": True, "sid": "sid-1", "name": "feature"}
    runner._service_auto_share_outcome()
    first = [n[0] for n in runner._session_notices.values() if "trimming" in n[0]]
    assert len(first) == 1 and "feature" in first[0]

    # A second truncated auto-share of the SAME session stays silent.
    runner._session_notices.clear()
    runner._auto_share_outcome = {"ok": True, "truncated": True, "sid": "sid-1", "name": "feature"}
    runner._service_auto_share_outcome()
    assert not any("trimming" in n[0] for n in runner._session_notices.values())

    # A DIFFERENT session still gets its own one-time notice.
    runner._auto_share_outcome = {"ok": True, "truncated": True, "sid": "sid-2", "name": "other"}
    runner._service_auto_share_outcome()
    assert any("trimming" in n[0] and "other" in n[0] for n in runner._session_notices.values())


def test_auto_share_success_without_truncation_stays_silent(tmp_path, monkeypatch):
    runner, _repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())
    runner._auto_share_outcome = {"ok": True, "truncated": False, "sid": "sid-1", "name": "feature"}
    runner._service_auto_share_outcome()
    assert not any("trimming" in n[0] for n in runner._session_notices.values())


def test_auto_share_bounds_the_push_with_a_timeout(tmp_path, monkeypatch):
    # A stalled push must not strand the worker — that would block EVERY future auto-share for
    # the run via the in-flight guard, silently freezing the shared copy.
    from agitrack.sessions.store import PublishResult

    runner, _repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend(transcript="turns"))
    runner.state.set_auto_share("sid-123", True)
    store = _ResultStore(PublishResult(remote=True, pushed=True))
    runner._shared_store = lambda: store

    _fire_auto_share(runner)

    assert store.publish_kwargs is not None
    assert store.publish_kwargs["timeout"] == runner.SHARE_PUSH_TIMEOUT


def test_service_auto_share_outcome_surfaces_a_failure_notice(tmp_path, monkeypatch):
    runner, _repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())
    runner._auto_share_outcome = {"failed": "remote rejected the push", "name": "feature"}

    runner._service_auto_share_outcome()

    assert runner._auto_share_outcome is None  # consumed
    assert any(
        "Auto-share" in n[0] and "feature" in n[0] and "failed" in n[0].lower()
        for n in runner._session_notices.values()
    )


def test_auto_share_behind_does_not_cache_and_surfaces_a_notice(tmp_path, monkeypatch):
    from agitrack.sessions.store import PublishResult

    runner, _repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend(transcript="turns"))
    runner.state.set_auto_share("sid-123", True)
    runner._shared_store = lambda: _ResultStore(PublishResult(remote=True, pushed=False, behind=True))

    _fire_auto_share(runner)
    assert "sid-123" not in runner._auto_share_hash  # behind ⇒ not marked as shared
    runner._service_auto_share_outcome()

    assert any("newer turns" in n[0] for n in runner._session_notices.values())


def test_manage_one_shared_session_closes_menu_on_unshare(tmp_path, monkeypatch):
    from agitrack.sessions import SharedEntry
    from agitrack.sessions.store import PublishResult

    runner, _repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())
    runner._shared_store = lambda: _ResultStore(PublishResult(remote=True, pushed=True))

    def _popup(title, options):
        if title.startswith("Manage"):
            return options[2]  # ✗ Unshare
        return "Yes, unshare"  # the confirm

    runner._select_popup = _popup
    entry = SharedEntry("tester", "s1", {"session_id": "sid-123"})

    assert runner._manage_one_shared_session(entry) == runner._MENU_DONE  # exits so progress shows
    assert any("Unsharing" in n[0] for n in runner._session_notices.values())  # "unsharing…" notice


def test_manage_one_shared_session_stays_when_unshare_cancelled(tmp_path, monkeypatch):
    from agitrack.sessions import SharedEntry

    runner, _repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())

    def _popup(title, options):
        if title.startswith("Manage"):
            return options[2]  # Unshare
        return "No, keep it"  # cancel the confirm

    runner._select_popup = _popup
    entry = SharedEntry("tester", "s1", {"session_id": "sid-123"})

    assert runner._manage_one_shared_session(entry) == runner._MENU_UP  # back to the list
    assert any("Kept" in m for m in runner.messages)


def test_manage_one_shared_session_esc_returns_up(tmp_path, monkeypatch):
    from agitrack.sessions import SharedEntry

    runner, _repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())
    runner._select_popup = lambda title, options: None  # Esc
    entry = SharedEntry("tester", "s1", {"session_id": "sid-123"})

    assert runner._manage_one_shared_session(entry) == runner._MENU_UP


def test_manage_menu_exits_after_unshare_instead_of_relisting(tmp_path, monkeypatch):
    # The whole shared-sessions menu must close after unsharing — not re-show the list over
    # the progress notice (the reported "returns to the parent menu" bug).
    runner, repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())
    SharedSessionStore(repo).publish(
        github_id="tester",
        name="s1",
        transcript="t",
        manifest={"github_id": "tester", "name": "s1", "session_id": "sid-123", "updated": 1, "transcript_bytes": 1},
    )
    shown = {"list": 0}

    def _popup(title, options):
        if title.startswith("Your shared sessions"):
            shown["list"] += 1
            return options[0]  # pick the entry
        if title.startswith("Manage"):
            return options[2]  # Unshare
        return "Yes, unshare"

    runner._select_popup = _popup

    assert runner._manage_shared_sessions_menu() == runner._MENU_DONE
    assert shown["list"] == 1  # the list was shown ONCE — it did not loop back after the action


def test_unshare_disables_auto_share_across_the_whole_lineage(tmp_path, monkeypatch):
    # Unsharing must turn auto-share OFF for the entire id lineage, not just the entry's id —
    # else a session opted in under a drifted id keeps re-sharing and still shows as shared.
    from agitrack.sessions import SharedEntry
    from agitrack.sessions.store import PublishResult

    runner, _repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())
    user = runner._user_state()
    user.set_auto_share("sid-old", True)  # opted in under the original id
    user.add_shared_session_alias("sid-new", "sid-old")  # backend forked a new id on resume
    assert runner._session_auto_shared("sid-new") is True  # the lineage sees the opt-in

    runner._shared_store = lambda: _ResultStore(PublishResult(remote=True, pushed=True))
    runner._unshare_entry(SharedEntry("tester", "s1", {"session_id": "sid-new"}))

    assert runner._session_auto_shared("sid-new") is False  # off across the lineage now
    assert runner._user_state().auto_share_enabled("sid-old") is False  # fresh read from disk


def _repo_with_bare_remote(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    (tmp_path / "src").mkdir()
    src = _init_repo(tmp_path / "src")
    branch = src.current_branch()
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=src.repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=src.repo, check=True)
    subprocess.run(["git", "-C", str(remote), "symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True)
    return src


def test_publish_rolls_back_local_entry_when_origin_rejects_the_push(tmp_path, monkeypatch):
    # Origin refused the push (GitHub push protection / a ruleset / a pre-receive hook). The
    # session NEVER reached origin, so it must NOT linger in the LOCAL ref masquerading as
    # "shared" in the menus or the resume-shared list (the reported bug).
    src = _repo_with_bare_remote(tmp_path)
    store = SharedSessionStore(src)
    monkeypatch.setattr(
        src,
        "push_ref",
        lambda *a, **k: (False, "remote: error: GH013: Repository rule violations found\n ! [remote rejected]"),
    )

    result = store.publish(
        github_id="alice", name="blocked", transcript="t", manifest=_manifest("blocked", session_id="x", updated=1)
    )

    assert result.pushed is False
    assert result.remote is True
    assert store.entries() == []  # rolled back — a rejected share never shows as shared


def test_publish_keeps_local_only_entry_when_there_is_no_remote(tmp_path):
    # The rollback is for REJECTED pushes only: with no remote at all, saving locally is the
    # legitimate behaviour and must be preserved.
    store = SharedSessionStore(_init_repo(tmp_path))

    result = store.publish(
        github_id="alice", name="local", transcript="t", manifest=_manifest("local", session_id="x", updated=1)
    )

    assert result.remote is False
    assert [e.name for e in store.entries()] == ["local"]  # kept — there was nowhere to push


def test_publish_does_not_loop_retrying_a_permanent_hook_rejection(tmp_path, monkeypatch):
    # A pre-receive hook decline won't change on a retry — unlike a stale-lease race. Don't
    # waste a fetch+retry round-trip on it (and don't let "[remote rejected]" fool the
    # stale-lease check into looping).
    src = _repo_with_bare_remote(tmp_path)
    store = SharedSessionStore(src)
    attempts = {"n": 0}

    def reject(*a, **k):
        attempts["n"] += 1
        return (False, " ! [remote rejected] refs/agitrack/shared-sessions (pre-receive hook declined)")

    monkeypatch.setattr(src, "push_ref", reject)
    store.publish(github_id="alice", name="x", transcript="t", manifest=_manifest("x", session_id="x", updated=1))

    assert attempts["n"] == 1  # pushed once, recognised it as permanent, did not retry


def test_unshare_retries_after_a_stale_lease(tmp_path, monkeypatch):
    # A concurrent push (e.g. this session's own auto-share) moved the remote ref, so the
    # unshare's force-with-lease push is rejected once. unshare must re-sync and retry — like
    # publish — or the removal never lands and the user just sees "the push was rejected".
    src = _repo_with_bare_remote(tmp_path)
    store = SharedSessionStore(src)
    store.publish(github_id="alice", name="drop", transcript="d", manifest=_manifest("drop", session_id="d", updated=1))

    real_push = src.push_ref
    attempts = {"n": 0}

    def flaky_push(refspec, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return (False, "! [rejected] (stale info)")  # lost the race the first time
        return real_push(refspec, **kwargs)

    monkeypatch.setattr(src, "push_ref", flaky_push)

    result = store.unshare("alice", "drop")

    assert result.pushed is True  # the retry landed the removal
    assert attempts["n"] == 2  # rejected once, then re-synced and retried
    assert store.entries() == []  # actually gone


def test_unshare_does_not_retry_on_auth_failure(tmp_path, monkeypatch):
    # A non-race failure (auth) must fail fast and surface the real reason — no retry loop.
    src = _repo_with_bare_remote(tmp_path)
    store = SharedSessionStore(src)
    store.publish(github_id="alice", name="drop", transcript="d", manifest=_manifest("drop", session_id="d", updated=1))
    attempts = {"n": 0}

    def auth_fail(refspec, **kwargs):
        attempts["n"] += 1
        return (False, "fatal: Authentication failed for 'origin'")

    monkeypatch.setattr(src, "push_ref", auth_fail)

    result = store.unshare("alice", "drop")

    assert result.pushed is False
    assert attempts["n"] == 1  # failed fast — a broken credential can't spin a retry loop
    assert "Authentication failed" in result.error  # the real reason is surfaced, not hidden


def test_unshare_falls_back_to_full_fetch_when_partial_unsupported(tmp_path):
    # When the remote doesn't support partial (blob-filtered) fetch, the filtered sync fails.
    # Without a fallback to a full fetch, the unshare's --force-with-lease is built against a
    # STALE local tip and the push is rejected on every attempt ("rerun shows the same
    # message"). It must fall back to a full fetch and then push successfully.
    class _NoPartialFetchRepo:
        def __init__(self):
            self.fetches: list = []
            self.pushed = False

        def remote_exists(self, name="origin"):
            return True

        def ref_exists(self, ref):
            return False  # no legacy ref present

        def root_commit(self):
            return "fp"

        def read_tree_paths(self, ref):
            return {"fp/alice/drop/transcript.jsonl": "b1", "fp/alice/drop/manifest.json": "b2"}

        def ref_sha(self, ref):
            return "tip"

        def write_tree_from(self, entries):
            return "tree"

        def commit_tree_orphan(self, tree, message):
            return "commit"

        def update_ref(self, ref, sha):
            pass

        def delete_orphaned_objects(self, old):
            return 0

        def fetch_ref(self, refspec, *, remote="origin", filter_blobs=None, timeout=None, cancel=None):
            self.fetches.append(filter_blobs)
            return filter_blobs is None  # the filtered fetch FAILS; a full fetch succeeds

        def push_ref(self, refspec, *, remote="origin", force_with_lease=None, timeout=None, cancel=None):
            self.pushed = True
            return (True, "")

    repo = _NoPartialFetchRepo()
    result = SharedSessionStore(repo).unshare("alice", "drop")  # type: ignore[arg-type]

    assert repo.fetches == ["blob:limit=16k", None]  # tried filtered, then fell back to a full fetch
    assert repo.pushed is True  # the removal pushed once the lease matched the synced tip
    assert result.pushed is True


def test_push_rejection_reason_extracts_the_meaningful_git_line():
    from agitrack.proxy.runner import _push_rejection_reason

    stderr = (
        "Enumerating objects: 5, done.\n"
        "To github.com:org/repo.git\n"
        " ! [remote rejected] refs/agitrack/shared-sessions -> refs/agitrack/shared-sessions (pre-receive hook declined)\n"
        "error: failed to push some refs to 'github.com:org/repo.git'\n"
    )
    reason = _push_rejection_reason(stderr)
    assert "pre-receive hook declined" in reason  # the WHY, not a blind prefix slice
    assert "timed out" in _push_rejection_reason("")  # empty ⇒ actionable hint, never a bare "[]"
    assert "stale info" in _push_rejection_reason("x\n ! [rejected] foo (stale info)\nerror: failed")

    # GitHub's push protection / rulesets (NOT a custom hook) put the actionable detail — which
    # secret, the unblock URL — on lines OTHER than the bare "declined" summary. Surface those.
    gh = (
        "remote: error: GH013: Repository rule violations found for refs/agitrack/shared-sessions.\n"
        "remote: - GITHUB PUSH PROTECTION\n"
        "remote:   - Push cannot contain secrets\n"
        "remote:     To allow, visit https://github.com/org/repo/security/secret-scanning/unblock-secret/abc\n"
        " ! [remote rejected] refs/agitrack/shared-sessions (push declined due to repository rule violations)\n"
    )
    detail = _push_rejection_reason(gh)
    assert "secret" in detail.lower()  # tells the user it's a blocked secret, not a vague "declined"
    assert "unblock-secret/abc" in detail  # and hands them the URL to resolve it
    assert "remote:" not in detail  # git's noisy prefix stripped for legibility


def test_unshare_of_a_local_only_entry_reports_not_rejected(tmp_path):
    # An entry that was only ever saved locally (its share never reached origin) has nothing to
    # push. unshare must report that — NOT "rejected with no error output" — and remove it.
    src = _repo_with_bare_remote(tmp_path)
    store = SharedSessionStore(src)
    store.publish(github_id="alice", name="keep", transcript="k", manifest=_manifest("keep", session_id="k", updated=1))
    store._add_session("alice", "drop", "d", _manifest("drop", session_id="d", updated=2))  # local only
    assert {e.name for e in store.entries()} == {"keep", "drop"}

    result = store.unshare("alice", "drop")

    assert result.remote is True
    assert result.pushed is False  # nothing on origin to push...
    assert result.error == ""  # ...but it was NOT a rejection
    assert [e.name for e in store.entries()] == ["keep"]  # actually gone


def test_unshare_clears_a_stale_mirror_entry_from_the_menu(tmp_path):
    # The menu also lists from the cached remote MIRROR ref. An entry lingering there (a stale
    # listing of a session no longer on origin) must be dropped on unshare, or it keeps showing
    # as shared and re-running just repeats the same no-op.
    from agitrack.sessions.store import REMOTE_MIRROR

    repo = _init_repo(tmp_path)  # no remote
    store = SharedSessionStore(repo)
    SharedSessionStore(repo, ref=REMOTE_MIRROR)._add_session(
        "alice", "ghost", "g", _manifest("ghost", session_id="g", updated=1)
    )
    assert [e.name for e in store.entries()] == ["ghost"]  # visible only via the mirror

    store.unshare("alice", "ghost")

    assert store.entries() == []  # dropped from the mirror → gone from the menu


def test_unshare_empty_stderr_push_reports_a_timeout_hint(tmp_path):
    # A push that exits non-zero with NO stderr (a killed/timed-out push) must not surface as a
    # blank "no error output" — the user gets an actionable hint instead.
    class _EmptyErrPushRepo:
        def remote_exists(self, name="origin"):
            return True

        def ref_exists(self, ref):
            return False

        def root_commit(self):
            return "fp"

        def read_tree_paths(self, ref):
            return {"fp/alice/drop/transcript.jsonl": "b1", "fp/alice/drop/manifest.json": "b2"}

        def ref_sha(self, ref):
            return "tip"

        def write_tree_from(self, entries):
            return "tree"

        def commit_tree_orphan(self, tree, message):
            return "commit"

        def update_ref(self, ref, sha):
            pass

        def delete_orphaned_objects(self, old):
            return 0

        def fetch_ref(self, refspec, *, remote="origin", filter_blobs=None, timeout=None, cancel=None):
            return True

        def push_ref(self, refspec, *, remote="origin", force_with_lease=None, timeout=None, cancel=None):
            return (False, "")  # non-zero exit, no stderr (killed / timed out)

    result = SharedSessionStore(_EmptyErrPushRepo()).unshare("alice", "drop")  # type: ignore[arg-type]

    assert result.pushed is False
    assert "timed out" in result.error  # actionable, not a blank "no output"


def test_manage_menu_empty_state_shows_a_visible_message(tmp_path, monkeypatch):
    # No shared sessions: a clear message must be SHOWN. Returning _MENU_UP re-shows the
    # sessions menu straight over it ("nothing shows"); _MENU_DONE leaves it on the screen.
    runner, _repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())  # nothing shared

    result = runner._manage_shared_sessions_menu()

    assert result == runner._MENU_DONE
    assert any("No sessions are shared" in m for m in runner.messages)


def test_manage_menu_fetches_origin_before_listing(tmp_path, monkeypatch):
    # The manage menu must sync origin first so it reflects what's actually shared there — not a
    # stale local/mirror view (where a local-only session masquerades as shared).
    runner, _repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())
    fetched: list = []
    runner._fetch_shared_with_cancel = lambda store, message: fetched.append(message) or True

    runner._manage_shared_sessions_menu()

    assert fetched and "Fetching shared sessions" in fetched[0]  # a fetch ran before the list


def test_manage_menu_lists_a_session_shared_from_another_machine(tmp_path, monkeypatch):
    # A session shared from another machine lives in the remote MIRROR, not the local ref. Once
    # the menu fetches, it must show it so the user can manage/unshare it here (the "it's on
    # origin only and I can't see it" case).
    from agitrack.sessions.store import REMOTE_MIRROR

    runner, repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())
    SharedSessionStore(repo, ref=REMOTE_MIRROR)._add_session(
        "tester", "from-laptop", "t", _manifest("from-laptop", session_id="laptop-sid", updated=5)
    )
    runner._fetch_shared_with_cancel = lambda store, message: True  # mirror already populated
    captured: dict = {}
    runner._select_popup = lambda title, options: captured.update(title=title, options=options) or None

    runner._manage_shared_sessions_menu()

    assert captured.get("options") and any("from-laptop" in option for option in captured["options"])


def test_share_session_signals_done_on_share_and_up_on_cancel(tmp_path, monkeypatch):
    # Per the menu rule: a completed action closes the menu (_MENU_DONE) so its progress shows;
    # backing out at the consent prompt does nothing and re-shows the list (_MENU_UP).
    runner, _repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())

    runner._select_popup = lambda title, options: "No, cancel"
    assert runner._share_session() == runner._MENU_UP  # declined → back to the list

    answers = iter(["Yes, share it", "No, I'll re-share manually"])
    runner._select_popup = lambda title, options: next(answers)
    assert runner._share_session() == runner._MENU_DONE  # shared → close the menu
