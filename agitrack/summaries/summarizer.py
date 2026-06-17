from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from agitrack.env import getenv_compat

from agitrack.summaries.prompts import (
    COMMIT_SUMMARY_SYSTEM,
    PRE_COMPACTION_SYSTEM,
    SESSION_UPDATE_SYSTEM,
)

if TYPE_CHECKING:
    from agitrack.backends.base import AgentBackend
    from agitrack.transcripts.types import ExportedSession


class UnusableSummaryError(RuntimeError):
    """The summarizer backend returned an error instead of a summary."""


# Bound the summarizer prompt. The model is perfectly capable of this task; the
# prompt-echo failure comes from *how much* we send it. Feeding an unbounded input
# to `claude -p` puts the "you are a summarizer, output only the summary"
# instruction at the very top, with the generation cue ("Summary:") far below — so
# the model brushes its context limit and/or slips into completion/echo mode,
# continuing the input instead of summarizing. Keeping the input bounded (and
# restating the instruction next to the generation cue) keeps it in summarization
# mode.
_MAX_TRACE_CHARS = 60_000
_MAX_RESPONSE_CHARS = 6_000
_MAX_TURNS_CHARS = 60_000
_MAX_PRIOR_SUMMARY_CHARS = 8_000

# Restated right before the generation cue so the directive is never lost in the
# middle of a long prompt — the practical antidote to the echo failure mode.
_GENERATION_REMINDER = (
    "Write the summary now, following the instructions above. Begin immediately "
    "with the topic sentence and output only the summary — do not repeat these "
    "instructions or the conversation."
)


def _truncate(text: str | None, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated {len(text) - limit} more chars]"


def _turns_block(turns, *, budget: int = _MAX_TURNS_CHARS) -> str:
    """Render the conversation turns within a character budget, keeping the most
    recent (most relevant to this commit) and capping each response, so a long
    conversation can't blow the prompt up."""
    rendered: list[str] = []
    used = 0
    for turn in reversed(list(turns)):
        chunk = ""
        if turn.user_prompt:
            chunk += "\nUser: " + _truncate(turn.user_prompt, _MAX_RESPONSE_CHARS)
        if turn.final_response:
            chunk += "\nAgent: " + _truncate(turn.final_response, _MAX_RESPONSE_CHARS)
        if not chunk:
            continue
        if used + len(chunk) > budget and rendered:
            rendered.append("\n[earlier turns omitted]")
            break
        rendered.append(chunk)
        used += len(chunk)
    rendered.reverse()
    return "".join(rendered)


# Backend error messages that come back through the result text with a zero
# exit code (issue #8: "You've hit your session limit..." ended up as a commit
# subject). Matched against the summary's first line only — the would-be
# subject — with error-shaped phrasings, so a legitimate summary that merely
# *mentions* limits or errors is never rejected.
_UNUSABLE_SUMMARY_RE = re.compile(
    r"(?i)(?:"
    r"you'?\s*(?:ha)?ve hit your"
    r"|hit your (?:session|usage|rate|spending) limit"
    r"|(?:session|usage) limit reached"
    r"|limit will reset"
    r"|credit balance is too low"
    r"|please run /login"
    r"|invalid api key"
    r"|not logged in"
    r"|^\s*api error"
    r"|overloaded_error"
    r"|rate_limit_error"
    r"|authentication_error"
    r")"
)


# Phrases that only ever appear in the summarizer's *prompt* (its system
# instructions and scaffolding), never in a real summary. Some headless backend
# runs return the input prompt verbatim as the "response" with a zero exit code;
# without this guard that echoed prompt became the commit message — the bug where
# a commit subject was "You are a technical summarizer for a coding session…".
# Matched anywhere in the text (an echo restates these), and chosen to be
# distinctive enough that a genuine code-change summary never contains them.
_PROMPT_ECHO_MARKERS = (
    "you are a technical summarizer",
    "you are maintaining a running session summary",
    "you are capturing the full context of a coding session",
    "recent conversation turns:",
)


def summary_is_usable(text: str | None) -> bool:
    """True when *text* looks like an actual summary — not a backend error, and
    not an echo of the summarizer's own prompt."""
    stripped = (text or "").strip()
    first_line = next((line for line in stripped.splitlines() if line.strip()), "")
    if not first_line:
        return False
    if _UNUSABLE_SUMMARY_RE.search(first_line) is not None:
        return False
    lowered = stripped.lower()
    return not any(marker in lowered for marker in _PROMPT_ECHO_MARKERS)


