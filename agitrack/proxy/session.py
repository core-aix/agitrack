"""Session: one backend session's runtime state as a real object (#29, P3).

Replaces ``agit/session_runtime.py``'s field-swapping multiplexer (where the
active session's state lived directly on the ProxyRunner and background
sessions were snapshots swapped on/off the runner).  A :class:`Session` now
*owns* its state for its whole lifetime; the runner keeps ``self.sessions``
(a list of Session objects) and ``self.active`` (a pointer into that list),
and switching sessions is a pointer assignment — never an attribute copy.

The runner still exposes every per-session field as an attribute on itself
(``runner.agent_in_flight`` etc.) through a backward-compat property layer
that delegates to ``runner.active`` — see the bottom of ``runner.py``.  P7
removes that layer and moves call sites to ``runner.active.<field>``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agitrack.config import AgitrackState

import threading

from agitrack.proxy.platform.base import ChildProcess
from agitrack.proxy.process import BackendProcess


class Session:
    """All runtime state belonging to one backend session.

    Anything NOT listed in :data:`FIELDS` is host-level (terminal size, colour
    detection, host query cache, the management lock, the ProxyInput, signal
    handlers, render throttling) and lives on the ProxyRunner, shared across
    sessions.
    """

    # The per-session runtime state (former session_runtime.SESSION_FIELDS).
    FIELDS: tuple[str, ...] = (
        # backend + git identity
        "repo",
        "state",
        "backend",
        "actions",
        "name",
        "worktree",
        "turn",
        "merge_ctx",
        # The branch this session integrates ("merges") its work into. Per-session
        # so concurrent sessions can target independent branches, decoupled from the
        # branch checked out in the repo directory.
        "_base_branch",
        # Whether a directory-branch change during THIS session's run deferred a
        # merge-target prompt. Per-session so one session's pending prompt is never
        # swallowed (or double-asked) by switching to another session.
        "_pending_merge_prompt",
        # child process / screen
        "child_pid",
        "master_fd",
        "screen",
        "stream",
        # file watcher
        "file_change_event",
        "file_observer",
        # commit / parse tracking
        "agent_in_flight",
        "turn_awaiting_commit",
        # A cancelled (Esc) turn whose changes the user chose to KEEP ("commit with your
        # next turn"). Until a commit claims them those changes are still the AGENT's, so
        # nothing may re-ask about them as if they were the user's own or as leftovers.
        "cancelled_work_kept",
        # The untracked paths kept that way. Needed because a no-worktree turn's commit
        # stages only files that did NOT exist before the turn, and these do.
        "kept_cancelled_paths",
        "untracked_before_turn",
        # Untracked files that predate EVERY turn folded so far (the intersection of each
        # turn's start snapshot since the last fold), i.e. the USER's own files rather than
        # any turn's output. None until the first turn after a fold. The no-worktree fold
        # excludes them, so a file the user never consented to stage cannot ride into an
        # agent commit that does not mention it.
        "user_untracked_since_fold",
        # (base sha, source sha) the user last answered "Leave for later" for. The conflict box
        # is raised from a POLL, so without remembering the answer it came straight back every
        # couple of seconds. Both shas are in the key so a genuinely new situation — the base
        # moved, or this session committed again — still asks.
        "conflict_deferred_for",
        "agent_parse_thread",
        "agent_parse_result",
        "agent_parse_active",
        "agent_parse_lock",
        "parse_pending",
        "last_parse_start",
        "last_parse_attempt_status",
        "last_parse_finish",
        "pre_agent_reconciled_status",
        # Tool-use ids of the session's still-running background tasks, recorded from the
        # last consumed parse. While non-empty, the automatic pre-agent user-commit dialog
        # stands down: the task and the user may both be editing the tree, so ownership of
        # uncommitted changes is unknowable (see _offer_pre_agent_user_commit).
        "live_background_task_ids",
        "status_check_pending",
        "last_poll",
        # Background-poll backoff: consecutive passes that found the session had produced no
        # new backend output, and the `last_child_output` value the last pass saw. A background
        # session's servicing runs on the REACTOR thread, so polling every idle one every couple
        # of seconds spends the TUI's own thread on git for sessions that cannot have changed.
        "_bg_poll_misses",
        "_bg_poll_seen",
        "last_status",
        "last_status_change",
        "_last_change_at",
        "last_child_output",
        "last_child_output_sample",
        "_pre_spawn_sessions",
        # background commit summarization (#8) — per session so two sessions can
        # summarize concurrently, each with its own worker/result/pending slot
        "_summary_thread",
        "_summary_result",
        "_summary_pending",
        "_last_agent_commit_id",
        "_commit_merged_pending",
        "_commit_summarized",
        # pending passthrough prompt
        "passthrough_prompt",
        "passthrough_escape",
        "pending_forwarded",
        "pending_prompt_text",
        # input buffering + per-session view state
        "_input_tail",
        "child_mouse",
        "scroll_back",
        "sel_active",
        "sel_anchor",
        "sel_point",
    )

    # ``child_pid`` / ``master_fd`` are owned by the session's BackendProcess
    # (P2) and exposed as properties below, so they are excluded from slots.
    __slots__ = tuple(f for f in FIELDS if f not in ("child_pid", "master_fd")) + ("process",)

    if TYPE_CHECKING:
        # Per-session fields are set dynamically from FIELDS; annotate the ones
        # accessed directly on a Session object so mypy can check them.
        state: "AgitrackState | None"
        last_child_output: float
        last_poll: float
        _bg_poll_misses: int
        _bg_poll_seen: float
        _summary_thread: "threading.Thread | None"
        _summary_result: "dict | None"
        _summary_pending: "dict | None"
        _last_agent_commit_id: "str | None"
        _commit_merged_pending: bool
        _commit_summarized: bool

    def __init__(self, **fields) -> None:
        # Typed as the platform-agnostic ChildProcess contract: a freshly-constructed
        # session holds a POSIX BackendProcess, but the runner replaces it with the
        # platform's child (a Windows ConPTY child on native Windows) when it spawns.
        self.process: ChildProcess = BackendProcess(
            master_fd=fields.get("master_fd"),
            child_pid=fields.get("child_pid"),
        )
        for field in self.FIELDS:
            if field in ("child_pid", "master_fd"):
                continue
            setattr(self, field, fields.get(field))

    # ------------------------------------------------------------------
    # Process ownership: the Session owns a BackendProcess; child_pid and
    # master_fd remain addressable as plain session fields over it.
    # ------------------------------------------------------------------

    # Both read as ``None`` when there is no process yet — the documented meaning of None for
    # each ("has not been spawned yet, or has been torn down"). Reading straight through to
    # ``self.process`` raised AttributeError instead, so every ``if session.master_fd is None``
    # guard in the runner — the exact idiom written to handle this state — crashed on the one
    # case it exists for. The setters keep raising: assigning an fd to a session that owns no
    # process is a genuine mistake, and silently swallowing it would lose the fd.

    @property
    def child_pid(self) -> int | None:
        return self.process.child_pid if self.process is not None else None

    @child_pid.setter
    def child_pid(self, value: int | None) -> None:
        self.process.child_pid = value

    @property
    def master_fd(self) -> int | None:
        return self.process.master_fd if self.process is not None else None

    @master_fd.setter
    def master_fd(self, value: int | None) -> None:
        self.process.master_fd = value

    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        return self.state.backend_session_id if self.state is not None else None

    @classmethod
    def bare(cls) -> "Session":
        """A session with fresh per-session runtime defaults (before its
        backend identity/screen are assigned)."""
        return cls(**cls.runtime_defaults())

    @staticmethod
    def runtime_defaults() -> dict:
        """Fresh per-session runtime values for a newly created session
        (former ``session_runtime.default_session_fields``)."""
        return {
            "child_pid": None,
            "master_fd": None,
            "screen": None,
            "stream": None,
            "file_change_event": threading.Event(),
            "file_observer": None,
            "agent_in_flight": False,
            "turn_awaiting_commit": False,
            "cancelled_work_kept": False,
            "kept_cancelled_paths": frozenset(),
            "untracked_before_turn": frozenset(),
            "user_untracked_since_fold": None,
            "conflict_deferred_for": None,
            "_pending_merge_prompt": False,
            "agent_parse_thread": None,
            "agent_parse_result": None,
            "agent_parse_active": False,
            "agent_parse_lock": threading.Lock(),
            "parse_pending": False,
            "last_parse_start": 0.0,
            "last_parse_attempt_status": "",
            "last_parse_finish": 0.0,
            "pre_agent_reconciled_status": "",
            "status_check_pending": False,
            "last_poll": 0.0,
            "_bg_poll_misses": 0,
            "_bg_poll_seen": 0.0,
            "last_status": "",
            "last_status_change": 0.0,
            "_last_change_at": 0.0,
            "last_child_output": 0.0,
            "last_child_output_sample": b"",
            "_pre_spawn_sessions": None,
            "_summary_thread": None,
            "_summary_result": None,
            "_summary_pending": None,
            "_last_agent_commit_id": None,
            "_commit_merged_pending": False,
            "_commit_summarized": False,
            "passthrough_prompt": bytearray(),
            "passthrough_escape": None,
            "pending_forwarded": None,
            "pending_prompt_text": "",
            "_input_tail": b"",
            "child_mouse": False,
            "scroll_back": 0,
            "sel_active": False,
            "sel_anchor": None,
            "sel_point": None,
            "turn": 0,
            "merge_ctx": None,
            "_base_branch": None,
        }
