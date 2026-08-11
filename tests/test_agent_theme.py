"""Tests for the agent background ("canvas") in agitrack/proxy/renderer.py.

aGiTrack paints every cell itself, so a cell the backend leaves at the terminal default is
emitted as such and shows the HOST TERMINAL's background. That is the behaviour, and it is
not negotiable: the terminal's own colours are the default and the fallback. Only an explicit
``agent_background`` of "dark"/"light" overrides them, and then for the whole session.

The bulk of this file exists because aGiTrack once inferred the agent's own light/dark scheme
from the colours on screen and repainted the terminal to match. That inference read the
screen's CONTENT, so it moved with the content: a turn that printed a code block (its own dark
fill) voted dark, the plain prose after it voted light, and the background flipped back and
forth every couple of seconds — "the color keeps switching every few seconds, which makes the
TUI unusable". So the tests below assert the *absence* of that: whatever the agent paints, and
whatever the terminal's background is, the canvas is a pure function of the setting.

Everything here drives the renderer directly (no pty, no backend).
"""

from __future__ import annotations

import os
import select

import pytest
from proxy_helpers import make_runner

from agitrack.config.settings import GlobalConfig
from agitrack.proxy.renderer import ScreenRenderer, forced_canvas_osc_values

# The canvas pairs the renderer installs, named so an assertion says which SCHEME it means.
DARK_CANVAS = ("1c1c1c", "d0d0d0")
LIGHT_CANVAS = ("ffffff", "1c1c1c")

# Real Terminal.app profiles, decoded from the user's own preferences. `Novel` and
# `Silver Aerogel` are the ones that broke the old rule: a cream and an exactly-50%-grey
# background sit on the light/dark threshold, so the comparison against them was a coin toss.
TERMINAL_PROFILES = {
    "Basic": b"rgb:ffff/ffff/ffff",  # white (the profile stores no colour: white IS the default)
    "Novel": b"rgb:dfdf/dbdb/c3c3",  # cream — mid-tone
    "Silver Aerogel": b"rgb:8080/8080/8080",  # exactly 50% grey — the worst case
    "Homebrew": b"rgb:0000/0000/0000",  # black
    "Ocean": b"rgb:2222/4f4f/bcbc",  # deep blue
    "Grass": b"rgb:1313/7777/3d3d",  # green
}
LIGHT_TERMINAL = TERMINAL_PROFILES["Basic"]
DARK_TERMINAL = TERMINAL_PROFILES["Homebrew"]

# What a themed agent puts on the screen. These are what the old rule voted on.
DARK_AGENT = b"\x1b[38;2;212;212;212m" + b"dark theme text line\r\n" * 8 + b"\x1b[48;2;30;30;30m" + b" panel " * 6
LIGHT_AGENT = b"\x1b[38;2;40;40;40m" + b"light theme text line\r\n" * 8 + b"\x1b[48;2;245;245;245m" + b" panel " * 6
PLAIN_AGENT = b"no colours here at all\r\n" * 8

# A turn's worth of screens, in the order a real one produces them. The middle frame is the
# code block whose own dark fill used to flip the vote — the user's own diagnosis: "it shows
# dark if the view doesn't have a code snippet, and it switches back to bright when there is".
TURN_FRAMES = [
    PLAIN_AGENT,
    LIGHT_AGENT,
    b"\x1b[48;2;24;24;24m\x1b[38;2;220;220;220m" + b"def render(self): return 1   \r\n" * 9,  # code block
    LIGHT_AGENT,
    b"\x1b[48;2;250;250;250m\x1b[38;2;20;20;20m" + b"plain prose after the block  \r\n" * 9,
    DARK_AGENT,
    PLAIN_AGENT,
]


def make_renderer(host_bg: bytes | None, feed: bytes, *, setting: str = "terminal", rows: int = 12, cols: int = 60):
    renderer = ScreenRenderer(rows, cols)
    renderer.host_bg_value = host_bg
    renderer.agent_background = setting
    renderer.apply_agent_background()
    renderer.init_screen(rows, cols)
    assert renderer.stream is not None
    renderer.stream.feed(feed)
    return renderer


