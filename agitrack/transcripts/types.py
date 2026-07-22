from __future__ import annotations

from dataclasses import dataclass, field

from agitrack.backends.base import TokenUsage


@dataclass
class SessionTurn:
    user_message_id: str
    assistant_message_id: str
    user_prompt: str
    final_response: str
    tokens: TokenUsage
    model: str | None
    # Every user-facing text message the agent emitted during this turn, in order
    # — the conversational replies a human would read, NOT tool calls, tool
    # results, reasoning, or file edits. ``final_response`` is the last of these;
    # this full list backs the opt-in "include all agent messages" commit trace
    # (off by default). Empty for backends/turns where only the final reply is
    # recovered, in which case the trace falls back to ``final_response``.
    agent_messages: list[str] = field(default_factory=list)
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
    # Extra user messages the user QUEUED while the agent was already working on this turn (Claude
    # records them as `attachment` rows, not `user` rows). They belong to THIS turn — the agent's
    # response covers them — but each is a distinct message the user sent after the agent had
    # already said something, so the trace renders each as its OWN ``## User`` heading (rather than
    # merged into ``user_prompt``). Empty for a plain single-prompt turn.
    queued_followups: list[str] = field(default_factory=list)
    # The file edits the agent made during this turn (Edit/Write/MultiEdit tool calls),
    # recovered from the raw transcript's tool-call inputs — the one signal the plain parse
    # deliberately drops. Populated ONLY when a session is exported with ``collect_edits=True``
    # (the ``--backtrace`` feature): it reconstructs how a past conversation changed files
    # without any git history. Empty for ordinary exports and for turns that edited nothing.
    edits: list[FileEdit] = field(default_factory=list)


@dataclass
class FileEdit:
    """One file an agent turn changed, reconstructed from a transcript tool call.

    ``patch`` is a git-style unified diff for the single file (``diff --git`` header,
    ``@@`` hunks, ``+``/``-`` lines) so the dashboard's diff view can colour it exactly
    like a real commit's diff. ``insertions``/``deletions`` are the added/removed line
    counts within that patch. A pure whole-file write records the new content as
    insertions (its prior content is not in the transcript, so deletions stay 0)."""

    path: str
    insertions: int
    deletions: int
    patch: str = ""


@dataclass
class ExportedSession:
    session_id: str
    model: str | None
    updated: int | None
    turns: list[SessionTurn]
    # Harness task ids of background tasks that are demonstrably STILL RUNNING: they have
    # streamed an intermediate monitor `<task-notification>` (an <event> payload) with no
    # terminal notification after it. While any are live, ownership of uncommitted tree
    # changes is unknowable (the user and the task can both edit the same files), so the
    # automatic user-commit dialog stands down and shows a warning instead. Judged from
    # the notification stream, not launches: a task finishing while the agent is mid-turn
    # never emits a terminal notification. Claude-only; other backends leave it empty.
    live_background_task_ids: list[str] = field(default_factory=list)


@dataclass
class SessionRef:
    """A lightweight reference to one of a repository's backend sessions, used
    to list, discover, and switch the session aGiTrack tracks."""

    id: str
    updated: float  # epoch seconds; newest wins
    label: str | None = None


def turns_after(session: ExportedSession, last_message_id: str | None) -> list[SessionTurn]:
    """The turns not yet covered by the ``last_message_id`` watermark.

    A watermark matching a turn's ASSISTANT id marks that whole turn committed: return
    what follows. A watermark matching only a turn's USER id is different: it is stored
    when a turn was FORCE-committed before the agent replied (a backend crash, an exit
    finalize, a tracker restart). If that turn NOW carries an assistant response, the
    agent continued the very same turn after the commit — its final message and edits
    exist nowhere in history — so the turn itself is returned too, letting it commit in
    its completed form. (The continuation re-counts the turn's tokens; work never being
    lost outweighs that inflation in this crash-recovery corner.) A user-id watermark on
    a turn still without a reply behaves as before: nothing new to export."""
    if not last_message_id:
        return session.turns
    for index, turn in enumerate(session.turns):
        if turn.assistant_message_id == last_message_id:
            return session.turns[index + 1 :]
        if turn.user_message_id == last_message_id:
            if turn.assistant_message_id and turn.final_response:
                return session.turns[index:]  # the turn continued past its force-commit
            return session.turns[index + 1 :]
    return session.turns
