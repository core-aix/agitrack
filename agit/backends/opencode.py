from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TextIO

from agit.backends.base import AgentResult


class OpenCodeBackend:
    name = "opencode"

    def __init__(self, repo: Path, *, verbose: bool = False) -> None:
        self.repo = repo
        self.verbose = verbose

    def run(self, prompt: str, *, model: str | None, session_id: str | None) -> AgentResult:
        command = ["opencode", "run", "--format", "json"]
        if model:
            command.extend(["--model", model])
        if session_id:
            command.extend(["--session", session_id])
        if prompt.startswith("/"):
            slash_command, args = self._split_slash_command(prompt)
            if slash_command:
                command.extend(["--command", slash_command])
                command.extend(args)
            else:
                command.append(prompt)
        else:
            command.append(prompt)

        process = subprocess.Popen(
            command,
            cwd=self.repo,
            text=True,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
        )
        final_response, parsed_session_id, parsed_model = self._read_events(process.stdout)
        exit_code = process.wait()

        return AgentResult(
            backend=self.name,
            session_id=parsed_session_id or session_id,
            model=parsed_model or model,
            final_response=final_response.strip(),
            exit_code=exit_code,
        )

    def _read_events(self, output: TextIO | None) -> tuple[str, str | None, str | None]:
        if output is None:
            return "", None, None

        final_parts: list[str] = []
        session_id = None
        model = None
        for line in output:
            parsed = self._parse_event_line(line)
            if parsed is None:
                if self.verbose and line.strip():
                    print(line.rstrip())
                continue

            display_text, final_text, parsed_session_id, parsed_model = parsed
            session_id = session_id or parsed_session_id
            model = model or parsed_model
            if display_text:
                print(display_text, end="" if display_text.endswith("\n") else "\n")
            if final_text:
                final_parts.append(final_text)

        return "".join(final_parts).strip(), session_id, model

    def _split_slash_command(self, prompt: str) -> tuple[str | None, list[str]]:
        parts = prompt[1:].strip().split()
        if not parts:
            return None, []
        return parts[0], parts[1:]

    def _parse_output(self, output: str) -> tuple[str, str | None, str | None]:
        final_response = ""
        session_id = None
        model = None

        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            parsed = self._parse_event_line(line)
            if parsed is None:
                continue

            _display_text, final_text, parsed_session_id, parsed_model = parsed
            session_id = session_id or parsed_session_id
            model = model or parsed_model
            if final_text:
                final_response += final_text

        return final_response.strip(), session_id, model

    def _parse_event_line(self, line: str) -> tuple[str | None, str | None, str | None, str | None] | None:
        line = line.strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None

        session_id = self._find_value(event, {"sessionID", "sessionId", "session_id"})
        model = self._find_value(event, {"model"})
        event_type = str(event.get("type", "")).lower()
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        part_type = str(part.get("type", "")).lower()

        if "thinking" in event_type or "thinking" in part_type:
            return None

        text = self._event_text(event)
        if text:
            is_final = self._is_final_text(event, part)
            return text, text if is_final else None, session_id, model

        status = self._event_status(event, part)
        return status, None, session_id, model

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

    def _event_text(self, event: dict) -> str | None:
        part = event.get("part")
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                return text
            for key in ("content", "message", "response"):
                value = part.get(key)
                if isinstance(value, str) and value.strip():
                    return value

        for key in ("text", "content", "message", "response"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value
        data = event.get("data")
        if isinstance(data, dict):
            for key in ("text", "content", "message", "response"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return None

    def _is_final_text(self, event: dict, part: dict) -> bool:
        metadata = part.get("metadata")
        if isinstance(metadata, dict) and self._find_value(metadata, {"phase"}) == "final_answer":
            return True
        event_type = str(event.get("type", "")).lower()
        part_type = str(part.get("type", "")).lower()
        return event_type in {"final", "complete", "done"} or part_type in {"final", "complete", "done"}

    def _event_status(self, event: dict, part: dict) -> str | None:
        event_type = str(event.get("type", "")).lower()
        part_type = str(part.get("type", "")).lower()
        if event_type == "tool" or "tool" in part_type:
            tool = part.get("tool") or part.get("name") or event.get("tool") or event.get("name")
            if isinstance(tool, str) and tool:
                return f"[{tool}]"
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
