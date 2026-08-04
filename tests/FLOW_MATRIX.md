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
| Ctrl-Z suspends aGiTrack itself as a normal shell job (terminal handed back, SIGSTOP, then alt-screen/raw/mouse re-asserted and a repaint on `fg`) instead of being forwarded — Claude answered the raw byte by tearing its UI down into a blank, unrecoverable screen; Ctrl-\\ is swallowed (SIGQUIT would kill the backend mid-turn), and the backend's pty is spawned with ISIG/IXON off so NO forwarded byte can raise a signal on it or stall its output | `test_signal_control_bytes_never_reach_the_backend`, `test_backend_pty_cannot_be_wedged_by_forwarded_bytes` | mock |
| Typed TUI answers are editable in place: Left/Right (plus Home/End, Ctrl-A/E, Delete) move an insertion point and the caret renders at it, so a typo near the start no longer means retyping the line; the console user-commit prompt lists the files it is about to commit, as the TUI popup already did | `test_text_prompt_supports_cursor_editing`, `test_console_user_commit_prompt_lists_the_files` | mock / real-git |
| Option questions in the TUI are SELECTIONS, never typed text: the untracked-files prompt is up/down + Enter, so a stray keystroke or a misread question can no longer silently take a default (Esc still cancels) | `test_untracked_files_prompt_is_a_selection` | mock |
| A multi-line PASTE never answers aGiTrack's own UI: inside CSI 200~/201~ no byte acts as a key — a popup is not confirmed (its text is handed to the backend afterwards), a text prompt takes the paste as content without submitting, the palette types it without running a command, and a pasted \x03 starts no exit flow; state is seeded for a paste already in flight when the popup opened and survives markers split across reads | `test_pasted_newlines_never_answer_a_popup`, `test_bracketed_paste_state_survives_a_split_read`, `test_pasted_newlines_never_run_a_palette_command` | mock |
| State/marker/handshake saves survive CONCURRENT aGiTrack processes on one repo (unique tmp per write; the old fixed `<file>.tmp` crashed the interactive session mid-prompt when an export/daemon saved simultaneously) | `test_fileio.py::test_concurrent_writers_do_not_crash_or_corrupt`, `_creates_parents_and_replaces` | subprocess |

