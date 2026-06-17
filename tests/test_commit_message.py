import pytest

from agitrack.commits import build_agent_commit_message, build_user_commit_message, render_interaction_trace


def test_render_interaction_trace_matches_committed_trace_and_masks_secrets():
    # The summarizer's sole input is this rendered trace; it must be the same
    # "## User"/"## Agent" body that lands in the commit, with secrets masked.
    trace = [
        {"role": "user", "content": "ship it, key is sk-ant-api03-SECRETSECRETSECRETSECRET"},
        {"role": "agent", "content": "Shipped it."},
    ]
    rendered = render_interaction_trace(trace, trace_turn_limit=10)
    assert rendered.startswith("## User")
    assert "## Agent\n\nShipped it." in rendered
    assert "SECRETSECRETSECRETSECRET" not in rendered  # masked

    # It is exactly the body the commit carries under "# Interaction Trace".
    message = build_agent_commit_message(
        latest_prompt="ship it",
        trace=trace,
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="m",
    )
    committed = message.split("# Interaction Trace\n\n", 1)[1].split("\n# aGiTrack Metadata", 1)[0].strip()
    assert committed == rendered


def test_render_interaction_trace_respects_turn_limit():
    trace = [{"role": "user", "content": f"turn {i}"} for i in range(5)]
    rendered = render_interaction_trace(trace, trace_turn_limit=2)
    # Only the most recent 2 turns are kept (same limiting the commit applies).
    assert "turn 4" in rendered and "turn 3" in rendered
    assert "turn 0" not in rendered


def test_agent_commit_message_records_reasoning_effort_when_given():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[{"role": "user", "content": "fix it"}],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
        reasoning_effort="high",
    )

    assert "model: provider/model\nreasoning_effort: high\n" in message


def test_agent_commit_message_omits_reasoning_effort_when_absent():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[{"role": "user", "content": "fix it"}],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )

    assert "reasoning_effort:" not in message


def test_agent_commit_message_records_conversation_anchor_when_given():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[{"role": "user", "content": "fix it"}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
        conversation_anchor="msg-abc",
    )

    # The anchor sits with the session identity so a reader can locate the
    # exact place in the transcript referenced by this commit.
    assert "backend_session_id: ses-1\nconversation_anchor: msg-abc\n" in message


def test_agent_commit_message_omits_conversation_anchor_when_absent():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[{"role": "user", "content": "fix it"}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )

    assert "conversation_anchor:" not in message


def test_agent_commit_message_contains_trace_and_metadata():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[{"role": "user", "content": "fix it"}, {"role": "agent", "content": "fixed"}],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
        token_usage={
            "context": 100,
            "total": 40,
            "input": 130,
            "output": 10,
            "reasoning": 0,
            "cache_read": 20,
            "cache_write": 5,
        },
    )

    assert message.startswith("<aGiTrack> fix it")
    assert "# Interaction Trace" in message
    assert "## User\n\nfix it" in message
    assert "## Agent\n\nfixed" in message
    assert "# aGiTrack Metadata" in message
    assert "backend: opencode" in message
    assert "backend_session_id: ses-1" in message
    assert "context_tokens: 100" in message
    # Input counts uncached input PLUS cache-creation tokens (130 + 5): cache
    # writes are fresh input processed once (#14). Cache reads stay separate.
    assert "tokens_since_last_commit_input: 135" in message
    assert "tokens_since_last_commit_output: 10" in message
    assert "tokens_since_last_commit_cache_read: 20" in message
    assert "tokens_since_last_commit_cache_write: 5" in message
    assert "tokens_since_last_commit_total" not in message
    assert "tokens_since_last_commit_subagent_input" not in message
    assert "token_note" not in message


def test_trace_message_headings_are_nested_under_role():
    # A message's own Markdown headings must be pushed one level below its
    # "## User"/"## Agent" role heading so they nest (and render) correctly,
    # instead of a message "# Title" outranking the role it belongs to.
    message = build_agent_commit_message(
        latest_prompt="do it",
        trace=[
            {"role": "user", "content": "# Big ask\nplease\n## Detail\nmore"},
            {"role": "agent", "content": "### Already deep\nkept as-is"},
        ],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="m",
    )
    # User content's shallowest heading was level 1, so everything shifts +2: the
    # role stays "## User" and the message's headings start one level below it.
    assert "## User\n\n### Big ask" in message
    assert "#### Detail" in message
    # The original level-1/level-2 headings must no longer appear as such.
    assert "\n# Big ask" not in message
    assert "\n## Detail" not in message
    # Agent content already started at level 3 (one below the role) — left intact.
    assert "## Agent\n\n### Already deep" in message


