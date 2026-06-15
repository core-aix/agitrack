"""Non-blocking commit summarization (issue #8).

The reported problems, each pinned by a test here:
- the summary LLM call blocked the proxy UI → commits now happen immediately
  and the summary is computed on a worker thread, then amended in;
- the summary appeared after the prompts → it now leads the message, its
  first line (72-char budget) is the subject; the prompts are not duplicated
  into the message (the interaction trace already carries them);
- a commit was re-amended although nothing changed → applying a summary is
  idempotent and an already-summarized message is never amended again;
- integration raced the summary → it waits up to SUMMARY_WAIT_SECONDS, then
  proceeds with the summary landing in git notes only;
- summarization cost is tracked → summary_model / summary_tokens_* metadata.
"""

import threading
import time

from agit.commits import apply_summary_to_message, build_agent_commit_message, summary_metadata_lines
from agit.config import AgitState
from agit.git import GitRepo

from proxy_helpers import make_runner

SUMMARY = "Implement the widget renderer with caching\n\nAlso reworks the cache keys."

# The interaction trace is now the summarizer's sole input (the second arg to
# _start_commit_summary), in place of the turns list it used to receive.
_TRACE_TEXT = "## User\n\nplease add the widget renderer\n\n## Agent\n\nImplemented the widget renderer with caching."


# --- message format (summary first, subject from summary) ---------------------


def _base_message(**overrides) -> str:
    kwargs = dict(
        latest_prompt="please add the widget renderer / and cache it",
        trace=[{"role": "user", "content": "please add the widget renderer"}],
        backend="claude",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="m1",
        session_name="s1",
    )
    kwargs.update(overrides)
    return build_agent_commit_message(**kwargs)


def test_summary_leads_message_and_takes_subject():
    message = _base_message(summary=SUMMARY)
    assert message.startswith("<aGiT> Implement the widget renderer with caching\n")
    body = message.split("\n", 1)[1]
    # The rest of the summary is the body's first paragraph (no # Summary
    # section), then straight to the trace — no # Prompts duplication.
    assert "# Summary" not in message
    assert "# Prompts" not in message
    assert body.lstrip("\n").startswith("Also reworks the cache keys.")
    assert body.index("Also reworks the cache keys.") < body.index("# Interaction Trace")
    # The prompt is recoverable from the trace's ## User section.
    assert "please add the widget renderer" in body.split("# Interaction Trace")[1]


def test_long_summary_first_line_is_truncated_to_subject_width():
    message = _base_message(summary="word " * 40)
    subject = message.splitlines()[0]
    assert subject.startswith("<aGiT> ")
    assert len(subject) <= 72
    assert subject.endswith("...")


def test_without_summary_prompts_still_head_the_message():
    message = _base_message()
    assert message.startswith("<aGiT> please add the widget renderer / and cache it")
    assert "# Summary" not in message
    assert "# Prompts" not in message


def test_summary_metadata_lines_record_cost():
    lines = summary_metadata_lines(model="cheap-model", tokens_input=120, tokens_output=40)
    assert lines == [
        "summary_model: cheap-model",
        "summary_tokens_input: 120",
        "summary_tokens_output: 40",
    ]
    message = _base_message(summary=SUMMARY, summary_metadata=lines)
    assert message.index("summary_model: cheap-model") < message.index("agit_version:")


def test_apply_summary_rewrites_subject_and_preserves_everything():
    original = _base_message()
    amended = apply_summary_to_message(
        original, SUMMARY, summary_metadata=summary_metadata_lines(model="m", tokens_input=5)
    )
    assert amended.startswith("<aGiT> Implement the widget renderer with caching\n")
    # The rest of the summary is the first paragraph (no # Summary section).
    assert "# Summary" not in amended
    assert "# Prompts" not in amended
    assert "Also reworks the cache keys." in amended.split("\n", 1)[1].split("# Interaction Trace")[0]
    # The prompt is preserved in the trace, not a separate # Prompts section.
    assert "please add the widget renderer" in amended.split("# Interaction Trace")[1]
    # Trace and metadata survive; metrics land before the version line.
    assert "# Interaction Trace" in amended
    assert "backend_session_id: ses-1" in amended
    assert amended.index("summary_tokens_input: 5") < amended.index("agit_version:")


