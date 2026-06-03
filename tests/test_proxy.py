from agit.proxy import ProxyInput


def test_proxy_ctrl_g_enters_command_mode():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x07status\r")

    assert forwarded == []
    assert local_echo == b"\r\n[aGiT] status\r\n"
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
    assert local_echo == b"\r\n[aGiT] sta"
    assert command is None
    assert should_exit is True


def test_proxy_ctrl_c_exits_in_passthrough_mode():
    parser = ProxyInput()

    forwarded, local_echo, command, should_exit = parser.feed(b"\x03")

    assert forwarded == []
    assert local_echo == b""
    assert command is None
    assert should_exit is True
