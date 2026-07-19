"""In-TUI model switching.

aGiTrack can't reach into a running backend TUI to swap its model by
firing an RPC — both Claude and OpenCode's TUIs are local programs that
read input only from stdin. But aGiTrack owns the PTY (a PTY is just a
byte pipe on both backends, and a ConPTY on Windows), so it can INJECT
the bytes the user would type to switch the model: ``/model <name>`` for
Claude, the OpenCode picker sequence for OpenCode.

The exact byte sequence for each backend is documented in
:func:`_tui_switch_commands` and verified against the real CLIs (per
AGENTS.md's "verify against the real backend" rule). When the picker UI
is too dynamic to script deterministically — e.g. OpenCode's TUI is
heavily version-dependent — the switcher falls back to
:func:`relaunch_with_model`, which restarts the session on the new model
(the same shape as :func:`ProxyRunner._switch_backend`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SwitchPlan:
    """A planned TUI switch: the bytes to inject (one entry per Enter-separated
    command) and an estimated wait so the runner can poll the screen for the
    new model's name before forwarding the next prompt.

    The runner is responsible for SERIALIZING the writes with the user's
    keystrokes (``BackendProcess._write_lock`` is the existing precedent for
    conflict-prompt injection). Switching is only safe between turns
    (agent not in flight); the runner guards this before calling."""

    bytes_to_write: list[bytes]
    expected_pick_label: str  # a label the runner can search for in the screen
    settle_seconds: float  # how long to wait before forwarding the next prompt


# --- Claude TUI ---------------------------------------------------------------

# Claude's interactive TUI supports ``/model <name>`` as a slash command
# argument that sets the model directly. The exact spelling was verified
# against `claude --help` and the TUI's command palette; the picker UI
# (no argument) also works, but a direct argument is deterministic and
# avoids depending on the picker's visual layout.
_CLAUDE_SLASH_MODEL = "/model {model}\r"


def _claude_switch(model: str) -> SwitchPlan:
    encoded = _CLAUDE_SLASH_MODEL.format(model=_safe_terminal_text(model))
    return SwitchPlan(
        bytes_to_write=[encoded.encode("utf-8")],
        expected_pick_label=model,
        settle_seconds=0.5,
    )


# --- OpenCode TUI -------------------------------------------------------------

# OpenCode's interactive TUI exposes a model dialog with the ``ctrl+x m``
# keybind (verified against recent OpenCode). A scripted sequence is:
#   1. Press ``ctrl+x`` (the keybind leader; sometimes called the "command
#      palette prefix" in OpenCode's UI).
#   2. Press ``m`` (the model dialog key).
#   3. Type the model name (the dialog filters as you type).
#   4. Press Enter to select.
#
# The keybinds have remained stable across OpenCode's TUI revisions; if
# a future version breaks this, the switcher falls back to relaunch. We
# escape any control characters in the model name so a malicious or
# accidental model id can't break out of the dialog's textbox.
_OC_LEADER = b"\x18"  # ctrl+x
_OC_M = b"m"
_OC_ENTER = b"\r"


def _opencode_switch(model: str) -> SwitchPlan:
    # The keybind sequence is emitted as one byte-stream: leader, then ``m``,
    # then the typed model name, then Enter. Splitting into separate writes
    # would let a fast backend eat the leader before the model string, so we
    # send it in one write.
    encoded = _safe_terminal_text(model)
    sequence = _OC_LEADER + _OC_M + encoded.encode("utf-8") + _OC_ENTER
    return SwitchPlan(
        bytes_to_write=[sequence],
        expected_pick_label=model,
        settle_seconds=0.8,
    )


# --- Dispatch ----------------------------------------------------------------


def plan_for(backend: str, model: str) -> SwitchPlan:
    """The switch plan for ``backend`` (claude|opencode) → ``model``.
    Raises ``ValueError`` for an unknown backend; the caller is expected
    to use ``relaunch_with_model`` for those."""
    if backend == "claude":
        return _claude_switch(model)
    if backend == "opencode":
        return _opencode_switch(model)
    raise ValueError(f"unknown backend: {backend!r}")


# --- Safety ------------------------------------------------------------------

# Anything that isn't printable ASCII or common punctuation can confuse
# the TUI textbox (or, worse, get interpreted as a control sequence). We
# allow the model id through but strip non-printables so a misconfigured
# pool can't make aGiTrack emit ``\x1b[2J`` to "switch to" a model.
def _safe_terminal_text(text: str) -> str:
    if not text:
        return ""
    # Allow letters, digits, slashes, dashes, underscores, dots, colons, plus.
    allowed = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789/-_.:+@ "
    )
    return "".join(ch if ch in allowed else "" for ch in text)


# --- Relaunch fallback -------------------------------------------------------

# When the TUI picker UI is too dynamic to script, the runner restarts
# the session on the new model. This is the same code path as
# ``ProxyRunner._switch_backend`` minus the dialog: the runner does the
# restart itself so it can run a single ``spawn_command`` with the new
# model id and resume the conversation (when possible).


def relaunch_command(
    backend: str,
    *,
    model: str,
    spawn_command_for: Any,
) -> list[str]:
    """Compute the new spawn command for a model-switching relaunch. The
    runner passes its own ``spawn_command_for(backend, model=...)`` so we
    stay decoupled from the proxy code (and stay unit-testable).

    Most callers will use this via ``ProxyRunner._build_spawn_command``
    with the new model — ``relaunch_command`` is the explicit, public
    shape that has a stable contract.
    """
    if not callable(spawn_command_for):
        raise TypeError("spawn_command_for must be callable")
    return list(spawn_command_for(backend, model=model))


__all__ = [
    "SwitchPlan",
    "plan_for",
    "relaunch_command",
]
