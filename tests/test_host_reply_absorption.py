"""A host-terminal reply split across pty reads must not become keystrokes.

An OSC reply is terminated by BEL, and BEL is 0x07 — Ctrl-G, the default menu key. The absorber
only ever matched a COMPLETE reply inside one read, but a pty read is not framed: a slow terminal
(tmux relaying outward, a forwarded session) answers in pieces. Both halves then flowed to the key
handler, so the palette opened by itself and the rest of the reply was typed at the agent — and
the Ctrl-G the user pressed next toggled it shut again. That is what "my menu key sometimes types
into the chat" (twice in one live scenario, with `sessions` submitted as an accidental turn)
actually was.
"""

from __future__ import annotations

from agitrack.proxy.runner import ProxyRunner


def _runner():
    runner = ProxyRunner.__new__(ProxyRunner)
    runner._held_reply_prefix = bytearray()
    runner._in_bracketed_paste = False
    runner.debug_proxy = False
    runner._debug = lambda message: None
    runner._parse_host_terminal_responses = lambda data: None
    return runner


def test_a_reply_split_across_reads_is_absorbed_whole():
    runner = _runner()

    first = runner._absorb_host_terminal_reply(b"\x1b]11;rgb:ffff/ffff")
    second = runner._absorb_host_terminal_reply(b"/ffff\x07")

    assert first == b""  # the partial is held, not forwarded
    assert second == b""  # ...and the BEL never reaches the key handler
    assert runner._held_reply_prefix == bytearray()


def test_a_split_reply_does_not_leak_the_bel_that_is_ctrl_g():
    """The precise failure: the tail carrying the BEL used to be forwarded verbatim."""
    runner = _runner()
    runner._absorb_host_terminal_reply(b"\x1b]10;rgb:0000/0000")
    tail = runner._absorb_host_terminal_reply(b"/0000\x07")
    assert b"\x07" not in tail


def test_a_real_ctrl_g_still_reaches_the_key_handler():
    """The whole point of the menu key: holding must never apply to an ordinary keypress."""
    runner = _runner()
    assert runner._absorb_host_terminal_reply(b"\x07") == b"\x07"
    # ...including right after a complete reply was absorbed.
    assert runner._absorb_host_terminal_reply(b"\x1b]11;rgb:1/1/1\x07\x07") == b"\x07"


def test_ordinary_typing_is_untouched():
    runner = _runner()
    assert runner._absorb_host_terminal_reply(b"sessions\r") == b"sessions\r"
    assert runner._absorb_host_terminal_reply(b"\x1b[A") == b"\x1b[A"  # an arrow key


def test_a_truncated_reply_is_eventually_released_rather_than_eating_input():
    """A terminal that starts an answer and never finishes it must not silence the keyboard."""
    runner = _runner()
    runner._absorb_host_terminal_reply(b"\x1b]11;" + b"a" * 400)
    # Past the cap the partial is not held, so it flows on rather than being swallowed forever.
    assert runner._held_reply_prefix == bytearray()


def test_nothing_is_absorbed_inside_a_bracketed_paste():
    """Pasted bytes are content the user copied — a pasted terminal transcript must arrive
    at the agent intact."""
    runner = _runner()
    runner._in_bracketed_paste = True
    payload = b"\x1b]11;rgb:ffff/ffff/ffff\x07"
    assert runner._absorb_host_terminal_reply(payload) == payload
