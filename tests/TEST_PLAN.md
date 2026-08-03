# Test-coverage audit and edge-case plan

Companion to `FLOW_MATRIX.md`. The matrix answers *"is this flow listed?"*; this file answers
*"where is the suite structurally blind, and what should we build next?"* — with the measurements
that justify each claim, so the priorities are auditable rather than opinion.

Measured on **2026-08-03**, `dev` @ `ddaf2b8`, macOS / Python 3.12:

```
2607 passed, 10 skipped, 10 deselected (timing) in 6m12s
79% line coverage — 23,938 statements, 4,933 missed, 86 test files
```

---

## 1. What the numbers actually say

79% overall is healthy, and the *leaf* logic is genuinely well covered: `commits/message.py` 97%,
`config/state.py` 96%, `metrics/collect.py` 97%, `metrics/insights.py` 98%, `transcripts/capabilities.py`
97%, `sessions/redact.py` 100%, `summaries/summarizer.py` 99%. Merge/conflict/worktree failure paths
have real-git tests (`test_worktree.py`, `test_integration_service.py`). That part of the suite is not
the problem and does not need more of the same.

The misses are concentrated in **orchestration** — the code that *sequences* the well-tested leaves:

| Module | Cover | Missed | What is missed |
|---|---|---|---|
| `proxy/runner.py` | 75% | **1,825** | `run()` startup, the reactor loop, the interactive menus |
| `shell/runner.py` | 43% | 209 | the whole JSON / `--prompt` / UI-bridge mode |
| `metrics/backtrace.py` | 66% | 273 | the backtrace daemon lifecycle |
| `proxy/pty_backend.py` | 38% | 126 | non-default spawn paths |
| `proxy/platform/nt.py` | 27% | 221 | native Windows (see §4) |
| `cli.py` | 74% | 205 | `main()` launch composition |

Inside `runner.py` the misses are not scattered — they sit on the functions that own composition:

```
113 missed / ~253 lines   run()                       :1556   startup sequence
 55 missed / ~180 lines   _reactor_stdin_phase        :8347
 30 missed / ~ 49 lines   _loop                       :8104   the main event loop
 23 missed / ~ 28 lines   _reactor_pty_output_phase   :8318
 20 missed / ~ 36 lines   _reactor_select_phase       :8281
 42 missed / ~217 lines   _resume_shared_session_menu :6451
 42 missed / ~149 lines   _session_menu               :4911
 38 missed / ~111 lines   _maybe_agent_commit         :12422
```

### Why this is the shape it is

`test_proxy.py` — 596 tests, 11,886 lines — builds its subject with `make_runner()` →
`ProxyRunner.for_testing()` **263 times**, then calls one method directly. That is a fine way to test
a method and a structurally impossible way to test a sequence.

`ProxyRunner.run()` is invoked by exactly **four** tests (`test_proxy.py:3784, 3812, 3826, 3841`), and
every one of them asserts `runner.run() == 1` — the early-exit gates. The 253-line happy path (gates →
resume staging → worktree setup → spawn → hand-off to `_loop`) never executes in CI.

The drift this invites is already visible in the tree: `test_mode_switching.py:62` documents itself as
*"Reproduce the startup hook sequence from `ProxyRunner.run()`"* — a test that **re-implements** the
sequence instead of calling it. A copy like that stays green while the original changes.

This is also the class of bug the project has already been bitten by. From the maintainer's own notes:
*"Startup resume bypasses staging — `run()`→`_spawn()` skips `_stage_backend_resume` that `_new_session`
uses; fixes added only there miss the startup resume."* That defect is invisible to every unit test in
the suite and would be caught by a single test that runs `run()` against a fake PTY.

**Conclusion: the top priority is not more coverage. It is one composition-level harness.**

---

## 2. Backend parity — the largest single gap

aGiTrack ships two backends. The suite tests one.

- **6** test functions in the entire suite parameterize over both backends
  (`test_manual_commits.py:387,432,940`; `test_background.py:260,304,1300`).
- `ProxyRunner.for_testing()` seeds `state_data["backend"] = "claude"` (`runner.py:1524`). Every one of
  those 263 `make_runner()` tests therefore runs on Claude, silently.
- **Zero** tests invoke a real `claude` or `opencode` binary. The only `shutil.which`-gated tests are
  for `node`, `sandbox-exec` and `bwrap`.

That contradicts the project's own standing rule (*"every feature must work on Claude AND OpenCode,
verified by actually invoking each backend"*) — the rule exists but nothing enforces it.

### 2a. The concrete bug this hides — OpenCode turn-end detection

