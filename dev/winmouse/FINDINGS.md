# Task A findings — ConPTY mouse pass-through (deterministic, in-VM)

Built a deterministic harness (`dev/winmouse/`) and ran it against the raw ConPTY driver and
the **real** claude/opencode backends on this Windows build (26200). Results overturn the
handoff's root-cause hypothesis. Summary of what is actually true here.

## Harness gotcha (important)
ConPTY children only bind their std handles to the pseudoconsole when launched from a **real
attached console**. The non-interactive PowerShell/Bash agent tools do NOT provide one, so the
child's output leaks to the parent console and its stdin handle is invalid (`ERROR_INVALID_HANDLE`).
The *existing* `test_windows_conpty.py` also fails this way under the agent shell. Run everything
via `Start-Process` (fresh console) and have the script write results to a file:
```
Start-Process <uv> -ArgumentList 'run','--python','3.12','python','dev/winmouse/harness.py' -Wait
```

## What conhost actually does with input-pipe mouse bytes
Probe: write `\x1b[<0;5;5M` (SGR left-press at col5/row5) into the ConPTY input, vary the child's
console input mode.

| child console mode | what the child receives |
|---|---|
| VT-input (`ENABLE_VIRTUAL_TERMINAL_INPUT`) + mouse | `\x1b[<0;5;5M` **verbatim** (passthrough → mouse works) |
| VT-input **+ win32-input-mode** (`?9001h`) | win32 KEY records, one per char (`\x1b[0;0;27;1;0;1_` …) — the "leak as keystrokes" |
| `ENABLE_MOUSE_INPUT`, **no** VT-input (classic `ReadConsoleInput`) | a real **`MOUSE_EVENT_RECORD`** `pos=(4,4) btn=0x1` |
| VT-input + `ENABLE_MOUSE_INPUT` | per-char KEY records (VT-input wins; not parsed as mouse) |

Conclusions:
- **ConPTY CAN deliver mouse.** For a VT-input child it passes SGR mouse through unchanged; for a
  classic `ReadConsoleInput` child it produces a real `MOUSE_EVENT_RECORD`.
- The only broken combination is **VT-input + win32-input-mode**, where passthrough chars are
  re-encoded as win32 key records.
- **conhost emits `\x1b[?9001h\x1b[?1004h` at startup for EVERY ConPTY child**, unconditionally.
  It is conhost asking the connected terminal to send win32 input/focus — NOT proof the child
  enabled win32-input-mode.

## What the real backends actually do (probe_backend.py)
Captured each backend's terminal init under our ConPTY and counted DEC private modes:

- **claude**: `?9001h` count = **1** (conhost startup only — claude does NOT request win32-input-mode).
  Enables mouse `?1000/1002/1003/1006h`, bracketed paste `?2004h`, **kitty keyboard** `CSI>1u`,
  modifyOtherKeys `>4;2m`, `?2031h`.
- **opencode**: `?9001h` count = **1** (conhost startup only — opencode does NOT request win32-input-mode).
  Enables mouse `?1000/1002/1003/1006h`, bracketed paste, modifyOtherKeys `>4;1m`, `?2027h`, `?2031h`.

**Neither backend uses win32-input-mode.** The handoff's premise ("the backend turns on
`?9001h`") was a mis-attribution of conhost's unconditional startup emission. ⇒ Implementing
win32-input-mode input encoding in the driver would fix nothing.

## End-to-end injection into the live backends (inject_backend.py)
Spawned each backend, let it paint its TUI (it enables mouse itself), then forwarded:
`MARKERxyz` → click `\x1b[<0;5;5M\x1b[<0;5;5m` → wheel `\x1b[<64;10;10M` → `END`.

- Both backends: the marker text rendered normally; the click/wheel produced **no output and no
  leak**; `END` rendered cleanly immediately afterwards. **No `[<…M` literal text ever appeared.**
- i.e. forwarding clean SGR button/wheel mouse to either backend through our ConPTY works — it is
  consumed as a mouse event (silent here only because nothing was actionable at those coords).

## So what was the original leak?
Not a ConPTY input-encoding problem. It is **host-side motion tracking over RDP**: the host
console floods `?1002/1003h` motion reports that the RDP console does not round-trip cleanly
(split/garbled sequences), which then forward to the backend and render as literal `[<35;..M`
in its input box. That is a host-terminal artifact, not the ConPTY input path — clean *injected*
motion sequences pass through identically to button/wheel, but the *host's own* motion stream over
RDP does not.

## CONFIRMED by live real-mouse testing (the user)
- **A single backend works fully** — mouse click, wheel/scroll, drag-select **copy** — no leak.
- **Switching backends leaks** (e.g. Claude→OpenCode): mouse codes (`[<0;..`, `[<35;..`) appear in
  the backend's input box, AND keyboard input is corrupted — a phantom char on Enter, and Ctrl-C
  stops exiting. Switch-only; a single backend is clean.
- An earlier motion-only mitigation (`_no_host_enable = {1002,1003}`) **broke backend copy**
  (drag-select needs motion) and only masked one symptom of the switch carryover — reverted.
- A mouse scroll while the exit "Finalizing…" message showed also leaked (host mouse still on
  during the cooked, stdin-unserviced teardown) — fixed separately.

## Final fix (what landed)
1. **Drop the win32-input-mode plan** — unnecessary; neither backend uses it.
2. **Forward ALL mouse (button/wheel/click/motion) + focus to the backend** on Windows, same as
   POSIX: `child_mouse` tracks the backend's real `?1000h` (no longer forced `False`); no
   mouse/focus stripping; motion mirrored to the host (the backend needs it for drag-select/copy).
3. **Disable host terminal modes at the start of exit finalize** so a scroll during the cooked
   "Finalizing…" teardown can't echo its raw report; re-enabled if exit is aborted.

## OPEN ISSUE — switching backends carries over state (single backend is fine)
That switching breaks Enter/Ctrl-C *and* mouse together points to a **process-global host/terminal
state set by the first backend that the second doesn't reset** (a keyboard-protocol mode, host
console mode, or input-pipeline state). Per-session input state (`_input_tail`, `passthrough_escape`,
`child_mouse`) is reset on switch, and the foreground output path *does* mirror the new backend's
modes (`_sync_terminal_modes` at the `<` drain), so the obvious candidates are ruled out — needs a
byte-exact `DEBUG_RAW=1` capture of the post-switch input (`.agitrack/proxy-raw-*.log`) to pin it.
