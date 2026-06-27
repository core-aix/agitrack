# User-flow test matrix

The authoritative, auditable index of aGiTrack's user-interaction flows and the tests that cover
them. It exists so completeness is **verifiable** instead of assumed: every interactive sequence a
user can drive is listed here with the test(s) that exercise it, so a reviewer can confirm at a
glance that nothing is untested.

**Rule (enforced by AGENTS.md): any change that adds or alters a user flow MUST add/extend the
covering test AND update this matrix in the same change.** A new menu action, prompt, decision
branch, or exit/copy/commit/switch behavior is not "done" until it appears here with a test.

Conventions:
- **real-git** = the test runs against a real temporary git repo (catches real `git` failures, e.g.
  the Windows cp1252 commit-encoding bug). **mock** = the git layer is stubbed (faster, but cannot
  catch real-git bugs — prefer real-git for anything touching commit/merge/worktree).
- Tests live in `tests/`. Names are unique across the suite; grep for them.
- All rows run on **every OS** unless marked *(posix-only)* with a reason.

---

## 1. Startup & session restore
| Sequence | Test(s) | Kind |
|---|---|---|
| First run, no global backend → select/install backend | `test_select_default_backend_*` | mock |
| Resume last session by stored name (no prompt) | `test_startup_name_keeps_stored_name_without_prompting` | real-git |
| Unnamed session → prompt and record name | `test_startup_name_prompts_when_unnamed_and_records_it` | real-git |
| Default session name is a word, not `session-1` | `test_startup_default_name_is_a_word_not_session_1` | real-git |
| Recorded conversation is empty → drop it, start fresh | `test_baseline_drops_session_with_no_conversation` | mock |
| Resume stages transcript into the launch dir | `test_stage_backend_resume_retargets_cwd_to_launch_dir` | real-git |
| Dormant/stale worktree reconciliation | `test_reconcile_flags_conflicting_stale_worktree`, `test_recovery.py::*` | real-git |
| `--no-worktree`: new session runs on the base tree | `test_new_session_no_worktree_runs_in_base_dir_not_a_worktree` | real-git |
| `--no-worktree`: blank session starts a fresh conversation | `test_new_session_no_worktree_blank_starts_fresh_conversation` | real-git |

## 2. Prompt submission (pre-agent commit)
| Sequence | Test(s) | Kind |
|---|---|---|
| Clean tree → prompt forwarded, prompt traced | `test_finish_agent_parse_commits_once_turn_is_complete` | real-git |
| Dirty worktree → reconcile transcript, then user-commit | `test_pre_agent_commit_*` (test_proxy), `test_turn_copy_offer_defers_user_commit_prompt` | mock |
| Base-repo user edits committed + merged before the agent | `test_base_user_edit_declined_then_restaged_is_not_stranded`, `test_base_user_untracked_file_counts_as_pending` | real-git |
| Submit while agent active → prompt held as follow-up | `test_await_followup_appends_normalized`, `test_await_followup_skips_empty/slash_commands` | mock |

## 3. Agent turn lifecycle (commit + attribution)
| Sequence | Test(s) | Kind |
|---|---|---|
| Complete turn with staged changes → real commit | `test_agent_turn_commit_lands_in_real_git_with_unicode_trace` | **real-git** |
| Commit message carries box-drawing/emoji trace (Windows cp1252 bug) | `test_agent_turn_commit_lands_in_real_git_with_unicode_trace`, `test_git_commit_encoding.py::*` | **real-git** |
| Nothing staged → no commit | `test_agent_commit_is_skipped_when_nothing_is_staged`, `test_commit_turns_returns_false_when_nothing_staged` | real-git / mock |
| Empty turn list → no commit | `test_commit_turns_returns_false_for_empty_turns` | mock |
| Subject joins multiple prompts with `/` | `test_commit_turns_subject_joins_multiple_prompts`, `test_agent_commit_subject_joins_all_prompts_with_slash` | mock |
| Trace records final reply only (default) / all messages (opt-in) | `test_commit_turns_records_only_final_agent_message_by_default`, `test_commit_turns_records_all_agent_messages_when_option_on` | mock |
| Failed attempt does not double-count tokens | `test_agent_commit_failed_attempt_does_not_double_count_tokens` | mock |
| Backend-made commits → cover commit (hashes preserved) | `test_clean_tree_covers_backend_commits_without_rewriting_them`, `test_cover_commit_*` | real-git |
| Token usage / reasoning effort / compactions recorded | `test_commit_turns_records_latest_reasoning_effort`, `test_commit_turns_surfaces_compactions_and_clears_origin_event` | mock |

