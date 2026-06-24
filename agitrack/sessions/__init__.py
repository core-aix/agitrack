"""Sharing full agent sessions between collaborators via git (issue #55).

A user can choose to publish one of *this* repo's Claude conversations so teammates
can resume it. The transcript is redacted, stored on a dedicated history-free ref
(`refs/agit/shared-sessions`) that keeps only the latest copy, and pushed to the
remote. Others see sessions as ``<github-id>/<name>`` and can resume them.

The package is backend-agnostic at the storage layer; only the transcript
import/export (in :mod:`agitrack.transcripts.claude`) is Claude-specific today.
"""

from agitrack.sessions.identity import github_login
from agitrack.sessions.redact import redact_transcript
from agitrack.sessions.store import (
    PublishResult,
    SharedEntry,
    SharedSessionStore,
    count_transcript_rows,
)

__all__ = [
    "PublishResult",
    "SharedEntry",
    "SharedSessionStore",
    "count_transcript_rows",
    "github_login",
    "redact_transcript",
]
