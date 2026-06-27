"""Probe what terminal private modes a real backend enables under our ConPTY.

The Task-A fix strategy depends entirely on facts about the real backends that we must NOT
guess at:
  * Does the backend enable win32-input-mode (\\x1b[?9001h) for ITS OWN reads? (conhost emits
    a ?9001h at startup regardless, so we must distinguish the backend's own request.)
  * Does it enable mouse tracking (?1000/1002/1003h) and SGR ext (?1006h)?
  * Does it enable bracketed paste (?2004h), focus (?1004h), kitty keyboard (CSI > u)?

Spawns the backend via aGiTrack's own NtChildProcess (real ConPTY), captures raw output for a
few seconds, and reports every DEC private mode set/reset it sees, in order.

Must run in a REAL console (Start-Process), not the agent's non-interactive shell tools.

  uv run --python 3.12 python dev/winmouse/probe_backend.py claude
  uv run --python 3.12 python dev/winmouse/probe_backend.py opencode
"""

from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from agitrack.proxy.platform.nt import NtChildProcess  # noqa: E402

_PRIVMODE = re.compile(rb"\x1b\[\?([0-9;]+)([hl])")
_KITTY = re.compile(rb"\x1b\[>([0-9;]*)u")


def main() -> None:
    backend = sys.argv[1] if len(sys.argv) > 1 else "claude"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
    cwd = sys.argv[3] if len(sys.argv) > 3 else os.getcwd()

    proc = NtChildProcess.spawn([backend], cwd)
    out = b""
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        chunk = proc.drain()
        if chunk:
            out += chunk
        else:
            time.sleep(0.02)
        if proc.poll() is not None:
            extra = proc.drain()
            if extra:
                out += extra
            break
    proc.teardown()

    modes: list[str] = []
    for m in _PRIVMODE.finditer(out):
        for code in m.group(1).split(b";"):
            modes.append(f"?{code.decode()}{m.group(2).decode()}")
    kitty = [f"CSI>{m.group(1).decode()}u" for m in _KITTY.finditer(out)]

    seen_order: list[str] = []
    for x in modes + kitty:
        if x not in seen_order:
            seen_order.append(x)

    lines = [
        f"backend={backend} cwd={cwd} bytes={len(out)} exited={proc.poll()}",
        f"distinct DEC private modes (in first-seen order): {seen_order}",
        "interpretation:",
        f"  win32-input-mode (?9001h) present : {'?9001h' in modes}  "
        f"(count={modes.count('?9001h')}; conhost emits 1 at startup, so >1 means backend asked too)",
        f"  mouse button (?1000h)             : {'?1000h' in modes}",
        f"  mouse motion (?1002h/?1003h)      : {'?1002h' in modes or '?1003h' in modes}",
        f"  SGR ext mouse (?1006h)            : {'?1006h' in modes}",
        f"  bracketed paste (?2004h)          : {'?2004h' in modes}",
        f"  focus events (?1004h)             : {'?1004h' in modes}  (conhost also emits 1 at startup)",
        f"  kitty keyboard (CSI>...u)         : {bool(kitty)}",
        "",
        f"raw output (first 600B): {out[:600]!r}",
    ]
    report = "\n".join(lines)
    print(report)
    with open(os.path.join(os.path.dirname(__file__), f"probe_{backend}.txt"), "w", encoding="utf-8") as f:
        f.write(report)


if __name__ == "__main__":
    main()
