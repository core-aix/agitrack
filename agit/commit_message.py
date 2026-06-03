from __future__ import annotations

from datetime import datetime, timezone
from textwrap import shorten

from agit import __version__

DEFAULT_USER_MESSAGE = "No user message provided"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_agent_commit_message(
    *,
    latest_prompt: str,
    trace: list[dict],
    backend: str,
    backend_session_id: str | None,
    agit_session_id: str,
    model: str | None,
    token_usage: dict[str, int | None] | None = None,
    created_at: str | None = None,
) -> str:
    subject_prompt = _subject_text(latest_prompt)
    lines = [f"<agent> {subject_prompt}", "", "Interaction Trace:"]
    for item in trace:
        role = item.get("role", "").strip().lower()
        content = item.get("content", "")
        label = "User" if role == "user" else "Agent"
        lines.extend([f"{label}:", content, ""])

    lines.extend(
        [
            "aGiT Metadata:",
            "commit_type: agent",
            f"backend: {backend}",
            f"model: {model or 'unknown'}",
            f"agit_session_id: {agit_session_id}",
            f"backend_session_id: {backend_session_id or 'unknown'}",
        ]
    )
    lines.extend(_token_metadata_lines(token_usage))
    lines.extend([f"agit_version: {__version__}", f"created_at: {created_at or utc_now()}"])
    return "\n".join(lines).rstrip() + "\n"


def build_user_commit_message(
    *,
    message: str | None,
    agit_session_id: str,
    created_at: str | None = None,
) -> str:
    user_message = message.strip() if message and message.strip() else DEFAULT_USER_MESSAGE
    lines = [
        f"<user> {_subject_text(user_message)}",
        "",
        "User Message:",
        user_message,
        "",
        "aGiT Metadata:",
        "commit_type: user",
        "backend: agit",
        f"agit_session_id: {agit_session_id}",
        f"agit_version: {__version__}",
        f"created_at: {created_at or utc_now()}",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _subject_text(text: str) -> str:
    one_line = " ".join(text.strip().split()) or DEFAULT_USER_MESSAGE
    return shorten(one_line, width=120, placeholder="...")


def _token_value(token_usage: dict[str, int | None] | None, key: str) -> int | str:
    if not token_usage:
        return "unknown"
    value = token_usage.get(key)
    return value if value is not None else "unknown"


def _token_metadata_lines(token_usage: dict[str, int | None] | None) -> list[str]:
    lines = [f"context_tokens: {_token_value(token_usage, 'context')}"]
    if token_usage:
        input_tokens = token_usage.get("input")
        reasoning_tokens = token_usage.get("reasoning") or 0
        if input_tokens:
            lines.append(f"tokens_since_last_commit_input: {input_tokens}")
        if reasoning_tokens:
            lines.append(f"tokens_since_last_commit_reasoning: {reasoning_tokens}")
    else:
        lines.append("tokens_since_last_commit_input: unknown")
    lines.extend(
        [
            f"tokens_since_last_commit_output_excluding_reasoning: {_token_value(token_usage, 'output')}",
            "token_note: output excludes reasoning/thinking tokens when the backend reports them separately; reasoning may be unavailable from OpenCode export",
        ]
    )
    return lines