def backgrounds_across(renderer, frames) -> list[str]:
    """The background SGR each frame is painted with, one entry per frame.

    Reads the trailing (never-written-to) row, whose colour is the canvas itself: with no
    canvas it is bare spaces, with one it carries the canvas's fill. Any change in this list
    is a change the user would have watched happen to their whole screen.
    """
    seen = []
    for frame in frames:
        renderer.init_screen(renderer.rows, renderer.cols)
        assert renderer.stream is not None
        renderer.stream.feed(frame)
        body = renderer.visible_lines(renderer.rows)
        seen.append(renderer.render_line(body[len(body) - 1], cols=8))
    return seen


# ---------------------------------------------------------------------------
# The rule: the terminal's colours, unless the user said otherwise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent", [DARK_AGENT, LIGHT_AGENT, PLAIN_AGENT], ids=["dark", "light", "plain"])
@pytest.mark.parametrize("profile", sorted(TERMINAL_PROFILES), ids=sorted(TERMINAL_PROFILES))
def test_the_terminals_own_colours_are_kept_whatever_the_agent_paints(profile, agent):
    # The whole matrix: six real Terminal.app profiles (white, cream, 50% grey, black, blue,
    # green) against a dark, a light and an uncoloured agent. None of the 18 combinations may
    # produce a canvas — there is nothing to weigh up, so there is nothing to get wrong.
    renderer = make_renderer(TERMINAL_PROFILES[profile], agent)
    assert renderer._canvas is None
    assert renderer.reset_sgr() == "\x1b[0m"
    assert renderer.canvas_sgr_body() == ""


def test_an_unknown_terminal_background_changes_nothing_either():
    # No OSC 11 answer and nothing derivable from the platform. "When in doubt, always use the
    # terminal's colour" — and being in doubt is not special-cased, because nothing is inferred.
    for agent in (DARK_AGENT, LIGHT_AGENT):
        assert make_renderer(None, agent)._canvas is None


def test_the_frame_is_emitted_exactly_as_it_would_be_without_the_feature():
    renderer = make_renderer(LIGHT_TERMINAL, DARK_AGENT)
    body = renderer.visible_lines(renderer.rows)
    assert renderer.render_line(body[len(body) - 1], cols=5) == "     "  # untouched row: plain spaces


# ---------------------------------------------------------------------------
# Oscillation: the bug. Content must never move the background.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", sorted(TERMINAL_PROFILES), ids=sorted(TERMINAL_PROFILES))
def test_a_turns_worth_of_changing_content_never_moves_the_background(profile):
    """THE BUG: "the color keeps switching between light and dark every few seconds".

    A turn prints prose, then a code block with its own dark fill, then prose again. The old
    rule read the screen's content, so each of those frames voted differently and the whole
    background followed — repainting the user's screen every couple of seconds for as long as
    the session ran. Painting is now decided by the setting alone, so the sequence of frames
    is irrelevant: every frame in the turn must be painted on the same background.
    """
    renderer = make_renderer(TERMINAL_PROFILES[profile], b"")
    seen = backgrounds_across(renderer, TURN_FRAMES)
    assert len(set(seen)) == 1, f"the background changed mid-turn: {seen}"
    assert renderer._canvas is None


@pytest.mark.parametrize("setting", ["dark", "light"])
def test_a_forced_background_is_just_as_immovable(setting):
    # The other half: an explicit setting is not a starting point that content may revise.
    renderer = make_renderer(TERMINAL_PROFILES["Novel"], b"", setting=setting)
    canvas = renderer._canvas
    assert canvas == (DARK_CANVAS if setting == "dark" else LIGHT_CANVAS)
    seen = backgrounds_across(renderer, TURN_FRAMES)
    assert len(set(seen)) == 1, f"a forced background moved: {seen}"
    assert renderer._canvas == canvas


def test_painting_a_frame_cannot_touch_the_canvas():
    """The structural guarantee behind the two tests above.

    `render` used to call `update_canvas` on the body it was about to paint, so *drawing* the
    screen was also what decided what colour to draw it in. Nothing in the paint path may
    write `_canvas` any more — that is what makes oscillation impossible rather than rare.
    """
    runner = make_runner(rows=12, cols=60, host_bg_value=LIGHT_TERMINAL)
    runner.agent_background = "dark"
    runner._apply_agent_background(repaint=False)
    ScreenRenderer.init_screen(runner, 12, 60)
    for frame in TURN_FRAMES:
        runner.stream.feed(frame)
        runner._render()
        assert runner._canvas == DARK_CANVAS


