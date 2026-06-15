"""Sharing full agent sessions via git (issue #55).

Real temp repos exercise the history-free shared-session ref, redaction, identity
resolution, the Claude transcript import/export, and a push/fetch round-trip
through a local bare remote.
"""

import subprocess
from pathlib import Path

from agit.git import GitRepo
from agit.sessions import SharedSessionStore, github_login, redact_transcript
from agit.sessions.identity import slug


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


# --- redaction --------------------------------------------------------------


def test_redact_masks_secrets_and_home_path_but_keeps_structure():
    line = '{"cwd":"/Users/alice/Code/x","t":"api_key=sk-ABCDEFGHIJKLMNOP token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"}'
    out = redact_transcript(line)
    assert "sk-ABCDEFGHIJKLMNOP" not in out and "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in out
    assert "[REDACTED]" in out
    assert "/Users/alice" not in out and "/Users/user/Code/x" in out  # username masked, path kept
    assert out.startswith('{"cwd"')  # JSON shape preserved


def test_redact_leaves_ordinary_text_untouched():
    assert redact_transcript("just a normal sentence\nsecond line") == "just a normal sentence\nsecond line"


# --- identity ---------------------------------------------------------------


def test_github_login_prefers_gh(monkeypatch):
    import agit.sessions.identity as identity

    monkeypatch.setattr(identity.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(
        identity.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="octocat\n", stderr=""),
    )
    assert github_login() == "octocat"


def test_github_login_falls_back_to_git_name(tmp_path, monkeypatch):
    import agit.sessions.identity as identity

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


def test_fetch_lists_with_filter_and_reads_transcript_on_demand():
    # Listing fetches only the small manifests (blob filter); a chosen session's
    # large transcript is fetched on demand the first time it's read.
    fetches: list = []
    blobs = {"abc/me/sess/manifest.json": '{"updated": 1}'}  # transcript not local yet

    class FakeRepo:
        def remote_exists(self, name="origin"):
            return True

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
    assert fetches == ["blob:limit=16k"]  # listing used the size filter
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
    assert seen == [12.0]


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

    from agit.sessions import SharedEntry

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

    from agit.sessions import SharedEntry

    store = SharedSessionStore(FakeRepo())  # type: ignore[arg-type]
    entry = SharedEntry(github_id="me", name="sess", manifest={})
    assert store.read_transcript(entry) == "NEW shared latest"
    assert fetched == ["+refs/agit/shared-sessions:refs/agit/shared-sessions"]  # synced before reading


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

    from agit.sessions import SharedEntry

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

    from agit.sessions import SharedEntry

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


class _PublishFakeRepo:
    """Records fetch/push calls so the push-first publish path can be asserted.

    ``push_results`` is consumed one per push attempt (``(ok, stderr)``)."""

    def __init__(self, push_results):
        self.push_results = list(push_results)
        self.calls: list = []

    def remote_exists(self, name="origin"):
        return True

    def root_commit(self):
        return "fp"

    def ref_sha(self, ref):
        return "localtip"

    def read_tree_paths(self, ref):
        return {}

    def write_blob(self, content):
        return "blob"

    def write_tree_from(self, entries):
        return "tree"

    def commit_tree_orphan(self, tree, message):
        return "commit"

    def update_ref(self, ref, sha):
        pass

    def delete_orphaned_objects(self, old):
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
    assert repo.calls == ["push"]  # no fetch in the common case


def test_publish_retries_after_stale_lease(tmp_path):
    # A concurrent contributor moved the remote: the optimistic push is rejected
    # with a stale lease, so publish syncs and retries exactly once.
    repo = _PublishFakeRepo([(False, "! [rejected] shared (stale info)"), (True, "")])
    result = SharedSessionStore(repo).publish(  # type: ignore[arg-type]
        github_id="a", name="s", transcript="t", manifest=_manifest("s", session_id="id", updated=1)
    )
    assert result.pushed is True
    assert repo.calls == ["push", "fetch", "push"]  # push-first, sync, retry


def test_publish_does_not_retry_on_auth_failure(tmp_path):
    # A non-race failure (auth) must fail fast: no fetch, no second push, so a
    # broken credential can't spin the publish into a retry loop.
    repo = _PublishFakeRepo([(False, "fatal: Authentication failed for 'origin'")])
    result = SharedSessionStore(repo).publish(  # type: ignore[arg-type]
        github_id="a", name="s", transcript="t", manifest=_manifest("s", session_id="id", updated=1)
    )
    assert result.pushed is False
    assert repo.calls == ["push"]  # failed fast — no fetch/retry