def test_apply_summary_is_idempotent():
    # A summarized message is marked by its summary_model: metadata (added with the
    # summary, as in production); a second apply sees it and returns unchanged.
    meta = summary_metadata_lines(model="m")
    once = apply_summary_to_message(_base_message(), SUMMARY, summary_metadata=meta)
    twice = apply_summary_to_message(once, "a different summary", summary_metadata=meta)
    assert twice == once  # an already-summarized message is never rewritten


# --- runner: async worker, amend safety, integration deferral -----------------


class FakeSummarizer:
    """Deterministic stand-in; `gate` (if set) blocks the worker so tests can
    observe the non-blocking window; `fail`/`fail_session` (if set) raise from
    the corresponding call, mimicking an unusable summary (#8)."""

    gate: "threading.Event | None" = None
    fail: "Exception | None" = None
    fail_session: "Exception | None" = None

    def __init__(self, backend, *, model=None):
        self.backend = backend
        self.model = model
        self.tokens_input = 7
        self.tokens_output = 3

    def summarize_commit(self, *, trace):
        if FakeSummarizer.gate is not None:
            FakeSummarizer.gate.wait(timeout=10)
        if FakeSummarizer.fail is not None:
            raise FakeSummarizer.fail
        self.trace = trace
        return SUMMARY

    def update_session_summary(self, *, current_summary, trace, commit_summary):
        if FakeSummarizer.fail_session is not None:
            raise FakeSummarizer.fail_session
        return "rolling narrative v2"


def _summary_runner(tmp_path, monkeypatch):
    monkeypatch.setattr("agit.summaries.Summarizer", FakeSummarizer)
    monkeypatch.setenv("AGIT_CONFIG_DIR", str(tmp_path / "agit-config"))
    FakeSummarizer.gate = None
    FakeSummarizer.fail = None
    FakeSummarizer.fail_session = None
    repo = GitRepo.init(tmp_path)
    base = repo.current_branch()
    repo.switch("agit/test/s1/t1", create=True)
    runner = make_runner(repo=repo, state=AgitState(tmp_path), worktree=object(), _base_branch=base)
    runner.global_config = None
    runner._render = lambda *a, **k: None
    return runner, repo


def _commit_change(repo: GitRepo, name: str, message: str) -> str:
    (repo.repo / name).write_text(f"{name}\n", encoding="utf-8")
    repo.stage_paths([name])
    return repo.commit(message)


def _finish_summary(runner):
    assert runner._summary_thread is not None
    runner._summary_thread.join(timeout=10)
    runner._service_commit_summary()


def test_commit_path_does_not_block_on_summarization(tmp_path, monkeypatch):
    runner, repo = _summary_runner(tmp_path, monkeypatch)
    FakeSummarizer.gate = threading.Event()  # summary hangs until released
    sha = _commit_change(repo, "a.txt", "<aGiT> prompt subject")

    started = time.monotonic()
    runner._start_commit_summary(sha, _TRACE_TEXT)
    assert time.monotonic() - started < 1.0  # returned while the LLM call hangs
    assert runner._summary_pending is not None
    # The "summarizing…" popup names the session but NOT the commit hash (the
    # hash is noise to the user while the summary is in flight).
    assert "summarizing" in (runner.message or "")
    assert sha not in (runner.message or "")
    assert repo.commit_message("HEAD").startswith("<aGiT> prompt subject")  # commit untouched so far

    FakeSummarizer.gate.set()
    _finish_summary(runner)
    head = repo.commit_message("HEAD")
    assert head.startswith("<aGiT> Implement the widget renderer with caching")
    assert "# Prompts" not in head  # prompts are not duplicated into a section
    assert "summary_tokens_input: 7" in head and "summary_tokens_output: 3" in head
    assert runner._summary_pending is None
    # Summary and rolling session summary are queryable as git notes too.
    final = repo.rev_parse("HEAD")
    assert "widget renderer" in (repo.notes_show(final, namespace="agit/commit-summary") or "")
    assert runner.state.session_summary == "rolling narrative v2"
    assert (repo.notes_show(final, namespace="agit/session-summary") or "").startswith("rolling narrative")


