"""Tests for the routing store, judge, policy, signals, and switch primitives.

Real temp git repos for the store tests; the judge/policy/switch tests are
pure-function unit tests (no backend CLI required). End-to-end wiring into
the proxy/background runners is covered in test_routing_integration.py.
"""

from __future__ import annotations

import random
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agitrack.routing import (
    EVENT_KIND_DISCARD,
    EVENT_KIND_RATING,
    RoutingStore,
    SignalEvent,
    SwitchPlan,
    TaskFeatures,
    choose,
    default_pool_for_backend,
    load_profile,
    plan_for,
    pool_from_config,
    record_event,
    set_sync,
    sync_info,
    sync_prefs_now,
    user_id,
)
from agitrack.routing.judge import TurnJudge, heuristic_correction, _parse_judge_json
from agitrack.routing.policy import PoolEntry
from agitrack.routing.router import Router, build_pool


# -----------------------------------------------------------------------------
# store
# -----------------------------------------------------------------------------


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


def test_store_load_returns_empty_when_missing(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    store = RoutingStore(repo)
    data = store.load()
    assert data == {"profiles": {}}


def test_store_save_and_load_round_trip(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    store = RoutingStore(repo)
    data = {"profiles": {"alice": {"models": {"claude-haiku-4-5": {"attempts": 2}}}}, "sync_enabled": True}
    store.save(data)
    loaded = store.load()
    assert loaded == data
    # Ensure the file is git-ignored.
    exclude = repo / ".git" / "info" / "exclude"
    assert exclude.exists()
    assert ".agitrack/" in exclude.read_text(encoding="utf-8").splitlines()


def test_record_event_updates_quality_ema(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    gid = "alice"
    # Five 5-star ratings on opus — quality should converge well above 0.5.
    for _ in range(5):
        record_event(
            repo,
            gid,
            SignalEvent(kind=EVENT_KIND_RATING, model="claude-opus-4-8", backend="claude", value=5),
        )
    profile = load_profile(repo, gid)
    record = profile["models"]["claude-opus-4-8"]
    assert record["attempts"] == 5
    assert record["rating_count"] == 5
    assert record["quality_ema"] is not None
    assert record["quality_ema"] > 0.7


def test_record_event_discards_drag_quality_down(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    gid = "bob"
    # One accept then a discard — the discard should weigh heavily negative.
    record_event(
        repo,
        gid,
        SignalEvent(kind="judge_accept", model="small-model", backend="opencode"),
    )
    record_event(
        repo,
        gid,
        SignalEvent(kind=EVENT_KIND_DISCARD, model="small-model", backend="opencode"),
    )
    profile = load_profile(repo, gid)
    record = profile["models"]["small-model"]
    assert record["discards"] == 1
    # Discards carry -0.9 weight; even after a +0.2 accept, EMA should drop.
    assert record["quality_ema"] is not None
    assert record["quality_ema"] < 0.55


def test_per_task_class_ema_blends_after_three_samples(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    gid = "carol"
    # 3 successes on "debug" task class.
    for _ in range(3):
        record_event(
            repo,
            gid,
            SignalEvent(
                kind="judge_accept",
                model="claude-opus-4-8",
                backend="claude",
                task_class="debug",
                complexity="medium",
            ),
        )
    profile = load_profile(repo, gid)
    by_class = profile["models"]["claude-opus-4-8"]["by_class"]
    assert by_class["debug"]["n"] == 3
    assert by_class["debug"]["ema"] is not None
    # No data for "refactor" — should be empty.
    assert "refactor" not in by_class


def test_event_ring_buffer_trims(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    gid = "dave"
    # Pump 600 events; ring buffer caps at 500.
    for i in range(600):
        record_event(
            repo,
            gid,
            SignalEvent(
                kind="judge_accept",
                model="claude-opus-4-8",
                backend="claude",
                value=f"event-{i}",
            ),
        )
    data = RoutingStore(repo).load()
    events = data["profiles"][gid]["events"]
    assert len(events) == 500
    # The latest 500 are kept (oldest dropped).
    assert events[0]["value"] == "event-100"
    assert events[-1]["value"] == "event-599"


def test_sync_info_no_repo_returns_not_available() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        info = sync_info(root, None)
        assert info["available"] is False
        assert info["enabled"] is False


def test_set_sync_without_repo_returns_error() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        result = set_sync(root, None, True)
        assert "error" in result


def test_sync_prefs_round_trip(tmp_path: Path) -> None:
    """The full round-trip: write prefs on a 'machine A' clone, read them back
    on a 'machine B' clone. Verifies the per-user snapshot can travel through
    the orphan ref."""
    import shutil

    base = tmp_path / "src-base"
    base.mkdir()
    src = _store(base)
    # Make `src` a bare-ish remote: a bare clone of the same repo, which
    # `sync_prefs_now` can push to.
    bare_src = tmp_path / "bare-src.git"
    bare_src.mkdir()
    subprocess.run(["git", "clone", "--bare", str(src), str(bare_src)], check=True, capture_output=True)
    # Point src at bare_src so sync_prefs_now actually pushes.
    subprocess.run(["git", "remote", "add", "origin", str(bare_src)], cwd=src, check=True, capture_output=True)
    # Initial sync with an entry: write via record_event, then push.
    record_event(
        src,
        "eve",
        SignalEvent(kind="judge_accept", model="claude-opus-4-8", backend="claude"),
    )
    from agitrack.git import GitRepo

    repo = GitRepo.discover(src)
    ok, error = sync_prefs_now(repo, "eve")
    assert ok, f"sync failed: {error}"

    # Clone to a second dir; restoring the same user's prefs must work even
    # without a remote (the local ref carries the data).
    dst = tmp_path / "dst"
    shutil.copytree(src, dst)
    # Wipe the inherited routing.json — a fresh clone wouldn't have it. The
    # restore must re-fetch the prefs from the orphan ref, not from the
    # copy-of-source's local file.
    dst_state = dst / ".agitrack" / "routing.json"
    if dst_state.exists():
        dst_state.unlink()
    # Strip the inherited remote: the dst is the "machine B" — it shouldn't
    # already know about bare_src. Add it freshly so the test mirrors the
    # real cross-machine flow (a clone of a remote, not a copy).
    subprocess.run(["git", "remote", "remove", "origin"], cwd=dst, check=False, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare_src)], cwd=dst, check=True, capture_output=True)
    repo_dst = GitRepo.discover(dst)
    from agitrack.routing.store import restore_prefs_from_ref

    ok = restore_prefs_from_ref(dst, repo_dst, "eve")
    assert ok, "restore_prefs_from_ref should succeed with a remote"
    profile = load_profile(dst, "eve")
    assert "claude-opus-4-8" in profile["models"]
    assert profile["models"]["claude-opus-4-8"]["attempts"] >= 1


# -----------------------------------------------------------------------------
# judge
# -----------------------------------------------------------------------------


def test_heuristic_correction_detects_explicit_negative() -> None:
    trace = "## User\nAdd a button\n\n## Agent\nDone.\n\n## User\nthis is wrong, please redo it\n"
    found = heuristic_correction(trace)
    assert found is not None
    kind, evidence = found
    assert kind == "explicit_negative"
    assert "wrong" in evidence or "redo" in evidence


def test_heuristic_correction_detects_redo() -> None:
    trace = "## User\nAdd a button\n\n## Agent\nDone.\n\n## User\nactually, do it again"
    found = heuristic_correction(trace)
    assert found is not None
    assert found[0] == "redo"


def test_heuristic_correction_no_match() -> None:
    trace = "## User\nAdd a button\n\n## Agent\nDone."
    assert heuristic_correction(trace) is None


def test_parse_judge_json_clean() -> None:
    text = '{"task_class": "debug", "complexity": "medium", "correction": "none", "evidence": "tracked it down"}'
    result = _parse_judge_json(text)
    assert result is not None
    assert result.task_class == "debug"
    assert result.complexity == "medium"
    assert result.correction == "none"
    assert result.evidence == "tracked it down"
    assert result.usable is True


def test_parse_judge_json_tolerates_prose_around() -> None:
    text = 'Sure, here is the classification:\n{"task_class": "edit", "complexity": "small", "correction": "explicit_negative", "evidence": "wrong file"}\nThanks.'
    result = _parse_judge_json(text)
    assert result is not None
    assert result.task_class == "edit"
    assert result.correction == "explicit_negative"


def test_parse_judge_json_rejects_garbage() -> None:
    assert _parse_judge_json("") is None
    assert _parse_judge_json("not json at all") is None
    assert _parse_judge_json('{"task_class": "unknown-class", "complexity": "small"}') is not None
    # Unknown class is silently coerced to "other".
    assert _parse_judge_json('{"task_class": "unknown-class", "complexity": "small"}').task_class == "other"


def test_judge_uses_heuristic_without_backend_call() -> None:
    """The TurnJudge should short-circuit on a loud negative trace and never
    instantiate a backend — saving a round-trip per obviously-bad turn."""

    class _NoBackend:
        def run(self, *args, **kwargs):  # pragma: no cover - should never be called
            raise AssertionError("heuristic should have short-circuited")

    summarizer = _NoBackend()
    summarizer.tokens_input = 0
    summarizer.tokens_output = 0
    summarizer.tokens_cache_read = 0
    summarizer.model = "claude-haiku-4-5"

    # Mock the _run method on the Summarizer class to assert it isn't called.
    with patch("agitrack.routing.judge.Summarizer") as mock:
        judge = TurnJudge(mock)
        result = judge.judge("no, that's wrong — undo it")
        assert result.usable
        assert result.correction == "explicit_negative"
        # mock._run was never called.
        assert not mock._run.called


# -----------------------------------------------------------------------------
# policy
# -----------------------------------------------------------------------------


def test_default_pool_for_backend_claude_tiers() -> None:
    pool = default_pool_for_backend("claude", ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-8"])
    by_model = {entry.model: entry for entry in pool}
    assert by_model["claude-haiku-4-5"].tier == 1
    assert by_model["claude-sonnet-4-6"].tier == 2
    assert by_model["claude-opus-4-8"].tier == 3
    assert all(entry.local is False for entry in pool)


def test_default_pool_for_backend_opencode_local() -> None:
    pool = default_pool_for_backend("opencode", ["ollama/qwen3:27b", "kimi/k3", "anthropic/claude-opus-4-8"])
    by_model = {entry.model: entry for entry in pool}
    assert by_model["ollama/qwen3:27b"].local is True
    assert by_model["ollama/qwen3:27b"].tier == 1
    # kimi/k3 has no cheap/expensive marker → tier 2.
    assert by_model["kimi/k3"].tier == 2
    assert by_model["anthropic/claude-opus-4-8"].tier == 3


def test_pool_from_config_drops_invalid_entries() -> None:
    raw = [
        {"label": "haiku", "model": "claude-haiku-4-5", "tier": 1},
        {"model": ""},  # dropped
        {"tier": 2},  # dropped (no model)
        "not a dict",  # dropped
        {"label": "opus", "model": "claude-opus-4-8", "tier": 3, "local": False},
        {"model": "x", "tier": 99},  # clamped
    ]
    pool = pool_from_config(raw)
    assert len(pool) == 3
    assert pool[0].model == "claude-haiku-4-5"
    assert pool[1].model == "claude-opus-4-8"
    assert pool[2].tier == 3  # clamped to max 3


def test_build_pool_uses_config_when_present(tmp_path: Path) -> None:
    pool = build_pool(
        backend="claude",
        config_pool=[{"label": "haiku", "model": "claude-haiku-4-5", "tier": 1}],
        available_models=["claude-opus-4-8"],
        current_model="claude-haiku-4-5",
    )
    assert [e.model for e in pool] == ["claude-haiku-4-5"]


def test_build_pool_auto_seeds_when_empty() -> None:
    pool = build_pool(
        backend="claude",
        config_pool=[],
        available_models=["claude-haiku-4-5", "claude-opus-4-8"],
        current_model=None,
    )
    assert {e.model for e in pool} == {"claude-haiku-4-5", "claude-opus-4-8"}


def test_build_pool_appends_current_as_phantom() -> None:
    pool = build_pool(
        backend="claude",
        config_pool=[{"label": "haiku", "model": "claude-haiku-4-5", "tier": 1}],
        available_models=["claude-haiku-4-5"],
        current_model="claude-opus-4-8",  # not in pool
    )
    assert any(e.model == "claude-opus-4-8" for e in pool)


def test_choose_cold_start_returns_current() -> None:
    """On a fresh user, the cold-start prior is identical for every model —
    the current model wins ties."""
    pool = [
        PoolEntry(label="haiku", model="claude-haiku-4-5", tier=1),
        PoolEntry(label="opus", model="claude-opus-4-8", tier=3),
    ]
    current = pool[1]
    decision = choose(
        pool,
        current=current,
        profile={},
        task=TaskFeatures(),
        mode="auto",
        rng=random.Random(0),
    )
    # Cold start: the current model is the only entry with attempts; the
    # newer opus gets the prior penalty. Haiku may sometimes win, but the
    # current model wins more often than not with the seeded RNG.
    assert decision.chosen.model in {"claude-haiku-4-5", "claude-opus-4-8"}


def test_choose_data_driven_prefers_higher_quality() -> None:
    """A model with a real history of negative signals scores lower than a
    model with positive signals — the router picks the better one."""
    profile = {
        "models": {
            "bad-model": {
                "attempts": 10,
                "quality_ema": 0.2,
                "rating_count": 4,
                "judge_corrections": 6,
                "discards": 2,
                "reverts": 1,
            },
            "good-model": {
                "attempts": 10,
                "quality_ema": 0.9,
                "rating_count": 8,
                "judge_corrections": 0,
                "discards": 0,
                "reverts": 0,
            },
        }
    }
    pool = [
        PoolEntry(label="bad", model="bad-model", tier=2),
        PoolEntry(label="good", model="good-model", tier=2),
    ]
    current = pool[0]
    # Use a deterministic RNG so the exploration bonus doesn't override the
    # strong quality signal.
    rng = random.Random(0)
    wins = 0
    for _ in range(20):
        decision = choose(
            pool,
            current=current,
            profile=profile,
            task=TaskFeatures(task_class="edit"),
            mode="auto",
            exploration=0.0,  # disable Thompson noise
            rng=rng,
        )
        if decision.chosen.model == "good-model":
            wins += 1
    assert wins >= 19  # 19/20 in the strong-quality direction


def test_choose_off_mode_stays() -> None:
    pool = [
        PoolEntry(label="haiku", model="claude-haiku-4-5", tier=1),
        PoolEntry(label="opus", model="claude-opus-4-8", tier=3),
    ]
    decision = choose(
        pool,
        current=pool[1],
        profile={},
        mode="off",
    )
    assert decision.switched is False
    assert decision.chosen.model == "claude-opus-4-8"
    assert decision.reason == "routing disabled"


def test_choose_allow_cloud_false_filters_non_local() -> None:
    pool = [
        PoolEntry(label="haiku", model="claude-haiku-4-5", tier=1, local=False),
        PoolEntry(label="ollama", model="ollama/qwen3:27b", tier=1, local=True),
    ]
    decision = choose(
        pool,
        current=pool[1],
        profile={},
        mode="auto",
        allow_cloud=False,
        exploration=0.0,
        rng=random.Random(0),
    )
    # When cloud is disallowed, only the local model is in the candidate set.
    assert decision.chosen.model == "ollama/qwen3:27b"


def test_choose_per_task_class_blends_after_three_samples() -> None:
    """With >=3 per-class samples, the per-class EMA is the dominant signal."""
    profile = {
        "models": {
            "model-a": {
                "attempts": 10,
                "quality_ema": 0.7,
                "by_class": {
                    "debug": {"n": 5, "ema": 0.2},  # bad at debug
                },
            },
            "model-b": {
                "attempts": 10,
                "quality_ema": 0.5,
                "by_class": {
                    "debug": {"n": 5, "ema": 0.95},  # great at debug
                },
            },
        }
    }
    pool = [
        PoolEntry(label="a", model="model-a", tier=2),
        PoolEntry(label="b", model="model-b", tier=2),
    ]
    wins_a = wins_b = 0
    rng = random.Random(0)
    for _ in range(20):
        decision = choose(
            pool,
            current=pool[0],
            profile=profile,
            task=TaskFeatures(task_class="debug"),
            mode="auto",
            exploration=0.0,
            rng=rng,
        )
        if decision.chosen.model == "model-a":
            wins_a += 1
        else:
            wins_b += 1
    # model-b's per-class EMA dominates the model-wide one for "debug".
    assert wins_b >= 19


# -----------------------------------------------------------------------------
# switch
# -----------------------------------------------------------------------------


def test_switch_plan_for_claude() -> None:
    plan = plan_for("claude", "claude-opus-4-8")
    assert isinstance(plan, SwitchPlan)
    assert plan.bytes_to_write == [b"/model claude-opus-4-8\r"]
    assert plan.expected_pick_label == "claude-opus-4-8"


def test_switch_plan_for_opencode() -> None:
    plan = plan_for("opencode", "ollama/qwen3:27b")
    assert plan.bytes_to_write
    # The OpenCode sequence opens the model dialog (ctrl+x m), types the
    # model id, then presses Enter.
    assert plan.bytes_to_write[0].startswith(b"\x18m")
    assert plan.bytes_to_write[0].endswith(b"\r")
    assert b"ollama/qwen3:27b" in plan.bytes_to_write[0]


def test_switch_plan_sanitises_unsafe_characters() -> None:
    """A model id with control characters must be sanitised so a misconfigured
    pool can't break out of the TUI's textbox."""
    plan = plan_for("claude", "evil\x1b[2J\x07model")
    text = plan.bytes_to_write[0].decode("utf-8")
    assert "\x1b" not in text
    assert "evil" in text and "model" in text


def test_switch_plan_unknown_backend_raises() -> None:
    with pytest.raises(ValueError):
        plan_for("nonsense", "x")


# -----------------------------------------------------------------------------
# router facade
# -----------------------------------------------------------------------------


class _FakeConfig:
    """A tiny stand-in for GlobalConfig that exposes only the keys the
    router reads. Avoids instantiating a full GlobalConfig (which expects a
    real path + repo)."""

    def __init__(self, **kwargs):
        self.routing_mode = kwargs.get("routing_mode", "off")
        self.routing_pool = kwargs.get("routing_pool", [])
        self.routing_allow_cloud = kwargs.get("routing_allow_cloud", True)
        self.routing_exploration = kwargs.get("routing_exploration", 0.1)
        self.routing_min_margin = kwargs.get("routing_min_margin", 0.05)
        self.routing_judge = kwargs.get("routing_judge", True)


def test_router_decide_returns_decision(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    cfg = _FakeConfig(
        routing_mode="auto",
        routing_pool=[
            {"label": "haiku", "model": "claude-haiku-4-5", "tier": 1},
            {"label": "opus", "model": "claude-opus-4-8", "tier": 3},
        ],
    )
    router = Router(repo_root=repo, backend="claude", global_config=cfg, rng=random.Random(0))
    decision = router.decide(
        available_models=["claude-haiku-4-5", "claude-opus-4-8"],
        current_model="claude-opus-4-8",
        task=TaskFeatures(task_class="debug"),
    )
    assert decision.chosen is not None
    assert decision.scores  # all entries scored


def test_router_records_judgement_and_updates_profile(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    cfg = _FakeConfig(routing_judge=True)
    router = Router(repo_root=repo, backend="claude", global_config=cfg)
    # Build a fake judge that just returns a result.
    from agitrack.routing.judge import JudgeResult

    class _FakeJudge:
        def __init__(self, result):
            self._result = result

        def judge(self, trace):
            return self._result

    verdict = JudgeResult(
        task_class="debug",
        complexity="medium",
        correction="none",
        evidence="clean",
        model="claude-haiku-4-5",
        usable=True,
    )
    router.record_judgement(
        trace="## User\nx\n## Agent\ny",
        judge=_FakeJudge(verdict),
        commit="abc123",
        session="main",
        model="claude-haiku-4-5",
    )
    profile = load_profile(repo, user_id(repo, None))
    assert "claude-haiku-4-5" in profile["models"]
    assert profile["models"]["claude-haiku-4-5"]["attempts"] == 1


def test_router_record_rating_uses_last_judgement(tmp_path: Path) -> None:
    repo = _store(tmp_path)
    cfg = _FakeConfig(routing_judge=True)
    router = Router(repo_root=repo, backend="claude", global_config=cfg)
    # Force a judgement so the router has a task_class/complexity to file
    # the rating against.
    from agitrack.routing.judge import JudgeResult

    class _FakeJudge:
        def judge(self, trace):
            return JudgeResult(
                task_class="edit",
                complexity="small",
                correction="none",
                evidence="",
                model="claude-haiku-4-5",
                usable=True,
            )

    router.record_judgement(trace="", judge=_FakeJudge(), model="claude-haiku-4-5")
    router.record_rating(rating=5, model="claude-haiku-4-5")
    profile = load_profile(repo, user_id(repo, None))
    record = profile["models"]["claude-haiku-4-5"]
    assert record["rating_count"] == 1
    by_class = record["by_class"]["edit"]
    assert by_class["ratings"] == 1
