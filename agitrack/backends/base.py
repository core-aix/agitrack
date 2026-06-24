from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class TokenUsage:
    context: int | None = None
    total: int = 0
    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    cache_write: int = 0
    # Tokens consumed by sub-agents / sidechain turns the backend spawns. Kept
    # separate from the main-line counters so they can be reported as their own
    # category rather than silently dropped or conflated with the main session.
    subagent_input: int = 0
    subagent_output: int = 0
    subagent_reasoning: int = 0
    subagent_cache_read: int = 0
    subagent_cache_write: int = 0

    _SUM_FIELDS = (
        "total",
        "input",
        "output",
        "reasoning",
        "cache_read",
        "cache_write",
        "subagent_input",
        "subagent_output",
        "subagent_reasoning",
        "subagent_cache_read",
        "subagent_cache_write",
    )

    def to_dict(self) -> dict[str, int | None]:
        data: dict[str, int | None] = {"context": self.context}
        for field in self._SUM_FIELDS:
            data[field] = getattr(self, field)
        return data

    def add(self, other: "TokenUsage") -> None:
        if other.context is not None:
            self.context = other.context
        for field in self._SUM_FIELDS:
            setattr(self, field, getattr(self, field) + getattr(other, field))


@dataclass
class AgentResult:
    backend: str
    session_id: str | None
    model: str | None
    final_response: str
    exit_code: int
    tokens: TokenUsage


class AgentBackend(Protocol):
    name: str

    def run(
        self,
        prompt: str,
        *,
        model: str | None,
        session_id: str | None,
        bare: bool = False,
        system_prompt: str | None = None,
        commit_guidance: bool = True,
    ) -> AgentResult: ...

    def update_command(self) -> list[str] | None:
        """The command that updates this backend CLI in place (e.g. ``["opencode", "upgrade"]``),
        or None if it has no self-update. aGiTrack runs this from its UNCONFINED proxy, NOT the
        worktree-sandboxed agent: a package-manager updater (notably Homebrew's own
        ``sandbox-exec``) cannot run nested inside the agent's macOS sandbox — that nesting is
        what breaks the backend's in-app self-update."""
        ...

    # ``commit_guidance``: on a non-bare coding run, append the note telling the agent that
    # aGiTrack auto-commits (so it doesn't self-commit), where the backend supports it.
    # Disabled per-run by --no-commit-guidance. Ignored on a bare (summarizer) run.

    # ``bare``: run as a plain text completion — no tools, no agent system prompt, no
    # project/user memory or MCP servers — so the only input is the caller's prompt. Used
    # by the summarizer, which must read just its instruction plus the interaction trace
    # and nothing else; the default agent context would otherwise add thousands of input
    # tokens of system prompt and tool schemas the summary never needs.
    #
    # ``system_prompt`` (bare only): the system prompt to run under. The summarizer passes
    # its task instruction here — putting the directive in the SYSTEM role (not crammed into
    # the user message) so the model summarizes the user content instead of completing/echoing
    # an instruction-shaped prompt. None falls back to a minimal generic directive.
