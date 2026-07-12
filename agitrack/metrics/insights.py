"""Agent-usage efficiency insights for the dashboard.

The commit metadata aGiTrack records (per-turn tokens, prompts, session ids, timestamps,
durations) plus the per-file change history is enough to spot HOW the agent is being used
inefficiently — not how much code it wrote. Each category extracts one behavioural signal,
backs it with concrete numbers, and pairs it with a specific suggestion.

Three properties make the panel useful rather than decorative:

* **Scoped to the view.** Insights are computed from the SAME filtered commits the rest of
  the dashboard shows, so narrowing to a time range (or a backend/model/committer) re-asks
  the question for that slice. Judging the whole history forever would make an improvement
  invisible — the bad old turns never leave the denominator.

* **Trend within the window.** Every category reduces to one scalar where *lower is better*.
  That scalar is recomputed on the earlier and later halves of the window, so each card
  reports whether the habit is improving, worsening, or steady — and a habit that was a
  problem early and is gone late is surfaced as a "resolved" win rather than vanishing
  silently.

* **Only what's evidenced.** A category stays silent unless its own threshold is met, so a
  young repo (or one with healthy habits) sees nothing rather than filler. Which cards
  appear therefore changes with the slice and as habits change.

Everything is a single pass over data the dashboard already holds (the commit stats and the
file browser's change index) — no extra git calls, no LLM — so the panel costs a few
milliseconds and appears with the rest of the page.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median

from agitrack.metrics.collect import CommitStat

# The turn kinds that represent agent work (mirrors the dashboard's AI_KINDS).
_AI_KINDS = {"agent", "covered", "agent-merge"}

# Below this many token-bearing agent turns the window is too thin to judge patterns —
# a young repo (or a narrow filter) gets no insights rather than noisy ones.
MIN_TURNS = 12

# Each half of the window needs at least this many turns before a trend is claimed. Above
# MIN_TURNS/2, deliberately: a 12-turn window would otherwise "trend" on six turns a side,
# which is noise. A window under ~16 turns therefore reports findings but no direction.
MIN_HALF_TURNS = 8

# A metric must move by more than this fraction before it counts as a real change,
# rather than noise in a small sample.
_TREND_BAND = 0.20

# A turn's recorded start/end can span far more than the turn itself — a resumed conversation
# stamps the whole conversation's start on its first commit, yielding "turns" of several days.
# Anything past this is a metadata artifact, not a long run, so it is dropped rather than
# reported (a card claiming a 266-hour turn destroys trust in every other number on the page).
_MAX_PLAUSIBLE_TURN_SECONDS = 8 * 3600

_SESSION_RE = re.compile(r"backend_session_id:\s*(\S+)")

# Synthetic markers the transcript injects in place of a real user prompt. A turn the agent ran
# off the back of a completed background task carries ``(background task completed)`` rather than
# anything the user typed (see ``_BACKGROUND_TURN_LABEL`` in transcripts/claude.py). When several
# such turns fold into one commit the label repeats. These are not user asks, so they must not be
# read as prompts — otherwise the repeated-asks card "detects" the machine talking to itself. The
# marker is stripped wherever it appears; whatever real text remains (a follow-up the user typed
# after the background turn opened) is kept and analysed normally.
_SYNTHETIC_PROMPT_MARKERS = ("(background task completed)",)


def _user_prompt(prompt: str) -> str:
    """The genuine user text in a turn's prompt: the recorded prompt with synthetic
    background-task markers removed. Empty when the turn had no real user prompt at all."""
    text = prompt
    for marker in _SYNTHETIC_PROMPT_MARKERS:
        text = text.replace(marker, " ")
    return re.sub(r"\s+", " ", text).strip()


# A prompt that reacts to the PREVIOUS turn going wrong. Start-anchored phrases plus a few
# unambiguous "it is still broken" fragments; deliberately conservative — a missed
# correction only weakens the signal, a false positive poisons it.
_CORRECTIVE_START = re.compile(
    r"^(no[,.! ]|nope\b|wrong\b|that('?s| is) (not|wrong|incorrect)|not (what|quite|right)\b"
    r"|still (not|no|doesn|isn|fails|failing|broken|wrong|incorrect|missing|shows)"
    r"|it('?s| is)? still\b|again[,.! ]|you (forgot|missed|didn'?t|removed|broke)"
    r"|undo\b|revert\b|that didn'?t\b|actually[, ]|instead[, ])",
    re.IGNORECASE,
)
_CORRECTIVE_ANYWHERE = (
    "doesn't work",
    "doesn't fix",
    "does not work",
    "not working",
    "didn't work",
    "same error",
    "same problem",
    "still incorrect",
    "still doesn't",
    "still fails",
    "still failing",
    "still getting",
    "still see",
    "still shows",
    "still broken",
    "still wrong",
    "that's incorrect",
    "it's still",
    "you missed",
)

# Source files whose change is "code work" for the verification-gap category. Config,
# markdown and data files are excluded: not shipping a test with a README edit is fine.
_CODE_SUFFIXES = (
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".cs",
    ".swift",
    ".scala",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".m",
    ".mm",
)


def _is_test_path(path: str) -> bool:
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    if name.startswith("test_") or name.startswith("test."):
        return True
    if ".test." in name or ".spec." in name or name.endswith("_test.py") or name.endswith("_test.go"):
        return True
    return any(part in ("test", "tests", "spec", "specs", "__tests__") for part in lowered.split("/")[:-1])


def _is_code_path(path: str) -> bool:
    return path.lower().endswith(_CODE_SUFFIXES) and not _is_test_path(path)


def _is_corrective(prompt: str) -> bool:
    text = prompt.strip()
    if len(text) < 4:
        return False
    if _CORRECTIVE_START.match(text):
        return True
    lowered = text.lower()
    return any(phrase in lowered for phrase in _CORRECTIVE_ANYWHERE)


def _session_id(stat: CommitStat) -> str:
    match = _SESSION_RE.search(stat.metadata_block or stat.message or "")
    return match.group(1) if match else ""


def _duration_seconds(stat: CommitStat) -> float | None:
    """Wall-clock seconds the agent spent on this turn, or None when the metadata doesn't
    record a plausible span (see :data:`_MAX_PLAUSIBLE_TURN_SECONDS`)."""
    if not (stat.started_at and stat.ended_at):
        return None
    try:
        start = datetime.fromisoformat(stat.started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(stat.ended_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    seconds = (end - start).total_seconds()
    if seconds < 0 or seconds > _MAX_PLAUSIBLE_TURN_SECONDS:
        return None
    return seconds


def _fmt(number: float) -> str:
    """1234567 -> '1.2M', 43210 -> '43.2k' — token counts read better rounded."""
    n = float(number)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def _norm_prompt(prompt: str) -> str:
    return re.sub(r"\s+", " ", prompt.strip().lower())[:120]


# ---------------------------------------------------------------------------
# The analysis context and the finding each category produces
# ---------------------------------------------------------------------------


@dataclass
class Context:
    """Everything the categories read, already scoped to the dashboard's current filter."""

    turns: list[CommitStat]  # oldest first, AI-kind, token-bearing
    files: dict[str, list[tuple[int, int, int]]] = field(default_factory=dict)  # path -> [(ts, ins, del)]
    sha_paths: dict[str, set[str]] = field(default_factory=dict)  # turn sha -> paths it changed

    def slice(self, turns: list[CommitStat]) -> "Context":
        """The same context restricted to ``turns`` (used for the earlier/later halves)."""
        shas = {stat.sha for stat in turns}
        stamps = {stat.timestamp for stat in turns}
        low, high = (min(stamps), max(stamps)) if stamps else (0, 0)
        files = {
            path: [change for change in changes if low <= change[0] <= high] for path, changes in self.files.items()
        }
        return Context(
            turns=turns,
            files={path: changes for path, changes in files.items() if changes},
            sha_paths={sha: paths for sha, paths in self.sha_paths.items() if sha in shas},
        )


