"""Agent-usage efficiency insights for the dashboard.

The commit metadata aGiTrack records (per-turn tokens, prompts, session ids, timestamps)
plus the per-file change history is enough to spot HOW the agent is being used
inefficiently — not how much code it wrote. Each category below extracts one behavioural
signal, backs it with concrete numbers from this repo's history, and pairs it with a
specific suggestion. A category stays silent unless its own evidence threshold is met, so
a young repo with a handful of commits gets no half-baked advice.

Everything is a single pass over data the dashboard already holds in memory (the commit
stats and the file browser's change index) — no extra git calls, no LLM — so the panel
costs nothing to serve and appears instantly.
"""

from __future__ import annotations

import re
from collections import defaultdict
from statistics import median

from agitrack.metrics.collect import CommitStat

# The turn kinds that represent agent work (mirrors the dashboard's AI_KINDS).
_AI_KINDS = {"agent", "covered", "agent-merge"}

# Below this many token-bearing agent turns the history is too thin to judge patterns —
# a young repo gets no insights rather than noisy ones.
MIN_TURNS = 12

_SESSION_RE = re.compile(r"backend_session_id:\s*(\S+)")

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
# Categories. Each takes the eligible (AI, token-bearing, oldest-first) turns and
# returns an insight dict, or None when its threshold isn't met.
# ---------------------------------------------------------------------------


def _correction_loops(turns: list[CommitStat]) -> dict | None:
    """Turns spent re-doing the previous turn: the clearest sign the first ask was
    under-specified or the result wasn't verified before handing back."""
    by_session: dict[str, list[CommitStat]] = defaultdict(list)
    for stat in turns:
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
            if not stat.prompt.strip():
                continue
            eligible += 1
            if _is_corrective(stat.prompt):
                corrective.append(stat)
                chain += 1
                if chain > longest_chain:
                    longest_chain = chain
                    chain_example = stat.prompt.strip()
            else:
                chain = 0
    if eligible < 15 or len(corrective) < 5:
        return None
    fraction = len(corrective) / eligible
    if fraction < 0.12:
        return None
    tokens = sum(stat.tokens.get("output", 0) for stat in corrective)
    evidence = [
        f"{len(corrective)} of {eligible} follow-up turns ({fraction:.0%}) reacted to the previous "
        f"turn being wrong or incomplete ('still fails', 'no, …', 'you missed …').",
        f"Those correction turns alone produced {_fmt(tokens)} output tokens of re-work.",
    ]
    if longest_chain >= 2:
        evidence.append(f'Longest correction chain: {longest_chain} consecutive turns (e.g. "{chain_example[:90]}").')
    return {
        "key": "correction-loops",
        "title": "Correction loops",
        "severity": "high" if fraction >= 0.25 else "medium",
        "summary": f"{fraction:.0%} of follow-up turns re-do the previous turn.",
        "evidence": evidence,
        "suggestion": (
            "Front-load the first prompt: name the files involved, the acceptance criteria, and how "
            "the result will be checked. Ask the agent to verify (run the tests / drive the feature) "
            "before reporting done — a verified turn rarely needs a correction turn."
        ),
    }


def _repeated_prompts(turns: list[CommitStat]) -> dict | None:
    """The same request typed again and again is a standing task — automation material."""
    counts: dict[str, tuple[int, str]] = {}
    for stat in turns:
        prompt = stat.prompt.strip()
        if len(prompt) < 15 or prompt.startswith("/"):
            continue  # too short to be a task; slash commands are already automation
        key = _norm_prompt(prompt)
        seen, original = counts.get(key, (0, prompt))
        counts[key] = (seen + 1, original)
    repeats = sorted(
        ((count, original) for count, original in counts.values() if count >= 3),
        reverse=True,
    )
    if not repeats:
        return None
    evidence = [f'Asked {count} times: "{original[:100]}"' for count, original in repeats[:3]]
    total = sum(count - 1 for count, _ in repeats)
    return {
        "key": "repeated-prompts",
        "title": "Repeated asks",
        "severity": "medium" if total >= 6 else "info",
        "summary": f"{len(repeats)} request(s) keep being typed from scratch.",
        "evidence": evidence,
        "suggestion": (
            "A request typed three or more times is a standing task: capture it as a skill or a "
            "CLAUDE.md instruction (or a script the agent can run) so one short command replaces "
            "re-explaining it — and the agent stops re-deriving the steps each time."
        ),
    }


