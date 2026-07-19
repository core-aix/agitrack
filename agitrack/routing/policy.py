"""The model-routing policy.

Picks which coding model aGiTrack should use for the next turn, given:

* a configured pool of candidate models for the active backend
  (``routing_pool`` config key; auto-seeded from the backend's own model list)
* the user's per-(model, task-class) quality scores from the routing store
* a hard rule set: the current model is always in the pool (so the router
  can never strand the user on an uninstalled backend), ``routing_allow_cloud``
  drops cloud-tier entries when set
* a simple Thompson-sampling bonus to keep exploring underused models

The result is a :class:`RoutingDecision` the runner acts on: which model
to use, why, and the score breakdown. The decision is read-only; the
runner mutates the session's model and (in auto mode) optionally injects
the in-TUI model-switch command.

The policy is intentionally a pure function of its inputs: it never reads
git, never talks to the backend, and is fully unit-testable. The runner
provides the inputs (pool, profile, current model) and applies the
output.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Iterable

# The score assigned to a model with NO data — the cold-start prior. Tuned
# so cheap/local models are slightly preferred for cold start (the
# expectation: the user started on the cheap model for a reason, so
# default-elsewhere nudges things toward quality once data arrives, but
# doesn't instantly overturn an established choice).
_PRIOR_SCORE = 0.55
# Per-attempt bonus for exploration (Thompson sampling). The bonus shrinks
# as attempts grow — once a model has 30+ attempts, the bonus is barely
# visible. Tunable via config; 0.1 is the default.
_DEFAULT_EXPLORATION = 0.1
# How aggressively cost penalises the score. Cost tiers (1=cheap, 2=mid,
# 3=expensive) are mapped to [-0.0, -0.15, -0.30] in the final score, so a
# user on Claude Opus pays a 0.3 point penalty vs Sonnet. Configurable per
# pool entry; the default lambda balances cost vs quality.
_COST_LAMBDA = 0.15
# If the BEST model in the pool beats the CURRENT model by less than this
# margin, the router stays on the current model — a small win isn't worth a
# disruptive TUI switch.
_DEFAULT_MIN_MARGIN = 0.05


@dataclass
class PoolEntry:
    """One model in the routing pool. ``tier`` is the relative cost rank
    (1=cheapest, 2=mid, 3=expensive). ``local`` flags a model that's
    served on the user's machine (e.g. an Ollama provider) — used by the
    ``routing_allow_cloud`` filter."""

    label: str  # human label for menus
    model: str  # the model id to pass to the backend
    tier: int = 2
    local: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "model": self.model, "tier": self.tier, "local": self.local}


@dataclass
class TaskFeatures:
    """What the router needs to know about the next turn to make a good
    decision. The runner builds this from the prompt, the worktree state,
    and the previous turn's classification."""

    task_class: str | None = None
    complexity: str | None = None
    prompt_chars: int = 0
    files_touched: int = 0


@dataclass
class ModelScore:
    """A scoring breakdown for one model — what the router computed and why."""

    model: str
    label: str
    tier: int
    local: bool
    quality: float
    attempts: int
    cost_penalty: float
    exploration_bonus: float
    final: float
    source: str  # 'class' | 'model' | 'prior'
    rating_count: int = 0
    corrections: int = 0
    discards: int = 0
    reverts: int = 0


@dataclass
class RoutingDecision:
    """The router's choice for the next turn."""

    chosen: PoolEntry
    current: PoolEntry | None
    scores: list[ModelScore]
    switched: bool
    reason: str
    explored: bool = False  # True when the chosen model is NOT the current and the win is small

    def to_dict(self) -> dict[str, Any]:
        return {
            "chosen": self.chosen.to_dict(),
            "current": self.current.to_dict() if self.current else None,
            "switched": self.switched,
            "reason": self.reason,
            "explored": self.explored,
            "scores": [
                {
                    "model": s.model,
                    "label": s.label,
                    "tier": s.tier,
                    "local": s.local,
                    "quality": round(s.quality, 4),
                    "attempts": s.attempts,
                    "cost_penalty": round(s.cost_penalty, 4),
                    "exploration_bonus": round(s.exploration_bonus, 4),
                    "final": round(s.final, 4),
                    "source": s.source,
                    "rating_count": s.rating_count,
                    "corrections": s.corrections,
                    "discards": s.discards,
                    "reverts": s.reverts,
                }
                for s in self.scores
            ],
        }


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _cost_penalty(tier: int) -> float:
    # tier 1 → 0.0, tier 2 → -0.15, tier 3 → -0.30
    return -_COST_LAMBDA * max(0, tier - 1)


