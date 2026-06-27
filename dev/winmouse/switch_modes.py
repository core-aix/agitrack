"""Deterministically reproduce the host-mode carryover across a backend switch.

Feeds each backend's real terminal init sequences through ProxyRunner._sync_terminal_modes
(capturing everything it writes to the host) and reports the net host mode state for:
  * claude alone
  * opencode alone
  * claude THEN opencode (a switch)
The difference (claude-then-opencode vs opencode-alone) is the carried-over host state — the
suspect for the switch-only input corruption. Tries host_kitty_keyboard False and True.

  uv run --python 3.12 python dev/winmouse/switch_modes.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from agitrack.proxy.runner import ProxyRunner  # noqa: E402

# Real init sequences each backend emits (from dev/winmouse/probe_backend.py captures).
CLAUDE = (
    b"\x1b[?9001h\x1b[?1004h\x1b[?2004h\x1b[?1049h"
    b"\x1b[<u\x1b[>1u\x1b[>4;2m"
    b"\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h"
)
OPENCODE = (
    b"\x1b[?9001h\x1b[?1004h\x1b[?2031h\x1b[?u\x1b[?1049h\x1b[?2027h\x1b[?2004h"
    b"\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[>4;1m"
)


def _runner(host_kitty: bool):
    r = ProxyRunner.for_testing()
    r.host_kitty_keyboard = host_kitty
    return r


def _capture(seqs: list[bytes], host_kitty: bool) -> list[bytes]:
    """Run a list of backend outputs through _sync_terminal_modes, returning the ordered list
    of byte chunks aGiTrack wrote to the host (stdout)."""
    import agitrack.proxy.runner as mod

    writes: list[bytes] = []
    orig = mod.os.write
    mod.os.write = lambda fd, data: writes.append(data) or len(data)
    try:
        r = _runner(host_kitty)
        for s in seqs:
            r._sync_terminal_modes(s)
    finally:
        mod.os.write = orig
    return writes


def _net_state(writes: list[bytes]) -> dict:
    """Collapse the ordered host writes into a final mode state (last write wins per family)."""
    blob = b"".join(writes)
    state: dict[str, str] = {}
    # modifyOtherKeys: last >4;Nm wins
    import re

    mok = re.findall(rb"\x1b\[>4(?:;(\d+))?m", blob)
    if mok:
        state["modifyOtherKeys"] = (mok[-1] or b"0").decode()
    # kitty push/pop: track net
    kitty = re.findall(rb"\x1b\[([<>=?])([0-9;]*)u", blob)
    if kitty:
        state["kitty_seqs"] = ",".join(f"{k.decode()}{v.decode()}u" for k, v in kitty)
    for mode in (b"1000", b"1002", b"1003", b"1006", b"1004", b"2004"):
        ons = blob.count(b"\x1b[?" + mode + b"h")
        offs = blob.count(b"\x1b[?" + mode + b"l")
        if ons or offs:
            state[f"?{mode.decode()}"] = f"on={ons} off={offs}"
    return state


def main() -> None:
    for host_kitty in (False, True):
        print("=" * 72)
        print(f"host_kitty_keyboard = {host_kitty}")
        for label, seqs in (
            ("claude alone", [CLAUDE]),
            ("opencode alone", [OPENCODE]),
            ("claude THEN opencode (switch)", [CLAUDE, OPENCODE]),
        ):
            writes = _capture(seqs, host_kitty)
            print(f"\n  {label}")
            print(f"    host writes: {b''.join(writes)!r}")
            print(f"    net state  : {_net_state(writes)}")
        # The carryover = switch state minus opencode-alone state
        sw = _net_state(_capture([CLAUDE, OPENCODE], host_kitty))
        oc = _net_state(_capture([OPENCODE], host_kitty))
        diff = {k: (oc.get(k), sw.get(k)) for k in set(sw) | set(oc) if sw.get(k) != oc.get(k)}
        print(f"\n  >>> CARRYOVER (opencode-alone -> switch): {diff or 'none'}")
        print()


if __name__ == "__main__":
    main()
