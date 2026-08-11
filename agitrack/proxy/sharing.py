"""Sharing whole agent sessions through git (issue #55), as a mixin on ``ProxyRunner``.

Everything about a session that leaves this machine lives here: publishing one to the
remote, keeping an opted-in session's shared copy current (auto-share, live and on exit),
the manage/resume menus, and resolving the conflicts that arise when two people push the
same lineage.

WHY A MIXIN AND NOT A COLLABORATOR. These methods are ``ProxyRunner`` behaviour: they read
and write a dozen pieces of runner state (the session list, the active session, the message
line, the worktree, the modal machinery) and call back into the runner for rendering,
popups, and session switching. Passing all of that to a standalone class would produce a
wider interface than the code it moved, so the split here is by CONCERN, not by dependency:
the methods keep the exact ``self`` they always had, and only their file changes. They are
still ``ProxyRunner`` methods to every caller, to ``getattr``, and to a test that patches
one — nothing about the runtime object changed.
"""

from __future__ import annotations

import hashlib
import select
import threading
import time

from agitrack.backends.proxy_agents import make_proxy_agent
from agitrack.proxy.runner_host import RunnerHost
from agitrack.sessions.share_cap import DEFAULT_MAX_SHARED_BYTES


def _push_rejection_reason(error: str) -> str:
    """The most informative line of a failed `git push`'s stderr — the part that names WHY
    the remote rejected it (a stale lease, a protected ref, a declined hook, a permission
    denial). Git buries the reason among progress lines, so showing the whole blob (or a
    blind prefix slice) hides it; this surfaces the line that actually explains the failure."""
    raw = [line.strip() for line in (error or "").splitlines()]
    # Drop git's "remote: " prefix so the reason reads cleanly.
    lines = [(line[len("remote:") :].strip() if line.startswith("remote:") else line) for line in raw]
    lines = [line for line in lines if line]
    if not lines:
        return "origin returned no error — the push likely timed out; check your connection and retry"
    # GitHub's push protection / repository rulesets (NOT a custom hook) carry the actionable
    # detail — which secret was found, an unblock URL, the rule — on lines OTHER than the bare
    # "declined" summary. Surface those so the user can actually resolve it.
    detail = [
        line
        for line in lines
        if any(m in line.lower() for m in ("secret", "push protection", "rule violation", "ruleset", "unblock", "http"))
    ]
    if detail:
        return " | ".join(detail[:4])[:400]
    markers = ("rejected", "denied", "declined", "permission", "forbidden", "protected", "stale info")
    for line in lines:
        if any(marker in line.lower() for marker in markers):
            return line[:200]
    return lines[-1][:200]  # else the last line, which usually carries git's summary


def _shared_transcript_rows(transcript: str) -> int:
    # Recorded in the share manifest so the resume menu can tell at a glance whether
    # a shared copy is older/newer than the local one without reading either blob.
    from agitrack.sessions import count_transcript_rows

    return count_transcript_rows(transcript)


def _redact_and_cap(backend, raw: str, max_bytes: int) -> tuple[str, bool]:
    """Redact secrets, then bound the transcript to ``max_bytes`` (keeping a resumable recent
    tail) so a large session can't exceed Git's per-file size limit. Returns the (possibly
    trimmed) shared text and whether it was trimmed."""
    from agitrack.sessions import redact_transcript

    redacted = redact_transcript(raw)
    capped = backend.cap_shared_transcript(redacted, max_bytes)
    return capped, capped != redacted


