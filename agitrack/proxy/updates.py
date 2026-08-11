"""Keeping aGiTrack and the backend CLI current, as a mixin on ``ProxyRunner``.

Two update paths that look alike and behave very differently:

* **aGiTrack's own** — checked periodically in the background, NEVER applied under a live
  session. It waits for a moment when nothing is in flight, then restarts into the new
  code (or, for the Windows MSI, hands off to a bootstrapper and records the argv to come
  back with).
* **the backend CLI's** — offered or applied per backend, on the backend's own terms.

Both are slow, both can fail, and neither may ever interrupt a turn — which is why they
live together and away from everything else.

See ``sharing.py``'s header for why these are mixins rather than collaborator objects.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time

from agitrack.proxy.textutil import strip_ansi

from agitrack.proxy.runner_host import RunnerHost


class UpdatesMixin(RunnerHost):
    """``ProxyRunner``'s update half. Mixed in, never instantiated on its own."""

    # ------------------------------------------------------------------
    # Self-update (#: check periodically, apply once sessions are finished)
    # ------------------------------------------------------------------

    def _update_checks_enabled(self) -> bool:
        gc = getattr(self, "global_config", None)
        return bool(getattr(gc, "check_for_updates", True)) if gc is not None else False

    def _manual_update_pending(self) -> bool:
        # True when a previous automatic update failed (or wasn't retried). The user
        # is reminded once at startup, so the periodic in-session notice is held back.
        gc = getattr(self, "global_config", None)
        return bool(getattr(gc, "pending_manual_update", None)) if gc is not None else False

    def _merge_session_active(self) -> bool:
        # True while a merge / conflict resolution is in progress in ANY session
        # (the active one, or a background session integrating its turn). Update
        # prompts and updates are suppressed for the duration so they never
        # interrupt a merge the user is in the middle of.
        if getattr(self, "merge_ctx", None) is not None:
            return True
        return any(getattr(session, "merge_ctx", None) is not None for session in getattr(self, "sessions", []))

    def _maybe_check_for_update(self) -> None:
        # Kick off a background self-update check on a throttle, and surface a
        # finished one. Network I/O (`git fetch`) runs on a worker thread so the
        # terminal never stalls; the result is handed back and consumed here on
        # the main thread.
        if self._merge_session_active():
            # Don't prompt mid-merge; a result that lands now stays pending and
            # is surfaced once the merge is done.
            return
        self._consume_update_check_result()
        if self._updater is None or not self._update_checks_enabled():
            return
        if self._update_pending or self._update_applying:
            return  # already decided / in progress — stop nagging
        if self._update_check_thread is not None and self._update_check_thread.is_alive():
            return
        now = time.monotonic()
        if self._update_check_at and now - self._update_check_at < self.UPDATE_CHECK_SECONDS:
            return
        self._update_check_at = now

        def worker() -> None:
            try:
                # Self-update rather than nag: keeping the INSTALLATION current is not a
                # decision worth interrupting someone for. The session keeps running on the
                # code it loaded — restarting mid-conversation would be the interruption —
                # so this only installs, and the reminder below asks for a restart.
                # attempt_self_update holds the cross-process lock, so several aGiTrack
                # instances never upgrade the same install at once; on_status hands us the
                # check it already ran so we don't pay for a second one.
                from agitrack.update.selfupdate import attempt_self_update

                def _status(status) -> None:
                    self._update_worker_result = status

                self._self_update_record = attempt_self_update(debug=self._debug, on_status=_status)
            except Exception as error:  # never let a check crash the worker
                self._debug(f"update check failed: {error!r}")
                self._update_worker_result = None

        self._update_worker_result = None
        self._update_check_thread = threading.Thread(target=worker, daemon=True, name="agit-update-check")
        self._update_check_thread.start()

    def _consume_update_check_result(self) -> None:
        thread = self._update_check_thread
        if thread is None or thread.is_alive():
            return
        result = self._update_worker_result
        self._update_check_thread = None
        self._update_worker_result = None
        if result is None or not result.ok:
            return
        self._update_status = result
        # Feed the shared update marker so the dashboard banner and the pre-commit reminder
        # reflect the same finding (best-effort; cleared when we're up to date again).
        try:
            from agitrack.update.marker import clear_update_marker, write_update_marker

            if result.available:
                write_update_marker(
                    self.base_repo.repo, current=result.current, latest=result.latest, message=result.message
                )
            else:
                clear_update_marker(self.base_repo.repo)
        except Exception as error:
            self._debug(f"update marker write failed: {error!r}")
        record = getattr(self, "_self_update_record", None)
        if record is not None and record.needs_user and not self._update_offered:
            # aGiTrack could not install this one itself (Homebrew, an MSI, a Windows pip
            # install): say so once, with the command that does it.
            self._update_offered = True
            detail = f" {record.instructions}" if record.instructions else ""
            self._set_message(
                f"aGiTrack {record.latest} is available but this installation has to be updated by you.{detail}",
                seconds=15.0,
            )
            self._render()
            return
        if self._running_code_is_stale() and not self._update_offered:
            # A self-update (ours or another instance's) landed newer code on disk while
            # this session kept running on the old one. Remind, never restart.
            self._update_offered = True
            self._set_message(
                "aGiTrack updated itself in the background. Restart aGiTrack when convenient to load "
                f"the new version — {self._menu_label()} → 'update' restarts it once your sessions finish.",
                seconds=15.0,
            )
            self._render()
            return
        if result.available and not self._update_offered and not self._manual_update_pending():
            # Still worth a pointer when an update exists that we have not installed yet
            # (e.g. the lock was held by another instance this round).
            self._update_offered = True
            self._set_message(
                f"{result.message}\n{self._menu_label()} → 'update' to install it when your sessions finish.",
                seconds=12.0,
            )
            self._render()

    def _running_code_is_stale(self) -> bool:
        """Whether the code on disk has moved past what this process loaded — i.e. an
        update landed underneath a running session (see selfupdate: daemons restart
        themselves, sessions are only reminded)."""
        try:
            from agitrack.update.restart import RUNNING_FINGERPRINT, disk_fingerprint

            current = disk_fingerprint()
            return bool(current) and bool(RUNNING_FINGERPRINT) and current != RUNNING_FINGERPRINT
        except Exception:
            return False

    def _ready_for_update(self) -> bool:
        # "All sessions finished and commits are in": nothing is mid-turn,
        # mid-parse, mid-merge, or mid-summary anywhere. The actual commit +
        # integration of finished work is flushed by _finalize_pending_work()
        # right before the update is applied.
        if self._merge_session_active():
            return False
        if getattr(self, "agent_in_flight", False):
            return False
        if getattr(self, "agent_parse_active", False):
            return False
        if getattr(self, "pending_forwarded", None) or getattr(self, "pending_prompt_text", ""):
            return False
        if getattr(self, "_summary_pending", None) is not None:
            return False
        summary_thread = getattr(self, "_summary_thread", None)
        if summary_thread is not None and summary_thread.is_alive():
            return False
        if self._running_background_session_names():
            return False
        return True

    def _maybe_apply_pending_update(self) -> None:
        if not self._update_pending or self._update_applying:
            return
        if not self._ready_for_update():
            return
        self._apply_update_and_restart()

    def _apply_update_and_restart(self) -> None:
        # Install the update, then commit + integrate every session's finished work
        # (same path as exit) and ask run()'s teardown to re-exec aGiTrack.
        #
        # Order matters: apply the update FIRST, while the session is still fully
        # intact. If it fails, the user is left exactly where they were — nothing
        # torn down — and can keep working or retry. Doing the exit-finalize first
        # (which removes the worktree and terminates the backend) and only THEN
        # discovering apply() failed left the reactor running against a deleted
        # worktree, so the next `git status` crashed with FileNotFoundError.
        self._update_applying = True
        self._update_pending = False
        # When the code on disk is already current and only the running process is
        # stale, there is nothing to install — just finish work and re-exec. (Don't
        # run apply(): it would fetch/merge or pip-upgrade needlessly, and a dirty
        # source checkout would even block a pure restart.)
        restart_only = self._update_status is not None and self._update_status.restart_only
        if restart_only:
            message = "Restarting aGiTrack to load the updated code…"
        else:
            self._set_message("Updating aGiTrack…", seconds=30.0)
            self._render()
            result = self._updater.apply()
            if not result.ok:
                self._update_applying = False
                # Remember so the next startup reminds the user (once) instead of the
                # in-session notice re-appearing every check; the message already
                # carries manual-update instructions. Session untouched — keep running.
                if self.global_config is not None:
                    target = (self._update_status.latest if self._update_status else "") or "available"
                    self.global_config.pending_manual_update = target
                self._update_offered = True  # stop the periodic notice this session
                manual = ""
                if self._updater is not None:
                    try:
                        manual = f" {self._updater.manual_update_instructions()}"
                    except Exception:  # instructions are best-effort, never block the message
                        manual = ""
                self._set_message(f"aGiTrack update failed: {result.error}.{manual}", seconds=15.0)
                self._render()
                return  # session untouched — keep running
            # The MSI updater only DOWNLOADS during apply(); the actual install is an
            # elevated hand-off after we exit (the MSI replaces the running agitrack.exe).
            # Flag it so the teardown launches the bootstrapper instead of re-exec'ing.
            if getattr(self._updater, "pending_msi_path", None):
                self._pending_msi_handoff = True
            # Likewise, a Windows pip upgrade only RECORDS the command during apply() — the
            # running agitrack.exe is locked, so the upgrade runs in a helper after we exit
            # (which then relaunches us). Flag it for the teardown's bootstrapper hand-off.
            if getattr(self._updater, "pending_pip_upgrade", None):
                self._pending_pip_handoff = True
            message = f"{result.message} Restarting aGiTrack…"
        # Update is in place (or unnecessary): now finish commits and tear down for
        # the re-exec.
        self._set_message("Finishing commits, then restarting aGiTrack…", seconds=30.0)
        self._render()
        try:
            self._finalize_pending_work()
        except Exception as error:  # don't let a commit hiccup strand the update
            self._debug(f"finalize before update restart failed: {error!r}")
        # Stop the loop and let run()'s finally restore the terminal and release the
        # lock before _pending_restart triggers the re-exec.
        self._set_message(message, seconds=10.0)
        self._render()
        self._exit_child()
        self._pending_restart = True
        self.running = False

    def _restart_now(self, message: str) -> None:
        """Finish pending work and re-exec aGiTrack (the same teardown a self-update uses) so
        launch-time settings — worktrees on/off, the default backend, timings — take effect.
        run()'s teardown sees _pending_restart and performs the re-exec after restoring the
        terminal and releasing the lock."""
        self._set_message("Finishing commits, then restarting aGiTrack…", seconds=30.0)
        self._render()
        try:
            self._finalize_pending_work()
        except Exception as error:  # don't let a commit hiccup strand the restart
            self._debug(f"finalize before settings restart failed: {error!r}")
        self._set_message(message, seconds=10.0)
        self._render()
        self._exit_child()
        self._pending_restart = True
        self.running = False

    def _msi_last_args_path(self) -> str | None:
        # Per-user, no UAC needed to write, survives reboots. The MSI bootstrapper reads it
        # to re-launch with the same flags after the install. None when LOCALAPPDATA is unset.
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return None
        return os.path.join(local, "aGiTrack", "last-args.txt")

    def _write_msi_last_args(self) -> None:
        # Frozen-Windows (MSI) only: record this launch's argv so a self-update can restore it.
        if sys.platform != "win32" or not getattr(sys, "frozen", False):
            return
        path = self._msi_last_args_path()
        if path is None:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(subprocess.list2cmdline(list(sys.argv[1:])))
        except OSError as error:
            self._debug(f"could not write MSI last-args ({error!r})")

    def _launch_msi_bootstrapper(self) -> bool:
        """Hand the downloaded MSI off to the elevated installer and arrange the re-launch.
        Delegates to :meth:`Updater.launch_msi_bootstrapper` (shared with the startup path so
        both install MSI updates identically); on failure (e.g. UAC declined) records the
        pending update so the next launch reminds, then the caller falls back to a normal
        re-exec of the current version. Passes ``--skip-privacy-ack`` so the relaunched build
        doesn't re-ask for the acknowledgment the user already gave this session."""
        if self._updater is None:
            return False
        ok = self._updater.launch_msi_bootstrapper(["--skip-privacy-ack"])
        if not ok and self.global_config is not None:
            self.global_config.pending_manual_update = getattr(self._updater, "_msi_latest", "") or "available"
        return ok

    def _handle_update_command(self) -> None:
        # Ctrl-G → "update": show the current update status and let the user opt
        # in (applied once sessions finish), postpone, or stop update checks.
        if self._update_applying:
            self._set_message("An aGiTrack update is already in progress.")
            self._render()
            return
        # Run a FRESH check on explicit request. The cached `_update_status` comes
        # from the periodic background check (up to UPDATE_CHECK_SECONDS old), so it
        # can miss a remote push or a local-disk update that landed since — which
        # showed "up to date" even though a newer version already existed. A live
        # check (compares running vs local HEAD vs remote) reflects reality now.
        if self._updater is not None:
            self._set_message("Checking for aGiTrack updates…")
            self._render()
            try:
                self._update_status = self._updater.check()
            except Exception as error:  # network/git hiccup: fall back to cached status
                self._debug(f"on-demand update check failed: {error!r}")
        status = self._update_status
        if status is None:
            # No completed check yet — make sure one is running and ask the user
            # to retry, rather than blocking the UI on a network fetch.
            self._update_check_at = 0.0
            self._maybe_check_for_update()
            self._set_message("Checking for aGiTrack updates… run 'update' again in a moment.")
            self._render()
            return
        if not status.ok:
            self._set_message(f"Update check failed: {status.error}")
            self._render()
            return
        if not status.available:
            self._set_message(f"aGiTrack is up to date ({status.current or 'current'}).")
            self._render()
            return
        choice = self._select_popup(
            status.message,
            ["Update when sessions finish", "Not now", "Stop checking for updates"],
        )
        if choice == "Stop checking for updates":
            if self.global_config is not None:
                self.global_config.check_for_updates = False
            self._set_message("aGiTrack will no longer check for updates.")
            self._render()
            return
        if choice != "Update when sessions finish":
            self._set_message("Update postponed.")
            self._render()
            return
        self._update_pending = True
        if self._ready_for_update():
            self._apply_update_and_restart()
        else:
            self._set_message(
                "aGiTrack will update and restart once all sessions finish and commits are in.",
                seconds=8.0,
            )
            self._render()

    def _backend_update_command(self) -> list[str] | None:
        getter = getattr(self.backend, "update_command", None)
        if not callable(getter):
            return None
        try:
            cmd = getter()
        except Exception as error:
            self._debug(f"backend update_command failed: {error!r}")
            return None
        return list(cmd) if cmd else None

    def _backend_update_via_agitrack(self) -> bool:
        """Whether aGiTrack should drive this backend's update itself (from its UNCONFINED proxy)
        rather than leaving it to the backend's own updater. True for a Homebrew-managed CLI on
        macOS — independent of the sandbox toggle:
          - sandboxed: the backend's own `brew upgrade` can't run at all (macOS forbids nesting
            `sandbox-exec`), so aGiTrack MUST do it;
          - unsandboxed: it *would* work, but the backend only nags with a prompt (and OpenCode's,
            run interactively, has been failing for users), so aGiTrack does it silently instead.
        (npm/native installs self-update cleanly; aGiTrack leaves those to the backend.)"""
        if sys.platform != "darwin":
            return False
        exe = shutil.which(self.backend.name)
        if not exe:
            return False
        real = os.path.realpath(exe)
        # Homebrew kegs resolve under …/Cellar/…; the prefix is /opt/homebrew (Apple Silicon)
        # or /usr/local (Intel). Matching either covers both layouts.
        return "/Cellar/" in real or real.startswith("/opt/homebrew/") or real.startswith("/usr/local/")

    def _backend_version(self) -> str:
        """The backend CLI's reported version string, used to detect whether an update actually
        landed. Empty when it can't be read."""
        try:
            from agitrack.proc import UTF8_TEXT, console_isolation_kwargs

            proc = subprocess.run(
                [self.backend.name, "--version"],
                capture_output=True,
                **UTF8_TEXT,
                timeout=20,
                **console_isolation_kwargs(),  # keep the backend CLI off the host console (proc.py)
            )
        except Exception as error:
            self._debug(f"backend version check failed: {error!r}")
            return ""
        return (proc.stdout or proc.stderr or "").strip()

    def _maybe_auto_update_backend(self) -> None:
        """Timers phase: when aGiTrack should drive this backend's update (a brew-managed CLI on
        macOS — see _backend_update_via_agitrack), apply it AUTOMATICALLY from the UNCONFINED
        proxy — no menu, no prompt, regardless of the sandbox toggle. Evaluated once per backend
        (re-armed on a switch) and gated by the global update-check toggle. The updater itself
        decides whether an upgrade is needed (it checks the backend's release server, not
        Homebrew's possibly-stale local tap), so we don't pre-gate on `brew outdated`. Runs on a
        background thread; the result is surfaced by _service_backend_update."""
        name = getattr(self.backend, "name", None)
        if name is None or self._backend_update_checked_for == name:
            return
        self._backend_update_checked_for = name  # evaluate each backend once (re-armed on switch)
        if self.global_config is not None and not getattr(self.global_config, "check_for_updates", True):
            return  # the user turned update checks off
        if not self._backend_update_via_agitrack():
            return  # the backend self-updates cleanly on its own; leave it to do so
        if self._backend_update_thread is not None and self._backend_update_thread.is_alive():
            return
        cmd = self._backend_update_command()
        if cmd is None:
            return
        thread = threading.Thread(
            target=self._auto_update_backend_worker, args=(name, cmd), daemon=True, name="agit-backend-update"
        )
        self._backend_update_thread = thread
        thread.start()

    def _auto_update_backend_worker(self, name: str, cmd: list[str]) -> None:
        # Background: run the backend's own updater UNCONFINED so a package-manager updater
        # (notably Homebrew's own sandbox-exec) isn't nested inside the agent's macOS sandbox —
        # the very nesting macOS forbids, which is what breaks the in-backend self-update. The
        # updater no-ops fast when already current, so we don't pre-check; we compare the CLI
        # version before and after to report only a real change (and to catch a silent failure).
        before = self._backend_version()
        self._set_message(
            f"Checking {name} for updates in the background "
            f"(applying any — its own updater can't, inside aGiTrack's sandbox)…",
            seconds=600.0,
            sticky=True,
        )
        self._render()
        cwd = str(getattr(self.base_repo, "repo", None) or getattr(self.repo, "repo", "."))
        result: dict = {"name": name, "before": before}
        try:
            from agitrack.proc import UTF8_TEXT, console_isolation_kwargs

            proc = subprocess.run(
                cmd, cwd=cwd, capture_output=True, **UTF8_TEXT, timeout=600, **console_isolation_kwargs()
            )
            result["code"] = proc.returncode
            result["output"] = (proc.stdout or "") + (proc.stderr or "")
        except Exception as error:
            result["error"] = repr(error)
        result["after"] = self._backend_version()
        self._backend_update_result = result

    def _service_backend_update(self) -> None:
        """Main loop: surface a finished backend auto-update result (set by its worker thread)."""
        result = self._backend_update_result
        if result is None:
            return
        self._backend_update_result = None
        name = result.get("name", "the agent")
        if "error" in result:
            self._set_message(f"Updating {name} failed: {result['error']}", seconds=12.0)
            self._render()
            return
        before, after = result.get("before", ""), result.get("after", "")
        output = strip_ansi(result.get("output", ""))
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if after and before and after != before:
            # The version actually changed — the update landed. (Authoritative, unlike the
            # updater's exit code, which `opencode upgrade` reports as 0 even when it failed.)
            self._set_message(f"{name} updated ({before} → {after}). Start a new session to use it.", seconds=12.0)
        elif result.get("code") not in (0, None) or "fail" in output.lower() or "error" in output.lower():
            flagged = [line for line in lines if "fail" in line.lower() or "error" in line.lower()]
            detail = (flagged[-1] if flagged else (lines[-1] if lines else ""))[:200]
            self._set_message(f"Updating {name} may have failed: {detail}", seconds=14.0)
        else:
            self._set_message(f"{name} is already up to date.", seconds=6.0)
        self._render()
