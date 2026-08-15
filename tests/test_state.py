import json
import subprocess

from agitrack.backends.base import TokenUsage
from agitrack.config import AgitrackState


def test_exclude_path_resolved_via_git_only_once(tmp_path, monkeypatch):
    # ensure_local_ignore()/save() ask for the info/exclude path constantly; resolving it via a
    # `git rev-parse` subprocess every time was a chunk of the slow startup (and the "git" title
    # flicker). It must be cached per repo so git is spawned at most once, not on every call.
    import agitrack.config.state as state_mod

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    state_mod._EXCLUDE_PATH_CACHE.clear()
    calls = []
    real_run = subprocess.run

    def counting_run(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and "--git-path" in cmd and "info/exclude" in cmd:
            calls.append(cmd)
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(state_mod.subprocess, "run", counting_run)
    state = AgitrackState(tmp_path)
    for _ in range(5):
        state.ensure_local_ignore()
    assert len(calls) <= 1  # resolved once, then served from the cache


def test_state_is_repository_local(tmp_path):
    state = AgitrackState(tmp_path)
    state.add_declined(["new.py"])

    loaded = AgitrackState(tmp_path)
    assert loaded.path == tmp_path / ".agitrack" / "state.json"
    assert loaded.declined_untracked() == ["new.py"]


def test_backend_defaults_to_configured_default_not_opencode(tmp_path):
    # A fresh repo with no recorded backend must honour the configured default
    # rather than silently falling back to a hardcoded backend.
    state = AgitrackState(tmp_path, default_backend="claude")
    assert state.backend == "claude"

    # An explicitly null/empty stored backend also falls back to the default.
    state.data["backend"] = None
    assert state.backend == "claude"

    # A stale/unknown stored backend is coerced to the default rather than passed
    # through (which would later raise in make_proxy_agent).
    state.data["backend"] = "retired-agent"
    assert state.backend == "claude"

    # A known stored backend is honoured as-is.
    state.data["backend"] = "opencode"
    assert state.backend == "opencode"


def test_state_prunes_declined_untracked_files(tmp_path):
    state = AgitrackState(tmp_path)
    state.add_declined(["ignored.log", "keep.py", "removed.py"])

    state.keep_declined(["keep.py"])

    assert state.declined_untracked() == ["keep.py"]


def test_session_name_roundtrip(tmp_path):
    state = AgitrackState(tmp_path)
    assert state.session_name_for("sess-1") is None
    state.name_session("sess-1", "my-feature")

    assert AgitrackState(tmp_path).session_name_for("sess-1") == "my-feature"
    # Naming also stamps a real time, so a transcript-less session can be dated.
    import time

    assert abs(AgitrackState(tmp_path).session_named_at("sess-1") - time.time()) < 60
    assert AgitrackState(tmp_path).session_named_at("unknown") == 0.0
    # A None/empty id is a no-op, and clearing removes the name and its stamp.
    state.name_session(None, "ignored")
    state.name_session("sess-1", None)
    assert AgitrackState(tmp_path).session_name_for("sess-1") is None
    assert AgitrackState(tmp_path).session_named_at("sess-1") == 0.0


def test_trace_roundtrip(tmp_path):
    state = AgitrackState(tmp_path)
    state.append_trace("user", "hello")
    state.append_trace("agent", "hi")

    assert state.pending_trace() == [
        {"role": "user", "content": "hello"},
        {"role": "agent", "content": "hi"},
    ]
    state.clear_trace()
    assert state.pending_trace() == []


def test_state_adds_repo_local_git_exclude(tmp_path):
    git_info = tmp_path / ".git" / "info"
    git_info.mkdir(parents=True)
    exclude = git_info / "exclude"
    exclude.write_text("", encoding="utf-8")

    AgitrackState(tmp_path).save()

    assert ".agitrack/" in exclude.read_text(encoding="utf-8").splitlines()


def test_state_accumulates_pending_token_usage(tmp_path):
    state = AgitrackState(tmp_path)
    state.add_token_usage(TokenUsage(context=100, total=25, input=20, output=5, cache_read=3))
    state.add_token_usage(
        TokenUsage(context=120, total=12, input=10, output=2, reasoning=1, subagent_input=8, subagent_output=15)
    )

    assert state.pending_token_usage() == {
        "context": 120,
        "total": 37,
        "input": 30,
        "output": 7,
        "reasoning": 1,
        "cache_read": 3,
        "cache_write": 0,
        "subagent_input": 8,
        "subagent_output": 15,
        "subagent_reasoning": 0,
        "subagent_cache_read": 0,
        "subagent_cache_write": 0,
    }
    state.clear_trace()
    assert state.pending_token_usage()["total"] == 0


def test_backend_session_is_repo_scoped(tmp_path):
    state = AgitrackState(tmp_path)
    state.backend_session_id = "ses-1"

    assert state.backend_session_matches_repo() is True

    state.data["backend_session_repo"] = str(tmp_path / "other")
    assert state.backend_session_matches_repo() is False


def test_per_conversation_watermark_isolates_conversations(tmp_path):
    # Switching between backend conversations must not replay/double-count either one:
    # each remembers its OWN committed high-water mark.
    state = AgitrackState(tmp_path)

    # Tracking conversation A: the current conversation uses the single watermark.
    state.backend_session_id = "A"
    assert state.backend_message_id_for("A") is None  # nothing committed yet
    state.set_backend_message_id("A", "a-msg-2")
    assert state.backend_message_id_for("A") == "a-msg-2"
    assert state.last_backend_message_id == "a-msg-2"  # legacy value kept in sync

    # User switches to conversation B inside the backend. While A is still the tracked
    # (backend_session_id) conversation, B is a DIFFERENT conversation: fresh, no replay.
    assert state.backend_message_id_for("B") is None
    state.backend_session_id = "B"  # the switch is adopted
    state.set_backend_message_id("B", "b-msg-5")

    # Switching back to A must read A's own mark, NOT B's — so A's committed turns are
    # not replayed. (A is now the "other" conversation, read from the per-conversation map.)
    assert state.backend_message_id_for("A") == "a-msg-2"
    assert state.backend_message_id_for("B") == "b-msg-5"


def test_a_new_conversation_never_inherits_the_previous_conversations_watermark(tmp_path):
    # THE BUG (2026-08-15, commit bccd5332): the map was consulted only for a conversation
    # OTHER than the tracked one, so a brand-new conversation — backend_session_id already
    # reassigned to it, nothing of its own committed — read the global watermark still holding
    # the PREVIOUS conversation's id. That id matches no turn boundary in the new conversation
    # and its marked_at is (rightly) absent, so turns_after fell through to its last-resort
    # "newest turn only" branch and silently discarded every earlier turn: the user's prompt,
    # its tokens and its trace reached no commit at all.
    state = AgitrackState(tmp_path)
    state.backend_session_id = "old"
    state.set_backend_message_id("old", "msg-from-old", marked_at=1000)

    state.backend_session_id = "brand-new"  # aGiTrack adopts a fresh backend conversation

    assert state.backend_message_id_for("brand-new") is None  # no mark of its own -> all new
    assert state.backend_message_marked_at_for("brand-new") is None
    assert state.backend_message_id_for("old") == "msg-from-old"  # the old one keeps its mark


def test_a_watermark_predating_the_per_conversation_map_is_still_honoured(tmp_path):
    # Upgrade continuity: set_backend_message_id always writes the global AND the map entry,
    # so a global appearing NOWHERE in the map predates the map and is the tracked
    # conversation's own mark. Dropping it would re-export that conversation's whole history
    # on the first run after an upgrade — the very thing the watermark exists to prevent.
    state = AgitrackState(tmp_path)
    state.backend_session_id = "A"
    state.data["last_backend_message_id"] = "legacy-msg"  # written before the map existed
    state.save()

    assert state.backend_message_id_for("A") == "legacy-msg"


def test_switching_back_reads_the_conversations_own_mark_not_the_global(tmp_path):
    # The tracked conversation now reads its OWN map entry too, so a switch away and back is
    # exact even while the global still holds whatever committed most recently.
    state = AgitrackState(tmp_path)
    state.backend_session_id = "A"
    state.set_backend_message_id("A", "a-msg", marked_at=100)
    state.backend_session_id = "B"
    state.set_backend_message_id("B", "b-msg", marked_at=200)
    state.backend_session_id = "A"  # switched back; the global still says "b-msg"

    assert state.last_backend_message_id == "b-msg"
    assert state.backend_message_id_for("A") == "a-msg"


def test_current_conversation_watermark_still_uses_single_value(tmp_path):
    # A plain reset of the legacy single watermark still governs the CURRENT conversation,
    # so all existing "recompute from scratch on resume" resets keep working.
    state = AgitrackState(tmp_path)
    state.backend_session_id = "A"
    state.set_backend_message_id("A", "a-msg-9")
    state.last_backend_message_id = None  # an existing reset path
    assert state.backend_message_id_for("A") is None  # current conversation recomputes


def test_trace_turn_limit_defaults_and_reads_config(tmp_path):
    state = AgitrackState(tmp_path)
    assert state.trace_turn_limit == 5

    config = tmp_path / ".agitrack" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"trace_turn_limit": 3}\n', encoding="utf-8")

    assert AgitrackState(tmp_path).trace_turn_limit == 3


