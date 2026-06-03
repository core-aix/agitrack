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

    def to_dict(self) -> dict[str, int | None]:
        return {
            "context": self.context,
            "total": self.total,
            "input": self.input,
            "output": self.output,
            "reasoning": self.reasoning,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }

    def add(self, other: "TokenUsage") -> None:
        if other.context is not None:
            self.context = other.context
        self.input += other.input
        self.total += other.total
        self.output += other.output
        self.reasoning += other.reasoning
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write


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

    def run(self, prompt: str, *, model: str | None, session_id: str | None) -> AgentResult:
        ...