def test_unshare_removes_only_that_entry(tmp_path):
    store = SharedSessionStore(_init_repo(tmp_path))
    store.publish(github_id="alice", name="keep", transcript="k", manifest=_manifest("keep", session_id="k", updated=1))
    store.publish(github_id="alice", name="drop", transcript="d", manifest=_manifest("drop", session_id="d", updated=2))
    store.unshare("alice", "drop")
    assert [e.name for e in store.entries()] == ["keep"]


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
    from agit.transcripts import claude

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
    from agit.config import AgitState

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
    assert AgitState(repo.repo).shared_origin_name("claude-copy-id") == "sess"


# --- auto-share opt-in state ------------------------------------------------


def test_state_auto_share_opt_in(tmp_path):
    from agit.config import AgitState

    state = AgitState(tmp_path)
    assert state.auto_share_enabled("sid") is False
    state.set_auto_share("sid", True)
    assert state.auto_share_enabled("sid") is True
    assert "sid" in state.auto_share_session_ids()
    state.set_auto_share("sid", False)
    assert state.auto_share_enabled("sid") is False
    assert state.auto_share_enabled(None) is False


def test_state_shared_session_lineage_chain(tmp_path):
    from agit.config import AgitState

    state = AgitState(tmp_path)
    assert state.session_lineage("a") == ["a"]
    # Two successive resume drifts: a -> b -> c.
    state.add_shared_session_alias("b", "a")
    state.add_shared_session_alias("c", "b")
    assert state.session_lineage("c") == ["c", "b", "a"]
    assert state.session_lineage("b") == ["b", "a"]
    # Persists to base state and survives reload.
    assert AgitState(tmp_path).session_lineage("c") == ["c", "b", "a"]
    # Defensive: a corrupt self-referential alias never loops.
    state.add_shared_session_alias("d", "d")  # ignored (new == previous)
    assert state.session_lineage("d") == ["d"]


def test_runner_recognises_shared_session_after_id_drift(tmp_path):
    # #55: a session shared under id "old" that the backend resumes as "new" must
    # still be marked shared and keep auto-sharing, via the recorded lineage.
    from proxy_helpers import make_runner
    from agit.config import AgitState

    (tmp_path / "repo").mkdir()
    repo = _init_repo(tmp_path / "repo")
    runner = make_runner()
    runner.base_repo = repo
    runner._debug = lambda *a, **k: None
    base_state = AgitState(repo.repo)
    runner._user_state = lambda: AgitState(repo.repo)
    runner._my_shared_session_ids = lambda: {"old"}

    # Before drift: "old" is recognised directly.
    assert runner._session_is_shared("old", {"old"}) is True
    # Auto-share opted in under "old".
    base_state.set_auto_share("old", True)

    # The backend forks "old" -> "new" on resume.
    runner._record_shared_alias_on_drift("old", "new")

    assert runner._session_is_shared("new", runner._my_shared_session_ids()) is True
    assert runner._session_auto_shared("new") is True


# --- Claude transcript export / import --------------------------------------


def test_claude_export_and_import_retargets_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    from agit.transcripts import claude

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
    from agit.transcripts import claude

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
    from agit.transcripts import opencode

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
    from agit.transcripts import opencode

    monkeypatch.setattr(opencode, "_run_export_pty", lambda repo, sid, *, sanitize=False: ("{not json", 0))
    assert opencode.export_session_raw(tmp_path, "ses_1") is None
    # A non-zero exit code is also treated as a failed export.
    monkeypatch.setattr(opencode, "_run_export_pty", lambda repo, sid, *, sanitize=False: ('{"info":{}}', 1))
    assert opencode.export_session_raw(tmp_path, "ses_1") is None


def test_opencode_import_runs_cli_and_checks_success(monkeypatch, tmp_path):
    from agit.transcripts import opencode

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
    from agit.transcripts import opencode

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
    from agit.transcripts import opencode

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
    from agit.transcripts import opencode

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
    from agit.transcripts import opencode

    monkeypatch.setattr(opencode, "session_belongs_to_repo", lambda repo, sid: sid == "mine")
    assert opencode.has_imported_session(tmp_path, "mine") is True
    assert opencode.has_imported_session(tmp_path, "other") is False
    assert opencode.has_imported_session(tmp_path, "") is False