## 3. Agent turn lifecycle (commit + attribution)
| Sequence | Test(s) | Kind |
|---|---|---|
| Complete turn with staged changes → real commit | `test_agent_turn_commit_lands_in_real_git_with_unicode_trace` | **real-git** |
| Commit message carries box-drawing/emoji trace (Windows cp1252 bug) | `test_agent_turn_commit_lands_in_real_git_with_unicode_trace`, `test_git_commit_encoding.py::*` | **real-git** |
| Nothing staged → no commit | `test_agent_commit_is_skipped_when_nothing_is_staged`, `test_commit_turns_returns_false_when_nothing_staged` | real-git / mock |
| Empty turn list → no commit | `test_commit_turns_returns_false_for_empty_turns` | mock |
| Subject joins multiple prompts with `/` | `test_commit_turns_subject_joins_multiple_prompts`, `test_agent_commit_subject_joins_all_prompts_with_slash` | mock |
| **The subject really IS the first sentence in `git log --oneline`**: git's subject is the whole first PARAGRAPH (newlines folded to spaces), so the continuation glued under the first sentence was still part of it — a 4-sentence summary rendered a 286-character one-line entry. A blank line now separates subject from body (agent, user and summary-led messages alike), and applying a summary replaces the whole lead, not just up to the first blank | `test_agent_commit_subject_splits_at_first_sentence`, `test_user_commit_subject_splits_at_first_sentence`, `test_long_summary_first_line_splits_at_first_sentence`, `test_applying_a_summary_replaces_the_WHOLE_prompt_led_lead` | mock |
| Trace records final reply only (default) / all messages (opt-in) | `test_commit_turns_records_only_final_agent_message_by_default`, `test_commit_turns_records_all_agent_messages_when_option_on` | mock |
| A prompt the user REWOUND (edited and re-sent before the agent acted) is not part of the trace: its row stays in the transcript as an abandoned branch — a sibling off the same `parentUuid` with nothing descending from it — and was showing the discarded draft beside the real prompt; genuine mid-turn follow-ups (attachment rows) and conversation starts (no parent) are untouched | `test_parse_rows_drops_a_prompt_the_user_rewound_and_rewrote`, `test_parse_rows_keeps_prompts_that_only_look_like_siblings`, `test_parse_rows_still_keeps_a_genuine_mid_turn_followup_after_a_rewind` | mock |
| **Queued follow-up messages (sent mid-turn) captured as DISTINCT `## User` headings, no duplication, tokens not doubled** | `test_claude_session.py::test_parse_rows_captures_queued_followup_messages_in_the_turn`, `_ignores_non_human_or_slash_queued_attachments`, `test_commit_engine.py::test_queued_followups_render_as_separate_user_headings_without_duplication` | mock |
| Failed attempt does not double-count tokens | `test_agent_commit_failed_attempt_does_not_double_count_tokens` | mock |
| Only work made HERE is covered: commits that arrived by merge/pull/PR (the branch reflog shows them under `merge`/`pull`, not `commit:`) are never listed in `covered_commits` — a release PR and a teammate's PR had been attributed to an agent turn; a commit made locally on ANOTHER branch and merged in is foreign too | `test_commits_merged_in_from_another_branch_are_never_covered` | real-git |
| Backend-made commits → cover commit (hashes preserved) | `test_clean_tree_covers_backend_commits_without_rewriting_them`, `test_cover_commit_*` | real-git |
| Token usage / reasoning effort / compactions recorded | `test_commit_turns_records_latest_reasoning_effort`, `test_commit_turns_surfaces_compactions_and_clears_origin_event` | mock |
| A force-captured turn re-committed after finishing counts its tokens EXACTLY ONCE (partial recorded, re-commit adds the delta; floors at zero) — restarts mid-turn no longer inflate recorded tokens | `test_partial_turn_recommit_adds_only_the_token_delta`, `test_partial_turn_delta_never_goes_negative` | mock |
| A LOST watermark (compaction reshaped turn boundaries; the id matches no boundary) never re-exports the whole session: fall back to the recorded mark TIMESTAMP, or (legacy, no timestamp) the newest turn only; every advance records the timestamp | `test_lost_watermark_never_reexports_the_whole_session`, `test_watermark_advance_records_the_mark_timestamp` | mock |
| Second engine guard: a turn that ENDED at or before the committed frontier adds NO tokens, whatever path re-exported it (only the partial-delta continuation reaches back) | `test_turns_ended_before_the_frontier_are_never_recounted` | mock |
| The dashboard DETECTS and EXCLUDES abnormal commits at read time: a commit whose conversation span starts >24h behind its session's committed frontier (a lost-watermark re-export, incl. ones still written by pre-fix installs) has its tokens dropped from every total, keeps its lines, and carries a red "tokens excluded" badge; honest force-capture overlap and other sessions untouched; the anomalous span never advances the frontier | `test_token_anomaly_commits_are_excluded_from_totals`, `test_token_anomaly_badge_reaches_the_log` | mock |
| Input-includes-cache-write convention (issue #14) holds on EVERY surface: backtrace turn stats apply it, and the collector heals pre-#14 legacy blocks at read time (input can never show below cache_write) | `test_turn_tokens_apply_the_input_includes_cache_write_convention`, `test_parse_tokens_heals_legacy_raw_input_blocks` | mock |

## 4. Interruption & follow-ups (timing)
| Sequence | Test(s) | Kind |
|---|---|---|
| Interrupted (Esc) turn, no final response → cancellation handler, not a commit | `test_interrupted_turn_routes_to_cancellation_handler_not_a_commit`, `test_finish_agent_parse_interrupt_clears_awaited_followups` | real-git / mock |
| Interrupted turn that left a dangling response still commits | `test_finish_agent_parse_interrupted_dangling_turn_still_commits` | mock |
| Cancelled-turn handler "keep" does not advance watermark | `test_finish_parse_cancel_handler_keep_does_not_advance_watermark` | mock |
| **An interrupted turn's SUBJECT says so too** (`<aGiTrack> (interrupted) …`), applied by aGiTrack rather than left to the summarizer: given an accurate "the turn was interrupted" body, a live run still titled the commit "Session created placeholder text files r1.txt through r10.txt" over a two-file diff, and `git log --oneline` shows only that line. Survives the async summary amend (read back from the committed metadata) | `test_a_summarized_interrupted_commit_keeps_the_interrupted_mark_in_its_subject`, `test_an_uninterrupted_summarized_commit_has_no_such_mark` | mock |
| **An interrupted turn's commit SAYS it was interrupted** — a trace note plus `interrupted: true`, and the note also reaches the summarizer (the trace is its only input). Claude answers "I'll create the ten files now" *before* its first tool call, so the turn carries a final response and reads as complete: the commit then documented ten files over a two-file diff | `test_an_interrupted_turn_is_committed_as_interrupted_not_as_completed_work`, `test_the_summarizer_is_told_the_turn_was_interrupted`, `test_an_ordinary_turn_carries_no_interruption_note` | mock |
| **"Keep them (commit with your next turn)" is KEPT**: the kept changes stay the AGENT's until a commit claims them, so they are not re-offered as the user's own commit, not offered as copy-back leftovers ("intentionally unstaged or git-ignored"), and not excluded from the next turn's commit by the no-worktree "existed before this turn" rule. Committing or discarding them ends the ownership | `test_kept_cancelled_work_stays_the_agents_until_a_commit_claims_it`, `test_committing_the_kept_work_ends_the_agents_ownership`, `test_discarding_the_kept_work_also_ends_the_agents_ownership` | mock |
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
| **No AI turns → zero aGiTrack footprint** (empty trailer; hook appends nothing; commit stays plain/untracked) | `test_manual_trailer_with_no_pending_turns_is_empty_no_footprint`, `test_hook_leaves_commit_untouched_when_no_pending_turns`, `test_runner_git_commit_with_no_pending_turns_is_plain_user_commit` | real-git |

## 6a. Commit accounting when the AGENT commits itself
The agent running `git commit` mid-turn is ordinary, not exotic. Its commit is stamped with an
IN-FLIGHT block — attribution only, no trace, no tokens — whose text promises they land in a
later commit. These rows are that promise being kept, across every mode.

| Sequence | Test(s) | Kind |
|---|---|---|
| **Manual mode: a turn whose only action was the agent's own mid-turn commit is still RECORDED** — gate and record must agree, or the gate's widening is vetoed one step later and the tokens vanish | `test_manual_mode_records_a_turn_whose_only_action_was_its_own_midturn_commit`, `test_manual_daemon_records_a_turn_whose_only_action_was_its_own_midturn_commit` | real-git |
| …and the record guard still refuses a turn that genuinely changed nothing (else every poll chains an empty latent commit) | `test_the_record_guard_still_refuses_a_turn_that_genuinely_changed_nothing`, `test_the_daemons_record_guard_still_refuses_a_turn_that_changed_nothing` | real-git |
| A custom `core.hooksPath` is DETECTED from real git config (not a hand-set flag), the fold hooks are skipped, and poll+cover takes over | `test_setup_falls_back_to_poll_cover_under_a_real_core_hookspath`, `test_hooks_are_installed_when_no_custom_hookspath_is_set`, `test_core_hooks_path_is_detected_from_real_git_config`, `test_a_custom_hookspath_really_does_stop_our_hook_from_running` | real-git |
| `-b` autostart after a pre-commit sync resumes the mode the user last chose — auto as well as manual | `test_precommit_sync_autostart_resumes_auto_mode`, `test_precommit_sync_autostart_spawns_daemon` | real-git |

## 6b. Base-commit guard: keeping the agent out of the base repo (`tests/test_base_commit_guard.py`)
Worktree mode only. The agent works in a linked worktree and aGiTrack commits and merges for it;
an agent that commits into the BASE repo instead puts an untracked commit on the user's branch —
there is no fold hook there, and `_uncovered_backend_commits` only scans the session's managed
turn branch, so that commit is invisible to aGiTrack forever.

TWO hooks, because one of them is bypassable. `git commit --no-verify` skips `pre-commit` (git's
documented behaviour; no hook can change it), so that guard alone is advisory.
`reference-transaction` is NOT skipped and aborts the ref update itself, so the commit cannot
land. `pre-commit` is kept as the friendly first line: it fails earlier, with a better message,
and exists on git < 2.28. All real-git — a guard that only works in theory is worse than none,
because it is trusted.

| Sequence | Test(s) | Kind |
|---|---|---|
| The agent cannot commit into the base repo | `test_the_agent_cannot_commit_into_the_base_repo` | real-git |
| **…and `--no-verify` cannot get one past it either** (the reason the second hook exists) | `test_no_verify_cannot_get_a_commit_past_the_guard` | real-git |
| The USER is never blocked (the marker is on the agent's process only) | `test_the_user_is_never_blocked` | real-git |
| The agent commits freely inside its own worktree, with or without the bypass flag | `test_the_agent_commits_freely_inside_its_worktree`, `test_the_agent_commits_freely_in_its_worktree_even_with_no_verify` | real-git |
| **aGiTrack's own integration is never blocked** — it merges into the base branch from its own process, the exact transaction the guard aborts | `test_agitracks_own_integration_is_never_blocked`, `test_the_marker_is_only_ever_set_on_the_agent_child` | real-git |
| The ref hook fires on EVERY update, so: checkout, branch creation, tag, fetch and reads all still work under the guard | `test_checkout_still_works_under_the_guard`, `test_creating_a_branch_still_works_under_the_guard`, `test_tagging_still_works_under_the_guard`, `test_fetching_still_works_under_the_guard`, `test_reading_history_is_never_affected` | real-git |
| Both hooks install and remove together; a project's own hook is chained, receives its stdin, can still veto, and survives removal | `test_both_hooks_are_installed_and_removed_together`, `test_an_existing_project_reference_transaction_hook_is_chained_not_destroyed`, `test_a_chained_project_hook_still_runs_and_receives_its_stdin`, `test_a_chained_hook_can_still_veto`, `test_installing_twice_does_not_clobber_the_backup`, `test_removal_leaves_a_foreign_hook_untouched` | real-git |
| Neither hook names git's bypass flag (the refusal is only ever shown to the agent) | `test_the_guard_never_names_gits_bypass_flag` | mock |

## 6c. Repos that TRACK the agent scaffolding dirs (`.claude/`, `.opencode/`, `.agitrack/`)
Committing `.claude/settings.json` or a `.claude/commands/` dir is ordinary practice — a team
shares its agent setup like an editorconfig. But every "has the working tree changed?" question
in manual / no-worktree / background mode is a comparison between `snapshot_worktree_tree()` and
some commit's tree, and the snapshot deliberately STRIPS those dirs while a raw `^{tree}` keeps
them. The two could therefore never be equal, so all of those questions answered "dirty" forever
and each mode broke in a different direction. `GitRepo.comparable_tree` strips the same paths
from the commit side; a raw `^{tree}` reads as obviously correct, so these rows exist to stop it
being reintroduced. **Verified live** as well as in tests: pre-fix, a `-b` run in such a repo left
the agent's own commit carrying only an in-flight block with no cover ever arriving.

| Sequence | Test(s) | Kind |
|---|---|---|
| `comparable_tree` strips tracked scaffolding, is a no-op when none is tracked, and is idempotent on an already-stripped (latent) tree | `test_comparable_tree_strips_tracked_scaffolding_so_a_clean_tree_reads_as_clean`, `test_comparable_tree_is_a_no_op_when_no_scaffolding_is_tracked`, `test_comparable_tree_is_idempotent_on_an_already_stripped_tree` | real-git |
| **`-b`: the daemon still covers the agent's own commit** — `_agent_committed_own_work` bails on a dirty tree, so it covered NOTHING in such a repo: the exact loss the persistent watermark exists to prevent | `test_daemon_covers_an_agent_self_commit_in_a_repo_that_tracks_claude_config` | real-git |
| …and still records latently while the agent's edits ARE uncommitted (the strip must not make a dirty tree read clean), and still leaves no footprint for a pure-Q&A turn | `test_daemon_still_records_latently_while_the_agents_edits_are_uncommitted`, `test_daemon_does_not_cover_a_pure_qa_turn_in_a_scaffolded_repo` | real-git |
| `-m`: a turn that changed nothing records no latent commit; a real edit still does | `test_a_turn_that_changed_nothing_records_no_latent_commit_in_a_scaffolded_repo`, `test_a_real_edit_is_still_recorded_in_a_scaffolded_repo` | real-git |
| A turn that edited ONLY the tracked scaffolding records nothing — deliberate, since the snapshot cannot represent it and claiming it would bill a turn against no diff | `test_an_edit_to_the_tracked_scaffolding_itself_is_never_agent_work` | real-git |
| A stale chain is still reset, and an abandoned chain still pruned/trimmed (both halves of `prune_abandoned_refs` were dead here, so the whole feature was a no-op) | `test_a_stale_chain_is_still_reset_in_a_scaffolded_repo`, `test_an_abandoned_chain_is_still_pruned_in_a_scaffolded_repo`, `test_a_trailing_turn_matching_head_is_still_trimmed_in_a_scaffolded_repo` | real-git |
| A purely human commit made DURING an agent turn still gets no in-flight footprint | `test_a_human_commit_during_an_agent_turn_gets_no_footprint_in_a_scaffolded_repo` | real-git |
| The proxy's own `_manual_*` copy agrees with the tracker here too (interactive `-m` is the path users type) | `test_the_proxys_own_manual_copy_agrees_with_the_tracker_in_a_scaffolded_repo` | real-git |

### 6c-i. Guards that were exempt rather than defensive
| Sequence | Test(s) | Kind |
|---|---|---|
| `record()`'s "nothing new since the tip" guard also applies to an EMPTY chain (baseline HEAD) — it previously skipped that case entirely, so an ungated `record()` wrote a phantom first turn against an untouched tree | `test_a_turn_that_changed_nothing_records_no_latent_commit_in_a_scaffolded_repo` | real-git |
| …but never after a re-anchor past a `git gc --prune`d tip, where HEAD is a fallback parent rather than evidence nothing happened | `test_a_pruned_latent_object_does_not_kill_all_future_tracking` | real-git |
| The proxy's `_manual_record` survives a pruned latent object too — the re-anchor lived ONLY in the tracker, so the daemon coped and interactive `-m` raised on every turn for the rest of the session | `test_the_proxys_own_manual_copy_also_survives_a_pruned_latent_object` | real-git |
| The tail trim of `prune_abandoned_refs` is actually exercised: a trailing turn that left the code exactly as HEAD has it is dropped, and the turn that wrote code is kept. (The previous test recorded a "conversation-only" turn that `gate()` silently refused, so the chain never grew a tail and its `len <= before` assertion held no matter what.) | `test_a_trailing_turn_that_left_the_code_as_head_has_it_is_trimmed`, `test_the_trim_keeps_the_turn_that_actually_wrote_code`, `test_a_turn_that_only_talked_never_reaches_the_chain_at_all` | real-git |

## 7. Switching sessions
| Sequence | Test(s) | Kind |
|---|---|---|
| Swap session state (pointer + per-session fields) | `test_switch_active_swaps_session_state` | mock |
| Join parse worker before swapping | `test_switch_active_joins_worker_before_swapping` | mock |
| **Resume in place — never interrupt the target backend** | `test_switch_active_resumes_in_place_without_interrupting_target` | mock |
| Reconcile transcript (bg parse) before the switch copy/commit offer | `test_deferred_switch_offer_reconciles_transcript_before_offering` | mock |
| Select current session → integrate / "already here" | `test_session_switch_prompt_keeps_or_switches_active_session`, `test_session_menu_explicit_integrate_choice_integrates` | mock |
| Typed `sessions <n>` jumps to a session; `sessions new` prompts; bare opens the menu | `test_handle_session_command_numeric_switches_new_prompts_blank_opens_menu` | mock |
| **A new session inherits the RUN's backend** (not just the global default, which `agitrack --backend X` never recorded on a fresh repo — the flag only persisted when it DIFFERED from what the state already resolved to, and the state had just been seeded from that same flag). Without it `make_proxy_agent(self.state.backend)` raised "No coding agent backend is configured for this session" and crashed the `/clear` relocation MID-WAY: new worktree created, outgoing identity not yet restored, both worktrees left recording one conversation | `test_a_new_session_inherits_the_runs_backend_when_none_is_configured`, `test_a_new_worktree_session_inherits_the_runs_backend_too` | mock |
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

## 9a2. Background monitor ticks (deferred commits)
| Sequence | Test(s) | Kind |
|---|---|---|
| Monitor `<event>` notification opens a turn labeled `(background monitor update)`; terminal/unknown notifications keep `(background task completed)` | `test_parse_rows_monitor_event_notification_gets_the_update_label`, `test_parse_rows_background_task_work_opens_its_own_turn` | mock |
| TRIVIAL monitor-update-only completed turns (short ack, little output) are DEFERRED (no commit, watermark untouched) while the live loop runs | `test_finish_parse_defers_monitor_update_only_turns` | mock |
| A substantive turn commits the deferred ticks in the SAME commit; a monitor turn with a normal final message or heavy output commits immediately; exit finalize flushes tick-only sessions | `test_finish_parse_commits_monitor_updates_with_a_substantive_turn`, `test_finish_parse_commits_substantive_monitor_turn_immediately`, `test_finish_parse_commits_monitor_turn_with_heavy_output_despite_short_reply`, `test_finish_parse_exit_finalize_commits_monitor_update_only_turns` | mock |
| Summarizer refusals ("I don't have any coding session turns...") are unusable, falling back to the prompt-led subject | `test_summarizer_raises_on_refusal_text`, `test_summary_first_person_content_is_still_usable` | mock |
| A turn force-committed before the agent replied (crash/restart) re-exports when it CONTINUES, so its real final message and edits are committed rather than hidden behind the user-id watermark | `test_turns_after_re_exports_a_turn_that_continued_past_its_force_commit` | mock |
| "No response requested." (Claude's crash filler) never counts as a final message; the restarted process's real reply becomes the turn's final | `test_no_response_requested_filler_is_not_a_final_message` | mock |
| A dangling turn whose only reply is the crash filler stays in-flight (not complete-but-answerless) | `test_filler_only_turn_stays_incomplete` | mock |
| A prompt-only dangling turn (no assistant row yet) is in-flight; the live loop defers it instead of committing a user-message-only trace | `test_prompt_only_dangling_turn_is_in_flight`, `test_prompt_only_turn_defers_until_the_agent_answers` | mock |
| A bare "/compact" user row (recorded without the command-name artifact) opens no turn | `test_bare_compact_command_row_does_not_open_a_turn` | mock |
| A commit's trace never ends with an unanswered user message: force commits trim trailing final-less turns (watermark stays before them), but a SOLE in-flight turn still force-commits on exit | `test_force_commit_trims_trailing_unanswered_turn`, `test_sole_unanswered_turn_still_force_commits_on_exit`, `test_finish_parse_forces_in_progress_commit_on_exit` | mock |

## 9a3. Live background tasks vs the user-commit dialog
| Sequence | Test(s) | Kind |
|---|---|---|
| Liveness from the notification stream: a recent monitor `<event>` with no terminal notification after it = live; terminal ends it; events beyond the horizon age out (launch-counting overcounts: mid-turn completions never notify) | `test_parse_rows_tracks_live_background_tasks_from_the_notification_stream` | mock |
| While a background task is live, the automatic user-commit dialog is REPLACED by a warning (changes could be the user's or the task's; they'll be committed after the next agent turn); the dialog returns once no task is live | `test_live_background_task_replaces_user_commit_dialog_with_warning`, `test_user_commit_dialog_returns_once_background_tasks_end` | mock |

## 9b. Headless background tracker (`-b`, issue #143)
| Sequence | Test(s) | Kind |
|---|---|---|
| `-b` launcher spawns a DETACHED daemon and returns to the shell | `test_start_background_daemon_spawns_and_reports` | mock |
| `-b` RESTARTS a daemon already running (rerun picks up updated code, like `-d`/`--backtrace`); failure to stop the old one refuses to spawn | `test_start_background_daemon_restarts_running`, `test_start_background_daemon_fails_when_old_tracker_will_not_stop` | mock |
| `-b` over the repo lock: a lock-holding BACKGROUND tracker is stopped and replaced; any other holder (interactive session) still refuses | `test_background_rerun_replaces_a_running_background_tracker`, `test_replace_running_tracker_only_replaces_a_background_tracker`, `test_background_refused_when_another_instance_holds_the_repo` | mock |
| `-b` reports failure when the daemon child dies at startup | `test_start_background_daemon_reports_failure_when_child_dies` | mock |
| `-b stop` / `-b status` target the daemon via its handshake | `test_background_status_*`, `test_background_stop_cleans_stale_handshake`, `test_background_run_writes_and_removes_handshake` | mock |
| `-b` refused when another instance holds the repo lock | `test_background_refused_when_another_instance_holds_the_repo` | mock |
| Daemon / proxy write a user event log (`--log-file` / `log_file`): daemon-start, ai-change-detected, commit | `test_background_writes_event_log`, `tests/test_events.py::*` | real-git + unit |
| `agitrack --status` / `-s` reports the running mode (background / interactive / not running; auto/manual; worktree/no-worktree) | `test_repo_status_reports_each_mode`, `test_proxy_status_write_and_clear` | real-git |

## 9c. Persistent auto-track pre-commit hook (remind / auto-start on commit)
| Sequence | Test(s) | Kind |
|---|---|---|
| Hook installs (frozen-aware invocation + PATH fallback baked in), chains a project hook, restores on removal | `test_autotrack_precommit_hook_install_remove_and_chain`, `test_autotrack_hook_is_frozen_aware_and_has_path_fallback` | real-git |
| Hook is a no-op inside a linked worktree | `test_autotrack_hook_is_a_noop_inside_a_worktree` | unit |
| `--precommit-sync` records pending AI turns + folds the trace into the triggering commit | `test_precommit_sync_folds_ai_work_into_the_commit` | real-git |
| No AI work since last commit → no footprint (no trailer, no nag) | `test_precommit_sync_no_ai_work_is_a_noop` | real-git |
| Defers to a live tracker (never double-tracks) | `test_precommit_sync_defers_to_a_running_tracker` | real-git |
| Sync auto-starts the daemon in the LAST run's commit mode (persisted); `off` folds but never spawns | `test_precommit_sync_autostart_spawns_daemon`, `test_precommit_sync_off_does_not_spawn_daemon`, `test_background_mode_persist_roundtrip` | real-git |
| `agitrack -b` explains the auto-start hook + asks enable/off (default on; shows how to remove); re-asks whenever off (incl. after `--remove-hooks`), skips once enabled | `test_background_hook_prompt_enable_off_and_reask_when_off`, `test_background_hook_prompt_skipped_when_scripted` | mock |
| Daemon honors `autotrack_hook`: installs by default, REMOVES the hook when off | `test_daemon_installs_autotrack_hook_by_default_and_skips_when_off` | real-git |
| AUTO fold writes a CLEAN agent commit (prompt/summary subject, one metadata block — not the squash-into-user format) | `test_background_auto_folds_pending_into_a_commit_itself`, `test_noworktree_auto_folds_latent_turn_into_commit` | real-git |
| Daemon AUTO fold waits for the LLM summary, then uses it as the subject | `test_background_auto_fold_waits_for_summary_then_uses_it_as_subject` | real-git |
| AUTO fold bails early (doesn't hang) when the summary worker finished without a note | `test_fold_summary_ready_bails_when_worker_finished_without_note` | real-git |
| Global `summarization_enabled: false` wins in background mode (not shadowed by state default) | `test_global_summarization_disabled_is_not_shadowed_by_state_default` | mock |
| `agitrack --remove-hooks` removes all aGiTrack hooks, restores chained originals | `test_remove_all_installed_hooks_removes_everything_and_restores_chains`, `_noop_when_none` | real-git |
| `.agitrack/` git-ignored before the daemon/hook write state (no `git add -A` leak) | `test_precommit_sync_git_ignores_agitrack_dir` | real-git |
| **Session discovery is strictly repo-scoped — no cross-repo trace/token contamination** | `test_claude_session.py::test_session_discovery_is_strictly_repo_scoped`, `test_opencode_session.py::test_session_belongs_to_repo` / `_no_matching_directory_returns_no_sessions` | real-git + mock |

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
| Exit asks keep-or-delete worktrees (default keep); delete only fully-merged | `test_exit_keeps_fully_merged_worktree`, `test_exit_worktree_prompt_lists_paths_and_caches_decision`, `test_finalize_worktree_on_exit_deletes_merged_when_user_chooses`, `test_finalize_worktree_on_exit_delete_choice_keeps_unintegrated` | real-git |
| Exit/no-worktree cleanup announces "Deleting worktree…" before the (slow) removal | `test_finalize_worktree_on_exit_announces_deletion`, `test_present_pending_noworktree_cleanup_deletes_on_confirm` | real-git |
| Persist resume pointer (last active, even if not primary / worktree kept) | `test_exit_persists_resume_pointer_*` | mock |
| `exit`/`quit` command routes through the unified flow | `test_exit_command_routes_through_unified_exit_flow`, `_cancelled_does_not_request_exit` | mock |
| Signal teardown (terminal closed) keeps a worktree with leftover files | `test_handle_exit_signal_*` *(posix-only: SIGHUP/SIGTERM delivery)* | mock |

## 11b. Startup & reactor composition (`tests/test_startup_composition.py`)
The whole of `ProxyRunner.run()` and `_loop` driven end to end against a real git repo, with only
the three platform boundaries faked (`tests/harness.py`). Every other row in this matrix tests a
METHOD; these test the SEQUENCE, which is where a bug survives a green suite — each method is
correct and only the order or the wiring is wrong. The recorded precedent is a fix applied to
`_new_session`'s resume path but not to the `run()` → `_spawn()` startup path.

| Sequence | Test(s) | Kind |
|---|---|---|
| Launch reaches the reactor having spawned exactly one backend child in the repo | `test_startup_runs_to_the_reactor_and_spawns_the_backend` | real-git |
| **Startup stages the backend resume BEFORE spawning** (else `_should_continue_session` reads the stale recorded dir, calls our own session a stranger, and silently starts a fresh one) | `test_startup_stages_the_backend_resume_before_spawning` | real-git |
| `--new-session` skips resume staging | `test_startup_skips_resume_staging_for_a_forced_new_session` | real-git |
| Startup ORDER is pinned: screen → spawn → watcher → git worker → reconcile → hooks → first paint → reactor | `test_startup_sequence_order_is_stable` | real-git |
| Terminal enters raw mode and is restored (incl. after a reactor crash, which still writes a crash report) | `test_startup_puts_the_terminal_in_raw_mode_and_restores_it`, `test_startup_restores_the_terminal_even_when_the_reactor_crashes` | real-git |
| Management lock released, so a second launch succeeds | `test_startup_releases_the_lock_so_a_second_launch_succeeds` | real-git |
| Proxy status written while live, cleared on exit | `test_startup_records_the_proxy_status_and_clears_it_on_exit` | real-git |
| Worktree vs `--no-worktree`: the child's cwd is the worktree / the base checkout | `test_worktree_startup_runs_the_agent_inside_the_worktree`, `test_no_worktree_startup_runs_the_agent_in_the_base_repo` | real-git |
| Commit guidance reaches the real spawn argv and names this session's worktree; `--no-commit-guidance` omits it | `test_commit_guidance_reaches_the_real_spawn_command`, `test_no_commit_guidance_omits_the_note` | real-git |
| Reactor drains real backend output through all five phases onto the screen | `test_reactor_drains_real_backend_output_through_all_five_phases` | real-git |
| Reactor forwards keystrokes to the backend | `test_reactor_forwards_keystrokes_to_the_backend` | real-git |
| Backend exits on its own → relaunch + resume; keeps dying → crash-loop guard stops with a notice | `test_reactor_relaunches_a_backend_that_exits_on_its_own`, `test_reactor_gives_up_on_a_backend_stuck_in_a_crash_loop` | real-git |
| The harness's fakes still satisfy the platform Protocols | `test_fakes_satisfy_the_platform_protocols` | mock |

## 11c. Backend parity (`tests/test_backend_parity.py`, `tests/test_turn_end_detection.py`)
Structural checks that every registered backend implements the same contract, so "works on Claude"
can no longer silently mean "does nothing on OpenCode". The runner reaches most backend methods via
`getattr(..., None)`, which turns a missing one into a SILENT degradation. Parameterized over
`available_backends()`, so a third backend is covered the moment it is registered.

| Sequence | Test(s) | Kind |
|---|---|---|
| Every backend implements the whole `ProxyAgent` contract, with matching signatures | `test_every_backend_implements_the_whole_proxy_agent_contract`, `test_every_backend_matches_the_contract_signatures` | mock |
| `retarget_working_dir` is part of the DECLARED contract (it was called on every backend while declared nowhere) | `test_retarget_working_dir_is_part_of_the_declared_contract` | mock |
| Every backend offers a turn-end liveness signal (transcript path OR direct activity mtime) | `test_every_backend_offers_a_turn_end_liveness_signal` | mock |
| Liveness lookups answer None (never raise) for an empty/unknown session — they are polled from the reactor | `test_liveness_signals_are_safe_on_an_unknown_session` | mock |
| Unknown backend name raises rather than substituting one | `test_unknown_backend_raises_rather_than_substituting_one` | mock |
| Spawn command starts with the backend binary and honours a launch wrapper | `test_spawn_command_starts_with_the_backend_binary`, `test_spawn_command_honours_a_launch_wrapper` | mock |
| **Turn end: a quiet sub-agent must not read as the turn ending** (else a half-finished turn is committed) — both backends | `test_a_quiet_subagent_does_not_read_as_the_turn_ending` | mock |
| **Turn end: a chattering idle heartbeat must not prevent it** (else NOTHING is ever committed) — both backends | `test_a_chattering_idle_heartbeat_does_not_prevent_the_turn_from_ending` | mock |
| OpenCode's signal is session-scoped, read-only, and degrades to None on any store problem | `test_opencode_reports_activity_from_its_session_store`, `test_opencode_activity_is_none_when_the_store_is_unusable`, `test_opencode_never_writes_to_the_users_database` | mock |
| OpenCode's model comes from its session store (its event stream names none) | `test_opencode_resolves_the_model_from_its_session_store`, `test_opencode_model_lookup_tolerates_an_unexpected_store_shape` | mock |

## 11d. Live backend smoke (`tests/test_live_backends.py`, `-m live`)
The only tests that call the REAL backend CLIs, and therefore the only ones that can catch a CLI
changing its output format under us — mocks assert the shape we believed was true when we wrote
them. Excluded from the default run and from CI (they need the backend installed and
authenticated and cost real tokens); each skips itself when its binary is absent. Run `pytest -m
live`. They found OpenCode reporting no model on the very first run.

| Sequence | Test(s) | Kind |
|---|---|---|
| A bare run returns usable text and exits cleanly | `test_a_bare_run_returns_usable_text` | live |
| A bare run reports its model and non-zero token counts (the fields every commit records) | `test_a_bare_run_reports_its_model_and_token_usage` | live |
| The backend's self-update command names the real binary | `test_the_update_command_names_the_real_binary` | live |

## 12. Session sharing
| Sequence | Test(s) | Kind |
|---|---|---|
| Share / list / read back | `test_share_lists_and_reads_back`, `test_share_runs_in_background_without_blocking` | real-git |
| Resume shared (fetch / already-live / name prompt / errors) | `test_shared_resume_*` | real-git |
| Share-behind → overwrite + reshare / cancel | `test_share_behind_offers_overwrite_and_reshares`, `_cancel_leaves_shared_copy_untouched` | real-git |
| Unshare (confirm, retries, fallbacks, lineage) | `test_unshare_*` | real-git |

## 12b. Learning page (dashboard `/learn`, `tests/test_learn.py`)
The dashboard's learning coach: the user opens `/learn`, taps how much time they have (5/15/30 min)
and how they feel (fresh/okay/tired), optionally picks whose traces to learn from (their own,
a teammate's, or the whole team) and a period, and presses one button. The backend agent reads a
digest of those interaction traces, assesses the learner, identifies knowledge gaps, and proposes
3-4 sized lesson suggestions; tapping one (behind a full-screen processing overlay) generates a step-by-step lesson
(3-7 small steps walked one at a time; links, quiz and an in-page exercise unlock at the end,
with the exercise answered in the page and reviewed by the aGiTrack coach). Progress
(opened, completed, time on page, quiz score, exercise attempts) is tracked automatically per
GitHub user in `.agitrack/learning.json`, and optionally synced to git
(`refs/agitrack/learning-progress`) like shared sessions. The coach engine (backend + model) is
selectable on the page and persisted as `learning_backend` / `learning_model` in the repo config.
The page is served by BOTH dashboards: the live server and the backtrace reconstruction (where a
directory that is not a git repo still gets the full page, with progress sync reported unavailable).

| Sequence | Test(s) | Kind |
|---|---|---|
| The page fits a phone exactly (no horizontal scrollbar): progress rows wrap and their titles shrink, and the stat tooltip anchors to the stats ROW capped at its width — an absolutely positioned bubble adds scrollable overflow even while invisible | `test_learn_page_fits_a_phone_width_exactly` | real-git |
| Engine resolution: config keys > latest session backend/model; cross-backend model dropped; none → clear error | `test_resolve_prefers_config_over_latest_session`, `_falls_back_to_latest_session`, `_config_model_wins`, `_without_any_backend_raises` | real-git |
| Engine picker persists to / clears from the repo config overlay; unknown backend refused | `test_set_learning_config_roundtrip`, `_rejects_unknown_backend` | real-git |
| Check-in → suggestions: digest covers prompts/insights/files/README/progress; capped; persisted per GitHub user; agent failure and empty window surface as in-page errors; one agent call at a time | `test_digest_*`, `test_suggest_persists_profile_per_user`, `_reports_agent_failure_as_error`, `_with_no_turns_explains_instead_of_calling_agent`, `test_agent_lock_reports_busy` | real-git |
| Suggestion → lesson: normalized (bad links dropped, quiz validated, exercise attached), stored under the learner | `test_lesson_generation_normalizes_and_persists`, `test_unknown_suggestion_is_an_error` | real-git |
| Automatic progress: time accumulates, quiz results stored, completion closes the linked gap | `test_progress_tracks_time_quiz_completion_and_closes_gap` | real-git |
| Exercise: aGiTrack coach review logs the attempt and a pass marks it done; skip via progress | `test_exercise_check_logs_attempt_and_marks_done`, `test_exercise_skip_via_progress` | real-git |
| Follow-up chat appends bounded history | `test_lesson_chat_appends_bounded_history` | real-git |
| Progress sync: opt-in toggle writes the orphan ref and pushes to origin; works offline; two users coexist; disable stops pushing | `test_sync_progress_writes_ref_and_pushes_to_origin`, `test_sync_without_remote_still_records_locally`, `test_two_users_coexist_on_the_sync_ref` | real-git |
| New machine / fresh clone: empty local profile is restored from the synced ref on first page load, sync re-enabled; never overwrites local progress; reported once | `test_progress_restores_on_a_new_machine` | real-git |
| "Start over" clears stale suggestions (new commits / changed filters) but keeps lessons, gaps, assessment | `test_reset_suggestions_clears_picks_but_keeps_progress` | real-git |
| Delete a lesson from the progress history (two-step confirm in the UI); closed gaps stay closed; unknown id → in-page error | `test_delete_lesson_removes_it_but_keeps_gaps` | real-git |
| Branch selector: the trace slice (and check-in context) is per git ref, validated server-side and passed through the shared dispatcher | `test_handle_learn_post_dispatches_and_404s`, `test_suggest_persists_profile_per_user` (context) | real-git |
| (Almost) no captured trace → no agent call; notice explains --backtrace / running sessions through aGiTrack; starter topics offered and flow into the normal lesson pipeline; a later real-trace suggest clears the notice | `test_suggest_with_little_trace_offers_starter_topics_without_agent_call` | real-git |
| No duplicate picks: digest lists completed AND in-progress lessons as no-repeat; near-duplicate suggestions are dropped server-side (kept only if the model duplicated everything) | `test_suggest_drops_picks_duplicating_recent_lessons`, `test_digest_lists_in_progress_lessons_as_no_repeat` | real-git |
| Identity: GitHub login with git user.name fallback | `test_learner_id_falls_back_to_git_user_name` | real-git |
| Page + routes served over HTTP (GET /learn, /learn/state; POSTs return in-page errors, never 500) | `test_learn_html_contains_the_page`, `test_dashboard_serves_learn_routes` | real-git |
| Unavailable-feature / error notices show as a fixed toast, visible from anywhere on the page (click dismisses) | `test_flash_notices_are_a_fixed_toast` | real-git |
| Backtrace mode: learn works without a git repo (repo=None; sync reported unavailable), shared POST dispatcher routes and 404s | `test_learn_works_without_a_git_repo`, `test_handle_learn_post_dispatches_and_404s` | real-git + plain-dir |
| Backtrace server end-to-end: /data efficiency insights over reconstructed turns; /learn carries the frozen "based on backtracing" warning strip; state (no branches, sync unavailable); suggestions personalized from the reconstruction | `test_backtrace_server_serves_learn_with_banner_and_insights` | plain-dir + HTTP |
| Re-running `--backtrace` restarts a running daemon on the same port (like `-d`); a cold start pins no port | `test_backtrace_start_restarts_a_running_daemon_on_the_same_port`, `test_backtrace_cold_start_does_not_request_a_port` | mock |
| The reconstruction lists AGENT turns only: a prompt the agent never answered (an interrupted message, then a re-ask) is joined to the turn that DID answer instead of appearing as its own user-only entry, and a trailing unanswered prompt (the turn in flight) waits rather than showing empty; an interrupted turn that spent tokens or changed files is real history and keeps its own entry | `test_a_prompt_the_agent_never_answered_joins_the_turn_that_did`, `test_an_unanswered_prompt_with_nothing_after_it_is_not_listed_yet`, `test_an_interrupted_turn_that_did_work_is_still_its_own_entry` | mock |
| The backtrace daemon keeps up with new sessions without burning CPU: a stat-only signature polls the transcripts (files = a session gained turns, dirs = one may have appeared, only the latter re-discovers), rebuilds are floored at a minute and re-read ONLY the sessions whose transcript moved (in-process memo keyed by stat identity, so no result outlives the code that made it), and the handler renders whatever view was last built | `test_watch_signature_separates_files_from_directories`, `test_rebuild_reprocesses_only_the_sessions_that_changed`, `test_served_view_follows_the_rebuilt_one` | mock |
| Backtrace log opens with an explainer of what its entries are (reconstructed turns, unhidden only under the BACKTRACE flag); log subject lines truncate at word ends with an ellipsis, never bare mid-word — both the client-side cap AND the reconstruction's server-side subject builder | `test_backtrace_log_explains_what_the_entries_are`, `test_subject_truncation_cuts_at_word_ends`, `test_backtrace.py::test_subject_truncates_at_word_ends_with_ellipsis` | real-git |
| Asking for a turn's file diff in BACKTRACE when none was recovered explains why (the reconstruction reads only the agent's file-editing tool calls, so shell commands, formatters and generated files leave nothing to recover) and points at running the agent through aGiTrack — the live dashboard's plain "no changes to show" is unchanged | `test_backtrace_explains_an_empty_diff_instead_of_claiming_no_changes` | real-git |
| Backtrace tracked-lines card admits it can undercount (reconstruction sees only edit tool calls, not shell/formatter changes) with the full reason as a tooltip; the non-tracked card and bar row — structurally always zero in backtrace — are dropped, not rendered as dead zeros | `test_backtrace_lines_card_admits_undercount_and_drops_nontracked` | real-git |
| Dashboard first paint never waits on the network (no shared-ref fetch in the "/" response; polls fetch instead), so the loading screen, not a blank tab, covers slow starts | `test_first_paint_never_fetches_the_shared_ref` | real-git |
| An expanded commit is plain page content: no frame, no scroll region of its own (it scrolled separately from the page and read as a nested pane), and no second vertical rule beside the log's timeline rail — and ONE rule now covers both the commit log and the files view, which rendered the same element, matched none of it and showed messages in the page's large body font. The `#trace` deep link therefore parks the Interaction Trace heading itself under the sticky chrome, since there is no inner box left to scroll. Footer is just aGiTrack + website + GitHub | `test_expanded_commit_message_has_no_box_and_reads_the_same_everywhere`, `test_footer_is_just_the_name_and_the_two_links`, `test_export.py::test_export_disables_filters_and_cans_learn_actions` | real-git |
| Commit-message paragraphs reflow to the column: consecutive source lines are ONE paragraph whose hard breaks are dropped when the layout wraps them further (and kept verbatim when they fit); blank-line paragraph breaks and the aGiTrack metadata block always keep their breaks; re-decided on resize | `test_commit_message_paragraphs_reflow_when_the_column_is_narrower` | real-git |
| Phone layout gives content the full screen (6px gutters, narrowed log rail with concentric dots) and no page ever scrolls sideways — a space-less subject breaks instead of widening the row | `test_phone_layout_uses_the_full_width` | real-git |
| The shell-mode boot loader stays visible through the /data crunch: its rules are id-scoped so the body's own "booting" state class can never match them and hide the whole page | `test_boot_loader_is_not_hidden_by_the_body_state_class` | real-git |
| Log pager pages by number: windowed page buttons with … gaps, first/last jumps, and a go-to-page box; every jump lands on a (page−1)×PAGE_SIZE offset so the static demo's pre-baked log pages resolve; while the page fetch is in flight a floating "loading…" badge (filter-badge look) hangs below the page selector | `test_log_pager_has_numbered_pages_first_last_and_a_goto_box` | real-git |
| `--backtrace commit` counts every turn ONCE in a repo that used aGiTrack for part of its life: turns already committed by aGiTrack (matched via backend_session_id + conversation_anchor, and by turn id across forks) are not re-attributed, and a forked/resumed conversation that replays earlier turns contributes each turn once — so no trace is printed twice and no tokens are summed twice; only commits matched to NON-tracked agent turns are annotated | `test_turns_already_committed_by_agitrack_are_not_counted_again`, `test_a_forked_conversation_contributes_each_turn_once` | real-git |

## 12c. Static demo export (`agitrack -d export`, `tests/test_export.py`)
A server-free copy of the dashboard + learn page for static hosts (powers the public demo at
agitrack.core-aix.org/dashboard/, rebuilt by `.github/workflows/pages.yml` on each push to main). That
workflow re-asserts the "GitHub Actions" Pages source on every run and refuses to deploy a site without
`dashboard/index.html`: the legacy `main:/docs` branch build publishes docs/ alone, so if the source drifts
back to it the public demo 404s.
| Sequence | Test(s) | Kind |
|---|---|---|
| Export is complete: every /data granularity, every /log page for every sort, every in-scope commit's /diff, the whole file browser (filelog + per-change filediff) baked as files | `test_export_writes_a_complete_static_site` | real-git |
| The demo ships the last 30 days (anchored to the newest commit, never empty), not all time: log pages, embedded first paint, and baked diffs are scoped; the banner and the disabled range dropdown say "last 30 days" | `test_export_scopes_the_demo_to_the_last_30_days` | real-git |
| The fetch shim is installed before any page script runs | `test_export_shim_installs_before_the_page_script` | real-git |
| Honest degradation: demo banner + install hint on both pages, filter controls disabled, agent-driven learn POSTs answered with the install hint; tapping a disabled filter or the reset button flashes the same demo note as a fixed toast (learn-page style, click to dismiss) | `test_export_writes_a_complete_static_site`, `test_export_disables_filters_and_cans_learn_actions` | real-git |
| The `#trace` deep link maximizes the expanded commit: the window parks the ENTRY under the chrome that is ACTUALLY stuck (the filter bar is sticky on desktop but scrolls away on a phone) and the message box scrolls internally to its Interaction Trace; corrections scroll instantly, so the view never overshoots and creeps back | `test_export_disables_filters_and_cans_learn_actions` | real-git |
| Learn profile fallback: the store's single non-empty profile ships when the exporting identity has none (how CI exports the checked-in fixture) | `test_export_learn_state_falls_back_to_the_single_store_profile` | real-git |
| CLI: `-d export --export-dir` writes the site and reports the path | `test_cli_export_writes_the_site` | real-git |

## 12d. Non-interactive modes: JSON loop and the UI bridge (`tests/test_bridge_protocol.py`)
`--json`, `--prompt` and `--ui-bridge` all drive `shell/runner.py`, and the bridge is the transport
the **VSCode extension** uses for every session it opens. A regression breaks every extension user
at once and does it silently — the editor just never receives the event it is waiting for. In
bridge mode stdout IS the protocol channel, so nothing may be printed as free text.

| Sequence | Test(s) | Kind |
|---|---|---|
| `:exit` / `:quit` end the session | `test_exit_commands_end_the_session` | real-git |
| `:status` / `:unstaged` answer as notices, never as stray stdout | `test_status_reports_a_clean_tree_as_a_notice`, `test_unstaged_reports_when_there_is_nothing_intentionally_unstaged`, `test_unstaged_lists_the_declined_files` | real-git |
| `:new-session` mints an id and announces it in a `ready` frame (the editor keys its view on it) | `test_new_session_mints_an_id_and_announces_it` | real-git |
| Unknown command warns instead of failing; a command with args dispatches on the verb alone | `test_an_unknown_command_warns_instead_of_failing`, `test_a_command_with_arguments_dispatches_on_the_verb_alone` | real-git |
| Backend switch: unknown / not-installed warn and change nothing; a real switch stashes the outgoing conversation and announces the new backend | `test_switching_to_an_unknown_backend_warns_and_changes_nothing`, `test_switching_to_an_uninstalled_backend_warns_with_an_install_hint`, `test_switching_backend_remembers_the_outgoing_conversation`, `test_switching_backend_announces_the_new_backend_to_the_editor` | real-git |
| `:summarizer on/off` is case-insensitive and persists globally; an unknown argument changes nothing | `test_summarizer_toggle_is_case_insensitive_and_persists_globally`, `test_an_unknown_summarizer_argument_does_not_change_the_setting` | real-git |
| Transport survives a partial/malformed frame and ignores unknown frame types; closed stdin becomes an exit so the loop always unblocks | `test_the_reader_survives_a_partial_or_malformed_frame`, `test_a_frame_of_an_unknown_type_is_ignored_not_queued`, `test_closed_stdin_becomes_an_exit_so_the_loop_always_unblocks` | mock |

## 12e. Trace fidelity and silent-failure edge cases (`tests/test_slash_directives.py`, `tests/test_session_switch_summary.py`, `tests/test_edge_cases.py`)
Cases that fail without printing anything — the user only finds out later, from a history that is
missing a turn or attributes it to the wrong model.

| Sequence | Test(s) | Kind |
|---|---|---|
| A slash command whose ARGS are prose (`/goal …`, `/loop …`, a skill) is recorded with its full instruction — decided from the arguments, not a list of command names | `test_a_command_with_args_followed_by_work_is_recorded_with_its_instruction`, `test_the_rule_is_not_a_list_of_known_command_names`, `test_multiline_arguments_are_preserved` | mock |
| **Recorded even with no reply yet** — typed mid-tool-call, or as the last row of the transcript. Requiring a reply dropped exactly the ordinary uses (steering work in progress) | `test_an_instruction_is_recorded_even_when_no_reply_has_arrived_yet`, `test_an_instruction_typed_mid_turn_is_recorded` | mock |
| …and the exclusions: no args at all, or a single bare token (`/model sonnet`, `/goal clear`) — a parameter or control word, not a request, and an unanswered turn defers commits; `/compact` is still never a turn | `test_a_command_with_no_arguments_is_not_a_prompt`, `test_single_token_arguments_are_configuration_not_an_instruction`, `test_a_control_word_argument_is_not_an_instruction`, `test_compact_is_still_never_a_turn` | mock |
| A directive opens the first turn of a conversation, or its own turn mid-conversation; a following prompt gets its own turn; `/init`-style expansion is unchanged | `test_a_directive_can_open_the_very_first_turn_of_a_conversation`, `test_a_directive_mid_conversation_opens_its_own_turn`, `test_a_following_prompt_gets_its_own_turn`, `test_an_expanding_command_keeps_its_expansion_behaviour` | mock |
| A directive turn is a REAL turn: billed and attributed like any other, and an `/init`-style expansion with args still prefers the instruction text | `test_directive_turns_carry_their_tokens_and_model`, `test_an_expanding_command_with_args_prefers_the_instruction` | mock |
| **Agent-native session switch (`/clear`, `/resume`, OpenCode's picker) discards the OLD conversation's pending summary and rolling summary** — else its result is applied and reported against the new conversation, and the stale rolling summary poisons every later summary | `test_a_native_switch_discards_the_old_conversations_pending_summary`, `test_a_native_switch_clears_the_rolling_session_summary` | real-git |
| …and the guard is narrow: staying on the same conversation, or a stale sibling, keeps the state | `test_staying_on_the_same_conversation_keeps_the_summary_state`, `test_a_stale_sibling_conversation_does_not_trigger_a_switch` | real-git |
| A transcript read mid-append (torn trailing line, corrupt middle line, undecodable byte) keeps every completed turn | `test_a_half_written_trailing_line_does_not_lose_the_completed_turns`, `test_a_corrupt_line_in_the_middle_does_not_discard_what_follows`, `test_a_prompt_containing_a_lone_surrogate_does_not_crash_the_parser` | mock |
| Empty / blank-only / missing transcripts are not errors | `test_an_empty_transcript_is_not_an_error`, `test_a_transcript_of_only_blank_lines_yields_no_turns`, `test_a_missing_transcript_returns_nothing_rather_than_raising` | mock |
| Token accounting: every field sums, `context` is a level (not summed), a message's usage is counted once, `<synthetic>` never becomes the turn's model | `test_token_usage_sums_every_field_when_added`, `test_adding_usage_takes_the_latest_context_rather_than_summing_it`, `test_a_turns_tokens_are_counted_once_per_message`, `test_a_synthetic_model_marker_never_becomes_the_turns_model` | mock |
| Commit messages survive emoji / multi-script / control characters / very long subjects | `test_a_commit_message_survives_awkward_text` | real-git |
| Bracketed paste split across reads is still recognised; pasted content reaches the backend byte-for-byte and a pasted newline/Ctrl-C is never interpreted | `test_bracketed_paste_markers_are_recognised_when_split_across_reads`, `test_pasted_content_reaches_the_backend_byte_for_byte`, `test_a_paste_split_mid_stream_still_forwards_everything` | mock |
| Signal control bytes never reach the backend; ordinary bytes pass through untouched | `test_signal_control_bytes_are_never_forwarded_to_the_backend`, `test_ordinary_bytes_pass_through_untouched_when_not_capturing` | mock |

## 12f. Headless recovery (`agitrack --recover`, `tests/test_recovery.py`, `tests/test_recovery_report.py`)
Eager, standalone recovery of work left by a session that exited abruptly — the editor window
closed mid-turn, or the process SIGKILLed. The agent's work survives on disk (uncommitted changes
in the session worktree, plus the backend transcript) but is neither committed nor merged; the
normal recovery is lazy (the next launch reconciles it), and this makes it immediate so the
editor extension can run it the moment a session closes.

The policy hinges on one distinction: a FINISHED turn is committed and merged, an ABORTED or
still-in-flight turn is left strictly alone. Getting that backwards commits half a turn.

| Sequence | Test(s) | Kind |
|---|---|---|
| Latest turn finished → commit its changes, then merge (skipping the merge on conflict) | `test_finished_turn_is_committed_and_merged` | real-git |
| Latest turn finished and summarization on → the commit is summarized | `test_finished_turn_is_summarized_when_enabled` | real-git |
| **Latest turn aborted / still in flight → changes left untouched and the session flagged** — never commit a half-finished turn | `test_aborted_turn_is_left_untouched` | real-git |
| Work already committed but unmerged → merged, as startup reconciliation does | `test_committed_but_unmerged_work_is_integrated` | real-git |
| A live aGiTrack holds the repo lock → recovery no-ops (it must never commit under a running agent) | `test_recovery_skips_when_a_live_session_holds_the_lock`, `test_recover_returns_skipped_busy_when_lock_not_acquired`, `test_recovery_does_not_run_while_a_live_session_holds_the_lock` | real-git |
| A SIGKILLed session's `flock` is released by the kernel, so recovery can take the repo (the premise the whole feature rests on) | `test_a_sigkilled_session_frees_the_repo_lock_for_recovery` | real-git |
| No worktrees / nothing pending → clean no-op | `test_nothing_to_recover_with_no_worktrees`, `test_summary_nothing_to_recover` | real-git |
| A worktree mid-merge, or one that raises, is FLAGGED for attention rather than silently skipped | `test_recover_one_flags_mid_merge_worktree`, `test_recover_one_exception_adds_to_flagged_once`, `test_recover_locked_handles_worktree_list_exception` | real-git |
| The report says what happened (recovered / integrated / flagged / skipped-busy) | `test_recovery_report.py::test_summary_*`, `test_did_work_*` | mock |
| Scope: `--no-worktree` sessions are deliberately NOT auto-committed (agent edits are intermixed with the user's own) | documented in `recovery.py`; enforced by the worktree-only scan in `test_nothing_to_recover_with_no_worktrees` | real-git |

## 12g. Backtrace daemon lifecycle (`agitrack --backtrace`, `tests/test_backtrace_daemon.py`)
The daemon's handshake file is its whole notion of "am I running": a JSON record naming a pid and
a URL. Every row here is about that record staying honest — a stale one makes every later
`--backtrace` believe a daemon is already running, permanently, since nothing else removes it.

| Sequence | Test(s) | Kind |
|---|---|---|
| `status` with no daemon / with a live one (names its URL and pid) | `test_status_reports_nothing_when_no_daemon_has_run`, `test_status_names_the_url_and_pid_of_a_live_daemon` | real-proc |
| `status` does not report a daemon whose process is gone | `test_status_does_not_report_a_daemon_whose_process_is_gone` | real-proc |
| **A stale record is cleared, so a new daemon can start** (otherwise the feature is bricked for good) | `test_a_stale_handshake_is_cleared_so_a_new_daemon_can_start` | real-proc |
| A corrupt / non-object / pid-less handshake reads as "no daemon" rather than raising | `test_a_corrupt_handshake_is_treated_as_no_daemon`, `test_a_handshake_that_is_not_an_object_is_rejected`, `test_a_handshake_without_a_pid_is_not_treated_as_live` | mock |
| `stop` with nothing running says so; with a stale record it clears rather than claiming a stop | `test_stop_reports_plainly_when_nothing_is_running`, `test_stop_clears_a_stale_record_rather_than_claiming_to_stop_it` | real-proc |
| `stop` really terminates a live process and clears its record | `test_stop_terminates_a_real_process_and_clears_its_record` | real-proc |
| The preferred port being held by something else scans forward instead of failing | `test_the_daemon_binds_past_a_port_something_else_already_holds` | real-net |
| Two directories track their daemons (and logs) independently | `test_the_daemon_log_path_is_scoped_to_the_directory`, `test_two_directories_track_their_daemons_independently` | mock |

## 13. Self-update
| Sequence | Test(s) | Kind |
|---|---|---|
| Self-updating is ON by default for users; the global `self_update` setting turns it off, and aGiTrack then only REPORTS the newer version (dashboards still say what to install). Internally the daemon watcher takes it as an explicit argument and no test can reach the real updater (autouse conftest guard), because a source update FETCHES and MERGES the checkout — defaulting that on let a watcher test fast-forward CI's own checkout mid-run and fail the version test | `test_self_update_is_on_by_default_but_can_be_turned_off`, `test_daemon_watcher_also_installs_updates` | mock |
| aGiTrack SELF-UPDATES without asking: the TUI and every daemon install a newer version on their own; only ONE instance may do it at a time (OS file lock in the global config dir, kernel-released if the holder dies) | `test_only_one_instance_may_self_update_at_a_time` | mock |
| Install modes are never mixed: source and POSIX pip/pipx self-update; MSI (needs elevation), Homebrew (not ours) and Windows pip (locked exe) are recorded as "needs you" and never half-attempted | `test_only_install_modes_that_can_finish_unattended_are_attempted`, `test_windows_package_installs_are_left_to_the_post_exit_helper`, `test_a_mode_that_cannot_self_update_is_recorded_without_attempting` | mock |
| A failed or impossible self-update is recorded globally and clears itself on success | `test_a_failed_self_update_is_recorded_for_the_dashboards`, `test_a_successful_self_update_clears_the_reminder` | mock |
| Dashboards show the two notices SEPARATELY: "install it yourself" (global, on every dashboard incl. backtrace) vs "restart your session" (this repo only, from the fingerprint the session recorded in its repo lock) — the session is never restarted from under the user | `test_dashboards_show_the_two_notices_separately`, `test_a_session_running_older_code_is_detectable` | mock |
| A lone dashboard or backtrace daemon still keeps the install current: the same watcher thread that restarts it also drives the self-update | `test_daemon_watcher_also_installs_updates` | mock |
| Source: detect/apply (clean / diverged / conflict / offline) | `test_source_check_*`, `test_source_apply_*` | real-git |
| Startup prompt (apply / default-enter / explicit-no / pending reminder) | `test_startup_prompt_*`, `test_startup_reminds_without_reprompting_when_pending` | mock |
| Apply failure records pending and keeps running | `test_startup_apply_failure_records_pending_and_keeps_running` | mock |
| Windows MSI: detect (frozen+registry) / check GitHub release / download / no-asset / api-error | `test_updater.py::test_install_method_msi_*`, `test_check_msi_*`, `test_apply_msi_*` | unit |
| Windows MSI: manual-instructions route (releases URL + SmartScreen) | `test_updater.py::test_manual_instructions_msi_route` | unit |
| Restart command shape (frozen exe vs `python -m agitrack`) — self-update **and** settings "restart now" | `test_updater.py::test_restart_command_*` | unit |
| Background daemon records an available update to the shared marker (never auto-installs); clears when current | `test_daemon_update_check_writes_marker_and_clears` | real-git |
| Update surfaced on every surface: `-b status`, commit-time (pre-commit hook), dashboard banner | `test_background_status_shows_available_update`, `test_precommit_sync_reminds_about_update_on_every_commit`, `test_update_marker.py::*` | real-git + unit |
| Daemons RESTART themselves once aGiTrack's update has fully COMPLETED (never installs; source install → NEW COMMIT landed and index.lock gone, wheel → new dist version readable; debounced to two consecutive sightings) | `test_update_restart.py::test_updated_fingerprint_only_reports_a_settled_change`, `test_source_fingerprint_is_the_head_commit_and_waits_for_index_lock`, `test_wheel_fingerprint_is_the_installed_version`, `_requires_two_consecutive_sightings` | real-git + unit |
| Dashboard/backtrace restart pins the bound port (open URLs survive); cleanup runs before the exec | `test_dashboard_daemon_restarts_itself_after_an_update`, `test_restart_command_appends_the_port_flag_only_when_missing` | mock |
| The TEST SUITE can never see or signal the developer's real daemons: the registry follows AGITRACK_CONFIG_DIR (isolated by conftest) and the OS process-table scan is stubbed suite-wide (scan tests restore it explicitly) — before this, update-restart tests SIGTERM'd live dashboards on every full run | `test_registry_dir_honors_config_dir_isolation`, `tests/conftest.py::_never_touch_real_daemons`, `test_list_running_finds_unregistered_daemon_via_ps` | unit |
| A FAILED restart never strands a dead daemon: dashboard/backtrace SPAWN the replacement and exit only after verifying its handshake (a crashing replacement is reaped and the old daemon serves on); the tracker resumes tracking on exec failure — all retry until success or an explicit stop, which always wins | `test_dashboard_daemon_retries_after_a_failed_restart`, `test_dashboard_explicit_stop_wins_over_retry`, `test_background_retries_and_restores_tracking_after_a_failed_restart` | mock |
| Background tracker restart leaves an in-flight turn for the replacement (no force-capture at the swap); a real stop still captures it | `test_background_restart_leaves_in_flight_turns_for_the_replacement`, `test_background_run_execs_replacement_after_update` | mock |
| Ctrl-G dashboard is a free-standing daemon like `-d`: it keeps serving after aGiTrack quits AND after the terminal closes, until `-d stop` (popup says so); exit never kills it | `test_proxy_dashboard.py::test_dashboard_command_spawns_process_and_opens_browser`, `test_dashboard_is_never_stopped_by_agitrack_exit` | mock |

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

Closed by the 2026-08-03 coverage audit (see `tests/TEST_PLAN.md` for the measurements):
- ~~the composition layer — `run()` and `_loop` were reachable only through their callees~~ → §11b
- ~~backend parity was unenforced; `test_proxy.py` ran entirely on Claude~~ → §11c
- ~~nothing ever called a real backend CLI~~ → §11d (`-m live`)
- ~~JSON mode / the UI bridge / `--recover` had no matrix section~~ → §12d, §12f
- ~~the backtrace daemon lifecycle was untested~~ → §12g

Still open from that audit:
- `shell/runner.py` is at 56% (was 43%): `_handle_agent_prompt` and `_handle_pre_compaction` are
  covered only indirectly. The command surface is now pinned (§12d); the TURN path is not.
- The Windows job now runs the platform-agnostic core, but not the proxy/reactor suites — those
  are POSIX-terminal-specific and would need ConPTY equivalents of `tests/harness.py`.

## Candidate findings from the 2026-08-03/04 subagent audits — NOT yet verified or fixed

Each was reported with a file:line and a grep showing it uncovered, but none has been confirmed
against the real code path, so treat them as leads rather than known bugs. Recorded here so they
outlive the conversation they were found in.

**Backend parity** (the recurring shape: a guard Claude has and OpenCode does not — the same
class as the turn-end signal, `SessionTurn.complete`, and the context calculation, all of which
turned out to be real):
- `transcripts/opencode.py:118` — `list_sessions` never filters headless `opencode run`
  transcripts. Claude's parser drops SDK-driven ones via `ref.programmatic`; OpenCode never sets
  it, so a headless run in the repo dir can be silently adopted as the tracked conversation.
- `transcripts/opencode.py:131` — `latest_session_id` lacks the empty-session guard Claude has
  *and is tested for*; a fresh empty OpenCode session can be persisted as "last session" and
  resumed blank.

**Session / sharing:**
- `config/settings.py:590` — the GitHub identity for sharing is cached globally and never
  re-validated, so every share after `gh auth switch` uses the stale identity.
- `runner.py` `_service_native_session_switch` — never calls `_note_backend_session_change` /
  `_record_shared_alias_on_drift`, so a native `/resume` drops auto-share lineage. Adjacent to
  the summary bug fixed in that same function, and missed because only summary state was reviewed.
- `config/state.py:294` — `pending_session_name` is a repo-root scalar, not per-session, so a
  second session finishing first wipes the crash-recovery name of a still-unlinked first one.

**Commit workflow:**
- ~~`git/repo.py:258` — `snapshot_worktree_tree` strips `.claude/`, `.opencode/` and `.agitrack/`
  unconditionally, including already-TRACKED files the agent edited.~~ **CONFIRMED and fixed —
  see §6c.** The lead was right about the cause and understated the effect: the damage was not a
  turn confined to those paths, it was that the snapshot could never equal any raw `^{tree}`, so
  every "is the working tree clean?" test in three modes answered "dirty" forever.
- `commit_engine.py:173` — `_prompt_covered_by` is a bag-of-words heuristic that can drop a
  NEGATED user prompt ("don't do X" matching "do X") from the trace.
- `git/repo.py:574` — `arrived_from_elsewhere` has an untested fallback that can misattribute
  foreign commits.
- `background.py` — the trivial-monitor-tick deferral is never driven through `_process_once`;
  all its tests call `CommitEngine.finish_parse_if_ready` directly.


Remaining from the 2026-06-27 self-audit — lower-risk message/guard branches, to be filled:
- `runner.py:_change_session_merge_branch_menu` — the "'X' is running a turn — change its merge branch when idle" refusal for an in-flight session (happy-path retarget IS tested).
- `runner.py:_rename_session` — the move-failure recovery ("Could not rename session…") and the "Name unchanged" no-op (collision path IS tested).
- `runner.py:_prompt_new_session` — the runtime fork-failure fallback ("Couldn't fork…; starting a blank one instead") (the capability-gate path IS tested).
- `runner.py:_run_command("git-commit")` — the "Committed your changes…" / "No changes to commit in the base repo." messaging wrapper (the underlying `_create_user_commit_popup` IS tested).
- mock-only → upgrade to real-git when convenient: `_present_copy_offer` per-file "confirm each" combined with a real `shutil.copy2` OSError branch; `_finalize_pending_work` multi-session loop where one background session's real commit/merge fails.

## How to extend (the rule, restated)
When you touch a user flow:
1. Add/extend the test (prefer **real-git** for commit/merge/worktree/copy paths).
2. Add or update the matching row above with the test name and kind.
3. If you couldn't cover something, add it to **Known gaps / TODO** rather than leaving it implicit.
