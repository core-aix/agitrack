"""Public surface of the ``agitrack.proxy`` package.

The proxy subsystem is spread across several modules (runner, renderer,
terminal, session, integration, commit_engine, process); this package
re-exports the names that callers need so that ``from agitrack.proxy import Foo``
always works regardless of which module ``Foo`` lives in.

Internal helpers prefixed with ``_`` are re-exported for test compatibility
but are not part of the stable API.
"""

from agitrack.proxy.commit_engine import CommitEngine
from agitrack.proxy.integration import IntegrationService, MergeContext, MergePhase
from agitrack.proxy.process import BackendProcess
from agitrack.proxy.renderer import ScreenRenderer, _BackgroundColorEraseScreen, detect_color_mode
from agitrack.proxy.session import Session
from agitrack.proxy.terminal import TerminalHost
from agitrack.proxy.runner import (
    ProxyInput,
    ProxyRunner,
    _escape_sequence_complete,
    _short_session,
)

__all__ = [
    # Public production API
    "CommitEngine",
    "BackendProcess",
    "IntegrationService",
    "MergeContext",
    "MergePhase",
    "ProxyInput",
    "ProxyRunner",
    "ScreenRenderer",
    "Session",
    "TerminalHost",
    "detect_color_mode",
    # Internal helpers (re-exported for test access)
    "_BackgroundColorEraseScreen",
    "_escape_sequence_complete",
    "_short_session",
]
