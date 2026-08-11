"""Screen renderer for the proxy package (#29, P1).

Contains :class:`ScreenRenderer` — extracted from :class:`~agitrack.proxy.runner.ProxyRunner`
— plus the module-level palette helpers and :func:`detect_color_mode` that the
renderer depends on.  The helpers are also re-exported from ``agitrack.proxy`` so
existing import sites (tests, shim) keep working unchanged.
"""

from __future__ import annotations

import functools
import os
import re
import sys
import textwrap
import time
from typing import NamedTuple, Protocol

import pyte
import pyte.modes as _pyte_modes

# Device Control String sequences (``ESC P … ST``). pyte does NOT consume these — it renders
# the payload as visible text — so a backend's DCS (e.g. an XTVERSION reply ``ESC P >|tmux…``,
# or, inside tmux, a passthrough-wrapped query ``ESC P tmux; ESC ESC ]11;? BEL ST``) would
# otherwise leak onto the screen as stray characters like "tmux;]11;?". Stripped before the
# feed. Non-greedy up to the first ST; BEL-terminated payloads (e.g. a wrapped OSC query) keep
# the real ST as the only ``ESC \``. Gated on a cheap ``ESC P`` presence check at the call site.
_DCS_RE = re.compile(rb"\x1bP.*?\x1b\\", re.DOTALL)

# Lightweight inline emphasis for box text: ``**bold**`` marks a run that should be
# rendered bold. Only balanced pairs count, so stray ``*``/``**`` stay literal.
_BOLD_MARKUP_RE = re.compile(r"\*\*(.+?)\*\*")


def write_frame(payload: bytes) -> None:
    """Write one rendered frame to the terminal, whole.

    ``os.write`` on a tty is allowed to write SHORT (the driver's buffer fills, or a signal
    lands mid-write), and a frame cut in half is a cut ESCAPE SEQUENCE: the terminal is then
    left in whatever mode the truncated sequence implied, which is how a repaint storm ends
    in a screen nobody can get back. So loop until it is all out.

    A terminal that has gone away (closed window, dropped SSH) surfaces as EPIPE/EIO here.
    That is not an error worth a traceback in the middle of a redraw: drop the frame and let
    the reactor notice the pty is gone and shut down the normal way."""
    # Sliced as bytes rather than through a memoryview: the whole frame goes out in one
    # write on any healthy terminal, so the copy only happens in the rare short-write case,
    # and callers (and test stubs) always see plain bytes.
    while payload:
        try:
            written = os.write(sys.stdout.fileno(), payload)
        except (BrokenPipeError, ConnectionResetError):
            return
        except OSError as error:  # the controlling terminal is gone
            import errno

            if error.errno in (errno.EIO, errno.EBADF, errno.ENXIO):
                return
            raise
        if not written:
            return
        payload = payload[written:]


def _markup_words(line: str) -> list[tuple[str, bool]]:
    """Split *line* into ``(word, bold)`` tokens, dropping the ``**`` markers. Bold
    runs are assumed to fall on whole-word boundaries (true for aGiTrack's notices)."""
    words: list[tuple[str, bool]] = []
    pos = 0
    for match in _BOLD_MARKUP_RE.finditer(line):
        for chunk, bold in ((line[pos : match.start()], False), (match.group(1), True)):
            words.extend((word, bold) for word in chunk.split(" ") if word)
        pos = match.end()
    words.extend((word, False) for word in line[pos:].split(" ") if word)
    return words


def _wrap_markup(line: str, inner: int) -> list[str]:
    """Word-wrap *line* (which contains ``**bold**`` markup) to *inner* visible columns,
    returning rendered rows padded to ``inner`` with the markers turned into SGR bold
    (``\\x1b[1m`` … ``\\x1b[22m``). Width math uses the *visible* text only, so the box
    borders stay aligned, and a bold run that crosses a wrap re-opens on the next row."""
    rows: list[list[tuple[str, bool]]] = []
    current: list[tuple[str, bool]] = []
    used = 0
    for word, bold in _markup_words(line):
        # Break a single token longer than the row (e.g. a long path with no spaces) so it
        # wraps within the box instead of spilling past the border. textwrap handles this
        # for plain lines; the markup path must do it explicitly.
        while len(word) > inner:
            if current:
                rows.append(current)
                current, used = [], 0
            rows.append([(word[:inner], bold)])
            word = word[inner:]
        if not word:
            continue
        extra = len(word) + (1 if current else 0)
        if current and used + extra > inner:
            rows.append(current)
            current, used = [], 0
            extra = len(word)
        current.append((word, bold))
        used += extra
    if current:
        rows.append(current)
    rendered: list[str] = []
    for words in rows:
        buf: list[str] = []
        visible = 0
        bold_open = False
        for index, (word, bold) in enumerate(words):
            if index:
                # Close bold before the separating space when leaving a bold run, so the
                # space itself isn't styled (a run of bold words keeps a bold space).
                if bold_open and not bold:
                    buf.append("\x1b[22m")
                    bold_open = False
                buf.append(" ")
                visible += 1
            if bold and not bold_open:
                buf.append("\x1b[1m")
                bold_open = True
            elif not bold and bold_open:
                buf.append("\x1b[22m")
                bold_open = False
            buf.append(word)
            visible += len(word)
        if bold_open:
            buf.append("\x1b[22m")
        if visible < inner:
            buf.append(" " * (inner - visible))
        rendered.append("".join(buf))
    return rendered


# ---------------------------------------------------------------------------
# xterm-256 palette helpers
# ---------------------------------------------------------------------------

# Map every xterm-256 palette colour back to its index so that colours pyte
# collapsed to hex can be re-emitted in their original 256-colour encoding.
# First occurrence wins, which keeps the ANSI palette indices (0-15) that
# OpenCode's "system" theme relies on, so the host terminal's own palette is
# respected instead of being frozen to fixed RGB values.
_PALETTE_256: list[tuple[int, int, int]] = []
_REVERSE_256: dict[str, int] = {}


def _build_palette_256() -> None:
    try:
        import pyte.graphics as graphics
    except Exception:  # pragma: no cover - pyte always present in practice
        return
    for index in range(256):
        hex_value = graphics.FG_BG_256[index]
        _REVERSE_256.setdefault(hex_value, index)
        _PALETTE_256.append((int(hex_value[0:2], 16), int(hex_value[2:4], 16), int(hex_value[4:6], 16)))


_build_palette_256()


def _nearest_256(red: int, green: int, blue: int) -> int:
    best_index = 0
    best_distance = None
    for index, (pr, pg, pb) in enumerate(_PALETTE_256):
        distance = (pr - red) ** 2 + (pg - green) ** 2 + (pb - blue) ** 2
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index


def _nearest_ansi16(red: int, green: int, blue: int) -> int:
    best_index = 0
    best_distance = None
    for index in range(16):
        pr, pg, pb = _PALETTE_256[index]
        distance = (pr - red) ** 2 + (pg - green) ** 2 + (pb - blue) ** 2
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index


_ANSI_COLOR_NAMES: dict[str, int] = {
    "black": 0,
    "red": 1,
    "green": 2,
    "brown": 3,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
    "grey": 7,
    "gray": 7,
}

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