# --- issue #17: corrupt state must not brick startup; writes are atomic --------


def test_corrupt_state_json_falls_back_to_defaults_and_keeps_backup(tmp_path):
    state_path = tmp_path / ".agitrack" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"agitrack_session_id": "agit-x", "backe', encoding="utf-8")  # truncated mid-write

    state = AgitrackState(tmp_path)  # must not raise

    assert state.session_id.startswith("agitrack-")
    # The corrupt file is kept aside for debugging, not silently destroyed.
    backup = state_path.with_name("state.json.bak")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8").endswith('"backe')
    # And the state is usable again: save() writes a fresh valid file.
    state.save()
    assert AgitrackState(tmp_path).session_id == state.session_id


def test_non_dict_state_json_is_treated_as_corrupt(tmp_path):
    state_path = tmp_path / ".agitrack" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text('["not", "a", "dict"]', encoding="utf-8")

    state = AgitrackState(tmp_path)  # must not raise

    assert state.session_id.startswith("agitrack-")
    assert state_path.with_name("state.json.bak").exists()


def test_corrupt_config_json_falls_back_to_defaults(tmp_path):
    config_path = tmp_path / ".agitrack" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{not json", encoding="utf-8")

    state = AgitrackState(tmp_path)  # must not raise

    assert state.trace_turn_limit == 5  # default config


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    state = AgitrackState(tmp_path)
    state.save()

    state_dir = tmp_path / ".agitrack"
    assert (state_dir / "state.json").exists()
    assert list(state_dir.glob("*.tmp")) == []
    # The written file is complete, valid JSON round-tripping the data.
    import json

    with (state_dir / "state.json").open(encoding="utf-8") as handle:
        assert json.load(handle)["agitrack_session_id"] == state.session_id


def test_missing_info_exclude_is_created_with_agitrack_ignore(tmp_path):
    # Issue #26: repos created without the default template have no
    # info/exclude; saving state must create it rather than leave .agitrack/
    # unignored.
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    exclude = tmp_path / ".git" / "info" / "exclude"
    if exclude.exists():
        exclude.unlink()

    state = AgitrackState(tmp_path)
    state.save()

    assert exclude.exists()
    assert ".agitrack/" in exclude.read_text(encoding="utf-8").splitlines()


def test_no_exclude_created_outside_a_git_repo(tmp_path):
    state = AgitrackState(tmp_path)  # tmp_path is not a git repo
    state.save()
    assert not (tmp_path / ".git").exists()


def test_session_origin_event_roundtrip_and_one_shot(tmp_path):
    state = AgitrackState(tmp_path)
    assert state.session_origin_event() is None  # none by default

    state.set_session_origin_event(kind="copy", source="ses_orig", collaborator="alice", source_name="feature-x")
    event = state.session_origin_event()
    assert event["kind"] == "copy"
    assert event["source"] == "ses_orig"
    assert event["collaborator"] == "alice"
    assert event["source_name"] == "feature-x"
    assert isinstance(event["at"], int)

    # Survives a reload (persisted to state.json), then clears as a one-shot.
    assert AgitrackState(tmp_path).session_origin_event()["kind"] == "copy"
    state.clear_session_origin_event()
    assert state.session_origin_event() is None
    assert AgitrackState(tmp_path).session_origin_event() is None


# --- lost updates: two live objects over ONE state file -------------------------------
# AgitrackState.save() used to serialize its whole in-memory dict over the file. Atomicity
# is not isolation: a second instance on the same path — a second object in this process
# (without a worktree the session state and the repo-root state ARE the same file), or
# another aGiTrack process on the repo (background tracker, dashboard, a --json run) —
# held its own copy and whichever saved last silently discarded the other's keys. A session
# name written through a second instance vanished on the session state's next setter.


def test_a_second_instances_write_is_not_clobbered_by_the_first(tmp_path):
    session = AgitrackState(tmp_path)
    session.backend = "claude"

    # a second live object over the SAME file records a name…
    other = AgitrackState(tmp_path)
    other.name_session("sid-1", "jubilee")

    # …and the first instance's next save must not erase it.
    session.backend_session_id = "sid-1"

    assert AgitrackState(tmp_path).session_name_for("sid-1") == "jubilee"
    assert AgitrackState(tmp_path).backend_session_id == "sid-1"


def test_the_first_instance_also_survives_the_second(tmp_path):
    first = AgitrackState(tmp_path)
    first.name_session("sid-1", "jubilee")

    second = AgitrackState(tmp_path)  # loads the name, then writes something else
    second.backend = "codex"

    reloaded = AgitrackState(tmp_path)
    assert reloaded.session_name_for("sid-1") == "jubilee"
    assert reloaded.data["backend"] == "codex"


def test_a_key_this_instance_deleted_is_still_deleted(tmp_path):
    # Merging must not resurrect a removed key: clearing a pending name has to stick even
    # though the on-disk file still carries it.
    state = AgitrackState(tmp_path)
    state.remember_pending_session_name("jubilee")
    AgitrackState(tmp_path).backend = "codex"  # someone else rewrites the file meanwhile

    state.remember_pending_session_name(None)

    assert AgitrackState(tmp_path).pending_session_name is None
    assert AgitrackState(tmp_path).data["backend"] == "codex"


def test_a_stale_instance_does_not_revert_a_key_it_never_touched(tmp_path):
    # The long-lived case: an instance loaded minutes ago writes ONE key. Everything it did
    # not touch must keep the value the other writer left, not the value it remembers.
    stale = AgitrackState(tmp_path)
    stale.backend = "claude"

    AgitrackState(tmp_path).backend_session_id = "sid-new"

    stale.name_session("sid-new", "aurora")

    reloaded = AgitrackState(tmp_path)
    assert reloaded.backend_session_id == "sid-new"  # not reverted to None
    assert reloaded.session_name_for("sid-new") == "aurora"


def test_the_repo_config_file_is_merged_too(tmp_path):
    # <repo>/.agitrack/config.json is written by TWO classes with disjoint key sets:
    # AgitrackState._save_config and GlobalConfig.save_repo (which the dashboard daemon also
    # writes). An unmerged write here dropped every setting the other one owns.
    from agitrack.config.settings import GlobalConfig

    state = AgitrackState(tmp_path)
    state.merge_branch = "main"

    overlay = GlobalConfig(path=tmp_path / "global.json")
    overlay.load_repo_overlay(tmp_path)
    overlay.set("learning_model", "gpt-5.4-mini", scope="repo")

    state.summarization_enabled = False

    written = json.loads((tmp_path / ".agitrack" / "config.json").read_text(encoding="utf-8"))
    assert written["merge_branch"] == "main"
    assert written["learning_model"] == "gpt-5.4-mini"
    assert written["summarization_enabled"] is False


def test_suspended_saves_keep_mutations_in_memory(tmp_path):
    state = AgitrackState(tmp_path)
    state.backend = "claude"

    with state.suspend_saves():
        state.backend = "codex"
        state.backend_session_id = "sid-x"
        assert state.backend == "codex"  # visible in memory
        assert AgitrackState(tmp_path).data["backend"] == "claude"  # nothing written

    state.save()
    assert AgitrackState(tmp_path).data["backend"] == "codex"
    assert AgitrackState(tmp_path).backend_session_id == "sid-x"


def test_a_second_state_on_the_active_sessions_config_does_not_drop_its_settings(tmp_path):
    # The env-copy flow opens a SECOND AgitrackState on a worktree path to stamp
    # `copy_full_env` — and for the ACTIVE session that path is the one `self.state` already
    # holds, so the stamp and the session's own settings were two copies of one config.json.
    session = AgitrackState(tmp_path)
    session.merge_branch = "main"
    session.summarization_model = "gpt-5.4-mini"

    AgitrackState(tmp_path).copy_full_env = True

    session.summarization_enabled = False

    reloaded = AgitrackState(tmp_path)
    assert reloaded.copy_full_env is True
    assert reloaded.merge_branch == "main"
    assert reloaded.summarization_model == "gpt-5.4-mini"
    assert reloaded.summarization_enabled is False


def _repo_with_history(tmp_path):
    import subprocess

    from agitrack.git import GitRepo

    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "a.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return GitRepo.discover(root)


def _porcelain(repo):
    import subprocess

    # core.excludesFile=/dev/null: this machine's personal global ignore must not mask the leak,
    # which is exactly how it stayed invisible on the developer's own box.
    return subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", "status", "-uall", "--porcelain"],
        cwd=repo.repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout


def test_the_state_dir_ignores_itself_the_moment_it_exists(tmp_path):
    """`.git/info/exclude` was the only thing keeping `.agitrack/` out of the user's tree, and it
    is written by a DIFFERENT code path — so any run that created a lock, a config or a dashboard
    log and then exited before tracking started left `?? .agitrack/` behind, in a repo aGiTrack
    had just said it never touched. A `.gitignore` holding `*` inside the directory needs no
    second code path and no ordering."""
    from agitrack.fileio import ensure_state_dir

    repo = _repo_with_history(tmp_path)
    ensure_state_dir(repo.repo / ".agitrack")

    assert (repo.repo / ".agitrack" / ".gitignore").read_text(encoding="utf-8").endswith("*\n")
    assert _porcelain(repo) == ""


def test_taking_the_repo_lock_alone_never_dirties_the_tree(tmp_path):
    """Acquiring the lock is often the FIRST thing a run does, before the exclude line is
    written. A run refused as a second writer, or aborted, must leave nothing behind."""
    from agitrack.git import RepoLock

    repo = _repo_with_history(tmp_path)
    lock = RepoLock(repo.repo / ".agitrack" / "lock")
    assert lock.acquire()
    try:
        assert _porcelain(repo) == ""
    finally:
        lock.release()
    assert _porcelain(repo) == ""


def test_writing_any_state_file_ignores_the_directory(tmp_path):
    """Every writer goes through atomic_write_text, so none of them can be the one that forgets."""
    from agitrack.fileio import atomic_write_text

    repo = _repo_with_history(tmp_path)
    atomic_write_text(repo.repo / ".agitrack" / "state.json", "{}")
    assert _porcelain(repo) == ""


def test_the_self_ignore_is_only_written_inside_the_state_dir(tmp_path):
    """ensure_state_dir is used for ordinary directories too; it must never drop a `*` gitignore
    into one of the user's own."""
    from agitrack.fileio import ensure_state_dir

    other = tmp_path / "not-ours" / "nested"
    ensure_state_dir(other)
    assert other.is_dir()
    assert not (other / ".gitignore").exists()
    assert not (tmp_path / "not-ours" / ".gitignore").exists()


def test_the_claude_settings_file_agitrack_writes_is_excluded(tmp_path):
    """aGiTrack excluded its own `.agitrack/` but never `.claude/settings.local.json`, which it
    also writes (the Stop / SessionStart hooks). A new user saw `?? .claude/` the moment they ran
    `agitrack -b`; in a submodule it surfaced in the parent as ` M vendor/sub`. It stayed
    invisible on the developers' machines only because of a personal global ignore rule aGiTrack
    did not create."""
    from agitrack.config.state import AgitrackState

    repo = _repo_with_history(tmp_path)
    AgitrackState(repo.repo).ensure_local_ignore()
    (repo.repo / ".claude").mkdir()
    (repo.repo / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")

    assert _porcelain(repo) == ""
    exclude = (repo.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert ".claude/settings.local.json" in exclude
    assert ".agitrack/" in exclude


def test_ensure_local_ignore_is_idempotent(tmp_path):
    from agitrack.config.state import AgitrackState

    repo = _repo_with_history(tmp_path)
    state = AgitrackState(repo.repo)
    state.ensure_local_ignore()
    state.ensure_local_ignore()
    state.ensure_local_ignore()

    lines = (repo.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert lines.count(".agitrack/") == 1
    assert lines.count(".claude/settings.local.json") == 1


def test_add_tracked_never_stages_agitracks_own_edits(tmp_path):
    """B5: in a repo that TRACKS `.claude/`, aGiTrack's own hook edits were committed into the
    user's history — `add_tracked()` is `git add -u`, which has no `_NEVER_STAGE_PREFIXES`
    filter. A machine-absolute venv path landed in a file the whole team shares, and stopping
    then left the repo dirty with a change the user never made."""
    import subprocess

    repo = _repo_with_history(tmp_path)
    settings = repo.repo / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text('{"hooks": {}}\n', encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".claude"], cwd=repo.repo, check=True)
    subprocess.run(["git", "commit", "-qm", "team settings"], cwd=repo.repo, check=True)

    # aGiTrack rewrites it (adding its Stop hook); the user edits a file of their own.
    settings.write_text('{"hooks": {"Stop": ["/home/someone/.venv/bin/python"]}}\n', encoding="utf-8")
    (repo.repo / "a.txt").write_text("edited by the agent\n", encoding="utf-8")

    repo.add_tracked()

    staged = repo.staged_paths()
    assert "a.txt" in staged  # the agent's real work still gets committed
    assert ".claude/settings.local.json" not in staged


def test_a_config_with_a_utf8_bom_is_read_not_discarded(tmp_path, monkeypatch):
    """A realistic Windows footgun: `Out-File -Encoding utf8` writes a BOM, which makes
    json.load raise — and the whole config was then silently discarded and replaced with
    defaults, with no warning. The user's settings simply stopped applying."""
    import json

    from agitrack.config import GlobalConfig

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(cfg))
    (cfg / "config.json").write_text(
        json.dumps({"default_backend": "codex", "manual_commits": True}), encoding="utf-8-sig"
    )

    config = GlobalConfig()

    assert config.default_backend == "codex"
    assert config.manual_commits is True


def test_repo_state_with_a_utf8_bom_is_read_not_quarantined(tmp_path):
    import json

    from agitrack.config import AgitrackState

    repo = _repo_with_history(tmp_path)
    state_path = repo.repo / ".agitrack" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"backend": "opencode"}), encoding="utf-8-sig")

    assert AgitrackState(repo.repo).backend == "opencode"
    assert not state_path.with_name("state.json.bak").exists()  # not quarantined as corrupt