def test_summarizing_notice_precedes_created_popup_worktree(tmp_path, monkeypatch):
    # The reported bug: the "Created <aGiT> commit … merged" popup appeared
    # before the "summarizing…" one. A worktree session announces the commit
    # only at integration, so while the summary is in flight only the
    # "summarizing…" notice may show — never a premature "Created" popup.
    runner, repo = _summary_runner(tmp_path, monkeypatch)
    FakeSummarizer.gate = threading.Event()  # hold the summary open
    sha = _commit_change(repo, "a.txt", "<aGiT> prompt subject")
    runner._last_agent_commit_id = repo.short_sha(sha)
    runner._commit_merged_pending = True  # armed by on_commit_fn at commit time

    runner._start_commit_summary(sha, _TRACE_TEXT)

    assert "summarizing" in (runner.message or "")
    assert "Created <aGiT> commit" not in (runner.message or "")
    # Only at integration does the "created (summarized)" popup appear.
    FakeSummarizer.gate.set()
    _finish_summary(runner)
    runner._announce_agent_commit()
    assert "Created <aGiT> commit" in (runner.message or "") and "(summarized)" in runner.message


def test_no_worktree_commit_announced_only_after_summary(tmp_path, monkeypatch):
    # A no-worktree session has no integration step to announce its commit, so
    # the "created" popup is shown after the summary lands — still after, never
    # before, the "summarizing…" notice.
    runner, repo = _summary_runner(tmp_path, monkeypatch)
    runner.worktree = None  # delegates to the active session
    FakeSummarizer.gate = threading.Event()
    sha = _commit_change(repo, "a.txt", "<aGiT> prompt subject")
    runner._last_agent_commit_id = repo.short_sha(sha)
    runner._commit_merged_pending = True

    runner._start_commit_summary(sha, _TRACE_TEXT)
    assert "summarizing" in (runner.message or "")
    assert "Created <aGiT> commit" not in (runner.message or "")

    FakeSummarizer.gate.set()
    _finish_summary(runner)
    # The summary service announced the commit (nothing else would have).
    assert "Created <aGiT> commit" in (runner.message or "")
    assert runner._commit_merged_pending is False


def test_summary_after_integration_lands_as_notes_only(tmp_path, monkeypatch):
    runner, repo = _summary_runner(tmp_path, monkeypatch)
    sha = _commit_change(repo, "a.txt", "<aGiT> prompt subject")
    runner._start_commit_summary(sha, _TRACE_TEXT)
    runner._summary_thread.join(timeout=10)
    # The commit integrated (base advanced) before the summary arrived.
    full = repo.rev_parse("HEAD")
    repo._run(["git", "branch", "-f", runner._base_branch, full])

    runner._service_commit_summary()

    assert repo.commit_message("HEAD").startswith("<aGiT> prompt subject")  # no amend of integrated history
    assert "widget renderer" in (repo.notes_show(full, namespace="agit/commit-summary") or "")


def test_summary_with_staged_changes_lands_as_notes_only(tmp_path, monkeypatch):
    runner, repo = _summary_runner(tmp_path, monkeypatch)
    sha = _commit_change(repo, "a.txt", "<aGiT> prompt subject")
    runner._start_commit_summary(sha, _TRACE_TEXT)
    runner._summary_thread.join(timeout=10)
    # The next turn already staged work: --amend would swallow it.
    (repo.repo / "next.txt").write_text("next\n", encoding="utf-8")
    repo.stage_paths(["next.txt"])

    runner._service_commit_summary()

    assert repo.commit_message("HEAD").startswith("<aGiT> prompt subject")