`ClaudeProxyAgent` implements `session_transcript_path()` and `session_last_activity()`.
`OpenCodeProxyAgent` implements **neither**, and `agitrack/transcripts/opencode.py` has no equivalent.
Both call sites reach them through `getattr(..., None)` (`runner.py:3970`, `runner.py:12189`), so on
OpenCode they degrade silently to `None`.

Trace `_backend_idle_for` (`runner.py:12204`) with `mtime = None`:

```python
transcript_quiet_for = None                      # no transcript on OpenCode, ever
if transcript_quiet_for is not None and ... :    # skipped
if time.monotonic() - self.last_child_output >= seconds:
    return True                                  # PTY-only — commits mid-turn if a sub-agent is quiet
return transcript_quiet_for is not None and ...  # ALWAYS False if the PTY chatters
```

Both failure modes the docstring was written to prevent are live on OpenCode, with no second opinion
available:

1. **Never commits.** If OpenCode's TUI emits an idle heartbeat (Claude's measured at 8 bytes/sec on
   Linux), the PTY is never quiet, the transcript can't overrule it, `_maybe_agent_commit` never fires,
   and the session commits *nothing* until the user types their next prompt.
2. **Commits mid-turn.** A quiet OpenCode sub-agent reads as "turn finished", so aGiTrack commits a
   half-finished turn — and in `--no-worktree` mode offers to commit the user's own changes too.

Failure mode 1 is platform- and version-specific by nature (it reproduced on Linux and not macOS for
Claude), which is exactly why it needs a test rather than a manual check.

### 2b. No interface conformance test

`ProxyAgent` is a `Protocol`, so nothing verifies the concrete classes satisfy it. Two live drifts:

- `retarget_working_dir()` is implemented on **both** concrete agents and called by the runner, but is
  **not declared on the Protocol** — it is an undocumented part of the real contract.
- `session_last_activity()` is declared nowhere and exists only on Claude.

A `Protocol` with `runtime_checkable` proves nothing about method sets here; the check has to be
explicit.

---

## 3. Under-covered user-facing modes

| Mode | Entry | Coverage | Tests |
|---|---|---|---|
| Interactive proxy (TUI) | default | 75% (leaves only) | ~600 |
| JSON prompt-loop | `--json` / `--prompt` | **43%** | 10 (`test_shell.py`) |
| UI bridge (VSCode extension) | `--ui-bridge` | 43% (same module) | 14 (`test_bridge.py`) |
| Headless recovery | `--recover` | 82% | 6 (`test_recovery.py`) |
| Backtrace daemon | `--backtrace` | **66%** | — |

`shell/runner.py`'s misses are its dispatch spine: `_handle_agent_prompt` (34), `_handle_command` (28),
`_handle_pre_compaction` (25), `_handle_summarizer_command` (24), `run()` (23), `_bridge_command` (23),
`_bridge_switch_backend` (16). The UI bridge is how the **VSCode extension** talks to aGiTrack — a
regression there breaks every extension user, and `FLOW_MATRIX.md` has no section for it, `--json`, or
`--recover` at all.

---

## 4. Platform and process gaps

- **Windows CI runs 4 of 86 test files** (`test_proc`, `test_dashboard_daemon`, `test_shutdown_request`,
  `test_windows_conpty`). Commit engine, worktree, session store, config, dashboard and share flows —
  none platform-specific in intent, all full of path handling — are never executed on Windows. The MSI
  is built and installed on every PR, so a user can install a build whose commit path never ran on
  their OS.
- **No coverage floor.** CI runs `coverage report` with no `--fail-under`, and (as the workflow's own
  comment notes for a past Windows bug) a step that always exits 0 hides regressions. Coverage can
  decay silently.
- `proc.py` 59%, `proxy/terminal.py` 61%, `proxy/process.py` 60% — subprocess/termios/pty edges, i.e.
  precisely where cross-platform bugs live.

## 5. Housekeeping

`agitrack/proxy/runner.py.bak` and `.bak3` (723 KB each) are **committed to git**. They are dead
weight, they confuse grep-based navigation, and a future `packages.find` or lint glob change can pick
them up. Delete them.

---

# Current status (2026-08-03) — plan complete

**`2801 passed, 10 skipped`. Ruff, ruff format and mypy clean. Coverage 81.45% against a 78%
floor. ~1m40s under `-n auto`, down from 6m12s. Three consecutive full parallel runs green,
plus 6 live tests against the real Claude and OpenCode CLIs.**

Every item in the plan below is now implemented. The five that were still outstanding at the
first pass — and were found by re-auditing rather than by remembering — are done:

