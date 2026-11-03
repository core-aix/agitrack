"""The dashboard's indicative API cost (agitrack/metrics/pricing.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agitrack.commits import build_agent_commit_message
from agitrack.git import GitRepo
from agitrack.metrics import build_dashboard, pricing
from agitrack.metrics.collect import CommitStat
from agitrack.metrics.price_table import ADDED, PRICES
from agitrack.metrics.render import format_dashboard
from agitrack.metrics.web import log_page


def test_a_claude_id_is_found_with_its_date_or_context_suffix():
    plain = pricing.rates_for("claude", "claude-haiku-4-5")
    assert plain is not None and plain.input == 1.0
    assert pricing.rates_for("claude", "claude-haiku-4-5-20251001") == plain
    assert pricing.rates_for("claude", "claude-opus-4-8[1m]") == pricing.rates_for("claude", "claude-opus-4-8")
    assert plain.estimated_from is None


def test_each_backend_names_its_provider():
    assert pricing.rates_for("codex", "gpt-5.4") is not None  # bare ids under openai
    assert pricing.rates_for("opencode", "openai/gpt-5.4") == pricing.rates_for("codex", "gpt-5.4")
    assert pricing.rates_for("opencode", "anthropic/claude-opus-5-5") == pricing.rates_for("claude", "claude-opus-5-5")


def test_a_local_or_unknown_model_has_no_price():
    assert pricing.rates_for("opencode", "ollama/qwen3-coder-next") is None
    assert pricing.rates_for("opencode", "lmstudio/my-model") is None
    assert pricing.rates_for("claude", None) is None
    assert pricing.rates_for("opencode", "gpt-5.4") is None  # OpenCode always names a provider


def test_a_free_model_is_treated_as_unpriced_not_as_zero():
    free = [name for name in PRICES.get("opencode", {}) if name.endswith("-free")]
    assert not free, "the generator must drop $0 rows"
    assert pricing._rates((0.0, 0.0, 0.0, None, None)) is None


def test_a_model_newer_than_the_reference_borrows_its_familys_newest_price():
    newest_opus = pricing.rates_for("claude", "claude-opus-5-5")
    future = pricing.rates_for("claude", "claude-opus-6-1")
    assert future is not None
    assert (future.input, future.output, future.cache_read) == (
        newest_opus.input,
        newest_opus.output,
        newest_opus.cache_read,
    )
    assert future.estimated_from == "claude-opus-5-5"
    # Not the newest of ANOTHER family (Fable costs more than Opus).
    assert future.input != pricing.rates_for("claude", "claude-fable-5-1").input
    # Within a family, the newest model not newer than the request: 4.9 sits between 4.8 and 5.
    assert pricing.rates_for("claude", "claude-opus-4-9").estimated_from == "claude-opus-4-8"
    # The same rule for other providers' naming.
    sol = pricing.rates_for("codex", "gpt-6.9-sol")
    assert sol is not None and sol.estimated_from == "gpt-6.1-sol"


def test_an_older_unknown_model_is_not_guessed_at():
    assert pricing.rates_for("claude", "claude-opus-3") is None


def test_each_token_kind_is_priced_at_its_own_rate():
    rates = pricing.Rates(input=5.0, output=25.0, cache_read=0.5, cache_write=6.25)
    tokens = {
        "input": 1_000_000,  # includes the 200,000 cache-write tokens (aGiTrack's convention)
        "cache_write": 200_000,
        "cache_read": 1_000_000,
        "output": 1_000_000,
        "subagent_output": 1_000_000,
    }
    # 800k uncached x 5 + 200k x 6.25 + 1M x 0.5 + 2M x 25
    assert pricing.agent_cost(rates, tokens) == pytest.approx(4.0 + 1.25 + 0.5 + 50.0)


def test_missing_cache_rates_fall_back_to_input_and_reasoning_to_output():
    rates = pricing.Rates(input=2.0, output=10.0)
    tokens = {"input": 1_000_000, "cache_read": 1_000_000, "reasoning": 1_000_000}
    assert pricing.agent_cost(rates, tokens) == pytest.approx(2.0 + 2.0 + 10.0)


def test_a_squash_prices_each_part_at_its_own_model():
    opus = CommitStat(
        sha="",
        author="",
        email="",
        subject="",
        kind="agent",
        backend="claude",
        model="claude-opus-4-8",
        tokens={"output": 1_000_000},
    )
    local = CommitStat(
        sha="",
        author="",
        email="",
        subject="",
        kind="agent",
        backend="opencode",
        model="ollama/x",
        tokens={"output": 1_000_000},
    )
    haiku = CommitStat(
        sha="",
        author="",
        email="",
        subject="",
        kind="agent",
        backend="claude",
        model="claude-haiku-4-5",
        tokens={"output": 1_000_000},
    )
    squash = CommitStat(sha="s", author="", email="", subject="", kind="agent", constituents=[opus, local, haiku])
    cost = pricing.commit_cost(squash)
    assert cost.total == pytest.approx(25.0 + 5.0)
    assert cost.priced and cost.unpriced
    assert (
        pricing.cost_payload(CommitStat(sha="x", author="", email="", subject="", kind="agent", constituents=[local]))
        is None
    )


def test_the_summary_call_is_priced_at_the_summary_model():
    stat = CommitStat(
        sha="x",
        author="",
        email="",
        subject="",
        kind="agent",
        backend="claude",
        model="claude-opus-4-8",
        tokens={"summary_input": 1_000_000, "summary_output": 1_000_000},
        metadata={"summary_model": "claude-haiku-4-5-20251001"},
    )
    cost = pricing.commit_cost(stat)
    assert cost.agent == 0 and cost.summarizer == pytest.approx(1.0 + 5.0)


def _commit(repo: GitRepo, name: str, *, backend: str, model: str, tokens: dict) -> None:
    (repo.repo / name).write_text("x\n", encoding="utf-8")
    repo.stage_paths([name])
    repo.commit(
        build_agent_commit_message(
            latest_prompt=f"write {name}",
            trace=[{"role": "user", "content": f"write {name}"}, {"role": "agent", "content": "done"}],
            backend=backend,
            backend_session_id="ses-1",
            agitrack_session_id="agit-1",
            model=model,
            token_usage=tokens,
        )
    )


def test_the_dashboard_shows_a_cost_only_where_a_price_is_known(tmp_path: Path):
    repo = GitRepo.init(tmp_path)
    tokens = {
        "context": 1,
        "total": 1,
        "input": 1_000_000,
        "output": 100_000,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
    }
    _commit(repo, "a.txt", backend="claude", model="claude-opus-4-8", tokens=tokens)
    _commit(repo, "b.txt", backend="opencode", model="ollama/qwen3-coder-next", tokens=tokens)

    dash = build_dashboard(repo)
    summary = dash.cost_summary
    assert summary["priced"] == 1 and summary["unpriced"] == 1
    assert summary["total"] == pytest.approx(5.0 + 2.5)  # 1M input x $5 + 100k output x $25
    assert summary["reference"]["added"] == ADDED

    costs = {entry["eff_model"]: entry["cost"] for entry in log_page(dash)["entries"]}
    assert costs["claude-opus-4-8"] == pytest.approx(7.5)
    assert costs["ollama/qwen3-coder-next"] is None
    assert dash.by_model["claude-opus-4-8"]["cost"] == pytest.approx(7.5)
    assert "cost" not in dash.by_model["ollama/qwen3-coder-next"]

    text = format_dashboard(dash)
    assert "$7.50" in text and ADDED in text


def test_a_repo_with_only_local_models_shows_no_cost_at_all(tmp_path: Path):
    repo = GitRepo.init(tmp_path)
    tokens = {"context": 1, "total": 1, "input": 10, "output": 10, "reasoning": 0, "cache_read": 0, "cache_write": 0}
    _commit(repo, "a.txt", backend="opencode", model="ollama/qwen3-coder-next", tokens=tokens)
    dash = build_dashboard(repo)
    assert dash.cost_summary["priced"] == 0 and dash.cost_summary["total"] is None
    assert "Cost (" not in format_dashboard(dash)
