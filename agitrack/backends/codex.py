from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import IO

from agitrack.backends.base import AgentResult, TokenUsage
from agitrack.proc import console_isolation_kwargs, resolve_subprocess_command

# Cap a ``bare`` (summarizer) call. A hung call would never finish, leaving the commit
# unsummarized and — since one summary runs per session at a time — blocking every later
# commit's summary too. Like OpenCode (and unlike Claude's blocking ``subprocess.run``) the
# events are streamed here, so a plain ``timeout=`` wouldn't interrupt a stalled read; a
# watchdog kills the process instead, which ends the stream and yields a non-zero exit the
# summarizer treats as unusable. Only bare runs are capped — agent turns can legitimately be long.
_SUMMARIZER_TIMEOUT_SECONDS = 90

# Codex feature flags turned OFF for a bare (summarizer) run. Summarizing is pure text
# reduction: every one of these only gives the model a way to go do agentic work instead of
# answering. Measured on a live run, leaving them on made the summarizer shell out ~20 times —
# reading unrelated repositories on the machine — before replying, at several times the tokens
# and latency of the summary itself. ``--disable`` is Codex's own flag (``-c features.<n>=false``).
_BARE_DISABLED_FEATURES = (
    "shell_tool",
    "multi_agent",
    "plugins",
    "apps",
    "view_image",
    "image_generation",
    "computer_use",
    "browser_use",
    "goals",
    "memories",
)

# The summarizer wants the cheapest possible thinking. ``minimal`` is Codex's floor but the API
# REJECTS it whenever the web_search tool is attached ("The following tools cannot be used with
# reasoning.effort 'minimal': web_search", HTTP 400) — and web_search is not one of the features
# ``--disable`` can remove. ``low`` is the cheapest effort that is always accepted, so it is used
# rather than a setting that makes every summary fail on some configurations.
_SUMMARIZER_REASONING_EFFORT = "low"


