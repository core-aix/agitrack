"""The stretch in which aGiTrack was deliberately NOT tracking, and what it means for commits.

``agitrack stop`` is a user saying "stop recording me on this repository". Stopping the
processes is only half of that. The conversation usually carries straight on in the backend's
own UI, and its turns keep piling up in the very same transcript file aGiTrack reads. The
per-conversation watermark is PERSISTENT on purpose (it is what lets a crashed or updated
tracker pick up exactly where it left off), so the next ``agitrack -b`` exported everything
after it: every prompt and every reply from the stretch the user had switched recording OFF
for, folded verbatim into the next commit message. Worse for a conversation STARTED during
that stretch, which has no watermark at all and was exported whole. Once written, no later
command takes it back out of git history.

So the stop is RECORDED, and the next start stamps a floor: the moment tracking resumed.
A turn that BEGAN before the floor is never recorded by anyone, whatever a watermark says.

What the floor deliberately does NOT do:

* hide the file changes. Work done while aGiTrack was off is still sitting in the working
  tree and still needs committing; the next commit's diff carries it as it always did. What
  is withheld is the CONVERSATION: prompts, replies, token counts, model, capabilities, none
  of it reaches a commit message.
* reach backwards. Turns committed before the stop stay in history, and ``--backtrace`` (an
  explicit "reconstruct that conversation" request) still reads whatever it is pointed at.
  This governs AUTOMATIC tracking only.
* outlive the gap. The floor is a moment, not a mode: everything from the restart onwards is
  tracked exactly as before.

The record lives in the base repo's ``.agitrack/tracking-gap.json`` rather than in
``state.json`` because the process that writes it is not the one that reads it: ``agitrack
stop`` writes after killing the tracker, and the next tracker reads on startup. Its own small
file keeps those two out of each other's whole-document saves.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from agitrack import paths
from agitrack.fileio import atomic_write_text, read_json_object

_MARKER_NAME = "tracking-gap.json"

# An aGiTrack session worktree lives at ``<base>/.agitrack/worktrees/<name>``. The gap is a fact
# about the REPOSITORY, so a worktree run has to record it against the base repo: that is the
# directory `agitrack stop` was typed in, and the one the next tracker will read.
_WORKTREE_SEGMENTS = "/.agitrack/worktrees/"


def base_root(repo_root: Path) -> Path:
    """The repository the gap is recorded against: ``repo_root`` itself, or the base repo when
    it is one of our session worktrees."""
    if paths.contains_segments(str(repo_root), _WORKTREE_SEGMENTS):
        parents = repo_root.parents
        if len(parents) > 2:
            return parents[2]  # <base>/.agitrack/worktrees/<name> -> <base>
    return repo_root


def marker_path(repo_root: Path) -> Path:
    return base_root(repo_root) / ".agitrack" / _MARKER_NAME


def mark_stopped(repo_root: Path, *, now: float | None = None) -> float | None:
    """Record that the user has switched tracking off for this repo. Returns the epoch second
    the current gap began, or None when the record could not be written.

    Idempotent within one gap: a second stop (``agitrack stop`` after ``agitrack -b stop``, or
    a repeated command) keeps the moment the gap actually STARTED rather than moving it later.
    Any existing floor is preserved, so it keeps governing until the next start replaces it.
    """
    record = _read(repo_root)
    stopped_at = _stamp(record.get("stopped_at"))
    resumed_at = _stamp(record.get("resumed_at"))
    if stopped_at is not None and (resumed_at is None or resumed_at < stopped_at):
        return stopped_at  # already inside a gap; keep when it began
    moment = float(now if now is not None else time.time())
    record["stopped_at"] = moment
    return moment if _write(repo_root, record) else None


def resume_tracking(repo_root: Path, *, now: float | None = None) -> float | None:
    """Close an open gap and stamp the floor. Returns the floor when this call ENDED a gap
    (so the caller can say so), None when tracking was never stopped or has already resumed.

    Called by every mode that starts TRACKING a repo, and by none that merely reads one: a
    dashboard opened during a gap must not end it.
    """
    record = _read(repo_root)
    stopped_at = _stamp(record.get("stopped_at"))
    if stopped_at is None:
        return None
    resumed_at = _stamp(record.get("resumed_at"))
    if resumed_at is not None and resumed_at >= stopped_at:
        return None  # this stop has already been resumed from
    moment = float(now if now is not None else time.time())
    record["resumed_at"] = moment
    return moment if _write(repo_root, record) else None


def gap_is_open(repo_root: Path) -> bool:
    """Whether the user has stopped tracking here and not started it again.

    While this holds, NOTHING may start tracking on its own. `agitrack stop` already promises
    "auto-start is off for this repo until you run `agitrack -b` (or `agitrack -i`) again", and
    removing the hooks alone does not keep that promise: a backend's session-start hook is a file
    in the user's repo that a later daemon reinstalls, a plugin the backend may have cached, or a
    config the user restores from their dotfiles. Measured: with an OpenCode plugin armed, a
    tracker was back 1.6 seconds after `agitrack stop`, closed the stretch, and committed the very
    conversation the user had switched recording off for. The record is the authority the hooks
    are not.
    """
    record = _read(repo_root)
    stopped_at = _stamp(record.get("stopped_at"))
    if stopped_at is None:
        return False
    resumed_at = _stamp(record.get("resumed_at"))
    return resumed_at is None or resumed_at < stopped_at


def tracking_floor(repo_root: Path) -> float | None:
    """The moment before which no turn may be recorded, or None if tracking was never stopped.

    Normally the epoch second tracking last resumed. While a stretch is still OPEN it is
    ``inf``: nothing at all is recorded. That case is not hypothetical even with auto-start
    refused, because a stop can FAIL to reach a wedged daemon (`agitrack stop` says so, and exits
    1). The user has still said stop, so a tracker that outlives the command must not go on
    quoting the conversation into commit messages.
    """
    if gap_is_open(repo_root):
        return math.inf
    return _stamp(_read(repo_root).get("resumed_at"))


def split_untracked_turns(turns, floor: float | None) -> tuple[list, list]:
    """``(untracked, tracked)``: the leading turns that began before *floor*, and the rest.

    Turns are chronological, so the untracked ones are always a leading PREFIX; splitting at
    the first turn at or after the floor keeps the remainder contiguous, which is what every
    downstream consumer (the trace, the cover commit's parents) assumes.

    A turn is placed by when it BEGAN, not when it ended: one that started during the gap and
    was still running when tracking resumed was prompted while recording was off, so it is
    excluded whole rather than half-recorded. A turn carrying NO timestamps cannot be placed on
    either side and is kept, the same way ``turns_after`` errs toward exporting: real
    transcripts always stamp times, and dropping every turn on a backend that did not would
    silently stop committing altogether.
    """
    if not floor or not turns:
        return [], list(turns)
    if floor == math.inf:
        return list(turns), []  # the stretch is still open: none of this is ours
    # Whole seconds: transcript stamps have second resolution, so a turn prompted in the very
    # second tracking resumed counts as tracked rather than being lost to sub-second rounding.
    cutoff = int(floor)
    index = 0
    for turn in turns:
        began = getattr(turn, "started_at", None) or getattr(turn, "ended_at", None)
        if began is None or began >= cutoff:
            break
        index += 1
    return list(turns[:index]), list(turns[index:])


# ---------------------------------------------------------------------------
# Storage — best-effort throughout: a gap record that cannot be written or read must never
# take down a stop command or a tracker startup.
# ---------------------------------------------------------------------------


def _stamp(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def _read(repo_root: Path) -> dict:
    try:
        return read_json_object(marker_path(repo_root))
    except Exception:
        return {}


def _write(repo_root: Path, record: dict) -> bool:
    try:
        atomic_write_text(marker_path(repo_root), json.dumps(record, indent=2) + "\n")
        return True
    except Exception:
        return False