def _looks_like_prompt_echo(prompt: str, response: str) -> bool:
    """The backend returned (a leading chunk of) the prompt we sent instead of a
    summary. Independent of which prompt was used: an echo restates the prompt
    from the top, so the normalised openings match. This backstops
    :data:`_PROMPT_ECHO_MARKERS` if the prompt wording ever changes."""

    def norm(text: str) -> str:
        return " ".join(text.split()).lower()

    prompt_norm, response_norm = norm(prompt), norm(response)
    window = min(len(prompt_norm), len(response_norm), 80)
    return window >= 40 and prompt_norm[:window] == response_norm[:window]


# A meta-preamble the model sometimes prepends despite being told not to —
# "The summary has been written…", "Here is the summary:", "No further action is
# needed…". It must not become the commit subject; the real summary follows it.
# An opening acknowledgement a genuine topic sentence never starts with:
_PREAMBLE_ACK_RE = re.compile(r"(?is)^\s*(?:sure|certainly|of course|okay|ok|absolutely|got it|understood)\b[,!.:\s]")
# Paragraph that talks ABOUT the summary/the task rather than being the summary:
_PREAMBLE_META_RE = re.compile(
    r"(?is)(?:"
    r"\bsummary (?:has been|is|follows|will follow|below|here|complete|ready|as requested)\b"
    r"|\bhere(?:'s| is| are)\b[^\n]{0,80}\bsummar"
    r"|\b(?:provided|wrote|written|prepared|composed|produced|generated)\b[^\n]{0,80}\bsummar"
    r"|\bno further action\b"
    r"|\bthe (?:conversation turns|diff|input|changes)\b[^\n]{0,120}\bsummari"
    r")"
)
# "Here is the summary: <real text>" / "Below is the summary — <real text>":
_PREAMBLE_COLON_RE = re.compile(
    r"(?is)^\s*(?:here(?:'s| is| are)|below (?:is|are)|the following is)[^:\n]{0,60}[:—-]\s+(?=\S)"
)


