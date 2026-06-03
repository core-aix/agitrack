from agit.commit_message import build_agent_commit_message, build_user_commit_message


def test_agent_commit_message_contains_trace_and_metadata():
    message = build_agent_commit_message(
        latest_prompt="fix it",
        trace=[{"role": "user", "content": "fix it"}, {"role": "agent", "content": "fixed"}],
        backend="opencode",
        backend_session_id="ses-1",
        agit_session_id="agit-1",
        model="provider/model",
        created_at="now",
    )

    assert message.startswith("<agent> fix it")
    assert "User:\nfix it" in message
    assert "Agent:\nfixed" in message
    assert "backend: opencode" in message
    assert "backend_session_id: ses-1" in message


def test_user_commit_message_defaults_blank_message():
    message = build_user_commit_message(message="", agit_session_id="agit-1", created_at="now")

    assert message.startswith("<user> No user message provided")
    assert "User Message:\nNo user message provided" in message
