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


def test_background_task_markers_are_not_read_as_user_prompts():
    # A turn the agent ran off a completed background task carries "(background task completed)"
    # instead of a real user prompt (and the label repeats when such turns fold together). It must
    # never be counted as a repeated ask — that is the machine talking to itself, not the user.
    turns = [_turn(i, prompt=f"real task {i}") for i in range(20)]
    turns += [_turn(100 + i, prompt="(background task completed) (background task completed)") for i in range(4)]
    insight = _by_key(build_insights(turns)).get("repeated-prompts")
    assert insight is None or all("background task" not in e.lower() for e in insight["evidence"])


def test_background_marker_is_stripped_leaving_the_real_follow_up():
    from agitrack.metrics.insights import _user_prompt

    assert _user_prompt("(background task completed)") == ""
    # A real prompt typed after a background turn opened must survive, and read as corrective.
    cleaned = _user_prompt("(background task completed) still doesn't work, same error")
    assert cleaned == "still doesn't work, same error"

    # That mixed turn should count as a correction (the user reacting), not be dropped.
    turns = [_turn(i, prompt=f"add feature {i}") for i in range(20)]
    turns += [_turn(100 + i, prompt="(background task completed) no, still fails, redo it") for i in range(20)]
    insight = _by_key(build_insights(turns)).get("correction-loops")
    assert insight is not None
    assert all("background task" not in e.lower() for e in insight["evidence"])


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


def test_file_rework_ignores_pure_growth_of_a_multi_feature_file():
    # A file edited over and over but only GROWING (insertions, ~no deletions) is a file that
    # houses several features being built up — not the same lines being redone. It must stay
    # silent even though the quick-return frequency is identical to a real hotspot.
    turns = [_turn(i, prompt=f"work {i}") for i in range(20)]
    base = 1_700_000_000
    # grows.py: 12 rapid edits, each a pure addition (new feature code), deletions ~0.
    grows = [(base + i * 600, 30, 0) for i in range(12)]
    # redone.py: 12 rapid edits that add and delete in equal measure — churn in place.
    redone = [(base + i * 600, 20, 19) for i in range(12)]
    insights = _by_key(build_insights(turns, {"grows.py": grows, "redone.py": redone}))
    insight = insights.get("file-rework")
    assert insight is not None
    assert "redone.py" in insight["evidence"][0]
    assert all("grows.py" not in line for line in insight["evidence"])


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


# --------------------------------------------------------------------------- scoping & trend


def test_insights_are_scoped_to_the_stats_passed_in():
    # The caller narrows the commits (the dashboard's filter); the insights must reflect only
    # those. A bad early period and a clean late one must not be judged together.
    bad = [_turn(i, prompt="still fails, try again") for i in range(30)]
    good = [_turn(100 + i, prompt=f"implement feature {i} with tests") for i in range(30)]

    assert "correction-loops" in _by_key(build_insights(bad))
    assert "correction-loops" not in _by_key(build_insights(good))  # same repo, later slice: clean


def test_trend_reports_improvement_between_the_windows_halves():
    # Corrections dominate the first half and vanish in the second: the card must say "better".
    turns = [_turn(i, prompt="still broken, fix it again") for i in range(20)]
    turns += [_turn(100 + i, prompt=f"add feature {i} and its test") for i in range(20)]
    insight = _by_key(build_insights(turns))["correction-loops"]
    trend = insight["trend"]
    assert trend["direction"] == "better"
    assert trend["change"] < 0  # lower is better, so a fall is negative
    assert trend["later"] < trend["earlier"]


def test_trend_reports_regression_when_a_habit_worsens():
    turns = [_turn(i, prompt=f"add feature {i}") for i in range(20)]
    turns += [_turn(100 + i, prompt="no, that is wrong, redo it") for i in range(20)]
    trend = _by_key(build_insights(turns))["correction-loops"]["trend"]
    assert trend["direction"] == "worse" and trend["change"] > 0


def test_steady_metric_is_not_reported_as_a_change():
    # A small wobble inside the noise band must read "steady", not a false win.
    prompts = ["still fails again" if i % 4 == 0 else f"work {i}" for i in range(40)]
    trend = _by_key(build_insights([_turn(i, prompt=p) for i, p in enumerate(prompts)]))["correction-loops"]["trend"]
    assert trend["direction"] == "steady"