def test_summary_after_head_moved_lands_as_notes_only(tmp_path, monkeypatch):
    runner, repo = _summary_runner(tmp_path, monkeypatch)
    sha = _commit_change(repo, "a.txt", "<aGiT> first")
    runner._start_commit_summary(sha, _TRACE_TEXT)
    runner._summary_thread.join(timeout=10)
    first_full = repo.rev_parse("HEAD")
    _commit_change(repo, "b.txt", "<aGiT> second")

    runner._service_commit_summary()

    assert repo.commit_message("HEAD").startswith("<aGiT> second")
    assert repo.commit_message(first_full).startswith("<aGiT> first")
    assert "widget renderer" in (repo.notes_show(first_full, namespace="agit/commit-summary") or "")


def test_unusable_summary_keeps_prompt_led_message(tmp_path, monkeypatch):
    # The backend answered "You've hit your session limit..." instead of a
    # summary (#8): the Summarizer raises, no amend happens, and the commit
    # keeps the user prompt as its subject.
    from agit.summaries import UnusableSummaryError

    runner, repo = _summary_runner(tmp_path, monkeypatch)
    FakeSummarizer.fail = UnusableSummaryError("You've hit your session limit.")
    sha = _commit_change(repo, "a.txt", "<aGiT> prompt subject")

    runner._start_commit_summary(sha, _TRACE_TEXT)
    _finish_summary(runner)

    assert repo.commit_message("HEAD").startswith("<aGiT> prompt subject")
    full = repo.rev_parse("HEAD")
    assert repo.notes_show(full, namespace="agit/commit-summary") is None
    assert "keeping the prompt-based message" in (runner.message or "")
    assert runner._summary_pending is None  # integration is not held back


def test_failed_rolling_summary_does_not_discard_commit_summary(tmp_path, monkeypatch):
    runner, repo = _summary_runner(tmp_path, monkeypatch)
    FakeSummarizer.fail_session = RuntimeError("session summary failed")
    runner.state.session_summary = "previous narrative"
    sha = _commit_change(repo, "a.txt", "<aGiT> prompt subject")

    runner._start_commit_summary(sha, _TRACE_TEXT)
    _finish_summary(runner)

    # The commit summary still lands (subject + notes)...
    assert repo.commit_message("HEAD").startswith("<aGiT> Implement the widget renderer")
    full = repo.rev_parse("HEAD")
    assert "widget renderer" in (repo.notes_show(full, namespace="agit/commit-summary") or "")
    # ...and the previous rolling summary stays current instead of being lost.
    assert runner.state.session_summary == "previous narrative"


def test_summary_popups_name_the_owning_session(tmp_path, monkeypatch):
    # Background sessions summarize too: the "summarizing…" popup must say which
    # session it is about, and a summary that lands after the user switched away
    # must still be applied to the OWNING session's commit, not the active one.
    runner, repo = _summary_runner(tmp_path, monkeypatch)
    runner.name = "feature-x"
    sha = _commit_change(repo, "a.txt", "<aGiT> prompt subject")

    runner._start_commit_summary(sha, _TRACE_TEXT)
    assert "in session 'feature-x'" in (runner.message or "")

    runner.name = "other"  # the user switched sessions before the summary landed
    _finish_summary(runner)
    # The summary was amended into the owning session's commit (correct
    # attribution) and the owning session is flagged as summarized so its
    # eventual "created & merged" notice can say so.
    assert repo.commit_message("HEAD").startswith("<aGiT> Implement the widget renderer")
    assert runner._commit_summarized is True