def test_trace_heading_nesting_skips_fenced_code_comments():
    # A leading '#' inside a fenced code block is a comment, not a heading, and
    # must be left untouched even while real headings around it are shifted.
    message = build_agent_commit_message(
        latest_prompt="x",
        trace=[{"role": "agent", "content": "# Heading\n```sh\n# just a comment\n```"}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="m",
    )
    assert "### Heading" in message
    assert "# just a comment" in message  # the comment kept its single '#'


def test_commit_message_masks_secrets_in_subject_and_trace():
    message = build_agent_commit_message(
        latest_prompt="use api_key=sk-abc12345678901234567890",
        trace=[
            {"role": "user", "content": "password=hunter2"},
            {"role": "agent", "content": "token: ghp_abcdefghijklmnopqrstuvwxyz"},
        ],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )

    assert "hunter2" not in message
    assert "sk-abc" not in message
    assert "ghp_" not in message
    assert "[REDACTED]" in message


def test_commit_message_removes_mouse_coordinate_reports():
    message = build_agent_commit_message(
        latest_prompt="fix [<35;59;45M[<35;59;44M bug",
        trace=[{"role": "user", "content": "[<35;60;43M[<0;42;39m keep text"}],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )

    assert "[<35;" not in message
    assert "[<0;" not in message
    assert "keep text" in message


def test_agent_commit_message_preserves_user_trace_order():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[
            {"role": "user", "content": "fix it"},
            {"role": "agent", "content": "working"},
            {"role": "user", "content": "also handle errors"},
            {"role": "agent", "content": "done"},
        ],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )

    assert message.index("## User\n\nfix it") < message.index("## User\n\nalso handle errors")


def test_agent_commit_message_omits_zero_reasoning():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
        token_usage={
            "context": 100,
            "total": 5,
            "input": 100,
            "output": 5,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
        },
    )

    assert "tokens_since_last_commit_input: 100" in message
    assert "tokens_since_last_commit_reasoning" not in message


def test_agent_commit_message_includes_nonzero_reasoning():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
        token_usage={
            "context": 100,
            "total": 11,
            "input": 100,
            "output": 5,
            "reasoning": 6,
            "cache_read": 0,
            "cache_write": 0,
        },
    )

    assert "tokens_since_last_commit_reasoning: 6" in message


def test_first_run_input_counts_cache_creation_tokens():
    # First commit in a fresh repo (#14): the backend reports almost the whole
    # context as cache_creation_input_tokens and only a sliver as input_tokens.
    # All of it was processed as input exactly once, so the input line must
    # reflect input + cache_write — not look near zero next to the cache.
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="m",
        token_usage={
            "context": 18250,
            "total": 200,
            "input": 250,
            "output": 200,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 18000,
        },
    )

    assert "tokens_since_last_commit_input: 18250" in message
    assert "tokens_since_last_commit_cache_write: 18000" in message
    assert "tokens_since_last_commit_cache_read" not in message


def test_subagent_input_counts_subagent_cache_creation_tokens():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="m",
        token_usage={
            "context": 100,
            "total": 5,
            "input": 100,
            "output": 5,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
            "subagent_input": 40,
            "subagent_output": 60,
            "subagent_reasoning": 0,
            "subagent_cache_read": 0,
            "subagent_cache_write": 500,
        },
    )

    assert "tokens_since_last_commit_subagent_input: 540" in message
    assert "tokens_since_last_commit_subagent_cache_write: 500" in message


def test_agent_commit_message_records_subagent_token_categories():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="claude-opus-4-8",
        token_usage={
            "context": 100,
            "total": 5,
            "input": 100,
            "output": 5,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
            "subagent_input": 40,
            "subagent_output": 60,
            "subagent_reasoning": 0,
            "subagent_cache_read": 700,
            "subagent_cache_write": 0,
        },
    )

    assert "tokens_since_last_commit_subagent_input: 40" in message
    assert "tokens_since_last_commit_subagent_output: 60" in message
    assert "tokens_since_last_commit_subagent_cache_read: 700" in message
    # Zero-valued sub-agent categories stay out of the metadata.
    assert "tokens_since_last_commit_subagent_reasoning" not in message
    assert "tokens_since_last_commit_subagent_cache_write" not in message


