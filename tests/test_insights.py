"""Tests for the agent-efficiency insights (agitrack/metrics/insights.py).

Each category is driven with synthetic histories shaped like the inefficiency it detects,
plus the negative: a clean history (or one too thin to judge) yields nothing.
"""

from __future__ import annotations

from agitrack.metrics.collect import CommitStat
from agitrack.metrics.insights import MIN_TURNS, build_insights


def _turn(
    index: int,
    *,
    prompt: str = "do something useful",
    session: str = "sess-1",
    output: int = 5_000,
    cache_read: int = 1_000_000,
    ins: int = 10,
    dl: int = 2,
    kind: str = "agent",
) -> CommitStat:
    return CommitStat(
        sha=f"{index:040x}",
        author="Dev",
        email="d@e.com",
        subject=prompt[:50],
        kind=kind,
        timestamp=1_700_000_000 + index * 600,
        backend="claude",
        model="claude-opus-4-8",
        tokens={"input": 2_000, "output": output, "cache_read": cache_read},
        insertions=ins,
        deletions=dl,
        prompt=prompt,
        message=f"subject\n\nbackend_session_id: {session}\n",
    )


def _by_key(insights: list[dict]) -> dict[str, dict]:
    return {insight["key"]: insight for insight in insights}


def test_thin_history_yields_no_insights():
    # A young repo must show nothing, not noise.
    turns = [_turn(i, prompt="no, that is wrong") for i in range(MIN_TURNS - 1)]
    assert build_insights(turns) == []


def test_clean_history_yields_no_insights():
    # Plenty of data, but nothing inefficient: varied prompts, stable context, real edits.
    turns = [_turn(i, prompt=f"implement feature number {i} with tests") for i in range(40)]
    assert build_insights(turns) == []


def test_correction_loops_detected_with_token_cost():
    prompts = []
    for i in range(40):
        # Every third follow-up is corrective — 33%, well past the 12% gate.
        prompts.append("still doesn't work, the test fails again" if i % 3 == 0 else f"build part {i}")
    turns = [_turn(i, prompt=p) for i, p in enumerate(prompts)]
    insight = _by_key(build_insights(turns)).get("correction-loops")
    assert insight is not None
    assert "follow-up turns" in insight["evidence"][0]
    assert "output tokens" in insight["evidence"][1]  # the cost of the re-work is quantified
    assert insight["severity"] in ("medium", "high")


def test_corrective_prompt_detection_is_conservative():
    # A first turn is never corrective (nothing before it), and neutral prompts don't match.
    neutral = [_turn(i, prompt=f"please add endpoint {i}") for i in range(30)]
    assert "correction-loops" not in _by_key(build_insights(neutral))


def test_repeated_prompts_become_an_automation_suggestion():
    turns = [_turn(i, prompt=f"task number {i}") for i in range(30)]
    for i in range(3):
        turns.append(_turn(100 + i, prompt="Regenerate the dashboard screenshot for the docs"))
    insight = _by_key(build_insights(turns)).get("repeated-prompts")
    assert insight is not None
    assert "Asked 3 times" in insight["evidence"][0]
    assert "skill" in insight["suggestion"] or "CLAUDE.md" in insight["suggestion"]


def test_repeated_short_or_slash_prompts_are_ignored():
    turns = [_turn(i, prompt=f"task {i}") for i in range(20)]
    turns += [_turn(100 + i, prompt="continue") for i in range(5)]  # short: not a standing task
    turns += [_turn(200 + i, prompt="/compact now please") for i in range(5)]  # already automation
    assert "repeated-prompts" not in _by_key(build_insights(turns))


def test_context_growth_in_long_sessions():
    # One long session whose context grows 5x from the first third to the last.
    turns = [_turn(i, session="sess-long", cache_read=1_000_000 + i * 500_000, prompt=f"step {i}") for i in range(15)]
    turns += [_turn(100 + i, session="sess-b", cache_read=2_000_000 + i * 700_000) for i in range(9)]
    insight = _by_key(build_insights(turns)).get("context-growth")
    assert insight is not None
    assert "more context per turn" in insight["evidence"][0]


def test_flat_context_stays_silent():
    turns = [_turn(i, session="sess-long", cache_read=5_000_000) for i in range(20)]
    assert "context-growth" not in _by_key(build_insights(turns))


def test_session_fragmentation_detected():
    # 10 sessions, 8 of them one-shot.
    turns = []
    for s in range(8):
        turns.append(_turn(s, session=f"one-shot-{s}", prompt=f"quick fix {s}"))
    for s in range(2):
        for i in range(5):
            turns.append(_turn(100 + s * 10 + i, session=f"long-{s}", prompt=f"work {s}.{i}"))
    insight = _by_key(build_insights(turns)).get("session-fragmentation")
    assert insight is not None
    assert "sessions contain at most 2 turns" in insight["evidence"][0]


def test_file_rework_keys_on_quick_returns_not_delete_ratio():
    turns = [_turn(i, prompt=f"work {i}") for i in range(20)]
    base = 1_700_000_000
    # hot.py: 12 edits, each 10 minutes after the previous — rapid rework.
    hot = [(base + i * 600, 20, 18) for i in range(12)]
    # steady.py: 12 edits spread a day apart with the SAME delete ratio — normal iteration,
    # must NOT be flagged (a line replacement is always 1 ins + 1 del).
    steady = [(base + i * 86_400, 20, 18) for i in range(12)]
    insights = _by_key(build_insights(turns, {"hot.py": hot, "steady.py": steady}))
    insight = insights.get("file-rework")
    assert insight is not None
    assert "hot.py" in insight["evidence"][0]
    assert all("steady.py" not in line for line in insight["evidence"])


def test_low_yield_turns_detected():
    turns = [_turn(i, prompt=f"implement {i}") for i in range(30)]
    turns += [_turn(100 + i, prompt="analyze the architecture in depth", output=40_000, ins=0, dl=0) for i in range(6)]
    insight = _by_key(build_insights(turns)).get("low-yield-turns")
    assert insight is not None
    assert "without" in insight["evidence"][0]


def test_insights_sorted_most_severe_first():
    # Build a history that trips several categories and check the ordering contract.
    turns = []
    for i in range(60):
        prompt = "still broken, fix it again" if i % 2 == 0 else f"work {i}"
        turns.append(_turn(i, prompt=prompt))
    insights = build_insights(turns)
    assert insights, "expected at least one insight"
    ranks = [{"high": 0, "medium": 1, "info": 2}[insight["severity"]] for insight in insights]
    assert ranks == sorted(ranks)


def test_non_ai_and_tokenless_commits_are_excluded():
    user = [_turn(i, kind="user", prompt="no, wrong again") for i in range(50)]
    for stat in user:
        stat.tokens = {}
    assert build_insights(user) == []
