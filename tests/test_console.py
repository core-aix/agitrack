"""Tests for agitrack/console.py — the pre-TUI prompt helpers.

The point of these helpers is that a question asked after a long install is answered by a
deliberate keypress, and that a silent installer still looks alive. Nothing here touches a
real terminal: stdin/stdout are stubbed, so the drains are no-ops that must stay harmless.
"""

from __future__ import annotations

import agitrack.console as console


def test_drain_terminal_input_is_a_noop_without_a_tty(monkeypatch):
    monkeypatch.setattr(console.sys.stdin, "isatty", lambda: False, raising=False)
    console.drain_terminal_input()  # must not raise (pytest's stdin is not a terminal)


def test_drain_terminal_input_never_raises_on_a_broken_stdin(monkeypatch):
    class Broken:
        def isatty(self):
            raise OSError("no tty here")

    monkeypatch.setattr(console.sys, "stdin", Broken())
    console.drain_terminal_input()


def test_ask_drains_before_reading(monkeypatch):
    # A keypress made while an installer ran must not answer the NEXT question: the input
    # queue is discarded first, then the question is read.
    events: list[str] = []
    monkeypatch.setattr(console, "drain_terminal_input", lambda: events.append("drain"))
    answer = console.ask("Pick one: ", input_fn=lambda prompt: events.append(f"input:{prompt}") or "2")
    assert answer == "2"
    assert events == ["drain", "input:Pick one: "]


def test_progress_ticker_disabled_produces_no_output():
    lines: list[str] = []
    with console.progress_ticker("installing", output_fn=lines.append, interval=0.001, enabled=False):
        pass
    assert lines == []


def test_progress_ticker_reports_while_a_slow_step_runs():
    # The reassurance for an installer that prints nothing for minutes.
    import time

    lines: list[str] = []
    with console.progress_ticker("installing Claude Code", output_fn=lines.append, interval=0.01, enabled=True):
        time.sleep(0.15)
    assert lines, "a long step must report progress"
    assert all("still installing Claude Code" not in line or "elapsed" in line for line in lines)
    assert "installing Claude Code" in lines[0]


def test_progress_ticker_stops_ticking_after_the_block():
    import time

    lines: list[str] = []
    with console.progress_ticker("working", output_fn=lines.append, interval=0.01, enabled=True):
        time.sleep(0.05)
    count = len(lines)
    time.sleep(0.05)
    assert len(lines) == count  # the thread stopped with the block
