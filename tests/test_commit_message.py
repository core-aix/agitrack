import pytest

from agit.commit_message import build_agent_commit_message, build_user_commit_message


def test_agent_commit_message_contains_trace_and_metadata():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[{"role": "user", "content": "fix it"}, {"role": "agent", "content": "fixed"}],
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
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

    assert message.startswith("<agent> fix it")
    assert "User:\nfix it" in message
    assert "Agent:\nfixed" in message
    assert "# aGiT Metadata" in message
    assert "backend: opencode" in message
    assert "backend_session_id: ses-1" in message
    assert "context_tokens: 100" in message
    assert "tokens_since_last_commit_input: 130" in message
    assert "tokens_since_last_commit_output_no_reasoning: 10" in message
    assert "tokens_since_last_commit_total" not in message
    assert "tokens_since_last_commit_cache_read" not in message


def test_commit_message_masks_secrets_in_subject_and_trace():
    message = build_agent_commit_message(
        latest_prompt="use api_key=sk-abc12345678901234567890",
        trace=[{"role": "user", "content": "password=hunter2"}, {"role": "agent", "content": "token: ghp_abcdefghijklmnopqrstuvwxyz"}],
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
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
        agit_session_id="agit-1",
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
        agit_session_id="agit-1",
        model="provider/model",
    )

    assert message.index("User:\nfix it") < message.index("User:\nalso handle errors")


def test_agent_commit_message_omits_zero_reasoning():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[],
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="provider/model",
        token_usage={"context": 100, "total": 5, "input": 100, "output": 5, "reasoning": 0, "cache_read": 0, "cache_write": 0},
    )

    assert "tokens_since_last_commit_input: 100" in message
    assert "tokens_since_last_commit_reasoning" not in message


def test_agent_commit_message_includes_nonzero_reasoning():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[],
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="provider/model",
        token_usage={"context": 100, "total": 11, "input": 100, "output": 5, "reasoning": 6, "cache_read": 0, "cache_write": 0},
    )

    assert "tokens_since_last_commit_reasoning: 6" in message


def test_user_commit_message_rejects_blank_message():
    with pytest.raises(ValueError, match="required"):
        build_user_commit_message(message="", agit_session_id="agit-1")


def test_user_commit_message_uses_required_message_subject():
    message = build_user_commit_message(message="save work", agit_session_id="agit-1")

    assert message.startswith("save work")
    assert "User Message:" not in message
    assert "# aGiT Metadata" in message


def test_user_commit_message_masks_secret_subject():
    message = build_user_commit_message(message="save password=secret123", agit_session_id="agit-1")

    assert message.startswith("save password=[REDACTED]")
    assert "secret123" not in message


def test_commit_messages_include_current_agit_version_without_created_at():
    message = build_user_commit_message(message="save work", agit_session_id="agit-1")

    assert "agit_version: 0.0.1" in message
    assert "created_at" not in message


def test_agent_commit_subject_is_capped_for_github():
    message = build_agent_commit_message(
        latest_prompt="please " * 40,
        trace=[],
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="provider/model",
    )

    subject = message.splitlines()[0]
    assert len(subject) <= 50
    assert subject.startswith("<agent> ")
    assert subject.endswith("...")
    assert "Full subject:\n" in message
    assert "please please please" in message


def test_user_commit_subject_is_capped_for_github():
    message = build_user_commit_message(message="save " * 40, agit_session_id="agit-1")

    subject = message.splitlines()[0]
    assert len(subject) <= 50
    assert subject.endswith("...")
    assert "Full subject:\n" in message


def test_commit_message_body_lines_are_wrapped_to_72():
    message = build_agent_commit_message(
        latest_prompt="change it",
        trace=[{"role": "agent", "content": "a " * 80}],
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="provider/model",
    )

    body_lines = message.splitlines()[1:]
    assert all(len(line) <= 72 for line in body_lines)


def test_agent_commit_trace_is_limited_by_user_turns():
    trace = []
    for index in range(7):
        trace.extend([
            {"role": "user", "content": f"user {index}"},
            {"role": "agent", "content": f"agent {index}"},
        ])

    message = build_agent_commit_message(
        latest_prompt="change it",
        trace=trace,
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="provider/model",
        trace_turn_limit=5,
    )

    assert "User:\nuser 0" not in message
    assert "User:\nuser 1" not in message
    assert "User:\nuser 2" in message
    assert "Agent:\nagent 6" in message