## 4. Interruption & follow-ups (timing)
| Sequence | Test(s) | Kind |
|---|---|---|
| Interrupted (Esc) turn, no final response → cancellation handler, not a commit | `test_interrupted_turn_routes_to_cancellation_handler_not_a_commit`, `test_finish_agent_parse_interrupt_clears_awaited_followups` | real-git / mock |
| Interrupted turn that left a dangling response still commits | `test_finish_agent_parse_interrupted_dangling_turn_still_commits` | mock |
| Cancelled-turn handler "keep" does not advance watermark | `test_finish_parse_cancel_handler_keep_does_not_advance_watermark` | mock |
| Cancelled turn with no changes → no prompt | `test_handle_cancelled_turn_no_changes_does_not_prompt` | mock |
| Follow-up queued before its turn lands → commit deferred | `test_followup_queued_before_its_turn_lands_defers_the_commit`, `test_finish_agent_parse_defers_for_queued_followup_not_in_transcript` | real-git / mock |
| Follow-up landed → both prompts in one commit | `test_followup_that_landed_is_committed_with_its_turn`, `test_finish_agent_parse_commits_both_turns_once_followup_lands` | **real-git** / mock |
| Incomplete (mid-tool-call) latest turn → deferred | `test_incomplete_latest_turn_defers_until_it_finishes`, `test_finish_agent_parse_defers_commit_while_turn_in_progress` | real-git / mock |
| Cancelled follow-up does not block the commit forever | `test_finish_agent_parse_does_not_block_on_cancelled_followup` | mock |
| In-progress turn force-committed on exit | `test_finish_agent_parse_forces_in_progress_commit_on_exit` | mock |

## 5. Copy-back (worktree leftovers → base directory)
| Sequence | Test(s) | Kind |
|---|---|---|
| Untracked + git-ignored copied; hidden/scaffolding skipped | `test_copies_untracked_and_ignored_skips_hidden_and_scaffolding`, `test_offer_copy_includes_git_ignored_files` | real-git |
| Decline → files left, set muted until it changes | `test_offer_copy_unstaged_declined_leaves_files_and_notifies`, `test_offer_copy_decline_notice_warns_worktree_is_removed` | mock |
| New file re-opens the muted set (turn + exit) | `test_offer_copy_decline_mutes_same_set_reasks_on_new_file`, `test_offer_copy_on_exit_respects_mute_unless_new_file` | mock |
| Esc on the exit copy offer aborts the exit (stays running) | `test_offer_copy_on_exit_esc_aborts_exit`, `test_popup_exit_flow_aborted_by_esc_on_copy_offer_stays_running` | mock |
| Overwrite conflict: all / each / decline-keep-base | `test_offer_copy_unstaged_overwrite_all_prompts_once`, `_confirm_each_one`, `_declined_keeps_base`, `test_overwrite_is_confirmed_before_replacing_base_files` | real-git / mock |
| Offer user-commit for edits before copy (switch/exit only) | `test_copy_offer_offers_user_commit_for_edits_on_switch`, `test_copy_offer_skips_user_commit_when_no_edits` | mock |
| Per-turn offer defers the user-commit prompt to the worker | `test_turn_copy_offer_defers_user_commit_prompt` | mock |
| aGiTrack's own copy does NOT trigger "Agent edited base repo" | `test_rebaseline_base_edits_absorbs_agitracks_own_copy` | mock |
| Agent edits the base repo directly (un-sandboxed) → warns once, then rebaselines | `test_warn_if_base_edited_fires_then_rebaselines_and_noop_when_off` | mock |

## 6. User commit (worktree / base edits)
| Sequence | Test(s) | Kind |
|---|---|---|
| Empty message re-prompts until non-empty | `test_create_user_commit_terminal_retries_until_non_empty`, `test_create_user_commit_ui_empty_then_valid` | mock |
| Esc / cancel → no commit | `test_create_user_commit_ui_cancel_returns_false` | mock |
| Nothing staged → silent no-op | `test_create_user_commit_no_staged_silent` | mock |
| **Commit failure (hook/config) → surfaced, no crash, changes kept** | `test_user_commit_popup_surfaces_failure_without_crashing` (mock) + `test_commit_raises_catchable_giterror_on_failing_pre_commit_hook` (**real-git**, real pre-commit hook) | mock + real-git |

