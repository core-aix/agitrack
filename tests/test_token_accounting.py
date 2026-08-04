"""Per-turn token accounting, pinned against REAL backend output.

Token counts ride on every commit and drive the dashboard's cost and context reporting. They
fail silently by construction — a wrong number looks exactly like a right one — and the two
backends report usage in completely different shapes, so this is where a per-backend bug hides
longest.

The event payloads below are copied verbatim from a live `opencode run --format json` on a
three-step turn (read a file, read another, answer). Recording them makes the question
"are these per-step or cumulative?" a settled fact instead of an assumption: aGiTrack SUMS
across events, which is only correct if they are per-step.
"""

from __future__ import annotations

import json

import pytest

from agitrack.backends.base import TokenUsage
from agitrack.backends.opencode import OpenCodeBackend

# Verbatim from a live OpenCode turn. Three steps; note `input` GROWS (the conversation
# accumulates) while `cache.read` stays constant (the same cached prefix is re-read each step).
_REAL_STEPS = [
    {"total": 6108, "input": 347, "output": 69, "reasoning": 60, "cache": {"write": 0, "read": 5632}},
    {"total": 6267, "input": 568, "output": 67, "reasoning": 0, "cache": {"write": 0, "read": 5632}},
    {"total": 6365, "input": 727, "output": 6, "reasoning": 0, "cache": {"write": 0, "read": 5632}},
]


def _step_finish_line(tokens: dict) -> str:
    return json.dumps({"type": "step_finish", "sessionID": "ses_x", "part": {"type": "step-finish", "tokens": tokens}})


def _read(lines):
    backend = OpenCodeBackend(repo=None)  # type: ignore[arg-type]  # no process is spawned here
    _, _, _, tokens = backend._read_events(iter(lines), stream_console=False)
    return tokens


# --- are the events per-step or cumulative? --------------------------------


def test_opencodes_step_totals_are_per_step_not_cumulative():
    """The assumption aGiTrack's summing rests on, checked against the real numbers.

    Each step's `total` is exactly that step's own input+output+reasoning+cache.read. If the
    field were a running total, summing it (which is what `_read_events` does) would inflate
    every turn — and nothing downstream would notice.
    """
    for step in _REAL_STEPS:
        derived = step["input"] + step["output"] + step["reasoning"] + step["cache"]["read"]
        assert step["total"] == derived, f"{step} is not a per-step total"


def test_output_tokens_sum_across_the_steps_of_one_turn():
    # 69 + 67 + 6. Each step is a separate model call, so its output is genuinely new.
    tokens = _read([_step_finish_line(step) for step in _REAL_STEPS])

    assert tokens.output == 142
    assert tokens.reasoning == 60
    assert tokens.total == 142 + 60  # `total` is what the turn produced: output + reasoning


def test_context_counts_the_cached_prefix_not_just_the_fresh_input():
    """`context` is how FULL the window is, so it must include the cached tokens the model read.

    Counting only `input` under-reported it by the entire cache — on this real turn 727 against
    a true 6365, nearly 9x low, which is what the dashboard's context gauge would have shown.
    Claude has always summed the three; this keeps the number meaning the same thing on both.
    """
    tokens = _read([_step_finish_line(_REAL_STEPS[-1])])

    assert tokens.context == 727 + 5632


def test_context_reports_the_latest_step_rather_than_a_sum():
    # A window level is not a quantity to accumulate: summing the three steps would claim a
    # context far larger than the model's window.
    tokens = _read([_step_finish_line(step) for step in _REAL_STEPS])

    assert tokens.context == 727 + 5632  # the last step's, not 347+568+727+3*5632


def test_the_two_backends_compute_context_the_same_way():
    # Parity, stated directly: the same conversation must not report a different context
    # depending on which agent produced it, or cross-backend comparison is meaningless.
    from agitrack.backends.claude import ClaudeBackend

    usage = {"input_tokens": 727, "output_tokens": 6, "cache_read_input_tokens": 5632, "cache_creation_input_tokens": 0}
    claude_tokens = ClaudeBackend(repo=None)._tokens(usage)  # type: ignore[arg-type]
    opencode_tokens = _read([_step_finish_line(_REAL_STEPS[-1])])

    assert claude_tokens.context == opencode_tokens.context


# --- shape tolerance --------------------------------------------------------


def test_events_without_a_token_payload_contribute_nothing():
    # Most events in the stream (step_start, text, tool) carry no tokens at all.
    tokens = _read(
        [
            json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
            json.dumps({"type": "text", "part": {"type": "text", "text": "hi"}}),
            _step_finish_line(_REAL_STEPS[0]),
        ]
    )

    assert tokens.output == 69


def test_a_missing_cache_block_is_treated_as_zero_not_an_error():
    # A provider that reports no caching at all omits the block entirely.
    tokens = _read([_step_finish_line({"total": 10, "input": 5, "output": 5, "reasoning": 0})])

    assert tokens.cache_read == 0 and tokens.cache_write == 0
    assert tokens.context == 5


def test_non_integer_token_values_are_ignored_rather_than_crashing():
    # The stream is JSON from another program; a null or a string must not take the turn down.
    tokens = _read([_step_finish_line({"input": None, "output": "many", "reasoning": 3, "cache": {"read": None}})])

    assert tokens.input == 0 and tokens.output == 0 and tokens.reasoning == 3


@pytest.mark.parametrize("line", ["", "   ", "not json at all", "{}", '{"type":"step_finish"}'])
def test_unparseable_or_empty_lines_are_skipped(line):
    # A torn line mid-write must cost that line, never the turn's accounting.
    tokens = _read([line, _step_finish_line(_REAL_STEPS[0])])

    assert tokens.output == 69


# --- the shared accumulator -------------------------------------------------


def test_token_usage_sums_every_field_including_the_subagent_buckets():
    # A field missed in `add` reads zero on every commit forever, which looks like "no sub-agent
    # ran" rather than like a bug.
    total = TokenUsage()
    for _ in range(3):
        total.add(
            TokenUsage(
                total=1,
                input=2,
                output=3,
                reasoning=4,
                cache_read=5,
                cache_write=6,
                subagent_input=7,
                subagent_output=8,
                subagent_reasoning=9,
                subagent_cache_read=10,
                subagent_cache_write=11,
            )
        )

    assert (total.total, total.input, total.output, total.reasoning) == (3, 6, 9, 12)
    assert (total.cache_read, total.cache_write) == (15, 18)
    assert (total.subagent_input, total.subagent_output, total.subagent_reasoning) == (21, 24, 27)
    assert (total.subagent_cache_read, total.subagent_cache_write) == (30, 33)


def test_a_report_that_omits_context_does_not_clear_what_we_already_knew():
    total = TokenUsage(context=1000)
    total.add(TokenUsage(context=None))
    assert total.context == 1000
