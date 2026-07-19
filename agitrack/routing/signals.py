"""Convenience recorders for routing signals.

The runners call into here instead of building :class:`SignalEvent` objects by
hand. Each recorder is a thin wrapper that captures the local context
(backend, session, current model) and delegates to
:func:`agitrack.routing.store.record_event`.

Recording is best-effort: a failure here must NEVER abort the runner. Every
recorder swallows exceptions after a debug log, so a corrupted store file or
a missing git user.name can't block a turn's commit or a user's exit.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from agitrack.routing.store import (
    EVENT_KIND_CANCEL,
    EVENT_KIND_DISCARD,
    EVENT_KIND_POST_EDIT,
    EVENT_KIND_RATING,
    EVENT_KIND_REDO,
    EVENT_KIND_REVERT,
    EVENT_KIND_REROUTE,
    EVENT_KIND_SWITCH,
    SignalEvent,
    maybe_sync,
    record_event,
    user_id,
)

if TYPE_CHECKING:
    from agitrack.git import GitRepo


def _resolve(
    repo_root: Path,
    backend: str | None,
    model: str | None,
    *,
    debug_log: Any = None,
) -> tuple[str, str | None, str | None] | None:
    """Return (gid, backend_name, model) — or None when the repo isn't a git
    repo (in which case recording would crash). Failures are logged via
    ``debug_log`` (a no-op default is fine)."""
    try:
        from agitrack.git import GitRepo

        repo: GitRepo | None = GitRepo(repo_root)
        gid = user_id(repo_root, repo)
    except Exception as error:  # noqa: BLE001
        if debug_log is not None:
            debug_log(f"routing signal: could not resolve user identity: {error!r}")
        return None
    return gid, backend or "unknown", model


def record_rating(
    repo_root: Path,
    *,
    backend: str | None,
    model: str | None,
    rating: int,
    commit: str | None = None,
    session: str | None = None,
    task_class: str | None = None,
    complexity: str | None = None,
    debug_log: Any = None,
) -> None:
    """Record an explicit 1-5 star rating for the current turn's model."""
    if not 1 <= int(rating) <= 5:
        return
    resolved = _resolve(repo_root, backend, model, debug_log=debug_log)
    if resolved is None:
        return
    gid, backend_name, model_name = resolved
    try:
        record_event(
            repo_root,
            gid,
            SignalEvent(
                kind=EVENT_KIND_RATING,
                model=model_name,
                backend=backend_name,
                task_class=task_class,
                complexity=complexity,
                value=int(rating),
                commit=commit,
                session=session,
            ),
        )
        _try_maybe_sync(repo_root, debug_log)
    except Exception as error:  # noqa: BLE001
        if debug_log is not None:
            debug_log(f"routing record_rating failed: {error!r}")


def record_discard(
    repo_root: Path,
    *,
    backend: str | None,
    model: str | None,
    commit: str | None = None,
    session: str | None = None,
    task_class: str | None = None,
    complexity: str | None = None,
    debug_log: Any = None,
) -> None:
    """Record an implicit "user discarded the agent's interrupted turn" signal."""
    _record_implicit(
        repo_root,
        kind=EVENT_KIND_DISCARD,
        backend=backend,
        model=model,
        commit=commit,
        session=session,
        task_class=task_class,
        complexity=complexity,
        debug_log=debug_log,
    )


def record_cancel(
    repo_root: Path,
    *,
    backend: str | None,
    model: str | None,
    commit: str | None = None,
    session: str | None = None,
    debug_log: Any = None,
) -> None:
    """Record an implicit "user cancelled the turn" signal (weaker than discard)."""
    _record_implicit(
        repo_root,
        kind=EVENT_KIND_CANCEL,
        backend=backend,
        model=model,
        commit=commit,
        session=session,
        task_class=None,
        complexity=None,
        debug_log=debug_log,
    )


def record_revert(
    repo_root: Path,
    *,
    backend: str | None,
    model: str | None,
    commit: str | None = None,
    session: str | None = None,
    task_class: str | None = None,
    complexity: str | None = None,
    debug_log: Any = None,
) -> None:
    """Record an implicit "user reverted the agent's commit" signal."""
    _record_implicit(
        repo_root,
        kind=EVENT_KIND_REVERT,
        backend=backend,
        model=model,
        commit=commit,
        session=session,
        task_class=task_class,
        complexity=complexity,
        debug_log=debug_log,
    )


