"""Tests for the agent-theme adaptation ("canvas") in agitrack/proxy/renderer.py.

aGiTrack paints every cell itself, so a cell the backend left at the terminal default used
to come out in the HOST terminal's background. With a dark agent theme in a light terminal
that produced a screen that was half dark panels and half white — the bug these cover. The
canvas fills those cells to match the scheme the AGENT is painting in, and follows the agent
when the user switches its theme mid-session.

Everything here drives the renderer directly (no pty, no backend): the screen is fed the
escape sequences a dark- or light-themed agent emits, and the decision is checked against
the frame it produces.
"""

from __future__ import annotations

import os

import pytest
from proxy_helpers import make_runner

from agitrack.proxy.renderer import ScreenRenderer, forced_canvas_osc_values

LIGHT_TERMINAL = b"rgb:ffff/ffff/ffff"
DARK_TERMINAL = b"rgb:0000/0000/0000"

# What a themed agent puts on the screen: coloured text, plus a filled panel (an input box,
# a banner) — the two signals the canvas decision reads.
DARK_AGENT = b"\x1b[38;2;212;212;212m" + b"dark theme text line\r\n" * 8 + b"\x1b[48;2;30;30;30m" + b" panel " * 6
LIGHT_AGENT = b"\x1b[38;2;40;40;40m" + b"light theme text line\r\n" * 8 + b"\x1b[48;2;245;245;245m" + b" panel " * 6
PLAIN_AGENT = b"no colours here at all\r\n" * 8  # nothing to infer a scheme from


def make_renderer(host_bg: bytes | None, feed: bytes, *, setting: str = "auto", rows: int = 12, cols: int = 60):
    renderer = ScreenRenderer(rows, cols)
    renderer.host_bg_value = host_bg
    renderer.agent_background = setting
    renderer.init_screen(rows, cols)
    assert renderer.stream is not None
    renderer.stream.feed(feed)
    return renderer


def sample(renderer, *, times: int = 1) -> None:
    """Run the canvas decision ``times`` times, far enough apart to clear the throttle."""
    body = renderer.visible_lines(renderer.rows)
    for step in range(times):
        renderer.update_canvas(body, now=renderer._canvas_sampled_at + 10 * (step + 1))


# ---------------------------------------------------------------------------
# Reading the agent's colour scheme off the screen
# ---------------------------------------------------------------------------


def test_agent_theme_read_from_what_the_backend_paints():
    dark = make_renderer(LIGHT_TERMINAL, DARK_AGENT)
    light = make_renderer(LIGHT_TERMINAL, LIGHT_AGENT)
    assert dark.agent_theme_is_dark(dark.visible_lines(dark.rows)) is True
    assert light.agent_theme_is_dark(light.visible_lines(light.rows)) is False


def test_an_uncoloured_screen_has_no_opinion():
    # No fills, no coloured text: nothing to infer, and inferring anyway would flip the
    # canvas on every plain screen (a backend still starting up, a cleared session).
    plain = make_renderer(LIGHT_TERMINAL, PLAIN_AGENT)
    assert plain.agent_theme_is_dark(plain.visible_lines(plain.rows)) is None
    sample(plain, times=4)
    assert plain._canvas is None


def test_a_few_coloured_cells_do_not_decide_the_screen():
    # Two coloured words are below every minimum, so a stray highlight can't repaint the
    # whole background.
    renderer = make_renderer(LIGHT_TERMINAL, b"plain\r\n\x1b[38;2;250;250;250mtwo words\x1b[0m\r\nplain\r\n")
    assert renderer.agent_theme_is_dark(renderer.visible_lines(renderer.rows)) is None


# ---------------------------------------------------------------------------
# Adopting (and not adopting) a canvas
# ---------------------------------------------------------------------------


def test_dark_agent_in_a_light_terminal_gets_a_dark_canvas():
    renderer = make_renderer(LIGHT_TERMINAL, DARK_AGENT)
    sample(renderer)
    assert renderer._canvas == ("1c1c1c", "d0d0d0")


def test_light_agent_in_a_dark_terminal_gets_a_light_canvas():
    renderer = make_renderer(DARK_TERMINAL, LIGHT_AGENT)
    sample(renderer)
    assert renderer._canvas == ("ffffff", "1c1c1c")


@pytest.mark.parametrize("host,feed", [(DARK_TERMINAL, DARK_AGENT), (LIGHT_TERMINAL, LIGHT_AGENT)])
def test_a_matching_terminal_is_left_completely_alone(host, feed):
    # The common setup. Nothing is overridden, so the frame is byte-identical to what
    # aGiTrack drew before the canvas existed.
    renderer = make_renderer(host, feed)
    sample(renderer, times=4)
    assert renderer._canvas is None
    assert renderer.reset_sgr() == "\x1b[0m"
    assert renderer.canvas_sgr_body() == ""


def test_the_first_canvas_is_adopted_without_waiting_for_repeats():
    # Votes are only cast when something repaints, and a session that has gone quiet does
    # not repaint — so requiring repeats here could leave a mismatched screen half dark and
    # half white until the user typed.
    renderer = make_renderer(LIGHT_TERMINAL, DARK_AGENT)
    sample(renderer)
    assert renderer._canvas is not None


