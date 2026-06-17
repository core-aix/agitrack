from __future__ import annotations

from dataclasses import dataclass

from agitrack.backends.base import TokenUsage


@dataclass
class SessionTurn:
    user_message_id: str
    assistant_message_id: str
    user_prompt: str
    final_response: str
    tokens: TokenUsage
    model: str | None
    # Whether the agent's response to this prompt has finished. False while the
    # turn is still mid-flight (the backend's last message was a tool call, not a
    # final answer), so aGiTrack can defer committing until the prompt is fully
    # answered and never split one prompt across several commits. Defaults True so
    # backends that don't report a finish reason keep committing on idle as before.
    complete: bool = True
    # True when the user interrupted this turn (Esc): it will never receive more
    # messages, and any prompts the user had queued behind it were discarded by
    # the backend — so nothing should keep waiting for them.
    interrupted: bool = False
    # Epoch seconds for when the user's prompt began this turn and when the
    # agent's last message arrived — the AI-driven conversation's span. None when
    # the backend transcript carries no timestamps.
    started_at: int | None = None
    ended_at: int | None = None
    # The reasoning effort / thinking level the model used for this turn, when the
    # backend transcript reveals it. Neither backend records a numeric budget, so
    # this is a coarse, best-effort signal: a named effort/variant when the export
    # carries one (OpenCode), otherwise ``"on"`` when the turn shows extended
    # thinking / reasoning was active (Claude thinking blocks, OpenCode reasoning
    # tokens). None when nothing about reasoning is recorded — never asserts "off".
    reasoning_effort: str | None = None
    # How many context compactions the backend performed at the start of this turn
    # (the conversation history was summarized to fit the window). A compaction
    # resets what "context" means and shrinks the tokens the turn runs against, so
    # it is recorded to make the turn's token counts interpretable. Almost always 0
    # or 1; only the boundary before a turn is attributed to it.
    compaction_count: int = 0


@dataclass
class ExportedSession:
    session_id: str
    model: str | None
    updated: int | None
    turns: list[SessionTurn]


@dataclass
class SessionRef:
    """A lightweight reference to one of a repository's backend sessions, used
    to list, discover, and switch the session aGiTrack tracks."""

    id: str
    updated: float  # epoch seconds; newest wins
    label: str | None = None


def turns_after(session: ExportedSession, last_message_id: str | None) -> list[SessionTurn]:
    if not last_message_id:
        return session.turns
    for index, turn in enumerate(session.turns):
        if turn.assistant_message_id == last_message_id or turn.user_message_id == last_message_id:
            return session.turns[index + 1 :]
    return session.turns
