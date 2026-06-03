from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class AgentResult:
    backend: str
    session_id: str | None
    model: str | None
    final_response: str
    exit_code: int


class AgentBackend(Protocol):
    name: str

    def run(self, prompt: str, *, model: str | None, session_id: str | None) -> AgentResult:
        ...
