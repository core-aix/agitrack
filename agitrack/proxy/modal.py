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
"""

from __future__ import annotations


def _escape_sequence_complete(sequence: bytes) -> bool:
    """Return True when *sequence* is a complete ANSI/VT escape sequence."""
    if sequence.startswith(b"\x1b[<"):
        return sequence[-1:] in {b"M", b"m"}
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

    def __init__(self, title: str, prompt: str, *, default: str = "") -> None:
        self.title = title
        self.prompt = prompt
        self.value = default
        self._escape_buffer: bytearray | None = None

    def render_message(self) -> str:
        """Return the message string that should be shown in the popup area."""
        return f"{self.title}\n{self.prompt}\n> {self.value}{self.CARET}"

    def feed(self, data: bytes) -> tuple[str, str | None]:
        """Process *data* bytes and return an action tuple.

        The caller should loop: render → read → feed → handle action.
        A lone Esc byte (``b"\\x1b"``) returned from ``_popup_read_input``
        is treated as an immediate cancel before the byte-level loop runs.
        """
        # Lone Esc read: immediate cancel (matches original _prompt_popup).
        if data == b"\x1b":
            return ("cancel", None)

        for byte in data:
            char = bytes([byte])

            # Inside an escape sequence: accumulate until complete, then drop.
            if self._escape_buffer is not None:
                self._escape_buffer.extend(char)
                if _escape_sequence_complete(bytes(self._escape_buffer)):
                    self._escape_buffer = None
                continue

            if char == b"\x03":
                return ("exit", None)

            if char == b"\x1b":
                self._escape_buffer = bytearray(char)
                continue

            if char in {b"\r", b"\n"}:
                return ("done", self.value)

            if char in {b"\x7f", b"\b"}:
                self.value = self.value[:-1]
            elif byte >= 32:
                self.value += char.decode(errors="ignore")

        return ("redraw", None)


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
        for index, option in enumerate(self.options):
            if self._is_separator(option):
                lines.append("")  # a blank gap between groups
                continue
            prefix = "> " if index == self.selected else "  "
            lines.append(prefix + option)
        return "\n".join(lines)

    def feed(self, data: bytes) -> tuple[str, str | None]:
        """Process *data* bytes and return an action tuple."""
        # Lone Esc read: immediate cancel.
        if data == b"\x1b":
            return ("cancel", None)

        for byte in data:
            char = bytes([byte])

            if self._escape_buffer is not None:
                self._escape_buffer.extend(char)
                sequence = bytes(self._escape_buffer)
                if sequence == b"\x1b[A":
                    self._advance(-1)
                    self._escape_buffer = None
                elif sequence == b"\x1b[B":
                    self._advance(1)
                    self._escape_buffer = None
                elif sequence == b"\x1b[5~":  # PageUp — scroll the detail list up
                    self.detail_scroll = max(0, self.detail_scroll - max(1, self._detail_window() - 1))
                    self._escape_buffer = None
                elif sequence == b"\x1b[6~":  # PageDown — scroll the detail list down
                    self.detail_scroll += max(1, self._detail_window() - 1)
                    self._escape_buffer = None
                elif _escape_sequence_complete(sequence):
                    self._escape_buffer = None
                continue

            if char == b"\x03":
                return ("exit", None)

            if char == b"\x1b":
                self._escape_buffer = bytearray(char)
                continue

            if char in {b"\r", b"\n"}:
                return ("done", self.options[self.selected])

        return ("redraw", None)