def _score_for_model(
    entry: PoolEntry,
    profile: dict[str, Any] | None,
    task: TaskFeatures,
    *,
    exploration: float,
    rng: random.Random,
) -> ModelScore:
    """The composite score for one model. ``profile`` is the user's full
    profile from the store (may be empty on a fresh user)."""
    quality = _PRIOR_SCORE
    attempts = 0
    source = "prior"
    rating_count = 0
    corrections = 0
    discards = 0
    reverts = 0
    if isinstance(profile, dict):
        models = profile.get("models", {})
        record = models.get(entry.model) if isinstance(models, dict) else None
        if isinstance(record, dict):
            attempts = int(record.get("attempts", 0) or 0)
            quality = float(record.get("quality_ema", quality) or quality)
            source = "model"
            rating_count = int(record.get("rating_count", 0) or 0)
            corrections = int(record.get("judge_corrections", 0) or 0)
            discards = int(record.get("discards", 0) or 0)
            reverts = int(record.get("reverts", 0) or 0)
            if task.task_class and task.task_class not in ("", "other"):
                classes = record.get("by_class", {})
                row = classes.get(task.task_class) if isinstance(classes, dict) else None
                if isinstance(row, dict) and int(row.get("n", 0) or 0) >= 3:
                    # With >=3 samples for this task class, the per-class EMA
                    # is a better signal than the model-wide one. We blend
                    # them (70/30) to avoid over-reacting to a small sample.
                    class_ema = float(row.get("ema", quality) or quality)
                    quality = 0.7 * class_ema + 0.3 * quality
                    source = "class"
    cost_penalty = _cost_penalty(entry.tier)
    # Thompson sampling: draw a noisy estimate of the true quality. With few
    # attempts the noise is large (encourages exploration); with many attempts
    # it converges to the recorded quality.
    if attempts > 0:
        std = max(0.02, exploration * (1.0 / math.sqrt(attempts)))
    else:
        std = exploration  # fully noisy until we have any data
    sample = rng.gauss(quality, std)
    final = sample + cost_penalty
    return ModelScore(
        model=entry.model,
        label=entry.label,
        tier=entry.tier,
        local=entry.local,
        quality=quality,
        attempts=attempts,
        cost_penalty=cost_penalty,
        exploration_bonus=sample - quality,
        final=final,
        source=source,
        rating_count=rating_count,
        corrections=corrections,
        discards=discards,
        reverts=reverts,
    )


def choose(
    pool: list[PoolEntry],
    *,
    current: PoolEntry | None,
    profile: dict[str, Any] | None,
    task: TaskFeatures | None = None,
    mode: str = "suggest",
    allow_cloud: bool = True,
    min_margin: float = _DEFAULT_MIN_MARGIN,
    exploration: float = _DEFAULT_EXPLORATION,
    rng: random.Random | None = None,
) -> RoutingDecision:
    """Pick the next model.

    * ``mode`` is one of: ``"off"`` (always stay), ``"suggest"`` (compute
      but only flag a switch when the margin is clear), ``"auto"``
      (compute and act). The function always returns a decision; the
      caller decides whether to act.
    * ``allow_cloud=False`` filters out non-local entries.
    * ``min_margin`` is the smallest final-score gap between the chosen
      model and the current model that justifies a switch.
    * ``exploration`` is the Thompson-sampling standard-deviation scale
      (0.0 ⇒ pure greedy; 0.3 ⇒ very exploratory).
    """
    if rng is None:
        rng = random.Random()
    if task is None:
        task = TaskFeatures()
    if mode == "off" or not pool:
        keep = current or (pool[0] if pool else None)
        if keep is None:
            raise ValueError("routing pool is empty")
        return RoutingDecision(
            chosen=keep,
            current=current,
            scores=[],
            switched=False,
            reason="routing disabled" if mode == "off" else "empty pool",
        )
    candidates = pool if allow_cloud else [entry for entry in pool if entry.local]
    if not candidates:
        candidates = pool  # never strand the user — fall back if every entry was a cloud model
    # Ensure the current model is always a candidate: if the user is on a
    # model the pool doesn't list, add it as a phantom so the router can
    # reason about "stay where you are" vs switching to a new one.
    if current is not None and not any(entry.model == current.model for entry in candidates):
        candidates.append(current)
    scores = [
        _score_for_model(entry, profile, task, exploration=exploration, rng=rng)
        for entry in candidates
    ]
    # Greedy winner by final score.
    scores_sorted = sorted(scores, key=lambda s: s.final, reverse=True)
    chosen_score = scores_sorted[0]
    chosen_entry = next(
        (entry for entry in candidates if entry.model == chosen_score.model),
        candidates[0],
    )
    # Margin: the score gap to the current model (None when there's no current
    # or the current isn't in the pool).
    current_score = next(
        (s for s in scores if current is not None and s.model == current.model),
        None,
    )
    margin = (
        chosen_score.final - current_score.final
        if current_score is not None
        else float("inf")
    )
    explored = bool(current is not None and chosen_score.model != current.model and margin < min_margin * 2)
    if current is not None and chosen_score.model == current.model:
        return RoutingDecision(
            chosen=chosen_entry,
            current=current,
            scores=scores_sorted,
            switched=False,
            reason="current model already scores highest",
            explored=False,
        )
    # In "suggest" mode, only recommend a switch when the margin is clear.
    if mode == "suggest" and current is not None and margin < min_margin:
        return RoutingDecision(
            chosen=current,
            current=current,
            scores=scores_sorted,
            switched=False,
            reason=f"current model within {min_margin:.2f} margin (gap {margin:.3f})",
            explored=False,
        )
    reason = _format_reason(chosen_score, current_score, margin, mode)
    return RoutingDecision(
        chosen=chosen_entry,
        current=current,
        scores=scores_sorted,
        switched=True,
        reason=reason,
        explored=explored,
    )


