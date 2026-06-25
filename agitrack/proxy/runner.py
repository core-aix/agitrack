from __future__ import annotations

import base64
import hashlib
import os
from agitrack.env import getenv_compat
import queue
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
from types import FrameType
from typing import Any, Callable, cast
import threading
import time

import pyte

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - exercised only without optional dependency
    FileSystemEvent = None  # type: ignore[misc, assignment]
    FileSystemEventHandler = object  # type: ignore[misc, assignment]
    Observer = None  # type: ignore[misc, assignment]

from agitrack.commits import AgitrackActions
from agitrack.backends.setup import BackendUnavailable, backend_installed, ensure_installed_backend, install_hint
from agitrack.backends.proxy_agents import available_backends, make_proxy_agent
from agitrack.commits import (
    METADATA_HEADER,
    apply_summary_to_message,
    build_user_commit_message,
    summary_metadata_lines,
)
from agitrack.git import GitRepo
from agitrack.config import GlobalConfig
from agitrack.git import RepoLock, already_running_message
from agitrack.proxy import host_prompt, sandbox
from agitrack.config import AgitrackState
from agitrack.git import WorktreeInfo, WorktreeManager, _sanitize_name, is_managed_branch
from agitrack.proxy.commit_engine import CommitEngine
from agitrack.proxy.integration import IntegrationService, MergeContext, MergePhase
from agitrack.proxy.platform import make_child_process, make_host_terminal, make_waker
from agitrack.proxy.process import BackendProcess
from agitrack.proxy.session import Session
from agitrack.transcripts import SessionRef


# Palette helpers, _BackgroundColorEraseScreen, and detect_color_mode live in
# renderer.py (P1). Re-import them here so call sites in this file are unchanged
# and the agitrack.proxy shim can export them under their original names.
from agitrack.proxy.renderer import (
    detect_color_mode,
    _BackgroundColorEraseScreen,
    ScreenRenderer,
)

# TerminalHost lives in terminal.py (P1).
from agitrack.proxy.terminal import TerminalHost
from agitrack.update import Updater, UpdateStatus, restart_agitrack

# Modal state-machines (P6 Stage 2): PromptModal and SelectModal encode the
# byte-handling logic for free-text and selection popups.
from agitrack.proxy.modal import PromptModal, SelectModal, _escape_sequence_complete

_SGR_MOUSE_RE = re.compile(rb"\x1b\[<\d+;\d+;\d+[Mm]")
_SGR_MOUSE_EVENT_RE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
# Legacy X10/normal mouse reports: ESC [ M then exactly three raw bytes (button, column, row —
# each the value offset by 32). Terminals that don't honour SGR mouse mode (?1006) — some tmux
# configs and the native Windows console — send THESE instead of the SGR form even though
# aGiTrack requested SGR. Their raw coordinate bytes are ordinary characters (column/row 3 is
# '#'), so if they go unrecognised they leak into the backend's input as stray text — the
# "mouse cursor hash". Matched and stripped alongside the SGR form. DOTALL: a coordinate byte
# can be any value, including ones the default '.' would skip.
_X10_MOUSE_RE = re.compile(rb"\x1b\[M.{3}", re.DOTALL)
# A trailing, not-yet-complete X10 report (ESC [ M with fewer than three coordinate bytes),
# held back like the SGR/CSI tail below so its bytes aren't forwarded split across reads.
_INCOMPLETE_X10_RE = re.compile(rb"\x1b\[M.{0,2}$", re.DOTALL)
# Terminal focus in/out reports (CSI I / CSI O), emitted on window focus changes
# when focus reporting is on. Like mouse reports they are not keystrokes.
_FOCUS_EVENT_RE = re.compile(rb"\x1b\[[IO]")
_PAGE_KEY_RE = re.compile(rb"\x1b\[(5|6)(?:;\d+)?~")  # PageUp / PageDown (with optional modifiers)
# A trailing, not-yet-complete CSI sequence (e.g. a mouse report split across
# reads). Held back so it is not forwarded as stray bytes. A lone trailing ESC
# is deliberately NOT matched so the Escape key is never delayed.
_INCOMPLETE_TAIL_RE = re.compile(rb"\x1b\[[<0-9;]*$")
# Private-marker CSI sequences (parameter prefix ``>``, ``<`` or ``=``) — xterm
# keyboard/feature negotiation such as XTMODKEYS (``CSI > Ps m``), XTVERSION
# (``CSI > Ps q``) and the kitty keyboard protocol (``CSI < Ps u``). pyte cannot
# model these and, worse, mis-tokenises ``\x1b[>4m`` as the SGR ``\x1b[4m``
# (underline on), which then sticks to everything drawn afterwards. None of them
# affect the visible grid, so they are stripped from the copy fed to pyte. The
# ``?`` (DEC private) forms are deliberately NOT matched — pyte models several of
# them and aGiTrack syncs the rest from the raw output separately.
_PYTE_HOSTILE_CSI_RE = re.compile(rb"\x1b\[[<>=][0-9;:]*[ -/]*[@-~]")
# Keyboard-protocol negotiation the backend sends to its (virtual) terminal:
# kitty keyboard protocol push/set/pop/query (``CSI > flags u`` / ``CSI = ... u``
# / ``CSI < ... u`` / ``CSI ? u``) and xterm modifyOtherKeys (``CSI > 4 ; N m``).
# aGiTrack renders from a pyte model, so the HOST terminal never sees these unless
# they are mirrored — and without them the host keeps sending plain ``\r`` for
# Shift+Enter instead of the disambiguated encoding the backend's keybindings
# (e.g. Claude's newline-in-input) expect.
_KEYBOARD_PROTO_RE = re.compile(rb"\x1b\[(?:[><=][0-9;]*u|\?u|>4(?:;[0-9]+)?m)")
# Kitty keyboard protocol encoding for control keys: CSI <keycode> ; <modifiers> u
# For Ctrl-A through Ctrl-Z: keycode is 97-122 (lowercase a-z), modifier is 5 (Ctrl+base)
# We decode these back to plain control bytes (0x01-0x1a) so the menu key works
# even when the terminal is in kitty keyboard protocol mode.
_KITTY_CTRL_KEY_RE = re.compile(rb"\x1b\[(\d+);5u")
# Kitty keyboard protocol encoding for Escape key: CSI 27 u or CSI 27 ; 1 u
# We decode this back to plain \x1b so Esc handling works in kitty mode.
_KITTY_ESC_KEY_RE = re.compile(rb"\x1b\[27(?:;1)?u")
# xterm modifyOtherKeys (formatOtherKeys=0) encoding: CSI 27 ; <modifier> ; <code> ~
# This is the OTHER way a terminal can report modified keys (the kitty form above
# is CSI <code> ; <modifier> u). iTerm2 answers the backend's modifyOtherKeys
# negotiation with THIS ~-terminated form, so e.g. Ctrl-C arrives as \x1b[27;5;99~
# and Ctrl-G as \x1b[27;5;103~. Without decoding it, those are forwarded to the
# backend instead of opening aGiTrack's exit/menu. modifier 5 = Ctrl (1 + 4).
_MODIFY_OTHER_KEYS_RE = re.compile(rb"\x1b\[27;(\d+);(\d+)~")
# A bracketed paste (CSI 200~ ... CSI 201~, possibly split across reads, hence
# the `|$`): newlines inside it are pasted CONTENT, not prompt submissions.
_BRACKETED_PASTE_RE = re.compile(rb"\x1b\[200~.*?(?:\x1b\[201~|$)", re.S)

# String-level escape stripper for captured subprocess output (e.g. a backend updater's
# spinner) so it reduces to readable lines for a status message: drop CSI/OSC sequences and
# lone ESC pairs, and turn carriage-returns (used to redraw a spinner in place) into newlines
# so each frame is its own line and the last meaningful one survives.
_ANSI_CSI_OSC_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def _strip_ansi(text: str) -> str:
    return _ANSI_CSI_OSC_RE.sub("", text).replace("\r", "\n")


def _push_rejection_reason(error: str) -> str:
    """The most informative line of a failed `git push`'s stderr — the part that names WHY
    the remote rejected it (a stale lease, a protected ref, a declined hook, a permission
    denial). Git buries the reason among progress lines, so showing the whole blob (or a
    blind prefix slice) hides it; this surfaces the line that actually explains the failure."""
    lines = [line.strip() for line in (error or "").splitlines() if line.strip()]
    if not lines:
        return "no error output from git"
    markers = ("rejected", "denied", "declined", "permission", "forbidden", "protected", "stale info")
    for line in lines:
        if any(marker in line.lower() for marker in markers):
            return line[:200]
    return lines[-1][:200]  # else the last line, which usually carries git's summary


# The claude CLI refuses to resume a session that is still held by a running
# background agent, printing this and exiting (e.g. a stale `bg` agent from an
# earlier run). Its own remedy is --fork-session, which aGiTrack applies
# automatically rather than crash-looping and exiting (#114).
_FORK_HINT_RE = re.compile(r"running as a background agent|--fork-session", re.IGNORECASE)


# Sentinel: the summarizer-model picker was dismissed (Esc), distinct from a real None choice
# ("same as the session model"). Lets _choose_summarizer_model report "no change" unambiguously.
_NO_MODEL_PICK = object()


def _shared_transcript_rows(transcript: str) -> int:
    # Recorded in the share manifest so the resume menu can tell at a glance whether
    # a shared copy is older/newer than the local one without reading either blob.
    from agitrack.sessions import count_transcript_rows

    return count_transcript_rows(transcript)


def _decode_kitty_ctrl_keys(data: bytes) -> bytes:
    """Decode enhanced-keyboard-protocol control keys and Escape to plain bytes.

    When the backend negotiates an enhanced keyboard protocol, the host terminal
    starts reporting modified keys as escape sequences instead of plain bytes. Two
    encodings exist, and which one a terminal uses depends on the terminal:
    - kitty keyboard protocol:  Ctrl-A → \\x1b[97;5u,  Escape → \\x1b[27u
    - xterm modifyOtherKeys:    Ctrl-A → \\x1b[27;5;97~, Escape → \\x1b[27;1;27~
      (iTerm2 answers with this ~-terminated form).

    This function converts both back to plain bytes so the menu key matching and
    Esc handling work regardless of which form the host terminal uses.

    Only decodes pure-Ctrl keys (modifier 5 = Ctrl) and Escape; every other
    modified key is left untouched so it still reaches the backend encoded.
    """
    # First decode Escape key (both encodings).
    data = _KITTY_ESC_KEY_RE.sub(b"\x1b", data)

    def _ctrl_letter_byte(keycode: int) -> bytes | None:
        # keycode 97-122 = lowercase a-z → Ctrl-A=0x01 .. Ctrl-Z=0x1a.
        if 97 <= keycode <= 122:
            return bytes([keycode - 96])
        return None

    # kitty form: CSI <code> ; 5 u
    def replace_kitty_ctrl(match: re.Match) -> bytes:
        return _ctrl_letter_byte(int(match.group(1))) or match.group(0)

    data = _KITTY_CTRL_KEY_RE.sub(replace_kitty_ctrl, data)

    # modifyOtherKeys form: CSI 27 ; <modifier> ; <code> ~
    def replace_modify_other_keys(match: re.Match) -> bytes:
        modifier, keycode = int(match.group(1)), int(match.group(2))
        if keycode == 27 and modifier == 1:  # bare Escape
            return b"\x1b"
        if modifier == 5:  # pure Ctrl
            return _ctrl_letter_byte(keycode) or match.group(0)
        return match.group(0)

    return _MODIFY_OTHER_KEYS_RE.sub(replace_modify_other_keys, data)


def _short_session(session_id: str | None) -> str:
    if not session_id:
        return "(none)"
    return session_id[:8]


class RepoChangeHandler(FileSystemEventHandler):
    IGNORED_PARTS = {".agitrack", ".git", ".pytest_cache", ".venv", "__pycache__"}

    def __init__(self, repo_path, changed: threading.Event, wake: "threading.Event | None" = None) -> None:
        self.repo_path = repo_path
        self.changed = changed
        # The git worker sleeps on `wake`; setting it lets a real worktree write
        # wake the worker at once instead of waiting for its poll timeout.
        self.wake = wake

    def on_any_event(self, event: FileSystemEvent) -> None:
        # watchdog reports src_path as str or bytes depending on how the watch was
        # set up; normalise to str so the IGNORED_PARTS check is uniform.
        src_path = os.fsdecode(event.src_path)
        try:
            relative = os.path.relpath(src_path, self.repo_path)
        except ValueError:
            relative = src_path
        if any(part in self.IGNORED_PARTS for part in relative.split(os.sep)):
            return
        self.changed.set()
        if self.wake is not None:
            self.wake.set()


class _ModalRequest:
    """A dialog the git worker needs the main (reactor) thread to present.

    The worker can't touch stdin/screen, so it enqueues one of these on
    ``_modal_mailbox`` and blocks on ``done`` until the main thread has run the
    modal and stored the user's answer in ``result``.
    """

    __slots__ = ("modal", "done", "result")

    def __init__(self, modal) -> None:
        self.modal = modal
        self.done = threading.Event()
        self.result: "str | None" = None


class ProxyInput:
    # Order matters (shown in the palette). Only "sessions" starts with "s" so
    # that pressing s+Enter jumps straight to the session picker. Git-specific
    # commands are grouped under a "git-" prefix.
    COMMANDS = [
        "sessions",
        "agent-backend",
        "git-unstaged",
        "git-user-commit",
        "dashboard",
        "settings",
        "update",
        # Reads "exit aGiTrack" so it is never mistaken for "exit the menu" (Esc does that).
        # `_run_command` parses the command name from the first word, so the trailing word is
        # ignored and "exit"/"quit" still work when typed directly.
        "exit aGiTrack",
    ]

    def __init__(self, menu_key: bytes = b"\x07") -> None:
        self.capturing = False
        self.buffer = bytearray()
        self.selected_index = 0
        self.escape_buffer: bytearray | None = None
        # The control byte or sequence that opens the command menu (default Ctrl-G;
        # configurable via "menu_key" in ~/.agitrack/config.json). For shift-modified keys,
        # this is a multi-byte kitty keyboard protocol escape sequence.
        self.menu_key = menu_key
        # Buffer for accumulating partial matches of multi-byte menu key sequences.
        # Only used when menu_key is longer than 1 byte.
        self.menu_key_buffer = bytearray()
        # Context-dependent commands shown ABOVE the fixed COMMANDS (the runner refreshes
        # this when the palette opens). Used to surface "merge" at the top when there are
        # unmerged worktrees the user should rescue.
        self.extra_commands: list[str] = []

    def feed(self, data: bytes) -> tuple[list[bytes], bytes, str | None, bool]:
        forwarded: list[bytes] = []
        command = None
        should_exit = False
        for byte in data:
            char = bytes([byte])
            if char == b"\x03":
                if self.capturing:
                    # Inside aGiTrack's own command palette, Ctrl-C cancels it
                    # (like Esc) rather than starting the exit flow.
                    self.buffer.clear()
                    self.capturing = False
                    self.selected_index = 0
                    self.escape_buffer = None
                    continue
                # Ctrl-C starts aGiTrack's exit flow: the first press opens the
                # confirmation popup, and a second press while it is open exits
                # immediately but still gracefully (see _run_exit_flow).
                should_exit = True
                break
            if self.capturing:
                if self.escape_buffer is not None:
                    self.escape_buffer.extend(char)
                    sequence = bytes(self.escape_buffer)
                    if sequence in {b"\x1b[A", b"\x1b[B"}:
                        self._move_selection(-1 if sequence == b"\x1b[A" else 1)
                        self.escape_buffer = None
                    elif _escape_sequence_complete(sequence):
                        self.escape_buffer = None
                    continue
                if char == b"\x1b":
                    if byte == data[-1]:
                        self.buffer.clear()
                        self.capturing = False
                        self.selected_index = 0
                        self.escape_buffer = None
                        continue
                    self.escape_buffer = bytearray(char)
                    continue
                if char in {b"\r", b"\n"}:
                    typed = self.buffer.decode(errors="ignore").strip()
                    command = self.selected() or typed
                    self.buffer.clear()
                    self.capturing = False
                    self.selected_index = 0
                elif char in {b"\x7f", b"\b"}:
                    if self.buffer:
                        self.buffer.pop()
                        self.selected_index = 0
                elif char == b"\t":
                    match = self.selected()
                    if match:
                        self.buffer = bytearray(match.encode())
                        self.selected_index = 0
                else:
                    self.buffer.extend(char)
                    self.selected_index = 0
                continue

            # Check for menu key match (single byte or multi-byte sequence)
            if len(self.menu_key) == 1:
                # Single byte menu key (plain ctrl-<letter>)
                if char == self.menu_key:
                    self.capturing = True
                    self.selected_index = 0
                    self.escape_buffer = None
                    continue
                forwarded.append(char)
            else:
                # Multi-byte menu key sequence (ctrl+shift+<letter>)
                self.menu_key_buffer.append(byte)
                buffer_bytes = bytes(self.menu_key_buffer)

                # Check if we have a complete match
                if buffer_bytes == self.menu_key:
                    self.capturing = True
                    self.selected_index = 0
                    self.escape_buffer = None
                    self.menu_key_buffer.clear()
                    continue

                # Check if we have a partial match (prefix of menu_key)
                if self.menu_key.startswith(buffer_bytes):
                    # Still accumulating, don't forward yet
                    continue

                # No match - forward the buffered bytes (which includes current byte)
                # and clear the buffer
                forwarded.append(buffer_bytes)
                self.menu_key_buffer.clear()

        return forwarded, b"", command, should_exit

    def text(self) -> str:
        return self.buffer.decode(errors="ignore")

    def matches(self) -> list[str]:
        commands = self.extra_commands + self.COMMANDS
        text = self.text()
        if not text:
            return commands
        return [command for command in commands if command.startswith(text)] or commands

    def selected(self) -> str | None:
        matches = self.matches()
        if not matches:
            return None
        self.selected_index = min(self.selected_index, len(matches) - 1)
        return matches[self.selected_index]

    def _move_selection(self, delta: int) -> None:
        matches = self.matches()
        if matches:
            self.selected_index = (self.selected_index + delta) % len(matches)


class ProxyRunner:
    # The active session's integration target. It is a Session.FIELDS entry exposed
    # via a custom property added at the bottom of this module; declared here so mypy
    # knows its type (the property is attached dynamically).
    _base_branch: "str | None"
    # Per-session flag (a Session.FIELDS entry, delegated like the others); declared here
    # so mypy can resolve its type at the read sites above its dynamic attachment.
    _pending_merge_prompt: bool

    # Defaults for the tunable timings; overridden per-instance from the global
    # config in __init__ (see GlobalConfig.timings). Kept as class constants so
    # `self.X` still resolves for runners built via __new__ (tests).
    FILE_STABLE_SECONDS = 8.0
    CHILD_IDLE_SECONDS = 4.0
    POLL_SECONDS = 2.0
    PARSE_COOLDOWN_SECONDS = 10.0
    BASE_POLL_SECONDS = 3.0
    IDLE_AFTER_SECONDS = 30.0  # idle threshold: no input/output/work for this long ⇒ low-power loop
    IDLE_POLL_SECONDS = 30.0  # select timeout while idle (vs ACTIVE_POLL_SECONDS when working)
    ACTIVE_POLL_SECONDS = 1.0  # select timeout while active; every background sweep self-throttles to ≥2s anyway
    BASE_EDIT_CHECK_SECONDS = 3.0
    CWD_CHECK_SECONDS = 3.0
    BASE_DRIFT_CHECK_SECONDS = 2.0
    RENDER_MIN_INTERVAL = 0.033  # coalesce output-driven repaints to ~30fps
    SYNC_MAX_HOLD = 0.05  # cap how long a backend synchronized-update may defer a paint
    SUMMARY_WAIT_SECONDS = 45.0  # how long integration waits for a background commit summary (#8)
    # On exit we don't start new summaries (see _start_commit_summary); we only give a
    # summary that was ALREADY running (from the live path) a brief moment to land before
    # integrating, then drop it rather than holding teardown hostage. Short on purpose —
    # the commit already has a usable prompt-based message, so a slow summary isn't worth
    # the wait. Per session.
    EXIT_SUMMARY_GRACE_SECONDS = 3.0
    EXIT_SHARE_TIMEOUT = (
        45.0  # cap the exit-path auto-share push; long enough for a real push, short enough to never truly hang
    )
    SHARED_FETCH_TIMEOUT = 30.0  # cap the shared-session listing fetch so a slow/offline remote can't hang the menu
    RESUME_FETCH_TIMEOUT = 300.0  # cap the full-transcript fetch when resuming a shared session; generous since the user can cancel manually
    SHARE_PUSH_TIMEOUT = (
        120.0  # cap a background share/update push to origin so a stalled remote can't strand the worker thread
    )

    def __init__(
        self,
        repo: GitRepo,
        *,
        verbose: bool = False,
        backend: str | None = None,
        new_session: bool = False,
        use_worktrees: bool = True,
        backend_args: list[str] | None = None,
        commit_guidance: bool = True,
        full_agent_messages: bool = False,
        delay_merge: bool = False,
        sandbox: bool = True,
        allowed_edit_paths: list[str] | None = None,
        backend_command: list[str] | None = None,
        # True when cli.py already ran the blocking startup gh-availability prompt for
        # this launch, so run() skips its own in-TUI gh notice (avoids double-nagging).
        gh_prechecked: bool = False,
        # Suppress the privacy acknowledgment (set on an in-app menu re-exec, where the
        # user already acknowledged it earlier this session) — see _acknowledge_privacy_warning.
        skip_privacy_ack: bool = False,
        # Optional injected collaborators (default to production construction).
        # These keyword arguments are for testing and advanced use; the CLI call
        # site passes only the first five parameters and is unaffected.
        _global_config: "GlobalConfig | None" = None,
        _state: "AgitrackState | None" = None,
        _integration: "IntegrationService | None" = None,
        _lock: "RepoLock | None" = None,
    ) -> None:
        # Attach the initial session; per-session state lives on it.
        self.active = Session.bare()
        self.repo = repo
        self._use_worktrees = use_worktrees  # #9: when False, run on the current branch directly
        self._warned_parallel_no_worktree = False  # one-time shared-tree caveat for extra --no-worktree sessions
        # When True, tell a coding agent that aGiTrack auto-commits so it doesn't self-commit
        # (--no-commit-guidance / config turns it off). Appended to the agent's system prompt
        # where the backend supports it (Claude).
        self._commit_guidance = commit_guidance
        # Effective sandbox settings (CLI flag wins over config; resolved in cli.py).
        self._sandbox = sandbox
        self._allowed_edit_paths = list(allowed_edit_paths or [])
        # Per-run override for the "include all agent messages" trace option
        # (--full-agent-messages). True forces it on for this run regardless of the
        # per-repo config; False (the default) defers to ``state.full_agent_messages``.
        self._full_agent_messages = full_agent_messages
        # --delay-merge: when True, a turn's committed changes are NOT auto-integrated
        # into the base branch. Instead the user is told to review/edit them in the
        # working directory and merge on their confirmation (session menu → "Merge
        # reviewed changes"). Off by default (aGiTrack integrates each turn immediately).
        self._delay_merge = delay_merge
        # Extra CLI args forwarded verbatim to every backend spawn (#32).
        self._backend_args = list(backend_args or [])
        # Per-run override (from --backend-command) for the command that launches the
        # backend, replacing its executable so the agent runs under a user wrapper. When
        # empty, the per-backend config value (GlobalConfig.backend_command) applies. The
        # override wins for whatever backend is active; the config form is keyed by backend.
        self._backend_command = list(backend_command or [])
        # cli.py already showed the blocking startup gh prompt ⇒ suppress the in-TUI notice.
        self._gh_prechecked = gh_prechecked
        self._skip_privacy_ack = skip_privacy_ack
        self._force_new_session = new_session  # start a fresh conversation, do not resume
        self.name = "main"  # session label (multiplexer assigns names to others)
        self._primary_worktree_name: str | None = None  # session kept across exits for auto-resume
        self._exit_resume_worktree: str | None = None  # session active at exit → auto-resumes next start
        self.worktree: WorktreeInfo | None = None  # set when this session runs in a git worktree
        self.global_config = _global_config if _global_config is not None else GlobalConfig()
        # Load THIS repo's .agitrack/config.json overlay so repo-scoped settings are both READ
        # and WRITABLE in the session. Without it `repo_path` is None and save_repo() silently
        # drops every "this repository only" settings change (e.g. turning worktrees off), so
        # the change never persists and reverts on the next launch. Uses the base repo path
        # (this arg, before any worktree is created) so the overlay is the durable one.
        if getattr(self.global_config, "repo_path", "set") is None:
            self.global_config.load_repo_overlay(repo.repo)
        self._apply_timings(self.global_config.timings)
        self.state = (
            _state
            if _state is not None
            # An explicit --backend seeds the state's default so a brand-new repo (no
            # stored backend) resolves to it rather than to the configured default,
            # which may be unset (there is no hardcoded fallback).
            else AgitrackState(repo.repo, default_backend=backend or self.global_config.default_backend)
        )
        if backend and backend != self.state.backend:
            self.state.remember_backend_session()
            self.state.backend = backend
            self.global_config.default_backend = backend
            self.state.backend_session_id = self.state.stored_backend_session(backend)
            self.state.last_backend_message_id = None
        self.backend = make_proxy_agent(self.state.backend)
        self.actions = AgitrackActions(repo, self.state, verbose=verbose)
        self.verbose = verbose
        # Use the menu key sequence (single byte for ctrl-<letter>, multi-byte
        # escape sequence for ctrl+shift+<letter>)
        menu_key_seq = self.global_config.menu_key_sequence
        self.input = ProxyInput(menu_key=menu_key_seq if menu_key_seq else self.global_config.menu_key_byte)
        self.child_pid: int | None = None
        self.master_fd: int | None = None
        self.last_poll = 0.0
        self.status_check_pending = False
        self.file_change_event = threading.Event()
        self.file_observer: Any = None
        self.parse_pending = False
        self.last_parse_start = 0.0
        self.running = True
        self.old_attrs: Any = None
        self.original_sigwinch: Callable[[int, FrameType | None], Any] | int | None = None
        self.original_signal_handlers: dict = {}
        self.rows = 24
        self.cols = 80
        self.screen: pyte.HistoryScreen | None = None
        self.stream: pyte.ByteStream | None = None
        # Scrollback: whether the backend manages the mouse itself (OpenCode) or
        # aGiTrack must provide wheel-driven scrollback (Claude streams to the normal
        # screen and relies on native scrollback, which aGiTrack's render replaces).
        self.child_mouse = False
        self.scroll_back = 0
        self._last_render = 0.0
        self._render_pending = False
        # Synchronized output (DECSET 2026): while the backend is mid-update we
        # hold the repaint so a half-drawn frame is never shown (tearing), and
        # each repaint aGiTrack emits is itself wrapped in a 2026 update so the host
        # applies the whole frame atomically (the flicker fix).
        self._in_sync_update = False
        self._sync_since = 0.0
        # Mouse drag selection -> clipboard (for backends aGiTrack renders itself).
        self.sel_active = False
        self.sel_anchor: tuple[int, int] | None = None
        self.sel_point: tuple[int, int] | None = None
        self._input_tail = b""
        self.last_child_output = 0.0
        self.last_user_input = 0.0  # monotonic time of the user's last keystroke (drives idle backoff)
        self.last_child_output_sample = b""
        # Set when the backend exits repeatedly and aGiTrack stops relaunching, so
        # run() can echo the reason on the restored host screen instead of leaving
        # the user with a silent flash (#114).
        self._backend_exit_notice: str | None = None
        # The claude session we want to resume is held by a running background agent:
        # fork a copy on the next spawn (claude --fork-session), and remember we did so
        # we only try once rather than looping (#114).
        self._fork_next_spawn = False
        self._forked_for_busy = False
        self.last_status = ""
        self.last_status_change = 0.0
        self.message: str | None = None
        self.message_until = 0.0
        # Scroll position of an over-tall popup message and how far it can scroll (set on
        # render). 0/0 = the message fits, so PgUp/PgDn pass through to history scrolling.
        self._message_scroll = 0
        self._message_max_scroll = 0
        # Track whether we proactively enabled the kitty keyboard protocol for
        # shift-modified menu keys. Used for cleanup on exit.
        self._kitty_keyboard_enabled = False
        # A sticky message stays up until the user's next keypress instead of
        # timing out — used for the auto-commit confirmation so the user actually
        # sees that aGiTrack committed (and isn't misled by the backend asking to
        # commit work aGiTrack has already captured).
        self._message_sticky = False
        # Per-session status notices (commit/summary lifecycle), keyed by session
        # name and composed into a multi-line popup so concurrent sessions each
        # get their own line. Host-level (a display concern shared across sessions).
        self._session_notices: dict[str, tuple[str, float, bool]] = {}
        self._notice_shown = False  # True while self.message reflects the notices
        self._last_agent_commit_id: str | None = None
        # Prompts the user queued while the agent was busy; the next commit waits
        # for each to appear as a turn so a queued follow-up shares one commit with
        # the turn it follows instead of producing a second commit.
        self._awaited_followups: list[str] = []
        self.agent_parse_thread: threading.Thread | None = None
        self.agent_parse_result = None
        self.agent_parse_active = False
        self.agent_parse_lock = threading.Lock()
        self.agent_in_flight = False
        self.pre_agent_reconciled_status = ""
        self.last_parse_attempt_status = ""
        self.last_parse_finish = 0.0
        self.passthrough_prompt = bytearray()
        self.passthrough_escape: bytearray | None = None
        self.pending_forwarded: list[bytes] | None = None
        self.pending_prompt_text = ""
        # Session ids that existed before aGiTrack launched a fresh backend session,
        # used to identify (and then pin to) the session aGiTrack actually spawned
        # rather than chasing whichever session is globally newest.
        self._pre_spawn_session_ids: set[str] | None = None
        # Raw responses captured from the host terminal so we can answer the
        # same queries OpenCode makes (foreground/background/palette colors and
        # device attributes). Without these, OpenCode cannot detect the real
        # terminal theme and its colors do not match a native session.
        self.host_fg_value: bytes | None = None
        self.host_bg_value: bytes | None = None
        self.host_palette: dict[bytes, bytes] = {}
        self.host_da: bytes | None = None
        self.host_kitty_keyboard: bool = False
        self.color_mode = detect_color_mode()
        # Single-writer management: only one aGiTrack may auto-commit/merge in a
        # working tree. A second instance is refused at startup (see `run`).
        self.management_lock = _lock if _lock is not None else RepoLock(repo.repo / ".agitrack" / "lock")
        # Multiplexer: every session (active included) is a Session object in
        # `self.sessions`; `self.active` points at the one being serviced and
        # switching sessions is a pointer assignment. With a single session
        # this stays empty/identity and the loop is unchanged. `base_repo` is
        # the main working tree (worktrees branch off it).
        self.base_repo = repo
        # IntegrationService: encapsulates all branch/merge/integration policy.
        # base_branch is set at startup (run()) and updated by _perform_base_switch.
        self._integration: IntegrationService = (
            _integration if _integration is not None else IntegrationService(repo, None, menu_label=self._menu_label())
        )
        # _base_branch is per-session (a Session.FIELDS entry that delegates to the
        # active session); each session integrates into its own branch.
        self._repo_dir_branch: str | None = None  # branch checked out in the repo dir (for the status bar)
        # _pending_merge_prompt is per-session (a Session field) — see Session.FIELDS.
        self._integration_paused = False  # reserved; integration is no longer globally paused
        self._base_drift_check_at = 0.0
        self.turn = 0  # per-session transient-branch counter
        self.merge_ctx: MergeContext | None = None  # in-progress agent merge resolution
        self._pending_enter_at: float | None = None  # deferred submit of an injected prompt
        self._pending_enter_fd: int | None = None  # the PTY that injected prompt's Enter must go to
        self._base_advanced = False  # base moved; sync idle sessions onto it on the next loop pass
        self._last_base_head: str | None = None  # last-polled base HEAD, to catch out-of-band commits
        self._base_head_mtime = -1.0  # mtime signature of the repo dir's .git/HEAD (gates the branch poll)
        self._base_ref_mtime = -1.0  # mtime signature of the base branch ref (gates the base-HEAD poll)
        self._base_edits_declined_status: str | None = None  # base status the user declined to commit
        self._popup_exit_pending = False  # a popup Ctrl-C exit flow is running
        self._popup_exit_force = False  # second Ctrl-C inside the exit confirmation
        self._reap_pids: list[int] = []  # signalled backends awaiting their waitpid
        self._idle_integrate_at = 0.0  # throttle for integrating agent-made commits
        self._attach_uncovered_until = 0.0  # deadline for attaching traces to backend-made commits (#35)
        self._summary_thread: threading.Thread | None = None  # background commit-summary worker (#8)
        self._summary_result: dict | None = None  # finished summary awaiting main-thread application
        self._summary_pending: dict | None = None  # {"sha", "since"} while a summary is being computed
        self._precompact_thread: threading.Thread | None = None  # background pre-compaction summary worker
        self._precompact_result: dict | None = None
        # Automatic backend self-update: when the agent's own updater is sandbox-blocked,
        # aGiTrack runs it from its UNCONFINED proxy (not the sandboxed agent) on a background
        # thread, plus the finished result the main loop surfaces. See _maybe_auto_update_backend.
        self._backend_update_thread: threading.Thread | None = None
        self._backend_update_result: dict | None = None
        # Name of the backend we've already evaluated for an automatic self-update (when its own
        # updater is sandbox-blocked). Switching to a different backend re-evaluates (once).
        self._backend_update_checked_for: str | None = None
        self._base_poll_at = 0.0  # throttle for the base-HEAD poll
        self._warned_backend_session = False  # one-shot "use agit to start sessions" notice
        # Auto-share (issue #55): for sessions the user opted to keep shared, the
        # last-pushed content hash per backend session id, plus the in-flight
        # background push thread (only one at a time). Triggered per commit.
        self._auto_share_hash: dict[str, str] = {}
        self._auto_share_thread: threading.Thread | None = None
        # The live auto-share worker (off-thread) leaves its result here for the main loop to
        # surface; a notice is shown only on FAILURE/behind (success is silent, since it fires
        # every commit). None ⇒ nothing to report. See _service_auto_share_outcome.
        self._auto_share_outcome: dict | None = None
        # aGiTrack session ids (stable, never drift) that saw at least one committed turn
        # THIS run. The exit-path auto-share consults this so a session that was only
        # resumed and never typed into is not re-shared — robust where a transcript
        # digest is not, since Claude's resume id-churn changes the digest across runs
        # even with no user input.
        self._sessions_with_activity: set[str] = set()
        # Backend message ids of interrupted (user-cancelled) turns we've already
        # offered a commit-or-discard choice for, so a "decide later" answer doesn't
        # re-prompt every parse cycle. In-memory and per-run by design — a restart
        # offers the choice again if the changes are still sitting there.
        self._cancel_prompted: set[str] = set()
        # Worktree files we've already offered to copy into the base repo, keyed by
        # repo-relative path → content fingerprint, so a file the user accepted/left in
        # place is re-offered only once its content changes again (per-run, in-memory).
        self._worktree_sessions_cache: tuple[float, list] | None = None  # memoizes _worktree_sessions
        self._copy_prompted: dict[str, tuple[int, int]] = {}
        # Repo-relative paths the user declined to copy: muted so the SAME set isn't
        # re-asked even as its contents change. A genuinely new path un-mutes the whole
        # set (ask about all again); cleared on session switch and aGiTrack restart.
        self._copy_declined: set[str] = set()
        # A turn's copy offer, collected on the git worker (the worktree read) and handed to
        # the main thread to PRESENT — so the worker never blocks on the popup and keeps the
        # commit/summary/merge pipeline flowing while the user decides. `(context, collected)`.
        self._pending_copy_offer: tuple[str, tuple[list[str], list[tuple[str, tuple[int, int]]], set[str]]] | None = (
            None
        )
        # Settings-menu scratch: edits collected as pending changes (each with its chosen
        # scope) and written only when the user confirms "save" on the way out.
        self._settings_pending: dict[str, tuple[object, str, bool]] = {}
        self._settings_pending_timings: dict[str, tuple[float, str]] = {}
        # Lifecycle flags read before their first conditional assignment. These
        # MUST be initialized here: their getattr() guards were removed in P7,
        # and for_testing() seeding them alone would hide a missing init from
        # the suite (the real __init__ is the production path).
        self._monitor_base_edits = False
        self._base_check_at = 0.0
        self._cwd_drift_checked = False
        self._cwd_check_at = 0.0
        self._cwd_launch_at = 0.0  # epoch of the latest backend launch (set in _spawn)
        # Shared-session resume runs the (possibly slow) transcript fetch on a worker
        # thread while the menu waits (cancellably); the main loop completes the
        # resume. The cancel Event lets the user stop a slow fetch (Esc) and lets the
        # exit path stop any in-flight fetch immediately.
        self._shared_resume_thread: threading.Thread | None = None
        self._shared_resume_result: dict | None = None
        self._shared_resume_cancel: "threading.Event | None" = None
        # Fire-and-forget network share ops (e.g. unshare) run on daemon threads so the
        # session never freezes; _service_background_share_ops surfaces each result as a
        # notice when it finishes. Each entry: {key, thread, box, outcome_fn}.
        self._background_share_ops: list[dict] = []
        # A share refused because the SHARED copy is already newer (PublishResult.behind):
        # the background op stashes the context here, and _service_share_conflicts later
        # (on the main thread, NOT mid-iteration over _background_share_ops) prompts the
        # user to overwrite or merge. Each entry: {payload, store, display, session_id}.
        self._pending_share_conflicts: list[dict] = []
        self._relaunch_times: list[float] = []
        self._exiting = False
        self._finalized_on_exit = False
        # Set when the user presses Esc on the on-exit copy offer: aborts the in-progress
        # exit so they can deal with the worktree files themselves (see _run_exit_flow).
        self._exit_aborted = False
        # The metrics dashboard, when started from the Ctrl-G menu, runs as a separate
        # background process (read-only HTTP server) rather than in-process, so its git
        # log work and energy use are charged to that child, not the TUI (#110). It is
        # killed when aGiTrack exits (_stop_dashboard, plus the child's own parent-death
        # watchdog). None until first started.
        self._dashboard_proc: "subprocess.Popen[bytes] | None" = None
        self._dashboard_url: str | None = None
        # Set once the interactive exit flow has committed to quitting (worktree
        # removed). The reactor loop checks this after running a menu command so an
        # "exit"/"quit" command breaks the loop instead of falling through to the
        # timers phase and running git in the deleted worktree.
        self._exit_requested = False
        # The user's intentionally-unstaged files belong to the base working tree
        # (their repo), not the ephemeral session worktree; cache the list so the
        # status line can show its count without a per-frame disk read.
        self._user_declined: list[str] = []
        self.sessions: list[Session] = []
        self.worktree_manager: WorktreeManager | None = None
        # AGITRACK_DEBUG_RAW records every raw child-output / user-input chunk so an
        # interactive glitch (e.g. Claude's native session picker) can be replayed
        # byte-for-byte; it implies debug logging too.
        self.raw_capture = (getenv_compat("DEBUG_RAW") or "").strip().lower() in {"1", "true", "yes"}
        self.debug_proxy = (
            verbose or self.raw_capture or (getenv_compat("DEBUG_PROXY") or "").strip().lower() in {"1", "true", "yes"}
        )
        # One diagnostic-log file per run, in the base repo's .agitrack/ (survives the
        # per-run worktree teardown).
        self._diag_run = time.strftime("%Y%m%d-%H%M%S")
        # Self-update: a background check runs on a throttle; when the user opts
        # in, the update is applied once every session is finished and committed,
        # then aGiTrack re-execs itself.
        self._updater = Updater()
        self._update_status: UpdateStatus | None = None  # latest completed check result
        self._update_check_at = 0.0  # throttle for the periodic background check
        self._update_check_thread: threading.Thread | None = None
        self._update_worker_result: UpdateStatus | None = None  # worker -> main handoff
        self._update_offered = False  # have we notified the user about the current update?
        self._update_pending = False  # user accepted; apply once sessions finish
        self._update_applying = False  # apply+restart in progress
        self._pending_restart = False  # re-exec aGiTrack after the loop tears down
        self._reopen_after_exit = False  # host terminal closed; user asked to reopen in a new window
        # Git worker (the automatic commit/merge pipeline runs here, never on the main
        # reactor thread, so typing is never blocked by a git subprocess). The worker
        # marshals any user dialog back to the main thread via `_modal_mailbox`.
        self._main_thread_ident: int | None = None  # set in run(); identifies the reactor thread
        self._git_worker: threading.Thread | None = None  # the dedicated git pipeline thread
        self._stop_worker = False  # set to break the worker loop (exit/restart/teardown)
        self._git_wake = threading.Event()  # wakes the worker on file changes / session changes
        self._pipeline_lock = threading.RLock()  # guards a pipeline pass vs. session swap / exit
        self._modal_mailbox: queue.Queue = queue.Queue()  # worker → main: dialogs to present
        self._wake_r = -1  # self-pipe read end (added to select) — wakes the reactor on demand
        self._wake_w = -1  # self-pipe write end — the worker writes to wake the main loop
        # Platform host-I/O (issue #118), created in run(): the host terminal (raw mode +
        # a select-able stdin source) and the reactor waker. POSIX wraps termios/os.pipe;
        # Windows uses the Win32 console + socketpairs. None until run() (and in tests).
        self._host: Any = None
        self._waker: Any = None
        self._last_shutdown_check = 0.0  # rate-limit the .agitrack/shutdown sentinel poll

    def _apply_timings(self, timings: dict[str, float]) -> None:
        # Override the class-constant timing defaults with the user's configured
        # values (GlobalConfig.timings already validated + filled in the defaults).
        self.FILE_STABLE_SECONDS = timings["file_stable_seconds"]
        self.CHILD_IDLE_SECONDS = timings["child_idle_seconds"]
        self.POLL_SECONDS = timings["background_poll_seconds"]
        self.PARSE_COOLDOWN_SECONDS = timings["parse_cooldown_seconds"]
        self.BASE_POLL_SECONDS = timings["base_poll_seconds"]
        self.BASE_EDIT_CHECK_SECONDS = timings["base_edit_check_seconds"]
        self.CWD_CHECK_SECONDS = timings["cwd_check_seconds"]
        self.BASE_DRIFT_CHECK_SECONDS = timings["base_drift_check_seconds"]
        self.SUMMARY_WAIT_SECONDS = timings["summary_wait_seconds"]
        self.UPDATE_CHECK_SECONDS = timings["update_check_seconds"]
        self.IDLE_AFTER_SECONDS = timings["idle_after_seconds"]
        self.IDLE_POLL_SECONDS = timings["idle_poll_seconds"]

    # --- session pointer -------------------------------------------------

    # NOTE: inside temp-swap helpers (_with_session, _pump_background,
    # _stop_session, _finalize_pending_work) `active` — and therefore
    # `active_index` — refers to the session being SERVICED, not the
    # user-facing foreground one. Do not call UI or session-list helpers
    # (_session_name, _session_status, _background_fds, popups) from code
    # reachable inside those windows; they would classify the real foreground
    # session as background.
    @property
    def active(self) -> Session:
        """The Session whose state the runner currently operates on."""
        return self.__dict__["_active_session"]

    @active.setter
    def active(self, session: Session) -> None:
        self.__dict__["_active_session"] = session
        # _base_branch is per-session; keep the single IntegrationService pointed at
        # whichever session is active (including the temporary actives set by
        # _with_session during background integration), so each merges into its own
        # branch. Skipped until the service exists (early construction).
        svc = self.__dict__.get("_integration_svc")
        if svc is not None:
            svc.base_branch = getattr(session, "_base_branch", None)

    @property
    def active_index(self) -> int:
        """Derived from the session pointer: position of the active Session in self.sessions."""
        sessions = self.__dict__.get("sessions")
        active = self.__dict__.get("_active_session")
        if sessions and active is not None:
            for index, session in enumerate(sessions):
                if session is active:
                    return index
        return 0

    @active_index.setter
    def active_index(self, index: int) -> None:
        sessions = self.__dict__.get("sessions")
        if sessions and 0 <= index < len(sessions) and isinstance(sessions[index], Session):
            self.active = sessions[index]

    @property
    def _integration(self) -> IntegrationService:
        """The integration service. Lazily constructed only as a safety net for
        partially-constructed runners; production wires it in __init__ and
        tests inject it via for_testing()/_integration kwarg."""
        svc = self.__dict__.get("_integration_svc")
        if svc is None:
            base_repo = self.__dict__.get("base_repo")
            # _base_branch is per-session now; seed the service from the active session.
            active = self.__dict__.get("_active_session")
            base_branch = getattr(active, "_base_branch", None) if active is not None else None
            # Production always wires base_repo before _integration is read; this lazy
            # branch is only a safety net for partially-constructed/test runners, which
            # may build the service before base_repo is set, so pass it through as-is.
            svc = IntegrationService(cast("GitRepo", base_repo), base_branch, menu_label=self._menu_label())
            self.__dict__["_integration_svc"] = svc
        return svc

    @_integration.setter
    def _integration(self, svc: IntegrationService) -> None:
        self.__dict__["_integration_svc"] = svc

    # ------------------------------------------------------------------
    # Test factory: builds a fully-initialised ProxyRunner without the
    # production __init__ path (which requires a real filesystem, a TTY,
    # etc.).  Call sites in tests must migrate from ProxyRunner.__new__
    # to ProxyRunner.for_testing(**overrides).
    # ------------------------------------------------------------------

    @classmethod
    def for_testing(cls, **overrides) -> "ProxyRunner":
        """Return a ProxyRunner suitable for unit tests.

        A real :class:`Session` is attached and all runner-level fields are
        initialised to safe defaults. Any keyword argument whose name matches
        a :data:`~agitrack.proxy.session.Session.FIELDS` entry is routed to the
        session; all other keyword arguments are set directly on the runner.

        Example::

            runner = ProxyRunner.for_testing(
                repo=fake_repo,
                state=AgitrackState(tmp_path),
                verbose=False,
            )
        """
        instance = cls.__new__(cls)

        # --- runner-level defaults (fields that live on the runner, not the session) ---
        instance.__dict__.update(
            {
                "verbose": False,
                "input": ProxyInput(),
                "running": True,
                "old_attrs": None,
                "original_sigwinch": None,
                "original_signal_handlers": {},
                "rows": 24,
                "cols": 80,
                "_last_render": 0.0,
                "_render_pending": False,
                "_in_sync_update": False,
                "_sync_since": 0.0,
                "message": None,
                "message_until": 0.0,
                "last_user_input": 0.0,
                "_message_scroll": 0,
                "_message_max_scroll": 0,
                "_message_sticky": False,
                "_gh_prechecked": False,
                "_skip_privacy_ack": False,
                "_session_notices": {},
                "_notice_shown": False,
                "_awaited_followups": [],
                "host_fg_value": None,
                "host_bg_value": None,
                "host_palette": {},
                "host_da": None,
                "host_kitty_keyboard": False,
                "color_mode": "truecolor",
                "management_lock": None,
                "base_repo": None,
                "_repo_dir_branch": None,
                "_integration_paused": False,
                "_base_drift_check_at": 0.0,
                "_pending_enter_at": None,
                "_pending_enter_fd": None,
                "_base_advanced": False,
                "_last_base_head": None,
                "_base_head_mtime": -1.0,
                "_base_ref_mtime": -1.0,
                "_base_edits_declined_status": None,
                "_popup_exit_pending": False,
                "_popup_exit_force": False,
                "_reap_pids": [],
                "_idle_integrate_at": 0.0,
                "_attach_uncovered_until": 0.0,
                "_precompact_thread": None,
                "_precompact_result": None,
                "_base_poll_at": 0.0,
                "_warned_backend_session": False,
                "_auto_share_hash": {},
                "_auto_share_thread": None,
                "_auto_share_outcome": None,
                "_backend_update_thread": None,
                "_backend_update_result": None,
                "_backend_update_checked_for": None,
                "_sessions_with_activity": set(),
                "_cancel_prompted": set(),
                "_worktree_sessions_cache": None,
                "_copy_prompted": {},
                "_copy_declined": set(),
                "_pending_copy_offer": None,
                "_settings_pending": {},
                "_settings_pending_timings": {},
                "_full_agent_messages": False,
                "_delay_merge": False,
                "_user_declined": [],
                "sessions": [],
                "worktree_manager": None,
                "raw_capture": False,
                "debug_proxy": False,
                "_diag_run": "test",
                "_force_new_session": False,
                "_primary_worktree_name": None,
                "_exit_resume_worktree": None,
                "global_config": None,
                # Lazily-set fields that getattr() guards in production methods:
                "_monitor_base_edits": False,
                "_base_check_at": 0.0,
                "_cwd_drift_checked": False,
                "_cwd_check_at": 0.0,
                "_cwd_launch_at": 0.0,
                "_shared_resume_thread": None,
                "_shared_resume_result": None,
                "_shared_resume_cancel": None,
                "_background_share_ops": [],
                "_pending_share_conflicts": [],
                "_use_worktrees": True,
                "_warned_parallel_no_worktree": False,
                "_sandbox": True,
                "_allowed_edit_paths": [],
                "_backend_command": [],
                "_commit_guidance": True,
                "_relaunch_times": [],
                "_fork_next_spawn": False,
                "_forked_for_busy": False,
                "_backend_exit_notice": None,
                "_exiting": False,
                "_finalized_on_exit": False,
                "_exit_aborted": False,
                "_dashboard_proc": None,
                "_dashboard_url": None,
                "_exit_requested": False,
                # Self-update fields (production sets these in __init__).
                "_updater": None,
                "_update_status": None,
                "_update_check_at": 0.0,
                "_update_check_thread": None,
                "_update_worker_result": None,
                "_update_offered": False,
                "_update_pending": False,
                "_update_applying": False,
                "_pending_restart": False,
                "_reopen_after_exit": False,
                "_main_thread_ident": threading.get_ident(),
                "_git_worker": None,
                "_stop_worker": False,
                "_git_wake": threading.Event(),
                "_pipeline_lock": threading.RLock(),
                "_modal_mailbox": queue.Queue(),
                "_wake_r": -1,
                "_wake_w": -1,
                "_host": None,
                "_waker": None,
                "_last_shutdown_check": 0.0,
                "UPDATE_CHECK_SECONDS": 300.0,
            }
        )
        # Apply timing class-constant defaults (so CHILD_IDLE_SECONDS etc. resolve).
        # These stay as class attributes; no instance-level override needed unless
        # the test provides one via **overrides below.

        # --- Separate session-level overrides from runner-level overrides ---
        session_fields = set(Session.FIELDS)
        session_overrides = {k: v for k, v in overrides.items() if k in session_fields}
        runner_overrides = {k: v for k, v in overrides.items() if k not in session_fields}

        # Build the session with any provided session-level values merged on top of
        # Session.bare() defaults.
        session_kwargs = Session.runtime_defaults()
        session_kwargs.update(session_overrides)
        session = Session(**session_kwargs)
        instance.__dict__["_active_session"] = session

        # Tests build minimal AgitrackStates without a backend; production always resolves
        # one before launch (there is no hardcoded fallback anymore), so reading
        # ``state.backend`` on an unconfigured state now raises. Seed a concrete backend so
        # the test factory keeps producing a launch-ready session unless a test set one.
        # Guarded so tests that pass a non-AgitrackState stub (no ``data`` dict) are untouched.
        state_data = getattr(session.state, "data", None)
        if isinstance(state_data, dict) and not state_data.get("backend"):
            state_data["backend"] = "claude"

        # Apply runner-level overrides. Names shadowed by a class property are
        # routed through it; a read-only property makes the misuse loud instead
        # of silently leaving the kwarg inert in __dict__.
        for key, value in runner_overrides.items():
            descriptor = getattr(cls, key, None)
            if isinstance(descriptor, property):
                if descriptor.fset is None:
                    raise TypeError(
                        f"for_testing() cannot set {key!r}: it is a read-only property derived from runner state"
                    )
                setattr(instance, key, value)
            else:
                instance.__dict__[key] = value

        # base_repo defaults to repo if not explicitly overridden.
        if instance.__dict__.get("base_repo") is None:
            repo = getattr(instance.active, "repo", None)
            if repo is not None:
                instance.__dict__["base_repo"] = repo

        return instance

    def run(self) -> int:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("Proxy mode requires an interactive terminal. Use --mode json for non-TTY use.")
        if not self._ensure_backend_available():
            return 1
        if not self.management_lock.acquire():
            print(already_running_message(self.management_lock.owner_pid()))
            return 1
        # Privacy warning LAST in the cooked-mode startup — after the backend
        # install/availability gate above, after the second-instance lock check (a refused
        # instance is never asked to acknowledge), and after cli.py's gh-login and menu-key
        # prompts — so it's the final thing the user acknowledges right before the full-screen
        # TUI takes over, not buried above those configuration steps.
        from agitrack.cli import _acknowledge_privacy_warning

        if not _acknowledge_privacy_warning(skip=self._skip_privacy_ack):
            self.management_lock.release()
            return 1
        self.state.save()
        if self.actions.has_pre_agent_user_changes():
            print("User changes detected before the agent starts.")
            self.actions.create_user_commit()
        # Base-merge-only: run even the first session in a worktree so the base
        # branch is only advanced by integration, never edited by a live agent.
        self._base_branch = self.base_repo.current_branch()
        self._integration.base_branch = self._base_branch
        # Cached branch checked out in the repo directory, refreshed by the drift
        # poll. The status bar bolds a session's integration branch when it differs
        # from this (e.g. after "keep integrating into X while the repo is on Y").
        self._repo_dir_branch = self._base_branch
        self._cleanup_stale_state_on_startup()
        self._reload_user_declined()  # files the pre-agent flow left intentionally unstaged
        self._setup_base_merge_only_session()
        self._apply_new_session_if_requested()
        self._sanitize_state_trace()
        self._initialize_session_baseline()
        # Stage the resume (retarget the backend's recorded working dir to THIS directory) BEFORE
        # the startup spawn. Crucially this is NOT gated on _should_continue_session: that gate
        # asks "does this session belong to this repo?" using the backend's recorded directory —
        # which is exactly what has gone stale (the repo was renamed, or worktrees were turned off
        # so the session that last ran in a now-removed worktree must move to the base repo). So
        # without retargeting first, our own recorded session is mistaken for a stranger and a
        # FRESH one starts in the wrong/old dir. Staging only ever moves OUR recorded session id
        # (set for this repo), so it's safe; after it, _spawn's gate sees the dir match and resumes.
        if self.state.backend_session_id and not self._force_new_session:
            self._stage_backend_resume(self.state.backend_session_id)
        self._init_screen()
        self._spawn()
        self._start_file_watcher()
        # Identify the reactor thread and open the self-pipe the git worker writes to
        # so it can wake `select` on demand (e.g. to present a dialog) instead of
        # waiting for the next poll tick. Then start the git worker itself.
        self._main_thread_ident = threading.get_ident()
        # Host I/O via the platform layer (#118): the waker (self-pipe on POSIX, socketpair
        # on Windows) the git worker uses to break the reactor out of select, and the host
        # terminal (termios fd 0 on POSIX, Win32 console + a stdin bridge socket on Windows).
        self._waker = make_waker()
        self._wake_r = self._waker.wake_fileno()
        self._host = make_host_terminal(self)
        self._start_git_worker()
        # Register the initial session as the sole (active) entry in the
        # multiplexer. Additional sessions are appended by `_new_session`.
        self.sessions = [self.active]
        self._reconcile_sessions_on_startup()
        # Reclaim dangling shared-session snapshots from prior runs, in the
        # background so startup never waits on it (issue #55).
        threading.Thread(target=lambda: self._sweep_orphan_shared_sessions(fetch=True), daemon=True).start()
        # Recommend installing / logging into gh when it's unavailable, since the
        # dashboard's committer identities and session sharing depend on it (#76).
        # Runs in the background so a slow `gh auth status` never blocks startup.
        # Skipped when cli.py already showed the blocking startup gh prompt (no double-nag).
        if not self._gh_prechecked:
            threading.Thread(target=self._notify_if_gh_unavailable, daemon=True).start()
        # Resolve the GitHub login now, off the hot path, so neither a live nor an
        # exit-time auto-share has to shell out to `gh` mid-share (that lookup can
        # stall and would otherwise eat into the bounded share budget).
        threading.Thread(target=self._warm_share_login, daemon=True).start()
        try:
            self._enter_host_screen()
            self._set_raw()  # POSIX saves old_attrs here; Windows enters console raw mode
            self._detect_host_terminal()
            self._resize_child()
            # SIGWINCH/SIGHUP don't exist on native Windows — there the host terminal's
            # resize watcher feeds _resize_child via consume_resize_pending() in the select
            # phase, and console-close is handled by the platform console-control handler.
            if sys.platform != "win32":
                self.original_sigwinch = signal.getsignal(signal.SIGWINCH)
                self.original_signal_handlers = {
                    signal.SIGTERM: signal.getsignal(signal.SIGTERM),
                    signal.SIGHUP: signal.getsignal(signal.SIGHUP),
                }
                signal.signal(signal.SIGWINCH, lambda _signum, _frame: self._resize_child())
                signal.signal(signal.SIGTERM, self._handle_exit_signal)
                signal.signal(signal.SIGHUP, self._handle_exit_signal)
            self._setup_worktree_confinement_notice()
            exit_code = self._loop()  # the timers phase auto-applies a sandbox-blocked backend update
        finally:
            if self.original_sigwinch is not None:
                signal.signal(signal.SIGWINCH, self.original_sigwinch)
            for signum, handler in self.original_signal_handlers.items():
                signal.signal(signum, handler)
            self._stop_git_worker()  # join before tearing the worktree/PTY down
            self._stop_file_watcher()
            self._stop_dashboard()
            self._cleanup_child()
            self._restore_terminal()
            if self._host is not None:
                self._host.stop()  # stop the Windows stdin/resize threads; no-op on POSIX
                self._host = None
            if self._waker is not None:
                self._waker.close()
                self._waker = None
            self._wake_r = self._wake_w = -1
            if self.master_fd is not None:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
                # Nulling routes through the platform child: a no-op extra on POSIX (the fd
                # is closed above), but on Windows the setter closes the ConPTY bridge socket
                # (os.close can't close a socket fd there), so it isn't leaked.
                self.master_fd = None
            self.management_lock.release()
            # The host terminal closed mid-work and the user chose "Reopen" in the
            # forced-exit dialog: launch a fresh window now that the lock is free so
            # the new instance can acquire it and auto-resume the session.
            if self._reopen_after_exit:
                self._reopen_in_new_window()
        # The backend kept exiting on launch and aGiTrack stopped relaunching: the
        # terminal is restored now, so surface why on the normal screen rather than
        # exiting with just a flash (#114).
        if self._backend_exit_notice:
            print(self._backend_exit_notice)
        # A self-update was applied; re-exec aGiTrack in place now that the terminal
        # is restored and the management lock is released. Does not return.
        if self._pending_restart:
            # Finalize may have removed the worktree this process was launched in; the
            # re-exec'd aGiTrack would then start with a deleted CWD and crash before it
            # could discover the repo. Move to the base repo root (which always exists)
            # first so the restart lands cleanly.
            try:
                os.getcwd()
            except OSError:
                try:
                    os.chdir(self.base_repo.repo)
                except OSError:
                    pass
            # This restart follows an in-app (menu) update; the user already saw
            # and acknowledged the privacy warning when this session started, so
            # don't make them acknowledge it again. (A startup-time update re-execs
            # via the CLI without this flag, so that path still shows the warning.)
            restart_agitrack(["--skip-privacy-ack"])
        return exit_code

    def _ensure_backend_available(self) -> bool:
        # A user-supplied launch command (--backend-command / config backend_command)
        # replaces the backend's own binary — a wrapper that ultimately execs the agent —
        # so the default backend CLI need not be installed on PATH. Skip the install gate
        # in that case (it would otherwise block a perfectly valid wrapped setup).
        if self._launch_command():
            return True
        try:
            resolved = ensure_installed_backend(self.state.backend, self.global_config, interactive=True)
        except BackendUnavailable as error:
            print(error)
            return False
        if resolved != self.state.backend:
            self.state.backend = resolved
            self.backend = make_proxy_agent(resolved)
        return True

    def _spawn(self) -> None:
        resume = self._should_continue_session()
        fork = self._fork_next_spawn and resume
        self._fork_next_spawn = False
        if resume:
            session_id = self.state.backend_session_id
            if fork:
                # --fork-session resumes this conversation but mints a NEW id (the
                # original is left to its running background agent). Snapshot existing
                # sessions so the forked one is discovered and adopted on first parse,
                # exactly like a backend that assigns its own id.
                self._pre_spawn_session_ids = {ref.id for ref in self.backend.list_sessions(self.repo.repo)}
            else:
                self._pre_spawn_session_ids = None
        else:
            session_id = self.backend.new_session_id()
            if session_id:
                # The backend lets aGiTrack choose the id, so it is pinned already.
                self.state.backend_session_id = session_id
                self._pre_spawn_session_ids = None
            else:
                # The backend assigns its own id; snapshot existing sessions so
                # the one it creates can be identified on the first parse.
                self._pre_spawn_session_ids = {ref.id for ref in self.backend.list_sessions(self.repo.repo)}
        command = self.backend.spawn_command(
            self.repo.repo,
            session_id=session_id,
            resume=resume,
            fork=fork,
            commit_guidance=self._commit_guidance,
            use_worktrees=self._use_worktrees,
            executable=self._launch_command() or None,
        )
        # Forward any backend-specific args the user passed through aGiTrack (#32),
        # before the sandbox wrapper so they reach the backend, not sandbox-exec.
        command = command + getattr(self, "_backend_args", [])
        command = self._confine_to_worktree(command)
        # Pseudo-terminal mechanics delegated to the platform child process (POSIX PTY
        # via BackendProcess, or Windows ConPTY via NtChildProcess); policy (command
        # construction, sandbox wrapping) stays here in the runner. The session owns its
        # child process; child_pid / master_fd remain readable on the runner via the
        # Session-delegating compat properties.
        self.active.process = make_child_process(command, str(self.repo.repo), extra_env=self._backend_child_env())
        # Re-arm the cwd-drift check for this launch, and remember when it started:
        # only turns recorded at/after this time count, so a stale cwd left in the
        # transcript before this launch can't trigger a false drift warning (#72).
        self._cwd_drift_checked = False
        self._cwd_check_at = 0.0
        self._cwd_launch_at = time.time()

    def _launch_command(self) -> list[str]:
        # The command that launches the current backend, replacing its executable with a
        # user wrapper (e.g. ["somewrapper", "claude"]). A per-run --backend-command
        # override wins; otherwise the per-backend config value applies. Resolved per
        # spawn because the active backend can change mid-session (menu switch / new
        # session with another backend), so the right wrapper follows the backend. Empty
        # ⇒ launch the backend binary directly.
        if self._backend_command:
            return list(self._backend_command)
        config = self.global_config
        if config is None:
            return []
        getter = getattr(config, "backend_command", None)
        return list(getter(self.backend.name)) if callable(getter) else []

    def _notify_if_gh_unavailable(self) -> None:
        # Recommend installing / authenticating gh when it isn't usable, so the
        # user knows why features that depend on it (the dashboard's committer
        # identities, session sharing) are limited (#76). Best-effort and one-shot;
        # runs off the startup thread so a slow auth check never blocks the UI.
        try:
            from agitrack.metrics.github import gh_status

            status = gh_status()
        except Exception as error:
            self._debug(f"gh availability check failed: {error!r}")
            return
        message = self._gh_unavailable_hint(status)
        if message is None:
            return  # gh is installed and authenticated — nothing to suggest
        self._set_message(message, seconds=12.0)
        self._render()

    def _warm_share_login(self) -> None:
        # Populate the GitHub-login cache at startup so auto-share never resolves it
        # mid-share. Only when sharing is actually reachable (backend supports it and
        # a remote exists), to avoid a pointless `gh` call on solo/offline repos.
        try:
            if not getattr(self.backend, "supports_session_sharing", False):
                return
            if self.global_config.github_login or not self.base_repo.remote_exists():
                return
            self._cached_or_resolve_login()
        except Exception as error:
            self._debug(f"warm share login failed: {error!r}")

    @staticmethod
    def _gh_unavailable_hint(status: str) -> str | None:
        if status == "missing":
            return (
                "GitHub CLI (gh) isn't installed. aGiTrack features that rely on it are limited —\n"
                "the dashboard can't resolve committer GitHub identities, and session sharing\n"
                "is unavailable. Install it from https://cli.github.com then run `gh auth login`."
            )
        if status == "unauthenticated":
            return (
                "GitHub CLI (gh) isn't logged in. aGiTrack features that rely on it are limited —\n"
                "the dashboard can't resolve committer GitHub identities, and session sharing\n"
                "is unavailable. Run `gh auth login` to enable them."
            )
        return None

    def _setup_worktree_confinement_notice(self) -> None:
        # When confinement is requested but the platform can't enforce it (no
        # sandbox), silently watch the base repo so we can warn *if and when* the
        # agent actually writes into it (see _warn_if_base_edited). We no longer show
        # a proactive "can't sandbox" notice at startup — it confused users by
        # flagging a capability limitation even when nothing went wrong.
        self._monitor_base_edits = False
        if self.worktree is None:
            return
        if not (self._sandbox and sandbox.is_enabled()):
            return  # user disabled confinement
        if sandbox.is_available():
            return  # the sandbox enforces it; nothing to monitor
        self._monitor_base_edits = True
        self._base_check_at = 0.0
        try:
            self._base_status_baseline: set[str] = set(self.base_repo.status_short().splitlines())
        except Exception:
            self._base_status_baseline = set()

    def _check_base_branch_drift(self) -> None:
        # Poll the branch checked out in the repo directory. The status bar bolds a
        # session whose merge target differs from it; when the directory MOVES to a
        # branch that differs from the active session's merge target, ask where this
        # session's changes should merge. (Each session has its own merge branch,
        # independent of the directory's checkout and of the other sessions.)
        if self._base_branch is None:
            return
        if not self._use_worktrees:
            # No-worktree mode (#9): the agent edits the directory's branch directly, so a
            # session can only ever land its work on that current branch — never a different
            # merge target. There is no merge-target dialog; just warn (and follow) when the
            # directory's branch is switched.
            self._check_no_worktree_branch_change()
            return
        if self.worktree is None:
            return
        now = time.monotonic()
        if now - self._base_drift_check_at < self.BASE_DRIFT_CHECK_SECONDS:
            return
        self._base_drift_check_at = now
        # Cheap pre-check: the checked-out branch only changes when `.git/HEAD` is
        # rewritten (a checkout — a commit does not touch it), so skip the git
        # subprocess and reuse the cached branch when HEAD's mtime is unchanged. The
        # pending-merge-prompt handling below depends on session merge state, not
        # HEAD, so it still runs every tick.
        git_dir = self._base_git_dir()
        head_sig = self._newest_mtime([git_dir / "HEAD"]) if git_dir is not None else None
        if head_sig is not None and head_sig == self._base_head_mtime:
            current = self._repo_dir_branch  # HEAD untouched — branch is unchanged
        else:
            if head_sig is not None:
                self._base_head_mtime = head_sig
            try:
                current = self.base_repo.current_branch()  # the branch checked out in the repo dir
            except Exception:
                return
        if current is None:
            return
        moved = current != self._repo_dir_branch
        self._repo_dir_branch = current  # keep the status bar's "current dir branch" fresh
        # A dir-change prompt this session deferred during a run fires once the run is idle
        # AND its changes have MERGED into the session's original branch — not the moment
        # the run goes idle (the just-finished work is still committing/integrating then,
        # and must land on the original branch before we ask where future work should go).
        # A cancelled run leaves nothing to integrate, so the dialog is free to appear. The
        # flag is per-session, evaluated against the FRESH dir branch: if the directory has
        # returned to this session's branch there's nothing to ask, and a different
        # session's pending prompt is never touched (it has its own flag).
        if self._pending_merge_prompt:
            if self._base_branch == current:
                self._pending_merge_prompt = False  # back in sync — this session's deferral is moot
            elif not self.agent_in_flight and self._session_work_merged_into_base():
                self._pending_merge_prompt = False
                self._prompt_merge_targets_on_dir_change()
            return  # handled this session's pending deferral; nothing else to do this tick
        if not moved:
            return  # the repo directory hasn't moved since the last poll
        if self._base_branch == current:
            return  # the session already merges into the directory's branch
        if self.agent_in_flight:
            # The session is mid-run — don't change its merge branch now. Warn that this
            # run still lands on its current branch, and re-ask once its changes have
            # merged there (or the run is cancelled, leaving nothing to merge).
            self._pending_merge_prompt = True
            self._set_message(
                f"Repo is now on '{current}', but this run still merges into '{self._base_branch}'. "
                f"You'll be asked again once its changes have merged into '{self._base_branch}'.",
                seconds=10.0,
            )
            self._render()
            return
        self._prompt_merge_targets_on_dir_change()

    def _check_no_worktree_branch_change(self) -> None:
        # No-worktree mode: a session always works on (merges into) whatever branch is
        # checked out in the repo directory and can never target a different one. If the
        # user switches the directory's branch, future work simply lands on the new branch —
        # warn so that change is never silent, and follow the directory so the status bar and
        # accounting stay accurate. The warning fires once per switch (``_base_branch`` is
        # advanced immediately, so the next poll sees no change).
        now = time.monotonic()
        if now - self._base_drift_check_at < self.BASE_DRIFT_CHECK_SECONDS:
            return
        self._base_drift_check_at = now
        # Cheap pre-check: a checkout rewrites `.git/HEAD`; if its mtime is unchanged
        # the branch can't have switched, so skip the git subprocess entirely.
        git_dir = self._base_git_dir()
        if git_dir is not None:
            head_sig = self._newest_mtime([git_dir / "HEAD"])
            if head_sig == self._base_head_mtime:
                return
            self._base_head_mtime = head_sig
        try:
            current = self.base_repo.current_branch()  # the branch checked out in the repo dir
        except Exception:
            return
        if not current or current == self._base_branch:
            if current:
                self._repo_dir_branch = current  # keep the status bar fresh
            return
        old = self._base_branch
        self._base_branch = current
        self._repo_dir_branch = current
        self._integration.base_branch = current
        self._set_message(
            f"The repo directory switched from '{old}' to '{current}'. Running without a "
            f"worktree, the agent's changes now land on '{current}' — a session always "
            f"follows the directory's current branch and can't merge into a different one.",
            seconds=12.0,
        )
        self._render()

    def _session_work_merged_into_base(self) -> bool:
        # True once the active session's work has landed on its merge branch — nothing
        # uncommitted, mid-merge, or committed-but-unintegrated remains. Used to hold the
        # deferred branch-switch dialog until a just-finished run's changes have merged
        # into the original branch (a cancelled run leaves nothing pending, so it's True).
        try:
            return not self._integration.session_unintegrated(self.repo)
        except Exception:
            return True

    def _prompt_merge_targets_on_dir_change(self) -> None:
        # The repo directory was switched to another branch. Sessions keep merging into
        # their own branches by default; offer to realign them. The first (default)
        # option does nothing; then "switch only this session"; then, with more than
        # one session, "switch ALL sessions" to the directory's branch.
        if self.worktree is None or self._base_branch is None or self._repo_dir_branch is None:
            return
        if self._base_branch == self._repo_dir_branch:
            return  # the active session already merges into the directory's branch
        dir_branch = self._repo_dir_branch
        options = [
            "Do nothing — keep every session merging into its own branch",
            f"Switch only '{self.name}' to '{dir_branch}'",
        ]
        if len(self.sessions or []) > 1:
            options.append(f"Switch all idle sessions to '{dir_branch}' (running sessions keep their branch)")
        choice = self._select_popup(
            f"The repo directory is now on '{dir_branch}', but session '{self.name}' merges into "
            f"'{self._base_branch}'. What should happen? "
            f"(Change any session's merge branch later via Ctrl-G → session → Change a session's merge branch.)",
            options,
        )
        if not choice or choice.startswith("Do nothing"):
            self._set_message(
                f"Sessions keep merging into their own branches (the repo directory is on '{dir_branch}').",
                seconds=6.0,
            )
            self._render()
            return
        if choice.startswith("Switch all idle"):
            self._retarget_all_sessions(dir_branch)
            return
        self._retarget_active_session(dir_branch)  # switch only the active session

    def _prompt_merge_target_if_diverged(self) -> None:
        # Used on a SESSION SWITCH: when the newly-active session merges into a branch
        # other than the directory's, ask whether to keep its own branch (default) or
        # switch it to the directory's.
        if self.worktree is None or self._base_branch is None or self._repo_dir_branch is None:
            return
        if self._base_branch == self._repo_dir_branch:
            return
        if self.agent_in_flight:
            return  # a running session's branch can't change mid-run; the status bar bolds the difference
        choice = self._select_popup(
            f"The repo directory is on '{self._repo_dir_branch}', but session '{self.name}' merges into "
            f"'{self._base_branch}'. Where should this session's changes merge?",
            [
                f"Keep merging into '{self._base_branch}'",
                f"Switch to '{self._repo_dir_branch}' (the current directory branch)",
            ],
        )
        if choice and choice.startswith("Switch to '"):
            self._retarget_active_session(self._repo_dir_branch)
        else:
            self._set_message(
                f"'{self.name}' keeps merging into '{self._base_branch}' "
                f"(the repo directory is on '{self._repo_dir_branch}').",
                seconds=6.0,
            )
            self._render()

    def _retarget_all_sessions(self, target: str) -> None:
        # Re-point every IDLE session at `target` (used by "switch all idle sessions").
        # A running session keeps the branch it started its turn on — its in-flight work
        # must not split across branches — so it is left untouched and reported.
        skipped: list[str] = []
        for index in range(len(self.sessions)):
            session = self.sessions[index]
            if getattr(session, "agent_in_flight", False):
                skipped.append(self._session_name(index))
                continue
            if session is self.active:
                self._retarget_active_session(target)  # active, in place
            else:
                self._with_session(session, lambda: self._retarget_active_session(target))
        if skipped:
            self._set_message(
                f"Kept {', '.join(skipped)} on their current branch — they're running a turn; "
                f"re-point them once idle via Ctrl-G → session → Change a session's merge branch.",
                seconds=10.0,
            )
            self._render()

    def _reconcile_merge_branch(self, default_base: str | None) -> None:
        # On startup/resume aGiTrack assumes a session merges into the directory's current
        # branch (`default_base`). If a PRIOR run assigned this worktree a different merge
        # branch (persisted in its state), honor that assignment instead and flag a
        # confirmation prompt, so the user confirms the branch change before it takes
        # effect rather than silently merging into a branch they didn't choose. Otherwise
        # record `default_base` as this worktree's merge branch.
        if self.state is None:
            return
        previous = self.state.merge_branch  # what the previous aGiTrack instance assigned
        if previous and previous != default_base and self._branch_exists(previous):
            self._base_branch = previous  # keep merging where the prior run was pointed
            self._pending_merge_prompt = True  # confirm with the user once the TUI is ready
        elif default_base is not None:
            # No prior assignment, it already matches, or its branch is gone — record the
            # directory's current branch as this worktree's merge branch.
            self.state.merge_branch = default_base

    def _branch_exists(self, branch: str) -> bool:
        # Whether `branch` is a real local branch right now. On any error assume it does,
        # so a prior merge-branch assignment is never silently dropped on a transient
        # failure — only when the branch is provably gone.
        try:
            return branch in self.base_repo.list_branches()
        except Exception:
            return True

    def _retarget_active_session(self, target: str) -> bool:
        # Change the ACTIVE session's merge destination to `target` — per-session, so
        # the other sessions and the repo directory are left untouched. Flush its
        # pending work into the OLD target first, then re-point its worktree there.
        if self.worktree is None or self._base_branch is None:
            return False
        if target == self._base_branch:
            return True
        if self.agent_in_flight:
            # Never change a running session's merge branch mid-turn — its in-flight
            # work would split across branches. Only an idle session can be re-pointed.
            self._set_message(
                f"'{self.name}' is running a turn — its merge branch can only be changed when idle. "
                f"This run will merge into '{self._base_branch}'.",
                seconds=10.0,
            )
            self._render()
            return False
        old = self._base_branch
        self._exiting = True  # suppress the conflict-resolve popup during the flush
        self._set_message(f"Integrating '{self.name}' into '{old}' before re-targeting…", seconds=30)
        self._render()
        self._integrate_session_keeping_alive()
        if self._integration.session_unintegrated(self.repo):
            self._exiting = False
            self._set_message(
                f"Can't change '{self.name}' merge target: it has unresolved work in '{old}'. "
                f"Resolve it first ({self._menu_label()} → session).",
                seconds=12.0,
            )
            self._render()
            return False
        self._base_branch = target  # the active session's new merge target (syncs the service)
        if self.state is not None:
            self.state.merge_branch = target  # keep the worktree's recorded merge branch in step
        self._repoint_current_to_base()
        self._exiting = False
        self._set_message(f"'{self.name}' now merges into '{target}'.", seconds=8.0)
        self._render()
        return True

    def _warn_if_base_edited(self) -> None:
        # Fallback for un-sandboxed platforms: detect the agent editing the base
        # repo (its working tree gaining uncommitted changes beyond the startup
        # baseline) and warn, since those edits bypass aGiTrack's worktree tracking.
        if not self._monitor_base_edits:
            return
        now = time.monotonic()
        if now - self._base_check_at < self.BASE_EDIT_CHECK_SECONDS:
            return
        self._base_check_at = now
        try:
            current = set(self.base_repo.status_short().splitlines())
        except Exception:
            return
        new = current - self._base_status_baseline
        if new:
            files = ", ".join(sorted(line[3:] for line in list(new) if len(line) > 3)[:5])
            self._set_message(
                f"Agent edited the base repo, outside its worktree ({files}). These "
                "changes are not tracked by aGiTrack — move them into the worktree.",
                seconds=12.0,
            )
            self._base_status_baseline = current  # don't repeat for the same files
            self._render()

    @staticmethod
    def _newest_mtime(paths) -> float:
        """Newest modification time across *paths* (missing entries ignored).

        A cheap change signal: a single ``stat`` per path, no work-tree walk, so a
        git subprocess only runs when the underlying ref/HEAD file actually moved.
        Returns 0.0 when nothing is present.
        """
        newest = 0.0
        for path in paths:
            try:
                newest = max(newest, os.stat(path).st_mtime)
            except OSError:
                continue
        return newest

    def _base_git_dir(self):
        """The repo dir's ``.git`` directory as a Path, or None when it can't be
        resolved (e.g. a stubbed base repo in tests). Callers that can't resolve it
        skip mtime-gating and fall back to reading git directly."""
        repo = getattr(self.base_repo, "repo", None)
        try:
            return repo / ".git" if repo is not None else None
        except TypeError:
            return None

    def _poll_base_advanced(self) -> None:
        # aGiTrack advances the base itself (integration sets `_base_advanced`), but the
        # base branch can also gain commits out of band — the user commits directly
        # to it, pulls, rebases, etc. Poll its HEAD on a throttle and, when it moves
        # for any reason, flag a sync so idle worktrees pick the new commits up
        # (`_sync_idle_worktrees_to_base`). The first observation only records the
        # baseline; it never triggers on startup.
        if self.worktree is None or self._base_branch is None:
            return
        now = time.monotonic()
        if now - self._base_poll_at < self.BASE_POLL_SECONDS:
            return
        self._base_poll_at = now
        # Cheap pre-check: only shell out to `git rev-parse` when the base branch's
        # ref (loose file or packed-refs) was actually rewritten since the last poll.
        git_dir = self._base_git_dir()
        if git_dir is not None:
            sig = self._newest_mtime([git_dir / "refs" / "heads" / self._base_branch, git_dir / "packed-refs"])
            if sig == self._base_ref_mtime:
                self._prune_user_declined()  # keep the status-line count current
                return
            self._base_ref_mtime = sig
        try:
            head = self.base_repo.rev_parse(self._base_branch)
        except Exception as error:
            self._debug(f"base-head poll failed: {error!r}")
            return
        if self._last_base_head is not None and head != self._last_base_head:
            self._base_advanced = True
        self._last_base_head = head
        self._prune_user_declined()  # keep the status-line count current

    def _warn_if_cwd_drifted(self) -> None:
        # `claude --resume` can restore a session's *saved* working directory and
        # ignore the worktree aGiTrack launched it in (Claude Code issue #58591). When
        # that happens the agent works in the wrong directory: its turns aren't
        # tracked here and writes outside the worktree are sandbox-blocked. Detect
        # it from the cwd the backend records, and warn once with how to recover.
        #
        # Only the cwd of a turn recorded *after* this launch counts (`since`): a
        # resume — especially of an imported/shared session — leaves a stale cwd in
        # the transcript that points elsewhere (the base repo, another machine's
        # path) but is harmless, because the next real turn runs in the worktree.
        # Without the time gate that stale value latched a confusing false warning
        # before the agent had done anything (#72).
        if self._cwd_drift_checked:
            return
        if self.worktree is None:
            return
        now = time.monotonic()
        if now - self._cwd_check_at < self.CWD_CHECK_SECONDS:
            return
        self._cwd_check_at = now
        fn = getattr(self.backend, "recorded_working_dir", None)
        if fn is None:
            self._cwd_drift_checked = True  # backend doesn't record a cwd
            return
        try:
            recorded = fn(self.state.backend_session_id, since=self._cwd_launch_at or None)
        except Exception as error:
            self._debug(f"cwd drift check failed: {error!r}")
            return
        if not recorded:
            return  # no post-launch turn recorded yet — check again next tick
        self._cwd_drift_checked = True
        if os.path.realpath(recorded) == os.path.realpath(str(self.repo.repo)):
            return  # on the worktree, as intended
        self._debug(f"cwd drift: backend recorded {recorded}, worktree is {self.repo.repo}")
        self._set_message(
            f"⚠ The agent ran a turn in:\n    {recorded}\n"
            f"not this session's worktree:\n    {self.repo.repo}\n"
            "This is Claude's resume-cwd bug (#58591): turns made there are NOT committed "
            "by aGiTrack, and edits outside the worktree are blocked by the sandbox.\n"
            f"To recover: {self._menu_label()} → session → start a NEW session (it launches fresh in the "
            "worktree) and re-send your request there; resuming this conversation will keep "
            "landing in the wrong directory. Any work already done in the other directory "
            "stays there — move it into the worktree by hand if you need it tracked.",
            seconds=30.0,
        )
        self._render()

    # ------------------------------------------------------------------
    # Self-update (#: check periodically, apply once sessions are finished)
    # ------------------------------------------------------------------

    def _update_checks_enabled(self) -> bool:
        gc = getattr(self, "global_config", None)
        return bool(getattr(gc, "check_for_updates", True)) if gc is not None else False

    def _manual_update_pending(self) -> bool:
        # True when a previous automatic update failed (or wasn't retried). The user
        # is reminded once at startup, so the periodic in-session notice is held back.
        gc = getattr(self, "global_config", None)
        return bool(getattr(gc, "pending_manual_update", None)) if gc is not None else False

    def _merge_session_active(self) -> bool:
        # True while a merge / conflict resolution is in progress in ANY session
        # (the active one, or a background session integrating its turn). Update
        # prompts and updates are suppressed for the duration so they never
        # interrupt a merge the user is in the middle of.
        if getattr(self, "merge_ctx", None) is not None:
            return True
        return any(getattr(session, "merge_ctx", None) is not None for session in getattr(self, "sessions", []))

    def _maybe_check_for_update(self) -> None:
        # Kick off a background self-update check on a throttle, and surface a
        # finished one. Network I/O (`git fetch`) runs on a worker thread so the
        # terminal never stalls; the result is handed back and consumed here on
        # the main thread.
        if self._merge_session_active():
            # Don't prompt mid-merge; a result that lands now stays pending and
            # is surfaced once the merge is done.
            return
        self._consume_update_check_result()
        if self._updater is None or not self._update_checks_enabled():
            return
        if self._update_pending or self._update_applying:
            return  # already decided / in progress — stop nagging
        if self._update_check_thread is not None and self._update_check_thread.is_alive():
            return
        now = time.monotonic()
        if self._update_check_at and now - self._update_check_at < self.UPDATE_CHECK_SECONDS:
            return
        self._update_check_at = now
        updater = self._updater

        def worker() -> None:
            try:
                self._update_worker_result = updater.check()
            except Exception as error:  # never let a check crash the worker
                self._debug(f"update check failed: {error!r}")
                self._update_worker_result = None

        self._update_worker_result = None
        self._update_check_thread = threading.Thread(target=worker, daemon=True, name="agit-update-check")
        self._update_check_thread.start()

    def _consume_update_check_result(self) -> None:
        thread = self._update_check_thread
        if thread is None or thread.is_alive():
            return
        result = self._update_worker_result
        self._update_check_thread = None
        self._update_worker_result = None
        if result is None or not result.ok:
            return
        self._update_status = result
        if result.available and not self._update_offered and not self._manual_update_pending():
            # First time we have seen this update: prompt the user (a status-bar
            # notice pointing at the `update` command, so we don't seize the
            # screen mid-keystroke). Suppressed when a manual update is already
            # pending (a prior automatic attempt failed) — that user is reminded
            # once at startup instead of being nagged here every check.
            self._update_offered = True
            self._set_message(
                f"{result.message}\n{self._menu_label()} → 'update' to install it when your sessions finish.",
                seconds=12.0,
            )
            self._render()

    def _ready_for_update(self) -> bool:
        # "All sessions finished and commits are in": nothing is mid-turn,
        # mid-parse, mid-merge, or mid-summary anywhere. The actual commit +
        # integration of finished work is flushed by _finalize_pending_work()
        # right before the update is applied.
        if self._merge_session_active():
            return False
        if getattr(self, "agent_in_flight", False):
            return False
        if getattr(self, "agent_parse_active", False):
            return False
        if getattr(self, "pending_forwarded", None) or getattr(self, "pending_prompt_text", ""):
            return False
        if getattr(self, "_summary_pending", None) is not None:
            return False
        summary_thread = getattr(self, "_summary_thread", None)
        if summary_thread is not None and summary_thread.is_alive():
            return False
        if self._running_background_session_names():
            return False
        return True

    def _maybe_apply_pending_update(self) -> None:
        if not self._update_pending or self._update_applying:
            return
        if not self._ready_for_update():
            return
        self._apply_update_and_restart()

    def _apply_update_and_restart(self) -> None:
        # Install the update, then commit + integrate every session's finished work
        # (same path as exit) and ask run()'s teardown to re-exec aGiTrack.
        #
        # Order matters: apply the update FIRST, while the session is still fully
        # intact. If it fails, the user is left exactly where they were — nothing
        # torn down — and can keep working or retry. Doing the exit-finalize first
        # (which removes the worktree and terminates the backend) and only THEN
        # discovering apply() failed left the reactor running against a deleted
        # worktree, so the next `git status` crashed with FileNotFoundError.
        self._update_applying = True
        self._update_pending = False
        # When the code on disk is already current and only the running process is
        # stale, there is nothing to install — just finish work and re-exec. (Don't
        # run apply(): it would fetch/merge or pip-upgrade needlessly, and a dirty
        # source checkout would even block a pure restart.)
        restart_only = self._update_status is not None and self._update_status.restart_only
        if restart_only:
            message = "Restarting aGiTrack to load the updated code…"
        else:
            self._set_message("Updating aGiTrack…", seconds=30.0)
            self._render()
            result = self._updater.apply()
            if not result.ok:
                self._update_applying = False
                # Remember so the next startup reminds the user (once) instead of the
                # in-session notice re-appearing every check; the message already
                # carries manual-update instructions. Session untouched — keep running.
                if self.global_config is not None:
                    target = (self._update_status.latest if self._update_status else "") or "available"
                    self.global_config.pending_manual_update = target
                self._update_offered = True  # stop the periodic notice this session
                manual = ""
                if self._updater is not None:
                    try:
                        manual = f" {self._updater.manual_update_instructions()}"
                    except Exception:  # instructions are best-effort, never block the message
                        manual = ""
                self._set_message(f"aGiTrack update failed: {result.error}.{manual}", seconds=15.0)
                self._render()
                return  # session untouched — keep running
            message = f"{result.message} Restarting aGiTrack…"
        # Update is in place (or unnecessary): now finish commits and tear down for
        # the re-exec.
        self._set_message("Finishing commits, then restarting aGiTrack…", seconds=30.0)
        self._render()
        try:
            self._finalize_pending_work()
        except Exception as error:  # don't let a commit hiccup strand the update
            self._debug(f"finalize before update restart failed: {error!r}")
        # Stop the loop and let run()'s finally restore the terminal and release the
        # lock before _pending_restart triggers the re-exec.
        self._set_message(message, seconds=10.0)
        self._render()
        self._exit_child()
        self._pending_restart = True
        self.running = False

    def _restart_now(self, message: str) -> None:
        """Finish pending work and re-exec aGiTrack (the same teardown a self-update uses) so
        launch-time settings — worktrees on/off, the default backend, timings — take effect.
        run()'s teardown sees _pending_restart and performs the re-exec after restoring the
        terminal and releasing the lock."""
        self._set_message("Finishing commits, then restarting aGiTrack…", seconds=30.0)
        self._render()
        try:
            self._finalize_pending_work()
        except Exception as error:  # don't let a commit hiccup strand the restart
            self._debug(f"finalize before settings restart failed: {error!r}")
        self._set_message(message, seconds=10.0)
        self._render()
        self._exit_child()
        self._pending_restart = True
        self.running = False

    def _handle_update_command(self) -> None:
        # Ctrl-G → "update": show the current update status and let the user opt
        # in (applied once sessions finish), postpone, or stop update checks.
        if self._update_applying:
            self._set_message("An aGiTrack update is already in progress.")
            self._render()
            return
        # Run a FRESH check on explicit request. The cached `_update_status` comes
        # from the periodic background check (up to UPDATE_CHECK_SECONDS old), so it
        # can miss a remote push or a local-disk update that landed since — which
        # showed "up to date" even though a newer version already existed. A live
        # check (compares running vs local HEAD vs remote) reflects reality now.
        if self._updater is not None:
            self._set_message("Checking for aGiTrack updates…")
            self._render()
            try:
                self._update_status = self._updater.check()
            except Exception as error:  # network/git hiccup: fall back to cached status
                self._debug(f"on-demand update check failed: {error!r}")
        status = self._update_status
        if status is None:
            # No completed check yet — make sure one is running and ask the user
            # to retry, rather than blocking the UI on a network fetch.
            self._update_check_at = 0.0
            self._maybe_check_for_update()
            self._set_message("Checking for aGiTrack updates… run 'update' again in a moment.")
            self._render()
            return
        if not status.ok:
            self._set_message(f"Update check failed: {status.error}")
            self._render()
            return
        if not status.available:
            self._set_message(f"aGiTrack is up to date ({status.current or 'current'}).")
            self._render()
            return
        choice = self._select_popup(
            status.message,
            ["Update when sessions finish", "Not now", "Stop checking for updates"],
        )
        if choice == "Stop checking for updates":
            if self.global_config is not None:
                self.global_config.check_for_updates = False
            self._set_message("aGiTrack will no longer check for updates.")
            self._render()
            return
        if choice != "Update when sessions finish":
            self._set_message("Update postponed.")
            self._render()
            return
        self._update_pending = True
        if self._ready_for_update():
            self._apply_update_and_restart()
        else:
            self._set_message(
                "aGiTrack will update and restart once all sessions finish and commits are in.",
                seconds=8.0,
            )
            self._render()

    def _backend_update_command(self) -> list[str] | None:
        getter = getattr(self.backend, "update_command", None)
        if not callable(getter):
            return None
        try:
            cmd = getter()
        except Exception as error:
            self._debug(f"backend update_command failed: {error!r}")
            return None
        return list(cmd) if cmd else None

    def _backend_update_via_agitrack(self) -> bool:
        """Whether aGiTrack should drive this backend's update itself (from its UNCONFINED proxy)
        rather than leaving it to the backend's own updater. True for a Homebrew-managed CLI on
        macOS — independent of the sandbox toggle:
          - sandboxed: the backend's own `brew upgrade` can't run at all (macOS forbids nesting
            `sandbox-exec`), so aGiTrack MUST do it;
          - unsandboxed: it *would* work, but the backend only nags with a prompt (and OpenCode's,
            run interactively, has been failing for users), so aGiTrack does it silently instead.
        (npm/native installs self-update cleanly; aGiTrack leaves those to the backend.)"""
        if sys.platform != "darwin":
            return False
        exe = shutil.which(self.backend.name)
        if not exe:
            return False
        real = os.path.realpath(exe)
        # Homebrew kegs resolve under …/Cellar/…; the prefix is /opt/homebrew (Apple Silicon)
        # or /usr/local (Intel). Matching either covers both layouts.
        return "/Cellar/" in real or real.startswith("/opt/homebrew/") or real.startswith("/usr/local/")

    def _backend_child_env(self) -> dict[str, str] | None:
        """Extra environment for the interactive backend child only. When aGiTrack will apply
        this backend's update itself (it's a brew-managed backend on macOS and update checks are
        on), disable OpenCode's in-app auto-update so the user isn't shown its prompt — which is
        redundant with aGiTrack's silent update and, under the sandbox, fails outright. Does NOT
        affect aGiTrack's own `opencode upgrade`, which runs from the unconfined proxy with the
        normal environment."""
        checks_on = self.global_config is None or getattr(self.global_config, "check_for_updates", True)
        if getattr(self.backend, "name", None) == "opencode" and checks_on and self._backend_update_via_agitrack():
            return {"OPENCODE_DISABLE_AUTOUPDATE": "1"}
        return None

    def _backend_version(self) -> str:
        """The backend CLI's reported version string, used to detect whether an update actually
        landed. Empty when it can't be read."""
        try:
            proc = subprocess.run([self.backend.name, "--version"], capture_output=True, text=True, timeout=20)
        except Exception as error:
            self._debug(f"backend version check failed: {error!r}")
            return ""
        return (proc.stdout or proc.stderr or "").strip()

    def _maybe_auto_update_backend(self) -> None:
        """Timers phase: when aGiTrack should drive this backend's update (a brew-managed CLI on
        macOS — see _backend_update_via_agitrack), apply it AUTOMATICALLY from the UNCONFINED
        proxy — no menu, no prompt, regardless of the sandbox toggle. Evaluated once per backend
        (re-armed on a switch) and gated by the global update-check toggle. The updater itself
        decides whether an upgrade is needed (it checks the backend's release server, not
        Homebrew's possibly-stale local tap), so we don't pre-gate on `brew outdated`. Runs on a
        background thread; the result is surfaced by _service_backend_update."""
        name = getattr(self.backend, "name", None)
        if name is None or self._backend_update_checked_for == name:
            return
        self._backend_update_checked_for = name  # evaluate each backend once (re-armed on switch)
        if self.global_config is not None and not getattr(self.global_config, "check_for_updates", True):
            return  # the user turned update checks off
        if not self._backend_update_via_agitrack():
            return  # the backend self-updates cleanly on its own; leave it to do so
        if self._backend_update_thread is not None and self._backend_update_thread.is_alive():
            return
        cmd = self._backend_update_command()
        if cmd is None:
            return
        thread = threading.Thread(
            target=self._auto_update_backend_worker, args=(name, cmd), daemon=True, name="agit-backend-update"
        )
        self._backend_update_thread = thread
        thread.start()

    def _auto_update_backend_worker(self, name: str, cmd: list[str]) -> None:
        # Background: run the backend's own updater UNCONFINED so a package-manager updater
        # (notably Homebrew's own sandbox-exec) isn't nested inside the agent's macOS sandbox —
        # the very nesting macOS forbids, which is what breaks the in-backend self-update. The
        # updater no-ops fast when already current, so we don't pre-check; we compare the CLI
        # version before and after to report only a real change (and to catch a silent failure).
        before = self._backend_version()
        self._set_message(
            f"Checking {name} for updates in the background "
            f"(applying any — its own updater can't, inside aGiTrack's sandbox)…",
            seconds=600.0,
            sticky=True,
        )
        self._render()
        cwd = str(getattr(self.base_repo, "repo", None) or getattr(self.repo, "repo", "."))
        result: dict = {"name": name, "before": before}
        try:
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
            result["code"] = proc.returncode
            result["output"] = (proc.stdout or "") + (proc.stderr or "")
        except Exception as error:
            result["error"] = repr(error)
        result["after"] = self._backend_version()
        self._backend_update_result = result

    def _service_backend_update(self) -> None:
        """Main loop: surface a finished backend auto-update result (set by its worker thread)."""
        result = self._backend_update_result
        if result is None:
            return
        self._backend_update_result = None
        name = result.get("name", "the agent")
        if "error" in result:
            self._set_message(f"Updating {name} failed: {result['error']}", seconds=12.0)
            self._render()
            return
        before, after = result.get("before", ""), result.get("after", "")
        output = _strip_ansi(result.get("output", ""))
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if after and before and after != before:
            # The version actually changed — the update landed. (Authoritative, unlike the
            # updater's exit code, which `opencode upgrade` reports as 0 even when it failed.)
            self._set_message(f"{name} updated ({before} → {after}). Start a new session to use it.", seconds=12.0)
        elif result.get("code") not in (0, None) or "fail" in output.lower() or "error" in output.lower():
            flagged = [line for line in lines if "fail" in line.lower() or "error" in line.lower()]
            detail = (flagged[-1] if flagged else (lines[-1] if lines else ""))[:200]
            self._set_message(f"Updating {name} may have failed: {detail}", seconds=14.0)
        else:
            self._set_message(f"{name} is already up to date.", seconds=6.0)
        self._render()

    def _confine_to_worktree(self, command: list[str]) -> list[str]:
        # Wrap the backend so it can only write inside its session worktree (plus
        # the repo's .git), not the base repo it lives in. A no-op when there is
        # no worktree, or when confinement is disabled / unavailable (the loop
        # then warns if the base working tree is touched).
        if self.worktree is None:
            return command
        if not self._sandbox:
            return command
        base = self.base_repo
        if base is None:
            return command
        return sandbox.wrap_command(
            command,
            base=str(base.repo),
            worktree=str(self.repo.repo),
            allowed_paths=self._allowed_edit_paths,
        )

    def _should_continue_session(self) -> bool:
        session_id = self.state.backend_session_id
        if not session_id:
            return False
        if self.state.backend_session_matches_repo():
            return True
        return self.backend.session_belongs_to_repo(self.repo.repo, session_id)

    def _teardown_child(self) -> None:
        # Dispatch through the bound method (not BackendProcess.cleanup directly)
        # so subclass/test overrides of _cleanup_child keep applying to teardown.
        self._cleanup_child()
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        self.child_pid = None

    def _reset_agent_tracking(self) -> None:
        self.agent_in_flight = False
        self.agent_parse_thread = None
        self.agent_parse_result = None
        self.agent_parse_active = False
        self.pending_forwarded = None
        self.pending_prompt_text = ""
        self.passthrough_prompt.clear()
        self.passthrough_escape = None
        self.last_status = ""
        self.parse_pending = False
        self.status_check_pending = False
        # Re-detect mouse ownership for the new backend: OpenCode enables mouse
        # (so wheel events are forwarded to it), Claude does not (so aGiTrack keeps
        # the wheel for scrollback). Without this reset, switching OpenCode→Claude
        # would leave child_mouse stuck True and break Claude's scrollback.
        self.child_mouse = False
        self.scroll_back = 0

    def _restart_agent(self, message: str) -> None:
        # Tear down the running TUI and relaunch it for the current backend and
        # session state, re-baselining so existing history is not re-committed.
        self._teardown_child()
        self._reset_agent_tracking()
        self._sanitize_state_trace()
        self._initialize_session_baseline()
        self._init_screen()
        self._spawn()
        self._resize_child()
        # Re-assert host mouse reporting for the new backend so wheel scrollback
        # keeps working regardless of what the previous backend left behind.
        self._enable_host_mouse()
        self._set_message(message)
        self._render()

    def _live_session_for_backend(self, name: str) -> int | None:
        # Index of a live session currently running the given backend (the active
        # session's live backend is on self; background ones on their snapshot).
        for index in range(len(self.sessions)):
            backend = self.backend if index == self.active_index else getattr(self.sessions[index], "backend", None)
            if getattr(backend, "name", None) == name:
                return index
        return None

    def _switch_backend(self, name: str) -> None:
        if not backend_installed(name):
            self._set_message(f"'{name}' is not installed.\n{install_hint(name)}", seconds=8.0)
            self._render()
            return
        if name == getattr(self.backend, "name", None):
            self._set_message(f"Already using {name}.")
            self._render()
            return
        # Re-arm the automatic self-update check so it re-evaluates for the backend being
        # switched to (a sandbox-blocked update is then applied on switch too).
        self._backend_update_checked_for = None
        # Remember the switch for THIS repo only (repo-scoped), not globally: switching to a
        # backend in one repo (e.g. to try it) must not change the user's global default for
        # every other repo. The repo overlay value drives this repo's backend on next launch.
        self.global_config.set("default_backend", name, scope="repo")
        if self.worktree is None:
            # A non-worktree session has nothing to multiplex; restart the single
            # backend in place (legacy behaviour).
            self.state.remember_backend_session()
            self.state.backend = name
            self.backend = make_proxy_agent(name)
            self.state.backend_session_id = self.state.stored_backend_session(name)
            self.state.last_backend_message_id = None
            self.state.clear_trace()
            self._restart_agent(f"Backend set to {name}")
            return
        # Keep the current backend's session running in the background and switch
        # to this backend's own session.
        index = self._live_session_for_backend(name)
        if index is not None:
            self._switch_active(index)
            return
        # Resume this backend's last conversation if we remember one (recreating
        # its worktree at the same path so the backend finds its transcript).
        record = self._recall_backend_session(name)
        if record and record.get("id"):
            self._new_session(
                record.get("worktree") or self._next_session_name(), backend=name, resume_session_id=record["id"]
            )
            return
        # Otherwise start fresh, confirming the session name first.
        session_name = self._prompt_session_name(f"New {name} session", default=self._next_session_name())
        if session_name is None:
            return
        self._new_session(session_name, backend=name)

    def _persisted_session_names(self) -> set[str]:
        # Sanitized names recorded for past conversations — both the durable
        # repo-root record and the worktree directory names the backend still
        # knows. A new session must not reuse one of these: resuming that past
        # conversation later recreates its worktree at worktree_path(name), so a
        # collision would put two backends in one directory.
        return {_sanitize_name(name) for name in self._agitrack_named_sessions().values() if name}

    def _taken_session_names(self) -> set[str]:
        # Sanitized names already in use: live sessions, on-disk worktrees, and
        # names already claimed by a past (named/dormant) conversation.
        used: set[str] = set()
        try:
            used.update(_sanitize_name(info.name) for info in self._worktrees().list())
        except Exception:
            pass
        used.update(_sanitize_name(self._session_name(index)) for index in range(len(self.sessions)))
        used.update(self._persisted_session_names())
        return used

    def _next_session_name(self) -> str:
        # A friendly random word (neutral/harmless), avoiding existing worktrees and
        # live sessions. A word is easier to remember than ``session-N`` and, since
        # sessions are tracked/shared as ``<github-id(s)>/<name>``, far less likely to
        # clash with another contributor's session names. The user can still rename it
        # or type their own at the new-session prompt.
        from agitrack.proxy.session_names import random_session_name

        return random_session_name(self._taken_session_names())

    def _session_name_taken(self, name: str) -> bool:
        return _sanitize_name(name) in self._taken_session_names()

    def _dedupe_session_name(self, base: str) -> str:
        # A collision-free local name derived from ``base`` (e.g. a shared
        # session's "<sharer>-<name>"), so the resume prompt's default can be
        # accepted as-is even when the same session was imported before.
        candidate = _sanitize_name(base)
        if not self._session_name_taken(candidate):
            return candidate
        suffix = 2
        while self._session_name_taken(f"{candidate}-{suffix}"):
            suffix += 1
        return f"{candidate}-{suffix}"

    def _prompt_session_name(self, title: str, *, default: str) -> str | None:
        # Ask for a session name, rejecting duplicates (a session and its worktree
        # are 1:1, so names must be unique). Returns the chosen name, or None on
        # cancel / empty input.
        prompt = "Name for the new session (its own git worktree):"
        while True:
            name = self._prompt_popup(title, prompt, default=default)
            if name is None or not name.strip():
                return None
            name = name.strip()
            if self._session_name_taken(name):
                prompt = f"'{name}' is already in use. Choose a different name:"
                default = self._next_session_name()
                continue
            return name

    def _recall_backend_session(self, backend: str) -> dict | None:
        try:
            root = AgitrackState(self.base_repo.repo, default_backend=self.global_config.default_backend)
            return root.recall_session(backend)
        except Exception as error:
            self._debug(f"recall backend session failed: {error!r}")
            return None

    def _remember_session_for_backend(self) -> None:
        # Persist the current session's conversation under its backend in the
        # durable repo-root state, so switching back to that backend later resumes
        # it (its worktree is recreated at the same path).
        info = self.worktree
        if info is None:
            return
        try:
            root = AgitrackState(self.base_repo.repo, default_backend=self.global_config.default_backend)
            root.remember_session(
                self.state.backend,
                session_id=self.state.backend_session_id,
                worktree=info.name,
                message_id=self.state.last_backend_message_id,
                model=self.state.model,
            )
            # Remember a user-given name (not an auto session-N) keyed by the
            # backend conversation id, so resuming it later restores the name.
            if info.name and not self._AUTO_NAME_RE.match(info.name):
                root.name_session(self.state.backend_session_id, info.name)
        except Exception as error:
            self._debug(f"remember backend session failed: {error!r}")

    def _persist_session_name(self, session_id: str | None) -> None:
        # Link this session's user-given name to its backend conversation id in
        # the durable repo-root record as soon as the id is known — and again
        # whenever the backend forks a new id — not only on clean exit. Waiting
        # for exit strands the name under a stale id (or never records it) when
        # the worktree is kept, aGiTrack crashes, or the conversation id drifts
        # across resumes, leaving the session unnamed in the resume list.
        name = self.name
        if not session_id or not name or self._AUTO_NAME_RE.match(name):
            return
        try:
            root = AgitrackState(self.base_repo.repo, default_backend=self.global_config.default_backend)
            if root.session_name_for(session_id) != name:
                root.name_session(session_id, name)
        except Exception as error:
            self._debug(f"persist session name failed: {error!r}")

    # --- live-session multiplexer ---

    def _worktrees(self) -> WorktreeManager:
        if self.worktree_manager is None:
            self.worktree_manager = WorktreeManager(self.base_repo)
        return self.worktree_manager

    def _apply_new_session_if_requested(self) -> None:
        # `agitrack --new-session`: start a fresh backend conversation (don't resume)
        # and mint a new aGiTrack session id.
        if not self._force_new_session:
            return
        self.state.backend_session_id = None
        self.state.last_backend_message_id = None
        self.state.new_agitrack_session_id()

    def _turn_from_branch(self, branch: str) -> int:
        return self._integration.turn_from_branch(branch)

    def _setup_base_merge_only_session(self) -> None:
        # Move the initial session into its own worktree so the base tree is only
        # ever advanced by integration. Reuses an existing worktree (resuming a
        # previous run) or creates a fresh one; falls back to running on the base
        # tree (legacy behaviour, no auto-integration) if neither is possible.
        if not self._use_worktrees:
            # #9: opt-out — run on the current branch directly (worktree stays
            # None; all the `worktree is None` paths commit straight to it).
            # Worktrees are off, so any session worktrees left by a previous worktree-mode run
            # are dead weight — remove them (their committed work was already integrated into
            # the base branch). Best-effort; a removal failure must not block startup.
            removed = 0
            try:
                manager = self._worktrees()
                for info in manager.list():
                    manager.remove(info.name)
                    removed += 1
            except Exception as error:
                self._debug(f"no-worktree cleanup failed: {error!r}")
            note = f" Removed {removed} leftover worktree(s)." if removed else ""
            self._set_message(
                "Running without a worktree: the agent edits this branch directly (visible live), "
                "but there's no isolation or auto-integration. Extra sessions started this way share "
                "this directory and edit the same files at once." + note,
                seconds=12.0,
            )
            return
        root_state = self.state  # the durable repo-root "last session" record
        backend_name = root_state.backend
        prior_message_id = root_state.last_backend_message_id
        prior_model = root_state.model
        prior_worktree = (root_state.recall_session(backend_name) or {}).get("worktree")
        # Which conversation to continue at startup: aGiTrack's own last session if we
        # have one, otherwise the repo's most recent backend conversation (e.g. one
        # you ran with plain claude/opencode before aGiTrack). Resume is by id, which
        # the backend resolves regardless of which directory it runs in.
        if self._force_new_session:
            resume_id = None
        else:
            resume_id = root_state.backend_session_id or self._repo_latest_session_id()
        name = self._resolve_startup_session_name(root_state, resume_id, prior_worktree)
        try:
            info, repo = self._open_session_worktree(name)
        except Exception as error:
            self._debug(f"base-merge-only setup failed; running on the base tree: {error!r}")
            return
        self.name = info.name
        self._primary_worktree_name = info.name  # the session that auto-resumes across exits
        self.worktree = info
        self.repo = repo
        # Load this worktree's state and reconcile its merge branch BEFORE aligning: a
        # prior run's assignment must be honored (and the cross-branch guard compares the
        # align target against it), with a confirmation prompt deferred to the TUI.
        self.state = AgitrackState(info.path, default_backend=self.global_config.default_backend)
        self._reconcile_merge_branch(self._base_branch)
        self._align_session_to_base(repo)
        self.turn = self._turn_from_branch(repo.current_branch())
        self.state.backend = backend_name
        # The worktree is recreated fresh each run (its working state is not kept),
        # so seed its resume pointer to continue the chosen conversation.
        if not self.state.backend_session_id and resume_id:
            if prior_model:
                self.state.model = prior_model
            self.state.last_backend_message_id = prior_message_id
            self.state.backend_session_id = resume_id  # setter records this worktree as its repo
        self.backend = make_proxy_agent(backend_name)
        self.actions = AgitrackActions(self.repo, self.state, verbose=self.verbose)

    _AUTO_NAME_RE = re.compile(r"^session-\d+$")

    def _repo_latest_session_id(self) -> str | None:
        # The conversation a bare `claude -c` / `opencode` would continue in the
        # repo aGiTrack was launched in (its most recent recorded session).
        try:
            return self.backend.latest_session_id(self.base_repo.repo)
        except Exception as error:
            self._debug(f"latest_session_id failed: {error!r}")
            return None

    def _resolve_startup_session_name(self, root_state, resume_id, prior_worktree) -> str:
        # Keep the conversation's existing (user-given) name if it has one; only
        # prompt when there is no real name yet. Auto `session-N` names don't count.
        # A fresh conversation (``--new-session``, so ``resume_id`` is None) always
        # prompts for its own name — it must NOT inherit the prior session's name,
        # since it is a brand-new conversation, not a continuation of that one.
        existing = root_state.session_name_for(resume_id)
        if not existing and resume_id and prior_worktree and not self._AUTO_NAME_RE.match(prior_worktree):
            # Resuming a conversation whose name only lived in the last-session
            # record; key it by the conversation id too so it stays linked once that
            # record moves on.
            existing = prior_worktree
            root_state.name_session(resume_id, existing)
        if existing:
            return existing
        name = self._prompt_startup_name(resume_id is not None)
        if resume_id:
            root_state.name_session(resume_id, name)
        return name

    def _startup_taken_names(self) -> set[str]:
        # Sanitized names already claimed when the primary session is set up
        # (self.sessions is not built yet): on-disk worktrees plus names recorded
        # for past (named/dormant) conversations, so a fresh session can't reuse
        # the name of one the user may resume later.
        try:
            used = {_sanitize_name(info.name) for info in self._worktrees().list()}
        except Exception:
            used = set()
        used |= self._persisted_session_names()
        return used

    def _startup_default_name(self) -> str:
        # A friendly random word for the very first session, mirroring
        # ``_next_session_name`` for in-session new sessions. ``session-N`` is
        # forgettable and, since sessions are tracked/shared as
        # ``<github-id(s)>/<name>``, two people each starting ``session-1`` collide;
        # a random word is easier to remember and far less likely to clash. The
        # startup taken-set is used because ``self.sessions`` isn't built yet. The
        # user can still type their own at the new-session prompt.
        from agitrack.proxy.session_names import random_session_name

        return random_session_name(self._startup_taken_names())

    def _prompt_startup_name(self, continuing: bool) -> str:
        # Pre-reactor, cooked mode: the alt-screen has not been entered yet so
        # the terminal is still in line-buffered cooked mode.  Using input()
        # here is intentional — the reactor loop is not running, so there are
        # no PTY fds to drain, and the simple cooked readline is the right tool.
        # Do NOT convert this to a modal; modals require the reactor to be live.
        default = self._startup_default_name()
        print("Continuing a conversation that has no name yet." if continuing else "Starting a new session.")
        taken = self._startup_taken_names()
        while True:
            try:
                raw = input(f"Name this session [{default}]: ").strip()
            except (EOFError, KeyboardInterrupt, OSError):
                # No interactive stdin (piped/scripted run, or a closed/uncaptured
                # fd): accept the default name rather than failing startup.
                return default
            name = raw or default
            if _sanitize_name(name) in taken:
                print(f"'{name}' is already in use. Choose a different name.")
                continue
            return name

    def _align_session_to_base(self, repo: GitRepo) -> None:
        # Bring a clean, idle session worktree up to date with the base after the
        # base branch gained commits (another session integrated, or a previous
        # run). Two cases, by whether the worktree has its own committed work:
        #   * No unintegrated commits → re-point (detach) it onto the current
        #     base, so it works from, and later integrates into, the branch the
        #     user launched from — not whatever branch an earlier run left current.
        #   * Has its own work → merge the new base commits into its turn branch so
        #     it stays current, but only when that merges cleanly; a conflicting
        #     base is backed out and left for the session's own integration to
        #     surface. (Direction base → worktree; the reverse is integration.)
        # A worktree mid-merge or with uncommitted changes is left untouched.
        if self._base_branch is None:
            return
        # Cross-branch safeguard: only ever merge a worktree's OWN recorded merge
        # branch into it. If the base we're about to merge differs from the branch this
        # worktree was set up for, refuse and warn loudly — one session's branch must
        # never be merged into another session's worktree (the demo-video→dev bug).
        own = self._worktree_merge_branch(repo)
        if own is not None and own != self._base_branch:
            self._set_message(
                f"⚠ Prevented a cross-branch merge: NOT merging '{self._base_branch}' into a session "
                f"worktree that merges into '{own}'. Sessions on different branches are kept separate.",
                seconds=15.0,
            )
            self._render()
            self._debug(f"cross-branch align blocked: base '{self._base_branch}' != worktree base '{own}'")
            return
        try:
            outcome = self._integration.align_session_to_base(repo)
        except Exception as error:
            self._debug(f"align to base failed: {error!r}")
            return
        if outcome.startswith("merged:"):
            branch = outcome[len("merged:") :]
            self._debug(f"merged base '{self._base_branch}' into session branch {branch}")
        elif outcome.startswith("conflict:"):
            branch = outcome[len("conflict:") :]
            self._debug(f"base '{self._base_branch}' conflicts with {branch}; left for integration")
        elif outcome == "repointed":
            self._debug(f"re-pointed session worktree to current base '{self._base_branch}'")

    def _worktree_merge_branch(self, repo: GitRepo) -> str | None:
        # The merge branch recorded in the worktree's OWN state file — read straight
        # from disk so it reflects THIS worktree regardless of which session is active.
        # None when unknown (e.g. a worktree created before this was tracked).
        try:
            return AgitrackState(repo.repo).merge_branch
        except Exception:
            return None

    def _worktree_has_pending_work(self, repo: GitRepo, branch: str) -> bool:
        # Pending = uncommitted changes, or commits on its branch not yet in base.
        return self._integration.worktree_has_pending_work(repo, branch)

    def _cleanup_stale_state_on_startup(self) -> None:
        # A non-graceful exit (the backend dying, SIGKILL, a crash) can leave junk
        # that makes the *next* run misbehave until a second restart — chiefly:
        #   • prunable git worktree registrations whose directories are gone, and
        #   • orphaned worktree *directories* that are no longer valid worktrees
        #     (e.g. only a `.agitrack/` recreated by a write after teardown).
        # git's own `worktree list` can't see the orphaned dirs, so sweep the
        # filesystem under the worktrees root and delete anything that isn't a real,
        # registered worktree. Valid dormant worktrees (kept for resume) are
        # registered, so they're left untouched. Runs before the primary session is
        # set up so startup is clean from the first launch, not the second.
        try:
            self.base_repo.worktree_prune()
        except Exception as error:
            self._debug(f"startup prune failed: {error!r}")
        try:
            worktrees = self._worktrees()
            root = worktrees.root
            if not root.is_dir():
                return
            registered = set()
            for info in worktrees.list():
                try:
                    registered.add(info.path.resolve())
                except OSError:
                    pass
            removed = 0
            for entry in sorted(root.iterdir()):
                if not entry.is_dir():
                    continue
                try:
                    resolved = entry.resolve()
                except OSError:
                    continue
                if resolved in registered or self._is_valid_worktree(entry):
                    continue  # a real worktree — keep it
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
                self._debug(f"startup cleanup: removed orphaned worktree dir {entry}")
            if removed:
                self.base_repo.worktree_prune()  # removing dirs may strand registrations
        except Exception as error:
            self._debug(f"startup cleanup failed: {error!r}")

    def _reconcile_sessions_on_startup(self) -> None:
        # Clean up worktrees left by previous runs: integrate any pending commits
        # into the base, then delete the worktree. The Claude conversation itself
        # persists (keyed by the worktree path) and stays resumable from the
        # session list, so nothing of value is lost. Worktrees whose work cannot
        # be merged cleanly (a conflict, or uncommitted changes) are kept and
        # flagged for the user to resolve.
        if self.worktree is None:
            return
        active_pending = False
        try:
            active_pending = self._worktree_has_pending_work(self.repo, self.repo.current_branch())
        except Exception:
            pass
        flagged: list[str] = []
        try:
            infos = self._worktrees().list()
        except Exception:
            infos = []
        for info in infos:
            if info.name == self.name:  # the active session, handled below
                continue
            try:
                if not self._cleanup_stale_worktree(info):
                    flagged.append(info.name)
            except Exception as error:
                self._debug(f"reconcile skipped '{info.name}': {error!r}")
        self._delete_orphan_merged_branches()
        notes: list[str] = []
        if active_pending:
            notes.append(f"this session ('{self.name}') has commits to integrate")
        if flagged:
            notes.append(f"{len(flagged)} stale session(s) need attention: {', '.join(flagged)}")
        if notes:
            if active_pending:
                instruction = (
                    f"Open {self._menu_label()} → session and choose "
                    "'✓ Integrate this session's commits' (or 'resolve' for a flagged session)."
                )
            else:
                instruction = f"Open {self._menu_label()} → session to resolve them."
            self._set_message("⚠ " + "; ".join(notes) + ".\n" + instruction, seconds=12.0)

    def _cleanup_stale_worktree(self, info) -> bool:
        # Integrate a dormant worktree's pending commits (if any) and delete it.
        # Returns False (keep + flag) when it has uncommitted changes or its work
        # conflicts with the base and so needs the user/agent to resolve.
        result = self._integration.cleanup_stale_worktree(info, self._worktrees())
        if result:
            self._debug(f"cleaned stale worktree '{info.name}'")
        return result

    def _delete_orphan_merged_branches(self) -> None:
        # Remove agit/* branches that no worktree checks out and that are already
        # contained in the base branch (stale leftovers).
        try:
            for branch in self._integration.delete_orphan_merged_branches():
                self._debug(f"deleted stale merged branch {branch}")
        except Exception as error:
            self._debug(f"orphan branch cleanup failed: {error!r}")

    def _integrate_session_turn(self) -> None:
        # Integrate the current session's just-committed turn branch into the
        # base branch: merge base into the turn branch (in the session's
        # worktree), then fast-forward the base to it and start the next turn
        # branch. When it cannot fast-forward (the base gained conflicting work
        # from another session), surface the resolve options box and let the
        # user choose how to handle it.
        if self._delay_merge and self.worktree is not None and not self._exiting:
            # --delay-merge: hold the merge so the user can review/edit first and
            # confirm it themselves. (Exit still finalizes via _integrate_session_on_exit
            # so committed work isn't stranded across runs.)
            self._notify_merge_deferred()
            return
        result = self._integrate_turn_or_conflict()
        if result == "conflict" and not self._exiting:
            # On exit there is no UI to drive a resolution; the work stays on its
            # branch for the next startup / session menu to surface.
            self._prompt_resolve_conflict(self.repo.current_branch())

    def _notify_merge_deferred(self) -> None:
        # Tell the user their turn was committed but not merged (only under
        # --delay-merge), naming the WORKING DIRECTORY so they know where to review —
        # crucial when it's a worktree, whose location they may not otherwise know.
        if not self._active_has_pending():
            return
        base = self._base_branch or "the base branch"
        self._set_message(
            f"Changes committed but not merged into {base} yet (--delay-merge). Review and edit them in "
            f"{self.repo.repo}, then merge via {self._menu_label()} → session → "
            f'"Merge reviewed changes into {base}".',
            seconds=30.0,
        )
        self._render()

    def _integrate_committed_turn_before_new_turn(self) -> None:
        # A new prompt is about to start a turn. If the previous turn was already
        # committed but its integration was deferred — typically because a
        # background commit summary was still being computed
        # (_summary_blocks_integration) — merge it into the base NOW, before the
        # new turn begins, instead of letting it ride along on the same branch and
        # only land once the whole new turn finishes.
        #
        # The merge is attempted directly (bypassing the summary hold): a pending
        # summary, if any, simply lands as git notes once it finishes
        # (_amend_summary_into_head sees the commit already in base and records
        # notes-only). A conflict is left on the branch with no resolve popup — per
        # the design it waits until the current agent call ends.
        if self.worktree is None or self.merge_ctx or self._base_branch is None:
            return
        if self._integration_paused or self._delay_merge:
            return  # --delay-merge: the user merges on their own confirmation
        try:
            if self.repo.has_tracked_changes() or self.repo.merge_in_progress():
                # A real in-flight tracked edit or a mid-merge: leave it for the normal
                # path. But UNTRACKED leftovers (files the agent created that the user
                # declined to stage) must NOT block this merge — they're not part of the
                # turn branch and don't affect the fast-forward, yet has_changes() counts
                # them, which stranded a ready, summarized commit whenever the next prompt
                # started before it had merged.
                return
            branch = self.repo.current_branch()
            if not is_managed_branch(branch):
                return
            if not self.base_repo.log_range(self._base_branch, branch):
                return  # nothing committed ahead of base to integrate
        except Exception as error:
            self._debug(f"pre-prompt integration check failed: {error!r}")
            return
        # Ignore a "conflict" result: integrate_turn_or_conflict already backed the
        # merge out, leaving the tree clean, and the conflict is surfaced when the
        # current turn finishes rather than interrupting the new prompt.
        self._integrate_turn_or_conflict()

    def _integrate_turn_or_conflict(self) -> str:
        # Try to fast-forward the current session's turn branch into the base.
        # Returns "integrated" (base advanced), "conflict" (the merge was backed
        # out and needs resolution), or "skip" (nothing to integrate).
        if self.worktree is None or self._base_branch is None or self.merge_ctx:
            return "skip"
        if self._integration_paused:
            return "skip"  # base switched out-of-band; merging is paused
        turn_branch = self.repo.current_branch()
        if not is_managed_branch(turn_branch):
            return "skip"
        try:
            if not self.repo.merge(self._base_branch):
                # Back out of the conflicted merge so the worktree is clean again;
                # the chosen resolution path re-starts the merge from here.
                self.repo.merge_abort()
                return "conflict"
            self._advance_base_to(turn_branch)
            self._debug(f"integrated '{self.name}' {turn_branch} -> {self._base_branch}")
            self._announce_agent_commit()
            return "integrated"
        except Exception as error:
            self._debug(f"integration failed for '{self.name}': {error!r}")
            return "skip"

    def _prompt_resolve_conflict(self, source_branch: str) -> None:
        # A finished turn cannot fast-forward into the base because it conflicts
        # with work another session already integrated. Surface the same resolve
        # options the session menu offers for a conflicting session, labelled
        # with this session and its backend, then dispatch the choice.
        backend = getattr(self.backend, "name", "?")
        choice = self._select_popup(
            f"Session '{self.name}' ({backend}) finished, but its changes conflict with '{self._base_branch}'.",
            [
                "Merge automatically (agent resolves conflicts)",
                "Merge manually (you resolve here, then Complete merge)",
                f"Leave for later (resolve via {self._menu_label()} → session)",
            ],
        )
        if not choice or choice.startswith("Leave"):
            self._set_message(
                f"'{self.name}' has unintegrated work that conflicts with '{self._base_branch}'. "
                f"Resolve it any time via {self._menu_label()} → session.",
                seconds=8.0,
            )
            self._render()
            return
        self._start_merge_for_active(auto=choice.startswith("Merge automatically"))

    def _integrate_session_on_exit(self) -> None:
        # Clean up the current session's branch when aGiTrack exits: integrate any
        # committed-but-unintegrated work into the base, then detach and delete
        # the turn branch (an empty one is dropped too). Conflicts / dirty trees
        # are left intact for the next startup to surface.
        if self.worktree is None or self._base_branch is None:
            return
        try:
            self._integration.integrate_session_on_exit(self.repo, self.merge_ctx)
        except Exception as error:
            self._debug(f"exit integration failed for '{self.name}': {error!r}")

    def _advance_base_to(self, source_branch: str) -> None:
        # The source branch now contains the base plus this session's work; move
        # the base to it, then detach the worktree at the new base and delete the
        # turn branch. A fresh turn branch is created lazily on the next commit,
        # so a fully-merged session leaves no branch behind — only its worktree,
        # whose conversation context can still be resumed.
        # When the base is the checked-out branch this is a working-tree
        # fast-forward; when the user has checked out a different branch in the
        # directory, advance_base_to advances the base's ref directly — but only
        # on a true fast-forward, never a force, so it can never move the wrong
        # branch or drop commits. Callers wrap this in try/except and treat a
        # failure (e.g. not a fast-forward) as "not integrated".
        self._integration.advance_base_to(self.repo, source_branch)
        # The base moved; other idle sessions should fast-forward onto it.
        self._base_advanced = True

    def _sync_idle_worktrees_to_base(self) -> None:
        # Keep every idle session's worktree current with the (just-advanced) base:
        # a session with nothing of its own is re-pointed onto the base, and one
        # that has its own committed work has the new base commits merged into its
        # turn branch (cleanly, or skipped on conflict) — see `_align_session_to_base`.
        # Only idle, clean worktrees are touched, so in-flight work is left alone.
        if self._integration_paused:
            return  # base switched out-of-band; don't re-point worktrees meanwhile
        for index in range(len(self.sessions)):
            if index == self.active_index:
                if not self.agent_in_flight:
                    self._align_session_to_base(self.repo)
                continue
            snapshot = self.sessions[index]
            if getattr(snapshot, "repo", None) is None or getattr(snapshot, "agent_in_flight", False):
                continue
            # Switch to the idle session so _base_branch (and the integration service)
            # reflect ITS OWN merge branch — never the active session's, which would
            # merge a different branch into this worktree (cross-branch contamination).
            self._with_session(snapshot, lambda: self._align_session_to_base(self.repo))

    def _ensure_turn_branch(self) -> None:
        # A merged-and-detached session sits at base between turns. Before its
        # next commit, put it on a fresh turn branch (this preserves the working
        # tree, so the agent's changes carry over).
        # Recovery paths can reset the turn counter (e.g. a recreated worktree
        # starts detached at base, so the counter restarts at 0) while earlier
        # turn branches still exist — deliberately kept when they hold
        # unintegrated commits. Never reuse such a name: resetting it would
        # destroy that work. Skip to the next free turn number instead.
        if self.worktree is None or not self.repo.is_detached():
            return
        new_turn = self._integration.ensure_turn_branch(
            repo=self.repo,
            worktree=self.worktree,
            turn=self.turn,
            worktree_manager=self._worktrees(),
            session_name=self.name,
            backend_name=self.backend.name,
        )
        self.turn = new_turn

    def _merge_resolution_prompt(self, files: list[str], context: str) -> str:
        # Delegates to IntegrationService.merge_resolution_prompt indirectly;
        # kept as a runner method because tests mock it directly.
        listing = ", ".join(files) if files else "the conflicted files"
        commits = context.replace("\n", "; ") if context else "(none recorded)"
        return (
            f"[aGiTrack] Merge conflict: the base branch '{self._base_branch}' gained changes from another "
            f"session that conflict with your work in {listing}. The conflicting base commits are: {commits}. "
            "Please open the conflicted files, resolve every <<<<<<< / ======= / >>>>>>> marker keeping both "
            "changes' intent, and save. Do NOT run git or commit — aGiTrack will create the merge commit once you are done."
        )

    def _inject_prompt(self, text: str) -> None:
        # Type a synthesized single-line prompt into the active backend, then
        # submit it with a separate Enter a beat later. Backends like Claude Code
        # use bracketed paste, where a trailing "\r" in the same write is treated
        # as a newline inside the box rather than a submit; sending Enter as its
        # own keystroke once the text has settled reliably submits the prompt.
        if self.master_fd is None:
            return
        payload = " ".join(text.split()).encode("utf-8", errors="replace")
        try:
            self.active.process.write(payload)
        except OSError:
            return  # text never reached the backend; don't schedule its Enter
        self._pending_enter_at = time.monotonic() + 0.4
        self._pending_enter_fd = self.master_fd  # submit to THIS backend, not whatever is active later

    def _flush_pending_enter(self) -> None:
        # Submit a previously-injected prompt once its text has settled. The Enter
        # is sent to the PTY the text was typed into, even if the active session
        # changed in the meantime, so it never lands in another backend.
        if self._pending_enter_at is None or time.monotonic() < self._pending_enter_at:
            return
        self._pending_enter_at = None
        fd = self._pending_enter_fd
        self._pending_enter_fd = None
        if fd is None:
            return
        try:
            if self.master_fd == fd:
                # The common case (the prompt's session is still active): write through its
                # platform child, which knows how to reach the backend (a raw os.write to a
                # bridge-socket fd would be wrong on Windows).
                self.active.process.write(b"\r")
            else:
                # A backgrounded session's PTY (POSIX multi-session merge); a transient
                # POSIX child writes straight to its master fd.
                BackendProcess(fd).write(b"\r")
        except OSError:
            return  # Enter never reached the backend; the merge gate stays closed
        if self.merge_ctx is not None and self.master_fd == fd:
            self.merge_ctx["prompt_sent_at"] = time.monotonic()
            if self.merge_ctx.phase is MergePhase.PENDING:
                self.merge_ctx.phase = MergePhase.RESOLVING

    def _begin_agent_merge(self, source_branch: str) -> None:
        # A merge is in progress (conflicted) in the worktree. Ask the session's
        # agent to resolve it; aGiTrack finalizes once the conflicts are gone.
        assert self._base_branch is not None  # a merge only starts once the base is established
        files = self.repo.unmerged_paths()
        try:
            context = self.base_repo.log_range(source_branch, self._base_branch, paths=files)
        except Exception:
            context = ""
        self._inject_prompt(self._merge_resolution_prompt(files, context))
        self.merge_ctx = MergeContext(
            source_branch=source_branch,
            context=context,
            phase=MergePhase.PENDING,
            auto_tried=False,
            prompt_sent_at=None,  # set once the submit Enter goes out
        )
        self.agent_in_flight = True
        self._set_message(
            f"Merge conflict in {', '.join(files) or 'this session'} — asking the agent to resolve it… "
            f"aGiTrack will commit the merge once the agent finishes (or use {self._menu_label()} → session → Complete merge).",
            seconds=12.0,
        )
        self._render()

    def _note_backend_session_change(self, new_session_id: str | None) -> None:
        # Keep the durable name record pointing at the conversation this session
        # is actually running (ids drift when the backend forks on resume).
        self._persist_session_name(new_session_id)
        previous = self.state.backend_session_id
        # Preserve shared-session recognition across the id drift: if this session
        # was shared/auto-shared under its previous id, remember new→previous so it
        # still shows as shared (and keeps auto-updating) after resume (#55). Also
        # carries the original share name onto the new id (round-trip re-sharing).
        self._record_shared_alias_on_drift(previous, new_session_id)
        # If the worktree's active conversation changed to a different backend
        # session that aGiTrack didn't start, the user likely started it from inside
        # the backend. Warn once that such sessions share this branch.
        if (
            self.worktree is not None
            and previous
            and new_session_id
            and new_session_id != previous
            and not self._warned_backend_session
        ):
            self._warned_backend_session = True
            self._set_message(
                "Detected a new conversation started inside the backend. Its changes are tracked on "
                f"this session's branch. To get a separate branch, start sessions with {self._menu_label()} → session → New.",
                seconds=12.0,
            )

    def _maybe_complete_agent_merge(self) -> None:
        # Auto-finalize a pending agent merge only after the agent has actually
        # engaged with the injected prompt (produced output after we submitted
        # it) and then gone idle — never before the prompt has even been sent.
        # MANUAL contexts (phase == MANUAL, auto_tried == True) are never
        # auto-finalized; should_auto_complete_merge gates them via auto_tried.
        ctx = self.merge_ctx
        if not ctx:
            return
        if not self._integration.should_auto_complete_merge(ctx, self.last_child_output, self.CHILD_IDLE_SECONDS):
            return
        ctx.auto_tried = True  # prevent a second attempt
        self._finalize_agent_merge()

    def _finalize_agent_merge(self) -> bool:
        ctx = self.merge_ctx
        if not ctx:
            return False
        try:
            success, message = self._integration.finalize_agent_merge(
                self.repo,
                ctx,
                session_name=self.name,
                agitrack_session_id=self.state.session_id,
                backend_name=self.backend.name,
                backend_session_id=self.state.backend_session_id,
            )
        except Exception as error:
            self._debug(f"finalize agent merge failed: {error!r}")
            return False  # merge_ctx intentionally kept — user can retry
        if success is False and message is None:
            # merge not in progress — already resolved/aborted elsewhere
            self.merge_ctx = None
            return False
        if message and not success:
            self._set_message(message, seconds=10.0)
            return False
        if success:
            self.merge_ctx = None
            self.agent_in_flight = False
            self._base_advanced = True  # sync idle worktrees after base advanced
            self._set_message(message, seconds=6.0)
            self._render()
            return True
        return False

    def _session_name(self, index: int) -> str:
        if index == self.active_index:
            return self.name or f"session{index}"
        return getattr(self.sessions[index], "name", None) or f"session{index}"

    def _session_status(self, index: int) -> str:
        # "running" = the backend is actively working (a turn is in flight or it
        # produced output recently); otherwise "idle" (waiting for input).
        if index == self.active_index:
            in_flight, last = self.agent_in_flight, self.last_child_output
        else:
            session = self.sessions[index]
            in_flight = getattr(session, "agent_in_flight", False)
            last = getattr(session, "last_child_output", 0.0)
        working = in_flight or (last and time.monotonic() - last < self.CHILD_IDLE_SECONDS)
        return "running" if working else "idle"

    def _handle_session_command(self, arg: str) -> str:
        # Ctrl-G then "sessions": manage the live concurrent sessions.
        arg = arg.strip()
        if arg in {"new", "fresh"}:
            return self._prompt_new_session()
        if arg.isdigit():
            self._switch_active(int(arg) - 1)
            return self._MENU_DONE
        return self._session_menu()

    def _handle_summarizer_command(self, arg: str) -> str:
        sub = arg.strip().lower()
        if sub in ("on", "off"):
            enabled = sub == "on"
            self._persist_summarizer_enabled(enabled)
            self._set_message(f"Summarizer {'enabled' if enabled else 'disabled'}.")
            self._render()
            return self._MENU_DONE
        if sub == "model":
            self._select_summarizer_model_popup()
            return self._MENU_DONE
        # Interactive menu: "Set model" opens the model picker, whose Esc returns here
        # (one level up); Esc on this list goes up one level to the Ctrl-G palette.
        while True:
            enabled = self._summarization_enabled()
            model = self.state.summarization_model or self.global_config.summarization_model or "(same as session)"
            choice = self._select_popup(
                "Summarizer",
                [
                    f"Toggle ({'ON' if enabled else 'OFF'})",
                    f"Set model (current: {model})",
                ],
            )
            if choice is None:  # Esc → up one level to the palette
                self._render()
                return self._MENU_UP
            if choice.startswith("Toggle"):
                self._persist_summarizer_enabled(not enabled)
                self._set_message(f"Summarizer {'enabled' if not enabled else 'disabled'}.")
            elif choice.startswith("Set model"):
                self._select_summarizer_model_popup()
            self._render()

    def _choose_summarizer_model(self) -> "str | None | object":
        """Show the summarizer-model picker and RETURN the chosen model name, None for "same as
        the session model", or ``_NO_MODEL_PICK`` if the user backed out. Does not persist — so
        both the immediate popup and the settings menu's pending-save flow can reuse it. Lists
        the current backend's models (smallest tier first as the cheap default); a backend whose
        models can't be listed falls back to free-text entry."""
        from agitrack.summaries.model_select import list_available_models, smallest_model

        backend_name = self.state.backend
        current = self.state.summarization_model or self.global_config.summarization_model
        models = list_available_models(backend_name)
        if models:
            smallest = smallest_model(backend_name, models)
            ordered = [smallest, *(m for m in models if m != smallest)] if smallest else list(models)
            label_for: dict[str, str | None] = {}
            options: list[str] = []
            for model in ordered:
                label = f"{model}  (smallest — default)" if model == smallest else model
                label_for[label] = model
                options.append(label)
            clear_label = "Same as the agent's session model"
            label_for[clear_label] = None
            options.append(clear_label)
            choice = self._select_popup(f"Summarizer model (current: {current or 'same as session'})", options)
            return _NO_MODEL_PICK if choice is None else label_for.get(choice, choice)
        # No model list available (the backend's CLI couldn't be queried) — type a name.
        raw = self._prompt_popup(
            "Summarizer Model",
            f"Current: {current or '(same as session)'}\nEnter model (empty to clear):",
            default=current or "",
        )
        return _NO_MODEL_PICK if raw is None else (raw.strip() or None)

    def _select_summarizer_model_popup(self) -> None:
        # Pick a model and persist it immediately (the `:summarizer model` path). The settings
        # menu instead stores the same choice as a pending edit (see _edit_one_setting).
        chosen = self._choose_summarizer_model()
        if chosen is not _NO_MODEL_PICK:
            self._persist_summarizer_model(chosen if chosen is None else str(chosen))
            self._set_message(f"Summarizer model: {self.global_config.summarization_model or '(same as session)'}")
        self._render()

    def _persist_summarizer_model(self, value: str | None) -> None:
        # Persist the choice in the GLOBAL config so it survives restarts, session
        # switches, and applies across the whole repo; clear any per-session override
        # (which lives in a transient worktree state) so the global value takes effect.
        self.global_config.summarization_model = value
        self.state.summarization_model = None

    def _persist_summarizer_enabled(self, enabled: bool) -> None:
        # Persist the on/off toggle in the GLOBAL config so it survives restarts, like the
        # model. The per-session state lives in the session worktree's .agitrack/config.json,
        # which is removed when the worktree is torn down on exit — a toggle written only
        # there always reset to the default ("on") on the next launch (the reported bug).
        if self.global_config is not None:
            self.global_config.summarization_enabled = enabled
        # Keep the active session's view consistent for the current run.
        self.state.summarization_enabled = enabled

    # --- switch base branch ---

    def _prompt_merge_branch(self, title: str, current: str | None) -> str | None:
        # Pick a real (non-managed) branch as a session's merge destination; the
        # session's current target is marked. Returns the branch, or None on cancel.
        branches = [b for b in self.base_repo.list_branches() if not is_managed_branch(b)]
        if not branches:
            self._set_message("No branches available to merge into.")
            self._render()
            return None
        label_for: dict[str, str] = {}
        options: list[str] = []
        for branch in branches:
            label = f"{branch}  (current target)" if branch == current else branch
            label_for[label] = branch
            options.append(label)
        choice = self._select_popup(title, options)
        return None if choice is None else label_for.get(choice, choice)

    def _change_session_merge_branch_menu(self) -> None:
        # Session-config entry: change the merge destination of ANY session.
        if not self.sessions:
            self._set_message("No sessions.")
            self._render()
            return
        label_for: dict[str, int] = {}
        options: list[str] = []
        for index in range(len(self.sessions)):
            target = getattr(self.sessions[index], "_base_branch", None) or "?"
            label = f"{self._session_name(index)}  → {target}"
            label_for[label] = index
            options.append(label)
        choice = self._select_popup("Change the merge branch of which session?", options)
        if choice is None:
            return
        index = label_for[choice]
        session = self.sessions[index]
        if getattr(session, "agent_in_flight", False):
            # A running session's merge branch can't change mid-turn (its in-flight work
            # would split across branches); only idle sessions can be re-pointed.
            self._set_message(
                f"'{self._session_name(index)}' is running a turn — change its merge branch when it's idle.",
                seconds=8.0,
            )
            self._render()
            return
        current = getattr(session, "_base_branch", None)
        target = self._prompt_merge_branch(f"Merge '{self._session_name(index)}' into which branch?", current)
        if not target:
            return
        if target == current:
            self._set_message(f"'{self._session_name(index)}' already merges into '{target}'.")
            self._render()
            return
        if session is self.active:
            self._retarget_active_session(target)
        else:
            # Re-target a background session by temporarily making it active.
            self._with_session(session, lambda: self._retarget_active_session(target))

    def _integrate_session_keeping_alive(self) -> None:
        # Commit the latest turn and integrate it into the current base WITHOUT
        # the exit semantics of _finalize_pending_work — the backend child keeps
        # running and the worktree is kept. With _exiting set by the caller, a
        # conflict is left on the branch (no resolve popup) for the caller's
        # unintegrated-sessions check to report.
        self._commit_latest_turn_sync()
        if self._summary_thread is not None and self._summary_thread.is_alive():
            self._summary_thread.join(timeout=10)
        self._service_commit_summary()
        self._integrate_session_turn()

    def _repoint_current_to_base(self) -> None:
        # Detach the current session's worktree at the new base so its next turn
        # branches from there. The session and its conversation keep running.
        try:
            new_turn = self._integration.repoint_to_base(self.repo, self.worktree)
        except Exception as error:
            self._debug(f"re-point failed for '{self.name}': {error!r}")
            return
        if new_turn is not None:
            self.turn = new_turn

    def _session_menu(self) -> str:
        # One loop, two outcomes: Esc on the list → _MENU_UP (one level up to the Ctrl-G
        # palette); a context transition (switch/new/resume/integrate) → _MENU_DONE (unwind
        # to the agent). A configuration sub-action (rename, change merge branch, share,
        # manage shared, stop) or a child menu the user backed out of falls through to
        # ``continue``, re-showing this list so Esc inside it lands one level up here.
        while True:
            options: list[str] = []
            actions: list[tuple[str, object]] = []
            if self.merge_ctx or (self.worktree is not None and self.repo.merge_in_progress()):
                options.append("✓ Complete merge for this session")
                actions.append(("complete-merge", None))
            if not self.merge_ctx and self.worktree is not None and self._active_has_pending():
                # The active session has committed work not yet in the base — offer an
                # explicit, discoverable way to integrate it now (otherwise the only path
                # is re-selecting the current session, which isn't obvious). Under
                # --delay-merge this is the confirm point for reviewed changes; otherwise
                # it surfaces leftover commits (e.g. from a session resumed at startup).
                base = self._base_branch or "the base branch"
                label = (
                    "✓ Merge reviewed changes into "
                    if self._delay_merge
                    else "✓ Integrate this session's commits into "
                )
                options.append(label + base)
                actions.append(("integrate-active", None))
            shared_ids = self._my_shared_session_ids()  # mark which sessions are shared (#55)
            live_names = set()
            for index, session in enumerate(self.sessions):
                live_names.add(self._session_name(index))
                marker = "* " if index == self.active_index else "  "
                backend = getattr(getattr(session, "backend", None), "name", "?")
                label = f"{marker}{self._session_name(index)} [{self._session_status(index)}] ({backend})"
                merge_branch = getattr(session, "_base_branch", None)
                if merge_branch:
                    label += f" → {merge_branch}"  # the branch this session merges into
                if index == self.active_index and not self.merge_ctx and self._active_has_pending():
                    label += " — commits to integrate"
                sid = getattr(getattr(session, "state", None), "backend_session_id", None)
                # Recognise a shared session across resume id-drift (lineage-aware).
                if sid and self._session_auto_shared(sid):
                    label += " · ⇪ auto-share"
                elif sid and self._session_is_shared(sid, shared_ids):
                    label += " · ⇪ shared"
                options.append(label)
                actions.append(("switch", index))
            for info in self._dormant_worktrees(live_names):
                if self._dormant_has_pending(info):
                    options.append(f"  {info.name} [unmerged changes — resolve]")
                    actions.append(("resolve", info.name))
                else:
                    options.append(f"  {info.name} [idle — resume]")
                    actions.append(("resume", info.name))
            options.append(
                "+ New session (own worktree)" if self._use_worktrees else "+ New session (shares this directory)"
            )
            actions.append(("new", None))
            if self._use_worktrees and self.sessions:
                options.append("")  # gap: separate session-creation from session-management
                actions.append(("separator", None))
                options.append("✎ Rename a session")
                actions.append(("rename", None))
                options.append("⤳ Change a session's merge branch")
                actions.append(("merge-branch", None))
            if self._resumable_sessions():
                options.append("↻ Resume a past conversation…")
                actions.append(("resume-past", None))
            # "Share" is offered for every backend so the user gets a clear answer;
            # a backend without a portable transcript says so when chosen. "Resume a
            # shared session" only appears where it can actually work.
            options.append("")  # gap: set the sharing group apart
            actions.append(("separator", None))
            options.append("⇪ Share this session with collaborators…")
            actions.append(("share", None))
            if getattr(self.backend, "supports_session_sharing", False):
                options.append("⇩ Resume a shared session…")
                actions.append(("resume-shared", None))
                options.append("⚙ Manage shared sessions…")
                actions.append(("manage-shared", None))
            if len(self.sessions) > 1:
                options.append("- Stop a session")
                actions.append(("stop", None))
            choice = self._select_popup("Sessions", options)
            if choice is None:  # Esc → up one level (to the Ctrl-G palette), silently
                return self._MENU_UP
            kind, value = actions[options.index(choice)]
            if kind == "separator":
                continue  # the blank spacer row isn't an action; re-show the list
            # Context transitions — these move you into a different session, so unwind to the agent.
            if kind == "switch":
                assert isinstance(value, int)  # "switch" pairs with a session index
                if value == self.active_index:
                    self._select_current_session()
                else:
                    self._switch_active(value)
                return self._MENU_DONE
            if kind == "integrate-active":
                self._integrate_active_session()  # explicit "integrate now" for the active session
                return self._MENU_DONE
            if kind == "complete-merge":
                self._finalize_agent_merge()
                return self._MENU_DONE
            if kind == "resolve":
                assert isinstance(value, str)  # "resolve" pairs with a worktree name
                self._resolve_dormant_worktree(value)
                return self._MENU_DONE
            if kind == "resume":
                assert isinstance(value, str)  # "resume" pairs with a worktree name
                self._new_session(value)
                return self._MENU_DONE
            if kind == "new":
                if self._prompt_new_session() == self._MENU_DONE:
                    return self._MENU_DONE
                continue
            # Sub-menus that may transition (resume) OR be backed out of: a transition
            # unwinds to the agent; backing out re-shows this list (Esc lands here).
            if kind == "resume-past":
                if self._resume_session_menu() == self._MENU_DONE:
                    return self._MENU_DONE
                continue
            if kind == "resume-shared":
                if self._resume_shared_session_menu() == self._MENU_DONE:
                    return self._MENU_DONE
                continue
            if kind == "manage-shared":
                if self._manage_shared_sessions_menu() == self._MENU_DONE:
                    return self._MENU_DONE
                continue
            # Configuration actions: run, then re-show this list (so their Esc lands here).
            if kind == "rename":
                self._rename_session_menu()
            elif kind == "merge-branch":
                self._change_session_merge_branch_menu()
            elif kind == "share":
                self._share_session()
            else:
                self._stop_session_menu()

    def _active_has_pending(self) -> bool:
        # True if the active session has committed work not yet in the base.
        return self._integration.active_has_pending(self.repo, self.worktree)

    def _active_has_mergeable_work(self) -> bool:
        # True if the (idle) active session has work worth merging now: committed-but-
        # unmerged commits, or uncommitted committable changes. Excludes mid-turn state,
        # where uncommitted changes are expected and auto-committed.
        return (
            self.worktree is not None
            and not self.merge_ctx
            and not self.agent_in_flight
            and (self._active_has_pending() or self._active_has_committable_changes())
        )

    def _select_current_session(self) -> None:
        # Picking the session you're already in: if it has work to merge, integrate it now
        # (the commit/merge — or conflict-resolve — messages pop up directly); otherwise
        # just acknowledge you're already here, rather than the confusing "nothing to
        # integrate".
        if self._active_has_mergeable_work() and self._base_branch is not None:
            self._merge_active_into(self._base_branch)
        else:
            self._set_message(f"Already in session '{self.name}'.")
            self._render()

    def _integrate_active_session(self) -> None:
        # Selecting the current session offers to integrate its outstanding
        # commits (the "merge box"), since there is nothing to switch to.
        if self.worktree is None or self._base_branch is None:
            self._set_message("This session has no worktree to integrate.")
            self._render()
            return
        if self.repo.has_changes():
            # Best effort: the user asked to merge, so don't dead-end them on uncommitted
            # changes. Only a turn that is GENUINELY still running should block (its edits
            # are mid-flight). Refresh the in-flight flag first (the git worker may not have
            # run since the turn went quiet), then if a turn really is active, ask them to
            # finish/stop it; otherwise commit the committable leftovers now and proceed.
            self._clear_agent_in_flight_if_idle()
            if self._agent_is_active():
                self._set_message(
                    "Finish or stop the current turn before integrating — a turn is still running.",
                    seconds=8.0,
                )
                self._render()
                return
            if self._active_has_committable_changes():
                self._set_message(f"Committing '{self.name}' changes before merging…", seconds=30)
                self._render()
                self._commit_latest_turn_sync()
            # Anything still uncommitted now is non-committable (git-ignored or intentionally
            # unstaged files) and can't enter the merge, but committed work still can —
            # fall through and integrate that rather than refuse the whole merge.
        if not self._active_has_pending():
            self._set_message(f"'{self.name}' has nothing to integrate.")
            self._render()
            return
        # Try a clean / fast-forward integration first — if the work merges without
        # conflicts there is nothing to resolve, so the agent is never involved and
        # the user is not asked. Only a real conflict surfaces the resolve options.
        result = self._integrate_turn_or_conflict()
        if result == "integrated":
            self._set_message(f"Integrated '{self.name}' into {self._base_branch} (no conflicts).", seconds=6.0)
            self._render()
            return
        if result == "conflict":
            self._prompt_resolve_conflict(self.repo.current_branch())
            return
        self._set_message(f"'{self.name}' has nothing to integrate.")
        self._render()

    # ------------------------------------------------------------------
    # Ctrl-G "merge": rescue un-integrated worktrees into a chosen branch
    # ------------------------------------------------------------------

    def _unmerged_worktrees(self) -> list[tuple[str, str]]:
        """Worktrees carrying un-integrated work, as ``(label, name)`` — ``name`` is
        empty for the active session, else the dormant worktree's name.

        "Un-integrated work" is either committed-but-unmerged commits OR uncommitted
        committable changes (tracked edits / untracked files that are neither
        git-ignored nor intentionally unstaged). The active session is only offered
        when idle — mid-turn uncommitted changes are expected and auto-committed, so
        surfacing "merge" then would be noise."""
        items: list[tuple[str, str]] = []
        try:
            if self._active_has_mergeable_work():
                items.append((f"{self.name} (current session)", ""))
        except Exception:
            pass
        live = {self._session_name(index) for index in range(len(self.sessions))}
        for info in self._dormant_worktrees(live):
            try:
                if self._dormant_has_pending(info):  # already counts uncommitted changes
                    items.append((f"{info.name} (dormant)", info.name))
            except Exception:
                continue
        return items

    def _active_has_committable_changes(self) -> bool:
        """True if the active worktree has uncommitted changes worth committing: tracked
        edits, or untracked files that aren't git-ignored and weren't intentionally
        unstaged. Excludes ignored/declined files so we don't offer to merge noise."""
        try:
            if self.repo.has_tracked_changes():
                return True
            declined = set(self.state.declined_untracked()) if self.state is not None else set()
            return any(path not in declined for path in self.repo.untracked_files())
        except Exception:
            return False

    def _has_unmerged_work(self) -> bool:
        """True when some worktree still has work that hasn't been integrated — gates
        the top-level Ctrl-G "merge" command."""
        return bool(self._unmerged_worktrees())

    def _handle_merge_command(self) -> None:
        items = self._unmerged_worktrees()
        if not items:
            self._set_message("No unmerged worktrees — everything is integrated.")
            self._render()
            return
        if len(items) == 1:
            name = items[0][1]
        else:
            choice = self._select_popup("Merge which worktree's changes?", [label for label, _ in items])
            if choice is None:
                return
            name = next(n for label, n in items if label == choice)
        target = self._choose_merge_target(name)
        if target is None:
            return
        if name == "":
            self._merge_active_into(target)
        else:
            # Load the dormant worktree as the active session, then merge it — this
            # reuses the integration + conflict-resolution path (the agent can resolve
            # conflicts in the now-live session, just like the resolve flow).
            self._new_session(name)
            self._merge_active_into(target)

    def _choose_merge_target(self, name: str) -> str | None:
        """Offer the three merge destinations the user asked for: the branch checked out
        in the working directory, the session's own merge branch, or any other branch.
        Returns the chosen branch name, or None on cancel."""
        session_branch = self._base_branch if name == "" else (self._dormant_merge_branch(name) or self._base_branch)
        current = self._repo_dir_branch or self.base_repo.current_branch()
        _CUSTOM = "\x00custom"
        options: list[tuple[str, str]] = [(f"Current branch ({current})", current)]
        if session_branch and session_branch != current:
            options.append((f"Session's branch ({session_branch})", session_branch))
        options.append(("A different branch…", _CUSTOM))
        choice = self._select_popup("Merge into which branch?", [label for label, _ in options])
        if choice is None:
            return None
        target = next(branch for label, branch in options if label == choice)
        if target == _CUSTOM:
            return self._prompt_merge_branch("Merge into which branch?", session_branch)
        return target

    def _merge_active_into(self, target: str) -> None:
        """Integrate the active session's un-integrated work into ``target`` (re-pointing
        the session's merge branch to it). Reuses _integrate_active_session, which best-
        effort commits any uncommitted committable changes first (so nothing is stranded)
        and surfaces the same resolve options on a conflict."""
        if self.worktree is None or self._base_branch is None:
            self._set_message("This session has no worktree to merge.")
            self._render()
            return
        self._base_branch = target  # syncs the IntegrationService to the chosen destination
        self._integrate_active_session()

    def _dormant_merge_branch(self, name: str) -> str | None:
        """The merge branch a dormant worktree recorded for itself, if any."""
        try:
            info = next((i for i in self._worktrees().list() if i.name == name), None)
            if info is None:
                return None
            return getattr(AgitrackState(info.path), "merge_branch", None)
        except Exception:
            return None

    def _resolve_dormant_worktree(self, name: str) -> None:
        choice = self._select_popup(
            f"Session '{name}' has unmerged changes",
            [
                "Merge automatically (agent resolves conflicts)",
                "Merge manually (you resolve in the session)",
                "Discard this session's changes",
            ],
        )
        if choice is None:
            return
        if choice.startswith("Discard"):
            confirm = self._select_popup(
                f"Discard ALL un-integrated changes in '{name}'? This cannot be undone.",
                ["No, keep it", "Yes, discard"],
            )
            if confirm == "Yes, discard":
                self._worktrees().remove(name)
                self._set_message(f"Discarded session '{name}'.")
            else:
                self._set_message("Kept.")
            self._render()
            return
        # Both merge paths relaunch the session, then start the merge in it.
        self._new_session(name)
        self._start_merge_for_active(auto=choice.startswith("Merge automatically"))

    def _start_merge_for_active(self, *, auto: bool) -> None:
        # Begin merging base into the (now active) session's branch.
        outcome, ctx, message = self._integration.start_merge(
            repo=self.repo,
            name=self.name,
            worktree=self.worktree,
            auto=auto,
        )
        if outcome == "skip":
            return
        if outcome == "error":
            self._set_message(message, seconds=8.0)
            self._render()
            return
        if outcome == "clean":
            # advance_base_to was already called inside the service; set _base_advanced.
            self._base_advanced = True
            self._set_message(message, seconds=6.0)
            self._render()
            return
        # conflict_auto or conflict_manual: ctx is a MergeContext
        assert ctx is not None
        if outcome == "conflict_auto":
            # Re-enter _begin_agent_merge to inject the prompt (it uses self.repo etc.)
            self._begin_agent_merge(ctx.source_branch)
        else:  # conflict_manual
            self.merge_ctx = ctx
            self._set_message(message, seconds=12.0)
            self._render()

    RESUME_LIST_LIMIT = 20
    _WORKTREE_SESSIONS_TTL = 10.0  # seconds to memoize the slow `opencode session list` enumeration

    def _worktree_sessions(self) -> list:
        # (worktree-name, SessionRef) for every conversation this backend recorded
        # under any of the repo's aGiTrack worktrees — including ones whose worktree
        # has since been removed (the backend keeps the transcript keyed by path
        # or id). The worktree directory name IS the session's user-given name, so
        # this is how a named session — and its name — survives across runs even
        # after its worktree is gone.
        #
        # Briefly MEMOIZED: a single open of the resume menu calls this 3× (via
        # _resumable_sessions and _agitrack_named_sessions), each a slow `opencode session
        # list` subprocess — without the cache, entering the menu fired several of them back
        # to back and the TUI hung for seconds. The short TTL covers one menu render while
        # staying fresh enough that a just-created session still appears next time.
        now = time.monotonic()
        cached = self._worktree_sessions_cache
        if cached is not None and now - cached[0] < self._WORKTREE_SESSIONS_TTL:
            return cached[1]
        try:
            result = list(self.backend.list_worktree_sessions(self._worktrees().root))
        except Exception as error:
            self._debug(f"list_worktree_sessions failed: {error!r}")
            result = []
        self._worktree_sessions_cache = (time.monotonic(), result)
        return result

    def _resumable_sessions(self) -> list:
        # Past conversations to offer for resume: those the backend recorded in
        # the base repo (a plain claude/opencode run, or --no-worktree mode) plus
        # every conversation recorded under an aGiTrack worktree of this repo. The
        # worktree ones are the named sessions, and they are surfaced here even
        # once their worktree has been emptied on quit — otherwise a named session
        # that ran in a worktree (i.e. every normal session) would vanish from the
        # list on the next run. Resuming is by session id, which the backend
        # resolves regardless of directory.
        try:
            refs = list(self.backend.list_sessions(self.base_repo.repo))
        except Exception as error:
            self._debug(f"list_sessions failed: {error!r}")
            refs = []
        seen = {ref.id for ref in refs}
        for _name, ref in self._worktree_sessions():
            if ref.id not in seen:
                seen.add(ref.id)
                refs.append(ref)
        # A session that produced no commits still reserves its name in the durable
        # record, but the backend may no longer enumerate it (no transcript under a
        # current worktree path). Surface those named conversations too, so a
        # reserved name is never stranded — unresumable yet un-reusable (#75).
        # Resuming one recreates its worktree under the saved name and continues the
        # conversation if the backend still has it, or starts fresh there if not.
        # Date them by when they were last named (not the Unix epoch, which showed
        # as an absurd "20000d ago").
        try:
            root = AgitrackState(self.base_repo.repo, default_backend=self.global_config.default_backend)
        except Exception:
            root = None
        for sid, name in self._agitrack_named_sessions().items():
            if sid and sid not in seen:
                seen.add(sid)
                named_at = root.session_named_at(sid) if root is not None else 0.0
                refs.append(SessionRef(id=sid, updated=named_at, label=name))
        refs = sorted(refs, key=lambda ref: getattr(ref, "updated", 0) or 0, reverse=True)
        return refs[: self.RESUME_LIST_LIMIT]

    def _agitrack_named_sessions(self) -> dict:
        # Friendly names for conversations aGiTrack itself created/named, keyed by
        # backend session id. Used only to label/resume the list that comes from
        # the backend. Two sources, in precedence order:
        #   1. the durable repo-root record (user-given names, which follow id
        #      drift across resumes), then
        #   2. the worktree directory name a conversation ran in — which is the
        #      session's name — recovered for any conversation the record missed
        #      (its id drifted before being linked, or it was never persisted).
        names: dict = {}
        record = None
        try:
            root = AgitrackState(self.base_repo.repo, default_backend=self.global_config.default_backend)
            names.update({str(k): str(v) for k, v in (root.data.get("session_names") or {}).items() if v})
            record = root.recall_session(self.backend.name)
        except Exception:
            record = None
        if record and record.get("id") and record.get("worktree"):
            names.setdefault(str(record["id"]), str(record["worktree"]))
        for name, ref in self._worktree_sessions():
            if name:
                names.setdefault(str(ref.id), name)
        return names

    def _resume_session_menu(self) -> str:
        sessions = self._resumable_sessions()
        if not sessions:
            self._set_message("No past conversations found to resume.")
            self._render()
            return self._MENU_UP
        live_ids = {getattr(getattr(s, "state", None), "backend_session_id", None) for s in self.sessions}
        names = self._agitrack_named_sessions()
        options: list[str] = []
        for ref in sessions:
            mark = "● " if ref.id in live_ids else "  "
            label = (names.get(ref.id) or ref.label or "(no prompt recorded)").strip()[:48]
            options.append(f"{mark}{_short_session(ref.id)}  {self._format_age(ref.updated)}  {label}")
        choice = self._select_popup("Resume a conversation (newest first)", options)
        if choice is None:  # Esc → back up one level to the sessions menu
            return self._MENU_UP
        ref = sessions[options.index(choice)]
        self._resume_conversation(names.get(ref.id) or self._next_session_name(), ref.id)
        return self._MENU_DONE

    def _resume_conversation(self, name: str, session_id: str, *, backend: str | None = None) -> None:
        # If this conversation is already live, just switch to it; otherwise
        # create a worktree for it and resume the backend by id there. ``backend``
        # pins the new session to a specific backend (e.g. resuming a shared
        # OpenCode session while the active backend is Claude).
        for index, session in enumerate(self.sessions):
            if getattr(getattr(session, "state", None), "backend_session_id", None) == session_id:
                self._switch_active(index)
                return
        if self._live_session_name_taken(name):
            # The name is occupied by a different LIVE session. Two live sessions can't share a
            # name — a session and its git worktree are 1:1, so they'd share a worktree and run
            # two backends in one directory. Don't silently rename: ASK the user, explaining
            # why, and offer a random word they can keep or change (Esc cancels the resume).
            chosen = self._prompt_session_name(
                f"A session named '{name}' is already open, so this resumed conversation needs a "
                f"different name (two live sessions can't share a worktree). New name:",
                default=self._next_session_name(),
            )
            if chosen is None:
                return  # user cancelled — leave the active session as-is
            name = chosen
        # Resuming relocates and re-spawns the backend (OpenCode in --no-worktree mode also
        # exports+imports the session to retarget its directory) — a few seconds. Paint a
        # notice first so the screen shows progress instead of looking frozen.
        self._set_message(f"Resuming '{name}'…")
        self._render()
        self._new_session(name, resume_session_id=session_id, backend=backend)

    def _live_session_name_taken(self, name: str) -> bool:
        sanitized = _sanitize_name(name)
        return any(_sanitize_name(self._session_name(index)) == sanitized for index in range(len(self.sessions)))

    def _live_session_for_lineage(self, owner: str, name: str) -> int | None:
        """Index of a live session that is the SAME shared session as ``owner/name`` —
        matched by its recorded shared-lineage origin, not its backend id (which differs
        per collaborator). None when no open session shares that lineage."""
        user = self._user_state()
        for index, session in enumerate(self.sessions):
            sid = getattr(getattr(session, "state", None), "backend_session_id", None)
            rec = user.shared_origin(sid) if sid else None
            if rec and rec.get("name") == name and (not owner or rec.get("owner") == owner):
                return index
        return None

    # --- sharing full sessions via git (issue #55) -------------------------

    def _shared_store(self):
        from agitrack.sessions import SharedSessionStore

        # Operates on the base repo: it owns the remote and the shared object db.
        return SharedSessionStore(self.base_repo)

    def _share_identity(self, session_id: str | None, login: str) -> tuple[str, str, list[str]]:
        """The ``(origin_owner, name, contributors)`` a share/auto-share writes under.

        For a session imported from someone else, this is its recorded lineage origin
        (owner + name), with the current sharer merged into the contributor set — so the
        re-share updates the SAME entry (`<id1>+<id2>/<name>`, order-independent) rather
        than spawning `<sharer>/<name>` on each machine. For a session originated here,
        it's our own login + local name, contributors = [login]. A fork ("Keep both" or a
        deliberate copy) records no origin, so it starts a fresh lineage here (#55)."""
        rec = self._user_state().shared_origin(session_id) if session_id else None
        if rec and rec.get("name"):
            owner = rec.get("owner") or login
            contributors = sorted({*rec.get("contributors", []), owner, login})
            return owner, str(rec["name"]), contributors
        return login, self._session_name(self.active_index), [login]

    def _sweep_orphan_shared_sessions(self, *, fetch: bool) -> None:
        # Reclaim dangling shared-session snapshots (old/unshared versions) left by
        # rewrites — only when this repo has actually used sharing (the ref exists),
        # so unrelated repos pay nothing. Only touches genuine session snapshots,
        # never other unreachable commits. Best-effort.
        try:
            store = self._shared_store()
            if not self.base_repo.ref_exists(store.ref):
                return
            store.cleanup_orphans(fetch=fetch)
        except Exception as error:
            self._debug(f"orphan-session sweep failed: {error!r}")

    def _share_session(self) -> None:
        backend = self.backend
        if not getattr(backend, "supports_session_sharing", False):
            self._set_message(
                f"Sharing sessions isn't supported for the {backend.name} backend yet — "
                "it has no portable transcript to share.",
                seconds=10.0,
            )
            self._render()
            return
        session_id = self.state.backend_session_id
        if not session_id or not backend.session_belongs_to_repo(self.repo.repo, session_id):
            self._set_message("No resumable session for this repo to share yet.")
            self._render()
            return
        # Informed consent before EVERY manual share — sharing is opt-in and never
        # automatic, and each push uploads a fresh, possibly sensitive transcript, so
        # the warning must appear every time, not just once. The first time it spells
        # out exactly what is uploaded; afterwards a concise reminder — but the share
        # never proceeds without an explicit "Yes".
        if not self.global_config.session_sharing_acknowledged:
            prompt = (
                "Share this conversation with collaborators?\n"
                "The conversation transcript is pushed to 'origin' and shared with the team.\n"
                "It can contain file contents, command output, and secrets — review what's in\n"
                "this session before sharing. Only this repo's sessions are ever uploaded."
            )
        else:
            prompt = (
                "Share this conversation now?\n"
                "Its transcript — which can contain file contents, command output, and secrets —\n"
                "will be pushed to 'origin'. Review what's in this session before sharing."
            )
        choice = self._select_popup(prompt, ["Yes, share it", "No, cancel"])
        if choice != "Yes, share it":
            self._set_message("Sharing cancelled.")
            self._render()
            return
        self.global_config.acknowledge_session_sharing()
        payload = self._share_payload(session_id)
        if payload is None:
            self._set_message("Could not read the session transcript to share.")
            self._render()
            return
        display = payload["display"]
        store = self._shared_store()

        def op():
            return store.publish(
                github_id=payload["owner"],
                name=payload["name"],
                transcript=payload["redacted"],
                manifest=payload["manifest"],
                prune_gid=payload["sharer"],
                timeout=self.SHARE_PUSH_TIMEOUT,
            )

        def outcome(box) -> str | None:
            if "error" in box:
                return f"Could not share session: {box['error']}"
            result = box["result"]
            if result.behind:
                # The shared copy already has newer turns than this session, so the push
                # was refused. Don't just give up — stash it so the main loop can ask the
                # user whether to overwrite or merge (can't prompt here: we're mid-iteration
                # over the background-op list). Leave the auto-share hash/origin untouched
                # since nothing was published yet.
                self._pending_share_conflicts.append(
                    {"payload": payload, "store": store, "display": display, "session_id": session_id}
                )
                return None
            self._auto_share_hash[session_id] = payload["digest"]  # don't immediately re-push the same content
            # Record the lineage origin so a later re-share (here or on another machine)
            # updates this same entry and keeps accumulating contributors.
            self._user_state().set_shared_origin(
                session_id, owner=payload["owner"], name=payload["name"], contributors=payload["contributors"]
            )
            return self._share_outcome_message(result, display)

        # The push to origin runs in the BACKGROUND so the terminal never freezes; its
        # result lands as a notice. Only the exit-path share blocks (it must finish
        # before the process quits, since daemon threads die with it).
        self._run_share_op_async(f"share:{display}", f"Sharing '{display}' — pushing to origin…", op, outcome)
        # Offer to keep it current automatically. This is a quick interactive prompt
        # (not a network wait), shown while the push proceeds in the background.
        if not self._session_auto_shared(session_id):
            keep = self._select_popup(
                "Keep this shared session up to date automatically?\n"
                "New turns will be pushed to the shared copy as the conversation grows.",
                ["Yes, keep it updated", "No, I'll re-share manually"],
            )
            if keep == "Yes, keep it updated":
                self._set_session_auto_share(session_id, True)
                self._set_message(
                    f"'{display}' will auto-update as you work. Manage it via session → Manage shared.",
                    seconds=8.0,
                )
                self._render()

    def _share_payload(self, session_id: str):
        """Read + redact the session transcript and build its manifest (no network,
        no UI). Returns a dict with the lineage identity (``owner``/``name``/
        ``contributors``/``display``), the actual ``sharer``, ``redacted`` text,
        ``digest``, and ``manifest`` — or None when the transcript can't be read."""
        backend = self.backend
        raw = backend.export_session_raw(self.repo.repo, session_id) or backend.export_session_raw(
            self.base_repo.repo, session_id
        )
        if not raw:
            return None
        from agitrack.sessions import github_login, redact_transcript

        redacted = redact_transcript(raw)
        digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
        login = self.global_config.github_login or github_login(self.base_repo)
        self.global_config.github_login = login
        owner, name, contributors = self._share_identity(session_id, login)
        manifest = {
            "github_id": owner,  # the lineage origin owner = the entry's ref path owner
            "name": name,
            "contributors": contributors,  # every github id that has shared this session
            "backend": backend.name,
            "model": self.state.model,
            "session_id": session_id,
            "agitrack_session_id": self.state.session_id,
            "updated": int(time.time()),
            "content_hash": digest,
            "transcript_bytes": backend.transcript_size(self.base_repo.repo, session_id),
            "transcript_rows": _shared_transcript_rows(redacted),
        }
        return {
            "owner": owner,
            "name": name,
            "contributors": contributors,
            "sharer": login,
            "display": f"{'+'.join(contributors)}/{name}",
            "redacted": redacted,
            "digest": digest,
            "manifest": manifest,
        }

    def _share_outcome_message(self, result, display: str) -> str:
        if result.behind:
            # The shared copy already has newer turns than this machine's copy —
            # sharing would rewind it. Tell the user in plain language (no git jargon).
            return (
                f"Didn't share '{display}': the shared copy already has newer changes than "
                f"this session. Resume the shared version first to catch up, then share again."
            )
        if not result.remote:
            message = f"Saved shared session '{display}' locally (no 'origin' remote to push to)."
        elif result.pushed:
            # The ref isn't a branch, so GitHub's web UI won't show it; point the
            # user at how to see/confirm it instead.
            message = (
                f"Shared '{display}' to origin (it lives on the custom ref refs/agitrack/shared-sessions, "
                f"so it won't show on GitHub's web page). See it via session → Manage shared sessions, "
                f"or: git ls-remote origin 'refs/agitrack/*'."
            )
        else:
            message = (
                f"Saved '{display}' locally, but the push was rejected — someone else may have "
                f"updated the shared ref. Try sharing again. [{result.error[:80]}]"
            )
        if getattr(result, "merged", 0):
            # A concurrent contributor's diverged copy was folded in rather than
            # overwritten, so neither side's turns were lost.
            message += f" Merged {result.merged} turn(s) shared by a collaborator."
        if result.pruned:
            message += f" Pruned {result.pruned} older shared session(s)."
        return message

    def _manage_shared_sessions_menu(self) -> str:
        # Fetch the remote FIRST so the list reflects what's actually shared on origin — a
        # session shared from another machine becomes manageable here, and one removed
        # elsewhere drops out, instead of trusting a possibly-stale local/mirror view (which is
        # what let a local-only session that never reached origin masquerade as "shared"). The
        # fetch is cancelable and bounded; with no remote it's an instant local call. The list
        # itself is still labelled from cheap data only (manifest + a stat-sized "newer?"
        # check), never a transcript read/redact.
        store = self._shared_store()
        login = self.global_config.github_login or self._cached_or_resolve_login()
        if not self._fetch_shared_with_cancel(store, "Fetching shared sessions from origin…"):
            return self._MENU_UP  # the user stopped the fetch — back to the sessions menu
        while True:
            mine = [entry for entry in store.entries() if entry.github_id == login]
            if not mine:
                # _MENU_DONE (not _UP) so this message is VISIBLE on the agent screen: returning
                # _UP re-shows the sessions menu straight over it, which reads as "nothing shows".
                self._set_message("No sessions are shared in this repo. Use session → Share this session to share one.")
                self._render()
                return self._MENU_DONE
            auto_state = self._user_state()  # base-repo opt-in, read once for the whole list
            options: list[str] = []
            for entry in mine:
                sid = entry.manifest.get("session_id", "")
                status = self._shared_entry_status(entry, sid)
                auto = " · auto-update on" if auto_state.auto_share_enabled(sid) else ""
                age = self._format_age(entry.manifest["updated"]) if entry.manifest.get("updated") else ""
                options.append(f"{entry.display}  ({age}{auto}) — {status}")
            choice = self._select_popup("Your shared sessions — pick one to manage", options)
            if choice is None:  # Esc → up one level (back to the sessions menu)
                return self._MENU_UP
            # An action that kicks off a background network op (update/unshare/auto-on) returns
            # _MENU_DONE so the whole menu closes and the user can WATCH its progress notice
            # ("Unsharing…" → "Unshared") on the live screen — instead of the menu re-showing
            # over it. Backing out of an entry (_MENU_UP) re-shows this list.
            if self._manage_one_shared_session(mine[options.index(choice)]) == self._MENU_DONE:
                return self._MENU_DONE

    def _shared_entry_status(self, entry, session_id: str) -> str:
        # Cheap "is the shared copy current?" — compare the transcript's byte size
        # (a stat) to the size recorded when it was shared. No read, no redact.
        shared_bytes = entry.manifest.get("transcript_bytes")
        current = self.backend.transcript_size(self.base_repo.repo, session_id) if session_id else None
        if not shared_bytes or current is None:
            return "shared"
        if current > shared_bytes:
            return "local has newer turns — Update to push them"
        return "shared (up to date)"

    def _manage_one_shared_session(self, entry) -> str:
        # Returns _MENU_DONE to close the whole menu (so a background op's progress notice is
        # visible on the live screen), or _MENU_UP to re-show the shared-sessions list.
        sid = entry.manifest.get("session_id", "")
        auto_on = self._session_auto_shared(sid)
        actions = [
            ("update", "↻ Update now (push latest turns)"),
            ("auto", ("✓ Auto-update is ON — turn it off" if auto_on else "○ Turn ON auto-update")),
            ("unshare", "✗ Unshare (remove for everyone)"),
        ]
        choice = self._select_popup(f"Manage {entry.display}", [label for _, label in actions])
        if choice is None:
            return self._MENU_UP  # Esc backs out to the list
        kind = actions[[label for _, label in actions].index(choice)][0]
        if kind == "update":
            self._update_shared_entry(entry)
            return self._MENU_DONE  # close the menu so the "Updating…" → result notice shows
        if kind == "auto":
            self._set_session_auto_share(sid, not auto_on)
            if auto_on:
                self._set_message(f"Auto-update disabled for {entry.display}.")
                self._render()
            else:
                # Enabling syncs once right away, so the shared copy is current
                # immediately instead of only on the next commit.
                self._set_message(f"Auto-update on for {entry.display} — pushing the latest now…", seconds=10.0)
                self._render()
                self._update_shared_entry(entry)
            return self._MENU_DONE  # close the menu so the message/push progress is visible
        # Unsharing removes the session from origin for every collaborator and can't be undone,
        # so confirm before doing it (mirrors the discard-confirm flow).
        confirm = self._select_popup(
            f"Unshare '{entry.display}'? This removes it from origin for everyone and can't be undone.",
            ["No, keep it", "Yes, unshare"],
        )
        if confirm == "Yes, unshare":
            self._unshare_entry(entry)
            return self._MENU_DONE  # close the menu so "Unsharing…" → "Unshared" is visible
        self._set_message("Kept the shared session.")
        self._render()
        return self._MENU_UP  # cancelled — back to the list

    def _update_shared_entry(self, entry) -> None:
        sid = entry.manifest.get("session_id", "")
        raw = self.backend.export_session_raw(self.base_repo.repo, sid) if sid else None
        if not raw:
            self._set_message(f"Can't read the transcript for {entry.display} to update it.")
            self._render()
            return
        from agitrack.sessions import redact_transcript

        redacted = redact_transcript(raw)
        # Updating from the Manage menu counts as a (re-)share by the current user, so
        # fold them into the contributor set — the entry stays under its origin owner.
        login = self._cached_or_resolve_login()
        contributors = sorted({*entry.contributors, login})
        manifest = {
            **entry.manifest,
            "contributors": contributors,
            "updated": int(time.time()),
            "content_hash": hashlib.sha256(redacted.encode("utf-8")).hexdigest(),
            "transcript_bytes": self.backend.transcript_size(self.base_repo.repo, sid),
            "transcript_rows": _shared_transcript_rows(redacted),
        }
        display = f"{'+'.join(contributors)}/{entry.name}"
        store = self._shared_store()

        def op():
            return store.publish(
                github_id=entry.github_id,
                name=entry.name,
                transcript=redacted,
                manifest=manifest,
                prune_gid=login,
                timeout=self.SHARE_PUSH_TIMEOUT,
            )

        def outcome(box) -> str:
            if "error" in box:
                return f"Could not update {display}: {box['error']}"
            result = box["result"]
            if sid:
                self._auto_share_hash[sid] = manifest["content_hash"]
            return self._share_outcome_message(result, display)

        # Push in the BACKGROUND (same as the initial share) so the terminal never
        # freezes; a progress notice shows now, the result lands when it finishes.
        self._run_share_op_async(f"update:{display}", f"Updating '{display}' — pushing to origin…", op, outcome)

    def _unshare_entry(self, entry) -> None:
        # Unsharing is a one-way, fire-and-forget network op with no follow-up, and the
        # user shouldn't have to wait on it — run it in the BACKGROUND so the session
        # never freezes, showing a progress notice now and the result when it lands.
        store = self._shared_store()
        sid = entry.manifest.get("session_id", "")
        if sid:
            # Stop auto-pushing it immediately — across the WHOLE id lineage, not just this
            # entry's id. The backend mints a new id on resume, so auto-share may be opted in
            # under a drifted id; clearing only `sid` left it enabled (the menu kept showing it
            # as shared/auto and it kept re-pushing). _session_auto_shared reads the lineage too.
            for lineage_sid in {sid, *self._user_state().session_lineage(sid)}:
                self._set_session_auto_share(lineage_sid, False)

        def op():
            return store.unshare(entry.github_id, entry.name, timeout=self.SHARE_PUSH_TIMEOUT)

        def outcome(box) -> str:
            if "error" in box:
                return f"Could not unshare {entry.display}: {box['error']}"
            result = box["result"]
            if not result.remote:
                return f"Removed {entry.display} from the local shared ref (no remote to push the removal to)."
            if result.pushed:
                return f"Unshared {entry.display} (removed from origin)."
            if not result.error:
                # Nothing was rejected: the entry wasn't on origin (its share never reached it,
                # or it was already removed there). It's gone from your list either way.
                return f"Removed {entry.display} — it wasn't on origin (nothing to push)."
            # Removed locally but the origin push was rejected even after the auto-retry — show
            # the REASON git gave (not a blind prefix slice) so the user can tell a transient
            # race from a permission/protected-ref/hook rejection they must fix on the remote.
            return (
                f"Removed {entry.display} locally, but origin rejected the push — re-run unshare "
                f"from the menu to retry. Reason: {_push_rejection_reason(result.error)}"
            )

        self._run_share_op_async(
            f"unshare:{entry.display}", f"Unsharing {entry.display} — removing from origin…", op, outcome
        )

    def _run_share_op_async(self, key: str, pending_text: str, op, outcome_fn) -> None:
        """Run a best-effort network share op (e.g. unshare) on a daemon thread so the
        session never freezes. Shows *pending_text* as a notice now; a later main-loop
        tick (_service_background_share_ops) replaces it with ``outcome_fn(box)`` once
        the worker finishes, where ``box`` is ``{"result": ...}`` or ``{"error": str}``.
        The user keeps working throughout."""
        self._set_session_notice(key, pending_text, seconds=180.0)
        self._render()
        box: dict = {}

        def worker() -> None:
            try:
                box["result"] = op()
            except Exception as error:
                box["error"] = str(error)

        thread = threading.Thread(target=worker, daemon=True, name="agit-share-op")
        thread.start()
        self._background_share_ops.append({"key": key, "thread": thread, "box": box, "outcome_fn": outcome_fn})

    def _service_background_share_ops(self) -> None:
        """Main-loop tick: surface each finished background share op's result as a
        notice (replacing its in-progress one), and drop it from the pending list."""
        if not self._background_share_ops:
            return
        still: list[dict] = []
        for entry in self._background_share_ops:
            if entry["thread"].is_alive():
                still.append(entry)
                continue
            try:
                text = entry["outcome_fn"](entry["box"])
            except Exception as error:
                self._debug(f"background share op outcome failed: {error!r}")
                text = None
            if text:
                self._set_session_notice(entry["key"], text, seconds=10.0)
        self._background_share_ops = still

    def _service_share_conflicts(self) -> None:
        """Main-loop tick: for each share refused because the shared copy was already
        newer, ask the user whether to overwrite or merge, then re-share their way. Run
        AFTER _service_background_share_ops (not inside it) so opening a modal and queuing
        a new background op doesn't mutate the list that method is iterating."""
        if not self._pending_share_conflicts:
            return
        pending, self._pending_share_conflicts = self._pending_share_conflicts, []
        for conflict in pending:
            self._resolve_share_behind(conflict)

    def _resolve_share_behind(self, conflict: dict) -> None:
        """Prompt the user about one behind-refused share — the shared copy already has
        newer changes — and, if they choose, overwrite it with this session.

        Only *overwrite* (or keep-as-is) is offered: when two copies of a session CAN be
        combined they're union-merged automatically during a normal publish and never
        reach this refusal, so the only sessions that land here are ones that can't be
        merged (e.g. OpenCode's single-object export). For those, replace-or-keep is the
        real choice."""
        payload, store, display, session_id = (
            conflict["payload"],
            conflict["store"],
            conflict["display"],
            conflict["session_id"],
        )
        overwrite_opt = "Overwrite the shared copy with this session"
        choice = self._select_popup(
            f"The shared copy of '{display}' already has newer changes than this session.\n"
            "Sharing would replace those newer changes with this older one. Proceed?",
            [overwrite_opt, "Keep the newer shared copy (cancel)"],
        )
        if choice != overwrite_opt:
            self._set_message(f"Didn't share '{display}' — left the newer shared copy as is.")
            self._render()
            return

        def op():
            return store.publish(
                github_id=payload["owner"],
                name=payload["name"],
                transcript=payload["redacted"],
                manifest=payload["manifest"],
                prune_gid=payload["sharer"],
                timeout=self.SHARE_PUSH_TIMEOUT,
                overwrite=True,
            )

        def outcome(box) -> str | None:
            if "error" in box:
                return f"Could not share session: {box['error']}"
            result = box["result"]
            self._auto_share_hash[session_id] = payload["digest"]
            self._user_state().set_shared_origin(
                session_id, owner=payload["owner"], name=payload["name"], contributors=payload["contributors"]
            )
            if not result.pushed:
                return self._share_outcome_message(result, display)
            return f"Overwrote the shared copy of '{display}' on origin with this session."

        self._run_share_op_async(f"share:{display}", f"Overwriting '{display}' — pushing to origin…", op, outcome)

    def _session_auto_shared(self, session_id: str | None) -> bool:
        # Read the opt-in from the BASE repo state (persists across runs); the
        # session worktree where self.state lives is removed on exit (#55). Check
        # the whole id lineage: the backend mints a new id on resume, so a session
        # opted in under an earlier id must still count after that drift.
        if not session_id:
            return False
        user = self._user_state()
        return any(user.auto_share_enabled(sid) for sid in user.session_lineage(session_id))

    def _session_is_shared(self, session_id: str | None, shared_ids: set[str]) -> bool:
        # A session counts as shared if its current id, or any id it drifted from
        # on resume, is in the shared set (#55) — so a resumed shared session is
        # still recognised after the backend forks its id.
        if not session_id:
            return False
        if session_id in shared_ids:
            return True
        return any(sid in shared_ids for sid in self._user_state().session_lineage(session_id))

    def _my_shared_session_ids(self) -> set[str]:
        # session_ids of conversations I've shared in this repo, from the LOCAL ref
        # (no network) — used to mark shared sessions in the session menu.
        if not getattr(self.backend, "supports_session_sharing", False):
            return set()
        try:
            login = self.global_config.github_login
            ids = set()
            for entry in self._shared_store().entries():
                if login and entry.github_id != login:
                    continue
                sid = entry.manifest.get("session_id")
                if sid:
                    ids.add(sid)
            return ids
        except Exception:
            return set()

    def _record_shared_alias_on_drift(self, previous: str | None, new_id: str | None) -> None:
        # When the backend forks a new session id on resume, link new→previous so a
        # session shared/auto-shared under the previous id stays recognised. Only
        # record it for ids that actually belong to a shared lineage, to keep the
        # alias map scoped (and avoid recording drift for unshared sessions) (#55).
        if not previous or not new_id or previous == new_id:
            return
        try:
            user = self._user_state()
            relevant = (
                user.auto_share_enabled(previous)
                or previous in user.shared_session_aliases()
                or previous in self._my_shared_session_ids()
            )
            if relevant:
                user.add_shared_session_alias(new_id, previous)
            # Carry the full lineage origin (owner + name + contributors) onto the new
            # id, so a re-share after the backend forks the id still updates the same
            # shared entry and keeps the contributor set (#55).
            origin = user.shared_origin(previous)
            if origin:
                user.set_shared_origin(
                    new_id, owner=origin["owner"], name=origin["name"], contributors=origin["contributors"]
                )
        except Exception as error:
            self._debug(f"record shared alias failed: {error!r}")

    def _set_session_auto_share(self, session_id: str, enabled: bool) -> None:
        self._user_state().set_auto_share(session_id, bool(enabled))

    def _cached_or_resolve_login(self) -> str:
        # Resolve and cache the GitHub login. Only writes config when it actually
        # changes, so callers on a hot path (auto-share) don't re-save every time.
        cached = self.global_config.github_login
        if cached:
            return cached
        from agitrack.sessions import github_login

        login = github_login(self.base_repo)
        self.global_config.github_login = login
        return login

    # --- auto-share: keep an opted-in session's shared copy current ---------

    def _maybe_auto_share_active(self) -> None:
        # Called when a commit lands (see _announce_agent_commit), so the GitHub
        # round-trip happens at the commit cadence — not on a frequent timer.
        # Reactor-thread part: only cheap checks, then hand ALL the heavy work
        # (read transcript, redact, hash, push) to a background thread, so the UI
        # loop never blocks. The in-flight guard + the worker's content-hash gate
        # keep it from pushing redundantly on rapid commits.
        backend = self.backend
        if not getattr(backend, "supports_session_sharing", False):
            return
        sid = self.state.backend_session_id
        if not sid or not self._session_auto_shared(sid):
            return
        if self._auto_share_thread is not None and self._auto_share_thread.is_alive():
            return
        # Snapshot everything the worker needs on the main thread (these touch the
        # active session / config, which can change underneath a thread).
        login = self._cached_or_resolve_login()
        owner, name, contributors = self._share_identity(sid, login)
        ctx = {
            "session_id": sid,
            "owner": owner,
            "name": name,
            "contributors": contributors,
            "login": login,
            "backend": backend,
            "repo_path": self.repo.repo,
            "base_repo_path": self.base_repo.repo,
            "model": self.state.model,
            "agitrack_session_id": self.state.session_id,
            "store": self._shared_store(),
            "last_hash": self._auto_share_hash.get(sid),
        }
        self._auto_share_thread = threading.Thread(target=self._auto_share_worker, args=(ctx,), daemon=True)
        self._auto_share_thread.start()

    def _auto_share_worker(self, ctx: dict):
        # Runs off the reactor thread; best-effort. Reads and redacts the (possibly large)
        # transcript and pushes here, not on the loop. Returns the PublishResult on a push,
        # or None when it skipped (no transcript / unchanged) or hit an error — the exit path
        # inspects it. Records the outcome for the main loop to surface ONLY on failure/behind
        # (success is silent: this fires on every commit). See _service_auto_share_outcome.
        sid = ctx["session_id"]
        try:
            backend = ctx["backend"]
            raw = backend.export_session_raw(ctx["repo_path"], sid) or backend.export_session_raw(
                ctx["base_repo_path"], sid
            )
            if not raw:
                return None
            from agitrack.sessions import redact_transcript

            redacted = redact_transcript(raw)
            digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
            if digest == ctx["last_hash"]:
                return None  # nothing new since the last push — skip the network round-trip
            manifest = {
                "github_id": ctx["owner"],  # lineage origin owner (entry's ref path owner)
                "name": ctx["name"],
                "contributors": ctx["contributors"],
                "backend": backend.name,
                "model": ctx["model"],
                "session_id": sid,
                "agitrack_session_id": ctx["agitrack_session_id"],
                "updated": int(time.time()),
                "content_hash": digest,
                "transcript_bytes": backend.transcript_size(ctx["base_repo_path"], sid),
                "transcript_rows": _shared_transcript_rows(redacted),
            }
            result = ctx["store"].publish(
                github_id=ctx["owner"],
                name=ctx["name"],
                transcript=redacted,
                manifest=manifest,
                prune_gid=ctx["login"],
                # Bound the push: a stalled remote must not strand this worker, because the
                # in-flight guard (_auto_share_thread.is_alive()) would then block EVERY future
                # auto-share for the run — the session would silently stop updating (the bug).
                timeout=self.SHARE_PUSH_TIMEOUT,
            )
            # Cache the digest ONLY after a real success, so a failed/refused push retries on
            # the next commit instead of being silently marked "already shared" (which left the
            # shared copy days stale with no error). A remote-less repo only has the local ref,
            # so a successful local write counts as shared.
            if result.pushed or not result.remote:
                self._auto_share_hash[sid] = digest
                self._auto_share_outcome = {"ok": True}
            elif result.behind:
                self._auto_share_outcome = {"behind": True, "name": ctx["name"]}
            else:
                self._auto_share_outcome = {"failed": result.error or "push rejected", "name": ctx["name"]}
            return result
        except Exception as error:
            self._debug(f"auto-share failed: {error!r}")
            self._auto_share_outcome = {"failed": str(error), "name": ctx["name"]}
            return None

    def _service_auto_share_outcome(self) -> None:
        """Main-loop tick: surface the live auto-share worker's result. Only FAILURE and
        'behind' are shown (success is silent — auto-share fires on every commit), so a push
        that keeps failing becomes visible instead of leaving the shared copy quietly stale."""
        outcome = self._auto_share_outcome
        if outcome is None:
            return
        self._auto_share_outcome = None
        name = outcome.get("name", "this session")
        if "failed" in outcome:
            self._set_session_notice(
                "auto-share",
                f"Auto-share for {name} failed: {str(outcome['failed'])[:120]} — it will retry on the next commit.",
                seconds=12.0,
            )
            self._render()
        elif outcome.get("behind"):
            self._set_session_notice(
                "auto-share",
                f"Auto-share for {name} skipped — the shared copy already has newer turns than this machine.",
                seconds=10.0,
            )
            self._render()

    def _auto_share_on_exit(self) -> None:
        # Exit-path counterpart to _maybe_auto_share_active. The live auto-share
        # runs in a daemon thread fired on commit; quitting right after a turn
        # (before that thread is scheduled, or while it is still pushing) would
        # leave the final conversation unshared, since daemon threads are killed
        # when the process exits. So push the active session's latest transcript
        # here — but ONLY when it actually changed since the last share, and ALWAYS
        # bounded so a stalled network can never hang exit.
        backend = self.backend
        if not getattr(backend, "supports_session_sharing", False):
            return
        sid = self.state.backend_session_id
        if not sid or not self._session_auto_shared(sid):
            return
        # Nothing happened this run ⇒ nothing to share. This is the ground-truth
        # gate: it skips a session that was only resumed and never typed into, so
        # exit stays instant with no "Sharing…" message. It is robust where a
        # transcript-digest comparison is not — Claude forks a new session id on
        # resume and rewrites every transcript row, so the digest changes across
        # runs even when the user did nothing. A turn this run, by contrast, always
        # routes through on_commit_fn, which records the activity.
        if self.state.session_id not in self._sessions_with_activity:
            return
        # Let a still-running live auto-share finish first, so we don't race it and
        # so its updated content hash is visible to the change check below.
        if self._auto_share_thread is not None and self._auto_share_thread.is_alive():
            self._auto_share_thread.join(timeout=self.EXIT_SHARE_TIMEOUT)
        # Among sessions that DID see a turn, still avoid a redundant push: compare
        # the current transcript digest against this run's last live-pushed hash,
        # then the already-published manifest hash.
        digest = self._exit_share_digest(backend, sid)
        if digest is None:
            return  # transcript unreadable ⇒ nothing to share
        last = self._auto_share_hash.get(sid) or self._published_content_hash(sid)
        if digest == last:
            return  # already shared this exact content ⇒ nothing to do
        ctx = {
            "session_id": sid,
            # owner/name/contributors are resolved inside the bounded thread, after the
            # login lookup (a gh call can stall) it depends on.
            "login": None,
            "backend": backend,
            "repo_path": self.repo.repo,
            "base_repo_path": self.base_repo.repo,
            "model": self.state.model,
            "agitrack_session_id": self.state.session_id,
            "store": self._shared_store(),
            "last_hash": last,
        }
        self._set_message("Sharing this session before exit…", seconds=30)
        self._render()
        # Bound the network round-trip: run the push (and the login lookup, which
        # may shell out to gh) in a thread and wait at most EXIT_SHARE_TIMEOUT. A
        # stalled push (offline, auth, unreachable remote) can never block exit —
        # git ref updates are atomic, so an abandoned push simply doesn't land. On
        # timeout or push failure, warn and continue.
        outcome: dict = {}

        def push() -> None:
            login = self._cached_or_resolve_login()
            owner, name, contributors = self._share_identity(sid, login)
            ctx.update(login=login, owner=owner, name=name, contributors=contributors)
            outcome["result"] = self._auto_share_worker(ctx)

        thread = threading.Thread(target=push, daemon=True, name="agit-exit-share")
        thread.start()
        thread.join(timeout=self.EXIT_SHARE_TIMEOUT)
        if thread.is_alive():
            self._set_message("Couldn't share this session before exit (timed out); continuing.", seconds=6.0)
            self._render()
            return
        result = outcome.get("result")
        if result is not None and result.remote and not result.pushed:
            self._set_message("Couldn't share this session before exit (push failed); continuing.", seconds=6.0)
            self._render()

    def _exit_share_digest(self, backend, sid: str) -> str | None:
        # The redacted-transcript digest for *sid*, matching the worker's gate, so
        # the exit path can tell whether the latest conversation differs from what
        # was last shared. None when the transcript can't be read.
        try:
            raw = backend.export_session_raw(self.repo.repo, sid) or backend.export_session_raw(
                self.base_repo.repo, sid
            )
            if not raw:
                return None
            from agitrack.sessions import redact_transcript

            return hashlib.sha256(redact_transcript(raw).encode("utf-8")).hexdigest()
        except Exception as error:
            self._debug(f"exit share digest failed: {error!r}")
            return None

    def _published_content_hash(self, sid: str) -> str | None:
        # The content_hash of this session's already-published shared entry, read
        # from the LOCAL shared ref (no network), so the exit gate can tell an
        # unedited resumed session from one with genuinely new turns. Matched by
        # session id across resume drift (lineage-aware).
        try:
            lineage = set(self._user_state().session_lineage(sid))
            for entry in self._shared_store().entries():
                if entry.manifest.get("session_id") in lineage:
                    return entry.manifest.get("content_hash")
        except Exception as error:
            self._debug(f"published content hash lookup failed: {error!r}")
        return None

    def _fetch_shared_with_cancel(self, store, message: str) -> bool:
        """Fetch the shared-session ref while keeping the UI alive and letting the
        user press Esc to stop — needed when the fetch stalls on bad internet.
        Returns True if the fetch finished, False if the user stopped it or it timed
        out. Either way the LOCAL ref is left usable (possibly stale) for listing.

        No remote ⇒ nothing to fetch over the network: do the cheap local call
        inline (this also keeps headless/test runs off the interactive wait path)."""
        if not store.repo.remote_exists():
            store.fetch()
            return True
        result: dict = {}
        cancel = threading.Event()

        def worker() -> None:
            try:
                # Bound the git fetch and make it killable, so a stopped fetch's
                # subprocess is terminated at once — never left running.
                result["ok"] = store.fetch(timeout=self.SHARED_FETCH_TIMEOUT, cancel=cancel)
            except Exception as error:  # never let a fetch failure escape the thread
                result["error"] = repr(error)

        thread = threading.Thread(target=worker, daemon=True, name="agit-shared-fetch")
        thread.start()
        self._set_message(f"{message}   ·   press Esc to stop", seconds=600)
        self._render()
        status = self._drain_pty_until_done_or_esc(thread, deadline=time.monotonic() + self.SHARED_FETCH_TIMEOUT + 2.0)
        if status != "done":
            cancel.set()  # kill the git fetch subprocess now — don't leave it running
            note = "timed out" if status == "timeout" else "stopped"
            self._set_message(f"Stopped fetching shared sessions ({note}).", seconds=6.0)
            self._render()
            return False
        if result.get("error"):
            self._debug(f"shared fetch failed: {result['error']}")
        return True

    def _drain_pty_until_done_or_esc(self, thread, *, deadline: float | None = None) -> str:
        """Wait for *thread* while keeping the UI alive (PTYs draining) so the wait
        is responsive, not a freeze, and the user can press Esc to stop.
        Returns ``"done"`` when the thread finishes, ``"cancel"`` on Esc, or
        ``"timeout"`` if the optional *deadline* passes first. Shared by the two
        cancellable shared-session fetches (listing and full-transcript)."""
        thread.join(timeout=0.05)  # fast fetches (and tests) finish without the wait UI
        if not thread.is_alive():
            return "done"
        try:
            stdin_fd = self._stdin_fileno()
        except (OSError, ValueError):
            # No real stdin (headless/non-interactive): can't offer interactive
            # cancel, so just wait for the thread, still honouring the deadline.
            while thread.is_alive():
                if deadline is not None and time.monotonic() > deadline:
                    return "timeout"
                thread.join(timeout=0.2)
            return "done"
        while thread.is_alive():
            if deadline is not None and time.monotonic() > deadline:
                return "timeout"
            master = self.master_fd
            background = self._background_fds() if self.sessions else {}
            fds = [stdin_fd]
            if master is not None:
                fds.append(master)
            fds.extend(background)
            try:
                readable, _, _ = select.select(fds, [], [], 0.2)
            except (OSError, ValueError):
                # stdin/PTY not selectable (headless): just wait for the thread.
                thread.join(timeout=0.2)
                continue
            for fd in readable:
                if fd == stdin_fd:
                    if self._stdin_has_cancel(self._read_stdin(32)):
                        return "cancel"
                elif fd == master:
                    output = self._drain_child_output()
                    if output is not None:
                        self.last_child_output = time.monotonic()
                        self._feed_child_output(output)
                elif fd in background:
                    self._pump_background(background[fd])
        return "done"

    @staticmethod
    def _stdin_has_cancel(data: bytes) -> bool:
        """Whether *data* is a genuine cancel keystroke — a lone Esc or Ctrl-C — as
        opposed to an escape SEQUENCE (mouse report, focus event, arrow key, bracketed
        paste), every one of which also begins with ESC. With host mouse reporting on,
        a mere mouse move emits ``\\x1b[<…`` and must NOT be read as the user pressing
        Esc, or a fetch is cancelled the instant the pointer moves."""
        if b"\x03" in data:  # Ctrl-C
            return True
        return data == b"\x1b"  # a bare Esc, not the lead byte of a longer sequence

    @staticmethod
    def _is_real_keypress(data: bytes) -> bool:
        """Whether *data* carries an actual keystroke rather than only terminal-emitted
        escape sequences (mouse reports, focus in/out). Lets a 'press any key' notice
        ignore an incidental mouse move while host mouse reporting is on."""
        stripped = _SGR_MOUSE_RE.sub(b"", data)
        stripped = _X10_MOUSE_RE.sub(b"", stripped)
        stripped = _FOCUS_EVENT_RE.sub(b"", stripped)
        return bool(stripped)

    def _shared_is_older_than_local(self, entry, agent, session_id: str) -> bool:
        """Whether the shared copy of ``entry`` has FEWER turns than the local copy of
        ``session_id`` — i.e. resuming it would hand back an older conversation than the
        user already has. Compares the manifest's recorded row count (cheap, no blob
        read) against the local transcript's. Returns False when either is unknown (an
        older manifest without the field, or no local transcript), so we never warn on a
        guess."""
        from agitrack.sessions import count_transcript_rows

        shared_rows = entry.manifest.get("transcript_rows")
        if not isinstance(shared_rows, int):
            return False
        raw = agent.export_session_raw(self.base_repo.repo, session_id)
        if not raw:
            return False
        return shared_rows < count_transcript_rows(raw)

    def _resume_shared_session_menu(self) -> str:
        store = self._shared_store()
        completed = self._fetch_shared_with_cancel(store, "Fetching shared sessions…")
        if not completed:
            # The user stopped the fetch: leave the menu entirely rather than
            # dropping them into a possibly-stale, previously-fetched list (which
            # would read as if the stop did nothing). _fetch_shared_with_cancel has
            # already shown the "Stopped fetching…" notice; let it linger.
            return self._MENU_UP
        entries = store.entries()
        if not entries:
            self._set_message("No shared sessions found for this repo.")
            self._render()
            return self._MENU_UP
        options: list[str] = []
        for entry in entries:
            extra = [str(entry.manifest[k]) for k in ("model",) if entry.manifest.get(k)]
            if entry.manifest.get("updated"):
                extra.append(self._format_age(entry.manifest["updated"]))
            options.append(entry.display + (f"  ({' · '.join(extra)})" if extra else ""))
        choice = self._select_popup("Resume a shared session (newest first)", options)
        if choice is None:  # Esc → up one level to the sessions menu
            return self._MENU_UP
        entry = entries[options.index(choice)]
        session_id = entry.manifest.get("session_id")
        if not session_id:
            self._set_message("That shared session is incomplete; cannot resume it.")
            self._render()
            return self._MENU_UP
        # Resume with the backend the session was recorded by, not necessarily the
        # active one — a shared OpenCode session must be imported/resumed by the
        # OpenCode agent even while Claude is active (and vice versa). Reuse the
        # active agent when it already matches; only build a fresh one to cross
        # backends.
        entry_backend = entry.manifest.get("backend") or self.backend.name
        if entry_backend == self.backend.name:
            agent = self.backend
        else:
            try:
                agent = make_proxy_agent(entry_backend)
            except ValueError:
                self._set_message(f"Can't resume '{entry.display}': unknown backend '{entry_backend}'.", seconds=8.0)
                self._render()
                return self._MENU_UP
        # Remember the lineage origin this session was shared under (owner + name +
        # contributors), so a later re-share (on this or any machine) updates the SAME
        # shared entry and adds us to the contributor set, instead of spawning a new
        # `<sharer>/<name>` that never converges (#55).
        self._user_state().set_shared_origin(
            session_id, owner=entry.github_id, name=entry.name, contributors=entry.contributors
        )
        # Everything below resolves WHAT to do (interactive popups) WITHOUT touching
        # the transcript — the transcript fetch (which may hit the network) and the
        # import then run on a worker thread (_begin_shared_resume) so the UI never
        # freezes, and the resume itself completes on the main loop once ready.
        live_index = next(
            (
                i
                for i, s in enumerate(self.sessions)
                if getattr(getattr(s, "state", None), "backend_session_id", None) == session_id
            ),
            None,
        )
        if live_index is not None:
            # This exact conversation is already running here.
            keep_both_id = getattr(agent, "new_import_id", lambda: None)()
            # Guard against silently downgrading: if the shared copy is OLDER than the
            # running session (it doesn't have your latest turns), lead with keeping
            # the newer one so "update" can't quietly throw away recent work.
            shared_older = self._shared_is_older_than_local(entry, agent, session_id)
            if shared_older:
                header = (
                    f"'{entry.display}' is already running here, and the shared copy is OLDER than "
                    f"your current session — it doesn't include your latest changes. What would you like to do?"
                )
                opts = ["Stay as it is (keep my newer session)"]
                if keep_both_id:
                    opts.append("Keep both — copy the older shared version to a new session")
                opts.append("Update anyway (replace my session with the older shared copy)")
            else:
                header = f"'{entry.display}' is already running here.\nWhat would you like to do?"
                opts = ["Update this session to the shared version"]
                if keep_both_id:
                    opts.append("Keep both — copy the shared version to a new session")
                opts.append("Stay as it is (no change)")
            pick = self._select_popup(header, opts)
            if pick is None:  # Esc → up one level to the sessions menu
                return self._MENU_UP
            if pick.startswith("Stay"):
                self._switch_active(live_index)
                return self._MENU_DONE
            if pick.startswith("Update"):
                # Pull the shared version into the running session: fetch it, then
                # (on the main loop) restart the backend so it loads the new
                # transcript — the agent can't pick up a swapped transcript live.
                self._begin_shared_resume(
                    store,
                    entry,
                    agent,
                    action="update_live",
                    name=None,
                    resume_id=session_id,
                    overwrite=True,
                    as_id=None,
                    backend=entry_backend,
                )
                return self._MENU_DONE
            assert keep_both_id is not None
            copy_name = self._prompt_session_name(
                "Name the copied session", default=self._dedupe_session_name(entry.name)
            )
            if copy_name is None:  # Esc → up one level
                return self._MENU_UP
            self._begin_shared_resume(
                store,
                entry,
                agent,
                action="new",
                name=copy_name,
                resume_id=keep_both_id,
                overwrite=False,
                as_id=keep_both_id,
                backend=entry_backend,
            )
            return self._MENU_DONE
        # You may already have this exact shared session open under a DIFFERENT backend
        # id: a multi-collaborator entry carries the *last sharer's* session_id, not
        # yours, so the id check above misses your own copy and the resume would mint a
        # new, differently-named session (the "session name got lost" report). Match by
        # the shared LINEAGE (origin owner + name) instead and offer to continue your
        # existing session — keeping its name — rather than duplicating it.
        lineage_index = self._live_session_for_lineage(entry.github_id, entry.name)
        if lineage_index is not None:
            local_name = self._session_name(lineage_index)
            keep_both_id = getattr(agent, "new_import_id", lambda: None)()
            opts = [f"Continue my existing '{local_name}' session"]
            if keep_both_id:
                opts.append("Fetch the shared version as a separate copy")
            pick = self._select_popup(
                f"You already have this shared session open locally as '{local_name}'.\nWhat would you like to do?",
                opts,
            )
            if pick is None:  # Esc → up one level to the sessions menu
                return self._MENU_UP
            if pick.startswith("Continue"):
                self._switch_active(lineage_index)
                return self._MENU_DONE
            assert keep_both_id is not None
            copy_name = self._prompt_session_name(
                "Name the copied session", default=self._dedupe_session_name(entry.name)
            )
            if copy_name is None:  # Esc → up one level
                return self._MENU_UP
            self._begin_shared_resume(
                store,
                entry,
                agent,
                action="new",
                name=copy_name,
                resume_id=keep_both_id,
                overwrite=False,
                as_id=keep_both_id,
                backend=entry_backend,
            )
            return self._MENU_DONE
        # Not running locally: pick a clear local name (#71), default to the original
        # share name (deduped) — NOT a "<sharer>-<name>" slug, which grew without
        # bound when sharing back and forth (#55).
        name = self._prompt_session_name("Resume shared session", default=self._dedupe_session_name(entry.name))
        if name is None:  # Esc → up one level to the sessions menu
            return self._MENU_UP
        overwrite, as_id, resume_id = False, None, session_id
        if agent.has_local_session(self.base_repo.repo, session_id):
            age = self._format_age(entry.manifest["updated"]) if entry.manifest.get("updated") else "earlier"
            keep_both_id = getattr(agent, "new_import_id", lambda: None)()
            # If the shared copy is OLDER than the local one, default to keeping the
            # local (newer) copy so the user can't unknowingly replace recent work with
            # a stale shared version (the "much older after resume" report).
            shared_older = self._shared_is_older_than_local(entry, agent, session_id)
            if shared_older:
                header = (
                    f"You already have a local copy of {entry.display}, and it's NEWER than the shared "
                    f"version — the shared copy is missing your latest changes. What do you want to do?"
                )
                opts = ["Keep my local copy (the newer one)"]
                if keep_both_id:
                    opts.append("Keep both (fetch the older shared copy as a separate session)")
                opts.append(f"Replace my local copy with the OLDER shared version (updated {age})")
            else:
                header = f"You already have a local copy of {entry.display}.\nWhich do you want to continue?"
                opts = [f"Replace my local copy with the shared version (updated {age})"]
                if keep_both_id:
                    opts.append("Keep both (fetch the shared copy as a separate session)")
                opts.append("Keep my local copy")
            pick = self._select_popup(header, opts)
            if pick is None:  # Esc → up one level to the sessions menu
                return self._MENU_UP
            if pick.startswith("Keep both"):
                assert keep_both_id is not None
                as_id = resume_id = keep_both_id
            elif pick.startswith("Replace"):
                overwrite = True
            else:  # keep the local copy: resume it directly, no fetch/import needed
                self._resume_conversation(name, session_id, backend=entry_backend)
                return self._MENU_DONE
        self._begin_shared_resume(
            store,
            entry,
            agent,
            action="new",
            name=name,
            resume_id=resume_id,
            overwrite=overwrite,
            as_id=as_id,
            backend=entry_backend,
        )
        return self._MENU_DONE

    def _begin_shared_resume(self, store, entry, agent, *, action, name, resume_id, overwrite, as_id, backend) -> None:
        # Fetch the (possibly large) transcript on a worker thread, then WAIT for it
        # cancellably: the UI keeps draining (no freeze) and the user can press Esc to
        # stop a slow fetch. The import + session switch/restart still happen on the
        # main loop (_service_shared_resume) once the result lands.
        if self._shared_resume_thread is not None and self._shared_resume_thread.is_alive():
            self._set_message("Already fetching a shared session — please wait.")
            self._render()
            return
        session_id = entry.manifest.get("session_id")
        self._shared_resume_result = None
        cancel = threading.Event()
        self._shared_resume_cancel = cancel

        def worker() -> None:
            try:
                # Bound the full fetch (it can be large) and make it killable, so a
                # cancel/exit terminates the git process at once instead of waiting.
                transcript = store.read_transcript(entry, timeout=self.RESUME_FETCH_TIMEOUT, cancel=cancel)
                if cancel.is_set():
                    return  # cancelled (or exiting) while fetching — drop the result
                if not transcript:
                    self._shared_resume_result = {"error": "incomplete"}
                    return
                self._shared_resume_result = {
                    "transcript": transcript,
                    "action": action,
                    "agent": agent,
                    "session_id": session_id,
                    "name": name,
                    "resume_id": resume_id,
                    "overwrite": overwrite,
                    "as_id": as_id,
                    "backend": backend,
                    "entry_name": entry.name,
                    # Lineage of the shared session being copied here, for the origin
                    # note the first commit of the new session records.
                    "origin_contributors": "+".join(entry.contributors),
                }
            except Exception as error:
                if not cancel.is_set():
                    self._shared_resume_result = {"error": repr(error)}

        self._set_message(f"Fetching '{entry.display}'…   press Esc to cancel", seconds=600)
        self._render()
        self._shared_resume_thread = threading.Thread(target=worker, daemon=True, name="agit-shared-resume")
        self._shared_resume_thread.start()
        status = self._drain_pty_until_done_or_esc(
            self._shared_resume_thread, deadline=time.monotonic() + self.RESUME_FETCH_TIMEOUT + 2.0
        )
        if status == "cancel":
            # The user pressed Esc: stop and reset all fetch state so a retry
            # can start immediately. This is the ONLY path that reports "cancelled".
            self._abort_shared_resume(cancel)
            self._set_message(f"Stopped fetching '{entry.display}' (cancelled).", seconds=6.0)
            self._render()
            return
        if status == "timeout":
            # Past the deadline with the worker still stuck (a stalled network its own
            # timeout didn't unwind in time): a FAILURE, not a user cancel. Say why and
            # hold the notice until the user acknowledges it.
            self._abort_shared_resume(cancel)
            self._await_keypress(
                f"Couldn't fetch '{entry.display}': the fetch timed out after "
                f"{int(self.RESUME_FETCH_TIMEOUT)}s. Press any key to continue."
            )
            return
        # The worker finished. If it failed, report WHY and keep the notice up until a
        # keypress — never let a generic auto-dismissing (or "cancelled") message stand
        # in for a real failure the user needs to see.
        result = self._shared_resume_result
        if result is not None and "error" in result:
            self._abort_shared_resume(cancel)
            reason = "the shared transcript is incomplete" if result["error"] == "incomplete" else result["error"]
            self._await_keypress(f"Couldn't fetch '{entry.display}': {reason}. Press any key to continue.")
            return
        # Success: the import + session switch run on the main loop (_service_shared_resume).

    def _abort_shared_resume(self, cancel: "threading.Event") -> None:
        # Stop the in-flight transcript fetch and clear ALL resume state so the user
        # can retry at once. Setting *cancel* makes the (daemon) worker drop any late
        # result and the bounded git fetch self-terminate; nulling the shared cancel
        # token clears the "fetch in progress" flag so nothing lingers to block or
        # mis-handle an immediate retry. The next fetch installs a fresh token.
        cancel.set()
        self._shared_resume_result = None
        self._shared_resume_thread = None
        self._shared_resume_cancel = None

    def _await_keypress(self, message: str) -> None:
        """Show *message* and block — keeping the PTYs draining so the screen stays
        live — until the user presses any key, so a failure notice can't scroll past
        unseen. Headless/non-interactive callers (no real stdin) just set the message
        and return, since there is no key to wait on."""
        self._set_message(message, seconds=3600)
        self._render()
        try:
            stdin_fd = self._stdin_fileno()
        except (OSError, ValueError):
            return
        while self.running:
            master = self.master_fd
            background = self._background_fds() if self.sessions else {}
            fds = [stdin_fd]
            if master is not None:
                fds.append(master)
            fds.extend(background)
            try:
                readable, _, _ = select.select(fds, [], [], 0.2)
            except (OSError, ValueError):
                return
            for fd in readable:
                if fd == stdin_fd:
                    if self._is_real_keypress(self._read_stdin(32)):  # a key (not a mouse move) dismisses
                        return
                elif fd == master:
                    output = self._drain_child_output()
                    if output is not None:
                        self.last_child_output = time.monotonic()
                        self._feed_child_output(output)
                elif fd in background:
                    self._pump_background(background[fd])

    def _cancel_inflight_shared_fetches(self) -> None:
        # Stop any in-flight shared-session fetch immediately (used on exit): signal
        # the cancel token so a still-running worker drops its result and never
        # triggers a late session switch. The bounded git fetch self-terminates, and
        # the daemon thread dies with the process. Best-effort and idempotent.
        if self._shared_resume_cancel is not None:
            self._shared_resume_cancel.set()
        self._shared_resume_result = None

    def _service_shared_resume(self) -> None:
        result = self._shared_resume_result
        if result is None:
            return
        # A cancelled/abandoned fetch must never complete a switch: drop a late result
        # when there is no active fetch (token cleared by _abort_shared_resume) or its
        # token is set (the user stopped it, or aGiTrack is exiting).
        cancel = self._shared_resume_cancel
        if cancel is None or cancel.is_set():
            self._shared_resume_result = None
            self._shared_resume_thread = None
            return
        if self._shared_resume_thread is not None and self._shared_resume_thread.is_alive():
            return  # still fetching
        self._shared_resume_result = None
        self._shared_resume_thread = None
        self._shared_resume_cancel = None  # fetch concluded — no token lingers to block a retry
        if result.get("error") == "incomplete":
            self._await_keypress("That shared session is incomplete; cannot resume it. Press any key to continue.")
            return
        if "error" in result:
            self._await_keypress(f"Could not fetch the shared session: {result['error']}. Press any key to continue.")
            return
        if result["action"] == "update_live":
            self._complete_live_shared_update(result)
            return
        # A new (or copied) session: import the transcript and resume it.
        agent, sid = result["agent"], result["session_id"]
        if not agent.import_shared_session(
            self.base_repo.repo, sid, result["transcript"], overwrite=result["overwrite"], as_id=result["as_id"]
        ):
            self._set_message("Could not install the shared session for resume.", seconds=8.0)
            self._render()
            return
        # A "Keep both" fork (as_id set) deliberately starts a SEPARATE lineage: record
        # no origin for it, so sharing it later publishes a new `<you>/<name>` entry of
        # its own rather than updating the session it was copied from (#55).
        live_before = {getattr(getattr(s, "state", None), "backend_session_id", None) for s in self.sessions}
        self._resume_conversation(result["name"], result["resume_id"], backend=result["backend"])
        state = self.state
        if (
            state is not None
            and result["resume_id"] not in live_before
            and state.backend_session_id == result["resume_id"]
        ):
            # A genuinely new local session copied from a collaborator's shared one
            # (not a switch to an already-live session): record the copy so its first
            # commit notes the context/tokens inherited from the shared conversation.
            state.set_session_origin_event(
                kind="copy",
                source=result.get("session_id") or result["resume_id"],
                source_name=result.get("entry_name"),
                collaborator=result.get("origin_contributors"),
            )

    def _complete_live_shared_update(self, result: dict) -> None:
        # Update the already-running session to the shared version: switch to it,
        # overwrite its worktree transcript, then restart the backend so it loads
        # the new content (a live agent won't pick up a transcript swapped under it).
        agent, sid = result["agent"], result["session_id"]
        idx = next(
            (
                i
                for i, s in enumerate(self.sessions)
                if getattr(getattr(s, "state", None), "backend_session_id", None) == sid
            ),
            None,
        )
        if idx is None:
            # It stopped being live while fetching — fall back to a fresh resume.
            agent.import_shared_session(self.base_repo.repo, sid, result["transcript"], overwrite=True)
            self._resume_conversation(result["entry_name"], sid, backend=result["backend"])
            return
        self._switch_active(idx)
        if not agent.import_shared_session(self.repo.repo, sid, result["transcript"], overwrite=True):
            self._set_message("Could not update the session from the shared version.", seconds=8.0)
            self._render()
            return
        self._restart_agent("Updated this session to the shared version.")

    def _format_age(self, updated: float) -> str:
        # An unknown/unset timestamp (0 or None) must not be rendered as a date —
        # it would show as ~20000d ago (the Unix epoch). Say so honestly instead.
        if not updated or updated <= 0:
            return "date unknown"
        delta = max(0, int(time.time() - updated))
        for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
            if delta >= size:
                return f"{delta // size}{unit} ago"
        return "just now"

    def _dormant_worktrees(self, live_names: set) -> list:
        # Worktrees that exist on disk but are not currently live sessions. These
        # are kept across restarts so their conversation context can be resumed,
        # whether or not they still carry unmerged work.
        try:
            return [info for info in self._worktrees().list() if info.name not in live_names]
        except Exception:
            return []

    def _dormant_has_pending(self, info) -> bool:
        # True if a dormant worktree still has work to integrate into the base.
        try:
            repo = GitRepo(info.path)
            return self._worktree_has_pending_work(repo, repo.current_branch())
        except Exception:
            return True  # err toward offering the resolve flow

    def _prompt_new_session(self) -> str:
        # Returns _MENU_DONE once a session is actually started (a transition → agent), or
        # _MENU_UP if any prompt is backed out of (silently re-show the sessions menu).
        name = self._prompt_session_name("New Session", default=self._next_session_name())
        if name is None:
            return self._MENU_UP
        # Fork the current conversation, or start a blank one?
        fork = self._prompt_fork_or_blank()
        if fork is None:
            return self._MENU_UP
        # A fork continues the current session's work, so default its merge branch to
        # the current session's; a blank session defaults to the repo directory's.
        base = self._prompt_new_session_base(default=self._base_branch if fork else None)
        if base is None:
            return self._MENU_UP
        # The new session merges into its OWN branch — independent of the other
        # sessions and of the branch checked out in the repo directory.
        if fork and self._fork_current_session(name, base_branch=base):
            return self._MENU_DONE
        if fork:
            self._set_message("Couldn't fork the current session; starting a blank one instead.", seconds=8.0)
        # A new session inherits the CURRENT session's backend, not the global default —
        # the user is coding in this backend, so a sibling session should start there too
        # (the global default may be stale, e.g. left at opencode). Captured here because
        # _new_session replaces self.active (and thus self.state) before reading it.
        self._new_session(name, base_branch=base, backend=self.state.backend)
        return self._MENU_DONE

    def _can_fork_active(self) -> bool:
        # A fork copies the active conversation, so it needs a portable transcript
        # (the same capability session sharing relies on) and a session to copy.
        return bool(getattr(self.state, "backend_session_id", None)) and getattr(
            self.backend, "supports_session_sharing", False
        )

    def _prompt_fork_or_blank(self) -> bool | None:
        # True = fork the current session, False = blank, None = cancel. No prompt
        # (blank) when there's no live conversation to fork.
        if not self._can_fork_active():
            return False
        choice = self._select_popup(
            "Start the new session as…",
            [
                "Blank session — a fresh conversation",
                f"Fork '{self.name}' — copy this conversation (under a new id)",
            ],
        )
        if choice is None:
            return None
        return choice.startswith("Fork")

    def _fork_current_session(self, name: str, *, base_branch: str | None = None) -> bool:
        # Duplicate the active conversation under a FRESH backend id, so the fork runs
        # independently and never clashes with the original. Returns False if forking
        # isn't possible (the caller then starts a blank session).
        agent = self.backend
        src_id = self.state.backend_session_id
        if not src_id or not getattr(agent, "supports_session_sharing", False):
            return False
        source_name = self.name  # the original session's name, captured before switching
        # The LATEST conversation state lives in the active session's worktree (its
        # cwd), so export from there first; the base repo may only hold an older
        # mirrored copy. (Same order as the auto-share path.)
        transcript = agent.export_session_raw(self.repo.repo, src_id) or agent.export_session_raw(
            self.base_repo.repo, src_id
        )
        new_id = getattr(agent, "new_import_id", lambda: None)() or agent.new_session_id()
        if not transcript or not new_id:
            return False
        # Install the copied transcript under the new id (does not touch the original),
        # then start a session resuming it — same path as a "keep both" shared resume.
        if not agent.import_shared_session(self.base_repo.repo, src_id, transcript, as_id=new_id):
            return False
        self._new_session(name, resume_session_id=new_id, backend=self.state.backend, base_branch=base_branch)
        if self.state.backend_session_id == new_id:
            # The fork took: record that this session resumes a copy of another
            # conversation, so its first commit notes the inherited context/tokens
            # (issue: track fork/copy in the interaction trace).
            self.state.set_session_origin_event(kind="fork", source=src_id, source_name=source_name)
        return True

    def _merge_target_default(self) -> str | None:
        # The default merge destination for a new session: the branch checked out in
        # the repo directory ("the base branch").
        return self._repo_dir_branch or (self.base_repo.current_branch() if self.base_repo is not None else None)

    def _prompt_new_session_base(self, *, default: str | None = None) -> str | None:
        # Which branch the new session merges into. Defaults to ``default`` (a fork's
        # current branch) or the repo directory's branch ("the base branch"); picking
        # another lets the session work toward a different branch without checking it
        # out. Returns the chosen branch, None on cancel, or the default when there's
        # nothing else to offer.
        default_branch = default or self._merge_target_default()
        if not self._use_worktrees or default_branch is None:
            return default_branch
        others = [b for b in self.base_repo.list_branches() if b != default_branch and not is_managed_branch(b)]
        if not others:
            return default_branch
        default_label = f"{default_branch}  (default)"
        options = [default_label, *others]
        choice = self._select_popup("Merge this session's changes into which branch?", options)
        if choice is None:
            return None
        return default_branch if choice == default_label else choice

    def _stop_session_menu(self) -> None:
        options = [self._session_name(index) for index in range(len(self.sessions))]
        choice = self._select_popup("Stop which session?", options)
        if choice is None:
            return
        self._stop_session(options.index(choice))

    def _fork_lineage_on_rename(self, session_id: str | None) -> None:
        # Rename-as-fork (#55): a deliberate rename re-identifies the session, so drop
        # any tracked shared-lineage origin. Sharing it now publishes a NEW
        # `<you>/<new-name>` entry of its own instead of updating the session it was
        # resumed from (or first shared as) — a rename means "make a separate copy".
        if session_id and self._user_state().shared_origin(session_id):
            self._user_state().set_shared_origin(session_id, owner=None, name=None)

    def _rename_session_menu(self) -> None:
        options = [self._session_name(index) for index in range(len(self.sessions))]
        choice = self._select_popup("Rename which session?", options)
        if choice is None:
            return
        index = options.index(choice)
        new_name = self._prompt_popup("Rename session", "New name for this session:", default=self._session_name(index))
        if new_name is None or not new_name.strip():
            return
        self._rename_session(index, new_name.strip())

    def _rename_session(self, index: int, new_name: str) -> None:
        # Rename a session by moving its worktree directory. The worktree is in use
        # (the backend's cwd is inside it), so release the backend first, move, then
        # restart it pointing at the new path.
        if not (0 <= index < len(self.sessions)):
            return
        sanitized = _sanitize_name(new_name)
        if self._session_name_taken(sanitized) and sanitized != _sanitize_name(self._session_name(index)):
            self._set_message(f"'{sanitized}' is already in use.", seconds=6.0)
            self._render()
            return
        self._switch_active(index)
        info = self.worktree
        if info is None or not self._use_worktrees:
            self._set_message("This session has no worktree to rename.")
            self._render()
            return
        if _sanitize_name(info.name) == sanitized:
            self._set_message("Name unchanged.")
            self._render()
            return
        old_name = info.name
        sid = self.state.backend_session_id
        # Release the worktree (the backend has its cwd inside) before moving it.
        self._stop_file_watcher()
        self._teardown_child()
        try:
            new_info = self._worktrees().move(old_name, sanitized)
        except Exception as error:
            self._debug(f"rename worktree failed: {error!r}")
            # Recover: respawn in the original worktree so the session isn't stranded.
            self._init_screen()
            self._spawn()
            self._start_file_watcher()
            self._set_message(f"Could not rename session: {error}", seconds=8.0)
            self._render()
            return
        self.name = new_info.name
        self.worktree = new_info
        self.repo = GitRepo(new_info.path)
        self.turn = self._turn_from_branch(self.repo.current_branch())
        self.state = AgitrackState(new_info.path, default_backend=self.global_config.default_backend)
        if sid:
            self.state.backend_session_id = sid  # re-point backend_session_repo at the new path
        self.actions = AgitrackActions(self.repo, self.state, verbose=self.verbose)
        # Whether this session was tracking a shared lineage BEFORE the rename forks
        # it (the fork below clears that origin). Used to warn the user that a later
        # share now creates a new shared entry instead of updating the original.
        shared_before_rename = bool(sid and self._user_state().shared_origin(sid))
        if sid:
            self._stage_backend_resume(sid)  # re-link the transcript under the new path
            self._persist_session_name(sid)
            self._fork_lineage_on_rename(sid)
        self._reset_agent_tracking()
        self._sanitize_state_trace()
        self._initialize_session_baseline()
        self._init_screen()
        self._spawn()
        self._resize_child()
        self._enable_host_mouse()
        self._start_file_watcher()
        if shared_before_rename:
            self._set_message(
                f"Renamed session to '{new_info.name}'. This makes it a separate copy — sharing it now "
                "creates a NEW shared session rather than updating the one it came from.",
                seconds=12.0,
            )
        else:
            self._set_message(f"Renamed session to '{new_info.name}'.")
        self._render()

    def _switch_active(self, index: int) -> None:
        if not (0 <= index < len(self.sessions)) or index == self.active_index:
            return
        # Hold the pipeline lock across the foreground swap so the git worker (which
        # reads `self.active`) can't be mid-pass while the pointer moves. Draining the
        # modal mailbox while we wait keeps a worker that's blocked on a dialog from
        # deadlocking us. (RLock: reentrant when already held by this thread, e.g. the
        # background-session conflict path that calls us from the locked cluster.)
        self._acquire_pipeline_lock_from_main()
        try:
            self._join_parse_worker_before_swap()
            # Swap under the outgoing session's parse lock: if the join above timed
            # out, the still-running worker writes its result to its owning Session
            # under this same lock, so it sees either fully-before or fully-after.
            lock = self.agent_parse_lock or threading.Lock()
            with lock:
                self.active = self.sessions[index]
            self.scroll_back = 0
            self._resize_child()
            self._enable_host_mouse()
            self._set_message(f"Switched to session '{self._session_name(index)}'")
            self._render()
            # The copy-back mute is per active-session-visit: a switch resets it so the
            # session we just landed on gets offered its own worktree-only files (background
            # sessions are never interrupted mid-run; this is where we catch up). Only when
            # it's idle — a session still mid-turn gets offered by the turn path once it settles.
            self._copy_declined = set()
            if not self._agent_is_active():
                self._offer_copy_unstaged_to_base(context="switch")
        finally:
            self._pipeline_lock.release()
        # The newly-active session may merge into a different branch than the one
        # checked out in the repo directory — ask where its changes should merge.
        self._prompt_merge_target_if_diverged()

    def _join_parse_worker_before_swap(self) -> None:
        # A parse worker started for the active session reads that session's
        # backend/repo and must not straddle a session swap. The export normally
        # takes well under a second; wait for it rather than racing it.
        thread = self.agent_parse_thread
        if thread is not None and thread.is_alive():
            self._set_message("Finishing this session's transcript export...")
            self._render()
            thread.join(timeout=10)

    def _pump_background(self, session: Session) -> None:
        # Keep a background session's screen current by draining + feeding its
        # output. No render and no commit here — committing happens separately in
        # _service_background_sessions (synchronously, via _with_session), since
        # the async parse worker is not safe to run on a background session.
        if session not in self.sessions:
            return
        saved = self.active
        self.active = session
        died = False
        try:
            output = self._drain_child_output()
            if output is None:
                died = True
            elif output:
                session.last_child_output = time.monotonic()
                self._answer_terminal_queries(output)
                self._feed_child_output(output)
        finally:
            self.active = saved
        if died:
            self._stop_session(self.sessions.index(session), commit=False)

    def _ensure_worktree_alive(self) -> None:
        if self.worktree is None:
            return
        if self.worktree.path.exists():
            return
        self._debug(f"Worktree '{self.name}' directory gone; recovering...")
        self._teardown_child()
        self._stop_file_watcher()
        try:
            self.base_repo._run(["git", "worktree", "prune"], check=False)
        except Exception:
            pass
        backend_name = self.state.backend
        try:
            info, repo = self._open_session_worktree(self.name)
        except Exception as error:
            self._debug(f"worktree recovery failed: {error!r}; falling back to base tree")
            self.worktree = None
            self.name = "main"
            self.repo = self.base_repo
            self.state = AgitrackState(self.base_repo.repo, default_backend=self.global_config.default_backend)
            self.state.backend = backend_name
            self.backend = make_proxy_agent(backend_name)
            self.actions = AgitrackActions(self.repo, self.state, verbose=self.verbose)
            self._reset_agent_tracking()
            self._sanitize_state_trace()
            self._initialize_session_baseline()
            self._init_screen()
            self._spawn()
            self._start_file_watcher()
            self._resize_child()
            self._enable_host_mouse()
            self._set_message("Worktree was deleted externally; now tracking base repo.", seconds=8.0)
            self._render()
            return
        self.name = info.name
        self.worktree = info
        self.repo = repo
        self.turn = self._turn_from_branch(repo.current_branch())
        self.state = AgitrackState(info.path, default_backend=self.global_config.default_backend)
        self.state.backend = backend_name
        self.backend = make_proxy_agent(backend_name)
        self.actions = AgitrackActions(self.repo, self.state, verbose=self.verbose)
        self._reset_agent_tracking()
        self._sanitize_state_trace()
        self._initialize_session_baseline()
        self._init_screen()
        self._spawn()
        self._start_file_watcher()
        self._resize_child()
        self._enable_host_mouse()
        self._set_message(f"Worktree was deleted externally; recreated '{info.name}'.", seconds=8.0)
        self._render()

    def _with_session(self, session: Session, fn):
        # Run fn with `session` as the runner's active session: runner methods
        # (and the compat attribute layer) then read/write that session's state
        # directly on its own Session object. Pointer re-assignment only — no
        # field copying — so mutations made by fn persist on the session.
        if session not in self.sessions:
            return None
        saved = self.active
        self.active = session
        try:
            return fn()
        finally:
            self.active = saved

    def _commit_and_integrate_background(self) -> str:
        # A clean tree can still need the commit pipeline: the backend may have
        # committed its own work, which the parse then amends with the
        # trace/metadata before integration (#35).
        if self.repo.has_changes() or self._uncovered_backend_commits():
            self._commit_latest_turn_sync()
        self._clear_agent_in_flight_if_idle()
        # Defer integration while this session's commit summary is still being
        # computed, exactly as the active path does: it lets the summary be
        # amended into the message (not notes-only) and keeps this session's
        # "summarizing…" status line up so concurrent sessions are each visible.
        if self._summary_blocks_integration(time.monotonic()):
            return "skip"
        return self._integrate_turn_or_conflict()

    def _service_background_sessions(self) -> None:
        # Integrate background sessions as soon as they go idle, so finished work
        # lands in the base without waiting to be switched to. A background
        # session whose finished turn cannot fast-forward is brought to the
        # foreground and its resolve options box is surfaced (session + backend).
        if self.merge_ctx is not None:
            return
        now = time.monotonic()
        for index in range(len(self.sessions)):
            if index == self.active_index:
                continue
            session = self.sessions[index]
            if getattr(session, "merge_ctx", None) is not None:
                self._with_session(session, self._maybe_complete_agent_merge)
                continue
            if now - getattr(session, "last_child_output", 0.0) < self.CHILD_IDLE_SECONDS:
                continue
            if now - getattr(session, "last_poll", 0.0) < self.POLL_SECONDS:
                continue
            session.last_poll = now
            if self._with_session(session, self._commit_and_integrate_background) == "conflict":
                self._switch_active(index)
                self._prompt_resolve_conflict(self.repo.current_branch())
                return

    def _open_session_worktree(self, name: str, *, base_branch: str | None = None) -> tuple[WorktreeInfo, GitRepo]:
        worktrees = self._worktrees()
        path = worktrees.worktree_path(name)
        if self._is_valid_worktree(path):
            repo = GitRepo(path)
            return WorktreeInfo(name=path.name, path=path, branch=repo.current_branch()), repo
        # A leftover, *invalid* dir (e.g. only `.agitrack/` recreated by a write after
        # the previous run tore the worktree down) would otherwise make GitRepo
        # fail and drop us into a fresh session — the "first restart starts new,
        # second restart resumes" off-by-one. Clear it so `create` (which prunes
        # first) can re-add the worktree cleanly.
        if path.exists():
            self._debug(f"removing invalid leftover worktree dir {path}")
            shutil.rmtree(path, ignore_errors=True)
        base = base_branch or self._base_branch or self.base_repo.current_branch()
        info = worktrees.create(name, base=base)
        return info, GitRepo(info.path)

    def _is_valid_worktree(self, path) -> bool:
        # A real linked worktree has a `.git` file pointing at its gitdir and a
        # resolvable current branch; a torn-down leftover (only `.agitrack/`) has not.
        if not (path / ".git").exists():
            return False
        try:
            GitRepo(path).current_branch()
            return True
        except Exception:
            return False

    def _warn_parallel_no_worktree_sessions(self) -> None:
        """Explain the shared-tree caveat the first time a user opens an additional
        --no-worktree session in a run. Parallel sessions are allowed; this only makes
        sure the user knows they have no isolation (they edit the same files at once and
        a turn's commit captures whatever is in the tree)."""
        if self._warned_parallel_no_worktree:
            return
        self._warned_parallel_no_worktree = True
        self._select_popup(
            "These sessions share this directory (--no-worktree): they have no isolation, so "
            "they edit the same files at the same time and a turn's commit captures whatever is "
            "in the working tree then — coordinate as you would with another person editing the "
            "same checkout. Restart without --no-worktree to give each session its own worktree.",
            ["Got it"],
        )

    def _new_session(
        self,
        name: str,
        *,
        resume_session_id: str | None = None,
        backend: str | None = None,
        base_branch: str | None = None,
    ) -> None:
        # Fresh per-session runtime state; the outgoing active session keeps
        # its state on its own Session object in self.sessions.
        self.active = Session.bare()
        if self._use_worktrees:
            # The branch this new session merges into — its own, independent of the
            # other sessions and of the branch checked out in the repo directory.
            base = base_branch or self._merge_target_default()
            try:
                info, repo = self._open_session_worktree(name, base_branch=base)
            except Exception as error:
                self._set_message(f"Could not create worktree: {error}", seconds=8.0)
                self._render()
                return
            self.name = info.name
            self.worktree = info
            self.repo = repo
            self._base_branch = base  # this session integrates into `base` (per-session)
            self.turn = self._turn_from_branch(repo.current_branch())
            self.state = AgitrackState(info.path, default_backend=self.global_config.default_backend)
            if base_branch is None:
                # No branch was explicitly chosen (e.g. resuming a dormant worktree): honor
                # any merge branch a prior run assigned it, confirming the change with the user.
                self._reconcile_merge_branch(base)
            else:
                self.state.merge_branch = base  # an explicit choice wins; record it
            started_msg = f"Started session '{info.name}' in .agitrack/worktrees/{info.path.name}"
        else:
            # --no-worktree: sessions run directly on the base tree with no isolation,
            # editing the same files in parallel (the user accepts this; we warn once).
            # There is no worktree and no per-session merge branch — commits land on the
            # branch checked out in the directory, like the startup no-worktree session.
            self._warn_parallel_no_worktree_sessions()
            self.name = name
            self.worktree = None
            self.repo = self.base_repo
            self._base_branch = self.base_repo.current_branch()
            self.turn = 0
            self.state = AgitrackState(self.base_repo.repo, default_backend=self.global_config.default_backend)
            if not resume_session_id:
                # A blank session is a FRESH conversation, not a continuation of whatever
                # the shared base-dir state last recorded (worktree sessions each got a
                # clean state dir; here the state file is shared, so reset it explicitly).
                self.state.backend_session_id = None
                self.state.last_backend_message_id = None
                self.state.new_agitrack_session_id()
                self.state.clear_trace()
            started_msg = f"Started session '{name}' (no worktree — shares this directory)"
        if backend:
            # Pin this session to a specific backend (e.g. created by switching
            # backends), independent of the global default.
            self.state.backend = backend
        if resume_session_id:
            # Resume this exact backend conversation.
            self.state.backend_session_id = resume_session_id
            self._persist_session_name(resume_session_id)
        self.backend = make_proxy_agent(self.state.backend)
        if resume_session_id:
            # Stage the transcript into THIS session's directory before spawning, so a
            # `--resume` finds it. Crucial when the conversation last ran somewhere else:
            # a shared session imported under the base repo, or — when worktrees are off —
            # a session that previously ran in a worktree. `_stage_backend_resume` also
            # retargets the transcript's recorded cwd to this directory, so resuming a
            # worktree session under --no-worktree runs in the base dir, not the old
            # (now-gone) worktree path.
            self._stage_backend_resume(resume_session_id)
        self.actions = AgitrackActions(self.repo, self.state, verbose=self.verbose)
        self._sanitize_state_trace()
        self._initialize_session_baseline()
        self._init_screen()
        self._spawn()
        self._start_file_watcher()
        self.sessions.append(self.active)
        self._resize_child()
        self._enable_host_mouse()
        self._set_message(started_msg)
        self._render()

    def _stop_session(self, index: int, *, commit: bool = True) -> None:
        if not (0 <= index < len(self.sessions)):
            return
        if len(self.sessions) <= 1:
            self._set_message("Cannot stop the only session.")
            self._render()
            return
        session = self.sessions[index]
        active = session is self.active
        if active:
            # Don't let an in-flight parse worker outlive its session's runtime
            # and write into whichever session becomes active next.
            self._join_parse_worker_before_swap()
        saved = self.active
        self.active = session
        try:
            if commit:
                self._commit_latest_turn_sync()
            self._stop_file_watcher()
            if self.child_pid:
                self.active.process.interrupt()  # SIGINT on POSIX, ConPTY ETX on Windows
                self._note_pid_for_reaping(self.child_pid)
            if self.master_fd is not None:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
                self.master_fd = None  # closes the ConPTY bridge socket on Windows (see run())
            worktree = self.worktree
        finally:
            self.active = saved
        # Leave the worktree's branch on disk (recoverable); just drop the live session.
        self._debug(f"stopped session index={index} worktree={getattr(worktree, 'path', None)}")
        self.sessions.pop(index)
        if active:
            self.active = self.sessions[min(index, len(self.sessions) - 1)]
            self.scroll_back = 0
            self._resize_child()
            self._enable_host_mouse()
        self._render()

    def _handle_active_session_exit(self) -> bool:
        # The active backend's PTY closed. If other sessions remain, drop this
        # one and switch to another; otherwise tell the loop to stop.
        if len(self.sessions) <= 1:
            return False
        self._stop_file_watcher()
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        self.master_fd = None
        self.child_pid = None
        index = self.active_index
        self.sessions.pop(index)
        self.active = self.sessions[min(index, len(self.sessions) - 1)]
        self.scroll_back = 0
        self._resize_child()
        self._enable_host_mouse()
        self._set_message(f"Session ended; switched to '{self._session_name(self.active_index)}'")
        self._render()
        return True

    def _relaunch_backend_or_exit(self) -> bool:
        # The only session's backend process exited on its own — most often Claude
        # quitting when you Esc its native session picker, where Claude restores
        # the terminal and terminates (its shutdown sequence is in the debug log).
        # aGiTrack should stay up and relaunch+resume the conversation rather than
        # quitting with it. Guard against a crash loop: if the backend keeps dying
        # quickly, stop relaunching and exit normally.
        # Root-cause case: the backend refused to resume because the session is held
        # by a running background agent. Fork a copy (claude's own remedy) once,
        # instead of relaunching into the same refusal until the crash-loop guard
        # gives up. The forked id is discovered and adopted on the first parse (#114).
        if self._should_fork_after_busy_exit():
            self._forked_for_busy = True
            self._fork_next_spawn = True
            self.child_pid = None  # already gone
            try:
                self._restart_agent("Previous session is busy; forked a copy to continue.")
            except Exception as error:
                self._debug(f"fork relaunch failed, exiting: {error!r}")
                self._backend_exit_notice = self._format_backend_exit_notice()
                self._finalize_on_backend_exit()
                return False
            return True
        now = time.monotonic()
        recent = [t for t in self._relaunch_times if now - t < 12.0]
        if len(recent) >= 3:
            self._debug("backend exited 3x within 12s; quitting instead of relaunching")
            self._backend_exit_notice = self._format_backend_exit_notice()
            self._finalize_on_backend_exit()
            return False
        recent.append(now)
        self._relaunch_times = recent
        self.child_pid = None  # already gone
        try:
            # _restart_agent tears down the dead PTY, re-baselines (so existing
            # history is not re-committed) and respawns; _spawn resumes the same
            # conversation via _should_continue_session.
            self._restart_agent("Backend exited — relaunched and resumed (Ctrl-C to quit aGiTrack).")
        except Exception as error:
            self._debug(f"relaunch failed, exiting: {error!r}")
            self._finalize_on_backend_exit()
            return False
        return True

    def _should_fork_after_busy_exit(self) -> bool:
        # True when the just-exited backend was claude refusing to resume a session
        # that is still running as a background agent, and we have not already forked
        # for this. Keyed off claude's own message so it tracks the CLI's behaviour
        # rather than guessing. State-guarded so it is inert in tests without state.
        if self._forked_for_busy:
            return False
        if getattr(self.state, "backend", None) != "claude":
            return False
        if not getattr(self.state, "backend_session_id", None):
            return False
        text = _strip_ansi(self.last_child_output_sample.decode(errors="replace"))
        return bool(_FORK_HINT_RE.search(text))

    def _format_backend_exit_notice(self) -> str | None:
        # The backend died on launch several times in a row (e.g. the claude CLI
        # refusing to resume a session that is still running as a background agent),
        # so aGiTrack is giving up. In proxy mode that output lived on the alt-screen,
        # which the terminal restore discards, so the user otherwise just sees a flash
        # and a bare prompt with no reason (#114). Echo the backend's last readable
        # lines, plus how to get unstuck.
        backend = getattr(self.state, "backend", None) or "the backend"
        text = _strip_ansi(self.last_child_output_sample.decode(errors="replace"))
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        notice = f"aGiTrack: {backend} exited repeatedly on launch and was not relaunched."
        if lines:
            tail = "\n".join("  " + line for line in lines[-12:])
            notice += f"\nLast output from {backend}:\n{tail}"
        notice += (
            "\nTry `agitrack --new-session` to start a fresh conversation, "
            "or `agitrack --mode json` to see the full backend error."
        )
        return notice

    def _finalize_on_backend_exit(self) -> None:
        # The only session's backend process is gone and we are NOT relaunching
        # (e.g. a real exit, or a crash loop). Commit/integrate its last turn and
        # persist the resume pointer before aGiTrack leaves, instead of just dropping
        # out of the loop (which would lose the last commit and leave resume
        # pointing at a stale session).
        self.child_pid = None  # already gone; don't signal a dead process
        try:
            self._finalize_pending_work()
        except Exception as error:
            self._debug(f"finalize on backend exit failed: {error!r}")

    def _start_new_session(self) -> None:
        self.state.backend_session_id = None
        self.state.last_backend_message_id = None
        self.state.clear_trace()
        self._restart_agent("Started a new session.")

    def _switch_to_session(self, session_id: str) -> None:
        if session_id == self.state.backend_session_id:
            self._set_message(f"Already tracking session {_short_session(session_id)}.")
            self._render()
            return
        self.state.backend_session_id = session_id
        self.state.last_backend_message_id = None
        self.state.clear_trace()
        self._restart_agent(f"Now tracking session {_short_session(session_id)}")

    def _sync_tracked_session(self) -> None:
        # Re-point tracking at the most recently active session without
        # relaunching the TUI, e.g. after the user started a new session inside
        # the backend itself.
        refs = self.backend.list_sessions(self.repo.repo)
        if not refs:
            self._set_message("No sessions found to sync.")
            self._render()
            return
        # Prefer sessions with content (a label is the first real user prompt):
        # Claude mints a fresh EMPTY session id on resume/picker actions, which
        # is newest by mtime but has nothing to track — adopting it blanks the
        # next restart (same trap claude_session.latest_session_id avoids).
        resumable = [ref for ref in refs if ref.label]
        newest = max(resumable or refs, key=lambda ref: ref.updated)
        if newest.id == self.state.backend_session_id:
            self._set_message(f"Already tracking the most recent session ({_short_session(newest.id)}).")
            self._render()
            return
        self.state.backend_session_id = newest.id
        self.state.last_backend_message_id = None
        self._initialize_session_baseline()
        self._set_message(f"Now tracking session {_short_session(newest.id)}")
        self._render()

    def _start_file_watcher(self) -> None:
        if Observer is None:
            return
        observer = Observer()
        observer.schedule(
            RepoChangeHandler(self.repo.repo, self.file_change_event, self._git_wake),
            str(self.repo.repo),
            recursive=True,
        )
        observer.start()
        self.file_observer = observer

    def _stop_file_watcher(self) -> None:
        observer = self.file_observer
        if observer is None:
            return
        observer.stop()
        observer.join(timeout=2.0)
        self.file_observer = None

    def _start_git_worker(self) -> None:
        """Start the dedicated git thread. From here on the automatic commit/merge
        pipeline for the foreground session runs there, never on the main reactor
        thread, so a `git status`/commit/merge can never block the user's typing."""
        self._stop_worker = False
        worker = threading.Thread(target=self._git_worker_loop, name="agitrack-git", daemon=True)
        self._git_worker = worker
        worker.start()

    def _stop_git_worker(self) -> None:
        """Stop and join the git worker. Idempotent. Called before exit-time finalize
        and before a restart re-exec so the foreground git that runs there can never
        race the worker.

        While joining, keep cancelling any dialog the worker is blocked on (returning
        None) so it can observe the stop and unwind — otherwise a worker waiting on an
        unanswered modal would never finish. Capped so teardown can't hang."""
        worker = self._git_worker
        if worker is None:
            return
        self._stop_worker = True
        self._git_wake.set()
        deadline = time.monotonic() + 10.0
        while worker.is_alive() and time.monotonic() < deadline:
            self._cancel_pending_modals()
            worker.join(timeout=0.05)
        self._git_worker = None

    def _cancel_pending_modals(self) -> None:
        """Unblock any worker waiting on a queued dialog by returning None for it.
        Used during teardown, where presenting the dialog is neither possible nor
        wanted."""
        while True:
            try:
                request = self._modal_mailbox.get_nowait()
            except queue.Empty:
                return
            request.result = None
            request.done.set()

    def _git_worker_loop(self) -> None:
        """The git thread's main loop: sleep on `_git_wake` (set by the file watcher
        on worktree writes, and once per reactor tick), then run one foreground
        pipeline pass under `_pipeline_lock`. Backs off to the idle cadence when the
        user has stepped away, so it doesn't spin (preserving the battery win)."""
        while self.running and not self._stop_worker:
            timeout = self.IDLE_POLL_SECONDS if self._is_idle() else self.POLL_SECONDS
            self._git_wake.wait(timeout=timeout)
            self._git_wake.clear()
            if not self.running or self._stop_worker:
                break
            try:
                with self._pipeline_lock:
                    if self.running and not self._stop_worker and self.master_fd is not None:
                        self._run_git_pipeline()
            except Exception as error:  # a git failure must never kill the worker
                self._debug(f"git worker pass failed: {error!r}")

    def _run_git_pipeline(self) -> None:
        """One foreground-session git pass — the work that used to run inline in the
        reactor's timers phase. Operates only on the active (foreground) session and
        never swaps `self.active`, so it can run concurrently with the main thread's
        stdin/render without misrouting. Any user dialog it raises is marshaled to the
        main thread via `_run_modal`; any repaint just flags `_render_pending`.

        Background / multi-session servicing (which DOES swap `self.active`) stays on
        the main thread, mutually excluded from this pass by `_pipeline_lock`.
        """
        if self._stop_worker or not self.running or self.master_fd is None:
            return
        if self.merge_ctx:
            # A foreground merge is being resolved; don't make normal commits meanwhile.
            self._maybe_complete_agent_merge()
            return
        self._maybe_agent_commit()
        self._poll_base_advanced()
        self._warn_if_base_edited()
        self._warn_if_cwd_drifted()

    def _background_fds(self) -> dict:
        # master_fd -> session object, for every session that is not the active
        # one. Keyed by object (not index) so that removing a session that has
        # died does not invalidate the other entries.
        mapping = {}
        for index, session in enumerate(self.sessions):
            if index == self.active_index:
                continue
            fd = getattr(session, "master_fd", None)
            if fd is not None:
                mapping[fd] = session
        return mapping

    def _loop(self) -> int:
        # Main event loop.  Each iteration is decomposed into five named reactor
        # phases; the phases communicate via a simple sentinel convention:
        #   "continue"  → restart the while loop immediately (skip later phases)
        #   "break"     → leave the while loop (clean exit)
        #   int         → leave the while loop and return that exit code
        #   None        → proceed to the next phase as usual
        assert self.master_fd is not None
        while self.running:
            # --- phase 1: select ------------------------------------------
            background, readable = self._reactor_select_phase()
            # --- phase 2: pty-output --------------------------------------
            sentinel: str | int | None = self._reactor_pty_output_phase(readable)
            if sentinel == "continue":
                continue
            if sentinel == "break":
                break
            # --- phase 3: stdin -------------------------------------------
            sentinel = self._reactor_stdin_phase(readable)
            if sentinel == "continue":
                continue
            if sentinel == "break":
                break
            # A stdin action may have torn the session down for a restart/exit (e.g. a
            # menu "update" → _apply_update_and_restart, which finalizes and REMOVES the
            # worktree). Stop now rather than fall through to the timers phase, whose
            # _ensure_worktree_alive / _maybe_agent_commit would run git in the deleted
            # worktree (FileNotFoundError). Mirrors the exit command's "break" above.
            if not self.running:
                break
            # --- phase 4: timers / background tasks -----------------------
            self._reactor_timers_phase()
            # A deferred update can apply here (sessions just went idle), tearing the
            # worktree down mid-phase; don't reap/inspect children against it.
            if not self.running:
                break
            # --- phase 5: child-exit --------------------------------------
            sentinel = self._reactor_child_exit_phase()
            if sentinel == "continue":
                continue
            if isinstance(sentinel, int):
                return sentinel
        return 0

    # ------------------------------------------------------------------
    # Reactor phases (called exclusively from _loop)
    # ------------------------------------------------------------------

    def _select_timeout(self) -> float:
        """How long the reactor blocks in ``select`` this iteration.

        ``select`` returns the instant stdin or any PTY fd becomes readable, so
        this timeout only bounds how long we may sit idle before re-running the
        timer-driven work — it never delays the user's keystrokes or the
        backend's output. The value is the time until the *next* thing that must
        happen:

        * a queued repaint → ``0.016`` (coalesced output frame, ~next tick);
        * a deferred prompt-submit (the injected Enter, scheduled 0.4s out) →
          the exact time remaining to it, so it still fires promptly even though
          the normal active poll is a full second;
        * otherwise ``ACTIVE_POLL_SECONDS`` while there is work to service, or
          ``IDLE_POLL_SECONDS`` once :meth:`_is_idle` says nothing does — the
          long idle block is what lets the CPU sleep and saves battery while the
          user is away.

        Every background sweep self-throttles to its own interval (≥2s), so the
        active poll being a coarse 1s never makes any check run more often.
        """
        if self._render_pending:
            return 0.016
        if self._pending_enter_at is not None:
            remaining = self._pending_enter_at - time.monotonic()
            return max(0.0, min(remaining, self.ACTIVE_POLL_SECONDS))
        return self.IDLE_POLL_SECONDS if self._is_idle() else self.ACTIVE_POLL_SECONDS

    def _is_idle(self) -> bool:
        """True when no foreground or background work needs the fast poll loop.

        Idle means: no turn in flight (active or background), no merge being
        resolved, no per-turn pipeline work pending (file change to process,
        parse/commit due, deferred Enter or queued prompt), no background worker
        thread alive, no timed notice still on screen, and no keystroke or
        backend output within ``IDLE_AFTER_SECONDS``. A sticky message (one that
        waits for a keypress) does NOT keep us awake — the keypress that
        dismisses it wakes ``select`` on its own.
        """
        now = time.monotonic()
        # An active turn, or output recent enough to still be inside the
        # post-turn commit-debounce window, keeps the fast loop.
        if self.agent_in_flight or self.merge_ctx is not None:
            return False
        if now - self.last_child_output < self.CHILD_IDLE_SECONDS:
            return False
        if now - self.last_user_input < self.IDLE_AFTER_SECONDS:
            return False
        # Pending per-turn pipeline work. file_change_event is set by the file
        # watcher thread (it does not wake select), so it must be checked here.
        if self.file_change_event.is_set() or self.parse_pending or self.status_check_pending:
            return False
        if self.pending_forwarded or self.pending_prompt_text:
            return False
        if self._pending_enter_at is not None or self._base_advanced:
            return False
        # A timed notice must expire/refresh on schedule; a sticky one need not.
        if self.message is not None and not self._message_sticky and now < self.message_until:
            return False
        for attr in (
            "agent_parse_thread",
            "_summary_thread",
            "_precompact_thread",
            "_auto_share_thread",
            "_shared_resume_thread",
            "_update_check_thread",
        ):
            thread = getattr(self, attr, None)
            if thread is not None and thread.is_alive():
                return False
        # Any *background* session still working (the active one is covered above).
        # Background turns are committed/integrated by _service_background_sessions
        # once they have been quiet for CHILD_IDLE_SECONDS, with POLL_SECONDS between
        # service passes; stay non-idle a little past that window so a just-finished
        # background turn is integrated promptly rather than waiting a full idle tick.
        background_quiet = self.CHILD_IDLE_SECONDS + self.POLL_SECONDS + 1.0
        for index in range(len(self.sessions)):
            if index == self.active_index:
                continue
            session = self.sessions[index]
            if getattr(session, "agent_in_flight", False) or getattr(session, "merge_ctx", None) is not None:
                return False
            if now - getattr(session, "last_child_output", 0.0) < background_quiet:
                return False
        return True

    def _reactor_select_phase(self) -> tuple[dict, list]:
        """Phase 1 — compute the fd set, block in select, drain background PTYs.

        Returns (background_fds_map, readable_list).  Background sessions are
        drained here so their PTY buffers never fill up regardless of which
        phase the main loop is in.
        """
        timeout = self._select_timeout()
        background = self._background_fds()
        watch = [self._stdin_fileno(), self.master_fd, *background]
        if self._wake_r >= 0:
            watch.append(self._wake_r)  # the git worker's wake channel (e.g. it queued a dialog)
        readable, _, _ = select.select(watch, [], [], timeout)
        if self._wake_r in readable:
            if self._waker is not None:
                self._waker.drain()  # drain the wake byte(s); presence is the signal
            else:
                try:
                    os.read(self._wake_r, 4096)
                except OSError:
                    pass
        # On Windows a resize is delivered by the host's watcher, not SIGWINCH; pick it up
        # here (POSIX always returns False — it uses the SIGWINCH handler instead).
        if self._host is not None and self._host.consume_resize_pending():
            self._resize_child()
        for fd in readable:
            if fd in background:
                self._pump_background(background[fd])
        return background, readable

    def _reactor_pty_output_phase(self, readable: list) -> "str | None":
        """Phase 2 — drain and process the active session's PTY output.

        Returns a loop-control sentinel or None to continue normally.
        """
        if self.master_fd not in readable:
            return None
        output = self._drain_child_output()
        if output is None:
            sample = self.last_child_output_sample[-2048:].decode(errors="replace").replace("\x1b", "\\x1b")
            self._debug(f"master_fd closed (backend gone); last_output={sample!r}")
            self._raw_capture("EOF", b"")
            if self._handle_active_session_exit():
                return "continue"
            if self._relaunch_backend_or_exit():
                return "continue"
            return "break"
        if output:
            self._raw_capture("<", output)
            self.last_child_output = time.monotonic()
            self.last_child_output_sample = (self.last_child_output_sample + output)[-4096:]
            self._answer_terminal_queries(output)
            self._sync_terminal_modes(output)
            self._track_sync_update(output)
            self._feed_child_output(output)
            self._render_output()
        return None

    def _reactor_stdin_phase(self, readable: list) -> "str | None":
        """Phase 3 — read stdin and route bytes to the active session or handler.

        Returns a loop-control sentinel or None to continue normally.
        """
        if self._stdin_fileno() not in readable:
            return None
        data = self._read_stdin(4096)
        # Only a real keystroke resets the idle backoff — NOT a mouse wheel / move /
        # click or a focus in/out report. Scrolling history is passive reading: it
        # needs no commits or background polling, so it must not pin aGiTrack in the
        # active loop. (select still woke instantly to redraw the scrolled view; this
        # only governs whether we then drop back to the low-power idle cadence.)
        if self._is_real_keypress(data):
            self.last_user_input = time.monotonic()
        self._raw_capture(">", data)
        self._debug(f"stdin: {data!r} menu_key={self.input.menu_key!r}")
        # A popup message taller than the screen scrolls with PgUp/PgDn, handled before
        # anything else so a long notice can be read in full (and isn't dismissed mid-read).
        if self._message_max_scroll > 0 and not self.input.capturing and _PAGE_KEY_RE.search(data):
            page = max(self.rows - 4, 1)
            for match in _PAGE_KEY_RE.finditer(data):
                step = page if match.group(1) == b"6" else -page  # PgDn scrolls down, PgUp up
                self._message_scroll = max(0, min(self._message_scroll + step, self._message_max_scroll))
            data = _PAGE_KEY_RE.sub(b"", data)
            self.message_until = time.monotonic() + 30  # keep the notice up while scrolling
            self._render()
            if not data:
                return None
        # Any keypress dismisses a sticky message (e.g. the auto-commit
        # confirmation) so it no longer overlays the live view; repaint to
        # remove the popup even if the key produces no child echo.
        if self._clear_sticky_message_on_input():
            self._render_pending = True
        data = self._input_tail + data
        data, self._input_tail = self._hold_incomplete_tail(data)
        data = self._intercept_scroll(data)
        self._debug(f"after intercept: {data!r}")
        # Decode kitty keyboard protocol control keys back to plain bytes.
        # When the terminal is in kitty mode (enabled by the backend), Ctrl-A
        # through Ctrl-Z are sent as escape sequences like \x1b[97;5u instead
        # of plain bytes like \x01. We decode them so the menu key matching works.
        data = _decode_kitty_ctrl_keys(data)
        self._debug(f"after kitty decode: {data!r}")
        was_capturing = self.input.capturing
        forwarded, local_echo, command, should_exit = self.input.feed(data)
        self._debug(f"feed result: forwarded={forwarded!r} command={command!r} capturing={self.input.capturing}")
        if self.input.capturing and not was_capturing:
            # The Ctrl-G palette just opened: surface a top-level "merge" command when
            # there is unmerged worktree work, so the user is reminded to rescue it
            # before it gets lost. Recomputed each open so it reflects current state.
            self.input.extra_commands = ["merge"] if self._has_unmerged_work() else []
        if should_exit:
            # One shared flow (also reachable from inside popups): a
            # second Ctrl-C during the confirmation popup exits
            # immediately but still finalizes pending work first.
            if self._run_exit_flow():
                return "break"
            self._render()
            return "continue"
        if local_echo:
            self._render_status(local_echo.decode(errors="ignore"))
        if self.input.capturing:
            self._render()
        elif was_capturing and command is None:
            self._render()
        if forwarded:
            self.scroll_back = 0  # interacting snaps back to the live view
            submit = self._forwarded_submits(forwarded)
            self._update_passthrough_prompt(forwarded)
            submitted_prompt = ""
            if submit:
                submitted_prompt = self.passthrough_prompt.decode(errors="ignore").strip()
                if submitted_prompt.startswith("/compact"):
                    self._handle_pre_compaction()
                # Submit-time commits touch the foreground worktree's index, the same
                # one the git worker commits the agent's turn into — take the pipeline
                # lock so the two never run at once (draining dialogs while we wait).
                self._acquire_pipeline_lock_from_main()
                try:
                    if not self._pre_agent_commit_if_needed(submitted_prompt):
                        self.pending_forwarded = [chunk for chunk in forwarded if chunk in {b"\r", b"\n"}]
                        self.pending_prompt_text = submitted_prompt
                        forwarded = [chunk for chunk in forwarded if chunk not in {b"\r", b"\n"}]
                        submit = False
                finally:
                    self._pipeline_lock.release()
            if submit:
                self.passthrough_prompt.clear()
                self.passthrough_escape = None
            if forwarded:
                if submit:
                    self.agent_in_flight = True
                    if submitted_prompt:
                        # Flush the previous turn's deferred integration first, then
                        # put this new prompt on its own branch (same shared index as
                        # the worker → under the pipeline lock).
                        self._acquire_pipeline_lock_from_main()
                        try:
                            self._integrate_committed_turn_before_new_turn()
                            self._ensure_turn_branch()
                        finally:
                            self._pipeline_lock.release()
                        # Drop the previous turn's "created & merged" status line so
                        # it doesn't linger into — and read as belonging to — the
                        # new turn (which would show "created" before "summarizing").
                        self._set_session_notice(self._session_label(), None)
                self.active.process.write(b"".join(forwarded))
        if command:
            self._run_command(command)
            # An "exit"/"quit" command runs the same finalize-and-teardown flow as
            # Ctrl-C, which removes the worktree. It must break the loop here just
            # like the Ctrl-C path above — otherwise this iteration falls through to
            # the timers phase and _maybe_agent_commit runs git in the deleted
            # worktree (FileNotFoundError on exit).
            if self._exit_requested:
                return "break"
        return None

    def _reactor_timers_phase(self) -> None:
        """Phase 4 — main-thread servicing. The foreground commit/merge pipeline runs
        on the git worker (`_run_git_pipeline`); this phase keeps only terminal I/O,
        prompt forwarding, dialog hand-off, and the background / multi-session git
        that swaps `self.active` (guarded by `_pipeline_lock`, non-blocking so a busy
        worker never stalls the reactor — and thus never delays typing)."""
        if not self.running:
            return  # a restart/exit is underway; touch no (possibly-removed) worktree
        self._check_shutdown_request()
        if not self.running:
            return  # an external graceful-shutdown request just fired
        self._flush_pending_render()
        self._flush_pending_enter()
        self._drain_modal_mailbox()  # present any dialog the git worker queued
        self._present_pending_copy_offer()  # the turn's "copy stranded files?" popup, if any
        self._resume_pending_prompt_if_ready()
        self._service_shared_resume()  # complete a shared-session resume once fetched
        # Background / multi-session git (these swap self.active via _with_session, so
        # they must be mutually exclusive with the worker's foreground pass). Skip the
        # whole cluster this tick when the worker holds the lock — it runs on the next.
        if self._pipeline_lock.acquire(blocking=False):
            try:
                self._ensure_worktree_alive()  # recovers a vanished worktree (PTY/watcher work)
                self._check_base_branch_drift()  # may surface a dir-change session swap
                self._service_commit_summaries()  # apply finished background summaries (#8)
                self._service_precompact_summary()
                self._service_background_sessions()
                if self._base_advanced:
                    self._base_advanced = False
                    self._sync_idle_worktrees_to_base()
            finally:
                self._pipeline_lock.release()
        self._maybe_check_for_update()
        self._maybe_apply_pending_update()
        if not self.running:
            # _maybe_apply_pending_update finalized and removed the worktree for a
            # restart; stop before any further servicing.
            return
        self._service_background_share_ops()  # surface finished background unshare/etc. results
        self._service_auto_share_outcome()  # surface a live auto-share failure (else it's silent)
        self._service_share_conflicts()  # prompt overwrite/merge for a share refused as behind
        self._service_backend_update()  # surface a finished backend auto-update result
        self._maybe_auto_update_backend()  # auto-apply a brew-managed backend update; once per backend
        self._service_session_notices()  # expire/refresh per-session status lines
        self._git_wake.set()  # nudge the worker so its pass tracks the reactor's cadence

    def _reactor_child_exit_phase(self) -> "str | int | None":
        """Phase 5 — reap stopped children and check whether the active child exited.

        Returns a loop-control sentinel (``"continue"``, an ``int`` exit code),
        or ``None`` to proceed normally.
        """
        self._reap_stopped_children()  # collect SIGINT'd backends as they exit
        if self.child_pid is not None:
            # Non-blocking exit check via the platform child (POSIX waitpid / Windows
            # ConPTY isalive) — os.waitpid/os.WNOHANG don't exist on native Windows.
            exit_code = self.active.process.poll()
            if exit_code is not None:
                sample = self.last_child_output_sample[-512:].decode(errors="replace").replace("\x1b", "\\x1b")
                self._debug(f"child exited pid={self.child_pid} exit_code={exit_code} last_output={sample!r}")
                # Same handling as the master_fd-EOF path: switch away (multi
                # session) or relaunch+resume (single) so Claude exiting its own
                # picker on Esc doesn't take aGiTrack down. These two detectors race;
                # whichever sees the exit first must relaunch.
                if self._handle_active_session_exit():
                    return "continue"
                if self._relaunch_backend_or_exit():
                    return "continue"
                return exit_code
        return None

    def _drain_child_output(self) -> bytes | None:
        # Delegate to the session-owned BackendProcess for the bounded PTY read loop.
        return self.active.process.drain()

    def _diag_path(self, kind: str):
        # Diagnostic logs live in the *base* repo's .agitrack/ (one file per run), not
        # the session worktree's — the worktree is removed on exit, which would
        # both destroy the log and recreate a half-dir that breaks the next resume.
        base = self.base_repo
        root = (base.repo if base is not None else self.repo.repo) / ".agitrack"
        run = self._diag_run or time.strftime("%Y%m%d-%H%M%S")
        return root / f"{kind}-{run}.log"

    def _debug(self, message: str) -> None:
        if not self.debug_proxy:
            return
        try:
            path = self._diag_path("proxy-debug")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
        except OSError:
            pass

    def _raw_capture(self, tag: str, data: bytes) -> None:
        # Append a raw I/O chunk (child output "<", user input ">", or "EOF") for
        # byte-exact replay of an interactive glitch.
        if not self.raw_capture:
            return
        try:
            path = self._diag_path("proxy-raw")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%H:%M:%S.')}{int(time.time() * 1000) % 1000:03d} {tag} {data!r}\n")
        except OSError:
            pass

    def _sanitize_state_trace(self) -> None:
        CommitEngine(self.repo, self.state, debug_fn=self._debug).sanitize_state_trace(self.backend)

    def _recover_nonempty_session(self):
        # When the recorded conversation turns out empty, fall back to this
        # worktree's newest conversation that has real content. Returns
        # (session_id, ExportedSession) or None if nothing resumable exists.
        return CommitEngine(self.repo, self.state, debug_fn=self._debug).recover_nonempty_session(
            self.backend, self.repo, self._stage_backend_resume
        )

    def _initialize_session_baseline(self) -> None:
        CommitEngine(self.repo, self.state, debug_fn=self._debug).initialize_session_baseline(
            self.backend,
            self.repo,
            should_continue_fn=self._should_continue_session,
            stage_backend_resume_fn=self._stage_backend_resume,
            debug_fn=self._debug,
        )

    def _mirror_session_to_base(self, session_id: str | None) -> None:
        # Link an aGiTrack-born conversation's transcript into the base repo's project
        # dir so a plain CLI run in the repo root can see/continue it. Idempotent
        # (no-op once linked, or when the session already lives at the base, or for
        # backends without per-directory storage).
        fn = getattr(self.backend, "mirror_to_base", None)
        if fn is None or not session_id or self.worktree is None:
            return
        try:
            fn(self.base_repo.repo, self.repo.repo, session_id)
        except Exception as error:
            self._debug(f"mirror_to_base failed: {error!r}")

    def _stage_backend_resume(self, session_id: str | None) -> None:
        fn = getattr(self.backend, "ensure_resumable", None)
        if fn is None or not session_id:
            return
        try:
            if not fn(self.repo.repo, session_id):
                self._debug(f"resume transcript not found for {session_id}")
        except Exception as error:
            self._debug(f"ensure_resumable failed: {error!r}")
        # The backend spawns with its cwd at self.repo.repo, but a resumed transcript
        # can carry an OLD recorded cwd (e.g. a worktree this session previously ran
        # in) that Claude's --resume would restore — leaving it operating in the wrong
        # directory, most visibly under --no-worktree. Align the transcript's cwd with
        # the launch dir (a no-op when they already match, so the normal in-worktree
        # resume is untouched and its shared hardlink is preserved).
        retarget = getattr(self.backend, "retarget_working_dir", None)
        if retarget is not None:
            try:
                if retarget(self.repo.repo, session_id, str(self.repo.repo)):
                    self._debug(f"retargeted resumed session {session_id} cwd to {self.repo.repo}")
            except Exception as error:
                self._debug(f"retarget_working_dir failed: {error!r}")

    def _init_screen(self) -> None:
        self.rows, self.cols = self._terminal_size()
        # HistoryScreen keeps lines that scroll off the top so aGiTrack can offer
        # scrollback for backends that stream to the normal screen (Claude).
        self.screen = _BackgroundColorEraseScreen(self.cols, max(self.rows - 1, 1), history=5000, ratio=0.5)
        self.stream = pyte.ByteStream(self.screen)
        self.scroll_back = 0
        self._in_sync_update = False

    def _feed_child_output(self, output: bytes) -> None:
        ScreenRenderer.feed(self, output, pyte_hostile_csi_re=_PYTE_HOSTILE_CSI_RE)

    def _sync_terminal_modes(self, output: bytes) -> None:
        # OpenCode enables mouse reporting on its PTY. Because aGiTrack renders the
        # screen itself, the host terminal never sees those mode switches unless
        # we mirror them explicitly.
        for mode in (
            b"9",
            b"1000",
            b"1001",
            b"1002",
            b"1003",
            b"1004",
            b"1005",
            b"1006",
            b"1007",
            b"1015",
            b"1016",
            b"2004",
        ):
            if b"\x1b[?" + mode + b"h" in output:
                os.write(sys.stdout.fileno(), b"\x1b[?" + mode + b"h")
            if b"\x1b[?" + mode + b"l" in output:
                os.write(sys.stdout.fileno(), b"\x1b[?" + mode + b"l")
        # Track whether the backend drives the mouse itself. If it does, wheel
        # events are forwarded to it; if not, aGiTrack uses the wheel for scrollback.
        for mode in (b"1000", b"1002", b"1003"):
            if b"\x1b[?" + mode + b"h" in output:
                self.child_mouse = True
            if b"\x1b[?" + mode + b"l" in output:
                self.child_mouse = False
        # Mirror keyboard-protocol negotiation (kitty protocol, modifyOtherKeys)
        # so the host starts sending the enhanced key encodings the backend asked
        # for — Shift+Enter et al. arrive on stdin already encoded and are
        # forwarded to the backend like any other input.
        for match in _KEYBOARD_PROTO_RE.finditer(output):
            seq = match.group(0)
            # Kitty-protocol sequences end in ``u``; only mirror them to a host that
            # speaks the protocol, or they leak as visible codes (the modifyOtherKeys
            # ``...m`` form is an ordinary CSI any terminal consumes, so always mirror it).
            if seq.endswith(b"u") and not self.host_kitty_keyboard:
                continue
            os.write(sys.stdout.fileno(), seq)

    def _detect_host_terminal(self) -> None:
        # Pre-reactor: called once during run() startup, before _loop() starts.
        # The bounded 0.5 s select-and-read loop inside terminal.py is correct
        # here because no PTY children are running yet and no reactor iteration
        # is live.  Do NOT convert to a modal — there is nothing to drain yet.
        debug_fn = self._debug if self.debug_proxy else None
        if self._host is not None:
            self._host.detect_host_terminal(debug_fn=debug_fn)
        else:
            TerminalHost.detect_host_terminal(self, debug_fn=debug_fn)
        # If the menu key requires shift modifier, enable the kitty keyboard protocol
        # so the terminal sends distinguishable escape sequences.
        if self.global_config.is_shift_modified:
            self._enable_kitty_keyboard()

    def _enable_kitty_keyboard(self) -> None:
        # Proactively enable the kitty keyboard protocol for shift-modified menu keys.
        # This allows Ctrl+Shift+<letter> to be distinguished from Ctrl+<letter>.
        # The protocol is: CSI > flags u, where flags=1 means "disambiguate escape codes"
        if not self.host_kitty_keyboard:
            # The host doesn't speak the protocol; sending CSI > 1 u would just leak
            # as a visible code at startup without enabling anything.
            return
        try:
            os.write(sys.stdout.fileno(), b"\x1b[>1u")
            self._kitty_keyboard_enabled = True
            if self.debug_proxy:
                self._debug("Enabled kitty keyboard protocol for shift-modified menu key")
        except OSError:
            # Terminal doesn't support it or write failed
            if self.debug_proxy:
                self._debug("Failed to enable kitty keyboard protocol (terminal may not support it)")

    def _parse_host_terminal_responses(self, data: bytes) -> None:
        TerminalHost.parse_host_terminal_responses(self, data, debug_fn=self._debug if self.debug_proxy else None)

    def _answer_terminal_queries(self, output: bytes) -> None:
        if self.master_fd is None:
            return
        # These queries (OSC color/palette, cursor-position, device attributes)
        # only appear at startup. Skip the regex scans entirely when none of
        # their cheap markers are present, so heavy scroll output (potentially
        # megabytes per drain) is not scanned with regexes and never stalls the
        # event loop — which would otherwise back up the PTY and kill the backend.
        if b"\x1b]" not in output and b"\x1b[6n" not in output and b"\x1b[c" not in output and b"\x1b[0c" not in output:
            return
        response = bytearray()
        if self.host_fg_value and re.search(rb"\x1b\]10;\?(?:\x07|\x1b\\)", output):
            response += b"\x1b]10;" + self.host_fg_value + b"\x07"
        if self.host_bg_value and re.search(rb"\x1b\]11;\?(?:\x07|\x1b\\)", output):
            response += b"\x1b]11;" + self.host_bg_value + b"\x07"
        for match in re.finditer(rb"\x1b\]4;(\d+);\?(?:\x07|\x1b\\)", output):
            value = self.host_palette.get(match.group(1))
            if value:
                response += b"\x1b]4;" + match.group(1) + b";" + value + b"\x07"
        if self.screen is not None:
            for _ in range(output.count(b"\x1b[6n")):
                row = min(self.screen.cursor.y + 1, max(self.rows - 1, 1))
                col = min(self.screen.cursor.x + 1, self.cols)
                response += b"\x1b[%d;%dR" % (row, col)
        if self.host_da and re.search(rb"\x1b\[(?:0)?c", output):
            response += self.host_da
        if response:
            try:
                self.active.process.write(bytes(response))
            except OSError:
                pass

    def _track_sync_update(self, output: bytes) -> None:
        ScreenRenderer.track_sync_update(self, output)

    def _sync_hold(self, now: float) -> bool:
        return ScreenRenderer.sync_hold(self, now, self.SYNC_MAX_HOLD)

    def _render_output(self) -> None:
        ScreenRenderer.render_output(self, self._render, self.RENDER_MIN_INTERVAL, self.SYNC_MAX_HOLD)

    def _flush_pending_render(self) -> None:
        ScreenRenderer.flush_pending_render(self, self._render, self.RENDER_MIN_INTERVAL, self.SYNC_MAX_HOLD)

    def _cursor_sequence(self) -> str:
        return ScreenRenderer.cursor_sequence(self, self.rows, self.cols, self.scroll_back)

    def _render(self) -> None:
        if self.screen is None:
            return
        # The git worker must never write to the terminal (only the main reactor
        # thread owns the screen). When it asks to render, just request a repaint;
        # the main loop flushes it within ~16 ms via _flush_pending_render.
        if not self._on_main_thread():
            self._render_pending = True
            self._wake_main_loop()
            return
        capturing = self.input.capturing
        ScreenRenderer.render(
            self,
            rows=self.rows,
            cols=self.cols,
            scroll_back=self.scroll_back,
            status_line_str=self._status_line(),
            input_capturing=capturing,
            input_text=self.input.text() if capturing else "",
            input_matches=self.input.matches() if capturing else [],
            input_selected=self.input.selected() if capturing else None,
            message=self.message,
            message_sticky=self._message_sticky,
            message_until=self.message_until,
            message_scroll=self._message_scroll,
        )

    def _append_command_palette(self, parts: list[str]) -> None:
        ScreenRenderer.append_command_palette(
            self,
            parts,
            rows=self.rows,
            cols=self.cols,
            input_text=self.input.text(),
            input_matches=self.input.matches(),
            input_selected=self.input.selected(),
        )

    def _append_message_popup(self, parts: list[str], message: str) -> None:
        ScreenRenderer.append_message_popup(
            self, parts, message, rows=self.rows, cols=self.cols, scroll=self._message_scroll
        )

    def _append_box(
        self, parts: list[str], row: int, col: int, width: int, lines: list[str], highlight: str | None = None
    ) -> None:
        ScreenRenderer.append_box(self, parts, row, col, width, lines, highlight, rows=self.rows)

    def _render_line(self, cells, sel: tuple[int, int] | None = None) -> str:
        return ScreenRenderer.render_line(self, cells, sel, cols=self.cols)

    def _hold_incomplete_tail(self, data: bytes) -> tuple[bytes, bytes]:
        # If the read ends mid escape-sequence (e.g. a mouse report split across
        # reads), hold the trailing partial so it is completed on the next read
        # rather than leaking to the backend as stray bytes (the "[<35;..." hex, or
        # a half-delivered legacy "[M" X10 report).
        match = _INCOMPLETE_TAIL_RE.search(data) or _INCOMPLETE_X10_RE.search(data)
        if match:
            return data[: match.start()], data[match.start() :]
        return data, b""

    def _intercept_scroll(self, data: bytes) -> bytes:
        # Backends that drive the mouse (OpenCode) get all input forwarded so they
        # scroll themselves. For backends that do not (Claude), aGiTrack handles the
        # mouse: the wheel scrolls history, drag selects-and-copies, and
        # PageUp/PageDown also scroll. Consumed events are stripped from input.
        if self.child_mouse:
            return data
        page = max(self.rows - 2, 1)
        for match in _PAGE_KEY_RE.finditer(data):
            self._scroll(page if match.group(1) == b"5" else -page)
        data = _PAGE_KEY_RE.sub(b"", data)
        if b"\x1b[<" in data:
            for match in _SGR_MOUSE_EVENT_RE.finditer(data):
                self._handle_mouse(int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(4))
            data = _SGR_MOUSE_RE.sub(b"", data)
        if b"\x1b[M" in data:
            # Legacy X10 reports (terminals that ignored ?1006). The three bytes are button,
            # column, row each offset by 32; the offset button matches the SGR button numbers
            # _handle_mouse expects (wheel = 64/65), so the wheel still scrolls. Strip them so
            # the raw coordinate bytes don't leak into the backend's input.
            for match in _X10_MOUSE_RE.finditer(data):
                report = match.group()
                self._handle_mouse(report[3] - 32, report[4] - 32, report[5] - 32, b"M")
            data = _X10_MOUSE_RE.sub(b"", data)
        return data

    def _handle_mouse(self, button: int, col: int, row: int, kind: bytes) -> None:
        # Only the wheel is aGiTrack's to act on (history scrollback for a backend that
        # doesn't drive the mouse itself, e.g. Claude). Left-button press/drag/release are
        # deliberately left alone so the TERMINAL owns text selection.
        #
        # aGiTrack used to drag-select-and-copy here, but that path only ever ran in
        # terminals that forward a plain drag to the application (mouse mode 1000) — and
        # there it both SUPPRESSED the terminal's native selection and popped an unwanted
        # "Copied N char(s) to clipboard" message (#112). Terminals that select natively
        # never forward the drag, so they never hit this code and are unaffected; dropping
        # it removes only the harmful case. The mouse bytes are still stripped from the
        # input forwarded to the backend by _intercept_scroll.
        if button & 64:  # wheel
            self._scroll(-3 if button & 1 else 3)

    def _selection_ranges(self) -> dict[int, tuple[int, int]]:
        return ScreenRenderer.selection_ranges(self, self.cols)

    def _copy_selection(self) -> None:
        ScreenRenderer.copy_selection(
            self, self.rows, self.cols, self._copy_to_clipboard, lambda msg, **kw: self._set_message(msg, **kw)
        )

    def _copy_to_clipboard(self, text: str) -> None:
        payload = text.encode("utf-8", errors="replace")
        if shutil.which("pbcopy"):
            try:
                subprocess.run(["pbcopy"], input=payload, check=False)
                return
            except OSError:
                pass
        # OSC 52 clipboard fallback for terminals that support it.
        encoded = base64.b64encode(payload).decode("ascii")
        try:
            os.write(sys.stdout.fileno(), b"\x1b]52;c;" + encoded.encode("ascii") + b"\x07")
        except OSError:
            pass

    def _history_len(self) -> int:
        return ScreenRenderer.history_len(self)

    def _scroll(self, delta: int) -> None:
        ScreenRenderer.scroll(self, delta, self._render)

    def _visible_lines(self) -> list:
        return ScreenRenderer.visible_lines(self, self.rows)

    def _cell_sgr(self, cell) -> str:
        return ScreenRenderer.cell_sgr(self, cell)

    def _color_code(self, color: str, *, foreground: bool) -> str | None:
        return ScreenRenderer.color_code(self, color, foreground=foreground)

    def _hex_color_code(self, color: str, *, foreground: bool) -> str:
        return ScreenRenderer.hex_color_code(self, color, foreground=foreground)

    # Public aliases used by ScreenRenderer's internal self-calls when 'self'
    # is a ProxyRunner (duck-typing delegation; no underscore prefix).
    def cell_sgr(self, cell) -> str:
        return ScreenRenderer.cell_sgr(self, cell)

    def color_code(self, color: str, *, foreground: bool) -> str | None:
        return ScreenRenderer.color_code(self, color, foreground=foreground)

    def hex_color_code(self, color: str, *, foreground: bool) -> str:
        return ScreenRenderer.hex_color_code(self, color, foreground=foreground)

    def history_len(self) -> int:
        return ScreenRenderer.history_len(self)

    def scroll(self, delta: int, render_fn) -> None:
        ScreenRenderer.scroll(self, delta, render_fn)

    def visible_lines(self, rows: int) -> list:
        return ScreenRenderer.visible_lines(self, rows)

    def selection_ranges(self, cols: int) -> dict:
        return ScreenRenderer.selection_ranges(self, cols)

    def copy_selection(self, rows: int, cols: int, copy_fn, set_msg_fn) -> None:
        ScreenRenderer.copy_selection(self, rows, cols, copy_fn, set_msg_fn)

    def render_line(self, cells, sel=None, *, cols: int) -> str:
        return ScreenRenderer.render_line(self, cells, sel, cols=cols)

    def append_box(
        self, parts, row, col, width, lines, highlight=None, *, rows: int, scrollable: bool = False, scroll: int = 0
    ) -> None:
        ScreenRenderer.append_box(
            self, parts, row, col, width, lines, highlight, rows=rows, scrollable=scrollable, scroll=scroll
        )

    def append_command_palette(
        self, parts, *, rows: int, cols: int, input_text: str, input_matches, input_selected
    ) -> None:
        ScreenRenderer.append_command_palette(
            self,
            parts,
            rows=rows,
            cols=cols,
            input_text=input_text,
            input_matches=input_matches,
            input_selected=input_selected,
        )

    def append_message_popup(self, parts, message: str, *, rows: int, cols: int, scroll: int = 0) -> None:
        ScreenRenderer.append_message_popup(self, parts, message, rows=rows, cols=cols, scroll=scroll)

    def wrapped_message_height(self, lines, cols: int) -> int:
        return ScreenRenderer.wrapped_message_height(self, lines, cols)

    def cursor_sequence(self, rows: int, cols: int, scroll_back: int) -> str:
        return ScreenRenderer.cursor_sequence(self, rows, cols, scroll_back)

    def sync_hold(self, now: float, sync_max_hold: float) -> bool:
        return ScreenRenderer.sync_hold(self, now, sync_max_hold)

    def render_output(self, render_fn, render_min_interval: float, sync_max_hold: float) -> None:
        ScreenRenderer.render_output(self, render_fn, render_min_interval, sync_max_hold)

    def flush_pending_render(self, render_fn, render_min_interval: float, sync_max_hold: float) -> None:
        ScreenRenderer.flush_pending_render(self, render_fn, render_min_interval, sync_max_hold)

    def track_sync_update(self, output: bytes) -> None:
        ScreenRenderer.track_sync_update(self, output)

    def feed(self, output: bytes, *, pyte_hostile_csi_re) -> None:
        ScreenRenderer.feed(self, output, pyte_hostile_csi_re=pyte_hostile_csi_re)

    def init_screen(self, rows: int, cols: int) -> None:
        ScreenRenderer.init_screen(self, rows, cols)

    # Public aliases used by TerminalHost's internal self-calls when 'self' is a
    # ProxyRunner (same duck-typing contract as the ScreenRenderer aliases).
    def set_raw(self) -> None:
        TerminalHost.set_raw(self)

    def set_cooked(self) -> None:
        TerminalHost.set_cooked(self)

    def enable_host_mouse(self) -> None:
        TerminalHost.enable_host_mouse(self)

    def disable_host_terminal_modes(self) -> None:
        TerminalHost.disable_host_terminal_modes(self)

    def parse_host_terminal_responses(self, data: bytes, *, debug_fn=None) -> None:
        TerminalHost.parse_host_terminal_responses(self, data, debug_fn=debug_fn or self._debug)

    def _menu_label(self) -> str:
        # Human-readable name of the configured menu key, for the status line
        # and every message that points the user at the aGiTrack menu.
        return getattr(self.global_config, "menu_key_label", None) or "Ctrl-G"

    def _status_line(self) -> str:
        return ScreenRenderer.status_line(
            self,
            cols=self.cols,
            name=self.name,
            backend_name=self.backend.name,
            session_id=self.state.backend_session_id,
            base_branch=self._base_branch,
            current_dir_branch=self._repo_dir_branch,
            worktree=self.worktree,
            scroll_back=self.scroll_back,
            user_declined=self._user_declined,
            short_session_fn=_short_session,
            menu_label=self._menu_label(),
            summarizer_on=self._summarization_enabled(),
            # The project the agent works on: the BASE repository, not the
            # session worktree under .agitrack/worktrees/ (an internal detail whose
            # long path mostly repeats the session name shown next to it).
            cwd=str(repo_path)
            if (repo_path := getattr(self.base_repo, "repo", None) or getattr(self.repo, "repo", None))
            else None,
        )

    def _summarization_enabled(self) -> bool:
        # The GLOBAL config is the durable source of truth (survives restarts and worktree
        # teardown), so it wins. The per-session worktree state — which always defaults to
        # "on" on a fresh worktree — must NOT shadow it, or the toggle never persists. Fall
        # back to the per-session value only when there is no global config (e.g. tests).
        gc_enabled = getattr(self.global_config, "summarization_enabled", None)
        if gc_enabled is not None:
            return bool(gc_enabled)
        state_enabled = getattr(self.state, "summarization_enabled", None)
        return True if state_enabled is None else bool(state_enabled)

    def _render_status(self, text: str) -> None:
        # Writes straight to stdout, so only the main reactor thread may run it: the
        # git worker calls it (verbose mode) but must never touch the screen. This is
        # a debug-only status line, so dropping it off-thread is harmless.
        if not self._on_main_thread():
            return
        prompt = text.replace("\r", "").replace("\n", "")
        line = f" aGiTrack> {prompt}"[: self.cols].ljust(self.cols)
        os.write(sys.stdout.fileno(), f"\x1b[{self.rows};1H\x1b[7m{line}\x1b[0m".encode())

    def _enter_host_screen(self) -> None:
        if self._host is not None:
            self._host.enter_host_screen()
        else:
            TerminalHost.enter_host_screen(self)

    def _enable_host_mouse(self) -> None:
        TerminalHost.enable_host_mouse(self)

    # --- menu navigation convention -----------------------------------------
    #
    # Every Ctrl-G menu is one ``while True`` loop and returns just TWO signals, so **Esc
    # always moves up exactly one level** and the code's call hierarchy mirrors the on-screen
    # hierarchy. To author a menu: build options → ``_select_popup`` → dispatch, where
    #
    #   Esc (``_select_popup`` returns None)  → ``return self._MENU_UP``  (back out one level)
    #   a context transition (switch/new/resume a session)
    #                                         → ``return self._MENU_DONE`` (unwind to the agent)
    #   any other action (config, or a child menu the user backed out of)
    #                                         → ``continue``               (re-show this menu)
    #
    # A child menu is entered by CALLING it; the parent does
    # ``if child() == self._MENU_DONE: return self._MENU_DONE`` else ``continue`` — so the
    # child's Esc lands back on this parent, while a transition unwinds past every level.
    # ``_after_menu_command`` turns a top-level menu's UP into "re-open the Ctrl-G palette",
    # the parent of every command menu. Navigation is silent — no "closed"/"cancelled"
    # message on the way up — and modal closes don't repaint the bare screen (see
    # ``_run_modal``), so stepping between levels is instant and flicker-free.
    #
    # Parent → child today: _run_command → {_session_menu, _settings_menu, summarizer menu,
    # backend picker}; _session_menu → {_rename_session_menu, _change_session_merge_branch_menu,
    # _share_session, _manage_shared_sessions_menu, _resume_session_menu,
    # _resume_shared_session_menu, …}; _manage_shared_sessions_menu → _manage_one_shared_session;
    # _settings_menu → {_edit_one_setting, _settings_timings_menu → _edit_one_timing}.
    _MENU_UP = "up"
    _MENU_DONE = "done"

    def _after_menu_command(self, signal: str) -> None:
        """A top-level command menu closed. Esc/back (UP) goes up one level to the Ctrl-G
        command palette; a transition (DONE) drops through to the agent."""
        if signal == self._MENU_UP:
            self._reopen_command_palette()

    def _reopen_command_palette(self) -> None:
        # Re-open the input-layer Ctrl-G palette (the parent of every command menu) so Esc
        # out of a command lands back here rather than at the agent. Mirrors the state reset
        # the palette does when first opened with the menu key.
        self.input.capturing = True
        self.input.buffer.clear()
        self.input.selected_index = 0
        self.input.escape_buffer = None
        self.input.extra_commands = ["merge"] if self._has_unmerged_work() else []
        self._render()

    def _run_command(self, command: str) -> None:
        # aGiTrack commands in proxy mode are triggered via Ctrl-G and are plain
        # names; ":" is not a command trigger here (it is forwarded to the
        # backend like any other input). On a confirmed "exit"/"quit" this runs
        # the teardown flow (which sets self._exiting); the caller checks that
        # flag to break the reactor loop.
        name, _, arg = command.partition(" ")
        if name in {"exit", "quit"}:
            if not self._run_exit_flow():
                self._render()
            return

        if name == "git-user-commit":
            created = self._create_user_commit_popup(
                repo=self.base_repo, state=self._user_state(), include_declined=True
            )
            self._set_message(
                "Committed your changes to the base repo." if created else "No changes to commit in the base repo."
            )
            self._reload_user_declined()
            self._render()
            return

        if name == "git-unstaged":
            self._prune_declined_untracked(self.base_repo, self._user_state())
            self._reload_user_declined()
            declined = self._user_declined
            if declined:
                self._set_message("Intentionally unstaged files:\n" + "\n".join(f"  {path}" for path in declined))
            else:
                self._set_message("No intentionally unstaged files.")
        elif name == "agent-backend":
            backends = available_backends()
            selected = arg.strip() or self._select_popup("Backend Agent", backends)
            if selected is None:  # Esc on the picker → up one level to the palette
                self._after_menu_command(self._MENU_UP)
                return
            if selected not in backends:
                self._set_message(f"Unknown backend: {selected}. Available: {', '.join(backends)}")
            elif selected == self.state.backend:
                self._set_message(f"Backend already set to {selected}")
            else:
                self._switch_backend(selected)
                return
        elif name == "sessions":
            self._after_menu_command(self._handle_session_command(arg))
            return
        elif name == "settings":
            self._after_menu_command(self._settings_menu())
            return
        elif name == "update":
            self._handle_update_command()
            return
        elif name == "dashboard":
            self._handle_dashboard_command()
            return
        elif name == "merge":
            self._handle_merge_command()
            return
        elif name == "":
            self._set_message("Select an aGiTrack command.")
        else:
            self._set_message(f"Unknown aGiTrack command: {name}")
        self._render()

    # --- settings menu ------------------------------------------------------
    #
    # The menu is a small form: edits accumulate as PENDING changes (each with the scope
    # the user chose — repo-local or global) and are written only when the user confirms
    # "save" on the way out. Esc always goes up ONE level (value editor → list, scope →
    # value editor, sub-list → main list, main list → close), and closing with unsaved
    # changes asks whether to save or discard them.

    @staticmethod
    def _fmt_setting(value: object) -> str:
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, list):
            return ", ".join(str(p) for p in value) if value else "(none)"
        return str(value) if value not in (None, "") else "(unset)"

    def _pending_count(self) -> int:
        return len(self._settings_pending) + len(self._settings_pending_timings)

    def _choose_scope(self, label: str) -> str | None:
        """Ask whether an edit applies repo-locally or globally. None = Esc/Back."""
        pick = self._select_popup(
            f"Apply '{label}' to:",
            [
                "This repository only (.agitrack/config.json)",
                "Global — all repositories (~/.agitrack/config.json)",
                "← Back",
            ],
        )
        if pick is None or pick.startswith("←"):
            return None
        return "repo" if pick.startswith("This repository") else "global"

    def _settings_specs(self) -> list[dict]:
        """Declarative registry of the user-editable config options shown in the settings
        menu. Labels are self-explanatory and the ORDER groups related settings (backend →
        session isolation → agent behavior → commit summaries → aGiTrack itself). ``kind``
        drives the editor; ``restart`` marks settings resolved at launch (so a change applies
        to new sessions / the next launch). Effective values are read from ``global_config``
        (repo overlay > global > built-in default).

        Note the two distinct "worktree" settings, kept adjacent and worded so they aren't
        confused: ``use_worktrees`` decides WHETHER each session gets its own worktree, while
        ``sandbox`` only CONFINES the agent's writes to that worktree (and is moot when
        worktrees are off)."""
        return [
            {
                "key": "default_backend",
                "label": "Default coding agent (Claude Code or OpenCode)",
                "kind": "choice",
                "options": ["claude", "opencode"],
                "restart": True,
            },
            # --- session isolation: the worktree toggle, then the sandbox that depends on it ---
            {
                "key": "use_worktrees",
                "label": "Isolate each session in its own git worktree (off: edit the current branch directly)",
                "kind": "bool",
                "restart": True,
            },
            {
                "key": "sandbox",
                "label": "Sandbox: block the agent from writing outside its worktree (no effect when worktrees are off)",
                "kind": "bool",
                "restart": True,
            },
            {
                "key": "allowed_edit_paths",
                "label": "Folders/files the agent may write to outside its worktree (sandbox carve-out)",
                "kind": "paths",
                "restart": True,
            },
            # --- agent behavior ---
            {
                "key": "commit_guidance",
                "label": "Ask the agent not to make its own git commits (aGiTrack commits each turn)",
                "kind": "bool",
                "restart": True,
            },
            # --- commit summaries ---
            {"key": "summarization_enabled", "label": "Write an AI summary for each commit", "kind": "bool"},
            {"key": "summarization_model", "label": "Model used to write commit summaries", "kind": "model"},
            # --- aGiTrack itself ---
            {"key": "check_for_updates", "label": "Automatically check for aGiTrack updates", "kind": "bool"},
            {
                "key": "menu_key",
                "label": "Key that opens this aGiTrack menu (e.g. ctrl-g)",
                "kind": "text",
                "restart": True,
            },
            {"key": "timings", "label": "Polling & debounce timings (advanced) …", "kind": "timings"},
        ]

    def _setting_value_display(self, spec: dict) -> str:
        key, kind = spec["key"], spec["kind"]
        if kind == "timings":
            n = len(self._settings_pending_timings)
            return f"{n} unsaved change(s)" if n else ""
        if key in self._settings_pending:  # a pending (unsaved) edit shows its new value
            value, scope, _ = self._settings_pending[key]
            return f"{self._fmt_setting(value)}  · UNSAVED → {scope}"
        if kind == "bool":
            text = "on" if bool(getattr(self.global_config, key)) else "off"
        elif kind == "paths":
            paths = self.global_config.allowed_edit_paths
            text = ", ".join(paths) if paths else "(none)"
        else:
            value = getattr(self.global_config, key, None)
            text = str(value) if value not in (None, "") else "(default)"
        suffix = {"repo": " · repo", "global": " · global", "default": ""}[self.global_config.source(key)]
        return f"{text}{suffix}"

    def _settings_menu(self) -> str:
        """Ctrl-G → "settings": browse and edit ALL config options. Changes are collected
        as PENDING edits (each with its chosen scope) and written only when you confirm on
        the way out. Esc goes up one level; closing with unsaved changes asks to save them.
        Returns _MENU_UP so closing the menu lands back on the Ctrl-G palette."""
        self._settings_pending = {}
        self._settings_pending_timings = {}
        while True:
            specs = self._settings_specs()
            options = []
            for spec in specs:
                disp = self._setting_value_display(spec)
                options.append(f"{spec['label']}: {disp}" if disp else spec["label"])
            pending = self._pending_count()
            close = f"← Close (save {pending} change(s))" if pending else "← Close"
            options += ["", close]
            title = "Settings — Esc = back; edits are saved only when you close"
            choice = self._select_popup(title, options)
            if choice is None or choice.startswith("← Close") or not choice.strip():
                # Leaving the top level: offer to save/discard any unsaved edits.
                if pending and self._confirm_save_pending() == "keep":
                    continue  # "Keep editing" → stay in the menu
                self._set_message("Settings closed.")
                self._render()
                return self._MENU_UP
            spec = specs[options.index(choice)]
            if spec["kind"] == "timings":
                self._settings_timings_menu()
            else:
                self._edit_one_setting(spec)

    def _edit_one_setting(self, spec: dict) -> None:
        """Edit one setting into a PENDING change. Esc at the value prompt returns to the
        list; Esc at the scope prompt returns to the value prompt (one level up each time)."""
        key, kind, label = spec["key"], spec["kind"], spec["label"]
        while True:
            if kind == "bool":
                pick = self._select_popup(label, ["Turn ON", "Turn OFF", "← Back"])
                if pick is None or pick.startswith("←"):
                    return
                value: object = pick == "Turn ON"
            elif kind == "choice":
                pick = self._select_popup(label, [*spec["options"], "← Back"])
                if pick is None or pick.startswith("←"):
                    return
                value = pick
            elif kind == "model":
                chosen = self._choose_summarizer_model()  # picker (smallest tier first) or free-text
                if chosen is _NO_MODEL_PICK:
                    return
                value = chosen
            elif kind == "paths":
                current = self._pending_paths(key)
                raw = self._prompt_popup(
                    label, f"Paths separated by '{os.pathsep}' (blank = none):", default=os.pathsep.join(current)
                )
                if raw is None:
                    return
                value = [p.strip() for p in raw.split(os.pathsep) if p.strip()]
            else:  # text
                raw = self._prompt_popup(label, "New value (blank = unset):", default=self._pending_text(key))
                if raw is None:
                    return
                value = raw.strip() or None
            scope = self._choose_scope(label)
            if scope is None:  # Esc at scope → one level up: re-edit the value
                continue
            self._settings_pending[key] = (value, scope, bool(spec.get("restart")))
            self._set_message(
                f"'{label}' → {self._fmt_setting(value)} ({scope}) — unsaved; choose Close to save.", seconds=7.0
            )
            self._render()
            return

    def _pending_paths(self, key: str) -> list[str]:
        if key in self._settings_pending:
            pending = self._settings_pending[key][0]
            if isinstance(pending, list):
                return [str(p) for p in pending]
        return self.global_config.allowed_edit_paths

    def _pending_text(self, key: str) -> str:
        if key in self._settings_pending:
            return self._fmt_setting(self._settings_pending[key][0]).replace("(unset)", "")
        return str(getattr(self.global_config, key, None) or "")

    def _settings_timings_menu(self) -> None:
        from agitrack.config.settings import DEFAULT_TIMINGS

        keys = list(DEFAULT_TIMINGS)
        while True:
            timings = self.global_config.timings
            options = []
            for k in keys:
                if k in self._settings_pending_timings:
                    v, scope = self._settings_pending_timings[k]
                    options.append(f"{k}: {v:g}s  · UNSAVED → {scope}")
                else:
                    options.append(f"{k}: {timings[k]:g}s")
            options += ["", "← Back"]
            choice = self._select_popup("Timings (seconds) — Esc/← Back returns to settings", options)
            if choice is None or choice == "← Back" or not choice.strip():
                return  # one level up: back to the settings list
            tkey = keys[options.index(choice)]
            self._edit_one_timing(tkey, timings[tkey])

    def _edit_one_timing(self, tkey: str, current: float) -> None:
        while True:
            default = (
                f"{self._settings_pending_timings[tkey][0]:g}"
                if tkey in self._settings_pending_timings
                else str(current)
            )
            raw = self._prompt_popup(tkey, f"New value in seconds (current {current:g}):", default=default)
            if raw is None:
                return  # back to the timings list
            try:
                seconds = float(raw)
                if seconds <= 0:
                    raise ValueError
            except ValueError:
                self._set_message("Enter a positive number of seconds.")
                self._render()
                continue
            scope = self._choose_scope(tkey)
            if scope is None:  # Esc at scope → re-edit the value
                continue
            self._settings_pending_timings[tkey] = (seconds, scope)
            self._set_message(f"'{tkey}' → {seconds:g}s ({scope}) — unsaved; choose Close to save.", seconds=7.0)
            self._render()
            return

    def _confirm_save_pending(self) -> str:
        """Prompt to save/discard pending settings edits when closing. Returns 'keep' if the
        user chose to keep editing (the menu should stay open), else 'saved'/'discarded'."""
        n = self._pending_count()
        pick = self._select_popup(
            f"You have {n} unsaved settings change(s). Save them?",
            ["Yes, save them", "No, discard them", "← Keep editing"],
        )
        if pick is None or pick.startswith("←"):
            return "keep"
        if pick.startswith("No"):
            self._settings_pending = {}
            self._settings_pending_timings = {}
            self._set_message(f"Discarded {n} unsaved settings change(s).", seconds=6.0)
            self._render()
            return "discarded"
        # Write every pending edit to its chosen scope.
        needs_restart = False
        for key, (value, scope, restart) in self._settings_pending.items():
            self.global_config.set(key, value, scope=scope)
            needs_restart = needs_restart or restart
        by_scope: dict[str, dict[str, float]] = {}
        for tkey, (value, scope) in self._settings_pending_timings.items():
            by_scope.setdefault(scope, {})[tkey] = value
        for scope, changes in by_scope.items():
            store = self.global_config.repo_data if scope == "repo" else self.global_config.data
            merged = dict(store.get("timings") or {})
            merged.update(changes)
            self.global_config.set("timings", merged, scope=scope)
            needs_restart = True  # timings are read at launch
        self._settings_pending = {}
        self._settings_pending_timings = {}
        if needs_restart:
            # These settings are read only at launch (worktrees on/off, default backend,
            # timings), so they don't apply to the running session — offer to restart now
            # rather than silently leaving them inert until the next manual launch.
            pick = self._select_popup(
                f"Saved {n} change(s). Some take effect only after a restart. Restart aGiTrack now?",
                ["Yes, restart now", "Not now"],
            )
            if pick == "Yes, restart now":
                self._restart_now("Restarting aGiTrack to apply the new settings…")
            else:
                self._set_message(
                    f"Saved {n} settings change(s). The restart-only ones apply next time you start aGiTrack.",
                    seconds=10.0,
                )
                self._render()
            return "saved"
        self._set_message(f"Saved {n} settings change(s).", seconds=8.0)
        self._render()
        return "saved"

    def _handle_dashboard_command(self) -> None:
        """Ctrl-G → "dashboard": serve aGiTrack's metrics dashboard for this repo and
        open it in the browser. The dashboard is read-only and runs as a separate
        background process (#110) — keeping its git-log work and energy use off the TUI
        process — owned by this aGiTrack so it is reused if already up and killed on exit
        (here and via the child's own owner-death watchdog)."""
        from agitrack.metrics import (
            clear_handshake,
            log_path,
            open_dashboard_in_browser,
            remote_browser_hint,
            spawn_dashboard_daemon,
            wait_for_handshake,
        )

        if self._dashboard_proc is not None and self._dashboard_proc.poll() is None:
            url = self._dashboard_url or ""
            opened = open_dashboard_in_browser(url)
            self._set_message(
                f"Dashboard already running at {url}."
                if opened
                else f"Dashboard running. {remote_browser_hint(url, 0)}"
            )
            self._render()
            return
        try:
            clear_handshake(self.base_repo)  # drop any record from a dead earlier daemon
            proc = spawn_dashboard_daemon(
                self.base_repo, owner_pid=os.getpid(), email_logins=self._dashboard_email_logins()
            )
            record = wait_for_handshake(self.base_repo, pid=proc.pid, timeout=5.0)
            if record is None:
                # The child died before binding, or never published. Reap it and point
                # the user at its log so a startup failure isn't swallowed silently.
                if proc.poll() is None:
                    proc.terminate()
                self._set_message(f"Could not start the dashboard. See {log_path(self.base_repo)}.")
                self._render()
                return
            url = str(record.get("url", ""))
            port = int(record.get("port", 0))
            self._dashboard_proc = proc
            self._dashboard_url = url
            # Only open a browser when it would land on THIS machine; on a remote/SSH/
            # Mosh host, tell the user how to reach the forwarded URL from their own
            # machine instead of opening a (headless) browser on the remote.
            if open_dashboard_in_browser(url):
                self._set_message(f"Dashboard live at {url} — opening in your browser.")
            else:
                self._set_message(f"Dashboard live. {remote_browser_hint(url, port)}")
        except Exception as error:
            self._set_message(f"Could not start the dashboard: {error}")
        self._render()

    def _dashboard_email_logins(self) -> dict[str, str]:
        """Map the current user's git email → their GitHub login, so the dashboard can
        label this session's fresh, unpushed commits with a GitHub ID even though `gh`
        (which only knows commits already on the remote) can't resolve them yet.
        Best-effort: an empty map just means the email/no-reply heuristics still apply."""
        try:
            login = self._cached_or_resolve_login()
            email = self.base_repo._run(["git", "config", "user.email"], check=False).stdout.strip()
            if login and email:
                return {email.lower(): login}
        except Exception:
            pass
        return {}

    def _stop_dashboard(self) -> None:
        """Kill the dashboard process if it was started this session.

        Called on the TUI's teardown path so the dashboard never outlives aGiTrack.
        (The child also self-terminates via its owner-death watchdog, which covers a
        SIGKILL of the TUI where this never runs.)"""
        proc = self._dashboard_proc
        if proc is None:
            return
        self._dashboard_proc = None
        self._dashboard_url = None
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass
        try:
            from agitrack.metrics import clear_handshake

            clear_handshake(self.base_repo)
        except Exception:
            pass

    def _popup_read_input(self) -> bytes:
        # Read the user's next keypress for a modal popup WITHOUT suspending the
        # rest of the event loop: every session's PTY keeps being drained while
        # the popup waits, since back-pressure on a full PTY buffer blocks the
        # backend's writes and can stall or kill it (a popup can stay open for a
        # long time, and agents keep streaming behind it).
        stdin_fd = self._stdin_fileno()
        dead: set[int] = set()
        while True:
            background = self._background_fds() if self.sessions else {}
            master = self.master_fd
            fds = [stdin_fd]
            if master is not None and master not in dead:
                fds.append(master)
            fds.extend(fd for fd in background if fd not in dead)
            readable, _, _ = select.select(fds, [], [], 1.0)
            for fd in readable:
                if fd == stdin_fd:
                    continue
                if fd in background:
                    # A dead background session is stopped and dropped by
                    # _pump_background, so the next pass stops selecting on it.
                    self._pump_background(background[fd])
                elif fd == master:
                    output = self._drain_child_output()
                    if output is None:
                        dead.add(fd)  # EOF: let the main loop handle the exit
                        continue
                    self.last_child_output = time.monotonic()
                    self.last_child_output_sample = (self.last_child_output_sample + output)[-4096:]
                    self._answer_terminal_queries(output)
                    self._sync_terminal_modes(output)
                    self._track_sync_update(output)
                    # Feed the screen model but don't render: the popup overlays
                    # the view; the next normal render shows the updated screen.
                    self._feed_child_output(output)
            if stdin_fd in readable:
                return self._read_stdin(32)

    def _run_modal(self, modal: "PromptModal | SelectModal") -> "str | None":
        """Run *modal* to completion, keeping all session PTYs draining.

        This is the shared reactor iteration for modal dialogs.  It repeatedly:
          1. Renders the modal's current message via ``_set_message`` + ``_render``.
          2. Calls ``_popup_read_input()`` — which selects on stdin AND every
             session PTY so back-pressure never builds up while the popup is open.
          3. Passes the bytes to ``modal.feed()`` and acts on the returned action:

             ``("done",   value)``  — clears the message and returns value.
             ``("cancel", None)``   — clears the message and returns None.
             ``("exit",   None)``   — calls ``_run_exit_flow()``; returns None on
                                      confirmed exit, otherwise redraws and continues.
             ``("redraw", None)``   — redraws and continues.

        On ``done``/``cancel`` we deliberately do NOT repaint here: the modal's last
        frame stays on screen and we only flag ``_render_pending``. When this modal was
        opened from a parent menu, the parent's next ``_select_popup`` paints the next
        menu straight over this one — so stepping between menu levels never flashes the
        bare backend screen in between (smooth, instant navigation). When nothing follows
        (the interaction returns to the agent), the reactor flushes that one pending frame
        within a few ms, clearing the popup cleanly.

        The call-shape is synchronous, which preserves the ~25 existing call
        sites unmodified.  ``_prompt_popup`` and ``_select_popup`` are thin
        facades that construct the appropriate modal and delegate here.

        When called from the git worker thread (which must never touch stdin/the
        screen), the modal is marshaled to the main reactor thread via
        ``_run_modal_via_main`` and this call blocks until the user answers there.
        """
        if not self._on_main_thread():
            return self._run_modal_via_main(modal)
        return self._run_modal_inline(modal)

    def _run_modal_inline(self, modal: "PromptModal | SelectModal") -> "str | None":
        """The actual modal loop — always runs on the main reactor thread (directly,
        or via ``_drain_modal_mailbox`` for a worker-queued dialog). Reads stdin and
        paints the screen, so it must never run off the main thread."""
        while True:
            self._set_message(modal.render_message(), seconds=60)
            self._render()
            data = self._popup_read_input()
            action, value = modal.feed(data)
            if action == "done":
                self._clear_message()
                self._render_pending = True
                return value
            if action == "cancel":
                self._clear_message()
                self._render_pending = True
                return None
            if action == "exit":
                if self._run_exit_flow():
                    return None
                # Exit declined: redraw the modal and keep listening.

    def _on_main_thread(self) -> bool:
        """Whether the caller is the main reactor thread (or no worker is running,
        as in tests/early construction, where everything is effectively 'main')."""
        return self._main_thread_ident is None or threading.get_ident() == self._main_thread_ident

    def _run_modal_via_main(self, modal: "PromptModal | SelectModal") -> "str | None":
        """Worker → main hand-off: queue *modal* for the reactor thread to present
        and block until it returns the user's answer. The git worker has no access
        to stdin or the screen, so every dialog it raises is shown by the main
        thread (which drains ``_modal_mailbox`` in its timers phase)."""
        request = _ModalRequest(modal)
        self._modal_mailbox.put(request)
        self._wake_main_loop()  # don't wait for the next poll tick to present it
        request.done.wait()
        return request.result

    def _drain_modal_mailbox(self) -> None:
        """Main thread: present every modal the worker queued, returning each answer
        to the blocked worker. Safe to call when no worker is running (no-op)."""
        while True:
            try:
                request = self._modal_mailbox.get_nowait()
            except queue.Empty:
                return
            try:
                request.result = self._run_modal_inline(request.modal)  # on main thread
            finally:
                request.done.set()

    def _wake_main_loop(self) -> None:
        """Wake the reactor out of ``select`` at once (e.g. a worker queued a dialog
        or finished work that should repaint). Writes a byte to the self-pipe whose
        read end is in the select set. No-op before the waker exists (tests)."""
        if self._waker is None:
            return
        self._waker.wake()

    def _acquire_pipeline_lock_from_main(self) -> None:
        """Acquire ``_pipeline_lock`` from the main thread WITHOUT deadlocking on a
        worker that holds it while blocked on a modal: keep draining the modal
        mailbox while we wait, so the worker's dialog is serviced and it can release.
        Pair every call with ``self._pipeline_lock.release()``."""
        while not self._pipeline_lock.acquire(timeout=0.02):
            self._drain_modal_mailbox()
        self._drain_modal_mailbox()

    def _prompt_popup(self, title: str, prompt: str, *, default: str = "") -> str | None:
        """Free-text input popup.  Thin facade over ``_run_modal(PromptModal(...))``.

        Tests stub this method heavily; the name and signature are stable.
        """
        return self._run_modal(PromptModal(title, prompt, default=default))

    def _select_popup(self, title: str, options: list[str], *, detail: list[str] | None = None) -> str | None:
        """Selection popup.  Thin facade over ``_run_modal(SelectModal(...))``.

        Tests stub this method heavily; the name and signature are stable.
        Returns ``""`` immediately when *options* is empty (unchanged behaviour).
        ``detail`` is an optional list of lines (e.g. a file list) shown between the
        title and the options, windowed and PgUp/PgDn-scrollable when it overflows.
        """
        if not options:
            return ""
        return self._run_modal(SelectModal(title, options, detail=detail, viewport_rows=self.rows))

    def _create_user_commit_popup(
        self,
        *,
        repo: GitRepo | None = None,
        state: AgitrackState | None = None,
        include_declined: bool = False,
    ) -> bool:
        # Defaults to the active worktree (capturing uncommitted worktree changes
        # before the next prompt). The user-facing `git-user-commit` command passes
        # the base repo/state instead, since the user's own edits live there.
        # ``include_declined`` re-offers every untracked (added-but-unstaged) file at the
        # stage prompt — used for the base-repo paths, where the user is explicitly
        # committing, so a file is always offer-able and never permanently un-stageable.
        # The automatic worktree capture keeps it False so the agent's own untracked
        # decline still sticks.
        on_worktree = repo is None
        repo = repo or self.repo
        state = state or self.state
        if on_worktree:
            self._ensure_turn_branch()  # turn branches are a worktree concept only
        repo.add_tracked()
        self._review_untracked_popup(include_declined=include_declined, repo=repo, state=state)
        if not repo.has_staged_changes():
            return False
        message = ""
        prompt = "Commit message:"
        while not message.strip():
            result = self._prompt_popup("User Commit", prompt)
            if result is None:
                return False
            message = result
            prompt = "Commit message is required. Enter a commit message:"
        repo.commit(build_user_commit_message(message=message, agitrack_session_id=state.session_id))
        state.clear_trace()
        return True

    def _review_untracked_popup(
        self, *, include_declined: bool, repo: GitRepo | None = None, state: AgitrackState | None = None
    ) -> str:
        repo = repo or self.repo
        state = state or self.state
        self._prune_declined_untracked(repo, state)
        untracked = repo.untracked_files()
        declined = set(state.declined_untracked())
        candidates = untracked if include_declined else [path for path in untracked if path not in declined]
        if not candidates:
            return "No untracked files to review."
        # Cap the listed files to the terminal height so the question and the input
        # line (which the modal draws LAST) are never pushed off the popup. The
        # decision is all-or-none, so a preview plus "…and N more" is enough.
        limit = max(5, self.rows - 10)
        preview = candidates[:limit]
        listing = "\n".join(preview)
        if len(candidates) > len(preview):
            listing += f"\n…and {len(candidates) - len(preview)} more"
        answer = self._prompt_popup(
            "Untracked Files",
            f"Stage all {len(candidates)} new file(s)? [y/N]\n{listing}",
        )
        if answer is None:
            return "Cancelled."
        answer = answer.strip().lower()
        if answer in {"y", "yes"}:
            repo.stage_paths(candidates)
            state.remove_declined(candidates)
            return f"Staged {len(candidates)} untracked file(s)."
        else:
            state.add_declined(candidates)
            return f"Left {len(candidates)} untracked file(s) unstaged."

    def _create_agent_commit_from_turns_popup(
        self,
        *,
        turns,
        backend: str,
        backend_session_id: str | None,
        model: str | None,
        quiet: bool,
        prompt_untracked: bool = True,
    ) -> bool:
        if prompt_untracked:

            def stage_untracked_fn(repo, state):
                self._review_untracked_popup(include_declined=False)
        else:
            # Non-interactive commit (worktree session or exit finalize): stage
            # the agent's new files too, except any the user intentionally declined.
            def stage_untracked_fn(repo, state):
                declined = set(state.declined_untracked())
                repo.stage_paths([path for path in repo.untracked_files() if path not in declined])

        def on_commit_fn(sha, trace_text, is_cover):
            self._last_agent_commit_id = sha
            # Mark that this session saw a real turn this run (stable aGiTrack session id,
            # not the drift-prone backend id) so the exit-path auto-share knows there
            # is genuinely something new to push.
            self._sessions_with_activity.add(self.state.session_id)
            # Don't announce the commit yet: the "created" popup is shown only once
            # the commit is merged into the base branch (see _integrate_turn_or_conflict),
            # so the user is never told a commit landed before it actually has.
            self._commit_merged_pending = True
            self._commit_summarized = False
            # When the backend agent committed its own work, aGiTrack puts a merge-shaped
            # "cover" commit on top instead of amending (which would change the
            # agent's commit hashes). Tell the user briefly what just happened (#35).
            if is_cover:
                self._set_session_notice(
                    self._session_label(),
                    "aGiTrack is committing a cover — a wrapper commit over the agent's own commits "
                    "that adds the interaction trace without changing their hashes.",
                    seconds=8.0,
                )
            # The commit is made immediately; the LLM summary is computed in the
            # background and amended in afterwards (#8) so the UI never blocks. The
            # trace passed here is exactly the one that landed in the commit (built
            # inside commit_turns), which is the summarizer's sole input.
            self._start_commit_summary(sha, trace_text)

        return CommitEngine(
            self.repo,
            self.state,
            debug_fn=self._debug,
            # The --full-agent-messages flag forces the option on for this run; None
            # otherwise so the per-repo config decides.
            full_agent_messages=self._full_agent_messages or None,
        ).commit_turns(
            turns=turns,
            backend=backend,
            backend_session_id=backend_session_id,
            model=model,
            stage_untracked_fn=stage_untracked_fn,
            pre_commit_fn=self._ensure_turn_branch,
            on_commit_fn=on_commit_fn,
            session_name=self.name,
            backend_commits=self._uncovered_backend_commits(),
        )

    def _uncovered_backend_commits(self) -> list[str]:
        """Unintegrated commits on the session's turn branch that the backend
        created itself and no aGiTrack commit accounts for yet (#35). Full SHAs,
        oldest first. Backend commits keep their own messages forever (their
        hashes must stay stable, #58), so "covered" cannot be read off the
        commit itself: an aGiTrack metadata commit covers everything before it on
        the branch (its ``covered_commits`` named them), leaving only commits
        NEWER than the newest metadata commit unaccounted for. Only commits
        ahead of base qualify at all."""
        if self.worktree is None or self._base_branch is None:
            return []
        try:
            branch = self.repo.current_branch()
            if not is_managed_branch(branch):
                return []
            uncovered: list[str] = []
            for sha in self.repo.log_shas(self._base_branch, branch):  # oldest first
                if METADATA_HEADER in self.repo.commit_message(sha):
                    uncovered = []
                else:
                    uncovered.append(sha)
            return uncovered
        except Exception as error:
            self._debug(f"uncovered backend commit check failed: {error!r}")
            return []

    def _session_label(self) -> str:
        """The name of the session the runner is currently operating on — the
        one popups should attribute work to. Inside temp-swap windows
        (_with_session) this is the serviced background session, which is
        exactly the session whose commit/summary the popup announces."""
        return self.name or "main"

    def _agent_commit_message(self) -> str:
        # The auto-commit confirmation, shown only once the commit has landed in
        # the base branch (worktree sessions) or been committed and summarized
        # (no-worktree sessions). Includes the short SHA so the user can find the
        # commit (e.g. `git show <id>`), the session it belongs to (background
        # sessions auto-commit too), the base it landed on, and whether it was
        # summarized.
        commit_id = self._last_agent_commit_id
        session = self._session_label()
        base = self._base_branch or "the base branch"
        summarized = " (summarized)" if self._commit_summarized else ""
        head = f"Created <aGiTrack> commit {commit_id}" if commit_id else "Created <aGiTrack> commit"
        return f"{head} in session '{session}' — merged into {base}{summarized}."

    def _announce_agent_commit(self) -> None:
        # Surface the deferred "created" notice for the session whose commit just
        # landed — at integration time for a worktree session (the commit is now
        # in the base), or after the commit/summary for a no-worktree session
        # (which never integrates). Only fires when there is a just-made agent
        # commit to announce; a base fast-forward with nothing pending, or an
        # already-announced commit, stays silent. Uses a session notice (not a
        # sticky message) so it never stomps the "summarizing…" notice and the
        # ordering — summarizing first, then created — is preserved.
        if not self._commit_merged_pending:
            return
        self._commit_merged_pending = False
        self._set_session_notice(self._session_label(), self._agent_commit_message())
        self._commit_summarized = False
        # Sync an opted-in shared session to GitHub HERE — piggy-backing on the
        # commit so the network round-trip happens at the commit cadence, not on a
        # frequent timer (issue #55, avoid hammering the remote).
        self._maybe_auto_share_active()

    # ------------------------------------------------------------------
    # Background commit summarization (#8)
    # ------------------------------------------------------------------
    #
    # The summary is an LLM call that can take many seconds; running it inside
    # the commit path froze the proxy UI and delayed integration past the next
    # turn. Instead the commit is created immediately and a worker thread
    # computes the summary; the main loop then amends it into the commit
    # message — only while the commit is still HEAD, unintegrated, and the
    # tree is clean, so an amend can never rewrite history or swallow staged
    # work. Integration waits for the summary up to SUMMARY_WAIT_SECONDS, then
    # proceeds without it (the summary still lands in git notes).

    def _make_summarizer(self):
        if not self._summarization_enabled():
            return None
        from agitrack.summaries import Summarizer, summary_scratch_dir
        from agitrack.backends.claude import ClaudeBackend
        from agitrack.backends.opencode import OpenCodeBackend

        backend_class = OpenCodeBackend if self.state.backend == "opencode" else ClaudeBackend
        model = self.state.summarization_model
        if model is None and self.global_config is not None:
            model = self.global_config.summarization_model
        # The summarizer must NOT run in the session worktree (or the repo):
        # its headless calls record real backend sessions keyed by cwd, which
        # the parse worker / exit adoption would then resume instead of the
        # user's conversation (issues #8/#56).
        # Honour a configured backend wrapper for the summarizer's headless calls too,
        # so the agent binary is always launched the same way the user asked.
        launch = self._launch_command()
        return Summarizer(backend_class(summary_scratch_dir(), launch_command=launch or None), model=model)

    def _start_commit_summary(self, sha: str, trace_text: str) -> None:
        # Never kick off a fresh summarizer call while exiting. The exit-finalize commits
        # each session's final turn (_commit_latest_turn_sync), and a summary started here
        # would only be blocked on a few lines later (_finalize_summary_then_integrate_on_exit
        # joins it), adding a whole LLM round-trip PER SESSION to teardown — the main reason
        # exit dragged on for tens of seconds. The commit keeps its prompt-based message; the
        # AI summary is an optional enhancement, not worth holding the exit for.
        if self._exiting:
            return
        summarizer = self._make_summarizer()
        if summarizer is None:
            return
        # The owning session: summary state is per session (so two sessions can
        # summarize at once), and the worker writes its result back to THIS
        # session even if the user switches away before it finishes.
        owner = self.active
        if owner._summary_thread is not None and owner._summary_thread.is_alive():
            # One summary at a time PER SESSION; a turn committed before this
            # session's previous summary finished keeps its prompt-based message.
            self._debug(f"summary worker busy for '{self._session_label()}'; skipping summary for {sha}")
            return
        try:
            full_sha = self.repo.rev_parse(sha)
        except Exception as error:
            self._debug(f"summary snapshot failed: {error!r}")
            return
        session_summary = self.state.session_summary
        # The summary may finish after the user switches sessions: remember the
        # owning repo/state/name so it is never applied to — or reported
        # against — a different session.
        result: dict = {
            "sha": full_sha,
            "short_sha": sha,
            "repo": self.repo,
            "state": self.state,
            "session_name": self._session_label(),
        }
        owner._summary_result = None
        owner._summary_pending = {"sha": full_sha, "since": time.monotonic()}
        model = self.state.model

        def worker() -> None:
            try:
                result["summary"] = summarizer.summarize_commit(trace=trace_text)
            except Exception as error:  # surfaced by the service tick
                result["error"] = repr(error)
            else:
                try:
                    result["session_summary"] = summarizer.update_session_summary(
                        current_summary=session_summary,
                        trace=trace_text,
                        commit_summary=result["summary"],
                    )
                except Exception as error:
                    # A failed rolling summary must not discard a good commit
                    # summary; the previous session summary simply stays current.
                    result["session_summary_error"] = repr(error)
            result["metadata"] = summary_metadata_lines(
                model=summarizer.model or model,
                tokens_input=summarizer.tokens_input,
                tokens_output=summarizer.tokens_output,
                tokens_cache_read=summarizer.tokens_cache_read,
            )
            # Write back to the OWNING session (not self.active, which may have
            # changed): the service tick swaps each session in to apply it.
            owner._summary_result = result

        owner._summary_thread = threading.Thread(target=worker, daemon=True, name="agit-summary")
        owner._summary_thread.start()
        self._set_session_notice(
            self._session_label(),
            f"aGiTrack is summarizing the latest commit in session '{self._session_label()}'…",
            seconds=self.SUMMARY_WAIT_SECONDS,
        )

    def _service_commit_summaries(self) -> None:
        """Apply finished background summaries for every session. Each session has
        its own summary worker/result (so two can summarize concurrently), so the
        active session is serviced in place and the rest via a context swap."""
        self._service_commit_summary()  # active session, in place
        for session in list(self.sessions):
            if session is not self.active:
                self._with_session(session, self._service_commit_summary)

    def _service_commit_summary(self) -> None:
        """Main-loop tick: apply this session's finished background summary (#8).
        All git and state mutations happen here, on the main thread."""
        result = self._summary_result
        if result is None:
            return
        self._summary_result = None
        self._summary_pending = None
        session = result.get("session_name") or "main"
        if "error" in result:
            # Includes UnusableSummaryError (backend returned "You've hit your
            # session limit..." or similar, issue #8): the commit keeps its
            # prompt-led message instead of getting the error as a subject.
            self._debug(f"commit summarization failed: {result['error']}")
            self._set_session_notice(
                session,
                f"aGiTrack: commit summarization failed in session '{session}'; keeping the prompt-based message.",
            )
            return
        sha, summary, repo, state = result["sha"], result["summary"], result["repo"], result["state"]
        try:
            # Amend first, then attach notes to whatever commit survives — notes
            # added before an amend would hang off the orphaned pre-amend object.
            target = self._amend_summary_into_head(repo, sha, summary, result.get("metadata"))
            if target:
                # The commit is now summarized; the "created" notice (set at
                # integration for a worktree session, or just below for a
                # no-worktree one) will note this. No separate "summary added"
                # popup — that fired before the merge and confused the ordering.
                self._commit_summarized = True
            else:
                # Already integrated or superseded: the summary lives in the
                # commit's git notes instead of its message.
                target = sha
                self._debug(f"summary for {sha} recorded as notes only")
            repo.notes_add(target, summary, namespace="agitrack/commit-summary")
            session_summary = result.get("session_summary")
            if session_summary:
                state.session_summary = session_summary
                state.session_summary_commit = target
                repo.notes_add(target, session_summary, namespace="agitrack/session-summary")
            elif "session_summary_error" in result:
                self._debug(f"session summary update failed: {result['session_summary_error']}")
            if self.worktree is None:
                # No integration step will announce a no-worktree commit, and the
                # summary has now landed, so surface the "created (summarized)"
                # notice here — replacing the "summarizing…" notice in the right
                # order. Idempotent: a no-op if the commit was already announced.
                self._announce_agent_commit()
        except Exception as error:
            self._debug(f"applying commit summary failed: {error!r}")

    def _amend_summary_into_head(self, repo, sha: str, summary: str, metadata: list[str] | None) -> str | None:
        """Amend the summary into *sha*'s message, only while that is safe:
        the commit is still HEAD, not yet integrated into base, and nothing is
        staged (``--amend`` would otherwise pull staged work into the commit).
        Returns the amended commit's full SHA, or None if no amend happened."""
        try:
            if repo.rev_parse("HEAD") != sha:
                return None
            if self._base_branch is None or sha not in repo.log_shas(self._base_branch, "HEAD"):
                return None
            if repo.has_staged_changes():
                return None
            message = repo.commit_message("HEAD")
            amended = apply_summary_to_message(message, summary, summary_metadata=metadata)
            if amended == message:
                return None  # already summarized: never amend twice
            new_short = repo.amend_commit(amended)
            if self._last_agent_commit_id and sha.startswith(str(self._last_agent_commit_id)):
                self._last_agent_commit_id = new_short
            return repo.rev_parse("HEAD")
        except Exception as error:
            self._debug(f"summary amend failed: {error!r}")
            return None

    def _summary_blocks_integration(self, now: float) -> bool:
        # Hold integration briefly so the summary can be amended in before the
        # commit leaves the turn branch — but never stall: past the deadline
        # the commit integrates as-is and the summary becomes notes-only.
        pending = self._summary_pending
        if pending is None:
            return False
        if now - pending["since"] >= self.SUMMARY_WAIT_SECONDS:
            return False
        thread = self._summary_thread
        if thread is None or not thread.is_alive():
            # Result ready (or worker gone): apply it now so integration can
            # proceed this tick instead of waiting for the next one.
            self._service_commit_summary()
            return self._summary_pending is not None
        return True

    def _set_message(self, message: str | None, *, seconds: float = 4.0, sticky: bool = False) -> None:
        # message may be None when relayed straight from a service result; storing
        # None simply leaves no popup to paint (the same state as a cleared message).
        self.message = message
        self.message_until = time.monotonic() + seconds
        self._message_scroll = 0  # a fresh message starts at the top
        # Sticky messages ignore the timeout and persist until the user's next
        # keypress clears them (see _clear_sticky_message_on_input).
        self._message_sticky = sticky
        # A one-off message takes over the popup from the per-session notices; the
        # notices reappear (via _service_session_notices) once it expires.
        self._notice_shown = False
        # Request a repaint so the popup actually shows. Without this a message set
        # from the background idle loop (e.g. the auto-commit confirmation, set when
        # the agent has gone quiet and produces no output to trigger a render) would
        # never be painted.
        self._render_pending = True

    def _clear_message(self) -> None:
        self.message = None
        self.message_until = 0.0
        self._message_sticky = False

    # ------------------------------------------------------------------
    # Per-session status notices (commit/summary lifecycle, multi-line)
    # ------------------------------------------------------------------
    #
    # Commit/summary progress is reported per session, keyed by session name, so
    # several concurrent sessions each get their own line in the popup instead of
    # clobbering one shared message. The notices compose into ``self.message``;
    # one-off ``_set_message`` calls (errors, menu results) temporarily take over
    # and the notices reassert themselves on the next service tick.

    def _set_session_notice(self, name: str, text: str | None, *, seconds: float = 6.0) -> None:
        """Set, replace, or (text=None) clear the status line for session *name*."""
        if text is None:
            self._session_notices.pop(name, None)
        else:
            self._session_notices[name] = (text, time.monotonic() + seconds, False)
        self._refresh_notice_message()

    def _refresh_notice_message(self) -> None:
        # Drop expired notices, then reflect the live ones into the popup. Skips
        # over a still-showing one-off message so an error/confirmation is not
        # stomped; that message's expiry brings the notices back on a later tick.
        now = time.monotonic()
        self._session_notices = {n: v for n, v in self._session_notices.items() if v[1] > now}
        one_off_showing = (
            not self._notice_shown and self.message is not None and (self._message_sticky or now < self.message_until)
        )
        if one_off_showing:
            return
        lines = [text for text, _until, _sticky in self._session_notices.values()]
        if lines:
            composed = "\n".join(lines)
            # Only request a repaint when the popup's content actually changes.
            # _service_session_notices runs every reactor tick, so re-flagging a
            # render each time (even when the text is unchanged) forces a
            # full-frame repaint at tick cadence — which flickers the notice popup
            # on terminals without synchronized-update support while the backend is
            # also streaming output. message_until is refreshed silently (it only
            # gates the render's "still showing" check, and the per-notice expiries
            # are fixed at set time, so updating it never needs a repaint).
            changed = not self._notice_shown or composed != self.message
            self.message = composed
            self.message_until = max(until for _t, until, _s in self._session_notices.values())
            self._message_sticky = False
            self._notice_shown = True
            if changed:
                self._render_pending = True
        elif self._notice_shown:
            # The notices just emptied and we owned the popup: clear it.
            self._clear_message()
            self._notice_shown = False
            self._render_pending = True

    def _service_session_notices(self) -> None:
        # Main-loop tick: expire per-line notices and keep the popup current (so a
        # finished line disappears even while another session's line lives on).
        if self._session_notices or self._notice_shown:
            self._refresh_notice_message()

    def _clear_sticky_message_on_input(self) -> bool:
        # The next keypress dismisses a sticky message. Returns True if one was
        # showing (so the caller can repaint to remove the popup).
        if self._message_sticky:
            self._clear_message()
            return True
        return False

    def _confirm_exit(self) -> bool:
        # "(Ctrl-C again)" hints that a second Ctrl-C confirms the exit (the
        # double-Ctrl-C fast path in _run_exit_flow), matching the keystroke that
        # opened this prompt.
        choice = self._select_popup("Exit aGiTrack?", ["No, keep working", "Yes, exit (Ctrl-C again)"])
        return choice == "Yes, exit (Ctrl-C again)"

    def _run_exit_flow(self) -> bool:
        # THE single exit path (P6 Stage 3 — exit-path unification).
        #
        # Every interactive way out of aGiTrack goes through here:
        #   * Main loop: Ctrl-C in _reactor_stdin_phase → _run_exit_flow()
        #   * Modal: Ctrl-C inside any popup → _run_modal() → _run_exit_flow()
        #   * "exit" command: _run_command("exit") → _run_exit_flow()
        #
        # This guarantees _finalize_pending_work() is never skipped for
        # interactive exits.  Signal exits (SIGTERM/SIGHUP) go through
        # _handle_exit_signal which does a fast non-interactive teardown.
        #
        # Double-Ctrl-C: a second Ctrl-C while the confirmation popup is open
        # sets _popup_exit_force and exits immediately — but still gracefully
        # (finalize included). Returns True when aGiTrack is exiting.
        if self._popup_exit_pending:
            # A second Ctrl-C, inside one of the exit-confirmation popups: take
            # it as an emphatic yes — skip the questions, keep the finalize.
            self._popup_exit_force = True
            self._exit_requested = True
            return True
        self._popup_exit_pending = True
        self._popup_exit_force = False
        self._exit_aborted = False
        try:
            # Exiting always asks first (a deliberate safety net), regardless of
            # whether there is pending work. Whether there is anything to *finalize*
            # only affects the message shown during teardown (see _finalize_pending_work).
            if not self._confirm_exit() and not self._popup_exit_force:
                self._set_message("Exit cancelled — still running; nothing was shut down.")
                self._render()
                return False
            if not self._popup_exit_force:
                if not self._confirm_terminate_background_sessions() and not self._popup_exit_force:
                    self._set_message("Exit cancelled — kept working; the background sessions are still running.")
                    self._render()
                    return False
            self._finalize_pending_work()
            if self._exit_aborted and not self._popup_exit_force:
                # Esc on the on-exit copy offer: undo the "we're exiting" state and stay
                # running so the user can deal with the files. Already-made commits/merges
                # stand (they never lose work); nothing was deleted.
                self._exiting = False
                self._finalized_on_exit = False
                self._set_message(
                    "Exit cancelled. Your worktree and its files were NOT deleted — copy/move what you "
                    "need, then exit again. (Commits already made this exit were kept.)",
                    seconds=12.0,
                )
                self._render()
                return False
            self._exit_child()
            self._exit_requested = True
            return True
        finally:
            self._popup_exit_pending = False

    def _running_background_session_names(self) -> list[str]:
        # Names of background (non-active) sessions whose backend is still working.
        return [
            self._session_name(index)
            for index in range(len(self.sessions))
            if index != self.active_index and self._session_status(index) == "running"
        ]

    def _confirm_terminate_background_sessions(self) -> bool:
        # Second exit confirmation, shown only when background sessions are still
        # running: it names them and warns that exiting terminates them (which may
        # lose in-flight work). Returns True to proceed with exit, False to keep
        # working. No-op (returns True) when nothing is running in the background.
        names = self._running_background_session_names()
        if not names:
            return True
        listing = ", ".join(f"'{name}'" for name in names)
        lead = "background sessions are" if len(names) > 1 else "A background session is"
        choice = self._select_popup(
            f"{lead} still running ({listing}). Exiting now terminates them and may lose in-progress work.",
            ["No, keep working", "Yes, terminate them and exit"],
        )
        return choice == "Yes, terminate them and exit"

    def _commit_latest_turn_sync(self) -> None:
        # Synchronously (joining the parse worker) commit the latest completed
        # turn for the *current* session state, non-interactively.
        try:
            if self.agent_parse_thread and self.agent_parse_thread.is_alive():
                self.agent_parse_thread.join(timeout=20)
            self._finish_agent_parse_if_ready(
                quiet=True, prompt_untracked=False, integrate=False, require_complete=False
            )
            if self._start_agent_parse() and self.agent_parse_thread:
                self.agent_parse_thread.join(timeout=20)
            self._finish_agent_parse_if_ready(
                quiet=True, prompt_untracked=False, integrate=False, require_complete=False
            )
        except Exception as error:  # never block on a commit failure
            self._debug(f"sync commit failed: {error!r}")

    def _finalize_pending_work(self) -> None:
        # On a confirmed exit, make sure the latest completed agent turn is
        # committed for *every* session before aGiTrack leaves — otherwise quitting
        # right after a turn drops a commit the idle/stable debounce had not yet
        # made. (Background sessions are committed via the context swap.)
        if self._finalized_on_exit:
            return  # already finalized (e.g. the backend exited and we ran this)
        self._finalized_on_exit = True
        self._exiting = True
        # ALWAYS show a notice the moment exit begins, before any of the (sometimes
        # multi-second) teardown below — joining the git worker, pushing an
        # auto-shared session, integrating, removing worktrees, joining threads. A
        # silent terminal during that wait looks frozen. Name the specific work when
        # we can; otherwise a generic notice. (On a forced/terminal-closed exit the
        # screen is already None, so _render no-ops — harmless.) Computed before
        # stopping the worker so it still reflects any in-flight turn.
        detail = self._describe_exit_finalize()
        self._set_message(detail or "Finalizing things before exiting…", seconds=30)
        self._render()
        # Stop the git worker first so the exit-time foreground commits below run on
        # this (main) thread without racing a worker pass. After this join the
        # finalize git is the only git touching the worktree.
        self._stop_git_worker()
        self._cancel_inflight_shared_fetches()  # stop any unfinished session fetch at once
        # The session the user was working in when they quit is the one to
        # auto-resume next start — not necessarily the primary (they may have
        # switched to a new or shared session). It is finalized first, below.
        self._exit_resume_worktree = getattr(self.worktree, "name", None)
        self._commit_latest_turn_sync()  # active session, in place
        self._auto_share_on_exit()  # push the latest conversation if auto-shared
        self._finalize_summary_then_integrate_on_exit()
        if self._exit_aborted:
            return  # Esc on the active session's copy offer — leave the rest untouched
        for session in list(self.sessions):
            if session is self.active:
                continue
            saved = self.active
            self.active = session
            try:
                self._commit_latest_turn_sync()
                self._auto_share_on_exit()
                self._finalize_summary_then_integrate_on_exit()
            finally:
                self.active = saved
            if self._exit_aborted:
                return  # Esc on a session's copy offer — stop finalizing the rest
        self._delete_orphan_merged_branches()
        # NB: deliberately NOT sweeping orphan shared-session snapshots here. That
        # sweep runs `git fsck` over the whole object graph — seconds on a large
        # repo — and would stall exit even when the session made no commits. It is
        # redundant anyway: each publish reclaims its
        # previous snapshot immediately, and the startup sweep (a background thread)
        # mops up any stragglers. So leave exit fast.

    def _finalize_summary_then_integrate_on_exit(self) -> None:
        # Give the current session's in-flight commit summary a short grace period
        # so it can be amended in before the final integration; past that it is
        # dropped rather than holding the exit hostage (#8). Runs for the active
        # session in place and for each background session under a context swap.
        if self._summary_thread is not None and self._summary_thread.is_alive():
            self._summary_thread.join(timeout=self.EXIT_SUMMARY_GRACE_SECONDS)
        self._service_commit_summary()
        self._integrate_session_on_exit()
        self._remove_worktree_on_exit()

    def _adopt_latest_backend_session(self) -> None:
        # The user may have switched conversations inside the backend itself (e.g.
        # Claude's native session picker), which leaves aGiTrack's tracked id stale.
        # Point the resume record at the worktree's most recent conversation so the
        # next launch restores what they were actually using, not the id aGiTrack
        # originally spawned.
        try:
            latest = self.backend.latest_session_id(self.repo.repo)
        except Exception as error:
            self._debug(f"adopt latest backend session failed: {error!r}")
            return
        if latest and latest != self.state.backend_session_id:
            self._debug(f"adopting backend session {latest} (was {self.state.backend_session_id})")
            self.state.backend_session_id = latest
            self.state.last_backend_message_id = None  # recomputed from the transcript on resume
            self._persist_session_name(latest)

    def _persist_last_session_record(self) -> None:
        # Save just the resume pointer for the current (primary) session into the
        # repo-root state — the durable "last session" record. Only the minimal
        # fields needed to resume the conversation are kept; the worktree's working
        # tree and per-turn state (pending trace, etc.) are intentionally dropped.
        self._adopt_latest_backend_session()
        try:
            root = AgitrackState(self.base_repo.repo, default_backend=self.global_config.default_backend)
            root.data["backend"] = self.state.backend
            root.data["backend_session_id"] = self.state.backend_session_id
            root.data["backend_session_repo"] = self.state.data.get("backend_session_repo")
            root.data["last_backend_message_id"] = self.state.last_backend_message_id
            root.data["model"] = self.state.model
            root.data["agitrack_session_id"] = self.state.session_id
            root.save()
            self._debug(f"persisted last session record backend_session_id={self.state.backend_session_id}")
        except Exception as error:
            self._debug(f"persist last session record failed: {error!r}")

    def _note_pid_for_reaping(self, pid: int | None) -> None:
        # A signalled backend exits asynchronously; remember its pid so the main
        # loop can wait on it once it dies, instead of leaving a zombie for the
        # rest of the run. Host-level (shared across sessions), not swapped.
        if pid:
            self._reap_pids.append(pid)

    def _reap_stopped_children(self) -> None:
        pids = self._reap_pids
        if not pids:
            return
        if os.name == "nt":
            # No zombie reaping on Windows: a ConPTY child is fully closed by terminate(),
            # and os.waitpid/os.WNOHANG don't exist there. Nothing to wait on.
            self._reap_pids = []
            return
        remaining = []
        for pid in pids:
            try:
                done, _status = os.waitpid(pid, os.WNOHANG)
                if not done:
                    remaining.append(pid)
            except (ChildProcessError, OSError):
                pass  # already reaped elsewhere (or not our child anymore)
        self._reap_pids = remaining

    def _terminate_child(self) -> None:
        # Stop the current session's backend process and release its PTY, so its
        # worktree is no longer in use and can be removed.
        pid = self.child_pid
        if pid:
            self._note_pid_for_reaping(pid)
        # terminate() nulls the fd/pid on the session-owned process in place.
        self.active.process.terminate()

    def _remove_worktree_on_exit(self) -> None:
        # On exit, drop a fully-merged session's worktree so directories do not
        # pile up across runs. A session whose work could not be integrated
        # (conflict, uncommitted changes, or commits still ahead of the base)
        # keeps its worktree so the next startup can surface it. The backend
        # conversation persists (keyed by the worktree path) and is recreated if
        # the session is resumed, so nothing of value is lost.
        info = self.worktree
        if info is None or self._base_branch is None:
            return
        # Persist the resume pointer FIRST — before deciding whether the worktree
        # can be removed. _persist_last_session_record runs _adopt_latest_backend_session,
        # which captures a conversation the user switched to inside the backend's
        # own picker (Claude's session view). Gating it behind a clean worktree
        # removal meant a session that still had uncommitted or unintegrated work —
        # the usual state right after a mid-work switch — never updated its resume
        # pointer, so the next start resumed a stale conversation and only the start
        # after that landed on the right one (the "first restart starts fresh,
        # second restart resumes it" off-by-one). Adopting writes both the worktree
        # state (used when the worktree is kept) and the repo-root state (used when
        # it is removed), so the right conversation resumes either way.
        #
        # Persist for the session the user was actually in when they quit (set in
        # _finalize_pending_work), so quitting from a freshly-started or resumed
        # *shared* session auto-resumes THAT next start — not the original primary,
        # which left the next start prompting for a brand-new session instead.
        if info.name == (self._exit_resume_worktree or self._primary_worktree_name):
            self._persist_last_session_record()
        try:
            if self.merge_ctx or self.repo.merge_in_progress():
                self._debug(f"keeping worktree '{info.name}' on exit: a merge is in progress")
                return
            # Uncommitted leftover FILES (untracked / git-ignored / files the user declined
            # to stage) do NOT by themselves keep the worktree — they are exactly what the
            # copy offer below handles, and a copy leaves the source in place, so refusing
            # removal here is what left dirty worktrees stranded forever ("open end"). They
            # only force us to keep the worktree on a *signal* teardown, where there's no UI
            # to offer the copy and silently deleting the files would lose the user's work.
            has_leftover_files = self.repo.has_changes()
            branch = self.repo.current_branch()
            if is_managed_branch(branch):
                # Only drop the worktree if we can CONFIRM its branch is fully
                # contained in the base. If the base ref can't be resolved — e.g.
                # the user deleted/renamed the branch aGiTrack integrates into while it
                # was running — rev_parse raises and we keep the worktree (and its
                # branch) rather than risk discarding unmerged work.
                self.base_repo.rev_parse(self._base_branch)
                if self.base_repo.log_range(self._base_branch, branch):
                    self._debug(
                        f"keeping worktree '{info.name}' on exit: '{branch}' still ahead of {self._base_branch}"
                    )
                    return  # commits still ahead of base → unintegrated; keep it
        except Exception as error:
            self._debug(f"keeping worktree '{info.name}' on exit: {error!r}")
            return
        # The worktree (and any uncommitted/ignored files in it) is about to be deleted.
        # Offer to copy those out first — but only on an interactive exit (Ctrl-G → exit),
        # never a signal teardown (terminal/window close sets screen=None), where there is
        # no UI to prompt with. The "exit" context warns that declining discards them.
        if self.screen is not None:
            self._offer_copy_unstaged_to_base(context="exit")
            if self._exit_aborted:
                return  # Esc on the copy offer: keep this worktree + files, abort the exit
        elif has_leftover_files:
            # Signal teardown with leftover files and no UI to rescue them: keep the
            # worktree so the next startup can surface the files rather than discarding
            # them. (An interactive exit just gave the user the copy offer above, so by
            # here those files were handled and the worktree is safe to remove forcibly.)
            self._debug(f"keeping worktree '{info.name}' on signal exit: leftover files, no UI to copy them out")
            return
        # Remember this session's conversation under its backend so switching back
        # to that backend (this run or a later one) resumes it.
        self._remember_session_for_backend()
        self._terminate_child()
        try:
            self._worktrees().remove(info.name)
            self._debug(f"removed merged worktree '{info.name}' on exit")
        except Exception as error:
            self._debug(f"exit worktree removal failed for '{getattr(info, 'name', '?')}': {error!r}")
        self.worktree = None

    def _exit_child(self) -> None:
        self.running = False
        self._disable_host_terminal_modes()
        # SIGINT to child delegated to the session's BackendProcess (does not
        # close fd -- the run() finally block handles that so it always runs).
        self.active.process.signal_exit()

    def _check_shutdown_request(self) -> None:
        """Honor an external graceful-shutdown request: a ``<repo>/.agitrack/shutdown`` file.

        This is the cross-platform equivalent of the SIGTERM/SIGHUP a closing terminal
        sends — used by the VS Code extension on Windows, where a separate process can't
        deliver a catchable signal (Node's ``process.kill`` hard-kills there). Polled
        cheaply on a ~1 s interval; on finding the file we remove it, finalize the pending
        turn (so a just-finished turn is committed, not stranded), and stop the loop."""
        now = time.monotonic()
        if now - self._last_shutdown_check < 1.0:
            return
        self._last_shutdown_check = now
        try:
            path = self.base_repo.repo / ".agitrack" / "shutdown"
            if not path.exists():
                return
        except (OSError, AttributeError, TypeError):
            return  # no real base repo (e.g. a unit-test runner) ⇒ nothing to honor
        try:
            path.unlink()
        except OSError:
            pass
        self._debug("external shutdown request — finalizing and exiting")
        try:
            self._finalize_pending_work()
        except Exception as error:  # never let a slow/failing finalize block the shutdown
            self._debug(f"shutdown finalize failed: {error!r}")
        self.running = False

    def _handle_exit_signal(self, signum, _frame) -> None:
        # A self-update is mid-flight (pip is uninstalling/reinstalling aGiTrack). Do
        # NOT tear down: unwinding would abort the apply and could leave aGiTrack
        # half-uninstalled. The upgrade runs in its own session (see updater) so this
        # SIGHUP/SIGTERM never reached it; ignore the signal and let the apply +
        # restart finish. (The user can still hard-kill with SIGKILL if truly stuck.)
        if self._update_applying:
            return
        self.running = False
        # Closing the terminal (the VSCode terminal, an SSH/Mosh session, a `kill`)
        # delivers SIGHUP/SIGTERM. Finalize pending work first so a just-completed turn
        # is committed and integrated instead of being stranded — i.e. exit gracefully,
        # not abruptly. Rendering is suppressed (the terminal may already be gone:
        # `_render()` no-ops when `screen` is None) and the whole finalize is guarded so
        # a slow or failing finalize can never stop the process from exiting.
        #
        # Whether work was actually in progress is captured *before* finalizing (which
        # commits it away) so the out-of-terminal prompt only fires when the close
        # interrupted something — a clean close stays silent.
        had_work = self._had_unfinalized_work()
        self.screen = None
        try:
            self._finalize_pending_work()
        except Exception:
            pass
        self._disable_host_terminal_modes()
        self._cleanup_child()
        self._restore_terminal()
        if had_work:
            self._prompt_forced_exit()
        raise SystemExit(128 + int(signum))

    def _describe_exit_finalize(self) -> str | None:
        """A specific, user-facing description of what exit finalize will actually
        do — or ``None`` when everything is already committed and merged (a clean,
        silent exit).

        It deliberately ignores the base repo's own dirty/untracked files and a
        worktree's git-ignored or intentionally-declined files: aGiTrack never
        commits those on exit, so they must not make exit claim there is something
        to "finalize". The signals are aGiTrack's own pending work: a turn still in
        flight, a session with committable or committed-but-unmerged work
        (``_unmerged_worktrees`` already excludes ignored/declined noise), or a
        commit summary still being written. Best-effort; any error reads as
        "nothing to report".
        """
        actions: list[str] = []
        try:
            if self._agent_is_active() or any(getattr(s, "agent_in_flight", False) for s in self.sessions):
                actions.append("committing the latest turn")
            unmerged = self._unmerged_worktrees()
            if unmerged:
                actions.append("committing and merging unsaved work in " + ", ".join(label for label, _ in unmerged))
            if any(self._session_summary_in_progress(s) for s in self.sessions):
                actions.append("saving the commit summary")
        except Exception:
            return None
        if not actions:
            return None
        return "Finishing up before exit — " + ", and ".join(actions) + "…"

    @staticmethod
    def _session_summary_in_progress(session) -> bool:
        """Whether *session* still has a commit summary being computed (a pending
        slot or a live summarizer thread) that exit finalize would wait to apply."""
        if getattr(session, "_summary_pending", None) is not None:
            return True
        thread = getattr(session, "_summary_thread", None)
        return thread is not None and thread.is_alive()

    def _had_unfinalized_work(self) -> bool:
        """Whether aGiTrack still has work to finalize on exit — an agent turn in
        flight, a session with committable/unmerged work, or a commit summary being
        written (see :meth:`_describe_exit_finalize`). Drives the forced-exit prompt
        and the exit confirmation, so a clean close (everything committed and merged)
        exits silently. Ignores the base repo's own dirt and declined/ignored files.
        """
        return self._describe_exit_finalize() is not None

    def _prompt_forced_exit(self) -> None:
        """Prompt the user about an exit forced by the host terminal closing.

        The TUI is gone by the time SIGHUP/SIGTERM arrives, so the user can no
        longer be asked anything inside the terminal. Surface a blocking dialog
        through the OS window server instead (macOS only — a silent no-op on
        SSH/headless/Linux, and time-boxed so it can never hang a system
        restart). "Reopen aGiTrack" arms a best-effort relaunch in a fresh
        window, performed after the management lock is released (see `run`).
        """
        if not host_prompt.can_show_dialog():
            return
        try:
            choice = host_prompt.confirm_forced_exit(
                "Choose Reopen aGiTrack to keep working in a new window, or Quit aGiTrack to close."
            )
        except Exception as error:
            self._debug(f"forced-exit prompt failed: {error!r}")
            return
        if choice == host_prompt.REOPEN:
            self._reopen_after_exit = True

    def _reopen_in_new_window(self) -> None:
        """Relaunch aGiTrack in a fresh window after a forced exit.

        Called from `run`'s teardown once the management lock is released so the
        new instance can take it. Mirrors `restart_agitrack`'s
        `python -m agitrack <argv>` invocation (the original terminal is gone, so
        this opens a new window) and starts it in the base repo root so the last
        session auto-resumes. Best-effort; never raises.
        """
        try:
            args = " ".join(shlex.quote(arg) for arg in sys.argv[1:])
            command = f"{shlex.quote(sys.executable)} -m agitrack {args}".rstrip()
            host_prompt.reopen_in_new_terminal(command, str(self.base_repo.repo))
        except Exception as error:
            self._debug(f"reopen in new window failed: {error!r}")

    def _cleanup_child(self) -> None:
        # Delegate SIGINT -> wait -> SIGTERM escalation to the session's BackendProcess.
        self.active.process.cleanup()

    def _pre_agent_commit_if_needed(self, prompt_text: str = "") -> bool:
        self._clear_agent_in_flight_if_idle()
        status = self.repo.status_short().strip()
        finished = self._finish_agent_parse_if_ready(quiet=True)
        if finished is True:
            self.pre_agent_reconciled_status = ""
        if status and self._agent_is_active():
            self._record_user_prompt(prompt_text)
            self._await_followup(prompt_text)
            return True
        if finished is False:
            self.pre_agent_reconciled_status = status
        if status and status != self.pre_agent_reconciled_status:
            if self.agent_parse_thread and self.agent_parse_thread.is_alive():
                self._set_message("aGiTrack is checking existing git changes before sending your prompt...", seconds=60)
                self._render()
                return False
            if self._start_agent_parse():
                self._set_message("aGiTrack is checking existing git changes before sending your prompt...", seconds=60)
                self._render()
                return False
        # Commit the user's own uncommitted work before the agent runs, from whichever
        # tree holds it: this session's worktree (the agent's tree) and/or the base repo
        # (your working directory) — both, when both are dirty. The base commit is then
        # merged into the worktree so the agent starts from your edits.
        if self.actions.has_pre_agent_user_changes():
            self._set_message("Uncommitted changes in this session's worktree — committing them before the agent runs.")
            self._render()
            self._create_user_commit_popup()
        self._commit_base_user_edits_if_needed()
        # Trace every submitted prompt as a user message. The agent-active path
        # above already recorded; the held path records in _forward_pending_prompt.
        # This covers the remaining (clean / user-changes-committed) submits so no
        # follow-up is dropped from the commit trace. Dedup in the agent-commit
        # builder collapses this with the backend's own turn.user_prompt.
        self._record_user_prompt(prompt_text)
        return True

    def _handle_pre_compaction(self) -> None:
        # The transcript snapshot must happen before /compact is forwarded, but
        # the LLM summarization of it must NOT block the UI (#8): export here,
        # summarize on a worker thread, apply via _service_precompact_summary.
        summarizer = self._make_summarizer()
        if summarizer is None:
            return
        try:
            session_id = self.state.backend_session_id
            if not session_id:
                return
            exported = self.backend.export_session(self.repo.repo, session_id)
            if not exported or not exported.turns:
                return
        except Exception as error:
            self._debug(f"pre-compaction export failed: {error!r}")
            return
        if self._precompact_thread is not None and self._precompact_thread.is_alive():
            self._debug("pre-compaction summary worker busy; skipping")
            return
        current_summary = self.state.session_summary
        # The summary may finish after the user switches sessions: remember the
        # owning repo/state/name so it is applied to — and reported against —
        # the session that requested it, not whichever is active by then.
        result: dict = {"repo": self.repo, "state": self.state, "session_name": self._session_label()}
        self._precompact_result = None

        def worker() -> None:
            try:
                result["summary"] = summarizer.summarize_pre_compaction(
                    exported_session=exported,
                    current_summary=current_summary,
                )
            except Exception as error:
                result["error"] = repr(error)
            self._precompact_result = result

        self._precompact_thread = threading.Thread(target=worker, daemon=True, name="agit-precompact")
        self._precompact_thread.start()
        self._set_message(
            f"aGiTrack: Capturing session summary before compaction (session '{self._session_label()}')..."
        )

    def _service_precompact_summary(self) -> None:
        result = self._precompact_result
        if result is None:
            return
        self._precompact_result = None
        session = result.get("session_name") or "main"
        if "error" in result:
            self._debug(f"pre-compaction summary failed: {result['error']}")
            self._set_message(f"aGiTrack: Pre-compaction summary failed (session '{session}').")
            return
        repo = result.get("repo") or self.repo
        state = result.get("state") or self.state
        try:
            summary = result["summary"]
            state.session_summary = summary
            head_sha = repo.rev_parse("HEAD")
            if head_sha:
                state.session_summary_commit = head_sha
                repo.notes_add(head_sha, summary, namespace="agitrack/session-summary")
            self._set_message(f"aGiTrack: Session summary captured (session '{session}').")
        except Exception as error:
            self._debug(f"applying pre-compaction summary failed: {error!r}")

    def _base_user_edits_pending(self) -> bool:
        # The user's own edits land in the BASE repo's working tree (the session
        # worktree is the agent's sandbox), so the worktree-side pre-agent check
        # never sees them. Any uncommitted change there — tracked edit OR new
        # (non-ignored) file — counts, so it can be committed and synced into the
        # worktree before the agent runs. Re-prompt nagging is avoided by the
        # fingerprint gate in `_commit_base_user_edits_if_needed` (decline is
        # remembered until the base tree changes again), NOT by permanently
        # excluding a once-declined file — which used to strand it (the user could
        # never get it committed/synced through aGiTrack).
        base = self.base_repo
        if base is None or self.worktree is None:
            return False
        try:
            # Any uncommitted work counts: a tracked-file edit, OR a new untracked
            # (added-but-unstaged) file — `untracked_files()` lists those (and already
            # excludes aGiTrack's own `.agitrack/`). The user is offered to stage and
            # commit it; a "don't stage" answer only suppresses re-prompting for the
            # same tree state via the fingerprint gate, never permanently, so a file is
            # always offer-able.
            return base.has_tracked_changes() or bool(base.untracked_files())
        except Exception as error:
            self._debug(f"base user-edit check failed: {error!r}")
            return False

    def _commit_base_user_edits_if_needed(self) -> None:
        # Offer to commit the user's uncommitted base-repo edits before the
        # prompt reaches the agent, then sync the session worktree onto the new
        # base commit so the agent actually sees those edits. Declining skips
        # re-prompting until the base working tree changes again.
        if not self._base_user_edits_pending():
            self._base_edits_declined_status = None
            return
        status = self._base_edits_fingerprint()
        if status is not None and status == self._base_edits_declined_status:
            return  # already declined for this exact state; don't nag every prompt
        self._set_message(
            "Uncommitted changes in the base repo (your working directory) — committing them onto "
            "the base branch and syncing them into this session's worktree so the agent sees them."
        )
        self._render()
        if self._create_user_commit_popup(repo=self.base_repo, state=self._user_state(), include_declined=True):
            self._base_edits_declined_status = None
            self._reload_user_declined()
            # Reflect the new base commit into the session worktrees now — before
            # the prompt is forwarded — so the agent works from the user's edits
            # instead of waiting for the next base-HEAD poll.
            self._base_advanced = False
            self._sync_idle_worktrees_to_base()
        else:
            # Snapshot AFTER the popup: it stages tracked edits (add_tracked), so
            # the pre-popup fingerprint would never match the declined state.
            self._base_edits_declined_status = self._base_edits_fingerprint()

    def _base_edits_fingerprint(self) -> str | None:
        # Status plus the actual diff content, so a decline is remembered for
        # exactly the edits the user saw — any further edit re-prompts.
        try:
            return self.base_repo.status_short() + self.base_repo.diff_head()
        except Exception:
            return None

    def _resume_pending_prompt_if_ready(self) -> None:
        if self.pending_forwarded is None:
            return
        finished = self._finish_agent_parse_if_ready(quiet=True)
        if finished is None:
            if self.agent_parse_thread and self.agent_parse_thread.is_alive():
                self._set_message("aGiTrack is checking existing git changes before sending your prompt...", seconds=60)
                return
            # The parse already ran and deferred (its result is consumed): the
            # agent's latest turn is still in progress, so the uncommitted changes
            # belong to the in-flight agent — there is nothing of the user's to
            # commit before this prompt. Forward it now so the backend queues the
            # follow-up instead of holding it (and the "checking" message) forever.
            self._forward_pending_prompt()
            return
        if finished is False and self.actions.has_pre_agent_user_changes():
            self._set_message("User changes detected before agent runs.")
            self._render()
            if not self._create_user_commit_popup():
                self.pending_forwarded = None
                self.pending_prompt_text = ""
                self._set_message("Prompt not sent because existing user changes were not committed.")
                self._render()
                return
        self._commit_base_user_edits_if_needed()
        self._forward_pending_prompt()

    def _forward_pending_prompt(self) -> None:
        if self.pending_forwarded is None or self.master_fd is None:
            return
        forwarded = self.pending_forwarded
        prompt_text = self.pending_prompt_text
        self.pending_forwarded = None
        self.pending_prompt_text = ""
        self.passthrough_prompt.clear()
        self.passthrough_escape = None
        if prompt_text:
            self._record_user_prompt(prompt_text)
            # Flush the previous turn's deferred integration before starting this
            # one so it merges now rather than riding along with the new turn.
            self._integrate_committed_turn_before_new_turn()
            self._ensure_turn_branch()  # a new prompt starts a turn on its own branch
        self.agent_in_flight = True
        self._clear_message()
        if prompt_text:
            # Drop the previous turn's "created & merged" status line so it does
            # not linger into the new turn (clearing the registry too, or the
            # notice service tick would just repaint it).
            self._set_session_notice(self._session_label(), None)
        self.active.process.write(b"".join(forwarded))

    def _prune_declined_untracked(self, repo: GitRepo | None = None, state: AgitrackState | None = None) -> None:
        repo = repo or self.repo
        state = state or self.state
        state.keep_declined(repo.untracked_files())

    def _user_state(self) -> AgitrackState:
        # The user's working tree is the base repo (the session worktree is the
        # agent's sandbox and only holds tracked files). Its intentionally-unstaged
        # list and user commits live there. A fresh instance reads the latest
        # on-disk state so transient base-state writers elsewhere aren't clobbered.
        return AgitrackState(self.base_repo.repo, default_backend=self.global_config.default_backend)

    def _reload_user_declined(self) -> None:
        # Re-read the base repo's intentionally-unstaged list (after a command that
        # may have changed it) and seed the status-line cache.
        self._user_declined = self._user_state().declined_untracked()

    def _prune_user_declined(self) -> None:
        # Drop cached entries no longer untracked in the base tree (committed,
        # staged, or deleted out-of-band). Cheap enough for the base-poll cadence.
        untracked = set(self.base_repo.untracked_files())
        self._user_declined = [path for path in self._user_declined if path in untracked]

    def _forwarded_submits(self, forwarded: list[bytes]) -> bool:
        # Not every Enter submits the prompt — several keybindings insert a
        # NEWLINE in the backend's input box instead, and treating them as
        # submits ran the pre-agent flow (and could hold the keystroke or queue
        # a never-landing awaited prompt) on a mere line break:
        #   * Alt/Option+Enter sends ESC CR (the Apple Terminal / no-protocol
        #     way to get a newline in Claude's input),
        #   * a backslash immediately before Enter is Claude's "\<Enter>"
        #     line continuation,
        #   * newlines inside a bracketed paste are pasted content.
        data = b"".join(forwarded)
        data = _BRACKETED_PASTE_RE.sub(b"", data)
        data = data.replace(b"\x1b\r", b"").replace(b"\x1b\n", b"")
        for index, byte in enumerate(data):
            if byte not in (0x0D, 0x0A):
                continue
            if index == 0:
                # The Enter arrived in its own read; the backslash, if any, is
                # already at the end of the reconstructed prompt.
                if not bytes(self.passthrough_prompt).endswith(b"\\"):
                    return True
            elif data[index - 1 : index] != b"\\":
                return True
        return False

    def _update_passthrough_prompt(self, forwarded: list[bytes]) -> None:
        for chunk in forwarded:
            # Drop terminal escape sequences (arrow keys, etc.) so their residue
            # such as "[B" never leaks into the reconstructed prompt. State is
            # kept on the instance so sequences split across reads still match.
            if self.passthrough_escape is not None:
                self.passthrough_escape.extend(chunk)
                if _escape_sequence_complete(bytes(self.passthrough_escape)):
                    self.passthrough_escape = None
                continue
            if chunk == b"\x1b":
                self.passthrough_escape = bytearray(chunk)
                continue
            if chunk in {b"\r", b"\n"}:
                continue
            if chunk in {b"\x7f", b"\b"}:
                if self.passthrough_prompt:
                    self.passthrough_prompt.pop()
                continue
            if len(chunk) == 1 and chunk[0] >= 32:
                self.passthrough_prompt.extend(chunk)

    def _agent_is_active(self) -> bool:
        return self.agent_in_flight or (self.agent_parse_thread is not None and self.agent_parse_thread.is_alive())

    def _clear_agent_in_flight_if_idle(self) -> None:
        if self.agent_in_flight and time.monotonic() - self.last_child_output >= self.CHILD_IDLE_SECONDS:
            self.agent_in_flight = False

    def _record_user_prompt(self, prompt_text: str) -> None:
        CommitEngine(self.repo, self.state).record_user_prompt(prompt_text)

    def _await_followup(self, prompt_text: str) -> None:
        # Remember a prompt the user queued while the agent was busy so the next
        # commit waits for it to land as a turn (see _finish_agent_parse_if_ready),
        # keeping the queued prompt in the same commit as the turn it follows.
        self._awaited_followups = CommitEngine(self.repo, self.state).await_followup(
            prompt_text, self._awaited_followups
        )

    def _discover_spawned_session(self) -> str | None:
        # Identify the session aGiTrack just spawned: the newest one that did not
        # exist before launch. Falls back to the newest overall when no snapshot
        # was taken.
        refs = self.backend.list_sessions(self.repo.repo)
        if not refs:
            return None
        snapshot = self._pre_spawn_session_ids
        candidates = [ref for ref in refs if ref.id not in snapshot] if snapshot is not None else refs
        if not candidates:
            return None
        return max(candidates, key=lambda ref: ref.updated).id

    def _start_agent_parse(self) -> bool:
        # The worker holds its OWNING Session (issue #15): it can finish after
        # the user switched sessions, and resolving `self.backend` / `self.repo`
        # at that point would hit the WRONG session. CommitEngine.start_parse
        # captures the owning session explicitly and writes results back to it.
        return CommitEngine(self.repo, self.state, debug_fn=self._debug).start_parse(
            session=self.active,
            discover_session_id_fn=self._discover_spawned_session,
            debug_fn=self._debug,
        )

    def _finish_agent_parse_if_ready(
        self,
        *,
        quiet: bool,
        prompt_untracked: bool | None = None,
        integrate: bool = True,
        require_complete: bool = True,
    ) -> bool | None:
        if prompt_untracked is None:
            # Worktree sessions are isolated sandboxes, so agent commits there
            # auto-stage everything; only the main working tree prompts.
            prompt_untracked = self.worktree is None

        engine = CommitEngine(self.repo, self.state, debug_fn=self._debug)
        committed, new_awaited = engine.finish_parse_if_ready(
            session=self.active,
            quiet=quiet,
            prompt_untracked=prompt_untracked,
            require_complete=require_complete,
            awaited_followups=self._awaited_followups,
            agent_is_active_fn=self._agent_is_active,
            debug_fn=self._debug,
            note_session_change_fn=self._note_backend_session_change,
            mirror_fn=self._mirror_session_to_base,
            commit_fn=self._create_agent_commit_from_turns_popup,
            # Only the live interactive path (integrate=True) prompts about a
            # cancelled turn's leftover changes; the sync/exit finalize paths leave
            # them as-is rather than raising a modal during teardown.
            on_cancelled_fn=self._handle_cancelled_turn if integrate else None,
        )
        self._awaited_followups = new_awaited
        if committed:
            self.agent_in_flight = False
            self.last_status = ""
            if not quiet:
                # Repaint now so the "summarizing…" notice (set by the commit) is
                # visible before the integrate below runs git merge/fast-forward on
                # the main thread; the "created & merged" notice replaces it once
                # the turn actually lands in the base.
                self._render()
            if integrate:
                # Interactive integration only for the active session. The
                # sync/background commit path passes integrate=False. While a
                # background summary is pending, integration is deferred so the
                # summary can be amended in first; the idle-integration path
                # picks the commit up once it lands (or the wait deadline passes).
                if not self._summary_blocks_integration(time.monotonic()):
                    self._integrate_session_turn()
        return committed

    def _handle_cancelled_turn(self, turns) -> bool:
        """Offer commit-or-discard for a user-cancelled turn's leftover changes.

        Called by the commit engine when the user interrupted the agent (Esc) and
        it produced no committable response — so the normal auto-commit won't fire
        and the agent's partial edits would otherwise sit uncommitted in the
        worktree. If there are such changes, ask what to do with them.

        Returns True once the changes were committed or discarded (so the engine
        advances the parse watermark past the cancelled turn); False to leave the
        turn untouched — either nothing to act on, or the user chose to keep the
        changes and decide later (the next real turn's commit will fold them in).
        """
        last = turns[-1] if turns else None
        if last is None:
            return False
        turn_id = last.assistant_message_id or last.user_message_id or ""
        if turn_id and turn_id in self._cancel_prompted:
            return False  # already offered this turn — don't nag on every parse
        try:
            if not self.repo.has_changes():
                return False  # the cancellation left nothing behind to act on
        except Exception as error:
            self._debug(f"cancelled-turn change check failed: {error!r}")
            return False
        if turn_id:
            self._cancel_prompted.add(turn_id)
        choice = self._select_popup(
            "The agent was interrupted before finishing, leaving uncommitted changes.",
            ["Keep them (commit with your next turn)", "Commit the changes now", "Discard the changes"],
        )
        if choice is None or choice.startswith("Keep"):
            self._set_message("Keeping the agent's changes — they'll be committed with your next turn.")
            self._render()
            return False
        if choice.startswith("Commit"):
            committed = self._create_agent_commit_from_turns_popup(
                turns=turns,
                backend=getattr(self.backend, "name", None) or self.state.backend,
                backend_session_id=self.state.backend_session_id,
                model=self.state.model,
                quiet=False,
                prompt_untracked=self.worktree is None,
            )
            self._set_message(
                "Committed the agent's interrupted changes." if committed else "Nothing staged to commit."
            )
            self._render()
            return committed
        # Discard — destructive, so confirm before throwing the work away.
        confirm = self._select_popup(
            "Discard ALL uncommitted changes from the interrupted turn? This cannot be undone.",
            ["No, keep them", "Yes, discard"],
        )
        if confirm == "Yes, discard":
            try:
                self.repo.discard_all_changes()
            except Exception as error:
                self._debug(f"discard cancelled-turn changes failed: {error!r}")
                self._set_message("Could not discard the changes.")
                self._render()
                return False
            self._set_message("Discarded the agent's interrupted changes.")
            self._render()
            return True
        self._set_message("Keeping the agent's changes.")
        self._render()
        return False

    def _commit_available_agent_turns(self, *, quiet: bool) -> bool:
        if self._finish_agent_parse_if_ready(quiet=quiet) is True:
            return True
        self._start_agent_parse()
        return False

    def _maybe_agent_commit(self) -> None:
        now = time.monotonic()
        if self.file_change_event.is_set():
            self.file_change_event.clear()
            self._last_change_at = now  # a real worktree write just landed
            self.status_check_pending = True
        elif Observer is None:
            if now - self.last_poll < self.POLL_SECONDS:
                return
            self.last_poll = now
            self.status_check_pending = True
        self._clear_agent_in_flight_if_idle()
        # Defer the `git status` subprocess out of the active edit/type window. A
        # commit can only happen once the worktree AND the backend have been quiet
        # for the stable/idle windows, so reading status on every keystroke-adjacent
        # file event buys nothing — and doing it on the main thread is exactly what
        # made typing lag right after an edit. Hold off until things settle; then a
        # single read serves the commit. (With no file watcher, `_last_change_at` is
        # never advanced, so this naturally reduces to the old poll-then-read once
        # the backend goes idle.)
        worktree_settled = now - self._last_change_at >= self.FILE_STABLE_SECONDS
        backend_idle = now - self.last_child_output >= self.CHILD_IDLE_SECONDS
        if self.status_check_pending and worktree_settled and backend_idle:
            status = self.repo.status_short()
            self._prune_declined_untracked()
            self.status_check_pending = False
            if status != self.last_status:
                self.last_status = status
                self.last_status_change = now
            if status.strip():
                self.parse_pending = True
                self.last_parse_attempt_status = ""
            else:
                self.parse_pending = False
                self.last_parse_attempt_status = ""
        elif self.status_check_pending:
            # Worktree or backend still active — revisit next tick without touching git.
            if self.verbose:
                self._render_status(f"git changes found; waiting for {self.backend.name} to become idle")
            return
        else:
            status = self.last_status
        if not status.strip():
            if self.verbose:
                self._render_status("no git changes")
            self._integrate_agent_made_commits_if_idle(now)
            # A turn can leave only git-ignored files (nothing staged, clean tree),
            # so there's no commit to hang the copy offer on — offer here too.
            self._maybe_offer_copy_when_idle(now)
            return
        if self.agent_parse_thread and self.agent_parse_thread.is_alive():
            if self.verbose:
                self._render_status(f"git changes found; parsing {self.backend.name} session")
            return
        if (
            now - self._last_change_at < self.FILE_STABLE_SECONDS
            or now - self.last_child_output < self.CHILD_IDLE_SECONDS
        ):
            if self.verbose:
                self._render_status(f"git changes found; waiting for {self.backend.name} to become idle")
            return
        finished = self._finish_agent_parse_if_ready(quiet=not self.verbose)
        if finished is True:
            self.parse_pending = False
            self.last_parse_attempt_status = ""
            committed = True
        else:
            if finished is False:
                self.parse_pending = False
                self.last_parse_attempt_status = status
                # The turn concluded with nothing to commit (e.g. the only changed
                # files were untracked ones the user declined to stage). They still
                # live only in the worktree, so offer to copy them across — the same
                # offer the committed path makes below.
                self._maybe_offer_copy_when_idle(now)
            if not self.parse_pending or status == self.last_parse_attempt_status:
                if self.verbose:
                    self._render_status("git changes found; waiting for new file changes")
                return
            if now - self.last_parse_finish < self.PARSE_COOLDOWN_SECONDS:
                if self.verbose:
                    self._render_status("git changes found; waiting for parse cooldown")
                return
            self._start_agent_parse()
            return
        if not committed:
            if self.verbose:
                self._render_status("git changes found; no new final response available")
            return
        # Do NOT announce the commit here. A worktree session announces it only
        # once it merges into the base (via _announce_agent_commit at
        # integration); meanwhile the "summarizing…" notice must stand. A
        # no-worktree session never integrates, so it announces here — but only
        # after any summary has landed (so summarizing shows first), which the
        # summary service handles; if summarization is off there is no summary to
        # wait for, so announce immediately.
        if self.verbose:
            self._render_status("committed agent turn")
        elif self.worktree is None and self._summary_pending is None:
            self._announce_agent_commit()
        # The turn's committed changes integrate into the base branch, but any files
        # the agent left UNCOMMITTED in the worktree (e.g. untracked files the user
        # declined to stage) never reach the base working directory. Offer to copy
        # those over so they aren't stranded in the session's worktree. Hand the offer to the
        # main thread (it presents the popup) so this worker pass returns and the next
        # commit/summary/merge keeps flowing while the user decides.
        self._request_copy_offer()

    def _maybe_offer_copy_when_idle(self, now: float) -> None:
        """Idle-gated entry to the copy offer, used by the turn-polling loop so the
        offer fires after a turn that produced **no** commit too (e.g. the agent only
        touched git-ignored files, so nothing was staged) — not just committed turns.
        Skipped while a turn is running or a merge is in progress, so it never
        interrupts the agent; the offer itself is fingerprint-deduped, so a file is
        offered only once per change."""
        if self.worktree is None or self.merge_ctx:
            return
        if self._agent_is_active() or now - self.last_child_output < self.CHILD_IDLE_SECONDS:
            return
        self._request_copy_offer()

    def _offer_user_commit_for_worktree_edits(self) -> None:
        """Offer to COMMIT the user's own uncommitted edits in this worktree (tracked
        changes, or new files they haven't declined) — those belong in git, not just
        copied to the base directory. Run right before the copy offer so that when a user
        edit AND copy-able leftovers both exist, BOTH prompts appear: a commit prompt for
        the edits here, and the copy prompt (for declined/ignored files) afterwards.
        A no-op in the normal case, where the turn's auto-commit already committed the
        user's tracked edits and only declined/ignored files remain."""
        actions = getattr(self, "actions", None)
        check = getattr(actions, "has_pre_agent_user_changes", None)
        if check is None:
            return
        try:
            if check():
                self._set_message("Uncommitted changes in this session's worktree — commit them?")
                self._render()
                self._create_user_commit_popup()
        except Exception as error:
            self._debug(f"pre-copy user-commit offer failed: {error!r}")

    def _offer_copy_unstaged_to_base(self, *, context: str = "turn") -> None:
        """Offer to copy the worktree's would-be-stranded files into the base repo dir.

        Only relevant for an isolated worktree session: changes committed this turn
        integrate into the base branch, but files the agent left uncommitted (declined
        untracked files) or that are git-ignored live only in the worktree and the user —
        working in the base directory — never sees them. The user's OWN uncommitted edits
        are offered for a git commit first (`_offer_user_commit_for_worktree_edits`), so a
        commit prompt and the copy prompt can both appear when both apply.

        ``context``:
          - ``"turn"``   — after an idle turn in the ACTIVE session (default). Background
            sessions are never interrupted; their files are offered on switch/exit instead.
          - ``"switch"`` — when the user switches TO this session.
          - ``"exit"``   — last offer before the worktree (and these files) are deleted: the
            prompt warns that discarding them is permanent, and pressing Esc aborts the exit
            so the user can deal with the files themselves.

        A file already accepted/left in place isn't re-offered until its content changes
        (fingerprint). Declining mutes the whole current SET of paths: aGiTrack won't ask
        again while only those files keep changing — only a genuinely NEW path re-opens the
        set (ask about all again), in EVERY context including exit. The mute clears on
        session switch and aGiTrack restart.

        Runs collect (the worktree read) then present (the popup + copy) inline — used for the
        ``switch``/``exit`` contexts, which are already on the main thread. The per-turn offer
        instead goes through ``_request_copy_offer`` so the git worker never blocks on it."""
        collected = self._collect_copy_candidates(context=context)
        if collected is not None:
            self._present_copy_offer(collected, context=context)

    def _request_copy_offer(self, *, context: str = "turn") -> None:
        """Git-worker side of the turn copy offer: do the worktree read HERE (off the main
        thread) and, if there are fresh files, stash them for the main thread to present. The
        worker never blocks on the popup, so commit/summary/merge keep flowing while the user
        decides — only a merge CONFLICT (its own popup) holds integration up."""
        if self._pending_copy_offer is not None:
            return  # a previously collected batch is still waiting to be presented
        collected = self._collect_copy_candidates(context=context)
        if collected is not None:
            self._pending_copy_offer = (context, collected)
            self._wake_main_loop()  # present it on the next reactor tick

    def _present_pending_copy_offer(self) -> None:
        """Main thread: present a copy offer the git worker collected for this turn, if any.
        Deliberately NOT under `_pipeline_lock` — the worker must keep committing/merging
        while this popup is open. A merge that needs the user (a conflict) queues its own
        popup via the modal mailbox, so the two are answered one at a time."""
        pending = self._pending_copy_offer
        if pending is None:
            return
        self._pending_copy_offer = None
        context, collected = pending
        if self.worktree is None or self.base_repo is None:
            return  # the worktree went away before we could ask (a switch/exit) — drop it
        self._present_copy_offer(collected, context=context)

    def _collect_copy_candidates(
        self, *, context: str
    ) -> tuple[list[str], list[tuple[str, tuple[int, int]]], set[str]] | None:
        """The worktree read + dedup behind the copy offer (git/file I/O, no popup). Returns
        the fresh files to offer as ``(rels, fresh, current_set)``, or None when there is
        nothing to ask about. Safe to run on the git worker."""
        if self.worktree is None or self.base_repo is None:
            return None
        base_dir = self.base_repo.repo
        wt_dir = self.repo.repo
        if base_dir == wt_dir:
            return None
        if context != "turn":
            # switch/exit run on the main thread: offer to COMMIT the user's own edits first.
            # The turn path runs on the git worker and must NOT raise a (blocking) popup or
            # commit from there; those edits are still offered at the next switch/exit.
            self._offer_user_commit_for_worktree_edits()
        candidates = self._uncommitted_worktree_files()
        if not candidates:
            return None
        current = set(candidates)
        # A genuinely new path (one we haven't muted) re-opens the whole set: drop the
        # decline mute and the per-file fingerprint memory so "all files" means all again.
        if self._copy_declined and (current - self._copy_declined):
            self._copy_declined.clear()
            self._copy_prompted.clear()
        fresh: list[tuple[str, tuple[int, int]]] = []
        for rel in candidates:
            fingerprint = self._file_fingerprint(wt_dir / rel)
            if fingerprint is None:
                continue
            if rel in self._copy_declined:
                continue  # muted by a prior decline; not re-asked even on exit (no new file)
            if self._copy_prompted.get(rel) == fingerprint:
                continue  # already offered/copied at this exact content
            fresh.append((rel, fingerprint))
        if not fresh:
            return None
        for rel, fingerprint in fresh:
            self._copy_prompted[rel] = fingerprint
        rels = [rel for rel, _ in fresh]
        return (rels, fresh, current)

    def _present_copy_offer(
        self,
        collected: tuple[list[str], list[tuple[str, tuple[int, int]]], set[str]],
        *,
        context: str,
    ) -> None:
        """Main-thread side: show the copy popup(s) and copy the files. Runs WITHOUT the
        pipeline lock so the git worker keeps committing/merging behind the popup; a merge
        that needs the user (a conflict) just queues its own popup, answered one at a time."""
        if self.base_repo is None:
            return
        rels, fresh, current = collected
        base_dir = self.base_repo.repo
        wt_dir = self.repo.repo
        on_exit = context == "exit"
        # The file list is shown vertically (one per line, scrolls with PgUp/PgDn), led by
        # a "File(s):" line (set off with a blank line above it) so it's clear the message
        # refers to these names.
        if on_exit:
            title = (
                f"This session's worktree ({wt_dir}) has {len(rels)} file(s) not in the base repo "
                f"that will be DELETED when the worktree is removed on exit. Copy them into the base "
                f"repo ({base_dir}) first? (Press Esc to cancel the exit and handle them yourself.)\n"
                f"\nFile(s):"
            )
            options = ["No, discard them with the worktree", "Yes, copy to the base repo"]
        else:
            title = (
                f"This session's worktree ({wt_dir}) has {len(rels)} file(s) that won't be merged "
                f"into the base — intentionally unstaged or git-ignored. Copy them into the base repo ({base_dir})?\n"
                f"(Decline and aGiTrack won't ask about this set again until the files change as a set "
                f"or you switch sessions.)\n"
                f"\nFile(s):"
            )
            options = ["No, leave them in the worktree", "Yes, copy to the base repo"]
        choice = self._select_popup(title, options, detail=rels)
        if on_exit and choice is None:
            # Esc on the exit offer cancels the exit entirely (handled by the caller) so the
            # user can copy/move the files themselves — nothing is discarded or removed. Forget
            # we offered them so the NEXT exit re-offers (the user didn't actually resolve them).
            for rel in rels:
                self._copy_prompted.pop(rel, None)
            self._exit_aborted = True
            return
        if choice is None or choice.startswith("No"):
            if on_exit:
                self._set_message(f"Discarding {len(rels)} file(s) with the worktree.", seconds=6.0)
                self._render()
            else:
                self._copy_declined |= current  # mute the whole set until it changes / switch
                self._notice_files_remain(wt_dir, rels)
            return
        # Files that would overwrite something already in the base are handled in one
        # up-front choice: overwrite them all, keep them all (the new files still copy),
        # or confirm each one individually.
        conflicts = [rel for rel in rels if (base_dir / rel).exists()]
        overwrite_mode = "all"  # no conflicts → moot
        if conflicts:
            answer = self._select_popup(
                f"{len(conflicts)} of these already exist in the base repo. Overwrite them?",
                ["No, keep the base versions", "Yes, overwrite all", "Let me confirm each one"],
                detail=conflicts,
            )
            if answer == "Yes, overwrite all":
                overwrite_mode = "all"
            elif answer == "Let me confirm each one":
                overwrite_mode = "each"
            else:  # "No, keep the base versions" or cancelled
                overwrite_mode = "none"
        remained: list[str] = []
        copied = 0
        # Announce the copy BEFORE it runs (a wholly-ignored directory like node_modules can
        # take a moment to copy), so the user sees that work is happening rather than the
        # "Copied …" confirmation appearing as if from nowhere. Only when something will
        # actually be copied — a "keep the base versions" choice on every conflict copies
        # nothing, so it must not claim to be copying.
        attempting = [rel for rel, _ in fresh if not ((base_dir / rel).exists() and overwrite_mode == "none")]
        if attempting:
            self._set_message(f"Copying {len(attempting)} file(s) into the base repo directory…")
            self._render()
        for rel, _ in fresh:
            src, dst = wt_dir / rel, base_dir / rel
            if dst.exists():
                if overwrite_mode == "none":
                    remained.append(rel)  # keep all base versions
                    continue
                if overwrite_mode == "each":
                    per = self._select_popup(
                        f"'{rel}' already exists in the base repo. Overwrite it?",
                        ["No, keep the base version", "Yes, overwrite"],
                    )
                    if per != "Yes, overwrite":
                        remained.append(rel)
                        continue
            try:
                if src.is_dir():  # a wholly-ignored directory, reported as `dir/`
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                copied += 1
            except OSError as error:
                self._debug(f"copy {rel} to base failed: {error!r}")
                remained.append(rel)
        if remained:
            self._notice_files_remain(wt_dir, remained)
        elif copied:
            self._set_message(f"Copied {copied} file(s) into the base repo directory.")
            self._render()

    def _uncommitted_worktree_files(self) -> list[str]:
        """Repo-relative paths of files left in the worktree that no commit will carry
        into the base directory: unstaged tracked edits, untracked files, and
        git-*ignored* files (build output, local data, etc. the agent may have created).

        Always skipped: aGiTrack's own ``.agitrack/`` and any path whose final name
        starts with ``_`` or ``.`` — generated/hidden scaffolding (``__pycache__``,
        ``.venv``, ``.env`` …) the user doesn't want copied back. So a turn that touched
        only such files yields an empty list and is never offered for copying."""
        files: list[str] = []
        try:
            status = self.repo.status_short_ignored()
        except Exception as error:
            self._debug(f"status for uncommitted-files check failed: {error!r}")
            return files
        for line in status.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:  # a rename: take the destination
                path = path.split(" -> ", 1)[1]
            path = path.strip().strip('"')
            if not path or path.startswith(".agitrack/"):
                continue
            if self._copy_name_is_skipped(path):
                continue
            files.append(path)
        return files

    @staticmethod
    def _copy_name_is_skipped(path: str) -> bool:
        """True for paths whose final component starts with ``_`` or ``.`` (a wholly
        ignored directory is reported as ``dir/``, so the trailing slash is stripped
        first). These are never offered for copying into the base directory."""
        name = path.rstrip("/").rsplit("/", 1)[-1]
        return name.startswith("_") or name.startswith(".")

    @staticmethod
    def _file_fingerprint(path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_size, stat.st_mtime_ns)

    def _notice_files_remain(self, wt_dir, files: list[str]) -> None:
        listing = ", ".join(files[:5]) + (" …" if len(files) > 5 else "")
        self._set_message(
            f"{len(files)} file(s) left in this session's worktree: {listing}. They're available at "
            f"{wt_dir} for now, but **the worktree is removed when aGiTrack exits or the session "
            f"integrates,** so copy out anything you want to keep.",
            seconds=15.0,
        )
        self._render()

    def _integrate_agent_made_commits_if_idle(self, now: float) -> None:
        # The agent can run `git commit` itself (some workflows ask it to).
        # Those turns leave the worktree CLEAN, so the auto-commit path — which
        # integration used to piggyback on exclusively — never runs, and the
        # turn branch sat ahead of base until exit/restart. When the session is
        # idle, its tree is clean, and its branch holds unintegrated commits,
        # integrate them now.
        if self.worktree is None or self.merge_ctx:
            return
        if self._base_branch is None or self._integration_paused or self._delay_merge:
            return  # --delay-merge: don't integrate idle commits behind the user's back
        if self._agent_is_active() or now - self.last_child_output < self.CHILD_IDLE_SECONDS:
            return
        if now - self._idle_integrate_at < self.BASE_POLL_SECONDS:
            return
        self._idle_integrate_at = now
        try:
            branch = self.repo.current_branch()
            if not is_managed_branch(branch) or not self.base_repo.log_range(self._base_branch, branch):
                return
        except Exception as error:
            self._debug(f"idle integrate check failed: {error!r}")
            return
        if self._summary_blocks_integration(now):
            return  # summary worker still running — amend first, then integrate
        if not self._attach_trace_to_backend_commits(now):
            return  # parse still in flight — integrate once the trace is attached
        self._debug(f"integrating agent-made commits on {branch} (worktree clean and idle)")
        self._integrate_session_turn()

    def _attach_trace_to_backend_commits(self, now: float) -> bool:
        """Before integrating commits the backend made itself, give the parse
        pipeline a chance to amend the trace/metadata onto them (#35).

        Returns True when integration may proceed: the trace was attached, no
        attachment is needed/possible, or the attach deadline passed (the
        commits then integrate as-is rather than sit on the branch forever).
        """
        if not self._uncovered_backend_commits():
            self._attach_uncovered_until = 0.0
            return True
        if self._attach_uncovered_until == 0.0:
            self._attach_uncovered_until = now + self.PARSE_COOLDOWN_SECONDS * 3
        finished = self._finish_agent_parse_if_ready(quiet=not self.verbose, integrate=False)
        if finished is True:
            # commit_turns amended the backend's HEAD commit; integrate it now.
            self._attach_uncovered_until = 0.0
            return True
        if finished is False:
            # Parse consumed with nothing to attach (e.g. the turn was already
            # committed): there is no trace to recover, integrate as-is.
            self._debug("no trace available for backend-made commits; integrating as-is")
            self._attach_uncovered_until = 0.0
            return True
        if now >= self._attach_uncovered_until:
            self._debug("trace attach deadline passed; integrating backend-made commits as-is")
            self._attach_uncovered_until = 0.0
            return True
        self._start_agent_parse()
        return False

    # Host-terminal operations route through the platform host object (POSIX termios /
    # Windows Win32 console) once run() created it; before that (unit tests construct a
    # runner without run()), they fall back to the POSIX TerminalHost mixin directly.
    def _pause_child_ui(self) -> None:
        if self._host is not None:
            self._host.pause_child_ui()
        else:
            TerminalHost.pause_child_ui(self)

    def _resume_child_ui(self) -> None:
        if self._host is not None:
            self._host.resume_child_ui(self._render)
        else:
            TerminalHost.resume_child_ui(self, self._render)

    def _set_raw(self) -> None:
        if self._host is not None:
            self._host.set_raw()
        else:
            TerminalHost.set_raw(self)

    def _set_cooked(self) -> None:
        if self._host is not None:
            self._host.set_cooked()
        else:
            TerminalHost.set_cooked(self)

    def _restore_terminal(self) -> None:
        if self._host is not None:
            self._host.restore_terminal()
        else:
            TerminalHost.restore_terminal(self)

    def _disable_host_terminal_modes(self) -> None:
        if self._host is not None:
            self._host.disable_host_terminal_modes()
        else:
            TerminalHost.disable_host_terminal_modes(self)

    def _resize_child(self) -> None:
        if self.master_fd is None:
            return
        try:
            self.rows, self.cols = self._terminal_size()
            if self.screen is not None:
                self.screen.resize(max(self.rows - 1, 1), self.cols)
            self.scroll_back = 0  # history geometry changed; return to live view
            # PTY ioctl delegated to the session-owned BackendProcess.
            self.active.process.resize(max(self.rows - 1, 1), self.cols)
            self._render()
        except OSError:
            pass

    def _terminal_size(self) -> tuple[int, int]:
        if self._host is not None:
            return self._host.terminal_size()
        return TerminalHost.terminal_size(self)

    def _stdin_fileno(self) -> int:
        """The select-able stdin fd: the real terminal fd on POSIX, the host's
        console→socket bridge fd on Windows (Windows can't select on a console handle)."""
        return self._host.stdin_fileno() if self._host is not None else sys.stdin.fileno()

    def _read_stdin(self, length: int) -> bytes:
        if self._host is not None:
            return self._host.read_stdin(length)
        return os.read(sys.stdin.fileno(), length)


# ---------------------------------------------------------------------------
# P3 backward-compat layer: per-session state as runner attributes.
#
# Production runner methods use ``self.repo``, ``self.state``, ``self.backend``
# etc. throughout. These are Session-level fields that live on ``self.active``;
# the property layer delegates every Session.FIELDS name to the active session
# so that context-switching helpers like ``_with_session`` work correctly.
#
# The ``ProxyRunner.__new__`` test idiom has been replaced by
# ``ProxyRunner.for_testing()`` (Stage 1–3); Session.bare() lazy materialisation
# in the ``active`` getter and the ``_active_index_compat`` fallback have been
# removed (Stage 4). The delegation properties themselves remain because
# all production call sites still use the short forms.
# ---------------------------------------------------------------------------


def _delegate_to_active_session(field: str) -> property:
    def getter(self):
        return getattr(self.active, field)

    def setter(self, value):
        setattr(self.active, field, value)

    return property(
        getter,
        setter,
        doc=f"Per-session state: delegates to ``self.active.{field}``.",
    )


for _field in Session.FIELDS:
    # _base_branch gets a custom property below (it must also keep the single
    # IntegrationService in sync on every write), so skip the plain delegation.
    if _field == "_base_branch":
        continue
    setattr(ProxyRunner, _field, _delegate_to_active_session(_field))
del _field


def _get_base_branch(self):
    return self.active._base_branch


def _set_base_branch(self, value):
    # Per-session integration target. Writing it also re-points the single
    # IntegrationService, so a session always merges into its own branch.
    self.active._base_branch = value
    svc = self.__dict__.get("_integration_svc")
    if svc is not None:
        svc.base_branch = value


# Attached via setattr (not a plain assignment) so mypy uses the class-level
# annotation above rather than flagging the property vs. str|None mismatch.
setattr(
    ProxyRunner,
    "_base_branch",
    property(_get_base_branch, _set_base_branch, doc="Per-session integration target (delegates to self.active)."),
)