def test_user_commit_message_rejects_blank_message():
    with pytest.raises(ValueError, match="required"):
        build_user_commit_message(message="", agitrack_session_id="agit-1")


def test_user_commit_message_uses_required_message_subject():
    message = build_user_commit_message(message="save work", agitrack_session_id="agit-1")

    assert message.startswith("save work")
    assert "User Message:" not in message
    assert "# aGiTrack Metadata" in message


def test_user_commit_message_masks_secret_subject():
    message = build_user_commit_message(message="save password=secret123", agitrack_session_id="agit-1")

    assert message.startswith("save password=[REDACTED]")
    assert "secret123" not in message


def test_commit_messages_include_current_agitrack_version_without_created_at():
    from agitrack import __version__

    message = build_user_commit_message(message="save work", agitrack_session_id="agit-1")

    assert f"agitrack_version: {__version__}" in message
    assert "created_at" not in message


def test_commit_version_matches_installed_distribution():
    # The version stamped into commit metadata must equal the aGiTrack the user has
    # installed — agitrack.__version__ derives from the distribution metadata so the
    # two cannot drift (pyproject.toml is the single version source).
    from importlib.metadata import version

    from agitrack import __version__

    assert __version__ == version("agitrack")
    message = build_user_commit_message(message="save work", agitrack_session_id="agit-1")
    assert f"agitrack_version: {version('agitrack')}" in message


def test_source_version_falls_back_to_pyproject_when_metadata_missing():
    # When distribution metadata is unavailable (e.g. an editable install whose
    # .dist-info went missing after a failed reinstall), the version must fall back
    # to the source checkout's pyproject.toml — the real release version — rather
    # than the 0.0.0 placeholder, so commit metadata stays accurate.
    import re
    from pathlib import Path

    from agitrack import _source_version

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    expected = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.M).group(1)
    assert _source_version() == expected
    assert expected != "0.0.0"  # the checkout declares a real release version


def test_agent_commit_subject_splits_at_first_sentence():
    # The subject is the first sentence; the rest flows onto the next line. aGiTrack
    # never adds "…" — Git shortens the displayed subject if it's long.
    message = build_agent_commit_message(
        latest_prompt="Fix the failing parser test. Then add coverage and update the docs.",
        trace=[],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )

    lines = message.splitlines()
    assert lines[0] == "<aGiTrack> Fix the failing parser test."  # first sentence only
    assert "..." not in lines[0]
    # The remainder follows directly on the next line (no blank between).
    assert lines[1] == "Then add coverage and update the docs."


def test_agent_commit_subject_without_period_is_kept_whole():
    # No sentence-ending period ⇒ the whole prompt is the subject, untruncated, with
    # no "…" and no continuation line.
    message = build_agent_commit_message(
        latest_prompt="please " * 40,
        trace=[],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )

    lines = message.splitlines()
    assert lines[0].startswith("<aGiTrack> please please")
    assert not lines[0].endswith("...")
    assert lines[1] == ""  # straight to the (blank then) trace — no subject continuation


def test_user_commit_subject_splits_at_first_sentence():
    message = build_user_commit_message(
        message="Save my parser edits. They also touch the lexer.", agitrack_session_id="agit-1"
    )

    lines = message.splitlines()
    assert lines[0] == "Save my parser edits."  # first sentence
    assert "..." not in lines[0]
    assert lines[1] == "They also touch the lexer."


def test_commit_message_body_lines_are_wrapped_to_72():
    message = build_agent_commit_message(
        latest_prompt="change it",
        trace=[{"role": "agent", "content": "a " * 80}],
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
    )

    body_lines = message.splitlines()[1:]
    assert all(len(line) <= 72 for line in body_lines)


def test_agent_commit_trace_is_limited_by_user_turns():
    trace = []
    for index in range(7):
        trace.extend(
            [
                {"role": "user", "content": f"user {index}"},
                {"role": "agent", "content": f"agent {index}"},
            ]
        )

    message = build_agent_commit_message(
        latest_prompt="change it",
        trace=trace,
        backend="opencode",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="provider/model",
        trace_turn_limit=5,
    )

    assert "## User\n\nuser 0" not in message
    assert "## User\n\nuser 1" not in message
    assert "## User\n\nuser 2" in message
    assert "## Agent\n\nagent 6" in message


