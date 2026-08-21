"""The dashboard's advanced filter: selecting on ANY field the commits record.

The named filters (committer/backend/model/date) answer the questions asked most and nothing
else, while every commit already carries a full metadata block — harness version, OS, session
name, compactions, token counts. Before this, "show me only what Claude Code 2.1.238 produced"
or "only the commits from the Ubuntu box" meant reading `git log` by hand.

The field list is built from the commits in view rather than hard-coded, so a field aGiTrack
adds later is filterable the day it lands. That makes the parsing rules load-bearing: a commit
body can QUOTE a metadata block (a GitHub merge copies the whole PR body) and carries git
trailers, and neither may become a filter field.
"""

from __future__ import annotations

from agitrack.metrics.collect import CommitStat, Dashboard, filterable_metadata
from agitrack.metrics.web import _filter_stats, metadata_filter_fields, parse_meta_filters


def _stat(sha: str, **metadata) -> CommitStat:
    return CommitStat(
        sha=sha,
        author="dev",
        email="dev@example.com",
        subject="s",
        kind="agent",
        timestamp=1_700_000_000,
        metadata=dict(metadata),
    )


def _dash(*stats: CommitStat) -> Dashboard:
    return Dashboard(repo="r", branch="main", stats=list(stats))


def _filtered(dash: Dashboard, raw: list[str]) -> list[str]:
    stats = _filter_stats(dash, author="", backend="", model="", frm=0, to=0, meta=parse_meta_filters(raw))
    return [s.sha for s in stats]


# --------------------------------------------------------------------------- parsing


def test_a_condition_is_key_op_value_and_the_value_may_contain_colons():
    # An ISO timestamp and an `mcp__server__tool` name both carry colons, so the split is
    # bounded — a greedy split would truncate the value and silently match nothing.
    assert parse_meta_filters(["agent_started_at:ge:2026-08-01T00:00:00Z"]) == [
        ("agent_started_at", "ge", "2026-08-01T00:00:00Z")
    ]


def test_a_malformed_or_unknown_condition_is_dropped_not_guessed_at():
    # The filter drives every panel on the page. A condition that cannot be honoured exactly
    # must not be relaxed into one that can — that would misreport the whole slice.
    assert parse_meta_filters(["nope", "key:bogus:v", ":eq:v", "key:eq:"]) == []


# --------------------------------------------------------------------------- matching


def test_eq_is_exact_and_has_is_case_insensitive_substring():
    dash = _dash(_stat("a", system="macOS 15.7.3"), _stat("b", system="Ubuntu 24.04"))
    assert _filtered(dash, ["system:eq:macOS 15.7.3"]) == ["a"]
    assert _filtered(dash, ["system:eq:macOS"]) == []  # exact means exact
    assert _filtered(dash, ["system:has:macos"]) == ["a"]


def test_numeric_bounds_compare_as_numbers_not_strings():
    # "9000" > "10000" as strings. A token-count filter that sorted lexically would be worse
    # than useless — it would look like it worked.
    dash = _dash(_stat("a", context_tokens="9000"), _stat("b", context_tokens="10000"))
    assert _filtered(dash, ["context_tokens:ge:10000"]) == ["b"]
    assert _filtered(dash, ["context_tokens:le:9000"]) == ["a"]


def test_a_bound_on_a_non_numeric_value_matches_nothing_rather_than_raising():
    dash = _dash(_stat("a", session_name="beacon"))
    assert _filtered(dash, ["session_name:ge:5"]) == []


def test_a_commit_missing_the_field_never_matches():
    # Absent is not "matches anything": filtering by OS must not sweep in commits that never
    # recorded one.
    dash = _dash(_stat("a", system="macOS 15.7.3"), _stat("b"))
    assert _filtered(dash, ["system:has:o"]) == ["a"]


# --------------------------------------------------------------------------- combining


def test_conditions_on_different_fields_all_have_to_match():
    dash = _dash(
        _stat("a", system="macOS 15.7.3", backend_version="2.1.238"),
        _stat("b", system="macOS 15.7.3", backend_version="2.1.236"),
        _stat("c", system="Ubuntu 24.04", backend_version="2.1.238"),
    )
    assert _filtered(dash, ["system:eq:macOS 15.7.3", "backend_version:eq:2.1.238"]) == ["a"]


def test_several_values_of_the_SAME_field_match_any_of_them():
    # This is what makes it usable as a facet. ANDing them would make two values of one field
    # select nothing at all, which reads as the filter being broken rather than as a choice.
    dash = _dash(_stat("a", system="macOS 15.7.3"), _stat("b", system="Ubuntu 24.04"), _stat("c", system="Windows 11"))
    assert _filtered(dash, ["system:eq:macOS 15.7.3", "system:eq:Ubuntu 24.04"]) == ["a", "b"]


def test_no_conditions_changes_nothing():
    dash = _dash(_stat("a", system="macOS 15.7.3"), _stat("b"))
    assert _filtered(dash, []) == ["a", "b"]


# --------------------------------------------------------------------------- offered fields


