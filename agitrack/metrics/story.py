"""The storyline page (``/story``): the repo's history told as a story.

Where the dashboard shows WHAT changed and the learn page coaches the person behind it,
the storyline answers "how did this project get here?". It reads the same commit metadata
and interaction traces and hands them to the coding-agent BACKEND, which writes a timeline
of MOMENTS: turning points, features that landed, things that were torn out and rebuilt.

Four levels of detail, so a reader can stop wherever they like:

1. the arc (a title, a tagline, acts),
2. a moment card (when, what, one line of why),
3. the moment opened: the full telling plus WHAT THEY ASKED FOR at the turning points, and
4. the commits behind it, each expandable into its real diff.

Those asks are never invented. The quotes are the developer's own prompts, read
straight from the ``# Interaction Trace`` blocks in the commit messages; the agent only
picks WHICH moments were pivotal and writes a sentence about each. A quote the agent
cannot be matched to a real commit is dropped rather than shown.

Generation is incremental and bounded. A deterministic pre-pass (:func:`segment_episodes`)
groups commits into episodes by time gaps, so the agent is asked a handful of small,
well-scoped questions instead of one impossible one, and the number of calls per build is
capped. The result is stored in ``.agitrack/story.json`` (git-ignored, per branch) and
extended in place as new commits land, so re-opening the page costs nothing.

The page works on both dashboards: the live one (``agitrack -d``) tells the story of the
branch's real commits, and the backtrace one (``agitrack --backtrace``) tells the story of
the reconstruction, from the same code. The static export ships whatever story is stored,
with generation disabled.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agitrack.metrics import learn as learn_page
from agitrack.metrics.collect import CommitStat

# Housekeeping commits (aGiTrack's own bookkeeping) carry no story; everything else does,
# including plain user commits: releases and merges are part of how a project moved.
_SKIP_KINDS = {"agitrack-ops"}
_AI_KINDS = {"agent", "covered", "agent-merge"}

# --- segmentation ----------------------------------------------------------------
# A new episode starts when work stopped for this long: real sessions cluster, and the gap
# between clusters is the natural seam of a story.
_EPISODE_GAP_SECONDS = 6 * 3600
# ...and an episode never grows past this, so one marathon day still becomes several beats.
_EPISODE_MAX_COMMITS = 12

# --- agent budget ----------------------------------------------------------------
# Generation is LAZY, in two stages, because waiting twenty minutes for a story nobody has
# read yet is the wrong trade:
#
#   1. an OVERVIEW pass reads the WHOLE history once, cheaply, and names the eras: that is
#      the coarse story, always complete, however long the project is;
#   2. an OUTLINE pass writes moment headlines for the most RECENT stretch only, because
#      that is what a reader opens the page for (older stretches on request); and
#   3. a moment is WRITTEN OUT the first time someone opens it: one call, over the full
#      material of just that moment, cached in the store forever after.
#
# So a build is two calls and under a minute, and depth is paid for only where a reader goes.
# One telling is one call: spread() already bounds a build to _MOMENTS_FINE episodes, and
# these only exist so a pathological history cannot build a prompt without a ceiling.
# Splitting a telling across calls would leave the tail of a part untold, because only
# _MAX_BATCHES of them are ever sent.
_BATCH_EPISODES = 15
_BATCH_CHARS = 24000
# Outline calls per build. ONE: a build covers the newest stretch and stops. Forty-five
# moments in one go is both a long wait and more than anyone reads at once; "go further
# back" fetches the next stretch when it is actually wanted.
_MAX_BATCHES = 1
# Moments per part: a handful spread across its whole span. There was once a second, finer
# telling of the same span behind a "look closer" button. It was removed: it re-read commits
# the reader had already paid to have told, and because a part can hold no more moments than
# it has sittings of work, it could come back with FEWER moments than the view it was
# supposed to magnify. Choosing the DAYS you want a story about (the range control) gives a
# reader the same thing honestly - fewer commits told at the same grain is a closer look.
_MOMENTS_COARSE = 6
# How much of one episode the outline call reads: this many of its commits, spread across it
# so the writer sees its beginning, middle and end, with an ask under the first few.
_HEADLINE_COMMITS = 8
_HEADLINE_ASKS = 3
# Per-call bounds. The outline call reads a lot and writes little; a moment is the reverse.
_MOMENT_TIMEOUT_SECONDS = 300
_EXPAND_TIMEOUT_SECONDS = 240
_ARC_TIMEOUT_SECONDS = 180
# How long a build may wait for the learn page's agent lock before giving up (the two pages
# share it so a laptop never runs two backend CLIs at once).
_LOCK_WAIT_SECONDS = 420

# What a moment can be about. The agent picks one; anything else falls back to "moment".
KICKERS = ("turning point", "feature", "fix", "refactor", "milestone", "experiment", "moment")

# How the story is told. Purely a prompt flavour, stored with the story so a rebuild in the
# same tone is reproducible and the page can show which one produced what you are reading.
STORY_TONES: dict[str, str] = {
    "plain": (
        "Tell it plainly and warmly, like the best kind of engineering write-up: concrete, "
        "specific, no hype. Interesting because of what actually happened, not because you "
        "dressed it up."
    ),
    "playful": (
        "Tell it with light humour and personality, like a developer telling a friend over "
        "coffee what happened. Funny because the details are, never because you exaggerated "
        "them."
    ),
    "epic": (
        "Tell it like a chronicle of a small expedition: a little grand, a little cinematic, "
        "but every claim still grounded in what the commits and prompts actually show."
    ),
}
DEFAULT_TONE = "playful"

# Appended when a reply came back unusable: a model that drifted into prose or invented ids
# usually gets it right when told exactly what went wrong.
_RETRY_NUDGE = (
    "\n\nIMPORTANT: your previous answer could not be used. Reply with ONE JSON object only, "
    'shaped {"moments": [...]}, and put in each moment\'s "shas" ONLY commit ids copied '
    "exactly from the material above."
)

_STORY_SYSTEM = (
    # The persona is the whole difference between a changelog and something someone reads to
    # the end. It is a documentary narrator, not a release-notes generator: it has read every
    # commit and every prompt, it knows what the people were trying to do, and it tells you
    # the part you would have wanted to be told.
    "You narrate the story of a software project the way a good documentary does: you have "
    "read every commit and every message its developers typed to their coding agent, and you "
    "tell the part a listener would actually want. You are curious about people, not "
    "impressed by machinery. "
    "How you write: short sentences. Everyday words. One concrete detail beats three general "
    "ones. Name the thing that broke, the assumption that turned out wrong, the small "
    "decision everything followed from. A number only when it lands ('twelve tries', '150 MB "
    "of transcript'). Never a sentence that would fit any project ('various improvements', "
    "'enhanced the system', 'this commit'), never a list of features, never marketing. "
    "You may be wry. You may not exaggerate: everything you say has to be in the material, "
    "and if something is unclear you say less rather than more. "
    # A story is written from what CHANGED, which quietly implies the changes are the whole
    # picture: "it now runs on Windows" reads as "it only runs on Windows" when Windows was
    # simply the platform that took work. What was already true and needed no work is
    # invisible in a commit log, so never let an addition sound like the whole of something.
    "The material is a record of what CHANGED, never of what the project already was: what "
    "worked from the start left no trace in it. So never let an addition sound like the "
    "whole picture. Say what was ADDED or FIXED ('gained a Windows installer', 'learned to "
    "resume a session'), never what the project apparently IS or is limited to ('it now "
    "runs on Windows', 'it only supports X'), and never imply that the part of it you can "
    "see in the commits is all of it. "
    # The people in these commits are real, and the material never states anyone's pronouns.
    # Guessing from a name or a writing style misgenders someone in a story about their own
    # work, which the neutral form never does.
    "The developers are real people whose pronouns you do not know: always write about them "
    "as 'they', or by the name in the commit, and never as 'he' or 'she'. "
    # What a reader wants from a project's story is what it was TRYING to do and how that
    # changed, which the commits only show indirectly. Left to itself the model narrates the
    # mechanism (the refactor, the flag, the cache) because that is what the diff is made of.
    "What the project was TRYING TO DO, and how that changed, is the story. The mechanism is "
    "only interesting when it shows the intent: a cache is not a moment, but 'they gave up on "
    "reading it live and decided to precompute it' is. Ask of every moment what the people "
    "wanted that they did not want before, or wanted more than they realised, and lead with "
    "that; the implementation is the evidence, not the subject. "
    "Do not use em-dashes anywhere in your output. "
    "You must reply with ONE JSON object and nothing else: no prose before or after it, no "
    "code fences."
)

# One build at a time per process (each spawns backend CLIs); plus the per-target registry.
_BUILDS: dict[str, dict[str, Any]] = {}
_BUILDS_LOCK = threading.Lock()
_STORE_LOCK = threading.Lock()

# The deterministic outline is recomputed per request; memoize it per (target, head) so a
# 2-second poll during a build does not re-segment the whole history every time.
_OUTLINE_CACHE: dict[tuple[str, str], list[dict]] = {}


class StoryError(RuntimeError):
    """The storyline could not be generated."""


# --------------------------------------------------------------------------- store


class StoryStore:
    """``.agitrack/story.json``: one story per branch, plus the settings that produced it.

    Local plumbing next to ``learning.json``, written atomically so an interrupted build
    can never truncate a story that already exists."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / ".agitrack" / "story.json"

    def load(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"stories": {}}
        if not isinstance(data, dict) or not isinstance(data.get("stories"), dict):
            return {"stories": {}}
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:  # keep .agitrack/ git-ignored even in a repo that never ran aGiTrack
            from agitrack.config.state import AgitrackState

            AgitrackState(self.root).ensure_local_ignore()
        except Exception:
            pass
        from agitrack.fileio import atomic_write_text

        atomic_write_text(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def get(self, key: str) -> dict[str, Any] | None:
        story = self.load().get("stories", {}).get(key or "HEAD")
        if not isinstance(story, dict):
            return None
        if "moments" not in story and "chapters" in story:
            story["moments"] = story.pop("chapters")  # written before moments were called moments
        # A story told when "look closer" existed carries a second, finer set of moments over
        # days the first set already covers. That feature is gone; keeping them would show the
        # same stretch twice in one list. Drop them, and let the coarse telling stand.
        moments = story.get("moments")
        if isinstance(moments, list) and any(int(m.get("level") or 1) > 1 for m in moments if isinstance(m, dict)):
            story["moments"] = [m for m in moments if isinstance(m, dict) and int(m.get("level") or 1) <= 1]
        return story

    def put(self, key: str, story: dict[str, Any]) -> None:
        with _STORE_LOCK:
            data = self.load()
            data.setdefault("stories", {})[key or "HEAD"] = story
            self.save(data)

    def drop(self, key: str) -> None:
        with _STORE_LOCK:
            data = self.load()
            data.setdefault("stories", {}).pop(key or "HEAD", None)
            self.save(data)

    def any_story(self) -> dict[str, Any] | None:
        """The one story to ship when the caller has no branch to ask for (the static
        export): the richest stored story, so a demo built on any branch shows content."""
        stories = [s for s in self.load().get("stories", {}).values() if isinstance(s, dict) and s.get("moments")]
        if not stories:
            return None
        return max(stories, key=lambda s: len(s.get("moments") or []))


# --------------------------------------------------------------------------- episodes


@dataclass
class Episode:
    """A contiguous run of commits that belong together in time: one sitting of work."""

    index: int
    stats: list[CommitStat] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)

    @property
    def start(self) -> int:
        return min((s.timestamp for s in self.stats if s.timestamp), default=0)

    @property
    def end(self) -> int:
        return max((s.timestamp for s in self.stats if s.timestamp), default=0)

    @property
    def shas(self) -> list[str]:
        return [s.sha for s in self.stats]

    @property
    def insertions(self) -> int:
        return sum(s.insertions for s in self.stats)

    @property
    def deletions(self) -> int:
        return sum(s.deletions for s in self.stats)

    @property
    def tokens(self) -> int:
        return sum(_turn_tokens(s) for s in self.stats)

    @property
    def ai_turns(self) -> int:
        return sum(1 for s in self.stats if s.kind in _AI_KINDS)


def _turn_tokens(stat: CommitStat) -> int:
    """The commit's headline token count: what the model produced plus what it read fresh.
    Cache reads are deliberately excluded (they dwarf everything and say nothing about the
    work), and an anomalous commit contributes nothing at all."""
    if stat.token_anomaly:
        return 0
    tokens = stat.tokens or {}
    return int(tokens.get("input", 0) or 0) + int(tokens.get("output", 0) or 0)


def story_stats(stats: list[CommitStat]) -> list[CommitStat]:
    """The commits a story is told from, oldest first: everything but aGiTrack's own
    housekeeping, and nothing without a timestamp to place it on a timeline."""
    kept = [stat for stat in stats if stat.kind not in _SKIP_KINDS and stat.timestamp]
    return sorted(kept, key=lambda stat: (stat.timestamp, stat.sha))


def segment_episodes(stats: list[CommitStat], sha_paths: dict[str, set[str]] | None = None) -> list[Episode]:
    """Group ``stats`` (any order) into episodes: contiguous runs of commits separated by a
    real pause in the work, and never longer than :data:`_EPISODE_MAX_COMMITS`.

    This is the deterministic half of the pipeline. It costs no agent call, it is stable
    across rebuilds, and it is what keeps each agent prompt small and well scoped."""
    ordered = story_stats(stats)
    episodes: list[Episode] = []
    current: Episode | None = None
    previous_ts = 0
    for stat in ordered:
        gap = stat.timestamp - previous_ts if previous_ts else 0
        if current is None or gap > _EPISODE_GAP_SECONDS or len(current.stats) >= _EPISODE_MAX_COMMITS:
            current = Episode(index=len(episodes))
            episodes.append(current)
        current.stats.append(stat)
        previous_ts = stat.timestamp
    if sha_paths:
        for episode in episodes:
            seen: dict[str, int] = {}
            for stat in episode.stats:
                for path in sha_paths.get(stat.sha, ()):  # type: ignore[union-attr]
                    seen[path] = seen.get(path, 0) + 1
            episode.paths = [path for path, _ in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))[:8]]
    return episodes


def merge_episodes(group: list[Episode], index: int) -> Episode:
    """Fold several sittings of work into one, so a single telling can cover a wide span
    without reading every sitting in it."""
    merged = Episode(index=index)
    for episode in group:
        merged.stats.extend(episode.stats)
    seen: dict[str, int] = {}
    for episode in group:
        for path in episode.paths:
            seen[path] = seen.get(path, 0) + 1
    merged.paths = [path for path, _ in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))[:8]]
    return merged


def spread(episodes: list[Episode], count: int) -> list[Episode]:
    """``count`` evenly spread groups covering ALL of ``episodes``, oldest first.

    This is what makes a telling cover its whole date range instead of the first stretch of
    it: rather than taking the first N sittings and stopping, every sitting belongs to one of
    N groups, and each group becomes one moment."""
    if not episodes:
        return []
    count = max(1, min(count, len(episodes)))
    items = [{"from": episode.start, "to": episode.end} for episode in episodes]
    return [
        merge_episodes(episodes[start : end + 1], number)
        for number, (start, end) in enumerate(act_slices(items, count))
    ]


def _batches(episodes: list[Episode]) -> list[list[Episode]]:
    """Episodes grouped into agent calls: a few episodes each, inside the digest budget."""
    out: list[list[Episode]] = []
    current: list[Episode] = []
    size = 0
    for episode in episodes:
        # Measured against the OUTLINE line, which is what the call actually sends: sizing
        # batches by the full per-commit digest made them six episodes wide and left most of
        # the history uncovered.
        cost = len(_episode_headline(episode))
        if current and (len(current) >= _BATCH_EPISODES or size + cost > _BATCH_CHARS):
            out.append(current)
            current, size = [], 0
        current.append(episode)
        size += cost
    if current:
        out.append(current)
    return out


# --------------------------------------------------------------------------- digest