def _format_reason(chosen: ModelScore, current: ModelScore | None, margin: float, mode: str) -> str:
    if current is None:
        return f"{chosen.label}: no current model (cold start; quality {chosen.quality:.2f})"
    if chosen.model == current.model:
        return f"{chosen.label}: current already wins (margin {margin:+.3f})"
    if margin < 0.02:
        return f"{chosen.label}: best by {margin:+.3f} — too small to switch in {mode} mode"
    return f"{chosen.label}: beats {current.label} by {margin:+.3f} (quality {chosen.quality:.2f} vs {current.quality:.2f})"


# --- pool construction --------------------------------------------------------


def default_pool_for_backend(backend: str, models: Iterable[str]) -> list[PoolEntry]:
    """The auto-seeded pool for a backend. The user edits it in settings.
    Tiers are assigned by name: cheaper names → tier 1; everything else → tier 2.
    A 'haiku' / 'mini' / 'small' / 'flash' / 'lite' tag → tier 1; 'opus' / 'pro' /
    'large' / 'xl' / 'max' → tier 3; the rest → tier 2. Local providers
    (``ollama/``, ``local/``) → tier 1 + local=True."""
    cheap_markers = ("haiku", "mini", "small", "flash", "lite", "nano", "local")
    expensive_markers = ("opus", "pro", "large", "xl", "max", "ultra", "thinking", "reasoning", "27b", "32b", "70b")
    out: list[PoolEntry] = []
    for raw in models:
        model_id = str(raw)
        if not model_id:
            continue
        lowered = model_id.lower()
        if lowered.startswith(("ollama/", "local/", "lmstudio/", "llama.cpp/")):
            tier = 1
            local = True
        elif any(marker in lowered for marker in cheap_markers):
            tier = 1
            local = False
        elif any(marker in lowered for marker in expensive_markers):
            tier = 3
            local = False
        else:
            tier = 2
            local = False
        out.append(PoolEntry(label=model_id, model=model_id, tier=tier, local=local))
    return out


def pool_from_config(raw: Any) -> list[PoolEntry]:
    """Parse the ``routing_pool`` config value into ``PoolEntry`` objects.

    The config is a list of {label?, model, tier?, local?}. Invalid entries
    (missing model) are silently dropped. ``tier`` defaults to 2, ``local``
    defaults to False, ``label`` defaults to the model id."""
    if not isinstance(raw, list):
        return []
    out: list[PoolEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        model = item.get("model")
        if not isinstance(model, str) or not model:
            continue
        label = item.get("label")
        if not isinstance(label, str) or not label:
            label = model
        tier = item.get("tier", 2)
        try:
            tier_int = int(tier)
        except (TypeError, ValueError):
            tier_int = 2
        tier_int = max(1, min(3, tier_int))
        local = bool(item.get("local", False))
        out.append(PoolEntry(label=label, model=model, tier=tier_int, local=local))
    return out


__all__ = [
    "PoolEntry",
    "TaskFeatures",
    "ModelScore",
    "RoutingDecision",
    "choose",
    "default_pool_for_backend",
    "pool_from_config",
]