def test_opencode_transcript_size_is_unavailable(tmp_path):
    from agit.transcripts import opencode

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
    from agit.config import AgitState, GlobalConfig

    (tmp_path / "repo").mkdir()
    repo = _init_repo(tmp_path / "repo")
    state = AgitState(tmp_path / "repo")
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
    secret = '{"t":"token sk-ABCDEFGHIJKLMNOPQR"}'
    runner, repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend(transcript=secret))

    runner._share_session()

    store = SharedSessionStore(repo)
    entries = store.entries()
    assert len(entries) == 1
    transcript = store.read_transcript(entries[0])
    assert "sk-ABCDEFGHIJKLMNOPQR" not in transcript and "[REDACTED]" in transcript  # redacted
    assert entries[0].manifest["session_id"] == "sid-123"
    assert any("Saved shared session" in m or "Shared" in m for m in runner.messages)


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


def test_share_push_cancel_shows_not_shared_until_keypress(tmp_path, monkeypatch):
    # Cancelling the push to origin must make the non-result unmistakable, via a
    # notice that waits for a keypress (not a mouse move) to dismiss.
    backend = _StubBackend(transcript="hello")
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    runner._publish_with_cancel = lambda *a, **k: {"cancelled": True}  # user pressed Esc
    notices: list = []
    runner._await_keypress = lambda msg: notices.append(msg)

    runner._share_session()

    assert any("NOT shared" in m and "cancel" in m.lower() for m in notices)
    assert runner.state.session_id not in runner._auto_share_hash  # not marked as pushed


def test_publish_with_cancel_returns_cancelled_when_drain_stops():
    from proxy_helpers import make_runner

    runner = make_runner()
    runner._render = lambda: None
    runner._set_message = lambda *a, **k: None
    runner._drain_pty_until_done_or_esc = lambda thread, **k: "cancel"  # user/Esc or timeout

    class _SlowStore:
        def __init__(self):
            self.cancelled = None

        def publish(self, *, github_id, name, transcript, manifest, timeout=None, cancel=None):
            self.cancelled = cancel
            cancel.wait(timeout=5)  # block until told to stop
            return None

    store = _SlowStore()
    out = runner._publish_with_cancel(store, github_id="me", name="s", transcript="t", manifest={})
    assert out == {"cancelled": True}


def test_publish_with_cancel_returns_result_on_success():
    from proxy_helpers import make_runner

    runner = make_runner()
    runner._render = lambda: None
    runner._set_message = lambda *a, **k: None

    class _OkStore:
        def publish(self, *, github_id, name, transcript, manifest, timeout=None, cancel=None):
            return "PUBLISHED"

    out = runner._publish_with_cancel(_OkStore(), github_id="me", name="s", transcript="t", manifest={})
    assert out == {"result": "PUBLISHED"}


def test_runner_share_unsupported_backend_shows_message(tmp_path, monkeypatch):
    class _OpenCodeStub:
        name = "opencode"
        supports_session_sharing = False

    runner, repo = _runner_with_store(tmp_path, monkeypatch, _OpenCodeStub())

    runner._share_session()

    assert SharedSessionStore(repo).entries() == []  # nothing shared
    assert any("isn't supported" in m and "opencode" in m for m in runner.messages)


def test_runner_resume_shared_imports_and_resumes(tmp_path, monkeypatch):
    from agit.config import AgitState

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
    assert AgitState(repo.repo).shared_origin_name("bob-sid") == "cool-fix"


def test_runner_resume_shared_crosses_backends(tmp_path, monkeypatch):
    # Active backend is Claude, but the shared entry is an OpenCode session: it
    # must be imported and resumed by a freshly-built OpenCode agent, not Claude.
    from agit.proxy import runner as runner_module

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
    from agit.sessions.store import PublishResult

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
    # The opt-in must survive across aGiT runs: it has to live in the BASE repo
    # state, not the session worktree (which is removed on exit).
    from agit.config import AgitState

    runner, repo = _runner_with_store(tmp_path, monkeypatch, _StubBackend())
    (tmp_path / "worktree").mkdir()
    runner.state = AgitState(tmp_path / "worktree")  # session state lives in the (ephemeral) worktree

    runner._set_session_auto_share("sid-123", True)

    assert AgitState(repo.repo).auto_share_enabled("sid-123") is True  # base repo → persists
    assert AgitState(tmp_path / "worktree").auto_share_enabled("sid-123") is False  # not in the worktree
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
    # First popup picks the (only) session; the "Manage" popup picks Unshare (3rd action).
    runner._select_popup = lambda title, options: options[2] if title.startswith("Manage") else options[0]

    runner._manage_shared_sessions_menu()

    assert SharedSessionStore(repo).entries() == []


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
    # Pick the session, then "Turn ON auto-update" (2nd action).
    runner._select_popup = lambda title, options: options[1] if title.startswith("Manage") else options[0]

    runner._manage_shared_sessions_menu()

    assert runner._session_auto_shared("sid-123") is True  # opt-in persisted
    entry = SharedSessionStore(repo).entries()[0]
    assert SharedSessionStore(repo).read_transcript(entry) == "newest turns"  # pushed on enable


