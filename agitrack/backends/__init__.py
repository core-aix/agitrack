"""Agent backend adapters.

Two registries exist because a backend is driven two different ways, and they are NOT
interchangeable:

* :func:`agitrack.backends.proxy_agents.make_proxy_agent` builds the object that drives the
  backend's native TUI under the PTY proxy and reads its transcript.
* :func:`make_backend` (here) builds the HEADLESS adapter — the one with ``run()`` — used by
  shell mode, the summarizer, and the learning-page generator.

``make_backend`` exists to stop a pattern that had already been copied into four modules:
``OpenCodeBackend if state.backend == "opencode" else ClaudeBackend``. That ternary is not
merely repetitive, it is silently WRONG the moment a third backend exists — every unrecognized
name resolves to Claude, so a Codex session would have summarized itself by shelling out to
`claude`, with a Codex model id, and failed every summary. Adding a backend must be a
registration, not an edit to four ternaries that no test would catch.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agitrack.backends.base import AgentBackend


def _registry() -> dict[str, type]:
    # Imported lazily: the backend modules import from `agitrack.proc`, which pulls in the
    # platform layer, and importing them at package-import time makes `agitrack.backends`
    # unimportable in the minimal contexts (installer probes, --version) that only need names.
    from agitrack.backends.claude import ClaudeBackend
    from agitrack.backends.codex import CodexBackend
    from agitrack.backends.opencode import OpenCodeBackend

    return {
        ClaudeBackend.name: ClaudeBackend,
        CodexBackend.name: CodexBackend,
        OpenCodeBackend.name: OpenCodeBackend,
    }


def headless_backends() -> dict[str, type]:
    """Every headless backend adapter, keyed by name."""
    return _registry()


def backend_class(name: str | None) -> type:
    """The headless adapter class for ``name``.

    Falls back to Claude for an unknown/empty name — the historical behaviour of the ternaries
    this replaces, kept so a corrupt state file degrades to a working summarizer rather than
    crashing a commit. The difference is that every REGISTERED backend now resolves to itself.
    """
    from agitrack.backends.claude import ClaudeBackend

    return _registry().get(str(name or ""), ClaudeBackend)


def backend_name(name: str | None) -> str:
    """``name`` if it is a registered headless backend, else ``"claude"``.

    Callers need the resolved NAME as well as the class — the summarizer passes it to
    ``compatible_summarization_model`` — and the two must agree, or a Codex session would be
    handed a model id vetted for Claude.
    """
    return str(name) if str(name or "") in _registry() else "claude"


def make_backend(name: str | None, repo: Path, **kwargs) -> "AgentBackend":
    """The headless adapter for ``name``, constructed on ``repo``."""
    return backend_class(name)(repo, **kwargs)
