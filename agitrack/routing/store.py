"""Per-user model-routing preferences for aGiTrack.

Stores learned quality signals keyed by GitHub id, with the same shape and sync
mechanics as :mod:`agitrack.metrics.learn` (``.agitrack/routing.json``,
git-ignored, optional orphan-ref sync). Used by:

* :mod:`agitrack.routing.judge` to record judge output after each turn
* :mod:`agitrack.routing.policy` to score and choose the next coding model
* :mod:`agitrack.routing.signals` to record implicit signals (discard, revert)
* the dashboard panel under ``/routing``

The user is the *signal generator*: explicit ratings, the summarizer-as-judge
call (cheap model, structured JSON), and implicit signals from the runner
(cancelled+discarded turns, ``git revert`` of an agent commit, etc.). The
router combines them into a per-(model, task-class) quality score, then chooses
the coding model for the next turn.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agitrack.env import getenv_compat
from agitrack.git import GitRepo

# Sync ref: history-free orphan commits, one routing-prefs per user, scoped by
# repo fingerprint — the same shape as refs/agitrack/learning-progress. The
# shared-session store and the learning-progress store were the prior
# precedents; the routing-prefs ref MUST NOT clash with either.
PREFS_REF = "refs/agitrack/routing-prefs"
_SYNC_THROTTLE_SECONDS = 60.0
_PROGRESS_FETCH_TTL = 300.0
# Ring buffer of per-event records (the most recent N per user) — enough to
# audit recent decisions without letting the file grow forever.
_MAX_EVENTS = 500

# A single user-scoped write at a time. Multiple sources may record concurrently
# (judge worker, status bar hint, dashboard rate POST); the lock guarantees
# the file is never torn by interleaved reads/writes.
_STORE_LOCK = threading.Lock()
_sync_at: dict[str, float] = {}
_prefs_fetch_at: dict[str, float] = {}
_restore_checked: set[str] = set()

# GitHub id is resolved via `gh api user` (a network call); cache the result.
_identity_cache: dict[str, tuple[float, str]] = {}
_IDENTITY_TTL = 3600.0


def _scratch_path() -> Path:
    # Stable location, outside any repository. Same pattern as the summarizer
    # and learn-page scratch dirs — a router score probe (if any) wouldn't
    # accidentally pollute a repo's backend session history.
    config_dir = getenv_compat("CONFIG_DIR")
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".agitrack"
    return base


def routing_scratch_dir() -> Path:
    path = _scratch_path() / "routing"
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_id(root: Path, repo: GitRepo | None) -> str:
    """The current user's identity for preference keying: their GitHub login
    (slugged), falling back to the git user.name when ``repo`` is a git repo.
    Cached; ``gh api user`` is a network call."""
    key = str(root)
    import time

    now = time.monotonic()
    hit = _identity_cache.get(key)
    if hit and now - hit[0] < _IDENTITY_TTL:
        return hit[1]
    from agitrack.sessions.identity import github_login

    gid = github_login(repo)
    _identity_cache[key] = (now, gid)
    return gid


# --------------------------------------------------------------------------- store


class RoutingStore:
    """``.agitrack/routing.json``: per-user learned signals (judge output,
    explicit ratings, implicit events), keyed by GitHub id. Local plumbing
    next to ``state.json``; atomic writes so an interrupted request can't
    truncate the file. Mirrors :class:`agitrack.metrics.learn.LearnStore`."""

    def __init__(self, repo_root: Path) -> None:
        self.path = Path(repo_root) / ".agitrack" / "routing.json"
        self.root = Path(repo_root)

    def load(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"profiles": {}}
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
            return {"profiles": {}}
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Best-effort: keep .agitrack/ git-ignored even in a repo that never ran
        # aGiTrack. A no-op outside a git repo.
        try:
            from agitrack.config.state import AgitrackState

            AgitrackState(self.root).ensure_local_ignore()
        except Exception:
            pass
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.path)

    @staticmethod
    def profile(data: dict[str, Any], gid: str) -> dict[str, Any]:
        """Get-or-create the profile for user ``gid``."""
        profiles = data.setdefault("profiles", {})
        profile = profiles.get(gid)
        if not isinstance(profile, dict):
            profile = {
                "models": {},
                "events": [],
                "current_model": None,
            }
            profiles[gid] = profile
        profile.setdefault("models", {})
        if not isinstance(profile.get("events"), list):
            profile["events"] = []
        if not isinstance(profile.get("current_model"), (str, type(None))):
            profile["current_model"] = None
        return profile

    def update(self, gid: str, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Read-modify-write the user's profile under the store lock; returns it."""
        with _STORE_LOCK:
            data = self.load()
            profile = self.profile(data, gid)
            mutate(profile)
            # Trim the event ring buffer so the file cannot grow unbounded.
            events = profile.get("events")
            if isinstance(events, list) and len(events) > _MAX_EVENTS:
                profile["events"] = events[-_MAX_EVENTS:]
            self.save(data)
            return profile


