"""Per-turn token accounting, pinned against REAL backend output.

Token counts ride on every commit and drive the dashboard's cost and context reporting. They
fail silently by construction — a wrong number looks exactly like a right one — and the three
backends report usage in completely different shapes, so this is where a per-backend bug hides
longest.

The OpenCode event payloads below are copied verbatim from a live `opencode run --format json`
on a three-step turn (read a file, read another, answer). Recording them makes the question
"are these per-step or cumulative?" a settled fact instead of an assumption: aGiTrack SUMS
across events, which is only correct if they are per-step.

Codex's shape is the odd one out and gets its own section: its categories NEST (cached input
inside `input_tokens`, reasoning inside `output_tokens`) where Claude's and OpenCode's are
disjoint, so the conversion is where a Codex turn would silently double-count.
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


# The counters from a live Codex turn, quoted in `agitrack.transcripts.codex._turn_tokens`:
# 11532 input of which 8064 were cached, 245 output of which 20 was reasoning. Note what
# `total_tokens` is: input + output, with the cache NOT added on top — which is the evidence
# that `input_tokens` is the whole prompt rather than the uncached remainder.
_REAL_CODEX_USAGE = {
    "input_tokens": 11532,
    "cached_input_tokens": 8064,
    "cache_write_input_tokens": 0,
    "output_tokens": 245,
    "reasoning_output_tokens": 20,
    "total_tokens": 11777,
}


def _codex_tokens(usage, *, subagent=False):
    from agitrack.transcripts.codex import _turn_tokens

    return _turn_tokens(usage, subagent=subagent)


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


def test_every_backend_computes_context_the_same_way():
    # Parity, stated directly: the same conversation must not report a different context
    # depending on which agent produced it, or cross-backend comparison is meaningless.
    # One turn, three wire shapes — 727 fresh prompt tokens on top of a 5632-token cached
    # prefix. Claude and OpenCode report the fresh part and the cache separately (so the two
    # are ADDED); Codex reports the whole prompt in `input_tokens` with the cached part named
    # again as a subset (so nothing is added back). Same window, same number.
    from agitrack.backends.claude import ClaudeBackend

    usage = {"input_tokens": 727, "output_tokens": 6, "cache_read_input_tokens": 5632, "cache_creation_input_tokens": 0}
    claude_tokens = ClaudeBackend(repo=None)._tokens(usage)  # type: ignore[arg-type]
    opencode_tokens = _read([_step_finish_line(_REAL_STEPS[-1])])
    codex_tokens = _codex_tokens({"input_tokens": 727 + 5632, "cached_input_tokens": 5632, "output_tokens": 6})

    assert claude_tokens.context == opencode_tokens.context == codex_tokens.context == 727 + 5632
    # …and the fresh/cached split means the same thing on all three, so "input" is comparable.
    assert claude_tokens.input == opencode_tokens.input == codex_tokens.input == 727
    assert claude_tokens.cache_read == opencode_tokens.cache_read == codex_tokens.cache_read == 5632


# --- Codex: nested categories, unnested ------------------------------------
#
# Codex is the one backend whose reported categories CONTAIN each other. aGiTrack's categories
# are disjoint by contract (input excludes the cache, output excludes reasoning), so the two
# subsets are subtracted out on the way in. Getting that wrong is invisible: the turn still
# commits, with numbers that are simply too big.


def test_codexs_total_shows_that_its_input_already_contains_the_cached_prefix():
    """The assumption the subtraction rests on, checked against the real counters.

    `total_tokens` is `input_tokens + output_tokens`. If `input_tokens` were the FRESH input
    only, the cached 8064 would have to be added somewhere for the total to hold — it isn't,
    so `input_tokens` is the whole prompt the model read.
    """
    usage = _REAL_CODEX_USAGE

    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]
    assert usage["cached_input_tokens"] < usage["input_tokens"]  # a subset, not a separate pot


def test_codexs_cached_prefix_is_subtracted_out_of_the_reported_input():
    # Reporting Codex's raw `input_tokens` as `input` double-counts the cache on every turn:
    # on this real turn it would claim 11532 fresh tokens against a true 3468, and the
    # dashboard's cost line is computed from that number.
    tokens = _codex_tokens(_REAL_CODEX_USAGE)

    assert tokens.input == 11532 - 8064
    assert tokens.cache_read == 8064
    assert tokens.input + tokens.cache_read == 11532  # the split partitions the prompt exactly


def test_codexs_reasoning_is_subtracted_out_of_the_reported_output():
    # `output_tokens` includes `reasoning_output_tokens`. aGiTrack requires output + reasoning
    # to be the true generated total, so counting both raw would inflate a reasoning-heavy turn
    # by the whole reasoning budget.
    tokens = _codex_tokens(_REAL_CODEX_USAGE)

    assert tokens.output == 245 - 20
    assert tokens.reasoning == 20
    assert tokens.output + tokens.reasoning == tokens.total == 245


def test_codexs_context_is_the_whole_prompt_with_nothing_added_back():
    # Deliberately NOT the claude/opencode formula: adding the cache back here would count the
    # cached prefix twice and report a window fuller than the model's own limit.
    tokens = _codex_tokens(_REAL_CODEX_USAGE)

    assert tokens.context == 11532


def test_a_codex_turn_reads_the_same_from_the_live_stream_and_from_the_rollout():
    # Two implementations of one conversion: `codex exec --json` events (the headless backend)
    # and the rollout file (the proxy's parser). They are edited apart, and a turn re-read from
    # the rollout must not disagree with what was committed live — a drift here shows up as a
    # commit whose token metadata contradicts the dashboard's.
    from agitrack.backends.codex import CodexBackend

    live = CodexBackend(repo=None)._usage(_REAL_CODEX_USAGE)  # type: ignore[arg-type]
    rollout = _codex_tokens(_REAL_CODEX_USAGE)

    assert live == rollout


def test_a_cached_count_larger_than_the_input_never_produces_negative_tokens():
    # The invariant is guarded rather than trusted: a provider that ever reports more cached
    # than input would otherwise push NEGATIVE numbers into commit metadata and the dashboard,
    # where they read as nonsense rather than as a backend bug.
    tokens = _codex_tokens({"input_tokens": 100, "cached_input_tokens": 500, "output_tokens": 10})

    assert tokens.input == 0 and tokens.cache_read == 500

    generated = _codex_tokens({"input_tokens": 10, "output_tokens": 5, "reasoning_output_tokens": 50})
    assert generated.output == 0 and generated.reasoning == 50


def test_a_codex_usage_block_with_missing_or_non_integer_fields_contributes_zero():
    # The block is JSON from another program, and older/newer Codex releases omit fields.
    # A null or a string must cost that field, never the turn.
    tokens = _codex_tokens({"input_tokens": None, "output_tokens": "many", "cached_input_tokens": {}})

    assert (tokens.input, tokens.output, tokens.cache_read, tokens.reasoning) == (0, 0, 0, 0)
    assert tokens.context is None  # nothing known ⇒ don't claim a window level
    assert _codex_tokens({}) == _codex_tokens({"input_tokens": 0, "output_tokens": 0})


def test_a_codex_subagents_tokens_land_in_the_subagent_buckets_and_leave_context_alone():
    # A spawned agent has its OWN window, so its prompt must not move the parent turn's context
    # gauge — the same rule Claude sidechains and OpenCode `task` children follow.
    tokens = _codex_tokens(_REAL_CODEX_USAGE, subagent=True)

    assert tokens.context is None
    assert (tokens.input, tokens.output, tokens.reasoning) == (0, 0, 0)
    assert tokens.subagent_input == 11532 - 8064
    assert tokens.subagent_output == 245 - 20
    assert (tokens.subagent_reasoning, tokens.subagent_cache_read) == (20, 8064)


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