def test_nothing_samples_the_screen_on_a_timer_any_more():
    """The reactor's timers phase used to rebuild and re-scan the whole visible screen on
    EVERY tick (`_service_agent_theme` → `update_canvas` → a per-cell scan of the frame,
    measured at ~3 ms on a 50x200 screen and ~4 ms at 60x240). That ran on the same thread as
    stdin, up to 60 times a second while output was flowing, purely to re-take a decision that
    no longer exists. Both the service and the scan it drove are gone."""
    runner = make_runner()
    for gone in ("_service_agent_theme", "update_canvas", "agent_theme_is_dark", "remember_agent_theme"):
        assert not hasattr(runner, gone), f"{gone} is back on the hot path"
        assert not hasattr(ScreenRenderer, gone), f"ScreenRenderer.{gone} is back"


def test_the_cell_sgr_cache_is_keyed_on_everything_that_changes_the_answer():
    """`cell_sgr` runs once per CELL of every frame, so its result is memoized on the cell's
    style. A key that left anything out would paint a whole session in a stale colour — the
    canvas and the colour depth are both part of the answer, and both can differ between two
    renderer hosts alive in the same process (a session switch re-derives them).

    The glyph is deliberately NOT in the key: it does not affect the SGR, and including it
    would cache one entry per distinct character for no gain (counted on a real screen: 217
    distinct cells, 13 distinct styles)."""
    from pyte.screens import Char

    cell = Char(data="x", fg="default", bg="default")
    dark = make_renderer(LIGHT_TERMINAL, b"", setting="dark")
    plain = make_renderer(LIGHT_TERMINAL, b"", setting="terminal")

    # Same cell, different canvas → different SGR, and the cache must not conflate them.
    assert dark.cell_sgr(cell) != plain.cell_sgr(cell)
    assert plain.cell_sgr(cell) == ""  # untouched cell, no canvas: nothing is emitted for it
    assert dark.cell_sgr(cell) == dark.cell_sgr(Char(data="Z", fg="default", bg="default"))  # glyph is irrelevant

    # Same cell and canvas, different colour depth → different encoding.
    truecolor = make_renderer(LIGHT_TERMINAL, b"", setting="dark")
    truecolor.color_mode = "truecolor"
    palette = make_renderer(LIGHT_TERMINAL, b"", setting="dark")
    palette.color_mode = "256"
    assert "48;2;28;28;28" in truecolor.cell_sgr(cell)
    assert "48;5;" in palette.cell_sgr(cell)

    # And every attribute still reaches the output.
    styled = Char(data="x", fg="ff0000", bg="00ff00", bold=True, italics=True, underscore=True, reverse=True)
    assert truecolor.cell_sgr(styled) == "1;3;4;7;38;2;255;0;0;48;2;0;255;0"


def test_a_frame_builds_the_visible_screen_exactly_once(monkeypatch):
    # The per-tick scan doubled the screen's cost; the frame itself must still cost one pass.
    renderer = make_renderer(LIGHT_TERMINAL, LIGHT_AGENT)
    monkeypatch.setattr("agitrack.proxy.renderer.write_frame", lambda data: None)
    calls: list[int] = []
    original = renderer.visible_lines
    monkeypatch.setattr(renderer, "visible_lines", lambda rows: (calls.append(rows), original(rows))[1])
    renderer.render(
        rows=renderer.rows,
        cols=renderer.cols,
        scroll_back=0,
        status_line_str="",
        input_capturing=False,
        input_text="",
        input_matches=[],
        input_selected=None,
        message=None,
        message_sticky=False,
        message_until=0.0,
    )
    assert len(calls) == 1, calls


# ---------------------------------------------------------------------------
# What a forced background actually paints
# ---------------------------------------------------------------------------


def test_a_forced_background_fills_the_cells_the_agent_left_alone():
    renderer = make_renderer(LIGHT_TERMINAL, DARK_AGENT, setting="dark")
    body = renderer.visible_lines(renderer.rows)
    text_line = renderer.render_line(body[0], cols=20)
    assert "48;2;28;28;28" in text_line  # the agent's own text now sits on the canvas
    empty_line = renderer.render_line(body[len(body) - 1], cols=10)
    assert "48;2;28;28;28" in empty_line  # …and so does a row the agent never wrote to
    assert "38;2;208;208;208" in empty_line  # with a legible default foreground