@dataclass
class Finding:
    """One category's verdict on one window.

    ``metric`` is the category's single comparable scalar, always oriented so that LOWER IS
    BETTER. It is computed even when the category does not fire, which is what lets the
    earlier/later halves be compared and an improvement be reported.
    """

    key: str
    title: str
    metric: float
    metric_label: str
    triggered: bool = False
    severity: str = "medium"
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    suggestion: str = ""
    # How far past its firing threshold this finding is (0 when not triggered). Used to
    # rank equally-severe cards so the worst offender leads.
    excess: float = 0.0


# ---------------------------------------------------------------------------
# Categories. Each returns a Finding (metric always set), or None when the window
# holds too little of the relevant data to say anything at all.
# ---------------------------------------------------------------------------


def _correction_loops(ctx: Context) -> Finding | None:
    """Turns spent re-doing the previous turn: the clearest sign the first ask was
    under-specified or the result wasn't verified before handing back."""
    by_session: dict[str, list[CommitStat]] = defaultdict(list)
    for stat in ctx.turns:
        by_session[_session_id(stat)].append(stat)
    corrective: list[CommitStat] = []
    eligible = 0
    longest_chain = 0
    chain_example = ""
    for session_turns in by_session.values():
        chain = 0
        for index, stat in enumerate(session_turns):
            if index == 0:
                continue  # a session's first prompt has no prior turn to correct
            prompt = _user_prompt(stat.prompt)
            if not prompt:
                continue  # a background-task turn is not a user follow-up — don't count it
            eligible += 1
            if _is_corrective(prompt):
                corrective.append(stat)
                chain += 1
                if chain > longest_chain:
                    longest_chain = chain
                    chain_example = prompt
            else:
                chain = 0
    if eligible < 10:
        return None
    fraction = len(corrective) / eligible
    finding = Finding(
        key="correction-loops",
        title="Correction loops",
        metric=fraction,
        metric_label="share of follow-up turns that are corrections",
    )
    if eligible < 15 or len(corrective) < 5 or fraction < 0.12:
        return finding
    tokens = sum(stat.tokens.get("output", 0) for stat in corrective)
    evidence = [
        f"{len(corrective)} of {eligible} follow-up turns ({fraction:.0%}) reacted to the previous "
        f"turn being wrong or incomplete ('still fails', 'no, …', 'you missed …').",
        f"Those correction turns alone produced {_fmt(tokens)} output tokens of re-work.",
    ]
    if longest_chain >= 2:
        evidence.append(f'Longest correction chain: {longest_chain} consecutive turns (e.g. "{chain_example[:90]}").')
    finding.triggered = True
    finding.excess = fraction / 0.12
    finding.severity = "high" if fraction >= 0.25 else "medium"
    finding.summary = f"{fraction:.0%} of follow-up turns re-do the previous turn."
    finding.evidence = evidence
    finding.suggestion = (
        "Front-load the first prompt: name the files involved, the acceptance criteria, and how "
        "the result will be checked. Ask the agent to verify (run the tests / drive the feature) "
        "before reporting done — a verified turn rarely needs a correction turn."
    )
    return finding