def _context_growth(turns: list[CommitStat]) -> dict | None:
    """Within long sessions, every turn re-reads the whole accumulated context, so late
    turns cost a multiple of early ones for the same work."""
    by_session: dict[str, list[CommitStat]] = defaultdict(list)
    for stat in turns:
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
    top_share = late_read / total_read if total_read else 0.0
    if growth < 2.0:
        return None
    return {
        "key": "context-growth",
        "title": "Long-session context cost",
        "severity": "high" if growth >= 4 else "medium",
        "summary": f"Late turns in long sessions read {growth:.1f}× more context than early ones.",
        "evidence": [
            f"Across {long_sessions} session(s) of 6+ turns, a session's last third of turns reads a "
            f"median {growth:.1f}× more context per turn than its first third.",
            f"The most expensive 20% of turns account for {top_share:.0%} of all context reading "
            f"({_fmt(late_read)} of {_fmt(total_read)} tokens).",
        ],
        "suggestion": (
            "Start a fresh session per task instead of continuing one long conversation — each turn "
            "re-reads everything said before it. For work that only needs exploration (find the code, "
            "read the logs), have the agent delegate to sub-agents so the findings, not the search, "
            "enter the main context."
        ),
    }


def _session_fragmentation(turns: list[CommitStat]) -> dict | None:
    """The opposite failure: many one-shot sessions, each re-reading the repo from cold."""
    by_session: dict[str, int] = defaultdict(int)
    for stat in turns:
        session_id = _session_id(stat)
        if session_id:
            by_session[session_id] += 1
    if len(by_session) < 8:
        return None
    short = [session_id for session_id, count in by_session.items() if count <= 2]
    fraction = len(short) / len(by_session)
    if fraction < 0.5:
        return None
    return {
        "key": "session-fragmentation",
        "title": "Fragmented sessions",
        "severity": "medium",
        "summary": f"{fraction:.0%} of sessions end after one or two turns.",
        "evidence": [
            f"{len(short)} of {len(by_session)} sessions contain at most 2 turns, so the repo context "
            "is rebuilt from cold for most requests instead of being reused.",
        ],
        "suggestion": (
            "Resume the previous session for related follow-ups (aGiTrack resumes it by default) — a "
            "warm session already knows the code you just discussed. Save fresh sessions for genuinely "
            "new tasks."
        ),
    }


def _file_rework(files: dict[str, list[tuple[int, int, int]]] | None) -> dict | None:
    """Files the agent keeps RETURNING to shortly after editing them — iteration churn.

    The signal is quick returns (another edit within the hour), not the delete/insert
    ratio: replacing a line is one insertion plus one deletion, so any file whose edits
    modify existing lines has del≈ins no matter how efficient the work was. Coming back
    to a file over and over within the hour is what "we didn't get it right the first
    time" actually looks like in history."""
    if not files:
        return None
    hotspots: list[tuple[int, float, int, str]] = []
    for path, changes in files.items():
        if len(changes) < 8:
            continue
        timestamps = sorted(ts for ts, _ins, _dl in changes if ts)
        if len(timestamps) < 8:
            continue
        quick = sum(1 for a, b in zip(timestamps, timestamps[1:]) if 0 < b - a <= 3600)
        quick_ratio = quick / (len(timestamps) - 1)
        if quick < 6 or quick_ratio < 0.35:
            continue
        hotspots.append((quick, quick_ratio, len(changes), path))
    if not hotspots:
        return None
    hotspots.sort(reverse=True)
    evidence = []
    for quick, quick_ratio, count, path in hotspots[:3]:
        evidence.append(
            f"{path}: edited in {count} turns, and {quick} of those edits ({quick_ratio:.0%}) came "
            "within an hour of the previous edit to the same file."
        )
    worst = hotspots[0]
    return {
        "key": "file-rework",
        "title": "Rework hotspots",
        "severity": "high" if worst[0] >= 20 and worst[1] >= 0.5 else "medium",
        "summary": f"{len(hotspots)} file(s) keep being re-edited within the hour.",
        "evidence": evidence,
        "suggestion": (
            "Rapid re-edits to the same file mean the turn before didn't land it. For these areas, "
            "state the full requirement in one prompt (or ask for a plan first), and insist on a "
            "test or a verification run before the agent reports done — so the next turn builds on "
            "the last instead of redoing it."
        ),
    }