def record_redo_followup(
    repo_root: Path,
    *,
    backend: str | None,
    model: str | None,
    commit: str | None = None,
    session: str | None = None,
    task_class: str | None = None,
    complexity: str | None = None,
    debug_log: Any = None,
) -> None:
    """Record an implicit "user re-did the previous turn" signal."""
    _record_implicit(
        repo_root,
        kind=EVENT_KIND_REDO,
        backend=backend,
        model=model,
        commit=commit,
        session=session,
        task_class=task_class,
        complexity=complexity,
        debug_log=debug_log,
    )


def record_post_agent_edit(
    repo_root: Path,
    *,
    backend: str | None,
    model: str | None,
    commit: str | None = None,
    session: str | None = None,
    debug_log: Any = None,
) -> None:
    """Record an implicit "user edited the agent's output before the next prompt"
    signal. Weak; could be unrelated user work."""
    _record_implicit(
        repo_root,
        kind=EVENT_KIND_POST_EDIT,
        backend=backend,
        model=model,
        commit=commit,
        session=session,
        task_class=None,
        complexity=None,
        debug_log=debug_log,
    )


def record_switch(
    repo_root: Path,
    *,
    backend: str | None,
    from_model: str | None,
    to_model: str | None,
    session: str | None = None,
    debug_log: Any = None,
) -> None:
    """Record a model switch (in-TUI or relaunch). The model leaving gets a
    small negative nudge; the model entering gets a small positive nudge —
    this gives the router a soft prior on what switches led to good
    outcomes in the past, but stays defensible if a switch was made for
    unrelated reasons (the judgment comes from the next turn's rating)."""
    resolved = _resolve(repo_root, backend, to_model, debug_log=debug_log)
    if resolved is None:
        return
    gid, backend_name, _ = resolved
    try:
        record_event(
            repo_root,
            gid,
            SignalEvent(
                kind=EVENT_KIND_SWITCH,
                model=to_model,
                backend=backend_name,
                value={"from": from_model, "to": to_model},
                session=session,
            ),
        )
    except Exception as error:  # noqa: BLE001
        if debug_log is not None:
            debug_log(f"routing record_switch failed: {error!r}")


def record_reroute(
    repo_root: Path,
    *,
    backend: str | None,
    from_model: str | None,
    to_model: str | None,
    session: str | None = None,
    debug_log: Any = None,
) -> None:
    """Record an auto-reroute decision (router picked a different model than
    the user was on). Distinct from ``record_switch`` so the dashboard can
    show them separately — auto-reroutes should be rare, and a growing
    auto-reroute count means the policy is being noisy."""
    resolved = _resolve(repo_root, backend, to_model, debug_log=debug_log)
    if resolved is None:
        return
    gid, backend_name, _ = resolved
    try:
        record_event(
            repo_root,
            gid,
            SignalEvent(
                kind=EVENT_KIND_REROUTE,
                model=to_model,
                backend=backend_name,
                value={"from": from_model, "to": to_model},
                session=session,
            ),
        )
    except Exception as error:  # noqa: BLE001
        if debug_log is not None:
            debug_log(f"routing record_reroute failed: {error!r}")


def _record_implicit(
    repo_root: Path,
    *,
    kind: str,
    backend: str | None,
    model: str | None,
    commit: str | None,
    session: str | None,
    task_class: str | None,
    complexity: str | None,
    debug_log: Any,
) -> None:
    resolved = _resolve(repo_root, backend, model, debug_log=debug_log)
    if resolved is None:
        return
    gid, backend_name, model_name = resolved
    try:
        record_event(
            repo_root,
            gid,
            SignalEvent(
                kind=kind,
                model=model_name,
                backend=backend_name,
                task_class=task_class,
                complexity=complexity,
                commit=commit,
                session=session,
            ),
        )
        _try_maybe_sync(repo_root, debug_log)
    except Exception as error:  # noqa: BLE001
        if debug_log is not None:
            debug_log(f"routing _record_implicit({kind}) failed: {error!r}")


def _try_maybe_sync(repo_root: Path, debug_log: Any) -> None:
    """Throttled background push of the prefs after a meaningful event."""
    try:
        from agitrack.git import GitRepo

        maybe_sync(repo_root, GitRepo(repo_root))
    except Exception as error:  # noqa: BLE001
        if debug_log is not None:
            debug_log(f"routing maybe_sync failed: {error!r}")


__all__ = [
    "record_rating",
    "record_discard",
    "record_cancel",
    "record_revert",
    "record_redo_followup",
    "record_post_agent_edit",
    "record_switch",
    "record_reroute",
]
