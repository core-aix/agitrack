from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agitrack.env import getenv_compat

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


def normalize_menu_key(value: str) -> str | None:
    """Canonicalize a user-entered menu key to ``ctrl-<letter>`` / ``ctrl+shift+<letter>``,
    or ``None`` when it isn't a valid, allowed menu key. (The :class:`GlobalConfig`
    property falls back to the default for invalid input so a typo can't lock the user out;
    callers that need to *reject* bad input — e.g. the startup picker — use this directly.)"""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    match = _MENU_KEY_SHIFT_RE.match(normalized)
    if match:
        return f"ctrl+shift+{match.group(1)}"
    match = _MENU_KEY_RE.match(normalized)
    if match:
        return f"ctrl-{match.group(1)}"
    return None


def menu_key_is_shift(key: str) -> bool:
    """True if *key* (canonical form) is the ``ctrl+shift+<letter>`` variant."""
    return key.startswith("ctrl+shift+")


def menu_key_label_for(key: str) -> str:
    """Human label for a canonical menu key, e.g. ``Ctrl-G`` or ``Ctrl+Shift-G``."""
    if menu_key_is_shift(key):
        return f"Ctrl+Shift-{key.split('+')[-1].upper()}"
    return f"Ctrl-{key[-1].upper()}"


def menu_key_bytes_for(key: str) -> bytes:
    """The byte(s) the terminal sends for a canonical menu key: the single control byte
    (0x01–0x1a) for ``ctrl-<letter>``, or the kitty-keyboard-protocol CSI-u escape
    sequence for ``ctrl+shift+<letter>``. This is exactly what aGiTrack matches against
    stdin to open the menu, so it is also what a pre-TUI key test must look for."""
    if menu_key_is_shift(key):
        letter = key.split("+")[-1]
        # CSI <codepoint> ; <modifiers> u — modifiers = 1 (base) + 1 (shift) + 4 (ctrl) = 6
        return f"\x1b[{ord(letter)};6u".encode()
    return bytes([ord(key[-1]) - 96])  # ctrl-a..ctrl-z → 0x01..0x1a


def host_is_vscode(env: Mapping[str, str]) -> bool:
    """Whether aGiTrack is running inside VS Code's integrated terminal, which it tags
    with ``TERM_PROGRAM=vscode`` (and injects ``VSCODE_*`` vars via shell integration)."""
    return env.get("TERM_PROGRAM") == "vscode" or "VSCODE_INJECTION" in env or "VSCODE_GIT_IPC_HANDLE" in env


# Menu keys the host editor/terminal is known to bind itself, so the keypress may be
# swallowed before it ever reaches aGiTrack. Keyed by host predicate. Only a heads-up —
# the pre-TUI key test is the authoritative check.
def detect_menu_key_conflict(key: str, env: Mapping[str, str]) -> str | None:
    """A human-readable reason if *key* is likely intercepted by the detected host (so it
    won't reach aGiTrack's menu), else ``None``. Conservative: only well-known conflicts."""
    if key == "ctrl-g" and host_is_vscode(env):
        return (
            'VS Code binds Ctrl-G to "Go to Line", so in its integrated terminal the '
            "keypress can be intercepted before it reaches aGiTrack."
        )
    return None


# Preferred fallbacks, in order — control letters editors/terminals rarely grab. (All are
# in the allowed set; Ctrl-C/H/I/J/M are excluded as terminal-reserved.)
_MENU_KEY_SUGGESTIONS = ("ctrl-o", "ctrl-b", "ctrl-n", "ctrl-k", "ctrl-y", "ctrl+shift+g")