| Was outstanding | Now |
|---|---|
| P0: "reactor commits a turn end-to-end" | `test_reactor_commits_a_finished_turn_into_real_git` drives prompt → finished parse → timers phase → a real commit, plus the mirror that a still-running turn is NOT committed |
| P0: retire the `run()` reimplementation | the four-call startup hook sequence is now `ProxyRunner._reset_hook_slate`; `run()` and `test_mode_switching.py` call the SAME method, and `test_startup_installs_the_hook_slate_through_the_shared_method` pins that they stay that way |
| P2: backtrace daemon lifecycle | `tests/test_backtrace_daemon.py`, 13 tests → `FLOW_MATRIX.md` §12g |
| P2: `--recover` had no matrix section | `FLOW_MATRIX.md` §12f |
| P3: the remaining edge cases | `tests/test_durability.py` (10), `tests/test_token_accounting.py` (15), and the terminal/OpenCode-parser additions to `tests/test_edge_cases.py` |

## Three more real defects, found by the tests written for them

4. **OpenCode under-reported context by the whole cache.** It computed `context = input`, while
   Claude sums `input + cache_read + cache_write`. On a live OpenCode turn that is 727 against a
   true 6365 — the dashboard's context gauge reading ~9x low, and not comparable across backends.
   A pre-existing test had pinned the wrong formula, which is why it survived.
5. **`Session.master_fd` / `child_pid` raised instead of returning None.** Both are documented as
   `None` when there is no process ("not spawned yet, or torn down"), and the runner is full of
   `if session.master_fd is None` guards written for exactly that state — every one of which
   raised `AttributeError` on the one case it exists for.
6. **The OpenCode event stream carries no model** (verified against the real CLI), so every
   headless run recorded `model=None`. Now read from its session store.

Settled while testing, rather than fixed: OpenCode's step events are **per-step, not cumulative**
— each `total` is exactly that step's own input+output+reasoning+cache. So `_read_events` summing
them is correct. That was flagged as an open risk in the original audit; it is now a recorded
fact, pinned against real captured output in `tests/test_token_accounting.py`.

## Making the suite safe to parallelize

Turning on `-n auto` surfaced three latent problems. None was a product bug; all three were tests
relying on having the machine to themselves, and each needed a different fix:

* **Shared OS resources — ptys.** Concurrent workers forking pseudo-terminals exhaust the pool,
  surfacing as an unrelated-looking failure that never reproduced in isolation. Every pty-forking
  module is pinned to one worker via `xdist_group("pty")` + `--dist loadgroup`.
* **Shared OS resources — TCP ports.** `test_dashboard_network.py` picks a free base port and
  asserts `base+1`/`base+2` are allocated consecutively; another worker can take one in between.
  Waiting longer cannot fix that — the contention has to be removed — so every port-binding
  module shares `xdist_group("net")`.
* **Wall-clock bounds on thread scheduling.** Three waits in `test_story.py` measured how soon a
  background thread got scheduled against 14 competing workers. The agent is faked in all of
  them, so elapsed time was never the claim.

Worth knowing before adding tests that touch a global resource: a new pty- or port-binding module
should join the relevant group.

---

# Outcome (implemented 2026-08-03)

The plan below was carried out in full. Result:

```
BEFORE   2607 passed, 10 skipped   79% coverage   6m12s (serial)
AFTER    2718 passed, 10 skipped   81% coverage   1m15s (pytest -n auto)
         + 6 live tests behind `-m live`, run against the real Claude and OpenCode CLIs
```

**Four real defects were found and fixed, not just covered:**

1. **OpenCode had no turn-end signal at all.** `_backend_idle_for` needs a signal that advances
   only on real backend work; OpenCode answered neither `session_transcript_path` nor anything
   else, so the runner silently fell back to the PTY alone — which fails in both directions
   (never commits behind an idle heartbeat; commits mid-turn behind a quiet sub-agent). Fixed by
   `transcripts/opencode.session_activity_mtime`, a session-scoped read-only query against
   OpenCode's SQLite store. Session-scoping is load-bearing: the database file's own mtime
   advances for ANY session, so statting it would reintroduce the never-commits failure.

2. **A native session switch stranded the previous conversation's summary work.** `/clear`,
   `/resume` and OpenCode's picker repoint one aGiTrack session at a different conversation while
   the old one's summary state stays in the slots — so its result was applied and reported
   against the new conversation (the amend failing, since the sha was no longer head), and the
   stale rolling summary became INPUT to every later summary. Fixed in
   `_abandon_summary_for_switched_session`.

