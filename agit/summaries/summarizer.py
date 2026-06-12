from __future__ import annotations

from typing import TYPE_CHECKING

from agit.summaries.prompts import (
    COMMIT_SUMMARY_SYSTEM,
    PRE_COMPACTION_SYSTEM,
    SESSION_UPDATE_SYSTEM,
)

if TYPE_CHECKING:
    from agit.backends.base import AgentBackend
    from agit.transcripts.types import ExportedSession, SessionTurn


class Summarizer:
    def __init__(self, backend: AgentBackend, *, model: str | None = None) -> None:
        self.backend = backend
        self.model = model
        # Tokens this summarizer instance consumed across its LLM calls, so the
        # cost of summarization can be tracked next to the coding session's own
        # usage (issue #8).
        self.tokens_input = 0
        self.tokens_output = 0

    def summarize_commit(
        self,
        *,
        turns: list[SessionTurn],
        diff: str,
        session_summary: str | None = None,
    ) -> str:
        return self._run(self._build_commit_prompt(turns, diff, session_summary))

    def update_session_summary(
        self,
        *,
        current_summary: str | None,
        turns: list[SessionTurn],
        diff: str,
        commit_summary: str,
    ) -> str:
        return self._run(self._build_session_update_prompt(current_summary, turns, diff, commit_summary))

    def summarize_pre_compaction(
        self,
        *,
        exported_session: ExportedSession,
        current_summary: str | None = None,
    ) -> str:
        return self._run(self._build_pre_compaction_prompt(exported_session, current_summary))

    def _run(self, prompt: str) -> str:
        result = self.backend.run(prompt, model=self.model, session_id=None)
        tokens = getattr(result, "tokens", None)
        if tokens is not None:
            self.tokens_input += int(getattr(tokens, "input", 0) or 0)
            self.tokens_output += int(getattr(tokens, "output", 0) or 0)
        return result.final_response.strip()

    def _build_commit_prompt(
        self,
        turns: list[SessionTurn],
        diff: str,
        session_summary: str | None,
    ) -> str:
        parts = [COMMIT_SUMMARY_SYSTEM, "\n\n"]
        if session_summary:
            parts.extend(["Current session context:\n", session_summary, "\n\n"])
        parts.append("Recent conversation turns:\n")
        for turn in turns:
            if turn.user_prompt:
                parts.extend(["\nUser: ", turn.user_prompt])
            if turn.final_response:
                parts.extend(["\nAgent: ", turn.final_response])
        parts.extend(["\n\nCode changes (diff):\n```\n", diff, "\n```\n\nSummary:"])
        return "".join(parts)

    def _build_session_update_prompt(
        self,
        current_summary: str | None,
        turns: list[SessionTurn],
        diff: str,
        commit_summary: str,
    ) -> str:
        parts = [SESSION_UPDATE_SYSTEM, "\n\n"]
        if current_summary:
            parts.extend(["Current session summary:\n", current_summary, "\n\n"])
        else:
            parts.append("No previous session summary exists. Create an initial summary.\n\n")
        parts.extend(["New commit summary:\n", commit_summary, "\n\n"])
        parts.append("Recent conversation turns:\n")
        for turn in turns:
            if turn.user_prompt:
                parts.extend(["\nUser: ", turn.user_prompt])
            if turn.final_response:
                parts.extend(["\nAgent: ", turn.final_response])
        parts.extend(["\n\nCode changes (diff):\n```\n", diff, "\n```\n\nUpdated session summary:"])
        return "".join(parts)

    def _build_pre_compaction_prompt(
        self,
        exported_session: ExportedSession,
        current_summary: str | None,
    ) -> str:
        parts = [PRE_COMPACTION_SYSTEM, "\n\n"]
        if current_summary:
            parts.extend(["Current session summary:\n", current_summary, "\n\n"])
        parts.append("Full session transcript:\n")
        for turn in exported_session.turns:
            if turn.user_prompt:
                parts.extend(["\nUser: ", turn.user_prompt])
            if turn.final_response:
                parts.extend(["\nAgent: ", turn.final_response])
        parts.append("\n\nComprehensive session summary:")
        return "".join(parts)