def test_a_canvas_change_asks_for_a_repaint():
    # The status bar for this frame was composed before the decision, so the new scheme
    # needs one more paint to cover the whole screen.
    renderer = make_renderer(LIGHT_TERMINAL, DARK_AGENT)
    renderer._render_pending = False
    sample(renderer)
    assert renderer._render_pending is True


def test_the_decision_is_throttled():
    renderer = make_renderer(LIGHT_TERMINAL, DARK_AGENT)
    body = renderer.visible_lines(renderer.rows)
    renderer.update_canvas(body, now=100.0)
    assert renderer._canvas is not None
    renderer._canvas = None  # a second look inside the sample interval must not re-decide
    renderer.update_canvas(body, now=100.0 + ScreenRenderer.CANVAS_SAMPLE_INTERVAL / 2)
    assert renderer._canvas is None


# ---------------------------------------------------------------------------
# Following a theme switch inside the agent
# ---------------------------------------------------------------------------


def test_switching_the_agent_theme_switches_the_canvas():
    # The user runs /theme inside the backend: the canvas must follow, not stay on the
    # scheme the session started in.
    renderer = make_renderer(LIGHT_TERMINAL, DARK_AGENT)
    sample(renderer)
    assert renderer._canvas == ("1c1c1c", "d0d0d0")

    renderer.init_screen(renderer.rows, renderer.cols)  # the agent repaints in its new theme
    assert renderer.stream is not None
    renderer.stream.feed(LIGHT_AGENT)
    sample(renderer, times=ScreenRenderer.CANVAS_VOTES_TO_SWITCH)
    assert renderer._canvas is None  # a light agent in a light terminal needs no canvas


def test_one_odd_frame_does_not_flip_an_established_canvas():
    # A full-screen diff or a light banner inside a dark session is exactly the transient
    # that must NOT repaint the whole background.
    renderer = make_renderer(LIGHT_TERMINAL, DARK_AGENT)
    sample(renderer)
    established = renderer._canvas

    renderer.init_screen(renderer.rows, renderer.cols)
    assert renderer.stream is not None
    renderer.stream.feed(LIGHT_AGENT)
    sample(renderer)  # a single disagreeing sample
    assert renderer._canvas == established


# ---------------------------------------------------------------------------
# What actually gets painted
# ---------------------------------------------------------------------------


def test_unpainted_cells_carry_the_canvas_instead_of_the_terminal_background():
    renderer = make_renderer(LIGHT_TERMINAL, DARK_AGENT)
    sample(renderer)
    body = renderer.visible_lines(renderer.rows)

    text_line = renderer.render_line(body[0], cols=20)
    assert "48;2;28;28;28" in text_line  # the agent's own text now sits on the canvas
    empty_line = renderer.render_line(body[len(body) - 1], cols=10)
    assert "48;2;28;28;28" in empty_line  # …and so does a row the agent never wrote to
    assert "38;2;208;208;208" in empty_line  # with a legible default foreground


def test_without_a_canvas_the_line_is_emitted_exactly_as_before():
    renderer = make_renderer(DARK_TERMINAL, DARK_AGENT)
    sample(renderer)
    body = renderer.visible_lines(renderer.rows)
    assert renderer.render_line(body[len(body) - 1], cols=5) == "     "  # untouched row: plain spaces


def test_agitracks_own_chrome_sits_on_the_canvas():
    renderer = make_renderer(LIGHT_TERMINAL, DARK_AGENT)
    sample(renderer)
    # The status bar inverts the canvas colours (not the terminal's), and a popup's
    # interior resets to them, so aGiTrack's UI reads as part of the same screen.
    status = renderer.status_line(
        cols=40,
        name="s",
        backend_name="claude",
        session_id=None,
        base_branch=None,
        worktree=None,
        scroll_back=0,
        user_declined=[],
        short_session_fn=lambda value: value,
    )
    assert status.startswith("\x1b[0;38;2;208;208;208;48;2;28;28;28m\x1b[7m")
    parts: list[str] = []
    renderer.append_box(parts, 1, 1, 20, ["hello"], rows=10)
    assert any("48;2;28;28;28" in part for part in parts)


# ---------------------------------------------------------------------------
# The agent_background setting
# ---------------------------------------------------------------------------


def test_forced_dark_and_light_ignore_both_the_agent_and_the_terminal():
    dark = make_renderer(DARK_TERMINAL, LIGHT_AGENT, setting="dark")
    sample(dark)
    assert dark._canvas == ("1c1c1c", "d0d0d0")
    light = make_renderer(LIGHT_TERMINAL, DARK_AGENT, setting="light")
    sample(light)
    assert light._canvas == ("ffffff", "1c1c1c")


def test_terminal_setting_opts_out_of_the_whole_feature():
    renderer = make_renderer(LIGHT_TERMINAL, DARK_AGENT, setting="terminal")
    sample(renderer, times=4)
    assert renderer._canvas is None
    assert renderer.reset_sgr() == "\x1b[0m"


