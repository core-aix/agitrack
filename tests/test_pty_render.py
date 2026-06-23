"""End-to-end terminal-rendering tests: real OS pty -> pyte decode -> host render.

The proxy reads a backend TUI from a pseudo-terminal, decodes it with ``pyte``, and
re-renders it onto the host terminal with a reserved status line. That pipeline is where
behaviour diverges across operating systems (BSD vs glibc pty/ioctl) and terminal widths,
and it is not covered by the colour-math unit tests. These tests boot the render pipeline
headlessly — capturing the exact bytes aGiTrack writes to stdout — so the integration is
pinned and runs on every OS the CI matrix covers (macOS + Linux; Windows via WSL = Linux).
"""

from __future__ import annotations

import os
import re
import types
from pathlib import Path

import pytest

from agitrack.config import AgitrackState
from proxy_helpers import capture_fd, make_runner, run_in_pty

pytestmark = pytest.mark.skipif(os.name != "posix", reason="aGiTrack is POSIX-only (Windows runs under WSL = Linux)")


def _render_runner(
    tmp_path: Path, *, rows: int = 10, cols: int = 40, color_mode: str = "truecolor", name: str = "demo"
):
    """A ProxyRunner with a real pyte screen, ready to feed backend bytes and render —
    no TTY, no child process, no network."""
    runner = make_runner(state=AgitrackState(tmp_path), color_mode=color_mode)
    runner._terminal_size = lambda: (rows, cols)
    runner.name = name
    runner.backend = types.SimpleNamespace(name="claude")
    runner.worktree = None
    runner._repo_dir_branch = None
    runner._base_branch = "main"
    runner.base_repo = types.SimpleNamespace(repo=str(tmp_path))
    runner._init_screen()
    return runner


def _feed_and_render(runner, child_bytes: bytes) -> bytes:
    """Decode *child_bytes* into the runner's pyte screen and render once, returning the
    exact bytes written to stdout."""
    runner._feed_child_output(child_bytes)
    with capture_fd() as out:
        runner._render()
    return out[0]


def test_real_pty_child_output_decodes_and_renders(tmp_path):
    # The whole OS pty path: a real child writes through a pseudo-terminal, aGiTrack
    # reads those bytes, pyte decodes them, and the render emits the text plus a
    # reverse-video status line carrying the session name.
    child_bytes = run_in_pty(["printf", "HELLO-PTY-WORLD\\n"])
    runner = _render_runner(tmp_path, name="session-x")

    rendered = _feed_and_render(runner, child_bytes)
    assert "HELLO-PTY-WORLD" in runner.screen.display[0]  # pyte decoded the pty output
    assert b"HELLO-PTY-WORLD" in rendered
    assert b"\x1b[7m" in rendered  # reverse-video status line
    assert b"session-x" in rendered  # the status line names the session


def test_status_line_reserves_the_bottom_row(tmp_path):
    # The status line is aGiTrack's, drawn on the last row; the backend screen gets
    # rows-1 lines so the two never collide.
    runner = _render_runner(tmp_path, rows=12, cols=40)
    assert runner.screen.lines == 11  # one row reserved for aGiTrack's status line
    assert runner.screen.columns == 40
    rendered = _feed_and_render(runner, b"backend text\r\n")
    # The status row is addressed explicitly at the bottom (CSI <rows>;1H or a move into
    # the last row), and rendered in reverse video.
    assert b"\x1b[7m" in rendered


def test_render_addresses_rows_absolutely_so_a_stale_size_cannot_scroll(tmp_path):
    # Each body row and the status bar are placed with an absolute cursor move, not by
    # walking down with \r\n. That way, if `rows` is briefly larger than the real terminal
    # (a shrink not yet observed), the bottom write clamps to the last row instead of
    # scrolling the alt screen — which is what smeared a ghost status bar toward the top.
    runner = _render_runner(tmp_path, rows=10, cols=40, name="sx")
    rendered = _feed_and_render(runner, b"hello\r\n").decode()
    assert "\x1b[10;1H" in rendered  # status bar placed on the reserved bottom row absolutely
    assert "\r\n" not in rendered  # no newline walking, so an over-large `rows` can't scroll


@pytest.mark.parametrize("cols", [20, 80, 120])
def test_screen_width_tracks_terminal_columns(tmp_path, cols):
    # The pyte screen must be sized to the host width, so a line longer than the width
    # wraps onto the next row instead of overflowing.
    runner = _render_runner(tmp_path, rows=8, cols=cols)
    assert runner.screen.columns == cols
    line = b"X" * (cols + 5) + b"\r\n"
    runner._feed_child_output(line)
    assert runner.screen.display[0] == "X" * cols  # first row filled exactly to the width
    assert runner.screen.display[1].startswith("XXXXX")  # overflow wrapped to the next row


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _visible(text: str) -> str:
    """The on-screen text with SGR/cursor escapes stripped, for width measurement."""
    return _ANSI_RE.sub("", text)


