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

## Task A — make mouse (and rich keys) pass through to the backend (the real fix)
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

**Plan:**
1. Detect the agent's switch: poll `self.repo.current_branch()` (cheap `.git/HEAD` mtime gate, like
   the existing checks) and notice when the worktree is on a branch aGiTrack didn't put it on — i.e.
   NOT detached-at-base and NOT a managed `agitrack/<name>/tN` branch, and it changed. Distinguish
   aGiTrack's own changes (set a flag around `_ensure_turn_branch`/detach/integrate) from the agent's.
2. Follow: update `_base_branch` / `_repo_dir_branch` / `_integration.base_branch` and the status-bar
   branch; `self._set_message("Working branch switched to '<X>' by the backend agent.")` + `_render()`.
3. Make commits work on the followed branch: relax the `is_managed_branch` gate so the agent-chosen
   branch is treated as the working branch — cover commits land on it, and the integration target
   becomes that branch (or its merge base). Verify `_maybe_agent_commit` (~9322) →
   `_finish_agent_parse_if_ready` → the `<aGiTrack>` cover-commit path runs on it.
4. Test the real flow in the VM (worktree AND `--no-worktree`): give the backend a prompt that does
   `git checkout -b feature` (or checks out an existing branch), confirm the popup fires once, the
   status bar updates, and an `<aGiTrack>` cover commit lands on `feature`. (no-worktree already
   "follows" via `_check_no_worktree_branch_change`, but verify cover commits aren't blocked by
   `is_managed_branch` there either.)

## Verification constraints to remember
- Keep `./scripts/check.sh` green (1668 tests) — POSIX path must be untouched; Windows-only code is
  guarded by `os.name`/`sys.platform`.
- "Every feature must work on BOTH backends, verified by a real run" (AGENTS.md) — test claude AND
  opencode, proxy mode, for anything touching input/rendering/branching.
