"""Modal state machines for prompt and select popups (P6 Stage 2).

``PromptModal`` and ``SelectModal`` each encode the byte-handling logic that
previously lived inline inside ``_prompt_popup`` / ``_select_popup`` in the
runner.  They are pure state machines: they accept bytes through ``feed()``
and return an action tuple — the caller (``ProxyRunner._run_modal``) is
responsible for I/O and for invoking the exit flow when requested.

Action tuples returned by ``feed()``:

    ("done",   value)   — the user confirmed; ``value`` is the result string
    ("cancel", None)    — the user cancelled (Esc or bare Esc-only read)
    ("exit",   None)    — the user pressed Ctrl-C; caller should call
                          ``_run_exit_flow()``.  If the flow returns False
                          (exit declined), re-feed subsequent bytes normally.
    ("redraw", None)    — state changed; caller should re-render and continue

``_escape_sequence_complete`` lives HERE as the single source of truth;
runner.py imports it from this module (modal.py must not import runner —
runner imports the modal classes, so the dependency points this way).

Bracketed paste
---------------
A terminal with DECSET 2004 on (every backend enables it for its own input box)
wraps pasted text in ``CSI 200~`` … ``CSI 201~``. The newlines inside are pasted
CONTENT, not keypresses — but a modal that reads them as Enter answers itself, so
pasting a multi-line prompt at the agent while a popup happened to be open silently
picked the popup's default. Both modals therefore track the markers and never let
pasted bytes act as keys:

* ``SelectModal`` — a paste can never be a menu choice, so the raw bytes are
  collected in ``pasted`` for the caller to replay to the backend (the paste was
  meant for the agent), and nothing is selected, confirmed or cancelled.
* ``PromptModal`` — the user IS being asked to type, so pasted text lands in the
  field (newlines become spaces, since the field is one line) but never submits it.

``in_paste`` can be seeded True at construction: a paste already in flight when the
popup opens delivers its opening marker to the backend, so the modal only ever sees
the tail. The runner tracks that state at its single stdin read point.
"""

from __future__ import annotations

# CSI 200~ / CSI 201~ — the terminal's bracketed-paste delimiters.
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"


def _escape_sequence_complete(sequence: bytes) -> bool:
    """Return True when *sequence* is a complete ANSI/VT escape sequence — including an
    ABORTED one that will never legitimately complete.

    An SGR mouse report (``\\x1b[<Cb;Cx;Cy`` + ``M``/``m``) is otherwise open-ended: the
    caller keeps extending *sequence* byte by byte until this returns True, with no bound
    other than the M/m terminator. A report that gets corrupted or cut off in flight (a
    dropped byte, a multiplexer splitting it oddly) then never terminates — the accumulator
    swallows every keystroke typed afterwards, waiting for an M/m that will never arrive on
    its own, until an *unrelated* 'm' the user happens to type (e.g. the one in "model")
    satisfies the check and gets treated as the sequence's own terminator. The result was a
    stray ``[<35;124;48`` fragment landing in a commit's interaction trace as if someone had
    typed it, with the keystrokes in between silently eaten.

    The body of a real SGR report is only ASCII digits and ``;``, so any other byte proves
    the sequence has gone off the rails — treat the sequence as complete (aborted) right
    there instead of waiting forever. The offending byte is sacrificed along with it; that
    is the cost of a corrupted report, and far better than eating everything after it."""
    if sequence.startswith(b"\x1b[<"):
        body = sequence[3:]
        if not body:
            return False
        last = body[-1:]
        return last in {b"M", b"m"} or not (last.isdigit() or last == b";")
    if sequence.startswith(b"\x1b[M"):
        return len(sequence) >= 6
    if sequence.startswith(b"\x1b["):
        return len(sequence) >= 3 and 0x40 <= sequence[-1] <= 0x7E
    return len(sequence) >= 2