def _first_paragraph(text: str) -> tuple[str, str]:
    """Split off the first blank-line-delimited paragraph: ``(head, rest)``.
    ``rest`` is empty when there is only one paragraph."""
    parts = re.split(r"\n[ \t]*\n", text.strip(), maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (text.strip(), "")


def strip_summary_preamble(text: str) -> str:
    """Drop a leading meta-preamble so the summary's own topic sentence leads (and
    becomes the commit subject). Conservative: only a first paragraph that is
    recognisably *about* summarizing (or a bare acknowledgement), only while real
    content remains after it, and never reducing the text to nothing."""
    cleaned = text.strip()
    for _ in range(3):  # peel a few stacked preambles, but bail out quickly
        before = cleaned
        colon = _PREAMBLE_COLON_RE.match(cleaned)
        if colon and cleaned[colon.end() :].strip():
            cleaned = cleaned[colon.end() :].strip()
        else:
            head, rest = _first_paragraph(cleaned)
            if rest and len(head) <= 500 and (_PREAMBLE_ACK_RE.match(head) or _PREAMBLE_META_RE.search(head)):
                cleaned = rest
        if cleaned == before:
            break
    return cleaned or text.strip()


def summary_scratch_dir() -> Path:
    """A stable directory, outside any repository, for summarizer backends.

    Headless summarizer calls (``claude -p`` / ``opencode run``) record a real
    session transcript keyed by their working directory. Running them in the
    session worktree (or the repo root) made the summary conversation that
    directory's newest non-empty session, which the parse worker and the
    exit-time adoption then resumed instead of the user's actual conversation
    (issues #8/#56). Running every summarizer call from this scratch directory
    keeps summary sessions out of every repository's session records, so they
    can never be adopted, listed, or resumed as the previous session.
    """
    config_dir = getenv_compat("CONFIG_DIR")
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".agitrack"
    path = base / "summarizer"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Summarizer:
    def __init__(self, backend: AgentBackend, *, model: str | None = None) -> None:
        self.backend = backend
        self.model = model
        # Tokens this summarizer instance consumed across its LLM calls, so the
        # cost of summarization can be tracked next to the coding session's own
        # usage (issue #8). ``tokens_input`` counts fresh input the SAME way the main
        # commit line does — uncached input PLUS cache-creation tokens, since those
        # are input processed once and written to the cache. Counting only the bare
        # ``input_tokens`` made a cache-served summary report a tiny constant (~20)
        # while the real input sat in the cache fields (the "always 20" bug).
        self.tokens_input = 0
        self.tokens_output = 0
        # Cache READS are reported separately: those tokens were already counted as
        # input when first written, so folding them into ``tokens_input`` would
        # double-count them.
        self.tokens_cache_read = 0

    def summarize_commit(
        self,
        *,
        trace: str,
    ) -> str:
        # The commit summary is built from ONLY the interaction trace appended to
        # the commit — the same User/Agent text the commit carries, and nothing
        # else (no diff, no out-of-band context). The trace already states both the
        # request and what the agent did, so restricting the input to exactly the
        # committed trace keeps the summary faithful to it. It is also deliberately
        # not seeded with the rolling session summary (that folded earlier,
        # unrelated work in); the rolling summary is maintained separately.
        return self._run(self._build_commit_prompt(trace))

    def update_session_summary(
        self,
        *,
        current_summary: str | None,
        trace: str,
        commit_summary: str,
    ) -> str:
        return self._run(self._build_session_update_prompt(current_summary, trace, commit_summary))

    def summarize_pre_compaction(
        self,
        *,
        exported_session: ExportedSession,
        current_summary: str | None = None,
    ) -> str:
        return self._run(self._build_pre_compaction_prompt(exported_session, current_summary))

    def _run(self, prompt: str) -> str:
        # ``bare``: the summarizer must read ONLY its instruction plus the interaction
        # trace — not the backend's agent system prompt, tool schemas, or project/user
        # memory, which would add thousands of input tokens the summary never uses.
        result = self.backend.run(prompt, model=self.model, session_id=None, bare=True)
        tokens = getattr(result, "tokens", None)
        if tokens is not None:
            # Fresh input = uncached input + cache-creation tokens (both are input the
            # model processed this call); cache reads are tracked on their own line.
            self.tokens_input += int(getattr(tokens, "input", 0) or 0) + int(getattr(tokens, "cache_write", 0) or 0)
            self.tokens_output += int(getattr(tokens, "output", 0) or 0)
            self.tokens_cache_read += int(getattr(tokens, "cache_read", 0) or 0)
        text = result.final_response.strip()
        # A failed/echoed run must raise rather than return its text, or the error
        # (or the prompt itself) becomes the commit subject (issue #8). Callers
        # already treat a raising summarizer as "no summary" and keep the
        # prompt-led message.
        if result.exit_code != 0:
            raise UnusableSummaryError(f"summarizer backend exited with {result.exit_code}: {text[:200]}")
        if _looks_like_prompt_echo(prompt, text):
            raise UnusableSummaryError(f"summarizer echoed its prompt instead of summarizing: {text[:200]}")
        if not summary_is_usable(text):
            raise UnusableSummaryError(f"summarizer returned an error message: {text[:200]}")
        # Drop any "Here is the summary…" preamble so the topic sentence leads.
        return strip_summary_preamble(text)

    def _build_commit_prompt(self, trace: str) -> str:
        parts = [
            COMMIT_SUMMARY_SYSTEM,
            "\n\nInteraction trace:\n",
            _truncate(trace, _MAX_TRACE_CHARS),
            "\n\n",
            _GENERATION_REMINDER,
            "\n\nSummary:",
        ]
        return "".join(parts)

    def _build_session_update_prompt(
        self,
        current_summary: str | None,
        trace: str,
        commit_summary: str,
    ) -> str:
        parts = [SESSION_UPDATE_SYSTEM, "\n\n"]
        if current_summary:
            parts.extend(["Current session summary:\n", _truncate(current_summary, _MAX_PRIOR_SUMMARY_CHARS), "\n\n"])
        else:
            parts.append("No previous session summary exists. Create an initial summary.\n\n")
        parts.extend(["New commit summary:\n", _truncate(commit_summary, _MAX_PRIOR_SUMMARY_CHARS), "\n\n"])
        parts.extend(["Interaction trace:\n", _truncate(trace, _MAX_TRACE_CHARS), "\n\n"])
        parts.extend([_GENERATION_REMINDER, "\n\nUpdated session summary:"])
        return "".join(parts)

    def _build_pre_compaction_prompt(
        self,
        exported_session: ExportedSession,
        current_summary: str | None,
    ) -> str:
        parts = [PRE_COMPACTION_SYSTEM, "\n\n"]
        if current_summary:
            parts.extend(["Current session summary:\n", _truncate(current_summary, _MAX_PRIOR_SUMMARY_CHARS), "\n\n"])
        parts.append("Full session transcript:\n")
        # Pre-compaction summarises a whole session, so allow a larger transcript
        # budget than a single commit — but still bounded, to keep the model in
        # summarization mode rather than echoing a giant prompt.
        parts.append(_turns_block(exported_session.turns, budget=_MAX_TURNS_CHARS * 3))
        parts.extend(["\n\n", _GENERATION_REMINDER, "\n\nComprehensive session summary:"])
        return "".join(parts)