@pytest.mark.parametrize("cols", [8, 12, 20, 30, 40])
def test_status_line_stays_one_line_at_any_width(cols):
    # No matter how narrow the terminal, the status bar must fit on a single row: its
    # visible width never exceeds the column count and it contains no newline. Content
    # below is deliberately wider than every `cols` and exercises the bold-branch path.
    from agitrack.proxy.renderer import ScreenRenderer

    line = ScreenRenderer.status_line(
        None,  # status_line reads only its keyword args, never `self`
        cols=cols,
        name="a-very-long-session-name-that-overflows-narrow-terminals",
        backend_name="claude",
        session_id="abcdef1234567890",
        base_branch="a-long-integration-branch",
        current_dir_branch="main",  # differs from base -> bold-branch path is taken
        worktree=object(),
        scroll_back=42,
        user_declined=[1, 2, 3],
        short_session_fn=lambda sid: (sid or "")[:7],
        menu_label="Ctrl-G",
        summarizer_on=True,
        cwd="/home/someone/code/a/deep/nested/project/path",
    )
    visible = _visible(line)
    assert "\n" not in visible and "\r" not in visible  # single row, never wraps
    assert len(visible) <= cols  # clamped to the terminal width


@pytest.mark.parametrize("inner", [20, 30, 50, 80])
def test_wrap_markup_bolds_without_breaking_box_width(inner):
    # A popup line carrying **bold** markup must wrap to the box's inner width using the
    # *visible* text only: every rendered row is padded to exactly `inner` visible
    # columns (escapes carry no width), the `**` markers never leak as literal text, and
    # the bold run is emitted with SGR — reopened on each row it spans across a wrap.
    from agitrack.proxy.renderer import _wrap_markup

    line = (
        "Some files were left in the worktree. "
        "**the worktree is removed when aGiTrack exits or the session integrates,** "
        "so copy out anything you want to keep."
    )
    rows = _wrap_markup(line, inner)

    assert rows  # produced at least one row
    for row in rows:
        assert "**" not in row  # markers consumed, never shown literally
        assert len(_visible(row)) == inner  # padded to the box width exactly
    joined = "".join(rows)
    assert "\x1b[1m" in joined and "\x1b[22m" in joined  # bold opened and closed
    # Bold opens and closes are balanced across however many rows it spans.
    assert joined.count("\x1b[1m") == joined.count("\x1b[22m")


def test_wrap_markup_only_bolds_the_marked_run():
    # Text outside the markers stays plain; only the marked words are bold.
    from agitrack.proxy.renderer import _wrap_markup

    [row] = _wrap_markup("keep this **bold part** plain", 60)
    assert row.startswith("keep this ")  # leading text is not bold
    assert "\x1b[1mbold part\x1b[22m" in row  # exactly the marked words are bold
    assert _visible(row).rstrip() == "keep this bold part plain"


def test_wrap_markup_breaks_a_long_unspaced_path():
    # A single token longer than the row (e.g. a long path with no spaces) must break
    # across rows instead of overflowing the box; every row fits the inner width exactly.
    from agitrack.proxy.renderer import _wrap_markup

    inner = 24
    path = "/Users/someone/Code/agitrack/.agitrack/worktrees/session-1/very/deep/file.py"
    rows = _wrap_markup(f"Saved to **{path}** now", inner)
    assert len(rows) > 1  # the path forced a wrap
    for row in rows:
        assert len(_visible(row)) == inner  # padded/clamped to the box, never overflowing
    # The path's characters all survive across the wrapped rows (nothing truncated).
    assert path.replace("/", "") in _visible("".join(rows)).replace("/", "").replace(" ", "")


def test_append_box_scrolls_a_message_taller_than_the_screen(tmp_path):
    # A message with more lines than the terminal must not be silently truncated: it
    # windows with ↑/↓ "more" hints, and the scroll offset moves the window.
    from agitrack.proxy.renderer import ScreenRenderer

    runner = _render_runner(tmp_path, rows=10, cols=60)
    message = "\n".join(f"line-{i}" for i in range(40))

    parts: list[str] = []
    ScreenRenderer.append_message_popup(runner, parts, message, rows=10, cols=60, scroll=0)
    top = "".join(parts)
    assert "line-0" in top and "more below" in top  # top of the message + a hint
    assert runner._message_max_scroll > 0  # render recorded that it overflows

    parts2: list[str] = []
    ScreenRenderer.append_message_popup(runner, parts2, message, rows=10, cols=60, scroll=runner._message_max_scroll)
    bottom = "".join(parts2)
    assert "line-39" in bottom and "more above" in bottom  # scrolled to the end


def test_truecolor_sgr_preserved_in_truecolor_mode(tmp_path):
    runner = _render_runner(tmp_path, color_mode="truecolor")
    rendered = _feed_and_render(runner, b"\x1b[38;2;255;128;0mORANGE\x1b[0m\r\n")
    assert b"ORANGE" in rendered
    assert b"38;2;255;128;0" in rendered  # 24-bit colour relayed unchanged


def test_truecolor_sgr_downsampled_in_16_color_mode(tmp_path):
    # A 16-colour host must not be sent 24-bit SGR (it would render wrong or leak); the
    # same orange is snapped to the nearest ANSI-16 code instead.
    runner = _render_runner(tmp_path, color_mode="16")
    rendered = _feed_and_render(runner, b"\x1b[38;2;255;128;0mORANGE\x1b[0m\r\n")
    assert b"ORANGE" in rendered
    assert b"38;2;" not in rendered  # no truecolor escape survived the downsample


def test_cursor_addressing_does_not_leak_as_literal_text(tmp_path):
    # Cursor-position escapes must be consumed by pyte and reflected as placement, never
    # echoed as literal "[2;5H" text into the rendered screen.
    runner = _render_runner(tmp_path, rows=8, cols=20)
    runner._feed_child_output(b"\x1b[2;5HX")
    assert runner.screen.display[1][4] == "X"  # placed at row 2, col 5 (1-based)
    rendered = _feed_and_render(runner, b"")
    assert b"2;5H" not in rendered  # the move sequence itself never appears as text
    assert b"X" in rendered
