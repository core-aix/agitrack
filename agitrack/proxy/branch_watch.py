"""Watching what moves UNDER a session, as a mixin on ``ProxyRunner``.

A session is pinned to a merge target and a working tree, and both can change without
aGiTrack doing anything: the user checks out another branch in the repo directory, the agent
runs ``git switch`` inside its own worktree, someone commits in the base repo, or a resumed
backend starts running turns in the wrong directory entirely. Each of those silently
invalidates an assumption the commit pipeline is built on, so each is polled for, reported
once, and followed where following is right.

Everything here is THROTTLED and mtime-gated on purpose: it runs from the reactor's timers
pass, so an unthrottled check would spend a git subprocess per tick (see AGENTS.md, "The
Enter path is on the clock").

See ``sharing.py``'s header for why these are mixins rather than collaborator objects.
"""

from __future__ import annotations

import os
import time

from agitrack.git import is_managed_branch

from agitrack.proxy.runner_host import RunnerHost


class BranchWatchMixin(RunnerHost):
    """``ProxyRunner``'s branch/tree drift watch. Mixed in, never instantiated on its own."""

    def _check_base_branch_drift(self) -> None:
        # Poll the branch checked out in the repo directory. The status bar bolds a
        # session whose merge target differs from it; when the directory MOVES to a
        # branch that differs from the active session's merge target, ask where this
        # session's changes should merge. (Each session has its own merge branch,
        # independent of the directory's checkout and of the other sessions.)
        if self._base_branch is None:
            return
        if not self._use_worktrees:
            # No-worktree mode (#9): the agent edits the directory's branch directly, so a
            # session can only ever land its work on that current branch — never a different
            # merge target. There is no merge-target dialog; just warn (and follow) when the
            # directory's branch is switched.
            self._check_no_worktree_branch_change()
            return
        if self.worktree is None:
            return
        now = time.monotonic()
        if now - self._base_drift_check_at < self.BASE_DRIFT_CHECK_SECONDS:
            return
        self._base_drift_check_at = now
        # Cheap pre-check: the checked-out branch only changes when `.git/HEAD` is
        # rewritten (a checkout — a commit does not touch it), so skip the git
        # subprocess and reuse the cached branch when HEAD's mtime is unchanged. The
        # pending-merge-prompt handling below depends on session merge state, not
        # HEAD, so it still runs every tick.
        git_dir = self._base_git_dir()
        head_sig = self._newest_mtime([git_dir / "HEAD"]) if git_dir is not None else None
        if head_sig is not None and head_sig == self._base_head_mtime:
            current = self._repo_dir_branch  # HEAD untouched — branch is unchanged
        else:
            if head_sig is not None:
                self._base_head_mtime = head_sig
            try:
                current = self.base_repo.current_branch()  # the branch checked out in the repo dir
            except Exception:
                return
        if current is None:
            return
        moved = current != self._repo_dir_branch
        self._repo_dir_branch = current  # keep the status bar's "current dir branch" fresh
        # A dir-change prompt this session deferred during a run fires once the run is idle
        # AND its changes have MERGED into the session's original branch — not the moment
        # the run goes idle (the just-finished work is still committing/integrating then,
        # and must land on the original branch before we ask where future work should go).
        # A cancelled run leaves nothing to integrate, so the dialog is free to appear. The
        # flag is per-session, evaluated against the FRESH dir branch: if the directory has
        # returned to this session's branch there's nothing to ask, and a different
        # session's pending prompt is never touched (it has its own flag).
        if self._pending_merge_prompt:
            if self._base_branch == current:
                self._pending_merge_prompt = False  # back in sync — this session's deferral is moot
            elif not self.agent_in_flight and self._session_work_merged_into_base():
                self._pending_merge_prompt = False
                self._prompt_merge_targets_on_dir_change()
            return  # handled this session's pending deferral; nothing else to do this tick
        if not moved:
            return  # the repo directory hasn't moved since the last poll
        if self._base_branch == current:
            return  # the session already merges into the directory's branch
        if self.agent_in_flight:
            # The session is mid-run — don't change its merge branch now. Warn that this
            # run still lands on its current branch, and re-ask once its changes have
            # merged there (or the run is cancelled, leaving nothing to merge).
            self._pending_merge_prompt = True
            self._set_message(
                f"Repo is now on '{current}', but this run still merges into '{self._base_branch}'. "
                f"You'll be asked again once its changes have merged into '{self._base_branch}'.",
                seconds=10.0,
            )
            self._render()
            return
        self._prompt_merge_targets_on_dir_change()

    def _check_no_worktree_branch_change(self) -> None:
        # No-worktree mode: a session always works on (merges into) whatever branch is
        # checked out in the repo directory and can never target a different one. If the
        # user switches the directory's branch, future work simply lands on the new branch —
        # warn so that change is never silent, and follow the directory so the status bar and
        # accounting stay accurate. The warning fires once per switch (``_base_branch`` is
        # advanced immediately, so the next poll sees no change).
        now = time.monotonic()
        if now - self._base_drift_check_at < self.BASE_DRIFT_CHECK_SECONDS:
            return
        self._base_drift_check_at = now
        # Cheap pre-check: a checkout rewrites `.git/HEAD`; if its mtime is unchanged
        # the branch can't have switched, so skip the git subprocess entirely.
        git_dir = self._base_git_dir()
        if git_dir is not None:
            head_sig = self._newest_mtime([git_dir / "HEAD"])
            if head_sig == self._base_head_mtime:
                return
            self._base_head_mtime = head_sig
        try:
            current = self.base_repo.current_branch()  # the branch checked out in the repo dir
        except Exception:
            return
        if not current or current == self._base_branch:
            if current:
                self._repo_dir_branch = current  # keep the status bar fresh
            return
        old = self._base_branch
        self._base_branch = current
        self._repo_dir_branch = current
        self._integration.base_branch = current
        self._set_message(
            f"The repo directory switched from '{old}' to '{current}'. Running without a "
            f"worktree, the agent's changes now land on '{current}' — a session always "
            f"follows the directory's current branch and can't merge into a different one.",
            seconds=12.0,
        )
        self._render()

    def _follow_agent_worktree_branch(self) -> None:
        # The backend agent can switch the branch checked out IN ITS OWN WORKTREE with a plain
        # `git checkout`/`git switch` — the worktree directory does not move, only HEAD. aGiTrack
        # otherwise only ever leaves a worktree detached at base (between turns) or on a managed
        # `agitrack/<backend>/<name>/tN` turn branch, so the worktree sitting on a NON-managed, named
        # branch can only mean the agent moved it. Follow it: point the session's tracked branch
        # (status bar + integration accounting) at it so cover commits keep landing there as
        # normal — the existing `is_managed_branch` gates already skip auto-integrating a branch
        # the agent owns — and tell the user once. (No-worktree mode is handled separately by
        # `_check_no_worktree_branch_change`; there's no second branch to follow without a worktree.)
        if self.worktree is None or self._base_branch is None:
            return
        now = time.monotonic()
        if now - self._agent_branch_check_at < self.BASE_DRIFT_CHECK_SECONDS:
            return
        self._agent_branch_check_at = now
        try:
            current = self.repo.current_branch()  # the branch checked out in THIS session's worktree
        except Exception:
            return
        # "HEAD" = detached (aGiTrack's between-turns state), a managed turn branch = aGiTrack's
        # own doing, and a branch already equal to the tracked one = nothing new. None of those is
        # an agent-driven switch.
        if not current or current == "HEAD" or is_managed_branch(current) or current == self._base_branch:
            return
        self._base_branch = current
        self._repo_dir_branch = current  # keep the status bar's branch fresh
        self._integration.base_branch = current
        self._set_message(f"Working branch switched to '{current}' by the backend agent.", seconds=10.0)
        self._render()

    def _session_work_merged_into_base(self) -> bool:
        # True once the active session's work has landed on its merge branch — nothing
        # uncommitted, mid-merge, or committed-but-unintegrated remains. Used to hold the
        # deferred branch-switch dialog until a just-finished run's changes have merged
        # into the original branch (a cancelled run leaves nothing pending, so it's True).
        try:
            return not self._integration.session_unintegrated(self.repo)
        except Exception:
            return True

    def _prompt_merge_targets_on_dir_change(self) -> None:
        # The repo directory was switched to another branch. Sessions keep merging into
        # their own branches by default; offer to realign them. The first (default)
        # option does nothing; then "switch only this session"; then, with more than
        # one session, "switch ALL sessions" to the directory's branch.
        if self.worktree is None or self._base_branch is None or self._repo_dir_branch is None:
            return
        if self._base_branch == self._repo_dir_branch:
            return  # the active session already merges into the directory's branch
        dir_branch = self._repo_dir_branch
        options = [
            "Do nothing — keep every session merging into its own branch",
            f"Switch only '{self.name}' to '{dir_branch}'",
        ]
        if len(self.sessions or []) > 1:
            options.append(f"Switch all idle sessions to '{dir_branch}' (running sessions keep their branch)")
        choice = self._select_popup(
            f"The repo directory is now on '{dir_branch}', but session '{self.name}' merges into "
            f"'{self._base_branch}'. What should happen? "
            f"(Change any session's merge branch later via Ctrl-G → session → Change a session's merge branch.)",
            options,
        )
        if not choice or choice.startswith("Do nothing"):
            self._set_message(
                f"Sessions keep merging into their own branches (the repo directory is on '{dir_branch}').",
                seconds=6.0,
            )
            self._render()
            return
        if choice.startswith("Switch all idle"):
            self._retarget_all_sessions(dir_branch)
            return
        self._retarget_active_session(dir_branch)  # switch only the active session

    def _prompt_merge_target_if_diverged(self) -> None:
        # Used on a SESSION SWITCH: when the newly-active session merges into a branch
        # other than the directory's, ask whether to keep its own branch (default) or
        # switch it to the directory's.
        if self.worktree is None or self._base_branch is None or self._repo_dir_branch is None:
            return
        if self._base_branch == self._repo_dir_branch:
            return
        if self.agent_in_flight:
            return  # a running session's branch can't change mid-run; the status bar bolds the difference
        choice = self._select_popup(
            f"The repo directory is on '{self._repo_dir_branch}', but session '{self.name}' merges into "
            f"'{self._base_branch}'. Where should this session's changes merge?",
            [
                f"Keep merging into '{self._base_branch}'",
                f"Switch to '{self._repo_dir_branch}' (the current directory branch)",
            ],
        )
        if choice and choice.startswith("Switch to '"):
            self._retarget_active_session(self._repo_dir_branch)
        else:
            self._set_message(
                f"'{self.name}' keeps merging into '{self._base_branch}' "
                f"(the repo directory is on '{self._repo_dir_branch}').",
                seconds=6.0,
            )
            self._render()

    def _retarget_all_sessions(self, target: str) -> None:
        # Re-point every IDLE session at `target` (used by "switch all idle sessions").
        # A running session keeps the branch it started its turn on — its in-flight work
        # must not split across branches — so it is left untouched and reported.
        skipped: list[str] = []
        for index in range(len(self.sessions)):
            session = self.sessions[index]
            if getattr(session, "agent_in_flight", False):
                skipped.append(self._session_name(index))
                continue
            if session is self.active:
                self._retarget_active_session(target)  # active, in place
            else:
                self._with_session(session, lambda: self._retarget_active_session(target))
        if skipped:
            self._set_message(
                f"Kept {', '.join(skipped)} on their current branch — they're running a turn; "
                f"re-point them once idle via Ctrl-G → session → Change a session's merge branch.",
                seconds=10.0,
            )
            self._render()

    def _reconcile_merge_branch(self, default_base: str | None) -> None:
        # On startup/resume aGiTrack assumes a session merges into the directory's current
        # branch (`default_base`). If a PRIOR run assigned this worktree a different merge
        # branch (persisted in its state), honor that assignment instead and flag a
        # confirmation prompt, so the user confirms the branch change before it takes
        # effect rather than silently merging into a branch they didn't choose. Otherwise
        # record `default_base` as this worktree's merge branch.
        if self.state is None:
            return
        previous = self.state.merge_branch  # what the previous aGiTrack instance assigned
        if previous and previous != default_base and self._branch_exists(previous):
            self._base_branch = previous  # keep merging where the prior run was pointed
            self._pending_merge_prompt = True  # confirm with the user once the TUI is ready
        elif default_base is not None:
            # No prior assignment, it already matches, or its branch is gone — record the
            # directory's current branch as this worktree's merge branch.
            self.state.merge_branch = default_base

    def _branch_exists(self, branch: str) -> bool:
        # Whether `branch` is a real local branch right now. On any error assume it does,
        # so a prior merge-branch assignment is never silently dropped on a transient
        # failure — only when the branch is provably gone.
        try:
            return branch in self.base_repo.list_branches()
        except Exception:
            return True

    def _retarget_active_session(self, target: str) -> bool:
        # Change the ACTIVE session's merge destination to `target` — per-session, so
        # the other sessions and the repo directory are left untouched. Flush its
        # pending work into the OLD target first, then re-point its worktree there.
        if self.worktree is None or self._base_branch is None:
            return False
        if target == self._base_branch:
            return True
        if self.agent_in_flight:
            # Never change a running session's merge branch mid-turn — its in-flight
            # work would split across branches. Only an idle session can be re-pointed.
            self._set_message(
                f"'{self.name}' is running a turn — its merge branch can only be changed when idle. "
                f"This run will merge into '{self._base_branch}'.",
                seconds=10.0,
            )
            self._render()
            return False
        old = self._base_branch
        self._exiting = True  # suppress the conflict-resolve popup during the flush
        self._set_message(f"Integrating '{self.name}' into '{old}' before re-targeting…", seconds=30)
        self._render()
        self._integrate_session_keeping_alive()
        if self._integration.session_unintegrated(self.repo):
            self._exiting = False
            self._set_message(
                f"Can't change '{self.name}' merge target: it has unresolved work in '{old}'. "
                f"Resolve it first ({self._menu_label()} → session).",
                seconds=12.0,
            )
            self._render()
            return False
        self._base_branch = target  # the active session's new merge target (syncs the service)
        if self.state is not None:
            self.state.merge_branch = target  # keep the worktree's recorded merge branch in step
        self._repoint_current_to_base()
        self._exiting = False
        self._set_message(f"'{self.name}' now merges into '{target}'.", seconds=8.0)
        self._render()
        return True

    def _warn_if_base_edited(self) -> None:
        # Fallback for un-sandboxed platforms: detect the agent editing the base
        # repo (its working tree gaining uncommitted changes beyond the startup
        # baseline) and warn, since those edits bypass aGiTrack's worktree tracking.
        # (A direct *commit* to base is prevented outright by the pre-commit guard
        # hook — see agitrack/git/hooks.py — which can attribute reliably via an env
        # marker, something this status-based check cannot do.)
        if not self._monitor_base_edits:
            return
        now = time.monotonic()
        if now - self._base_check_at < self.BASE_EDIT_CHECK_SECONDS:
            return
        self._base_check_at = now
        try:
            current = set(self.base_repo.status_short().splitlines())
        except Exception:
            return
        # Everything the agent has stranded in the base tree since startup (baseline stays
        # pristine — only aGiTrack's own writes fold into it via _rebaseline_base_edits — so a
        # stranded file stays counted for the exit reminder even after it's been warned once).
        stranded = current - self._base_status_baseline
        unwarned = stranded - self._base_warned_files
        if unwarned:
            files = ", ".join(sorted(line[3:] for line in list(unwarned) if len(line) > 3)[:5])
            self._set_message(
                f"Agent edited the base repo, outside its worktree ({files}). These "
                "changes are not tracked by aGiTrack — move them into the worktree.",
                seconds=12.0,
            )
            self._base_warned_files |= stranded  # warned now; don't re-nag the same files
            self._render()

    def _rebaseline_base_edits(self) -> None:
        # Re-baseline the un-sandboxed base-edit monitor after aGiTrack ITSELF wrote into the base
        # repo (the copy-back of stranded worktree files). Those files are aGiTrack's own action,
        # not the agent editing outside its worktree, so fold them into the baseline rather than
        # letting _warn_if_base_edited flag them as "Agent edited the base repo". No-op when the
        # monitor isn't active (sandbox enforces confinement, or no worktree).
        if not self._monitor_base_edits:
            return
        try:
            self._base_status_baseline = set(self.base_repo.status_short().splitlines())
        except Exception as error:
            self._debug(f"rebaseline base edits failed: {error!r}")

    def _remind_stranded_base_edits_on_exit(self) -> None:
        # Last line of defense on an unsandboxed host (e.g. Ubuntu with unprivileged user
        # namespaces restricted, where bwrap can't confine): if the agent left edits in the base
        # tree outside its worktree, aGiTrack never committed them, and on exit the worktree is
        # about to be removed — so surface them ONE more time, non-transiently, listing the files
        # and where they are, rather than letting the work vanish quietly. Best-effort and never
        # blocks exit. No-op when the monitor is off (sandbox enforces confinement, or no worktree).
        if not self._monitor_base_edits:
            return
        try:
            current = set(self.base_repo.status_short().splitlines())
        except Exception:
            return
        stranded = sorted(line[3:] for line in (current - self._base_status_baseline) if len(line) > 3)
        if not stranded:
            return
        shown = ", ".join(stranded[:8]) + (" …" if len(stranded) > 8 else "")
        self._debug(f"stranded base-repo edits at exit: {stranded}")
        self._set_message(
            f"Heads up: the agent left {len(stranded)} change(s) in the base repo, outside its "
            f"worktree ({shown}), which aGiTrack did NOT commit. They are in {self.base_repo.repo}; "
            "review and move or commit them so the work isn't lost.",
            seconds=20.0,
        )
        self._render()

    @staticmethod
    def _newest_mtime(paths) -> float:
        """Newest modification time across *paths* (missing entries ignored).

        A cheap change signal: a single ``stat`` per path, no work-tree walk, so a
        git subprocess only runs when the underlying ref/HEAD file actually moved.
        Returns 0.0 when nothing is present.
        """
        newest = 0.0
        for path in paths:
            try:
                newest = max(newest, os.stat(path).st_mtime)
            except OSError:
                continue
        return newest

    def _base_git_dir(self):
        """The repo dir's ``.git`` directory as a Path, or None when it can't be
        resolved (e.g. a stubbed base repo in tests). Callers that can't resolve it
        skip mtime-gating and fall back to reading git directly."""
        repo = getattr(self.base_repo, "repo", None)
        try:
            return repo / ".git" if repo is not None else None
        except TypeError:
            return None

    def _poll_base_advanced(self) -> None:
        # aGiTrack advances the base itself (integration sets `_base_advanced`), but the
        # base branch can also gain commits out of band — the user commits directly
        # to it, pulls, rebases, etc. Poll its HEAD on a throttle and, when it moves
        # for any reason, flag a sync so idle worktrees pick the new commits up
        # (`_sync_idle_worktrees_to_base`). The first observation only records the
        # baseline; it never triggers on startup.
        if self.worktree is None or self._base_branch is None:
            return
        now = time.monotonic()
        if now - self._base_poll_at < self.BASE_POLL_SECONDS:
            return
        self._base_poll_at = now
        # Cheap pre-check: only shell out to `git rev-parse` when the base branch's
        # ref (loose file or packed-refs) was actually rewritten since the last poll.
        git_dir = self._base_git_dir()
        if git_dir is not None:
            sig = self._newest_mtime([git_dir / "refs" / "heads" / self._base_branch, git_dir / "packed-refs"])
            if sig == self._base_ref_mtime:
                self._prune_user_declined()  # keep the status-line count current
                return
            self._base_ref_mtime = sig
        try:
            head = self.base_repo.rev_parse(self._base_branch)
        except Exception as error:
            self._debug(f"base-head poll failed: {error!r}")
            return
        if self._last_base_head is not None and head != self._last_base_head:
            self._base_advanced = True
        self._last_base_head = head
        self._prune_user_declined()  # keep the status-line count current

    def _warn_if_cwd_drifted(self) -> None:
        # `claude --resume` can restore a session's *saved* working directory and
        # ignore the worktree aGiTrack launched it in (Claude Code issue #58591). When
        # that happens the agent works in the wrong directory: its turns aren't
        # tracked here and writes outside the worktree are sandbox-blocked. Detect
        # it from the cwd the backend records, and warn once with how to recover.
        #
        # Only the cwd of a turn recorded *after* this launch counts (`since`): a
        # resume — especially of an imported/shared session — leaves a stale cwd in
        # the transcript that points elsewhere (the base repo, another machine's
        # path) but is harmless, because the next real turn runs in the worktree.
        # Without the time gate that stale value latched a confusing false warning
        # before the agent had done anything (#72).
        if self._cwd_drift_checked:
            return
        if self.worktree is None:
            return
        now = time.monotonic()
        if now - self._cwd_check_at < self.CWD_CHECK_SECONDS:
            return
        self._cwd_check_at = now
        fn = getattr(self.backend, "recorded_working_dir", None)
        if fn is None:
            self._cwd_drift_checked = True  # backend doesn't record a cwd
            return
        try:
            recorded = fn(self.state.backend_session_id, since=self._cwd_launch_at or None)
        except Exception as error:
            self._debug(f"cwd drift check failed: {error!r}")
            return
        if not recorded:
            return  # no post-launch turn recorded yet — check again next tick
        self._cwd_drift_checked = True
        if os.path.realpath(recorded) == os.path.realpath(str(self.repo.repo)):
            return  # on the worktree, as intended
        self._debug(f"cwd drift: backend recorded {recorded}, worktree is {self.repo.repo}")
        self._set_message(
            f"⚠ The agent ran a turn in:\n    {recorded}\n"
            f"not this session's worktree:\n    {self.repo.repo}\n"
            "This is Claude's resume-cwd bug (#58591): turns made there are NOT committed "
            "by aGiTrack, and edits outside the worktree are blocked by the sandbox.\n"
            f"To recover: {self._menu_label()} → session → start a NEW session (it launches fresh in the "
            "worktree) and re-send your request there; resuming this conversation will keep "
            "landing in the wrong directory. Any work already done in the other directory "
            "stays there — move it into the worktree by hand if you need it tracked.",
            seconds=30.0,
        )
        self._render()