class SessionSharingMixin(RunnerHost):
    """``ProxyRunner``'s session-sharing half. Mixed in, never instantiated on its own."""

    # --- sharing full sessions via git (issue #55) -------------------------

    def _shared_store(self):
        from agitrack.sessions import SharedSessionStore

        # Operates on the base repo: it owns the remote and the shared object db.
        return SharedSessionStore(self.base_repo)

    def _share_identity(self, session_id: str | None, login: str) -> tuple[str, str, list[str]]:
        """The ``(origin_owner, name, contributors)`` a share/auto-share writes under.

        For a session imported from someone else, this is its recorded lineage origin
        (owner + name), with the current sharer merged into the contributor set — so the
        re-share updates the SAME entry (`<id1>+<id2>/<name>`, order-independent) rather
        than spawning `<sharer>/<name>` on each machine. For a session originated here,
        it's our own login + local name, contributors = [login]. A fork ("Keep both" or a
        deliberate copy) records no origin, so it starts a fresh lineage here (#55)."""
        rec = self._user_state().shared_origin(session_id) if session_id else None
        if rec and rec.get("name"):
            owner = rec.get("owner") or login
            contributors = sorted({*rec.get("contributors", []), owner, login})
            return owner, str(rec["name"]), contributors
        return login, self._session_name(self.active_index), [login]

    def _sweep_orphan_shared_sessions(self, *, fetch: bool) -> None:
        # Reclaim dangling shared-session snapshots (old/unshared versions) left by
        # rewrites — only when this repo has actually used sharing (the ref exists),
        # so unrelated repos pay nothing. Only touches genuine session snapshots,
        # never other unreachable commits. Best-effort.
        try:
            store = self._shared_store()
            if not self.base_repo.ref_exists(store.ref):
                return
            store.cleanup_orphans(fetch=fetch)
        except Exception as error:
            self._debug(f"orphan-session sweep failed: {error!r}")

    def _share_session(self) -> str:
        # Returns _MENU_DONE once a share starts/reports (so the menu closes and the progress is
        # visible) or _MENU_UP when the user backs out at the consent prompt (re-show the list).
        backend = self.backend
        if not getattr(backend, "supports_session_sharing", False):
            self._set_message(
                f"Sharing sessions isn't supported for the {backend.name} backend yet — "
                "it has no portable transcript to share.",
                seconds=10.0,
            )
            self._render()
            return self._MENU_DONE
        session_id = self.state.backend_session_id
        if not session_id or not backend.session_belongs_to_repo(self.repo.repo, session_id):
            self._set_message("No resumable session for this repo to share yet.")
            self._render()
            return self._MENU_DONE
        # Informed consent before EVERY manual share — sharing is opt-in and never
        # automatic, and each push uploads a fresh, possibly sensitive transcript, so
        # the warning must appear every time, not just once. The first time it spells
        # out exactly what is uploaded; afterwards a concise reminder — but the share
        # never proceeds without an explicit "Yes".
        if not self.global_config.session_sharing_acknowledged:
            prompt = (
                "Share this conversation with collaborators?\n"
                "The conversation transcript is pushed to 'origin' and shared with the team.\n"
                "It can contain file contents, command output, and secrets — review what's in\n"
                "this session before sharing. Only this repo's sessions are ever uploaded."
            )
        else:
            prompt = (
                "Share this conversation now?\n"
                "Its transcript — which can contain file contents, command output, and secrets —\n"
                "will be pushed to 'origin'. Review what's in this session before sharing."
            )
        choice = self._select_popup(prompt, ["Yes, share it", "No, cancel"])
        if choice != "Yes, share it":
            self._set_message("Sharing cancelled.")
            self._render()
            return self._MENU_UP  # backed out before sharing — re-show the sessions list
        self.global_config.acknowledge_session_sharing()
        store = self._shared_store()
        # The display name comes from cheap identity (no transcript read), so the progress
        # notice and the keep-updated prompt below appear INSTANTLY. The heavy read+redact (and
        # the push) run in the background op — previously they blocked the main thread here,
        # which is the "it takes a while before the second confirmation" delay.
        login = self.global_config.github_login or self._cached_or_resolve_login()
        _owner, _name, contributors = self._share_identity(session_id, login)
        display = f"{'+'.join(contributors)}/{_name}"

        def op():
            payload = self._share_payload(session_id)  # read + redact OFF the main thread
            if payload is None:
                return {"payload": None}
            return {
                "payload": payload,
                "result": store.publish(
                    github_id=payload["owner"],
                    name=payload["name"],
                    transcript=payload["redacted"],
                    manifest=payload["manifest"],
                    prune_gid=payload["sharer"],
                    timeout=self.SHARE_PUSH_TIMEOUT,
                ),
            }

        def outcome(box) -> str | None:
            if "error" in box:
                return f"Could not share session: {box['error']}"
            data = box["result"]
            payload = data.get("payload")
            if payload is None:
                return "Could not read the session transcript to share."
            result = data["result"]
            if result.behind:
                # The shared copy already has newer turns than this session, so the push
                # was refused. Don't just give up — stash it so the main loop can ask the
                # user whether to overwrite or merge (can't prompt here: we're mid-iteration
                # over the background-op list). Leave the auto-share hash/origin untouched
                # since nothing was published yet.
                self._pending_share_conflicts.append(
                    {"payload": payload, "store": store, "display": payload["display"], "session_id": session_id}
                )
                return None
            self._auto_share_hash[session_id] = payload["digest"]  # don't immediately re-push the same content
            # Record the lineage origin so a later re-share (here or on another machine)
            # updates this same entry and keeps accumulating contributors.
            self._user_state().set_shared_origin(
                session_id, owner=payload["owner"], name=payload["name"], contributors=payload["contributors"]
            )
            return self._share_outcome_message(result, payload["display"], truncated=payload.get("truncated", False))

        # The read+redact+push all run in the BACKGROUND so the terminal never freezes; the
        # result lands as a notice. Only the exit-path share blocks (it must finish before the
        # process quits, since daemon threads die with it).
        self._run_share_op_async(f"share:{display}", f"Sharing '{display}' — pushing to origin…", op, outcome)
        # Offer to keep it current automatically. This is a quick interactive prompt
        # (not a network wait), shown while the push proceeds in the background.
        if not self._session_auto_shared(session_id):
            keep = self._select_popup(
                "Keep this shared session up to date automatically?\n"
                "New turns will be pushed to the shared copy as the conversation grows.",
                ["Yes, keep it updated", "No, I'll re-share manually"],
            )
            if keep == "Yes, keep it updated":
                self._set_session_auto_share(session_id, True)
                self._set_message(
                    f"'{display}' will auto-update as you work. Manage it via session → Manage shared.",
                    seconds=8.0,
                )
                self._render()
        return self._MENU_DONE  # the share is underway — close the menu so its progress shows

    def _share_max_transcript_bytes(self) -> int:
        """The max-transcript-bytes cap for sharing — user-configurable, with a safe fallback
        when the config object predates the key. Already clamped to the hard limit by
        GlobalConfig (and an over-limit config is refused at startup)."""
        return getattr(self.global_config, "share_max_transcript_bytes", DEFAULT_MAX_SHARED_BYTES)

    def _share_payload(self, session_id: str):
        """Read + redact the session transcript and build its manifest (no network,
        no UI). Returns a dict with the lineage identity (``owner``/``name``/
        ``contributors``/``display``), the actual ``sharer``, ``redacted`` text,
        ``digest``, and ``manifest`` — or None when the transcript can't be read."""
        backend = self.backend
        raw = backend.export_session_raw(self.repo.repo, session_id) or backend.export_session_raw(
            self.base_repo.repo, session_id
        )
        if not raw:
            return None
        from agitrack.sessions import github_login

        shared, truncated = _redact_and_cap(backend, raw, self._share_max_transcript_bytes())
        digest = hashlib.sha256(shared.encode("utf-8")).hexdigest()
        login = self.global_config.github_login or github_login(self.base_repo)
        self.global_config.github_login = login
        owner, name, contributors = self._share_identity(session_id, login)
        manifest = {
            "github_id": owner,  # the lineage origin owner = the entry's ref path owner
            "name": name,
            "contributors": contributors,  # every github id that has shared this session
            "backend": backend.name,
            "model": self.state.model,
            "session_id": session_id,
            "agitrack_session_id": self.state.session_id,
            "updated": int(time.time()),
            "content_hash": digest,
            "transcript_bytes": backend.transcript_size(self.base_repo.repo, session_id),
            "transcript_rows": _shared_transcript_rows(shared),
            "truncated": truncated,  # oldest middle turns dropped to fit Git's file-size limit
        }
        return {
            "owner": owner,
            "name": name,
            "contributors": contributors,
            "sharer": login,
            "display": f"{'+'.join(contributors)}/{name}",
            "redacted": shared,
            "digest": digest,
            "truncated": truncated,
            "manifest": manifest,
        }

    def _share_outcome_message(self, result, display: str, *, truncated: bool = False) -> str:
        if result.behind:
            # The shared copy already has newer turns than this machine's copy —
            # sharing would rewind it. Tell the user in plain language (no git jargon).
            return (
                f"Didn't share '{display}': the shared copy already has newer changes than "
                f"this session. Resume the shared version first to catch up, then share again."
            )
        if not result.remote:
            message = f"Saved shared session '{display}' locally (no 'origin' remote to push to)."
        elif result.pushed:
            # The ref isn't a branch, so GitHub's web UI won't show it; point the
            # user at how to see/confirm it instead.
            message = (
                f"Shared '{display}' to origin (it lives on the custom ref refs/agitrack/shared-sessions, "
                f"so it won't show on GitHub's web page). See it via session → Manage shared sessions, "
                f"or: git ls-remote origin 'refs/agitrack/*'."
            )
        else:
            # The push to origin failed. Show the REAL reason (a stale-lease race, a protected
            # ref, a declined hook, a timeout) rather than always blaming a concurrent update —
            # and never a bare "[]" from an empty error.
            message = (
                f"Saved '{display}' locally, but couldn't push it to origin. "
                f"Reason: {_push_rejection_reason(result.error)}. Try sharing again."
            )
        if getattr(result, "merged", 0):
            # A concurrent contributor's diverged copy was folded in rather than
            # overwritten, so neither side's turns were lost.
            message += f" Merged {result.merged} turn(s) shared by a collaborator."
        if result.pruned:
            message += f" Pruned {result.pruned} older shared session(s)."
        if truncated and (result.pushed or not result.remote):
            # The session was too big for Git's per-file limit, so only the most recent turns
            # were shared. Put the note on its own line (after a blank one) so it stands out.
            message += (
                "\n\nNote: this session was large, so only the most recent turns were shared "
                "(older ones were trimmed to fit the share size limit). Compact the conversation "
                "if you want a smaller, cleaner shared copy."
            )
        return message

    def _manage_shared_sessions_menu(self) -> str:
        # Fetch the remote FIRST so the list reflects what's actually shared on origin — a
        # session shared from another machine becomes manageable here, and one removed
        # elsewhere drops out, instead of trusting a possibly-stale local/mirror view (which is
        # what let a local-only session that never reached origin masquerade as "shared"). The
        # fetch is cancelable and bounded; with no remote it's an instant local call. The list
        # itself is still labelled from cheap data only (manifest + a stat-sized "newer?"
        # check), never a transcript read/redact.
        store = self._shared_store()
        login = self.global_config.github_login or self._cached_or_resolve_login()
        if not self._fetch_shared_with_cancel(store, "Fetching shared sessions from origin…"):
            return self._MENU_UP  # the user stopped the fetch — back to the sessions menu
        while True:
            mine = [entry for entry in store.entries() if entry.github_id == login]
            if not mine:
                # _MENU_DONE (not _UP) so this message is VISIBLE on the agent screen: returning
                # _UP re-shows the sessions menu straight over it, which reads as "nothing shows".
                self._set_message("No sessions are shared in this repo. Use session → Share this session to share one.")
                self._render()
                return self._MENU_DONE
            auto_state = self._user_state()  # base-repo opt-in, read once for the whole list
            options: list[str] = []
            for entry in mine:
                sid = entry.manifest.get("session_id", "")
                status = self._shared_entry_status(entry, sid)
                auto = " · auto-update on" if auto_state.auto_share_enabled(sid) else ""
                age = self._format_age(entry.manifest["updated"]) if entry.manifest.get("updated") else ""
                options.append(f"{entry.display}  ({age}{auto}) — {status}")
            choice = self._select_popup("Your shared sessions — pick one to manage", options)
            if choice is None:  # Esc → up one level (back to the sessions menu)
                return self._MENU_UP
            # An action that kicks off a background network op (update/unshare/auto-on) returns
            # _MENU_DONE so the whole menu closes and the user can WATCH its progress notice
            # ("Unsharing…" → "Unshared") on the live screen — instead of the menu re-showing
            # over it. Backing out of an entry (_MENU_UP) re-shows this list.
            if self._manage_one_shared_session(mine[options.index(choice)]) == self._MENU_DONE:
                return self._MENU_DONE

    def _shared_entry_status(self, entry, session_id: str) -> str:
        # Cheap "is the shared copy current?" — compare the transcript's byte size
        # (a stat) to the size recorded when it was shared. No read, no redact.
        shared_bytes = entry.manifest.get("transcript_bytes")
        current = self.backend.transcript_size(self.base_repo.repo, session_id) if session_id else None
        if not shared_bytes or current is None:
            return "shared"
        if current > shared_bytes:
            return "local has newer turns — Update to push them"
        return "shared (up to date)"

    def _manage_one_shared_session(self, entry) -> str:
        # Returns _MENU_DONE to close the whole menu (so a background op's progress notice is
        # visible on the live screen), or _MENU_UP to re-show the shared-sessions list.
        sid = entry.manifest.get("session_id", "")
        auto_on = self._session_auto_shared(sid)
        actions = [
            ("update", "↻ Update now (push latest turns)"),
            ("auto", ("✓ Auto-update is ON — turn it off" if auto_on else "○ Turn ON auto-update")),
            ("unshare", "✗ Unshare (remove for everyone)"),
        ]
        choice = self._select_popup(f"Manage {entry.display}", [label for _, label in actions])
        if choice is None:
            return self._MENU_UP  # Esc backs out to the list
        kind = actions[[label for _, label in actions].index(choice)][0]
        if kind == "update":
            self._update_shared_entry(entry)
            return self._MENU_DONE  # close the menu so the "Updating…" → result notice shows
        if kind == "auto":
            self._set_session_auto_share(sid, not auto_on)
            if auto_on:
                self._set_message(f"Auto-update disabled for {entry.display}.")
                self._render()
            else:
                # Enabling syncs once right away, so the shared copy is current
                # immediately instead of only on the next commit.
                self._set_message(f"Auto-update on for {entry.display} — pushing the latest now…", seconds=10.0)
                self._render()
                self._update_shared_entry(entry)
            return self._MENU_DONE  # close the menu so the message/push progress is visible
        # Unsharing removes the session from origin for every collaborator and can't be undone,
        # so confirm before doing it (mirrors the discard-confirm flow).
        confirm = self._select_popup(
            f"Unshare '{entry.display}'? This removes it from origin for everyone and can't be undone.",
            ["No, keep it", "Yes, unshare"],
        )
        if confirm == "Yes, unshare":
            self._unshare_entry(entry)
            return self._MENU_DONE  # close the menu so "Unsharing…" → "Unshared" is visible
        self._set_message("Kept the shared session.")
        self._render()
        return self._MENU_UP  # cancelled — back to the list

    def _update_shared_entry(self, entry) -> None:
        sid = entry.manifest.get("session_id", "")
        raw = self.backend.export_session_raw(self.base_repo.repo, sid) if sid else None
        if not raw:
            self._set_message(f"Can't read the transcript for {entry.display} to update it.")
            self._render()
            return
        shared, truncated = _redact_and_cap(self.backend, raw, self._share_max_transcript_bytes())
        # Updating from the Manage menu counts as a (re-)share by the current user, so
        # fold them into the contributor set — the entry stays under its origin owner.
        login = self._cached_or_resolve_login()
        contributors = sorted({*entry.contributors, login})
        manifest = {
            **entry.manifest,
            "contributors": contributors,
            "updated": int(time.time()),
            "content_hash": hashlib.sha256(shared.encode("utf-8")).hexdigest(),
            "transcript_bytes": self.backend.transcript_size(self.base_repo.repo, sid),
            "transcript_rows": _shared_transcript_rows(shared),
            "truncated": truncated,
        }
        display = f"{'+'.join(contributors)}/{entry.name}"
        store = self._shared_store()

        def op():
            return store.publish(
                github_id=entry.github_id,
                name=entry.name,
                transcript=shared,
                manifest=manifest,
                prune_gid=login,
                timeout=self.SHARE_PUSH_TIMEOUT,
            )

        def outcome(box) -> str:
            if "error" in box:
                return f"Could not update {display}: {box['error']}"
            result = box["result"]
            if sid:
                self._auto_share_hash[sid] = manifest["content_hash"]
            return self._share_outcome_message(result, display, truncated=truncated)

        # Push in the BACKGROUND (same as the initial share) so the terminal never
        # freezes; a progress notice shows now, the result lands when it finishes.
        self._run_share_op_async(f"update:{display}", f"Updating '{display}' — pushing to origin…", op, outcome)

    def _unshare_entry(self, entry) -> None:
        # Unsharing is a one-way, fire-and-forget network op with no follow-up, and the
        # user shouldn't have to wait on it — run it in the BACKGROUND so the session
        # never freezes, showing a progress notice now and the result when it lands.
        store = self._shared_store()
        sid = entry.manifest.get("session_id", "")
        if sid:
            # Stop auto-pushing it immediately. _set_session_auto_share clears the WHOLE id
            # lineage (the backend mints a new id on resume, so the opt-in may sit under a
            # drifted id), so a single call disables it everywhere and the change persists.
            self._set_session_auto_share(sid, False)

        def op():
            return store.unshare(entry.github_id, entry.name, timeout=self.SHARE_PUSH_TIMEOUT)

        def outcome(box) -> str:
            if "error" in box:
                return f"Could not unshare {entry.display}: {box['error']}"
            result = box["result"]
            if not result.remote:
                return f"Removed {entry.display} from the local shared ref (no remote to push the removal to)."
            if result.pushed:
                return f"Unshared {entry.display} (removed from origin)."
            if not result.error:
                # Nothing was rejected: the entry wasn't on origin (its share never reached it,
                # or it was already removed there). It's gone from your list either way.
                return f"Removed {entry.display} — it wasn't on origin (nothing to push)."
            # Removed locally but the origin push was rejected even after the auto-retry — show
            # the REASON git gave (not a blind prefix slice) so the user can tell a transient
            # race from a permission/protected-ref/hook rejection they must fix on the remote.
            return (
                f"Removed {entry.display} locally, but origin rejected the push — re-run unshare "
                f"from the menu to retry. Reason: {_push_rejection_reason(result.error)}"
            )

        self._run_share_op_async(
            f"unshare:{entry.display}", f"Unsharing {entry.display} — removing from origin…", op, outcome
        )

    def _run_share_op_async(self, key: str, pending_text: str, op, outcome_fn) -> None:
        """Run a best-effort network share op (e.g. unshare) on a daemon thread so the
        session never freezes. Shows *pending_text* as a notice now; a later main-loop
        tick (_service_background_share_ops) replaces it with ``outcome_fn(box)`` once
        the worker finishes, where ``box`` is ``{"result": ...}`` or ``{"error": str}``.
        The user keeps working throughout."""
        self._set_session_notice(key, pending_text, seconds=180.0)
        self._render()
        box: dict = {}

        def worker() -> None:
            try:
                box["result"] = op()
            except Exception as error:
                box["error"] = str(error)

        thread = threading.Thread(target=worker, daemon=True, name="agit-share-op")
        thread.start()
        self._background_share_ops.append({"key": key, "thread": thread, "box": box, "outcome_fn": outcome_fn})

    def _service_background_share_ops(self) -> None:
        """Main-loop tick: surface each finished background share op's result as a
        notice (replacing its in-progress one), and drop it from the pending list."""
        if not self._background_share_ops:
            return
        still: list[dict] = []
        for entry in self._background_share_ops:
            if entry["thread"].is_alive():
                still.append(entry)
                continue
            try:
                text = entry["outcome_fn"](entry["box"])
            except Exception as error:
                self._debug(f"background share op outcome failed: {error!r}")
                text = None
            if text:
                self._set_session_notice(entry["key"], text, seconds=10.0)
        self._background_share_ops = still

    def _service_share_conflicts(self) -> None:
        """Main-loop tick: for each share refused because the shared copy was already
        newer, ask the user whether to overwrite or merge, then re-share their way. Run
        AFTER _service_background_share_ops (not inside it) so opening a modal and queuing
        a new background op doesn't mutate the list that method is iterating."""
        if not self._pending_share_conflicts:
            return
        pending, self._pending_share_conflicts = self._pending_share_conflicts, []
        for conflict in pending:
            self._resolve_share_behind(conflict)

    def _resolve_share_behind(self, conflict: dict) -> None:
        """Prompt the user about one behind-refused share — the shared copy already has
        newer changes — and, if they choose, overwrite it with this session.

        Only *overwrite* (or keep-as-is) is offered: when two copies of a session CAN be
        combined they're union-merged automatically during a normal publish and never
        reach this refusal, so the only sessions that land here are ones that can't be
        merged (e.g. OpenCode's single-object export). For those, replace-or-keep is the
        real choice."""
        payload, store, display, session_id = (
            conflict["payload"],
            conflict["store"],
            conflict["display"],
            conflict["session_id"],
        )
        overwrite_opt = "Overwrite the shared copy with this session"
        choice = self._select_popup(
            f"The shared copy of '{display}' already has newer changes than this session.\n"
            "Sharing would replace those newer changes with this older one. Proceed?",
            [overwrite_opt, "Keep the newer shared copy (cancel)"],
        )
        if choice != overwrite_opt:
            self._set_message(f"Didn't share '{display}' — left the newer shared copy as is.")
            self._render()
            return

        def op():
            return store.publish(
                github_id=payload["owner"],
                name=payload["name"],
                transcript=payload["redacted"],
                manifest=payload["manifest"],
                prune_gid=payload["sharer"],
                timeout=self.SHARE_PUSH_TIMEOUT,
                overwrite=True,
            )

        def outcome(box) -> str | None:
            if "error" in box:
                return f"Could not share session: {box['error']}"
            result = box["result"]
            self._auto_share_hash[session_id] = payload["digest"]
            self._user_state().set_shared_origin(
                session_id, owner=payload["owner"], name=payload["name"], contributors=payload["contributors"]
            )
            if not result.pushed:
                return self._share_outcome_message(result, display, truncated=payload.get("truncated", False))
            note = ""
            if payload.get("truncated"):
                note = " (only the most recent turns were shared — older ones trimmed to fit the size limit)"
            return f"Overwrote the shared copy of '{display}' on origin with this session.{note}"

        self._run_share_op_async(f"share:{display}", f"Overwriting '{display}' — pushing to origin…", op, outcome)

    def _session_auto_shared(self, session_id: str | None) -> bool:
        # Read the opt-in from the BASE repo state (persists across runs); the
        # session worktree where self.state lives is removed on exit (#55). Check
        # the whole id lineage: the backend mints a new id on resume, so a session
        # opted in under an earlier id must still count after that drift.
        if not session_id:
            return False
        user = self._user_state()
        return any(user.auto_share_enabled(sid) for sid in user.session_lineage(session_id))

    def _session_is_shared(self, session_id: str | None, shared_ids: set[str]) -> bool:
        # A session counts as shared if its current id, or any id it drifted from
        # on resume, is in the shared set (#55) — so a resumed shared session is
        # still recognised after the backend forks its id.
        if not session_id:
            return False
        if session_id in shared_ids:
            return True
        return any(sid in shared_ids for sid in self._user_state().session_lineage(session_id))

    def _my_shared_session_ids(self) -> set[str]:
        # session_ids of conversations I've shared in this repo, from the LOCAL ref
        # (no network) — used to mark shared sessions in the session menu.
        if not getattr(self.backend, "supports_session_sharing", False):
            return set()
        try:
            login = self.global_config.github_login
            ids = set()
            for entry in self._shared_store().entries():
                if login and entry.github_id != login:
                    continue
                sid = entry.manifest.get("session_id")
                if sid:
                    ids.add(sid)
            return ids
        except Exception:
            return set()

    def _record_shared_alias_on_drift(self, previous: str | None, new_id: str | None) -> None:
        # When the backend forks a new session id on resume, link new→previous so a
        # session shared/auto-shared under the previous id stays recognised. Only
        # record it for ids that actually belong to a shared lineage, to keep the
        # alias map scoped (and avoid recording drift for unshared sessions) (#55).
        if not previous or not new_id or previous == new_id:
            return
        try:
            user = self._user_state()
            relevant = (
                user.auto_share_enabled(previous)
                or previous in user.shared_session_aliases()
                or previous in self._my_shared_session_ids()
            )
            if relevant:
                user.add_shared_session_alias(new_id, previous)
            # Carry the full lineage origin (owner + name + contributors) onto the new
            # id, so a re-share after the backend forks the id still updates the same
            # shared entry and keeps the contributor set (#55).
            origin = user.shared_origin(previous)
            if origin:
                user.set_shared_origin(
                    new_id, owner=origin["owner"], name=origin["name"], contributors=origin["contributors"]
                )
        except Exception as error:
            self._debug(f"record shared alias failed: {error!r}")

    def _set_session_auto_share(self, session_id: str, enabled: bool) -> None:
        user = self._user_state()
        if enabled:
            user.set_auto_share(session_id, True)
            return
        # Disable across the WHOLE id lineage, not just this id. The backend mints a new
        # session id on resume, so the opt-in may have been recorded under an earlier
        # (ancestor) id; since `_session_auto_shared` checks the lineage, clearing only this
        # id would leave an ancestor enabled — the session would then re-appear as auto-shared
        # on the next aGiTrack run and the disable wouldn't persist (#55).
        for sid in {session_id, *user.session_lineage(session_id)}:
            if sid:
                user.set_auto_share(sid, False)

    def _cached_or_resolve_login(self) -> str:
        # Resolve and cache the GitHub login. Only writes config when it actually
        # changes, so callers on a hot path (auto-share) don't re-save every time.
        cached = self.global_config.github_login
        if cached:
            return cached
        from agitrack.sessions import github_login

        login = github_login(self.base_repo)
        self.global_config.github_login = login
        return login

    # --- auto-share: keep an opted-in session's shared copy current ---------

    def _maybe_auto_share_active(self) -> None:
        # Called when a commit lands (see _announce_agent_commit), so the GitHub
        # round-trip happens at the commit cadence — not on a frequent timer.
        # Reactor-thread part: only cheap checks, then hand ALL the heavy work
        # (read transcript, redact, hash, push) to a background thread, so the UI
        # loop never blocks. The in-flight guard + the worker's content-hash gate
        # keep it from pushing redundantly on rapid commits.
        backend = self.backend
        if not getattr(backend, "supports_session_sharing", False):
            return
        sid = self.state.backend_session_id
        if not sid or not self._session_auto_shared(sid):
            return
        if self._auto_share_thread is not None and self._auto_share_thread.is_alive():
            return
        # Snapshot everything the worker needs on the main thread (these touch the
        # active session / config, which can change underneath a thread).
        login = self._cached_or_resolve_login()
        owner, name, contributors = self._share_identity(sid, login)
        ctx = {
            "session_id": sid,
            "owner": owner,
            "name": name,
            "contributors": contributors,
            "login": login,
            "backend": backend,
            "repo_path": self.repo.repo,
            "base_repo_path": self.base_repo.repo,
            "model": self.state.model,
            "agitrack_session_id": self.state.session_id,
            "store": self._shared_store(),
            "last_hash": self._auto_share_hash.get(sid),
        }
        self._auto_share_thread = threading.Thread(target=self._auto_share_worker, args=(ctx,), daemon=True)
        self._auto_share_thread.start()

    def _auto_share_worker(self, ctx: dict):
        # Runs off the reactor thread; best-effort. Reads and redacts the (possibly large)
        # transcript and pushes here, not on the loop. Returns the PublishResult on a push,
        # or None when it skipped (no transcript / unchanged) or hit an error — the exit path
        # inspects it. Records the outcome for the main loop to surface ONLY on failure/behind
        # (success is silent: this fires on every commit). See _service_auto_share_outcome.
        sid = ctx["session_id"]
        try:
            backend = ctx["backend"]
            raw = backend.export_session_raw(ctx["repo_path"], sid) or backend.export_session_raw(
                ctx["base_repo_path"], sid
            )
            if not raw:
                return None
            shared, truncated = _redact_and_cap(backend, raw, self._share_max_transcript_bytes())
            digest = hashlib.sha256(shared.encode("utf-8")).hexdigest()
            if digest == ctx["last_hash"]:
                return None  # nothing new since the last push — skip the network round-trip
            manifest = {
                "github_id": ctx["owner"],  # lineage origin owner (entry's ref path owner)
                "name": ctx["name"],
                "contributors": ctx["contributors"],
                "backend": backend.name,
                "model": ctx["model"],
                "session_id": sid,
                "agitrack_session_id": ctx["agitrack_session_id"],
                "updated": int(time.time()),
                "content_hash": digest,
                "transcript_bytes": backend.transcript_size(ctx["base_repo_path"], sid),
                "transcript_rows": _shared_transcript_rows(shared),
                "truncated": truncated,
            }
            result = ctx["store"].publish(
                github_id=ctx["owner"],
                name=ctx["name"],
                transcript=shared,
                manifest=manifest,
                prune_gid=ctx["login"],
                # Bound the push: a stalled remote must not strand this worker, because the
                # in-flight guard (_auto_share_thread.is_alive()) would then block EVERY future
                # auto-share for the run — the session would silently stop updating (the bug).
                timeout=self.SHARE_PUSH_TIMEOUT,
            )
            # Cache the digest ONLY after a real success, so a failed/refused push retries on
            # the next commit instead of being silently marked "already shared" (which left the
            # shared copy days stale with no error). A remote-less repo only has the local ref,
            # so a successful local write counts as shared.
            if result.pushed or not result.remote:
                self._auto_share_hash[sid] = digest
                # Carry truncation + sid so the main loop can show a ONE-TIME notice (it owns the
                # "already warned" set, off this worker thread). Success is otherwise silent.
                self._auto_share_outcome = {"ok": True, "truncated": truncated, "sid": sid, "name": ctx["name"]}
            elif result.behind:
                self._auto_share_outcome = {"behind": True, "name": ctx["name"]}
            else:
                self._auto_share_outcome = {"failed": result.error or "push rejected", "name": ctx["name"]}
            return result
        except Exception as error:
            self._debug(f"auto-share failed: {error!r}")
            self._auto_share_outcome = {"failed": str(error), "name": ctx["name"]}
            return None

    def _service_auto_share_outcome(self) -> None:
        """Main-loop tick: surface the live auto-share worker's result. Only FAILURE and
        'behind' are shown (success is silent — auto-share fires on every commit), so a push
        that keeps failing becomes visible instead of leaving the shared copy quietly stale."""
        outcome = self._auto_share_outcome
        if outcome is None:
            return
        self._auto_share_outcome = None
        name = outcome.get("name", "this session")
        if "failed" in outcome:
            self._set_session_notice(
                "auto-share",
                f"Auto-share for {name} failed: {str(outcome['failed'])[:120]} — it will retry on the next commit.",
                seconds=12.0,
            )
            self._render()
        elif outcome.get("behind"):
            self._set_session_notice(
                "auto-share",
                f"Auto-share for {name} skipped — the shared copy already has newer turns than this machine.",
                seconds=10.0,
            )
            self._render()
        elif outcome.get("truncated") and outcome.get("sid") not in self._auto_share_truncation_warned:
            # Tell the user ONCE that their oversized session is being trimmed to fit the share
            # size limit — not on every commit's auto-share (that would spam). Marked here, on
            # the main thread, so the show-once gate is race-free.
            self._auto_share_truncation_warned.add(outcome["sid"])
            self._set_session_notice(
                "auto-share",
                f"{name} is large, so auto-share shares only its most recent turns "
                f"(older ones are trimmed to fit the share size limit). Compact the conversation for a smaller shared copy.",
                seconds=12.0,
            )
            self._render()

    def _auto_share_on_exit(self) -> None:
        # Exit-path counterpart to _maybe_auto_share_active. The live auto-share
        # runs in a daemon thread fired on commit; quitting right after a turn
        # (before that thread is scheduled, or while it is still pushing) would
        # leave the final conversation unshared, since daemon threads are killed
        # when the process exits. So push the active session's latest transcript
        # here — but ONLY when it actually changed since the last share, and ALWAYS
        # bounded so a stalled network can never hang exit.
        backend = self.backend
        if not getattr(backend, "supports_session_sharing", False):
            return
        sid = self.state.backend_session_id
        if not sid or not self._session_auto_shared(sid):
            return
        # Nothing happened this run ⇒ nothing to share. This is the ground-truth
        # gate: it skips a session that was only resumed and never typed into, so
        # exit stays instant with no "Sharing…" message. It is robust where a
        # transcript-digest comparison is not — Claude forks a new session id on
        # resume and rewrites every transcript row, so the digest changes across
        # runs even when the user did nothing. A turn this run, by contrast, always
        # routes through on_commit_fn, which records the activity.
        if self.state.session_id not in self._sessions_with_activity:
            return
        # Let a still-running live auto-share finish first, so we don't race it and
        # so its updated content hash is visible to the change check below.
        if self._auto_share_thread is not None and self._auto_share_thread.is_alive():
            self._auto_share_thread.join(timeout=self.EXIT_SHARE_TIMEOUT)
        # Among sessions that DID see a turn, still avoid a redundant push: compare
        # the current transcript digest against this run's last live-pushed hash,
        # then the already-published manifest hash.
        digest = self._exit_share_digest(backend, sid)
        if digest is None:
            return  # transcript unreadable ⇒ nothing to share
        last = self._auto_share_hash.get(sid) or self._published_content_hash(sid)
        if digest == last:
            return  # already shared this exact content ⇒ nothing to do
        ctx = {
            "session_id": sid,
            # owner/name/contributors are resolved inside the bounded thread, after the
            # login lookup (a gh call can stall) it depends on.
            "login": None,
            "backend": backend,
            "repo_path": self.repo.repo,
            "base_repo_path": self.base_repo.repo,
            "model": self.state.model,
            "agitrack_session_id": self.state.session_id,
            "store": self._shared_store(),
            "last_hash": last,
        }
        self._set_message("Sharing this session before exit…", seconds=30)
        self._render()
        # Bound the network round-trip: run the push (and the login lookup, which
        # may shell out to gh) in a thread and wait at most EXIT_SHARE_TIMEOUT. A
        # stalled push (offline, auth, unreachable remote) can never block exit —
        # git ref updates are atomic, so an abandoned push simply doesn't land. On
        # timeout or push failure, warn and continue.
        outcome: dict = {}

        def push() -> None:
            try:
                login = self._cached_or_resolve_login()
                owner, name, contributors = self._share_identity(sid, login)
                ctx.update(login=login, owner=owner, name=name, contributors=contributors)
                outcome["result"] = self._auto_share_worker(ctx)
            except Exception as error:  # a failed share is a warning, never a traceback on screen
                self._debug(f"exit share failed: {error!r}")

        thread = threading.Thread(target=push, daemon=True, name="agit-exit-share")
        thread.start()
        thread.join(timeout=self.EXIT_SHARE_TIMEOUT)
        if thread.is_alive():
            self._set_message("Couldn't share this session before exit (timed out); continuing.", seconds=6.0)
            self._render()
            return
        result = outcome.get("result")
        if result is not None and result.remote and not result.pushed:
            self._set_message("Couldn't share this session before exit (push failed); continuing.", seconds=6.0)
            self._render()

    def _exit_share_digest(self, backend, sid: str) -> str | None:
        # The redacted-transcript digest for *sid*, matching the worker's gate, so
        # the exit path can tell whether the latest conversation differs from what
        # was last shared. None when the transcript can't be read.
        try:
            raw = backend.export_session_raw(self.repo.repo, sid) or backend.export_session_raw(
                self.base_repo.repo, sid
            )
            if not raw:
                return None
            from agitrack.sessions import redact_transcript

            return hashlib.sha256(redact_transcript(raw).encode("utf-8")).hexdigest()
        except Exception as error:
            self._debug(f"exit share digest failed: {error!r}")
            return None

    def _published_content_hash(self, sid: str) -> str | None:
        # The content_hash of this session's already-published shared entry, read
        # from the LOCAL shared ref (no network), so the exit gate can tell an
        # unedited resumed session from one with genuinely new turns. Matched by
        # session id across resume drift (lineage-aware).
        try:
            lineage = set(self._user_state().session_lineage(sid))
            for entry in self._shared_store().entries():
                if entry.manifest.get("session_id") in lineage:
                    return entry.manifest.get("content_hash")
        except Exception as error:
            self._debug(f"published content hash lookup failed: {error!r}")
        return None

    def _fetch_shared_with_cancel(self, store, message: str) -> bool:
        """Fetch the shared-session ref while keeping the UI alive and letting the
        user press Esc to stop — needed when the fetch stalls on bad internet.
        Returns True if the fetch finished, False if the user stopped it or it timed
        out. Either way the LOCAL ref is left usable (possibly stale) for listing.

        No remote ⇒ nothing to fetch over the network: do the cheap local call
        inline (this also keeps headless/test runs off the interactive wait path)."""
        if not store.repo.remote_exists():
            store.fetch()
            return True
        result: dict = {}
        cancel = threading.Event()

        def worker() -> None:
            try:
                # Bound the git fetch and make it killable, so a stopped fetch's
                # subprocess is terminated at once — never left running.
                result["ok"] = store.fetch(timeout=self.SHARED_FETCH_TIMEOUT, cancel=cancel)
            except Exception as error:  # never let a fetch failure escape the thread
                result["error"] = repr(error)

        thread = threading.Thread(target=worker, daemon=True, name="agit-shared-fetch")
        thread.start()
        self._set_message(f"{message}   ·   press Esc to stop", seconds=600)
        self._render()
        status = self._drain_pty_until_done_or_esc(thread, deadline=time.monotonic() + self.SHARED_FETCH_TIMEOUT + 2.0)
        if status != "done":
            cancel.set()  # kill the git fetch subprocess now — don't leave it running
            note = "timed out" if status == "timeout" else "stopped"
            self._set_message(f"Stopped fetching shared sessions ({note}).", seconds=6.0)
            self._render()
            return False
        if result.get("error"):
            self._debug(f"shared fetch failed: {result['error']}")
        return True

    def _drain_pty_until_done_or_esc(self, thread, *, deadline: float | None = None) -> str:
        """Wait for *thread* while keeping the UI alive (PTYs draining) so the wait
        is responsive, not a freeze, and the user can press Esc to stop.
        Returns ``"done"`` when the thread finishes, ``"cancel"`` on Esc, or
        ``"timeout"`` if the optional *deadline* passes first. Shared by the two
        cancellable shared-session fetches (listing and full-transcript)."""
        thread.join(timeout=0.05)  # fast fetches (and tests) finish without the wait UI
        if not thread.is_alive():
            return "done"
        try:
            stdin_fd = self._stdin_fileno()
        except (OSError, ValueError):
            # No real stdin (headless/non-interactive): can't offer interactive
            # cancel, so just wait for the thread, still honouring the deadline.
            while thread.is_alive():
                if deadline is not None and time.monotonic() > deadline:
                    return "timeout"
                thread.join(timeout=0.2)
            return "done"
        while thread.is_alive():
            if deadline is not None and time.monotonic() > deadline:
                return "timeout"
            master = self.master_fd
            background = self._background_fds() if self.sessions else {}
            fds = [stdin_fd]
            if master is not None:
                fds.append(master)
            fds.extend(background)
            try:
                readable, _, _ = select.select(fds, [], [], 0.2)
            except (OSError, ValueError):
                # stdin/PTY not selectable (headless): just wait for the thread.
                thread.join(timeout=0.2)
                continue
            for fd in readable:
                if fd == stdin_fd:
                    chunk = self._read_stdin(32)
                    if self._stdin_has_cancel(chunk):
                        return "cancel"
                    # Keystrokes typed while this wait runs belong to the live backend, not
                    # to the wait — stash them on the same `_input_tail` pushback the main
                    # reactor prepends to its next stdin read, so they are forwarded once the
                    # wait ends instead of being silently dropped (the first keystrokes after
                    # a backend switch land here while the new session's fetch is in flight).
                    elif chunk:
                        self._input_tail = self._input_tail + chunk
                        # Restamp the hold: these are real keystrokes parked for the reactor,
                        # not a sequence caught mid-flight, so they must not look already
                        # expired to _input_tail_expired the moment the wait ends.
                        self._input_tail_at = time.monotonic()
                elif fd == master:
                    output = self._drain_child_output()
                    if output is not None:
                        self.last_child_output = time.monotonic()
                        self._feed_child_output(output)
                elif fd in background:
                    self._pump_background(background[fd])
        return "done"

    @staticmethod
    def _stdin_has_cancel(data: bytes) -> bool:
        """Whether *data* is a genuine cancel keystroke — a lone Esc or Ctrl-C — as
        opposed to an escape SEQUENCE (mouse report, focus event, arrow key, bracketed
        paste), every one of which also begins with ESC. With host mouse reporting on,
        a mere mouse move emits ``\\x1b[<…`` and must NOT be read as the user pressing
        Esc, or a fetch is cancelled the instant the pointer moves."""
        if b"\x03" in data:  # Ctrl-C
            return True
        return data == b"\x1b"  # a bare Esc, not the lead byte of a longer sequence

    def _shared_is_older_than_local(self, entry, agent, session_id: str) -> bool:
        """Whether the shared copy of ``entry`` has FEWER turns than the local copy of
        ``session_id`` — i.e. resuming it would hand back an older conversation than the
        user already has. Compares the manifest's recorded row count (cheap, no blob
        read) against the local transcript's. Returns False when either is unknown (an
        older manifest without the field, or no local transcript), so we never warn on a
        guess."""
        from agitrack.sessions import count_transcript_rows

        shared_rows = entry.manifest.get("transcript_rows")
        if not isinstance(shared_rows, int):
            return False
        raw = agent.export_session_raw(self.base_repo.repo, session_id)
        if not raw:
            return False
        return shared_rows < count_transcript_rows(raw)

    def _resume_shared_session_menu(self) -> str:
        store = self._shared_store()
        completed = self._fetch_shared_with_cancel(store, "Fetching shared sessions…")
        if not completed:
            # The user stopped the fetch: leave the menu entirely rather than
            # dropping them into a possibly-stale, previously-fetched list (which
            # would read as if the stop did nothing). _fetch_shared_with_cancel has
            # already shown the "Stopped fetching…" notice; let it linger.
            return self._MENU_UP
        entries = store.entries()
        if not entries:
            self._set_message("No shared sessions found for this repo.")
            self._render()
            return self._MENU_UP
        options: list[str] = []
        for entry in entries:
            extra = [str(entry.manifest[k]) for k in ("model",) if entry.manifest.get(k)]
            if entry.manifest.get("updated"):
                extra.append(self._format_age(entry.manifest["updated"]))
            options.append(entry.display + (f"  ({' · '.join(extra)})" if extra else ""))
        choice = self._select_popup("Resume a shared session (newest first)", options)
        if choice is None:  # Esc → up one level to the sessions menu
            return self._MENU_UP
        entry = entries[options.index(choice)]
        session_id = entry.manifest.get("session_id")
        if not session_id:
            self._set_message("That shared session is incomplete; cannot resume it.")
            self._render()
            return self._MENU_UP
        # Resume with the backend the session was recorded by, not necessarily the
        # active one — a shared OpenCode session must be imported/resumed by the
        # OpenCode agent even while Claude is active (and vice versa). Reuse the
        # active agent when it already matches; only build a fresh one to cross
        # backends.
        entry_backend = entry.manifest.get("backend") or self.backend.name
        if entry_backend == self.backend.name:
            agent = self.backend
        else:
            try:
                agent = make_proxy_agent(entry_backend)
            except ValueError:
                self._set_message(f"Can't resume '{entry.display}': unknown backend '{entry_backend}'.", seconds=8.0)
                self._render()
                return self._MENU_UP
        # Remember the lineage origin this session was shared under (owner + name +
        # contributors), so a later re-share (on this or any machine) updates the SAME
        # shared entry and adds us to the contributor set, instead of spawning a new
        # `<sharer>/<name>` that never converges (#55).
        self._user_state().set_shared_origin(
            session_id, owner=entry.github_id, name=entry.name, contributors=entry.contributors
        )
        # Everything below resolves WHAT to do (interactive popups) WITHOUT touching
        # the transcript — the transcript fetch (which may hit the network) and the
        # import then run on a worker thread (_begin_shared_resume) so the UI never
        # freezes, and the resume itself completes on the main loop once ready.
        live_index = next(
            (
                i
                for i, s in enumerate(self.sessions)
                if getattr(getattr(s, "state", None), "backend_session_id", None) == session_id
            ),
            None,
        )
        if live_index is not None:
            # This exact conversation is already running here.
            keep_both_id = getattr(agent, "new_import_id", lambda: None)()
            # Guard against silently downgrading: if the shared copy is OLDER than the
            # running session (it doesn't have your latest turns), lead with keeping
            # the newer one so "update" can't quietly throw away recent work.
            shared_older = self._shared_is_older_than_local(entry, agent, session_id)
            if shared_older:
                header = (
                    f"'{entry.display}' is already running here, and the shared copy is OLDER than "
                    f"your current session — it doesn't include your latest changes. What would you like to do?"
                )
                opts = ["Stay as it is (keep my newer session)"]
                if keep_both_id:
                    opts.append("Keep both — copy the older shared version to a new session")
                opts.append("Update anyway (replace my session with the older shared copy)")
            else:
                header = f"'{entry.display}' is already running here.\nWhat would you like to do?"
                opts = ["Update this session to the shared version"]
                if keep_both_id:
                    opts.append("Keep both — copy the shared version to a new session")
                opts.append("Stay as it is (no change)")
            pick = self._select_popup(header, opts)
            if pick is None:  # Esc → up one level to the sessions menu
                return self._MENU_UP
            if pick.startswith("Stay"):
                self._switch_active(live_index)
                return self._MENU_DONE
            if pick.startswith("Update"):
                # Pull the shared version into the running session: fetch it, then
                # (on the main loop) restart the backend so it loads the new
                # transcript — the agent can't pick up a swapped transcript live.
                self._begin_shared_resume(
                    store,
                    entry,
                    agent,
                    action="update_live",
                    name=None,
                    resume_id=session_id,
                    overwrite=True,
                    as_id=None,
                    backend=entry_backend,
                )
                return self._MENU_DONE
            assert keep_both_id is not None
            copy_name = self._prompt_session_name(
                "Name the copied session", default=self._dedupe_session_name(entry.name)
            )
            if copy_name is None:  # Esc → up one level
                return self._MENU_UP
            self._begin_shared_resume(
                store,
                entry,
                agent,
                action="new",
                name=copy_name,
                resume_id=keep_both_id,
                overwrite=False,
                as_id=keep_both_id,
                backend=entry_backend,
            )
            return self._MENU_DONE
        # You may already have this exact shared session open under a DIFFERENT backend
        # id: a multi-collaborator entry carries the *last sharer's* session_id, not
        # yours, so the id check above misses your own copy and the resume would mint a
        # new, differently-named session (the "session name got lost" report). Match by
        # the shared LINEAGE (origin owner + name) instead and offer to continue your
        # existing session — keeping its name — rather than duplicating it.
        lineage_index = self._live_session_for_lineage(entry.github_id, entry.name)
        if lineage_index is not None:
            local_name = self._session_name(lineage_index)
            keep_both_id = getattr(agent, "new_import_id", lambda: None)()
            opts = [f"Continue my existing '{local_name}' session"]
            if keep_both_id:
                opts.append("Fetch the shared version as a separate copy")
            pick = self._select_popup(
                f"You already have this shared session open locally as '{local_name}'.\nWhat would you like to do?",
                opts,
            )
            if pick is None:  # Esc → up one level to the sessions menu
                return self._MENU_UP
            if pick.startswith("Continue"):
                self._switch_active(lineage_index)
                return self._MENU_DONE
            assert keep_both_id is not None
            copy_name = self._prompt_session_name(
                "Name the copied session", default=self._dedupe_session_name(entry.name)
            )
            if copy_name is None:  # Esc → up one level
                return self._MENU_UP
            self._begin_shared_resume(
                store,
                entry,
                agent,
                action="new",
                name=copy_name,
                resume_id=keep_both_id,
                overwrite=False,
                as_id=keep_both_id,
                backend=entry_backend,
            )
            return self._MENU_DONE
        # Not running locally: pick a clear local name (#71), default to the original
        # share name (deduped) — NOT a "<sharer>-<name>" slug, which grew without
        # bound when sharing back and forth (#55).
        name = self._prompt_session_name("Resume shared session", default=self._dedupe_session_name(entry.name))
        if name is None:  # Esc → up one level to the sessions menu
            return self._MENU_UP
        overwrite, as_id, resume_id = False, None, session_id
        if agent.has_local_session(self.base_repo.repo, session_id):
            age = self._format_age(entry.manifest["updated"]) if entry.manifest.get("updated") else "earlier"
            keep_both_id = getattr(agent, "new_import_id", lambda: None)()
            # If the shared copy is OLDER than the local one, default to keeping the
            # local (newer) copy so the user can't unknowingly replace recent work with
            # a stale shared version (the "much older after resume" report).
            shared_older = self._shared_is_older_than_local(entry, agent, session_id)
            if shared_older:
                header = (
                    f"You already have a local copy of {entry.display}, and it's NEWER than the shared "
                    f"version — the shared copy is missing your latest changes. What do you want to do?"
                )
                opts = ["Keep my local copy (the newer one)"]
                if keep_both_id:
                    opts.append("Keep both (fetch the older shared copy as a separate session)")
                opts.append(f"Replace my local copy with the OLDER shared version (updated {age})")
            else:
                header = f"You already have a local copy of {entry.display}.\nWhich do you want to continue?"
                opts = [f"Replace my local copy with the shared version (updated {age})"]
                if keep_both_id:
                    opts.append("Keep both (fetch the shared copy as a separate session)")
                opts.append("Keep my local copy")
            pick = self._select_popup(header, opts)
            if pick is None:  # Esc → up one level to the sessions menu
                return self._MENU_UP
            if pick.startswith("Keep both"):
                assert keep_both_id is not None
                as_id = resume_id = keep_both_id
            elif pick.startswith("Replace"):
                overwrite = True
            else:  # keep the local copy: resume it directly, no fetch/import needed
                self._resume_conversation(name, session_id, backend=entry_backend)
                return self._MENU_DONE
        self._begin_shared_resume(
            store,
            entry,
            agent,
            action="new",
            name=name,
            resume_id=resume_id,
            overwrite=overwrite,
            as_id=as_id,
            backend=entry_backend,
        )
        return self._MENU_DONE

    def _begin_shared_resume(self, store, entry, agent, *, action, name, resume_id, overwrite, as_id, backend) -> None:
        # Fetch the (possibly large) transcript on a worker thread, then WAIT for it
        # cancellably: the UI keeps draining (no freeze) and the user can press Esc to
        # stop a slow fetch. The import + session switch/restart still happen on the
        # main loop (_service_shared_resume) once the result lands.
        if self._shared_resume_thread is not None and self._shared_resume_thread.is_alive():
            self._set_message("Already fetching a shared session — please wait.")
            self._render()
            return
        session_id = entry.manifest.get("session_id")
        self._shared_resume_result = None
        cancel = threading.Event()
        self._shared_resume_cancel = cancel

        def worker() -> None:
            try:
                # Bound the full fetch (it can be large) and make it killable, so a
                # cancel/exit terminates the git process at once instead of waiting.
                transcript = store.read_transcript(entry, timeout=self.RESUME_FETCH_TIMEOUT, cancel=cancel)
                if cancel.is_set():
                    return  # cancelled (or exiting) while fetching — drop the result
                if not transcript:
                    self._shared_resume_result = {"error": "incomplete"}
                    return
                self._shared_resume_result = {
                    "transcript": transcript,
                    "action": action,
                    "agent": agent,
                    "session_id": session_id,
                    "name": name,
                    "resume_id": resume_id,
                    "overwrite": overwrite,
                    "as_id": as_id,
                    "backend": backend,
                    "entry_name": entry.name,
                    # Lineage of the shared session being copied here, for the origin
                    # note the first commit of the new session records.
                    "origin_contributors": "+".join(entry.contributors),
                }
            except Exception as error:
                if not cancel.is_set():
                    self._shared_resume_result = {"error": repr(error)}

        self._set_message(f"Fetching '{entry.display}'…   press Esc to cancel", seconds=600)
        self._render()
        self._shared_resume_thread = threading.Thread(target=worker, daemon=True, name="agit-shared-resume")
        self._shared_resume_thread.start()
        status = self._drain_pty_until_done_or_esc(
            self._shared_resume_thread, deadline=time.monotonic() + self.RESUME_FETCH_TIMEOUT + 2.0
        )
        if status == "cancel":
            # The user pressed Esc: stop and reset all fetch state so a retry
            # can start immediately. This is the ONLY path that reports "cancelled".
            self._abort_shared_resume(cancel)
            self._set_message(f"Stopped fetching '{entry.display}' (cancelled).", seconds=6.0)
            self._render()
            return
        if status == "timeout":
            # Past the deadline with the worker still stuck (a stalled network its own
            # timeout didn't unwind in time): a FAILURE, not a user cancel. Say why and
            # hold the notice until the user acknowledges it.
            self._abort_shared_resume(cancel)
            self._await_keypress(
                f"Couldn't fetch '{entry.display}': the fetch timed out after "
                f"{int(self.RESUME_FETCH_TIMEOUT)}s. Press any key to continue."
            )
            return
        # The worker finished. If it failed, report WHY and keep the notice up until a
        # keypress — never let a generic auto-dismissing (or "cancelled") message stand
        # in for a real failure the user needs to see.
        result = self._shared_resume_result
        if result is not None and "error" in result:
            self._abort_shared_resume(cancel)
            reason = "the shared transcript is incomplete" if result["error"] == "incomplete" else result["error"]
            self._await_keypress(f"Couldn't fetch '{entry.display}': {reason}. Press any key to continue.")
            return
        # Success: the import + session switch run on the main loop (_service_shared_resume).

    def _abort_shared_resume(self, cancel: "threading.Event") -> None:
        # Stop the in-flight transcript fetch and clear ALL resume state so the user
        # can retry at once. Setting *cancel* makes the (daemon) worker drop any late
        # result and the bounded git fetch self-terminate; nulling the shared cancel
        # token clears the "fetch in progress" flag so nothing lingers to block or
        # mis-handle an immediate retry. The next fetch installs a fresh token.
        cancel.set()
        self._shared_resume_result = None
        self._shared_resume_thread = None
        self._shared_resume_cancel = None

    def _await_keypress(self, message: str) -> None:
        """Show *message* and block — keeping the PTYs draining so the screen stays
        live — until the user presses any key, so a failure notice can't scroll past
        unseen. Headless/non-interactive callers (no real stdin) just set the message
        and return, since there is no key to wait on."""
        self._set_message(message, seconds=3600)
        self._render()
        try:
            stdin_fd = self._stdin_fileno()
        except (OSError, ValueError):
            return
        while self.running:
            master = self.master_fd
            background = self._background_fds() if self.sessions else {}
            fds = [fd for fd in [stdin_fd, master] if fd is not None]
            fds.extend(background)
            try:
                readable, _, _ = select.select(fds, [], [], 0.2)
            except (OSError, ValueError):
                return
            for fd in readable:
                if fd == stdin_fd:
                    if self._is_real_keypress(self._read_stdin(32)):  # a key (not a mouse move) dismisses
                        return
                elif fd == master:
                    output = self._drain_child_output()
                    if output is not None:
                        self.last_child_output = time.monotonic()
                        self._feed_child_output(output)
                elif fd in background:
                    self._pump_background(background[fd])

    def _cancel_inflight_shared_fetches(self) -> None:
        # Stop any in-flight shared-session fetch immediately (used on exit): signal
        # the cancel token so a still-running worker drops its result and never
        # triggers a late session switch. The bounded git fetch self-terminates, and
        # the daemon thread dies with the process. Best-effort and idempotent.
        if self._shared_resume_cancel is not None:
            self._shared_resume_cancel.set()
        self._shared_resume_result = None

    def _service_shared_resume(self) -> None:
        result = self._shared_resume_result
        if result is None:
            return
        # A cancelled/abandoned fetch must never complete a switch: drop a late result
        # when there is no active fetch (token cleared by _abort_shared_resume) or its
        # token is set (the user stopped it, or aGiTrack is exiting).
        cancel = self._shared_resume_cancel
        if cancel is None or cancel.is_set():
            self._shared_resume_result = None
            self._shared_resume_thread = None
            return
        if self._shared_resume_thread is not None and self._shared_resume_thread.is_alive():
            return  # still fetching
        self._shared_resume_result = None
        self._shared_resume_thread = None
        self._shared_resume_cancel = None  # fetch concluded — no token lingers to block a retry
        if result.get("error") == "incomplete":
            self._await_keypress("That shared session is incomplete; cannot resume it. Press any key to continue.")
            return
        if "error" in result:
            self._await_keypress(f"Could not fetch the shared session: {result['error']}. Press any key to continue.")
            return
        if result["action"] == "update_live":
            self._complete_live_shared_update(result)
            return
        # A new (or copied) session: import the transcript and resume it.
        agent, sid = result["agent"], result["session_id"]
        if not agent.import_shared_session(
            self.base_repo.repo, sid, result["transcript"], overwrite=result["overwrite"], as_id=result["as_id"]
        ):
            self._set_message("Could not install the shared session for resume.", seconds=8.0)
            self._render()
            return
        # A "Keep both" fork (as_id set) deliberately starts a SEPARATE lineage: record
        # no origin for it, so sharing it later publishes a new `<you>/<name>` entry of
        # its own rather than updating the session it was copied from (#55).
        live_before = {getattr(getattr(s, "state", None), "backend_session_id", None) for s in self.sessions}
        self._resume_conversation(result["name"], result["resume_id"], backend=result["backend"])
        state = self.state
        if (
            state is not None
            and result["resume_id"] not in live_before
            and state.backend_session_id == result["resume_id"]
        ):
            # A genuinely new local session copied from a collaborator's shared one
            # (not a switch to an already-live session): record the copy so its first
            # commit notes the context/tokens inherited from the shared conversation.
            state.set_session_origin_event(
                kind="copy",
                source=result.get("session_id") or result["resume_id"],
                source_name=result.get("entry_name"),
                collaborator=result.get("origin_contributors"),
            )

    def _complete_live_shared_update(self, result: dict) -> None:
        # Update the already-running session to the shared version: switch to it,
        # overwrite its worktree transcript, then restart the backend so it loads
        # the new content (a live agent won't pick up a transcript swapped under it).
        agent, sid = result["agent"], result["session_id"]
        idx = next(
            (
                i
                for i, s in enumerate(self.sessions)
                if getattr(getattr(s, "state", None), "backend_session_id", None) == sid
            ),
            None,
        )
        if idx is None:
            # It stopped being live while fetching — fall back to a fresh resume.
            agent.import_shared_session(self.base_repo.repo, sid, result["transcript"], overwrite=True)
            self._resume_conversation(result["entry_name"], sid, backend=result["backend"])
            return
        self._switch_active(idx)
        if not agent.import_shared_session(self.repo.repo, sid, result["transcript"], overwrite=True):
            self._set_message("Could not update the session from the shared version.", seconds=8.0)
            self._render()
            return
        self._restart_agent("Updated this session to the shared version.")
