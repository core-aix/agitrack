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
