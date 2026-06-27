import os
import sys
import threading
import time

import pytest

import types

_posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")

from agitrack.backends.base import TokenUsage
from agitrack.transcripts.opencode import SessionTurn
from agitrack.backends.proxy_agents import make_proxy_agent
from agitrack.proxy import ProxyInput, ProxyRunner, _escape_sequence_complete, _short_session, detect_color_mode
from agitrack.proxy.integration import MergeContext, MergePhase
from agitrack.proxy.session import Session
from agitrack.transcripts import ExportedSession, SessionRef
from agitrack.config import AgitrackState
from proxy_helpers import make_runner


class _FakeBackend:
    name = "fake"

    def __init__(self, refs):
        self._refs = refs

    def list_sessions(self, repo):
        return list(self._refs)


def _runner_with_sessions(refs):
    runner = make_runner(
        repo=type("Repo", (), {"repo": "/repo"})(),
        backend=_FakeBackend(refs),
    )
    return runner


def test_discover_spawned_session_picks_the_new_session():
    refs = [SessionRef("old", 100.0), SessionRef("new", 200.0)]
    runner = _runner_with_sessions(refs)
    runner._pre_spawn_session_ids = {"old"}
    assert runner._discover_spawned_session() == "new"


def test_discover_spawned_session_returns_none_when_nothing_new():
    refs = [SessionRef("old", 100.0), SessionRef("older", 50.0)]
    runner = _runner_with_sessions(refs)
    runner._pre_spawn_session_ids = {"old", "older"}
    assert runner._discover_spawned_session() is None


def test_discover_spawned_session_without_snapshot_uses_newest():
    refs = [SessionRef("a", 100.0), SessionRef("b", 300.0), SessionRef("c", 200.0)]
    runner = _runner_with_sessions(refs)
    runner._pre_spawn_session_ids = None
    assert runner._discover_spawned_session() == "b"


def test_short_session():
    assert _short_session("35e076c5-8653-439c") == "35e076c5"
    assert _short_session(None) == "(none)"


class FakeCommitRepo:
    def __init__(self):
        self.message = ""

    def add_tracked(self):
        pass

    def has_staged_changes(self):
        return True

    def commit(self, message: str):
        self.message = message
        return "abc1234"  # mirror GitRepo.commit returning the new short SHA


def test_proxy_ctrl_g_enters_command_mode():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07git-unstaged\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "git-unstaged"
    assert should_exit is False


def test_kitty_ctrl_key_decoding():
    """Test that kitty keyboard protocol control keys are decoded to plain bytes."""
    from agitrack.proxy.runner import _decode_kitty_ctrl_keys

    # Ctrl-O (o=111) should decode to 0x0f
    assert _decode_kitty_ctrl_keys(b"\x1b[111;5u") == b"\x0f"

    # Ctrl-G (g=103) should decode to 0x07
    assert _decode_kitty_ctrl_keys(b"\x1b[103;5u") == b"\x07"

    # Ctrl-A (a=97) should decode to 0x01
    assert _decode_kitty_ctrl_keys(b"\x1b[97;5u") == b"\x01"

    # Ctrl-Z (z=122) should decode to 0x1a
    assert _decode_kitty_ctrl_keys(b"\x1b[122;5u") == b"\x1a"

    # Mixed content: text + Ctrl-O + text
    assert _decode_kitty_ctrl_keys(b"hello\x1b[111;5uworld") == b"hello\x0fworld"

    # Non-ctrl kitty sequences should not be decoded (modifier != 5)
    # Shift+O would be modifier 2, so \x1b[111;2u should remain unchanged
    assert _decode_kitty_ctrl_keys(b"\x1b[111;2u") == b"\x1b[111;2u"

    # Plain bytes should pass through unchanged
    assert _decode_kitty_ctrl_keys(b"\x0f") == b"\x0f"
    assert _decode_kitty_ctrl_keys(b"hello") == b"hello"


def test_kitty_escape_key_decoding():
    """Test that kitty keyboard protocol Escape key is decoded to plain \\x1b."""
    from agitrack.proxy.runner import _decode_kitty_ctrl_keys

    # Escape key (keycode 27) should decode to \x1b
    assert _decode_kitty_ctrl_keys(b"\x1b[27u") == b"\x1b"

    # Escape key with explicit modifier 1 should also decode to \x1b
    assert _decode_kitty_ctrl_keys(b"\x1b[27;1u") == b"\x1b"

    # Mixed content: Escape + text
    assert _decode_kitty_ctrl_keys(b"\x1b[27uhello") == b"\x1bhello"

    # Multiple Escape keys
    assert _decode_kitty_ctrl_keys(b"\x1b[27u\x1b[27u") == b"\x1b\x1b"

    # Plain Escape should pass through unchanged
    assert _decode_kitty_ctrl_keys(b"\x1b") == b"\x1b"


def test_modify_other_keys_ctrl_decoding():
    """iTerm2 answers modifyOtherKeys with CSI 27 ; mod ; code ~ — decode Ctrl keys.

    Regression: in iTerm on macOS Ctrl-C / Ctrl-G arrived in this form and were
    forwarded to the backend instead of opening aGiTrack's exit/menu.
    """
    from agitrack.proxy.runner import _decode_kitty_ctrl_keys

    # Ctrl-C (c=99) → 0x03; Ctrl-G (g=103) → 0x07.
    assert _decode_kitty_ctrl_keys(b"\x1b[27;5;99~") == b"\x03"
    assert _decode_kitty_ctrl_keys(b"\x1b[27;5;103~") == b"\x07"
    # Ctrl-A / Ctrl-Z bounds.
    assert _decode_kitty_ctrl_keys(b"\x1b[27;5;97~") == b"\x01"
    assert _decode_kitty_ctrl_keys(b"\x1b[27;5;122~") == b"\x1a"
    # Mixed with surrounding text.
    assert _decode_kitty_ctrl_keys(b"ab\x1b[27;5;103~cd") == b"ab\x07cd"
    # Escape: CSI 27 ; 1 ; 27 ~ → \x1b.
    assert _decode_kitty_ctrl_keys(b"\x1b[27;1;27~") == b"\x1b"
    # Non-Ctrl modifiers are left encoded for the backend (Shift+Enter, mod 2,
    # code 13 — must NOT be turned into a bare \r that would submit a prompt).
    assert _decode_kitty_ctrl_keys(b"\x1b[27;2;13~") == b"\x1b[27;2;13~"


def test_modify_other_keys_ctrl_g_opens_menu():
    # End-to-end of the iTerm path: the modifyOtherKeys Ctrl-G decodes to \x07 and
    # is matched as the (default) menu key rather than forwarded to the backend.
    from agitrack.proxy.runner import _decode_kitty_ctrl_keys

    decoded = _decode_kitty_ctrl_keys(b"\x1b[27;5;103~")
    parser = ProxyInput(menu_key=b"\x07")  # default Ctrl-G
    forwarded, _local_echo, command, should_exit = parser.feed(decoded + b"git-unstaged\r")

    assert forwarded == []
    assert command == "git-unstaged"
    assert should_exit is False


def test_modify_other_keys_ctrl_c_triggers_exit():
    # The modifyOtherKeys Ctrl-C decodes to \x03 and starts aGiTrack's exit flow
    # instead of being forwarded to the backend.
    from agitrack.proxy.runner import _decode_kitty_ctrl_keys

    decoded = _decode_kitty_ctrl_keys(b"\x1b[27;5;99~")
    parser = ProxyInput(menu_key=b"\x07")
    forwarded, _local_echo, _command, should_exit = parser.feed(decoded)

    assert should_exit is True
    assert forwarded == []


def _drive_host_input(runner, parser, chunks):
    """Mimic _reactor_stdin_phase's tail-hold → decode → feed pipeline.

    Runs each chunk through the same steps the reactor does for host stdin so a
    test can exercise the full path (including escape sequences split across
    reads) for either keyboard protocol.
    """
    from agitrack.proxy.runner import _decode_kitty_ctrl_keys

    forwarded_all: list[bytes] = []
    command = None
    should_exit = False
    for chunk in chunks:
        data = runner._input_tail + chunk
        data, runner._input_tail = runner._hold_incomplete_tail(data)
        data = _decode_kitty_ctrl_keys(data)
        forwarded, _echo, cmd, ex = parser.feed(data)
        forwarded_all.extend(forwarded)
        command = cmd or command
        should_exit = should_exit or ex
    return forwarded_all, command, should_exit


# The same Ctrl-G / Ctrl-C in each of the two enhanced keyboard encodings a host
# terminal may use once the backend negotiates one: kitty (CSI code;5u, the
# earlier fix) and xterm modifyOtherKeys (CSI 27;5;code~, what iTerm2 sends).
_CTRL_G_ENCODINGS = {"kitty": b"\x1b[103;5u", "modifyOtherKeys": b"\x1b[27;5;103~"}
_CTRL_C_ENCODINGS = {"kitty": b"\x1b[99;5u", "modifyOtherKeys": b"\x1b[27;5;99~"}


@pytest.mark.parametrize("protocol", ["kitty", "modifyOtherKeys"])
def test_menu_key_opens_under_both_keyboard_protocols(protocol):
    runner = make_runner()
    parser = ProxyInput(menu_key=b"\x07")  # default Ctrl-G
    forwarded, command, should_exit = _drive_host_input(
        runner, parser, [_CTRL_G_ENCODINGS[protocol] + b"git-unstaged\r"]
    )
    assert forwarded == []
    assert command == "git-unstaged"
    assert should_exit is False


def test_menu_key_again_closes_the_palette():
    # Ctrl-G opens the command palette; pressing it again CLOSES it (a toggle), rather than
    # typing a literal control byte into the command buffer.
    parser = ProxyInput(menu_key=b"\x07")
    parser.feed(b"\x07")
    assert parser.capturing is True
    forwarded, _echo, command, should_exit = parser.feed(b"\x07")
    assert parser.capturing is False  # closed
    assert command is None and should_exit is False
    assert parser.text() == ""  # the menu key was NOT typed into the buffer


def test_after_menu_command_closes_all_when_ctrl_g_requested():
    # Ctrl-G inside a popup requests a full close: _after_menu_command must drop to the agent
    # (not reopen the Ctrl-G palette), and consume the request.
    runner = make_runner()
    reopened: list = []
    runner._reopen_command_palette = lambda: reopened.append(True)
    runner._exit_menu_requested = True

    runner._after_menu_command(runner._MENU_UP)
    assert reopened == []  # did NOT reopen the palette
    assert runner._exit_menu_requested is False  # consumed

    runner._after_menu_command(runner._MENU_UP)  # ordinary back-out
    assert reopened == [True]  # reopens the palette as usual


def test_run_modal_menu_key_requests_a_full_close():
    import types

    from agitrack.proxy.modal import SelectModal

    runner = make_runner()
    runner.input = types.SimpleNamespace(menu_key=b"\x07")
    runner._set_message = lambda *a, **k: None
    runner._clear_message = lambda: None
    runner._render = lambda: None
    runner._popup_read_input = lambda: b"\x07"  # Ctrl-G pressed inside the popup

    result = runner._run_modal_inline(SelectModal("pick", ["a", "b"], viewport_rows=10))

    assert result is None  # treated as cancel...
    assert runner._exit_menu_requested is True  # ...but flags the WHOLE menu to close


def test_run_modal_short_circuits_when_close_already_requested():
    from agitrack.proxy.modal import SelectModal

    runner = make_runner()
    runner._exit_menu_requested = True
    runner._clear_message = lambda: None
    runner._render = lambda: None
    reads: list = []
    runner._popup_read_input = lambda: reads.append(True) or b""

    result = runner._run_modal_inline(SelectModal("pick", ["a"], viewport_rows=10))

    assert result is None
    assert reads == []  # never painted or read input — the close unwinds straight through


@pytest.mark.parametrize("protocol", ["kitty", "modifyOtherKeys"])
def test_ctrl_c_exits_under_both_keyboard_protocols(protocol):
    runner = make_runner()
    parser = ProxyInput(menu_key=b"\x07")
    forwarded, _command, should_exit = _drive_host_input(runner, parser, [_CTRL_C_ENCODINGS[protocol]])
    assert should_exit is True
    assert forwarded == []


@pytest.mark.parametrize("protocol", ["kitty", "modifyOtherKeys"])
def test_menu_key_split_across_reads_under_both_protocols(protocol):
    # The single-byte menu key relies on _hold_incomplete_tail to reassemble an
    # escape sequence split across reads before decoding — verify for both forms.
    runner = make_runner()
    parser = ProxyInput(menu_key=b"\x07")
    seq = _CTRL_G_ENCODINGS[protocol]
    split = len(seq) - 2  # cut mid-sequence, before the final u / ~
    forwarded, command, should_exit = _drive_host_input(runner, parser, [seq[:split], seq[split:] + b"exit\r"])
    assert forwarded == []
    assert command == "exit aGiTrack"  # typing "exit" selects the "exit aGiTrack" entry
    assert should_exit is False


def test_proxy_menu_key_works_with_kitty_encoding():
    """Test that the menu key works even when terminal sends kitty-encoded keys."""
    from agitrack.proxy.runner import _decode_kitty_ctrl_keys

    # Simulate what happens in _reactor_stdin_phase:
    # 1. Terminal sends kitty-encoded Ctrl-O
    # 2. We decode it to plain byte
    # 3. ProxyInput matches it as menu key

    kitty_encoded_ctrl_o = b"\x1b[111;5u"
    decoded = _decode_kitty_ctrl_keys(kitty_encoded_ctrl_o)

    parser = ProxyInput(menu_key=b"\x0f")  # Ctrl-O
    forwarded, local_echo, command, should_exit = parser.feed(decoded + b"git-unstaged\r")

    assert forwarded == []
    assert command == "git-unstaged"
    assert should_exit is False


def test_proxy_shift_modified_menu_key_enters_command_mode():
    # Test multi-byte kitty keyboard protocol sequence for Ctrl+Shift+G
    parser = ProxyInput(menu_key=b"\x1b[103;6u")

    forwarded, local_echo, command, should_exit = parser.feed(b"\x1b[103;6ugit-unstaged\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "git-unstaged"
    assert should_exit is False


def test_proxy_shift_modified_menu_key_partial_match():
    # Test that partial matches are buffered and forwarded if they don't complete
    parser = ProxyInput(menu_key=b"\x1b[103;6u")

    # Send partial sequence followed by other data
    forwarded, local_echo, command, should_exit = parser.feed(b"\x1b[103;7u")

    # Should forward the partial match since it doesn't match the menu key
    assert b"".join(forwarded) == b"\x1b[103;7u"
    assert command is None


def test_proxy_shift_modified_menu_key_split_across_reads():
    # Test that sequences split across multiple feed() calls work correctly
    parser = ProxyInput(menu_key=b"\x1b[103;6u")

    # Send first part
    forwarded1, _, command1, _ = parser.feed(b"\x1b[103")
    assert forwarded1 == []  # Still buffering
    assert command1 is None

    # Send second part
    forwarded2, _, command2, _ = parser.feed(b";6u")
    assert forwarded2 == []  # Still buffering, haven't completed yet

    # Send the command
    forwarded3, _, command3, _ = parser.feed(b"sessions\r")
    assert command3 == "sessions"


def test_proxy_shift_modified_menu_key_non_match_forwards():
    # Test that non-matching sequences are forwarded immediately
    parser = ProxyInput(menu_key=b"\x1b[103;6u")

    # Send a different escape sequence
    forwarded, local_echo, command, should_exit = parser.feed(b"\x1b[112;6u")

    # Should forward the non-matching sequence
    assert b"".join(forwarded) == b"\x1b[112;6u"
    assert command is None


def test_proxy_s_jumps_to_session():
    # Only "sessions" starts with "s", so s+Enter selects it directly.
    parser = ProxyInput()
    _f, _e, command, _x = parser.feed(b"\x07s\r")
    assert command == "sessions"


def test_proxy_forwards_colon_at_line_start():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b":status\r")

    assert b"".join(forwarded) == b":status\r"
    assert local_echo == b""
    assert command is None
    assert should_exit is False


def test_proxy_forwards_colon_inside_prompt():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"fix this: bug\r")

    assert b"".join(forwarded) == b"fix this: bug\r"
    assert local_echo == b""
    assert command is None
    assert should_exit is False


def test_proxy_forwards_slash_commands():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"/help\r")

    assert b"".join(forwarded) == b"/help\r"
    assert local_echo == b""
    assert command is None
    assert should_exit is False


def test_proxy_ctrl_c_cancels_command_capture():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07sta\x03")

    # Inside aGiTrack's palette Ctrl-C cancels it (like Esc): nothing forwarded,
    # no command, no exit — and the parser is back in passthrough mode.
    assert forwarded == []
    assert local_echo == b""
    assert command is None
    assert should_exit is False
    assert parser.capturing is False
    assert parser.text() == ""


def test_proxy_escape_cancels_command_capture():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07sta\x1b")

    assert forwarded == []
    assert local_echo == b""
    assert command is None
    assert should_exit is False
    assert parser.capturing is False


def test_proxy_escape_clears_command_buffer():
    parser = ProxyInput()

    parser.feed(b"\x07sta\x1b")

    assert parser.text() == ""


def test_proxy_tab_completes_command():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07git-un\t\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "git-unstaged"
    assert should_exit is False


def test_proxy_arrow_selection_runs_selected_command():
    parser = ProxyInput()

    # Down-arrow from the first command (session) selects the second.
    forwarded, local_echo, command, should_exit = parser.feed(b"\x07\x1b[B\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "agent-backend"
    assert should_exit is False


def test_proxy_tab_completes_selected_command():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07\x1b[B\t\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "agent-backend"


def test_proxy_enter_runs_selected_partial_match_without_tab():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07git-un\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "git-unstaged"
    assert should_exit is False


def test_proxy_agent_backend_command_name():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07agent-b\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "agent-backend"
    assert should_exit is False


def test_proxy_ignores_sgr_mouse_sequences_in_command_mode():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07\x1b[<35;88;11Mgit-unstaged\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "git-unstaged"
    assert should_exit is False


def test_popup_escape_sequence_consumer_waits_for_mouse_terminator():
    assert _escape_sequence_complete(b"\x1b[<35;88;11") is False
    assert _escape_sequence_complete(b"\x1b[<35;88;11M") is True
    assert _escape_sequence_complete(b"\x1b[<35;88;11m") is True
    assert _escape_sequence_complete(b"\x1b[35;88;11") is False
    assert _escape_sequence_complete(b"\x1b[35;88;11M") is True


def test_proxy_ctrl_c_starts_exit_flow_in_passthrough_mode():
    # A single Ctrl-C opens the exit confirmation popup (via _run_exit_flow);
    # a second press while that popup is open exits gracefully.
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x03")

    assert forwarded == []
    assert local_echo == b""
    assert command is None
    assert should_exit is True


def test_proxy_agent_commit_preserves_incomplete_initial_user_turn(tmp_path):
    runner = make_runner(
        repo=FakeCommitRepo(),
        state=AgitrackState(tmp_path),
        verbose=False,
    )
    runner._review_untracked_popup = lambda include_declined: "No untracked files to review."
    runner.state.append_trace("user", "also handle errors")

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[
            SessionTurn("u1", "a1", "fix it", "", TokenUsage(), None),
            SessionTurn("u2", "a2", "also handle errors", "done", TokenUsage(total=1, output=1), None),
        ],
        backend="opencode",
        backend_session_id="ses-1",
        model="provider/model",
        quiet=True,
    )

    assert committed is True
    message = runner.repo.message
    # The subject lists every prompt that led to the commit, joined by " / ".
    assert message.startswith("<aGiTrack> fix it / also handle errors")
    assert message.index("## User\n\nfix it") < message.index("## User\n\nalso handle errors")
    assert message.index("## User\n\nalso handle errors") < message.index("## Agent\n\ndone")


def test_full_agent_messages_flag_records_all_messages(tmp_path):
    # The runner's per-run override (set by --full-agent-messages) makes a commit
    # include every agent message, overriding the default-off per-repo config.
    runner = make_runner(repo=FakeCommitRepo(), state=AgitrackState(tmp_path), verbose=False)
    runner._review_untracked_popup = lambda include_declined: "No untracked files to review."
    runner._full_agent_messages = True
    turn = SessionTurn("u1", "a1", "do it", "Done.", TokenUsage(total=1, output=1), None)
    turn.agent_messages = ["On it.", "Done."]

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[turn],
        backend="opencode",
        backend_session_id="ses-1",
        model="provider/model",
        quiet=True,
    )

    assert committed is True
    assert "On it." in runner.repo.message
    assert runner.repo.message.count("## Agent") == 2


def test_delay_merge_defers_integration_and_names_working_dir(tmp_path):
    import types

    runner = make_runner()
    runner._delay_merge = True
    runner._exiting = False
    runner.worktree = types.SimpleNamespace(name="s", path=tmp_path)
    runner.repo = types.SimpleNamespace(repo=tmp_path)
    runner._base_branch = "main"
    runner._active_has_pending = lambda: True
    runner._menu_label = lambda: "Ctrl-G"
    integrated: list = []
    runner._integrate_turn_or_conflict = lambda: integrated.append(True) or "integrated"
    msgs: list[str] = []
    runner._set_message = lambda m, **k: msgs.append(m)
    runner._render = lambda *a, **k: None

    runner._integrate_session_turn()

    assert integrated == []  # deferred, not merged
    # The notice names the working directory (the worktree) so the user can find it.
    assert any(str(tmp_path) in m and "not merged" in m for m in msgs)


def test_delay_merge_off_integrates_immediately(tmp_path):
    import types

    runner = make_runner()
    runner._delay_merge = False
    runner._exiting = False
    runner.worktree = types.SimpleNamespace(name="s", path=tmp_path)
    runner.repo = types.SimpleNamespace(repo=tmp_path)
    runner._base_branch = "main"
    integrated: list = []
    runner._integrate_turn_or_conflict = lambda: integrated.append(True) or "integrated"

    runner._integrate_session_turn()

    assert integrated == [True]  # merged right away (default behavior unchanged)


def _delay_menu_runner(tmp_path):
    import types

    runner = make_runner()
    runner._delay_merge = True
    runner.merge_ctx = None
    runner.worktree = types.SimpleNamespace(name="s", path=tmp_path)
    runner.repo = types.SimpleNamespace(repo=tmp_path, merge_in_progress=lambda: False)
    runner._base_branch = "main"
    runner._active_has_pending = lambda: True
    runner.sessions = []
    runner._my_shared_session_ids = lambda: set()
    runner._dormant_worktrees = lambda names: []
    runner._resumable_sessions = lambda: []
    runner.backend = types.SimpleNamespace(supports_session_sharing=False, name="claude")
    runner._use_worktrees = True
    runner._set_message = lambda *a, **k: None
    runner._render = lambda *a, **k: None
    return runner


# --- Ctrl-G "merge": rescue un-integrated worktrees into a chosen branch -----------


def test_proxy_input_matches_puts_extra_commands_first():
    from agitrack.proxy.runner import ProxyInput

    inp = ProxyInput()
    inp.extra_commands = ["merge"]
    assert inp.matches()[0] == "merge"  # surfaced at the very top
    assert "sessions" in inp.matches()
    inp.extra_commands = []
    assert inp.matches()[0] == "sessions"  # default order unchanged when nothing extra


def _merge_runner(tmp_path):
    import types

    runner = make_runner()
    runner.merge_ctx = None
    runner.agent_in_flight = False
    runner.worktree = types.SimpleNamespace(name="s", path=tmp_path)
    runner.repo = types.SimpleNamespace(
        repo=tmp_path,
        has_tracked_changes=lambda: False,
        untracked_files=lambda: [],
        current_branch=lambda: "agit/x",
    )
    runner.state = types.SimpleNamespace(declined_untracked=lambda: [])
    runner.name = "feature"
    runner._base_branch = "session-base"
    runner._repo_dir_branch = "main"
    runner._active_has_pending = lambda: True
    runner.sessions = []
    runner._dormant_worktrees = lambda live: []
    runner._set_message = lambda *a, **k: None
    runner._render = lambda *a, **k: None
    return runner


def test_has_unmerged_work_true_when_active_has_pending(tmp_path):
    runner = _merge_runner(tmp_path)
    assert runner._unmerged_worktrees() == [("feature (current session)", "")]
    assert runner._has_unmerged_work() is True


def test_has_unmerged_work_false_when_nothing_pending(tmp_path):
    runner = _merge_runner(tmp_path)
    runner._active_has_pending = lambda: False
    assert runner._has_unmerged_work() is False


def test_unmerged_includes_active_with_only_uncommitted_changes(tmp_path):
    # No committed-but-unmerged commits, but the worktree has uncommitted edits — still
    # offered (and committed before merging), so the work isn't stranded.
    runner = _merge_runner(tmp_path)
    runner._active_has_pending = lambda: False
    runner.repo.has_tracked_changes = lambda: True
    assert runner._unmerged_worktrees() == [("feature (current session)", "")]


def test_unmerged_excludes_active_while_agent_running(tmp_path):
    # Mid-turn uncommitted changes are expected and auto-committed, so don't nag then.
    runner = _merge_runner(tmp_path)
    runner.agent_in_flight = True
    runner.repo.has_tracked_changes = lambda: True
    assert runner._unmerged_worktrees() == []


def test_committable_changes_ignores_declined_untracked(tmp_path):
    runner = _merge_runner(tmp_path)
    runner.repo.has_tracked_changes = lambda: False
    runner.repo.untracked_files = lambda: ["a.py"]
    runner.state.declined_untracked = lambda: ["a.py"]  # intentionally unstaged -> not committable
    assert runner._active_has_committable_changes() is False
    runner.state.declined_untracked = lambda: []
    assert runner._active_has_committable_changes() is True


def test_merge_active_retargets_then_delegates_to_integrate(tmp_path):
    runner = _merge_runner(tmp_path)
    runner.repo.has_tracked_changes = lambda: True  # uncommitted work present
    integrated: list = []
    runner._integrate_active_session = lambda: integrated.append(runner._base_branch)

    runner._merge_active_into("release")

    # _merge_active_into retargets the base then hands off; the commit-before-merge now
    # lives in _integrate_active_session (one place), exercised by the test below.
    assert integrated == ["release"]
    assert runner._base_branch == "release"


def test_integrate_active_commits_committable_changes_before_integrating(tmp_path):
    # Best effort: with no turn in flight, uncommitted committable work is committed first
    # rather than dead-ending the user with "finish or stop the current turn".
    runner = _merge_runner(tmp_path)
    runner.repo.has_changes = lambda: True  # worktree dirty, but...
    runner.repo.has_tracked_changes = lambda: True  # ...with committable work, and...
    runner.agent_in_flight = False  # ...no turn is actually running
    runner.agent_parse_thread = None
    order: list = []
    runner._commit_latest_turn_sync = lambda: order.append("commit")
    runner._integrate_turn_or_conflict = lambda: order.append("integrate") or "integrated"

    runner._integrate_active_session()

    assert order == ["commit", "integrate"]  # committed first, then integrated — no dead-end


def test_integrate_active_blocks_only_while_a_turn_is_running(tmp_path):
    # A genuinely in-flight turn still blocks the merge (its edits are mid-flight).
    runner = _merge_runner(tmp_path)
    runner.repo.has_changes = lambda: True
    runner.agent_in_flight = True
    runner.agent_parse_thread = None
    runner.last_child_output = time.monotonic()  # output just now: the turn is genuinely live
    runner.CHILD_IDLE_SECONDS = 999.0  # so the in-flight flag is NOT cleared as idle
    messages: list = []
    runner._set_message = lambda msg, **k: messages.append(msg)
    committed: list = []
    runner._commit_latest_turn_sync = lambda: committed.append(True)
    runner._integrate_turn_or_conflict = lambda: committed.append("integrate") or "integrated"

    runner._integrate_active_session()

    assert committed == []  # neither committed nor integrated
    assert any("still running" in m for m in messages)


def test_active_has_mergeable_work_excludes_in_flight_turn(tmp_path):
    runner = _merge_runner(tmp_path)  # _active_has_pending True
    assert runner._active_has_mergeable_work() is True
    runner.agent_in_flight = True
    assert runner._active_has_mergeable_work() is False  # mid-turn: not "stranded"


def test_select_current_session_integrates_when_there_is_work(tmp_path):
    # Picking the session you're already in, with work to merge, runs the integrate
    # (commit/merge messages pop up) rather than the confusing "nothing to integrate".
    runner = _merge_runner(tmp_path)  # _active_has_pending True
    merged: list = []
    runner._merge_active_into = lambda target: merged.append(target)

    runner._select_current_session()

    assert merged == ["session-base"]  # integrates into the session's own merge branch


def test_select_current_session_acknowledges_when_nothing_to_merge(tmp_path):
    runner = _merge_runner(tmp_path)
    runner._active_has_pending = lambda: False
    runner.repo.has_tracked_changes = lambda: False
    runner.repo.untracked_files = lambda: []
    messages: list = []
    runner._set_message = lambda message, **kwargs: messages.append(message)

    runner._select_current_session()

    assert messages and "Already in session 'feature'" in messages[0]
    assert "nothing to integrate" not in messages[0]


def test_merge_command_merges_single_item_into_chosen_target(tmp_path):
    runner = _merge_runner(tmp_path)
    runner._choose_merge_target = lambda name: "release"
    merged: list = []
    runner._merge_active_into = lambda target: merged.append(target)

    runner._handle_merge_command()

    assert merged == ["release"]  # one unmerged worktree -> straight to target choice + merge


def test_choose_merge_target_offers_current_session_and_custom(tmp_path):
    runner = _merge_runner(tmp_path)
    seen: list = []
    runner._select_popup = lambda title, options: seen.append(options) or None  # cancel after building

    assert runner._choose_merge_target("") is None  # cancelled
    options = seen[0]
    assert any("Current branch (main)" in opt for opt in options)
    assert any("Session's branch (session-base)" in opt for opt in options)
    assert any("different branch" in opt for opt in options)


def test_choose_merge_target_custom_prompts_for_any_branch(tmp_path):
    runner = _merge_runner(tmp_path)
    runner._select_popup = lambda title, options: next(o for o in options if "different branch" in o)
    runner._prompt_merge_branch = lambda title, current: "hotfix"

    assert runner._choose_merge_target("") == "hotfix"


def test_merge_active_into_retargets_base_then_integrates(tmp_path):
    runner = _merge_runner(tmp_path)
    integrated: list = []
    runner._integrate_active_session = lambda: integrated.append(runner._base_branch)

    runner._merge_active_into("release")

    assert runner._base_branch == "release"  # re-pointed to the chosen destination
    assert integrated == ["release"]  # then integrated into it (handles conflict via that path)


def test_delay_merge_menu_offers_merge_entry(tmp_path):
    runner = _delay_menu_runner(tmp_path)
    seen: list = []
    runner._select_popup = lambda title, options: seen.append(options) or None  # cancel after building

    runner._session_menu()

    assert any("Merge reviewed changes into main" in opt for opt in seen[0])


def test_delay_merge_menu_choice_integrates(tmp_path):
    runner = _delay_menu_runner(tmp_path)
    runner._select_popup = lambda title, options: next(o for o in options if "Merge reviewed changes" in o)
    called: list = []
    runner._integrate_active_session = lambda: called.append(True)

    runner._session_menu()

    assert called == [True]


def test_session_menu_offers_explicit_integrate_when_active_has_pending(tmp_path):
    # Even outside --delay-merge, a session resumed with un-integrated commits gets an
    # explicit, discoverable "Integrate this session's commits" entry (not just the
    # non-obvious "re-select the current session" path).
    runner = _delay_menu_runner(tmp_path)
    runner._delay_merge = False
    seen: list = []
    runner._select_popup = lambda title, options: seen.append(options) or None

    runner._session_menu()

    assert any("Integrate this session's commits into main" in opt for opt in seen[0])


def test_session_menu_explicit_integrate_choice_integrates(tmp_path):
    runner = _delay_menu_runner(tmp_path)
    runner._delay_merge = False
    runner._select_popup = lambda title, options: next(o for o in options if "Integrate this session's commits" in o)
    called: list = []
    runner._integrate_active_session = lambda: called.append(True)

    runner._session_menu()

    assert called == [True]


def test_session_menu_esc_signals_up_and_reopens_palette(tmp_path):
    # Esc on the sessions list goes UP one level — to the Ctrl-G command palette — not all
    # the way to the agent. _session_menu returns _MENU_UP; _run_command re-opens the palette.
    runner = _delay_menu_runner(tmp_path)
    runner._select_popup = lambda title, options: None  # Esc on the list

    assert runner._session_menu() == runner._MENU_UP

    # Going through the command dispatch, an UP signal re-opens the input-layer palette.
    runner.input.capturing = False
    runner._render = lambda *a, **k: None
    runner._has_unmerged_work = lambda: False
    runner._after_menu_command(runner._MENU_UP)
    assert runner.input.capturing is True  # back at the palette, one level up


def test_session_menu_transition_signals_done(tmp_path):
    # Starting a new session is a context transition: _prompt_new_session reports _MENU_DONE
    # and the sessions menu unwinds straight to the agent (the palette is NOT re-opened).
    runner = _delay_menu_runner(tmp_path)
    runner._prompt_new_session = lambda: runner._MENU_DONE
    runner._select_popup = lambda title, options: next(o for o in options if o.startswith("+ New session"))

    assert runner._session_menu() == runner._MENU_DONE


def test_session_menu_new_session_cancel_returns_to_list(tmp_path):
    # Backing out of the new-session prompts (_prompt_new_session → _MENU_UP) must NOT drop
    # to the agent: the sessions list re-shows so Esc lands one level up here.
    runner = _delay_menu_runner(tmp_path)
    runner._prompt_new_session = lambda: runner._MENU_UP
    shown: list[bool] = []

    def select(title, options):
        shown.append(True)
        if len(shown) == 1:
            return next(o for o in options if o.startswith("+ New session"))
        return None  # second visit → Esc closes the menu

    runner._select_popup = select
    assert runner._session_menu() == runner._MENU_UP
    assert len(shown) == 2  # list re-shown after the cancelled new-session prompt


def test_session_menu_back_out_shows_no_message(tmp_path):
    # Backing out of a menu is silent — no "closed"/"cancelled" flash before the parent
    # (here, the palette) re-appears.
    runner = _delay_menu_runner(tmp_path)
    msgs: list = []
    runner._set_message = lambda *a, **k: msgs.append(a[0] if a else k.get("message"))
    runner._select_popup = lambda title, options: None  # Esc immediately

    assert runner._session_menu() == runner._MENU_UP
    assert msgs == []  # nothing announced on the way up


def _copy_runner(tmp_path, status):
    import types

    base = tmp_path / "base"
    wt = tmp_path / "wt"
    base.mkdir()
    wt.mkdir()
    runner = make_runner()
    runner.base_repo = types.SimpleNamespace(repo=base)
    # The copy offer reads ignored entries too (git status --short --ignored).
    runner.repo = types.SimpleNamespace(repo=wt, status_short=lambda: status, status_short_ignored=lambda: status)
    runner.worktree = types.SimpleNamespace(name="s", path=wt)
    runner._offer_user_commit_for_worktree_edits = lambda: None  # tested separately
    msgs: list[str] = []
    runner._set_message = lambda m, **k: msgs.append(m)
    runner._render = lambda *a, **k: None
    return runner, base, wt, msgs


def test_offer_copy_unstaged_copies_on_consent(tmp_path):
    runner, base, wt, _ = _copy_runner(tmp_path, "?? new.txt\n")
    (wt / "new.txt").write_text("hello\n")
    runner._select_popup = lambda *a, **k: "Yes, copy to the base repo"

    runner._offer_copy_unstaged_to_base()

    assert (base / "new.txt").read_text() == "hello\n"


def test_copy_announces_before_confirming(tmp_path):
    # The user is told the files are being copied BEFORE the "Copied …" confirmation, so a
    # slow copy (a large ignored dir) doesn't look like nothing is happening.
    runner, base, wt, msgs = _copy_runner(tmp_path, "?? new.txt\n")
    (wt / "new.txt").write_text("hello\n")
    runner._select_popup = lambda *a, **k: "Yes, copy to the base repo"

    runner._offer_copy_unstaged_to_base()

    copying = next((i for i, m in enumerate(msgs) if m.startswith("Copying 1 file(s)")), None)
    copied = next((i for i, m in enumerate(msgs) if m.startswith("Copied 1 file(s)")), None)
    assert copying is not None and copied is not None
    assert copying < copied  # "Copying …" precedes the "Copied …" confirmation


def test_copy_offer_offers_user_commit_for_edits_on_switch(tmp_path):
    # On a switch/exit offer (main thread), when the worktree has the user's own uncommitted
    # edits AND copy-able leftovers, BOTH prompts show: a commit prompt for the edits, then
    # the copy prompt. (The per-turn offer defers the commit prompt — see the test below.)
    import types

    runner, base, wt, _ = _copy_runner(tmp_path, "?? leftover.txt\n")
    (wt / "leftover.txt").write_text("x\n")
    runner.actions = types.SimpleNamespace(has_pre_agent_user_changes=lambda: True)
    events: list[str] = []
    runner._create_user_commit_popup = lambda *a, **k: events.append("user-commit")
    runner._select_popup = lambda *a, **k: events.append("copy") or "No, leave them in the worktree"
    del runner._offer_user_commit_for_worktree_edits  # use the real method

    runner._offer_copy_unstaged_to_base(context="switch")

    assert events == ["user-commit", "copy"]  # both shown, commit prompt first


def test_turn_copy_offer_defers_user_commit_prompt(tmp_path):
    # The per-turn offer's worktree read runs on the git worker, which must never raise the
    # (blocking) user-commit popup or commit from there; that prompt is left for switch/exit.
    import types

    runner, base, wt, _ = _copy_runner(tmp_path, "?? leftover.txt\n")
    (wt / "leftover.txt").write_text("x\n")
    runner.actions = types.SimpleNamespace(has_pre_agent_user_changes=lambda: True)
    events: list[str] = []
    runner._create_user_commit_popup = lambda *a, **k: events.append("user-commit")
    runner._select_popup = lambda *a, **k: events.append("copy") or "No, leave them in the worktree"
    del runner._offer_user_commit_for_worktree_edits

    runner._offer_copy_unstaged_to_base()  # context="turn"

    assert events == ["copy"]  # no user-commit prompt mid-turn (deferred to switch/exit)


def test_request_copy_offer_stashes_for_main_without_presenting(tmp_path):
    # The git-worker side collects candidates and stashes them for the main thread, but must
    # NOT present any popup itself (that would block the worker, stalling commit/merge). The
    # main thread then presents the stash via _present_pending_copy_offer.
    runner, base, wt, _ = _copy_runner(tmp_path, "?? leftover.txt\n")
    (wt / "leftover.txt").write_text("x\n")
    presented: list = []
    runner._present_copy_offer = lambda collected, *, context: presented.append((collected, context))

    runner._request_copy_offer()  # worker side: collect + stash, no popup
    assert presented == []  # nothing presented on the worker
    assert runner._pending_copy_offer is not None  # stashed for the main thread
    assert runner._pending_copy_offer[0] == "turn"

    runner._present_pending_copy_offer()  # main side: present the stash
    assert len(presented) == 1 and presented[0][1] == "turn"
    assert runner._pending_copy_offer is None  # consumed


def test_copy_offer_skips_user_commit_when_no_edits(tmp_path):
    # No committable user edits → no commit prompt, just the copy offer.
    import types

    runner, base, wt, _ = _copy_runner(tmp_path, "?? leftover.txt\n")
    (wt / "leftover.txt").write_text("x\n")
    runner.actions = types.SimpleNamespace(has_pre_agent_user_changes=lambda: False)
    events: list[str] = []
    runner._create_user_commit_popup = lambda *a, **k: events.append("user-commit")
    runner._select_popup = lambda *a, **k: events.append("copy") or "No, leave them in the worktree"
    del runner._offer_user_commit_for_worktree_edits

    runner._offer_copy_unstaged_to_base()

    assert events == ["copy"]  # only the copy offer


def test_offer_copy_decline_mutes_same_set_reasks_on_new_file(tmp_path):
    # Declining mutes the SET of files: it isn't re-asked even as the same files keep
    # changing — but a genuinely NEW file re-opens the whole set (ask about all again).
    runner, base, wt, _ = _copy_runner(tmp_path, "?? a.txt\n?? b.txt\n")
    (wt / "a.txt").write_text("1\n")
    (wt / "b.txt").write_text("1\n")
    titles: list[str] = []
    details: list = []

    def decline(title, options, **k):
        titles.append(title)
        details.append(k.get("detail"))
        return "No, leave them in the worktree"

    runner._select_popup = decline
    runner._offer_copy_unstaged_to_base()
    assert len(titles) == 1  # asked once
    assert sorted(details[0]) == ["a.txt", "b.txt"]  # file list passed as scrollable detail

    # The same set keeps changing content → NOT re-asked.
    (wt / "a.txt").write_text("2\n")
    (wt / "b.txt").write_text("2\n")
    runner._offer_copy_unstaged_to_base()
    assert len(titles) == 1  # still muted

    # A new file appears → re-ask about ALL of them.
    runner.repo.status_short_ignored = lambda: "?? a.txt\n?? b.txt\n?? c.txt\n"
    (wt / "c.txt").write_text("1\n")
    runner._offer_copy_unstaged_to_base()
    assert len(titles) == 2
    assert sorted(details[1]) == ["a.txt", "b.txt", "c.txt"]


def test_offer_copy_turn_popup_explains_the_decline_mute(tmp_path):
    # The offer popup tells the user that declining is sticky until the fileset changes
    # or they switch sessions, so they know why they won't be re-asked.
    runner, base, wt, _ = _copy_runner(tmp_path, "?? a.txt\n")
    (wt / "a.txt").write_text("x\n")
    seen: list[str] = []
    runner._select_popup = lambda title, options, **k: seen.append(title) or "No, leave them in the worktree"

    runner._offer_copy_unstaged_to_base()

    assert "switch sessions" in seen[0] and "change as a set" in seen[0]


def test_offer_copy_on_exit_respects_mute_unless_new_file(tmp_path):
    # On exit, a set the user already declined (and no new file) is NOT re-prompted —
    # the mute is respected. A genuinely new file re-opens the whole set.
    runner, base, wt, _ = _copy_runner(tmp_path, "?? a.txt\n")
    (wt / "a.txt").write_text("x\n")
    runner._copy_declined = {"a.txt"}  # already muted by a prior in-session decline
    seen: list[str] = []
    runner._select_popup = lambda title, options, **k: seen.append(title) or "Yes, copy to the base repo"

    runner._offer_copy_unstaged_to_base(context="exit")
    assert seen == []  # same set, already declined → not asked again, even on exit

    # A new file appears → re-offer all of them on exit, with the deletion warning.
    runner.repo.status_short_ignored = lambda: "?? a.txt\n?? b.txt\n"
    (wt / "b.txt").write_text("y\n")
    runner._offer_copy_unstaged_to_base(context="exit")
    assert len(seen) == 1 and "DELETED" in seen[0]
    assert (base / "a.txt").read_text() == "x\n" and (base / "b.txt").read_text() == "y\n"


def test_offer_copy_on_exit_esc_aborts_exit(tmp_path):
    # Esc on the exit copy offer aborts the exit (the caller checks _exit_aborted) and
    # discards nothing; the just-recorded fingerprints are forgotten so the next exit re-asks.
    runner, base, wt, _ = _copy_runner(tmp_path, "?? a.txt\n")
    (wt / "a.txt").write_text("x\n")
    runner._exit_aborted = False
    runner._select_popup = lambda title, options, **k: None  # Esc

    runner._offer_copy_unstaged_to_base(context="exit")

    assert runner._exit_aborted is True
    assert not (base / "a.txt").exists()  # nothing copied/discarded
    assert "a.txt" not in runner._copy_prompted  # re-offered on the next exit


def test_offer_copy_unstaged_declined_leaves_files_and_notifies(tmp_path):
    runner, base, wt, msgs = _copy_runner(tmp_path, "?? keep.txt\n")
    (wt / "keep.txt").write_text("x\n")
    runner._select_popup = lambda *a, **k: "No, leave them in the worktree"

    runner._offer_copy_unstaged_to_base()

    assert not (base / "keep.txt").exists()
    assert any("left in this session's worktree" in m and str(wt) in m for m in msgs)

    # An unchanged file is not prompted again.
    prompted: list = []
    runner._select_popup = lambda *a, **k: prompted.append(a) or None
    runner._offer_copy_unstaged_to_base()
    assert prompted == []


def test_offer_copy_unstaged_overwrite_declined_keeps_base(tmp_path):
    runner, base, wt, msgs = _copy_runner(tmp_path, "?? dup.txt\n")
    (wt / "dup.txt").write_text("new\n")
    (base / "dup.txt").write_text("old\n")
    answers = iter(["Yes, copy to the base repo", "No, keep the base versions"])
    runner._select_popup = lambda *a, **k: next(answers)

    runner._offer_copy_unstaged_to_base()

    assert (base / "dup.txt").read_text() == "old\n"  # not overwritten
    assert any("left in this session's worktree" in m for m in msgs)


def test_offer_copy_unstaged_overwrite_confirmed(tmp_path):
    runner, base, wt, _ = _copy_runner(tmp_path, "?? dup.txt\n")
    (wt / "dup.txt").write_text("new\n")
    (base / "dup.txt").write_text("old\n")
    answers = iter(["Yes, copy to the base repo", "Yes, overwrite all"])
    runner._select_popup = lambda *a, **k: next(answers)

    runner._offer_copy_unstaged_to_base()

    assert (base / "dup.txt").read_text() == "new\n"  # overwritten


def test_offer_copy_unstaged_overwrite_all_prompts_once(tmp_path):
    # "Yes, overwrite all" is a SINGLE confirmation covering every conflict, not one
    # prompt per file.
    runner, base, wt, _ = _copy_runner(tmp_path, "?? a.txt\n?? b.txt\n?? c.txt\n")
    for name in ("a.txt", "b.txt", "c.txt"):
        (wt / name).write_text("new\n")
    (base / "a.txt").write_text("old\n")  # only a.txt and b.txt pre-exist in the base
    (base / "b.txt").write_text("old\n")
    titles: list[str] = []

    def popup(title, options, **k):
        titles.append(title)
        return "Yes, copy to the base repo" if "Copy them" in title else "Yes, overwrite all"

    runner._select_popup = popup
    runner._offer_copy_unstaged_to_base()

    # Exactly two popups: the copy offer + ONE overwrite confirmation for both conflicts.
    assert len(titles) == 2
    assert sum("verwrite" in t for t in titles) == 1
    assert (base / "a.txt").read_text() == "new\n"
    assert (base / "b.txt").read_text() == "new\n"
    assert (base / "c.txt").read_text() == "new\n"  # the new (non-conflicting) file too


def test_offer_copy_unstaged_overwrite_confirm_each_one(tmp_path):
    # "Let me confirm each one" then prompts per conflicting file: overwrite a.txt, keep
    # b.txt. The non-conflicting c.txt copies without a per-file prompt.
    runner, base, wt, _ = _copy_runner(tmp_path, "?? a.txt\n?? b.txt\n?? c.txt\n")
    for name in ("a.txt", "b.txt", "c.txt"):
        (wt / name).write_text("new\n")
    (base / "a.txt").write_text("old\n")
    (base / "b.txt").write_text("old\n")

    def popup(title, options, **k):
        if "Copy them" in title:
            return "Yes, copy to the base repo"
        if title.startswith(f"{2} of these"):  # the up-front overwrite question
            return "Let me confirm each one"
        if title.startswith("'a.txt'"):
            return "Yes, overwrite"
        return "No, keep the base version"  # for 'b.txt'

    runner._select_popup = popup
    runner._offer_copy_unstaged_to_base()

    assert (base / "a.txt").read_text() == "new\n"  # confirmed overwrite
    assert (base / "b.txt").read_text() == "old\n"  # declined per-file
    assert (base / "c.txt").read_text() == "new\n"  # non-conflicting, copied


def test_offer_copy_unstaged_noop_without_worktree(tmp_path):
    runner, base, wt, _ = _copy_runner(tmp_path, "?? x.txt\n")
    runner.worktree = None  # no-worktree mode: nothing to copy
    prompted: list = []
    runner._select_popup = lambda *a, **k: prompted.append(a) or None
    runner._offer_copy_unstaged_to_base()
    assert prompted == []


def test_offer_copy_includes_git_ignored_files(tmp_path):
    # A turn that only created git-ignored files makes no commit, but those files
    # (here `data.bin`, reported with the `!!` ignored status) should still be
    # offered for copying into the base directory.
    runner, base, wt, _ = _copy_runner(tmp_path, "!! data.bin\n")
    (wt / "data.bin").write_text("payload\n")
    runner._select_popup = lambda *a, **k: "Yes, copy to the base repo"

    runner._offer_copy_unstaged_to_base()

    assert (base / "data.bin").read_text() == "payload\n"


def test_offer_copy_skips_underscore_and_dot_files(tmp_path):
    # Files whose name starts with `_` or `.` are generated/hidden scaffolding and
    # are never offered — so a turn touching only such files prompts nothing.
    status = "?? __pycache__/\n!! .env\n?? _scratch.tmp\n"
    runner, base, wt, _ = _copy_runner(tmp_path, status)
    prompted: list = []
    runner._select_popup = lambda *a, **k: prompted.append(a) or None

    runner._offer_copy_unstaged_to_base()

    assert prompted == []
    assert runner._uncommitted_worktree_files() == []


def test_offer_copy_mixes_real_files_and_skips_hidden(tmp_path):
    # When real files and hidden/scaffolding files are both present, only the real
    # ones are offered; `.cache` and `_tmp` are filtered out.
    status = "?? keep.txt\n?? .cache\n!! _tmp\n"
    runner, base, wt, _ = _copy_runner(tmp_path, status)
    (wt / "keep.txt").write_text("real\n")
    runner._select_popup = lambda *a, **k: "Yes, copy to the base repo"

    assert runner._uncommitted_worktree_files() == ["keep.txt"]
    runner._offer_copy_unstaged_to_base()
    assert (base / "keep.txt").read_text() == "real\n"


def test_offer_copy_decline_notice_warns_worktree_is_removed(tmp_path):
    # Declining keeps the files in the worktree, but the notice must say where they
    # are (the worktree path) and that the worktree is removed on aGiTrack exit.
    runner, base, wt, msgs = _copy_runner(tmp_path, "?? keep.txt\n")
    (wt / "keep.txt").write_text("x\n")
    runner._select_popup = lambda *a, **k: "No, leave them in the worktree"

    runner._offer_copy_unstaged_to_base()

    assert msgs
    notice = msgs[-1]
    assert str(wt) in notice  # the worktree path is spelled out
    assert "removed when aGiTrack exits" in notice
    # The removal warning is marked bold (**…**), which the popup renderer renders as
    # SGR bold; the marked run covers the removal words.
    assert "**the worktree is removed when aGiTrack exits or the session integrates,**" in notice


def test_maybe_offer_copy_when_idle_is_gated_on_idleness(tmp_path):
    # The idle wrapper used by the turn loop must not prompt while the agent is
    # still active, but should once it's idle.
    runner, base, wt, _ = _copy_runner(tmp_path, "!! data.bin\n")
    (wt / "data.bin").write_text("payload\n")
    runner.merge_ctx = None
    runner.last_child_output = 0.0  # long ago → past the idle threshold
    calls: list = []
    # The idle wrapper hands the offer to the main thread via _request_copy_offer (so the git
    # worker never blocks on the popup); it must not even collect while the agent is active.
    runner._request_copy_offer = lambda: calls.append(True)

    runner._agent_is_active = lambda: True
    runner._maybe_offer_copy_when_idle(1e9)
    assert calls == []  # agent active → no offer

    runner._agent_is_active = lambda: False
    runner._maybe_offer_copy_when_idle(1e9)
    assert calls == [True]  # idle → offer fires


def test_stage_backend_resume_retargets_cwd_to_launch_dir(tmp_path):
    # After staging a resume, the transcript's cwd is realigned to the launch dir
    # (self.repo.repo), so Claude --resume can't restore an old worktree directory.
    import types

    runner = make_runner()
    runner.repo = types.SimpleNamespace(repo=tmp_path)
    calls = {}
    runner.backend = types.SimpleNamespace(
        ensure_resumable=lambda repo, sid: True,
        retarget_working_dir=lambda repo, sid, cwd: calls.update(repo=repo, sid=sid, cwd=cwd) or True,
    )
    runner._stage_backend_resume("sid-1")
    assert calls == {"repo": tmp_path, "sid": "sid-1", "cwd": str(tmp_path)}


class _CancelRepo:
    # Minimal repo for _handle_cancelled_turn: reports leftover changes and records
    # whether they were discarded.
    def __init__(self, *, changes=True):
        self._changes = changes
        self.discarded = False

    def has_changes(self):
        return self._changes

    def discard_all_changes(self):
        self.discarded = True
        self._changes = False


def _cancel_runner(tmp_path, *, changes=True):
    runner = make_runner(repo=_CancelRepo(changes=changes), state=AgitrackState(tmp_path), verbose=False)
    runner._set_message = lambda *a, **k: None
    runner._render = lambda *a, **k: None
    return runner


def _cancelled_turn():
    return SessionTurn("u1", "a1", "build it", "", TokenUsage(), None, interrupted=True)


def test_handle_cancelled_turn_keep_leaves_changes(tmp_path):
    runner = _cancel_runner(tmp_path)
    runner._select_popup = lambda *a, **k: "Keep them (commit with your next turn)"
    handled = runner._handle_cancelled_turn([_cancelled_turn()])
    assert handled is False
    assert runner.repo.discarded is False
    # The turn was offered, so a second pass won't re-prompt.
    assert "a1" in runner._cancel_prompted


def test_handle_cancelled_turn_commit_commits_changes(tmp_path):
    runner = _cancel_runner(tmp_path)
    runner._select_popup = lambda *a, **k: "Commit the changes now"
    calls = []
    runner._create_agent_commit_from_turns_popup = lambda **k: (calls.append(k), True)[1]
    handled = runner._handle_cancelled_turn([_cancelled_turn()])
    assert handled is True
    assert len(calls) == 1


def test_handle_cancelled_turn_discard_after_confirm(tmp_path):
    runner = _cancel_runner(tmp_path)
    answers = iter(["Discard the changes", "Yes, discard"])
    runner._select_popup = lambda *a, **k: next(answers)
    handled = runner._handle_cancelled_turn([_cancelled_turn()])
    assert handled is True
    assert runner.repo.discarded is True


def test_handle_cancelled_turn_discard_declined_keeps_changes(tmp_path):
    runner = _cancel_runner(tmp_path)
    answers = iter(["Discard the changes", "No, keep them"])
    runner._select_popup = lambda *a, **k: next(answers)
    handled = runner._handle_cancelled_turn([_cancelled_turn()])
    assert handled is False
    assert runner.repo.discarded is False


def test_handle_cancelled_turn_no_changes_does_not_prompt(tmp_path):
    runner = _cancel_runner(tmp_path, changes=False)
    prompted = []
    runner._select_popup = lambda *a, **k: prompted.append(a) or None
    handled = runner._handle_cancelled_turn([_cancelled_turn()])
    assert handled is False
    assert prompted == []  # nothing to act on → no popup
    assert runner._cancel_prompted == set()


def test_handle_cancelled_turn_skips_already_prompted(tmp_path):
    runner = _cancel_runner(tmp_path)
    runner._cancel_prompted.add("a1")
    prompted = []
    runner._select_popup = lambda *a, **k: prompted.append(a) or None
    handled = runner._handle_cancelled_turn([_cancelled_turn()])
    assert handled is False
    assert prompted == []


def test_proxy_agent_commit_does_not_repeat_whitespace_variant_prompt(tmp_path):
    # The prompt recorded at submit keeps the user's raw typing (trailing
    # newline etc.) while the transcript normalizes it; the old exact-string
    # match failed and re-appended the prompt at the end of the trace (#8).
    runner = make_runner(
        repo=FakeCommitRepo(),
        state=AgitrackState(tmp_path),
        verbose=False,
    )
    runner._review_untracked_popup = lambda include_declined: "No untracked files to review."
    runner.state.append_trace("user", "fix the bug \n")

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[SessionTurn("u1", "a1", "fix the bug", "done", TokenUsage(total=1, output=1), None)],
        backend="claude",
        backend_session_id="ses-1",
        model="m",
        quiet=True,
    )

    assert committed is True
    message = runner.repo.message
    assert message.splitlines()[0] == "<aGiTrack> fix the bug"
    assert message.count("## User") == 1  # not repeated before the metadata


def test_proxy_agent_commit_collapses_double_recorded_prompt(tmp_path):
    # A prompt recorded twice in the pending trace (two submit paths firing for
    # one prompt) must appear once, not once per recording (#8).
    runner = make_runner(
        repo=FakeCommitRepo(),
        state=AgitrackState(tmp_path),
        verbose=False,
    )
    runner._review_untracked_popup = lambda include_declined: "No untracked files to review."
    runner.state.append_trace("user", "do the thing")
    runner.state.append_trace("user", "do the thing ")

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[SessionTurn("u1", "a1", "", "done", TokenUsage(total=1, output=1), None)],
        backend="claude",
        backend_session_id="ses-1",
        model="m",
        quiet=True,
    )

    assert committed is True
    message = runner.repo.message
    assert message.splitlines()[0] == "<aGiTrack> do the thing"
    assert message.count("## User") == 1


def test_proxy_agent_commit_drops_edit_garbled_duplicate_prompt(tmp_path):
    # Real-world case (commit 62856c7): line editing while typing garbles the
    # raw recorded prompt relative to the transcript's clean version — same
    # words, joined/reordered differently — so equality matching re-added it
    # to the trace as if it were a separate prompt (#8).
    garbled = (
        "Check the latest comments in issues 8, 35, and , and 14Fix them one by one "
        "with one after another. Write test cases to confirm the fixes as needed.  "
        "carefully Then include things that were not fixed before.Also add what you "
        "fixed as a new comment in the issues. Don't close the issue yourself though., "
        "then make a commit"
    )
    clean = (
        "Check the latest comments in issues 8, 35, 56, and 14 carefully. Then include "
        "things that were not fixed before. Fix them one by one with one after another. "
        "Write test cases to confirm the fixes as needed. Also add what you fixed as a "
        "new comment in the issues, then make a commit. Don't close the issue yourself though."
    )
    runner = make_runner(
        repo=FakeCommitRepo(),
        state=AgitrackState(tmp_path),
        verbose=False,
    )
    runner._review_untracked_popup = lambda include_declined: "No untracked files to review."
    runner.state.append_trace("user", garbled)

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[SessionTurn("u1", "a1", clean, "all done", TokenUsage(total=1, output=1), None)],
        backend="claude",
        backend_session_id="ses-1",
        model="m",
        quiet=True,
    )

    assert committed is True
    message = runner.repo.message
    assert message.count("## User") == 1  # the garbled near-duplicate is dropped
    assert "14Fix" not in message


def test_proxy_agent_commit_places_followup_notes_before_the_response(tmp_path):
    # Follow-up notes typed while the agent was working belong between the
    # turn's prompt and its response — not appended after the response (#8).
    runner = make_runner(
        repo=FakeCommitRepo(),
        state=AgitrackState(tmp_path),
        verbose=False,
    )
    runner._review_untracked_popup = lambda include_declined: "No untracked files to review."
    runner.state.append_trace("user", "also read all the comments")
    runner.state.append_trace("user", "no need to verify the full thread")

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[SessionTurn("u1", "a1", "fix the open issues", "all fixed", TokenUsage(total=1, output=1), None)],
        backend="claude",
        backend_session_id="ses-1",
        model="m",
        quiet=True,
    )

    assert committed is True
    message = runner.repo.message
    prompt = message.index("## User\n\nfix the open issues")
    note_one = message.index("## User\n\nalso read all the comments")
    note_two = message.index("## User\n\nno need to verify the full thread")
    response = message.index("## Agent\n\nall fixed")
    assert prompt < note_one < note_two < response


def test_agent_commit_subject_joins_all_prompts_with_slash(tmp_path):
    runner = make_runner(
        repo=FakeCommitRepo(),
        state=AgitrackState(tmp_path),
        verbose=False,
    )
    runner._review_untracked_popup = lambda include_declined: "No untracked files to review."

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[
            SessionTurn("u1", "a1", "add the parser", "done one", TokenUsage(total=1, output=1), None),
            SessionTurn("u2", "a2", "now add tests", "done two", TokenUsage(total=1, output=1), None),
            SessionTurn("u3", "a3", "and fix the lint", "done three", TokenUsage(total=1, output=1), None),
        ],
        backend="claude",
        backend_session_id="ses-1",
        model="m",
        quiet=True,
    )

    assert committed is True
    subject = runner.repo.message.splitlines()[0]
    # Every prompt that led to the commit, in order, joined by " / ".
    assert subject == "<aGiTrack> add the parser / now add tests / and fix the lint"


def test_proxy_agent_commit_preserves_previous_no_change_trace(tmp_path):
    runner = make_runner(
        repo=FakeCommitRepo(),
        state=AgitrackState(tmp_path),
        verbose=False,
    )
    runner._review_untracked_popup = lambda include_declined: "No untracked files to review."
    runner.state.append_trace("user", "explain only")
    runner.state.append_trace("agent", "no code changed")

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[
            SessionTurn("u1", "a1", "explain only", "no code changed", TokenUsage(), None),
            SessionTurn("u2", "a2", "now edit", "edited", TokenUsage(total=1, output=1), None),
        ],
        backend="opencode",
        backend_session_id="ses-1",
        model="provider/model",
        quiet=True,
    )

    assert committed is True
    message = runner.repo.message
    assert message.index("## User\n\nexplain only") < message.index("## Agent\n\nno code changed")
    assert message.index("## Agent\n\nno code changed") < message.index("## User\n\nnow edit")
    assert message.count("## User\n\nexplain only") == 1


def _parse_ready_runner(tmp_path, session, *, last_message_id=None):
    runner = make_runner()
    runner.state = AgitrackState(tmp_path)
    runner.worktree = None
    runner.agent_parse_thread = None
    runner.backend = types.SimpleNamespace(name="claude")
    runner._debug = lambda *a, **k: None
    runner._note_backend_session_change = lambda sid: None
    runner._mirror_session_to_base = lambda sid: None
    runner._integrate_session_turn = lambda: None
    runner.commits = []
    runner._create_agent_commit_from_turns_popup = lambda **k: (runner.commits.append(k), True)[1]
    runner.agent_parse_result = (session.session_id, session, last_message_id, runner.state)
    return runner


def test_finish_agent_parse_defers_commit_while_turn_in_progress(tmp_path):
    # The latest prompt is still being answered (last message was a tool call), so
    # the idle/file-stable debounce must NOT commit — otherwise one prompt gets
    # split into several commits (code now, tests later).
    in_progress = ExportedSession(
        session_id="ses-9",
        model="claude-opus-4-8",
        updated=None,
        turns=[
            SessionTurn(
                "u1", "a1", "fix it and add tests", "Let me add a sanitizer.", TokenUsage(), None, complete=False
            )
        ],
    )
    runner = _parse_ready_runner(tmp_path, in_progress)

    result = runner._finish_agent_parse_if_ready(quiet=True)

    assert result is None  # deferred, not committed
    assert runner.commits == []
    # The conversation id is still tracked while we wait, so resume stays correct.
    assert runner.state.backend_session_id == "ses-9"


def test_finish_agent_parse_commits_once_turn_is_complete(tmp_path):
    finished = ExportedSession(
        session_id="ses-9",
        model="claude-opus-4-8",
        updated=None,
        turns=[
            SessionTurn(
                "u1",
                "a1",
                "fix it and add tests",
                "Done — code and tests are in.",
                TokenUsage(total=1, output=1),
                None,
                complete=True,
            )
        ],
    )
    runner = _parse_ready_runner(tmp_path, finished)

    assert runner._finish_agent_parse_if_ready(quiet=True) is True
    assert len(runner.commits) == 1


def test_finish_agent_parse_forces_in_progress_commit_on_exit(tmp_path):
    # On exit the worktree is torn down, so an unfinished turn must still be
    # committed (require_complete=False) rather than lost.
    in_progress = ExportedSession(
        session_id="ses-9",
        model="claude-opus-4-8",
        updated=None,
        turns=[
            SessionTurn(
                "u1",
                "a1",
                "fix it and add tests",
                "Let me add a sanitizer.",
                TokenUsage(total=1, output=1),
                None,
                complete=False,
            )
        ],
    )
    runner = _parse_ready_runner(tmp_path, in_progress)

    forced = runner._finish_agent_parse_if_ready(quiet=True, integrate=False, require_complete=False)

    assert forced is True
    assert len(runner.commits) == 1


def test_finish_agent_parse_defers_for_queued_followup_not_in_transcript(tmp_path):
    # Turn 1 is complete, but the user queued a follow-up mid-turn that the backend
    # hasn't processed yet (absent from the transcript). While the agent is still
    # active, the commit must wait so the follow-up shares this commit — otherwise
    # it would land as a separate second commit.
    session = ExportedSession(
        "ses-1",
        "m",
        None,
        [
            SessionTurn("u1", "a1", "first prompt", "done one", TokenUsage(total=1, output=1), None, complete=True),
        ],
    )
    runner = _parse_ready_runner(tmp_path, session)
    runner._awaited_followups = ["second prompt"]
    runner._agent_is_active = lambda: True

    assert runner._finish_agent_parse_if_ready(quiet=True) is None  # deferred
    assert runner.commits == []


def test_finish_agent_parse_commits_both_turns_once_followup_lands(tmp_path):
    session = ExportedSession(
        "ses-1",
        "m",
        None,
        [
            SessionTurn("u1", "a1", "first prompt", "done one", TokenUsage(total=1, output=1), None, complete=True),
            SessionTurn("u2", "a2", "second prompt", "done two", TokenUsage(total=1, output=1), None, complete=True),
        ],
    )
    runner = _parse_ready_runner(tmp_path, session)
    runner._awaited_followups = ["second prompt"]
    runner._agent_is_active = lambda: True

    assert runner._finish_agent_parse_if_ready(quiet=True) is True
    assert len(runner.commits) == 1  # ONE commit covering both queued turns
    # The committed batch carried both prompts; the queue is cleared.
    assert [t.user_prompt for t in runner.commits[0]["turns"]] == ["first prompt", "second prompt"]
    assert runner._awaited_followups == []


def test_finish_agent_parse_does_not_block_on_cancelled_followup(tmp_path):
    # The queued prompt never landed and the agent has gone idle (user cancelled
    # it): commit turn 1 rather than block commits forever.
    session = ExportedSession(
        "ses-1",
        "m",
        None,
        [
            SessionTurn("u1", "a1", "first prompt", "done one", TokenUsage(total=1, output=1), None, complete=True),
        ],
    )
    runner = _parse_ready_runner(tmp_path, session)
    runner._awaited_followups = ["cancelled prompt"]
    runner._agent_is_active = lambda: False

    assert runner._finish_agent_parse_if_ready(quiet=True) is True
    assert runner._awaited_followups == []


def test_agent_commit_popup_includes_commit_id_and_session(tmp_path):
    # The auto-commit confirmation — shown only once the commit is MERGED into the
    # base — names the short SHA so the user can find the commit, the session it
    # belongs to (background sessions auto-commit too), and the base it landed on.
    runner = make_runner(
        repo=FakeCommitRepo(),
        state=AgitrackState(tmp_path),
        verbose=False,
        name="feature-x",
        _base_branch="main",
    )
    runner._last_agent_commit_id = "abc1234"
    runner._commit_merged_pending = True

    runner._announce_agent_commit()

    assert runner.message == "Created <aGiTrack> commit abc1234 in session 'feature-x' — merged into main."
    # Summarized turns note that in the same line.
    runner._last_agent_commit_id = "def5678"
    runner._commit_merged_pending = True
    runner._commit_summarized = True
    runner._announce_agent_commit()
    assert runner.message == "Created <aGiTrack> commit def5678 in session 'feature-x' — merged into main (summarized)."


def test_agent_commit_not_announced_before_merge(tmp_path):
    # The "created" popup must NOT appear at commit time — only after the turn is
    # merged into the base. Committing just arms the deferred announcement.
    runner = make_runner(
        repo=FakeCommitRepo(),
        state=AgitrackState(tmp_path),
        verbose=False,
        name="feature-x",
    )
    runner._review_untracked_popup = lambda include_declined: "No untracked files to review."
    runner.state.summarization_enabled = False  # no summarizer popup either

    committed = runner._create_agent_commit_from_turns_popup(
        turns=[SessionTurn("u1", "a1", "do the thing", "done", TokenUsage(total=1, output=1), None)],
        backend="opencode",
        backend_session_id="ses-1",
        model="provider/model",
        quiet=False,
    )

    assert committed is True
    assert runner._commit_merged_pending is True  # armed, awaiting integration
    assert runner.message is None  # nothing announced yet


def _make_cell(data=" ", **attrs):
    class Cell:
        pass

    cell = Cell()
    cell.data = data
    cell.fg = "default"
    cell.bg = "default"
    cell.bold = cell.italics = cell.underscore = cell.blink = cell.reverse = cell.strikethrough = False
    for key, value in attrs.items():
        setattr(cell, key, value)
    return cell


def test_proxy_cell_sgr_reproduces_attributes():
    runner = make_runner()

    # Default cell carries no styling.
    assert runner._cell_sgr(_make_cell()) == ""
    # Reverse video is forwarded verbatim, not flattened to white-on-black.
    assert runner._cell_sgr(_make_cell(reverse=True)) == "7"
    # Truecolor and 256-color (which pyte stores as hex) become 24-bit SGR.
    assert runner._cell_sgr(_make_cell(fg="ff8000")) == "38;2;255;128;0"
    # Named ANSI colors plus attributes round-trip to their SGR codes.
    assert runner._cell_sgr(_make_cell(bold=True, fg="red", bg="black")) == "1;31;40"
    assert runner._cell_sgr(_make_cell(italics=True, fg="brightcyan")) == "3;96"
    # Faint (SGR 2) — pyte drops it, so aGiTrack tracks it on _DimChar and re-emits it,
    # otherwise dim text (Claude's grey autosuggestion) renders like typed text (#113).
    assert runner._cell_sgr(_make_cell(dim=True)) == "2"
    assert runner._cell_sgr(_make_cell(bold=True, dim=True)) == "1;2"


def test_proxy_render_line_preserves_colors():
    runner = make_runner(cols=3)

    class Screen:
        buffer = {
            0: {
                0: _make_cell("a", fg="ff8000"),
                1: _make_cell("b", fg="ff8000"),
                2: _make_cell("c"),
            }
        }

    runner.screen = Screen()

    # The style is emitted once for the run of matching cells and reset before
    # the default-styled cell, so OpenCode's colors survive the round-trip.
    assert runner._render_line(runner.screen.buffer[0]) == "\x1b[38;2;255;128;0mab\x1b[0mc"


def test_proxy_render_line_handles_empty_pyte_cell_data():
    runner = make_runner(cols=2)

    cells = {0: _make_cell("")}  # pyte can leave a cell with empty data

    assert runner._render_line(cells) == "  "


def test_detect_color_mode_from_environment():
    assert detect_color_mode({"COLORTERM": "truecolor"}) == "truecolor"
    assert detect_color_mode({"COLORTERM": "24bit", "TERM": "xterm"}) == "truecolor"
    # OpenCode's default macOS env: no COLORTERM, a 256-colour TERM.
    assert detect_color_mode({"TERM": "xterm-256color"}) == "256"
    assert detect_color_mode({"TERM": "screen-256color"}) == "256"
    assert detect_color_mode({"TERM": "xterm"}) == "16"
    assert detect_color_mode({}) == "16"


def test_proxy_hex_color_preserves_256_encoding():
    runner = make_runner(color_mode="256")
    # These hexes are exact xterm-256 palette entries OpenCode emits via 38;5;N.
    # They must round-trip back to the same palette index so the host terminal
    # renders them with its own palette, exactly like a native session.
    assert runner._hex_color_code("080808", foreground=False) == "48;5;232"
    assert runner._hex_color_code("eeeeee", foreground=True) == "38;5;255"
    assert runner._hex_color_code("7f7f7f", foreground=True) == "38;5;8"
    assert runner._hex_color_code("5fafff", foreground=True) == "38;5;75"


def test_proxy_hex_color_preserves_truecolor_encoding():
    runner = make_runner(color_mode="truecolor")
    assert runner._hex_color_code("ff8000", foreground=True) == "38;2;255;128;0"
    assert runner._hex_color_code("0a0a0a", foreground=False) == "48;2;10;10;10"


def test_proxy_hex_color_falls_back_to_ansi16_in_16_mode():
    runner = make_runner(color_mode="16")
    # Exact ANSI palette entries map to their base/bright SGR codes; arbitrary
    # hexes snap to the nearest of the 16.
    assert runner._hex_color_code("cd0000", foreground=True) == "31"
    assert runner._hex_color_code("cd0000", foreground=False) == "41"
    assert runner._hex_color_code("ff0000", foreground=True) == "91"
    assert runner._hex_color_code("ffffff", foreground=False) == "107"
    assert runner._hex_color_code("123456", foreground=True) == "30"


def test_proxy_named_color_codes():
    runner = make_runner()
    assert runner._color_code("red", foreground=True) == "31"
    assert runner._color_code("blue", foreground=False) == "44"
    assert runner._color_code("brightgreen", foreground=True) == "92"
    assert runner._color_code("brown", foreground=True) == "33"  # pyte's name for yellow
    assert runner._color_code("default", foreground=True) is None
    assert runner._color_code("nonsense", foreground=True) is None


def test_selection_ranges_span_multiple_rows():
    runner = make_runner(
        cols=10,
        sel_active=True,
        sel_anchor=(0, 3),
        sel_point=(2, 1),
    )
    # First row runs from the anchor column to the end, middle rows span fully,
    # last row runs up to the point column.
    assert runner._selection_ranges() == {0: (3, 9), 1: (0, 9), 2: (0, 1)}
    # Anchor and point are order-independent.
    runner.sel_anchor, runner.sel_point = runner.sel_point, runner.sel_anchor
    assert runner._selection_ranges() == {0: (3, 9), 1: (0, 9), 2: (0, 1)}
    runner.sel_active = False
    assert runner._selection_ranges() == {}


def test_proxy_render_line_emits_256_colors_in_256_mode():
    runner = make_runner(color_mode="256", cols=3)

    class Screen:
        buffer = {
            0: {
                0: _make_cell("a", fg="eeeeee", bg="080808"),
                1: _make_cell("b", fg="eeeeee", bg="080808"),
                2: _make_cell("c"),
            }
        }

    runner.screen = Screen()

    out = runner._render_line(runner.screen.buffer[0])
    assert "38;5;255" in out and "48;5;232" in out
    assert "38;2;" not in out and "48;2;" not in out  # no truecolor leakage


def test_proxy_render_line_emits_reverse_video():
    runner = make_runner(cols=3)

    class Screen:
        buffer = {
            0: {
                0: _make_cell("a"),
                1: _make_cell("b", reverse=True),
                2: _make_cell("c"),
            }
        }

    runner.screen = Screen()

    assert runner._render_line(runner.screen.buffer[0]) == "a\x1b[7mb\x1b[0mc"


def test_screen_erase_does_not_carry_glyph_attributes():
    # A backend that clears the screen while underline is still active (Claude's
    # session-choice picker) must not leave underlined blank cells behind — those
    # render as stray horizontal lines that linger after the view is dismissed.
    import pyte

    from agitrack.proxy import _BackgroundColorEraseScreen

    screen = _BackgroundColorEraseScreen(6, 3, history=10, ratio=0.5)
    stream = pyte.ByteStream(screen)
    # Underline on, draw text, then clear the whole display — all while the
    # cursor still carries the underline + a real background colour.
    stream.feed(b"\x1b[4m\x1b[44mhi\x1b[2J")

    # The drawn cells (which carried underline) are erased to clean blanks that
    # keep only the background colour, not the underline.
    for x in (0, 1):
        cell = screen.buffer[0][x]
        assert cell.data == " "
        assert cell.underscore is False  # glyph attribute dropped on erase
        assert cell.bg == "blue"  # background-colour-erase preserved


def test_screen_erase_in_line_does_not_carry_underline():
    import pyte

    from agitrack.proxy import _BackgroundColorEraseScreen

    screen = _BackgroundColorEraseScreen(6, 2, history=10, ratio=0.5)
    stream = pyte.ByteStream(screen)
    # Underline on, then erase to end of line: the blanked cells must be clean.
    stream.feed(b"\x1b[4mx\x1b[K")

    assert screen.buffer[0][0].underscore is True  # the drawn char keeps it
    for x in range(1, 6):
        assert screen.buffer[0][x].underscore is False


def test_screen_tracks_faint_dim_attribute():
    # pyte 0.8.2 has no slot for faint (SGR 2) and silently drops it, so Claude's grey
    # autosuggestion ghost text would re-render at full intensity, indistinguishable from
    # what the user typed (#113). _BackgroundColorEraseScreen tracks faint on _DimChar so
    # it survives into the rendered cells.
    import pyte

    from agitrack.proxy import _BackgroundColorEraseScreen

    screen = _BackgroundColorEraseScreen(20, 2, history=10, ratio=0.5)
    stream = pyte.ByteStream(screen)
    # Faint on, draw "ab", reset, draw "cd" at full intensity.
    stream.feed(b"\x1b[2mab\x1b[0mcd")

    assert screen.buffer[0][0].dim is True  # 'a' is faint
    assert screen.buffer[0][1].dim is True  # 'b' is faint
    assert screen.buffer[0][2].dim is False  # 'c' after reset is not
    assert screen.buffer[0][3].dim is False  # 'd' after reset is not


def test_faint_tracking_ignores_the_2_inside_truecolor_sgr():
    # The faint code (2) must be parsed exactly where pyte expects it, never matched
    # anywhere in the parameter list: in a 24-bit colour like `38;2;r;g;b` the `2` is the
    # colour selector, not faint. Otherwise normal truecolor text — e.g. the user's own
    # white input in Claude — gets wrongly dimmed (regression of the #113 fix).
    import pyte

    from agitrack.proxy import _BackgroundColorEraseScreen

    screen = _BackgroundColorEraseScreen(30, 2, history=10, ratio=0.5)
    stream = pyte.ByteStream(screen)
    stream.feed(b"\x1b[38;2;255;255;255mhi")  # 24-bit white
    assert screen.buffer[0][0].dim is False  # not dimmed by the colour's "2"
    assert screen.buffer[0][0].fg == "ffffff"  # colour still applied

    # 256-colour (38;5;n) likewise must not be read as faint.
    stream.feed(b"\x1b[38;5;240mx")
    assert screen.buffer[0][2].dim is False

    # Genuine faint COMBINED with a truecolor fg keeps both.
    stream.feed(b"\x1b[2;38;2;128;128;128mg")
    cell = screen.buffer[0][3]
    assert cell.dim is True and cell.fg == "808080"


def test_screen_faint_cleared_by_normal_intensity_and_erase():
    # SGR 22 (normal intensity) clears faint, and an erase leaves clean (non-dim) blanks.
    import pyte

    from agitrack.proxy import _BackgroundColorEraseScreen

    screen = _BackgroundColorEraseScreen(6, 2, history=10, ratio=0.5)
    stream = pyte.ByteStream(screen)
    stream.feed(b"\x1b[2mx\x1b[22my")  # faint x, then normal-intensity y
    assert screen.buffer[0][0].dim is True
    assert screen.buffer[0][1].dim is False

    stream.feed(b"\x1b[2m\x1b[2J")  # clear the display while faint is active
    assert screen.buffer[0][0].dim is False  # erased blanks are not dim


def test_screen_survives_private_device_status_query():
    # Claude/Ink emits \x1b[?6n (DEC-private cursor-position query) mid-redraw —
    # e.g. while collapsing an option menu after a selection. pyte's
    # report_device_status() doesn't accept the private flag and would raise
    # TypeError, aborting the feed and dropping the rest of the chunk (which
    # truncated the collapse redraw and left stale menu rows on screen). The screen
    # must absorb the query and keep applying the bytes that follow it.
    import pyte

    from agitrack.proxy import _BackgroundColorEraseScreen

    screen = _BackgroundColorEraseScreen(6, 2, history=10, ratio=0.5)
    stream = pyte.ByteStream(screen)
    stream.feed(b"\x1b[1;1HAA\x1b[?6nBB")  # private DSR sandwiched between writes

    row0 = "".join((screen.buffer[0][x].data or " ") for x in range(6)).rstrip()
    assert row0 == "AABB"  # the text after the query was NOT dropped


def test_feed_child_output_strips_xtmodkeys_mistaken_for_underline():
    # Claude toggles the xterm modifyOtherKeys keyboard mode (CSI > 4 m) when it
    # enters/leaves the session-choice picker. pyte mis-tokenises the >-private
    # sequence as the SGR \x1b[4m (underline on), which then sticks to everything
    # drawn afterwards — the stray horizontal lines. The private sequence must be
    # stripped from what is fed to pyte so no underline leaks.
    import pyte

    from agitrack.proxy import _BackgroundColorEraseScreen

    runner = make_runner(
        screen=_BackgroundColorEraseScreen(10, 2, history=10, ratio=0.5),
    )
    runner.stream = pyte.ByteStream(runner.screen)

    runner._feed_child_output(b"\x1b[>4mhello")

    row = runner.screen.buffer[0]
    assert "".join((row[x].data or " ") for x in range(5)) == "hello"  # not "4mhel.."
    for x in range(10):
        assert row[x].underscore is False


def test_feed_child_output_preserves_dec_private_modes_and_real_sgr():
    # The strip must be surgical: real SGR (incl. genuine underline) and DEC
    # private (?) sequences pass through untouched.
    import pyte

    from agitrack.proxy import _BackgroundColorEraseScreen

    runner = make_runner(
        screen=_BackgroundColorEraseScreen(10, 2, history=10, ratio=0.5),
    )
    runner.stream = pyte.ByteStream(runner.screen)

    runner._feed_child_output(b"\x1b[?25l\x1b[4mU\x1b[24mP")

    assert runner.screen.buffer[0][0].underscore is True  # genuine underline kept
    assert runner.screen.buffer[0][1].underscore is False
    assert runner.screen.cursor.hidden is True  # ?25l honoured


def test_drain_child_output_reads_all_available():
    read_fd, write_fd = os.pipe()
    runner = make_runner(master_fd=read_fd)
    try:
        os.write(write_fd, b"hello ")
        os.write(write_fd, b"world")
        assert runner._drain_child_output() == b"hello world"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_drain_child_output_returns_none_on_eof():
    read_fd, write_fd = os.pipe()
    os.close(write_fd)  # EOF, nothing buffered
    runner = make_runner(master_fd=read_fd)
    try:
        assert runner._drain_child_output() is None
    finally:
        os.close(read_fd)


def _history_runner():
    import pyte

    screen = pyte.HistoryScreen(12, 4, history=100, ratio=0.5)
    stream = pyte.ByteStream(screen)
    for i in range(20):
        stream.feed(f"line{i:02d}\r\n".encode())
    runner = make_runner(
        cols=12,
        rows=5,
        child_mouse=False,
        scroll_back=0,
        screen=screen,
    )
    runner.stream = stream
    runner._render = lambda: None
    return runner


def test_wheel_scrolls_history_and_strips_mouse_when_backend_has_no_mouse():
    runner = _history_runner()
    # Wheel-up consumes the event (returns empty) and scrolls back.
    assert runner._intercept_scroll(b"\x1b[<64;5;5M") == b""
    assert runner.scroll_back == 3
    runner._intercept_scroll(b"\x1b[<64;5;5M")
    assert runner.scroll_back == 6
    # Wheel-down moves toward the live view.
    runner._intercept_scroll(b"\x1b[<65;5;5M")
    assert runner.scroll_back == 3


def test_x10_mouse_reports_are_stripped_and_wheel_scrolls():
    # Terminals that ignore SGR mouse mode (?1006) — some tmux configs, the native Windows
    # console — send legacy X10 reports (ESC [ M + three offset bytes) even though aGiTrack
    # asked for SGR. They must be consumed like SGR reports, or their raw coordinate bytes
    # (column/row 3 is '#') leak into the backend's input as the "mouse cursor hash".
    runner = _history_runner()
    wheel_up = b"\x1b[M" + bytes([32 + 64, 32 + 5, 32 + 5])  # button 64 = wheel up
    assert runner._intercept_scroll(wheel_up) == b""  # consumed, nothing forwarded
    assert runner.scroll_back == 3
    wheel_down = b"\x1b[M" + bytes([32 + 65, 32 + 5, 32 + 5])  # button 65 = wheel down
    runner._intercept_scroll(wheel_down)
    assert runner.scroll_back == 0
    # A non-wheel X10 report whose coordinate byte is '#' (column/row 3) is stripped too,
    # leaving the surrounding real keystrokes intact.
    leaky = b"\x1b[M" + bytes([32, ord("#"), ord("#")])
    assert runner._intercept_scroll(b"x" + leaky + b"y") == b"xy"


def test_is_real_keypress_ignores_x10_mouse_reports():
    leaky = b"\x1b[M" + bytes([32, ord("#"), ord("#")])  # an incidental X10 mouse move
    assert ProxyRunner._is_real_keypress(leaky) is False
    assert ProxyRunner._is_real_keypress(b"a" + leaky) is True  # a real key alongside it


def test_scrolled_view_shows_history_lines():
    runner = _history_runner()
    runner.scroll_back = 9

    def text(lines):
        return ["".join((c.get(x).data if c.get(x) else " ") for x in range(7)).rstrip() for c in lines]

    assert text(runner._visible_lines()) == ["line08", "line09", "line10", "line11"]
    runner.scroll_back = 0
    assert text(runner._visible_lines())[-2] == "line19"


def _paint_runner():
    import types
    import pyte

    screen = pyte.HistoryScreen(8, 3, history=50, ratio=0.5)
    runner = make_runner(
        cols=8,
        rows=4,
        scroll_back=0,
        sel_active=False,
        sel_anchor=None,
        sel_point=None,
        screen=screen,
        message=None,
        message_until=0.0,
    )
    runner.stream = pyte.ByteStream(screen)
    runner._in_sync_update = False
    runner._sync_since = 0.0
    runner.input = types.SimpleNamespace(capturing=False)
    runner._status_line = lambda: "STATUS".ljust(8)
    return runner


def test_render_wraps_frame_in_synchronized_update(monkeypatch):
    runner = _paint_runner()
    writes = []
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append(data) or len(data))

    runner.stream.feed(b"\x1b[Hr0\r\nr1\r\nr2")
    runner._render()
    assert len(writes) == 1
    out = writes[0].decode()
    # The whole repaint is one atomic synchronized update (no tearing/flicker).
    assert out.startswith("\x1b[?2026h") and out.endswith("\x1b[?2026l")
    assert out.count("\x1b[?2026h") == 1 and out.count("\x1b[?2026l") == 1
    # It is a full repaint: every visible row plus the status line is present.
    assert "r0" in out and "r1" in out and "r2" in out and "STATUS" in out


def test_sticky_message_renders_after_timeout(monkeypatch):
    runner = _paint_runner()
    runner.cols = 60  # wide enough that the popup text isn't truncated
    writes = []
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append(data) or len(data))

    runner._set_message("Created <aGiTrack> commit.", sticky=True)
    runner.message_until = time.monotonic() - 100  # the timeout passed long ago

    runner._render()

    # A sticky message stays up past its timeout (until the next keypress).
    assert "Created <aGiTrack> commit." in writes[0].decode()


def test_nonsticky_message_hidden_after_timeout(monkeypatch):
    runner = _paint_runner()
    runner.cols = 60
    writes = []
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append(data) or len(data))

    runner.message = "transient note"
    runner.message_until = time.monotonic() - 100
    runner._message_sticky = False

    runner._render()

    assert "transient note" not in writes[0].decode()


def test_keypress_dismisses_sticky_message():
    runner = make_runner()
    runner._set_message("Created <aGiTrack> commit.", sticky=True)
    assert runner._message_sticky is True

    assert runner._clear_sticky_message_on_input() is True
    assert runner.message is None
    assert runner._message_sticky is False
    # A following keypress has nothing sticky to clear.
    assert runner._clear_sticky_message_on_input() is False


def test_keypress_leaves_nonsticky_message_intact():
    runner = make_runner()
    runner._set_message("transient note")  # default: not sticky

    assert runner._clear_sticky_message_on_input() is False
    assert runner.message == "transient note"


def test_set_message_requests_a_render():
    # The render loop only paints when _render_pending is set (or on child
    # output). A message set from the background idle loop — e.g. the auto-commit
    # confirmation, when the agent is quiet — must therefore request a repaint, or
    # the popup is never drawn.
    runner = make_runner(_render_pending=False)

    runner._set_message("Created <aGiTrack> commit.", sticky=True)

    assert runner._render_pending is True


def test_session_notices_compose_into_multiline_popup():
    # Concurrent sessions each get their own status line; the popup shows both.
    runner = make_runner()
    runner._set_session_notice("alpha", "aGiTrack is summarizing commit a1 in session 'alpha'…", seconds=30)
    runner._set_session_notice("beta", "aGiTrack is summarizing commit b2 in session 'beta'…", seconds=30)

    assert runner.message is not None
    lines = runner.message.split("\n")
    assert lines == [
        "aGiTrack is summarizing commit a1 in session 'alpha'…",
        "aGiTrack is summarizing commit b2 in session 'beta'…",
    ]
    assert runner._notice_shown is True


def test_session_notice_replaces_same_session_line():
    # A session's later notice replaces its own line in place (no duplicate line).
    runner = make_runner()
    runner._set_session_notice("alpha", "aGiTrack is summarizing commit a1 in session 'alpha'…", seconds=30)
    runner._set_session_notice("beta", "aGiTrack is summarizing commit b2 in session 'beta'…", seconds=30)
    runner._set_session_notice("alpha", "Created <aGiTrack> commit a1 in session 'alpha' — merged into main.")

    lines = runner.message.split("\n")
    assert lines == [
        "Created <aGiTrack> commit a1 in session 'alpha' — merged into main.",
        "aGiTrack is summarizing commit b2 in session 'beta'…",
    ]


def test_expired_session_notice_line_is_dropped_on_service():
    runner = make_runner()
    runner._set_session_notice("alpha", "alpha line", seconds=30)
    runner._set_session_notice("beta", "beta line", seconds=30)
    # Expire only beta's line.
    text, _until, sticky = runner._session_notices["beta"]
    runner._session_notices["beta"] = (text, time.monotonic() - 1, sticky)

    runner._service_session_notices()

    assert runner.message == "alpha line"
    assert "beta" not in runner._session_notices


def test_live_notice_service_does_not_request_repaint_when_unchanged():
    # _service_session_notices runs every reactor tick. While a notice's text is
    # unchanged it must NOT keep re-requesting a render — doing so forces a
    # full-frame repaint at tick cadence and flickers the popup on terminals
    # without synchronized-update support. Only a content change repaints.
    runner = make_runner()
    runner._set_session_notice("alpha", "aGiTrack is summarizing commit a1 in session 'alpha'…", seconds=30)
    assert runner._render_pending is True  # first appearance repaints

    runner._render_pending = False
    runner._service_session_notices()  # same text, later tick
    runner._service_session_notices()
    assert runner._render_pending is False  # no churn while unchanged

    # A new line for the session (summarizing -> created) does repaint.
    runner._set_session_notice("alpha", "Created <aGiTrack> commit a1 in session 'alpha' — merged into main.")
    assert runner._render_pending is True


def test_all_notices_expiring_clears_the_popup():
    runner = make_runner()
    runner._set_session_notice("alpha", "alpha line", seconds=30)
    text, _until, sticky = runner._session_notices["alpha"]
    runner._session_notices["alpha"] = (text, time.monotonic() - 1, sticky)

    runner._service_session_notices()

    assert runner.message is None
    assert runner._notice_shown is False


def test_one_off_message_takes_over_then_notices_reassert():
    runner = make_runner()
    runner._set_session_notice("alpha", "alpha line", seconds=30)
    # A one-off message (e.g. an error) temporarily takes over the popup.
    runner._set_message("Cancelled.", seconds=4)
    assert runner.message == "Cancelled." and runner._notice_shown is False
    # While the one-off is still live, the service tick leaves it alone.
    runner._service_session_notices()
    assert runner.message == "Cancelled."
    # Once it expires, the live notice reasserts itself.
    runner.message_until = time.monotonic() - 1
    runner._service_session_notices()
    assert runner.message == "alpha line"


def test_track_sync_update_defers_then_releases_render():
    runner = make_runner(
        _in_sync_update=False,
        _sync_since=0.0,
        _render_pending=False,
        _last_render=0.0,
    )
    rendered = []
    runner._render = lambda: rendered.append(1)

    # Begin-sync with no matching end: aGiTrack is mid-update and must defer.
    runner._track_sync_update(b"\x1b[?2026h")
    assert runner._in_sync_update is True
    runner._render_output()
    assert rendered == [] and runner._render_pending is True

    # End-sync releases the hold; the next output paints.
    runner._track_sync_update(b"\x1b[?2026l")
    assert runner._in_sync_update is False
    runner._render_output()
    assert rendered == [1]

    # A begin+end inside one chunk ends not-in-update.
    runner._track_sync_update(b"\x1b[?2026hABC\x1b[?2026l")
    assert runner._in_sync_update is False


def test_hold_incomplete_tail_buffers_split_escape_sequence():
    runner = make_runner()
    # A mouse report split across reads must be held back, not leaked as bytes.
    head, tail = runner._hold_incomplete_tail(b"abc\x1b[<35;10;")
    assert head == b"abc"
    assert tail == b"\x1b[<35;10;"
    head2, tail2 = runner._hold_incomplete_tail(tail + b"5M")
    assert head2 == b"\x1b[<35;10;5M"
    assert tail2 == b""
    # A complete buffer leaves no tail.
    assert runner._hold_incomplete_tail(b"plain text") == (b"plain text", b"")


def test_hold_incomplete_tail_buffers_split_x10_mouse():
    runner = make_runner()
    # A legacy X10 report (ESC [ M + 3 bytes) split across reads must also be held, not
    # leaked — its trailing coordinate bytes are ordinary characters.
    head, tail = runner._hold_incomplete_tail(b"abc\x1b[M")
    assert head == b"abc"
    assert tail == b"\x1b[M"
    head2, tail2 = runner._hold_incomplete_tail(tail + bytes([32, ord("#"), ord("#")]))
    assert head2 == b"\x1b[M" + bytes([32, ord("#"), ord("#")])
    assert tail2 == b""


def test_pageup_pagedown_scroll_history():
    runner = _history_runner()
    runner._intercept_scroll(b"\x1b[5~")  # PageUp
    assert runner.scroll_back == max(runner.rows - 2, 1)
    runner._intercept_scroll(b"\x1b[6~")  # PageDown
    assert runner.scroll_back == 0


def test_mouse_drag_does_not_copy_or_hijack_selection():
    # Text selection is the terminal's job. A left-button drag must NOT trigger aGiTrack's
    # own copy: that only ran in terminals that forward the drag to the app (mouse mode
    # 1000), where it suppressed native selection and popped an unwanted "Copied N char(s)
    # to clipboard" message (#112). aGiTrack now ignores left-button events entirely.
    runner = _history_runner()
    import pyte

    runner.screen = pyte.HistoryScreen(20, 4, history=50, ratio=0.5)
    pyte.ByteStream(runner.screen).feed(b"hello world\r\n")
    runner.sel_active = False
    runner.sel_anchor = runner.sel_point = None
    copied: list[str] = []
    messages: list[str] = []
    runner._copy_to_clipboard = lambda text: copied.append(text)
    runner._set_message = lambda msg, **k: messages.append(msg)

    runner._intercept_scroll(b"\x1b[<0;1;1M")  # press at col 1, row 1
    runner._intercept_scroll(b"\x1b[<32;5;1M")  # drag to col 5
    runner._intercept_scroll(b"\x1b[<0;5;1m")  # release at a different cell (a real drag)
    assert copied == []  # nothing was copied
    assert messages == []  # no "Copied N chars" popup
    assert runner.sel_active is False  # aGiTrack never started a selection


def test_mouse_events_are_stripped_from_forwarded_input():
    runner = _history_runner()
    runner.sel_active = False
    runner.sel_anchor = runner.sel_point = None
    runner._copy_to_clipboard = lambda text: None
    runner._set_message = lambda *a, **k: None
    assert runner._intercept_scroll(b"X\x1b[<0;3;2MY") == b"XY"


def test_reset_agent_tracking_reenables_scrollback_for_new_backend():
    # Switching OpenCode -> Claude must clear child_mouse so aGiTrack reclaims the
    # wheel for scrollback instead of forwarding it to a backend that ignores it.
    runner = make_runner(
        child_mouse=True,
        scroll_back=7,
        passthrough_prompt=bytearray(b"abc"),
        passthrough_escape=None,
    )

    runner._reset_agent_tracking()

    assert runner.child_mouse is False
    assert runner.scroll_back == 0


def test_wheel_forwarded_when_backend_manages_mouse():
    runner = _history_runner()
    runner.child_mouse = True
    event = b"\x1b[<64;5;5M"
    assert runner._intercept_scroll(event) == event  # passed through to backend
    assert runner.scroll_back == 0


def test_apply_timings_overrides_constants():
    from agitrack.config import DEFAULT_TIMINGS

    runner = make_runner()
    # Defaults are the class constants until config is applied.
    assert runner.BASE_POLL_SECONDS == DEFAULT_TIMINGS["base_poll_seconds"]

    custom = dict(DEFAULT_TIMINGS, base_poll_seconds=30.0, child_idle_seconds=1.5)
    runner._apply_timings(custom)

    assert runner.BASE_POLL_SECONDS == 30.0
    assert runner.CHILD_IDLE_SECONDS == 1.5
    assert runner.POLL_SECONDS == DEFAULT_TIMINGS["background_poll_seconds"]


def test_apply_timings_sets_idle_backoff_constants():
    from agitrack.config import DEFAULT_TIMINGS

    runner = make_runner()
    custom = dict(DEFAULT_TIMINGS, idle_after_seconds=12.0, idle_poll_seconds=45.0)
    runner._apply_timings(custom)
    assert runner.IDLE_AFTER_SECONDS == 12.0
    assert runner.IDLE_POLL_SECONDS == 45.0


def test_select_timeout_backs_off_when_idle():
    # A quiescent runner (no input/output/work) blocks in select for the long
    # IDLE_POLL_SECONDS so the CPU can sleep; any work drops it to the active
    # poll, and a queued repaint flushes on the very next tick. The active poll
    # is a coarse ≥1s — every background sweep self-throttles, and select still
    # wakes instantly on real input/output.
    runner = make_runner()
    runner.IDLE_POLL_SECONDS = 30.0
    runner.ACTIVE_POLL_SECONDS = 1.0
    assert runner.ACTIVE_POLL_SECONDS >= 1.0  # no sub-second busy-poll

    assert runner._is_idle() is True
    assert runner._select_timeout() == 30.0

    runner.agent_in_flight = True  # a turn is in flight → active poll
    assert runner._is_idle() is False
    assert runner._select_timeout() == 1.0

    runner.agent_in_flight = False
    runner._render_pending = True  # a paint is queued → flush next tick
    assert runner._select_timeout() == 0.016


def test_select_timeout_honors_deferred_prompt_submit():
    import time

    # The injected-Enter is scheduled 0.4s out; the active poll must not stretch
    # it to a full second, so the timeout collapses to the time remaining.
    runner = make_runner()
    runner._render_pending = False
    runner.ACTIVE_POLL_SECONDS = 1.0
    runner._pending_enter_at = time.monotonic() + 0.4
    timeout = runner._select_timeout()
    assert 0.0 < timeout <= 0.4 + 0.001  # blocks only until the Enter is due, not 1s

    runner._pending_enter_at = time.monotonic() - 5  # already overdue → fire now
    assert runner._select_timeout() == 0.0


def test_is_idle_false_until_user_and_backend_go_quiet():
    import time

    runner = make_runner()
    runner.IDLE_AFTER_SECONDS = 30.0
    now = time.monotonic()

    runner.last_user_input = now  # just typed
    assert runner._is_idle() is False

    runner.last_user_input = now - 100  # typed long ago
    runner.last_child_output = now  # but the backend just printed
    assert runner._is_idle() is False

    runner.last_child_output = now - 100  # both quiet now
    assert runner._is_idle() is True


def test_is_idle_false_for_pending_pipeline_work():
    import time

    runner = make_runner()
    old = time.monotonic() - 100
    runner.last_user_input = old
    runner.last_child_output = old
    assert runner._is_idle() is True  # baseline: idle

    # A queued prompt, a deferred Enter, or a set file-change event each keep the
    # fast loop so the per-turn pipeline is serviced without delay.
    runner.pending_prompt_text = "do the thing"
    assert runner._is_idle() is False
    runner.pending_prompt_text = ""

    runner._pending_enter_at = time.monotonic() + 0.4
    assert runner._is_idle() is False
    runner._pending_enter_at = None

    runner.file_change_event.set()
    assert runner._is_idle() is False
    runner.file_change_event.clear()
    assert runner._is_idle() is True


def test_is_idle_respects_timed_but_not_sticky_messages():
    import time

    runner = make_runner()
    old = time.monotonic() - 100
    runner.last_user_input = old
    runner.last_child_output = old

    # A timed notice must expire on schedule, so it keeps us awake.
    runner.message = "Saved."
    runner._message_sticky = False
    runner.message_until = time.monotonic() + 5
    assert runner._is_idle() is False

    # A sticky notice waits for a keypress (which wakes select on its own), so it
    # must NOT pin the CPU at the fast poll rate.
    runner._message_sticky = True
    assert runner._is_idle() is True


def test_proxy_refuses_second_instance(monkeypatch, capsys):
    import sys

    runner = make_runner()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    runner._ensure_backend_available = lambda: True
    # A live aGiTrack (PID 4321) already holds the lock: acquire fails.
    runner.management_lock = type("L", (), {"acquire": lambda self: False, "owner_pid": lambda self: 4321})()

    assert runner.run() == 1
    out = capsys.readouterr().out
    assert "already running" in out and "4321" in out  # names the holding process


def _run_startup_runner(monkeypatch):
    import sys

    runner = make_runner()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    runner.management_lock = type(
        "L", (), {"acquire": lambda self: True, "release": lambda self: None, "owner_pid": lambda self: 0}
    )()
    return runner


def test_run_asks_privacy_only_after_backend_gate(monkeypatch):
    # The privacy warning must come AFTER the backend install/availability gate (and the
    # gh-login / menu-key prompts cli.py runs before this), right before the TUI — so if the
    # backend isn't available, the user is never asked to acknowledge anything.
    import agitrack.cli as cli

    runner = _run_startup_runner(monkeypatch)
    runner._ensure_backend_available = lambda: False  # backend unavailable → stop here
    asked: list[bool] = []
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: asked.append(True) or True)

    assert runner.run() == 1
    assert asked == []  # privacy is asked only once the backend is available


def test_run_stops_when_privacy_declined(monkeypatch):
    # Declining the privacy warning stops startup before the backend is spawned.
    import agitrack.cli as cli

    runner = _run_startup_runner(monkeypatch)
    runner._ensure_backend_available = lambda: True
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: False)  # user declined
    spawned: list[bool] = []
    runner._spawn = lambda: spawned.append(True)

    assert runner.run() == 1
    assert spawned == []  # never reached the spawn


def test_run_forwards_skip_privacy_ack(monkeypatch):
    # The --skip-privacy-ack flag (set on an in-app menu re-exec) is passed through to the ack.
    import agitrack.cli as cli

    runner = _run_startup_runner(monkeypatch)
    runner._ensure_backend_available = lambda: True
    runner._skip_privacy_ack = True
    seen: list = []
    # Return False so run() stops right after the ack (before the heavier startup work).
    monkeypatch.setattr(cli, "_acknowledge_privacy_warning", lambda **k: seen.append(k.get("skip")) or False)

    assert runner.run() == 1
    assert seen == [True]


def _mux_runner():
    runner = make_runner(
        cols=20,
        rows=5,
        color_mode="truecolor",
        name="A",
        worktree=None,
        repo="repoA",
        state="stateA",
        backend="bA",
        actions="actA",
    )
    runner._render = lambda: None
    runner._resize_child = lambda: None
    runner._enable_host_mouse = lambda: None
    runner._set_message = lambda *a, **k: None
    runner._stop_file_watcher = lambda: None
    # On Windows a session switch performs a full state reset (teardown + fresh respawn)
    # instead of resuming the backgrounded ConPTY in place; stub it so the pointer-swap
    # assertions run identically on every CI OS.
    runner._restart_agent = lambda msg: None
    runner.sessions = [runner.active]
    return runner


def _bg_session(name):

    return Session(
        **{
            **Session.runtime_defaults(),
            "name": name,
            "repo": f"repo{name}",
            "state": f"state{name}",
            "backend": f"b{name}",
            "actions": f"act{name}",
        }
    )


def test_switch_active_swaps_session_state():
    runner = _mux_runner()
    runner.sessions.append(_bg_session("B"))

    runner._switch_active(1)
    assert runner.active_index == 1 and runner.repo == "repoB"
    assert runner.sessions[0].repo == "repoA"  # A's state was preserved

    runner._switch_active(0)
    assert runner.repo == "repoA"
    assert runner.sessions[1].repo == "repoB"


def test_background_fds_excludes_active_session():
    runner = _mux_runner()
    b = _bg_session("B")
    b.master_fd = 20
    runner.sessions.append(b)
    runner.sessions[0].master_fd = 10  # active snapshot fd (handled separately)
    assert runner._background_fds() == {20: b}


def test_pump_background_feeds_screen_without_disturbing_active():
    import os
    import pyte

    runner = _mux_runner()
    b = _bg_session("B")
    read_fd, write_fd = os.pipe()
    b.master_fd = read_fd
    b.screen = pyte.HistoryScreen(20, 4, history=10, ratio=0.5)
    b.stream = pyte.ByteStream(b.screen)
    runner.sessions.append(b)
    try:
        os.write(write_fd, b"hello-bg")
        runner._pump_background(b)
        row0 = "".join((b.screen.buffer[0].get(x).data if b.screen.buffer[0].get(x) else " ") for x in range(8))
        assert row0 == "hello-bg"
        assert runner.repo == "repoA"  # active session untouched
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_background_session_relaunches_on_unexpected_exit_then_stops_after_crashloop():
    # A background backend that exits on its own (e.g. Claude on Windows when it loses the
    # foreground) must be relaunched+resumed in the background like the active session is —
    # not silently dropped. A backend that refuses to stay up is finally stopped (crash-loop
    # guard) so it isn't relaunched forever.
    runner = _mux_runner()
    b = _bg_session("B")
    runner.sessions.append(b)
    runner._drain_child_output = lambda: None  # force the background drain to report "backend gone"
    # The background-safe respawn steps are no-ops here (their real work is covered elsewhere).
    for name in (
        "_teardown_child",
        "_reset_agent_tracking",
        "_sanitize_state_trace",
        "_initialize_session_baseline",
        "_init_screen",
        "_spawn",
        "_commit_latest_turn_sync",
        "_stop_file_watcher",
    ):
        setattr(runner, name, lambda *a, **k: None)

    # The first few unexpected exits relaunch the session in the background; it is never dropped.
    for _ in range(3):
        runner._pump_background(b)
        assert b in runner.sessions

    # A further quick exit trips the per-session crash-loop guard → the session is stopped.
    runner._pump_background(b)
    assert b not in runner.sessions


def test_baseline_drops_session_with_no_conversation(tmp_path):
    from types import SimpleNamespace

    from agitrack.transcripts import ExportedSession

    runner = make_runner(
        state=AgitrackState(tmp_path),
        repo=SimpleNamespace(repo=tmp_path),
    )
    runner.state.backend_session_id = "ses-empty"
    runner._should_continue_session = lambda: True
    # A session that exists but has no turns must not be resumed.
    runner.backend = SimpleNamespace(export_session=lambda repo, sid: ExportedSession(sid, None, None, []))

    runner._initialize_session_baseline()

    assert runner.state.backend_session_id is None
    assert runner.state.last_backend_message_id is None


def test_new_session_flag_clears_backend_session_and_mints_agitrack_id(tmp_path):
    runner = make_runner(
        _force_new_session=True,
        state=AgitrackState(tmp_path),
    )
    runner.state.backend_session_id = "old-session"
    old_agit = runner.state.session_id

    runner._apply_new_session_if_requested()

    assert runner.state.backend_session_id is None
    assert runner.state.session_id != old_agit


def test_status_line_shows_base_branch(tmp_path):
    import subprocess

    from agitrack.git import GitRepo
    from agitrack.config import AgitrackState

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    state = AgitrackState(tmp_path)
    state.backend_session_id = "abcdef123456"
    runner = make_runner(
        repo=GitRepo(tmp_path),
        state=state,
        name="session-1",
        backend=type("B", (), {"name": "claude"})(),
        _base_branch="main",
        worktree=object(),
        scroll_back=0,
        cols=120,
    )

    line = runner._status_line()
    assert "session-1" in line
    assert "→ main" in line  # the branch this session's work merges into
    # Repo dir is on the same branch ⇒ the branch is NOT bolded.
    assert "\x1b[1mmain" not in line


def test_status_line_bolds_base_branch_when_repo_dir_on_another_branch(tmp_path):
    import subprocess

    from agitrack.config import AgitrackState
    from agitrack.git import GitRepo

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    state = AgitrackState(tmp_path)
    state.backend_session_id = "abcdef123456"
    runner = make_runner(
        repo=GitRepo(tmp_path),
        state=state,
        name="session-1",
        backend=type("B", (), {"name": "claude"})(),
        _base_branch="feature-x",
        worktree=object(),
        scroll_back=0,
        cols=120,
    )
    runner._repo_dir_branch = "main"  # the repo directory is checked out elsewhere

    line = runner._status_line()
    # The integration branch differs from the repo dir's branch ⇒ it's bolded.
    assert "→ \x1b[1mfeature-x\x1b[22m" in line


def test_inject_prompt_defers_enter_until_text_settles():
    import os
    import time

    read_fd, write_fd = os.pipe()
    try:
        runner = make_runner(
            master_fd=write_fd,
            merge_ctx=MergeContext(source_branch="agit/s/t1", context="", phase=MergePhase.PENDING),
            _pending_enter_at=None,
        )

        runner._inject_prompt("resolve the\nconflict   now")
        # The text is typed immediately, collapsed to a single line, with NO
        # trailing carriage return (that would submit mid-paste).
        typed = os.read(read_fd, 4096)
        assert typed == b"resolve the conflict now"
        assert runner._pending_enter_at is not None
        assert runner.merge_ctx.prompt_sent_at is None  # not submitted yet

        # Too early: the Enter is still pending.
        runner._flush_pending_enter()
        assert runner._pending_enter_at is not None

        # Once the settle delay elapses, the Enter is sent as its own keystroke.
        runner._pending_enter_at = time.monotonic() - 0.01
        runner._flush_pending_enter()
        assert os.read(read_fd, 16) == b"\r"
        assert runner._pending_enter_at is None
        assert runner.merge_ctx.prompt_sent_at is not None
        assert runner.merge_ctx.phase is MergePhase.RESOLVING  # PENDING → RESOLVING on Enter
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_backend_session_change_warns_once(tmp_path):
    state = AgitrackState(tmp_path)
    state.backend_session_id = "old"
    runner = make_runner(
        worktree=object(),
        _warned_backend_session=False,
        state=state,
    )
    messages = []
    runner._set_message = lambda message, **kw: messages.append(message)

    runner._note_backend_session_change("new")
    assert messages and "separate branch" in messages[0].lower()
    assert runner._warned_backend_session is True

    # It only warns once, not on every subsequent change.
    messages.clear()
    runner.state.backend_session_id = "new"
    runner._note_backend_session_change("newer")
    assert messages == []


def _name_persisting_runner(tmp_path, name):
    base = tmp_path / "base"
    base.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    return make_runner(
        worktree=object(),
        _warned_backend_session=True,
        name=name,
        state=AgitrackState(worktree),
        base_repo=types.SimpleNamespace(repo=base),
        global_config=types.SimpleNamespace(default_backend="opencode"),
    ), base


def test_backend_session_change_persists_name_in_root_state(tmp_path):
    # The user-given name is linked to the conversation id as soon as the
    # runner observes it — not only on clean exit — so it survives crashes,
    # kept worktrees, and restarts.
    runner, base = _name_persisting_runner(tmp_path, "my-feature")

    runner._note_backend_session_change("sess-1")

    assert AgitrackState(base).session_name_for("sess-1") == "my-feature"


def test_backend_session_change_follows_id_drift(tmp_path):
    # When the backend forks a new conversation id (e.g. on resume), the name
    # follows it so the resume list still shows the session under its name.
    runner, base = _name_persisting_runner(tmp_path, "my-feature")

    runner._note_backend_session_change("sess-1")
    runner.state.backend_session_id = "sess-1"
    runner._note_backend_session_change("sess-2")

    assert AgitrackState(base).session_name_for("sess-2") == "my-feature"


def test_auto_session_names_are_not_persisted(tmp_path):
    # Auto session-N names are placeholders, not names; recording them would
    # mislabel unrelated conversations across runs.
    runner, base = _name_persisting_runner(tmp_path, "session-3")

    runner._note_backend_session_change("sess-1")

    assert AgitrackState(base).session_name_for("sess-1") is None


def _resume_listing_runner(tmp_path, *, base_refs, worktree_sessions, worktree_names=()):
    # A runner wired so the resume-list helpers can query a fake backend and
    # worktree manager: ``base_refs`` is what list_sessions(base) returns,
    # ``worktree_sessions`` is the (name, ref) list list_worktree_sessions
    # returns, and ``worktree_names`` are directories present on disk.
    base = tmp_path / "base"
    base.mkdir()
    root = tmp_path / "worktrees"
    root.mkdir()
    backend = types.SimpleNamespace(
        name="opencode",
        list_sessions=lambda repo: list(base_refs),
        list_worktree_sessions=lambda r: list(worktree_sessions),
    )
    runner = make_runner(name="A", worktree=None, backend=backend)
    runner._debug = lambda *a, **k: None
    runner.base_repo = types.SimpleNamespace(repo=base)
    runner.global_config = types.SimpleNamespace(default_backend="opencode")
    runner.worktree_manager = types.SimpleNamespace(
        root=root, list=lambda: [types.SimpleNamespace(name=n) for n in worktree_names]
    )
    runner.sessions = [runner.active]
    return runner, base


def test_resumable_sessions_includes_removed_worktree_conversations(tmp_path):
    # Named sessions run in worktrees that are emptied on quit; their backend
    # conversations must still surface for resume on the next run (issue: only
    # the base-repo conversation was listed, so named sessions vanished).
    runner, _base = _resume_listing_runner(
        tmp_path,
        base_refs=[SessionRef("base-1", 100.0)],
        worktree_sessions=[("alpha", SessionRef("wt-alpha", 300.0)), ("beta", SessionRef("wt-beta", 200.0))],
    )

    ids = [ref.id for ref in runner._resumable_sessions()]

    assert ids == ["wt-alpha", "wt-beta", "base-1"]  # merged, newest first


def test_resumable_sessions_dedupes_by_id(tmp_path):
    # A conversation reported by both the base list and a worktree list appears once.
    runner, _base = _resume_listing_runner(
        tmp_path,
        base_refs=[SessionRef("shared", 100.0)],
        worktree_sessions=[("alpha", SessionRef("shared", 100.0))],
    )

    assert [ref.id for ref in runner._resumable_sessions()] == ["shared"]


def test_resumable_sessions_includes_reserved_named_session_without_transcript(tmp_path):
    # #75: a no-commit session reserves its name in the durable record, but the
    # backend has no transcript for it (no commits, worktree emptied). It must
    # still be offered for resume so the reserved name isn't stranded — taken yet
    # un-resumable.
    runner, _base = _resume_listing_runner(tmp_path, base_refs=[], worktree_sessions=[])
    runner._agitrack_named_sessions = lambda: {"ghost-id": "experiment"}

    refs = runner._resumable_sessions()

    assert [(ref.id, ref.label) for ref in refs] == [("ghost-id", "experiment")]
    # The very same record reserves the name, so it is both taken AND resumable.
    assert runner._session_name_taken("experiment") is True


def test_resumable_named_session_dated_when_it_was_named_not_epoch(tmp_path):
    # A surfaced no-commit session must carry the time it was named, not 0.0 (which
    # rendered as an absurd "20000d ago").
    import time as _time

    from agitrack.config import AgitrackState

    runner, base = _resume_listing_runner(tmp_path, base_refs=[], worktree_sessions=[])
    state = AgitrackState(base)
    state.name_session("ghost-id", "experiment")  # stamps session_named_at
    runner._agitrack_named_sessions = lambda: {"ghost-id": "experiment"}

    ref = runner._resumable_sessions()[0]
    assert ref.id == "ghost-id"
    assert abs(ref.updated - _time.time()) < 60  # a real, recent timestamp
    assert runner._format_age(ref.updated) in ("just now", "0m ago") or ref.updated > 0


def test_format_age_handles_unknown_timestamp():
    runner = make_runner(name="main")
    assert runner._format_age(0) == "date unknown"
    assert runner._format_age(0.0) == "date unknown"
    assert "ago" in runner._format_age(time.time() - 7200)  # a real one still works


def test_resumable_sessions_does_not_duplicate_named_session_with_transcript(tmp_path):
    # When the backend still enumerates a named conversation, it appears once
    # (the durable record must not add a second copy).
    runner, _base = _resume_listing_runner(
        tmp_path,
        base_refs=[],
        worktree_sessions=[("alpha", SessionRef("wt-alpha", 300.0))],
    )
    runner._agitrack_named_sessions = lambda: {"wt-alpha": "alpha"}

    assert [ref.id for ref in runner._resumable_sessions()] == ["wt-alpha"]


def test_named_sessions_recovers_name_from_worktree_key(tmp_path):
    # When the persisted record never linked a conversation's name, the worktree
    # directory it ran in (its name) labels it in the resume list.
    runner, _base = _resume_listing_runner(
        tmp_path,
        base_refs=[],
        worktree_sessions=[("alpha", SessionRef("wt-alpha", 300.0))],
    )

    assert runner._agitrack_named_sessions().get("wt-alpha") == "alpha"


def test_named_sessions_persisted_name_wins_over_worktree_key(tmp_path):
    # The durable record follows id drift, so its name takes precedence over the
    # worktree key for the same conversation id.
    runner, base = _resume_listing_runner(
        tmp_path,
        base_refs=[],
        worktree_sessions=[("old-dir", SessionRef("wt-1", 300.0))],
    )
    AgitrackState(base, default_backend="opencode").name_session("wt-1", "renamed")

    assert runner._agitrack_named_sessions().get("wt-1") == "renamed"


def test_new_session_name_cannot_clash_with_dormant_named_session(tmp_path):
    # A fresh session must not reuse the name of a past/dormant conversation:
    # resuming that one later recreates its worktree at the same path.
    runner, _base = _resume_listing_runner(
        tmp_path,
        base_refs=[],
        worktree_sessions=[("alpha", SessionRef("wt-alpha", 300.0))],
    )

    assert runner._session_name_taken("alpha") is True
    assert "alpha" in runner._taken_session_names()
    assert runner._session_name_taken("gamma") is False


def test_new_session_not_applied_without_flag(tmp_path):
    runner = make_runner(
        _force_new_session=False,
        state=AgitrackState(tmp_path),
    )
    runner.state.backend_session_id = "keep-this"
    runner._apply_new_session_if_requested()
    assert runner.state.backend_session_id == "keep-this"


def test_finalize_pending_work_commits_non_interactively():
    runner = make_runner(
        agent_parse_thread=None,
    )
    runner.sessions = []
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._debug = lambda *a, **k: None
    runner._start_agent_parse = lambda: False
    calls = []
    runner._finish_agent_parse_if_ready = lambda **k: (calls.append(k), False)[1]

    runner._finalize_pending_work()

    assert calls, "finalize should attempt to commit the latest turn"
    # Exit must never block on the untracked-files prompt.
    assert all(call.get("prompt_untracked") is False for call in calls)


def _finalize_message_runner(monkeypatch=None):
    runner = make_runner()
    runner.sessions = []
    runner._render = lambda: None
    # Isolate the message from the rest of teardown.
    for name in (
        "_stop_git_worker",
        "_cancel_inflight_shared_fetches",
        "_commit_latest_turn_sync",
        "_auto_share_on_exit",
        "_finalize_summary_then_integrate_on_exit",
        "_delete_orphan_merged_branches",
    ):
        setattr(runner, name, lambda *a, **k: None)
    return runner


def test_finalize_always_shows_a_message_even_when_clean():
    # Even a "clean" exit does teardown (worktree removal, auto-share push, thread
    # joins) that can take seconds, so it must never be silent.
    runner = _finalize_message_runner()
    msgs: list = []
    runner._set_message = lambda m, **k: msgs.append(m)
    runner._describe_exit_finalize = lambda: None  # nothing specific to name

    runner._finalize_pending_work()

    assert msgs, "exit must show a message immediately"
    assert "Finalizing things before exiting" in msgs[0]
    assert "commit" not in msgs[0].lower()  # never the vague "finalizing commits"


def test_finalize_shows_the_specific_work_when_known():
    runner = _finalize_message_runner()
    msgs: list = []
    runner._set_message = lambda m, **k: msgs.append(m)
    runner._describe_exit_finalize = lambda: "Finishing up before exit — committing the latest turn…"

    runner._finalize_pending_work()

    assert msgs[0] == "Finishing up before exit — committing the latest turn…"


def _fake_completed(stdout):
    return types.SimpleNamespace(stdout=stdout, returncode=0)


def test_confirm_forced_exit_quit_without_gui(monkeypatch):
    from agitrack.proxy import host_prompt

    monkeypatch.setattr(host_prompt, "can_show_dialog", lambda: False)
    # No window server: never shell out, just resolve to QUIT so the exit is fast.
    monkeypatch.setattr(
        host_prompt.subprocess,
        "run",
        lambda *a, **k: pytest.fail("must not run osascript without a GUI"),
    )
    assert host_prompt.confirm_forced_exit("x") == host_prompt.QUIT


def test_confirm_forced_exit_parses_button_and_timeout(monkeypatch):
    from agitrack.proxy import host_prompt

    monkeypatch.setattr(host_prompt, "can_show_dialog", lambda: True)

    monkeypatch.setattr(
        host_prompt.subprocess,
        "run",
        lambda *a, **k: _fake_completed("button returned:Reopen aGiTrack, gave up:false"),
    )
    assert host_prompt.confirm_forced_exit("x") == host_prompt.REOPEN

    monkeypatch.setattr(
        host_prompt.subprocess,
        "run",
        lambda *a, **k: _fake_completed("button returned:Quit aGiTrack, gave up:false"),
    )
    assert host_prompt.confirm_forced_exit("x") == host_prompt.QUIT

    # A timed-out dialog (system restart) reports gave up:true and must QUIT,
    # even though the giving-up record can echo the default button.
    monkeypatch.setattr(
        host_prompt.subprocess,
        "run",
        lambda *a, **k: _fake_completed("button returned:Reopen aGiTrack, gave up:true"),
    )
    assert host_prompt.confirm_forced_exit("x") == host_prompt.QUIT


def test_confirm_forced_exit_quit_on_osascript_error(monkeypatch):
    from agitrack.proxy import host_prompt

    monkeypatch.setattr(host_prompt, "can_show_dialog", lambda: True)

    def boom(*a, **k):
        raise OSError("osascript blew up")

    monkeypatch.setattr(host_prompt.subprocess, "run", boom)
    assert host_prompt.confirm_forced_exit("x") == host_prompt.QUIT


def test_can_show_dialog_requires_macos(monkeypatch):
    from agitrack.proxy import host_prompt

    monkeypatch.setattr(host_prompt.shutil, "which", lambda _name: "/usr/bin/osascript")
    monkeypatch.setattr(host_prompt.sys, "platform", "linux")
    assert host_prompt.can_show_dialog() is False
    monkeypatch.setattr(host_prompt.sys, "platform", "darwin")
    assert host_prompt.can_show_dialog() is True
    monkeypatch.setattr(host_prompt.shutil, "which", lambda _name: None)
    assert host_prompt.can_show_dialog() is False


def test_had_unfinalized_work_detects_inflight_and_unmerged():
    runner = make_runner()
    runner.sessions = []
    runner._unmerged_worktrees = lambda: []

    runner._agent_is_active = lambda: True  # a turn is in flight
    assert runner._had_unfinalized_work() is True

    runner._agent_is_active = lambda: False
    runner._unmerged_worktrees = lambda: [("session-1 (current session)", "")]  # committed but unmerged
    assert runner._had_unfinalized_work() is True


def test_had_unfinalized_work_ignores_base_repo_and_declined_dirt():
    # A clean session (every turn committed AND merged) must exit silently even when
    # the developer's own base repo is dirty or the worktree has declined/ignored
    # files — aGiTrack does not finalize those, so they aren't "unfinalized work".
    runner = make_runner()
    runner.sessions = []
    runner._agent_is_active = lambda: False
    runner._unmerged_worktrees = lambda: []  # _unmerged_worktrees already excludes that noise
    assert runner._had_unfinalized_work() is False
    assert runner._describe_exit_finalize() is None


def test_describe_exit_finalize_names_the_pending_work():
    # When there IS something to finalize, the exit message says exactly what.
    runner = make_runner()
    runner.sessions = []
    runner._agent_is_active = lambda: True
    runner._unmerged_worktrees = lambda: [("foo (dormant)", "foo")]
    detail = runner._describe_exit_finalize()
    assert detail is not None
    assert "committing the latest turn" in detail
    assert "foo (dormant)" in detail
    assert "Finalizing commits" not in detail  # not the old vague message


@_posix_only
def test_handle_exit_signal_ignored_while_update_applying():
    # A terminal-close SIGHUP must NOT tear aGiTrack down mid-self-update: unwinding
    # would abort the pip apply and could leave aGiTrack half-uninstalled.
    import signal as signal_mod

    runner = make_runner()
    runner._update_applying = True
    torn = []
    runner._finalize_pending_work = lambda: torn.append("finalize")
    runner._cleanup_child = lambda: torn.append("cleanup")
    runner._restore_terminal = lambda: torn.append("restore")

    runner._handle_exit_signal(signal_mod.SIGHUP, None)  # returns, does NOT raise SystemExit

    assert torn == [], "exit signal must be ignored while an update is applying"
    assert runner.running is True


@_posix_only
def test_handle_exit_signal_prompts_only_when_work_pending(monkeypatch):
    import signal as signal_mod

    runner = make_runner()
    runner._disable_host_terminal_modes = lambda: None
    runner._cleanup_child = lambda: None
    runner._restore_terminal = lambda: None
    runner._finalize_pending_work = lambda: None
    prompted = []
    runner._prompt_forced_exit = lambda: prompted.append(True)

    runner._had_unfinalized_work = lambda: False
    with pytest.raises(SystemExit):
        runner._handle_exit_signal(signal_mod.SIGHUP, None)
    assert prompted == [], "a clean close must exit silently"

    runner._had_unfinalized_work = lambda: True
    with pytest.raises(SystemExit):
        runner._handle_exit_signal(signal_mod.SIGHUP, None)
    assert prompted == [True], "a close mid-work must prompt the user"


def test_prompt_forced_exit_arms_reopen_on_choice(monkeypatch):
    from agitrack.proxy import host_prompt

    runner = make_runner()
    monkeypatch.setattr(host_prompt, "can_show_dialog", lambda: True)

    monkeypatch.setattr(host_prompt, "confirm_forced_exit", lambda *a, **k: host_prompt.REOPEN)
    runner._prompt_forced_exit()
    assert runner._reopen_after_exit is True

    runner._reopen_after_exit = False
    monkeypatch.setattr(host_prompt, "confirm_forced_exit", lambda *a, **k: host_prompt.QUIT)
    runner._prompt_forced_exit()
    assert runner._reopen_after_exit is False


def test_prompt_forced_exit_noop_without_gui(monkeypatch):
    from agitrack.proxy import host_prompt

    runner = make_runner()
    monkeypatch.setattr(host_prompt, "can_show_dialog", lambda: False)
    monkeypatch.setattr(
        host_prompt,
        "confirm_forced_exit",
        lambda *a, **k: pytest.fail("must not prompt without a GUI"),
    )
    runner._prompt_forced_exit()
    assert runner._reopen_after_exit is False


def test_reopen_in_new_window_builds_resume_command(monkeypatch):
    from agitrack.proxy import host_prompt

    runner = make_runner()
    runner.base_repo = types.SimpleNamespace(status_short=lambda: "", repo="/repo/root")
    captured = {}

    def fake_reopen(command, cwd):
        captured["command"] = command
        captured["cwd"] = cwd
        return True

    monkeypatch.setattr(host_prompt, "reopen_in_new_terminal", fake_reopen)
    monkeypatch.setattr("agitrack.proxy.runner.sys.argv", ["agitrack", "--backend", "claude"])
    runner._reopen_in_new_window()

    assert "-m agitrack" in captured["command"]
    assert "--backend claude" in captured["command"]
    assert captured["cwd"] == "/repo/root"


def test_reopen_in_new_terminal_runs_osascript(monkeypatch):
    from agitrack.proxy import host_prompt

    monkeypatch.setattr(host_prompt.sys, "platform", "darwin")
    monkeypatch.setattr(host_prompt.shutil, "which", lambda _name: "/usr/bin/osascript")
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(host_prompt.subprocess, "run", fake_run)
    assert host_prompt.reopen_in_new_terminal("py -m agitrack", "/repo root") is True
    script = captured["argv"][-1]
    assert 'tell application "Terminal" to do script' in script
    # The cwd is single-quoted for the inner shell; a space must not split it.
    assert "cd '/repo root' && exec py -m agitrack" in script


def test_reopen_in_new_terminal_no_op_off_macos(monkeypatch):
    from agitrack.proxy import host_prompt

    monkeypatch.setattr(host_prompt.sys, "platform", "linux")
    monkeypatch.setattr(
        host_prompt.subprocess,
        "run",
        lambda *a, **k: pytest.fail("must not run osascript off macOS"),
    )
    assert host_prompt.reopen_in_new_terminal("x", "/y") is False


def test_host_prompt_quoting_is_injection_safe():
    from agitrack.proxy import host_prompt

    # An AppleScript literal escapes embedded double quotes and backslashes.
    assert host_prompt._quote('a"b\\c') == '"a\\"b\\\\c"'
    # A shell argument single-quotes and escapes embedded single quotes.
    assert host_prompt._sh_quote("it's") == "'it'\\''s'"


def test_git_pipeline_runs_on_worker_thread():
    # The automatic git pass must run on the dedicated worker, never the main thread.
    runner = make_runner()
    runner._main_thread_ident = threading.get_ident()
    runner.master_fd = 99  # non-None so the worker actually runs a pass
    ran_on: list = []
    done = threading.Event()

    def probe():
        ran_on.append(threading.get_ident())
        done.set()

    runner._run_git_pipeline = probe
    runner._start_git_worker()
    try:
        runner._git_wake.set()
        assert done.wait(timeout=3), "the worker never ran a pipeline pass"
    finally:
        runner._stop_git_worker()
    assert ran_on, "pipeline did not run"
    assert ran_on[0] != threading.get_ident(), "git pipeline ran on the MAIN thread"
    assert runner._git_worker is None  # stop joined and cleared it


def test_timers_phase_runs_no_foreground_git():
    # The foreground commit/merge moved to the worker, so the main reactor's timers
    # phase must NOT invoke them (that is what used to block typing).
    runner = make_runner()
    runner.running = True
    runner.merge_ctx = None
    runner._base_advanced = False
    called: list = []
    runner._maybe_agent_commit = lambda: called.append("commit")
    runner._maybe_complete_agent_merge = lambda: called.append("merge")
    runner._poll_base_advanced = lambda: called.append("poll")

    def noop(*a, **k):
        return None

    for name in [
        "_flush_pending_render",
        "_flush_pending_enter",
        "_drain_modal_mailbox",
        "_resume_pending_prompt_if_ready",
        "_service_shared_resume",
        "_ensure_worktree_alive",
        "_check_base_branch_drift",
        "_service_commit_summaries",
        "_service_precompact_summary",
        "_service_background_sessions",
        "_maybe_check_for_update",
        "_maybe_apply_pending_update",
        "_service_background_share_ops",
        "_service_session_notices",
    ]:
        setattr(runner, name, noop)

    runner._reactor_timers_phase()
    assert called == [], "foreground git ran on the main thread's timers phase"


def test_worker_modal_marshaled_to_main_and_answered():
    # A dialog raised off the main thread is queued for the main thread, which
    # presents it and returns the answer to the blocked caller.
    runner = make_runner()
    runner._main_thread_ident = threading.get_ident()  # this test thread acts as 'main'
    modal = object()
    box: dict = {}

    def worker():
        box["ret"] = runner._run_modal(modal)  # off-main → marshals + blocks

    t = threading.Thread(target=worker)
    t.start()
    request = runner._modal_mailbox.get(timeout=3)  # the worker enqueued it
    assert request.modal is modal
    assert not request.done.is_set(), "worker should still be blocked"
    assert t.is_alive()
    # Main presents the dialog and hands the answer back.
    request.result = "answer"
    request.done.set()
    t.join(timeout=3)
    assert box["ret"] == "answer"


def test_drain_modal_mailbox_presents_on_main():
    runner = make_runner()
    runner._main_thread_ident = threading.get_ident()
    presented: list = []
    runner._run_modal_inline = lambda m: (presented.append(threading.get_ident()), "picked")[1]

    request = _make_modal_request(runner, object())
    runner._modal_mailbox.put(request)
    runner._drain_modal_mailbox()

    assert request.done.is_set()
    assert request.result == "picked"
    assert presented == [threading.get_ident()]  # ran on the main (this) thread


def test_acquire_pipeline_lock_from_main_drains_to_avoid_deadlock():
    # Worker holds the pipeline lock AND is blocked on a dialog; the main thread's
    # lock acquisition must drain that dialog so the worker can release the lock.
    runner = make_runner()
    runner._main_thread_ident = threading.get_ident()
    runner._run_modal_inline = lambda m: "resolved"  # main presents instantly
    started = threading.Event()
    worker_ret: dict = {}

    def worker():
        with runner._pipeline_lock:  # worker holds the lock...
            started.set()
            worker_ret["ret"] = runner._run_modal(object())  # ...and blocks on a dialog

    t = threading.Thread(target=worker)
    t.start()
    assert started.wait(timeout=3)
    # Without draining this would deadlock; the helper drains while waiting.
    runner._acquire_pipeline_lock_from_main()
    try:
        assert worker_ret["ret"] == "resolved"  # the worker's dialog was serviced
    finally:
        runner._pipeline_lock.release()
    t.join(timeout=3)
    assert not t.is_alive()


def test_stop_git_worker_cancels_blocked_dialog():
    # Teardown must not hang on a worker blocked waiting for a dialog answer.
    runner = make_runner()
    runner._main_thread_ident = threading.get_ident()
    runner.master_fd = 99
    blocked = threading.Event()
    answer: dict = {}

    def pipeline():
        blocked.set()
        answer["ret"] = runner._run_modal(object())  # blocks until cancelled

    runner._run_git_pipeline = pipeline
    runner._start_git_worker()
    runner._git_wake.set()
    assert blocked.wait(timeout=3)
    runner._stop_git_worker()  # cancels the pending dialog so the worker can exit
    assert runner._git_worker is None
    assert answer["ret"] is None  # cancelled dialogs resolve to None


def _make_modal_request(runner, modal):
    from agitrack.proxy.runner import _ModalRequest

    return _ModalRequest(modal)


def test_confirm_exit_prompts_when_managing(monkeypatch):
    runner = make_runner()
    seen = {}

    def fake_popup(title, options, **k):
        seen["options"] = options
        return options[1]  # "Yes, exit (Ctrl-C again)"

    monkeypatch.setattr(runner, "_select_popup", fake_popup)
    assert runner._confirm_exit() is True
    # The exit option advertises the double-Ctrl-C confirm shortcut.
    assert seen["options"][1] == "Yes, exit (Ctrl-C again)"
    monkeypatch.setattr(runner, "_select_popup", lambda *a, **k: "No, keep working")
    assert runner._confirm_exit() is False
    monkeypatch.setattr(runner, "_select_popup", lambda *a, **k: None)  # cancelled
    assert runner._confirm_exit() is False


def test_exit_always_confirms_even_when_nothing_pending():
    # Exit is a deliberate safety net: it asks first regardless of pending work.
    runner = make_runner()
    runner._had_unfinalized_work = lambda: False  # clean tree, everything merged
    events = []
    runner._confirm_exit = lambda: events.append("confirm") or True
    runner._confirm_terminate_background_sessions = lambda: True
    runner._finalize_pending_work = lambda: events.append("finalize")
    runner._exit_child = lambda: events.append("exit")

    assert runner._run_exit_flow() is True
    assert events == ["confirm", "finalize", "exit"], "exit must confirm even when clean"


def test_exit_confirm_declined_keeps_running():
    runner = make_runner()
    events = []
    runner._confirm_exit = lambda: False  # user declines the exit prompt
    runner._finalize_pending_work = lambda: events.append("finalize")
    runner._exit_child = lambda: events.append("exit")
    runner._render = lambda: None

    assert runner._run_exit_flow() is False
    assert events == [], "declining the exit prompt leaves aGiTrack running"


def test_proxy_passthrough_prompt_drops_escape_sequences():
    runner = make_runner(
        passthrough_prompt=bytearray(),
        passthrough_escape=None,
    )

    # "fix" + down-arrow (ESC [ B) + " bug" must capture only the typed text.
    runner._update_passthrough_prompt([b"f", b"i", b"x", b"\x1b", b"[", b"B", b" ", b"b", b"u", b"g"])
    assert runner.passthrough_prompt.decode() == "fix bug"


def test_proxy_passthrough_prompt_handles_escape_split_across_reads():
    runner = make_runner(
        passthrough_prompt=bytearray(),
        passthrough_escape=None,
    )

    runner._update_passthrough_prompt([b"a", b"\x1b"])
    runner._update_passthrough_prompt([b"[", b"A", b"b"])  # up-arrow split, then 'b'
    assert runner.passthrough_prompt.decode() == "ab"


def test_proxy_parses_host_terminal_responses():
    runner = make_runner(
        host_fg_value=None,
        host_bg_value=None,
        host_da=None,
        host_palette={},
        debug_proxy=False,
    )

    runner._parse_host_terminal_responses(
        b"\x1b]10;rgb:1a1a/1a1a/1a1a\x07\x1b]11;rgb:fafa/fafa/fafa\x07\x1b]4;1;rgb:cccc/0000/0000\x07\x1b[?62;c"
    )

    assert runner.host_fg_value == b"rgb:1a1a/1a1a/1a1a"
    assert runner.host_bg_value == b"rgb:fafa/fafa/fafa"
    assert runner.host_palette == {b"1": b"rgb:cccc/0000/0000"}
    assert runner.host_da == b"\x1b[?62;c"
    assert runner.host_kitty_keyboard is False  # no CSI ? u reply in this data


def test_proxy_detects_kitty_keyboard_support_from_reply():
    runner = make_runner(debug_proxy=False)
    # A ``CSI ? <flags> u`` reply (here flags=1) marks kitty-keyboard support;
    # it must not be mistaken for the DA reply (which ends in ``c``).
    runner._parse_host_terminal_responses(b"\x1b[?1u\x1b[?62;c")
    assert runner.host_kitty_keyboard is True
    assert runner.host_da == b"\x1b[?62;c"


def test_proxy_answers_terminal_queries_from_host_cache(monkeypatch):
    runner = make_runner(
        master_fd=99,
        rows=30,
        cols=100,
        host_fg_value=b"rgb:1a1a/1a1a/1a1a",
        host_bg_value=b"rgb:fafa/fafa/fafa",
        host_palette={b"1": b"rgb:cccc/0000/0000"},
        host_da=b"\x1b[?62;c",
    )

    class Cursor:
        x = 4
        y = 2

    class Screen:
        cursor = Cursor()

    runner.screen = Screen()

    written = []
    real_write = os.write

    def fake_write(fd, data):
        if fd == 99:
            written.append(data)
            return len(data)
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", fake_write)
    runner._answer_terminal_queries(b"\x1b]10;?\x07\x1b]11;?\x07\x1b]4;1;?\x07\x1b[6n\x1b[0c")

    reply = b"".join(written)
    # OpenCode learns the real terminal colors, so it picks the matching theme.
    assert b"\x1b]10;rgb:1a1a/1a1a/1a1a\x07" in reply
    assert b"\x1b]11;rgb:fafa/fafa/fafa\x07" in reply
    assert b"\x1b]4;1;rgb:cccc/0000/0000\x07" in reply
    assert b"\x1b[3;5R" in reply  # cursor position report (1-based)
    assert b"\x1b[?62;c" in reply  # device attributes


def test_proxy_answers_nothing_without_host_values(monkeypatch):
    runner = make_runner(
        master_fd=99,
        rows=30,
        cols=100,
        host_fg_value=None,
        host_bg_value=None,
        host_da=None,
        host_palette={},
        screen=None,
    )

    written = []
    real_write = os.write

    def fake_write(fd, data):
        if fd == 99:
            written.append(data)
            return len(data)
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", fake_write)
    runner._answer_terminal_queries(b"\x1b]10;?\x07\x1b]11;?\x07")

    assert written == []


def test_proxy_status_check_runs_after_file_event_only(monkeypatch):
    # The `git status` read is DEFERRED out of the active edit window: it only fires
    # once the worktree (FILE_STABLE_SECONDS) and backend (CHILD_IDLE_SECONDS) have
    # gone quiet — never on every keystroke-adjacent file event. Drive a fixed clock.
    clock = [10_000.0]
    monkeypatch.setattr("agitrack.proxy.runner.time.monotonic", lambda: clock[0])
    runner = make_runner(
        file_change_event=threading.Event(),
        status_check_pending=False,
        last_poll=0.0,
        agent_in_flight=False,
        agent_parse_thread=None,
        agent_parse_result=None,
        last_child_output=0.0,
        last_status="",
        last_status_change=0.0,
        _last_change_at=0.0,
        verbose=False,
    )
    runner.CHILD_IDLE_SECONDS = 4.0
    runner.FILE_STABLE_SECONDS = 8.0
    runner._prune_declined_untracked = lambda: None
    runner._commit_available_agent_turns = lambda quiet: False
    # Keep this test focused on the status-read count; stub the downstream commit path.
    runner._finish_agent_parse_if_ready = lambda **k: None
    runner._start_agent_parse = lambda: False
    runner._maybe_offer_copy_when_idle = lambda now: None

    class Repo:
        calls = 0

        def status_short(self):
            self.calls += 1
            return " M file.txt\n"

    runner.repo = Repo()
    runner.file_change_event.set()

    # Event just fired: the worktree hasn't settled, so no git read yet.
    runner._maybe_agent_commit()
    assert runner.repo.calls == 0

    # Past the stable + idle windows: a single read serves the (deferred) commit.
    clock[0] += runner.FILE_STABLE_SECONDS + 1
    runner._maybe_agent_commit()
    assert runner.repo.calls == 1

    # No new file event → no further reads (cached status is reused).
    runner._maybe_agent_commit()
    assert runner.repo.calls == 1


def test_proxy_parse_starts_only_after_cooldown_between_file_events(monkeypatch):
    # Pin the clock: the cooldown is measured as now - last_parse_finish, and
    # last_parse_finish starts at 0.0. With the real monotonic clock this test
    # only passes when uptime exceeds the 60s cooldown (true on a dev box, false
    # on a freshly-booted CI runner) — so drive a fixed clock instead.
    clock = [10_000.0]
    monkeypatch.setattr("agitrack.proxy.runner.time.monotonic", lambda: clock[0])
    runner = make_runner(
        file_change_event=threading.Event(),
        status_check_pending=False,
        parse_pending=False,
        last_poll=0.0,
        agent_in_flight=False,
        agent_parse_thread=None,
        agent_parse_result=None,
        agent_parse_active=False,
        last_child_output=0.0,
        last_status="",
        last_status_change=0.0,
        last_parse_start=0.0,
        last_parse_finish=0.0,
        last_parse_attempt_status="",
        verbose=False,
    )
    runner.CHILD_IDLE_SECONDS = 0.0
    runner.FILE_STABLE_SECONDS = 0.0
    runner.PARSE_COOLDOWN_SECONDS = 60.0
    runner._prune_declined_untracked = lambda: None

    class Repo:
        def status_short(self):
            return " M file.txt\n"

    starts = []
    runner.repo = Repo()

    def start_parse():
        runner.last_parse_start = clock[0]
        runner.last_parse_finish = clock[0]  # use the pinned clock, not real time
        starts.append(True)
        return True

    runner._start_agent_parse = start_parse

    runner.file_change_event.set()
    runner._maybe_agent_commit()
    runner.file_change_event.set()
    runner._maybe_agent_commit()

    assert len(starts) == 1


def test_proxy_parse_cooldown_starts_after_parse_finish():
    runner = make_runner(
        file_change_event=threading.Event(),
        status_check_pending=False,
        parse_pending=False,
        last_poll=0.0,
        agent_in_flight=False,
        agent_parse_thread=None,
        agent_parse_result=None,
        agent_parse_active=False,
        last_child_output=0.0,
        last_status="",
        last_status_change=0.0,
        last_parse_start=0.0,
        last_parse_finish=time.monotonic(),
        last_parse_attempt_status="",
        verbose=False,
    )
    runner.CHILD_IDLE_SECONDS = 0.0
    runner.FILE_STABLE_SECONDS = 0.0
    runner.PARSE_COOLDOWN_SECONDS = 60.0
    runner._prune_declined_untracked = lambda: None

    class Repo:
        def status_short(self):
            return " M file.txt\n"

    starts = []
    runner.repo = Repo()
    runner._start_agent_parse = lambda: starts.append(True) or True

    runner.file_change_event.set()
    runner._maybe_agent_commit()

    assert starts == []


def test_proxy_start_agent_parse_rejects_active_parse(tmp_path):
    runner = make_runner(
        repo=type("Repo", (), {"repo": tmp_path})(),
        state=AgitrackState(tmp_path),
        agent_parse_thread=None,
        agent_parse_result=None,
        agent_parse_active=True,
    )

    assert runner._start_agent_parse() is False


def test_proxy_sanitizes_raw_opencode_event_agent_trace(tmp_path):
    runner = make_runner(
        repo=type("Repo", (), {"repo": tmp_path})(),
        state=AgitrackState(tmp_path),
        backend=make_proxy_agent("opencode"),
        debug_proxy=False,
    )
    runner.state.append_trace("user", "hi")
    runner.state.append_trace(
        "agent",
        "\n".join(
            [
                '{"type":"step_start","sessionID":"ses-1","part":{"type":"step-start"}}',
                '{"type":"text","sessionID":"ses-1","part":{"type":"text","text":"Hi."}}',
            ]
        ),
    )

    runner._sanitize_state_trace()

    assert runner.state.pending_trace() == [{"role": "user", "content": "hi"}]


def test_proxy_pending_prompt_forwards_after_agent_parse_commit(tmp_path):
    read_fd, write_fd = os.pipe()
    try:
        runner = make_runner(
            master_fd=write_fd,
            pending_forwarded=[b"\r"],
            pending_prompt_text="fix it",
            passthrough_prompt=bytearray(b"fix it"),
            state=AgitrackState(tmp_path),
            agent_parse_thread=None,
            agent_in_flight=False,
            message="waiting",
            message_until=1.0,
        )
        runner._finish_agent_parse_if_ready = lambda quiet: True

        runner._resume_pending_prompt_if_ready()

        assert os.read(read_fd, 1) == b"\r"
        assert runner.pending_forwarded is None
        assert runner.agent_in_flight is True
        assert runner.state.pending_trace() == [{"role": "user", "content": "fix it"}]
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_proxy_pending_prompt_forwards_when_agent_mid_turn(tmp_path):
    # The parse deferred (latest turn still in progress) so its result was
    # consumed and _finish_agent_parse_if_ready now returns None with no live
    # thread. The follow-up must be forwarded (queued by the backend), not held
    # behind the "checking" message forever.
    read_fd, write_fd = os.pipe()
    try:
        runner = make_runner(
            master_fd=write_fd,
            pending_forwarded=[b"\r"],
            pending_prompt_text="and also rename it",
            passthrough_prompt=bytearray(b"and also rename it"),
            state=AgitrackState(tmp_path),
            agent_parse_thread=None,
            agent_in_flight=False,
            message=None,
            message_until=0.0,
        )
        runner._finish_agent_parse_if_ready = lambda quiet: None
        runner._ensure_turn_branch = lambda: None

        runner._resume_pending_prompt_if_ready()

        assert os.read(read_fd, 1) == b"\r"
        assert runner.pending_forwarded is None
        assert runner.agent_in_flight is True
        assert runner.state.pending_trace() == [{"role": "user", "content": "and also rename it"}]
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_proxy_pending_prompt_user_commit_then_forwards(tmp_path):
    read_fd, write_fd = os.pipe()
    try:
        runner = make_runner(
            master_fd=write_fd,
            pending_forwarded=[b"\r"],
            pending_prompt_text="fix it",
            passthrough_prompt=bytearray(b"fix it"),
            state=AgitrackState(tmp_path),
            agent_parse_thread=None,
            agent_in_flight=False,
            screen=None,
            message=None,
            message_until=0.0,
        )
        runner._finish_agent_parse_if_ready = lambda quiet: False
        runner._create_user_commit_popup = lambda: True

        class Actions:
            def has_pre_agent_user_changes(self):
                return True

        runner.actions = Actions()

        runner._resume_pending_prompt_if_ready()

        assert os.read(read_fd, 1) == b"\r"
        assert runner.pending_forwarded is None
        assert runner.agent_in_flight is True
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_new_turn_clears_previous_created_notice(tmp_path):
    # A new prompt must drop the prior turn's "created & merged" status line, so
    # the user never sees the previous commit's "created" message lead into the
    # new turn's "summarizing…".
    read_fd, write_fd = os.pipe()
    try:
        runner = make_runner(
            master_fd=write_fd,
            pending_forwarded=[b"\r"],
            pending_prompt_text="next thing",
            passthrough_prompt=bytearray(b"next thing"),
            state=AgitrackState(tmp_path),
            name="session-1",
            agent_parse_thread=None,
            agent_in_flight=False,
            screen=None,
            message=None,
            message_until=0.0,
        )
        runner._record_user_prompt = lambda text: None
        runner._ensure_turn_branch = lambda: None
        # The previous turn left a "created & merged" notice for this session.
        runner._set_session_notice(
            "session-1", "Created <aGiTrack> commit abc1234 in session 'session-1' — merged into main."
        )
        assert runner.message is not None

        runner._forward_pending_prompt()

        assert os.read(read_fd, 1) == b"\r"
        # The stale notice is gone from both the popup and the registry, so the
        # notice service tick can't repaint it.
        assert "session-1" not in runner._session_notices
        runner._service_session_notices()
        assert runner.message is None
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_proxy_pending_prompt_cancelled_user_commit_does_not_forward(tmp_path):
    read_fd, write_fd = os.pipe()
    try:
        os.set_blocking(read_fd, False)
        runner = make_runner(
            master_fd=write_fd,
            pending_forwarded=[b"\r"],
            pending_prompt_text="fix it",
            passthrough_prompt=bytearray(b"fix it"),
            state=AgitrackState(tmp_path),
            agent_parse_thread=None,
            agent_in_flight=False,
            screen=None,
            message=None,
            message_until=0.0,
        )
        runner._finish_agent_parse_if_ready = lambda quiet: False
        runner._create_user_commit_popup = lambda: False

        class Actions:
            def has_pre_agent_user_changes(self):
                return True

        runner.actions = Actions()

        runner._resume_pending_prompt_if_ready()

        try:
            written = os.read(read_fd, 1)
        except BlockingIOError:
            written = b""
        assert written == b""
        assert runner.pending_forwarded is None
        assert runner.agent_in_flight is False
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_proxy_agent_active_does_not_depend_on_recent_output():
    runner = make_runner(
        agent_in_flight=False,
        agent_parse_thread=None,
        last_child_output=999999999.0,
    )

    assert runner._agent_is_active() is False

    runner.agent_in_flight = True
    assert runner._agent_is_active() is True


def test_proxy_clears_stale_agent_in_flight_when_idle():
    runner = make_runner(
        agent_in_flight=True,
        last_child_output=0.0,
    )
    runner.CHILD_IDLE_SECONDS = 4.0

    runner._clear_agent_in_flight_if_idle()

    assert runner.agent_in_flight is False


def _integration_runner(merge_ok):
    class FakeRepo:
        def __init__(self):
            self.aborted = False

        def current_branch(self):
            return "agit/session-1/t1"

        def merge(self, ref):
            return merge_ok

        def merge_abort(self):
            self.aborted = True

    runner = make_runner(
        worktree=object(),
        _base_branch="main",
        merge_ctx=None,
        name="session-1",
        repo=FakeRepo(),
    )
    runner._exiting = False
    runner._debug = lambda *a, **k: None
    return runner


def test_integrate_conflict_aborts_and_prompts_resolve_options():
    runner = _integration_runner(merge_ok=False)
    calls = []
    runner._prompt_resolve_conflict = lambda src: calls.append(src)
    runner._advance_base_to = lambda src: calls.append(("advance", src))

    runner._integrate_session_turn()

    assert runner.repo.aborted is True  # conflicted merge is backed out
    assert calls == ["agit/session-1/t1"]  # options box is surfaced


def test_integrate_clean_merge_advances_base_without_prompt():
    runner = _integration_runner(merge_ok=True)
    calls = []
    runner._prompt_resolve_conflict = lambda src: calls.append(("prompt", src))
    runner._advance_base_to = lambda src: calls.append(("advance", src))

    runner._integrate_session_turn()

    assert runner.repo.aborted is False
    assert calls == [("advance", "agit/session-1/t1")]


def test_created_notice_fires_only_after_merge_into_base():
    # The deferred "created" announcement appears when (and only when) the turn
    # merges into the base — never at commit time.
    runner = _integration_runner(merge_ok=True)
    runner._advance_base_to = lambda src: None
    runner._last_agent_commit_id = "abc1234"
    runner._commit_merged_pending = True  # armed by the commit

    result = runner._integrate_turn_or_conflict()

    assert result == "integrated"
    assert runner.message == "Created <aGiTrack> commit abc1234 in session 'session-1' — merged into main."
    assert runner._commit_merged_pending is False  # consumed, won't repeat


def test_no_created_notice_when_nothing_pending():
    # Integrating with no just-made commit (e.g. a manual integrate of older
    # work) stays silent rather than claiming a commit was created.
    runner = _integration_runner(merge_ok=True)
    runner._advance_base_to = lambda src: None
    runner._commit_merged_pending = False

    assert runner._integrate_turn_or_conflict() == "integrated"
    assert runner.message is None


def test_conflict_does_not_fire_created_notice():
    runner = _integration_runner(merge_ok=False)
    runner._prompt_resolve_conflict = lambda src: None
    runner._commit_merged_pending = True

    assert runner._integrate_turn_or_conflict() == "conflict"
    assert runner.message is None  # not merged → not announced
    assert runner._commit_merged_pending is True  # still armed for a later retry


def test_integrate_conflict_on_exit_leaves_for_startup():
    runner = _integration_runner(merge_ok=False)
    runner._exiting = True
    prompted = []
    runner._prompt_resolve_conflict = lambda src: prompted.append(src)
    runner._advance_base_to = lambda src: None

    runner._integrate_session_turn()

    assert runner.repo.aborted is True  # merge is aborted
    assert prompted == []  # but no UI on exit


def _resolve_runner(choice_index):
    import types

    runner = make_runner(
        name="session-1",
        _base_branch="main",
        backend=types.SimpleNamespace(name="opencode"),
    )
    runner._render = lambda: None
    msgs = []
    runner._set_message = lambda *a, **k: msgs.append(a[0] if a else "")
    runner._messages = msgs
    runner._select_popup = lambda title, options: options[choice_index]
    runner._dispatched = []
    runner._start_merge_for_active = lambda *, auto: runner._dispatched.append(auto)
    return runner


def test_prompt_resolve_conflict_dispatches_auto():
    runner = _resolve_runner(choice_index=0)
    runner._prompt_resolve_conflict("agit/session-1/t1")
    assert runner._dispatched == [True]


def test_prompt_resolve_conflict_dispatches_manual():
    runner = _resolve_runner(choice_index=1)
    runner._prompt_resolve_conflict("agit/session-1/t1")
    assert runner._dispatched == [False]


def test_prompt_resolve_conflict_leave_does_not_merge():
    runner = _resolve_runner(choice_index=2)
    runner._prompt_resolve_conflict("agit/session-1/t1")
    assert runner._dispatched == []  # nothing merged
    assert runner._messages and "unintegrated" in runner._messages[0]


# --- cross-backend sessions (Part B) ---


def test_live_session_for_backend_finds_active_and_background():
    import types

    runner = _mux_runner()
    runner.backend = types.SimpleNamespace(name="claude")  # active
    b = _bg_session("B")
    b.backend = types.SimpleNamespace(name="opencode")
    runner.sessions.append(b)

    assert runner._live_session_for_backend("claude") == 0
    assert runner._live_session_for_backend("opencode") == 1
    assert runner._live_session_for_backend("gemini") is None


def _backend_switch_runner(monkeypatch):
    import types

    runner = _mux_runner()
    runner.backend = types.SimpleNamespace(name="claude")
    runner.worktree = object()
    # A switch records the choice repo-scoped via global_config.set(...); capture it on .sets.
    cfg = types.SimpleNamespace(default_backend="claude", sets=[])
    cfg.set = lambda key, value, *, scope: cfg.sets.append((key, value, scope))
    runner.global_config = cfg
    monkeypatch.setattr("agitrack.proxy.runner.backend_installed", lambda n: True)
    return runner


def test_switch_backend_switches_to_live_session_without_teardown(monkeypatch):
    import types

    runner = _backend_switch_runner(monkeypatch)
    b = _bg_session("B")
    b.backend = types.SimpleNamespace(name="opencode")
    runner.sessions.append(b)
    calls = []
    runner._switch_active = lambda i: calls.append(("switch", i))
    runner._new_session = lambda *a, **k: calls.append(("new", a, k))

    runner._switch_backend("opencode")

    assert calls == [("switch", 1)]  # resumed the live opencode session, no respawn


def test_switch_backend_creates_per_backend_session_when_none_live(monkeypatch):
    runner = _backend_switch_runner(monkeypatch)
    runner._next_session_name = lambda: "session-2"
    prompts = []

    def fake_prompt(title, prompt, *, default=""):
        prompts.append((title, default))
        return default  # user accepts the prefilled name

    runner._prompt_popup = fake_prompt
    calls = []
    runner._new_session = lambda name, **k: calls.append((name, k))
    runner._switch_active = lambda i: calls.append(("switch", i))

    runner._switch_backend("opencode")

    # The name popup is prefilled with the next session-N; the backend is pinned.
    assert prompts == [("New opencode session", "session-2")]
    assert calls == [("session-2", {"backend": "opencode"})]


def test_switch_backend_uses_edited_session_name(monkeypatch):
    runner = _backend_switch_runner(monkeypatch)
    runner._next_session_name = lambda: "session-2"
    runner._prompt_popup = lambda title, prompt, *, default="": "my-feature"  # user renames
    calls = []
    runner._new_session = lambda name, **k: calls.append((name, k))
    runner._switch_active = lambda i: calls.append(("switch", i))

    runner._switch_backend("opencode")

    assert calls == [("my-feature", {"backend": "opencode"})]


def test_switch_backend_cancelled_name_does_not_create(monkeypatch):
    runner = _backend_switch_runner(monkeypatch)
    runner._next_session_name = lambda: "session-2"
    runner._prompt_popup = lambda *a, **k: None  # user cancels the name popup
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    created = []
    runner._new_session = lambda *a, **k: created.append((a, k))

    runner._switch_backend("opencode")

    assert created == []


def test_switch_backend_noop_when_same_backend(monkeypatch):
    runner = _backend_switch_runner(monkeypatch)
    msgs = []
    runner._set_message = lambda *a, **k: msgs.append(a[0])
    runner._render = lambda: None
    called = []
    runner._switch_active = lambda i: called.append(i)
    runner._new_session = lambda *a, **k: called.append("new")

    runner._switch_backend("claude")

    assert called == [] and any("Already using" in m for m in msgs)


def test_background_integration_defers_while_its_summary_is_pending():
    # A background session waits to integrate until its own summary is ready
    # (same as the active path), so the summary lands in the message and the
    # session's "summarizing…" status line stays up while it computes.
    runner = make_runner(worktree=object(), _base_branch="main")
    runner.repo = types.SimpleNamespace(has_changes=lambda: False)
    runner._uncovered_backend_commits = lambda: []
    runner._clear_agent_in_flight_if_idle = lambda: None
    runner._summary_blocks_integration = lambda now: True
    integrated = []
    runner._integrate_turn_or_conflict = lambda: integrated.append(1) or "integrated"

    assert runner._commit_and_integrate_background() == "skip"
    assert integrated == []  # not integrated while the summary is still computing


def test_service_background_integrates_idle_session_cleanly():
    runner = _mux_runner()
    runner.merge_ctx = None
    runner.CHILD_IDLE_SECONDS = 4.0
    runner.POLL_SECONDS = 2.0
    b = _bg_session("B")
    b.agent_in_flight = True
    b.last_child_output = 0.0  # long idle
    b.last_poll = 0.0
    runner.sessions.append(b)
    calls = []
    runner._with_session = lambda session, fn: calls.append(session.name) or "integrated"
    runner._switch_active = lambda i: calls.append(("switch", i))
    runner._prompt_resolve_conflict = lambda src: calls.append(("prompt", src))

    runner._service_background_sessions()

    assert calls == ["B"]  # serviced once, clean integration, no switch/prompt
    assert b.last_poll > 0.0  # throttle timestamp advanced


def test_service_background_integrates_even_when_not_in_flight():
    # Regression: a finished background session whose in-flight flag was already
    # cleared (e.g. it committed but didn't integrate) must still be serviced —
    # otherwise its commits sit unintegrated until the user switches to it.
    runner = _mux_runner()
    runner.merge_ctx = None
    runner.CHILD_IDLE_SECONDS = 4.0
    runner.POLL_SECONDS = 2.0
    b = _bg_session("B")
    b.agent_in_flight = False  # no longer flagged, but still has work to integrate
    b.last_child_output = 0.0
    b.last_poll = 0.0
    runner.sessions.append(b)
    serviced = []
    runner._with_session = lambda session, fn: serviced.append(session.name) or "integrated"
    runner._switch_active = lambda i: serviced.append(("switch", i))
    runner._prompt_resolve_conflict = lambda src: serviced.append(("prompt", src))

    runner._service_background_sessions()

    assert serviced == ["B"]


def test_service_background_conflict_switches_and_prompts():
    runner = _mux_runner()
    runner.merge_ctx = None
    runner.CHILD_IDLE_SECONDS = 4.0
    runner.POLL_SECONDS = 2.0
    b = _bg_session("B")
    b.agent_in_flight = True
    b.last_child_output = 0.0
    b.last_poll = 0.0
    runner.sessions.append(b)
    runner._with_session = lambda session, fn: "conflict"
    switched, prompted = [], []

    class _Repo:
        def current_branch(self):
            return "agit/B/t1"

    def _switch(i):
        switched.append(i)
        runner.repo = _Repo()

    runner._switch_active = _switch
    runner._prompt_resolve_conflict = lambda src: prompted.append(src)

    runner._service_background_sessions()

    assert switched == [1] and prompted == ["agit/B/t1"]


def test_service_background_finalizes_pending_merge():
    runner = _mux_runner()
    runner.merge_ctx = None
    b = _bg_session("B")
    b.merge_ctx = MergeContext(source_branch="agit/B/t1", context="")
    runner.sessions.append(b)
    called = []
    runner._with_session = lambda session, fn: called.append((session.name, fn.__name__))

    runner._service_background_sessions()

    assert called == [("B", "_maybe_complete_agent_merge")]


def test_service_background_skips_while_active_merge_in_progress():
    runner = _mux_runner()
    runner.merge_ctx = MergeContext(source_branch="agit/A/t1", context="")  # any truthy merge_ctx
    b = _bg_session("B")
    b.agent_in_flight = True
    runner.sessions.append(b)
    called = []
    runner._with_session = lambda *a, **k: called.append(1)

    runner._service_background_sessions()

    assert called == []


def test_with_session_swaps_in_and_restores_active():
    runner = _mux_runner()  # active session name "A"
    b = _bg_session("B")
    runner.sessions.append(b)
    seen = []

    def fn():
        seen.append(runner.name)  # "B" while swapped in
        runner.last_status = "touched"  # mutation must persist to the snapshot
        return "ok"

    result = runner._with_session(b, fn)

    assert result == "ok"
    assert seen == ["B"]
    assert runner.name == "A"  # active restored
    assert b.last_status == "touched"  # background snapshot updated


def test_next_session_name_is_a_word_avoiding_taken_names():
    import types

    from agitrack.proxy.session_names import SESSION_WORDS

    runner = _mux_runner()  # one active session named "A"
    runner.worktree_manager = types.SimpleNamespace(
        list=lambda: [types.SimpleNamespace(name="maple"), types.SimpleNamespace(name="willow")]
    )
    # A neutral word that isn't one of the taken names.
    name = runner._next_session_name()
    assert name in SESSION_WORDS
    assert name not in {"maple", "willow"} | runner._taken_session_names()


def test_new_session_prompts_for_name_not_inheriting_prior(tmp_path):
    # --new-session (resume_id is None) starts a fresh conversation, so it must
    # PROMPT for a new name rather than silently inheriting the prior session's
    # worktree name.
    runner = make_runner(state=AgitrackState(tmp_path), verbose=False)
    runner._prompt_startup_name = lambda continuing: "newname"
    name = runner._resolve_startup_session_name(runner.state, None, "willow")
    assert name == "newname"


def test_resume_inherits_prior_session_name_without_prompting(tmp_path):
    # Resuming a conversation (resume_id set) keeps its prior worktree name and
    # does NOT prompt.
    runner = make_runner(state=AgitrackState(tmp_path), verbose=False)
    prompted = []
    runner._prompt_startup_name = lambda continuing: prompted.append(True) or "unused"
    name = runner._resolve_startup_session_name(runner.state, "ses-1", "willow")
    assert name == "willow"
    assert prompted == []


def test_startup_default_name_is_a_word_not_session_1():
    from agitrack.proxy.session_names import SESSION_WORDS

    runner = _mux_runner()
    # The very first session no longer defaults to the forgettable "session-1";
    # it picks a friendly random word avoiding any startup-taken name.
    runner._startup_taken_names = lambda: {"maple", "willow"}
    name = runner._startup_default_name()
    assert name in SESSION_WORDS
    assert name not in {"maple", "willow"}
    assert not name.startswith("session-")


# --- injected-prompt targeting (cross-backend safety) ---


def test_inject_prompt_records_target_fd(monkeypatch):
    runner = make_runner(master_fd=5)
    writes = []
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append((fd, data)) or len(data))

    runner._inject_prompt("resolve the conflict")

    assert writes == [(5, b"resolve the conflict")]
    assert runner._pending_enter_fd == 5
    assert runner._pending_enter_at is not None


def test_flush_pending_enter_targets_injected_fd_not_active(monkeypatch):
    runner = make_runner(
        _pending_enter_at=0.0,
        _pending_enter_fd=7,
        master_fd=99,
        merge_ctx=None,
    )
    writes = []
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append((fd, data)) or len(data))

    runner._flush_pending_enter()

    # The submit Enter goes to the injected backend (7), never the active one (99).
    assert writes == [(7, b"\r")]
    assert runner._pending_enter_fd is None


def test_flush_pending_enter_marks_sent_only_when_still_active(monkeypatch):
    monkeypatch.setattr(os, "write", lambda fd, data: len(data))

    # Same session still active -> prompt_sent_at is recorded.
    active = make_runner(
        _pending_enter_at=0.0,
        _pending_enter_fd=7,
        master_fd=7,
        merge_ctx=MergeContext(source_branch="agit/s/t1", context="", phase=MergePhase.PENDING),
    )
    active._flush_pending_enter()
    assert active.merge_ctx.prompt_sent_at is not None
    assert active.merge_ctx.phase is MergePhase.RESOLVING  # PENDING promoted to RESOLVING

    # Switched away -> the active session's merge_ctx is NOT marked.
    switched = make_runner(
        _pending_enter_at=0.0,
        _pending_enter_fd=7,
        master_fd=99,
        merge_ctx=MergeContext(source_branch="agit/s/t1", context="", phase=MergePhase.PENDING),
    )
    switched._flush_pending_enter()
    assert switched.merge_ctx.prompt_sent_at is None
    assert switched.merge_ctx.phase is MergePhase.PENDING  # not promoted (Enter went to different fd)


# --- session name uniqueness + per-backend resume ---


def test_state_remember_and_recall_session(tmp_path):
    s = AgitrackState(tmp_path)
    assert s.recall_session("opencode") is None
    s.remember_session("opencode", session_id="abc", worktree="session-2", message_id="m1", model="o4")
    assert s.recall_session("opencode") == {"id": "abc", "worktree": "session-2", "message_id": "m1", "model": "o4"}
    # Survives a reload from disk.
    assert AgitrackState(tmp_path).recall_session("opencode")["id"] == "abc"
    # Clearing (no id) forgets it.
    s.remember_session("opencode", session_id=None, worktree="session-2")
    assert s.recall_session("opencode") is None


def test_session_name_taken_detects_live_and_on_disk():
    import types

    runner = _mux_runner()  # one live session named "A"
    runner.worktree_manager = types.SimpleNamespace(list=lambda: [types.SimpleNamespace(name="session-1")])
    assert runner._session_name_taken("A") is True
    assert runner._session_name_taken("session-1") is True
    assert runner._session_name_taken("fresh") is False


def test_prompt_session_name_rejects_duplicates_until_unique():
    import types

    runner = _mux_runner()  # live session "A"
    runner.worktree_manager = types.SimpleNamespace(list=lambda: [])
    runner._next_session_name = lambda: "session-9"
    answers = iter(["A", "my-session"])  # first a duplicate, then a fresh name
    runner._prompt_popup = lambda title, prompt, *, default="": next(answers)
    assert runner._prompt_session_name("New Session", default="session-2") == "my-session"


def test_prompt_session_name_cancel_returns_none():
    import types

    runner = _mux_runner()
    runner.worktree_manager = types.SimpleNamespace(list=lambda: [])
    runner._prompt_popup = lambda *a, **k: None
    assert runner._prompt_session_name("New Session", default="session-2") is None


def test_switch_backend_resumes_stored_session(monkeypatch):
    runner = _backend_switch_runner(monkeypatch)
    runner._recall_backend_session = lambda name: {"id": "sess-9", "worktree": "session-2"}
    calls = []
    runner._new_session = lambda name, **k: calls.append((name, k))
    runner._switch_active = lambda i: calls.append(("switch", i))

    runner._switch_backend("opencode")

    # Resumes the remembered conversation in its original worktree, no name prompt.
    assert calls == [("session-2", {"backend": "opencode", "resume_session_id": "sess-9"})]


# --- resume-past-conversation naming ---


def _resume_runner():
    import types

    runner = make_runner(
        name="session-1",
    )
    s0 = types.SimpleNamespace(name="session-1", state=types.SimpleNamespace(backend_session_id="live-1"))
    runner.sessions = [s0]
    runner._switch_active = lambda i: runner.__dict__.setdefault("_switched", []).append(i)
    runner._new_session = lambda name, **k: runner.__dict__.setdefault("_created", []).append((name, k))
    return runner


def test_resume_uses_original_worktree_when_name_is_free():
    runner = _resume_runner()
    runner._next_session_name = lambda: "session-9"

    runner._resume_conversation("session-2", "past-xyz")

    # "session-2" is not a live session, so resume in its original worktree.
    assert runner.__dict__.get("_created") == [("session-2", {"resume_session_id": "past-xyz", "backend": None})]


def test_resume_uses_fresh_name_when_colliding_with_live_session():
    runner = _resume_runner()
    runner._next_session_name = lambda: "session-3"
    # A collision with the live session now PROMPTS the user (it no longer silently renames);
    # here the user accepts the suggested fresh name (the prompt's default).
    runner._prompt_session_name = lambda title, *, default: default

    # A past conversation that ran in "session-1" — but session-1 is live now.
    runner._resume_conversation("session-1", "past-xyz")

    assert runner.__dict__.get("_created") == [("session-3", {"resume_session_id": "past-xyz", "backend": None})]


def test_resume_switches_to_already_live_conversation():
    runner = _resume_runner()

    runner._resume_conversation("session-1", "live-1")  # same id as the live session

    assert runner.__dict__.get("_switched") == [0]
    assert "_created" not in runner.__dict__


# --- parallel sessions under --no-worktree (shared base directory) ---


def _no_worktree_new_session_runner(tmp_path):
    """A runner with worktrees OFF and one live session, stubbed so `_new_session`
    can be driven without spawning a real backend."""
    import types

    from agitrack.git import GitRepo

    base = GitRepo.init(tmp_path / "base")
    (tmp_path / "base" / "README.md").write_text("hi\n", encoding="utf-8")
    base.stage_paths(["README.md"])
    base.commit("seed")

    runner = make_runner(name="first")
    runner._use_worktrees = False
    runner.worktree = None
    runner.base_repo = base
    runner.repo = base
    runner.global_config = types.SimpleNamespace(default_backend="claude")
    runner.sessions = [types.SimpleNamespace(name="first")]
    events: list = []
    # Stub the heavy lifecycle steps so the test exercises only the session wiring.
    runner._warn_parallel_no_worktree_sessions = lambda: events.append("warn")
    runner._stage_backend_resume = lambda sid: events.append(("stage", sid))
    runner._initialize_session_baseline = lambda: None
    runner._sanitize_state_trace = lambda: None
    runner._init_screen = lambda: None
    runner._spawn = lambda: None
    runner._start_file_watcher = lambda: None
    runner._resize_child = lambda: None
    runner._enable_host_mouse = lambda: None
    runner._render = lambda *a, **k: None
    runner._set_message = lambda m, **k: events.append(("msg", m))
    return runner, base, events


def test_new_session_no_worktree_runs_in_base_dir_not_a_worktree(tmp_path):
    # Worktrees off: a new session runs on the base tree (worktree stays None, repo is
    # the base repo) instead of being refused — so parallel sessions are allowed.
    runner, base, events = _no_worktree_new_session_runner(tmp_path)

    runner._new_session("second")

    assert runner.worktree is None
    assert runner.repo is base
    assert len(runner.sessions) == 2  # the new session was actually started
    assert "warn" in events  # the shared-tree caveat was surfaced


def test_new_session_no_worktree_blank_starts_fresh_conversation(tmp_path):
    # A blank no-worktree session must not inherit the shared base-dir state's recorded
    # conversation id — it mints a fresh one.
    runner, base, _ = _no_worktree_new_session_runner(tmp_path)
    from agitrack.config import AgitrackState

    stale = AgitrackState(base.repo, default_backend="claude")
    stale.backend_session_id = "stale-id"  # persisted in the shared base-dir state

    runner._new_session("blank")

    assert runner.state.backend_session_id is None  # not the stale base-dir id


def test_new_session_no_worktree_resume_stages_transcript(tmp_path):
    # Resuming a (previously-worktree) conversation under --no-worktree stages its
    # transcript into the base dir, which also retargets its recorded cwd there.
    runner, _base, events = _no_worktree_new_session_runner(tmp_path)

    runner._new_session("resumed", resume_session_id="old-sid")

    assert runner.state.backend_session_id == "old-sid"
    assert ("stage", "old-sid") in events


def test_warn_parallel_no_worktree_sessions_fires_once(tmp_path):
    # The shared-tree caveat is shown only once per run.
    runner, _base, _ = _no_worktree_new_session_runner(tmp_path)
    del runner._warn_parallel_no_worktree_sessions  # use the real method
    shown: list = []
    runner._select_popup = lambda title, opts: shown.append(title) or "Got it"

    runner._warn_parallel_no_worktree_sessions()
    runner._warn_parallel_no_worktree_sessions()

    assert len(shown) == 1
    assert "share this directory" in shown[0]


# --- shared-session resume gives a local name (#71) ---


def _shared_resume_runner():
    import types

    runner = make_runner(name="main")
    runner.sessions = []
    runner._render = lambda: None
    runner._set_message = lambda *a, **k: None
    runner.base_repo = types.SimpleNamespace(repo="/repo")
    runner._taken_session_names = lambda: set()
    runner.__dict__["_origins"] = {}
    runner._user_state = lambda: types.SimpleNamespace(
        set_shared_origin=lambda sid, *, owner, name, contributors=None: runner.__dict__["_origins"].__setitem__(
            sid, {"owner": owner, "name": name, "contributors": sorted(set(contributors or []))}
        ),
        shared_origin=lambda sid: runner.__dict__["_origins"].get(sid),
    )
    entry = types.SimpleNamespace(
        github_id="alice",
        name="fix-parser",
        contributors=["alice"],
        display="alice/fix-parser",
        manifest={"session_id": "sid-1", "backend": "claude"},
    )
    store = types.SimpleNamespace(
        repo=types.SimpleNamespace(remote_exists=lambda: False),  # no remote ⇒ fetch is inline
        fetch=lambda **k: None,
        entries=lambda: [entry],
        read_transcript=lambda e, **k: "transcript-body",
    )
    runner._shared_store = lambda: store
    runner._select_popup = lambda title, options: options[0]  # pick the only entry
    runner.backend = types.SimpleNamespace(
        name="claude",
        has_local_session=lambda *a, **k: False,
        import_shared_session=lambda *a, **k: True,
    )
    runner.__dict__["_resumed"] = []
    runner._resume_conversation = lambda name, sid, **k: runner.__dict__["_resumed"].append((name, sid, k))
    return runner


def _drain_shared_resume(runner):
    # The transcript fetch + import run on a worker thread; the resume completes on
    # the main loop's _service_shared_resume(). Drain both for the test.
    if runner._shared_resume_thread is not None:
        runner._shared_resume_thread.join(timeout=10)
    runner._service_shared_resume()


def test_shared_resume_records_copy_origin_event_on_new_session(tmp_path):
    # Copying a collaborator's shared session into a NEW local session records a
    # one-shot "copy" origin event, so its first agent commit notes the inherited
    # context/tokens. (A switch to an already-live session records nothing.)
    import threading
    import types

    from agitrack.config import AgitrackState

    runner = _shared_resume_runner()
    state = AgitrackState(tmp_path)
    runner.state = state
    # A realistic resume: the new session ends up tracking the shared id.
    runner._resume_conversation = lambda name, sid, **k: setattr(state, "backend_session_id", sid)
    runner._shared_resume_cancel = threading.Event()
    runner._shared_resume_thread = None
    runner._shared_resume_result = {
        "transcript": "body",
        "action": "new",
        "agent": types.SimpleNamespace(import_shared_session=lambda *a, **k: True),
        "session_id": "sid-1",
        "name": "fix-parser",
        "resume_id": "sid-1",
        "overwrite": False,
        "as_id": None,
        "backend": "claude",
        "entry_name": "fix-parser",
        "origin_contributors": "alice+bob",
    }

    runner._service_shared_resume()

    event = state.session_origin_event()
    assert event is not None
    assert event["kind"] == "copy"
    assert event["source"] == "sid-1"
    assert event["source_name"] == "fix-parser"
    assert event["collaborator"] == "alice+bob"


def test_fork_current_session_records_fork_origin_event(tmp_path):
    # A local fork resumes a copy of the active conversation under a fresh id, so the
    # new session records a one-shot "fork" origin event naming the original.
    import types

    from agitrack.config import AgitrackState

    runner = _shared_resume_runner()
    runner.name = "original"
    src_state = AgitrackState(tmp_path / "src", default_backend="claude")
    src_state.backend_session_id = "ses_src"
    runner.state = src_state
    runner.repo = types.SimpleNamespace(repo="/wt")
    runner.base_repo = types.SimpleNamespace(repo="/repo")
    runner.backend = types.SimpleNamespace(
        supports_session_sharing=True,
        export_session_raw=lambda repo, sid: "transcript-body",
        new_import_id=lambda: "ses_fork",
        new_session_id=lambda: "ses_fork",
        import_shared_session=lambda *a, **k: True,
    )

    new_state = AgitrackState(tmp_path / "new")

    def fake_new_session(name, *, resume_session_id=None, backend=None, base_branch=None):
        new_state.backend_session_id = resume_session_id
        runner.state = new_state

    runner._new_session = fake_new_session

    assert runner._fork_current_session("forked") is True
    event = new_state.session_origin_event()
    assert event is not None
    assert event["kind"] == "fork"
    assert event["source"] == "ses_src"
    assert event["source_name"] == "original"
    # The original session's state is untouched by the fork.
    assert src_state.session_origin_event() is None


def test_resume_shared_menu_stopped_fetch_quits_without_listing():
    # If the user stops the listing fetch (Esc), the menu must NOT fall through to a
    # possibly-stale previously-fetched list — it leaves the menu entirely.
    runner = _shared_resume_runner()
    runner._fetch_shared_with_cancel = lambda store, message: False  # user stopped it
    picks: list = []
    runner._select_popup = lambda *a, **k: picks.append(a) or None

    runner._resume_shared_session_menu()

    assert picks == []  # no session list was shown
    assert runner._shared_resume_thread is None  # and no transcript fetch began


def test_service_shared_resume_drops_result_when_cancelled():
    # A cancelled (or exit-time) fetch must never complete a switch, even if its
    # worker already left a result behind.
    import threading

    runner = _shared_resume_runner()
    runner._shared_resume_cancel = threading.Event()
    runner._shared_resume_cancel.set()
    runner._shared_resume_result = {"action": "import", "name": "x", "session_id": "sid-1"}
    runner._shared_resume_thread = None

    runner._service_shared_resume()

    assert runner._shared_resume_result is None  # dropped
    assert runner.__dict__["_resumed"] == []  # no resume happened


def test_cancel_inflight_shared_fetches_signals_and_clears():
    import threading

    runner = _shared_resume_runner()
    event = threading.Event()
    runner._shared_resume_cancel = event
    runner._shared_resume_result = {"action": "import"}

    runner._cancel_inflight_shared_fetches()

    assert event.is_set()  # the worker is told to stop
    assert runner._shared_resume_result is None  # and any pending result is dropped


def _failing_resume_runner(read_transcript):
    import types

    runner = _shared_resume_runner()
    runner._prompt_session_name = lambda *a, **k: "my-copy"
    old = runner._shared_store()
    failing = types.SimpleNamespace(
        repo=old.repo, fetch=old.fetch, entries=old.entries, read_transcript=read_transcript
    )
    runner._shared_store = lambda: failing
    notices: list = []
    runner._await_keypress = lambda msg: notices.append(msg)
    return runner, notices


def test_shared_resume_incomplete_reports_reason_not_cancelled():
    # A failed full-session fetch (empty transcript ⇒ incomplete) must say WHY via a
    # persistent notice — never be reported as a "cancelled" message — and must clear
    # all fetch state so the user can retry immediately.
    runner, notices = _failing_resume_runner(lambda e, **k: None)

    runner._resume_shared_session_menu()

    assert any("Couldn't fetch" in m and "incomplete" in m for m in notices)
    assert all("cancel" not in m.lower() for m in notices)  # a failure, not a cancel
    assert runner.__dict__["_resumed"] == []  # nothing resumed
    # Timers/state cleared so a retry can start at once.
    assert runner._shared_resume_cancel is None
    assert runner._shared_resume_result is None
    assert runner._shared_resume_thread is None


def test_shared_resume_fetch_error_reports_reason():
    # A raised fetch error surfaces its reason (not a cancel) and clears state.
    def boom(entry, **kwargs):
        raise RuntimeError("network unreachable")

    runner, notices = _failing_resume_runner(boom)

    runner._resume_shared_session_menu()

    assert any("Couldn't fetch" in m and "network unreachable" in m for m in notices)
    assert all("cancel" not in m.lower() for m in notices)
    assert runner._shared_resume_cancel is None  # cleared for an immediate retry


def test_stdin_has_cancel_only_for_lone_esc_or_ctrl_c():
    runner = _shared_resume_runner()
    # Genuine cancels.
    assert runner._stdin_has_cancel(b"\x1b") is True  # a bare Esc keypress
    assert runner._stdin_has_cancel(b"\x03") is True  # Ctrl-C
    assert runner._stdin_has_cancel(b"abc\x03") is True
    # Escape SEQUENCES (begin with ESC) must NOT count as a cancel — this is the
    # mouse-move-cancels-the-fetch bug: host mouse reporting emits these constantly.
    assert runner._stdin_has_cancel(b"\x1b[<35;10;20M") is False  # SGR mouse move
    assert runner._stdin_has_cancel(b"\x1b[A") is False  # up arrow
    assert runner._stdin_has_cancel(b"\x1b[I") is False  # focus-in
    assert runner._stdin_has_cancel(b"\x1b[200~hi\x1b[201~") is False  # bracketed paste
    assert runner._stdin_has_cancel(b"x") is False  # ordinary key


def test_is_real_keypress_ignores_mouse_and_focus():
    runner = _shared_resume_runner()
    # Mouse reports and focus events are not keystrokes.
    assert runner._is_real_keypress(b"\x1b[<35;10;20M") is False
    assert runner._is_real_keypress(b"\x1b[I") is False
    assert runner._is_real_keypress(b"\x1b[<0;5;5M\x1b[O") is False
    # Real keys (including arrows and Esc) dismiss a "press any key" notice.
    assert runner._is_real_keypress(b"q") is True
    assert runner._is_real_keypress(b"\r") is True
    assert runner._is_real_keypress(b"\x1b") is True
    assert runner._is_real_keypress(b"\x1b[A") is True  # an arrow key is still a key
    # A mouse move bundled with a real key still counts as a key.
    assert runner._is_real_keypress(b"\x1b[<35;1;1Mx") is True
    # Mouse SCROLL wheel reports (SGR buttons 64=up / 65=down) are not keystrokes
    # either: scrolling history must not reset the idle backoff (see _reactor_stdin_phase).
    assert runner._is_real_keypress(b"\x1b[<64;10;20M") is False
    assert runner._is_real_keypress(b"\x1b[<65;10;20M") is False


def test_mouse_scroll_does_not_reset_idle_backoff():
    import time

    # A runner quiet long enough to be idle stays idle through a mouse-scroll burst
    # (passive reading) but flips active the instant a real key arrives. This mirrors
    # the gate in _reactor_stdin_phase: only _is_real_keypress(data) touches
    # last_user_input, so scrolling never pins aGiTrack in the 1s active loop.
    runner = make_runner()
    runner.IDLE_AFTER_SECONDS = 30.0
    old = time.monotonic() - 100
    runner.last_user_input = old
    runner.last_child_output = old
    assert runner._is_idle() is True

    scroll = b"\x1b[<65;10;20M"  # wheel-down report
    if not runner._is_real_keypress(scroll):
        pass  # would NOT update last_user_input
    assert runner._is_idle() is True  # still idle after scrolling

    key = b"a"
    if runner._is_real_keypress(key):
        runner.last_user_input = time.monotonic()  # a real key wakes the active loop
    assert runner._is_idle() is False


def test_timers_phase_noops_when_not_running():
    # After a menu "update" (or exit) finalizes and REMOVES the worktree, the timers
    # phase must touch nothing — a stale call would run git in the deleted worktree
    # and raise FileNotFoundError. The loop also breaks before reaching here.
    from proxy_helpers import make_runner

    runner = make_runner()
    runner.running = False
    touched: list = []
    runner._flush_pending_render = lambda: touched.append("render")
    runner._ensure_worktree_alive = lambda: touched.append("alive")
    runner._maybe_agent_commit = lambda: touched.append("commit")

    runner._reactor_timers_phase()

    assert touched == []  # nothing ran on the torn-down session


def test_timers_phase_stops_after_pending_update_teardown():
    # A deferred update can apply mid-phase (sessions just went idle), finalizing and
    # removing the worktree; the post-update tail must then be skipped.
    from proxy_helpers import make_runner

    runner = make_runner()
    runner.running = True
    runner.merge_ctx = None
    runner._base_advanced = True
    tail: list = []
    runner._service_background_share_ops = lambda: tail.append("share")
    runner._service_session_notices = lambda: tail.append("notices")

    def noop(*a, **k):
        return None

    for name in [
        "_flush_pending_render",
        "_flush_pending_enter",
        "_drain_modal_mailbox",
        "_check_base_branch_drift",
        "_resume_pending_prompt_if_ready",
        "_ensure_worktree_alive",
        "_service_commit_summaries",
        "_service_precompact_summary",
        "_service_shared_resume",
        "_service_background_sessions",
        "_sync_idle_worktrees_to_base",
        "_maybe_check_for_update",
    ]:
        setattr(runner, name, noop)

    def apply_pending():
        runner.running = False  # finalize removed the worktree and stopped the loop

    runner._maybe_apply_pending_update = apply_pending
    runner._reactor_timers_phase()

    assert tail == []  # the post-update tail (share ops, notices) was skipped after teardown


def test_ensure_worktree_alive_recreates_externally_deleted_worktree(tmp_path):
    # If a live session's worktree directory disappears out from under it (an external
    # delete — a backup/indexer/cloud-sync, or a stray `git worktree` elsewhere), the
    # recovery must recreate it at the SAME path, keep it a worktree session (not the
    # base-tree fallback), and respawn the backend there — never silently keep running
    # git in a gone directory. (Pins the path the agent's "worktree re-synced (cwd
    # reset)" narration came from.)
    import shutil

    from agitrack.git import GitRepo
    from agitrack.git.worktree import WorktreeManager

    base = GitRepo.init(tmp_path / "base")
    (tmp_path / "base" / "seed.txt").write_text("seed\n", encoding="utf-8")
    base.stage_paths(["seed.txt"])
    base.commit("seed")
    manager = WorktreeManager(base)
    info = manager.create("session-1", base=base.current_branch())

    runner = make_runner(
        name="session-1",
        base_repo=base,
        repo=GitRepo(info.path),
        worktree=info,
        _base_branch=base.current_branch(),
        state=AgitrackState(info.path, default_backend="claude"),
    )
    runner.global_config = type("GC", (), {"default_backend": "claude"})()
    runner._set_message = lambda *a, **k: None
    runner._render = lambda *a, **k: None
    runner._debug = lambda *a, **k: None
    # Stub the process/screen lifecycle so no real backend is spawned.
    events: list = []
    runner._teardown_child = lambda: events.append("teardown")
    runner._stop_file_watcher = lambda: None
    runner._reset_agent_tracking = lambda: None
    runner._sanitize_state_trace = lambda: None
    runner._initialize_session_baseline = lambda: None
    runner._init_screen = lambda: None
    runner._spawn = lambda: events.append("spawn")
    runner._start_file_watcher = lambda: None
    runner._resize_child = lambda: None
    runner._enable_host_mouse = lambda: None

    # The worktree directory vanishes externally.
    shutil.rmtree(info.path)
    assert not info.path.exists()

    runner._ensure_worktree_alive()

    # Recreated at the same path as a real linked worktree, still a worktree session,
    # with the backend torn down and respawned (in that order).
    assert runner.worktree is not None  # not the base-tree fallback
    assert runner.worktree.path == info.path
    assert info.path.exists()
    assert (info.path / ".git").exists()  # a real linked worktree again
    assert runner.repo.repo == info.path
    assert events == ["teardown", "spawn"]


def test_ensure_worktree_alive_noop_when_directory_present(tmp_path):
    # The common case: the worktree is fine, so recovery must do nothing (no teardown,
    # no respawn) — it only acts when the directory is genuinely gone.
    from agitrack.git import GitRepo
    from agitrack.git.worktree import WorktreeManager

    base = GitRepo.init(tmp_path / "base")
    (tmp_path / "base" / "seed.txt").write_text("seed\n", encoding="utf-8")
    base.stage_paths(["seed.txt"])
    base.commit("seed")
    info = WorktreeManager(base).create("session-1", base=base.current_branch())

    runner = make_runner(name="session-1", base_repo=base, repo=GitRepo(info.path), worktree=info)
    touched: list = []
    runner._teardown_child = lambda: touched.append("teardown")
    runner._spawn = lambda: touched.append("spawn")

    runner._ensure_worktree_alive()

    assert touched == []  # directory present → recovery is a no-op


def test_abort_shared_resume_clears_token_for_retry():
    import threading

    runner = _shared_resume_runner()
    cancel = threading.Event()
    runner._shared_resume_cancel = cancel
    runner._shared_resume_result = {"action": "new"}
    runner._shared_resume_thread = object()

    runner._abort_shared_resume(cancel)

    assert cancel.is_set()  # the in-flight worker/git fetch is told to stop
    assert runner._shared_resume_cancel is None  # no token lingers to block a retry
    assert runner._shared_resume_result is None
    assert runner._shared_resume_thread is None


def test_shared_resume_prompts_for_local_name():
    runner = _shared_resume_runner()
    # The default offered to the prompt is the original share name (deduped, no
    # sharer prefix); the user accepts a local name of their own.
    seen = {}

    def fake_prompt(title, *, default):
        seen["default"] = default
        return "my-copy"

    runner._prompt_session_name = fake_prompt

    runner._resume_shared_session_menu()
    _drain_shared_resume(runner)

    assert seen["default"] == "fix-parser"
    assert runner.__dict__["_resumed"] == [("my-copy", "sid-1", {"backend": "claude"})]
    # The lineage origin (owner + name + contributors) is remembered so a later
    # re-share updates the same entry regardless of the local name (#55).
    assert runner.__dict__["_origins"]["sid-1"] == {
        "owner": "alice",
        "name": "fix-parser",
        "contributors": ["alice"],
    }


def test_shared_resume_defers_fetch_to_background_then_completes():
    # The transcript fetch must NOT run on the reactor thread (it can hit the
    # network and freeze the UI). The menu returns having only started a worker and
    # shown a message; the resume completes later, on the main loop.
    runner = _shared_resume_runner()
    runner._prompt_session_name = lambda title, *, default: "my-copy"
    messages: list = []
    runner._set_message = lambda msg, **k: messages.append(msg)

    runner._resume_shared_session_menu()

    assert runner.__dict__["_resumed"] == []  # nothing resumed synchronously
    assert runner._shared_resume_thread is not None  # a fetch worker is running
    assert any("Fetching" in m for m in messages)  # the user is told it's fetching

    _drain_shared_resume(runner)
    assert runner.__dict__["_resumed"] == [("my-copy", "sid-1", {"backend": "claude"})]


def test_shared_resume_already_live_stay_switches_without_fetch():
    import types

    runner = _shared_resume_runner()
    live = types.SimpleNamespace(state=types.SimpleNamespace(backend_session_id="sid-1"))
    runner.sessions = [live]
    runner.__dict__["_switched"] = []
    runner._switch_active = lambda i: runner.__dict__["_switched"].append(i)

    calls = {"n": 0}

    def popup(title, options, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return options[0]  # pick the entry
        return next(o for o in options if o.startswith("Stay"))  # already-live conflict

    runner._select_popup = popup
    runner._resume_shared_session_menu()

    assert runner.__dict__["_switched"] == [0]  # switched to the running session
    assert runner.__dict__["_resumed"] == []  # no new resume
    assert runner._shared_resume_thread is None  # no fetch started


def test_shared_resume_update_live_overwrites_worktree_and_restarts_agent():
    import types

    runner = _shared_resume_runner()
    live = types.SimpleNamespace(state=types.SimpleNamespace(backend_session_id="sid-1"))
    runner.sessions = [live]
    runner.repo = types.SimpleNamespace(repo="/wt")  # the live session's worktree
    runner.__dict__["_switched"] = []
    runner._switch_active = lambda i: runner.__dict__["_switched"].append(i)
    imported: list = []
    runner.backend.import_shared_session = lambda repo, sid, t, *, overwrite=False, as_id=None: (
        imported.append((repo, sid, overwrite, as_id)) or True
    )
    restarted: list = []
    runner._restart_agent = lambda msg: restarted.append(msg)

    calls = {"n": 0}

    def popup(title, options, **k):
        calls["n"] += 1
        return options[0] if calls["n"] == 1 else next(o for o in options if o.startswith("Update"))

    runner._select_popup = popup
    runner._resume_shared_session_menu()
    _drain_shared_resume(runner)

    assert runner.__dict__["_switched"] == [0]  # switched to the live session
    assert imported == [("/wt", "sid-1", True, None)]  # overwrote the worktree transcript in place
    assert restarted == ["Updated this session to the shared version."]  # backend restarted to load it


def test_share_identity_uses_remembered_origin_over_local_name():
    import types

    runner = make_runner(name="main")
    runner.active_index = 0
    runner._session_name = lambda i: "my-local-rename"
    origin = {"owner": "alice", "name": "fix-parser", "contributors": ["alice"]}
    runner._user_state = lambda: types.SimpleNamespace(shared_origin=lambda sid: origin if sid == "sid-1" else None)
    # A resumed shared session re-shares under its origin owner+name, with the sharer
    # joining the contributor set — not a fresh `<sharer>/<local-name>`.
    assert runner._share_identity("sid-1", "bob") == ("alice", "fix-parser", ["alice", "bob"])
    # A session that originated here (no origin) shares under the sharer + local name.
    assert runner._share_identity("other", "bob") == ("bob", "my-local-rename", ["bob"])
    assert runner._share_identity(None, "bob") == ("bob", "my-local-rename", ["bob"])


def test_new_session_stages_transcript_before_spawn_when_resuming(tmp_path, monkeypatch):
    # Resuming a shared session must stage its transcript into the fresh worktree
    # BEFORE spawning `--resume`, or the backend can't find it and the session
    # never loads (the transcript was imported under the base repo, not here).
    import types

    from agitrack.proxy import runner as runner_module

    runner = make_runner(name="main")
    runner._use_worktrees = True
    runner.global_config = types.SimpleNamespace(default_backend="claude")
    info = types.SimpleNamespace(name="bob-feature", path=tmp_path)
    repo = types.SimpleNamespace(repo=tmp_path, current_branch=lambda: "agit/claude/bob-feature/t1")
    runner._open_session_worktree = lambda name, **kwargs: (info, repo)
    monkeypatch.setattr(runner_module, "make_proxy_agent", lambda name: types.SimpleNamespace(name=name))
    monkeypatch.setattr(runner_module, "AgitrackActions", lambda *a, **k: types.SimpleNamespace())

    order: list = []
    runner._stage_backend_resume = lambda sid: order.append(("stage", sid))
    runner._spawn = lambda: order.append(("spawn", None))
    for name in (
        "_turn_from_branch",
        "_persist_session_name",
        "_sanitize_state_trace",
        "_initialize_session_baseline",
        "_init_screen",
        "_start_file_watcher",
        "_resize_child",
        "_enable_host_mouse",
        "_render",
    ):
        setattr(runner, name, lambda *a, **k: None)
    runner._set_message = lambda *a, **k: None
    runner.sessions = []

    runner._new_session("bob-feature", resume_session_id="sid-Y")

    assert order == [("stage", "sid-Y"), ("spawn", None)]  # staged BEFORE spawn


def test_fork_current_session_copies_under_a_new_id(tmp_path):
    import types

    runner = make_runner(name="main")
    runner._render = lambda: None
    runner._set_message = lambda *a, **k: None
    imported: dict = {}
    worktree_dir, base_dir = tmp_path / "worktree", tmp_path / "base"

    def export(repo, sid):
        if sid != "src-1":
            return None
        return "LATEST" if repo == worktree_dir else "STALE"  # the worktree holds the latest state

    agent = types.SimpleNamespace(
        name="claude",
        supports_session_sharing=True,
        export_session_raw=export,
        new_import_id=lambda: "fork-2",
        new_session_id=lambda: "fallback",
        import_shared_session=lambda repo, sid, transcript, *, overwrite=False, as_id=None: (
            imported.update(sid=sid, as_id=as_id, transcript=transcript) or True
        ),
    )
    runner.backend = agent
    runner.state = types.SimpleNamespace(backend_session_id="src-1", backend="claude")
    runner.base_repo = types.SimpleNamespace(repo=base_dir)
    runner.repo = types.SimpleNamespace(repo=worktree_dir)
    created: dict = {}
    runner._new_session = lambda name, **kw: created.update(name=name, **kw)

    ok = runner._fork_current_session("forked", base_branch="dev")

    assert ok is True
    # Installed under a NEW id, from the worktree's LATEST state (not the base repo's
    # older mirror); the original (src-1) transcript is untouched.
    assert imported == {"sid": "src-1", "as_id": "fork-2", "transcript": "LATEST"}
    # The forked session resumes the new id (not the original), so the two never clash.
    assert created["resume_session_id"] == "fork-2"
    assert created["base_branch"] == "dev"


def test_fork_falls_back_to_blank_when_backend_cannot_share():
    import types

    runner = make_runner(name="main")
    runner.backend = types.SimpleNamespace(name="opencode", supports_session_sharing=False)
    runner.state = types.SimpleNamespace(backend_session_id="src-1", backend="opencode")

    assert runner._can_fork_active() is False
    assert runner._prompt_fork_or_blank() is False  # no fork option offered
    assert runner._fork_current_session("x") is False  # forking not possible


def test_prompt_new_session_inherits_current_backend():
    # A blank new session must start in the SAME backend as the session it is
    # created from — not the global default, which may differ (e.g. left at
    # opencode while the user is coding in claude).
    import types

    runner = make_runner(name="main")
    runner._render = lambda: None
    runner._set_message = lambda *a, **k: None
    # Active session is claude with no live conversation (so the fork option is
    # not offered and the blank-session path is taken).
    runner.state = types.SimpleNamespace(backend_session_id=None, backend="claude")
    runner.backend = types.SimpleNamespace(name="claude", supports_session_sharing=False)
    runner._prompt_session_name = lambda *a, **k: "new-one"
    runner._prompt_new_session_base = lambda *, default=None: "main"
    created: dict = {}
    runner._new_session = lambda name, **kw: created.update(name=name, **kw)

    assert runner._prompt_new_session() == runner._MENU_DONE
    assert created["name"] == "new-one"
    # The new session inherits the active session's backend, not the global default.
    assert created["backend"] == "claude"


def test_shared_resume_cancel_on_name_prompt_does_not_resume():
    runner = _shared_resume_runner()
    runner._prompt_session_name = lambda *a, **k: None  # user cancels naming

    runner._resume_shared_session_menu()

    assert runner.__dict__["_resumed"] == []


def test_dedupe_session_name_avoids_collisions():
    runner = make_runner(name="main")
    runner._taken_session_names = lambda: {"alice-fix-parser", "alice-fix-parser-2"}
    assert runner._dedupe_session_name("alice/fix-parser") == "alice-fix-parser-3"
    runner._taken_session_names = lambda: set()
    assert runner._dedupe_session_name("alice/fix-parser") == "alice-fix-parser"


# --- base branch switched out-of-band ---


def _merge_drift_runner(dir_branch, *, session_target="dev", choice, sessions=None):
    import types

    runner = make_runner(
        worktree=types.SimpleNamespace(),
        _base_branch=session_target,
        _base_drift_check_at=0.0,
        base_repo=types.SimpleNamespace(current_branch=lambda: dir_branch),
        name="s1",
    )
    runner.sessions = sessions if sessions is not None else [runner.active]
    runner._repo_dir_branch = session_target  # start aligned (so no prompt until the dir moves)
    runner._debug = lambda *a, **k: None
    runner.messages = []
    runner._set_message = lambda m, **k: runner.messages.append(m)
    runner._render = lambda: None
    runner.popups = []
    runner._select_popup = lambda title, options: runner.popups.append((title, options)) or choice
    runner.retargeted: list = []
    runner._retarget_active_session = lambda target: runner.retargeted.append(target) or True
    runner._session_work_merged_into_base = lambda: True  # default: nothing pending to merge
    return runner


# --- agent switches the worktree's OWN branch (in-place git checkout) ---


def _agent_branch_runner(worktree_branch, *, base="master"):
    import types

    runner = make_runner(
        worktree=types.SimpleNamespace(),
        _base_branch=base,
        repo=types.SimpleNamespace(current_branch=lambda: worktree_branch),
        name="s1",
    )
    runner._integration = types.SimpleNamespace(base_branch=base)
    runner._agent_branch_check_at = 0.0
    runner.messages = []
    runner._set_message = lambda m, **k: runner.messages.append(m)
    runner._render = lambda: None
    return runner


def test_follows_agent_worktree_branch_switch():
    runner = _agent_branch_runner("feature")
    runner._follow_agent_worktree_branch()
    assert runner._base_branch == "feature"
    assert runner._repo_dir_branch == "feature"  # status bar follows
    assert runner._integration.base_branch == "feature"
    assert any("Working branch switched to 'feature' by the backend agent" in m for m in runner.messages)
    # Already on 'feature' → no re-notification, no churn.
    runner.messages.clear()
    runner._agent_branch_check_at = 0.0
    runner._follow_agent_worktree_branch()
    assert runner.messages == [] and runner._base_branch == "feature"


def test_does_not_follow_managed_turn_branch_or_detached_head():
    # aGiTrack's own worktree states — a managed turn branch, or detached at base between turns —
    # are NOT the agent switching the branch, so the follow must never fire for them.
    for branch in ("agitrack/claude/s1/t1", "HEAD"):
        runner = _agent_branch_runner(branch)
        runner._follow_agent_worktree_branch()
        assert runner._base_branch == "master", branch
        assert runner.messages == []


def test_no_worktree_session_does_not_poll_worktree_branch():
    # No-worktree mode is followed by _check_no_worktree_branch_change; there is no separate
    # worktree branch to track here, so this poll is a no-op.
    runner = _agent_branch_runner("feature")
    runner.worktree = None
    runner._follow_agent_worktree_branch()
    assert runner._base_branch == "master" and runner.messages == []


def test_repo_dir_change_keeps_all_sessions_on_their_branches_by_default():
    # The default (first) option does nothing — background sessions keep merging into
    # their own branches after the directory's branch changes.
    runner = _merge_drift_runner("feature-x", choice="Do nothing — keep every session merging into its own branch")
    runner._check_base_branch_drift()
    assert runner.popups and "feature-x" in runner.popups[0][0] and "dev" in runner.popups[0][0]
    assert runner.retargeted == []  # nothing was re-targeted
    assert runner._base_branch == "dev"
    assert runner._repo_dir_branch == "feature-x"  # cached directory branch updated

    # No re-prompt while the directory branch is unchanged.
    runner.popups.clear()
    runner._base_drift_check_at = 0.0
    runner._check_base_branch_drift()
    assert runner.popups == []


def test_repo_dir_change_can_switch_only_the_current_session():
    runner = _merge_drift_runner("feature-x", choice="Switch only 's1' to 'feature-x'")
    runner._check_base_branch_drift()
    assert runner.retargeted == ["feature-x"]  # only the active session follows the directory


def test_repo_dir_change_can_switch_all_sessions():
    from agitrack.proxy.session import Session

    choice = "Switch all idle sessions to 'feature-x' (running sessions keep their branch)"
    runner = _merge_drift_runner("feature-x", choice=choice)
    other = Session.bare()
    other._base_branch = "dev"
    runner.sessions = [runner.active, other]  # two live sessions
    runner._check_base_branch_drift()
    assert runner.retargeted == ["feature-x", "feature-x"]  # the active one and the background one


def test_switch_all_idle_sessions_skips_running_ones():
    from agitrack.proxy.session import Session

    choice = "Switch all idle sessions to 'feature-x' (running sessions keep their branch)"
    runner = _merge_drift_runner("feature-x", choice=choice)
    running = Session.bare()
    running._base_branch = "dev"
    running.agent_in_flight = True  # mid-turn — must keep its branch
    runner.sessions = [runner.active, running]
    runner._check_base_branch_drift()
    assert runner.retargeted == ["feature-x"]  # only the idle (active) session switched
    assert any("running a turn" in m for m in runner.messages)  # the skipped one is reported


def test_session_switch_prompt_keeps_or_switches_active_session():
    # On a session switch the prompt is two-option (this session only).
    runner = _merge_drift_runner("feature-x", choice="Switch to 'feature-x' (the current directory branch)")
    runner._repo_dir_branch = "feature-x"  # directory already on feature-x; session merges into dev
    runner._prompt_merge_target_if_diverged()
    assert runner.retargeted == ["feature-x"]


def test_no_worktree_session_follows_directory_branch_and_warns_on_switch():
    import types

    # In --no-worktree mode there is no separate merge target: a session always works on
    # the directory's CURRENT branch and can never be pointed at a different one. When the
    # directory's branch is switched, the session follows it and a warning is shown — but no
    # "where should this merge?" dialog is ever offered.
    dir_branch = ["main"]
    runner = make_runner(
        _use_worktrees=False,
        worktree=None,
        _base_branch="main",
        _base_drift_check_at=0.0,
        base_repo=types.SimpleNamespace(current_branch=lambda: dir_branch[0]),
        name="s1",
    )
    runner._repo_dir_branch = "main"
    runner._integration = types.SimpleNamespace(base_branch="main")
    runner.messages = []
    runner._set_message = lambda m, **k: runner.messages.append(m)
    runner._render = lambda: None
    runner.popups = []
    runner._select_popup = lambda *a, **k: runner.popups.append(a) or None

    # Aligned: no warning, no popup.
    runner._check_base_branch_drift()
    assert runner.messages == [] and runner.popups == []
    assert runner._base_branch == "main"

    # Directory branch switched out-of-band: the session follows it, a warning shows, and
    # NO merge-target dialog is offered.
    dir_branch[0] = "feature-y"
    runner._base_drift_check_at = 0.0  # bypass the poll throttle
    runner._check_base_branch_drift()
    assert runner._base_branch == "feature-y"  # follows the directory's current branch
    assert runner._repo_dir_branch == "feature-y"
    assert runner._integration.base_branch == "feature-y"
    assert runner.popups == []  # never asks where to merge — only one branch is allowed
    assert any("feature-y" in m and "without a worktree" in m for m in runner.messages)

    # No re-warn while the branch stays put.
    runner.messages.clear()
    runner._base_drift_check_at = 0.0
    runner._check_base_branch_drift()
    assert runner.messages == []


def test_repo_dir_change_while_running_defers_prompt_until_idle():
    # A running session's merge branch must NOT change mid-turn. The dir-change prompt
    # is deferred (with a warning) and fires once the session is idle.
    runner = _merge_drift_runner("feature-x", choice="Switch only 's1' to 'feature-x'")
    runner.agent_in_flight = True  # a turn is in flight

    runner._check_base_branch_drift()
    assert runner.popups == []  # NOT prompted while running
    assert runner._pending_merge_prompt is True
    assert runner.retargeted == []  # nothing re-targeted mid-run
    assert any("still merges into 'dev'" in m for m in runner.messages)  # the warning

    # The run finishes → on the next poll the deferred prompt fires.
    runner.agent_in_flight = False
    runner._base_drift_check_at = 0.0
    runner._check_base_branch_drift()
    assert runner.popups  # now it asks
    assert runner.retargeted == ["feature-x"]
    assert runner._pending_merge_prompt is False


def test_reconcile_merge_branch_honors_prior_assignment_and_defers_confirm():
    import types

    # At startup the session is assumed to merge into the directory branch ('dev'), but a
    # previous run assigned it 'feature-y'. The prior assignment is honored and a
    # confirmation prompt is deferred for the user.
    runner = make_runner(worktree=types.SimpleNamespace(), _base_branch="dev", name="s1")
    runner.base_repo = types.SimpleNamespace(list_branches=lambda: ["dev", "feature-y"])
    runner.state = types.SimpleNamespace(merge_branch="feature-y")
    runner._reconcile_merge_branch("dev")
    assert runner._base_branch == "feature-y"  # the prior assignment wins
    assert runner.state.merge_branch == "feature-y"  # not overwritten with the dir branch
    assert runner._pending_merge_prompt is True  # the user will be asked to confirm


def test_reconcile_merge_branch_records_dir_branch_when_unset():
    import types

    runner = make_runner(worktree=types.SimpleNamespace(), _base_branch="dev", name="s1")
    runner.state = types.SimpleNamespace(merge_branch=None)
    runner._reconcile_merge_branch("dev")
    assert runner.state.merge_branch == "dev"  # recorded for next time
    assert runner._base_branch == "dev"
    assert runner._pending_merge_prompt is False  # nothing to confirm


def test_reconcile_merge_branch_falls_back_when_prior_branch_deleted():
    import types

    # The prior run assigned 'feature-y', but that branch no longer exists — don't honor
    # a dangling branch (it would break integration); fall back to the directory branch.
    runner = make_runner(worktree=types.SimpleNamespace(), _base_branch="dev", name="s1")
    runner.base_repo = types.SimpleNamespace(list_branches=lambda: ["dev", "main"])
    runner.state = types.SimpleNamespace(merge_branch="feature-y")
    runner._reconcile_merge_branch("dev")
    assert runner._base_branch == "dev"  # falls back to the directory branch
    assert runner.state.merge_branch == "dev"  # the dangling assignment is reset
    assert runner._pending_merge_prompt is False  # nothing to confirm


def test_deferred_merge_prompt_waits_until_run_merged_into_base():
    # After a running session finishes, the branch-switch dialog must appear only once
    # the run's changes have merged into the original branch — not while integration is
    # still pending.
    runner = _merge_drift_runner("feature-x", choice="Switch only 's1' to 'feature-x'")
    runner.agent_in_flight = True
    runner._check_base_branch_drift()  # dir moved mid-run → deferred
    assert runner._pending_merge_prompt is True and runner.popups == []

    # The run goes idle, but its work hasn't integrated into 'dev' yet — still no dialog.
    runner.agent_in_flight = False
    runner._session_work_merged_into_base = lambda: False  # commit/integration still pending
    runner._base_drift_check_at = 0.0
    runner._check_base_branch_drift()
    assert runner._pending_merge_prompt is True  # held back
    assert runner.popups == []

    # Once the changes have merged into 'dev', the dialog appears.
    runner._session_work_merged_into_base = lambda: True
    runner._base_drift_check_at = 0.0
    runner._check_base_branch_drift()
    assert runner.popups  # asked now
    assert runner._pending_merge_prompt is False


def test_deferred_merge_prompt_is_per_session():
    # The deferred-prompt flag is PER-SESSION. A prompt deferred while session A runs is
    # never swallowed (or double-asked) by switching to a different, already-aligned
    # session B — each session carries its own flag.
    from agitrack.proxy.session import Session

    runner = _merge_drift_runner("feature-x", choice="Switch only 's1' to 'feature-x'")
    a = runner.active
    b = Session.bare()
    b._base_branch = "feature-x"  # B already merges into the dir branch (aligned)
    runner.sessions = [a, b]

    # A defers a prompt while running.
    a.agent_in_flight = True
    runner._check_base_branch_drift()
    assert a._pending_merge_prompt is True and runner.popups == []

    # Switch to B (idle, aligned): nothing is asked, B has no pending prompt, and A's
    # deferral is untouched on A's own flag.
    runner.active = b
    b.agent_in_flight = False
    runner._base_drift_check_at = 0.0
    runner._check_base_branch_drift()
    assert runner.popups == []
    assert b._pending_merge_prompt is False
    assert a._pending_merge_prompt is True  # A's deferral survives

    # Back on A (idle, merged, still diverged): A's deferred prompt finally fires.
    runner.active = a
    a.agent_in_flight = False
    runner._base_drift_check_at = 0.0
    runner._check_base_branch_drift()
    assert runner.popups  # asked at last
    assert a._pending_merge_prompt is False


def test_deferred_merge_prompt_dropped_when_dir_returns_to_session_branch():
    # If, after a run defers a prompt, the directory is checked back onto the session's
    # OWN branch, the deferral is moot and is dropped without asking (using the fresh dir
    # branch, not a stale cached one).
    runner = _merge_drift_runner("feature-x", choice="Switch only 's1' to 'feature-x'")
    runner.agent_in_flight = True
    runner._check_base_branch_drift()  # dir → feature-x mid-run → deferred
    assert runner._pending_merge_prompt is True

    runner.agent_in_flight = False
    runner.base_repo.current_branch = lambda: "dev"  # user checks the dir back to 'dev'
    runner._base_drift_check_at = 0.0
    runner._check_base_branch_drift()
    assert runner._pending_merge_prompt is False  # dropped — back in sync
    assert runner.popups == []  # nothing asked


def test_retarget_active_session_refused_while_running():
    import types

    runner = make_runner(worktree=types.SimpleNamespace(), _base_branch="dev", name="s1")
    runner.agent_in_flight = True
    runner.messages = []
    runner._set_message = lambda m, **k: runner.messages.append(m)
    runner._render = lambda: None

    assert runner._retarget_active_session("feature-x") is False  # refused
    assert runner._base_branch == "dev"  # unchanged
    assert any("running a turn" in m for m in runner.messages)


def test_integrate_turn_skips_while_paused():
    import types

    runner = make_runner(
        worktree=types.SimpleNamespace(),
        _base_branch="dev",
        merge_ctx=None,
        _integration_paused=True,
    )
    assert runner._integrate_turn_or_conflict() == "skip"


def test_advance_base_when_base_not_checked_out_uses_safe_fast_forward():
    # The user `git checkout`ed a different branch in the directory, so the base
    # ('dev') is not the checked-out branch. Integration must NOT use the
    # working-tree fast-forward (which would move the WRONG branch); it advances
    # the base ref directly, and only when that is a true fast-forward.
    import types

    merged, ff = [], []
    runner = make_runner(_base_branch="dev")
    runner.repo = types.SimpleNamespace(switch_detach=lambda ref: None, current_branch=lambda: "HEAD")
    runner.base_repo = types.SimpleNamespace(
        current_branch=lambda: "feature-x",  # base 'dev' is not checked out
        merge_ff_only=lambda ref: merged.append(ref),
        is_ancestor=lambda a, b: True,  # turn branch descends from 'dev' → real ff
        fast_forward_branch=lambda branch, target: ff.append((branch, target)),
        delete_branch=lambda name, force=False: None,
    )
    runner._integration.base_repo = runner.base_repo

    runner._advance_base_to("agit/claude/session-1/t1")

    assert merged == []  # never moved the checked-out 'feature-x'
    assert ff == [("dev", "agit/claude/session-1/t1")]  # advanced base's ref only


def _exit_removal_runner(*, log_range_result="", rev_parse_raises=False):
    import types

    def _rev_parse(ref):
        if rev_parse_raises:
            raise RuntimeError("unknown revision")
        return "abc123"

    runner = make_runner(
        _base_branch="dev",
        merge_ctx=None,
        _primary_worktree_name=None,
        worktree=types.SimpleNamespace(name="session-1"),
        repo=types.SimpleNamespace(
            current_branch=lambda: "agit/claude/session-1/t1",
            merge_in_progress=lambda: False,
            has_changes=lambda: False,
        ),
    )
    runner.base_repo = types.SimpleNamespace(
        rev_parse=_rev_parse,
        log_range=lambda base, head: log_range_result,
    )
    runner.removed = []
    runner._worktrees = lambda: types.SimpleNamespace(remove=lambda name: runner.removed.append(name))
    runner._remember_session_for_backend = lambda: None
    runner._persist_last_session_record = lambda: None
    runner._terminate_child = lambda: None
    runner._debug = lambda *a, **k: None
    return runner


def test_exit_keeps_worktree_with_unintegrated_commits():
    runner = _exit_removal_runner(log_range_result="deadbeef a commit")
    runner._remove_worktree_on_exit()
    assert runner.removed == []  # branch ahead of base (e.g. merging was paused) → preserved


def test_exit_keeps_worktree_when_base_ref_unresolvable():
    runner = _exit_removal_runner(rev_parse_raises=True)
    runner._remove_worktree_on_exit()
    assert runner.removed == []  # base branch deleted/renamed → can't confirm merged → preserved


def test_exit_removes_fully_merged_worktree():
    runner = _exit_removal_runner(log_range_result="")  # nothing ahead of base
    runner._remove_worktree_on_exit()
    assert runner.removed == ["session-1"]  # normal cleanup of a merged session still happens


def test_exit_persists_resume_pointer_even_when_worktree_kept():
    # A primary session that exits with unintegrated work keeps its worktree, but
    # its resume pointer (which adopts a backend-native session switch) must still
    # be persisted. Gating that behind worktree removal caused the off-by-one:
    # next start resumes a stale conversation, and only the start after that lands
    # on the one the user switched to.
    runner = _exit_removal_runner(log_range_result="deadbeef still ahead")
    runner._primary_worktree_name = "session-1"
    persisted = []
    runner._persist_last_session_record = lambda: persisted.append(True)

    runner._remove_worktree_on_exit()

    assert runner.removed == []  # unintegrated → worktree still kept
    assert persisted == [True]  # ...but the resume pointer was persisted anyway


def test_exit_does_not_persist_resume_pointer_for_background_session():
    # A non-primary (background) session the user was NOT in at quit must not
    # overwrite the durable resume pointer. (No exit-active session is set here, so
    # the gate falls back to the primary, which is a different session.)
    runner = _exit_removal_runner(log_range_result="deadbeef still ahead")
    runner._primary_worktree_name = "session-2"  # this session ("session-1") is not primary
    runner._exit_resume_worktree = None
    persisted = []
    runner._persist_last_session_record = lambda: persisted.append(True)

    runner._remove_worktree_on_exit()

    assert persisted == []


def test_exit_persists_resume_pointer_for_last_active_session_even_if_not_primary():
    # The session the user was in at quit (e.g. a resumed shared session) is the
    # one to auto-resume next start, so its pointer is persisted even though a
    # different session is the "primary". Without this, quitting from a shared
    # session left the next start prompting for a brand-new session.
    runner = _exit_removal_runner(log_range_result="")
    runner._primary_worktree_name = "session-2"  # primary is a different session
    runner._exit_resume_worktree = "session-1"  # ...but the user quit from session-1
    persisted = []
    runner._persist_last_session_record = lambda: persisted.append(True)

    runner._remove_worktree_on_exit()

    assert persisted == [True]


def _bg_confirm_runner(statuses):
    runner = make_runner()
    runner.sessions = [object()] * len(statuses)
    runner._session_status = lambda i: statuses[i]
    runner._session_name = lambda i: f"session-{i}"
    return runner


def test_confirm_terminate_background_sessions_no_prompt_when_all_idle():
    runner = _bg_confirm_runner(["running", "idle", "idle"])  # active(0) running, bg idle
    popups = []
    runner._select_popup = lambda *a, **k: popups.append(a) or "unused"
    # The active session running in the foreground never triggers the prompt.
    assert runner._confirm_terminate_background_sessions() is True
    assert popups == []


def test_confirm_terminate_background_sessions_prompts_and_names_them():
    runner = _bg_confirm_runner(["idle", "running", "idle"])  # background session-1 running
    captured = {}

    def fake_popup(title, options):
        captured["title"], captured["options"] = title, options
        return "Yes, terminate them and exit"

    runner._select_popup = fake_popup
    assert runner._confirm_terminate_background_sessions() is True
    assert "still running" in captured["title"]
    assert "session-1" in captured["title"]

    # Declining the second confirmation keeps working (cancels exit).
    runner._select_popup = lambda *a, **k: "No, keep working"
    assert runner._confirm_terminate_background_sessions() is False


def test_sync_idle_worktrees_skipped_while_paused():
    runner = make_runner(
        _integration_paused=True,
    )
    runner.sessions = ["would-explode-if-iterated"]  # not a real session; must not be touched
    runner._sync_idle_worktrees_to_base()  # returns early; no AttributeError


# --- corrupted-worktree reuse / diagnostics ---


def test_cleanup_stale_state_removes_orphaned_worktree_dirs(tmp_path):
    import types

    root = tmp_path / ".agitrack" / "worktrees"
    registered = root / "session-1"
    registered.mkdir(parents=True)
    orphan = root / "session-2"
    (orphan / ".agitrack").mkdir(parents=True)  # only .agitrack/ → not a valid worktree
    (root / "stray-file").write_text("x")  # a file, not a dir → ignored

    runner = make_runner()
    runner._debug = lambda *a, **k: None
    prunes = []
    runner.base_repo = types.SimpleNamespace(worktree_prune=lambda: prunes.append(1))
    runner.worktree_manager = types.SimpleNamespace(
        root=root,
        list=lambda: [types.SimpleNamespace(path=registered)],  # session-1 is registered
    )
    runner._worktrees = lambda: runner.worktree_manager

    runner._cleanup_stale_state_on_startup()

    assert registered.exists()  # a real registered worktree is kept
    assert not orphan.exists()  # the orphaned .agitrack/-only dir is swept
    assert (root / "stray-file").exists()
    assert prunes  # pruned stale git registrations


def test_is_valid_worktree_rejects_leftover_without_git(tmp_path):
    runner = make_runner()
    leftover = tmp_path / "session-1"
    (leftover / ".agitrack").mkdir(parents=True)  # only .agitrack/, no .git → invalid
    assert runner._is_valid_worktree(leftover) is False


def test_open_session_worktree_recreates_corrupted_leftover(tmp_path):
    import types

    runner = make_runner(_base_branch="dev")
    runner._debug = lambda *a, **k: None
    leftover = tmp_path / "session-1"
    (leftover / ".agitrack").mkdir(parents=True)  # corrupted leftover
    created = {}

    def _create(name, *, base):
        created["called"] = (name, base)
        (tmp_path / name / ".git").parent.mkdir(parents=True, exist_ok=True)
        return types.SimpleNamespace(name=name, path=tmp_path / name, branch="")

    runner.worktree_manager = types.SimpleNamespace(worktree_path=lambda name: tmp_path / name, create=_create)
    runner._worktrees = lambda: runner.worktree_manager
    import agitrack.proxy.runner as proxymod

    orig = proxymod.GitRepo
    proxymod.GitRepo = lambda path: types.SimpleNamespace(current_branch=lambda: "")
    try:
        runner._open_session_worktree("session-1")
    finally:
        proxymod.GitRepo = orig

    assert created["called"] == ("session-1", "dev")  # recreated, not reused
    assert not (leftover / ".agitrack").exists()  # corrupted leftover was cleared first


def test_diag_path_uses_base_repo(tmp_path):
    import types

    runner = make_runner(
        base_repo=types.SimpleNamespace(repo=tmp_path / "base"),
        repo=types.SimpleNamespace(repo=tmp_path / "base" / ".agitrack" / "worktrees" / "session-1"),
    )
    runner._diag_run = "20260101-000000"
    path = runner._diag_path("proxy-raw")
    # Lands in the *base* .agitrack/, not the ephemeral worktree's.
    assert path == tmp_path / "base" / ".agitrack" / "proxy-raw-20260101-000000.log"


# --- resume cwd drift guard ---


def _drift_runner(recorded_cwd, worktree_path):
    import types

    runner = make_runner(
        worktree=types.SimpleNamespace(name="session-1"),
        repo=types.SimpleNamespace(repo=worktree_path),
        state=types.SimpleNamespace(backend_session_id="sess-1"),
        backend=types.SimpleNamespace(recorded_working_dir=lambda sid, *, since=None: recorded_cwd),
    )
    runner._debug = lambda *a, **k: None
    runner._cwd_check_at = 0.0
    runner._cwd_launch_at = 1000.0
    runner.messages = []
    runner._set_message = lambda msg, **k: runner.messages.append(msg)
    runner._render = lambda: None
    return runner


def test_cwd_drift_warns_when_backend_left_the_worktree():
    runner = _drift_runner("/somewhere/else", "/repo/.agitrack/worktrees/session-1")
    runner._warn_if_cwd_drifted()
    assert runner.messages and "#58591" in runner.messages[0]
    assert runner._cwd_drift_checked is True
    # Warns once, then stops.
    runner.messages.clear()
    runner._warn_if_cwd_drifted()
    assert runner.messages == []


def test_cwd_drift_silent_when_on_the_worktree():
    runner = _drift_runner("/repo/.agitrack/worktrees/session-1", "/repo/.agitrack/worktrees/session-1")
    runner._warn_if_cwd_drifted()
    assert runner.messages == []
    assert runner._cwd_drift_checked is True


def test_cwd_drift_waits_when_no_cwd_recorded_yet():
    runner = _drift_runner(None, "/repo/.agitrack/worktrees/session-1")
    runner._warn_if_cwd_drifted()
    assert runner.messages == []
    assert getattr(runner, "_cwd_drift_checked", False) is False  # will re-check next tick


def test_cwd_drift_forwards_launch_time_as_since(monkeypatch):
    # The launch epoch is passed through as `since` so the backend can ignore a
    # stale pre-launch cwd (#72). A backend that only reports a post-launch turn
    # returns None until one exists, so no false warning is latched.
    import types

    seen = {}

    def recorded(sid, *, since=None):
        seen["since"] = since
        return None  # no post-launch turn yet

    runner = make_runner(
        worktree=types.SimpleNamespace(name="session-1"),
        repo=types.SimpleNamespace(repo="/repo/.agitrack/worktrees/session-1"),
        state=types.SimpleNamespace(backend_session_id="sess-1"),
        backend=types.SimpleNamespace(recorded_working_dir=recorded),
    )
    runner._debug = lambda *a, **k: None
    runner._cwd_check_at = 0.0
    runner._cwd_launch_at = 1234.5
    runner.messages = []
    runner._set_message = lambda msg, **k: runner.messages.append(msg)
    runner._render = lambda: None

    runner._warn_if_cwd_drifted()

    assert seen["since"] == 1234.5
    assert runner.messages == []
    assert runner._cwd_drift_checked is False  # nothing post-launch yet → keep checking


# --- worktree confinement ---


def test_confine_to_worktree_wraps_when_enabled(monkeypatch):
    import types
    from agitrack.proxy import sandbox

    # Force the macOS mechanism so the assertion is platform-independent.
    monkeypatch.setattr(sandbox, "_have_sandbox_exec", lambda: True)
    monkeypatch.delenv("AGITRACK_SANDBOX", raising=False)
    runner = make_runner(
        worktree=types.SimpleNamespace(name="session-1"),
        repo=types.SimpleNamespace(repo="/repo/.agitrack/worktrees/session-1"),
    )
    runner.global_config = types.SimpleNamespace(sandbox=True)
    runner.base_repo = types.SimpleNamespace(repo="/repo")

    wrapped = runner._confine_to_worktree(["claude"])

    assert wrapped[0] == "sandbox-exec" and wrapped[-1] == "claude"


def test_confine_to_worktree_noop_without_worktree_or_when_disabled(monkeypatch):
    import types
    from agitrack.proxy import sandbox

    monkeypatch.setattr(sandbox, "is_available", lambda: True)
    runner = make_runner(worktree=None)
    runner._sandbox = True
    assert runner._confine_to_worktree(["claude"]) == ["claude"]

    runner = make_runner(
        worktree=types.SimpleNamespace(name="session-1"),
        repo=types.SimpleNamespace(repo="/repo/.agitrack/worktrees/session-1"),
    )
    runner._sandbox = False  # user opted out (config or --no-sandbox)
    runner.base_repo = types.SimpleNamespace(repo="/repo")
    assert runner._confine_to_worktree(["claude"]) == ["claude"]


# --- backend-exit / native session switch ---


def test_adopt_latest_backend_session_repoints_after_native_switch():
    import types

    runner = make_runner(
        repo=types.SimpleNamespace(repo="/wt"),
        backend=types.SimpleNamespace(latest_session_id=lambda repo: "switched-id"),
        state=types.SimpleNamespace(backend_session_id="pinned-id", last_backend_message_id="m9"),
    )
    runner._debug = lambda *a, **k: None

    runner._adopt_latest_backend_session()

    # The worktree's newest conversation (what the user switched to) wins.
    assert runner.state.backend_session_id == "switched-id"
    assert runner.state.last_backend_message_id is None


def test_adopt_latest_backend_session_keeps_id_when_unchanged():
    import types

    runner = make_runner(
        repo=types.SimpleNamespace(repo="/wt"),
        backend=types.SimpleNamespace(latest_session_id=lambda repo: "same"),
        state=types.SimpleNamespace(backend_session_id="same", last_backend_message_id="m1"),
    )
    runner._debug = lambda *a, **k: None

    runner._adopt_latest_backend_session()

    assert runner.state.backend_session_id == "same"
    assert runner.state.last_backend_message_id == "m1"  # untouched


def test_recover_nonempty_session_returns_latest_with_content(tmp_path):
    import types

    state = AgitrackState(tmp_path)
    state.backend_session_id = "empty-id"
    real = ExportedSession("real-id", "claude-opus-4-8", None, [SessionTurn("u", "a", "p", "r", TokenUsage(), None)])
    runner = make_runner(
        state=state,
        repo=types.SimpleNamespace(repo="/wt"),
        backend=types.SimpleNamespace(
            latest_session_id=lambda repo: "real-id",
            export_session=lambda repo, sid: real if sid == "real-id" else ExportedSession(sid, None, None, []),
        ),
    )
    runner._debug = lambda *a, **k: None
    runner._stage_backend_resume = lambda sid: None

    assert runner._recover_nonempty_session() == ("real-id", real)


def test_recover_nonempty_session_none_when_latest_also_empty(tmp_path):
    import types

    state = AgitrackState(tmp_path)
    state.backend_session_id = "empty-id"
    runner = make_runner(
        state=state,
        repo=types.SimpleNamespace(repo="/wt"),
        backend=types.SimpleNamespace(
            latest_session_id=lambda repo: "other-empty",
            export_session=lambda repo, sid: ExportedSession(sid, None, None, []),
        ),
    )
    runner._debug = lambda *a, **k: None
    runner._stage_backend_resume = lambda sid: None

    assert runner._recover_nonempty_session() is None


def test_relaunch_backend_resumes_then_gives_up_on_crash_loop(monkeypatch):
    runner = make_runner()
    runner._debug = lambda *a, **k: None
    calls = []
    runner._restart_agent = lambda msg: calls.append("relaunch")
    runner._finalize_on_backend_exit = lambda: calls.append("finalize")

    t = [1000.0]
    monkeypatch.setattr("agitrack.proxy.runner.time.monotonic", lambda: t[0])

    # Backend keeps dying quickly: first 3 relaunch, the 4th gives up and exits.
    assert runner._relaunch_backend_or_exit() is True
    assert runner._relaunch_backend_or_exit() is True
    assert runner._relaunch_backend_or_exit() is True
    assert runner._relaunch_backend_or_exit() is False

    assert calls == ["relaunch", "relaunch", "relaunch", "finalize"]


def test_crash_loop_capture_surfaces_backend_output(monkeypatch):
    # When the backend keeps dying on launch, the crash-loop bail must capture the
    # backend's last output so run() can echo it instead of exiting silently (#114).
    runner = make_runner()
    runner._debug = lambda *a, **k: None
    runner._restart_agent = lambda msg: None
    runner._finalize_on_backend_exit = lambda: None
    runner.last_child_output_sample = (
        b"\x1b[2J\x1b[HError: Session abc123 is currently running as a "
        b"background agent (bg). Use `claude agents` ... or add --fork-session.\r\n"
    )

    t = [1000.0]
    monkeypatch.setattr("agitrack.proxy.runner.time.monotonic", lambda: t[0])
    for _ in range(3):
        assert runner._relaunch_backend_or_exit() is True
    assert runner._relaunch_backend_or_exit() is False

    notice = runner._backend_exit_notice
    assert notice is not None
    assert "exited repeatedly" in notice
    assert "running as a background agent" in notice  # the real reason, escapes stripped
    assert "\x1b" not in notice
    assert "--new-session" in notice


def test_backend_exit_notice_shows_the_launched_command(monkeypatch):
    # The notice must name the EXACT command aGiTrack tried to launch (and what it produced),
    # so a backend that dies on startup is diagnosable rather than a generic "exited".
    runner = make_runner()
    runner._debug = lambda *a, **k: None
    runner._restart_agent = lambda msg: None
    runner._finalize_on_backend_exit = lambda: None
    runner._last_spawn_command = ["claude", "--resume", "abc"]
    runner.last_child_output_sample = b""  # claude died producing nothing

    t = [1000.0]
    monkeypatch.setattr("agitrack.proxy.runner.time.monotonic", lambda: t[0])
    for _ in range(3):
        runner._relaunch_backend_or_exit()
    runner._relaunch_backend_or_exit()

    notice = runner._backend_exit_notice
    assert notice is not None
    assert "Command: claude --resume abc" in notice
    assert "no output before exiting" in notice  # explicit, not an empty section


def test_busy_session_forks_instead_of_crash_looping():
    # The claude session is held by a running background agent. aGiTrack should fork a
    # copy on the next spawn rather than relaunch into the same refusal (#114).
    import types

    runner = make_runner(
        state=types.SimpleNamespace(backend="claude", backend_session_id="old-id"),
        last_child_output_sample=(
            b"Error: Session old-id is currently running as a background agent (bg). "
            b"Use `claude agents` ... or add --fork-session to branch off a copy.\r\n"
        ),
    )
    runner._debug = lambda *a, **k: None
    messages = []
    runner._restart_agent = lambda msg: messages.append(msg)

    # First death: fork once.
    assert runner._relaunch_backend_or_exit() is True
    assert runner._fork_next_spawn is True
    assert runner._forked_for_busy is True
    assert messages and "forked" in messages[0].lower()

    # If the fork ALSO dies, don't fork again — fall through to the normal guard.
    runner._restart_agent = lambda msg: messages.append("relaunch")
    runner._finalize_on_backend_exit = lambda: messages.append("finalize")
    runner._relaunch_backend_or_exit()
    assert runner._forked_for_busy is True  # not re-forked


def test_claude_spawn_command_fork_appends_flag():
    from pathlib import Path

    from agitrack.backends.proxy_agents import ClaudeProxyAgent

    agent = ClaudeProxyAgent()
    cmd = agent.spawn_command(Path("/repo"), session_id="abc", resume=True, fork=True, commit_guidance=False)
    assert cmd[:4] == ["claude", "--resume", "abc", "--fork-session"]
    # No fork flag when not forking.
    cmd_plain = agent.spawn_command(Path("/repo"), session_id="abc", resume=True, fork=False, commit_guidance=False)
    assert "--fork-session" not in cmd_plain


def test_relaunch_backend_resets_loop_guard_after_quiet_period(monkeypatch):
    runner = make_runner()
    runner._debug = lambda *a, **k: None
    relaunches = []
    runner._restart_agent = lambda msg: relaunches.append(1)
    runner._finalize_on_backend_exit = lambda: relaunches.append("finalize")

    t = [1000.0]
    monkeypatch.setattr("agitrack.proxy.runner.time.monotonic", lambda: t[0])
    for _ in range(3):
        runner._relaunch_backend_or_exit()
    t[0] += 60.0  # a minute later the old exits no longer count
    assert runner._relaunch_backend_or_exit() is True
    assert relaunches.count("finalize") == 0  # never gave up


def test_finalize_on_backend_exit_finalizes_once_and_clears_pid():
    runner = make_runner(child_pid=4321)
    calls = []

    def fake_finalize():  # mirror the real guard inside _finalize_pending_work
        if runner.__dict__.get("_finalized_on_exit"):
            return
        runner.__dict__["_finalized_on_exit"] = True
        calls.append("finalized")

    runner._finalize_pending_work = fake_finalize
    runner._debug = lambda *a, **k: None

    runner._finalize_on_backend_exit()
    runner._finalize_on_backend_exit()  # idempotent (guarded inside _finalize_pending_work)

    assert runner.child_pid is None
    assert calls == ["finalized"]


# --- startup resume + naming ---


def test_resumable_sessions_come_from_backend_repo_record():
    import types

    refs = [
        SessionRef(id="a", updated=1.0, label="old"),
        SessionRef(id="b", updated=3.0, label="new"),
        SessionRef(id="c", updated=2.0, label="mid"),
    ]

    def _list(repo):
        asked["repo"] = repo
        return list(refs)

    runner = make_runner(
        base_repo=types.SimpleNamespace(repo="/repo-root"),
        backend=types.SimpleNamespace(list_sessions=_list),
    )
    asked = {}

    result = runner._resumable_sessions()

    # Sourced from the repo aGiTrack launched in (not worktrees), newest first.
    assert asked["repo"] == "/repo-root"
    assert [ref.id for ref in result] == ["b", "c", "a"]


def _startup_runner():
    import types

    runner = make_runner()
    runner._prompt_startup_name = lambda continuing: "prompted-name"
    runner.root = types.SimpleNamespace(
        _names={},
        session_name_for=lambda sid: runner.root._names.get(sid),
        name_session=lambda sid, name: runner.root._names.__setitem__(sid, name),
    )
    return runner


def test_startup_name_keeps_stored_name_without_prompting():
    runner = _startup_runner()
    runner.root._names["sess-1"] = "alpha"

    assert runner._resolve_startup_session_name(runner.root, "sess-1", None) == "alpha"


def test_startup_name_uses_user_given_prior_worktree():
    runner = _startup_runner()

    # A non-auto prior worktree name counts as a name; an auto one does not.
    assert runner._resolve_startup_session_name(runner.root, "sess-1", "my-feature") == "my-feature"
    # And it is keyed by the conversation id so the link survives once the
    # last-session record moves on to another conversation.
    assert runner.root._names["sess-1"] == "my-feature"


def test_startup_name_prompts_when_unnamed_and_records_it():
    runner = _startup_runner()

    name = runner._resolve_startup_session_name(runner.root, "sess-1", "session-3")

    assert name == "prompted-name"
    assert runner.root._names["sess-1"] == "prompted-name"  # remembered for next time


# --- idle worktree base-sync ---


def test_sync_idle_worktrees_aligns_idle_skips_in_flight():
    import types

    runner = make_runner(
        repo="repoA",
        agent_in_flight=False,
    )
    busy = types.SimpleNamespace(repo="repoB", agent_in_flight=True)  # working -> skip
    idle = types.SimpleNamespace(repo="repoC", agent_in_flight=False)  # idle -> sync
    runner.sessions = [types.SimpleNamespace(repo="repoA", agent_in_flight=False), busy, idle]
    aligned = []
    runner._align_session_to_base = lambda repo: aligned.append(repo)

    runner._sync_idle_worktrees_to_base()

    # Active (idle) + idle background are re-pointed; the in-flight one is left alone.
    assert aligned == ["repoA", "repoC"]


# --- user-facing git commands operate on the base tree, not the worktree -------
#
# A session runs in a worktree that only contains tracked files, but the user's
# own untracked / intentionally-unstaged files live in the base working tree.
# These commands (git-unstaged / git-user-commit) must therefore read
# and write the base repo + base state, or the user's files are invisible.


def _user_git_runner(tmp_path, answers):
    from agitrack.git import GitRepo

    repo = GitRepo.init(tmp_path)  # seeds an initial commit; user files stay untracked
    runner = make_runner(
        repo=repo,
        base_repo=repo,
        _base_branch=repo.current_branch(),
        _user_declined=[],
    )
    runner.global_config = type("GC", (), {"default_backend": "claude"})()
    runner.prompts = []  # (title, body) of each popup shown
    scripted = list(answers)

    def prompt(title, body):
        runner.prompts.append((title, body))
        return scripted.pop(0) if scripted else None

    runner._prompt_popup = prompt
    return runner, repo


def test_git_status_returns_full_long_format(tmp_path):
    from agitrack.git import GitRepo

    repo = GitRepo.init(tmp_path)
    (tmp_path / "new.py").write_text("x\n", encoding="utf-8")

    output = repo.status()

    assert "Untracked files" in output  # long format, not --short
    assert "new.py" in output


def test_summarizer_model_picker_lists_models_and_defaults_to_smallest_for_claude(tmp_path, monkeypatch):
    import agitrack.summaries.model_select as model_select
    from proxy_helpers import make_runner

    runner = make_runner(state=AgitrackState(tmp_path))
    runner.global_config = type("GC", (), {"summarization_model": None})()
    runner._render = lambda: None
    runner.messages = []
    runner._set_message = lambda msg, **k: runner.messages.append(msg)
    runner.state.backend = "claude"
    runner.state.summarization_model = None

    monkeypatch.setattr(
        model_select,
        "list_available_models",
        lambda name: ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-8"],
    )
    captured: dict = {}

    def popup(title, options, **k):
        captured["options"] = options
        return options[0]  # accept the default (smallest)

    runner._select_popup = popup
    runner._handle_summarizer_command("model")

    # The smallest (Haiku) tier is listed first and flagged as the default.
    assert captured["options"][0].startswith("claude-haiku-4-5-20251001")
    assert "smallest" in captured["options"][0].lower()
    # All three tiers plus a "same as session" clear option are offered.
    assert "Same as the agent's session model" in captured["options"]
    assert len(captured["options"]) == 4
    # Choosing the default persists the Haiku model GLOBALLY (so it survives restarts
    # and session switches) and clears any per-session override.
    assert runner.global_config.summarization_model == "claude-haiku-4-5-20251001"
    assert runner.state.summarization_model is None


def test_summarizer_menu_set_model_returns_to_menu_then_esc_closes(tmp_path):
    # Esc goes up ONE level: opening "Set model" then Esc'ing the picker lands back on the
    # Summarizer menu (not all the way out); Esc on the Summarizer menu then closes it.
    from agitrack.config.settings import GlobalConfig
    from proxy_helpers import make_runner

    runner = make_runner(state=AgitrackState(tmp_path))
    runner.global_config = GlobalConfig(tmp_path / "global" / "config.json")
    runner._render = lambda *a, **k: None
    runner._set_message = lambda *a, **k: None
    runner._select_summarizer_model_popup = lambda: None  # simulate the picker being Esc'd
    titles: list[str] = []

    def select(title, options, **k):
        titles.append(title)
        if title == "Summarizer" and titles.count("Summarizer") == 1:
            return next(o for o in options if o.startswith("Set model"))
        return None  # second visit to the Summarizer menu → Esc closes it

    runner._select_popup = select
    runner._handle_summarizer_command("")
    assert titles.count("Summarizer") == 2  # menu re-shown after the picker returned


def test_session_menu_config_action_loops_back_then_esc_closes(tmp_path):
    # A configuration action (Share) returns to the sessions list so its Esc lands one
    # level up here; Esc on the list then closes the whole menu.
    runner = _delay_menu_runner(tmp_path)
    runner._share_session = lambda: None
    shown: list[bool] = []

    def select(title, options):
        shown.append(True)
        if len(shown) == 1:
            return next(o for o in options if o.startswith("⇪ Share"))
        return None  # close on the second visit

    runner._select_popup = select
    runner._session_menu()
    assert len(shown) == 2  # the list re-showed after Share, then Esc closed it


def test_summarizer_toggle_persists_to_global_config_across_restart(tmp_path):
    # Turning the summarizer off must survive an aGiTrack restart: the toggle is written
    # to the durable GLOBAL config, not the transient per-session worktree state (which is
    # removed on exit and resets to "on"). Regression for "always starts on".
    from agitrack.config.settings import GlobalConfig
    from proxy_helpers import make_runner

    cfg_path = tmp_path / "global" / "config.json"
    runner = make_runner(state=AgitrackState(tmp_path / "wt"))
    runner.global_config = GlobalConfig(cfg_path)
    runner._render = lambda: None
    runner._set_message = lambda *a, **k: None
    assert runner._summarization_enabled() is True  # default

    runner._handle_summarizer_command("off")
    assert runner.global_config.summarization_enabled is False
    assert runner._summarization_enabled() is False

    # Simulate a restart: a brand-new GlobalConfig reading the same file, and a fresh
    # session worktree state (which would default to "on" on its own).
    restarted = make_runner(state=AgitrackState(tmp_path / "wt2"))
    restarted.global_config = GlobalConfig(cfg_path)
    assert restarted.global_config.summarization_enabled is False
    assert restarted._summarization_enabled() is False  # stays OFF, not reset to on

    # And turning it back on persists too.
    restarted._render = lambda: None
    restarted._set_message = lambda *a, **k: None
    restarted._handle_summarizer_command("on")
    assert GlobalConfig(cfg_path).summarization_enabled is True


def test_summarizer_model_picker_clear_resets_to_session_model(tmp_path, monkeypatch):
    import agitrack.summaries.model_select as model_select
    from proxy_helpers import make_runner

    runner = make_runner(state=AgitrackState(tmp_path))
    runner.global_config = type("GC", (), {"summarization_model": None})()
    runner._render = lambda: None
    runner._set_message = lambda *a, **k: None
    runner.state.backend = "claude"
    runner.state.summarization_model = "claude-opus-4-8"  # previously pinned

    monkeypatch.setattr(
        model_select, "list_available_models", lambda name: ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"]
    )
    runner._select_popup = lambda title, options: options[-1]  # "Same as the agent's session model"
    runner._handle_summarizer_command("model")

    assert runner.global_config.summarization_model is None  # cleared globally → same as the session model
    assert runner.state.summarization_model is None


def test_summarizer_model_picker_falls_back_to_text_when_no_models(tmp_path, monkeypatch):
    import agitrack.summaries.model_select as model_select
    from proxy_helpers import make_runner

    runner = make_runner(state=AgitrackState(tmp_path))
    runner.global_config = type("GC", (), {"summarization_model": None})()
    runner._render = lambda: None
    runner._set_message = lambda *a, **k: None
    runner.state.backend = "opencode"
    runner.state.summarization_model = None

    monkeypatch.setattr(model_select, "list_available_models", lambda name: [])  # CLI unavailable
    runner._select_popup = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not show a picker"))
    runner._prompt_popup = lambda title, body, default="": "some/model"

    runner._handle_summarizer_command("model")

    assert runner.global_config.summarization_model == "some/model"  # typed value persisted globally


def test_status_line_unstaged_count_reflects_base_declined(tmp_path):
    runner, repo = _user_git_runner(tmp_path, answers=[])
    runner.name = "s1"
    runner.backend = type("B", (), {"name": "claude"})()
    runner.state = type("S", (), {"backend_session_id": None})()
    runner.worktree = object()
    runner.scroll_back = 0
    runner.cols = 120
    runner._user_declined = ["a.txt", "b.txt"]

    assert "unstaged:2" in runner._status_line()


# --- issue #12: user edits to the base tree are committed and synced on prompt -
#
# The user's editor works in the BASE repo, but the pre-agent check used to look
# only at the session worktree — so user edits were never committed, and (since
# the worktree only follows committed base HEAD moves) never reached the agent.


def _base_edit_runner(tmp_path, answers):
    from agitrack.git import GitRepo

    base = GitRepo.init(tmp_path)
    (tmp_path / "notes.txt").write_text("original\n", encoding="utf-8")
    base.stage_paths(["notes.txt"])
    base.commit("add notes")
    wt_path = tmp_path / ".agitrack" / "worktrees" / "session-1"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    base.worktree_add_detached(str(wt_path), base=base.current_branch())
    worktree = GitRepo(wt_path)

    runner = make_runner(
        base_repo=base,
        repo=worktree,
        worktree=object(),
        _base_branch=base.current_branch(),
        _base_advanced=False,
        _base_edits_declined_status=None,
        _integration_paused=False,
        agent_in_flight=False,
        agent_parse_thread=None,
        state=AgitrackState(wt_path),
        _user_declined=[],
        message=None,
        message_until=0.0,
    )
    runner.sessions = [types.SimpleNamespace(repo=worktree, agent_in_flight=False)]
    runner.global_config = type("GC", (), {"default_backend": "claude"})()
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._debug = lambda *a, **k: None
    runner.prompts = []
    scripted = list(answers)

    def prompt(title, body, **kwargs):
        runner.prompts.append((title, body))
        return scripted.pop(0) if scripted else None

    runner._prompt_popup = prompt
    return runner, base, worktree, wt_path


def test_pre_agent_commit_detects_and_syncs_base_user_edits(tmp_path):
    runner, base, worktree, wt_path = _base_edit_runner(tmp_path, answers=["save notes edit"])
    runner.pre_agent_reconciled_status = ""
    runner._finish_agent_parse_if_ready = lambda quiet: False
    runner.actions = types.SimpleNamespace(has_pre_agent_user_changes=lambda: False)

    # The user edits a tracked file in the BASE repo while the session is open.
    (tmp_path / "notes.txt").write_text("edited by the user\n", encoding="utf-8")

    assert runner._pre_agent_commit_if_needed("improve the parser") is True

    # The edit was committed to the base branch as a user commit...
    assert base.has_tracked_changes() is False
    subject = base._run(["git", "log", "-1", "--format=%s"]).stdout.strip()
    assert subject == "save notes edit"
    # ...and the session worktree was synced so the agent sees the edit.
    assert (wt_path / "notes.txt").read_text(encoding="utf-8") == "edited by the user\n"
    assert runner._base_edits_declined_status is None


def test_pre_agent_commit_stages_commits_and_syncs_a_new_base_file(tmp_path):
    # The field scenario: the user drops a NEW (untracked, unstaged) file into the base
    # repo — e.g. a screenshot. Before the prompt goes out, aGiTrack offers to stage it
    # ([y/N]); on "y" it commits the file to the base branch and merges that commit into
    # the session worktree, so both the repo and the agent get it.
    runner, base, worktree, wt_path = _base_edit_runner(tmp_path, answers=["y", "add screenshot"])
    runner.pre_agent_reconciled_status = ""
    runner._finish_agent_parse_if_ready = lambda quiet: False
    runner.actions = types.SimpleNamespace(has_pre_agent_user_changes=lambda: False)

    (tmp_path / "shot.png").write_bytes(b"PNGDATA")  # added but unstaged (untracked)

    assert runner._pre_agent_commit_if_needed("use the screenshot") is True

    # Staged + committed to the base branch...
    assert base._run(["git", "log", "-1", "--format=%s"]).stdout.strip() == "add screenshot"
    assert "shot.png" in base._run(["git", "show", "--stat", "--format="]).stdout
    # ...and merged into the worktree so the agent sees it.
    assert (wt_path / "shot.png").read_bytes() == b"PNGDATA"


def test_base_user_edit_declined_then_restaged_is_not_stranded(tmp_path):
    # Saying "N" to the stage prompt must not permanently strand the file: a later
    # pre-agent check still offers it (so the user can stage it then), and the "no" only
    # suppresses re-prompting until the base tree changes (the fingerprint gate).
    runner, base, worktree, wt_path = _base_edit_runner(tmp_path, answers=["N", "y", "add later"])
    runner.pre_agent_reconciled_status = ""
    runner._finish_agent_parse_if_ready = lambda quiet: False
    runner.actions = types.SimpleNamespace(has_pre_agent_user_changes=lambda: False)
    (tmp_path / "shot.png").write_bytes(b"PNGDATA")

    runner._pre_agent_commit_if_needed("first")  # user says N to staging
    assert "shot.png" not in base._run(["git", "ls-files"]).stdout  # not committed
    assert runner._base_user_edits_pending() is True  # still offered, not stranded

    runner._base_edits_declined_status = None  # simulate the base tree changing → re-offer
    runner._pre_agent_commit_if_needed("second")  # now user says y
    assert "shot.png" in base._run(["git", "ls-files"]).stdout  # committed this time
    assert (wt_path / "shot.png").read_bytes() == b"PNGDATA"  # and synced


def test_base_user_edit_decline_remembered_until_new_edits(tmp_path):
    runner, base, worktree, wt_path = _base_edit_runner(tmp_path, answers=[None])

    (tmp_path / "notes.txt").write_text("first edit\n", encoding="utf-8")
    runner._commit_base_user_edits_if_needed()  # popup shown; user cancels
    assert len(runner.prompts) == 1
    runner._commit_base_user_edits_if_needed()  # same state: no re-prompt
    assert len(runner.prompts) == 1
    # Nothing was committed and the worktree still has the original content.
    assert base.has_tracked_changes() is True
    assert (wt_path / "notes.txt").read_text(encoding="utf-8") == "original\n"

    # A FURTHER edit (same file, so `status --short` is unchanged) re-prompts.
    (tmp_path / "notes.txt").write_text("second edit\n", encoding="utf-8")
    runner._commit_base_user_edits_if_needed()
    assert len(runner.prompts) == 2


def test_base_user_untracked_file_counts_as_pending(tmp_path):
    runner, base, worktree, wt_path = _base_edit_runner(tmp_path, answers=[])

    assert runner._base_user_edits_pending() is False
    # A new, untracked (added-but-unstaged) file in the base repo is a pending edit, so
    # the pre-agent flow offers to stage/commit it.
    (tmp_path / "added.txt").write_text("new\n", encoding="utf-8")
    assert runner._base_user_edits_pending() is True
    # It keeps counting even after a prior "don't stage" answer, so it can never become
    # permanently un-committable; re-prompt nagging is handled by the fingerprint gate.
    runner._user_state().add_declined(["added.txt"])
    assert runner._base_user_edits_pending() is True


def test_resume_pending_prompt_checks_base_user_edits(tmp_path):
    read_fd, write_fd = os.pipe()
    try:
        runner = make_runner(
            master_fd=write_fd,
            pending_forwarded=[b"\r"],
            pending_prompt_text="fix it",
            passthrough_prompt=bytearray(b"fix it"),
            state=AgitrackState(tmp_path),
            agent_parse_thread=None,
            agent_in_flight=False,
            screen=None,
            message=None,
            message_until=0.0,
        )
        runner._finish_agent_parse_if_ready = lambda quiet: False
        runner.actions = types.SimpleNamespace(has_pre_agent_user_changes=lambda: False)
        runner._ensure_turn_branch = lambda: None
        checked = []
        runner._commit_base_user_edits_if_needed = lambda: checked.append(True)

        runner._resume_pending_prompt_if_ready()

        assert checked == [True]  # base edits are handled before the prompt goes out
        assert os.read(read_fd, 1) == b"\r"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_agent_commit_failed_attempt_does_not_double_count_tokens(tmp_path):
    # Issue #14: a commit attempt that finds nothing to stage returns False
    # without advancing last_backend_message_id, so the next parse re-processes
    # the same turns. Token usage is cumulative state — it must only be added
    # once the commit actually happens, or the metadata overstates real usage
    # once per failed attempt.
    runner = make_runner(
        repo=FakeCommitRepo(),
        state=AgitrackState(tmp_path),
        verbose=False,
    )
    repo = runner.repo
    runner._review_untracked_popup = lambda include_declined: "No untracked files to review."

    first_turn = SessionTurn("u1", "a1", "fix it", "done", TokenUsage(total=140, input=130, output=10), None)

    # First attempt: the turn left nothing staged (e.g. the agent reverted its
    # edit) — no commit, and crucially no token accumulation.
    repo.has_staged_changes = lambda: False
    assert (
        runner._create_agent_commit_from_turns_popup(
            turns=[first_turn],
            backend="claude",
            backend_session_id="ses-1",
            model="m",
            quiet=True,
        )
        is False
    )
    assert runner.state.pending_token_usage()["input"] == 0

    # Next parse returns the same turn again plus a new one; this time changes
    # are staged and the commit happens. Each turn must be counted exactly once.
    repo.has_staged_changes = lambda: True
    second_turn = SessionTurn("u2", "a2", "now edit", "edited", TokenUsage(total=5, input=3, output=2), None)
    assert (
        runner._create_agent_commit_from_turns_popup(
            turns=[first_turn, second_turn],
            backend="claude",
            backend_session_id="ses-1",
            model="m",
            quiet=True,
        )
        is True
    )
    message = repo.message
    assert "tokens_since_last_commit_input: 133" in message
    assert "tokens_since_last_commit_output: 12" in message


def test_actions_agent_commit_failed_attempt_does_not_double_count(tmp_path):
    from agitrack.commits import AgitrackActions

    class Repo:
        def __init__(self):
            self.staged = False
            self.message = None

        def add_tracked(self):
            pass

        def untracked_files(self):
            return []

        def has_staged_changes(self):
            return self.staged

        def commit(self, message):
            self.message = message

    repo = Repo()
    state = AgitrackState(tmp_path)
    actions = AgitrackActions(repo, state)
    turn = SessionTurn("u1", "a1", "fix it", "done", TokenUsage(total=140, input=130, output=10), None)

    assert (
        actions.create_agent_commit_from_turns(
            turns=[turn],
            backend="claude",
            backend_session_id="ses-1",
            model="m",
            quiet=True,
        )
        is False
    )
    # Nothing staged: neither tokens nor trace were accumulated.
    assert state.pending_token_usage()["input"] == 0
    assert state.pending_trace() == []

    repo.staged = True
    assert (
        actions.create_agent_commit_from_turns(
            turns=[turn],
            backend="claude",
            backend_session_id="ses-1",
            model="m",
            quiet=True,
        )
        is True
    )
    assert "tokens_since_last_commit_input: 130" in repo.message
    assert repo.message.count("## User\n\nfix it") == 1


# --- issue #15: a parse worker must not straddle a session switch ---------------


def test_parse_worker_delivers_to_its_own_session_after_switch(tmp_path):

    release = threading.Event()
    state_a = AgitrackState(tmp_path / "a")
    state_b = AgitrackState(tmp_path / "b")
    exported = ExportedSession(session_id="ses-a", model=None, updated=None, turns=[])

    class Backend:
        name = "claude"

        def latest_session_id(self, repo):
            return "ses-a"

        def export_session(self, repo, session_id):
            release.wait(timeout=5)  # hold the worker mid-flight
            return exported

    runner = make_runner(
        backend=Backend(),
        repo=types.SimpleNamespace(repo="/wt-a"),
        state=state_a,
        worktree=object(),
        agent_parse_thread=None,
        agent_parse_result=None,
        agent_parse_active=False,
        agent_parse_lock=threading.Lock(),
    )
    runner._debug = lambda *a, **k: None

    assert runner._start_agent_parse() is True

    # The user switches sessions while the worker is still running: session A's
    # runtime stays on its own Session object and session B's becomes active.
    session_a = runner.active
    runner.sessions = [session_a]
    runner.active = Session.bare()
    runner.state = state_b
    runner.backend = types.SimpleNamespace(name="claude")
    runner.repo = types.SimpleNamespace(repo="/wt-b")
    runner.agent_parse_thread = None
    runner.agent_parse_result = None
    runner.agent_parse_active = False
    runner.agent_parse_lock = threading.Lock()

    release.set()
    session_a.agent_parse_thread.join(timeout=5)

    # The result reached session A's own Session object, tagged with A's state
    # — nothing leaked into the now-active session B.
    assert session_a.agent_parse_result is not None
    assert session_a.agent_parse_result[1] is exported
    assert session_a.agent_parse_result[3] is state_a
    assert session_a.agent_parse_active is False
    assert runner.agent_parse_result is None
    assert runner.agent_parse_active is False


def test_finish_agent_parse_discards_result_owned_by_another_session(tmp_path):
    complete = ExportedSession(
        session_id="ses-old",
        model="m",
        updated=None,
        turns=[SessionTurn("u1", "a1", "fix it", "done", TokenUsage(), None)],
    )
    runner = _parse_ready_runner(tmp_path, complete)
    # The result was produced for a different session's state (e.g. captured
    # before a switch); applying it here would re-commit or cross-attribute turns.
    session_id, session, last_message_id, _ = runner.agent_parse_result
    runner.agent_parse_result = (session_id, session, last_message_id, AgitrackState(tmp_path / "other"))

    result = runner._finish_agent_parse_if_ready(quiet=True)

    assert result is None
    assert runner.commits == []
    assert runner.agent_parse_result is None  # consumed, not retried forever
    assert runner.state.backend_session_id is None  # no cross-session adoption


def test_switch_active_joins_worker_before_swapping():

    events = []

    class FakeThread:
        def is_alive(self):
            return True

        def join(self, timeout=None):
            events.append(("join", timeout))

    runner = make_runner(
        agent_parse_thread=FakeThread(),
        agent_parse_lock=threading.Lock(),
        scroll_back=3,
    )
    runner.sessions = [runner.active, Session.bare()]
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._resize_child = lambda: None
    runner._enable_host_mouse = lambda: None
    runner._restart_agent = lambda msg: None  # Windows switch path; no-op on the fake
    runner._session_name = lambda index: f"s{index}"

    runner._switch_active(1)

    assert events and events[0][0] == "join"  # waited before swapping
    assert runner.active_index == 1


def test_debug_and_raw_logs_write_to_base_repo_when_enabled(tmp_path):
    # The diagnostic logs (DEBUG_PROXY -> proxy-debug, DEBUG_RAW -> proxy-raw) must behave the
    # same on every platform: written to the BASE repo's .agitrack/, one shared run-stamp per
    # run, and gated so nothing is written when the switch is off. Runs on both CI OSes.
    import types

    base = tmp_path / "base"
    base.mkdir()
    runner = make_runner()
    runner.base_repo = types.SimpleNamespace(repo=base)
    runner._diag_run = "20260627-101112"

    # Enabled: both logs are written under the base repo, named by the shared run-stamp.
    runner.debug_proxy = True
    runner.raw_capture = True
    runner._debug("session switch start")
    runner._raw_capture(">", b"\x1b[A")

    debug_log = base / ".agitrack" / "proxy-debug-20260627-101112.log"
    raw_log = base / ".agitrack" / "proxy-raw-20260627-101112.log"
    assert "session switch start" in debug_log.read_text(encoding="utf-8")
    raw_text = raw_log.read_text(encoding="utf-8")
    assert "> b'\\x1b[A'" in raw_text  # tag + byte-exact repr

    # Disabled: a second run writes nothing new (the gate is honoured on both methods).
    runner.debug_proxy = False
    runner.raw_capture = False
    runner._diag_run = "20260627-202122"
    runner._debug("must not appear")
    runner._raw_capture("<", b"x")
    assert not (base / ".agitrack" / "proxy-debug-20260627-202122.log").exists()
    assert not (base / ".agitrack" / "proxy-raw-20260627-202122.log").exists()


def test_switch_active_resumes_in_place_without_interrupting_target(monkeypatch):
    # Switching to a session must KEEP its backend running — resume the existing ConPTY in
    # place, never teardown/respawn it. A teardown would call interrupt() (Ctrl-C/ETX) on the
    # session being switched to and cancel its in-progress turn. So _switch_active must NOT call
    # _restart_agent on any platform; it resizes + resumes. On Windows it additionally clears the
    # host terminal-mode mirror (host-side only — never touches a backend's ConPTY).
    import agitrack.proxy.runner as proxy_mod

    runner = _mux_runner()
    runner.sessions.append(_bg_session("B"))
    runner._session_name = lambda index: f"s{index}"

    restarts: list[str] = []
    resizes: list[int] = []
    host_resets: list[int] = []
    runner._restart_agent = lambda msg: restarts.append(msg)
    runner._resize_child = lambda: resizes.append(1)
    runner._disable_host_terminal_modes = lambda: host_resets.append(1)

    monkeypatch.setattr(proxy_mod.os, "name", "nt")
    runner._switch_active(1)
    assert runner.active_index == 1 and runner.repo == "repoB"  # swapped
    assert resizes and not restarts  # in-place resume, the target backend is never respawned
    assert host_resets  # host mode mirror cleared on Windows

    restarts.clear()
    resizes.clear()
    host_resets.clear()
    monkeypatch.setattr(proxy_mod.os, "name", "posix")
    runner._switch_active(0)
    assert runner.active_index == 0 and runner.repo == "repoA"  # swapped back
    assert resizes and not restarts  # in-place resume on POSIX too
    assert not host_resets  # POSIX path leaves host modes alone


# --- issue #18: Ctrl-C inside a popup goes through the full exit flow -----------


def _popup_exit_runner():
    runner = make_runner()
    runner.events = []
    runner._finalize_pending_work = lambda: runner.events.append("finalize")
    runner._exit_child = lambda: runner.events.append("exit")
    return runner


def test_popup_exit_flow_declined_keeps_working():
    runner = _popup_exit_runner()
    runner._confirm_exit = lambda: False

    assert runner._run_exit_flow() is False
    assert runner.events == []  # neither finalized nor exited


def test_popup_exit_flow_confirmed_finalizes_then_exits():
    runner = _popup_exit_runner()
    runner._confirm_exit = lambda: True
    runner._confirm_terminate_background_sessions = lambda: True

    assert runner._run_exit_flow() is True
    assert runner.events == ["finalize", "exit"]  # commits before leaving


def test_popup_exit_flow_background_decline_keeps_working():
    runner = _popup_exit_runner()
    runner._confirm_exit = lambda: True
    runner._confirm_terminate_background_sessions = lambda: False

    assert runner._run_exit_flow() is False
    assert runner.events == []


def test_popup_exit_flow_aborted_by_esc_on_copy_offer_stays_running():
    # Esc on the on-exit copy offer sets _exit_aborted; the exit flow then stays running
    # (no _exit_child), restores the exiting state, and shows a clear "Exit cancelled" message.
    runner = _popup_exit_runner()
    runner._confirm_exit = lambda: True
    runner._confirm_terminate_background_sessions = lambda: True
    runner._exiting = True
    runner._finalized_on_exit = True
    msgs: list[str] = []
    runner._set_message = lambda m, **k: msgs.append(m)
    runner._render = lambda *a, **k: None

    def finalize():
        runner.events.append("finalize")
        runner._exit_aborted = True  # simulate Esc on the copy offer deep in finalize

    runner._finalize_pending_work = finalize

    assert runner._run_exit_flow() is False  # exit cancelled
    assert runner.events == ["finalize"]  # finalized (commits stand) but did NOT exit
    assert runner._exiting is False and runner._finalized_on_exit is False  # state restored
    assert any("Exit cancelled" in m for m in msgs)


def test_popup_exit_flow_double_ctrl_c_still_finalizes():
    runner = _popup_exit_runner()

    def confirm_via_popup():
        # A second Ctrl-C arrives inside the confirmation popup itself: the
        # nested request flags force-exit and the popup returns None (-> False).
        assert runner._run_exit_flow() is True
        return False

    runner._confirm_exit = confirm_via_popup

    assert runner._run_exit_flow() is True
    # Even the emphatic double Ctrl-C exits gracefully: finalize, then exit.
    assert runner.events == ["finalize", "exit"]


def test_prompt_popup_ctrl_c_routes_through_exit_flow():
    runner = make_runner()
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._clear_message = lambda: None

    # Exiting: Ctrl-C makes the popup return None once the exit flow ran.
    calls = []
    runner._popup_read_input = lambda: b"\x03"
    runner._run_exit_flow = lambda: (calls.append("flow"), True)[1]
    assert runner._prompt_popup("Title", "Prompt") is None
    assert calls == ["flow"]

    # Declined: the popup keeps running and still accepts input afterwards.
    feed = iter([b"\x03", b"o", b"k", b"\r"])
    runner._popup_read_input = lambda: next(feed)
    runner._run_exit_flow = lambda: False
    assert runner._prompt_popup("Title", "Prompt") == "ok"


def test_select_popup_ctrl_c_routes_through_exit_flow():
    runner = make_runner()
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._clear_message = lambda: None

    runner._popup_read_input = lambda: b"\x03"
    runner._run_exit_flow = lambda: True
    assert runner._select_popup("Pick", ["a", "b"]) is None

    feed = iter([b"\x03", b"\r"])
    runner._popup_read_input = lambda: next(feed)
    runner._run_exit_flow = lambda: False
    assert runner._select_popup("Pick", ["a", "b"]) == "a"


@_posix_only
def test_spawn_failed_exec_child_exits_with_127(tmp_path):
    # Issue #20: if execvp fails in the forked child (binary gone, PATH change,
    # worktree deleted), the child must die — not keep running aGiTrack's own
    # Python code from the fork point as a duplicate process.
    runner = make_runner(
        state=AgitrackState(tmp_path),
        repo=types.SimpleNamespace(repo=tmp_path),
        worktree=None,
        backend=types.SimpleNamespace(
            new_session_id=lambda: "ses-1",
            spawn_command=lambda repo, session_id, resume, fork=False, commit_guidance=True, use_worktrees=True, executable=None: [
                "agit-test-binary-that-does-not-exist"
            ],
            list_sessions=lambda repo: [],
        ),
    )
    runner._should_continue_session = lambda: False

    runner._spawn()

    _, status = os.waitpid(runner.child_pid, 0)
    assert os.waitstatus_to_exitcode(status) == 127
    os.close(runner.master_fd)


# --- issue #21: stopped backends are reaped, not left as zombies ----------------


@_posix_only
def test_terminate_child_queues_pid_and_reaper_collects_it():
    runner = make_runner(master_fd=None)
    pid = os.fork()
    if pid == 0:
        os._exit(0)  # the "backend" exits as soon as it is signalled
    runner.child_pid = pid

    runner._terminate_child()

    assert runner.child_pid is None
    assert pid in runner._reap_pids  # queued for the loop's reaper

    deadline = time.monotonic() + 2.0
    while pid in runner._reap_pids and time.monotonic() < deadline:
        runner._reap_stopped_children()
        time.sleep(0.01)
    assert runner._reap_pids == []
    # Fully reaped: the pid is no longer a child (zombie) of this process.
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


@_posix_only
def test_reaper_keeps_still_running_children():
    import signal as signal_mod

    runner = make_runner()
    pid = os.fork()
    if pid == 0:
        time.sleep(30)
        os._exit(0)
    runner._reap_pids = [pid]

    runner._reap_stopped_children()
    assert runner._reap_pids == [pid]  # still running: kept for later

    os.kill(pid, signal_mod.SIGKILL)
    os.waitpid(pid, 0)


# --- issue #22: popups keep draining PTYs while waiting for input ---------------


def _popup_io_runner(monkeypatch, stdin_fd):
    import agitrack.proxy.runner as proxy_mod

    runner = make_runner(
        master_fd=None,
        last_child_output=0.0,
        last_child_output_sample=b"",
    )
    monkeypatch.setattr(proxy_mod.sys, "stdin", types.SimpleNamespace(fileno=lambda: stdin_fd))
    runner.sessions = []
    runner._answer_terminal_queries = lambda output: None
    runner._sync_terminal_modes = lambda output: None
    runner._track_sync_update = lambda output: None
    runner._feed_child_output = lambda output: None
    return runner


@_posix_only
def test_popup_read_input_drains_active_pty_while_waiting(monkeypatch):
    stdin_r, stdin_w = os.pipe()
    child_r, child_w = os.pipe()
    try:
        runner = _popup_io_runner(monkeypatch, stdin_r)
        runner.master_fd = child_r
        fed = []
        runner._feed_child_output = lambda output: fed.append(output)

        # The backend streams while the popup is open; without draining, its
        # writes would eventually block on a full PTY buffer and stall it.
        os.write(child_w, b"streamed while popup open")
        os.write(stdin_w, b"x")

        assert runner._popup_read_input() == b"x"
        assert fed == [b"streamed while popup open"]  # screen model stayed fed
    finally:
        for fd in (stdin_r, stdin_w, child_r, child_w):
            os.close(fd)


@_posix_only
def test_popup_read_input_pumps_background_sessions(monkeypatch):
    stdin_r, stdin_w = os.pipe()
    bg_r, bg_w = os.pipe()
    try:
        runner = _popup_io_runner(monkeypatch, stdin_r)
        background = types.SimpleNamespace(master_fd=bg_r)
        runner.sessions = [None, background]
        runner.active_index = 0
        pumped = []
        runner._pump_background = lambda session: pumped.append(session)

        os.write(bg_w, b"background output")
        os.write(stdin_w, b"\r")

        assert runner._popup_read_input() == b"\r"
        assert pumped == [background]
    finally:
        for fd in (stdin_r, stdin_w, bg_r, bg_w):
            os.close(fd)


@_posix_only
def test_popup_read_input_survives_child_eof(monkeypatch):
    stdin_r, stdin_w = os.pipe()
    child_r, child_w = os.pipe()
    try:
        runner = _popup_io_runner(monkeypatch, stdin_r)
        runner.master_fd = child_r
        os.close(child_w)  # the backend died while the popup was open
        os.write(stdin_w, b"y")

        # No crash and no busy loop: the dead fd is dropped and the keypress
        # still arrives; the main loop handles the exit afterwards.
        assert runner._popup_read_input() == b"y"
    finally:
        for fd in (stdin_r, stdin_w, child_r):
            os.close(fd)


# --- issue #25: proxy commands and README stay in sync --------------------------


def test_git_unstaged_command_lists_declined_files(tmp_path):
    runner, repo = _user_git_runner(tmp_path, answers=[])
    (tmp_path / "kept.txt").write_text("x\n", encoding="utf-8")
    state = runner._user_state()
    state.add_declined(["kept.txt"])
    messages = []
    runner._set_message = lambda text, **k: messages.append(text)
    runner._render = lambda: None

    runner._run_command("git-unstaged")

    assert any("kept.txt" in message for message in messages)

    # Once nothing is declined any more, it says so instead.
    state.remove_declined(["kept.txt"])
    runner._run_command("git-unstaged")
    assert messages[-1] == "No intentionally unstaged files."


def test_readme_proxy_command_list_matches_implementation():
    import re
    from pathlib import Path

    readme = Path(__file__).resolve().parents[1] / "README.md"
    block = re.search(r"```text\n(.*?)```", readme.read_text(encoding="utf-8"), re.S).group(1)
    # The command is the field before the 2+-space column gap, so multi-word commands
    # (e.g. "exit aGiTrack") are captured whole rather than truncated to their first word.
    documented = {re.split(r"\s{2,}", line.strip(), maxsplit=1)[0] for line in block.strip().splitlines()}

    assert documented == set(ProxyInput.COMMANDS)


def test_sync_tracked_session_skips_empty_newest_session(tmp_path):
    # Issue #26: syncing must not adopt Claude's freshly-minted EMPTY session
    # (newest by mtime, no content) — the same blank-resume trap
    # claude_session.latest_session_id avoids.
    refs = [
        SessionRef("with-content", 100.0, label="fix the parser"),
        SessionRef("empty-newest", 200.0, label=None),
    ]
    runner = _runner_with_sessions(refs)
    runner.state = AgitrackState(tmp_path)
    runner._initialize_session_baseline = lambda: None
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None

    runner._sync_tracked_session()

    assert runner.state.backend_session_id == "with-content"


# --- issue #27: double Ctrl-C exits gracefully -----------------------------------


def test_exit_command_routes_through_unified_exit_flow(tmp_path):
    runner = make_runner()
    events = []
    runner._confirm_exit = lambda: (events.append("confirm"), True)[1]
    runner._confirm_terminate_background_sessions = lambda: True
    runner._finalize_pending_work = lambda: events.append("finalize")
    runner._exit_child = lambda: events.append("exit")

    runner._run_command("exit")

    assert events == ["confirm", "finalize", "exit"]
    # The exit flow must flag the reactor loop to stop. Without this the loop
    # falls through to the timers phase and runs git in the just-removed worktree
    # (FileNotFoundError on exit).
    assert runner._exit_requested is True


def test_exit_command_cancelled_does_not_request_exit(tmp_path):
    # Declining the exit confirmation keeps aGiTrack running: the loop-break flag
    # stays clear.
    runner = make_runner()
    runner._confirm_exit = lambda: False
    runner._render = lambda: None

    runner._run_command("exit")

    assert runner._exit_requested is False


def test_double_ctrl_c_finalizes_before_exiting():
    # Issue #27: the second Ctrl-C lands inside the exit-confirmation popup; it
    # must exit immediately but still gracefully — finalize runs, nothing is
    # skipped. (The non-graceful path used to call _exit_child() directly.)
    runner = make_runner()
    events = []
    runner._finalize_pending_work = lambda: events.append("finalize")
    runner._exit_child = lambda: events.append("exit")
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    runner._clear_message = lambda: None
    # The confirmation popup itself: the user answers with another Ctrl-C.
    feed = iter([b"\x03"])
    runner._popup_read_input = lambda: next(feed)
    runner._confirm_terminate_background_sessions = lambda: (_ for _ in ()).throw(AssertionError("skipped on force"))

    # First Ctrl-C: the loop starts the exit flow, which opens the real
    # _confirm_exit popup; the second Ctrl-C arrives inside it.
    assert runner._run_exit_flow() is True
    assert events == ["finalize", "exit"]


# --- issue #28: backend keybindings work through the proxy ----------------------


def test_sync_terminal_modes_mirrors_keyboard_protocol(monkeypatch):
    import agitrack.proxy.runner as proxy_mod

    runner = make_runner(child_mouse=False)
    runner.host_kitty_keyboard = True  # host speaks the kitty protocol
    writes = []
    monkeypatch.setattr(proxy_mod.os, "write", lambda fd, data: writes.append(data))

    # Claude/OpenCode negotiate enhanced key encodings (Shift+Enter etc.):
    # kitty protocol push/pop and xterm modifyOtherKeys. The host terminal
    # must see these or it keeps sending a plain \r for Shift+Enter.
    runner._sync_terminal_modes(b"hello\x1b[>1u world \x1b[>4;2m text \x1b[<u\x1b[>4;0m")

    assert b"\x1b[>1u" in writes  # kitty push
    assert b"\x1b[>4;2m" in writes  # modifyOtherKeys on
    assert b"\x1b[<u" in writes  # kitty pop
    assert b"\x1b[>4;0m" in writes  # modifyOtherKeys off
    # Only the negotiation sequences are mirrored, never the text around them.
    assert all(payload.startswith(b"\x1b[") for payload in writes)


def test_sync_terminal_modes_skips_kitty_on_unsupported_host(monkeypatch):
    # On a host that doesn't speak the kitty protocol (e.g. the raw Linux console),
    # the kitty push/pop must NOT be mirrored — they'd leak as visible codes. The
    # modifyOtherKeys form is an ordinary CSI and is still mirrored.
    import agitrack.proxy.runner as proxy_mod

    runner = make_runner(child_mouse=False)
    runner.host_kitty_keyboard = False
    writes = []
    monkeypatch.setattr(proxy_mod.os, "write", lambda fd, data: writes.append(data))

    runner._sync_terminal_modes(b"hello\x1b[>1u world \x1b[>4;2m text \x1b[<u\x1b[>4;0m")

    assert b"\x1b[>1u" not in writes  # kitty push suppressed
    assert b"\x1b[<u" not in writes  # kitty pop suppressed
    assert b"\x1b[>4;2m" in writes  # modifyOtherKeys still mirrored
    assert b"\x1b[>4;0m" in writes


def test_sync_terminal_modes_windows_drops_hover_motion_keeps_drag(monkeypatch):
    # On Windows the host floods 1003 (any-event/hover) motion that doesn't round-trip cleanly
    # and leaks into the backend as literal `[<35;..M` text. Mirror 1002 (button-event/drag, for
    # drag-select copy), button (1000), and SGR (1006), but NOT 1003. POSIX mirrors all.
    import agitrack.proxy.runner as proxy_mod

    backend_modes = b"\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h"

    runner = make_runner(child_mouse=False)
    writes: list[bytes] = []
    monkeypatch.setattr(proxy_mod.os, "write", lambda fd, data: writes.append(data))

    monkeypatch.setattr(proxy_mod.os, "name", "nt")
    runner._sync_terminal_modes(backend_modes)
    assert b"\x1b[?1000h" in writes  # button mirrored
    assert b"\x1b[?1002h" in writes  # drag mirrored (copy works)
    assert b"\x1b[?1006h" in writes  # SGR mirrored
    assert b"\x1b[?1003h" not in writes  # hover NOT mirrored (the flood/leak)

    writes.clear()
    monkeypatch.setattr(proxy_mod.os, "name", "posix")
    runner._sync_terminal_modes(backend_modes)
    assert b"\x1b[?1003h" in writes  # POSIX mirrors hover too (no leak there)


def test_disable_host_terminal_modes_pops_kitty_only_when_supported(monkeypatch):
    import agitrack.proxy.runner as proxy_mod

    writes = []
    monkeypatch.setattr(proxy_mod.os, "write", lambda fd, data: writes.append(data))

    runner = make_runner()
    runner.host_kitty_keyboard = False
    runner._disable_host_terminal_modes()
    blob = b"".join(writes)
    assert b"\x1b[<u" not in blob  # no kitty pop sent to an unsupporting host
    assert b"\x1b[?1000l" in blob  # ordinary mode resets still happen
    assert b"\x1b[>4;0m" in blob  # modifyOtherKeys reset stays unconditional

    writes.clear()
    runner.host_kitty_keyboard = True
    runner._disable_host_terminal_modes()
    assert b"\x1b[<u" in b"".join(writes)  # kitty pop sent when supported


def test_backend_exit_relaunches_and_resumes():
    # Claude exiting via Esc on its native session picker keeps the existing
    # recover-and-resume behavior.
    runner = make_runner(child_pid=1234)
    events = []
    runner._debug = lambda *a, **k: None
    runner._finalize_on_backend_exit = lambda: events.append("finalize")
    runner._restart_agent = lambda message: events.append("relaunch")

    assert runner._relaunch_backend_or_exit() is True
    assert events == ["relaunch"]


# --- Esc interrupts and newline keybindings must not stall commits/merges -------


def test_await_followup_skips_slash_commands(tmp_path):
    # /model, /compact etc. never appear as transcript turns (only as filtered
    # <command-name> rows). Awaiting one deferred every commit for the rest of
    # the session — the observed "agit stopped merging after I used /model".
    runner = make_runner(_awaited_followups=[])

    runner._await_followup("/model")
    runner._await_followup("/compact some args")
    assert runner._awaited_followups == []

    runner._await_followup("fix the tests")
    assert runner._awaited_followups == ["fix the tests"]


def test_finish_agent_parse_interrupt_clears_awaited_followups(tmp_path):
    # Esc makes Claude discard its queued prompts: awaited entries can never
    # land, so an interrupted turn must clear the queue and let commits flow.
    interrupted_session = ExportedSession(
        session_id="ses-1",
        model="m",
        updated=None,
        turns=[SessionTurn("u1", "a1", "fix it", "partial work", TokenUsage(), None, complete=True, interrupted=True)],
    )
    runner = _parse_ready_runner(tmp_path, interrupted_session)
    runner._awaited_followups = ["a prompt that was discarded by the interrupt"]
    runner.agent_in_flight = True  # agent looks active (e.g. UI repainting)
    runner.last_child_output = 0.0

    result = runner._finish_agent_parse_if_ready(quiet=True)

    assert result is True  # committed instead of deferring forever
    assert runner._awaited_followups == []
    assert len(runner.commits) == 1


def test_finish_agent_parse_interrupted_dangling_turn_still_commits(tmp_path):
    # The interrupted turn parses as complete, so a user who Esc's and walks
    # away still gets the turn's work committed on the idle debounce.
    interrupted_session = ExportedSession(
        session_id="ses-1",
        model="m",
        updated=None,
        turns=[SessionTurn("u1", "a1", "fix it", "got partway", TokenUsage(), None, complete=True, interrupted=True)],
    )
    runner = _parse_ready_runner(tmp_path, interrupted_session)

    assert runner._finish_agent_parse_if_ready(quiet=True) is True
    assert len(runner.commits) == 1


def _submit_runner(prompt=b""):
    runner = make_runner(passthrough_prompt=bytearray(prompt))
    return runner


def test_plain_enter_submits():
    assert _submit_runner(b"fix it")._forwarded_submits([b"f", b"\r"]) is True
    assert _submit_runner()._forwarded_submits([b"\r"]) is True


def test_alt_enter_is_a_newline_not_a_submit():
    # Option/Alt+Enter sends ESC CR — Claude's newline-in-input on terminals
    # without the kitty protocol (e.g. Apple Terminal with Option-as-Meta).
    assert _submit_runner(b"first line")._forwarded_submits([b"\x1b", b"\r"]) is False


def test_backslash_enter_is_a_line_continuation_not_a_submit():
    # "\<Enter>" typed in one read...
    chunks = [bytes([byte]) for byte in b"some text\\\r"]
    assert _submit_runner()._forwarded_submits(chunks) is False
    # ...and the Enter arriving in its own read after the backslash.
    assert _submit_runner(b"some text\\")._forwarded_submits([b"\r"]) is False


def test_bracketed_paste_newlines_are_content_not_submits():
    paste = b"\x1b[200~line one\rline two\x1b[201~"
    chunks = [bytes([byte]) for byte in paste]
    assert _submit_runner()._forwarded_submits(chunks) is False
    # An unterminated paste (split across reads) is held too.
    open_paste = [bytes([byte]) for byte in b"\x1b[200~abc\r"]
    assert _submit_runner()._forwarded_submits(open_paste) is False
    # A real Enter after the paste closed still submits.
    paste_then_enter = [bytes([byte]) for byte in b"\x1b[200~abc\x1b[201~"] + [b"\r"]
    assert _submit_runner()._forwarded_submits(paste_then_enter) is True


def test_idle_clean_worktree_integrates_agent_made_commits():
    runner = make_runner(
        worktree=object(),
        merge_ctx=None,
        _integration_paused=False,
        _base_branch="main",
        agent_in_flight=False,
        agent_parse_thread=None,
        last_child_output=0.0,
        repo=types.SimpleNamespace(current_branch=lambda: "agit/claude/s1/t1"),
    )
    runner.CHILD_IDLE_SECONDS = 4.0
    runner.BASE_POLL_SECONDS = 3.0
    runner._idle_integrate_at = 0.0
    runner._debug = lambda *a, **k: None
    runner.base_repo = types.SimpleNamespace(log_range=lambda base, head: "abc123 manual agent commit")
    integrations = []
    runner._integrate_session_turn = lambda: integrations.append(1)

    runner._integrate_agent_made_commits_if_idle(time.monotonic())
    assert integrations == [1]

    # Throttled: an immediate second pass does not re-integrate.
    runner._integrate_agent_made_commits_if_idle(time.monotonic())
    assert integrations == [1]


def test_idle_integration_skips_active_agent_and_clean_branches():
    runner = make_runner(
        worktree=object(),
        merge_ctx=None,
        _integration_paused=False,
        _base_branch="main",
        agent_parse_thread=None,
        last_child_output=0.0,
        repo=types.SimpleNamespace(current_branch=lambda: "agit/claude/s1/t1"),
    )
    runner.CHILD_IDLE_SECONDS = 4.0
    runner.BASE_POLL_SECONDS = 3.0
    runner._debug = lambda *a, **k: None
    integrations = []
    runner._integrate_session_turn = lambda: integrations.append(1)

    # Agent active: leave the branch alone.
    runner._idle_integrate_at = 0.0
    runner.agent_in_flight = True
    runner.base_repo = types.SimpleNamespace(log_range=lambda base, head: "abc123 pending")
    runner._integrate_agent_made_commits_if_idle(time.monotonic())
    assert integrations == []

    # Idle but nothing ahead of base: nothing to do.
    runner.agent_in_flight = False
    runner._idle_integrate_at = 0.0
    runner.base_repo = types.SimpleNamespace(log_range=lambda base, head: "")
    runner._integrate_agent_made_commits_if_idle(time.monotonic())
    assert integrations == []


# ---------------------------------------------------------------------------
# ScreenRenderer unit tests (P1 extraction; constructed directly, no
# ProxyRunner.__new__)
# ---------------------------------------------------------------------------

from agitrack.proxy.renderer import ScreenRenderer


def _make_renderer(rows=24, cols=80, color_mode="truecolor"):
    """Create a fresh ScreenRenderer with an initialized pyte screen."""
    r = ScreenRenderer(rows, cols, color_mode=color_mode)
    r.init_screen(rows, cols)
    return r


def test_screen_renderer_init_screen_creates_screen():
    r = _make_renderer(10, 40)
    assert r.screen is not None
    assert r.stream is not None
    assert r.scroll_back == 0
    assert r._in_sync_update is False


def test_command_palette_shows_all_commands_on_tall_terminal():
    # Regression: the palette used to slice input_matches[:8], hiding the last
    # commands (e.g. "update"/"exit") at the bottom of the box even with room.
    r = _make_renderer(24, 80)
    parts: list[str] = []
    matches = list(ProxyInput.COMMANDS)
    r.append_command_palette(
        parts,
        rows=24,
        cols=80,
        input_text="",
        input_matches=matches,
        input_selected=matches[0],
    )
    painted = "".join(parts)
    for command in matches:
        assert command in painted, command


def test_command_palette_scrolls_to_keep_selection_visible():
    # On a short terminal not every command fits; the window must scroll so the
    # selected command is always painted (otherwise it is invisible AND
    # unhighlighted).
    r = _make_renderer(12, 80)
    matches = list(ProxyInput.COMMANDS)
    last = matches[-1]
    parts: list[str] = []
    r.append_command_palette(
        parts,
        rows=12,
        cols=80,
        input_text="",
        input_matches=matches,
        input_selected=last,
    )
    painted = "".join(parts)
    assert last in painted
    assert "\x1b[7m" in painted  # the selected row is reverse-video highlighted


def test_screen_renderer_cell_sgr_bold_red():
    r = ScreenRenderer(24, 80, color_mode="truecolor")
    import pyte.screens

    cell = pyte.screens.Char(
        "X",
        fg="red",
        bg="default",
        bold=True,
        italics=False,
        underscore=False,
        strikethrough=False,
        reverse=False,
        blink=False,
    )
    result = r.cell_sgr(cell)
    assert "1" in result.split(";")  # bold
    assert "31" in result.split(";")  # fg red


def test_screen_renderer_color_code_named():
    r = ScreenRenderer(24, 80, color_mode="truecolor")
    assert r.color_code("red", foreground=True) == "31"
    assert r.color_code("blue", foreground=False) == "44"
    assert r.color_code("default", foreground=True) is None


def test_screen_renderer_hex_color_code_truecolor():
    r = ScreenRenderer(24, 80, color_mode="truecolor")
    result = r.hex_color_code("ff0000", foreground=True)
    assert result == "38;2;255;0;0"


def test_screen_renderer_hex_color_code_256():
    r = ScreenRenderer(24, 80, color_mode="256")
    result = r.hex_color_code("ff0000", foreground=True)
    assert result.startswith("38;5;")


def test_screen_renderer_visible_lines_live():
    r = _make_renderer(5, 20)
    r.stream.feed(b"Hello\r\n")
    lines = r.visible_lines(5)
    assert len(lines) == 4  # rows-1 = 4


def test_screen_renderer_visible_lines_scroll_back():
    r = _make_renderer(5, 10)
    # Write enough to fill history
    for i in range(20):
        r.stream.feed(f"line{i:02d}\r\n".encode())
    # History should have some lines
    assert r.history_len() > 0
    r.scroll_back = 2
    lines = r.visible_lines(5)
    # Must still return rows-1 lines
    assert len(lines) == 4


def test_screen_renderer_selection_ranges_empty():
    r = _make_renderer()
    assert r.selection_ranges(80) == {}


def test_screen_renderer_selection_ranges_span():
    r = _make_renderer()
    r.sel_active = True
    r.sel_anchor = (0, 2)
    r.sel_point = (1, 5)
    ranges = r.selection_ranges(80)
    assert 0 in ranges
    assert 1 in ranges
    assert ranges[0] == (2, 79)  # start=2, end=cols-1 on first row
    assert ranges[1] == (0, 5)  # start=0, end=5 on last row


def test_screen_renderer_render_line_empty_cells():
    r = ScreenRenderer(24, 10, color_mode="truecolor")
    line = r.render_line({}, cols=10)
    # Empty cells render as 10 spaces with no styling left active.
    import re

    plain = re.sub(r"\x1b\[[^m]*m", "", line)
    assert plain == " " * 10


def test_screen_renderer_track_sync_update_sets_flag():
    r = _make_renderer()
    assert r._in_sync_update is False
    r.track_sync_update(b"\x1b[?2026h")
    assert r._in_sync_update is True
    r.track_sync_update(b"\x1b[?2026l")
    assert r._in_sync_update is False


def test_screen_renderer_sync_hold_bounded():
    import time

    r = _make_renderer()
    r._in_sync_update = True
    r._sync_since = time.monotonic()
    assert r.sync_hold(time.monotonic(), 0.05) is True
    # With a very old sync_since it should release
    r._sync_since = time.monotonic() - 1.0
    assert r.sync_hold(time.monotonic(), 0.05) is False


def test_screen_renderer_cursor_sequence_hidden_when_scrolled():
    r = _make_renderer(10, 40)
    r.scroll_back = 3
    result = r.cursor_sequence(10, 40, 3)
    assert result == "\x1b[?25l"


def test_screen_renderer_cursor_sequence_visible_when_live():
    r = _make_renderer(10, 40)
    result = r.cursor_sequence(10, 40, 0)
    assert "\x1b[?25h" in result


def test_screen_renderer_history_len():
    r = _make_renderer(5, 10)
    assert r.history_len() == 0
    for i in range(20):
        r.stream.feed(f"l{i}\r\n".encode())
    assert r.history_len() > 0


def test_screen_renderer_scroll_changes_scroll_back():
    r = _make_renderer(5, 10)
    for i in range(20):
        r.stream.feed(f"l{i}\r\n".encode())
    rendered = []
    r.scroll(3, lambda: rendered.append(1))
    assert r.scroll_back == 3
    assert rendered == [1]


def test_screen_renderer_scroll_clamps_at_zero():
    r = _make_renderer(5, 10)
    r.scroll_back = 2
    r.stream.feed(b"hello\r\n" * 20)
    rendered = []
    r.scroll(-100, lambda: rendered.append(1))
    assert r.scroll_back == 0


def test_screen_renderer_status_line_basic():
    r = ScreenRenderer(5, 40, color_mode="truecolor")
    line = r.status_line(
        cols=40,
        name="main",
        backend_name="claude",
        session_id=None,
        base_branch=None,
        worktree=None,
        scroll_back=0,
        user_declined=[],
        short_session_fn=lambda s: "(none)",
    )
    assert "aGiTrack" in line
    assert "claude" in line


@_posix_only
def test_screen_renderer_status_line_shows_home_abbreviated_cwd(monkeypatch):
    monkeypatch.setenv("HOME", "/Users/dev")
    r = ScreenRenderer(5, 100, color_mode="truecolor")
    line = r.status_line(
        cols=100,
        name="session-1",
        backend_name="claude",
        session_id=None,
        base_branch=None,
        worktree=None,
        scroll_back=0,
        user_declined=[],
        short_session_fn=lambda s: "(none)",
        cwd="/Users/dev/code/repo/.agitrack/worktrees/session-1",
    )
    # The agent's working directory is visible, home-abbreviated.
    assert "~/code/repo/.agitrack/worktrees/session-1" in line


def test_screen_renderer_status_line_elides_long_cwd_from_left():
    cols = 60
    r = ScreenRenderer(5, cols, color_mode="truecolor")
    line = r.status_line(
        cols=cols,
        name="session-1",
        backend_name="claude",
        session_id=None,
        base_branch=None,
        worktree=None,
        scroll_back=0,
        user_declined=[],
        short_session_fn=lambda s: "(none)",
        cwd="/very/long/path/that/cannot/possibly/fit/in/the/status/bar/worktrees/session-1",
    )
    visible = line.replace("\x1b[7m", "").replace("\x1b[0m", "")
    assert len(visible) <= cols  # never overflows the row
    # Elided from the left: the identifying tail of the path survives.
    assert "…" in visible
    assert visible.rstrip().endswith("session-1")


def test_status_line_shows_base_repo_directory_not_the_worktree(tmp_path):
    # The path identifies the PROJECT (base repo), not the internal
    # .agitrack/worktrees/<name> sandbox — the session name next to it already
    # says which worktree is active.
    import subprocess

    from agitrack.git import GitRepo
    from agitrack.config import AgitrackState

    base = tmp_path / "project"
    worktree = base / ".agitrack" / "worktrees" / "session-1"
    worktree.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=base, check=True)
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    runner = make_runner(
        repo=GitRepo(worktree),
        base_repo=GitRepo(base),
        state=AgitrackState(worktree),
        name="session-1",
        backend=type("B", (), {"name": "claude"})(),
        scroll_back=0,
        cols=200,
    )

    line = runner._status_line()
    # Use name-based check to work on both POSIX (/) and Windows (\) separators.
    assert "project" in line
    assert "worktrees" not in line


def test_status_line_falls_back_to_repo_directory_without_base(tmp_path):
    # --no-worktree mode (and bare test runners) have no separate base repo:
    # the repo the agent works in is the project.
    import subprocess

    from agitrack.git import GitRepo
    from agitrack.config import AgitrackState

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    runner = make_runner(
        repo=GitRepo(tmp_path),
        state=AgitrackState(tmp_path),
        name="session-1",
        backend=type("B", (), {"name": "claude"})(),
        scroll_back=0,
        cols=200,
    )

    line = runner._status_line()
    assert tmp_path.name in line


def test_screen_renderer_status_line_scrollback():
    r = ScreenRenderer(5, 60, color_mode="truecolor")
    line = r.status_line(
        cols=60,
        name="s",
        backend_name="claude",
        session_id=None,
        base_branch=None,
        worktree=None,
        scroll_back=5,
        user_declined=[],
        short_session_fn=lambda s: "(none)",
    )
    assert "SCROLLBACK" in line


def test_screen_renderer_append_box():
    r = ScreenRenderer(20, 60, color_mode="truecolor")
    parts = []
    r.append_box(parts, 2, 2, 20, ["Line one", "Line two"], rows=20)
    combined = "".join(parts)
    assert "Line one" in combined
    assert "┃" in combined  # heavy border char (a prominent, thicker frame)
    assert "┏" in combined and "┛" in combined  # heavy corners
    assert "\x1b[1;38;2;" in combined  # bold, truecolor-encoded accent on the border


def test_screen_renderer_feed_strips_hostile_csi():
    import re

    r = _make_renderer(5, 20)
    hostile = re.compile(rb"\x1b\[[<>=][0-9;:]*[ -/]*[@-~]")
    # Should not raise even with hostile CSI
    r.feed(b"\x1b[>4mHello\x1b[>4m", pyte_hostile_csi_re=hostile)
    lines = r.visible_lines(5)
    # 'Hello' was written; check it survived
    row0 = lines[0]
    chars = [row0.get(c) for c in range(5)]
    text = "".join((c.data if c else " ") for c in chars)
    assert text == "Hello"


def test_duck_type_aliases_cover_extracted_classes():
    # ScreenRenderer and TerminalHost methods run with `self` being a
    # ProxyRunner via unbound delegation. A `self.foo()` inside those classes
    # therefore resolves on ProxyRunner, not on the class — so every method
    # name the classes SELF-CALL must exist on ProxyRunner too. A missing
    # alias crashes at runtime in paths the suite does not exercise (e.g.
    # run() startup), so pin the contract here.
    import inspect
    import re as _re

    from agitrack.proxy.renderer import ScreenRenderer
    from agitrack.proxy.terminal import TerminalHost

    for cls in (ScreenRenderer, TerminalHost):
        own_methods = {n for n, _ in inspect.getmembers(cls, inspect.isfunction)}
        self_calls = set(_re.findall(r"self\.([a-zA-Z_][a-zA-Z0-9_]*)\(", inspect.getsource(cls)))
        for name in sorted(self_calls & own_methods):
            assert hasattr(ProxyRunner, name), (
                f"ProxyRunner is missing alias {name!r}, self-called inside {cls.__name__}"
            )


def test_gh_unavailable_hint_text_by_status():
    from agitrack.proxy.runner import ProxyRunner

    missing = ProxyRunner._gh_unavailable_hint("missing")
    assert missing is not None and "isn't installed" in missing and "cli.github.com" in missing
    unauth = ProxyRunner._gh_unavailable_hint("unauthenticated")
    assert unauth is not None and "isn't logged in" in unauth and "gh auth login" in unauth
    assert ProxyRunner._gh_unavailable_hint("ok") is None


def test_notify_if_gh_unavailable_sets_message_when_missing(monkeypatch):
    runner = make_runner(name="main")
    runner.messages = []
    runner._set_message = lambda msg, **k: runner.messages.append(msg)
    runner._render = lambda: None
    monkeypatch.setattr("agitrack.metrics.github.gh_status", lambda: "missing")

    runner._notify_if_gh_unavailable()

    assert runner.messages and "gh" in runner.messages[0]


def test_notify_if_gh_unavailable_silent_when_ok(monkeypatch):
    runner = make_runner(name="main")
    runner.messages = []
    runner._set_message = lambda msg, **k: runner.messages.append(msg)
    runner._render = lambda: None
    monkeypatch.setattr("agitrack.metrics.github.gh_status", lambda: "ok")

    runner._notify_if_gh_unavailable()

    assert runner.messages == []


@_posix_only
def test_restore_terminal_clears_before_leaving_alt_screen(monkeypatch):
    # #70: on terminals without alt-screen support, leaving the alt screen is a
    # no-op, so aGiTrack's UI lingers after exit unless we clear the screen first.
    # restore_terminal must emit a clear+home BEFORE the `?1049l` leave so the
    # screen is clean on those terminals (and unchanged where altscreen works).
    import types

    from agitrack.proxy import terminal as terminal_mod
    from agitrack.proxy.terminal import TerminalHost

    writes: list[bytes] = []
    monkeypatch.setattr(terminal_mod.os, "write", lambda _fd, data: writes.append(data) or len(data))

    state = types.SimpleNamespace(old_attrs=None)
    # Stub the cooked/mode + mouse teardown so only the screen bytes matter here.
    state.disable_host_terminal_modes = lambda: None
    state.set_cooked = lambda: None
    monkeypatch.setattr(terminal_mod.termios, "tcflush", lambda *a, **k: None)

    TerminalHost.restore_terminal(state)

    out = b"".join(writes)
    assert b"\x1b[2J" in out  # clears the screen
    assert b"\x1b[?1049l" in out  # leaves the alt screen
    assert out.index(b"\x1b[2J") < out.index(b"\x1b[?1049l")  # clear comes first


def test_rename_session_menu_prompts_then_renames():
    runner = make_runner(name="main")
    runner.sessions = [object()]
    runner._session_name = lambda i: "old"
    runner._select_popup = lambda title, options: options[0]
    runner._prompt_popup = lambda title, prompt, *, default="": "new-name"
    runner._set_message = lambda *a, **k: None
    runner._render = lambda: None
    captured: list = []
    runner._rename_session = lambda index, name: captured.append((index, name))

    runner._rename_session_menu()

    assert captured == [(0, "new-name")]


def test_rename_forks_shared_lineage(tmp_path):
    # Rename-as-fork: renaming a session that was resumed/shared drops its tracked
    # lineage origin, so a later share publishes a new `<you>/<name>` entry instead of
    # updating the original. A session with no origin is unaffected.
    import types

    from agitrack.config import AgitrackState

    runner = make_runner(name="main")
    runner.base_repo = types.SimpleNamespace(repo=tmp_path)
    runner.global_config = types.SimpleNamespace(default_backend="claude")
    state = AgitrackState(tmp_path)
    state.set_shared_origin("sid-1", owner="alice", name="fix-parser", contributors=["alice"])

    runner._fork_lineage_on_rename("sid-1")
    assert AgitrackState(tmp_path).shared_origin("sid-1") is None  # origin dropped → next share forks

    # A purely local session (no origin) is a no-op (and never errors).
    runner._fork_lineage_on_rename("sid-local")
    assert AgitrackState(tmp_path).shared_origin("sid-local") is None


def _make_rename_runner(tmp_path, *, origin):
    import subprocess
    import types

    from agitrack.config import AgitrackState

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "x"], check=True)

    runner = make_runner(name="oldname")
    runner.global_config = types.SimpleNamespace(default_backend="claude")
    runner.sessions = [runner.active]
    runner._use_worktrees = True
    runner._session_name = lambda i: "oldname"
    runner._session_name_taken = lambda n: False
    runner._switch_active = lambda i: None
    runner.worktree = types.SimpleNamespace(name="oldname", path=tmp_path)
    st = AgitrackState(tmp_path)
    st.backend_session_id = "sid-1"
    runner.state = st
    new_info = types.SimpleNamespace(name="newname", path=tmp_path)
    runner._worktrees = lambda: types.SimpleNamespace(move=lambda old, new: new_info)
    for method in (
        "_stop_file_watcher",
        "_teardown_child",
        "_init_screen",
        "_spawn",
        "_resize_child",
        "_enable_host_mouse",
        "_start_file_watcher",
        "_reset_agent_tracking",
        "_sanitize_state_trace",
        "_initialize_session_baseline",
        "_render",
        "_stage_backend_resume",
        "_persist_session_name",
        "_fork_lineage_on_rename",
    ):
        setattr(runner, method, lambda *a, **k: None)
    runner._turn_from_branch = lambda b: 0
    runner._user_state = lambda: types.SimpleNamespace(shared_origin=lambda sid: origin if sid == "sid-1" else None)
    msgs: list[str] = []
    runner._set_message = lambda m, **k: msgs.append(m)
    return runner, msgs


def test_rename_shared_session_warns_it_becomes_a_separate_copy(tmp_path):
    # Renaming a session that tracked a shared lineage warns that a later share now
    # creates a NEW shared session rather than updating the original.
    runner, msgs = _make_rename_runner(tmp_path, origin={"owner": "alice", "name": "x", "contributors": ["alice"]})
    runner._rename_session(0, "newname")
    assert any("separate copy" in m and "NEW shared session" in m for m in msgs)


def test_rename_unshared_session_uses_plain_message(tmp_path):
    # A session with no shared lineage gets the plain rename confirmation.
    runner, msgs = _make_rename_runner(tmp_path, origin=None)
    runner._rename_session(0, "newname")
    assert msgs and msgs[-1] == "Renamed session to 'newname'."


def test_rename_session_rejects_taken_name_without_moving():
    import types

    runner = make_runner(name="main")
    runner.sessions = [object()]
    runner._session_name = lambda i: "old"
    runner._session_name_taken = lambda n: True  # the target name is already in use
    runner._switch_active = lambda i: None
    moved: list = []
    runner.worktree_manager = types.SimpleNamespace(move=lambda a, b: moved.append((a, b)))
    msgs: list = []
    runner._set_message = lambda msg, **k: msgs.append(msg)
    runner._render = lambda: None

    runner._rename_session(0, "taken")

    assert moved == []  # nothing moved
    assert any("already in use" in m for m in msgs)


def test_configured_menu_key_opens_command_capture():
    # menu_key in ~/.agitrack/config.json rebinds the aGiTrack menu (default Ctrl-G).
    parser = ProxyInput(menu_key=b"\x10")  # ctrl-p

    forwarded, _echo, command, _exit = parser.feed(b"\x10git-unstaged\r")
    assert forwarded == []
    assert command == "git-unstaged"

    # The default key is now ordinary input and goes to the backend.
    forwarded, _echo, command, _exit = parser.feed(b"\x07")
    assert forwarded == [b"\x07"]
    assert command is None


def test_real_init_defines_all_lifecycle_flags(tmp_path):
    # P7 removed the getattr() guards on these flags; for_testing() seeds them
    # too, which would mask a missing __init__ initialization from the whole
    # suite. Build a runner through the REAL __init__ (with injected stubs) and
    # require every flag to exist, so a future sweep cannot ship a runner that
    # crashes on its first reactor tick while the tests stay green.
    import subprocess as sp

    sp.run(["git", "init", "-q", str(tmp_path)], check=True)
    sp.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    from agitrack.git import GitRepo

    runner = ProxyRunner(GitRepo(tmp_path), backend="claude")
    for flag in (
        "_monitor_base_edits",
        "_base_check_at",
        "_cwd_drift_checked",
        "_cwd_check_at",
        "_relaunch_times",
        "_exiting",
        "_finalized_on_exit",
    ):
        assert flag in runner.__dict__ or hasattr(type(runner), flag), flag
        getattr(runner, flag)  # must not raise


def test_no_worktree_mode_skips_worktree_setup(tmp_path):
    # With worktrees off, setup leaves worktree=None and creates no worktree dir.
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    from agitrack.git import GitRepo
    from agitrack.proxy.runner import ProxyRunner

    runner = ProxyRunner(GitRepo(tmp_path), use_worktrees=False, backend="claude")
    runner._base_branch = "main"
    runner._setup_base_merge_only_session()
    assert runner.worktree is None
    assert not (tmp_path / ".agitrack" / "worktrees").exists()


# --- backend self-update (automatic, sandbox-blocked → run from the unconfined proxy) ---


def _update_runner(tmp_path):
    runner = make_runner(state=AgitrackState(tmp_path))
    runner._set_message = lambda m, **k: runner._msgs.append(m)  # type: ignore[attr-defined]
    runner._render = lambda *a, **k: None
    runner._msgs = []  # type: ignore[attr-defined]
    return runner


def test_backend_auto_update_runs_unconfined_and_reports_version_change(tmp_path, monkeypatch):
    import agitrack.proxy.runner as runner_mod

    runner = _update_runner(tmp_path)
    runner.backend = types.SimpleNamespace(name="opencode", update_command=lambda: ["opencode", "upgrade"])
    runner._backend_update_via_agitrack = lambda: True  # aGiTrack drives the update (brew-managed on macOS)
    runner._backend_version = lambda: next(versions)
    versions = iter(["1.15.13", "1.17.9"])  # before, after — the upgrade landed
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="Upgraded\n", stderr="")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)

    runner._maybe_auto_update_backend()  # automatic — no menu, no prompt, no brew pre-check
    runner._backend_update_thread.join(2)
    # The updater is the RAW backend command — NOT wrapped in sandbox-exec/bwrap. It runs in
    # the unconfined proxy, so a brew updater's own sandbox isn't nested (the whole bug).
    assert captured["cmd"] == ["opencode", "upgrade"]
    assert runner._backend_update_checked_for == "opencode"  # evaluated once; won't re-trigger

    runner._service_backend_update()
    assert any("updated" in m.lower() and "1.17.9" in m for m in runner._msgs)


def test_backend_auto_update_reports_up_to_date_when_version_unchanged(tmp_path, monkeypatch):
    # The updater no-ops when current (version unchanged before/after) — don't claim an update.
    import agitrack.proxy.runner as runner_mod

    runner = _update_runner(tmp_path)
    runner.backend = types.SimpleNamespace(name="opencode", update_command=lambda: ["opencode", "upgrade"])
    runner._backend_update_via_agitrack = lambda: True
    runner._backend_version = lambda: "1.17.9"  # unchanged before and after
    monkeypatch.setattr(
        runner_mod.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="already on latest\n", stderr=""),
    )

    runner._maybe_auto_update_backend()
    runner._backend_update_thread.join(2)
    runner._service_backend_update()

    assert any("up to date" in m for m in runner._msgs)
    assert not any("updated (" in m for m in runner._msgs)


def test_backend_auto_update_skips_when_self_update_not_blocked(tmp_path, monkeypatch):
    # An npm/native backend updates itself fine; aGiTrack must not touch it (no upgrade run).
    import agitrack.proxy.runner as runner_mod

    runner = _update_runner(tmp_path)
    runner.backend = types.SimpleNamespace(name="opencode", update_command=lambda: ["opencode", "upgrade"])
    runner._backend_update_via_agitrack = lambda: False
    ran = []
    monkeypatch.setattr(runner_mod.subprocess, "run", lambda *a, **k: ran.append(True))

    runner._maybe_auto_update_backend()

    assert runner._backend_update_thread is None
    assert ran == []  # never ran the updater (or a version check)


def test_backend_child_env_disables_opencode_autoupdate_when_agitrack_takes_over(tmp_path):
    # When aGiTrack will apply the update itself (OpenCode's own updater is sandbox-blocked and
    # update checks are on), the interactive child gets OPENCODE_DISABLE_AUTOUPDATE so OpenCode
    # doesn't show its own prompt — which would only fail inside the sandbox.
    runner = _update_runner(tmp_path)
    runner.backend = types.SimpleNamespace(name="opencode")
    runner.global_config = types.SimpleNamespace(check_for_updates=True)
    runner._backend_update_via_agitrack = lambda: True
    # Assert the autoupdate-suppression behavior independent of the color env vars the Windows
    # path also injects (COLORTERM/FORCE_COLOR), which are orthogonal to this.
    assert (runner._backend_child_env() or {}).get("OPENCODE_DISABLE_AUTOUPDATE") == "1"

    # npm/native (not sandbox-blocked) → leave OpenCode's own auto-update alone.
    runner._backend_update_via_agitrack = lambda: False
    assert "OPENCODE_DISABLE_AUTOUPDATE" not in (runner._backend_child_env() or {})

    # Update checks off → aGiTrack won't auto-update, so don't suppress OpenCode's either.
    runner._backend_update_via_agitrack = lambda: True
    runner.global_config = types.SimpleNamespace(check_for_updates=False)
    assert "OPENCODE_DISABLE_AUTOUPDATE" not in (runner._backend_child_env() or {})


def test_backend_child_env_forces_color_on_windows_only(tmp_path, monkeypatch):
    # On Windows aGiTrack skips host-terminal color detection, so a probing backend (Claude)
    # renders greyscale unless told the terminal is color-capable. Force truecolor there; POSIX
    # keeps real detection (no forced color env).
    import agitrack.proxy.runner as proxy_mod

    runner = _update_runner(tmp_path)
    runner.backend = types.SimpleNamespace(name="claude")
    runner.global_config = types.SimpleNamespace(check_for_updates=True)
    runner._backend_update_via_agitrack = lambda: False

    monkeypatch.setattr(proxy_mod.os, "name", "nt")
    env = runner._backend_child_env() or {}
    assert env.get("COLORTERM") == "truecolor"
    assert env.get("FORCE_COLOR") == "3"

    monkeypatch.setattr(proxy_mod.os, "name", "posix")
    assert runner._backend_child_env() is None


def test_backend_auto_update_respects_update_check_toggle(tmp_path):
    # Honors the same opt-out as aGiTrack's own self-update: if update checks are off, don't
    # even probe whether the self-update is blocked.
    runner = _update_runner(tmp_path)
    runner.backend = types.SimpleNamespace(name="opencode", update_command=lambda: ["opencode", "upgrade"])
    runner.global_config = types.SimpleNamespace(check_for_updates=False)
    runner._backend_update_via_agitrack = lambda: pytest.fail("should not probe when checks are off")

    runner._maybe_auto_update_backend()

    assert runner._backend_update_thread is None


def test_service_backend_update_flags_failure_even_on_exit_zero(tmp_path):
    # `opencode upgrade` exits 0 even when its brew step fails, so failure is read from output.
    runner = _update_runner(tmp_path)
    runner._backend_update_result = {
        "name": "opencode",
        "code": 0,
        "output": "Upgrading...\n\x1b[2K■  Upgrade failed for brew (exit code 1).\n",
    }

    runner._service_backend_update()

    assert any("may have failed" in m for m in runner._msgs)
    assert any("Upgrade failed" in m for m in runner._msgs)


def test_backend_auto_update_noop_when_backend_has_no_updater(tmp_path):
    # A backend without an update_command (or where it's blocked but returns nothing) simply
    # isn't auto-updated — no thread, no error.
    runner = _update_runner(tmp_path)
    runner.backend = types.SimpleNamespace(name="foo")  # no update_command attribute
    runner._backend_update_via_agitrack = lambda: True

    runner._maybe_auto_update_backend()

    assert runner._backend_update_thread is None


# --- settings persistence + restart prompt -----------------------------------


def test_runner_loads_repo_overlay_so_repo_scoped_settings_persist(tmp_path):
    # Regression: the proxy built a fresh GlobalConfig without loading the repo overlay, so
    # repo_path was None and save_repo() silently dropped every "this repository only" change.
    from agitrack.config import GlobalConfig
    from agitrack.git import GitRepo
    from agitrack.proxy.runner import ProxyRunner

    repo = GitRepo.init(tmp_path)
    runner = ProxyRunner(repo, use_worktrees=False, backend="claude")
    assert runner.global_config.repo_path is not None  # overlay loaded → save_repo() works

    runner.global_config.set("use_worktrees", False, scope="repo")

    fresh = GlobalConfig()  # re-read from disk
    fresh.load_repo_overlay(tmp_path)
    assert fresh.use_worktrees is False  # the repo-scoped change actually hit the file


def _save_pending_runner(tmp_path):
    runner = make_runner(state=AgitrackState(tmp_path))
    runner._set_message = lambda *a, **k: None
    runner._render = lambda *a, **k: None
    sets: list = []
    runner.global_config = types.SimpleNamespace(
        set=lambda key, value, *, scope: sets.append((key, value, scope)),
        repo_data={},
        data={},
    )
    runner._sets = sets  # type: ignore[attr-defined]
    return runner


def test_settings_save_offers_restart_for_restart_only_change(tmp_path):
    runner = _save_pending_runner(tmp_path)
    runner._settings_pending = {"use_worktrees": (False, "repo", True)}  # restart-only
    runner._settings_pending_timings = {}
    restarted: list = []
    runner._restart_now = lambda msg: restarted.append(msg)
    answers = iter(["Yes, save them", "Yes, restart now"])
    runner._select_popup = lambda *a, **k: next(answers)

    assert runner._confirm_save_pending() == "saved"
    assert ("use_worktrees", False, "repo") in runner._sets  # persisted at repo scope
    assert restarted  # restart offered and accepted


def test_settings_save_restart_only_not_now_keeps_running(tmp_path):
    runner = _save_pending_runner(tmp_path)
    runner._settings_pending = {"use_worktrees": (False, "repo", True)}
    runner._settings_pending_timings = {}
    restarted: list = []
    runner._restart_now = lambda msg: restarted.append(msg)
    answers = iter(["Yes, save them", "Not now"])
    runner._select_popup = lambda *a, **k: next(answers)

    assert runner._confirm_save_pending() == "saved"
    assert restarted == []  # declined → keep running, changes apply next launch


def test_settings_save_no_restart_prompt_for_live_change(tmp_path):
    # A non-restart setting (e.g. summarization on/off) saves without offering a restart:
    # _select_popup is consulted only once (the save confirmation), never a second time.
    runner = _save_pending_runner(tmp_path)
    runner._settings_pending = {"summarization_enabled": (True, "global", False)}
    runner._settings_pending_timings = {}
    runner._restart_now = lambda msg: pytest.fail("must not offer a restart for a live setting")
    answers = iter(["Yes, save them"])  # a second _select_popup call would StopIteration → fail
    runner._select_popup = lambda *a, **k: next(answers)

    assert runner._confirm_save_pending() == "saved"


def test_switch_backend_records_choice_repo_scoped_not_global(tmp_path, monkeypatch):
    # Switching backends in one repo (e.g. to try OpenCode) must persist at REPO scope, not
    # overwrite the user's GLOBAL default for every other repo.
    import agitrack.proxy.runner as rm

    runner = make_runner(state=AgitrackState(tmp_path), worktree=None)
    runner.backend = types.SimpleNamespace(name="claude")
    sets: list = []
    runner.global_config = types.SimpleNamespace(set=lambda key, value, *, scope: sets.append((key, value, scope)))
    monkeypatch.setattr(rm, "backend_installed", lambda name: True)
    monkeypatch.setattr(rm, "make_proxy_agent", lambda name: types.SimpleNamespace(name=name))
    runner._restart_agent = lambda msg: None
    runner._set_message = lambda *a, **k: None
    runner._render = lambda *a, **k: None

    runner._switch_backend("opencode")

    assert ("default_backend", "opencode", "repo") in sets
    assert not any(scope == "global" for _, _, scope in sets)  # global default left alone


def test_no_worktree_mode_removes_leftover_worktrees(tmp_path):
    # Turning worktrees off must clean up worktrees left by a previous worktree-mode run
    # (their committed work is already integrated into the base branch).
    runner = make_runner(state=AgitrackState(tmp_path))
    runner._use_worktrees = False
    runner._set_message = lambda *a, **k: None
    runner._render = lambda *a, **k: None
    removed: list = []
    runner._worktrees = lambda: types.SimpleNamespace(
        list=lambda: [types.SimpleNamespace(name="sess-a"), types.SimpleNamespace(name="sess-b")],
        remove=lambda name, **k: removed.append(name),
    )

    runner._setup_base_merge_only_session()

    assert removed == ["sess-a", "sess-b"]
    assert runner.worktree is None  # runs on the base tree directly


def test_worktree_sessions_is_memoized_to_avoid_repeated_slow_listing(tmp_path):
    # Entering the resume menu calls _worktree_sessions several times (via _resumable_sessions
    # and _agitrack_named_sessions); each is a slow `opencode session list`. It must be
    # memoized so the menu fires it ONCE, not back-to-back (which froze the TUI for seconds).
    runner = make_runner(state=AgitrackState(tmp_path))
    calls = []
    runner.backend = types.SimpleNamespace(list_worktree_sessions=lambda root: calls.append(1) or [])
    runner._worktrees = lambda: types.SimpleNamespace(root=tmp_path)

    runner._worktree_sessions()
    runner._worktree_sessions()
    runner._worktree_sessions()

    assert len(calls) == 1  # cached within the TTL, not three subprocesses


def _collision_resume_runner(tmp_path):
    runner = make_runner(state=AgitrackState(tmp_path))
    runner.sessions = []  # no live session matches the id -> skip the "already live, just switch" shortcut
    runner._set_message = lambda *a, **k: None
    runner._render = lambda *a, **k: None
    runner._next_session_name = lambda: "suggested-word"
    runner._created = []  # type: ignore[attr-defined]
    runner._new_session = lambda name, **k: runner._created.append(name)  # type: ignore[attr-defined]
    return runner


def test_resume_name_collision_prompts_with_random_suggestion(tmp_path):
    # Resuming a conversation whose name collides with a LIVE session must PROMPT the user
    # (explaining why, suggesting a random word) — never silently rename.
    runner = _collision_resume_runner(tmp_path)
    runner._live_session_name_taken = lambda name: True
    asked = []
    runner._prompt_session_name = lambda title, *, default: asked.append((title, default)) or "bar"

    runner._resume_conversation("foo", "ses_1")

    assert asked, "the user was not prompted"
    title, default = asked[0]
    assert default == "suggested-word"  # a random word is offered as the editable default
    assert "already open" in title and "name" in title.lower()  # explains why a new name is needed
    assert runner._created == ["bar"]  # resumed under the user's chosen name, not an auto one


def test_resume_name_collision_cancel_aborts_resume(tmp_path):
    runner = _collision_resume_runner(tmp_path)
    runner._live_session_name_taken = lambda name: True
    runner._prompt_session_name = lambda *a, **k: None  # Esc / cancel

    runner._resume_conversation("foo", "ses_1")

    assert runner._created == []  # nothing resumed when the user cancels the name prompt


def test_resume_without_collision_keeps_name_and_does_not_prompt(tmp_path):
    runner = _collision_resume_runner(tmp_path)
    runner._live_session_name_taken = lambda name: False
    runner._prompt_session_name = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not prompt"))

    runner._resume_conversation("foo", "ses_1")

    assert runner._created == ["foo"]  # original name kept, no prompt