# WHY THESE ARE CACHED. A cell's colour -> SGR conversion is a PURE function of
# (colour, foreground, colour mode), but it was recomputed for every cell of every frame:
# 40x120 cells x 2 (fg+bg) = 9600 conversions per repaint. On a truecolor terminal that is
# only string formatting, but on a 256- or 16-colour terminal (Apple Terminal sets
# TERM=xterm-256color and no COLORTERM, so `detect_color_mode` returns "256") every hex
# colour that is not an exact palette entry — which is every colour in a diff-coloured code
# block — fell through to `_nearest_256`, a 256-iteration scan. That is 2.4 MILLION inner
# iterations per frame: measured 176 ms to paint one 40x120 frame of diff-coloured
# scrollback, against a 1.0 ms plain frame and a RENDER_MIN_INTERVAL budget of 33 ms.
#
# The throttle cannot save a frame that costs 5.3x the whole frame interval: the reactor
# then holds the GIL essentially 100% of the time while scrolling, which is precisely the
# regime where the stdin reader thread loses bytes off the tty's ~1 KB input queue and
# mouse reports are chopped in half (see AGENTS.md, "Scrolling and repaint budget"). The
# number of DISTINCT colours on a screen is tiny, so a cache collapses the whole cost.
_COLOR_CACHE_SIZE = 4096  # distinct colours per mode; far more than any real screen shows


@functools.lru_cache(maxsize=_COLOR_CACHE_SIZE)
def _hex_color_code(color: str, foreground: bool, mode: str) -> str:
    # Re-emit a hex colour in the same encoding OpenCode used, decided by the
    # shared terminal colour depth. Truecolor terminals get 24-bit colour;
    # 256-colour terminals (e.g. Apple Terminal) get the original palette
    # index so their own palette renders it, exactly like a native session.
    red = int(color[0:2], 16)
    green = int(color[2:4], 16)
    blue = int(color[4:6], 16)
    prefix = "38" if foreground else "48"
    if mode == "truecolor":
        return f"{prefix};2;{red};{green};{blue}"
    index = _REVERSE_256.get(color)
    if index is None:
        index = _nearest_256(red, green, blue)
    if mode == "256":
        return f"{prefix};5;{index}"
    # 16-colour terminals: fall back to the nearest ANSI base/bright code.
    ansi = index if index < 16 else _nearest_ansi16(red, green, blue)
    base = 30 if foreground else 40
    bright_base = 90 if foreground else 100
    return str(base + ansi) if ansi < 8 else str(bright_base + ansi - 8)


@functools.lru_cache(maxsize=_COLOR_CACHE_SIZE)
def _color_code(color: str, foreground: bool, mode: str) -> str | None:
    if color in {"default", ""}:
        return None
    base = 30 if foreground else 40
    bright_base = 90 if foreground else 100
    if len(color) == 6 and all(char in _HEX_DIGITS for char in color):
        return _hex_color_code(color.lower(), foreground, mode)
    if color.startswith("bright"):
        key = color.removeprefix("bright")
        return str(bright_base + _ANSI_COLOR_NAMES[key]) if key in _ANSI_COLOR_NAMES else None
    return str(base + _ANSI_COLOR_NAMES[color]) if color in _ANSI_COLOR_NAMES else None


def detect_color_mode(environ=None) -> str:
    # Mirror the colour-depth detection OpenCode itself uses so that aGiTrack
    # re-emits colours in the exact encoding OpenCode produced. aGiTrack and the
    # backend share an environment, so the same depth applies to both.
    env = os.environ if environ is None else environ
    colorterm = (env.get("COLORTERM") or "").strip().lower()
    if colorterm in {"truecolor", "24bit"}:
        return "truecolor"
    term = (env.get("TERM") or "").strip().lower()
    if "256" in term:
        return "256"
    if colorterm or term:
        return "16"
    return "16"


# ---------------------------------------------------------------------------
# _BackgroundColorEraseScreen
# ---------------------------------------------------------------------------


def _home_relative(path: str) -> str:
    """Abbreviate the user's home directory to ``~`` for display."""
    text = str(path)
    home = os.path.expanduser("~")
    if home and (text == home or text.startswith(home + os.sep)):
        return "~" + text[len(home) :]
    return text


_OSC_RGB_RE = re.compile(r"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)")
_HEX6_RE = re.compile(r"#?([0-9a-fA-F]{6})\b")