class PromptModal:
    """Free-text input modal (like a mini readline inside a popup).

    State:
        title   — displayed as the popup heading
        prompt  — the question / label shown above the input line
        value   — the text typed so far (starts from *default*)

    Byte handling:
        Esc (lone)          → cancel
        Ctrl-C (\\x03)      → exit request
        Enter/\\r/\\n       → confirm with current value
        Backspace/\\x7f/\\b → delete last character
        Printable (>=32)    → append to value
        Escape sequences    → consumed silently (arrows etc. are ignored)
        Tab                 → ignored (not meaningful in a free-text field)
    """

    # Block glyph drawn at the end of the input line. The popup is static text
    # painted over the backend screen, and the real terminal cursor is hidden
    # while it is up (it belongs to the backend behind the popup), so the field
    # draws its own caret — otherwise the input line looks like a read-only label.
    CARET = "█"

    def __init__(
        self,
        title: str,
        prompt: str,
        *,
        default: str = "",
        detail: list[str] | None = None,
        viewport_rows: int | None = None,
        in_paste: bool = False,
    ) -> None:
        self.title = title
        self.prompt = prompt
        self.value = default
        # Optional context lines shown between the title and the input (e.g. the files being
        # committed). Windowed and PgUp/PgDn-scrollable when they overflow the terminal.
        self.detail = list(detail or [])
        self.viewport_rows = viewport_rows
        self.detail_scroll = 0
        self._escape_buffer: bytearray | None = None
        # Where the next character lands, as an index into `value`. Left/Right (and
        # Home/End) move it, so a typo near the start is fixed in place instead of
        # retyping the rest of the line. Starts at the end of any default.
        self.cursor = len(self.value)
        # Bracketed-paste state (see the module docstring). Pasted text is typed into the
        # field, never submitted; `pasted` stays empty here because the content is consumed.
        self.in_paste = in_paste
        self.pasted = bytearray()

    def _detail_window(self) -> int:
        """How many detail lines fit at once, leaving room for the title, prompt, input line,
        and scroll hints. Without a known terminal height, show them all (the box clamps)."""
        if not self.detail:
            return 0
        if not self.viewport_rows:
            return len(self.detail)
        title_lines = self.title.count("\n") + 1
        prompt_lines = self.prompt.count("\n") + 1
        overhead = title_lines + prompt_lines + 4  # input line, hint line, 2 scroll hints
        return max(3, self.viewport_rows - 4 - overhead)

    def _insert(self, text: str) -> None:
        """Insert *text* at the cursor and leave the cursor after it."""
        at = max(0, min(self.cursor, len(self.value)))
        self.value = self.value[:at] + text + self.value[at:]
        self.cursor = at + len(text)

    def render_message(self) -> str:
        """Return the message string that should be shown in the popup area."""
        lines = [self.title]
        window = self._detail_window()
        if self.detail:
            total = len(self.detail)
            start = max(0, min(self.detail_scroll, max(0, total - window)))
            self.detail_scroll = start  # clamp persisted so PgDn past the end is a no-op
            if start > 0:
                lines.append(f"  ↑ {start} more above")
            lines.extend("  " + line for line in self.detail[start : start + window])
            below = total - (start + window)
            if below > 0:
                lines.append(f"  ↓ {below} more below")
            if total > window:
                lines.append("(PgUp/PgDn scroll the file list)")
        lines.append(self.prompt)
        # The caret marks the insertion point: the character it sits on is drawn after it,
        # so text to the right of the cursor stays visible while editing mid-line.
        at = max(0, min(self.cursor, len(self.value)))
        lines.append(f"> {self.value[:at]}{self.CARET}{self.value[at:]}")
        return "\n".join(lines)

    def feed(self, data: bytes) -> tuple[str, str | None]:
        """Process *data* bytes and return an action tuple.

        The caller should loop: render → read → feed → handle action.
        A lone Esc byte (``b"\\x1b"``) returned from ``_popup_read_input``
        is treated as an immediate cancel before the byte-level loop runs.
        """
        # Lone Esc read: immediate cancel (matches original _prompt_popup).
        if data == b"\x1b":
            return ("cancel", None)

        # Only redraw when something actually changed. Mouse-motion reports (and other dropped
        # sequences) arrive as escape sequences here; without this a moving mouse would return
        # "redraw" for every report and repaint the popup dozens of times a second (title flash +
        # slowdown). Such no-op input returns "noop" so the caller keeps reading without repainting.
        changed = False
        for byte in data:
            char = bytes([byte])

            # Inside an escape sequence: accumulate until complete. PgUp/PgDn scroll the
            # detail list; any other complete sequence (arrows, mouse reports, etc.) is dropped.
            if self._escape_buffer is not None:
                self._escape_buffer.extend(char)
                sequence = bytes(self._escape_buffer)
                if sequence == PASTE_START:  # pasted text starts: it is content, not keys
                    self.in_paste = True
                    self._escape_buffer = None
                elif sequence == PASTE_END:
                    self.in_paste = False
                    self._escape_buffer = None
                elif sequence == b"\x1b[5~":  # PageUp — scroll the detail list up
                    self.detail_scroll = max(0, self.detail_scroll - max(1, self._detail_window() - 1))
                    self._escape_buffer = None
                    changed = True
                elif sequence == b"\x1b[6~":  # PageDown — scroll the detail list down
                    self.detail_scroll += max(1, self._detail_window() - 1)
                    self._escape_buffer = None
                    changed = True
                elif sequence == b"\x1b[D":  # Left — move the insertion point
                    self.cursor = max(0, self.cursor - 1)
                    self._escape_buffer = None
                    changed = True
                elif sequence == b"\x1b[C":  # Right
                    self.cursor = min(len(self.value), self.cursor + 1)
                    self._escape_buffer = None
                    changed = True
                elif sequence in (b"\x1b[H", b"\x1b[1~", b"\x1bOH"):  # Home
                    self.cursor = 0
                    self._escape_buffer = None
                    changed = True
                elif sequence in (b"\x1b[F", b"\x1b[4~", b"\x1bOF"):  # End
                    self.cursor = len(self.value)
                    self._escape_buffer = None
                    changed = True
                elif sequence == b"\x1b[3~":  # Delete — remove the character to the RIGHT
                    if self.cursor < len(self.value):
                        self.value = self.value[: self.cursor] + self.value[self.cursor + 1 :]
                        changed = True
                    self._escape_buffer = None
                elif _escape_sequence_complete(sequence):
                    self._escape_buffer = None
                continue

            if char == b"\x1b":
                self._escape_buffer = bytearray(char)
                continue

            # Inside a paste every byte is text the user copied, so it is typed into the
            # field and NOTHING acts as a key: a pasted newline must not submit the answer
            # (it becomes a space — the field is a single line) and a pasted \x03 must not
            # start the exit flow.
            if self.in_paste:
                if char in {b"\r", b"\n"}:
                    if not self.value[: self.cursor].endswith(" "):
                        self._insert(" ")
                        changed = True
                elif byte >= 32:
                    self._insert(char.decode(errors="ignore"))
                    changed = True
                continue

            if char == b"\x03":
                return ("exit", None)

            if char in {b"\r", b"\n"}:
                return ("done", self.value)

            if char in {b"\x7f", b"\b"}:
                if self.cursor > 0:  # backspace deletes to the LEFT of the cursor
                    self.value = self.value[: self.cursor - 1] + self.value[self.cursor :]
                    self.cursor -= 1
                    changed = True
            elif char == b"\x01":  # Ctrl-A — start of line (readline habit)
                self.cursor = 0
                changed = True
            elif char == b"\x05":  # Ctrl-E — end of line
                self.cursor = len(self.value)
                changed = True
            elif byte >= 32:
                self._insert(char.decode(errors="ignore"))
                changed = True

        return ("redraw", None) if changed else ("noop", None)


