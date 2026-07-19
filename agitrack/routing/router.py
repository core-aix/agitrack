"""The router facade — the small surface aGiTrack's runners use to consult
the routing policy and record signals.

Each runner holds one :class:`Router` instance. The router:

* builds a :class:`~agitrack.routing.policy.PoolEntry` list from the user's
  ``routing_pool`` config (auto-seeded from the backend's model list when empty)
* runs the policy (``choose``) to score and pick the next model
* owns the :class:`~agitrack.routing.judge.TurnJudge` instance and exposes a
  ``record_judgement`` method for the summary workers
* offers a :meth:`switch_or_reroute` method the runner calls when it decides
  to act on a recommendation (TUI PTY injection, with a relaunch fallback)
* records explicit and implicit signals on behalf of the runner

The router is intentionally runner-agnostic. The proxy, background, and
shell runners each construct one with their own state and logger; the
facade is the only thing the runners import from :mod:`agitrack.routing`.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from agitrack.routing import policy
from agitrack.routing import signals as signals_module
from agitrack.routing.judge import JudgeResult, TurnJudge
from agitrack.routing.policy import (
    ModelScore,
    PoolEntry,
    RoutingDecision,
    TaskFeatures,
    choose,
    default_pool_for_backend,
    pool_from_config,
)
from agitrack.routing.signals import (
    record_cancel,
    record_discard,
    record_post_agent_edit,
    record_rating,
    record_redo_followup,
    record_reroute,
    record_revert,
    record_switch,
)
from agitrack.routing.store import (
    EVENT_KIND_JUDGE_ACCEPT,
    EVENT_KIND_JUDGE_CORRECTION,
    RoutingStore,
    SignalEvent,
    load_profile,
    maybe_sync,
    record_event,
    user_id,
)

log = logging.getLogger(__name__)


@dataclass
class RouterConfig:
    """The router's view of the relevant config keys. Built from GlobalConfig
    once at startup; the runner re-reads it on a per-call basis when the
    user edits settings mid-session."""

    mode: str  # off | suggest | auto
    pool: list[PoolEntry]
    allow_cloud: bool
    exploration: float
    min_margin: float
    judge_enabled: bool


def build_pool(
    *,
    backend: str,
    config_pool: list[Any],
    available_models: list[str],
    current_model: str | None = None,
) -> list[PoolEntry]:
    """Resolve the routing pool for ``backend``.

    * ``config_pool`` (the user's explicit override) wins when non-empty.
    * Otherwise: auto-seed from ``available_models`` (the backend's own
      model list — Claude tiers, or whatever the user configured in
      OpenCode including a local Ollama provider).
    * The current model is always appended as a phantom entry so the
    router can reason about "stay" vs "switch" when the user is on a model
    not in the pool.
    """
    if config_pool:
        pool = pool_from_config(config_pool)
    else:
        pool = default_pool_for_backend(backend, available_models)
    if not pool and available_models:
        # Defensive: even the auto-seeder returned nothing. Fall back to a
        # trivial one-entry pool built from the current model so the router
        # at least has something to reason about.
        if current_model:
            pool = [PoolEntry(label=current_model, model=current_model, tier=2, local=False)]
    if current_model and not any(entry.model == current_model for entry in pool):
        pool.append(PoolEntry(label=current_model, model=current_model, tier=2, local=False))
    return pool


def _current_pool_entry(pool: list[PoolEntry], model: str | None) -> PoolEntry | None:
    if not model:
        return None
    for entry in pool:
        if entry.model == model:
            return entry
    # When the user is on a model that isn't in the pool, add a phantom.
    return PoolEntry(label=model, model=model, tier=2, local=False)


def find_model_in_pool(pool: list[PoolEntry], model: str | None) -> PoolEntry | None:
    if not model:
        return None
    for entry in pool:
        if entry.model == model:
            return entry
    return None


class Router:
    """The routing facade. Constructed once per runner.

    The router never raises — every public method is best-effort, so a
    misbehaving backend or a missing config key can never block a turn's
    commit or a user's exit."""

    def __init__(
        self,
        *,
        repo_root: Path,
        backend: str,
        global_config: Any,
        debug_log: Callable[[str], None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.backend = str(backend)
        self.global_config = global_config
        self._debug = debug_log or (lambda msg: None)
        self._rng = rng or random.Random()
        # Per-session state, mutated by record_*. Cleared on switch.
        self._last_judge: JudgeResult | None = None
        self._last_decision: RoutingDecision | None = None
        # The session's model after the most recent switch (or the initial
        # value); the runner queries this to know what model is current.
        self._current_model: str | None = None

    # ---- config helpers ----------------------------------------------------

    def _config(self) -> RouterConfig:
        if self.global_config is None:
            return RouterConfig(
                mode="off",
                pool=[],
                allow_cloud=True,
                exploration=0.1,
                min_margin=0.05,
                judge_enabled=True,
            )
        return RouterConfig(
            mode=str(getattr(self.global_config, "routing_mode", "off")),
            pool=list(getattr(self.global_config, "routing_pool", []) or []),
            allow_cloud=bool(getattr(self.global_config, "routing_allow_cloud", True)),
            exploration=float(getattr(self.global_config, "routing_exploration", 0.1) or 0.1),
            min_margin=float(getattr(self.global_config, "routing_min_margin", 0.05) or 0.05),
            judge_enabled=bool(getattr(self.global_config, "routing_judge", True)),
        )

    # ---- profile + pool ----------------------------------------------------

    def _profile(self) -> dict[str, Any]:
        try:
            gid = user_id(self.repo_root, None)
        except Exception as error:  # noqa: BLE001
            self._debug(f"routing: user_id resolution failed: {error!r}")
            return {}
        try:
            return load_profile(self.repo_root, gid)
        except Exception as error:  # noqa: BLE001
            self._debug(f"routing: load_profile failed: {error!r}")
            return {}

    def build_pool(self, *, available_models: list[str], current_model: str | None) -> list[PoolEntry]:
        cfg = self._config()
        return build_pool(
            backend=self.backend,
            config_pool=cfg.pool,
            available_models=available_models,
            current_model=current_model,
        )

    # ---- decisions ---------------------------------------------------------

    def decide(
        self,
        *,
        available_models: list[str],
        current_model: str | None,
        task: TaskFeatures | None = None,
    ) -> RoutingDecision:
        """Score the pool and pick the next model. The runner decides whether
        to ACT on the decision (it's the runner that knows the TUI is idle,
        the user isn't typing, etc.). Returns a decision even when the router
        is ``off`` — the runner can still surface a hint via the status bar."""
        cfg = self._config()
        pool = self.build_pool(available_models=available_models, current_model=current_model)
        if not pool:
            # No candidates: stay where we are (or do nothing if we have no current either).
            placeholder = current_model or ""
            return RoutingDecision(
                chosen=PoolEntry(label=placeholder, model=placeholder, tier=2, local=False),
                current=_current_pool_entry([], current_model),
                scores=[],
                switched=False,
                reason="no candidates in routing pool",
            )
        current_entry = _current_pool_entry(pool, current_model)
        self._current_model = current_model
        try:
            decision = choose(
                pool,
                current=current_entry,
                profile=self._profile(),
                task=task or TaskFeatures(),
                mode=cfg.mode,
                allow_cloud=cfg.allow_cloud,
                min_margin=cfg.min_margin,
                exploration=cfg.exploration,
                rng=self._rng,
            )
        except Exception as error:  # noqa: BLE001
            self._debug(f"routing.choose failed: {error!r}")
            return RoutingDecision(
                chosen=current_entry or pool[0],
                current=current_entry,
                scores=[],
                switched=False,
                reason=f"router error: {error!r}",
            )
        self._last_decision = decision
        return decision

    # ---- recording --------------------------------------------------------

    def record_judgement(
        self,
        *,
        trace: str,
        judge: TurnJudge | None,
        commit: str | None = None,
        session: str | None = None,
        model: str | None = None,
    ) -> JudgeResult | None:
        """Run the judge over a trace and record the verdict on the store.
        Skips silently when the judge is disabled. Returns the verdict (or
        None) for the caller to forward to a status bar / dashboard."""
        cfg = self._config()
        if not cfg.judge_enabled or judge is None:
            return None
        try:
            verdict = judge.judge(trace)
        except Exception as error:  # noqa: BLE001
            self._debug(f"routing.judge failed: {error!r}")
            return None
        self._last_judge = verdict
        # Record unless the judge rejected the trace (e.g. echo, garbage).
        if verdict.usable:
            self._record(
                kind=verdict.to_signal_kind() or EVENT_KIND_JUDGE_ACCEPT,
                backend=self.backend,
                model=model or verdict.model,
                task_class=verdict.task_class,
                complexity=verdict.complexity,
                value=(verdict.evidence or verdict.correction) or None,
                commit=commit,
                session=session,
            )
        return verdict

    def record_rating(
        self,
        *,
        rating: int,
        commit: str | None = None,
        session: str | None = None,
        model: str | None = None,
    ) -> None:
        # Use the most recent judge verdict (if any) for task_class/complexity
        # context, so the rating is filed against the same cell of the score
        # matrix as the verdict that earned it.
        task_class = self._last_judge.task_class if self._last_judge else None
        complexity = self._last_judge.complexity if self._last_judge else None
        self._record(
            kind="rating",
            backend=self.backend,
            model=model or self._current_model,
            task_class=task_class,
            complexity=complexity,
            value=int(rating),
            commit=commit,
            session=session,
        )

    def record_discard(self, *, commit: str | None = None, session: str | None = None) -> None:
        self._record_implicit("discard", commit=commit, session=session)

    def record_cancel(self, *, commit: str | None = None, session: str | None = None) -> None:
        self._record_implicit("cancel", commit=commit, session=session)

    def record_revert(self, *, commit: str | None = None, session: str | None = None) -> None:
        self._record_implicit("revert", commit=commit, session=session)

    def record_redo_followup(self, *, commit: str | None = None, session: str | None = None) -> None:
        self._record_implicit("redo_followup", commit=commit, session=session)

    def record_post_agent_edit(
        self, *, commit: str | None = None, session: str | None = None
    ) -> None:
        self._record_implicit("post_agent_edit", commit=commit, session=session)

    def record_switch(
        self,
        *,
        from_model: str | None,
        to_model: str | None,
        session: str | None = None,
    ) -> None:
        self._current_model = to_model
        self._record(
            kind="switch",
            backend=self.backend,
            model=to_model,
            value={"from": from_model, "to": to_model},
            session=session,
        )

    def record_reroute(
        self,
        *,
        from_model: str | None,
        to_model: str | None,
        session: str | None = None,
    ) -> None:
        self._current_model = to_model
        self._record(
            kind="reroute",
            backend=self.backend,
            model=to_model,
            value={"from": from_model, "to": to_model},
            session=session,
        )

    # ---- private helpers ---------------------------------------------------

    def _record_implicit(
        self,
        kind: str,
        *,
        commit: str | None = None,
        session: str | None = None,
    ) -> None:
        task_class = self._last_judge.task_class if self._last_judge else None
        complexity = self._last_judge.complexity if self._last_judge else None
        self._record(
            kind=kind,
            backend=self.backend,
            model=self._current_model,
            task_class=task_class,
            complexity=complexity,
            commit=commit,
            session=session,
        )

    def _record(
        self,
        *,
        kind: str,
        backend: str,
        model: str | None,
        task_class: str | None = None,
        complexity: str | None = None,
        value: Any = None,
        commit: str | None = None,
        session: str | None = None,
    ) -> None:
        try:
            record_event(
                self.repo_root,
                user_id(self.repo_root, None),
                SignalEvent(
                    kind=kind,
                    model=model,
                    backend=backend,
                    task_class=task_class,
                    complexity=complexity,
                    value=value,
                    commit=commit,
                    session=session,
                ),
            )
        except Exception as error:  # noqa: BLE001
            self._debug(f"routing.record_event({kind}) failed: {error!r}")
            return
        # Throttled push when sync is enabled.
        try:
            from agitrack.git import GitRepo

            maybe_sync(self.repo_root, GitRepo(self.repo_root))
        except Exception as error:  # noqa: BLE001
            self._debug(f"routing.maybe_sync failed: {error!r}")


__all__ = [
    "Router",
    "RouterConfig",
    "build_pool",
    "find_model_in_pool",
]