def test_agitracks_own_chrome_sits_on_a_forced_canvas():
    renderer = make_renderer(LIGHT_TERMINAL, DARK_AGENT, setting="dark")
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


@pytest.mark.parametrize("setting,canvas", [("dark", DARK_CANVAS), ("light", LIGHT_CANVAS)])
def test_a_forced_background_is_in_place_before_the_first_frame(setting, canvas):
    # Nothing has to be observed first — the setting IS the answer — so the session opens in
    # it rather than switching over once something has been painted.
    renderer = make_renderer(LIGHT_TERMINAL, PLAIN_AGENT, setting=setting)
    assert renderer._canvas == canvas


def test_a_forced_background_needs_no_answer_from_the_terminal():
    for setting, expected in (("dark", DARK_CANVAS), ("light", LIGHT_CANVAS)):
        assert make_renderer(None, DARK_AGENT, setting=setting)._canvas == expected


def test_an_unrecognised_setting_falls_back_to_the_terminals_colours():
    # Belt and braces for a hand-edited config: anything not understood must be the safe state.
    renderer = make_renderer(LIGHT_TERMINAL, DARK_AGENT, setting="chartreuse")
    assert renderer._canvas is None


# ---------------------------------------------------------------------------
# What the backend is told about the background
# ---------------------------------------------------------------------------


def test_the_terminals_real_colour_is_what_the_backend_is_told():
    """This is why no adaptation is needed. A backend that themes itself asks the terminal
    what background it is on; aGiTrack answers truthfully — including from the platform when
    the terminal itself will not answer at all (Apple Terminal) — so the backend paints for
    the user's actual terminal and there is nothing left to disagree with."""
    assert forced_canvas_osc_values(make_renderer(LIGHT_TERMINAL, DARK_AGENT)) is None


def test_a_forced_background_is_reported_in_place_of_the_terminals():
    forced = forced_canvas_osc_values(make_renderer(LIGHT_TERMINAL, DARK_AGENT, setting="dark"))
    assert forced == (b"rgb:d0d0/d0d0/d0d0", b"rgb:1c1c/1c1c/1c1c")
    forced_light = forced_canvas_osc_values(make_renderer(DARK_TERMINAL, LIGHT_AGENT, setting="light"))
    assert forced_light == (b"rgb:1c1c/1c1c/1c1c", b"rgb:ffff/ffff/ffff")


def test_what_the_runner_answers_the_backend_with(monkeypatch):
    # A backend that themes itself from the reported background (codex, opencode) has to agree
    # with whatever aGiTrack paints, or the two schemes fight.
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

    runner.agent_background = "terminal"
    runner._answer_terminal_queries(b"\x1b]11;?\x07")
    assert written == [b"\x1b]11;" + LIGHT_TERMINAL + b"\x07"]  # the terminal's real colour

    written.clear()
    runner.agent_background = "dark"
    runner._answer_terminal_queries(b"\x1b]10;?\x07\x1b]11;?\x07")
    assert written == [b"\x1b]10;rgb:d0d0/d0d0/d0d0\x07\x1b]11;rgb:1c1c/1c1c/1c1c\x07"]