class SelectModal:
    """Up/Down selection modal (like a menu inside a popup).

    State:
        title    — displayed as the popup heading
        options  — the list of selectable strings
        selected — index of the currently-highlighted option
        detail   — optional extra lines shown between the title and the options
                   (e.g. a file list). When they don't all fit, a window of them is
                   shown and PgUp/PgDn scroll it.

    Byte handling:
        Esc (lone)      → cancel
        Ctrl-C          → exit request
        Arrow-Up        → move selection up (wraps)
        Arrow-Down      → move selection down (wraps)
        PgUp / PgDn     → scroll the detail list (when it overflows)
        Enter/\\r/\\n   → confirm with ``options[selected]``
        Other escapes   → consumed silently
    """

    def __init__(
        self,
        title: str,
        options: list[str],
        *,
        detail: list[str] | None = None,
        viewport_rows: int | None = None,
        in_paste: bool = False,
    ) -> None:
        self.title = title
        self.options = options
        self.detail = list(detail or [])
        self.viewport_rows = viewport_rows
        self.detail_scroll = 0
        # A blank/whitespace-only option is a separator: rendered as a gap and
        # skipped during navigation (never highlighted, never returned). Start the
        # selection on the first real option.
        self.selected = 0
        if self.options and self._is_separator(self.options[self.selected]):
            self._advance(1)
        self._escape_buffer: bytearray | None = None
        # Bracketed-paste state (see the module docstring). A paste is never a menu choice,
        # so its raw bytes are collected here for the caller to replay to the backend.
        self.in_paste = in_paste
        self.pasted = bytearray()

    @staticmethod
    def _is_separator(option: str) -> bool:
        return option.strip() == ""

    def _advance(self, delta: int) -> None:
        """Move the selection by *delta*, wrapping and skipping separator rows."""
        count = len(self.options)
        index = self.selected
        for _ in range(count):
            index = (index + delta) % count
            if not self._is_separator(self.options[index]):
                self.selected = index
                return

    def _detail_window(self) -> int:
        """How many detail lines fit at once. Without a known terminal height, show them
        all (the box still clamps); otherwise leave room for the title, the scroll hints,
        the instruction line, and the options."""
        if not self.detail:
            return 0
        if not self.viewport_rows:
            return len(self.detail)
        title_lines = self.title.count("\n") + 1
        overhead = title_lines + len(self.options) + 5  # instructions, gaps, 2 scroll hints
        return max(3, self.viewport_rows - 4 - overhead)

    def _option_window(self, used_rows: int) -> tuple[int, int]:
        """The half-open ``[start, end)`` slice of options to show so the SELECTED row stays
        visible when the list is taller than the terminal. ``used_rows`` is everything else
        the popup already spends (title, detail, instruction, blank). Without a known
        terminal height, show every option (the box still clamps)."""
        count = len(self.options)
        if not self.viewport_rows:
            return 0, count
        budget = max(3, self.viewport_rows - 4 - used_rows - 2)  # 2 rows reserved for hints
        if count <= budget:
            return 0, count
        start = min(max(self.selected - budget // 2, 0), count - budget)
        return start, start + budget

    def render_message(self) -> str:
        """Return the message string that should be shown in the popup area."""
        lines = [self.title]
        window = self._detail_window()
        if self.detail:
            total = len(self.detail)
            start = max(0, min(self.detail_scroll, max(0, total - window)))
            self.detail_scroll = start  # clamp persisted so PgDn past the end is a no-op
            if start > 0:
                lines.append(f"  ↑ {start} more above")
            lines.extend("  " + line for line in self.detail[start : start + window])
            below = total - (start + window)
            if below > 0:
                lines.append(f"  ↓ {below} more below")
        scrollable = bool(self.detail) and len(self.detail) > window
        lines.append(
            "Up/Down selects. PgUp/PgDn scroll. Enter confirms." if scrollable else "Up/Down selects. Enter confirms."
        )
        lines.append("")
        # Window the options so a list taller than the terminal scrolls with the selection
        # (otherwise the box would truncate it and hide the highlighted row). Rows used so
        # far = the title's own height plus every line after it (detail, instruction, blank).
        title_rows = self.title.count("\n") + 1
        start, end = self._option_window(title_rows + (len(lines) - 1))
        if start > 0:
            lines.append(f"  ↑ {start} more above")
        for index in range(start, end):
            option = self.options[index]
            if self._is_separator(option):
                lines.append("")  # a blank gap between groups
                continue
            prefix = "> " if index == self.selected else "  "
            lines.append(prefix + option)
        if end < len(self.options):
            lines.append(f"  ↓ {len(self.options) - end} more below")
        return "\n".join(lines)

    def feed(self, data: bytes) -> tuple[str, str | None]:
        """Process *data* bytes and return an action tuple."""
        # Lone Esc read: immediate cancel.
        if data == b"\x1b":
            return ("cancel", None)

        # See PromptModal.feed: return "redraw" only on a real change so a moving mouse (whose
        # motion reports arrive here as dropped escape sequences) doesn't repaint the menu on
        # every report — which flashed the title and slowed everything down.
        changed = False
        for byte in data:
            char = bytes([byte])

            if self._escape_buffer is not None:
                self._escape_buffer.extend(char)
                sequence = bytes(self._escape_buffer)
                if sequence == PASTE_START:  # pasted text starts: it is content, not keys
                    self.in_paste = True
                    self.pasted.extend(sequence)
                    self._escape_buffer = None
                elif sequence == PASTE_END:
                    self.in_paste = False
                    self.pasted.extend(sequence)
                    self._escape_buffer = None
                elif self.in_paste:
                    # An escape sequence inside the paste is pasted content too.
                    if _escape_sequence_complete(sequence):
                        self.pasted.extend(sequence)
                        self._escape_buffer = None
                elif sequence == b"\x1b[A":
                    self._advance(-1)
                    self._escape_buffer = None
                    changed = True
                elif sequence == b"\x1b[B":
                    self._advance(1)
                    self._escape_buffer = None
                    changed = True
                elif sequence == b"\x1b[5~":  # PageUp — scroll the detail list up
                    self.detail_scroll = max(0, self.detail_scroll - max(1, self._detail_window() - 1))
                    self._escape_buffer = None
                    changed = True
                elif sequence == b"\x1b[6~":  # PageDown — scroll the detail list down
                    self.detail_scroll += max(1, self._detail_window() - 1)
                    self._escape_buffer = None
                    changed = True
                elif _escape_sequence_complete(sequence):
                    self._escape_buffer = None
                continue

            if char == b"\x1b":
                self._escape_buffer = bytearray(char)
                continue

            # Inside a paste nothing acts as a key — a pasted newline must not answer the
            # menu, and a pasted \x03 must not start the exit flow. The bytes are kept so
            # the caller can hand the paste to the backend, which is where it was headed.
            if self.in_paste:
                self.pasted.extend(char)
                continue

            if char == b"\x03":
                return ("exit", None)

            if char in {b"\r", b"\n"}:
                return ("done", self.options[self.selected])

        return ("redraw", None) if changed else ("noop", None)
