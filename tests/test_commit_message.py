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
    assert "tokens_since_last_commit_output_excluding_reasoning: 10" in message
    assert "tokens_since_last_commit_total" not in message
    assert "tokens_since_last_commit_cache_read" not in message


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


def test_user_commit_message_defaults_blank_message():
    message = build_user_commit_message(message="", agit_session_id="agit-1")

    assert message.startswith("No user message provided")
    assert "User Message:" not in message
    assert "# aGiT Metadata" in message


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
    assert len(subject) <= 72
    assert subject.startswith("<agent> ")
    assert subject.endswith("...")


def test_user_commit_subject_is_capped_for_github():
    message = build_user_commit_message(message="save " * 40, agit_session_id="agit-1")

    subject = message.splitlines()[0]
    assert len(subject) <= 72
    assert subject.endswith("...")
