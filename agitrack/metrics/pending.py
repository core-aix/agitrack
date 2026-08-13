"""What the backtrace can see that the live dashboard cannot.

The two dashboards are deliberately separate: one shows what aGiTrack RECORDED, the other what can
be RECONSTRUCTED from the agent's own transcripts, and the reconstruction is an inference, so
merging them would quietly downgrade the recorded history to the accuracy of the inferred one.

But separate is not the same as unaware. A repository where the agent has been doing work outside
aGiTrack has conversations in the backtrace that the live dashboard genuinely does not know about,
and the live dashboard saying nothing about that is the same failure as an empty page: it reports
"this is everything" when it is not. So it says so, and points at the thing that fixes it —
committing through aGiTrack, which turns those inferred links into recorded ones.

The probe has to be cheap enough to run on a dashboard poll, so it works at SESSION granularity by
default: one directory listing per backend plus one ``git log``, no transcript parsing at all. When
the reconstruction happens to be built already (the hub keeps it once anyone has looked at the
backtrace view), the exact turn count is used instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class PendingWork:
    """Agent work visible in the backtrace but not in aGiTrack's tracked commits."""

    sessions: int = 0
    """Whole sessions with no tracked commit at all."""

    turns: int = 0
    """Reconstructed turns not covered by a tracked commit. Only meaningful when ``exact``."""

    exact: bool = False
    """Whether ``turns`` was counted from a built reconstruction rather than estimated."""

    def __bool__(self) -> bool:
        return bool(self.sessions or self.turns)

    def to_dict(self) -> dict:
        return {"sessions": self.sessions, "turns": self.turns, "exact": self.exact}


def pending_work(directory: Path, view: object | None = None) -> PendingWork:
    """What the backtrace holds for ``directory`` that the tracked history does not.

    ``view`` is an already-built :class:`~agitrack.metrics.backtrace.BacktraceView`, when one
    happens to exist. Given one, the answer is exact; without one it is the cheap session-level
    estimate, because building a reconstruction to decorate a dashboard nobody asked to decorate
    would spend minutes of the user's CPU on a notice.
    """
    if view is not None:
        return _from_view(view)
    return _estimate(directory)


def _from_view(view) -> PendingWork:
    stats = getattr(getattr(view, "dashboard", None), "stats", None) or []
    untracked = [stat for stat in stats if not getattr(stat, "tracked", False)]
    if not untracked:
        return PendingWork()
    # Count the SESSIONS those turns belong to as well, so the message can say "3 turns across 2
    # conversations" rather than a bare number with no shape to it.
    sessions = {getattr(stat, "backend_session_id", "") for stat in untracked}
    sessions.discard("")
    return PendingWork(sessions=len(sessions), turns=len(untracked), exact=True)


def _estimate(directory: Path) -> PendingWork:
    """Sessions in this directory with no aGiTrack-tracked commit behind them.

    Deliberately conservative: a session with even one tracked commit is not counted, so the number
    never overstates what is missing. Any failure reports nothing at all — a notice that appears
    because a probe broke is worse than no notice."""
    try:
        from agitrack.metrics.backtrace import _committed_anchors, _discover

        sources = _discover(directory)
        if not sources:
            return PendingWork()
        anchors = _committed_anchors(directory)
        missing = [source for source in sources if source.ref_id not in anchors]
        return PendingWork(sessions=len(missing))
    except Exception:
        return PendingWork()


def empty_state(directory: Path, *, commits: int, tracked: bool) -> dict:
    """What the live dashboard should say when it has no agent work to show.

    An empty page is never an answer. There are three different reasons a dashboard can be empty
    and they call for three different things:

    * agent sessions exist here but nothing has been tracked — handled by :func:`notice_text`,
      which points at the reconstruction that CAN show them, so nothing is said here;
    * no agent sessions at all, but the repository has ordinary commits — say that there is no
      agent history to trace, and let the commits below stand as the repository's real content;
    * no agent sessions and no commits either — say plainly that there is nothing to show, rather
      than rendering a coverage bar over a void.
    """
    if tracked:
        return {}
    if _has_sessions(directory):
        return {}  # the "commit this work" notice is the right message here, not an empty state
    if commits:
        return {
            "kind": "no-sessions",
            "text": (
                "No traceable coding-agent sessions were found for this repository: there are no "
                "aGiTrack-tracked commits, and no local agent transcripts to reconstruct either. "
                "The commits below are the repository's own history, shown without any agent "
                "attribution. Run your agent through aGiTrack and its work starts appearing here."
            ),
        }
    return {
        "kind": "nothing",
        "text": (
            "There is nothing to show for this repository yet: no commits, and no coding-agent "
            "sessions to trace. Once you commit, or run an agent here through aGiTrack, this page "
            "fills in on its own."
        ),
    }


def _has_sessions(directory: Path) -> bool:
    try:
        from agitrack.metrics.backtrace import _discover

        return bool(_discover(directory))
    except Exception:
        # A failed probe must never claim there is no history: saying "nothing here" wrongly is
        # the exact failure this whole module exists to prevent.
        return True


def notice_text(work: PendingWork) -> str:
    """The sentence the live dashboard shows, or ``""`` when there is nothing to say."""
    if not work:
        return ""
    if work.exact and work.turns:
        turns = f"{work.turns} agent turn{'s' if work.turns != 1 else ''}"
        where = f" across {work.sessions} conversation{'s' if work.sessions != 1 else ''}" if work.sessions else ""
        what = f"{turns}{where}"
    else:
        what = f"{work.sessions} past agent session{'s' if work.sessions != 1 else ''}"
    return (
        f"The backtrace view shows {what} in this directory that no aGiTrack commit covers, so none "
        "of it appears below. Commit that work through aGiTrack and the link from conversation to "
        "code stops being reconstructed and becomes recorded, with the prompts, replies and token "
        "counts tied to the exact lines they changed."
    )