## 7. Switching sessions
| Sequence | Test(s) | Kind |
|---|---|---|
| Swap session state (pointer + per-session fields) | `test_switch_active_swaps_session_state` | mock |
| Join parse worker before swapping | `test_switch_active_joins_worker_before_swapping` | mock |
| **Resume in place — never interrupt the target backend** | `test_switch_active_resumes_in_place_without_interrupting_target` | mock |
| Reconcile transcript (bg parse) before the switch copy/commit offer | `test_deferred_switch_offer_reconciles_transcript_before_offering` | mock |
| Select current session → integrate / "already here" | `test_session_switch_prompt_keeps_or_switches_active_session`, `test_session_menu_explicit_integrate_choice_integrates` | mock |
| Typed `sessions <n>` jumps to a session; `sessions new` prompts; bare opens the menu | `test_handle_session_command_numeric_switches_new_prompts_blank_opens_menu` | mock |
| Stop a session (menu pick / Esc-back / can't-stop-the-only-one) | `test_stop_session_drops_it_keeps_others_and_refuses_the_last`, `test_stop_session_menu_routes_choice_and_esc_backs_out` | mock |

## 8. Backend switch
| Sequence | Test(s) | Kind |
|---|---|---|
| Switch to a live session of that backend (no respawn) | `test_switch_backend_switches_to_live_session_without_teardown` | mock |
| No live session → create per-backend session (prompt name) | `test_switch_backend_creates_per_backend_session_when_none_live` | mock |
| Resume that backend's stored conversation | `test_switch_backend_resumes_stored_session` | mock |
| Same backend → no-op | `test_switch_backend_noop_when_same_backend` | mock |
| Choice is repo-scoped, not global | `test_switch_backend_records_choice_repo_scoped_not_global` | mock |
| aGiTrack system note passed to Claude, not OpenCode (by design) | `test_claude_proxy_agent_spawn_command_*`, `test_opencode_proxy_agent_spawn_command_has_no_system_prompt_append` | mock |
| `agent-backend` already-set / unknown-backend; unknown Ctrl-G command | `test_run_command_agent_backend_already_set_and_unknown_command` | mock |

## 9. Background sessions
| Sequence | Test(s) | Kind |
|---|---|---|
| Idle background session auto-integrates | `test_service_background_integrates_idle_session_cleanly`, `_even_when_not_in_flight` | real-git |
| Background conflict → switch to foreground + resolve prompt | `test_service_background_conflict_switches_and_prompts` | real-git |
| Integration deferred while its summary is pending | `test_background_integration_defers_while_its_summary_is_pending` | mock |
| **Background backend exits → relaunch+resume; crash-loop → drop** | `test_background_session_relaunches_on_unexpected_exit_then_stops_after_crashloop` | mock |
| Skip background git while an active merge is in progress | `test_service_background_skips_while_active_merge_in_progress` | mock |

## 10. Integration / merge / conflict
| Sequence | Test(s) | Kind |
|---|---|---|
| Committed-but-unmerged work integrates into base | `test_committed_but_unmerged_work_is_integrated` | real-git |
| Conflict → abort + resolve-options prompt | `test_integrate_conflict_aborts_and_prompts_resolve_options`, `test_integrate_conflict_prompts_then_starts_agent_merge` | real-git |
| Conflict "leave for later" keeps work unintegrated | `test_integrate_conflict_leave_for_later_keeps_work_unintegrated` | real-git |
| Conflict on exit → left for next startup | `test_integrate_conflict_on_exit_leaves_for_startup` | real-git |
| `--delay-merge`: defer until explicit menu choice | `test_delay_merge_defers_integration_and_names_working_dir`, `test_delay_merge_menu_choice_integrates`, `test_delay_merge_off_integrates_immediately` | real-git / mock |
| Resolve-conflict dispatch (auto / manual / leave) | `test_prompt_resolve_conflict_dispatches_auto/manual`, `_leave_does_not_merge` | mock |
| Idle worktrees re-sync onto advanced base | `test_switch_all_idle_sessions_skips_running_ones`, `test_align_session_to_base_skips_conflicting_base` | real-git |
| "Integrate this session" refused mid-turn / no-worktree guard | `test_integrate_active_session_refuses_mid_turn_and_without_worktree` | mock |

## 11. Exit flow
| Sequence | Test(s) | Kind |
|---|---|---|
| Always confirm, even with nothing pending | `test_exit_always_confirms_even_when_nothing_pending` | mock |
| Confirm declined → keep running | `test_exit_confirm_declined_keeps_running` | mock |
| Background sessions running → second confirm names them | `test_confirm_terminate_background_sessions_prompts_and_names_them`, `_no_prompt_when_all_idle` | mock |
| **Esc on ANY finalize popup (user-commit/copy/merge) → abort whole exit** | `test_esc_on_a_popup_during_exit_finalize_aborts_the_whole_exit` | mock |
| Double-Ctrl-C → force exit but still finalize | `test_double_ctrl_c_finalizes_before_exiting` | mock |
| Ctrl-C inside a popup routes through the exit flow | `test_select_popup_ctrl_c_routes_through_exit_flow` | mock |
| Finalize commits the latest turn non-interactively | `test_finalize_pending_work_commits_non_interactively` | mock |
| Remove fully-merged worktree; keep unintegrated/unresolvable | `test_exit_removes_fully_merged_worktree`, `test_exit_keeps_worktree_with_unintegrated_commits`, `_when_base_ref_unresolvable` | real-git |
| Persist resume pointer (last active, even if not primary / worktree kept) | `test_exit_persists_resume_pointer_*` | mock |
| `exit`/`quit` command routes through the unified flow | `test_exit_command_routes_through_unified_exit_flow`, `_cancelled_does_not_request_exit` | mock |
| Signal teardown (terminal closed) keeps a worktree with leftover files | `test_handle_exit_signal_*` *(posix-only: SIGHUP/SIGTERM delivery)* | mock |

## 12. Session sharing
| Sequence | Test(s) | Kind |
|---|---|---|
| Share / list / read back | `test_share_lists_and_reads_back`, `test_share_runs_in_background_without_blocking` | real-git |
| Resume shared (fetch / already-live / name prompt / errors) | `test_shared_resume_*` | real-git |
| Share-behind → overwrite + reshare / cancel | `test_share_behind_offers_overwrite_and_reshares`, `_cancel_leaves_shared_copy_untouched` | real-git |
| Unshare (confirm, retries, fallbacks, lineage) | `test_unshare_*` | real-git |

## 13. Self-update
| Sequence | Test(s) | Kind |
|---|---|---|
| Source: detect/apply (clean / diverged / conflict / offline) | `test_source_check_*`, `test_source_apply_*` | real-git |
| Startup prompt (apply / default-enter / explicit-no / pending reminder) | `test_startup_prompt_*`, `test_startup_reminds_without_reprompting_when_pending` | mock |
| Apply failure records pending and keeps running | `test_startup_apply_failure_records_pending_and_keeps_running` | mock |
| Windows MSI: detect (frozen+registry) / check GitHub release / download / no-asset / api-error | `test_updater.py::test_install_method_msi_*`, `test_check_msi_*`, `test_apply_msi_*` | unit |
| Windows MSI: manual-instructions route (releases URL + SmartScreen) | `test_updater.py::test_manual_instructions_msi_route` | unit |
| Restart command shape (frozen exe vs `python -m agitrack`) — self-update **and** settings "restart now" | `test_updater.py::test_restart_command_*` | unit |

## 14. Windows-specific (#118)
| Sequence | Test(s) | Kind |
|---|---|---|
| Commit message UTF-8 (not cp1252) | `test_git_commit_encoding.py::*`, `test_agent_turn_commit_lands_in_real_git_with_unicode_trace` | **real-git** |
| Child subprocesses isolated from host console | `test_proc.py::test_console_isolation_kwargs_*` | unit |
| ConPTY spawn/read/exit | `test_windows_conpty.py::*` *(nt-only; strict stdout check skipped on constrained console hosts)* | real-proc |
| Color/host terminal modes | `test_backend_child_env_forces_color_on_windows_only`, `test_sync_terminal_modes_*` | mock |
| Diagnostic logs (DEBUG_PROXY/DEBUG_RAW) cross-platform | `test_debug_and_raw_logs_write_to_base_repo_when_enabled` | mock |

## 15. System prerequisites & installation (git, gh, identity, backend)
Flows that run on an interactive launch when a required tool, config, or login is missing.
| Sequence | Test(s) | Kind |
|---|---|---|
| Missing **git** (required) → offer install, gate launch if declined | `test_maybe_install_tool_accepts_and_installs`, `test_maybe_install_tool_declined_returns_false` | mock |
| Missing **gh** (optional) → offer install, continue if declined | `test_gh_check_missing_does_not_offer_login`, `test_maybe_install_tool_*` | mock |
| **gh unauthenticated** → offer `gh auth login` / continue / quit | `test_gh_check_login_runs_gh_auth_login`, `test_gh_check_unauthenticated_continue`, `test_gh_check_quit_aborts_startup` | mock |
| gh already authed / no GitHub remote → silent | `test_gh_check_silent_when_authenticated`, `test_gh_check_silent_without_a_github_remote` | mock |
| Missing **git identity** (`user.name`/`user.email`) → prompt and set both | `test_ensure_git_identity_prompts_and_sets_both`, `test_ensure_git_identity_noop_when_already_set` | mock |
| Missing **backend CLI** → install / switch to installed / manual hint / gate | `test_ensure_installed_backend_returns_installed_backend`, `_switches_to_installed_alternative`, `_quit_raises`, `_is_a_gate_not_an_installer` | mock |
| Backend auto-install path (script / npm / winget bootstrap) | `test_install_backend_posix_prefers_official_script`, `_uses_npm_when_no_script_tools`, `_no_installer_available_returns_false` | mock |
| First-run backend selection (status shown, install one/all/skip) | `test_select_default_backend_*` | mock |
| Platform package manager chosen correctly (winget/brew/distro) | `test_can_install_tool_windows_uses_winget`, `_macos_uses_brew`, `_linux_uses_distro_manager` | mock |
| System-tool install runs the right command per OS | `test_install_system_tool_windows_runs_winget`, `_linux_uses_sudo_apt`, `_no_manager_returns_false`, `_nonzero_returncode_returns_false` | mock |
| Manual install hints cover all platforms | `test_git_install_hint_covers_all_platforms`, `test_gh_install_hint_covers_all_platforms`, `test_install_hint_claude_mentions_*`, `test_install_hint_opencode_mentions_*` | mock |
| Scripted / non-TTY run → never prompts | `test_maybe_install_tool_non_tty_returns_false`, `test_gh_check_non_interactive_does_not_prompt`, `test_ensure_installed_backend_non_interactive_raises` | mock |
| Custom launch command bypasses the install gate | `test_custom_launch_command_bypasses_install_gate` | mock |

---

## Known gaps / TODO
Track anything not yet covered here so it's explicit rather than silently missing. Add a row, then
remove it once a test lands.

Remaining from the 2026-06-27 self-audit — lower-risk message/guard branches, to be filled:
- `runner.py:_change_session_merge_branch_menu` — the "'X' is running a turn — change its merge branch when idle" refusal for an in-flight session (happy-path retarget IS tested).
- `runner.py:_rename_session` — the move-failure recovery ("Could not rename session…") and the "Name unchanged" no-op (collision path IS tested).
- `runner.py:_prompt_new_session` — the runtime fork-failure fallback ("Couldn't fork…; starting a blank one instead") (the capability-gate path IS tested).
- `runner.py:_run_command("git-user-commit")` — the "Committed your changes…" / "No changes to commit in the base repo." messaging wrapper (the underlying `_create_user_commit_popup` IS tested).
- mock-only → upgrade to real-git when convenient: `_present_copy_offer` per-file "confirm each" combined with a real `shutil.copy2` OSError branch; `_finalize_pending_work` multi-session loop where one background session's real commit/merge fails.

## How to extend (the rule, restated)
When you touch a user flow:
1. Add/extend the test (prefer **real-git** for commit/merge/worktree/copy paths).
2. Add or update the matching row above with the test name and kind.
3. If you couldn't cover something, add it to **Known gaps / TODO** rather than leaving it implicit.
