"""Deterministic ConPTY mouse-input harness (Task A).

Spawns mouse_child.py inside a ConPTY using aGiTrack's own Windows child wrapper
(agitrack.proxy.platform.nt.NtChildProcess — the proven spawn + output bridge), then writes
test input sequences into the ConPTY input and reports exactly what the child received. No
physical mouse needed.

This answers the core Task A question empirically on THIS Windows build: when we write plain
SGR mouse bytes (or other encodings) into a ConPTY's input, does the child get a mouse event,
or do the bytes leak as literal text?

Run from the repo root:
  uv run --python 3.12 python devtools/winmouse/harness.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from agitrack.proxy.platform.nt import NtChildProcess  # noqa: E402

CHILD = os.path.join(os.path.dirname(__file__), "mouse_child.py")


def _drain_until(proc: NtChildProcess, needle: bytes, timeout: float) -> bytes:
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = proc.drain()
        if chunk:
            buf += chunk
            if needle in buf:
                return buf
        else:
            time.sleep(0.02)
    return buf


def run_case(name: str, modes: str, inputs: list[bytes]) -> dict:
    log = os.path.join(tempfile.gettempdir(), f"agit_mouse_{name}.log")
    if os.path.exists(log):
        os.remove(log)
    proc = NtChildProcess.spawn([sys.executable, CHILD, log, modes], os.getcwd())
    out = _drain_until(proc, b"READY", 5.0)
    time.sleep(0.2)
    for seq in inputs:
        proc.write(seq)
        time.sleep(0.15)
    time.sleep(0.3)
    proc.write(b"q")  # ask the child to quit
    out += _drain_until(proc, b"\x00never\x00", 0.6) or b""
    deadline = time.monotonic() + 2.0
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    proc.teardown()
    child_log = ""
    for _ in range(30):
        try:
            with open(log, encoding="utf-8") as f:
                child_log = f.read()
            if "DONE" in child_log:
                break
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    return {"name": name, "modes": modes, "output": out, "child_log": child_log}


def main() -> None:
    # The probe sequence: an SGR mouse press at col 5, row 5 (button 0 down).
    sgr_mouse = b"\x1b[<0;5;5M"
    cases = [
        ("vt_mouse_sgr", "vt,mouse", [sgr_mouse]),
        ("vt_mouse_input_sgr", "vt,mouse,mouseinput", [sgr_mouse]),
        ("vt_win32_mouse_sgr", "vt,mouse,win32", [sgr_mouse]),
        ("vt_plain_key", "vt", [b"hi"]),
        # Definitive probe: read raw INPUT_RECORDs. Does conhost ever turn input-pipe SGR
        # mouse bytes into a MOUSE_EVENT_RECORD, or only KEY_EVENTs?
        ("recinput_mouse_sgr", "mouseinput,readinput", [sgr_mouse, b"q"]),
        ("recinput_vt_mouse_sgr", "vt,mouse,mouseinput,readinput", [sgr_mouse, b"q"]),
    ]
    lines: list[str] = []

    def emit(s: str = "") -> None:
        print(s)
        lines.append(s)

    for case in cases:
        res = run_case(*case)
        emit("=" * 70)
        emit(f"CASE {res['name']}  modes={res['modes']}")
        emit(f"  child stdout (first 200B): {res['output'][:200]!r}")
        emit("  child log:")
        for line in res["child_log"].splitlines():
            emit(f"    {line}")
        log = res["child_log"]
        recv = "\n".join(l for l in log.splitlines() if l.startswith("recv"))
        hexcat = recv.replace(" ", "").lower()
        if "REC MOUSE" in log:
            emit("  >>> conhost produced a MOUSE_EVENT_RECORD  [MOUSE DELIVERED via ReadConsoleInput]")
        elif "REC KEY" in log:
            emit("  >>> conhost produced only KEY_EVENT records (no mouse event)")
        elif "1b5b3c" in hexcat:
            emit("  >>> child received an SGR mouse sequence back (\\x1b[<...)  [VT round-trip]")
        elif recv:
            emit("  >>> child received input, but NOT as SGR mouse (see hex above)")
        else:
            emit("  >>> child received NOTHING")
        emit()

    result_path = os.path.join(os.path.dirname(__file__), "result.txt")
    with open(result_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[wrote {result_path}]")


if __name__ == "__main__":
    main()