# --------------------------------------------------------------------------- data shapes


# The kind of event recorded in the store. Centralised so the dashboard and
# the policy read the same vocabulary. New kinds MUST be added here (and
# handled in :func:`update_quality_from_event`) — the dashboard filters
#/groups on them, so a typo would silently drop signals.
EVENT_KIND_RATING = "rating"
EVENT_KIND_JUDGE_CORRECTION = "judge_correction"
EVENT_KIND_JUDGE_ACCEPT = "judge_accept"
EVENT_KIND_DISCARD = "discard"
EVENT_KIND_REVERT = "revert"
EVENT_KIND_REDO = "redo_followup"
EVENT_KIND_POST_EDIT = "post_agent_edit"
EVENT_KIND_CANCEL = "cancel"
EVENT_KIND_SWITCH = "switch"
EVENT_KIND_REROUTE = "reroute"

# Task classes the judge emits. Coarse buckets so the cold-start matrix is
# small and the score updates have enough samples to be meaningful quickly.
TASK_CLASSES = (
    "greenfield",
    "edit",
    "debug",
    "refactor",
    "test",
    "docs",
    "explain",
    "config",
    "other",
)
# Complexity buckets the judge emits — also coarse so the cold-start matrix
# stays compact.
COMPLEXITY_LEVELS = ("trivial", "small", "medium", "large")


@dataclass
class SignalEvent:
    """One recorded signal — explicit rating, judge outcome, or implicit
    runner event. ``value`` is the kind-specific payload:

    * ``rating``  : integer 1..5
    * ``judge_*`` : str (the judge field, e.g. "explicit_negative", "redo")
    * ``discard`` / ``revert`` / ``redo`` / ``post_agent_edit`` / ``cancel`` :
      None (the kind itself is the signal)
    * ``switch``  : ``{"from": str | None, "to": str | None}``
    """

    kind: str
    model: str | None
    backend: str | None
    task_class: str | None = None
    complexity: str | None = None
    value: Any = None
    commit: str | None = None
    session: str | None = None
    # Auto-populated by ``to_dict``; callers can leave it as 0.
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        import time

        out: dict[str, Any] = {
            "kind": self.kind,
            "model": self.model,
            "backend": self.backend,
            "ts": int(self.ts or time.time()),
        }
        if self.task_class is not None:
            out["task_class"] = self.task_class
        if self.complexity is not None:
            out["complexity"] = self.complexity
        if self.value is not None:
            out["value"] = self.value
        if self.commit is not None:
            out["commit"] = self.commit
        if self.session is not None:
            out["session"] = self.session
        return out


# --------------------------------------------------------------------------- profile accessors


def _model_record(profile: dict[str, Any], model: str) -> dict[str, Any]:
    models = profile.setdefault("models", {})
    record = models.get(model)
    if not isinstance(record, dict):
        record = {
            "attempts": 0,
            "quality_ema": None,
            "ratings": [],
            "rating_count": 0,
            "discards": 0,
            "reverts": 0,
            "judge_corrections": 0,
            "by_class": {},
        }
        models[model] = record
    record.setdefault("attempts", 0)
    record.setdefault("quality_ema", None)
    record.setdefault("ratings", [])
    record.setdefault("rating_count", 0)
    record.setdefault("discards", 0)
    record.setdefault("reverts", 0)
    record.setdefault("judge_corrections", 0)
    if not isinstance(record.get("by_class"), dict):
        record["by_class"] = {}
    return record


def _class_record(model_record: dict[str, Any], task_class: str) -> dict[str, Any]:
    classes = model_record.setdefault("by_class", {})
    row = classes.get(task_class)
    if not isinstance(row, dict):
        row = {
            "n": 0,
            "ema": None,
            "ratings": 0,
            "discards": 0,
            "reverts": 0,
            "corrections": 0,
        }
        classes[task_class] = row
    for key, default in (
        ("n", 0),
        ("ema", None),
        ("ratings", 0),
        ("discards", 0),
        ("reverts", 0),
        ("corrections", 0),
    ):
        if not isinstance(row.get(key), (int, float, type(None))):
            row[key] = default
    return row