def suggest_menu_keys(current: str, env: Mapping[str, str], limit: int = 3) -> list[str]:
    """Up to *limit* alternative menu keys to offer when *current* may conflict — skipping
    the current key and any that also conflict with the detected host."""
    out: list[str] = []
    for candidate in _MENU_KEY_SUGGESTIONS:
        if candidate == current or detect_menu_key_conflict(candidate, env):
            continue
        out.append(candidate)
        if len(out) >= limit:
            break
    return out


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
    "idle_after_seconds": 30.0,  # no input/output AND no running session for this long ⇒ enter low-power idle
    "idle_poll_seconds": 30.0,  # background-sweep interval while idle (the loop still wakes instantly on input)
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
        # Repo-local overlay: settings written for THIS repository (in its
        # ``.agitrack/config.json``) take precedence over the global file. Loaded via
        # ``load_repo_overlay`` once aGiTrack knows which repo it's running in.
        self.repo_path: Path | None = None
        self.repo_data: dict[str, Any] = {}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _default_config(self) -> dict[str, Any]:
        """Every user-facing setting mapped to its built-in default. This is the single
        registry that :meth:`seed_defaults` writes into the global config file so the file
        lists every available knob (each getter still carries the same default as its own
        fallback, so a hand-deleted key keeps working). Deliberately EXCLUDES transient /
        state-like keys — ``pending_manual_update`` and ``session_sharing`` — which are
        runtime state, not user preferences, and must not appear as "settings"."""
        from agitrack.sessions.share_cap import DEFAULT_MAX_SHARED_BYTES

        return {
            "default_backend": None,
            "sandbox": True,
            "commit_guidance": True,
            "use_worktrees": True,
            "manual_commits": False,
            "background": False,
            "background_autostart": False,
            "autotrack_hook": "keep",
            "log_file": None,
            "allowed_edit_paths": [],
            "backend_command": "",
            "menu_key": DEFAULT_MENU_KEY,
            "summarization_enabled": True,
            "summarization_model": None,
            "check_for_updates": True,
            "share_max_transcript_bytes": DEFAULT_MAX_SHARED_BYTES,
            "timings": dict(DEFAULT_TIMINGS),
        }

    def seed_defaults(self) -> bool:
        """Write any missing default settings into the global config file so a user opening
        ``~/.agitrack/config.json`` can see every available knob and its default. Only fills
        gaps — never overwrites a value the user already set — and only writes when something
        was actually missing, so it is a no-op on every run after the first (and after an
        upgrade, it adds just the newly-introduced keys). Repo-local overlays are untouched.
        Returns True if the file was updated."""
        added = False
        for key, default in self._default_config().items():
            if key not in self.data:
                self.data[key] = default
                added = True
        if added:
            self.save()
        return added

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
            handle.write("\n")

    # --- repo-local overlay -------------------------------------------------

    def load_repo_overlay(self, repo_root: Path) -> None:
        """Load a repository's ``.agitrack/config.json`` so its settings override the
        global file for this run. Best-effort; a missing/corrupt file is ignored."""
        self.repo_path = Path(repo_root) / ".agitrack" / "config.json"
        if not self.repo_path.exists():
            self.repo_data = {}
            return
        try:
            with self.repo_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.repo_data = data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            self.repo_data = {}

    def save_repo(self) -> None:
        """Write the repo-local overlay back to ``<repo>/.agitrack/config.json``,
        preserving any other keys the file already holds (e.g. AgitrackState's)."""
        if self.repo_path is None:
            return
        self.repo_path.parent.mkdir(parents=True, exist_ok=True)
        with self.repo_path.open("w", encoding="utf-8") as handle:
            json.dump(self.repo_data, handle, indent=2, sort_keys=True)
            handle.write("\n")

    def _raw(self, key: str) -> Any:
        """The stored value for *key*: the repo-local overlay wins over the global file."""
        if key in self.repo_data:
            return self.repo_data[key]
        return self.data.get(key)

    def source(self, key: str) -> str:
        """Where *key*'s current value comes from: 'repo', 'global', or 'default'."""
        if key in self.repo_data:
            return "repo"
        if key in self.data:
            return "global"
        return "default"

    def set(self, key: str, value: Any, *, scope: str) -> None:
        """Set a setting at the given scope ('global' or 'repo') and persist it."""
        if scope == "repo":
            self.repo_data[key] = value
            self.save_repo()
        else:
            self.data[key] = value
            self.save()

    def unset(self, key: str, *, scope: str) -> None:
        """Remove a setting at the given scope (revert to the lower layer / default)."""
        if scope == "repo":
            self.repo_data.pop(key, None)
            self.save_repo()
        else:
            self.data.pop(key, None)
            self.save()

    def has_default_backend(self) -> bool:
        return bool(self._raw("default_backend"))

    @property
    def default_backend(self) -> str | None:
        """The configured default backend, or ``None`` when the user has never chosen one.

        There is deliberately NO silent fallback to a hardcoded backend: a missing default
        must surface as an explicit first-run prompt (interactive) or a clear error
        (non-interactive) at the call site — never quietly launch some default agent, which
        previously caused surprise OpenCode sessions when the value couldn't be read."""
        value = self._raw("default_backend")
        return str(value) if value else None

    @default_backend.setter
    def default_backend(self, value: str) -> None:
        self.data["default_backend"] = value
        self.save()

    @property
    def sandbox(self) -> bool:
        # Confine the agent's writes to its session worktree (on by default).
        value = self._raw("sandbox")
        return True if value is None else bool(value)

    @sandbox.setter
    def sandbox(self, value: bool) -> None:
        self.data["sandbox"] = bool(value)
        self.save()

    @property
    def commit_guidance(self) -> bool:
        # Whether to append a note to a coding agent's system prompt telling it that
        # aGiTrack auto-commits, so it should not create its own commits (on by default;
        # disable per-run with --no-commit-guidance).
        value = self._raw("commit_guidance")
        return True if value is None else bool(value)

    @commit_guidance.setter
    def commit_guidance(self, value: bool) -> None:
        self.data["commit_guidance"] = bool(value)
        self.save()

    @property
    def use_worktrees(self) -> bool:
        # Run each session in its own git worktree (on by default). When off,
        # the agent edits the current branch directly (#9) — visible live, but
        # no isolation/integration and unsafe with concurrent sessions.
        value = self._raw("use_worktrees")
        return True if value is None else bool(value)

    @use_worktrees.setter
    def use_worktrees(self, value: bool) -> None:
        self.data["use_worktrees"] = bool(value)
        self.save()

    @property
    def manual_commits(self) -> bool:
        # Manual-commit mode (off by default): the agent edits the current branch directly
        # (implies no worktrees) and each turn is recorded as a hidden "latent" commit on a
        # side ref instead of landing on the branch. Commits stay user-triggered — when you
        # commit (via the aGiTrack menu or an external `git commit`), the pending latent turns
        # are folded into that single commit. Enable per-run with --manual-commits / -m.
        value = self._raw("manual_commits")
        return False if value is None else bool(value)

    @manual_commits.setter
    def manual_commits(self, value: bool) -> None:
        self.data["manual_commits"] = bool(value)
        self.save()

    @property
    def background(self) -> bool:
        # Background (headless) mode (off by default): aGiTrack runs without its interactive
        # TUI, driving a native backend session and performing everything the TUI would —
        # committing turns, summaries, session sharing, and installing the commit hooks — so
        # tracking works when the user drives the agent from another UI (e.g. an IDE
        # extension). Always runs without a worktree, with either manual or auto commit.
        # Enable per-run with --background / -b.
        value = self._raw("background")
        return False if value is None else bool(value)

    @background.setter
    def background(self, value: bool) -> None:
        self.data["background"] = bool(value)
        self.save()

    @property
    def autotrack_hook(self) -> str:
        # Whether the PERSISTENT auto-track pre-commit hook is installed (repo-scoped). "keep"
        # (default): the hook stays after the background tracker exits, so a commit made when
        # aGiTrack isn't running still gets its AI work tracked (remove it with
        # `agitrack --remove-hooks`). "off": don't install it; track only while the tracker runs.
        value = self._raw("autotrack_hook")
        return "off" if str(value).lower() == "off" else "keep"

    @autotrack_hook.setter
    def autotrack_hook(self, value: str) -> None:
        self.data["autotrack_hook"] = "off" if str(value).lower() == "off" else "keep"
        self.save()

    @property
    def background_autostart(self) -> bool:
        # Repo-scoped opt-in (off by default): when set, a persistent pre-commit hook auto-starts
        # the background tracker on `git commit` if it isn't already running — so AI work is tracked
        # even after a reboot without remembering to run `agitrack -b`. The triggering commit still
        # folds in the pending trace/metadata (the hook records + renders synchronously first). When
        # off, the hook only REMINDS (and still folds the pending work into the current commit).
        value = self._raw("background_autostart")
        return False if value is None else bool(value)

    @background_autostart.setter
    def background_autostart(self, value: bool) -> None:
        self.data["background_autostart"] = bool(value)
        self.save()

    @property
    def log_file(self) -> str | None:
        # Optional path to a plain-text EVENT LOG aGiTrack appends notable events to (an AI
        # change detected, a commit made, a merge integrated, an update available). Works in
        # every mode — interactive proxy AND headless background. Unset (None) ⇒ no event log.
        # A relative path is resolved against the repo root (see agitrack.events.resolve_log_path).
        value = self._raw("log_file")
        return str(value) if isinstance(value, str) and value.strip() else None

    @log_file.setter
    def log_file(self, value: str | None) -> None:
        self.data["log_file"] = value or None
        self.save()

    @property
    def allowed_edit_paths(self) -> list[str]:
        # Extra paths the sandbox lets the agent write to, beyond its worktree (e.g. a
        # shared data dir, a sibling package). Stored as a JSON list of strings; the CLI
        # accepts them ":"-separated like PATH. Empty by default.
        value = self._raw("allowed_edit_paths")
        if isinstance(value, str):  # tolerate a ":"-joined string written by hand
            value = [p for p in value.split(os.pathsep) if p.strip()]
        if not isinstance(value, list):
            return []
        return [str(p) for p in value if isinstance(p, str) and p.strip()]

    @allowed_edit_paths.setter
    def allowed_edit_paths(self, value: list[str]) -> None:
        self.data["allowed_edit_paths"] = list(value)
        self.save()

    def backend_command(self, backend: str) -> list[str]:
        """Custom command used to launch *backend*, replacing the backend executable so
        a wrapper can sit beneath aGiTrack — e.g. ``["somewrapper", "claude"]`` runs the
        agent under ``somewrapper`` (aGiTrack's own sandbox wrapper then goes on top).

        Configured under ``"backend_command"`` as either a single command string
        (applies to whichever backend is launched) or an object mapping backend name →
        command string, so a user who switches backends can wrap each differently. A
        string is split like a shell command. Returns ``[]`` when unset/invalid, in
        which case aGiTrack launches the backend executable directly."""
        raw = self._raw("backend_command")
        if isinstance(raw, dict):
            raw = raw.get(backend)
        if isinstance(raw, list):  # tolerate a pre-split list written by hand
            return [str(token) for token in raw if isinstance(token, str) and token]
        if isinstance(raw, str) and raw.strip():
            import shlex

            try:
                # posix=False on Windows keeps backslashes in paths literal (not escapes).
                return shlex.split(raw, posix=(os.name != "nt"))
            except ValueError:
                return []  # an unbalanced quote can't lock the user out of launching
        return []

    @property
    def menu_key(self) -> str:
        # Normalized menu key spec. Supports "ctrl-<letter>" or "ctrl+shift+<letter>".
        # Invalid or conflicting values fall back to the default so a config typo
        # can never lock the user out of the menu.
        value = self._raw("menu_key")
        return normalize_menu_key(value) or DEFAULT_MENU_KEY if isinstance(value, str) else DEFAULT_MENU_KEY

    @property
    def menu_key_byte(self) -> bytes:
        # For plain ctrl-<letter>, return the control byte (0x01-0x1a).
        # For ctrl+shift+<letter>, return empty bytes (sequence-based matching).
        return b"" if self.is_shift_modified else menu_key_bytes_for(self.menu_key)

    @property
    def menu_key_sequence(self) -> bytes:
        # The bytes aGiTrack matches against stdin: the control byte for ctrl-<letter>,
        # or the kitty-keyboard CSI-u escape sequence for ctrl+shift+<letter>.
        return menu_key_bytes_for(self.menu_key)

    @property
    def is_shift_modified(self) -> bool:
        # True if the menu key uses ctrl+shift+<letter> format
        return menu_key_is_shift(self.menu_key)

    @property
    def menu_key_label(self) -> str:
        return menu_key_label_for(self.menu_key)

    @property
    def timings(self) -> dict[str, float]:
        # Defaults overlaid with any valid user overrides from the "timings" object.
        # An override must be a positive number; bad values (wrong type, <= 0, bool)
        # are ignored so a typo can never stall or busy-spin a poll loop.
        stored = self._raw("timings")
        stored = stored if isinstance(stored, dict) else {}
        result = dict(DEFAULT_TIMINGS)
        for key in DEFAULT_TIMINGS:
            value = stored.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                result[key] = float(value)
        return result

    @property
    def summarization_model(self) -> str | None:
        value = self._raw("summarization_model")
        return str(value) if value else None

    @summarization_model.setter
    def summarization_model(self, value: str | None) -> None:
        self.data["summarization_model"] = value
        self.save()

    @property
    def summarization_enabled(self) -> bool:
        value = self._raw("summarization_enabled")
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
        value = self._raw("check_for_updates")
        return True if value is None else bool(value)

    @check_for_updates.setter
    def check_for_updates(self, value: bool) -> None:
        self.data["check_for_updates"] = bool(value)
        self.save()

    @property
    def pending_manual_update(self) -> str | None:
        """Version of an update whose *automatic* install failed (or that the user
        chose not to retry after a failure). While set, aGiTrack shows a one-time
        manual-update reminder at startup but suppresses the regular in-session
        update notice, so a user on an older version isn't nagged repeatedly. It is
        cleared once aGiTrack is running that version or newer."""
        value = self.data.get("pending_manual_update")
        return value if isinstance(value, str) and value else None

    @pending_manual_update.setter
    def pending_manual_update(self, value: str | None) -> None:
        if value:
            self.data["pending_manual_update"] = str(value)
        else:
            self.data.pop("pending_manual_update", None)
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

    def _share_byte_setting(self, key: str, default: int) -> int:
        """A configurable byte size for the share size cap / head budget. Falls back to
        ``default`` on a missing or nonsensical value (wrong type, ≤ 0, bool) so a typo can't
        break sharing, and is hard-clamped to ``HARD_MAX_SHARED_BYTES`` so the effective value
        is ALWAYS shareable even if validation was somehow bypassed. ``share_config_error``
        surfaces an over-the-hard-limit value as an explicit startup error first."""
        from agitrack.sessions.share_cap import HARD_MAX_SHARED_BYTES

        raw = self._raw(key)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw <= 0:
            return default
        return min(int(raw), HARD_MAX_SHARED_BYTES)

    @property
    def share_max_transcript_bytes(self) -> int:
        """Max byte size of a shared session transcript. A longer session is trimmed (oldest
        middle turns dropped at a compaction boundary) so it never bloats git. Default 20 MiB;
        configurable, but never above the 100 MiB hard limit Git hosts allow."""
        from agitrack.sessions.share_cap import DEFAULT_MAX_SHARED_BYTES

        return self._share_byte_setting("share_max_transcript_bytes", DEFAULT_MAX_SHARED_BYTES)

    def share_config_error(self) -> str | None:
        """A human-readable error if the share-size cap is configured beyond the 100 MiB hard
        limit (Git hosts reject larger files), so startup can refuse with a clear message —
        else None. A non-numeric/≤0 value is NOT an error (it falls back to the default)."""
        from agitrack.sessions.share_cap import HARD_MAX_SHARED_BYTES

        raw = self._raw("share_max_transcript_bytes")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > HARD_MAX_SHARED_BYTES:
            return (
                f"config 'share_max_transcript_bytes' is {int(raw):,} bytes, over the maximum "
                f"{HARD_MAX_SHARED_BYTES // (1024 * 1024)} MiB ({HARD_MAX_SHARED_BYTES:,} bytes) — "
                f"Git hosts reject files larger than this. Lower it in your aGiTrack config.json."
            )
        return None