def _low_yield_turns(turns: list[CommitStat]) -> dict | None:
    """Heavy turns that changed nothing in the repo. Fine occasionally (Q&A, review);
    as a pattern it usually means exploration is running in the expensive main context."""
    heavy_no_change = [
        stat for stat in turns if stat.tokens.get("output", 0) >= 10_000 and stat.insertions + stat.deletions == 0
    ]
    fraction = len(heavy_no_change) / len(turns)
    if len(heavy_no_change) < 5 or fraction < 0.12:
        return None
    tokens = sum(stat.tokens.get("output", 0) for stat in heavy_no_change)
    example = next((stat.prompt.strip() for stat in heavy_no_change if stat.prompt.strip()), "")
    evidence = [
        f"{len(heavy_no_change)} turns ({fraction:.0%}) produced 10k+ output tokens each without "
        f"changing any file — {_fmt(tokens)} output tokens in total.",
    ]
    if example:
        evidence.append(f'Example: "{example[:90]}"')
    return {
        "key": "low-yield-turns",
        "title": "Heavy no-change turns",
        "severity": "medium",
        "summary": f"{fraction:.0%} of turns burn significant tokens without touching the repo.",
        "evidence": evidence,
        "suggestion": (
            "If these are research/exploration turns, run them through sub-agents or a separate "
            "session so only the conclusions enter the main conversation. If they are analyses you "
            "asked for, ask for the short answer first and the detail on demand."
        ),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_insights(
    stats: list[CommitStat],
    files: dict[str, list[tuple[int, int, int]]] | None = None,
) -> list[dict]:
    """The efficiency insights for a repo's history, most severe first.

    ``stats``: the dashboard's commit stats (oldest first, as ``Dashboard.stats``).
    ``files``: per-file change history, ``path -> [(timestamp, insertions, deletions), …]``
    (from the file browser's index); optional — the file-rework category is skipped
    without it.

    Returns ``[]`` when the history is too thin to judge (fewer than ``MIN_TURNS``
    token-bearing agent turns), so young repos show nothing rather than noise.
    """
    turns = [stat for stat in stats if stat.kind in _AI_KINDS and stat.tokens]
    if len(turns) < MIN_TURNS:
        return []
    insights = [
        insight
        for insight in (
            _correction_loops(turns),
            _file_rework(files),
            _context_growth(turns),
            _session_fragmentation(turns),
            _repeated_prompts(turns),
            _low_yield_turns(turns),
        )
        if insight
    ]
    rank = {"high": 0, "medium": 1, "info": 2}
    insights.sort(key=lambda insight: rank.get(insight["severity"], 3))
    return insights


def files_from_browser(browser) -> dict[str, list[tuple[int, int, int]]]:
    """Adapt a :class:`~agitrack.metrics.files.FileBrowser` index to the ``files`` input:
    the per-file change history the browser already computed (and caches), so the
    file-rework category costs no extra git work."""
    return {
        path: [(change.timestamp, change.insertions, change.deletions) for change in entry.changes]
        for path, entry in browser.index.items()
        if entry.changes
    }