# Quality is a scalar in [0, 1] — the higher, the more we want to route there.
# We start at 0.5 (uninformative) and move it via exponential moving average
# (alpha=0.3) per turn, modulated by the per-event weight.
_QUALITY_ALPHA = 0.3

# Per-event "is this a good turn?" weight in [-1, 1]. New entries should err
# on the side of low magnitude: heavy weights make the EMA overreact to a
# single noisy signal.
EVENT_WEIGHT: dict[str, float] = {
    EVENT_KIND_RATING: 0.8,  # 5★ ⇒ +0.8, 1★ ⇒ -0.8, scaled linearly
    EVENT_KIND_JUDGE_CORRECTION: -0.5,
    EVENT_KIND_JUDGE_ACCEPT: 0.2,
    EVENT_KIND_DISCARD: -0.9,
    EVENT_KIND_REVERT: -0.7,
    EVENT_KIND_REDO: -0.3,
    EVENT_KIND_POST_EDIT: -0.1,  # very weak; could be unrelated
    EVENT_KIND_CANCEL: -0.2,
    EVENT_KIND_SWITCH: 0.0,  # the switch itself isn't a quality signal
    EVENT_KIND_REROUTE: 0.0,
}

# A "good" turn (rating) maps to a positive target; 3★ is the neutral midpoint.
_RATING_MIDPOINT = 3.0
_RATING_SPAN = 2.0  # 1..5 ⇒ -1..+1 scaled by /span * weight


def _signal_to_delta(event: SignalEvent) -> float:
    """Convert a single :class:`SignalEvent` into a quality delta in [-1, 1]."""
    weight = EVENT_WEIGHT.get(event.kind, 0.0)
    if weight == 0.0:
        return 0.0
    if event.kind == EVENT_KIND_RATING:
        try:
            rating = int(event.value)
        except (TypeError, ValueError):
            return 0.0
        clamped = max(1, min(5, rating))
        normalised = (clamped - _RATING_MIDPOINT) / _RATING_SPAN  # -1..+1
        return weight * normalised
    return weight


def _ema(current: float | None, delta: float) -> float:
    prev = 0.5 if current is None else float(current)
    next_value = prev * (1.0 - _QUALITY_ALPHA) + (0.5 + 0.5 * delta) * _QUALITY_ALPHA
    # Clip to [0, 1] defensively — a 0.5-centred delta could drift.
    return max(0.0, min(1.0, next_value))


# --------------------------------------------------------------------------- recording API


def record_event(
    repo_root: Path,
    gid: str,
    event: SignalEvent,
) -> dict[str, Any]:
    """Record a signal event for user ``gid`` and update per-model quality.

    Safe to call from background threads; serialised internally. Returns the
    updated profile (a copy would be expensive; callers that mutate it should
    not)."""
    delta = _signal_to_delta(event)
    model = event.model

    def mutate(profile: dict[str, Any]) -> None:
        events = profile.setdefault("events", [])
        events.append(event.to_dict())
        if model:
            record = _model_record(profile, model)
            record["attempts"] = int(record.get("attempts", 0)) + 1
            if event.kind == EVENT_KIND_RATING:
                ratings = record.setdefault("ratings", [])
                if isinstance(ratings, list) and len(ratings) < 200:
                    ratings.append(int(event.value or 0))
                record["rating_count"] = int(record.get("rating_count", 0)) + 1
            elif event.kind == EVENT_KIND_DISCARD:
                record["discards"] = int(record.get("discards", 0)) + 1
            elif event.kind == EVENT_KIND_REVERT:
                record["reverts"] = int(record.get("reverts", 0)) + 1
            elif event.kind == EVENT_KIND_JUDGE_CORRECTION:
                record["judge_corrections"] = int(record.get("judge_corrections", 0)) + 1
            if delta != 0.0:
                record["quality_ema"] = _ema(record.get("quality_ema"), delta)
            task_class = event.task_class if event.task_class in TASK_CLASSES else "other"
            if event.task_class and event.task_class in TASK_CLASSES:
                row = _class_record(record, task_class)
                row["n"] = int(row.get("n", 0)) + 1
                if event.kind == EVENT_KIND_RATING:
                    row["ratings"] = int(row.get("ratings", 0)) + 1
                elif event.kind == EVENT_KIND_DISCARD:
                    row["discards"] = int(row.get("discards", 0)) + 1
                elif event.kind == EVENT_KIND_REVERT:
                    row["reverts"] = int(row.get("reverts", 0)) + 1
                elif event.kind == EVENT_KIND_JUDGE_CORRECTION:
                    row["corrections"] = int(row.get("corrections", 0)) + 1
                if delta != 0.0:
                    row["ema"] = _ema(row.get("ema"), delta)

    store = RoutingStore(repo_root)
    return store.update(gid, mutate)