def _file_rework(ctx: Context) -> Finding | None:
    """Files the agent keeps RETURNING to shortly after editing them — iteration churn.

    The signal is quick returns (another edit within the hour), not the delete/insert
    ratio: replacing a line is one insertion plus one deletion, so any file whose edits
    modify existing lines has del≈ins no matter how efficient the work was."""
    if not ctx.files:
        return None
    total_gaps = 0
    total_quick = 0
    hotspots: list[tuple[int, float, int, str]] = []
    for path, changes in ctx.files.items():
        timestamps = sorted(ts for ts, _ins, _dl in changes if ts)
        if len(timestamps) < 4:
            continue
        quick = sum(1 for a, b in zip(timestamps, timestamps[1:]) if 0 < b - a <= 3600)
        total_gaps += len(timestamps) - 1
        total_quick += quick
        if len(timestamps) >= 8:
            ratio = quick / (len(timestamps) - 1)
            if quick >= 6 and ratio >= 0.35:
                hotspots.append((quick, ratio, len(changes), path))
    if total_gaps < 10:
        return None
    finding = Finding(
        key="file-rework",
        title="Rework hotspots",
        metric=total_quick / total_gaps,
        metric_label="share of edits that revisit a file within the hour",
    )
    if not hotspots:
        return finding
    hotspots.sort(reverse=True)
    worst = hotspots[0]
    finding.triggered = True
    finding.excess = worst[1] / 0.35
    finding.severity = "high" if worst[0] >= 20 and worst[1] >= 0.5 else "medium"
    finding.summary = f"{len(hotspots)} file(s) keep being re-edited within the hour."
    finding.evidence = [
        f"{path}: edited in {count} turns, and {quick} of those edits ({ratio:.0%}) came within an "
        "hour of the previous edit to the same file."
        for quick, ratio, count, path in hotspots[:3]
    ]
    finding.suggestion = (
        "Rapid re-edits to the same file mean the turn before didn't land it. For these areas, "
        "state the full requirement in one prompt (or ask for a plan first), and insist on a test "
        "or a verification run before the agent reports done — so the next turn builds on the last "
        "instead of redoing it."
    )
    return finding