def test_a_silent_terminals_derived_background_is_what_the_backend_is_told(monkeypatch):
    """The whole chain for the terminal that answers NOTHING — which is the reporter's own
    (Apple Terminal implements no colour report at all).

    With inference gone, this relay is the ONLY thing that gets a self-theming backend onto
    the user's actual background, so it must not be quietly orphaned: `_seed_derived_host_background`
    asks the platform, and what it learns has to come back out of `_answer_terminal_queries`.
    Both halves are exercised here rather than mocked in the middle."""
    runner = make_runner(
        master_fd=99, host_fg_value=None, host_bg_value=None, host_da=None, host_palette={}, screen=None
    )
    written: list[bytes] = []
    real_write = os.write
    monkeypatch.setattr(
        os, "write", lambda fd, data: (written.append(data), len(data))[1] if fd == 99 else real_write(fd, data)
    )

    # Nothing known yet and nothing derivable: the query is simply not answered — an invented
    # background would be a guess painted over the user's real one.
    monkeypatch.setattr("agitrack.proxy.host_background.detect_host_background", lambda environ=None: None)
    runner._seed_derived_host_background()
    runner._answer_terminal_queries(b"\x1b]11;?\x07")
    assert written == []

    # Now the platform does know (COLORFGBG, an Apple Terminal profile, …): that value — not a
    # default, and not the backend's own fallback — is what reaches the backend.
    monkeypatch.setattr(
        "agitrack.proxy.host_background.detect_host_background", lambda environ=None: b"rgb:ffff/ffff/ffff"
    )
    runner._seed_derived_host_background()
    runner._answer_terminal_queries(b"\x1b]11;?\x07")
    assert written == [b"\x1b]11;rgb:ffff/ffff/ffff\x07"]

    # And a real reply that arrives later is never overwritten by the derived guess.
    written.clear()
    runner.host_bg_value = DARK_TERMINAL
    runner._seed_derived_host_background()
    runner._answer_terminal_queries(b"\x1b]11;?\x07")
    assert written == [b"\x1b]11;" + DARK_TERMINAL + b"\x07"]


@pytest.mark.parametrize("answered", [True, False], ids=["terminal answers OSC 11", "terminal is silent"])
def test_detection_still_enables_the_kitty_keyboard_for_a_shift_modified_menu_key(monkeypatch, tmp_path, answered):
    """A neighbour of the background derivation, broken by living inside it.

    `_seed_derived_host_background` returns early once a background is known, so once the
    kitty-keyboard enable was moved in there it stopped running at all on a terminal that
    ANSWERS OSC 11 — and on a silent one the only call that reached it was the pre-detection
    one, before `host_kitty_keyboard` was known, so it no-opped too. A shift-modified menu key
    (Ctrl-Shift-G) is then indistinguishable from Ctrl-G and the menu never opens."""
    config = GlobalConfig(path=tmp_path / "config.json")
    config.data["menu_key"] = "ctrl+shift+g"
    assert config.is_shift_modified
    runner = make_runner(host_bg_value=None, global_config=config)
    runner.host_kitty_keyboard = True
    runner._host = None
    monkeypatch.setattr(
        "agitrack.proxy.terminal.TerminalHost.detect_host_terminal",
        lambda self, debug_fn=None: setattr(self, "host_bg_value", LIGHT_TERMINAL) if answered else None,
    )
    monkeypatch.setattr("agitrack.proxy.host_background.detect_host_background", lambda environ=None: None)
    enabled: list[bool] = []
    monkeypatch.setattr(runner, "_enable_kitty_keyboard", lambda: enabled.append(True))

    runner._detect_host_terminal()

    assert enabled == [True]


# ---------------------------------------------------------------------------
# The runner side
# ---------------------------------------------------------------------------


def test_the_runner_exposes_every_canvas_hook_the_renderer_calls():
    # ScreenRenderer's methods run with ``self`` being the ProxyRunner (duck-typed
    # delegation), so a hook the runner doesn't re-export makes EVERY frame raise —
    # the screen then freezes on whatever was painted last.
    runner = make_runner(cols=20)
    for name in ("reset_sgr", "canvas_sgr_body", "apply_agent_background"):
        assert callable(getattr(runner, name)), name
    assert runner.reset_sgr() == "\x1b[0m"  # no canvas by default


def test_the_runner_fills_the_cleared_screen_for_a_forced_background(monkeypatch):
    written: list[bytes] = []
    runner = make_runner(rows=12, cols=60, host_bg_value=LIGHT_TERMINAL)
    runner.agent_background = "dark"
    monkeypatch.setattr("agitrack.proxy.renderer.write_frame", written.append)

    runner._apply_agent_background()

    assert runner._canvas == DARK_CANVAS
    # The alternate screen was just cleared to the TERMINAL's background; repaint it in the
    # forced one, or the user stares at a white screen until the backend's first frame lands.
    assert written and written[0].endswith(b"\x1b[2J\x1b[H") and b"48;2;28;28;28" in written[0]


