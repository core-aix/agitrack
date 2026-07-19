"""Model routing with preference learning for aGiTrack.

The router picks the CODING model (not the summarizer) per session/turn, and
learns preferences from signals collected while aGiTrack drives the agent:

* **Judge** (cheap summarizer model reading the trace) emits a structured
  verdict on every turn: task class, complexity, and whether the user
  re-prompted / corrected the agent.
* **Explicit rating** via the Ctrl-G ``rate`` command and the dashboard
  widget (1 to 5 stars).
* **Implicit signals** from the runner: cancelled+discarded turns, ``git
  revert`` of an agent commit, redo follow-ups, and immediate post-agent
  user edits in the worktree.

The signals accumulate in a per-user store
(``.agitrack/routing.json``, git-ignored) and are scored per model and
per (model, task class). The router combines those scores with cost and
exploration bonuses, then acts:

* :mod:`policy` — the scoring/decision function
* :mod:`store` — the on-disk store, per-user profile, optional git sync
* :mod:`judge` — the summarizer-as-judge call
* :mod:`signals` — convenience recorders (used by the runner)
* :mod:`switch` — in-TUI model switching (PTY injection) + relaunch fallback
* :mod:`runner` — the per-runner facade the proxy/background/shell use
"""

from agitrack.routing.judge import JudgeResult, TurnJudge, heuristic_correction
from agitrack.routing.policy import (
    ModelScore,
    PoolEntry,
    RoutingDecision,
    TaskFeatures,
    choose,
    default_pool_for_backend,
    pool_from_config,
)
from agitrack.routing.router import Router, RouterConfig, build_pool, find_model_in_pool
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
    COMPLEXITY_LEVELS,
    EVENT_KIND_CANCEL,
    EVENT_KIND_DISCARD,
    EVENT_KIND_JUDGE_ACCEPT,
    EVENT_KIND_JUDGE_CORRECTION,
    EVENT_KIND_POST_EDIT,
    EVENT_KIND_RATING,
    EVENT_KIND_REDO,
    EVENT_KIND_REROUTE,
    EVENT_KIND_REVERT,
    EVENT_KIND_SWITCH,
    PREFS_REF,
    RoutingStore,
    SignalEvent,
    TASK_CLASSES,
    load_events,
    load_profile,
    maybe_sync,
    record_event,
    restore_prefs_from_ref,
    routing_scratch_dir,
    set_sync,
    sync_enabled,
    sync_info,
    sync_prefs_now,
    synced_users,
    user_id,
)
from agitrack.routing.switch import SwitchPlan, plan_for, relaunch_command

__all__ = [
    # policy
    "PoolEntry",
    "TaskFeatures",
    "ModelScore",
    "RoutingDecision",
    "choose",
    "default_pool_for_backend",
    "pool_from_config",
    # store
    "RoutingStore",
    "SignalEvent",
    "TASK_CLASSES",
    "COMPLEXITY_LEVELS",
    "EVENT_KIND_RATING",
    "EVENT_KIND_JUDGE_CORRECTION",
    "EVENT_KIND_JUDGE_ACCEPT",
    "EVENT_KIND_DISCARD",
    "EVENT_KIND_REVERT",
    "EVENT_KIND_REDO",
    "EVENT_KIND_POST_EDIT",
    "EVENT_KIND_CANCEL",
    "EVENT_KIND_SWITCH",
    "EVENT_KIND_REROUTE",
    "PREFS_REF",
    "record_event",
    "load_profile",
    "load_events",
    "sync_enabled",
    "sync_info",
    "sync_prefs_now",
    "maybe_sync",
    "restore_prefs_from_ref",
    "synced_users",
    "set_sync",
    "user_id",
    "routing_scratch_dir",
    # judge
    "JudgeResult",
    "TurnJudge",
    "heuristic_correction",
    # signals
    "record_rating",
    "record_discard",
    "record_cancel",
    "record_revert",
    "record_redo_followup",
    "record_post_agent_edit",
    "record_switch",
    "record_reroute",
    # router facade
    "Router",
    "RouterConfig",
    "build_pool",
    "find_model_in_pool",
    # switch
    "SwitchPlan",
    "plan_for",
    "relaunch_command",
]
