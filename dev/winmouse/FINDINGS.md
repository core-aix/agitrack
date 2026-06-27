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

## CONFIRMED by live real-mouse testing (the user, over RDP)
After shipping full pass-through (button/wheel/motion forwarded, motion mirrored to host):
- Button/wheel/click forwarding to a freshly-started backend: **works, no leak.**
- **Motion leaks**: scrolling/hovering after a Claude→OpenCode switch produced `[<35;30;10M`
  (button 35 = no-button motion = `?1003h` hover) in OpenCode's input box. Starting a backend
  alone mostly hid it; the switch made it reliable.
- A mouse scroll while the exit "Finalizing…" message showed also leaked (host mouse still on
  during the cooked, stdin-unserviced teardown).

## Final fix (what landed)
1. **Drop the win32-input-mode plan** — unnecessary; neither backend uses it.
2. **Forward button/wheel/click + focus to the backend**: `child_mouse` tracks the backend's real
   `?1000h` (no longer forced `False` on Windows); no mouse/focus stripping. (Verified working.)
3. **Do NOT mirror motion (`?1002/1003h`) to the host on Windows** (`_no_host_enable = {1002,1003}`)
   — the RDP motion leak is real and host-side; the backend loses hover/drag but keeps clicks +
   wheel. This is the proven, conservative tradeoff.
4. **Disable host terminal modes at the start of exit finalize** so a scroll during the cooked
   "Finalizing…" teardown can't echo its raw report; re-enabled if exit is aborted.