# --------------------------------------------------------------------------- sync


def sync_enabled(repo_root: Path) -> bool:
    data = RoutingStore(repo_root).load()
    return bool(data.get("sync_enabled"))


def sync_info(repo_root: Path, repo: GitRepo | None) -> dict[str, Any]:
    if repo is None:
        return {"available": False, "enabled": False, "last": None, "users": []}
    data = RoutingStore(repo_root).load()
    _fetch_prefs_throttled(repo)
    return {
        "available": True,
        "enabled": bool(data.get("sync_enabled")),
        "last": data.get("last_sync"),
        "users": synced_users(repo),
    }


def _record_sync_result(repo_root: Path, ok: bool, error: str) -> None:
    store = RoutingStore(repo_root)
    with _STORE_LOCK:
        data = store.load()
        data["last_sync"] = {"ok": ok, "error": error[:300], "at": int(__import__("time").time())}
        store.save(data)


def sync_prefs_now(repo: GitRepo, gid: str) -> tuple[bool, str]:
    """Write the user's routing profile to the sync ref and push it (best-effort).

    Mirrors ``agitrack.metrics.learn.sync_progress_now``: the ref holds a
    history-free orphan commit, each user owns
    ``<fingerprint>/<gid>/routing.json``, and the push is guarded with a lease,
    retried once after a re-fetch on a lost race. The local routing.json is
    always the source of truth for OUR entry, so force-syncing the local ref
    from the remote can never lose our prefs: the entry is rebuilt from the
    store on every sync."""
    store = RoutingStore(repo.repo)
    with _STORE_LOCK:
        data = store.load()
        profile = RoutingStore.profile(data, gid)
        payload = json.dumps(
            {"gid": gid, "updated": int(__import__("time").time()), "profile": profile},
            indent=2,
            sort_keys=True,
        )
    fingerprint = repo.root_commit() or "no-root"
    path = f"{fingerprint}/{gid}/routing.json"
    error = ""
    if repo.remote_exists():
        repo.fetch_ref(f"+{PREFS_REF}:{PREFS_REF}", timeout=20)
    for _attempt in range(2):
        old = repo.ref_sha(PREFS_REF)
        entries = dict(repo.read_tree_paths(PREFS_REF))
        entries[path] = repo.write_blob(payload)
        tree = repo.write_tree_from(entries)
        sha = repo.commit_tree_orphan(tree, f"agitrack: routing prefs {gid}")
        repo.update_ref(PREFS_REF, sha)
        if not repo.remote_exists():
            return True, ""
        lease = f"{PREFS_REF}:{old}" if old else None
        ok, error = repo.push_ref(f"{PREFS_REF}:{PREFS_REF}", force_with_lease=lease, timeout=30)
        if ok:
            return True, ""
        from agitrack.sessions.store import _is_stale_lease

        if not _is_stale_lease(error):
            break
        repo.fetch_ref(f"+{PREFS_REF}:{PREFS_REF}", timeout=20)
    return False, error.strip()


def maybe_sync(root: Path, repo: GitRepo | None) -> None:
    """Kick a background prefs sync after a signal is recorded. Throttled; never
    blocks; a no-op without a git repo or when sync is disabled."""
    if repo is None or not sync_enabled(root):
        return
    key = str(root)
    import time

    now = time.monotonic()
    if now - _sync_at.get(key, 0.0) < _SYNC_THROTTLE_SECONDS:
        return
    _sync_at[key] = now
    gid = user_id(root, repo)

    def worker() -> None:
        try:
            ok, error = sync_prefs_now(repo, gid)
            _record_sync_result(root, ok, error)
        except Exception as exc:
            _record_sync_result(root, False, str(exc))

    threading.Thread(target=worker, daemon=True, name="agit-routing-sync").start()


