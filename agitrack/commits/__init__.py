"""Commit construction: message builders (with secret masking) and the
user/agent commit helpers."""

from agitrack.commits.actions import AgitrackActions
from agitrack.commits.manual import ManualCommitTracker, write_lf
from agitrack.commits.message import (
    IN_FLIGHT_MARKER,
    METADATA_HEADER,
    apply_summary_to_message,
    build_agent_commit_message,
    build_agent_merge_message,
    build_auto_fold_message,
    build_in_flight_trailer,
    build_manual_squash_trailer,
    build_pending_trailer,
    build_user_commit_message,
    carries_ai_history,
    is_fully_tracked_message,
    is_in_flight_only_message,
    render_interaction_trace,
    summary_metadata_lines,
)

__all__ = [
    "AgitrackActions",
    "ManualCommitTracker",
    "write_lf",
    "IN_FLIGHT_MARKER",
    "METADATA_HEADER",
    "apply_summary_to_message",
    "build_agent_commit_message",
    "build_agent_merge_message",
    "build_auto_fold_message",
    "build_in_flight_trailer",
    "build_manual_squash_trailer",
    "build_pending_trailer",
    "build_user_commit_message",
    "carries_ai_history",
    "is_fully_tracked_message",
    "is_in_flight_only_message",
    "render_interaction_trace",
    "summary_metadata_lines",
]
