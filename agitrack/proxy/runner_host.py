"""The ``ProxyRunner`` surface its split-out parts rely on, declared in one place.

``ProxyRunner`` is assembled from several mixins (``sharing``, ``updates``,
``manual_commits``, ``branch_watch``, …). Each of them is ``ProxyRunner`` behaviour that was
moved out for readability, so each still calls back into runner state and runner methods it
does not itself define. Split across files, a type checker can no longer see that those
exist — and the failure mode is not just noise: without a declared type it INFERS one from
whichever assignment it happens to see first, so a mixin that assigns
``self._update_check_thread = Thread(...)`` makes the runner's own
``_update_check_thread: Thread | None = None`` an error.

So the contract is written down rather than inferred. Everything a part reaches for is
declared here once, and both the runner and every part inherit it.

**This class is empty at runtime.** The whole body is under ``TYPE_CHECKING``, so it adds no
attributes, no methods, and nothing to the instance — a name that is genuinely missing still
raises ``AttributeError`` exactly as it did before the split, instead of silently resolving to
a do-nothing stub. It exists only so the checker (and the reader) can see what the parts
depend on.

Adding a name here is a deliberate act: it says "this part of the runner is now shared
surface". If a mixin needs something not listed, prefer moving the dependency with it.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any


class RunnerHost:
    """Declares the runner state/behaviour shared with the mixins. No runtime effect."""

    if TYPE_CHECKING:
        from agitrack.config import AgitrackState, GlobalConfig
        from agitrack.git import GitRepo, WorktreeInfo
        from agitrack.proxy.integration import IntegrationService
        from agitrack.proxy.session import Session

        # --- identity and the session being served ---------------------------------
        name: str
        active: Session
        active_index: int
        sessions: list[Session]
        state: AgitrackState
        repo: GitRepo
        base_repo: GitRepo
        worktree: WorktreeInfo | None
        backend: Any  # the proxy AGENT (claude/codex/opencode); a duck-typed protocol
        global_config: GlobalConfig
        master_fd: int | None
        running: bool
        agent_in_flight: bool

        # --- mode flags ------------------------------------------------------------
        _use_worktrees: bool
        _manual_commits: bool
        _integration: IntegrationService
        _updater: Any

        # --- tunables the parts read (set from config in _apply_timings) -----------
        BASE_DRIFT_CHECK_SECONDS: float
        BASE_EDIT_CHECK_SECONDS: float
        BASE_POLL_SECONDS: float
        CWD_CHECK_SECONDS: float
        EXIT_SHARE_TIMEOUT: float
        RESUME_FETCH_TIMEOUT: float
        SHARED_FETCH_TIMEOUT: float
        SHARE_PUSH_TIMEOUT: float
        UPDATE_CHECK_SECONDS: float
        _MENU_DONE: str
        _MENU_UP: str

        # --- worker handoff slots ---------------------------------------------------
        # Declared here, not left to inference: each is assigned from a mixin AND
        # initialised to None in the runner's __init__, and the optional half is the half
        # that carries the meaning ("no worker running").
        _auto_share_thread: threading.Thread | None
        _auto_share_outcome: dict | None
        _auto_share_hash: dict[str, str]
        _auto_share_truncation_warned: set[str]
        _shared_resume_thread: threading.Thread | None
        _shared_resume_cancel: threading.Event | None
        _shared_resume_result: dict | None
        _backend_update_thread: threading.Thread | None
        _backend_update_result: dict | None
        _update_check_thread: threading.Thread | None
        _update_worker_result: Any
        _last_auto_fold: dict | None
        user_untracked_since_fold: frozenset[str] | None
        _base_warned_files: set[str]
        _cwd_launch_at: float
        _monitor_base_edits: bool
        _sessions_with_activity: set[str]
        _pending_share_conflicts: list[dict]
        _background_share_ops: list[dict]

        # --- per-session bookkeeping the parts read and advance ---------------------
        # Throttle stamps and last-seen signatures. Declared for the same reason as the
        # worker slots: assigned from both sides of the split, so an inferred type from
        # either side contradicts the other.
        _base_branch: str | None
        _repo_dir_branch: str | None
        _pending_merge_prompt: bool
        _base_drift_check_at: float
        _agent_branch_check_at: float
        _base_head_mtime: float
        _base_ref_mtime: float
        _base_check_at: float
        _base_poll_at: float
        _last_base_head: str | None
        _cwd_check_at: float
        _cwd_drift_checked: bool
        _manual_last_head: str | None
        _manual_hooks_installed: bool
        _autotrack_hook_preexisting: bool | None
        _update_check_at: float
        _update_offered: bool
        _update_pending: bool
        _update_applying: bool
        _update_status: Any
        _self_update_record: Any
        _backend_update_checked_for: str | None
        _backend_update_notice: Any
        _share_login: Any
        _manual_pending_tree: str | None
        _manual_allow_unchanged: bool
        _last_agent_commit_id: str | None
        # Keystrokes parked for the reactor while a part owns stdin (see the shared-session
        # fetch's wait loop, which pushes what the user typed back onto this).
        _input_tail: bytes
        _input_tail_at: float

        # --- runner methods the parts call ------------------------------------------
        def _background_fds(self) -> dict: ...
        def _debug(self, message: str) -> None: ...
        def _dedupe_session_name(self, base: str) -> str: ...
        def _drain_child_output(self) -> bytes | None: ...
        def _exit_child(self) -> None: ...
        def _feed_child_output(self, output: bytes) -> None: ...
        def _finalize_pending_work(self) -> None: ...
        def _format_age(self, updated: float) -> str: ...
        def _install_autotrack_precommit_hook(self) -> None: ...
        def _integrate_session_keeping_alive(self) -> None: ...
        @staticmethod
        def _is_real_keypress(data: bytes) -> bool: ...
        def _live_session_for_lineage(self, owner: str, name: str) -> int | None: ...
        def _menu_label(self) -> str: ...
        def _prompt_session_name(self, title: str, *, default: str) -> str | None: ...
        def _prune_user_declined(self) -> None: ...
        def _pump_background(self, session: Session) -> None: ...
        def _read_stdin(self, length: int) -> bytes: ...
        def _render(self) -> None: ...
        def _repoint_current_to_base(self) -> None: ...
        def _restart_agent(self, message: str) -> None: ...
        def _resume_conversation(self, name: str, session_id: str, *, backend: str | None = None) -> None: ...
        def _running_background_session_names(self) -> list[str]: ...
        def _select_popup(self, title: str, options: list[str], *, detail: list[str] | None = None) -> str | None: ...
        def _session_name(self, index: int) -> str: ...
        def _set_message(self, message: str | None, *, seconds: float = 4.0, sticky: bool = False) -> None: ...
        def _set_session_notice(self, name: str, text: str | None, *, seconds: float = 6.0) -> None: ...
        def _stdin_fileno(self) -> int: ...
        def _summary_blocks_integration(self, now: float) -> bool: ...
        def _switch_active(self, index: int) -> None: ...
        def _uncovered_backend_commits(self) -> list[str]: ...
        def _user_state(self) -> AgitrackState: ...
        def _with_session(self, session: Session, fn) -> Any: ...