def test_agent_commit_records_conversation_span_as_utc_iso():
    message = build_agent_commit_message(
        latest_prompt="build it",
        trace=[{"role": "user", "content": "build it"}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="claude-opus-4-8",
        started_at=1_718_200_000,
        ended_at=1_718_200_123,
    )
    assert "agent_started_at: 2024-06-12T13:46:40Z" in message
    assert "agent_ended_at: 2024-06-12T13:48:43Z" in message


def test_agent_commit_omits_span_when_timestamps_absent():
    message = build_agent_commit_message(
        latest_prompt="build it",
        trace=[{"role": "user", "content": "build it"}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="claude-opus-4-8",
    )
    assert "agent_started_at" not in message
    assert "agent_ended_at" not in message


def test_subject_strips_terminal_escape_sequences():
    # Arrow-key residue and SGR colour codes must never reach the subject.
    message = build_user_commit_message(message="fix \x1b[Bthe \x1b[31mparser\x1b[0m bug", agitrack_session_id="agit-1")
    subject = message.splitlines()[0]
    assert subject == "fix the parser bug"
    assert "\x1b" not in message


def test_agent_subject_strips_orphan_mouse_and_control_chars():
    message = build_agent_commit_message(
        latest_prompt="run \x07tests\x1b]0;title\x07 now",
        trace=[{"role": "user", "content": "run \x1b[Atests now"}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="claude-opus-4-8",
    )
    subject = message.splitlines()[0]
    assert subject == "<aGiTrack> run tests now"
    assert "\x1b" not in message
    assert "\x07" not in message


def test_legitimate_bracketed_text_is_preserved():
    # Defensive escape stripping must not damage normal bracketed prose.
    message = build_user_commit_message(message="handle [Beta] flag and list[B]", agitrack_session_id="agit-1")
    assert message.splitlines()[0] == "handle [Beta] flag and list[B]"


def test_agent_merge_message_format():
    from agitrack.commits import build_agent_merge_message

    message = build_agent_merge_message(
        session_name="feature-x",
        base_branch="main",
        source_branch="agit/feature-x/t2",
        agitrack_session_id="agit-1",
        backend="claude",
        backend_session_id="ses-9",
        conflicting_commits="abc123 base edit",
    )
    assert message.splitlines()[0].startswith("<aGiTrack-merge> ")
    assert "commit_type: agent-merge" in message
    assert "session_name: feature-x" in message
    assert "source_branch: agit/feature-x/t2" in message
    assert "base_branch: main" in message
    assert "backend_session_id: ses-9" in message
    assert "abc123 base edit" in message


def _trace():
    return [{"role": "user", "content": "do it"}, {"role": "agent", "content": "done"}]


def _base_message(**kw):
    return build_agent_commit_message(
        latest_prompt="do it",
        trace=_trace(),
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agitrack-1",
        model="claude-opus-4-8",
        token_usage={"context": 100, "input": 10, "output": 5},
        **kw,
    )


def test_compaction_recorded_in_trace_note_and_metadata():
    message = _base_message(compactions=2)
    assert "context_compactions: 2" in message
    # A human-readable note leads the interaction trace, before the turns.
    trace_idx = message.index("# Interaction Trace")
    note_idx = message.index("compacted 2 times")
    user_idx = message.index("## User")
    assert trace_idx < note_idx < user_idx
    assert "> " in message  # rendered as a blockquote


def test_no_compaction_emits_no_note_or_metadata():
    message = _base_message(compactions=0)
    assert "context_compactions" not in message
    assert "compacted" not in message


def test_fork_origin_recorded_in_trace_and_metadata():
    message = _base_message(
        origin_event={"kind": "fork", "source": "ses_parent", "source_name": "main", "collaborator": "", "at": 1}
    )
    assert "forked_from: ses_parent (main)" in message
    assert "copied_from" not in message
    assert "forked from 'main'" in message


def test_copy_origin_records_contributor_in_trace_and_metadata():
    message = _base_message(
        origin_event={
            "kind": "copy",
            "source": "ses_shared",
            "source_name": "feature-x",
            "collaborator": "alice+bob",
            "at": 1,
        }
    )
    assert "copied_from: ses_shared (feature-x)" in message
    assert "copied_from_contributor: alice+bob" in message
    assert "copied from alice+bob's shared session 'feature-x'" in message
