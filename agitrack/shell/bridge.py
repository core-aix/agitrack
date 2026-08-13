"""Bidirectional JSON-RPC bridge over stdio (the VSCode-extension transport).

aGiTrack's interactive prompts (menus, confirmations, text input) normally read a
TTY. The VSCode extension runs aGiTrack as a long-lived child process with no
terminal, so those questions must be *asked of the editor* and answered by it.

This module carries that conversation as newline-delimited JSON:

  editor -> aGiTrack (stdin)
    {"type": "prompt",  "text": "..."}      run one agent turn
    {"type": "command", "text": ":status"}  run an aGiTrack ':' command
    {"type": "answer",  "id": "ask-3", "value": ...}  reply to an `ask`
    {"type": "exit"}                        shut the session down

  aGiTrack -> editor (stdout)
    {"type": "ready",   "session": ..., "backend": ..., "repo": ...}
    {"type": "response","text": ..., "session": ..., "model": ...}
    {"type": "commit",  "sha": ..., "session": ...}
    {"type": "no_changes"}
    {"type": "notice",  "level": "info|warn|error", "message": ...}
    {"type": "error",   "message": ...}
    {"type": "ask",     "id": "ask-3", "kind": "select|multiselect|input|confirm",
                        "message": ..., "options": [...], "detail": ...}
    {"type": "turn-complete"}               a prompt/command finished
    {"type": "bye"}                         session is exiting

The `ask`/`answer` pair is what makes menus and popups work without a terminal:
``BridgeUI`` emits an `ask` and blocks until the matching `answer` arrives.
"""

from __future__ import annotations

import itertools
import json
import queue
import sys
import threading
from typing import IO, Any


# Messages the editor sends that drive the main loop (everything except `answer`,
# which is consumed out-of-band by BridgeUI while a turn is in flight).
_REQUEST_TYPES = {"prompt", "command", "exit"}


class BridgeServer:
    """Owns the stdio transport: one reader thread, two queues, framed writes.

    Requests (prompt/command/exit) flow to the main loop via :meth:`next_request`.
    Answers flow to whichever :class:`BridgeUI` call is currently blocked on a
    question via :meth:`wait_answer`. Writes are newline-delimited JSON and are
    serialized by a lock so an `ask` emitted mid-turn never interleaves with a
    `response` line.
    """

    def __init__(self, out: IO[str] | None = None, inp: IO[str] | None = None) -> None:
        self._out: IO[str] = out if out is not None else sys.stdout
        self._in: IO[str] = inp if inp is not None else sys.stdin
        self._requests: queue.Queue[dict[str, Any]] = queue.Queue()
        self._answers: queue.Queue[dict[str, Any]] = queue.Queue()
        self._write_lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, name="agitrack-bridge-reader", daemon=True)
        self._started = False
        # Set once stdin is gone: no further request OR answer can ever arrive. wait_answer()
        # consults it so a question raised after the driver disconnected fails fast instead of
        # blocking forever.
        self._closed = threading.Event()

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._reader.start()

    def _read_loop(self) -> None:
        """Drain stdin forever, sorting each line into the right queue. When stdin
        closes (editor went away), synthesize an exit so the main loop unblocks."""
        try:
            for line in self._in:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(message, dict):
                    continue
                kind = message.get("type")
                if kind == "answer":
                    self._answers.put(message)
                elif kind in _REQUEST_TYPES:
                    self._requests.put(message)
                    if kind == "exit":
                        break
                else:
                    # SAY what was ignored. Unknown types were dropped in complete silence, so a
                    # developer who read "--ui-bridge speaks JSON-RPC" in `--help` and wrote a
                    # JSON-RPC 2.0 client ({"jsonrpc": "2.0", "method": …}) got nothing back at
                    # all and no way to tell a protocol mismatch from a hang. (The help no longer
                    # calls this JSON-RPC; see cli.py.)
                    self.emit(
                        {
                            "type": "notice",
                            "level": "warn",
                            "message": (
                                f"Ignored a message of unknown type {kind!r}. This protocol is "
                                "newline-delimited JSON, not JSON-RPC 2.0; the accepted types are "
                                + ", ".join(sorted(_REQUEST_TYPES | {"answer"}))
                                + "."
                            ),
                        }
                    )
        except (OSError, ValueError):
            pass
        # Closed stdin or a read error both mean "no more requests": tell the loop to stop.
        self._closed.set()
        self._requests.put({"type": "exit"})

    def emit(self, event: dict[str, Any]) -> None:
        with self._write_lock:
            try:
                self._out.write(json.dumps(event) + "\n")
                self._out.flush()
            except (OSError, ValueError):
                pass

    def next_request(self) -> dict[str, Any]:
        return self._requests.get()

    def wait_answer(self, ask_id: str) -> Any:
        """Block until the editor answers the question with ``ask_id``. Stale
        answers (a different id, e.g. a cancelled earlier question) are skipped.
        Returns the ``value`` field, or ``None`` if the editor signalled exit.

        Gives up the moment the transport is GONE. This was an unbounded
        ``self._answers.get()``, so an `ask` raised after stdin had already closed — a driver
        that piped one prompt and exited, a harness whose stdin was ``/dev/null`` — waited on an
        answer that could never arrive. The session deadlocked forever (killed after 7 minutes
        at ``{"type":"ask","id":"ask-1",…}``) while the reader thread had already queued its
        exit. ``None`` is the same answer a cancelled question gives, and every caller already
        handles it."""
        while True:
            try:
                message = self._answers.get(timeout=0.2)
            except queue.Empty:
                if self._closed.is_set():
                    return None  # nobody left to answer
                continue
            if message.get("type") == "answer" and message.get("id") == ask_id:
                return message.get("value")
            # A terminal control message can also arrive on the request queue; an
            # exit there is handled by the main loop. Here we just ignore mismatches.


