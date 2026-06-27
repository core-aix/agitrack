# Handoff — native-Windows terminal work (continue inside the Windows VM)

This is a working note for a Claude Code session running **inside the Windows VM**, picking up
where a host-side (Linux/QEMU) session left off. The host session could edit/build/push and drive
the VM via the QEMU monitor, but **could not inject mouse events** and couldn't iterate fast against
a real backend. Running in the VM removes both limits.

Branch: `msi-install-and-ci`. First: `git fetch && git checkout msi-install-and-ci && git pull` so
you have every commit below, and confirm `git push` is authenticated.

## How to run / iterate in the VM
- Source checkout is at `C:\agit2` (uv-managed). Run aGiTrack directly, no MSI rebuild:
  `& "$HOME\.local\bin\uv.exe" run --python 3.12 python -m agitrack --backend claude --repo <repo> --new-session`
  (add `--no-worktree` to test that mode; `--skip-privacy-ack` to skip the prompt).
- Gate before pushing (POSIX must stay green): `./scripts/check.sh` from a POSIX checkout, OR at
  minimum keep Windows-only code behind `os.name == "nt"` / `sys.platform` with `# type: ignore`.
- Backends: `claude` (npm `claude.cmd`) and `opencode` (npm `opencode.cmd`) are installed.

## What's already fixed this session (all pushed)
- **Raw ConPTY driver** `agitrack/proxy/platform/_conpty.py` replaced pywinpty (pywinpty's ConPTY
  killed every child with STATUS_CONTROL_C_EXIT when PyInstaller-frozen → MSI couldn't launch any
  backend). Four invariants that each broke a launch (documented in AGENTS.md "Native Windows"):
  read into a **`c_ubyte`** buffer; pass the **HPCON by value** (not `byref`) to
  UpdateProcThreadAttribute; **`EXTENDED_STARTUPINFO_PRESENT` only** (NOT `CREATE_NO_WINDOW`);
  `bInheritHandles=FALSE`. `poll()` waits for the reader thread to finish draining.