def test_manage_update_now_shows_pushing_message(tmp_path, monkeypatch):
    # "Update now" must show the same "pushing to origin…" progress notice as the
    # initial share — not push silently while the user waits.
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
    # Pick the (only) session, then "Update now" (1st action) — both are options[0].
    runner._select_popup = lambda title, options: options[0]

    runner._manage_shared_sessions_menu()

    assert any("pushing to origin" in m.lower() for m in runner.messages)
    entry = SharedSessionStore(repo).entries()[0]
    assert SharedSessionStore(repo).read_transcript(entry) == "newest turns"  # pushed


def test_manage_update_cancel_shows_not_updated_until_keypress(tmp_path, monkeypatch):
    # Cancelling the update push surfaces a key-dismissed "NOT updated" notice, like
    # the initial share's cancel path.
    backend = _StubBackend(transcript="newest")
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    SharedSessionStore(repo).publish(
        github_id="tester",
        name="session-1",
        transcript="stale",
        manifest={"github_id": "tester", "name": "session-1", "session_id": "sid-123", "updated": 1},
    )
    runner._publish_with_cancel = lambda *a, **k: {"cancelled": True}  # user pressed Esc
    notices: list = []
    runner._await_keypress = lambda msg: notices.append(msg)
    runner._select_popup = lambda title, options: options[0]

    runner._manage_shared_sessions_menu()

    assert any("NOT updated" in m and "cancel" in m.lower() for m in notices)


def test_manage_menu_opens_without_fetch_or_transcript_read(tmp_path, monkeypatch):
    # The menu must open instantly: no network fetch, and no transcript read/redact
    # while building the list (the "takes a few seconds" bug).
    backend = _StubBackend()
    runner, repo = _runner_with_store(tmp_path, monkeypatch, backend)
    SharedSessionStore(repo).publish(
        github_id="tester",
        name="s1",
        transcript="t",
        manifest={"github_id": "tester", "name": "s1", "session_id": "sid-123", "updated": 1, "transcript_bytes": 5},
    )

    def boom_fetch(*a, **k):
        raise AssertionError("manage menu must not fetch from the network")

    def boom_read(*a, **k):
        raise AssertionError("manage menu must not read/redact transcripts to build the list")

    monkeypatch.setattr(repo, "fetch_ref", boom_fetch)
    backend.export_session_raw = boom_read
    captured = {}
    runner._select_popup = lambda title, options: captured.update(title=title, options=options) or None

    runner._manage_shared_sessions_menu()

    assert captured["title"].startswith("Your shared sessions")
    assert len(captured["options"]) == 1 and "s1" in captured["options"][0]


def test_shared_entry_status_is_size_based(tmp_path, monkeypatch):
    from agit.sessions import SharedEntry

    backend = _StubBackend(transcript="x" * 100)  # current transcript = 100 bytes
    runner, _repo = _runner_with_store(tmp_path, monkeypatch, backend)
    up_to_date = SharedEntry("tester", "s", {"session_id": "sid-123", "transcript_bytes": 100})
    grown = SharedEntry("tester", "s", {"session_id": "sid-123", "transcript_bytes": 50})
    unknown = SharedEntry("tester", "s", {"session_id": "sid-123"})  # no recorded size
    assert "up to date" in runner._shared_entry_status(up_to_date, "sid-123")
    assert "newer turns" in runner._shared_entry_status(grown, "sid-123")
    assert runner._shared_entry_status(unknown, "sid-123") == "shared"


def test_both_backends_flag_sharing_support():
    from agit.backends.proxy_agents import make_proxy_agent

    # Claude (per-session .jsonl) and OpenCode (export/import CLI) both have a
    # portable transcript, so both advertise session sharing (issue #55).
    assert make_proxy_agent("claude").supports_session_sharing is True
    assert make_proxy_agent("opencode").supports_session_sharing is True


def test_opencode_agent_delegates_sharing_to_transcript_module(tmp_path, monkeypatch):
    from agit.backends.proxy_agents import make_proxy_agent
    from agit.transcripts import opencode as opencode_session

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
