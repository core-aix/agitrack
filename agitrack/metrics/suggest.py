"""When the live dashboard would be empty but a reconstruction would not.

A repo only has live-dashboard history once aGiTrack has committed in it. Someone who has been
coding with Claude or OpenCode *without* aGiTrack therefore gets an empty dashboard — while their
past conversations are sitting right there in the backends' local transcripts, which is exactly
what ``--backtrace`` reconstructs. Showing them an empty page in that situation is the worst
answer available: it tells them there is nothing to see when there is.

So the rule is: whenever the live dashboard would be empty AND the backtrace view would not,
show the backtrace instead. Both probes here are deliberately cheap enough to run on every
dashboard start — one ``git log`` that stops at the first match, and a session *listing* that
never parses a transcript.
"""

from __future__ import annotations

from pathlib import Path

from agitrack.commits import METADATA_HEADER
from agitrack.git import GitRepo


def has_tracked_history(repo: GitRepo) -> bool:
    """Whether any commit in this repo carries aGiTrack metadata — i.e. whether the live
    dashboard would have anything to show.

    Uses ``git log --grep`` stopped at the first hit rather than building the dashboard: this
    runs before every dashboard start, and on a large repo a full walk would be felt. ``--all``
    so history tracked on another branch still counts; a false "empty" would send a user who
    already has tracked history to the reconstruction instead.
    """
    try:
        result = repo._run(
            ["git", "log", "--all", "--format=%H", "--fixed-strings", "--grep", METADATA_HEADER, "-n", "1"],
            check=False,
        )
    except Exception:
        return True  # never divert on a failed probe — the live dashboard is the default
    return bool(result.stdout.strip())


def has_backtrace_history(directory: Path) -> bool:
    """Whether local backend transcripts exist for *directory* — i.e. whether the backtrace
    view would have anything to reconstruct.

    Only *discovers* sessions (a directory listing per backend); it never exports or parses one,
    so this stays cheap even where the reconstruction itself would be slow.
    """
    try:
        from agitrack.metrics.backtrace import _discover

        return bool(_discover(directory))
    except Exception:
        return False  # a failed probe must never divert away from the live dashboard


def should_show_backtrace(repo: GitRepo, directory: Path | None = None) -> bool:
    """True when the live dashboard would be empty and the backtrace view would not."""
    return not has_tracked_history(repo) and has_backtrace_history(directory or repo.repo)


# Shown when the reconstruction is opened in place of an empty live dashboard, and as the
# startup notice. It has to do two jobs: say why this is not the dashboard they asked for, and
# say what they get by committing through aGiTrack — that the link from conversation to code
# stops being reconstructed and becomes recorded.
SUBSTITUTION_NOTICE = (
    "No aGiTrack-tracked commits in this repository yet, so the live dashboard would be empty — "
    "showing the BACKTRACE view instead, reconstructed from your local Claude/OpenCode sessions.\n"
    "It infers which past conversations changed which files. Commit through aGiTrack and that link "
    "is recorded rather than inferred: each commit carries the prompts, the agent's replies and the "
    "token counts that produced exactly those lines."
)

STARTUP_HINT = (
    "This repository has no aGiTrack-tracked commits yet, but local Claude/OpenCode sessions for it "
    "were found. `agitrack --backtrace` reconstructs that history — how past conversations changed "
    "these files — so you can see it before aGiTrack has tracked anything.\n"
    "Once you commit through aGiTrack, the history is recorded rather than reconstructed: prompts, "
    "replies and token counts tied to the exact lines they changed."
)