class BridgeUI:
    """Asks the editor questions and blocks for the answer.

    Mirrors the small set of interactions aGiTrack's shell needs: a single-choice
    menu, a multi-select, a free-text box, a yes/no confirm, and fire-and-forget
    notices. Each `ask` carries a unique id so concurrent/stale answers can't be
    confused.
    """

    def __init__(self, server: BridgeServer) -> None:
        self._server = server
        self._ids = itertools.count(1)

    def _ask(self, kind: str, message: str, **extra: Any) -> Any:
        ask_id = f"ask-{next(self._ids)}"
        event: dict[str, Any] = {"type": "ask", "id": ask_id, "kind": kind, "message": message}
        for key, value in extra.items():
            if value is not None:
                event[key] = value
        self._server.emit(event)
        return self._server.wait_answer(ask_id)

    def select(self, message: str, options: list[str], *, detail: str | None = None) -> str | None:
        """Single-choice menu. Returns the chosen label, or None if dismissed."""
        value = self._ask("select", message, options=options, detail=detail)
        return value if isinstance(value, str) else None

    def multiselect(self, message: str, options: list[str], *, detail: str | None = None) -> list[str]:
        """Multi-choice menu. Returns the chosen labels (empty if none/dismissed)."""
        value = self._ask("multiselect", message, options=options, detail=detail)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
        return []

    def text(self, message: str, *, default: str = "") -> str | None:
        """Free-text box. Returns the entered string, or None if cancelled."""
        value = self._ask("input", message, default=default or None)
        return value if isinstance(value, str) else None

    def confirm(self, message: str) -> bool:
        """Yes/no confirmation. Returns True only on an explicit yes."""
        return self._ask("confirm", message) is True

    def info(self, message: str, *, level: str = "info") -> None:
        """Fire-and-forget notice (no answer expected)."""
        self._server.emit({"type": "notice", "level": level, "message": message})