class CodexBackend:
    name = "codex"

    def __init__(
        self,
        repo: Path,
        *,
        verbose: bool = False,
        backend_args: list[str] | None = None,
        launch_command: list[str] | None = None,
    ) -> None:
        self.repo = repo
        self.verbose = verbose
        self.backend_args = list(backend_args or [])  # forwarded verbatim to the backend CLI (#32)
        # Command that launches the backend, replacing the "codex" executable with a user
        # wrapper (e.g. ["somewrapper", "codex"]); empty ⇒ run "codex" directly.
        self.launch_command = list(launch_command or [])

    def update_command(self) -> list[str] | None:
        return [*(self.launch_command or ["codex"]), "update"]

    def run(
        self,
        prompt: str,
        *,
        model: str | None,
        session_id: str | None,
        bare: bool = False,
        system_prompt: str | None = None,
        commit_guidance: bool = True,
        timeout_seconds: int | None = None,
    ) -> AgentResult:
        head = list(self.launch_command or ["codex"])
        command = [*head, "exec", "--json", "--skip-git-repo-check", "-C", str(self.repo)]
        if model:
            command.extend(["-m", model])
        if bare:
            # ``--ephemeral`` writes NO session file at all. The other two backends can only
            # push their summarizer conversations into a scratch directory and hope nothing
            # adopts them (issues #8/#56 — a summary session became a repo's newest session and
            # got resumed instead of the user's); Codex can simply not create one, so a
            # summarizer run is structurally incapable of being listed, adopted or resumed.
            command.append("--ephemeral")
            # Read-only: the summarizer has nothing to write, so denying it removes a whole
            # class of accident rather than trusting the prompt to keep it read-only.
            command.extend(["-s", "read-only"])
            command.extend(["-c", f"model_reasoning_effort={json.dumps(_SUMMARIZER_REASONING_EFFORT)}"])
            for feature in _BARE_DISABLED_FEATURES:
                command.extend(["--disable", feature])
            if system_prompt:
                # ``experimental_instructions_file`` REPLACES Codex's agent system prompt with
                # the file's contents, which is exactly what a bare run wants: the summarizer's
                # TASK INSTRUCTION lands in the system role (not crammed into the user message,
                # where an instruction-shaped prompt gets completed or echoed instead of acted
                # on) and the coding-agent persona goes away. Measured on a live summary: the
                # prompt fell from 40,837 input tokens to 8,284.
                #
                # It has to be a FILE — Codex takes the prompt no other way. ``base_instructions``
                # is what the session record calls the resulting prompt, but it is not a settable
                # key ("unknown configuration field `base_instructions`" under --strict-config),
                # and setting it silently did nothing on a normal run.
                instructions_file = _write_instructions(system_prompt)
                if instructions_file is not None:
                    command.extend(["-c", f"experimental_instructions_file={json.dumps(str(instructions_file))}"])
        else:
            command.extend(["-s", "workspace-write"])
        # ``commit_guidance`` is accepted for a uniform interface but unused on a headless run:
        # ``base_instructions`` is the only system-prompt lever Codex exposes and it REPLACES
        # rather than appends, so using it here would delete the coding agent's own instructions.
        # The interactive proxy path has the same constraint — see CodexProxyAgent.spawn_command.
        command.extend(self.backend_args)  # passthrough options go before the positionals (#32)
        if session_id and not bare:
            # `codex exec resume` is a SUBCOMMAND, so every option above must already have been
            # emitted: `codex exec resume -s workspace-write <id>` is rejected outright
            # ("unexpected argument '-s' found"). Options first, then `resume <id> <prompt>`.
            command.extend(["resume", session_id])
        command.append(prompt)

        process = subprocess.Popen(
            resolve_subprocess_command(command),  # find/launch codex(.cmd/.exe) on Windows (#118)
            cwd=self.repo,
            text=True,
            # Codex reads a piped stdin and folds it into the prompt as a `<stdin>` block ("Reading
            # additional input from stdin..."). aGiTrack's own stdin is the user's TERMINAL, so
            # inheriting it would let the coding agent consume the keystrokes meant for the host —
            # and a summarizer run in the background daemon would block forever waiting on EOF.
            stdin=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
            # Keep the codex CLI off a console on Windows: the background daemon runs the
            # summarizer console-less, so without this each bare summary call flashes a console
            # window. detach_stdin=False because stdin is set explicitly above. See proc.py.
            **console_isolation_kwargs(detach_stdin=False),
        )
        watchdog: threading.Timer | None = None
        if bare:
            watchdog = threading.Timer(timeout_seconds or _SUMMARIZER_TIMEOUT_SECONDS, process.kill)
            watchdog.daemon = True
            watchdog.start()
        try:
            # A bare run is a BACKGROUND helper (the summarizer): it must never echo the streamed
            # agent text to aGiTrack's own stdout, which is the host terminal — otherwise the
            # summary leaks onto the screen next to the user's input box while a session is live.
            # A non-bare run is foreground shell mode, where streaming the progress is the point.
            final_response, parsed_session_id, tokens = self._read_events(process.stdout, stream_console=not bare)
            exit_code = process.wait()
        finally:
            if watchdog is not None:
                watchdog.cancel()

        # Codex's event stream names no model — verified against the real CLI: neither
        # thread.started, item.completed nor turn.completed carries one — so ask the session
        # store when the caller didn't pin one, exactly as the OpenCode backend has to.
        # Best-effort: None simply records no model, as before.
        resolved_model = model
        if not resolved_model and (parsed_session_id or session_id):
            from agitrack.transcripts.codex import session_model

            resolved_model = session_model(parsed_session_id or session_id or "")

        return AgentResult(
            backend=self.name,
            session_id=parsed_session_id or session_id,
            model=resolved_model,
            final_response=final_response.strip(),
            exit_code=exit_code,
            tokens=tokens,
        )

    def _read_events(
        self, output: IO[str] | None, *, stream_console: bool = True
    ) -> tuple[str, str | None, TokenUsage]:
        """Consume ``codex exec --json``'s JSONL event stream.

        Only the LAST ``agent_message`` is the answer: Codex narrates its work in earlier
        agent_messages ("I'm checking the repository state..."), so joining them all would put
        the narration into the commit summary. Non-JSON lines (Codex prints a plain
        "Reading additional input from stdin..." banner and any panic text) are ignored rather
        than parsed, so a diagnostic line can't become the final response.
        """
        if output is None:
            return "", None, TokenUsage()

        messages: list[str] = []
        session_id: str | None = None
        tokens = TokenUsage()
        for line in output:
            line = line.strip()
            if not line:
                continue
            if not line.startswith("{"):
                if self.verbose:
                    print(line)
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            kind = str(event.get("type") or "")
            if kind == "thread.started":
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str) and thread_id.strip():
                    session_id = thread_id.strip()
                continue
            if kind == "turn.completed":
                tokens.add(self._usage(event.get("usage")))
                continue
            if kind in ("turn.failed", "error"):
                if self.verbose:
                    print(line)
                continue
            if kind != "item.completed":
                continue
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    messages.append(text.strip())
                    if stream_console:
                        print(text.strip())
            elif stream_console and item.get("type") == "command_execution":
                command = item.get("command")
                if isinstance(command, str) and command:
                    print(f"[{command}]")
        return (messages[-1] if messages else ""), session_id, tokens

    def _usage(self, usage: object) -> TokenUsage:
        """A ``turn.completed`` usage block in aGiTrack's token categories.

        Same two Codex conversions the transcript parser documents at length: ``input_tokens``
        already INCLUDES ``cached_input_tokens``, and ``output_tokens`` already includes
        ``reasoning_output_tokens``. Both are subtracted out so ``input`` means fresh input and
        the generated categories don't overlap. Kept in step with
        ``agitrack.transcripts.codex._turn_tokens`` — the same numbers must come out whether a
        run is read live from the stream or later from the rollout file.
        """
        if not isinstance(usage, dict):
            return TokenUsage()
        input_tokens = _int(usage.get("input_tokens"))
        cache_read = _int(usage.get("cached_input_tokens"))
        cache_write = _int(usage.get("cache_write_input_tokens"))
        output_tokens = _int(usage.get("output_tokens"))
        reasoning = _int(usage.get("reasoning_output_tokens"))
        return TokenUsage(
            context=input_tokens or None,
            total=output_tokens,
            input=max(0, input_tokens - cache_read),
            output=max(0, output_tokens - reasoning),
            reasoning=reasoning,
            cache_read=cache_read,
            cache_write=cache_write,
        )


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _write_instructions(system_prompt: str) -> Path | None:
    """Persist a bare run's system prompt where Codex can read it, or None if it can't be written.

    Deliberately a STABLE path per prompt (its hash) inside aGiTrack's own config dir rather
    than a fresh temp file per call: the summarizer runs once per commit with one of a handful
    of fixed instructions, so reusing the file avoids littering the temp dir with thousands of
    near-identical prompts over a long session, and there is nothing to clean up on a crash.
    Returning None (rather than raising) degrades to a run under Codex's default agent prompt —
    a more expensive summary, not a failed commit.
    """
    import hashlib

    from agitrack.summaries.summarizer import summary_scratch_dir

    try:
        directory = summary_scratch_dir() / "instructions"
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:16]
        path = directory / f"{digest}.md"
        if not path.exists():
            path.write_text(system_prompt, encoding="utf-8")
        return path
    except OSError:
        return None
