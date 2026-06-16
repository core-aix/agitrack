from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agitrack.env import getenv_compat

DEFAULT_BACKEND = "opencode"

# The key that opens aGiTrack's command menu in proxy mode. Configurable as
# "menu_key" in config.json. Supports:
#   - "ctrl-<letter>" (e.g., "ctrl-g") — single control byte
#   - "ctrl+shift+<letter>" (e.g., "ctrl+shift+g") — kitty keyboard protocol
# A few control keys are excluded because the terminal or aGiTrack already gives
# them a meaning: Ctrl-C (exit flow), Ctrl-H (Backspace), Ctrl-I (Tab),
# Ctrl-J/Ctrl-M (Enter).
DEFAULT_MENU_KEY = "ctrl-g"
_MENU_KEY_RE = re.compile(r"^ctrl[-+]([a-bd-gk-ln-z])$")
_MENU_KEY_SHIFT_RE = re.compile(r"^ctrl\+shift\+([a-bd-gk-ln-z])$")

# Tunable timings (all in seconds) governing aGiTrack's polling / debounce behaviour.
# Stored under the "timings" key in config.json; any subset may be overridden, and
# anything missing or invalid falls back to the default below.
DEFAULT_TIMINGS: dict[str, float] = {
    "base_poll_seconds": 3.0,  # how often to re-check the base branch HEAD for out-of-band commits
    "background_poll_seconds": 2.0,  # how often an idle background session is serviced
    "file_stable_seconds": 8.0,  # quiet period after a file change before an auto-commit
    "child_idle_seconds": 4.0,  # no backend output for this long counts as idle
    "parse_cooldown_seconds": 10.0,  # minimum gap between agent-turn parses
    "base_edit_check_seconds": 3.0,  # how often to warn about un-sandboxed base-repo edits
    "cwd_check_seconds": 3.0,  # how often to check for the resume-cwd drift bug
    "base_drift_check_seconds": 2.0,  # how often to check the base repo's checked-out branch
    "summary_wait_seconds": 45.0,  # how long integration waits for a background commit summary (#8)
    "update_check_seconds": 300.0,  # how often to re-check for an aGiTrack self-update (every 5 min)
}


def _default_path() -> Path:
    config_dir = getenv_compat("CONFIG_DIR")
    if config_dir:
        base = Path(config_dir).expanduser()
    else:
        base = Path.home() / ".agitrack"
        # First aGiTrack run on a machine with a legacy ~/.agit: seed the new dir
        # from it so saved preferences carry over (best-effort, copy not move).
        from agitrack.config.migrate import migrate_global_config

        migrate_global_config(base)
    return base / "config.json"


