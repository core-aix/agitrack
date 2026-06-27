"""Inject mouse + a marker string into a live backend's ConPTY and capture what it renders.

This is the end-to-end truth test for Task A: does a real backend, running under our ConPTY,
treat forwarded SGR mouse bytes as a mouse event, or leak them as literal text into its prompt?

Strategy: spawn the backend, let it init (it enables ?1000h/?1006h itself), then write a
sequence of inputs and capture the rendered output after each. The marker string MARKERxyz is
typed; if mouse leaks, the rendered input box will also contain mouse-report digits/chars.

Must run in a REAL console (Start-Process).

  uv run --python 3.12 python dev/winmouse/inject_backend.py claude
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from agitrack.proxy.platform.nt import NtChildProcess  # noqa: E402


def _drain(proc: NtChildProcess, secs: float) -> bytes:
    out = b""
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        chunk = proc.drain()
        if chunk:
            out += chunk
        else:
            time.sleep(0.02)
    return out


def main() -> None:
    backend = sys.argv[1] if len(sys.argv) > 1 else "claude"
    cwd = os.getcwd()
    proc = NtChildProcess.spawn([backend], cwd)
    init = _drain(proc, 5.0)  # let it fully paint its TUI + enable mouse

    steps = []

    def step(name: str, data: bytes, wait: float = 1.2) -> None:
        proc.write(data)
        out = _drain(proc, wait)
        steps.append((name, data, out))

    # 1) type a marker so we can see the input box / text path working
    step("type-marker", b"MARKERxyz")
    # 2) a left-button click at row5 col5 (press + release)
    step("mouse-click", b"\x1b[<0;5;5M\x1b[<0;5;5m")
    # 3) a wheel-up event (button 64) — backends usually consume this to scroll
    step("wheel-up", b"\x1b[<64;10;10M")
    # 4) motion-drag (button 35 = move with no button) — the mode the band-aid blocked
    step("mouse-motion", b"\x1b[<35;12;7M\x1b[<35;13;7M")
    # 5) focus-out then focus-in (the events the Windows focus-strip removes)
    step("focus-out-in", b"\x1b[O\x1b[I")
    # 6) another marker to see if state recovered (and catch any phantom char prepended)
    step("type-marker2", b"END")

    proc.teardown()

    lines = [f"backend={backend} init_bytes={len(init)}"]
    for name, data, out in steps:
        # Heuristic leak markers: literal mouse-ish substrings appearing in rendered output.
        text = out
        leak_hits = []
        for needle in (b"[<", b"0;5;5", b"64;10;10", b";5M", b"<0;", b"<64;", b"<35;", b"35;12", b"[I", b"[O"):
            if needle in text:
                leak_hits.append(needle.decode("latin1"))
        lines.append("=" * 60)
        lines.append(f"STEP {name}  wrote={data!r}  out_bytes={len(out)}")
        lines.append(f"  leak-substring hits in rendered output: {leak_hits or 'none'}")
        # show a trimmed tail of the rendered output (where the input box usually is)
        tail = out[-400:]
        lines.append(f"  tail: {tail!r}")
    report = "\n".join(lines)
    print(report)
    with open(os.path.join(os.path.dirname(__file__), f"inject_{backend}.txt"), "w", encoding="utf-8") as f:
        f.write(report)


if __name__ == "__main__":
    main()