def _day(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "unknown date"


def _stamp(ts: int) -> str:
    """Date AND time. The writer cannot put things in the order they happened if all it is
    given is the day: a moment came back titled "storyline lands, the TUI stops scrolling
    wrong, and a release patch fails" with the release patch, which came first, told last."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "unknown time"


def _span(start: int, end: int) -> str:
    """``start`` to ``end``, dropping the repeated date when both fall on one day."""
    first, last = _stamp(start), _stamp(end)
    if first == last:
        return first
    return f"{first} to {last[11:] if last[:10] == first[:10] else last}"


def _sample(stats: list[CommitStat], limit: int) -> tuple[list[CommitStat], int]:
    """At most ``limit`` commits, EVENLY spread over the run and always keeping the first and
    the last, plus how many were left out.

    A merged sitting can hold a hundred commits; showing its first ten would tell the writer
    about its morning and nothing about its evening."""
    if len(stats) <= limit:
        return list(stats), 0
    step = (len(stats) - 1) / (limit - 1)
    picked: list[CommitStat] = []
    for position in range(limit):
        stat = stats[round(position * step)]
        if not picked or picked[-1] is not stat:
            picked.append(stat)
    return picked, len(stats) - len(picked)


def _shorten(text: str, limit: int) -> str:
    """``text`` capped at ``limit``, ending where a reader would: at a SENTENCE end if there
    is one in range, else at a word end. Never mid-word, and never mid-sentence when a
    sentence boundary is available.

    This matters most for the quoted asks, which show what the developer actually
    typed: a quote that stops at "please check why and avoi" reads like the tool broke."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    end = max(window.rfind(mark) for mark in (". ", "? ", "! ", ".\n", "?\n", "!\n", "; "))
    if end >= limit // 3:  # a real sentence ends in range: stop there, whole
        return window[: end + 1].rstrip()
    space = window.rfind(" ")
    cut = window[:space] if space > limit // 2 else window
    return cut.rstrip(" ,;:.") + "…"


def _prompts_of(stat: CommitStat, limit: int = 2, chars: int = 220) -> list[str]:
    """The developer's own words behind this commit, cleaned and trimmed at a sentence end
    (see :func:`_shorten`) so a quote never breaks off mid-word."""
    out: list[str] = []
    for raw in [*(stat.user_prompts or []), stat.prompt or ""]:
        text = learn_page._clean_prompt(raw)
        if not text or text in out:
            continue
        out.append(_shorten(text, chars))
        if len(out) >= limit:
            break
    return out


def _episode_headline(episode: Episode, number: int = 0) -> str:
    """One episode for the outline pass: its span, and the commits inside it IN THE ORDER
    THEY HAPPENED, each stamped with date and time.

    It used to be a single line carrying only the day and the episode's biggest subject, and
    the writer had nothing to order the rest of it by - so a moment covering a long sitting
    would name three things in whatever order they came to mind, with the earliest last.
    Commit ids are deliberately absent: a moment is one episode now (see
    :func:`_batch_prompt`), so which commits belong to it is not the writer's to guess."""
    number = number or episode.index + 1
    lines = [
        f"EPISODE {number} [{_span(episode.start, episode.end)}] "
        f"{len(episode.stats)} commits, +{episode.insertions}/-{episode.deletions} lines"
    ]
    if episode.paths:
        lines.append("  files: " + ", ".join(episode.paths))
    shown, dropped = _sample(episode.stats, _HEADLINE_COMMITS)
    asks = 0
    for stat in shown:
        lines.append(f"  {_stamp(stat.timestamp)}  {stat.subject[:100]}")
        if asks < _HEADLINE_ASKS:
            for prompt in _prompts_of(stat, limit=1, chars=140):
                lines.append(f'      asked: "{prompt}"')
                asks += 1
    if dropped:
        lines.append(f"  (+{dropped} more commits spread through the same stretch)")
    return "\n".join(lines) + "\n"


def _note_line(note: str) -> str:
    """The reader's own instructions, weighed heavily. They asked for this story; a request
    for a focus or a style should beat the default voice."""
    note = (note or "").strip()[:300]
    return f'\n\nThe reader asked for this specifically: "{note}". Follow it closely.' if note else ""


def _batch_prompt(
    batch: list[Episode], tone: str, context: str, note: str = "", *, from_the_start: bool = False
) -> str:
    digest = "".join(_episode_headline(episode, number) for number, episode in enumerate(batch, start=1))
    voice = STORY_TONES.get(tone, STORY_TONES[DEFAULT_TONE])
    prior = f"\nEARLIER IN THE STORY (do not retell these, continue from them):\n{context}\n" if context else ""
    voice += _note_line(note)
    # A repo's FIRST moment is the one place the material can answer "what was this for?", and
    # it is what a reader opening a story wants first. Episode 1 here is genuinely the start of
    # the project only when this telling reaches the very first commit.
    opening = (
        "\nEpisode 1 is the BEGINNING OF THIS PROJECT: the first commits that exist. Its moment "
        "must say what the project set out to do — what its author wanted that nothing they had "
        "already did — reading it out of those first commits and the messages behind them. Say "
        "so plainly; that is the frame every later moment is read against. If the material "
        "genuinely does not show the intent, say what the first work was aimed at and no more.\n"
        if from_the_start
        else ""
    )
    return f"""Below are {len(batch)} consecutive episodes from one repository's history, OLDEST FIRST. Each is a stretch of work with its span, and the commits inside it listed in the order they were made, every one stamped with its date and time.

Write ONE moment of the story for EACH episode, in the same order: {len(batch)} moments, numbered as they are numbered here. The grouping is already done; do not merge episodes and do not split one.

Everything inside an episode is in chronological order, and what you write must follow it. When a moment names more than one thing, name them in the order they happened: the timestamps tell you which came first. Getting this backwards is the one error a reader always catches, because they were there.

{voice}

Write it so someone who has never seen this repo WANTS to read the next moment. What makes it interesting is always a specific fact: the thing that broke, the assumption that turned out wrong, the small decision everything else followed from. Never filler ("various improvements", "several fixes", "this commit"), never a category where a fact would do.

Follow the GOALS, not the machinery. Each moment should answer "what were they trying to do here, and how had that changed since the last one?" — a feature that arrived because they wanted something they did not want before, a plan abandoned, a problem that turned out to be the real one. Name an implementation detail only when it IS the point (the decision that made the difference), never as the subject of the moment.
{opening}
For each moment:
- "n": the number of the episode it tells, exactly as listed below.
- "title": the thing that actually happened, named. Specific enough that it could only belong to this moment. Never a category like "Improvements", "Bug fixes" or "Dashboard work". When the episode holds several strands, lead with the one that mattered most and keep the rest in time order.
- "kicker": one of {", ".join(KICKERS)}.
- "emoji": a single emoji that fits the moment.
- "summary": ONE short sentence (under 20 words) with a hook: what was wrong, what got decided, or what surprised them. A reader skimming only the summaries should still get the story.

Headlines only here: the body of a moment is written later, and only if someone zooms into it.
{prior}
Reply with ONE JSON object: {{"moments": [ ... ]}}

MATERIAL:
{digest}"""


def _expand_prompt(
    moment: dict, stats: list[CommitStat], sha_paths: dict[str, set[str]], tone: str, note: str = ""
) -> str:
    """Write out ONE moment, given everything that happened in it. This is the call a reader
    pays for by opening the moment, so it sees the full material: every commit subject, the
    prompts behind them, and the files they touched."""
    voice = STORY_TONES.get(tone, STORY_TONES[DEFAULT_TONE]) + _note_line(note)
    lines = []
    paths: dict[str, int] = {}
    for stat in sorted(stats, key=lambda stat: (stat.timestamp, stat.sha)):
        lines.append(
            f"{_stamp(stat.timestamp)} {stat.short} [{stat.kind}] "
            f"{stat.subject[:120]} (+{stat.insertions}/-{stat.deletions})"
        )
        for prompt in _prompts_of(stat, limit=2, chars=260):
            lines.append(f'    asked: "{prompt}"')
        for path in sha_paths.get(stat.sha, ()) or ():
            paths[path] = paths.get(path, 0) + 1
    material = "\n".join(lines)[:9000]
    top = ", ".join(path for path, _ in sorted(paths.items(), key=lambda kv: (-kv[1], kv[0]))[:10])
    return f"""Someone just zoomed into one moment of a repository's story. Its headline is already written; do not change it. Tell them what they came for.

MOMENT: "{moment.get("title", "")}" ({moment.get("kicker", "moment")}, {moment.get("when", "")})
SUMMARY ALREADY SHOWN TO THE READER: {moment.get("summary", "")}

{voice}

Reply with ONE JSON object:
{{"detail": "2 or 3 SHORT sentences, one paragraph, no headings and no lists. The numbers are already on the page, so spend the words on WHY: the problem or the want (the prompts show it), what changed, naming a real file or feature. Never repeat the summary, and never list what the reader can see.",
 "thoughts": [{{"sha": "<a commit id from the material below>", "note": "one short sentence on what the developer was working out at that moment"}}]}}

Give 1 or 2 thoughts, picking the commits whose prompts show the thinking best. Never invent a quote: only the commit id and your note.

The material below is OLDEST FIRST, each commit stamped with its date and time. Tell it in that order: if you name two things, the earlier one comes first, and never say something led to what preceded it.

FILES TOUCHED: {top}

MATERIAL:
{material}"""


def act_slices(items: list[dict], target: int = 5) -> list[tuple[int, int]]:
    """Split dated items into contiguous eras, as ``(start, end)`` index pairs.

    The BOUNDARIES are decided here, not by the agent. Asking a model to both group and name
    left one act covering 26 of 44 moments and titled after the first three of them. Even
    spans, nudged onto the biggest pause between items so an era starts where the work
    actually did, are always defensible; naming is all the agent is asked for."""
    total = len(items)
    if total < 4:
        return [(0, total - 1)] if total else []
    count = max(2, min(target, max(2, total // 2)))
    step = total / count
    cuts: list[int] = []
    for index in range(1, count):
        ideal = round(index * step)
        window = [pos for pos in range(max(1, ideal - 2), min(total - 1, ideal + 3))]
        best = max(window, key=lambda pos: items[pos].get("from", 0) - items[pos - 1].get("to", 0))
        if not cuts or best > cuts[-1]:
            cuts.append(best)
    bounds = [0, *cuts, total]
    return [(bounds[i], bounds[i + 1] - 1) for i in range(len(bounds) - 1)]


def part_target(episodes: list[Episode]) -> int:
    """How many parts this history deserves.

    A fortnight of work is not five acts, and a two-year project is not five either. Two
    signals: elapsed time (about a part per three weeks) and how much actually happened
    (about one per twelve sittings of work), combined and bounded at 2 and 8.

    HOW MUCH THERE IS TO READ counts double the calendar. Both signals are needed - a quiet
    year is still a small story, and a dense fortnight is not one part - but weighting them
    equally made the SAME project read very differently depending on how much of it the
    reader could see: a backtrace, which only reaches as far back as the local transcripts
    do, covered 57% of this repo's days and 43% of its sittings yet came out with 2 parts
    against the live dashboard's 5. Leaning on volume brings the two readings together (3
    against 6 here) without flattening a long, slow history, which is what a volume-only
    rule would do."""
    if len(episodes) < 4:
        return max(1, len(episodes))
    days = max(1, (episodes[-1].end - episodes[0].start) / 86400)
    by_time = max(days / 21, 0.1)
    by_volume = max(len(episodes) / 12, 0.1)
    return max(2, min(8, round(by_time ** (1 / 3) * by_volume ** (2 / 3))))


def _era_rows(episodes: list[Episode]) -> list[dict]:
    """The coarse story's skeleton: the whole history split into eras, with the numbers that
    describe each one. Computed from the episodes themselves, so it exists (and is complete)
    however few moments have been written."""
    items = [{"from": episode.start, "to": episode.end} for episode in episodes]
    rows = []
    for start, end in act_slices(items, part_target(episodes)):
        span = episodes[start : end + 1]
        rows.append(
            {
                "from": span[0].start,
                "to": span[-1].end,
                "when": _day(span[0].start),
                "until": _day(span[-1].end),
                "commits": sum(len(episode.stats) for episode in span),
                "ins": sum(episode.insertions for episode in span),
                "del": sum(episode.deletions for episode in span),
                "turns": sum(episode.ai_turns for episode in span),
                # One bar per episode: the shape of the work inside the era.
                "spark": [episode.insertions + episode.deletions for episode in span],
                # Oldest first, each dated: an era's name has to fit the whole of it, and
                # naming it after the last thing read is how "it now runs on Windows" ends up
                # over a stretch that was mostly about something else.
                "subjects": [
                    f"{_day(episode.start)}: "
                    + max(episode.stats, key=lambda stat: (stat.lines, stat.timestamp)).subject[:70]
                    for episode in span
                ],
            }
        )
    return rows


def _overview_prompt(eras: list[dict], tone: str, repo_name: str, note: str = "") -> str:
    """Name the whole timeline in ONE call. It reads only headline subjects per era, so its
    cost does not grow with how much of the story has been written out."""
    voice = STORY_TONES.get(tone, STORY_TONES[DEFAULT_TONE]) + _note_line(note)
    blocks = []
    for number, era in enumerate(eras, start=1):
        subjects = "; ".join(era["subjects"])[:1200]
        blocks.append(f"ERA {number} ({era['when']} to {era['until']}, {era['commits']} commits): {subjects}")
    listing = "\n\n".join(blocks)[:7000]
    return f"""Below is the whole history of a repository called "{repo_name}", split into eras, each listed with the headline of every sitting of work inside it.

{voice}

Name them:
- "title": a title for the project's story so far. Short, specific to THIS project, memorable; the kind of title someone would click. Not a slogan, not "The Story of X".
- "tagline": one sentence under the title that makes someone want to read on.
- "arc": TWO short sentences on the overall journey: where it started and where it stands now.
- "eras": one entry per era above, each {{"n": <its number>, "title": "a few words naming what that era was about", "blurb": "one short sentence"}}.

Name an era for what the project GAINED in it: the capabilities and features that arrived, the thing it could do afterwards that it could not do before. Housekeeping is not a headline: a rename, a version bump, a lint pass, a docs pass, a CI tweak or a refactor belongs in an era's name only if there was genuinely nothing else in it. The name must fit EVERYTHING in that era, not just its first or largest entry.

Reply with ONE JSON object with exactly those four keys.

ERAS:
{listing}"""


# --------------------------------------------------------------------------- normalising

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(value: str, fallback: str) -> str:
    slug = _SLUG_RE.sub("-", str(value or "").lower()).strip("-")
    return (slug or fallback)[:60]


def _emoji(value: object) -> str:
    """One display glyph, or a default. Anything with markup, quotes or ASCII letters is
    not an emoji the model was asked for, so it is refused rather than rendered."""
    text = str(value or "").strip()
    if not text or len(text) > 4 or re.search(r"[a-zA-Z<>&\"'\\/]", text):
        return "✦"  # ✦
    return text[:2]


def _text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _number(value: object) -> int:
    """An episode number as the reply gave it: 3, "3", "EPISODE 3" and 3.0 all mean three."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.search(r"\d+", str(value or ""))
    return int(digits.group()) if digits else 0


def _resolve_shas(raw: object, by_sha: dict[str, CommitStat]) -> list[str]:
    """The full shas named by ``raw`` that really exist in this build's material, in the
    order given, without duplicates. A model that invents or mangles an id loses it."""
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        stat = by_sha.get(str(item or "").strip().lower())
        if stat is not None and stat.sha not in out:
            out.append(stat.sha)
    return out


def _thoughts(raw: object, by_sha: dict[str, CommitStat], claimed: list[str]) -> list[dict]:
    """What the developer asked for: their REAL prompt at each pivotal commit, paired
    with the agent's one-line reading of it.

    The quote is never taken from the model. It is read back from the commit the model
    pointed at, so what the page shows is always something the developer actually typed."""
    out: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        stat = by_sha.get(str(item.get("sha") or "").strip().lower())
        if stat is None or stat.sha in [t["sha"] for t in out]:
            continue
        quotes = _prompts_of(stat, limit=1, chars=600)
        if not quotes:
            continue
        out.append(
            {
                "sha": stat.sha,
                "short": stat.short,
                "quote": quotes[0],
                "note": _text(item.get("note"), 300),
                "at": stat.timestamp,
            }
        )
        if len(out) >= 3:
            break
    if out:
        return out
    # The model named nothing usable: fall back to the most prompt-heavy commit of the
    # moment, so a moment still shows the developer's voice rather than an empty section.
    for sha in claimed:
        stat = by_sha.get(sha)
        quotes = _prompts_of(stat, limit=1, chars=600) if stat is not None else []
        if stat is not None and quotes:
            return [{"sha": stat.sha, "short": stat.short, "quote": quotes[0], "note": "", "at": stat.timestamp}]
    return []


def _moment_stats(shas: list[str], by_sha: dict[str, CommitStat], sha_paths: dict[str, set[str]]) -> dict:
    stats = [by_sha[sha] for sha in shas if sha in by_sha]
    paths: dict[str, int] = {}
    for stat in stats:
        for path in sha_paths.get(stat.sha, ()) or ():
            paths[path] = paths.get(path, 0) + 1
    return {
        "commits": len(stats),
        "ins": sum(stat.insertions for stat in stats),
        "del": sum(stat.deletions for stat in stats),
        "tokens": sum(_turn_tokens(stat) for stat in stats),
        "turns": sum(1 for stat in stats if stat.kind in _AI_KINDS),
        "files": [path for path, _ in sorted(paths.items(), key=lambda kv: (-kv[1], kv[0]))[:8]],
        "files_total": len(paths),
    }


def _commit_rows(shas: list[str], by_sha: dict[str, CommitStat]) -> list[dict]:
    rows = []
    for sha in shas:
        stat = by_sha.get(sha)
        if stat is None:
            continue
        rows.append(
            {
                "sha": stat.sha,
                "short": stat.short,
                "subject": stat.subject,
                "kind": stat.kind,
                "ts": stat.timestamp,
                "ins": stat.insertions,
                "del": stat.deletions,
                "backend": stat.backend or "",
                "model": stat.model or "",
                # Who and when, so a commit here reads exactly like one in the dashboard log.
                "author": stat.author,
                "tokens": dict(stat.tokens or {}),
            }
        )
    return rows


def _normalize_moments(
    raw: object,
    batch: list[Episode],
    by_sha: dict[str, CommitStat],
    sha_paths: dict[str, set[str]],
    used_ids: set[str],
) -> list[dict]:
    """Validate one agent reply into moments, and make sure the batch's commits are all
    accounted for: an unclaimed commit is folded into the nearest moment in time rather
    than quietly vanishing from the story."""
    batch_shas = [sha for episode in batch for sha in episode.shas]
    allowed = {sha for sha in batch_shas}
    # A moment is an episode (the prompt asks for one per episode, by number), so which
    # commits it holds is decided here, from the segmentation, rather than copied back by
    # the writer. An id it mistypes or invents cannot cost a commit its place in the story.
    by_number = {number: episode for number, episode in enumerate(batch, start=1)}
    moments: list[dict] = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        episode = by_number.get(_number(item.get("n")))
        shas = (
            [sha for sha in episode.shas if sha in allowed]
            if episode is not None
            # Older replies (and older stored stories) name their commits instead.
            else [sha for sha in _resolve_shas(item.get("shas"), by_sha) if sha in allowed]
        )
        title = _text(item.get("title"), 110)
        if not shas or not title:
            continue
        kicker = str(item.get("kicker") or "").strip().lower()
        moment = {
            "id": _unique_id(_slug(item.get("id") or title, f"moment-{index}"), used_ids),
            "title": title,
            "kicker": kicker if kicker in KICKERS else "moment",
            "emoji": _emoji(item.get("emoji")),
            "summary": _text(item.get("summary"), 200),
            # A moment is a paragraph, not an essay: the commits under it carry the detail.
            "detail": _shorten(str(item.get("detail") or item.get("detail_md") or ""), 600),
            "shas": shas,
            # Resolved once the moment's final commit list is known (below), so a fallback
            # quote can come from a commit that was folded in afterwards.
            "_thoughts": item.get("thoughts"),
        }
        moments.append(moment)
    if not moments:
        return []
    _absorb_unclaimed(moments, batch_shas, by_sha)
    for moment in moments:
        moment["shas"] = sorted(moment["shas"], key=lambda sha: (by_sha[sha].timestamp, sha))
        _finalize_moment(moment, by_sha, sha_paths)
    moments.sort(key=lambda moment: (moment["from"], moment["to"]))
    return moments


def _fallback_moments(
    batch: list[Episode], by_sha: dict[str, CommitStat], sha_paths: dict[str, set[str]], used_ids: set[str]
) -> list[dict]:
    """Plain moments written from the commits alone, with no agent involved.

    Used when a reply comes back unusable twice: one bad answer in the middle of a long
    build must not throw away the moments around it, and a gap in the timeline would be
    worse than a plainly-told stretch. Marked ``auto`` so the page can say who wrote it."""
    moments = []
    for episode in batch:
        headline = max(episode.stats, key=lambda stat: (stat.lines, stat.timestamp))
        subjects = [stat.subject for stat in episode.stats if stat.subject][:6]
        moment = {
            "id": _unique_id(_slug(headline.subject, f"episode-{episode.index}"), used_ids),
            "title": headline.subject[:110] or f"{len(episode.stats)} commits",
            "kicker": "moment",
            "emoji": "✦",
            "auto": True,
            "summary": (
                f"{len(episode.stats)} commits, +{episode.insertions}/-{episode.deletions} lines. "
                "Told from the commits themselves: the storyteller could not be reached for this stretch."
            ),
            "detail": "\n".join(f"- {subject}" for subject in subjects),
            "shas": list(episode.shas),
            "_thoughts": None,
        }
        _finalize_moment(moment, by_sha, sha_paths)
        moments.append(moment)
    return moments


def _unique_id(base: str, used: set[str]) -> str:
    candidate, suffix = base, 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _absorb_unclaimed(moments: list[dict], batch_shas: list[str], by_sha: dict[str, CommitStat]) -> None:
    """Give every commit of the batch a home: unclaimed ones join the moment closest in
    time. Without this a moment's "3 commits" could silently omit real work."""
    claimed = {sha for moment in moments for sha in moment["shas"]}
    for sha in batch_shas:
        if sha in claimed:
            continue
        when = by_sha[sha].timestamp
        nearest = min(
            moments,
            key=lambda moment: min(abs(when - by_sha[other].timestamp) for other in moment["shas"]),
        )
        nearest["shas"].append(sha)


def _finalize_moment(moment: dict, by_sha: dict[str, CommitStat], sha_paths: dict[str, set[str]]) -> None:
    # The outline pass leaves these empty: a moment is written out the first time it is
    # opened (expand_moment), so nobody waits for prose nobody has asked to read.
    raw_thoughts = moment.pop("_thoughts", None)
    moment["thoughts"] = _thoughts(raw_thoughts, by_sha, moment["shas"]) if raw_thoughts else []
    times = [by_sha[sha].timestamp for sha in moment["shas"] if sha in by_sha]
    moment["from"] = min(times) if times else 0
    moment["to"] = max(times) if times else 0
    moment["when"] = _day(moment["from"])
    moment["stats"] = _moment_stats(moment["shas"], by_sha, sha_paths)
    moment["commits"] = _commit_rows(moment["shas"], by_sha)


# --------------------------------------------------------------------------- outline


def outline(stats: list[CommitStat], sha_paths: dict[str, set[str]] | None = None, *, limit: int = 60) -> list[dict]:
    """The story's shape WITHOUT any agent call: the newest ``limit`` episodes, each
    labelled with its own biggest commit.

    This is what the page shows before anything is generated (and everywhere no backend is
    available at all), so the timeline is never an empty page with a button on it."""
    episodes = segment_episodes(stats, sha_paths)
    rows = []
    for episode in episodes[-limit:]:
        headline = max(episode.stats, key=lambda stat: (stat.lines, stat.timestamp))
        rows.append(
            {
                "index": episode.index,
                "when": _day(episode.start),
                "from": episode.start,
                "to": episode.end,
                "title": _shorten(headline.subject, 110) or f"{len(episode.stats)} commits",
                "commits": len(episode.stats),
                "turns": episode.ai_turns,
                "ins": episode.insertions,
                "del": episode.deletions,
                "tokens": episode.tokens,
                "files": episode.paths,
                "prompt": (_prompts_of(headline, limit=1, chars=240) or [""])[0],
            }
        )
    rows.reverse()  # newest first, like the dashboard's log
    return rows


# --------------------------------------------------------------------------- building


def _progress(key: str) -> dict[str, Any] | None:
    with _BUILDS_LOCK:
        record = _BUILDS.get(key)
        return dict(record["progress"]) if record else None


def _set_progress(key: str, **fields: Any) -> None:
    with _BUILDS_LOCK:
        record = _BUILDS.get(key)
        if record:
            record["progress"].update(fields)


def _is_live(record: dict[str, Any]) -> bool:
    """Whether a registered build is still one the reader is waiting for. An abandoned one
    (Stop pressed, or the page that started it went away) is not, however long its last
    backend call takes to return."""
    thread = record.get("thread")
    return bool(thread and thread.is_alive()) and not record.get("abandoned") and not record["stop"].is_set()


def build_key(root: Path, branch: str) -> str:
    return f"{Path(root)}|{branch or 'HEAD'}"


def _ask(choice, prompt: str, timeout: int) -> dict | None:
    """One agent call, serialized against the learn page's calls (a shared lock, so a laptop
    never runs two backend CLIs at once).

    Returns the JSON object it replied with, or **None when the reply was not usable JSON**:
    that is a bad roll, worth another try, not a broken backend. A backend that actually
    fails (missing CLI, non-zero exit, timeout) raises :class:`StoryError` instead."""
    if not learn_page._AGENT_LOCK.acquire(timeout=_LOCK_WAIT_SECONDS):
        raise StoryError("The learning agent is busy; try again in a moment.")
    try:
        text = learn_page._run_agent(choice, _STORY_SYSTEM, prompt, timeout)
    except learn_page.LearnAgentError as exc:
        raise StoryError(str(exc)) from exc
    finally:
        learn_page._AGENT_LOCK.release()
    return learn_page._extract_json(text)


def _material(
    stats: list[CommitStat], story: dict | None, mode: str, part_id: str = ""
) -> tuple[list[CommitStat], str]:
    """Which commits this build tells, and where their moments go.

    EVERY telling is scoped to a PART, and covers that part end to end (the caller spreads
    the sittings across it). A first telling that took "the newest ten sittings" produced
    moments bunched at one end of a part, which is exactly what the reader then had to page
    through to escape.

    * ``rewrite`` - the newest part; replaces the story.
    * ``part`` - the named part.
    * ``extend`` - what landed after the story's newest covered commit; appends.

    Reaching further BACK used to be a mode of its own, behind a "go further back" button.
    It was removed: nobody could tell what it would give them, and choosing the days to tell
    (the range control) says the same thing precisely.
    """
    kept = story_stats(stats)
    if not kept:
        return [], "replace"
    covered = set(story.get("covered_shas") or []) if story else set()
    # A REWRITE re-reads the history as it stands now, so its parts are computed fresh. The
    # stored ones were drawn when the story was last told and their newest part ENDS at the
    # then-newest commit, so everything committed since fell outside every part and a rewrite
    # silently left it out. Telling one named part still uses the stored parts: that is the
    # part the reader clicked on, and its identity has to keep meaning what it meant.
    eras = _era_rows(segment_episodes(kept)) if mode == "rewrite" else ((story or {}).get("eras") or [])
    if not eras:
        eras = _era_rows(segment_episodes(kept))

    def inside(era: dict, *, fresh_only: bool) -> list[CommitStat]:
        return [
            stat
            for stat in kept
            if era.get("from", 0) <= stat.timestamp <= era.get("to", 0) and (not fresh_only or stat.sha not in covered)
        ]

    if story is None or mode == "rewrite":
        # The newest part, told across the whole of it. Everything older is one click away.
        return (inside(eras[-1], fresh_only=False) if eras else kept), "replace"
    if mode == "part":
        era = next((row for row in eras if row.get("id") == part_id), None)
        if era is None:
            return [], "append"
        return inside(era, fresh_only=True), "merge"
    newest = max((moment.get("to", 0) for moment in story.get("moments") or []), default=0)
    return [stat for stat in kept if stat.sha not in covered and (not newest or stat.timestamp >= newest)], "append"


_EMPTY_RANGE = (
    "There are no commits in those days on this branch, so there is no story to tell about "
    "them. Widen the range, or pick 'all time'."
)


def _epoch(value: object) -> int:
    """A timestamp from the page: seconds, or 0 for "no bound"."""
    try:
        seconds = int(float(value or 0))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return seconds if seconds > 0 else 0


def in_range(stats: list[CommitStat], since: int = 0, until: int = 0) -> list[CommitStat]:
    """``stats`` narrowed to the days the reader asked for (either bound may be 0 = open).

    The range scopes a TELLING, not a view: everything downstream - the parts, how many of
    them there are, which commits count as covered - is computed from what this returns, so a
    story of one week is that week's own story rather than a week highlighted inside a year."""
    return [stat for stat in stats if (not since or stat.timestamp >= since) and (not until or stat.timestamp <= until)]


def uncovered_count(stats: list[CommitStat], story: dict | None) -> int:
    """How many commits an ``extend`` would actually tell: what landed after the story's
    newest covered commit. Not simply "commits the story does not cover", which would put a
    number on a button that then had nothing to do."""
    if not story:
        return len(story_stats(stats))
    return len(_material(stats, story, "extend")[0])


def build_story(
    root: Path,
    stats: list[CommitStat],
    sha_paths: dict[str, set[str]],
    *,
    branch: str = "",
    tone: str = DEFAULT_TONE,
    mode: str = "extend",
    repo_name: str = "",
    note: str = "",
    part_id: str = "",
    since: int = 0,
    until: int = 0,
    key: str = "",
    stop: threading.Event | None = None,
) -> dict[str, Any]:
    """Generate (or extend) the story for ``branch`` and persist it. Blocking: this is what
    the background worker runs. Moments are saved as each batch lands, so the page shows
    the story appearing rather than a spinner that ends in everything at once."""
    store = StoryStore(root)
    story_key = branch or "HEAD"
    existing = store.get(story_key)
    tone = tone if tone in STORY_TONES else DEFAULT_TONE
    by_sha: dict[str, CommitStat] = {}
    for stat in story_stats(stats):
        by_sha[stat.sha] = stat
        by_sha[stat.short] = stat
    material, placement = _material(stats, existing, mode, part_id)
    if not material:
        raise StoryError("There is nothing new to tell: the story already covers this branch.")
    # Segment the material itself, not the whole history: an episode is a sitting of work
    # among the commits this build is actually telling.
    selected = segment_episodes(material, sha_paths)

    # A telling COVERS its material rather than starting at one end of it: the sittings are
    # spread into as many groups as there are moments to write, and each group becomes one
    # moment. Taking the first N sittings instead left the rest of a part untold and made
    # "show earlier moments" the only way to see the middle of your own history.
    selected = spread(selected, _MOMENTS_COARSE)
    # Does this telling reach the repo's very first commit? Only then does its opening moment
    # get to say what the project set out to do (see _batch_prompt), and only then is it true:
    # a story of the newest part, or of a picked range of days, starts in the middle.
    everything = story_stats(stats)
    from_the_start = bool(everything) and material[0].sha == everything[0].sha
    all_batches = _batches(selected)
    batches = all_batches[:_MAX_BATCHES] if placement in ("append", "merge") else all_batches[-_MAX_BATCHES:]
    left_behind = len(all_batches) - len(batches)
    choice = learn_page.resolve_learning_backend(Path(root))
    if key:
        _set_progress(key, phase="reading the commits", done=0, total=len(batches) + 1, moments=0)

    kept = list(existing.get("moments") or []) if (existing and placement != "replace") else []
    used_ids = {str(moment.get("id") or "") for moment in kept}
    produced: list[dict] = []
    context = ""
    if placement == "append" and kept:
        context = "\n".join(f"- {moment.get('title', '')}: {moment.get('summary', '')}" for moment in kept[-4:])

    for number, batch in enumerate(batches, start=1):
        if stop is not None and stop.is_set():
            raise StoryError("cancelled")
        if key:
            _set_progress(key, phase=f"writing moments ({number} of {len(batches)})", done=number - 1)
        prompt = _batch_prompt(batch, tone, context, note, from_the_start=from_the_start and number == 1)
        moments: list[dict] = []
        for attempt in range(2):
            if stop is not None and stop.is_set():
                raise StoryError("cancelled")
            raw = _ask(choice, prompt if attempt == 0 else prompt + _RETRY_NUDGE, _MOMENT_TIMEOUT_SECONDS)
            if stop is not None and stop.is_set():
                raise StoryError("cancelled")  # the reader stopped while this was in flight
            moments = _normalize_moments((raw or {}).get("moments"), batch, by_sha, sha_paths, used_ids)
            if moments:
                break
        if not moments:
            # Two unusable answers about this stretch. Tell it plainly from the commits and
            # keep going: one bad reply must not cost the whole build.
            moments = _fallback_moments(batch, by_sha, sha_paths, used_ids)
        for moment in moments:  # which part this telling belongs to
            if part_id:
                moment["part"] = part_id
        produced.extend(moments)
        context = "\n".join(f"- {moment['title']}: {moment['summary']}" for moment in produced[-4:])
        if key:
            _set_progress(key, done=number, moments=len(kept) + len(produced))
        # Persist as we go: a build interrupted after batch 3 leaves 3 real moments, not
        # nothing, and the page renders them the moment they exist.
        story = _assemble(
            kept, produced, placement, story_key, tone, note, existing, by_sha, choice, left_behind, partial=True
        )
        store.put(story_key, story)

    if key:
        _set_progress(key, phase="taking in the whole timeline", done=len(batches))
    story = _assemble(
        kept, produced, placement, story_key, tone, note, existing, by_sha, choice, left_behind, partial=False
    )
    # Which days this telling was asked for, kept with it: the page says so, and a reader
    # coming back tomorrow can see the story is of a stretch rather than of everything.
    story["range"] = {"from": since, "to": until}
    # The coarse story: the WHOLE history in eras, named in one call. Independent of how much
    # has been written out, so the reader always sees where the project has been, even on a
    # first build that only detailed the last week.
    eras = _era_rows(segment_episodes(stats, sha_paths))
    try:
        overview = _ask(choice, _overview_prompt(eras, tone, repo_name or story_key, note), _ARC_TIMEOUT_SECONDS) or {}
    except StoryError:
        overview = {}  # the moments are the story; a missing overview must never lose them
    _apply_overview(story, eras, overview, existing)
    store.put(story_key, story)
    if key:
        _set_progress(key, phase="done", done=len(batches) + 1, moments=len(story["moments"]))
    return story


def _assemble(
    kept: list[dict],
    produced: list[dict],
    placement: str,
    story_key: str,
    tone: str,
    note: str,
    existing: dict | None,
    by_sha: dict[str, CommitStat],
    choice,
    left_behind: int,
    *,
    partial: bool,
) -> dict[str, Any]:
    if placement in ("prepend", "merge"):
        moments = [*produced, *kept]  # order is fixed by the sort below
    elif placement == "append":
        moments = [*kept, *produced]
    else:
        moments = list(produced)
    moments.sort(key=lambda moment: (moment.get("from", 0), moment.get("to", 0)))
    covered_shas = sorted({sha for moment in moments for sha in moment.get("shas", [])})
    story: dict[str, Any] = {
        "branch": story_key,
        "tone": tone,
        "note": note,
        "moments": moments,
        "covered_shas": covered_shas,
        "covered": len(covered_shas),
        "built_at": int(time.time()),
        "partial": partial,
        "engine": {"backend": choice.backend_name, "model": choice.model or ""},
    }
    for field_name in ("title", "tagline", "arc", "eras"):
        if existing and existing.get(field_name):
            story[field_name] = existing[field_name]
    return story


def _apply_overview(story: dict[str, Any], eras: list[dict], overview: dict, existing: dict | None) -> None:
    """Fold the overview call's answer into the story: the whole-project title and arc, and a
    NAME for each era whose boundaries and numbers we computed."""
    title = _text(overview.get("title"), 120)
    if title:
        story["title"] = title
    tagline = _text(overview.get("tagline"), 240)
    if tagline:
        story["tagline"] = tagline
    arc_text = _text(overview.get("arc"), 400)
    if arc_text:
        story["arc"] = arc_text

    named: dict[int, dict] = {}
    raw_eras = overview.get("eras") if isinstance(overview.get("eras"), list) else overview.get("acts")
    for item in raw_eras if isinstance(raw_eras, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("n") or item.get("era") or 0)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= len(eras):
            named[number] = item
    rows = []
    for number, era in enumerate(eras, start=1):
        item = named.get(number, {})
        label = _text(item.get("title"), 90) or f"{era['when']} to {era['until']}"
        rows.append(
            {
                # Identity is the era's PLACE in the timeline, not its name. Slugging the
                # title looked tidier and broke everything that refers to a part: the
                # overview is re-run on every telling, so a renamed era became a different
                # era, and "tell this part" answered "there is nothing new to tell".
                "id": f"part-{number}",
                "n": number,
                "title": label,
                "blurb": _text(item.get("blurb"), 220),
                **{key: era[key] for key in ("from", "to", "when", "until", "commits", "ins", "del", "turns", "spark")},
            }
        )
    if rows:
        story["eras"] = rows
    elif existing and existing.get("eras"):
        story["eras"] = existing["eras"]
    story.pop("acts", None)  # superseded by eras, which cover the whole history
    story["partial"] = False


def expand_moment(
    root: Path,
    stats: list[CommitStat],
    sha_paths: dict[str, set[str]],
    *,
    branch: str = "",
    moment_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Write out one moment: the detail paragraph and the quoted asks behind it.

    This is the second half of lazy generation. It runs when a reader OPENS a moment, costs
    one agent call over that moment's own material, and is stored, so it is paid once. A
    moment that already has a body is returned as it stands."""
    store = StoryStore(root)
    story_key = branch or "HEAD"
    story = store.get(story_key)
    if not story:
        return {"error": "There is no story on this branch yet."}
    moment = next((item for item in story.get("moments") or [] if item.get("id") == moment_id), None)
    if moment is None:
        return {"error": "That moment is no longer part of the story; reload the page."}
    if moment.get("detail"):
        return {"moment": moment}

    by_sha = {stat.sha: stat for stat in story_stats(stats)}
    own = [by_sha[sha] for sha in moment.get("shas") or [] if sha in by_sha]
    if not own:
        return {"error": "The commits behind this moment are not on this branch any more."}
    choice = learn_page.resolve_learning_backend(Path(root))
    raw = _ask(
        choice,
        _expand_prompt(moment, own, sha_paths, story.get("tone") or DEFAULT_TONE, note or story.get("note") or ""),
        _EXPAND_TIMEOUT_SECONDS,
    )
    detail = _shorten(str((raw or {}).get("detail") or ""), 600)
    if not detail:
        return {"error": "The backend could not write this moment; try opening it again."}
    thoughts = _thoughts((raw or {}).get("thoughts"), {**by_sha, **{s.short: s for s in own}}, [s.sha for s in own])

    def apply(current: dict[str, Any]) -> None:
        for item in current.get("moments") or []:
            if item.get("id") == moment_id:
                item["detail"] = detail
                item["thoughts"] = thoughts
                item["expanded_at"] = int(time.time())

    with _STORE_LOCK:
        data = store.load()
        stored = data.get("stories", {}).get(story_key)
        if isinstance(stored, dict):
            apply(stored)
            store.save(data)
    moment = dict(moment, detail=detail, thoughts=thoughts)
    return {"moment": moment}


# --------------------------------------------------------------------------- the worker


def start_build(
    root: Path,
    stats: list[CommitStat],
    sha_paths: dict[str, set[str]],
    *,
    branch: str = "",
    tone: str = DEFAULT_TONE,
    mode: str = "extend",
    repo_name: str = "",
    note: str = "",
    part_id: str = "",
    since: int = 0,
    until: int = 0,
) -> dict[str, Any]:
    """Kick off a build on a background thread and return immediately.

    A story is several agent calls and minutes of work: holding an HTTP request open for
    that is how you get a page that looks broken. The page polls ``/story/state`` instead,
    which reports progress and the moments written so far."""
    key = build_key(root, branch)
    # Answer "there is nothing to tell" immediately instead of spawning a thread that fails a
    # moment later: the page would otherwise show a spinner and then an error for a button
    # press that could never have worked.
    material, _placement = _material(stats, StoryStore(root).get(branch or "HEAD"), mode, part_id)
    if not material:
        return {
            "error": (
                "There is nothing new to tell here. Use 'tell it again' to rewrite the story from scratch."
                if mode == "extend"
                else (
                    "Every moment in this part is already told."
                    if mode == "part"
                    else "The story already reaches as far back as this branch goes."
                )
            )
        }
    with _BUILDS_LOCK:
        # Only a build the reader has NOT abandoned may refuse another. A stopped build's
        # thread can stay alive for a while (it is inside a backend CLI call that cannot be
        # interrupted), and whatever that call returns is discarded — so counting it as "a
        # story is already being written" refused the reader the very thing Stop promised
        # them. The doomed call still holds the shared agent lock, so the new build's first
        # question waits for it inside the worker (see _ask) rather than in this request.
        if any(_is_live(record) for record in _BUILDS.values()):
            return {"busy": True, "error": "A story is already being written; let that one finish first."}
        stop = threading.Event()
        progress = {
            "phase": "starting",
            "done": 0,
            "total": 1,
            "moments": 0,
            "started": int(time.time()),
            "branch": branch,
            "mode": mode,
            "error": "",
            "running": True,
        }
        record: dict[str, Any] = {"progress": progress, "stop": stop}
        _BUILDS[key] = record

        def work() -> None:
            try:
                build_story(
                    root,
                    stats,
                    sha_paths,
                    branch=branch,
                    tone=tone,
                    mode=mode,
                    repo_name=repo_name,
                    note=note,
                    part_id=part_id,
                    since=since,
                    until=until,
                    key=key,
                    stop=stop,
                )
                _set_progress(key, running=False, phase="done")
            except StoryError as exc:
                stopped_by_reader = str(exc) == "cancelled"
                _set_progress(key, running=False, phase="stopped", error="" if stopped_by_reader else str(exc))
            except Exception as exc:  # never lose the reason in a background thread
                _set_progress(key, running=False, phase="stopped", error=f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=work, name="agitrack-story", daemon=True)
        record["thread"] = thread
        thread.start()
    return {"building": True, "progress": _progress(key)}


def cancel_build(root: Path, branch: str = "") -> dict[str, Any]:
    """Stop the build NOW, as far as the reader is concerned.

    The agent call in flight cannot be interrupted (it is a CLI, mid-request), but nothing
    says the reader has to wait for it: the build is marked stopped immediately, the page is
    released, and whatever that call eventually returns is DISCARDED by the worker when it
    sees the stop flag. Moments already written stay, because they are real story."""
    with _BUILDS_LOCK:
        record = _BUILDS.get(build_key(root, branch))
        if record and record["thread"].is_alive():
            record["stop"].set()
            record["abandoned"] = True  # ...so it can never refuse the next build
            record["progress"].update(running=False, phase="stopped", error="")
            return {"cancelling": True, "stopped": True}
    return {"cancelling": False}


def build_progress(root: Path, branch: str = "") -> dict[str, Any] | None:
    """The current (or last) build's progress for this target, or None if it never ran."""
    return _progress(build_key(root, branch))


# --------------------------------------------------------------------------- page data


def slim_for_demo(story: dict[str, Any]) -> dict[str, Any]:
    """A story stripped to what a READER needs, for the checked-in demo fixture and the
    static export.

    A story cannot be generated in CI (that needs a coding-agent backend and credentials),
    so the demo ships a real one from this repo, checked in like the learn page's profile.
    That makes its size everyone's problem: the build state (which commits a further build
    would still have to cover) and the per-commit fields the page never renders are dropped,
    which is most of the weight."""
    trimmed = {key: value for key, value in story.items() if key != "covered_shas"}
    moments = []
    for moment in story.get("moments") or []:
        rows = []
        for row in moment.get("commits") or []:
            tokens = row.get("tokens") or {}
            rows.append(
                {
                    key: value
                    for key, value in row.items()
                    if key not in ("backend", "model", "tokens")  # never rendered in a commit row
                }
                | ({"tokens": {"output": tokens["output"]}} if tokens.get("output") else {})
            )
        moments.append({key: value for key, value in moment.items() if key != "shas"} | {"commits": rows})
    trimmed["moments"] = moments
    return trimmed


def story_state(
    root: Path,
    stats: list[CommitStat],
    sha_paths: dict[str, set[str]],
    *,
    branch: str = "",
    branches: list[str] | None = None,
    repo_name: str = "",
    backtrace: bool = False,
) -> dict[str, Any]:
    """Everything the page paints from: the stored story (if any), the deterministic
    outline, what could still be told, and the engine that would tell it."""
    kept = story_stats(stats)
    head = kept[-1].sha if kept else ""
    cache_key = (build_key(root, branch), head)
    rows = _OUTLINE_CACHE.get(cache_key)
    if rows is None:
        rows = outline(kept, sha_paths)
        _OUTLINE_CACHE.clear()  # only the current tip is worth keeping
        _OUTLINE_CACHE[cache_key] = rows
    story = StoryStore(root).get(branch or "HEAD")
    return {
        "story": story,
        "outline": rows,
        "building": build_progress(root, branch),
        "tones": list(STORY_TONES),
        "branch": branch,
        "branches": branches or [],
        "repo_name": repo_name,
        "backtrace": backtrace,
        "meta": {
            "commits": len(kept),
            "turns": sum(1 for stat in kept if stat.kind in _AI_KINDS),
            "uncovered": uncovered_count(kept, story),
            "first": kept[0].timestamp if kept else 0,
            "last": kept[-1].timestamp if kept else 0,
            "head": head,
        },
        "engine": learn_page.describe_learning_backend(Path(root)),
    }


def handle_story_post(
    path: str,
    body: dict,
    *,
    root: Path,
    view: Callable[[str], tuple[list[CommitStat], dict[str, set[str]]]],
    repo_name: str = "",
) -> dict | None:
    """The POST dispatcher both dashboards share. ``view(branch)`` returns the
    ``(stats, sha_paths)`` that server would show for that branch, so the live dashboard
    tells the story of real commits and the backtrace one of the reconstruction, from
    identical code. Returns None for an unknown path (the caller 404s)."""
    branch = str(body.get("branch") or "")
    try:
        if path == "/story/build":
            stats, sha_paths = view(branch)
            mode = str(body.get("mode") or "extend")
            if mode not in ("extend", "rewrite", "part"):
                mode = "extend"
            # The reader's chosen days scope the WHOLE build: the parts are computed over the
            # range too, so a story of one week is that week split into parts, not a week's
            # worth of moments hanging off a year-long timeline.
            window = in_range(stats, _epoch(body.get("from")), _epoch(body.get("to")))
            if not window:
                return {"error": _EMPTY_RANGE}
            return start_build(
                root,
                window,
                sha_paths,
                branch=branch,
                tone=str(body.get("tone") or DEFAULT_TONE),
                mode=mode,
                repo_name=repo_name,
                note=str(body.get("note") or ""),
                part_id=str(body.get("part") or ""),
                since=_epoch(body.get("from")),
                until=_epoch(body.get("to")),
            )
        if path == "/story/moment":
            stats, sha_paths = view(branch)
            return expand_moment(
                root,
                stats,
                sha_paths,
                branch=branch,
                moment_id=str(body.get("id") or ""),
                note=str(body.get("note") or ""),
            )
        if path == "/story/cancel":
            return cancel_build(root, branch)
        if path == "/story/forget":
            # Stop any build first, or it would write its story back over the deletion.
            cancel_build(root, branch)
            StoryStore(root).drop(branch or "HEAD")
            return {"forgotten": True}
    except learn_page.LearnAgentError as exc:
        return {"error": str(exc)}
    except StoryError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # surface in the page, never a blank 500
        return {"error": f"{type(exc).__name__}: {exc}"}
    return None


# ------------------------------------------------------------------------- the page


def story_html(root: Path, *, banner_html: str = "") -> str:
    """The /story chrome. Like the dashboard and learn shells, it paints instantly and
    fetches ``/story/state`` afterwards. ``banner_html`` fills the frozen top strip (the
    backtrace notice, or the static demo's). Substituted FIRST so page content can never
    smuggle a placeholder in."""
    from agitrack.metrics.collect import _display_repo
    from agitrack.metrics.web import FONT_LINKS, PREBOOT_CSS, PREBOOT_HTML, _escape

    repo_path = _display_repo(str(root))
    repo_name = repo_path.rstrip("/").rsplit("/", 1)[-1] or repo_path
    from agitrack.metrics import ui

    return ui.render(
        _STORY_TEMPLATE.replace("__BACKTRACE_BANNER__", banner_html)
        .replace("__REPO_NAME__", _escape(repo_name))
        .replace("__REPO__", _escape(repo_path))
        .replace("__PREBOOT_CSS__", PREBOOT_CSS)
        .replace("__PREBOOT_HTML__", PREBOOT_HTML.replace("the aGiTrack dashboard", "the storyline"))
        .replace("__FONT_LINKS__", FONT_LINKS)
        # Built from the backend registry, never hand-written: a literal option list simply
        # could not offer a newly added backend, and nothing failed to say so.
        .replace("__BACKEND_OPTIONS__", learn_page._backend_option_html())
    )


def story_backtrace_banner(directory: str) -> str:
    """The storyline's frozen backtrace notice: this story is told from a RECONSTRUCTION
    of past local sessions, not from aGiTrack's live tracking."""
    from agitrack.metrics.web import _escape

    return (
        '<div class="btbanner">&#9194; BACKTRACE. This story is told from a reconstruction of past '
        f"coding-agent sessions in {_escape(directory)}, not from aGiTrack's live repo tracking. "
        "Tip: run <code>agitrack --backtrace commit</code> to bake this history into your git commit "
        "messages, then launch your coding agent through <code>agitrack</code> and every future "
        "moment writes itself.</div>"
    )


_STORY_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>story · __REPO_NAME__ · aGiTrack</title>
__PREBOOT_CSS__
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2064%2064'%3E%3Crect%20width='64'%20height='64'%20rx='13'%20fill='%23070b09'/%3E%3Ctext%20x='32'%20y='45'%20text-anchor='middle'%20font-family='ui-monospace,monospace'%20font-weight='700'%20font-size='42'%20letter-spacing='-1'%3E%3Ctspan%20fill='%23ffb454'%3Ea%3C/tspan%3E%3Ctspan%20fill='%233dffa0'%3EG%3C/tspan%3E%3C/text%3E%3C/svg%3E">
__FONT_LINKS__
<style>
__UI_TOKENS__
__UI_BASE_CSS__
html{scroll-behavior:smooth}
html,body{margin:0;padding:0;background:var(--ink);color:var(--fg);font:14px/1.55 var(--mono)}

/* Same drifting glow as the learn page, so the three pages feel like one product. */
.ambient{position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.5;
  background:
    radial-gradient(620px 420px at 12% 6%, rgba(61,255,160,.10), transparent 60%),
    radial-gradient(720px 480px at 88% 26%, rgba(103,184,214,.08), transparent 60%),
    radial-gradient(560px 440px at 50% 96%, rgba(255,180,84,.06), transparent 60%);
  animation:drift 36s ease-in-out infinite alternate}
@keyframes drift{from{transform:translate3d(0,0,0) scale(1)}to{transform:translate3d(-30px,20px,0) scale(1.06)}}

.wrap{max-width:1080px;margin:0 auto;padding:22px 20px 80px}
header{display:flex;align-items:baseline;justify-content:space-between;gap:14px;flex-wrap:wrap;
  border-bottom:1px dashed var(--line);padding-bottom:14px;margin-bottom:18px}
.brand{font-family:var(--display);font-weight:400;font-size:38px;line-height:.9;color:var(--phosphor);
  letter-spacing:1.5px;text-shadow:0 0 12px rgba(61,255,160,.5),0 0 44px rgba(61,255,160,.2)}
.brand .a{color:var(--amber);text-shadow:0 0 12px rgba(255,180,84,.5),0 0 44px rgba(255,180,84,.2)}
.brand .sub{font-family:var(--display);font-size:.5em;color:var(--fg-dim);letter-spacing:3px;text-shadow:none}
.meta{color:var(--fg-dim);font-size:12.5px} .meta b{color:var(--fg)}
.navlinks{font-size:12.5px;display:flex;gap:14px;flex-wrap:wrap}
select,input[type=text]{background:var(--panel2);border:1px solid var(--line);color:var(--fg);
  font:inherit;font-size:12.5px;padding:5px 8px;border-radius:4px}
#f-note{flex:1;min-width:220px}
#f-note::placeholder{color:var(--fg-dim)}

.rise{animation:rise .5s ease both}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion: reduce){
  html{scroll-behavior:auto}
  .rise,.ambient,.spin,.ch,.dot,.confetti span{animation:none !important}
  .bar i,.spark i,.dots i{transition:none !important}
}

/* ---------------------------------------------------------------- the hero */
.hero{margin:8px 0 22px}
.hero h1{font-family:var(--display);font-size:46px;line-height:1.02;margin:0 0 6px;color:var(--fg);
  letter-spacing:.5px;text-shadow:0 0 30px rgba(61,255,160,.14)}
.hero .tagline{margin:0 0 14px;color:var(--phosphor);font-size:15px}
.hero .arc{margin:12px 0 0;color:var(--fg-dim);font-size:13.5px;max-width:74ch}
.counters{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 0}
.counter{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 14px;min-width:96px}
.counter b{display:block;font-family:var(--display);font-size:26px;line-height:1;color:var(--phosphor)}
.counter span{font-size:11px;letter-spacing:1px;text-transform:uppercase;color:var(--fg-dim)}

/* ---------------------------------------------------------------- the studio */
.panel{background:var(--panel);border:1px solid var(--line);padding:14px 16px;border-radius:8px}
.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:8px 0}
.row label{color:var(--fg-dim);font-size:12px;min-width:74px}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{background:var(--chipbg);border:1px solid var(--line);color:var(--fg);padding:6px 13px;
  cursor:pointer;font:inherit;font-size:12.5px;border-radius:999px;transition:transform .12s,border-color .12s}
.chip:hover{border-color:var(--phosphor-dim);transform:translateY(-1px)}
.chip.sel{border-color:var(--phosphor);color:var(--phosphor);background:#0f2a1c}
.btn{background:var(--panel2);border:1px solid var(--line);color:var(--fg);font:inherit;font-size:12.5px;
  padding:7px 14px;border-radius:6px;cursor:pointer;transition:border-color .12s,color .12s,background .12s}
.btn:hover{border-color:var(--phosphor-dim);color:var(--phosphor)}
.btn.primary{border-color:var(--phosphor);color:var(--phosphor);background:#0f2a1c}
.btn.primary:hover{background:var(--phosphor);color:var(--ink)}
.btn.small{padding:4px 10px;font-size:11.5px}
.btn[disabled]{opacity:.45;cursor:not-allowed}
/* An armed destructive button. It is the same button one press later, so it has to STOP
   looking like the one that was pressed: red, filled, and pulsing until it is used or the
   note beside it is dismissed. */
.btn.danger{border-color:var(--bad);color:var(--ink);background:var(--bad);font-weight:600;
  animation:armed 1.1s ease-in-out infinite}
.btn.danger:hover{border-color:var(--bad);color:var(--ink);background:#ff8a8a}
@keyframes armed{50%{box-shadow:0 0 0 4px rgba(255,107,107,.22)}}
/* A setting changed with a story already on screen: the story is now out of date with the
   controls above it, and nothing happens until this is pressed. */
.btn.stale{border-color:var(--phosphor);color:var(--phosphor);background:#0f2a1c}
.btn.stale:hover{background:var(--phosphor);color:var(--ink)}
@media (prefers-reduced-motion: reduce){.btn.danger{animation:none}}
.hint{color:var(--fg-dim);font-size:12px;margin-top:8px;line-height:1.5}
.gobtn{font-family:var(--display);font-size:23px;letter-spacing:.5px;background:transparent;
  border:1px solid var(--phosphor);color:var(--phosphor);padding:6px 20px;border-radius:6px;cursor:pointer;
  transition:background .15s,color .15s}
.gobtn:hover{background:var(--phosphor);color:var(--ink)}
.gobtn[disabled]{opacity:.4;cursor:not-allowed}
/* Which days to tell: the dashboard's range control, scoping a telling instead of a view. */
__UI_RANGE_CSS__
#f-period{appearance:none;background:var(--ink);color:var(--fg);border:1px solid var(--line);
  font-family:var(--mono);font-size:13px;padding:6px 30px 6px 11px;cursor:pointer;border-radius:4px;
  background-image:linear-gradient(45deg,transparent 50%,var(--phosphor-dim) 50%),linear-gradient(135deg,var(--phosphor-dim) 50%,transparent 50%);
  background-position:calc(100% - 16px) 50%,calc(100% - 11px) 50%;background-size:5px 5px,5px 5px;background-repeat:no-repeat}
#f-period:focus{outline:none;border-color:var(--phosphor)}
/* The popup is anchored under the select, which sits inside a wrapping row: pin it to the
   LEFT here, or on a narrow screen it hangs off the side of the panel. */
#periodwrap .daterange{right:auto;left:0}

/* ---------------------------------------------------------------- build progress */
__UI_OVERLAY_CSS__
.pbar{width:100%;height:4px;background:var(--panel2);border-radius:3px;overflow:hidden}
.pbar span{display:block;height:100%;background:var(--phosphor);width:0;transition:width .4s ease}
__UI_FLASH_CSS__

/* ------------------------------------------------------------- the way back out */
/* There is no zoom control: the reader walks IN by clicking a part, and this one button
   walks them back out a level. A row of zoom stops looked like it went somewhere precise
   and did not - pressed from the parts view it landed in a part nobody had chosen. */
.zoomctx{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:24px 0 8px;font-size:12.5px;
  color:var(--fg-dim)}
.zoomctx .zin b{color:var(--phosphor)}
.emptypart{color:var(--fg-dim);font-size:13px;border:1px dashed var(--line);border-radius:8px;
  padding:18px;margin:8px 0}
/* An untold part is a page with one thing to do on it, so that thing is IN THE MIDDLE of it
   and looks like the offer it is. It used to be a small button at the right-hand end of the
   context bar above, where a reader looking at an empty page never found it. */
.empty-part-cta{display:flex;flex-direction:column;align-items:center;gap:14px;text-align:center;
  padding:34px 20px}
.empty-part-cta p{margin:0;max-width:56ch;line-height:1.6}
.era{cursor:pointer}
.era .zoomin{color:var(--phosphor-dim)}
.era:hover .zoomin{color:var(--phosphor)}
/* What to DO with the rows below, said once above them. A hover-only affordance tells a
   reader nothing until they happen to point at something, and nothing at all on a touch
   screen, where there is no hover. */
.howto{color:var(--fg-dim);font-size:12.5px;margin:-4px 0 14px;max-width:70ch}
/* Both the parts and the moments are controls, so they answer the keyboard as well. */
.era:focus-visible,.ch:focus-visible{outline:2px solid var(--phosphor);outline-offset:2px}

/* ---------------------------------------------------------------- the timeline */
h2.section{font-size:13px;letter-spacing:1.5px;text-transform:uppercase;color:var(--phosphor);
  margin:30px 0 10px;font-weight:600}
.act{margin:34px 0 6px;padding-left:44px;position:relative}
.act .actno{font-family:var(--display);font-size:15px;color:var(--amber);letter-spacing:2px}
.act h2{font-family:var(--display);font-size:30px;margin:2px 0 4px;color:var(--fg);line-height:1}
.act p{margin:0;color:var(--fg-dim);font-size:12.5px;max-width:70ch}
.timeline{position:relative;padding-left:44px;margin-top:10px}
.timeline::before{content:"";position:absolute;left:15px;top:6px;bottom:6px;width:2px;
  background:linear-gradient(180deg,transparent,var(--line) 6%,var(--line) 94%,transparent)}
.ch{position:relative;margin:0 0 14px;animation:rise .5s ease both}
.ch .dot{position:absolute;left:-44px;top:2px;width:32px;height:32px;border-radius:50%;
  background:var(--panel);border:1px solid var(--line);display:flex;align-items:center;justify-content:center;
  font-size:15px;transition:border-color .18s,box-shadow .18s,transform .18s}
.ch.open .dot,.ch:hover .dot{border-color:var(--phosphor);box-shadow:0 0 0 4px rgba(61,255,160,.08)}
.ch.cue .dot{transform:scale(1.14);border-color:var(--amber);box-shadow:0 0 0 6px rgba(255,180,84,.12)}
.chbody{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px 16px;
  cursor:pointer;transition:border-color .18s,background .18s}
.chbody:hover{border-color:var(--phosphor-dim)}
.ch.open .chbody{border-color:var(--phosphor-dim);background:linear-gradient(180deg,#0e1712,var(--panel) 60%)}
.chhead{display:flex;align-items:center;gap:9px;flex-wrap:wrap;font-size:11.5px;color:var(--fg-dim)}
.kick{border:1px solid var(--line);border-radius:999px;padding:1px 9px;font-size:11px;letter-spacing:.5px;
  color:var(--fg-dim)}
.kick.k-turning-point{border-color:var(--amber);color:var(--amber)}
.kick.k-feature{border-color:var(--phosphor-dim);color:var(--phosphor)}
.kick.k-fix{border-color:#5d7fa8;color:var(--accent)}
.kick.k-milestone{border-color:var(--amber-dim);color:var(--amber)}
.kick.k-refactor,.kick.k-experiment{border-color:var(--line);color:var(--fg-dim)}
.ch h3{margin:6px 0 4px;font-size:17px;color:var(--fg);line-height:1.3}
.ch .sum{margin:0;color:var(--fg-dim);font-size:13px;line-height:1.55}
.ch .open-hint{margin-top:9px;font-size:11.5px;color:var(--phosphor-dim);letter-spacing:.5px}
.ch.open .open-hint{color:var(--fg-dim)}
/* A long moment's "close" hint at the bottom is a scroll away, so an open moment also
   carries one in its header, where the reader's eye already is. */
.closehint{display:none;order:9;margin-left:auto;color:var(--fg-dim);border:1px solid var(--line);
  border-radius:999px;padding:1px 9px}
.ch.open .closehint{display:inline}
.more{margin-top:14px;border-top:1px dashed var(--line);padding-top:12px;cursor:default;min-width:0}
.md{font-size:13px;line-height:1.65;color:var(--fg)}
.md p{margin:0 0 10px} .md h3,.md h4{font-size:13px;color:var(--phosphor);margin:14px 0 6px}
.md ul,.md ol{margin:0 0 10px;padding-left:20px} .md li{margin:3px 0}
.md code{background:var(--panel2);border:1px solid var(--line);border-radius:3px;padding:0 4px;font-size:12.5px}
.md pre{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:10px;overflow-x:auto}
.md pre code{border:none;background:none;padding:0}

/* what they asked for: the developer's own words, quoted from the trace */
.writing{display:flex;align-items:center;gap:10px;color:var(--phosphor);font-size:12.5px;padding:4px 0}
.more .notice{border:1px solid var(--amber);color:var(--amber);background:var(--panel2);padding:9px 12px;
  border-radius:6px;font-size:12.5px}
.thoughts{margin:16px 0 0}
.thoughts > h4{font-size:11.5px;letter-spacing:1.5px;text-transform:uppercase;color:var(--amber);
  margin:0 0 9px;font-weight:600}
.th{border-left:2px solid var(--amber-dim);padding:2px 0 2px 12px;margin:0 0 12px}
.th .q{color:var(--fg);font-size:13px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
.th .q::before{content:"\201C"} .th .q::after{content:"\201D"}
.th .note{color:var(--fg-dim);font-size:12.5px;margin-top:5px}
.th .thmeta{color:var(--phosphor-dim);font-size:11px;margin-top:4px;letter-spacing:.5px}

/* ------------------------------------------------------------------ graphics
   The numbers carry more of the story than a sentence about them would, so they are drawn:
   a bar for what a moment added and removed, a dot per commit, a sparkline per era. */
.shape{display:flex;align-items:center;gap:16px;margin:10px 0 0;flex-wrap:wrap}
.bar{display:flex;height:6px;border-radius:3px;overflow:hidden;background:var(--panel2);
  width:120px;flex:none}
.bar i{display:block;height:100%;width:var(--w);transition:width .7s cubic-bezier(.2,.8,.2,1)}
.bar .add{background:var(--phosphor)} .bar .rem{background:var(--bad)}
.dots{display:flex;gap:3px;align-items:center}
.dots i{width:5px;height:5px;border-radius:50%;background:var(--phosphor-dim);opacity:.85}
.dots i.big{background:var(--phosphor)}
.dots span{font-size:11px;color:var(--fg-dim);margin-left:2px}
.metric{display:flex;align-items:center;gap:7px}
.mlabel{font-size:11.5px;color:var(--fg-dim);white-space:nowrap}
.spark{display:flex;align-items:flex-end;gap:2px;height:28px}
.spark i{width:6px;background:linear-gradient(180deg,var(--phosphor),var(--phosphor-dim));
  border-radius:1px;height:var(--h);transition:height .6s cubic-bezier(.2,.8,.2,1)}
.era{display:flex;gap:14px;align-items:flex-start;padding:13px 16px;margin:0 0 10px;
  background:var(--panel);border:1px solid var(--line);border-radius:10px;
  transition:border-color .18s,background .18s}
.era:hover{border-color:var(--phosphor-dim)}
.era .eran{font-family:var(--display);font-size:26px;line-height:1;color:var(--amber-dim);min-width:24px}
.era.has .eran{color:var(--amber)}
.era .erabody{flex:1;min-width:0}
.era h3{margin:0 0 3px;font-size:16px;color:var(--fg)}
.era .sum{margin:0}
.era .lit{color:var(--phosphor-dim)}
__UI_ENGINE_CSS__
.actmeta{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-top:8px;
  color:var(--fg-dim);font-size:11.5px}

/* Reveal on scroll, once, and only when motion is welcome. */
.ch,.era{opacity:1}
body.anim .ch,body.anim .era{opacity:0;transform:translateY(14px);
  transition:opacity .45s ease,transform .45s cubic-bezier(.2,.8,.2,1)}
body.anim .ch.in,body.anim .era.in{opacity:1;transform:none}

/* Waiting states: never an empty page with no explanation. */
.skel{display:grid;gap:12px;margin-top:12px}
.skel div{height:74px;border-radius:10px;background:linear-gradient(90deg,var(--panel) 25%,var(--panel2) 37%,var(--panel) 63%);
  background-size:400% 100%;animation:shimmer 1.4s linear infinite}
@keyframes shimmer{from{background-position:100% 0}to{background-position:0 0}}
.loading{display:flex;align-items:center;gap:10px;color:var(--phosphor);font-size:13px;margin:18px 0}

.facts{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 0}
.fact{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:4px 10px;font-size:11.5px;
  color:var(--fg-dim)}
.fact b{color:var(--fg)}
.paths{margin:10px 0 0;display:flex;gap:6px;flex-wrap:wrap}
.paths span{font-size:11.5px;color:var(--fg-dim);background:var(--panel2);border:1px solid var(--line);
  border-radius:4px;padding:2px 7px;word-break:break-all}

.commits{margin:16px 0 0;min-width:0}
/* The section's OWN heading only ("the commits themselves"), never a heading inside one of
   the commit messages below it: those are rendered by the shared commit renderer and must
   look exactly as they do on the dashboard. */
.commits > h4{font-size:11.5px;letter-spacing:1.5px;text-transform:uppercase;color:var(--phosphor);
  margin:0 0 8px;font-weight:600}
/* The same commit row as the dashboard's log: same badge colours, same line counts, plus
   who committed it and when. */
.cmt{border:1px solid var(--line);border-radius:6px;margin:0 0 7px;background:var(--panel2);
  min-width:0;overflow:hidden}
.cmt .chead{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;padding:8px 11px;cursor:pointer;font-size:12.5px}
.cmt .chead:hover{background:#0f1a14}
.cmt .sha{color:var(--amber);font-size:12.5px}
.cmt .badge{font-size:10.5px;letter-spacing:.5px;padding:1px 7px;border:1px solid var(--line);color:var(--fg-dim)}
.cmt.ai .badge{color:var(--phosphor);border-color:var(--phosphor-dim)}
.cmt.ops .badge{color:var(--accent);border-color:var(--accent)}
.cmt.nontracked .badge{color:var(--amber);border-color:var(--amber-dim)}
.cmt .ksub{flex:1;min-width:200px;color:var(--fg);overflow-wrap:anywhere}
.cmt .lc{font-size:12px;color:var(--fg-dim)}
.cmt .lc .add{color:var(--phosphor)} .cmt .lc .rem{color:var(--bad)}
.cmt .tok{font-size:11px;border:1px solid var(--line);padding:0 5px;color:var(--fg-dim)}
.cmt .tok.out{color:var(--phosphor);border-color:var(--phosphor-dim)}
.cmt .who{font-size:11.5px;color:var(--fg-dim)}
.cmt .num{color:var(--fg-dim);font-size:11.5px}
.cmt .add{color:var(--phosphor)} .cmt .rem{color:var(--bad)}
.cmt .cbody{border-top:1px solid var(--line);padding:9px 11px}
.cmt .cbar{margin:0 0 8px}
__UI_COMMIT_CSS__
.dmsg .diffempty{color:var(--fg-dim);font-size:12px;font-style:italic;padding:8px 12px;line-height:1.55}
.dmsg .diffempty b{color:var(--phosphor);font-style:normal}
/* loading spinner shown while a detail / diff / file history is being fetched */
  border-radius:50%;animation:spin .7s linear infinite}
/* rendered Markdown inside the expanded message */
.dmsg.md p{margin:7px 0}
/* Reflow mode (set by reflowParagraphs on paragraphs the layout wraps anyway):
   the source line breaks stop rendering and the paragraph flows as prose. The
   space preceding each <br> in the markup keeps the words apart. */
.dmsg.md p.mdp.reflow br{display:none}
.dmsg.md .md-h{font-family:var(--mono);color:var(--amber);margin:11px 0 5px;font-size:13px;font-weight:600}
/* Heading depth reads at a glance: structural sections (# …) brightest/largest,
   the ## User/## Agent role one step down, and a message's own nested headings
   smaller, dimmer and indented so they sit visibly under their role. md() maps a
   source level L to <h(L+2)>, so these are # → h3, ## → h4, content → h5/h6. */
.dmsg.md h3.md-h{font-size:15px;color:var(--amber)}
.dmsg.md h4.md-h{font-size:13.5px;color:var(--phosphor)}
.dmsg.md h5.md-h{font-size:12.5px;color:var(--ops);font-weight:500;padding-left:10px;border-left:2px solid var(--line)}
.dmsg.md h6.md-h{font-size:12px;color:var(--fg-dim);font-weight:500;padding-left:20px;border-left:2px solid var(--line)}
.dmsg.md ul{margin:6px 0 6px 18px} .dmsg.md li{margin:2px 0}
.dmsg.md code{background:var(--panel2);border:1px solid var(--line);padding:0 4px;color:var(--phosphor);font-size:12px}
.dmsg.md strong{color:var(--fg)} .dmsg.md em{color:var(--fg)}
.dmsg.md .md-code{white-space:pre-wrap;background:var(--panel2);border:1px solid var(--line);
  padding:8px 10px;margin:7px 0;color:var(--fg-dim);font-size:12px}
.dmsg.md a{color:var(--phosphor)}
.diffempty{color:var(--fg-dim);font-size:12.5px}

/* ---------------------------------------------------------------- the outline */
.outline .row2{display:flex;gap:12px;align-items:baseline;padding:8px 0;border-bottom:1px dashed var(--line);
  flex-wrap:wrap}
.outline .when{color:var(--phosphor-dim);font-size:11.5px;min-width:88px}
.outline .what{flex:1;min-width:220px;color:var(--fg);font-size:12.5px;word-break:break-word}
.outline .num{color:var(--fg-dim);font-size:11.5px}
.outline .said{color:var(--fg-dim);font-size:12px;font-style:italic;margin-top:3px;width:100%}


.confetti{position:fixed;inset:0;pointer-events:none;z-index:90}
.confetti span{position:absolute;top:-24px;animation:fall 3.2s linear forwards}
@keyframes fall{to{transform:translateY(104vh) rotate(360deg);opacity:0}}

/* The frozen top strips (backtrace notice, static-demo notice), matching the learn page. */
__UI_BANNER_CSS__

footer{margin-top:40px;padding-top:14px;border-top:1px dashed var(--line);color:var(--fg-dim);font-size:12px}
footer code{color:var(--fg)}

@media (max-width:640px){
  .wrap{padding-top:16px;padding-bottom:70px}
  .hero h1{font-size:34px}
  .timeline,.act{padding-left:32px}
  .timeline::before{left:9px}
  .ch .dot{left:-32px;width:22px;height:22px;font-size:11px}
  .counter{min-width:calc(50% - 5px);flex:1}
  /* The outline's numbers have no room beside a wrapped title on a phone, and a flex item
     with nothing left to take simply overflows: give them their own line. */
  .outline .num{flex-basis:100%}
}
</style>
</head>
<body>
__PREBOOT_HTML__
__BACKTRACE_BANNER__
<div class="ambient"></div>
<div class="wrap">
  <header class="rise">
    <div class="brand"><span class="a">a</span>GiTrack<span class="sub">&nbsp;story</span></div>
    <div class="meta"><span>repo</span> <b>__REPO__</b><span id="branchwrap"> &nbsp;·&nbsp; <span>branch</span>
      <select id="f-branch"></select></span></div>
    <div class="navlinks">
      <a id="backlink" href="./">&larr; dashboard</a>
      <a id="learnlink" href="learn">&#127891; learn</a>
    </div>
  </header>

  <section class="hero rise" id="hero">
    <h1 id="story-title">the story of __REPO_NAME__</h1>
    <p class="tagline" id="story-tagline">the same commits you already have, zoomed out until they make sense.</p>
    <div class="counters" id="counters"></div>
    <p class="arc" id="story-arc" hidden></p>
  </section>

  <div class="panel studio rise" id="studio">
    <div class="row"><label>tell it</label>
      <div class="chips" id="tone-chips">
        <button class="chip" data-v="plain">&#128220; plainly</button>
        <button class="chip sel" data-v="playful">&#128512; playfully</button>
        <button class="chip" data-v="epic">&#127905; epically</button>
      </div>
    </div>
    <div class="row"><label>which days</label>
      <div class="period-field" id="periodwrap">
        <select id="f-period">
          __UI_RANGE_OPTIONS__
        </select>
        <div class="daterange" id="daterange" hidden>
          <div class="dr-field"><label for="f-from">from</label><input type="date" id="f-from"></div>
          <div class="dr-field"><label for="f-to">to</label><input type="date" id="f-to"></div>
          <button class="dr-done" id="dr-done">done</button>
        </div>
      </div>
      <span class="hint" id="rangehint" style="margin-top:0"></span>
    </div>
    <div class="row"><label>anything to add</label>
      <input type="text" id="f-note" maxlength="300"
             placeholder="optional: a style, something to focus on, someone to credit…">
    </div>
    <div class="row" id="actions">
      <button class="gobtn" id="write">&#10024; tell me the story</button>
      <button class="btn" id="extend" hidden>&#10133; catch up on what is new</button>
      <button class="btn" id="rewrite" hidden>&#8635; tell it again</button>
      <button class="btn small" id="forget" hidden>forget this story</button>
    </div>
    <div class="hint" id="studiohint"></div>
  </div>

  <div class="overlay" id="overlay" hidden>
    <div class="ov-card">
      <span class="ovicon" id="ov-icon">&#128214;</span>
      <div class="ov-title" id="build-phase">reading your history…</div>
      <div class="ov-msg" id="build-sub"></div>
      <div class="ov-engine" id="ov-engine"></div>
      <div class="ov-bar"><span id="build-bar"></span></div>
      <div class="ov-hint">your agent is reading the commits and the messages behind them; this usually takes under a minute</div>
      <button class="btn small" id="build-cancel">stop</button>
    </div>
  </div>
  <div id="flash"></div>

  <div class="zoomctx" id="zoomctx" hidden></div>

  <div id="loading" class="loading"><span class="spin"></span><span>finding the story…</span></div>
  <div id="skeleton" class="skel" hidden><div></div><div></div><div></div></div>
  <div id="eras" hidden></div>
  <div id="timeline"></div>
  <div class="outline" id="outlinewrap" hidden>
    <h2 class="section" id="outlinehead">the shape of it</h2>
    <div class="hint" id="outlinehint"></div>
    <div id="outlinelist"></div>
  </div>

  <details class="engine" id="engine">
    <summary>settings: who tells it (backend &amp; model)</summary>
    <div class="ebody">
      <div class="row"><label>backend</label>
        <select id="e-backend">
          <option value="">auto (latest session)</option>
__BACKEND_OPTIONS__
        </select>
        <label style="min-width:auto">model</label>
        <select id="e-model"><option value="">auto (latest session)</option></select>
        <button class="btn" id="e-save">save</button>
        <span class="hint" id="e-msg" style="margin-top:0"></span>
      </div>
      <div class="hint">Saved as <code>learning_backend</code> / <code>learning_model</code> in
        <code>~/.agitrack/config.json</code> (or this repo's <code>.agitrack/config.json</code>, if it already
        pins them), the same pair the learn page uses, so one choice covers both.
        A bigger model tells a better story; a smaller one is quicker and cheaper.</div>
    </div>
  </details>

  <footer id="enginenote"></footer>
</div>

<script>
"use strict";
// The document is here and styled: drop the pre-boot overlay that covered its transfer.
{ const pb = document.getElementById("preboot"); if (pb) pb.remove(); }
__UI_DOM_JS__
const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const state = {
  data: null, story: null, tone: "playful", branch: "", open: new Set(), told: new Set(),
  poll: null, building: false,
  // Which days a telling covers. 0/0 is the whole history; anything else scopes the commits
  // the agent reads, which is how a reader asks for a closer look at a stretch of work.
  fromTs: 0, toTs: 0, touchedRange: false,
  // The settings the story on screen was told with, so a change to any of them can be shown
  // on the button that applies it.
  told0: "", lastMode: "",
  // Depth 2 is the top: the parts. You walk in from there; normalizeZoom() drops to the
  // flat moment list for a story that has no parts.
  zoom: 2, part: null
};

// ------------------------------------------------------------------ helpers
function num(n){ n = Number(n||0);
  if (n >= 1e9) return (n/1e9).toFixed(1).replace(/\.0$/,"") + "b";
  if (n >= 1e6) return (n/1e6).toFixed(1).replace(/\.0$/,"") + "m";
  if (n >= 1e3) return (n/1e3).toFixed(1).replace(/\.0$/,"") + "k";
  return String(n);
}
function day(ts){ if(!ts) return ""; const d = new Date(ts*1000);
  return d.toISOString().slice(0,10); }
// The dashboard's stamp: date and time, so a commit here reads the same as one there.
function stamp(ts){ return ts ? new Date(ts * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC" : ""; }
const AI_KINDS = new Set(["agent", "covered", "agent-merge"]);
const post = postJson;
__UI_RANGE_JS__

// ---------------------------------------------------------------- which days to tell
// The same control the dashboard filters WITH, used here to scope what gets WRITTEN: a
// story of last week is a closer look at last week than any re-telling of the whole year.
function historySpan(){
  const m = (state.data && state.data.meta) || {};
  return {from: m.first || 0, to: m.last || 0};
}
function applyPeriod(){
  const v = $("f-period").value;
  if (v === "") { state.fromTs = 0; state.toTs = 0; }
  else if (v === "custom") { state.fromTs = dateToTs($("f-from").value, false); state.toTs = dateToTs($("f-to").value, true); }
  else { state.fromTs = Math.floor(Date.now() / 1000) - (+v) * DAY_SECONDS; state.toTs = 0; }
  syncPeriodDates();
  renderRangeHint();
}
// The from/to inputs always show the range actually in force, so "last 30 days" is a pair of
// real dates rather than a phrase the reader has to work out.
function syncPeriodDates(){
  const v = $("f-period").value, span = historySpan();
  if (v === "custom") return;
  if (v === "") { $("f-from").value = ymd(span.from); $("f-to").value = ymd(span.to); }
  else {
    $("f-from").value = ymd(Math.floor(Date.now() / 1000) - (+v) * DAY_SECONDS);
    $("f-to").value = ymd(span.to || Math.floor(Date.now() / 1000));
  }
}
function setDateBounds(){
  const span = historySpan(), lo = ymd(span.from), hi = ymd(span.to);
  for (const id of ["f-from", "f-to"]) { const el = $(id); el.min = lo; el.max = hi; }
}
// How many commits the chosen days actually hold, said before an agent call is spent: a
// range with nothing in it is the one mistake this control invites. The outline only carries
// the most recent stretch of sittings, so a range reaching further back than it goes is
// reported by its dates alone rather than by a count that would be wrong.
function commitsInRange(){
  const rows = (state.data && state.data.outline) || [];
  if (!rows.length) return -1;
  const oldest = Math.min.apply(null, rows.map(r => r.from || 0));
  if (state.fromTs && state.fromTs < oldest) return -1;
  let n = 0;
  for (const row of rows) {
    if (state.fromTs && row.to < state.fromTs) continue;
    if (state.toTs && row.from > state.toTs) continue;
    n += row.commits || 0;
  }
  return n;
}
function rangeLabel(){
  if (!state.fromTs && !state.toTs) return "the whole history";
  const span = historySpan();
  return ymd(state.fromTs || span.from) + " → " + ymd(state.toTs || span.to || Math.floor(Date.now() / 1000));
}
// Put the control back to the range the stored story was told for.
function adoptStoryRange(){
  const range = (state.story && state.story.range) || {};
  const from = Number(range.from || 0), to = Number(range.to || 0);
  state.fromTs = from; state.toTs = to;
  if (!from && !to) { $("f-period").value = ""; return; }
  $("f-period").value = "custom";
  $("f-from").value = ymd(from || (state.data && state.data.meta && state.data.meta.first) || 0);
  $("f-to").value = ymd(to || (state.data && state.data.meta && state.data.meta.last) || 0);
}

function renderRangeHint(){
  if (!state.fromTs && !state.toTs) { $("rangehint").textContent = "the whole history"; return; }
  const n = commitsInRange();
  $("rangehint").textContent = rangeLabel() +
    (n < 0 ? "" : n ? " · about " + n + " commit" + (n === 1 ? "" : "s") : " · no commits in these days");
}
function flash(html){ const f = $("flash"); f.innerHTML = html; f.onclick = () => { f.innerHTML = ""; }; }
function notice(text){ flash('<div class="notice">' + esc(text) + "</div>"); }
function fail(text){ flash('<div class="error">' + esc(text) + "</div>"); }

// A very small markdown renderer, same shape as the learn page's.
function md(src){
  src = String(src || "");
  const blocks = [];
  src = src.replace(/```([\s\S]*?)```/g, (_, code) => {
    blocks.push("<pre><code>" + esc(code.replace(/^\w*\n/, "")) + "</code></pre>");
    return "\x00" + (blocks.length - 1) + "\x00";
  });
  let h = esc(src);
  h = h.replace(/^#{3,6}\s+(.+)$/gm, "<h4>$1</h4>")
       .replace(/^#{1,2}\s+(.+)$/gm, "<h3>$1</h3>")
       .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
       .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>")
       .replace(/`([^`\n]+)`/g, "<code>$1</code>")
       .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
                '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  const out = []; let list = null, para = [];
  const endPara = () => { if (para.length) { out.push("<p>" + para.join(" ") + "</p>"); para = []; } };
  const endList = () => { if (list) { out.push("</" + list + ">"); list = null; } };
  for (const line of h.split("\n")) {
    const t = line.trim();
    const ul = /^[-*]\s+(.*)/.exec(t), ol = /^\d+[.)]\s+(.*)/.exec(t);
    if (ul || ol) {
      endPara();
      const want = ul ? "ul" : "ol";
      if (list !== want) { endList(); out.push("<" + want + ">"); list = want; }
      out.push("<li>" + (ul ? ul[1] : ol[1]) + "</li>");
    } else if (!t) { endPara(); endList(); }
    else if (/^<(h3|h4)/.test(t) || /^\x00\d+\x00$/.test(t)) { endPara(); endList(); out.push(t); }
    else para.push(t);
  }
  endPara(); endList();
  return out.join("\n").replace(/\x00(\d+)\x00/g, (_, i) => blocks[Number(i)]);
}

// ------------------------------------------------------------------ loading
// The state fetch is retried rather than reported: a page restored from the back/forward
// cache, or opened again while an earlier request was still in flight, would otherwise greet
// the reader with "load failed" for a request that simply got cut short.
async function load(attempt){
  attempt = attempt || 0;
  const url = "story/state" + (state.branch ? "?branch=" + encodeURIComponent(state.branch) : "");
  try {
    const r = await fetch(url, {cache: "no-store"});
    if (!r.ok) throw new Error("server error " + r.status);
    state.data = await r.json();
    state.story = state.data.story;
    if (!state.branch && state.data.branch) state.branch = state.data.branch;
    if (state.story && state.story.tone) state.tone = state.story.tone;
    if (state.story && state.story.note && !$("f-note").value) $("f-note").value = state.story.note;
    // Show the controls the story on screen was actually told with, so "these settings" means
    // something the reader can see, and a change to any of them is visibly a change.
    if (!state.touchedRange) adoptStoryRange();
    state.loadFailed = false;
    render();
    state.told0 = state.story ? settingsKey() : "";
  } catch (error) {
    if (attempt < 3) {                        // a dropped fetch is normal; a broken page is not
      await new Promise(done => setTimeout(done, 400 * (attempt + 1)));
      return load(attempt + 1);
    }
    $("loading").hidden = true;
    state.loadFailed = true;
    // Keep whatever is already on screen; only say something when there is nothing to show.
    if (!state.data) fail("could not reach aGiTrack for this story. It may have been stopped; this page retries by itself.");
    throw error;
  }
}

// Coming back to the page (bfcache restore, or the tab becoming visible again) reloads the
// state instead of leaving a half-loaded page behind.
window.addEventListener("pageshow", event => { if (event.persisted || state.loadFailed) load().catch(() => {}); });
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && (state.loadFailed || !state.data)) load().catch(() => {});
});
// Leaving the page (a refresh, a link, closing the tab) ABANDONS a telling this page started.
// It is spending an agent call and the reader has walked away from it, and coming back to a
// build nobody is watching is how someone ended up unable to start a new one at all. Sent
// with sendBeacon: a normal fetch is cancelled the moment the page goes.
window.addEventListener("pagehide", () => {
  if (!state.building) return;
  const payload = JSON.stringify({branch: state.branch});
  if (navigator.sendBeacon) navigator.sendBeacon("story/cancel", new Blob([payload], {type: "application/json"}));
  else post("story/cancel", {branch: state.branch}).catch(() => {});
});

function render(){
  const d = state.data; if (!d) return;
  $("loading").hidden = true;
  $("skeleton").hidden = true;
  normalizeZoom();
  renderBranches();
  setDateBounds();
  syncPeriodDates();
  renderRangeHint();
  renderHero();
  renderStudio();
  renderZoomContext();
  renderEras();
  renderTimeline();
  revealAll();
  renderOutline();
  renderEngine();
  renderBuild(d.building);
  for (const chip of $("tone-chips").querySelectorAll(".chip"))
    chip.classList.toggle("sel", chip.dataset.v === state.tone);
  openFromHash();
}

function renderBranches(){
  const d = state.data, sel = $("f-branch");
  if (!d.branches || d.branches.length < 2) { $("branchwrap").hidden = true; return; }
  $("branchwrap").hidden = false;
  if (sel.dataset.filled !== "1" || sel.options.length !== d.branches.length) {
    sel.innerHTML = d.branches.map(b => '<option value="' + esc(b) + '">' + esc(b) + "</option>").join("");
    sel.dataset.filled = "1";
  }
  sel.value = state.branch || d.branch || "";
}

function countUp(el, target){
  if (REDUCED || target <= 0) { el.textContent = num(target); return; }
  const started = performance.now(), dur = 700;
  const step = now => {
    const k = Math.min(1, (now - started) / dur);
    el.textContent = num(Math.round(target * (1 - Math.pow(1 - k, 3))));
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function renderHero(){
  const d = state.data, s = state.story, m = d.meta || {};
  $("story-title").textContent = (s && s.title) || ("the story of " + (d.repo_name || "this repo"));
  const tag = (s && s.tagline) || (s ? "" : "the same commits you already have, zoomed out until they make sense.");
  $("story-tagline").textContent = tag;
  $("story-tagline").hidden = !tag;
  const arc = s && s.arc;
  $("story-arc").textContent = arc || "";
  $("story-arc").hidden = !arc;
  const days = m.first && m.last ? Math.max(1, Math.round((m.last - m.first) / 86400)) : 0;
  const moments = s && s.moments ? s.moments.length : 0;
  const cells = [
    [moments, moments === 1 ? "moment" : "moments"],
    [m.commits || 0, "commits"],
    [m.turns || 0, "agent turns"],
    [days, days === 1 ? "day" : "days"],
  ];
  $("counters").innerHTML = cells.map(c => '<div class="counter"><b data-n="' + c[0] + '">0</b><span>' + esc(c[1]) + "</span></div>").join("");
  for (const b of $("counters").querySelectorAll("b")) countUp(b, Number(b.dataset.n));
}

function renderStudio(){
  const d = state.data, s = state.story, m = d.meta || {};
  void s;
  const has = !!(s && s.moments && s.moments.length);
  const engineBroken = !!(d.engine && d.engine.error);
  $("write").hidden = has;
  $("extend").hidden = !has || !m.uncovered;
  $("rewrite").hidden = !has;
  $("forget").hidden = !has;
  if (m.uncovered && has) $("extend").innerHTML = "&#10133; add the " + m.uncovered + " new commit" + (m.uncovered === 1 ? "" : "s");
  for (const id of ["write","extend","rewrite"]) $(id).disabled = state.building || engineBroken;
  let hint;
  if (engineBroken)
    hint = "There is no coding agent configured here, so nobody can tell it yet. Run an aGiTrack session in this repo, or pick one below under \"who tells it\". The outline further down is built from the commits alone and needs no agent at all.";
  else if (!has)
    hint = "Your agent reads this " + (d.backtrace ? "reconstructed history" : "branch") + " twice: once over the " +
           "whole timeline for the parts it breaks into, and once over the most recent stretch for moments you can open. " +
           "About half a minute. Each moment is then written out the first time you open it, and everything is " +
           "stored in .agitrack/story.json, so you only ever pay for what you read.";
  else
    hint = "Click a part to go inside it, and a moment to have your agent write it out, once, then keep it. " +
           (m.uncovered ? m.uncovered + " commit" + (m.uncovered === 1 ? " has" : "s have") + " landed since. " : "") +
           "To read a different stretch of history, pick the days above and tell it again.";
  $("studiohint").textContent = hint;
  markStale();
}

// Changing a setting does nothing on its own: the story on screen was written with the OLD
// ones and only a new telling applies the new ones. Say so on the button that does it, and
// on the button itself rather than in a line of hint text nobody reads twice.
function settingsKey(){
  return [state.tone, state.fromTs || 0, state.toTs || 0, $("f-note").value.trim()].join("|");
}
function markStale(){
  const button = $("rewrite");
  if (button.hidden || button.dataset.armed) return;      // armed has its own, louder, look
  const stale = !!state.told0 && settingsKey() !== state.told0;
  button.classList.toggle("stale", stale);
  button.innerHTML = stale ? "&#8635; tell it again with these settings" : "&#8635; tell it again";
}

// The story has two depths and you walk between them:
//
//   2  the parts          click a part to go in
//   3  inside one part    its moments; open one for the telling and the commits
//
// There is deliberately no zoom control. A row of stops let someone jump to a depth that
// only means something INSIDE a part, from a view where no part was chosen; it either did
// nothing or landed somewhere they had not asked for. Going in is a click on the thing you
// want; coming out is one button that says where it goes. A reader who wants a CLOSER look
// than a part gives them asks for a narrower range of days, which is the same story told
// over fewer commits rather than a second telling of the same ones.
function partsLevel(){
  // A story told before parts existed has only its moments: that flat list is the top.
  return ((state.story && state.story.eras) || []).length ? 2 : 3;
}

// Keep the depth and the part consistent with the story actually loaded. Called before
// every paint, not only on a click: a story can arrive (or be forgotten, or rewritten with
// different parts) under a page that is already sitting at some depth.
function normalizeZoom(){
  const top = partsLevel();
  state.zoom = Math.max(top, Math.min(3, state.zoom || top));
  if (state.zoom === 2) { state.part = null; return; }       // the parts view is nobody's part
  // Depth 3 is per-part. Reached without one (an old link, a hash typed by hand), it opens
  // the newest part rather than a blank page.
  if (!currentPart()) {
    const eras = (state.story && state.story.eras) || [];
    state.part = eras.length ? eras[eras.length - 1].id : null;
  }
}

function setZoom(level, part){
  state.zoom = level || partsLevel();
  if (part !== undefined) state.part = part;
  normalizeZoom();
  renderZoomContext();
  renderEras();
  renderTimeline();
  revealAll();
  if (history.replaceState) history.replaceState(null, "", "#z" + state.zoom + (state.part ? "." + state.part : ""));
}

function currentPart(){
  const eras = (state.story && state.story.eras) || [];
  return eras.find(era => era.id === state.part) || null;
}

function partsLabel(){
  const n = ((state.story && state.story.eras) || []).length;
  return n === 1 ? "the whole story" : "the " + n + " parts";
}

// One step back out, labelled with where it lands.
function renderZoomContext(){
  const part = currentPart(), host = $("zoomctx");
  host.hidden = state.zoom <= partsLevel() || !part;
  if (host.hidden) return;
  host.innerHTML = '<button class="btn small zback">&larr; back to ' + partsLabel() + "</button>" +
    '<span class="zin">inside <b>' + esc(part.title) + "</b> &nbsp;·&nbsp; " +
    esc(part.when) + " &rarr; " + esc(part.until) + " &nbsp;·&nbsp; " + part.commits + " commits</span>" +
    // A part nobody has told yet is a dead end without this: the reader is looking straight
    // at it and there is nothing to read and no obvious way to get any.
    "";
}

// The moments of the part being read (all of them, in one telling), or the whole story when
// no part is chosen.
function momentsInView(){
  const all = (state.story && state.story.moments) || [];
  const part = currentPart();
  return part ? all.filter(m => m.from >= part.from && m.to <= part.to) : all;
}

// The whole project, coarse: one row per era, newest first, each with its span, size and the
// shape of the work inside it. This is complete even when only the last week has moments.
function renderEras(){
  const s = state.story, host = $("eras");
  const eras = (s && s.eras) || [];
  host.hidden = !eras.length || state.zoom !== 2;   // inside a part, the part owns the page
  if (host.hidden) return;
  // The rows are the way in, so the page says so once, above them: a card that responds only
  // on hover tells a reader nothing until they happen to point at it, and on a touch screen
  // there is no hover at all.
  host.innerHTML = '<h2 class="section">' + (eras.length === 1 ? "the whole story" : eras.length + " parts") + "</h2>" +
    '<p class="howto">&#128072; Tap or click any part to read the moments inside it.</p>' +
    eras.slice().reverse().map(era => {
      const told = ((s.moments || []).filter(m => m.from >= era.from && m.to <= era.to)).length;
      return '<div class="era' + (told ? " has" : "") + '" data-part="' + esc(era.id) + '" ' +
        'role="button" tabindex="0">' +
        '<div class="eran">' + era.n + "</div>" +
        '<div class="erabody"><h3>' + esc(era.title) + "</h3>" +
        (era.blurb ? '<p class="sum">' + esc(era.blurb) + "</p>" : "") +
        '<div class="actmeta">' + sparkHtml(era.spark) +
          "<span>" + esc(era.when) + " &rarr; " + esc(era.until) + "</span>" +
          "<span>" + era.commits + " commits</span>" +
          (told ? '<span class="lit">' + told + " moment" + (told === 1 ? "" : "s") + " written</span>" : "") +
        '<span class="zoomin">open this part &rarr;</span></div></div></div>';
    }).join("");
}

function sparkHtml(sizes){
  const values = (sizes || []).map(v => Math.max(1, Number(v) || 0));
  if (!values.length) return "";
  // Square-root scale: one 17,000-line stretch beside a dozen 300-line ones flattens a
  // linear sparkline into a dashed line.
  const peak = Math.sqrt(Math.max.apply(null, values));
  const bars = values.slice(-28).map((size, i) =>
    '<i style="--h:' + Math.max(4, Math.round(28 * Math.sqrt(size) / peak)) + "px;transition-delay:" + (i * 20) + 'ms"></i>').join("");
  return '<span class="spark">' + bars + "</span>";
}

// The story is stored oldest-first (that is the order it was written in) and read
// NEWEST-FIRST, like the dashboard's log: what happened lately is what people came for.
function renderTimeline(){
  const host = $("timeline");
  const part = currentPart();
  const all = momentsInView();
  if (state.zoom < 3) { host.innerHTML = ""; return; }        // the parts view owns the page
  if (!all.length) {                                          // an untold part explains itself
    host.innerHTML = part
      ? '<div class="emptypart empty-part-cta">' +
        "<p>Nothing from this part has been told yet. Your agent reads its " + part.commits +
        " commits and writes the moments inside it; about half a minute.</p>" +
        '<button class="gobtn zpart">&#10024; tell this part</button></div>'
      : "";
    return;
  }

  const newestFirst = all.slice().reverse();                  // a telling is bounded: no paging
  let html = '<h2 class="section">' +
    (part ? "inside " + esc(part.title.toLowerCase()) : "moment by moment") + "</h2>" +
    '<p class="howto">&#128072; Tap or click a moment to read it: what happened, what was asked ' +
    "for at the time, and the commits it came out of.</p>" +
    '<div class="timeline">';
  newestFirst.forEach((moment, position) => { html += momentHtml(moment, position); });
  host.innerHTML = html + "</div>";

  for (const id of state.open) {
    const el = document.getElementById("ch-" + id);
    if (el) { el.classList.add("open"); fillMoment(el, false); }
  }
}

// Cards appear as they are reached, not all at once on load (which fired for a hundred
// moments at the same moment, most of them off screen, and looked like a stutter).
let _revealer = null;
function revealAll(){
  // Both sections, one observer: calling it per host disconnected the other's.
  if (_revealer) _revealer.disconnect();
  _revealer = null;
  revealOnScroll(document.body);
}
function revealOnScroll(host){
  if (REDUCED) { document.body.classList.remove("anim"); return; }
  document.body.classList.add("anim");
  if (!("IntersectionObserver" in window)) {
    host.querySelectorAll(".ch,.era").forEach(el => el.classList.add("in"));
    return;
  }
  if (_revealer) _revealer.disconnect();
  _revealer = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      entry.target.classList.add("in");
      _revealer.unobserve(entry.target);   // one-shot: nothing re-animates on the way back
    }
  }, {rootMargin: "80px 0px"});
  host.querySelectorAll(".ch,.era").forEach(el => _revealer.observe(el));
}

// A moment's size, drawn: the split of what it added and removed, one dot per commit, and
// the token count. Three glanceable facts instead of a sentence nobody reads.
function shapeHtml(st){
  const ins = Number(st.ins || 0), del = Number(st.del || 0), total = ins + del;
  const parts = [];
  if (total) {
    const pct = Math.round(100 * ins / total);
    // Labelled, because a bare two-colour bar means nothing on its own.
    parts.push('<span class="metric"><span class="bar" title="' + ins + ' lines added, ' + del + ' removed">' +
      '<i class="add" style="--w:' + pct + '%"></i><i class="rem" style="--w:' + (100 - pct) + '%"></i></span>' +
      '<span class="mlabel"><span class="add">+' + num(ins) + '</span> <span class="rem">-' + num(del) +
      "</span> lines</span></span>");
  }
  const commits = Math.max(0, Number(st.commits || 0));
  if (commits) {
    const shown = Math.min(commits, 12);
    let dots = "";
    for (let d = 0; d < shown; d++) dots += '<i style="transition-delay:' + (d * 25) + 'ms"></i>';
    parts.push('<span class="metric"><span class="dots" title="one dot per commit">' + dots + "</span>" +
      '<span class="mlabel">' + commits + " commit" + (commits === 1 ? "" : "s") + "</span></span>");
  }
  if (st.turns) parts.push('<span class="metric"><span class="mlabel">' + st.turns + " agent turn" +
    (st.turns === 1 ? "" : "s") + "</span></span>");
  if (st.tokens) parts.push('<span class="metric"><span class="mlabel">' + num(st.tokens) + " tokens</span></span>");
  return parts.length ? '<div class="shape">' + parts.join("") + "</div>" : "";
}

function momentHtml(c, i){
  const st = c.stats || {};
  const bits = [];
  if (st.commits) bits.push(st.commits + " commit" + (st.commits === 1 ? "" : "s"));
  return '<article class="ch" id="ch-' + esc(c.id) + '" data-id="' + esc(c.id) + '" data-i="' + i + '"' +
      ' role="button" tabindex="0"' +
      ' style="animation-delay:' + Math.min(i * 40, 400) + 'ms">' +
    '<div class="dot">' + esc(c.emoji || "✦") + "</div>" +
    '<div class="chbody">' +
      '<div class="chhead"><span class="closehint">close &uarr;</span><span>' + esc(c.when || "") + "</span>" +
        '<span class="kick k-' + esc(String(c.kicker || "moment").replace(/\s+/g, "-")) + '">' + esc(c.kicker || "moment") + "</span>" +
        '<span>' + bits.join(" &nbsp;·&nbsp; ") + "</span></div>" +
      "<h3>" + esc(c.title) + "</h3>" +
      '<p class="sum">' + esc(c.summary || "") + "</p>" +
      shapeHtml(st) +
      '<div class="more" hidden></div>' +
      '<div class="open-hint">read this moment &darr;</div>' +
    "</div></article>";
}

// The moment body is built on first open: several hundred moments' worth of detail
// would otherwise be parsed into the DOM for a page most readers skim.
function fillMoment(el, animate){
  const c = momentById(el.dataset.id); if (!c) return;
  const box = el.querySelector(".more");
  box.hidden = false;
  el.querySelector(".open-hint").innerHTML = "close &uarr;";
  if (box.dataset.filled === "1") return;
  // A moment's body is written the first time someone opens it (one agent call), not up
  // front for a hundred moments nobody may read. Headline now, prose on demand.
  if (!c.detail) { writeMoment(el, c, box); return; }
  box.dataset.filled = "1";
  const st = c.stats || {};
  let html = '<div class="detail md">' + md(c.detail || c.summary || "") + "</div>";
  if (c.thoughts && c.thoughts.length) {
    html += '<div class="thoughts"><h4>&#128172; in their own words</h4>' + c.thoughts.map((t, i) =>
      '<div class="th" data-i="' + i + '">' +
        '<div class="q">' + esc(t.quote || "") + "</div>" +
        (t.note ? '<div class="note">' + esc(t.note) + "</div>" : "") +
        '<div class="thmeta">' + esc(t.short || "") + (t.at ? " &nbsp;·&nbsp; " + esc(day(t.at)) : "") + "</div>" +
      "</div>").join("") + "</div>";
  }
  const facts = [];
  if (st.turns) facts.push("<b>" + st.turns + "</b> agent turn" + (st.turns === 1 ? "" : "s"));
  if (st.files_total) facts.push("<b>" + st.files_total + "</b> file" + (st.files_total === 1 ? "" : "s") + " touched");
  if (st.tokens) facts.push("<b>" + num(st.tokens) + "</b> tokens");
  if (facts.length) html += '<div class="facts">' + facts.map(f => '<span class="fact">' + f + "</span>").join("") + "</div>";
  if (st.files && st.files.length)
    html += '<div class="paths">' + st.files.map(p => "<span>" + esc(p) + "</span>").join("") + "</div>";
  if (c.commits && c.commits.length) {
    // Newest first here too, and each row opens on its COMMIT MESSAGE (the conversation that
    // produced it) with a button to flip to the file changes, like the dashboard's log.
    html += '<div class="commits"><h4>&#128220; the commits themselves</h4>' + c.commits.slice().reverse().map(r => {
      const kind = AI_KINDS.has(r.kind) ? "ai" : (r.kind === "agitrack-ops" ? "ops" : "nontracked");
      const out = (r.tokens && r.tokens.output) ? '<span class="tok out">' + num(r.tokens.output) + " out</span>" : "";
      return '<div class="cmt entry ' + kind + '" data-sha="' + esc(r.sha) + '">' +
        '<div class="chead">' +
          '<span class="sha">' + esc(r.short) + "</span>" +
          '<span class="badge ' + kind + '">' + esc(r.kind) + "</span>" +
          '<span class="ksub">' + esc(r.subject) + "</span>" +
          '<span class="lc"><span class="add">+' + num(r.ins) + '</span> <span class="rem">-' + num(r["del"]) + "</span></span>" +
          out +
          '<span class="who">' + esc(r.author || "") + (r.ts ? " &nbsp;·&nbsp; " + esc(stamp(r.ts)) : "") + "</span>" +
          '<span class="num toggle">open &#9662;</span>' +
        "</div>" +
      "</div>";
    }).join("") + "</div>";
  }
  box.innerHTML = html;
}

// Ask the agent to write this moment out, then render it. Costs one call, once ever:
// the result is stored with the story, so every later open is instant.
async function writeMoment(el, c, box){
  if (box.dataset.writing === "1") return;
  box.dataset.writing = "1";
  box.innerHTML = '<div class="writing"><span class="spin"></span>' +
    "<span>writing this moment: reading its commits and what you asked for" +
    (engineLine() ? " (" + esc(engineLine()) + ")" : "") + "…</span></div>";
  try {
    const r = await post("story/moment", {branch: state.branch, id: c.id, note: $("f-note").value.trim()});
    box.dataset.writing = "";
    if (r.error || !r.moment) {
      box.innerHTML = '<div class="notice">' + esc(r.error || "this moment could not be written") + "</div>";
      return;
    }
    Object.assign(c, r.moment);       // keep it in the loaded story, so re-opening is instant
    box.dataset.filled = "";
    fillMoment(el, true);
  } catch (e) {
    box.dataset.writing = "";
    box.innerHTML = '<div class="notice">' + esc(e.message) + "</div>";
  }
}

// The blocking overlay, for the generations that rewrite the page under the reader (a
// build, a part, a closer telling). Writing ONE moment loads inside its own card instead.
function showOverlay(title, message, canStop){
  $("overlay").hidden = false;
  $("ov-icon").textContent = "\u{1F4D6}";
  $("build-phase").textContent = title;
  $("build-sub").textContent = message || "";
  showEngine();
  $("build-bar").style.width = canStop ? "5%" : "45%";
  $("build-cancel").hidden = !canStop;
  $("build-cancel").disabled = false;
}
// Whose time and tokens this is spending, on every overlay that spends any.
function showEngine(){
  const engine = engineLine();
  $("ov-engine").textContent = engine ? "written by " + engine : "";
}
function hideOverlay(){
  $("overlay").hidden = true;
  $("build-cancel").hidden = false;
  $("skeleton").hidden = true;   // it only ever stands in for a build that is now over
}

function momentById(id){
  const s = state.story;
  return ((s && s.moments) || []).find(c => c.id === id) || null;
}

function toggleMoment(el, force){
  const id = el.dataset.id;
  const wantOpen = force === undefined ? !state.open.has(id) : force;
  if (wantOpen) {
    state.open.add(id);
    el.classList.add("open");
    fillMoment(el, !state.told.has(id));
    state.told.add(id);
    if (history.replaceState) history.replaceState(null, "", "#ch-" + id);
  } else {
    state.open.delete(id);
    el.classList.remove("open");
    const box = el.querySelector(".more");
    if (box) box.hidden = true;
    el.querySelector(".open-hint").innerHTML = "read this moment &darr;";
  }
}

__UI_COMMIT_JS__

// ------------------------------------------------------ commits: message, then changes
// One fetch answers both (the /diff endpoint carries the commit message too), so opening a
// commit shows the conversation that produced it and flipping to the file changes is free.
const _commitCache = {};

async function toggleCommit(row){
  const sha = row.dataset.sha;
  const open = row.querySelector(".cbody");
  const label = row.querySelector(".chead .toggle");
  if (open) { open.remove(); if (label) label.innerHTML = "open &#9662;"; return; }
  if (label) label.innerHTML = "close &#9652;";
  const body = document.createElement("div");
  body.className = "cbody";
  body.innerHTML = '<div class="diffempty">loading…</div>';
  row.appendChild(body);
  let data = _commitCache[sha];
  if (!data) {
    try {
      const r = await fetch("diff?sha=" + encodeURIComponent(sha), {cache:"no-store"});
      data = await r.json();
    } catch (e) {
      data = {error: "could not load this commit (server unreachable)"};
    }
    _commitCache[sha] = data;
  }
  if (!row.querySelector(".cbody")) return;  // closed again while we were fetching
  body.innerHTML = '<div class="cbar"><button class="cflip">show file changes</button></div>' +
                   '<div class="cview dmsg"></div>';
  showCommitView(row, "message");
}

function showCommitView(row, mode){
  const data = _commitCache[row.dataset.sha] || {};
  const view = row.querySelector(".cview"), flip = row.querySelector(".cflip");
  if (!view) return;
  view.dataset.mode = mode;
  if (mode === "diff") {
    view.className = "cview dmsg diff";
    view.innerHTML = data.error ? '<div class="diffempty">' + esc(data.error) + "</div>"
                                : diffHtml(data.diff, data.truncated);
    if (flip) flip.textContent = "show commit message";
  } else {
    view.className = "cview dmsg md";
    view.innerHTML = data.error ? '<div class="diffempty">' + esc(data.error) + "</div>"
                                : commitMd(data.message || "(no message recorded)");
    reflowParagraphs(view);  // the dashboard's prose-reflow, so wrapping matches there too
    if (flip) flip.textContent = "show file changes";
  }
}
function diffHtml(text, truncated){
  const t = (text || "").trim();
  if (!t) return '<div class="diffempty">' + (state.data && state.data.backtrace
    ? "No file changes were recovered for this turn. A backtrace reads the agent's transcript, which records file-editing tool calls but not every way code can change (shell commands, formatters, generated files), so their diffs cannot be recovered after the fact."
    : "no changes to show for this commit") + "</div>";
  if (/Binary files .* differ/.test(t) && !/^@@/m.test(t))
    return '<div class="diffempty">binary file, no text diff to show</div>';
  return renderDiff(t) + (truncated ? '<div class="diffempty">…diff truncated (very large diff)</div>' : "");
}

// ------------------------------------------------------------------ the outline
function renderOutline(){
  const d = state.data, rows = d.outline || [];
  const has = !!(state.story && state.story.moments && state.story.moments.length);
  // Only before there is a story: once moments exist, every commit is reachable through
  // the moment it belongs to, and a second flat list of the same commits is just noise.
  $("outlinewrap").hidden = has || !rows.length;
  if (has) return;
  $("outlinehead").textContent = "what there is to tell";
  $("outlinehint").textContent =
    "aGiTrack has already grouped your commits into sittings of work. Press the button above and your agent turns these into something worth reading.";
  $("outlinelist").innerHTML = rows.map(r =>
    '<div class="row2"><span class="when">' + esc(r.when) + "</span>" +
      '<span class="what">' + esc(r.title) + "</span>" +
      '<span class="num">' + r.commits + " commit" + (r.commits === 1 ? "" : "s") +
        (r.ins || r["del"] ? " &nbsp;+" + num(r.ins) + "/-" + num(r["del"]) : "") + "</span>" +
      (r.prompt ? '<div class="said">&#8220;' + esc(r.prompt) + "&#8221;</div>" : "") +
    "</div>").join("");
}

// The storyteller runs on the same configured backend/model as the coach, so the panel
// writes the same repo-scoped keys through the endpoint the learn page already exposes.
async function loadModels(selected){
  const backend = $("e-backend").value;
  const sel = $("e-model");
  sel.innerHTML = '<option value="">auto (latest session)</option>';
  if (!backend) return;
  try {
    const r = await fetch("learn/models?backend=" + encodeURIComponent(backend), {cache: "no-store"});
    const data = await r.json();
    for (const model of (data.models || [])) {
      const option = document.createElement("option");
      option.value = model; option.textContent = model;
      sel.appendChild(option);
    }
    if (selected) sel.value = selected;
  } catch (e) { /* the select simply stays on "auto" */ }
}

async function saveEngine(){
  $("e-msg").textContent = "saving…";
  try {
    const r = await post("learn/config", {backend: $("e-backend").value, model: $("e-model").value});
    if (r.error) { $("e-msg").textContent = r.error; return; }
    $("e-msg").textContent = "saved. the next telling uses it.";
    await load();
  } catch (e) { $("e-msg").textContent = e.message; }
}

function renderEngine(){
  const e = (state.data && state.data.engine) || {};
  // A backend that cannot be resolved is fixable right here — open the panel that fixes it
  // rather than leaving the note as the only sign (#233).
  if (e.needs_choice) {
    $("engine").open = true;
    if ($("e-backend").dataset.touched !== "1" && !$("e-backend").value) {
      $("e-backend").value = (e.installed || [])[0] || "";
    }
  }
  if ($("e-backend").dataset.touched !== "1") {
    $("e-backend").value = e.backend_source === "config" ? (e.backend || "") : "";
    if ($("e-model").dataset.filled !== "1") {
      $("e-model").dataset.filled = "1";
      loadModels(e.model_source === "config" ? e.model : "");
    }
  }
  $("enginenote").innerHTML = e.error
    ? esc(e.error)
    : "moments written by <code>" + esc(e.backend || "?") + "</code> · " + esc(e.model || "backend default model") +
      " · stored in <code>.agitrack/story.json</code>";
}

// ------------------------------------------------------------------ building
function engineLine(){
  const e = (state.data && state.data.engine) || {};
  if (!e.backend || e.error) return "";
  // Which model is spending your time and tokens, said plainly, wherever the page waits.
  return e.backend + " \u00b7 " + (e.model || "its default model");
}

function renderBuild(p){
  const running = !!(p && p.running);
  state.building = running;
  $("overlay").hidden = !running;
  // A stop is not an error and the server no longer reports it as one (the page says so
  // itself, the moment the button is pressed), so anything left here is a real failure.
  if (p && p.error && !running) fail(p.error);
  if (!running) { renderStudio(); return; }
  $("build-phase").textContent = p.phase || "working";
  $("build-cancel").hidden = false;
  $("build-cancel").disabled = false;
  showEngine();          // also for a build this page did not start (another tab, a reload)
  const pct = p.total ? Math.round(100 * Math.min(1, p.done / p.total)) : 5;
  $("build-bar").style.width = Math.max(5, pct) + "%";
  $("build-sub").textContent = (p.moments || 0) + " moment" + (p.moments === 1 ? "" : "s") + " so far";
  renderStudio();
}

const BUILD_OPENING = {
  rewrite: "reading your history…",
  extend:  "reading the commits that landed since…",
  part:    "reading this part…",
};

async function build(mode){
  if (state.building) return;
  flash("");
  state.lastMode = mode;
  state.building = true; renderStudio();
  // At the click, not when the server answers. Starting a build is a POST that has to reach
  // the daemon and hand the work to a thread; waiting for it left about a second in which
  // the button had visibly done nothing and people pressed it again.
  showOverlay(BUILD_OPENING[mode] || "reading your history…", "", true);
  if (!(state.story && (state.story.moments || []).length)) $("skeleton").hidden = false;
  try {
    const r = await post("story/build", {branch: state.branch, tone: state.tone, mode: mode,
                                        note: $("f-note").value.trim(), part: state.part || "",
                                        from: state.fromTs || 0, to: state.toTs || 0});
    if (r.error || r.busy) { state.building = false; hideOverlay(); fail(r.error || "busy"); renderStudio(); return; }
    renderBuild(r.progress);
    startPolling();
  } catch (e) { state.building = false; hideOverlay(); fail(e.message); renderStudio(); }
}

// While a build runs, only the things that CHANGED are touched. The poll used to re-render
// the entire page every 2.5 seconds - hero, eras, every card - which flashed the whole
// screen, threw away scroll position, restarted the reveal animations, and made the stop
// button hard to hit.
function startPolling(){
  stopPolling();
  state.poll = setInterval(async () => {
    try {
      const before = state.story ? (state.story.moments || []).length : 0;
      const url = "story/state" + (state.branch ? "?branch=" + encodeURIComponent(state.branch) : "");
      const r = await fetch(url, {cache: "no-store"});
      if (!r.ok) return;
      const data = await r.json();
      state.data = data;
      state.story = data.story;
      const after = state.story ? (state.story.moments || []).length : 0;
      renderBuild(data.building);          // the bar, the phase, the counter: cheap, no reflow
      if (after !== before) {              // ...and the timeline only when there is new story
        $("skeleton").hidden = true;
        renderHero();
        renderEras();
        renderTimeline();
      }
      if (!data.building || !data.building.running) {
        stopPolling();
        $("skeleton").hidden = true;
        // A telling that rewrote the story rewrote its PARTS too, so wherever the reader was
        // standing may not exist any more. Land them on the parts, which is where a new story
        // starts, unless they had asked for one named part - then stay in it and read it.
        if (state.lastMode !== "part") setZoom(2);
        render();                          // one final, complete paint
        state.told0 = state.story ? settingsKey() : "";
        markStale();
        if (after) celebrate();
      }
    } catch (e) { /* a poll that fails is retried by the next one */ }
  }, 2500);
}
function stopPolling(){ if (state.poll) { clearInterval(state.poll); state.poll = null; } }

function celebrate(){
  if (REDUCED) return;
  const box = document.createElement("div");
  box.className = "confetti";
  const icons = ["\u{1F4D6}", "✨", "\u{1F4DC}", "\u{1F389}"];
  for (let i = 0; i < 16; i++) {
    const s = document.createElement("span");
    s.textContent = icons[i % icons.length];
    s.style.left = (4 + Math.random() * 92) + "vw";
    s.style.animationDelay = (Math.random() * 0.7) + "s";
    s.style.fontSize = (14 + Math.random() * 12) + "px";
    box.appendChild(s);
  }
  document.body.appendChild(box);
  setTimeout(() => box.remove(), 3600);
}

function openFromHash(){
  const z = /^#z(\d)(?:\.(.+))?$/.exec(location.hash || "");
  if (z) { setZoom(Number(z[1]), z[2] || null); return; }
  const m = /^#ch-(.+)$/.exec(location.hash || "");
  if (!m) return;
  const el = document.getElementById("ch-" + m[1]);
  if (!el) return;
  toggleMoment(el, true);
  setTimeout(() => el.scrollIntoView({block: "start", behavior: "auto"}), 30);
}

// ------------------------------------------------------------------ wiring
$("timeline").addEventListener("click", e => {
  if (e.target.closest(".zpart")) { build("part"); return; }
  const flip = e.target.closest(".cflip");
  if (flip) {
    const row = flip.closest(".cmt"), view = row.querySelector(".cview");
    showCommitView(row, view && view.dataset.mode === "diff" ? "message" : "diff");
    return;
  }
  const head = e.target.closest(".cmt .chead");
  if (head) { toggleCommit(head.parentElement); return; }
  if (e.target.closest(".more") && !e.target.closest(".open-hint")) return;  // reading, not toggling
  const ch = e.target.closest(".ch");
  if (!ch) return;
  toggleMoment(ch);
});
$("tone-chips").addEventListener("click", e => {
  const chip = e.target.closest(".chip"); if (!chip) return;
  state.tone = chip.dataset.v;
  for (const c of $("tone-chips").querySelectorAll(".chip")) c.classList.toggle("sel", c === chip);
  markStale();
});
$("f-note").addEventListener("input", markStale);
$("write").addEventListener("click", () => build("rewrite"));
$("extend").addEventListener("click", () => build("extend"));
$("rewrite").addEventListener("click", () => {
  // It is destructive and it is not obvious: everything written so far goes, including the
  // moments someone paid an agent call each to open. Say so, and make them mean it.
  const s = state.story || {};
  const told = (s.moments || []).length;
  const written = (s.moments || []).filter(m => m.detail).length;
  if (!$("rewrite").dataset.armed) {
    $("rewrite").dataset.armed = "1";
    $("rewrite").innerHTML = "&#9888; yes, throw it away and start over";
    // Red, filled and pulsing: the confirm must not look like the button just pressed, or a
    // second click lands on it by muscle memory rather than by decision.
    $("rewrite").classList.add("danger");
    $("rewrite").classList.remove("stale");
    notice("Telling it again starts from scratch: the " + told + " moment" + (told === 1 ? "" : "s") +
      " you have now" + (written ? " (" + written + " already written out)" : "") +
      " are deleted, and your agent lays out the newest stretch again from the commits. " +
      "Your commits are untouched. Press the button once more to confirm, or click this note to cancel.");
    const flash = $("flash");
    flash.onclick = () => { flash.innerHTML = ""; disarmRewrite(); };
    return;
  }
  disarmRewrite();
  build("rewrite");
});
function disarmRewrite(){
  delete $("rewrite").dataset.armed;
  $("rewrite").classList.remove("danger");
  $("rewrite").innerHTML = "&#8635; tell it again";
  markStale();
}
$("forget").addEventListener("click", async () => {
  try { await post("story/forget", {branch: state.branch}); state.open.clear(); await load(); notice("Forgotten. The commits are untouched; the telling is gone."); }
  catch (e) { fail(e.message); }
});
$("build-cancel").addEventListener("click", async () => {
  // Stopping IS immediate now: the build is marked stopped and abandoned the moment this is
  // pressed, whatever its last backend call eventually returns is discarded, and it can no
  // longer refuse the next telling. So the page closes and says what is kept, instead of the
  // old promise to "stop after the moment being written" - which was true when a reader had
  // to wait out that call, and became a page that sat there looking stuck.
  $("build-cancel").disabled = true;
  state.building = false;
  stopPolling();
  hideOverlay();
  notice("Stopped. The moments already written are kept.");
  try { await post("story/cancel", {branch: state.branch}); } catch (e) {}
  load().catch(() => {});     // ...and show exactly what that is
});
// Which days to tell: the dashboard's control, wired the same way (see ui.RANGE_JS).
bindRangeControl(() => { state.touchedRange = true; applyPeriod(); markStale(); });
$("e-backend").addEventListener("change", () => { $("e-backend").dataset.touched = "1"; loadModels(null); });
$("e-save").addEventListener("click", saveEngine);
$("eras").addEventListener("click", e => {
  const seg = e.target.closest("[data-part]");
  if (seg) setZoom(3, seg.dataset.part);
});
// A part and a moment are controls (role="button"), so Enter and Space must work them: the
// card is a div, and a div does not get that for free the way a <button> would.
function activateOnKey(host, act){
  host.addEventListener("keydown", e => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const target = e.target.closest("[data-part],.ch");
    if (!target) return;
    e.preventDefault();
    act(target);
  });
}
activateOnKey($("eras"), el => setZoom(3, el.dataset.part));
activateOnKey($("timeline"), el => toggleMoment(el));
$("zoomctx").addEventListener("click", e => {
  if (e.target.closest(".zback")) setZoom(2);
});
$("f-branch").addEventListener("change", () => {
  state.branch = $("f-branch").value; state.open.clear();
  load().catch(e => fail(e.message));
});
window.addEventListener("hashchange", openFromHash);

load().catch(() => {});   // load() reports for itself, after its retries
// A build started from another tab (or still running from before this page loaded) keeps
// the page live without anyone pressing anything.
setTimeout(() => { if (state.data && state.data.building && state.data.building.running) startPolling(); }, 100);
</script>
</body>
</html>
"""