class GlobalConfig:
    """User-wide aGiTrack configuration stored in ``~/.agitrack/config.json``.

    Holds preferences that should persist across repositories, such as the
    default agent backend used when a repository has no backend recorded yet.
    The location can be overridden with the ``AGITRACK_CONFIG_DIR`` environment
    variable.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_path()
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
            handle.write("\n")

    def has_default_backend(self) -> bool:
        return bool(self.data.get("default_backend"))

    @property
    def default_backend(self) -> str:
        value = self.data.get("default_backend")
        return str(value) if value else DEFAULT_BACKEND

    @default_backend.setter
    def default_backend(self, value: str) -> None:
        self.data["default_backend"] = value
        self.save()

    @property
    def sandbox(self) -> bool:
        # Confine the agent's writes to its session worktree (on by default).
        value = self.data.get("sandbox")
        return True if value is None else bool(value)

    @sandbox.setter
    def sandbox(self, value: bool) -> None:
        self.data["sandbox"] = bool(value)
        self.save()

    @property
    def use_worktrees(self) -> bool:
        # Run each session in its own git worktree (on by default). When off,
        # the agent edits the current branch directly (#9) — visible live, but
        # no isolation/integration and unsafe with concurrent sessions.
        value = self.data.get("use_worktrees")
        return True if value is None else bool(value)

    @use_worktrees.setter
    def use_worktrees(self, value: bool) -> None:
        self.data["use_worktrees"] = bool(value)
        self.save()

    @property
    def menu_key(self) -> str:
        # Normalized menu key spec. Supports "ctrl-<letter>" or "ctrl+shift+<letter>".
        # Invalid or conflicting values fall back to the default so a config typo
        # can never lock the user out of the menu.
        value = self.data.get("menu_key")
        if isinstance(value, str):
            normalized = value.strip().lower()
            match = _MENU_KEY_SHIFT_RE.match(normalized)
            if match:
                return f"ctrl+shift+{match.group(1)}"
            match = _MENU_KEY_RE.match(normalized)
            if match:
                return f"ctrl-{match.group(1)}"
        return DEFAULT_MENU_KEY

    @property
    def menu_key_byte(self) -> bytes:
        # For plain ctrl-<letter>, return the control byte (0x01-0x1a).
        # For ctrl+shift+<letter>, return empty bytes (sequence-based matching).
        if self.is_shift_modified:
            return b""
        return bytes([ord(self.menu_key[-1]) - 96])  # ctrl-a..ctrl-z → 0x01..0x1a

    @property
    def menu_key_sequence(self) -> bytes:
        # Kitty keyboard protocol escape sequence for the menu key.
        # For ctrl+shift+<letter>: CSI <unicode> ; <modifiers> u
        # Modifiers: 1 (base) + 1 (shift) + 4 (ctrl) = 6
        if self.is_shift_modified:
            letter = self.menu_key.split("+")[-1]
            unicode_codepoint = ord(letter)
            return f"\x1b[{unicode_codepoint};6u".encode()
        # For plain ctrl-<letter>, return the control byte
        return self.menu_key_byte

    @property
    def is_shift_modified(self) -> bool:
        # True if the menu key uses ctrl+shift+<letter> format
        return self.menu_key.startswith("ctrl+shift+")

    @property
    def menu_key_label(self) -> str:
        if self.is_shift_modified:
            letter = self.menu_key.split("+")[-1]
            return f"Ctrl+Shift-{letter.upper()}"
        return f"Ctrl-{self.menu_key[-1].upper()}"

    @property
    def timings(self) -> dict[str, float]:
        # Defaults overlaid with any valid user overrides from the "timings" object.
        # An override must be a positive number; bad values (wrong type, <= 0, bool)
        # are ignored so a typo can never stall or busy-spin a poll loop.
        stored = self.data.get("timings")
        stored = stored if isinstance(stored, dict) else {}
        result = dict(DEFAULT_TIMINGS)
        for key in DEFAULT_TIMINGS:
            value = stored.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                result[key] = float(value)
        return result

    @property
    def summarization_model(self) -> str | None:
        value = self.data.get("summarization_model")
        return str(value) if value else None

    @summarization_model.setter
    def summarization_model(self, value: str | None) -> None:
        self.data["summarization_model"] = value
        self.save()

    @property
    def summarization_enabled(self) -> bool:
        value = self.data.get("summarization_enabled")
        return True if value is None else bool(value)

    @summarization_enabled.setter
    def summarization_enabled(self, value: bool) -> None:
        self.data["summarization_enabled"] = bool(value)
        self.save()

    @property
    def check_for_updates(self) -> bool:
        # Whether aGiTrack checks for its own updates (at startup and periodically).
        # On by default; the user can turn it off here or by choosing "don't ask
        # again" when offered an update.
        value = self.data.get("check_for_updates")
        return True if value is None else bool(value)

    @check_for_updates.setter
    def check_for_updates(self, value: bool) -> None:
        self.data["check_for_updates"] = bool(value)
        self.save()

    # --- session sharing (issue #55) ---------------------------------------
    # Sharing is opt-in: nothing is ever uploaded until the user explicitly shares
    # a session and acknowledges the one-time consent notice. We remember that
    # acknowledgement and cache the resolved GitHub login so later shares are quiet.

    @property
    def session_sharing(self) -> dict[str, Any]:
        value = self.data.get("session_sharing")
        return value if isinstance(value, dict) else {}

    @property
    def session_sharing_acknowledged(self) -> bool:
        return bool(self.session_sharing.get("acknowledged"))

    def acknowledge_session_sharing(self) -> None:
        block = dict(self.session_sharing)
        block["acknowledged"] = True
        self.data["session_sharing"] = block
        self.save()

    @property
    def github_login(self) -> str | None:
        value = self.session_sharing.get("github_login")
        return value if isinstance(value, str) and value else None

    @github_login.setter
    def github_login(self, value: str | None) -> None:
        block = dict(self.session_sharing)
        if value:
            block["github_login"] = value
        else:
            block.pop("github_login", None)
        self.data["session_sharing"] = block
        self.save()