def _host_bg_known(host) -> bool:
    """Whether the terminal actually TOLD us its background, as opposed to us assuming one.

    ``_host_bg_is_dark`` has to return a bool for accent contrast, so it guesses dark when the
    answer is missing. A guess is fine for picking a highlight colour and wrong for deciding
    what to report to the backend as the terminal's background — see ``_answer_terminal_queries``.
    """
    raw = getattr(host, "host_bg_value", None)
    if not raw:
        return False
    text = raw.decode("ascii", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
    return bool(_OSC_RGB_RE.search(text) or _HEX6_RE.search(text))


def _host_bg_is_dark(host) -> bool:
    """Whether the terminal background is dark, read from its OSC-11 colour (``host_bg_value``).
    Defaults to dark when unknown/unparseable — the common terminal default — so a popup's
    accent colour can be chosen to contrast the background. Callers that must not act on a
    GUESS check ``_host_bg_known`` first."""
    raw = getattr(host, "host_bg_value", None)
    if not raw:
        return True
    try:
        text = raw.decode("ascii", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
    except Exception:
        return True
    match = _OSC_RGB_RE.search(text)
    if match:
        red, green, blue = (int(group[:2], 16) for group in match.groups())  # high byte of each component
    else:
        hex_match = _HEX6_RE.search(text)
        if not hex_match:
            return True
        value = hex_match.group(1)
        red, green, blue = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    return (0.299 * red + 0.587 * green + 0.114 * blue) < 128


# ---------------------------------------------------------------------------
# Agent background ("canvas")
# ---------------------------------------------------------------------------
#
# aGiTrack draws every cell itself, and a cell the backend left at the terminal's DEFAULT
# background is emitted as such — so it shows the host terminal's background. That is the
# behaviour: **the terminal's own colours are the default and the fallback.** aGiTrack never
# decides on its own to repaint the user's screen in some other scheme.
#
# The one exception is an explicit `agent_background` of "dark" or "light". The user has then
# said which background they want behind the agent, so aGiTrack fills every unpainted cell
# (and its own chrome — status bar, popups — via `reset_sgr`) with that colour and a
# contrasting default foreground, AND reports that colour to the backend in place of the
# terminal's when it asks (`forced_canvas_osc_values`), so a self-theming backend paints to
# match it. The canvas is then a pure function of the setting: it is computed once, at startup
# and whenever the setting is edited, and nothing on screen can ever change it.
#
# aGiTrack USED to infer the agent's own light/dark scheme from the pixels it painted — dark
# fills and light text meaning "this agent expects a dark background" — and repaint the
# terminal to match. That is removed, and it cannot come back in this shape. It read the
# CONTENT of the screen, so the decision moved with the content: a turn that printed a code
# block (its own dark fill) voted dark, the plain prose after it voted light, and the whole
# background flipped back and forth every couple of seconds for as long as the session ran.
# Mid-tone terminals made it worse — Terminal.app's "Novel" (cream, #dfdbc3) and "Silver
# Aerogel" (#808080, exactly the 50% the comparison was against) sit on the threshold, so the
# "does the agent disagree with the terminal?" half of the rule was a coin toss too. There is
# no content-derived signal that fixes this: any rule read off the screen changes when the
# screen does, and no per-backend tuning of minimums, majorities or which glyphs count changes
# that. Backends that theme themselves already ask the terminal what background they are on
# (OSC 11), and aGiTrack answers truthfully — including from the platform when the terminal
# itself will not answer (`host_background.py`) — so those backends agree with the terminal by
# construction and there is nothing left to adapt to.

_CANVAS_DARK = ("1c1c1c", "d0d0d0")  # (background, default foreground) for a forced dark background
_CANVAS_LIGHT = ("ffffff", "1c1c1c")  # …and for a forced light one


def _osc_color_value(color: str) -> bytes:
    """A hex colour as a terminal OSC 10/11 report body (``rgb:rrrr/gggg/bbbb``)."""
    red, green, blue = color[0:2], color[2:4], color[4:6]
    return f"rgb:{red}{red}/{green}{green}/{blue}{blue}".encode("ascii")


def forced_canvas_osc_values(host) -> tuple[bytes, bytes] | None:
    """``(foreground, background)`` OSC report bodies to answer the backend with when the
    user FORCED a background ("dark"/"light"), or None to relay the host terminal's own.

    Only the forced settings answer with something other than the terminal's real colours:
    a backend that themes itself from the reported background (codex, opencode) then matches
    the canvas aGiTrack paints. Otherwise the relay is truthful, which is the whole reason no
    adaptation is needed: the backend is told exactly what the user's terminal is drawing on
    (from the platform, if the terminal itself will not say) and themes to it."""
    setting = getattr(host, "agent_background", "auto")
    if setting not in {"dark", "light"}:
        return None
    background, foreground = _CANVAS_DARK if setting == "dark" else _CANVAS_LIGHT
    return _osc_color_value(foreground), _osc_color_value(background)


def _box_accent_sgr(host) -> str:
    """A bold SGR that colours a popup's border so the box stands out — a hue chosen to
    contrast the terminal background and encoded for its colour depth (via ``hex_color_code``).
    Only the border is coloured; the box's text keeps the default colour so it stays legible
    against whatever the terminal background is."""
    accent = "5ee7df" if _host_bg_is_dark(host) else "005f87"  # bright cyan on dark, deep blue on light
    return "\x1b[1;" + host.hex_color_code(accent, foreground=True) + "m"


class _DimChar(NamedTuple):
    """pyte's :class:`~pyte.screens.Char` plus a ``dim`` (faint, SGR 2) flag. pyte 0.8.2
    has no slot for faint and silently drops ``\\x1b[2m``, so dim text — e.g. Claude's
    autosuggestion ghost text — would otherwise re-render at full intensity (#113). Field
    order mirrors pyte's Char so name-based ``_replace``/``_asdict`` interop is exact."""

    data: str = " "
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    italics: bool = False
    underscore: bool = False
    strikethrough: bool = False
    reverse: bool = False
    blink: bool = False
    dim: bool = False


class _BackgroundColorEraseScreen(pyte.HistoryScreen):
    # pyte erases cells using the cursor's *full* SGR attributes, so a backend
    # that clears the screen (or a line) while underline — or any glyph
    # attribute — is still active leaves the blanked cells carrying that
    # attribute. The host terminal then renders those underlined blanks as stray
    # horizontal lines that linger after the view is dismissed (seen on Claude's
    # session-choice picker). Real terminals do background-colour-erase: erased
    # cells keep only the background colour, not glyph attributes. Mirror that by
    # blanking everything except the background on the cursor attrs we erase with.
    # --- faint (SGR 2) tracking ---------------------------------------------
    # pyte 0.8.2 ignores faint entirely (no ``dim`` field on Char, no ``2`` in its
    # SGR map), so dim text loses its dimness when aGiTrack re-renders it and shows
    # up at full intensity — most visibly Claude's grey autosuggestion ghost text,
    # which then looks identical to what the user typed (#113). We carry ``dim`` on
    # an extended Char (:class:`_DimChar`) ourselves and re-emit it in ``cell_sgr``.

    @property
    def default_char(self) -> _DimChar:  # type: ignore[override]
        reverse = _pyte_modes.DECSCNM in self.mode
        return _DimChar(data=" ", fg="default", bg="default", reverse=reverse)

    def reset(self) -> None:
        super().reset()
        # pyte's reset() leaves the cursor carrying a plain Char (no ``dim`` slot);
        # promote it to our default so every subsequent ``_replace`` keeps ``dim``.
        self.cursor.attrs = self.default_char  # type: ignore[assignment]  # _DimChar extends Char

    def select_graphic_rendition(self, *attrs: int) -> None:
        # Let pyte handle everything it knows (it harmlessly ignores the faint code),
        # then apply the faint state we tracked. A reset (0) or normal-intensity (22)
        # clears faint; a standalone 2 sets it; last occurrence wins.
        #
        # The 2 must be parsed exactly as pyte does, NOT matched anywhere in the list:
        # in a 24-bit colour like ``38;2;r;g;b`` (or ``48;2;…``) the ``2`` is the colour
        # selector, not faint. Consuming 38/48's parameters here keeps normal truecolor
        # text (e.g. the user's own white input) from being wrongly dimmed (#113 follow-up).
        dim: bool | None = None
        remaining = list(reversed(attrs or (0,)))
        while remaining:
            attr = remaining.pop()
            if attr == 2:
                dim = True
            elif attr in (0, 22):
                dim = False
            elif attr in (38, 48):  # extended fg/bg — skip its parameters
                mode = remaining.pop() if remaining else None
                if mode == 5 and remaining:  # 256-colour: one index byte
                    remaining.pop()
                elif mode == 2:  # 24-bit: three colour bytes
                    del remaining[-3:]
        super().select_graphic_rendition(*attrs)
        if dim is None:
            return
        current = self.cursor.attrs
        if hasattr(current, "dim"):
            self.cursor.attrs = current._replace(dim=dim)  # type: ignore[call-arg]  # _DimChar has dim
        elif dim:  # cursor still on a plain pyte Char — promote it
            self.cursor.attrs = _DimChar(**current._asdict(), dim=True)  # type: ignore[assignment]

    def _erase_attrs(self):
        return self.cursor.attrs._replace(
            data=" ",
            fg="default",
            bold=False,
            italics=False,
            underscore=False,
            strikethrough=False,
            reverse=False,
            blink=False,
            dim=False,
        )

    def erase_in_line(self, how: int = 0, private: bool = False) -> None:
        saved = self.cursor.attrs
        self.cursor.attrs = self._erase_attrs()
        try:
            super().erase_in_line(how, private)
        finally:
            self.cursor.attrs = saved

    def erase_in_display(self, how: int = 0, *args, **kwargs) -> None:
        saved = self.cursor.attrs
        self.cursor.attrs = self._erase_attrs()
        try:
            super().erase_in_display(how, *args, **kwargs)
        finally:
            self.cursor.attrs = saved

    def report_device_status(self, mode: int = 0, private: bool = False, **kwargs) -> None:
        # pyte's stream invokes report_device_status(mode, private=True) for
        # DEC-private DSR queries — notably ``\x1b[?6n`` (cursor-position request),
        # which Claude/Ink emits while redrawing — but pyte's own
        # Screen.report_device_status() doesn't accept ``private`` and raises
        # TypeError mid-parse. aGiTrack swallows feed errors to stay alive, but that
        # drops the rest of the output chunk: it truncated Claude's option-menu
        # collapse redraw, leaving stale menu rows on screen. aGiTrack answers terminal
        # queries itself (_answer_terminal_queries), so pyte's report is unused —
        # just accept ``private`` and never raise so the feed completes.
        if private:
            return
        try:
            super().report_device_status(mode)
        except TypeError:
            pass


# ---------------------------------------------------------------------------
# RendererHost protocol
# ---------------------------------------------------------------------------


class RendererHost(Protocol):
    """Structural type for whatever ``ScreenRenderer``'s methods run against.

    ``ScreenRenderer`` satisfies this directly; :class:`~agitrack.proxy.runner.ProxyRunner`
    satisfies it via its session-backed properties and thin delegator methods, so
    the runner can call ``ScreenRenderer.method(self, ...)`` unbound and still
    type-check. It enumerates only the ``self.<attr>`` / ``self.<method>`` surface
    the renderer methods actually touch.
    """

    # Per-session display state (owned by Session; mirrored on the renderer)
    rows: int
    cols: int
    color_mode: str
    screen: pyte.HistoryScreen | None
    stream: pyte.ByteStream | None
    scroll_back: int
    child_mouse: bool
    sel_active: bool
    sel_anchor: tuple[int, int] | None
    sel_point: tuple[int, int] | None
    # The session stores a _BackgroundColorEraseScreen (a pyte.HistoryScreen
    # subclass); the renderer and the runner both expose it under the HistoryScreen
    # type, which is the narrowest shared type that still exposes .history and keeps
    # the three sites invariant-compatible for this protocol.

    # Render-throttle state (host-level, not swapped per session)
    _last_render: float
    _render_pending: bool
    _in_sync_update: bool
    _sync_since: float
    # How far the current popup message can still scroll (0 = it fits on screen); set
    # during render so the input loop knows whether PgUp/PgDn should scroll the message.
    _message_max_scroll: int

    # Agent-theme adaptation (host-level): the user's setting, the canvas currently in
    # effect, and the vote/sample state that keeps it from flipping on a transient frame.
    agent_background: str
    _canvas: tuple[str, str] | None
    _canvas_votes: int
    _canvas_sampled_at: float
    _canvas_decided: bool
    CANVAS_SAMPLE_INTERVAL: float
    CANVAS_VOTES_TO_SWITCH: int
    CANVAS_MIN_BG_CELLS: int
    CANVAS_MIN_FG_CELLS: int
    CANVAS_MAJORITY: float

    # Renderer methods invoked on ``self`` from sibling methods
    def cell_sgr(self, cell) -> str: ...
    def agent_theme_is_dark(self, body) -> bool | None: ...
    def update_canvas(self, body, *, now: float | None = ...) -> None: ...
    def apply_remembered_theme(self, agent_dark: bool | None) -> None: ...
    def canvas_sgr_body(self) -> str: ...
    def reset_sgr(self) -> str: ...
    def color_code(self, color: str, *, foreground: bool) -> str | None: ...
    def hex_color_code(self, color: str, *, foreground: bool) -> str: ...
    def history_len(self) -> int: ...
    def render_line(self, cells, sel: tuple[int, int] | None = ..., *, cols: int) -> str: ...
    def selection_ranges(self, cols: int) -> dict[int, tuple[int, int]]: ...
    def sync_hold(self, now: float, sync_max_hold: float) -> bool: ...
    def visible_lines(self, rows: int) -> list: ...
    def cursor_sequence(self, rows: int, cols: int, scroll_back: int) -> str: ...
    def append_box(
        self,
        parts: list[str],
        row: int,
        col: int,
        width: int,
        lines: list[str],
        highlight: str | None = ...,
        *,
        rows: int,
        scrollable: bool = ...,
        scroll: int = ...,
    ) -> None: ...
    def wrapped_message_height(self, lines: list[str], cols: int) -> int: ...
    def append_command_palette(
        self,
        parts: list[str],
        *,
        rows: int,
        cols: int,
        input_text: str,
        input_matches: list[str],
        input_selected: str | None,
    ) -> None: ...
    def append_message_popup(
        self, parts: list[str], message: str, *, rows: int, cols: int, scroll: int = ...
    ) -> None: ...


# ---------------------------------------------------------------------------
# ScreenRenderer
# ---------------------------------------------------------------------------


class ScreenRenderer:
    """Owns the pyte screen/stream and converts the grid to ANSI output.

    Per-session display state (screen, stream, scroll_back, sel_active,
    sel_anchor, sel_point, child_mouse) lives on each proxy Session object
    (agitrack.proxy.session); the runner exposes it under the same attribute names
    via properties that delegate to the active session (see runner.py), so the
    duck-typed delegation here keeps reading ``self.<attr>`` unchanged.

    Render-throttle state (_last_render, _render_pending, _in_sync_update,
    _sync_since) is host-level and is NOT swapped per session.
    """

    # Throttle / sync-update constants (same defaults as ProxyRunner class
    # constants so __new__-built test runners that never call __init__ still
    # resolve them via getattr-default style).
    RENDER_MIN_INTERVAL = 0.033  # coalesce output-driven repaints to ~30fps
    SYNC_MAX_HOLD = 0.05  # cap how long a backend synchronized-update may defer a paint

    def __init__(self, rows: int, cols: int, *, color_mode: str = "truecolor") -> None:
        self.rows = rows
        self.cols = cols
        self.color_mode = color_mode

        # Per-session display state (owned by Session; mirrored here). Typed as
        # pyte.HistoryScreen so it matches the runner's annotation for the shared
        # RendererHost protocol; init_screen assigns a _BackgroundColorEraseScreen.
        self.screen: pyte.HistoryScreen | None = None
        self.stream: pyte.ByteStream | None = None
        self.scroll_back: int = 0
        self.child_mouse: bool = False
        self.sel_active: bool = False
        self.sel_anchor: tuple[int, int] | None = None
        self.sel_point: tuple[int, int] | None = None

        # Render-throttle state (host-level, not swapped)
        self._last_render: float = 0.0
        self._render_pending: bool = False
        self._in_sync_update: bool = False
        self._sync_since: float = 0.0

        # Agent-theme adaptation (see "canvas" above). ``agent_background`` is the user's
        # setting ("auto" | "dark" | "light" | "terminal"); ``_canvas`` is the (bg, fg) pair
        # currently painted under the agent, or None to use the host terminal's own colours.
        self.agent_background: str = "auto"
        self._canvas: tuple[str, str] | None = None
        self._canvas_votes: int = 0  # consecutive samples agreeing on a DIFFERENT canvas
        self._canvas_sampled_at: float = 0.0
        # Whether a frame has actually told us the agent's scheme yet. Until it has, every
        # frame is sampled (no throttle) and any canvas in place is only a remembered guess.
        self._canvas_decided: bool = False

    # ------------------------------------------------------------------
    # Screen initialisation
    # ------------------------------------------------------------------

    def init_screen(self: RendererHost, rows: int, cols: int) -> None:
        """Create (or replace) the pyte screen for the given terminal size."""
        self.rows = rows
        self.cols = cols
        self.screen = _BackgroundColorEraseScreen(cols, max(rows - 1, 1), history=5000, ratio=0.5)
        self.stream = pyte.ByteStream(self.screen)
        self.scroll_back = 0
        self._in_sync_update = False

    # ------------------------------------------------------------------
    # Feed
    # ------------------------------------------------------------------

    def feed(self: RendererHost, output: bytes, *, pyte_hostile_csi_re) -> None:
        """Feed child output into the pyte model (strips pyte-hostile CSI and DCS)."""
        if self.stream is not None:
            try:
                cleaned = pyte_hostile_csi_re.sub(b"", output)
                if b"\x1bP" in cleaned:  # cheap gate: only scan for DCS when one is present
                    cleaned = _DCS_RE.sub(b"", cleaned)
                self.stream.feed(cleaned)
            except Exception as error:  # never let a parse hiccup kill the session
                getattr(self, "_debug", lambda message: None)(f"pyte feed error: {error!r}")

    # ------------------------------------------------------------------
    # Synchronized-update tracking
    # ------------------------------------------------------------------

    def track_sync_update(self: RendererHost, output: bytes) -> None:
        # Honor the synchronized-update mode (DECSET 2026): backends wrap a
        # multi-write repaint in BSU (?2026h) / ESU (?2026l) so consumers can
        # apply it atomically. While inside such an update aGiTrack defers its own
        # repaint, so it never paints a half-drawn frame (the cause of tearing).
        # Only the last marker in the chunk decides the resulting state; a
        # stuck-open update is bounded by SYNC_MAX_HOLD in the paint deciders.
        begin = output.rfind(b"\x1b[?2026h")
        end = output.rfind(b"\x1b[?2026l")
        if begin == -1 and end == -1:
            return
        in_update = begin > end
        if in_update and not self._in_sync_update:
            self._sync_since = time.monotonic()
        self._in_sync_update = in_update

    def sync_hold(self: RendererHost, now: float, sync_max_hold: float) -> bool:
        """True while a backend synchronized-update should still defer the paint."""
        return self._in_sync_update and now - self._sync_since < sync_max_hold

    # ------------------------------------------------------------------
    # Throttled render dispatch
    # ------------------------------------------------------------------

    def render_output(self: RendererHost, render_fn, render_min_interval: float, sync_max_hold: float) -> None:
        """Coalesce repaints driven by a flood of backend output to ~30fps."""
        now = time.monotonic()
        if self.sync_hold(now, sync_max_hold):
            self._render_pending = True
            return
        if now - self._last_render >= render_min_interval:
            self._last_render = now
            self._render_pending = False
            render_fn()
        else:
            self._render_pending = True

    def flush_pending_render(self: RendererHost, render_fn, render_min_interval: float, sync_max_hold: float) -> None:
        if not self._render_pending:
            return
        now = time.monotonic()
        if self.sync_hold(now, sync_max_hold):
            return
        if now - self._last_render >= render_min_interval:
            self._last_render = now
            self._render_pending = False
            render_fn()

    # ------------------------------------------------------------------
    # Cursor
    # ------------------------------------------------------------------

    def cursor_sequence(self: RendererHost, rows: int, cols: int, scroll_back: int) -> str:
        """The trailing sequence that positions (and shows) or hides the cursor."""
        assert self.screen is not None
        if scroll_back > 0:
            # While scrolled into history, keep the cursor hidden (its live
            # position is not meaningful for the displayed lines).
            return "\x1b[?25l"
        cursor = self.screen.cursor
        cursor_row = min(cursor.y + 1, max(rows - 1, 1))
        cursor_col = min(cursor.x + 1, cols)
        return f"\x1b[{cursor_row};{cursor_col}H\x1b[?25h"

    # ------------------------------------------------------------------
    # Visible lines / history / scrollback
    # ------------------------------------------------------------------

    def history_len(self: RendererHost) -> int:
        # `screen` always resolves (ScreenRenderer sets it in __init__; the
        # runner delegates it to the active Session) but plain pyte.Screen has
        # no history attribute, hence the getattr on it.
        history = getattr(self.screen, "history", None)
        return len(history.top) if history is not None else 0

    def scroll(self: RendererHost, delta: int, render_fn, *, min_interval: float = 0.0) -> bool:
        """Move the view ``delta`` lines through history. Returns whether a repaint is still
        owed (the caller then flags ``_render_pending`` and lets the main loop paint it).

        ``min_interval`` puts scrolling on the SAME frame budget as backend output. Without
        it every wheel event painted the whole screen immediately: a single trackpad flick
        (hundreds of events) meant hundreds of full frames and megabytes written to the
        terminal through a BLOCKING ``os.write``, which stalls the reactor, backs up the
        backend's pty, and looks exactly like aGiTrack hanging."""
        new_back = max(0, min(self.scroll_back + delta, self.history_len()))
        if new_back == self.scroll_back:
            return False
        self.scroll_back = new_back
        # Selection coordinates refer to the displayed view, which just
        # shifted; drop any in-progress selection.
        self.sel_active = False
        self.sel_anchor = self.sel_point = None
        now = time.monotonic()
        if min_interval and now - self._last_render < min_interval:
            return True  # too soon for another frame; the caller schedules it
        self._last_render = now
        self._render_pending = False
        render_fn()
        return False

    def visible_lines(self: RendererHost, rows: int) -> list:
        """The (rows-1) lines to draw. Splices in history when scrolled back."""
        assert self.screen is not None
        num_rows = max(rows - 1, 1)
        live: list = [self.screen.buffer.get(row, {}) for row in range(num_rows)]
        if self.scroll_back <= 0 or not self.history_len():
            return live
        history = list(self.screen.history.top)
        combined = history + live
        end = len(combined) - self.scroll_back
        end = max(num_rows, min(end, len(combined)))
        return combined[end - num_rows : end]

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def selection_ranges(self: RendererHost, cols: int) -> dict[int, tuple[int, int]]:
        """Map each selected display row to its inclusive (start_col, end_col)."""
        if not (self.sel_active and self.sel_anchor and self.sel_point):
            return {}
        (r1, c1), (r2, c2) = sorted([self.sel_anchor, self.sel_point])
        ranges: dict[int, tuple[int, int]] = {}
        for row in range(r1, r2 + 1):
            start = c1 if row == r1 else 0
            end = c2 if row == r2 else cols - 1
            ranges[row] = (start, end)
        return ranges

    def copy_selection(self: RendererHost, rows: int, cols: int, copy_to_clipboard_fn, set_message_fn) -> None:
        lines = self.visible_lines(rows)
        text_lines = []
        for row, (start, end) in sorted(self.selection_ranges(cols).items()):
            cells = lines[row] if row < len(lines) else {}
            text = "".join(((cell := cells.get(x)) and cell.data or " ") for x in range(start, end + 1))
            text_lines.append(text.rstrip())
        text = "\n".join(text_lines).strip("\n")
        if not text.strip():
            return
        copy_to_clipboard_fn(text)
        set_message_fn(f"Copied {len(text)} char(s) to clipboard.", seconds=2.0)

    # ------------------------------------------------------------------
    # Cell / line rendering
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Agent-theme adaptation
    # ------------------------------------------------------------------

    CANVAS_SAMPLE_INTERVAL = 0.5  # how often the agent's colour scheme is re-sampled
    CANVAS_VOTES_TO_SWITCH = 2  # agreeing samples before an ESTABLISHED canvas changes
    CANVAS_MIN_BG_CELLS = 40  # painted background cells needed for the bg signal to count
    CANVAS_MIN_FG_CELLS = 80  # coloured glyphs needed for the (weaker) foreground signal
    CANVAS_MAJORITY = 0.6  # share of the sample that must agree

    def agent_theme_is_dark(self: RendererHost, body) -> bool | None:
        """Whether the backend is painting for a DARK background, read off the frame in
        ``body`` — or None when the frame carries no opinion (a plain, uncoloured screen).

        Two signals, strongest first: the backgrounds the agent fills (a dark theme fills
        dark), and the colour of its text (a dark theme writes light text). Either alone is
        enough; neither is consulted below a minimum sample, so a couple of coloured words
        can't decide the whole screen's canvas.

        The text signal counts only ALPHANUMERIC glyphs — real text, not the rules, borders
        and block graphics a TUI draws around it. That is not a detail: a backend picks a
        readable contrast for text it wants read, but a deliberately subtle one for furniture,
        and the furniture grey often lands on the wrong side of the midpoint in one of the two
        themes. Measured on Claude's opening screen, whose separators are 300 of the ~480
        coloured cells: counting everything gives "light text" for BOTH themes (the light
        theme's #999999 rules outvote its #666666 text), so a light-themed Claude in a dark
        terminal was never recognised and kept the terminal's colours; counting only letters
        and digits gives 144 cells that are unanimous in each theme — #666666 for light,
        #999999 for dark."""
        bg_dark = bg_light = fg_dark = fg_light = 0
        for cells in body:
            for cell in cells.values():
                background = _luminance(getattr(cell, "bg", "default"))
                if background is not None:
                    if background < 128:
                        bg_dark += 1
                    else:
                        bg_light += 1
                if not (getattr(cell, "data", " ") or " ").strip().isalnum():
                    continue  # blank cells and furniture say nothing about the TEXT colour
                foreground = _luminance(getattr(cell, "fg", "default"))
                if foreground is not None:
                    if foreground < 128:
                        fg_dark += 1
                    else:
                        fg_light += 1
        total_bg = bg_dark + bg_light
        if total_bg >= self.CANVAS_MIN_BG_CELLS:
            if bg_dark / total_bg >= self.CANVAS_MAJORITY:
                return True
            if bg_light / total_bg >= self.CANVAS_MAJORITY:
                return False
        total_fg = fg_dark + fg_light
        if total_fg >= self.CANVAS_MIN_FG_CELLS:
            if fg_light / total_fg >= self.CANVAS_MAJORITY:
                return True  # light text ⇒ the agent expects a dark background behind it
            if fg_dark / total_fg >= self.CANVAS_MAJORITY:
                return False
        return None

    def update_canvas(self: RendererHost, body, *, now: float | None = None) -> None:
        """Re-decide the canvas from the frame about to be painted (see "canvas" above).

        ``dark``/``light`` force it, ``terminal`` disables it entirely, and ``auto`` adapts:
        a canvas is painted only while the agent's inferred scheme DISAGREES with the host
        terminal's own background, so the usual matching setup renders exactly as before."""
        setting = getattr(self, "agent_background", "auto")
        if setting == "dark":
            self._canvas = _CANVAS_DARK
            return
        if setting == "light":
            self._canvas = _CANVAS_LIGHT
            return
        if setting != "auto":  # "terminal" (or anything unrecognised): host colours, as before
            self._canvas = None
            return
        moment = time.monotonic() if now is None else now
        # The throttle applies only AFTER the scheme is known. Until then every frame is
        # examined, so the moment the backend paints something readable the screen is already
        # in the right colours — throttling here would instead show the wrong scheme for up to
        # a sample interval at startup, which is precisely when the user is looking at it.
        if self._canvas_decided and moment - self._canvas_sampled_at < self.CANVAS_SAMPLE_INTERVAL:
            return
        self._canvas_sampled_at = moment
        agent_dark = self.agent_theme_is_dark(body)
        if agent_dark is None:
            return  # nothing to learn from this frame; keep whatever is painted now
        if not _host_bg_known(self):
            # THE TERMINAL HAS NOT SAID WHAT IT IS DRAWING ON — no OSC 11 support, or an answer
            # still in flight (tmux, ssh and forwarded sessions routinely reply after detection's
            # bounded wait). Leave its colours alone: an unknown background must never be turned
            # into a repaint of the user's whole screen.
            #
            # Guessing here is what broke it. `_host_bg_is_dark` answers "dark" when it does not
            # know, so a dark agent "matched" a WHITE terminal, no canvas was painted — and the
            # decision LATCHED, because the code below sets `_canvas_decided`. Every later change
            # then needs CANVAS_VOTES_TO_SWITCH agreeing samples, so the terminal's real answer,
            # arriving moments later on stdin, could not simply take effect: the session sat with
            # the agent's dark panels floating in a white screen until enough repaints happened
            # to out-vote the guess — which is why it came right only once the user SCROLLED.
            #
            # Staying undecided is the whole point: the first sample after the answer lands is
            # then a FIRST decision, adopted outright, and the throttle above does not apply
            # while undecided, so it is sampled on the very next reactor tick.
            self._canvas = None
            return
        host_dark = _host_bg_is_dark(self)
        target = None if agent_dark == host_dark else (_CANVAS_DARK if agent_dark else _CANVAS_LIGHT)
        # Remember the scheme for the NEXT launch of this backend, so that session starts in
        # the right colours instead of inferring them again from scratch.
        getattr(self, "remember_agent_theme", lambda dark: None)(agent_dark)
        if target == self._canvas:
            self._canvas_votes = 0
            self._canvas_decided = True  # what is painted is confirmed; throttle from here
            return
        # WHILE IT IS STILL UNCERTAIN, SHOW THE TERMINAL'S OWN COLOURS. Dropping TO the terminal
        # (target None) is that state, and BEFORE anything is committed it is adopted at once —
        # it is the safe answer and costs nothing. Moving AWAY from it always needs repeated
        # agreement, at startup as much as later; and once a canvas IS committed, every change
        # needs agreement too, including dropping back to the terminal — otherwise one light
        # banner inside a dark session would repaint the whole background.
        #
        # Adopting the first opinionated frame outright is what produced the flash: a backend
        # asks what background it is drawing on, paints its DEFAULT (dark) while it waits, and
        # re-themes when the answer arrives. aGiTrack read those first dark frames as the
        # agent's scheme, painted the whole screen dark to match — and then followed the agent
        # back to light a moment later. The user saw a dark session turn bright a few seconds in,
        # which is precisely the flip this whole mechanism exists to prevent.
        #
        # Requiring agreement costs nothing now that an undecided canvas is re-sampled on every
        # reactor tick rather than once per CANVAS_SAMPLE_INTERVAL: a genuinely dark agent is
        # matched within a few ticks, and a backend still making its mind up never repaints the
        # screen on the strength of a frame it is about to contradict.
        if target is not None or self._canvas_decided:
            self._canvas_votes += 1
            if self._canvas_votes < self.CANVAS_VOTES_TO_SWITCH:
                return
        self._canvas = target
        self._canvas_votes = 0
        # "Decided" means COMMITTED to what is on screen, not merely "a frame had an opinion".
        # It gates the sample throttle, so setting it before adoption made the confirming sample
        # wait out a whole interval — turning the settling period into a visible delay, which is
        # the opposite of the point.
        self._canvas_decided = True
        # Chrome composed before this decision (the status bar) is a frame behind, so ask for
        # one more paint: the next frame is drawn entirely in the new scheme.
        self._render_pending = True
        getattr(self, "_debug", lambda message: None)(
            f"agent theme {'dark' if agent_dark else 'light'} vs {'dark' if host_dark else 'light'} "
            f"terminal → canvas {target[0] if target else 'off (terminal colours)'}"
        )

    def apply_remembered_theme(self: RendererHost, agent_dark: bool | None) -> None:
        """Start the session in the scheme this backend used LAST time (``None`` = unknown).

        Inference needs a frame with colour in it, and the backend takes a moment to paint
        one — so without this the first second of a session is drawn in the terminal's own
        colours and then visibly flips. The remembered value is only a head start: it is
        applied before the first frame and replaced without ceremony by the first frame that
        actually has an opinion, so a theme changed since the last run costs one sample, not
        a wrong screen for the whole session.

        A FORCED ``agent_background`` needs no guess at all: the canvas is already known from
        the setting, so it is applied here too — without it a forced session opened in the
        terminal's colours and only switched on the first frame `update_canvas` saw."""
        setting = getattr(self, "agent_background", "auto")
        if setting in {"dark", "light"}:
            self._canvas = _CANVAS_DARK if setting == "dark" else _CANVAS_LIGHT
            self._canvas_votes = 0
            self._canvas_decided = True  # settled by the setting; no frame can overrule it
            return
        if agent_dark is None or setting != "auto":
            return
        if not _host_bg_known(self):
            # Same rule as `update_canvas`: with no answer from the terminal there is nothing to
            # disagree WITH, so the session opens in the terminal's own colours. Applying a
            # remembered scheme against an assumed background would repaint the whole screen on
            # the strength of two guesses stacked on each other.
            self._canvas = None
            return
        self._canvas = None if agent_dark == _host_bg_is_dark(self) else (_CANVAS_DARK if agent_dark else _CANVAS_LIGHT)
        self._canvas_votes = 0
        self._canvas_decided = False  # a guess, not an observation — the first frame overrules it

    def canvas_sgr_body(self: RendererHost) -> str:
        """The SGR parameters that paint the canvas colours, or "" when there is no canvas."""
        canvas = getattr(self, "_canvas", None)
        if not canvas:
            return ""
        background, foreground = canvas
        codes = [
            code
            for code in (
                self.color_code(foreground, foreground=True),
                self.color_code(background, foreground=False),
            )
            if code
        ]
        return ";".join(codes)

    def reset_sgr(self: RendererHost) -> str:
        """The "back to normal" sequence to emit between painted regions. With a canvas
        active this is a reset PLUS the canvas colours, so aGiTrack's own chrome and every
        gap between painted cells sit on the agent's background instead of the terminal's."""
        body = self.canvas_sgr_body()
        return f"\x1b[0;{body}m" if body else "\x1b[0m"

    def cell_sgr(self: RendererHost, cell) -> str:
        """Reproduce exactly what OpenCode rendered into this cell, including
        the original colour encoding, so the cell is byte-equivalent to a
        native session on the same terminal.

        The one exception is the canvas: when the agent's colour scheme disagrees with the
        host terminal's, a cell left at the terminal default is painted in the agent's
        scheme instead — otherwise the agent's own dark panels would sit in a sea of the
        terminal's white (see "Agent-theme adaptation")."""
        codes = []
        if getattr(cell, "bold", False):
            codes.append("1")
        if getattr(cell, "dim", False):
            codes.append("2")  # faint — pyte drops it, so we track it on _DimChar (#113)
        if getattr(cell, "italics", False):
            codes.append("3")
        if getattr(cell, "underscore", False):
            codes.append("4")
        if getattr(cell, "blink", False):
            codes.append("5")
        if getattr(cell, "reverse", False):
            codes.append("7")
        if getattr(cell, "strikethrough", False):
            codes.append("9")
        canvas = getattr(self, "_canvas", None)
        cell_fg = getattr(cell, "fg", "default")
        cell_bg = getattr(cell, "bg", "default")
        if canvas:
            canvas_bg, canvas_fg = canvas
            if cell_fg == "default":
                cell_fg = canvas_fg
            if cell_bg == "default":
                cell_bg = canvas_bg
        fg = self.color_code(cell_fg, foreground=True)
        bg = self.color_code(cell_bg, foreground=False)
        if fg:
            codes.append(fg)
        if bg:
            codes.append(bg)
        return ";".join(codes)

    def color_code(self: RendererHost, color: str, *, foreground: bool) -> str | None:
        return _color_code(color, foreground, getattr(self, "color_mode", "truecolor"))

    def hex_color_code(self: RendererHost, color: str, *, foreground: bool) -> str:
        return _hex_color_code(color, foreground, getattr(self, "color_mode", "truecolor"))

    def render_line(self: RendererHost, cells, sel: tuple[int, int] | None = None, *, cols: int) -> str:
        rendered = []
        current = ""  # SGR body currently applied on the host terminal ("" == default)
        sel_start, sel_end = sel if sel else (-1, -1)
        # A column the backend never wrote to is the canvas: with an agent theme that
        # disagrees with the terminal it must carry the canvas colours, not the terminal's.
        empty = self.canvas_sgr_body()
        for col in range(cols):
            cell = cells.get(col)
            base = empty if cell is None else self.cell_sgr(cell)
            char = (cell.data or " ") if cell is not None else " "
            if sel is not None and sel_start <= col <= sel_end:
                style = (base + ";7") if base else "7"  # reverse-video the selection
            else:
                style = base
            if style != current:
                rendered.append("\x1b[" + (style or "0") + "m")
                current = style
            rendered.append(char)
        if current:
            rendered.append(self.reset_sgr())
        return "".join(rendered)

    # ------------------------------------------------------------------
    # Status line
    # ------------------------------------------------------------------

    def status_line(
        self: RendererHost,
        *,
        cols: int,
        name: str,
        backend_name: str,
        session_id: str | None,
        base_branch: str | None,
        worktree,
        scroll_back: int,
        user_declined: list,
        short_session_fn,
        menu_label: str = "Ctrl-G",
        summarizer_on: bool = True,
        cwd: str | None = None,
        current_dir_branch: str | None = None,
        manual_pending: int = 0,
    ) -> str:
        declined = len(user_declined)
        session = f"{name or 'session'}" + (f" [{short_session_fn(session_id)}]" if session_id else "")
        # When the session integrates into a different branch than the one checked
        # out in the repo directory, bold that branch so the difference is obvious.
        bold_base = base_branch is not None and current_dir_branch is not None and base_branch != current_dir_branch
        if base_branch and worktree is not None:
            session += f" → {base_branch}"  # the branch this session's work merges into
        sum_indicator = "sum:on" if summarizer_on else "sum:off"
        left = f" aGiTrack {menu_label} | {session} | {backend_name} | {sum_indicator} "
        if scroll_back > 0:
            right = f" SCROLLBACK -{scroll_back} (scroll down to resume) "
        else:
            # Manual-commit mode: how many agent turns are recorded but not yet in a commit.
            # Without this the count existed only in the exit dialog, so while working the user
            # had no way to tell whether the agent had done nothing or twenty turns' worth since
            # they last committed — in the one mode where committing is THEIR job.
            parts = []
            if manual_pending:
                turns = "turn" if manual_pending == 1 else "turns"
                parts.append(f"{manual_pending} uncommitted {turns}")
            if declined:
                parts.append(f"unstaged:{declined}")
            right = (" " + " | ".join(parts) + " ") if parts else ""
        if cwd:
            # The directory the agent works in (its session worktree, or the
            # repo in --no-worktree mode), home-abbreviated. When space runs
            # out the path is elided from the LEFT — the trailing components
            # are the part that identifies the directory.
            cwd_text = _home_relative(cwd)
            if worktree is None:
                # No isolated worktree: the agent edits this base directory directly. Note the
                # mode right after the path so it's clear there's no worktree isolation.
                cwd_text += " (no worktree)"
            room = cols - len(left) - len(right) - 3  # the "| " separator + trailing space
            if len(cwd_text) > room:
                cwd_text = "…" + cwd_text[-(room - 1) :] if room > 1 else ""
            if cwd_text:
                left += f"| {cwd_text} "
        padding = " " * max(cols - len(left) - len(right), 0)
        line = left + padding + right
        # The status bar must stay exactly one line no matter how narrow the terminal
        # is. When left+right are wider than the terminal there is no padding, so the
        # composed line would overflow and wrap to a second row — clamp it to the
        # terminal width, cutting the overflow off the end.
        if len(line) > cols:
            line = line[:cols]
        if bold_base and worktree is not None and f"→ {base_branch}" in line:
            # Add bold around the branch name AFTER the width math above — the escape
            # codes carry no visible width. \x1b[22m resets only the bold intensity,
            # leaving the line's reverse-video (\x1b[7m) intact. Guarded by an `in`
            # check so a narrow-terminal truncation that cut the branch name skips it.
            line = line.replace(f"→ {base_branch}", f"→ \x1b[1m{base_branch}\x1b[22m", 1)
        # Reverse video swaps whatever colours are in effect, so set the canvas first: on an
        # adapted screen the bar then inverts the AGENT's scheme rather than the terminal's,
        # which is what makes it read as part of the same UI. Read through getattr so this
        # method keeps composing a bar from its keyword args alone, with no host at all.
        reset = getattr(self, "reset_sgr", lambda: "\x1b[0m")()
        return f"{reset}\x1b[7m{line}\x1b[0m"

    # ------------------------------------------------------------------
    # Box / popup painting primitives
    # ------------------------------------------------------------------

    def append_box(
        self: RendererHost,
        parts: list[str],
        row: int,
        col: int,
        width: int,
        lines: list[str],
        highlight: str | None = None,
        *,
        rows: int,
        scrollable: bool = False,
        scroll: int = 0,
    ) -> None:
        inner = max(width - 2, 1)
        # A heavy, accent-coloured frame so the popup (and the Ctrl-G menu, which uses this
        # same box) reads as a distinct panel over the backend's screen rather than blending
        # in. The accent contrasts the terminal background; the interior text is left at the
        # default colour so it stays readable. The escapes carry no visible width, so the
        # box geometry is unchanged.
        accent = _box_accent_sgr(self)
        reset = self.reset_sgr()  # canvas-aware: the box interior sits on the agent's background
        edge = f"{accent}┃{reset}"
        border_top = f"{accent}┏{'━' * inner}┓{reset}"
        border_bottom = f"{accent}┗{'━' * inner}┛{reset}"
        box_lines = [border_top]
        # Each wrapped row is (text, pre_rendered): a row carrying **bold** markup is
        # wrapped + padded + SGR-rendered up front (its escapes have no visible width,
        # so it must skip the plain truncate/pad below); every other row is plain text.
        wrapped_lines: list[tuple[str, bool]] = []
        for line in lines:
            if _BOLD_MARKUP_RE.search(line):
                wrapped_lines.extend((rendered, True) for rendered in _wrap_markup(line, inner))
            else:
                wrapped_lines.extend((w, False) for w in (textwrap.wrap(line, width=inner) or [""]))
        max_body = max(rows - row - 2, 1)
        # When the caller owns scrolling (a long message), window the wrapped rows so the
        # whole thing is reachable instead of silently truncated, with ↑/↓ "more" hints.
        if scrollable and len(wrapped_lines) > max_body:
            body = max(1, max_body - 2)
            start = max(0, min(scroll, len(wrapped_lines) - body))
            head = [(f"↑ {start} more above", False)] if start > 0 else []
            below = len(wrapped_lines) - start - body
            tail = [(f"↓ {below} more below", False)] if below > 0 else []
            wrapped_lines = head + wrapped_lines[start : start + body] + tail
        for line, pre_rendered in wrapped_lines[:max_body]:
            if pre_rendered:
                box_lines.append(f"{edge}{line}{edge}")
                continue
            content = line[:inner].ljust(inner)
            if highlight and line == highlight:
                box_lines.append(f"{edge}\x1b[7m{content}\x1b[0m{edge}")
            else:
                box_lines.append(f"{edge}{content}{edge}")
        box_lines.append(border_bottom)
        for offset, line in enumerate(box_lines):
            if row + offset >= rows:
                break
            parts.append(f"\x1b[{row + offset};{col}H{reset}{line}")

    def append_command_palette(
        self: RendererHost,
        parts: list[str],
        *,
        rows: int,
        cols: int,
        input_text: str,
        input_matches: list[str],
        input_selected: str | None,
    ) -> None:
        width = min(max(52, cols // 2), cols - 4)
        row = 2
        col = max(2, (cols - width) // 2)
        header = [
            "aGiTrack commands",
            f"> {input_text}",
            "Up/Down selects. Tab completes. Enter runs. Esc cancels.",
            "",
        ]
        # Size the match window to whatever fits below the header: append_box caps
        # the body at rows-row-2, so emitting more than that would push the last
        # commands off the bottom of the box (the bug this replaces, where a fixed
        # 8-row slice hid "update"/"exit"). Count the header's *wrapped* height the
        # same way append_box does, since the instruction line wraps on a narrow box.
        inner = max(width - 2, 1)
        header_height = sum(len(textwrap.wrap(line, width=inner)) or 1 for line in header)
        visible = max(rows - row - 2 - header_height, 1)
        matches = input_matches
        if len(matches) > visible:
            # Scroll a window so the selected row is always on screen — otherwise a
            # selection past the window is both invisible and unhighlighted.
            idx = matches.index(input_selected) if input_selected in matches else 0
            start = min(max(idx - visible + 1, 0), len(matches) - visible)
            matches = matches[start : start + visible]
        lines = header + list(matches)
        self.append_box(parts, row, col, width, lines, highlight=input_selected, rows=rows)

    def append_message_popup(
        self: RendererHost,
        parts: list[str],
        message: str,
        *,
        rows: int,
        cols: int,
        scroll: int = 0,
    ) -> None:
        width = min(max(52, cols // 2), cols - 4)
        row = 2
        col = max(2, (cols - width) // 2)
        lines = message.splitlines() or [message]
        # Record how far this message can scroll so the input loop knows whether to treat
        # PgUp/PgDn as message scrolling (0 = it fits, no scrolling needed). Must match
        # append_box's windowing: it shows ``body = max_body - 2`` content rows (the other
        # two are reserved for the ↑/↓ hints), so the last scroll position is height - body.
        max_body = max(rows - row - 2, 1)
        body = max(1, max_body - 2)
        self._message_max_scroll = max(0, self.wrapped_message_height(lines, cols) - body)
        self.append_box(parts, row, col, width, lines, rows=rows, scrollable=True, scroll=scroll)

    def wrapped_message_height(self: RendererHost, lines: list[str], cols: int) -> int:
        """Total wrapped row count a message occupies at the popup width — used to decide
        whether it overflows the screen and therefore needs scrolling."""
        width = min(max(52, cols // 2), cols - 4)
        inner = max(width - 2, 1)
        total = 0
        for line in lines:
            if _BOLD_MARKUP_RE.search(line):
                total += len(_wrap_markup(line, inner))
            else:
                total += len(textwrap.wrap(line, width=inner) or [""])
        return total

    # ------------------------------------------------------------------
    # Full-frame render
    # ------------------------------------------------------------------

    def render(
        self: RendererHost,
        *,
        rows: int,
        cols: int,
        scroll_back: int,
        status_line_str: str,
        input_capturing: bool,
        input_text: str,
        input_matches: list[str],
        input_selected: str | None,
        message: str | None,
        message_sticky: bool,
        message_until: float,
        message_scroll: int = 0,
    ) -> None:
        if self.screen is None:
            return
        # Paint the whole screen inside one synchronized update (DECSET 2026) so
        # the host terminal applies the frame atomically and never shows it
        # half-drawn. Terminals that don't support 2026 ignore the markers and
        # fall back to the previous (unwrapped) full-repaint behaviour.
        selection = self.selection_ranges(cols)
        # Address every row absolutely instead of walking down with \r\n. If `rows` is
        # momentarily LARGER than the real terminal (a shrink not yet observed via
        # SIGWINCH), a trailing \r\n on the bottom row SCROLLS the alt screen, so the status
        # bar — written at the bottom — drifts up a row each frame and leaves a ghost copy
        # near the top (the "status bar at top AND bottom" glitch). Absolute moves clamp to
        # the terminal's last row instead: an over-large `rows` just overwrites the bottom
        # row and never scrolls, so a stale geometry can't smear the status bar up the screen.
        body = self.visible_lines(rows)
        # Decide (at most a few times a second) whether the agent's colour scheme disagrees
        # with the terminal's, before anything in this frame is painted — every reset below
        # then carries the canvas, so the whole frame is drawn in one consistent scheme.
        self.update_canvas(body)
        reset = self.reset_sgr()
        parts = [f"\x1b[?2026h{reset}\x1b[?25l\x1b[H"]
        for index, cells in enumerate(body):
            parts.append(f"\x1b[{index + 1};1H{reset}" + self.render_line(cells, selection.get(index), cols=cols))
        # The status bar owns the reserved row just below the body, addressed absolutely too.
        parts.append(f"\x1b[{len(body) + 1};1H" + status_line_str)
        if input_capturing:
            self.append_command_palette(
                parts,
                rows=rows,
                cols=cols,
                input_text=input_text,
                input_matches=input_matches,
                input_selected=input_selected,
            )
        elif message and (message_sticky or time.monotonic() < message_until):
            self.append_message_popup(parts, message, rows=rows, cols=cols, scroll=message_scroll)
        else:
            self._message_max_scroll = 0  # no message shown → nothing to scroll
        parts.append(self.cursor_sequence(rows, cols, scroll_back))
        parts.append("\x1b[?2026l")
        write_frame("".join(parts).encode())