def _fetch_prefs_throttled(repo: GitRepo) -> None:
    if not repo.remote_exists():
        return
    key = str(repo.repo)
    import time

    now = time.monotonic()
    if now - _prefs_fetch_at.get(key, 0.0) < _PROGRESS_FETCH_TTL:
        return
    _prefs_fetch_at[key] = now

    def worker() -> None:
        try:
            repo.fetch_ref(f"+{PREFS_REF}:{PREFS_REF}", timeout=20)
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True, name="agit-routing-fetch").start()


def _profile_is_empty(profile: dict[str, Any]) -> bool:
    models = profile.get("models")
    events = profile.get("events")
    has_models = isinstance(models, dict) and bool(models)
    has_events = isinstance(events, list) and bool(events)
    return not (has_models or has_events)


def restore_prefs_from_ref(root: Path, repo: GitRepo, gid: str) -> bool:
    """Pull the user's routing profile back onto THIS machine. The sync ref is
    how prefs travel: on a fresh clone, fetch the ref from origin and import
    the user's own entry. Called when the local profile is empty, so prefs
    follow the user across machines with no import step. A successful restore
    also re-enables sync. Never overwrites a non-empty local profile."""
    if str(root) in _restore_checked:
        return False
    if repo.remote_exists():
        repo.fetch_ref(f"+{PREFS_REF}:{PREFS_REF}", timeout=15)
    fingerprint = repo.root_commit() or "no-root"
    raw = repo.read_ref_blob(PREFS_REF, f"{fingerprint}/{gid}/routing.json")
    if not raw:
        _restore_checked.add(str(root))
        return False
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return False
    profile = parsed.get("profile") if isinstance(parsed, dict) else None
    if not isinstance(profile, dict) or _profile_is_empty(profile):
        return False
    store = RoutingStore(root)
    with _STORE_LOCK:
        data = store.load()
        current = RoutingStore.profile(data, gid)
        if not _profile_is_empty(current):
            _restore_checked.add(str(root))
            return False
        data["profiles"][gid] = profile
        data["sync_enabled"] = True
        store.save(data)
    _restore_checked.add(str(root))
    return True


def synced_users(repo: GitRepo) -> list[dict]:
    """Who has synced routing prefs for THIS repo (from the local ref), newest first."""
    fingerprint = repo.root_commit() or "no-root"
    prefix = f"{fingerprint}/"
    users: list[dict] = []
    for path in repo.read_tree_paths(PREFS_REF):
        if not path.startswith(prefix) or not path.endswith("/routing.json"):
            continue
        gid = path[len(prefix) :].split("/", 1)[0]
        raw = repo.read_ref_blob(PREFS_REF, path)
        updated = 0
        try:
            parsed = json.loads(raw) if raw else {}
            updated = int(parsed.get("updated") or 0)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        users.append({"gid": gid, "updated": updated})
    return sorted(users, key=lambda user: -user["updated"])


def set_sync(root: Path, repo: GitRepo | None, enabled: bool) -> dict[str, Any]:
    """Flip the prefs-sync toggle. Enabling syncs immediately so the page can
    report the outcome; disabling just stops future pushes. Refused without a
    git repo: the sync ref has nowhere to live."""
    if repo is None:
        return {"error": "Routing prefs sync needs a git repository; this directory isn't one."}
    store = RoutingStore(root)
    with _STORE_LOCK:
        data = store.load()
        data["sync_enabled"] = bool(enabled)
        store.save(data)
    if enabled:
        try:
            ok, error = sync_prefs_now(repo, user_id(root, repo))
        except Exception as exc:
            ok, error = False, str(exc)
        _record_sync_result(root, ok, error)
    return {"sync": sync_info(root, repo)}


# --------------------------------------------------------------------------- snapshot


def load_profile(root: Path, gid: str) -> dict[str, Any]:
    """Read the current user's profile (a fresh copy, safe to mutate for views)."""
    data = RoutingStore(root).load()
    profile = RoutingStore.profile(data, gid)
    # Deep-copy via JSON round-trip: the profile is small and the caller may
    # post-process for the dashboard.
    return json.loads(json.dumps(profile))


def load_events(root: Path, gid: str, limit: int = 100) -> list[dict]:
    profile = load_profile(root, gid)
    events = profile.get("events", [])
    if not isinstance(events, list):
        return []
    return events[-limit:]


__all__ = [
    "RoutingStore",
    "SignalEvent",
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
    "TASK_CLASSES",
    "COMPLEXITY_LEVELS",
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
]
