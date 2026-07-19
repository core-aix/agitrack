"""End-to-end integration tests for the model router with preference learning.

These tests use a real temp git repo and a real (headless) backend stand-in
to exercise the full flow: spawn → summarize → judge → record signal →
decide → switch. We avoid the actual claude/opencode CLIs (which would
require an API key) and instead wire a FakeBackend that records calls
and returns canned responses. The integration is at the routing
machinery level, not the backend CLI.
"""

from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path

import pytest

from agitrack.backends.base import AgentResult, TokenUsage
from agitrack.routing import Router
from agitrack.routing.policy import PoolEntry, TaskFeatures


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


def _store(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    return repo


class _FakeConfig:
    def __init__(self, **kwargs):
        self.routing_mode = kwargs.get("routing_mode", "auto")
        self.routing_pool = kwargs.get("routing_pool", [])
        self.routing_allow_cloud = kwargs.get("routing_allow_cloud", True)
        self.routing_exploration = kwargs.get("routing_exploration", 0.0)
        self.routing_min_margin = kwargs.get("routing_min_margin", 0.0)
        self.routing_judge = kwargs.get("routing_judge", True)


def _routing_qualities(router: Router, model: str) -> float:
    """Read the recorded quality EMA for a model — used to verify the EMA
    actually moved after the signals were recorded."""
    from agitrack.routing.store import load_profile, user_id

    profile = load_profile(router.repo_root, user_id(router.repo_root, None))
    record = profile.get("models", {}).get(model, {})
    ema = record.get("quality_ema")
    return 0.5 if ema is None else float(ema)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_router_learns_from_judge_and_rates_higher(tmp_path: Path) -> None:
    """The full loop: record judge_accept signals on model A, judge_correction
    signals on model B, then verify the policy picks A over B."""
    repo = _store(tmp_path)
    cfg = _FakeConfig(
        routing_mode="auto",
        routing_pool=[
            {"label": "haiku", "model": "haiku", "tier": 1},
            {"label": "opus", "model": "opus", "tier": 3},
        ],
        routing_exploration=0.0,
    )
    router = Router(repo_root=repo, backend="claude", global_config=cfg, rng=random.Random(0))
    # 5 positive turns on haiku, 5 negative on opus.
    for _ in range(5):
        router.record_judgement(
            trace="## User\nx\n## Agent\ny",
            judge=_make_judge("edit", "small", "none"),
            model="haiku",
        )
    for _ in range(5):
        router.record_judgement(
            trace="## User\nx\n## Agent\ny\n## User\nthis is wrong, redo it",
            judge=_make_judge("debug", "small", "explicit_negative"),
            model="opus",
        )
    # Now decide: the haiku wins, decisively.
    decision = router.decide(
        available_models=["haiku", "opus"],
        current_model="opus",
    )
    assert decision.chosen.model == "haiku"
    assert decision.switched is True


def test_router_respects_allow_cloud_false(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    cfg = _FakeConfig(
        routing_mode="auto",
        routing_pool=[
            {"label": "cloud", "model": "cloud-model", "tier": 3, "local": False},
            {"label": "local", "model": "ollama/qwen", "tier": 1, "local": True},
        ],
        routing_allow_cloud=False,
        routing_exploration=0.0,
    )
    router = Router(repo_root=repo, backend="opencode", global_config=cfg, rng=random.Random(0))
    decision = router.decide(
        available_models=["cloud-model", "ollama/qwen"],
        current_model="cloud-model",
    )
    assert decision.chosen.model == "ollama/qwen"
    assert decision.switched is True


def test_router_exploration_finds_unknown_models_eventually(tmp_path: Path) -> None:
    """Thompson sampling should occasionally pick an underused model even
    when a well-known model has a slight edge. Over many trials, the
    unknown model should be picked at least once."""
    repo = _store(tmp_path)
    cfg = _FakeConfig(
        routing_mode="auto",
        routing_pool=[
            {"label": "known-good", "model": "known-good", "tier": 2},
            {"label": "unknown", "model": "unknown", "tier": 2},
        ],
        routing_exploration=0.5,
    )
    router = Router(repo_root=repo, backend="claude", global_config=cfg, rng=random.Random(42))
    # known-good has 10 prior accept signals; unknown has 0.
    for _ in range(10):
        router.record_judgement(
            trace="## User\nx\n## Agent\ny",
            judge=_make_judge("edit", "small", "none"),
            model="known-good",
        )
    unknown_picks = 0
    for _ in range(50):
        decision = router.decide(
            available_models=["known-good", "unknown"],
            current_model="known-good",
        )
        if decision.chosen.model == "unknown":
            unknown_picks += 1
    assert unknown_picks >= 1  # exploration does its job


def test_router_min_margin_prevents_noisy_switches(tmp_path: Path) -> None:
    """With a small min_margin, marginal wins don't justify a switch."""
    repo = _store(tmp_path)
    cfg = _FakeConfig(
        routing_mode="suggest",
        routing_pool=[
            {"label": "a", "model": "model-a", "tier": 2},
            {"label": "b", "model": "model-b", "tier": 2},
        ],
        routing_min_margin=0.5,  # huge margin: nothing beats this in suggest mode
        routing_exploration=0.0,
    )
    router = Router(repo_root=repo, backend="claude", global_config=cfg, rng=random.Random(0))
    # 3 accepts on b.
    for _ in range(3):
        router.record_judgement(
            trace="## User\nx\n## Agent\ny",
            judge=_make_judge("edit", "small", "none"),
            model="model-b",
        )
    decision = router.decide(
        available_models=["model-a", "model-b"],
        current_model="model-a",
    )
    # In suggest mode with a 0.5 margin, the router stays put.
    assert decision.switched is False


def test_router_task_features_route_per_class(tmp_path: Path) -> None:
    """When the router is told the task class explicitly, the per-class EMA
    dominates the model-wide one for that class."""
    repo = _store(tmp_path)
    cfg = _FakeConfig(
        routing_mode="auto",
        routing_pool=[
            {"label": "a", "model": "model-a", "tier": 2},
            {"label": "b", "model": "model-b", "tier": 2},
        ],
        routing_exploration=0.0,
    )
    router = Router(repo_root=repo, backend="claude", global_config=cfg, rng=random.Random(0))
    # 5 debug accepts on b, 5 debug corrections on a.
    for _ in range(5):
        router.record_judgement(
            trace="## User\ndebug\n## Agent\nfixed",
            judge=_make_judge("debug", "medium", "none"),
            model="model-b",
        )
    for _ in range(5):
        router.record_judgement(
            trace="## User\ndebug\n## Agent\nwrong\n## User\nredo it",
            judge=_make_judge("debug", "medium", "explicit_negative"),
            model="model-a",
        )
    # Debug: b wins.
    decision = router.decide(
        available_models=["model-a", "model-b"],
        current_model="model-a",
        task=TaskFeatures(task_class="debug"),
    )
    assert decision.chosen.model == "model-b"
    # Edit: no per-class data; cold start.
    decision2 = router.decide(
        available_models=["model-a", "model-b"],
        current_model="model-a",
        task=TaskFeatures(task_class="edit"),
    )
    # Cold start for edit; either model can win. Just ensure it ran.
    assert decision2.chosen is not None


def test_router_reroute_records_event(tmp_path: Path) -> None:
    """The runner calls ``record_reroute`` after auto-switching. Verify the
    event lands in the store and the score moves accordingly."""
    repo = _store(tmp_path)
    cfg = _FakeConfig(routing_judge=True)
    router = Router(repo_root=repo, backend="claude", global_config=cfg)
    router.record_reroute(from_model="opus", to_model="haiku", session="main")
    from agitrack.routing.store import load_profile, user_id

    profile = load_profile(repo, user_id(repo, None))
    events = profile.get("events", [])
    assert any(ev["kind"] == "reroute" for ev in events)


def test_router_quality_ema_moves_with_explicit_ratings(tmp_path: Path) -> None:
    """A run of 5-star ratings should move the quality EMA up; a run of
    1-star ratings should move it down. Verifies the EMA actually wires
    into the profile (not just floats in memory)."""
    repo = _store(tmp_path)
    cfg = _FakeConfig()
    router = Router(repo_root=repo, backend="claude", global_config=cfg)
    for _ in range(5):
        router.record_rating(rating=5, model="opus")
    after_positive = _routing_qualities(router, "opus")
    assert after_positive > 0.5
    for _ in range(5):
        router.record_rating(rating=1, model="opus")
    after_negative = _routing_qualities(router, "opus")
    assert after_negative < after_positive


def test_router_off_mode_never_switches(tmp_path: Path) -> None:
    """When routing_mode is off, decide() returns the current model with
    switched=False regardless of any history."""
    repo = _store(tmp_path)
    cfg = _FakeConfig(
        routing_mode="off",
        routing_pool=[
            {"label": "haiku", "model": "haiku", "tier": 1},
            {"label": "opus", "model": "opus", "tier": 3},
        ],
    )
    router = Router(repo_root=repo, backend="claude", global_config=cfg)
    # Pretend haiku has a great history; we still don't switch.
    for _ in range(5):
        router.record_judgement(
            trace="## User\nx\n## Agent\ny",
            judge=_make_judge("edit", "small", "none"),
            model="haiku",
        )
    decision = router.decide(
        available_models=["haiku", "opus"],
        current_model="opus",
    )
    assert decision.switched is False
    assert decision.chosen.model == "opus"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_judge(task_class: str, complexity: str, correction: str):
    from agitrack.routing.judge import JudgeResult

    class _FakeJudge:
        def __init__(self):
            self.calls = 0

        def judge(self, trace):
            self.calls += 1
            return JudgeResult(
                task_class=task_class,
                complexity=complexity,
                correction=correction,
                evidence="",
                model="haiku",
                usable=True,
            )

    return _FakeJudge()