3. **Slash commands carrying user instructions were dropped from the trace entirely.** `/goal …`
   and `/loop …` get no `isMeta` expansion row, so the invocation was remembered, never consumed,
   and discarded — a commit driven by a paragraph-long `/goal` recorded no prompt at all. Fixed
   behaviourally rather than by a list of names (Claude Code keeps adding commands, and skills are
   invoked the same way): *a slash command with arguments that the agent then responds to is a
   user instruction.*

4. **OpenCode reported no model.** Found by the live tests on their first run: OpenCode's
   `--format json` event stream names no model anywhere, so every headless run recorded
   `model=None` in the commit metadata while Claude recorded a real one. Fixed by reading it from
   the session store. This is precisely the drift class mocks cannot see.

Two robustness fixes fell out of the edge-case work: the Claude transcript reader now uses
`errors="replace"` (one undecodable byte — a torn multi-byte character mid-append — made the
WHOLE session unparseable, i.e. no commits at all, silently), and `tests/conftest.py` now anchors
the repo root on `sys.path` (five test modules were relying on the editable-install layout, so a
plain `uv sync` broke their collection).

**Infrastructure:** `pytest -n auto` (xdist) cuts the suite from 6m12s to 1m15s; coverage moved to
`pytest --cov` because a wrapping `coverage run` never enters the xdist workers and reported 10%
with every test passing; a `fail_under = 78` floor now makes a coverage regression fail the build;
the Windows job grew from 4 test files to 15, adding the platform-agnostic core (commit engine,
state, settings, paths, git/worktree) that had never executed on Windows despite the MSI being
built and installed on every PR; and the two 723 KB `runner.py.bak*` files were removed.

**Coverage moved where it was supposed to:** `runner.py` 75% → 79% (1,825 → 1,494 missed lines,
almost all of the recovery in `run()` and the reactor phases), `shell/runner.py` 43% → 56%,
`transcripts/claude.py` 83% → 84%, `transcripts/opencode.py` 79% → 80%.

Remaining gaps are recorded in `FLOW_MATRIX.md` under "Known gaps / TODO".

---

# The plan

Ordered by *(user-visible risk) ÷ (effort)*. P0 is the one that changes the suite's shape; everything
after it is additive.

## P0 — Composition harness: drive `run()` and the reactor end to end

**Why first.** It converts the single largest blind spot (1,825 missed lines, all sequencing) into
testable surface, it is the only thing that can catch the `run()`-vs-`_new_session` divergence class,
and every later priority reuses it.

**Build** `tests/harness.py`:

- `fake_backend_pty()` — a scripted PTY pair. The test writes canned backend output and the harness
  answers `select()`, so no real binary and no wall-clock waits are involved.
- `drive(runner, script)` — pumps `_loop` for a bounded number of iterations against a monotonic clock
  stub (`time.monotonic`/`time.time` injected, never slept on).
- `run_to_first_paint(**launch_kwargs)` — calls the real `ProxyRunner.run()` with the fake PTY and stops
  at the first render, returning the ordered list of side effects (staged resumes, worktrees created,
  hooks installed, spawn argv).

**Tests to land on it** (real-git):

1. `test_startup_stages_the_backend_resume_before_spawning` — the recorded regression: assert
   `_stage_backend_resume` ran on the `run()` path, not only via `_new_session`. **Write this one first;
   it is a known past defect with no guard.**
2. `test_startup_sequence_order_is_gates_then_resume_then_worktree_then_spawn` — pins the order so a
   future insertion at the wrong point fails loudly.
3. `test_reactor_commits_a_turn_end_to_end` — prompt → scripted turn output → idle → commit exists in
   real git, driving `_loop`, not `_maybe_agent_commit` directly.
4. `test_reactor_survives_a_backend_that_exits_mid_turn` — relaunch + resume, no crash-loop.
5. `test_startup_hook_sequence_is_the_real_one` — then **delete the reimplementation** at
   `test_mode_switching.py:62` and point it at the harness.

## P1 — Backend parity, enforced structurally

1. **Conformance test** (`tests/test_backend_parity.py`): for every name in `available_backends()`,
   assert the agent exposes the full `ProxyAgent` method set with matching signatures
   (`inspect.signature`). Fix the Protocol as it surfaces: **declare `retarget_working_dir`**, and
   either declare `session_last_activity`/`session_transcript_path` as optional-with-default or
   implement them for OpenCode.

