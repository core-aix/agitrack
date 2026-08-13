"""Terminal-text helpers shared by ``ProxyRunner`` and the mixins split out of it.

A leaf module on purpose: it imports nothing from the proxy package, so any of the mixin
modules can use it without importing ``runner`` and creating a cycle.
"""

from __future__ import annotations

import re

# CSI (``ESC [ … final``), OSC (``ESC ] … BEL/ST``) and the two-byte escapes in between. Wide
# enough to strip what a backend CLI paints into output aGiTrack has to read as TEXT — a
# version string, an update log, the tail of a dying backend's screen.
_ANSI_CSI_OSC_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def strip_ansi(text: str) -> str:
    """*text* with terminal escape sequences removed and ``\\r`` normalised to ``\\n``.

    The carriage-return rewrite matters as much as the escapes: a CLI that redraws a progress
    line in place separates its updates with ``\\r``, so without it every state of that line
    collapses onto one unreadable row.
    """
    return _ANSI_CSI_OSC_RE.sub("", text).replace("\r", "\n")