def test_only_well_formed_metadata_keys_are_offered():
    # A quoted metadata block and git trailers ride in the same `key: value` shape. Without the
    # key-shape rule the field list filled up with `Signed-off-by`, dependabot's `update-type`
    # and fragments of prose like `deps(vscode)` and `(Claude)`.
    kept = filterable_metadata(
        {
            "system": "macOS 15.7.3",
            "backend_version": "2.1.238",
            "Signed-off-by": "someone <a@b.c>",
            "update-type": "version-update:semver-patch",
            "deps(vscode)": "bump brace-expansion",
            "(Claude)": "** textbook diff-cache staleness",
            "empty": "",
        }
    )
    assert kept == {"system": "macOS 15.7.3", "backend_version": "2.1.238"}


def test_fields_carry_their_values_and_whether_they_are_numeric():
    dash = _dash(
        _stat("a", system="macOS 15.7.3", context_tokens="9000"),
        _stat("b", system="Ubuntu 24.04", context_tokens="10000"),
    )
    fields = {f["key"]: f for f in metadata_filter_fields(dash)}
    assert fields["system"]["values"] == ["Ubuntu 24.04", "macOS 15.7.3"] and not fields["system"]["numeric"]
    # Numeric fields sort numerically, so a value list reads in order rather than lexically.
    assert fields["context_tokens"]["numeric"] and fields["context_tokens"]["values"] == ["9000", "10000"]


def test_a_high_cardinality_field_is_offered_without_shipping_every_value():
    # A select holding 500 conversation anchors is a wall, not a control — the page offers a
    # text box instead, so the field stays filterable and the payload stays small.
    dash = _dash(*[_stat(f"s{i}", conversation_anchor=f"msg_{i}") for i in range(150)])
    field = next(f for f in metadata_filter_fields(dash) if f["key"] == "conversation_anchor")
    assert field["count"] == 150 and field["values"] == []
    # …and it still filters.
    assert _filtered(dash, ["conversation_anchor:eq:msg_7"]) == ["s7"]


# --------------------------------------------------------------------------- ranges


def test_bounds_order_versions_numbers_and_timestamps_naturally():
    # The three things worth bounding cannot share one comparison rule: plain string order puts
    # 2.1.9 AFTER 2.1.10, and plain numeric order cannot read a timestamp at all. `ge`/`le`
    # parsed both sides as floats before this, so a date or version bound matched NOTHING and
    # read as "no commits" rather than as a filter that could not run.
    dash = _dash(
        _stat("old", backend_version="2.1.9", agent_started_at="2026-07-30T09:00:00Z", context_tokens="9000"),
        _stat("new", backend_version="2.1.10", agent_started_at="2026-08-15T09:00:00Z", context_tokens="10000"),
    )
    assert _filtered(dash, ["backend_version:ge:2.1.10"]) == ["new"]
    assert _filtered(dash, ["agent_started_at:ge:2026-08-01T00:00:00Z"]) == ["new"]
    assert _filtered(dash, ["context_tokens:ge:10000"]) == ["new"]


def test_between_is_inclusive_of_both_ends():
    dash = _dash(
        _stat("a", context_tokens="50000"),
        _stat("b", context_tokens="100000"),
        _stat("c", context_tokens="300000"),
        _stat("d", context_tokens="500000"),
        _stat("e", context_tokens="900000"),
    )
    assert _filtered(dash, ["context_tokens:between:100000..500000"]) == ["b", "c", "d"]


def test_a_range_end_may_be_left_open():
    # "up to X" and "from X" are both real questions, and a half-filled range should ask the
    # one the reader actually typed rather than nothing at all.
    dash = _dash(_stat("a", context_tokens="50000"), _stat("b", context_tokens="500000"))
    assert _filtered(dash, ["context_tokens:between:..100000"]) == ["a"]
    assert _filtered(dash, ["context_tokens:between:100000.."]) == ["b"]


def test_a_range_over_versions_and_dates_reads_the_same_way():
    dash = _dash(
        _stat("a", backend_version="2.1.220", agent_started_at="2026-07-05T09:00:00Z"),
        _stat("b", backend_version="2.1.236", agent_started_at="2026-08-19T09:00:00Z"),
        _stat("c", backend_version="2.1.238", agent_started_at="2026-08-21T09:00:00Z"),
    )
    assert _filtered(dash, ["backend_version:between:2.1.230..2.1.237"]) == ["b"]
    assert _filtered(dash, ["agent_started_at:between:2026-08-01..2026-08-20"]) == ["b"]


def test_a_between_without_a_separator_matches_nothing_rather_than_becoming_a_bound():
    # Better no rows than silently applying a bound the user did not write.
    dash = _dash(_stat("a", context_tokens="50000"))
    assert _filtered(dash, ["context_tokens:between:50000"]) == []


def test_an_entirely_open_range_selects_every_commit_that_HAS_the_field():
    dash = _dash(_stat("a", context_tokens="50000"), _stat("b"))
    assert _filtered(dash, ["context_tokens:between:.."]) == ["a"]