def test_failed_summary_popup_names_the_owning_session(tmp_path, monkeypatch):
    from agit.summaries import UnusableSummaryError

    runner, repo = _summary_runner(tmp_path, monkeypatch)
    runner.name = "feature-x"
    FakeSummarizer.fail = UnusableSummaryError("You've hit your session limit.")
    sha = _commit_change(repo, "a.txt", "<aGiT> prompt subject")

    runner._start_commit_summary(sha, _TRACE_TEXT)
    runner.name = "other"
    _finish_summary(runner)

    assert "failed in session 'feature-x'" in (runner.message or "")


def test_integration_waits_for_summary_until_deadline():
    runner = make_runner()
    runner.SUMMARY_WAIT_SECONDS = 45.0
    runner._summary_pending = {"sha": "abc", "since": 100.0}
    runner._summary_thread = threading.Thread(target=lambda: time.sleep(30), daemon=True)
    runner._summary_thread.start()

    assert runner._summary_blocks_integration(110.0) is True  # worker running, within window
    assert runner._summary_blocks_integration(146.0) is False  # deadline passed: never stall


def test_integration_not_blocked_without_pending_summary():
    runner = make_runner()
    assert runner._summary_blocks_integration(100.0) is False


def test_pending_summary_does_not_block_integration_at_new_prompt(tmp_path, monkeypatch):
    # When a new prompt starts a turn while the previous turn's commit summary is
    # still in flight, the previous turn integrates NOW rather than riding along
    # on the same branch and only landing once the whole new turn finishes.
    runner, repo = _summary_runner(tmp_path, monkeypatch)
    FakeSummarizer.gate = threading.Event()  # hold the summary open
    sha = _commit_change(repo, "a.txt", "<aGiT> prompt subject")
    full = repo.rev_parse(sha)
    runner._start_commit_summary(sha, _TRACE_TEXT)

    # The summary is pending, so the normal post-commit path defers integration.
    assert runner._summary_pending is not None
    assert runner._summary_blocks_integration(time.monotonic()) is True
    assert repo.rev_parse(runner._base_branch) != full  # not yet integrated

    # A new prompt arrives: the deferred turn integrates immediately instead of
    # waiting behind the new agent call.
    runner._integrate_committed_turn_before_new_turn()

    assert repo.rev_parse(runner._base_branch) == full  # base advanced to the turn
    assert repo.is_detached()  # worktree re-pointed at base, ready for the next turn

    # The summary, landing afterwards, becomes notes-only (commit already in base).
    FakeSummarizer.gate.set()
    _finish_summary(runner)
    assert repo.commit_message(full).startswith("<aGiT> prompt subject")  # not amended
    assert "widget renderer" in (repo.notes_show(full, namespace="agit/commit-summary") or "")


def test_new_prompt_flush_leaves_conflicting_turn_for_later(tmp_path, monkeypatch):
    # If the deferred turn conflicts with the base, the flush must NOT pop a
    # resolve box mid-prompt: it backs the merge out (tree stays clean) and leaves
    # the work on its branch to be surfaced when the current agent call ends.
    runner, repo = _summary_runner(tmp_path, monkeypatch)
    (repo.repo / "a.txt").write_text("session line\n", encoding="utf-8")
    repo.stage_paths(["a.txt"])
    sha = repo.commit("<aGiT> session edit")
    # The base gains a conflicting change to the same line from "another session".
    base = runner._base_branch
    repo.switch_detach(base)
    (repo.repo / "a.txt").write_text("base line\n", encoding="utf-8")
    repo.stage_paths(["a.txt"])
    repo.commit("base edit")
    repo._run(["git", "branch", "-f", base, "HEAD"])
    repo.switch("agit/test/s1/t1")
    prompts: list = []
    runner._prompt_resolve_conflict = lambda *a, **k: prompts.append(a)

    runner._integrate_committed_turn_before_new_turn()

    assert prompts == []  # no resolve popup mid-prompt
    assert not repo.merge_in_progress() and not repo.has_changes()  # tree clean
    assert repo.current_branch() == "agit/test/s1/t1"  # work still on its branch
    assert repo.rev_parse(base) != repo.rev_parse(sha)  # base not advanced
