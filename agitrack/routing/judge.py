"""The summarizer-as-judge: extract a structured quality signal from the
interaction trace.

The summarizer model is always small and cheap (the user's design choice:
"the summarizer is always a small and cheap model"). After it summarizes the
trace for the commit message, this module makes ONE additional bare call —
same backend, same model, same scratch dir — to extract:

* task_class: greenfield | edit | debug | refactor | test | docs | explain | config | other
* complexity: trivial | small | medium | large
* correction: none | explicit_negative | redo | clarification
* evidence:    a short phrase from the trace that justifies the verdict

The output is strict JSON; malformed JSON is rejected and treated as no signal
(never an excuse to drop the commit summary). The same echo / error guards
from :mod:`agitrack.summaries.summarizer` apply, so a tiny local model that
echoes the prompt is detected and recorded as ``none`` rather than corrupting
the preference store.

The judge is intentionally cheap: it shares the summarizer backend/model, runs
on the same worker thread (no new background process), and bounds the trace
to keep the prompt small. The cost is "one small call per turn"; the value is
a structured signal that can drive the router without the user having to
rate anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from agitrack.summaries.summarizer import (
    Summarizer,
    UnusableSummaryError,
    summary_is_usable,
)

from agitrack.routing.store import (
    COMPLEXITY_LEVELS,
    TASK_CLASSES,
    EVENT_KIND_JUDGE_ACCEPT,
    EVENT_KIND_JUDGE_CORRECTION,
)

# Bound the trace we send to the judge. The summarizer's bound (60k chars) is
# too generous for the small judge model: it just dilutes the signal. The
# judge wants the *shape* of the turn (intent, outcome, any redo), not the
# full transcript. 12k characters is roughly the last 8-10 turns of a typical
# session — more than enough for a quality classification.
_MAX_TRACE_CHARS = 12_000
# The judge model is small. The expected response is one short JSON object; if
# the model is producing walls of text, something has gone wrong.
_MAX_RESPONSE_CHARS = 1_500

# Heuristics applied BEFORE the model call to short-circuit obvious cases and
# save the round trip. Cheap and conservative — they only fire when the signal
# is already loud, so a missed heuristic just falls through to the model.
_NEGATIVE_MARKERS = (
    "no, that",
    "no that's",
    "that's wrong",
    "thats wrong",
    "try again",
    "not what i asked",
    "i asked for",
    "undo that",
    "revert that",
    "wrong file",
    "stop doing",
    "don't do that",
    "do not do that",
    "this isn't",
    "this is not",
    "this is wrong",
    "this is broken",
    "you broke",
    "you missed",
    "you forgot",
    "please stop",
    "i didn't ask",
    "that's not what",
    "that is not what",
)
_REDO_MARKERS = (
    "actually,",
    "instead,",
    "rather than",
    "let's redo",
    "start over",
    "do it again",
    "from scratch",
    "ignore that",
    "i meant",
    "let me clarify",
    "to clarify",
    "what i wanted",
    "what i actually",
    "different approach",
    "redo that",
)


@dataclass
class JudgeResult:
    """The structured signal returned by the judge. Always carries a model
    label (for the routing store's metadata) and one of two verdicts:
    ``correction != "none"`` is a NEGATIVE signal, ``correction == "none"``
    is a (weak) POSITIVE signal. ``usable=False`` is treated as no signal."""

    task_class: str
    complexity: str
    correction: str
    evidence: str
    model: str | None
    usable: bool

    def to_signal_kind(self) -> str | None:
        """Map to a routing-store event kind, or None for "no signal"."""
        if not self.usable:
            return None
        if self.correction == "none":
            return EVENT_KIND_JUDGE_ACCEPT
        return EVENT_KIND_JUDGE_CORRECTION

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_class": self.task_class,
            "complexity": self.complexity,
            "correction": self.correction,
            "evidence": self.evidence,
            "model": self.model,
            "usable": self.usable,
        }


# JSON the judge must return. We accept a small tolerance (case-insensitive
# keys, extra fields) and fall back to "other" / "none" for any unknown value
# so a slightly-imprecise model can't crash the store.
_VALID_CORRECTIONS = {"none", "explicit_negative", "redo", "clarification"}


def _coerce(value: Any, allowed: tuple[str, ...], default: str) -> str:
    if not isinstance(value, str):
        return default
    candidate = value.strip().lower()
    if candidate in allowed:
        return candidate
    # Tolerate hyphens / spaces.
    candidate = candidate.replace("-", "_").replace(" ", "_")
    if candidate in allowed:
        return candidate
    return default


def _parse_judge_json(text: str) -> JudgeResult | None:
    """Extract the JSON object from the model's text. We accept leading/
    trailing prose (the small model often wraps the JSON in a sentence); the
    first balanced ``{…}`` block wins."""
    if not text:
        return None
    text = text.strip()
    # Fast path: a clean JSON document.
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = json.loads(text)
        except json.JSONError:
            parsed = None
    else:
        parsed = None
    if parsed is None:
        # Greedy first JSON object in the response.
        match = re.search(r"\{[^{}]*\}", text, flags=re.DOTALL)
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONError:
            return None
    if not isinstance(parsed, dict):
        return None
    task_class = _coerce(parsed.get("task_class"), TASK_CLASSES, "other")
    complexity = _coerce(parsed.get("complexity"), COMPLEXITY_LEVELS, "small")
    correction = _coerce(parsed.get("correction"), _VALID_CORRECTIONS, "none")
    evidence_raw = parsed.get("evidence")
    evidence = evidence_raw.strip()[:280] if isinstance(evidence_raw, str) else ""
    return JudgeResult(
        task_class=task_class,
        complexity=complexity,
        correction=correction,
        evidence=evidence,
        model=None,
        usable=True,
    )


def heuristic_correction(trace: str) -> tuple[str, str] | None:
    """A fast pre-check for correction markers in the trace. Returns
    (kind, evidence) when something is loud, else None. Conservative: the
    full judge call still happens, so a heuristic miss is harmless; a
    heuristic false-positive would only OVER-attribute corrections, which is
    the safer direction (we want to penalise a model that looks bad even if
    the judge itself is unsure)."""
    lowered = trace.lower()
    for marker in _NEGATIVE_MARKERS:
        if marker in lowered:
            return "explicit_negative", marker
    for marker in _REDO_MARKERS:
        if marker in lowered:
            return "redo", marker
    return None


# Prompts — kept inline so the judge is self-contained and easy to test.

_JUDGE_SYSTEM = (
    "You classify a coding-agent turn. Read the interaction trace and respond "
    "ONLY with a single JSON object on one line, no prose, no markdown, no "
    "code fences. The JSON shape is exactly: "
    '{"task_class": "<one of: greenfield, edit, debug, refactor, test, docs, explain, config, other>", '
    '"complexity": "<one of: trivial, small, medium, large>", '
    '"correction": "<one of: none, explicit_negative, redo, clarification>", '
    '"evidence": "<short phrase from the trace that justifies your verdict, max 25 words>"}'
)

_JUDGE_USER_TEMPLATE = (
    "Interaction trace:\n"
    "{trace}\n\n"
    "Classify this turn. Output JSON only."
)


class TurnJudge:
    """Run the cheap summarizer model as a judge over an interaction trace.

    Constructed from a :class:`agitrack.summaries.summarizer.Summarizer`
    (same backend, same model, same scratch dir). The judge call is a single
    bare ``backend.run`` — a small additional token cost per turn."""

    def __init__(self, summarizer: Summarizer) -> None:
        self._summarizer = summarizer
        self._last_tokens_input = 0
        self._last_tokens_output = 0
        self._last_tokens_cache_read = 0

    @property
    def model(self) -> str | None:
        return self._summarizer.model

    @property
    def tokens_input(self) -> int:
        return self._last_tokens_input

    @property
    def tokens_output(self) -> int:
        return self._last_tokens_output

    @property
    def tokens_cache_read(self) -> int:
        return self._last_tokens_cache_read

    def judge(self, trace: str) -> JudgeResult:
        """Return a structured judgement. Never raises: a failure is recorded
        as ``usable=False`` and treated as no signal by the store."""
        # Heuristic pre-check: a clearly-negative trace is recorded as
        # explicit_negative WITHOUT burning a model call. The store still gets
        # a clean verdict; the model is only called for the harder cases.
        heuristic = heuristic_correction(trace)
        if heuristic is not None:
            kind, evidence = heuristic
            return JudgeResult(
                task_class="other",
                complexity="small",
                correction=kind,
                evidence=evidence,
                model=self.model,
                usable=True,
            )
        # Capture the summarizer's token counts BEFORE the judge call so we
        # can report the deltas on the judge's own counters (the summarizer
        # uses them as a running total across its own summary call too).
        before_input = self._summarizer.tokens_input
        before_output = self._summarizer.tokens_output
        before_cache = self._summarizer.tokens_cache_read
        # Otherwise: prompt the small model and parse the JSON it returns.
        bounded_trace = trace[-_MAX_TRACE_CHARS:] if trace else ""
        user_prompt = _JUDGE_USER_TEMPLATE.format(trace=bounded_trace)
        try:
            response = self._summarizer._run(_JUDGE_SYSTEM, user_prompt)  # noqa: SLF001
        except UnusableSummaryError:
            return JudgeResult(
                task_class="other",
                complexity="small",
                correction="none",
                evidence="",
                model=self.model,
                usable=False,
            )
        except Exception:
            return JudgeResult(
                task_class="other",
                complexity="small",
                correction="none",
                evidence="",
                model=self.model,
                usable=False,
            )
        if not summary_is_usable(response):
            return JudgeResult(
                task_class="other",
                complexity="small",
                correction="none",
                evidence="",
                model=self.model,
                usable=False,
            )
        if len(response) > _MAX_RESPONSE_CHARS:
            response = response[:_MAX_RESPONSE_CHARS]
        parsed = _parse_judge_json(response)
        if parsed is None:
            return JudgeResult(
                task_class="other",
                complexity="small",
                correction="none",
                evidence="",
                model=self.model,
                usable=False,
            )
        parsed.model = self.model
        # Track the tokens used for the judge call on its own counters so the
        # store metadata can report them separately from the commit summary.
        self._last_tokens_input = self._summarizer.tokens_input - before_input
        self._last_tokens_output = self._summarizer.tokens_output - before_output
        self._last_tokens_cache_read = self._summarizer.tokens_cache_read - before_cache
        return parsed


__all__ = [
    "TurnJudge",
    "JudgeResult",
    "heuristic_correction",
    "_parse_judge_json",
]
