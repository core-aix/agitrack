from agit.backends.base import TokenUsage
from agit.opencode_session import SessionTurn
from agit.proxy import ProxyInput, ProxyRunner, _escape_sequence_complete
from agit.state import AgitState


class FakeCommitRepo:
    def __init__(self):
        self.message = ""

    def add_tracked(self):
        pass

    def has_staged_changes(self):
        return True

    def commit(self, message: str):
        self.message = message


def test_proxy_ctrl_g_enters_command_mode():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07status\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "status"
    assert should_exit is False


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


def test_proxy_ctrl_c_exits_in_command_capture():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07sta\x03")

    assert forwarded == []
    assert local_echo == b""
    assert command is None
    assert should_exit is True


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

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07sta\t\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "status"
    assert should_exit is False


def test_proxy_arrow_selection_runs_selected_command():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07\x1b[B\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "stage"
    assert should_exit is False


def test_proxy_tab_completes_selected_command():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07\x1b[B\t\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "stage"


def test_proxy_enter_runs_selected_partial_match_without_tab():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07sta\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "status"
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

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07\x1b[<35;88;11Mstatus\r")

    assert forwarded == []
    assert local_echo == b""
    assert command == "status"
    assert should_exit is False


def test_popup_escape_sequence_consumer_waits_for_mouse_terminator():
    assert _escape_sequence_complete(b"\x1b[<35;88;11") is False
    assert _escape_sequence_complete(b"\x1b[<35;88;11M") is True
    assert _escape_sequence_complete(b"\x1b[<35;88;11m") is True
    assert _escape_sequence_complete(b"\x1b[35;88;11") is False
    assert _escape_sequence_complete(b"\x1b[35;88;11M") is True


def test_proxy_ctrl_c_exits_in_passthrough_mode():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x03")

    assert forwarded == []
    assert local_echo == b""
    assert command is None
    assert should_exit is True


def test_proxy_agent_commit_preserves_incomplete_initial_user_turn(tmp_path):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.repo = FakeCommitRepo()
    runner.state = AgitState(tmp_path)
    runner.verbose = False
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
    assert message.startswith("<agent> fix it / also handle errors")
    assert message.index("User:\nfix it") < message.index("User:\nalso handle errors")
    assert message.index("User:\nalso handle errors") < message.index("Agent:\ndone")


def test_proxy_agent_commit_preserves_previous_no_change_trace(tmp_path):
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.repo = FakeCommitRepo()
    runner.state = AgitState(tmp_path)
    runner.verbose = False
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
    assert message.index("User:\nexplain only") < message.index("Agent:\nno code changed")
    assert message.index("Agent:\nno code changed") < message.index("User:\nnow edit")
    assert message.count("User:\nexplain only") == 1


def test_proxy_plain_row_handles_empty_pyte_cell_data():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.cols = 2

    class Cell:
        data = ""

    class Screen:
        buffer = {0: {0: Cell()}}

    runner.screen = Screen()

    assert runner._plain_row(0) == "  "


def test_proxy_agent_active_does_not_depend_on_recent_output():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.agent_in_flight = False
    runner.agent_parse_thread = None
    runner.last_child_output = 999999999.0

    assert runner._agent_is_active() is False

    runner.agent_in_flight = True
    assert runner._agent_is_active() is True


def test_proxy_clears_stale_agent_in_flight_when_idle():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner.agent_in_flight = True
    runner.last_child_output = 0.0
    runner.CHILD_IDLE_SECONDS = 4.0

    runner._clear_agent_in_flight_if_idle()

    assert runner.agent_in_flight is False