def test_the_default_setting_writes_nothing_to_the_screen(monkeypatch):
    # Nothing to fill, so no frame — the terminal's own cleared screen is already correct.
    written: list[bytes] = []
    runner = make_runner(rows=12, cols=60, host_bg_value=LIGHT_TERMINAL)
    monkeypatch.setattr("agitrack.proxy.renderer.write_frame", written.append)
    runner._apply_agent_background()
    assert runner._canvas is None
    assert written == []


def test_a_missing_or_broken_config_never_blocks_startup():
    runner = make_runner(host_bg_value=LIGHT_TERMINAL)
    runner.global_config = None  # e.g. a bare/for_testing runner
    runner._apply_agent_background()  # must not raise: this is a display detail
    assert runner._canvas is None


# ---------------------------------------------------------------------------
# The setting itself
# ---------------------------------------------------------------------------


def test_the_default_is_the_terminals_own_colours(tmp_path):
    assert GlobalConfig(path=tmp_path / "config.json").agent_background == "terminal"
    assert GlobalConfig.AGENT_BACKGROUND_CHOICES == ("terminal", "dark", "light")


def test_a_config_still_holding_the_old_auto_reads_as_terminal(tmp_path):
    # "auto" was the inference that oscillated. Every config on every machine that ran the
    # old default still says it, and it must decay to the safe state, not to a guess.
    path = tmp_path / "config.json"
    path.write_text('{"agent_background": "auto"}\n', encoding="utf-8")
    assert GlobalConfig(path=path).agent_background == "terminal"


def test_setting_it_to_something_unknown_falls_back_to_terminal(tmp_path):
    config = GlobalConfig(path=tmp_path / "config.json")
    config.agent_background = "auto"
    assert config.agent_background == "terminal"
    config.agent_background = "dark"
    assert config.agent_background == "dark"


# ---------------------------------------------------------------------------
# Answering the backend while aGiTrack is still starting
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX PTY only: the service selects on the child's pty master fd, and Windows "
    "skips host-terminal capability detection altogether (see _detect_host_terminal).",
)
def test_the_backend_is_answered_before_the_reactor_starts():
    """A backend asks the terminal for its colours in its first milliseconds and gives up
    within ~0.1 s (measured for codex). aGiTrack spawns it early on purpose and used to read
    nothing until the reactor started — one slow startup step later — so the query expired
    unread and the backend fell back to its no-information defaults: codex drew its DARK
    palette, and no panel tint at all, in a white terminal, in every session.

    This is now the ONLY thing keeping a self-theming backend in step with the terminal, so
    it matters more than it did: get the answer to it, and nothing has to be adapted after."""
    import pty
    import time
    import tty

    from agitrack.proxy.process import BackendProcess

    master, slave = pty.openpty()
    # Raw: a CANONICAL slave holds written input in the line discipline until a newline, and
    # a terminal reply carries none — the backend would see nothing, which is not the failure
    # under test.
    tty.setraw(slave)
    runner = make_runner(rows=12, cols=60, host_bg_value=LIGHT_TERMINAL, host_fg_value=b"rgb:1c1c/1c1c/1c1c")
    runner.init_screen(12, 60)
    runner.active.process = BackendProcess(master_fd=master, child_pid=None)
    runner.host_da = b"\x1b[?62c"
    try:
        runner._start_early_capability_service()
        os.write(slave, b"\x1b]11;?\x1b\\hello")  # the query, plus a byte of ordinary output
        reply = b""
        deadline = time.monotonic() + 5.0  # polled, not slept: no fixed-time synchronisation
        while time.monotonic() < deadline and b"\x07" not in reply:
            readable, _, _ = select.select([slave], [], [], 0.05)
            if slave in readable:
                reply += os.read(slave, 4096)
        assert b"\x1b]11;rgb:ffff/ffff/ffff\x07" in reply, reply
    finally:
        runner._stop_early_capability_service()
        os.close(slave)
        os.close(master)
    # Nothing it read is lost: the reactor takes over with the screen already carrying it.
    assert "hello" in "".join(runner.screen.display)


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY only (see above).")
def test_stopping_the_early_service_is_safe_to_repeat():
    # It is stopped on the normal path AND in run()'s finally, so a crash between the spawn
    # and the reactor cannot leave a second reader on the child's fd.
    runner = make_runner()
    runner._stop_early_capability_service()
    runner._stop_early_capability_service()
    assert runner._early_capability_thread is None
