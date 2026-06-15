# aGiT menu & messaging workflow audit

Purpose: enumerate every interactive user workflow reachable from aGiT's menus and
commands, and record for each one whether it (a) can block the reactor/main thread on
a slow operation (network/git/`gh`), and (b) gives the user appropriate feedback
(progress notice, result, cancellation). Use this as the checklist when adding or
reviewing menu actions so the "no message / frozen terminal" class of bug doesn't
recur.

## Threading model (how to stay non-blocking)

The reactor runs one loop on the main thread (`_loop` → phases). Anything it calls
synchronously blocks input to the backend until it returns. There are **three**
sanctioned ways to run a slow operation:

1. **Foreground, cancellable** — `_publish_with_cancel` / `_fetch_shared_with_cancel`
   / `_begin_shared_resume`. Runs the op on a worker thread and `_drain_pty_until_done_or_esc`
   keeps the screen painting + lets the user press **Esc** to cancel. Use when the
   user is *waiting on a result they need next* (share push, resume fetch). The UI is
   alive but the user can't type to the backend until it finishes/cancels.
2. **Background, fire-and-forget** — `_run_share_op_async` + `_service_background_share_ops`.
   Runs on a daemon thread; shows a progress notice now and a result notice when done.
   The user **keeps working**. Use for one-way ops with no follow-up (unshare).
3. **Background daemon + main-loop service** — auto-share (`_maybe_auto_share_active`
   → `_auto_share_worker`, consumed by a `_service_*` tick). Use for event-driven
   background syncs (auto-share on commit).

Never call a network op (`store.fetch/publish/unshare`, `push_ref`, `fetch_ref`,
`github_login`/`gh`) directly on the reactor thread without one of the above.

## Messaging rules

- Every menu action ends in **exactly one** user-visible outcome: a result `_set_message`,
  a per-session notice, or a `_await_keypress` (for failures the user must see).
- Cancelling a popup (`_select_popup` → `None`) shows `"Cancelled."`.
- A slow op shows a **progress** message *before* starting and a **result** after.
- Failure notices that must be read use `_await_keypress` (dismiss on a real key, not a
  mouse move — see `_is_real_keypress`). Cancel detection uses `_stdin_has_cancel`
  (lone Esc / Ctrl-C only, never a mouse/arrow/focus escape sequence).

## Workflows

Legend — Blocking: `none` (in-memory), `local` (fast local git), `net` (network/gh).
Status: ✅ ok · ⚠ minor risk · 🛠 fixed in this pass.

### Session menu (`Sessions`)

| Action | Does | Blocking | Feedback | Status |
|---|---|---|---|---|
| Switch session | `_switch_active` | local | repaint; integrates on re-pick | ✅ |
| Integrate current | `_integrate_active_session` | local | "Integrated…/nothing to integrate/conflict box" | ✅ |
| Resume a past conversation | `_resume_session_menu` | local (reads refs) | list, "Cancelled." | ✅ |
| Complete merge | `_finalize_agent_merge` | local | result message | ✅ |
| Resolve unmerged worktree | `_resolve_dormant_worktree` | local | options box + result | ✅ |
| Resume idle worktree | `_new_session(name)` | local | spawns, repaint | ✅ |
| New session | `_prompt_new_session` → `_new_session` | local | name prompt, repaint | ✅ |
| Rename a session | `_rename_session_menu` | none/local | prompt + result | ✅ |
| **Share this session** | `_share_session` → `_publish_with_cancel` | net | consent (every time) → "pushing…  Esc to cancel" → result / "NOT shared" (keypress) | ✅ |
| **Resume a shared session** | `_resume_shared_session_menu` → `_begin_shared_resume` | net | "Fetching…  Esc" listing + full fetch; failure persists to keypress; always re-syncs latest | ✅ |
| **Manage shared sessions** | `_manage_shared_sessions_menu` | net (`gh` login only) | opens from local ref (no fetch); see below | ⚠ |
| Stop a session | `_stop_session_menu` | local | options + result | ✅ |

### Manage shared sessions → per-entry (`Manage <id>/<name>`)

| Action | Does | Blocking | Feedback | Status |
|---|---|---|---|---|
| Update now | `_update_shared_entry` → `_publish_with_cancel` | net | "Updating… pushing…  Esc" → result / "NOT updated" (keypress) | 🛠 (earlier) |
| Turn ON/OFF auto-update | `_set_session_auto_share` (+ update on enable) | local/net | toggle message; enable syncs via the same cancellable push | ✅ |
| **Unshare** | `_unshare_entry` → `_run_share_op_async` | net (background) | "Unsharing… removing from origin…" now, result notice when done; **session stays usable** | 🛠 (this pass) |

### Top-level commands (menu / `Ctrl-G` palette)

| Command | Does | Blocking | Feedback | Status |
|---|---|---|---|---|
| `update` | `_handle_update_command` → `_apply_update_and_restart` | net check is backgrounded; apply blocks intentionally (then re-exec) | status popup; "Updating… / Finishing commits, then restarting…"; loop breaks cleanly after teardown | ✅ |
| `backend <name>` | switch backend | local | picker + result | ✅ |
| `summarizer on/off` | `_handle_summarizer_command` | none | result | ✅ |
| base branch switch | `_select_popup` + git switch | local | confirm + result | ✅ |
| `exit`/`quit`, Ctrl-C | `_run_exit_flow` → `_finalize_pending_work` | local (commit/merge) | "Finalizing commits before exit…"; loop breaks before phase 4 | ✅ |

## Known residual risks (⚠) — candidates for follow-up

- **`gh` login resolution on the reactor thread.** `_manage_shared_sessions_menu`
  (`_cached_or_resolve_login`) and `_share_payload` (`github_login`) resolve the GitHub
  login synchronously when it isn't cached. Mitigated by `_warm_share_login` at startup
  (a daemon thread that caches it before the user reaches these menus) and by `gh`'s own
  timeout, so in practice it's warm. If a freeze is ever observed opening the manage
  menu offline, move the lookup off the reactor (cache-or-background, like the dashboard's
  `cached_logins`).
- **Manual share's transcript read + redact** runs on the reactor thread in
  `_share_payload`. It's CPU/local (no network) and bounded by transcript size; only a
  pathologically large transcript would be noticeable.

## Invariants to keep (so this stays true)

1. A new menu action that touches the network MUST use mechanism 1, 2, or 3 above.
2. Show a progress message before any op that can take >~200ms, and a result after.
3. Reuse `_run_share_op_async` for fire-and-forget network ops; add a `_service_*`
   consumer if you introduce a new background result type, and call it from
   `_reactor_timers_phase` (after the `if not self.running: return` guard).
4. Keypress-dismissed notices use `_await_keypress`; cancellable waits use
   `_drain_pty_until_done_or_esc` — both already ignore mouse/scroll input.