def test_a_habit_that_stops_is_surfaced_as_an_improved_card():
    # Fires in the earlier half, gone in the later half, and — diluted across the whole window —
    # below the threshold overall. It must appear as a "good" win rather than silently vanishing.
    turns = [_turn(i, prompt="still doesn't work, again") for i in range(8)]
    turns += [_turn(100 + i, prompt=f"implement step {i} carefully") for i in range(70)]
    insights = _by_key(build_insights(turns))
    win = insights.get("correction-loops-resolved")
    assert win is not None
    assert win["severity"] == "good"
    assert win["trend"]["direction"] == "better"
    assert "correction-loops" not in insights  # not also reported as a live problem


def test_trend_names_the_two_periods_it_compares():
    # "vs earlier" is meaningless without saying WHEN earlier was. The trend must carry both
    # spans and their turn counts, split by turn count so each half holds the same sample size.
    turns = [_turn(i, prompt="still broken, fix it again") for i in range(20)]
    turns += [_turn(100 + i, prompt=f"add feature {i} and its test") for i in range(20)]
    trend = _by_key(build_insights(turns))["correction-loops"]["trend"]

    assert trend["earlier_turns"] == trend["later_turns"] == 20
    assert trend["earlier_from"] <= trend["earlier_to"] < trend["later_from"] <= trend["later_to"]
    assert trend["earlier_from"] == turns[0].timestamp  # the window really starts at the first turn
    assert trend["later_to"] == turns[-1].timestamp  # ...and ends at the last


def test_no_trend_when_a_half_is_too_thin():
    # At the MIN_TURNS floor each half is 6 turns — below MIN_HALF_TURNS, so findings are still
    # reported but no direction is claimed from six turns a side.
    turns = [_turn(i, prompt="still fails, again") for i in range(MIN_TURNS)]
    insights = build_insights(turns)
    assert insights, "expected findings even without a trend"
    for insight in insights:
        assert "trend" not in insight


# --------------------------------------------------------------------------- new categories


def _paths(turns, mapping):
    return {stat.sha: set(mapping(i)) for i, stat in enumerate(turns)}


def test_verification_gap_detected_when_code_ships_without_tests():
    turns = [_turn(i, prompt=f"change module {i}") for i in range(20)]
    # Every turn edits source; only the last two also edit a test.
    sha_paths = _paths(turns, lambda i: ["src/app.py"] + (["tests/test_app.py"] if i >= 18 else []))
    insight = _by_key(build_insights(turns, sha_paths=sha_paths)).get("verification-gap")
    assert insight is not None
    assert "changed no test file" in insight["evidence"][0]


def test_verification_gap_silent_when_tests_accompany_code():
    turns = [_turn(i, prompt=f"change module {i}") for i in range(20)]
    sha_paths = _paths(turns, lambda i: ["src/app.py", "tests/test_app.py"])
    assert "verification-gap" not in _by_key(build_insights(turns, sha_paths=sha_paths))


def test_verification_gap_silent_in_a_repo_with_no_tests_at_all():
    # Nothing to say about test discipline in a repo that has no tests in this window.
    turns = [_turn(i, prompt=f"change module {i}") for i in range(20)]
    sha_paths = _paths(turns, lambda i: ["src/app.py"])
    assert "verification-gap" not in _by_key(build_insights(turns, sha_paths=sha_paths))


def test_verification_gap_ignores_docs_only_turns():
    # A README edit with no test is not a verification gap; only code turns count.
    turns = [_turn(i, prompt=f"doc {i}") for i in range(20)]
    sha_paths = _paths(turns, lambda i: ["README.md"] + (["tests/test_app.py"] if i == 0 else []))
    assert "verification-gap" not in _by_key(build_insights(turns, sha_paths=sha_paths))


def test_wide_turns_detected():
    turns = [_turn(i, prompt=f"work {i}") for i in range(20)]
    sha_paths = _paths(turns, lambda i: [f"f{i}_{n}.py" for n in range(12 if i % 2 == 0 else 2)])
    insight = _by_key(build_insights(turns, sha_paths=sha_paths)).get("wide-turns")
    assert insight is not None
    assert "more than 8 files" in insight["evidence"][0]


def test_narrow_turns_stay_silent():
    turns = [_turn(i, prompt=f"work {i}") for i in range(20)]
    sha_paths = _paths(turns, lambda i: [f"f{i}.py", f"tests/test_f{i}.py"])
    assert "wide-turns" not in _by_key(build_insights(turns, sha_paths=sha_paths))