- **`SetConsoleCtrlHandler`** in `_winconsole.py` swallows Ctrl-C/Ctrl-Break so a stray console
  Ctrl-C (e.g. propagated from OpenCode's ConPTY teardown) can't raise SIGINT → it was crashing the
  reactor inside a menu popup and interrupting input.
- **Idempotent `RawConsole.enter()/leave()`** (`_winconsole.py`, `_entered` flag) — a redundant
  `set_raw()` no longer re-flushes input (dropped keystrokes) or re-saves the raw modes over the
  cooked ones (broke terminal restore, esp. over RDP after a turn).
- **`_drain_pty_until_done_or_esc`** (runner.py ~4811) now stashes non-cancel input on `_input_tail`
  instead of discarding it (first keystrokes after a switch were lost while a fetch was in flight).
- **Mouse/focus on Windows** (`_sync_terminal_modes` + `_reactor_stdin_phase`): don't mirror motion
  tracking 1002/1003 to the host; `child_mouse` forced False; focus events (`\x1b[I`/`\x1b[O`)
  stripped from forwarded input. **NOTE:** the `child_mouse=False` + mouse-strip part is a BAND-AID
  to stop codes leaking as text — it's the opposite of the desired design (pass mouse through). It
  must be **reverted** once Task A makes pass-through actually work. (The focus-strip and not-mirror-
  motion bits may stay or be folded into the real fix.)

## Task A — STATUS: largely RESOLVED; premise was wrong (see dev/winmouse/FINDINGS.md)
**What was actually found (deterministic harness + live-backend injection, in-VM, build 26200):**
- ConPTY **does** deliver clean mouse: it passes SGR button/wheel/**motion** and focus events
  through to a VT-input backend **verbatim**; injecting them into live claude+opencode produced
  **no leak**.
- **Neither backend enables win32-input-mode.** The `\x1b[?9001h` seen in their output is
  conhost's *unconditional startup emission*, NOT the backend — so implementing win32-input-mode
  input encoding (the plan below) would have fixed nothing. **That plan is dropped.**
- The original leak was a **host-side artifact** (motion/focus reports mangled by the RDP console),
  not the ConPTY input path.

**What was done (final, after real-mouse RDP testing):**
- Forward button/wheel/click + focus to the backend: `child_mouse` tracks the backend's real
  `?1000h` on Windows (no longer forced `False`); focus-event strip removed. **Verified working.**
- **Motion (1002/1003) is NOT mirrored to the host on Windows** (`_no_host_enable = {1002,1003}`):
  real-mouse testing over RDP confirmed the host's own motion stream leaks as literal `[<35;..M`
  in the backend's input box (button 35 = `?1003h` hover). Backend loses hover/drag, keeps
  clicks/wheel. The original band-aid was right about *this specific* risk.
- **Exit finalize disables host terminal modes up front** so a scroll during the cooked,
  stdin-unserviced "Finalizing…" teardown can't echo its raw mouse report (re-enabled on abort).

**Resolved.** The only behaviour given up is backend hover/drag motion over RDP — an inherent
host-console limitation, not the ConPTY path. If that's ever wanted, capture the raw host motion
bytes over RDP first to see exactly how they're mangled.

### (obsolete) original plan — kept for context only
**Design goal (from the user):** aGiTrack should forward **all** keys and mouse events to the
backend agent unchanged, intercepting **only the menu key** (Ctrl-O / the configured `menu_key`).
Backends evolve their own key/mouse handling; aGiTrack must not block them.

**Why it doesn't work on Windows today:** there is no PTY — there's ConPTY with `conhost.exe` in the
middle. The backend turns on **win32-input-mode** (`\x1b[?9001h` — confirmed in its output), and
**aGiTrack's ConPTY driver doesn't implement that input handshake** (grep: zero `9001` references).
So when aGiTrack writes plain-VT mouse bytes into the backend's ConPTY input, conhost re-encodes them
as keystrokes → the backend receives the literal text `\x1b[<0;..M` (the leak). Plain typed text
happens to round-trip; mouse does not. This is a ConPTY property, **not** a Python one — node-pty (a
TypeScript rewrite) sits on the same ConPTY; VS Code's terminal passes mouse to TUIs only because
**xterm.js implements the win32-input-mode input encoding**.

**Plan:**
1. Implement win32-input-mode in the ConPTY input path (`NtChildProcess.write` / `_conpty.py`):
   when the backend has enabled `?9001h`, encode the input aGiTrack writes accordingly. Reference:
   the microsoft/terminal "win32-input-mode" spec and how xterm.js (`InputHandler`/`coreMouseService`)
   + node-pty encode key and mouse input. Track per-child whether 9001 is active (parse it out of the
   backend's output stream, same place `_sync_terminal_modes` watches modes).
2. **Build a deterministic test harness FIRST (no physical mouse needed):** a small script that spawns
   a ConPTY child which enables mouse (`?1000h?1006h`) and prints whether it received a *mouse event*
   vs literal text; write VT mouse bytes via the driver and assert. Iterate the encoding until it
   passes. This is the whole reason to be in the VM — it makes the ConPTY-input work a tight,
   self-checking loop.
3. Once mouse delivers correctly: **revert the band-aid** (restore `child_mouse` tracking from the
   backend's modes; stop stripping mouse; re-mirror modes) so aGiTrack is a clean pass-through, and
   make sure **only the menu key** is intercepted (audit `self.input.feed` / the reactor for any other
   keys aGiTrack swallows — PgUp/PgDn message-scroll, scroll handling — and forward them to the backend
   unless the user is in aGiTrack's own UI/menu).
4. If, after a correct handshake, ConPTY still won't deliver mouse to the child (historically a thorny
   area), document precisely what fails — that's the evidence for weighing a different terminal stack.
5. The user (real mouse) does the final confirm: scroll/click/select inside live claude + opencode.

## Task B — follow the agent's branch switch (cover commits currently stop)
**Desired behavior:** when the backend agent switches the git branch (runs `git checkout`/`git switch`
in the working tree it edits), aGiTrack should automatically **follow** the new branch — update the
monitoring target and the branch shown in the status bar, pop a message "Working branch switched to
'<X>' by the backend agent", and keep making commits (including `<aGiTrack>` cover commits) on it as
normal.

**Scope (clarified by the user):** the worktree **directory does not move** — it's an in-place
`git checkout`/`git switch` inside the same `.agitrack/worktrees/<name>` dir, so `self.repo`'s path
is unchanged and the agent's working-tree edits carry over onto the new branch. The ONLY thing that
changed is `self.repo.current_branch()`. So detection is purely a branch poll on `self.repo` (no
directory/worktree re-creation involved), and the follow just needs to (a) update what aGiTrack
tracks/shows and (b) let the commit pipeline run on the new branch.

**Why it's broken today (root cause, with locations):**
- The drift detectors poll the **base repo directory** (`self.base_repo`), not the **session's
  worktree** (`self.repo`) where the agent actually works: `_check_base_branch_drift` (runner.py
  ~1460) and `_check_no_worktree_branch_change` (~1534). In worktree mode (plain `agitrack`) the
  agent's in-place checkout happens inside `.agitrack/worktrees/<name>`, so it's never noticed.
- The cover-commit / backend-commit accounting bails out on a non-managed branch:
  **runner.py:8026 `if not is_managed_branch(branch): return []`** (and likely other
  `is_managed_branch` call sites — grep them). aGiTrack's turn-branch model assumes it owns the
  branch (`agitrack/<name>/tN`, created by `_ensure_turn_branch` at ~2974, detached at base between
  turns). The moment the agent moves the worktree to `main`/`feature`, aGiTrack stops processing.

**Status: core implemented + unit-tested (host).** `_follow_agent_worktree_branch()` (runner.py,
called in the timers phase next to `_check_base_branch_drift`) polls `self.repo.current_branch()`
(throttled by `_agent_branch_check_at`); the agent's in-place switch is uniquely identified by the
worktree being on a NON-managed, named branch (`current != "HEAD"`, `not is_managed_branch(current)`,
`current != self._base_branch`) — because aGiTrack only ever leaves a worktree detached-at-base or on
a managed `agit*/...` turn branch. On detection it sets `_base_branch`/`_repo_dir_branch`/
`_integration.base_branch` to the new branch (status bar + integration follow), pops "Working branch
switched to '<X>' by the backend agent.", and the existing `is_managed_branch` gates then correctly
*skip* auto-integrating a branch the agent owns. The turn cover-commit path
(`_finish_agent_parse_if_ready` → `CommitEngine.finish_parse_if_ready`) does NOT gate on the branch,
so cover commits land on the followed branch. Tests: `test_follows_agent_worktree_branch_switch`,
`test_does_not_follow_managed_turn_branch_or_detached_head`,
`test_no_worktree_session_does_not_poll_worktree_branch` in `tests/test_proxy.py`.

**Remaining (verify/refine in the VM with a real backend):**
1. End-to-end: prompt the backend to run `git checkout -b feature` (in-place), confirm the popup fires
   once, the status bar shows `feature`, and a `<aGiTrack>` cover commit for the turn lands on
   `feature`. Test BOTH backends, worktree mode.
2. Covering the agent's OWN git commits on the followed branch: `_uncovered_backend_commits()`
   (runner.py:8026) returns `[]` for a non-managed branch, and after the follow `_base_branch ==
   branch` so `log_shas(base, branch)` is empty anyway — so an aGiTrack metadata commit won't *cover*
   commits the agent made itself on `feature`. If that matters, record the HEAD where the follow
   happened as the cover baseline and walk from there instead of `_base_branch`.

## Verification constraints to remember
- Keep `./scripts/check.sh` green (1668 tests) — POSIX path must be untouched; Windows-only code is
  guarded by `os.name`/`sys.platform`.
- "Every feature must work on BOTH backends, verified by a real run" (AGENTS.md) — test claude AND
  opencode, proxy mode, for anything touching input/rendering/branching.