def _context_growth(ctx: Context) -> Finding | None:
    """Within long sessions, every turn re-reads the whole accumulated context, so late
    turns cost a multiple of early ones for the same work."""
    by_session: dict[str, list[CommitStat]] = defaultdict(list)
    for stat in ctx.turns:
        if stat.tokens.get("cache_read", 0) > 0:
            by_session[_session_id(stat)].append(stat)
    ratios: list[float] = []
    long_sessions = 0
    total_read = 0
    late_read = 0
    for session_id, session_turns in by_session.items():
        if not session_id or len(session_turns) < 6:
            continue
        long_sessions += 1
        third = max(1, len(session_turns) // 3)
        early = [stat.tokens["cache_read"] for stat in session_turns[:third]]
        late = [stat.tokens["cache_read"] for stat in session_turns[-third:]]
        early_avg = sum(early) / len(early)
        late_avg = sum(late) / len(late)
        if early_avg > 0:
            ratios.append(late_avg / early_avg)
        reads = [stat.tokens["cache_read"] for stat in session_turns]
        total_read += sum(reads)
        late_read += sum(sorted(reads, reverse=True)[: max(1, len(reads) // 5)])
    if long_sessions < 2 or not ratios:
        return None
    growth = median(ratios)
    finding = Finding(
        key="context-growth",
        title="Long-session context cost",
        metric=growth,
        metric_label="context re-read by late turns vs early turns",
    )
    if growth < 2.0:
        return finding
    top_share = late_read / total_read if total_read else 0.0
    finding.triggered = True
    finding.excess = growth / 2.0
    finding.severity = "high" if growth >= 4 else "medium"
    finding.summary = f"Late turns in long sessions read {growth:.1f}× more context than early ones."
    finding.evidence = [
        f"Across {long_sessions} session(s) of 6+ turns, a session's last third of turns reads a "
        f"median {growth:.1f}× more context per turn than its first third.",
        f"The most expensive 20% of turns account for {top_share:.0%} of all context reading "
        f"({_fmt(late_read)} of {_fmt(total_read)} tokens).",
    ]
    finding.suggestion = (
        "Start a fresh session per task instead of continuing one long conversation — each turn "
        "re-reads everything said before it. For work that only needs exploration (find the code, "
        "read the logs), have the agent delegate to sub-agents so the findings, not the search, "
        "enter the main context."
    )
    return finding


def _session_fragmentation(ctx: Context) -> Finding | None:
    """The opposite failure: many one-shot sessions, each re-reading the repo from cold."""
    by_session: dict[str, int] = defaultdict(int)
    for stat in ctx.turns:
        session_id = _session_id(stat)
        if session_id:
            by_session[session_id] += 1
    if len(by_session) < 6:
        return None
    short = [session_id for session_id, count in by_session.items() if count <= 2]
    fraction = len(short) / len(by_session)
    finding = Finding(
        key="session-fragmentation",
        title="Fragmented sessions",
        metric=fraction,
        metric_label="share of sessions ending within two turns",
    )
    if len(by_session) < 8 or fraction < 0.5:
        return finding
    finding.triggered = True
    finding.excess = fraction / 0.5
    finding.severity = "medium"
    finding.summary = f"{fraction:.0%} of sessions end after one or two turns."
    finding.evidence = [
        f"{len(short)} of {len(by_session)} sessions contain at most 2 turns, so the repo context "
        "is rebuilt from cold for most requests instead of being reused.",
    ]
    finding.suggestion = (
        "Resume the previous session for related follow-ups (aGiTrack resumes it by default) — a "
        "warm session already knows the code you just discussed. Save fresh sessions for genuinely "
        "new tasks."
    )
    return finding


def _repeated_prompts(ctx: Context) -> Finding | None:
    """The same request typed again and again is a standing task — automation material."""
    counts: dict[str, tuple[int, str]] = {}
    for stat in ctx.turns:
        prompt = _user_prompt(stat.prompt)  # drop synthetic background-task markers first
        if len(prompt) < 15 or prompt.startswith("/"):
            continue  # too short to be a task; slash commands are already automation
        key = _norm_prompt(prompt)
        seen, original = counts.get(key, (0, prompt))
        counts[key] = (seen + 1, original)
    if not counts:
        return None
    repeats = sorted(((count, original) for count, original in counts.values() if count >= 3), reverse=True)
    retyped = sum(count - 1 for count, _ in repeats)
    finding = Finding(
        key="repeated-prompts",
        title="Repeated asks",
        metric=retyped / len(ctx.turns),
        metric_label="re-typed requests per turn",
    )
    if not repeats:
        return finding
    finding.triggered = True
    finding.excess = float(len(repeats))
    finding.severity = "medium" if retyped >= 6 else "info"
    finding.summary = f"{len(repeats)} request(s) keep being typed from scratch."
    finding.evidence = [f'Asked {count} times: "{original[:100]}"' for count, original in repeats[:3]]
    finding.suggestion = (
        "A request typed three or more times is a standing task: capture it as a skill or a "
        "CLAUDE.md instruction (or a script the agent can run) so one short command replaces "
        "re-explaining it — and the agent stops re-deriving the steps each time."
    )
    return finding


def _low_yield_turns(ctx: Context) -> Finding | None:
    """Heavy turns that changed nothing in the repo. Fine occasionally (Q&A, review);
    as a pattern it usually means exploration is running in the expensive main context."""
    heavy_no_change = [
        stat for stat in ctx.turns if stat.tokens.get("output", 0) >= 10_000 and stat.insertions + stat.deletions == 0
    ]
    fraction = len(heavy_no_change) / len(ctx.turns)
    finding = Finding(
        key="low-yield-turns",
        title="Heavy no-change turns",
        metric=fraction,
        metric_label="share of turns burning 10k+ tokens with no file change",
    )
    if len(heavy_no_change) < 5 or fraction < 0.12:
        return finding
    tokens = sum(stat.tokens.get("output", 0) for stat in heavy_no_change)
    example = next((_user_prompt(stat.prompt) for stat in heavy_no_change if _user_prompt(stat.prompt)), "")
    evidence = [
        f"{len(heavy_no_change)} turns ({fraction:.0%}) produced 10k+ output tokens each without "
        f"changing any file — {_fmt(tokens)} output tokens in total.",
    ]
    if example:
        evidence.append(f'Example: "{example[:90]}"')
    finding.triggered = True
    finding.excess = fraction / 0.12
    finding.severity = "medium"
    finding.summary = f"{fraction:.0%} of turns burn significant tokens without touching the repo."
    finding.evidence = evidence
    finding.suggestion = (
        "If these are research/exploration turns, run them through sub-agents or a separate "
        "session so only the conclusions enter the main conversation. If they are analyses you "
        "asked for, ask for the short answer first and the detail on demand."
    )
    return finding


def _verification_gap(ctx: Context) -> Finding | None:
    """Code-changing turns that touched no test. Untested turns are the ones that come back
    as corrections — this is the upstream cause of both the loops and the hotspots."""
    if not ctx.sha_paths:
        return None
    # Only meaningful in a repo that actually has tests in this window.
    if not any(_is_test_path(path) for paths in ctx.sha_paths.values() for path in paths):
        return None
    code_turns = 0
    untested = 0
    examples: list[str] = []
    for stat in ctx.turns:
        paths = ctx.sha_paths.get(stat.sha)
        if not paths or not any(_is_code_path(path) for path in paths):
            continue
        code_turns += 1
        if not any(_is_test_path(path) for path in paths):
            untested += 1
            example = _user_prompt(stat.prompt)
            if len(examples) < 3 and example:
                examples.append(example[:80])
    if code_turns < 10:
        return None
    fraction = untested / code_turns
    finding = Finding(
        key="verification-gap",
        title="Unverified code turns",
        metric=fraction,
        metric_label="share of code turns that touched no test",
    )
    if fraction < 0.5 or untested < 6:
        return finding
    finding.triggered = True
    finding.excess = fraction / 0.5
    finding.severity = "high" if fraction >= 0.75 else "medium"
    finding.summary = f"{fraction:.0%} of code-changing turns shipped without touching a test."
    finding.evidence = [
        f"{untested} of {code_turns} turns that changed source files changed no test file alongside them.",
    ]
    if examples:
        finding.evidence.append(f'For example: "{examples[0]}"')
    finding.suggestion = (
        "Ask for the test in the same turn as the change, not the turn after — 'add a failing test "
        "first, then make it pass' costs one turn and removes the correction turn that usually "
        "follows. It is also what stops the same file being reworked days later."
    )
    return finding


def _wide_turns(ctx: Context) -> Finding | None:
    """Turns that touch a very large number of files at once are hard to review, hard to
    revert, and usually mean several tasks were bundled into one prompt."""
    if not ctx.sha_paths:
        return None
    sized = [len(ctx.sha_paths.get(stat.sha, ())) for stat in ctx.turns]
    sized = [size for size in sized if size]
    if len(sized) < 10:
        return None
    wide = [size for size in sized if size > 8]
    fraction = len(wide) / len(sized)
    finding = Finding(
        key="wide-turns",
        title="Overloaded turns",
        metric=fraction,
        metric_label="share of turns touching more than 8 files",
    )
    if len(wide) < 4 or fraction < 0.15:
        return finding
    finding.triggered = True
    finding.excess = fraction / 0.15
    finding.severity = "medium"
    finding.summary = f"{fraction:.0%} of turns change more than 8 files at once."
    finding.evidence = [
        f"{len(wide)} of {len(sized)} file-changing turns touched more than 8 files "
        f"(largest: {max(wide)} files in a single turn).",
        "A turn that spans that many files bundles several tasks, so its commit can't be reviewed "
        "or reverted as one decision.",
    ]
    finding.suggestion = (
        "Split the ask: one prompt per behaviour change, letting each land as its own commit. "
        "Where a change genuinely is repo-wide (a rename, a lint fix), say so explicitly so the "
        "sweep is separated from the thinking."
    )
    return finding


def _slow_turns(ctx: Context) -> Finding | None:
    """Turns that ran for a very long wall-clock time. A long turn is a long feedback loop:
    you cannot steer it, and when it lands wrong the whole run is wasted."""
    durations = [seconds for seconds in (_duration_seconds(stat) for stat in ctx.turns) if seconds is not None]
    if len(durations) < 10:
        return None
    slow = [seconds for seconds in durations if seconds > 1800]
    fraction = len(slow) / len(durations)
    finding = Finding(
        key="slow-turns",
        title="Long feedback loops",
        metric=fraction,
        metric_label="share of turns running over 30 minutes",
    )
    if len(slow) < 4 or fraction < 0.10:
        return finding
    finding.triggered = True
    finding.excess = fraction / 0.10
    finding.severity = "medium"
    finding.summary = f"{fraction:.0%} of turns run longer than 30 minutes."
    finding.evidence = [
        f"{len(slow)} of {len(durations)} turns ran over 30 minutes "
        f"(longest {max(slow) / 60:.0f} min; median turn {median(durations) / 60:.0f} min).",
        "Nothing can be corrected mid-turn, so a long turn that lands wrong wastes its whole run.",
    ]
    finding.suggestion = (
        "Ask for a plan, or a first slice, before a long autonomous run — a checkpoint you can "
        "redirect at is cheaper than an hour spent in the wrong direction. Long sweeps are better "
        "delegated to sub-agents that report back."
    )
    return finding


_CATEGORIES = (
    _correction_loops,
    _verification_gap,
    _file_rework,
    _context_growth,
    _session_fragmentation,
    _wide_turns,
    _slow_turns,
    _low_yield_turns,
    _repeated_prompts,
)

_SEVERITY_RANK = {"high": 0, "medium": 1, "info": 2, "good": 3}


# ---------------------------------------------------------------------------
# Trend + assembly
# ---------------------------------------------------------------------------


def _run(ctx: Context) -> dict[str, Finding]:
    findings = {}
    for category in _CATEGORIES:
        finding = category(ctx)
        if finding is not None:
            findings[finding.key] = finding
    return findings


def _halves(turns: list[CommitStat], midpoint: int) -> dict:
    """The concrete spans the trend compares, so "vs earlier" names an actual period rather
    than an unexplained baseline. The split is by TURN COUNT (equal samples), which is why
    the two spans are rarely equal in calendar length — the dates say exactly where it fell."""
    earlier, later = turns[:midpoint], turns[midpoint:]
    return {
        "earlier_from": earlier[0].timestamp,
        "earlier_to": earlier[-1].timestamp,
        "earlier_turns": len(earlier),
        "later_from": later[0].timestamp,
        "later_to": later[-1].timestamp,
        "later_turns": len(later),
    }


def _trend(earlier: Finding | None, later: Finding | None, window: dict) -> dict | None:
    """How the category's metric moved from the window's earlier half to its later half.
    Lower is always better, so a fall is an improvement."""
    if earlier is None or later is None:
        return None
    before, after = earlier.metric, later.metric
    if before <= 0 and after <= 0:
        return None
    if before <= 0:
        direction, change = "worse", 1.0
    else:
        change = (after - before) / before
        if change <= -_TREND_BAND:
            direction = "better"
        elif change >= _TREND_BAND:
            direction = "worse"
        else:
            direction = "steady"
    return {
        "direction": direction,
        "change": change,  # signed fraction; negative = improving
        "earlier": before,
        "later": after,
        "label": earlier.metric_label,
        **window,
    }


def _as_dict(finding: Finding, trend: dict | None) -> dict:
    payload: dict = {
        "key": finding.key,
        "title": finding.title,
        "severity": finding.severity,
        "summary": finding.summary,
        "evidence": list(finding.evidence),
        "suggestion": finding.suggestion,
    }
    if trend:
        payload["trend"] = trend
    return payload


def _resolved(key: str, earlier: Finding, later: Finding, window: dict) -> dict:
    """A habit that fired in the window's earlier half and no longer fires in its later
    half. Surfaced so an improvement is visible instead of simply disappearing."""
    change = (later.metric - earlier.metric) / earlier.metric if earlier.metric > 0 else -1.0
    return {
        "key": f"{key}-resolved",
        "title": f"{earlier.title}: improved",
        "severity": "good",
        "summary": f"No longer a problem in the later part of this range ({abs(change):.0%} lower).",
        "evidence": [
            f"{earlier.metric_label}: {_ratio(earlier.metric)} earlier in this range, {_ratio(later.metric)} later.",
            "It was flagged over the earlier half of the range and is now below the threshold.",
        ],
        "suggestion": "Whatever changed here is working — keep it, and check the remaining cards for the next win.",
        "trend": {
            "direction": "better",
            "change": change,
            "earlier": earlier.metric,
            "later": later.metric,
            "label": earlier.metric_label,
            **window,
        },
    }


def _ratio(value: float) -> str:
    return f"{value:.0%}" if value <= 1.5 else f"{value:.1f}×"


def build_insights(
    stats: list[CommitStat],
    files: dict[str, list[tuple[int, int, int]]] | None = None,
    sha_paths: dict[str, set[str]] | None = None,
) -> list[dict]:
    """The efficiency insights for the commits in ``stats``, most severe first.

    ``stats``: the commit stats ALREADY narrowed to whatever the dashboard is showing
    (time range, backend, model, committer), oldest first. Scoping to the view is what lets
    a user watch a habit improve: a whole-history verdict would keep the bad old turns in the
    denominator forever.

    ``files``: per-file change history, ``path -> [(timestamp, insertions, deletions), …]``.
    ``sha_paths``: ``turn sha -> paths it changed``. Both come from the file browser's index
    (see :func:`context_from_browser`); the categories that need them are skipped without.

    Each returned insight may carry a ``trend`` comparing the window's earlier and later
    halves, and a habit that stopped firing over the window appears as a ``good`` card.
    Returns ``[]`` when the window holds fewer than ``MIN_TURNS`` token-bearing agent turns.
    """
    turns = [stat for stat in stats if stat.kind in _AI_KINDS and stat.tokens]
    if len(turns) < MIN_TURNS:
        return []
    turns.sort(key=lambda stat: (stat.timestamp, stat.sha))
    context = Context(turns=turns, files=files or {}, sha_paths=sha_paths or {})
    findings = _run(context)

    # Split the window in half by turn order to measure movement within it.
    earlier_findings: dict[str, Finding] = {}
    later_findings: dict[str, Finding] = {}
    midpoint = len(turns) // 2
    window: dict = {}
    if midpoint >= MIN_HALF_TURNS and len(turns) - midpoint >= MIN_HALF_TURNS:
        earlier_findings = _run(context.slice(turns[:midpoint]))
        later_findings = _run(context.slice(turns[midpoint:]))
        window = _halves(turns, midpoint)

    insights = [
        _as_dict(finding, _trend(earlier_findings.get(key), later_findings.get(key), window))
        for key, finding in findings.items()
        if finding.triggered
    ]
    # Wins: fired early in the window, no longer fires late (and isn't firing over the window
    # as a whole either — a category can be absent from the full run if its data thinned out).
    for key, early in earlier_findings.items():
        late = later_findings.get(key)
        whole = findings.get(key)
        if early.triggered and late is not None and not late.triggered and not (whole and whole.triggered):
            insights.append(_resolved(key, early, late, window))

    order = {finding.key: index for index, finding in enumerate(findings.values())}
    insights.sort(
        key=lambda insight: (
            _SEVERITY_RANK.get(insight["severity"], 9),
            -findings[insight["key"]].excess if insight["key"] in findings else 0.0,
            order.get(insight["key"], 99),
        )
    )
    return insights


def context_from_browser(browser, stats: list[CommitStat] | None = None) -> tuple[dict, dict]:
    """Adapt a :class:`~agitrack.metrics.files.FileBrowser` index into the ``(files, sha_paths)``
    inputs, restricted to ``stats`` when given — so the file-derived categories see exactly the
    commits the dashboard's filter selected. Reuses the browser's cached index, so this costs no
    git work."""
    keep = {stat.sha for stat in stats} if stats is not None else None
    files: dict[str, list[tuple[int, int, int]]] = {}
    sha_paths: dict[str, set[str]] = defaultdict(set)
    for path, entry in browser.index.items():
        changes = [
            (change.timestamp, change.insertions, change.deletions)
            for change in entry.changes
            if keep is None or change.sha in keep
        ]
        if changes:
            files[path] = changes
        for change in entry.changes:
            if keep is None or change.sha in keep:
                sha_paths[change.sha].add(path)
    return files, dict(sha_paths)