def _timed(index, minutes, **kw):
    stat = _turn(index, **kw)
    stat.started_at = "2026-01-01T00:00:00Z"
    total = minutes * 60
    stat.ended_at = f"2026-01-01T{total // 3600:02d}:{(total % 3600) // 60:02d}:00Z"
    return stat


def test_slow_turns_detected():
    turns = [_timed(i, 45 if i % 3 == 0 else 5, prompt=f"work {i}") for i in range(20)]
    insight = _by_key(build_insights(turns)).get("slow-turns")
    assert insight is not None
    assert "over 30 minutes" in insight["evidence"][0]


def test_fast_turns_stay_silent():
    turns = [_timed(i, 5, prompt=f"work {i}") for i in range(20)]
    assert "slow-turns" not in _by_key(build_insights(turns))


def test_slow_turns_waiting_on_background_tasks_are_not_flagged():
    # A long turn spent WAITING on a background task (build/test/sub-agent) is idle time the user
    # couldn't have steered — it must not read as a long feedback loop. Here every slow turn is a
    # background-task turn, so nothing steerable is slow and the category stays silent.
    turns = []
    for i in range(20):
        if i % 3 == 0:
            turns.append(_timed(i, 45, prompt="(background task completed)"))  # slow, but a wait
        else:
            turns.append(_timed(i, 5, prompt=f"work {i}"))  # fast, steerable
    assert "slow-turns" not in _by_key(build_insights(turns))


def test_slow_turns_notes_excluded_background_waits_alongside_real_ones():
    # Genuine long autonomous turns still fire, and the card notes how many long turns were set
    # aside as background-task waits so the number is transparent rather than silently different.
    turns = [_timed(i, 45 if i % 3 == 0 else 5, prompt=f"work {i}") for i in range(20)]  # real slow turns
    turns += [_timed(500 + i, 50, prompt="(background task completed)") for i in range(3)]  # excluded waits
    insight = _by_key(build_insights(turns)).get("slow-turns")
    assert insight is not None
    assert any("background-task waits" in line for line in insight["evidence"])


def test_turns_without_timestamps_yield_no_duration_category():
    turns = [_turn(i, prompt=f"work {i}") for i in range(20)]  # no started_at/ended_at
    assert "slow-turns" not in _by_key(build_insights(turns))


def test_implausible_turn_durations_are_dropped_not_reported():
    # A resumed conversation stamps its whole span on a commit, producing multi-day "turns".
    # Those are metadata artifacts: they must not create a slow-turns finding on their own,
    # nor appear in the evidence (a card claiming a 266-hour turn discredits the whole page).
    from agitrack.metrics.insights import _duration_seconds

    fast = [_timed(i, 5, prompt=f"work {i}") for i in range(20)]
    artifact = _turn(999, prompt="resumed conversation")
    artifact.started_at, artifact.ended_at = "2026-01-01T00:00:00Z", "2026-01-12T00:00:00Z"  # 264h
    assert _duration_seconds(artifact) is None

    insights = _by_key(build_insights([*fast, artifact]))
    assert "slow-turns" not in insights  # 20 fast turns + 1 artifact is not a slow-turn problem


def test_test_path_detection():
    from agitrack.metrics.insights import _is_code_path, _is_test_path

    for path in ("tests/test_app.py", "src/app_test.go", "web/app.spec.ts", "__tests__/x.js", "test_x.py"):
        assert _is_test_path(path), path
    for path in ("src/app.py", "README.md", "protest/main.py"):
        assert not _is_test_path(path), path
    assert _is_code_path("src/app.py") and not _is_code_path("tests/test_app.py")
    assert not _is_code_path("README.md")


def test_context_from_browser_restricts_to_the_given_stats():
    import types

    from agitrack.metrics.insights import context_from_browser

    change = lambda sha, ts: types.SimpleNamespace(sha=sha, timestamp=ts, insertions=3, deletions=1)  # noqa: E731
    browser = types.SimpleNamespace(
        index={"a.py": types.SimpleNamespace(changes=[change("keep", 10), change("drop", 20)])}
    )
    kept = _turn(0)
    kept.sha = "keep"

    files, sha_paths = context_from_browser(browser, [kept])

    assert files == {"a.py": [(10, 3, 1)]}
    assert sha_paths == {"keep": {"a.py"}}
