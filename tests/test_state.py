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