2. **Fix the OpenCode heartbeat (§2a)** — this is a bug fix, not just a test. Give
   `transcripts/opencode.py` a `session_transcript_path()` (or a `session_last_activity()` reading the
   OpenCode store) so `_backend_idle_for` has its second opinion. Then test both directions on **both**
   backends:
   - `test_turn_end_is_detected_despite_a_chattering_idle_heartbeat[claude|opencode]`
   - `test_quiet_subagent_does_not_read_as_turn_end[claude|opencode]`

3. **Parameterize the parity-relevant core.** Add a `backend_name` fixture and lift `for_testing()`'s
   hardcoded `"claude"` to it. Do **not** double the whole suite — parameterize only where the backend
   is genuinely a variable: turn parse → commit, session discovery/adoption, resume + cwd retarget,
   token accounting, session switching, share/import. Target ~60–80 parameterized tests, not 600.

4. **Opt-in live smoke** (`-m live`, deselected by default like `timing`, gated on `shutil.which`):
   one real turn per installed backend — spawn, prompt, turn lands, commit created, tokens non-zero.
   This is the only thing that catches a backend CLI changing its JSON/event shape under us, and it is
   what the project's own "test on both backends live" rule actually asks for.

## P2 — The modes nobody tests

- **UI bridge / JSON loop** (`shell/runner.py` 43% → target 80%): a `tests/test_bridge_protocol.py`
  that feeds recorded VSCode-extension request frames and asserts the response frames — covering
  `_bridge_command`, `_bridge_switch_backend`, `_handle_command`, `_handle_summarizer_command`,
  `_handle_pre_compaction`, and malformed/partial/unknown-verb frames.
- **Backtrace daemon** (66%): start → serve → shutdown, port already bound, and stale-pidfile recovery.
- **Add `FLOW_MATRIX.md` sections** for JSON mode, the UI bridge and `--recover`. They are user flows
  with no matrix rows today, so the matrix currently overstates completeness.

## P3 — Edge cases worth explicit tests

Chosen because each is a *silent* failure — the user loses work or trust without an error message.

**Data integrity (highest value)**
- Backend killed (SIGKILL) mid-turn with a dirty worktree → next launch recovers, never commits a
  half-turn. Both backends.
- Disk full / read-only `.agitrack/` during commit → clear error, no corrupt state file.
- Two aGiTrack sessions on one repo racing the same base branch → lock holds, no interleaved commit.
- Repo `HEAD` moved by an external `git checkout` mid-session → detected, not silently merged into the
  wrong branch.
- Backend writes a **truncated / half-written transcript line** (it is being appended to as we read) →
  parser must not drop the turn. Both parsers, both backends.

**Token accounting** — `OpenCodeBackend._read_events` sums `TokenUsage` per event
(`backends/opencode.py:_add_tokens`). If OpenCode emits *cumulative* totals on part updates rather than
deltas, every turn over-counts. Add a test asserting the arithmetic against a recorded real event
stream, and the Claude equivalent for `cache_read`/`cache_write` double-counting across sidechains.

**Unicode / encoding** — `test_git_commit_encoding.py` covers the cp1252 case; extend to a prompt
containing a lone surrogate, a NUL byte, and a 1 MB paste, on both backends.

**Terminal** — window resize mid-turn; `SIGWINCH` during a modal; bracketed paste split across two
`read()` calls (the `_observe_paste_marker` state machine at `runner.py:458` is byte-at-a-time and its
split-chunk path is worth pinning); Ctrl-Z suspend/resume while a popup is open.

## P4 — Guardrails

- `--fail-under=78` on `coverage report` in CI (one point below current, so it ratchets and never
  blocks a legitimate refactor).
- Extend the Windows CI job from 4 files to the platform-agnostic core: `test_commit_engine`,
  `test_state`, `test_settings`, `test_session_names`, `test_paths`, `test_fileio`, `test_git_repo`,
  `test_worktree`. These have no POSIX-terminal dependency and are where path bugs hide.
- `git rm agitrack/proxy/runner.py.bak agitrack/proxy/runner.py.bak3`.

---

## Sequencing

| Step | Work | Buys |
|---|---|---|
| 1 | P0 harness + test 1 (the known regression) | the composition blind spot, and a guard on a defect that already shipped |
| 2 | P1.1 conformance + P1.2 OpenCode heartbeat fix | a real P0-class OpenCode bug, closed |
| 3 | P1.3 parameterization + P4 guardrails | parity stops regressing silently |
| 4 | P2 bridge/JSON + matrix rows | the VSCode extension stops being untested |
| 5 | P1.4 live smoke + P3 edge cases | backend-CLI drift and the silent-data-loss set |

Steps 1–2 carry most of the risk reduction. Everything after is steady widening.
