"""Out-of-terminal prompts for when aGiTrack's host terminal closes.

When VS Code (or any terminal host) closes — whether the user quits it or the
machine restarts — aGiTrack receives SIGHUP/SIGTERM and its in-terminal TUI is
already gone, so it cannot ask the user anything *inside* the terminal. These
helpers put a blocking confirmation in front of the user via the OS window
server instead, so a forced exit that interrupted live work is acknowledged
rather than silent.

Everything here is best-effort and time-boxed: any failure — no GUI, a broken
``osascript``, or the dialog timing out — resolves to :data:`QUIT`, so a forced
exit (and, critically, a system restart) is never blocked or hung by a missing
or misbehaving dialog.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

# Outcomes of the forced-exit prompt.
QUIT = "quit"  # default button, dismissal, timeout, or no GUI available
REOPEN = "reopen"  # the user asked to keep working in a fresh window


def can_show_dialog() -> bool:
    """Whether a blocking GUI dialog can be shown to the user right now.

    Only macOS with ``osascript`` on PATH qualifies. SSH/headless/Linux hosts
    return ``False`` so those forced exits stay silent and fast — there is no
    window server to surface a dialog on, and blocking would risk hanging the
    exit. (``osascript`` itself fails fast when no GUI session is reachable, so
    we don't need to probe the window server here.)
    """
    return sys.platform == "darwin" and shutil.which("osascript") is not None


def confirm_forced_exit(detail: str, *, timeout: float = 25.0) -> str:
    """Block on a macOS dialog telling the user their terminal closed mid-work.

    ``detail`` is a one-line summary of what aGiTrack saved. Returns
    :data:`REOPEN` only when the user explicitly clicks "Reopen aGiTrack";
    otherwise :data:`QUIT` — the default "Quit aGiTrack" button, any dismissal,
    or ``timeout`` lapsing (so a system restart proceeds without waiting on a
    human). Never raises.
    """
    if not can_show_dialog():
        return QUIT
    message = "aGiTrack's terminal closed while a session was still working. Your work was committed safely. " + detail
    script = (
        f"display dialog {_quote(message)} "
        f"with title {_quote('aGiTrack')} "
        'buttons {"Reopen aGiTrack", "Quit aGiTrack"} '
        'default button "Quit aGiTrack" '
        "with icon caution "
        f"giving up after {int(timeout)}"
    )
    try:
        # Give osascript a few seconds past its own "giving up" deadline so the
        # subprocess timeout is a backstop, not the primary cutoff.
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except Exception:
        return QUIT
    out = result.stdout or ""
    # osascript prints e.g. "button returned:Reopen aGiTrack, gave up:false".
    # A timeout yields "gave up:true" with no button — treat that as QUIT.
    if "gave up:true" in out:
        return QUIT
    return REOPEN if "button returned:Reopen aGiTrack" in out else QUIT


def reopen_in_new_terminal(command: str, cwd: str) -> bool:
    """Best-effort: open a new macOS Terminal window that re-runs aGiTrack.

    ``command`` is the shell command to launch aGiTrack and ``cwd`` the
    directory to run it in (the repo root, so the last session auto-resumes).
    Returns ``True`` if the launch was issued. macOS only; never raises — during
    a system restart Terminal.app may be unavailable and this simply no-ops.
    """
    if sys.platform != "darwin" or shutil.which("osascript") is None:
        return False
    inner = f"cd {_sh_quote(cwd)} && exec {command}"
    script = f'tell application "Terminal" to do script {_quote(inner)}'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def _quote(text: str) -> str:
    """Quote a string as an AppleScript literal (escape backslashes and quotes)."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _sh_quote(text: str) -> str:
    """Single-quote a string for the shell command run inside ``do script``."""
    return "'" + text.replace("'", "'\\''") + "'"