def test_an_unknown_host_background_is_treated_as_dark():
    # A terminal that never answered the OSC 11 query: assume the common default rather
    # than repainting a screen on a guess.
    renderer = make_renderer(None, DARK_AGENT)
    sample(renderer, times=4)
    assert renderer._canvas is None


# ---------------------------------------------------------------------------
# What the backend is told about the background
# ---------------------------------------------------------------------------


def test_only_a_forced_background_is_reported_to_the_backend():
    # Under "auto" the relay stays truthful: the canvas is inferred FROM what the backend
    # paints, so reporting it back could make the two chase each other.
    assert forced_canvas_osc_values(make_renderer(LIGHT_TERMINAL, DARK_AGENT)) is None
    assert forced_canvas_osc_values(make_renderer(LIGHT_TERMINAL, DARK_AGENT, setting="terminal")) is None
    forced = forced_canvas_osc_values(make_renderer(LIGHT_TERMINAL, DARK_AGENT, setting="dark"))
    assert forced == (b"rgb:d0d0/d0d0/d0d0", b"rgb:1c1c/1c1c/1c1c")
    forced_light = forced_canvas_osc_values(make_renderer(DARK_TERMINAL, LIGHT_AGENT, setting="light"))
    assert forced_light == (b"rgb:1c1c/1c1c/1c1c", b"rgb:ffff/ffff/ffff")


# ---------------------------------------------------------------------------
# The runner side: ProxyRunner drives the canvas through its own delegation
# ---------------------------------------------------------------------------


def test_the_runner_exposes_every_canvas_hook_the_renderer_calls():
    # ScreenRenderer's methods run with ``self`` being the ProxyRunner (duck-typed
    # delegation), so a hook the runner doesn't re-export makes EVERY frame raise —
    # the screen then freezes on whatever was painted last. Regression test for exactly
    # that: reset_sgr/update_canvas were called by render before the runner re-exported them.
    runner = make_runner(cols=20)
    for name in ("reset_sgr", "canvas_sgr_body", "update_canvas", "agent_theme_is_dark"):
        assert callable(getattr(runner, name)), name
    assert runner.reset_sgr() == "\x1b[0m"  # no canvas by default
    for constant in (
        "CANVAS_SAMPLE_INTERVAL",
        "CANVAS_VOTES_TO_SWITCH",
        "CANVAS_MIN_BG_CELLS",
        "CANVAS_MIN_FG_CELLS",
        "CANVAS_MAJORITY",
    ):
        assert getattr(runner, constant) == getattr(ScreenRenderer, constant), constant


def _runner_showing(feed: bytes, *, host_bg: bytes, setting: str = "auto"):
    """A runner whose screen holds what a themed agent painted."""
    runner = make_runner(rows=12, cols=60, host_bg_value=host_bg)
    runner.agent_background = setting
    ScreenRenderer.init_screen(runner, 12, 60)
    runner.stream.feed(feed)
    return runner


def test_the_runner_follows_a_theme_change_while_the_screen_is_idle():
    # The decision is also taken from the main loop, not only while painting: after the
    # agent falls quiet nothing repaints, and a mismatched screen would otherwise stay
    # mismatched until the user typed.
    runner = _runner_showing(DARK_AGENT, host_bg=LIGHT_TERMINAL)
    runner._service_agent_theme()
    assert runner._canvas == ("1c1c1c", "d0d0d0")


def test_the_runner_theme_service_is_silent_when_opted_out():
    runner = _runner_showing(DARK_AGENT, host_bg=LIGHT_TERMINAL, setting="terminal")
    runner._service_agent_theme()
    assert runner._canvas is None


def test_the_runner_theme_service_never_raises_without_a_screen():
    runner = make_runner()
    runner.screen = None
    runner._service_agent_theme()  # must be a no-op, not an exception in the main loop


def test_a_forced_background_is_what_the_backend_is_told(monkeypatch):
    # A backend that themes itself from the reported background (OpenCode) has to agree
    # with the canvas aGiTrack paints, or the two schemes fight.
    runner = make_runner(
        master_fd=99,
        host_fg_value=b"rgb:0000/0000/0000",
        host_bg_value=LIGHT_TERMINAL,
        host_da=None,
        host_palette={},
        screen=None,
    )
    written: list[bytes] = []
    real_write = os.write

    def fake_write(fd, data):
        if fd == 99:
            written.append(data)
            return len(data)
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", fake_write)

    runner.agent_background = "auto"
    runner._answer_terminal_queries(b"\x1b]11;?\x07")
    assert written == [b"\x1b]11;" + LIGHT_TERMINAL + b"\x07"]  # the terminal's real colour

    written.clear()
    runner.agent_background = "dark"
    runner._answer_terminal_queries(b"\x1b]10;?\x07\x1b]11;?\x07")
    assert written == [b"\x1b]10;rgb:d0d0/d0d0/d0d0\x07\x1b]11;rgb:1c1c/1c1c/1c1c\x07"]
