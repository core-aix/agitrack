from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agit.backends.base import AgentResult


class OpenCodeBackend:
    name = "opencode"

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def run(self, prompt: str, *, model: str | None, session_id: str | None) -> AgentResult:
        command = ["opencode", "run", "--format", "json"]
        if model:
            command.extend(["--model", model])
        if session_id:
            command.extend(["--session", session_id])
        command.append(prompt)

        process = subprocess.run(
            command,
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        final_response, parsed_session_id, parsed_model = self._parse_output(process.stdout)
        if not final_response.strip():
            final_response = process.stdout.strip()

        return AgentResult(
            backend=self.name,
            session_id=parsed_session_id or session_id,
            model=parsed_model or model,
            final_response=final_response.strip(),
            exit_code=process.returncode,
        )

    def _parse_output(self, output: str) -> tuple[str, str | None, str | None]:
        final_response = ""
        session_id = None
        model = None

        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            session_id = session_id or self._find_value(event, {"sessionID", "sessionId", "session_id"})
            model = model or self._find_value(event, {"model"})

            event_type = str(event.get("type", "")).lower()
            if "thinking" in event_type:
                continue
            candidate = self._extract_final_text(event)
            if candidate:
                final_response = candidate

        return final_response, session_id, model

    def _extract_final_text(self, event: dict) -> str | None:
        event_type = str(event.get("type", "")).lower()
        if any(marker in event_type for marker in ("final", "complete", "done", "message")):
            for key in ("text", "content", "message", "response"):
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            data = event.get("data")
            if isinstance(data, dict):
                for key in ("text", "content", "message", "response"):
                    value = data.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        return None

    def _find_value(self, value: object, keys: set[str]) -> str | None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys and isinstance(item, str) and item.strip():
                    return item.strip()
                found = self._find_value(item, keys)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = self._find_value(item, keys)
                if found:
                    return found
        return None
