"""The storyline page (``/story``): the repo's history told as a story.

Where the dashboard shows WHAT changed and the learn page coaches the person behind it,
the storyline answers "how did this project get here?". It reads the same commit metadata
and interaction traces and hands them to the coding-agent BACKEND, which writes a timeline
of CHAPTERS: turning points, features that landed, things that were torn out and rebuilt.

Four levels of detail, so a reader can stop wherever they like:

1. the arc (a title, a tagline, acts),
2. a chapter card (when, what, one line of why),
3. the chapter opened: the full telling plus the developer's CHAIN OF THOUGHT, and
4. the commits behind it, each expandable into its real diff.

The chain of thought is never invented. The quotes are the developer's own prompts, read
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

from agitrack.git import GitRepo
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
# Episodes per agent call, and the character budget for the digest that call sees.
_BATCH_EPISODES = 6
_BATCH_CHARS = 7600
# The hard ceiling on agent calls per build (plus one for the arc). A history bigger than
# this is covered newest-first and the story records that older commits are not covered
# yet, so the reader is never told a partial story is the whole one.
_MAX_BATCHES = 12
# Per-call bounds. Chapters are a bigger completion than a summary but smaller than a lesson.
_CHAPTER_TIMEOUT_SECONDS = 300
_ARC_TIMEOUT_SECONDS = 180
# How long a build may wait for the learn page's agent lock before giving up (the two pages
# share it so a laptop never runs two backend CLIs at once).
_LOCK_WAIT_SECONDS = 420

# What a chapter can be about. The agent picks one; anything else falls back to "chapter".
KICKERS = ("turning point", "feature", "fix", "refactor", "milestone", "experiment", "chapter")

# How the story is told. Purely a prompt flavour, stored with the story so a rebuild in the
# same tone is reproducible and the page can show which one produced what you are reading.
STORY_TONES: dict[str, str] = {
    "plain": (
        "Tell it plainly and warmly, like a good engineering write-up: concrete, specific, "
        "no hype and no drama."
    ),
    "playful": (
        "Tell it with light humour and personality, like a developer telling a friend over "
        "coffee what happened. Still accurate and specific; funny because the details are, "
        "never because you exaggerated them."
    ),
    "epic": (
        "Tell it like a chronicle of a small expedition: a little grand, a little cinematic, "
        "but every claim still grounded in what the commits and prompts actually show."
    ),
}
DEFAULT_TONE = "playful"

_STORY_SYSTEM = (
    "You are the storyteller built into aGiTrack, a tool that records how people build "
    "software with coding agents. You are given real commit metadata and the developer's own "
    "prompts to their coding agent, and you turn them into the story of the project: what "
    "changed, why it mattered, and what the developer was clearly trying to do. Be concrete "
    "and specific, never generic. Use simple everyday words and short sentences. Never invent "
    "a fact that is not in the material: if something is unclear, say less rather than more. "
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
        return story if isinstance(story, dict) else None

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
        stories = [s for s in self.load().get("stories", {}).values() if isinstance(s, dict) and s.get("chapters")]
        if not stories:
            return None
        return max(stories, key=lambda s: len(s.get("chapters") or []))


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


def _batches(episodes: list[Episode]) -> list[list[Episode]]:
    """Episodes grouped into agent calls: a few episodes each, inside the digest budget."""
    out: list[list[Episode]] = []
    current: list[Episode] = []
    size = 0
    for episode in episodes:
        cost = len(_episode_digest(episode))
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


def _prompts_of(stat: CommitStat, limit: int = 2, chars: int = 220) -> list[str]:
    """The developer's own words behind this commit, cleaned and trimmed."""
    out: list[str] = []
    for raw in [*(stat.user_prompts or []), stat.prompt or ""]:
        text = learn_page._clean_prompt(raw)
        if not text or text in out:
            continue
        out.append(text[:chars])
        if len(out) >= limit:
            break
    return out


def _episode_digest(episode: Episode) -> str:
    """One episode rendered for the agent: when, how big, which files, and for each commit
    its subject plus what the developer asked for."""
    span = _day(episode.start)
    if _day(episode.end) != span:
        span += f" to {_day(episode.end)}"
    head = (
        f"EPISODE {episode.index} [{span}] {len(episode.stats)} commits, "
        f"+{episode.insertions}/-{episode.deletions} lines"
    )
    if episode.tokens:
        head += f", {episode.tokens} tokens"
    lines = [head]
    if episode.paths:
        lines.append("  files: " + ", ".join(episode.paths))
    for stat in episode.stats[:_EPISODE_MAX_COMMITS]:
        lines.append(f"  {stat.short} [{stat.kind}] {stat.subject[:120]}")
        for prompt in _prompts_of(stat):
            lines.append(f'    asked: "{prompt}"')
    return "\n".join(lines) + "\n"


def _batch_prompt(batch: list[Episode], tone: str, context: str) -> str:
    digest = "".join(_episode_digest(episode) for episode in batch)
    voice = STORY_TONES.get(tone, STORY_TONES[DEFAULT_TONE])
    prior = f"\nEARLIER IN THE STORY (do not retell these, continue from them):\n{context}\n" if context else ""
    return f"""Below are consecutive episodes from one repository's history, oldest first. Each episode is a sitting of work: its commits, the lines they moved, and the prompts the developer typed to their coding agent.

Turn them into 1 to {len(batch)} STORY CHAPTERS, in chronological order. A chapter usually covers one episode, but merge neighbouring episodes when they are clearly the same push, and split one episode in two when it obviously contains two different stories. Every commit listed must belong to exactly one chapter.

{voice}

For each chapter:
- "title": short and specific, about what actually happened. No generic titles like "Improvements" or "Updates".
- "kicker": one of {", ".join(KICKERS)}.
- "emoji": a single emoji that fits the chapter.
- "summary": ONE sentence a reader can skim.
- "detail": 2 to 4 short paragraphs in markdown: what changed, why it was needed (use the prompts as evidence), and what it made possible. Mention concrete file or feature names.
- "thoughts": 1 to 3 pivotal moments, each {{"sha": "<a commit id from this material>", "note": "one sentence on what the developer was working out at that moment"}}. Pick the commits whose prompts show the thinking best. Never invent a quote: only the commit id and your note.
- "shas": every commit id belonging to this chapter.
{prior}
Reply with ONE JSON object: {{"chapters": [ ... ]}}

MATERIAL:
{digest}"""


def _arc_prompt(chapters: list[dict], tone: str, repo_name: str) -> str:
    lines = [
        f"{index + 1}. [{chapter.get('when', '')}] {chapter.get('title', '')} ({chapter.get('kicker', '')}): "
        f"{chapter.get('summary', '')}"
        for index, chapter in enumerate(chapters)
    ]
    listing = "\n".join(lines)[:6000]
    voice = STORY_TONES.get(tone, STORY_TONES[DEFAULT_TONE])
    return f"""These are the chapters of the story of a repository called "{repo_name}", in order.

{voice}

Give the whole story a shape:
- "title": a title for the project's story so far. Short, specific to THIS project, not a slogan.
- "tagline": one sentence under the title.
- "arc": 2 to 4 sentences on the overall journey: where it started, what changed along the way, where it stands now.
- "acts": 2 to 5 acts grouping CONSECUTIVE chapters. Each act is {{"title": "...", "blurb": "one sentence", "start": <the number of its first chapter>}}. The first act must start at 1.

Reply with ONE JSON object with exactly those four keys.

CHAPTERS:
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
    """The chain of thought: the developer's REAL prompt for each pivotal commit, paired
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
    # chapter, so a chapter still shows the developer's voice rather than an empty section.
    for sha in claimed:
        stat = by_sha.get(sha)
        quotes = _prompts_of(stat, limit=1, chars=600) if stat is not None else []
        if stat is not None and quotes:
            return [{"sha": stat.sha, "short": stat.short, "quote": quotes[0], "note": "", "at": stat.timestamp}]
    return []


def _chapter_stats(shas: list[str], by_sha: dict[str, CommitStat], sha_paths: dict[str, set[str]]) -> dict:
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
            }
        )
    return rows


def _normalize_chapters(
    raw: object,
    batch: list[Episode],
    by_sha: dict[str, CommitStat],
    sha_paths: dict[str, set[str]],
    used_ids: set[str],
) -> list[dict]:
    """Validate one agent reply into chapters, and make sure the batch's commits are all
    accounted for: an unclaimed commit is folded into the nearest chapter in time rather
    than quietly vanishing from the story."""
    batch_shas = [sha for episode in batch for sha in episode.shas]
    allowed = {sha for sha in batch_shas}
    chapters: list[dict] = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        shas = [sha for sha in _resolve_shas(item.get("shas"), by_sha) if sha in allowed]
        title = _text(item.get("title"), 110)
        if not shas or not title:
            continue
        kicker = str(item.get("kicker") or "").strip().lower()
        chapter = {
            "id": _unique_id(_slug(item.get("id") or title, f"chapter-{index}"), used_ids),
            "title": title,
            "kicker": kicker if kicker in KICKERS else "chapter",
            "emoji": _emoji(item.get("emoji")),
            "summary": _text(item.get("summary"), 400),
            "detail": _text(item.get("detail") or item.get("detail_md"), 6000),
            "shas": shas,
        }
        chapter["thoughts"] = _thoughts(item.get("thoughts"), by_sha, shas)
        chapters.append(chapter)
    if not chapters:
        return []
    _absorb_unclaimed(chapters, batch_shas, by_sha)
    for chapter in chapters:
        chapter["shas"] = sorted(chapter["shas"], key=lambda sha: (by_sha[sha].timestamp, sha))
        _finalize_chapter(chapter, by_sha, sha_paths)
    chapters.sort(key=lambda chapter: (chapter["from"], chapter["to"]))
    return chapters


def _unique_id(base: str, used: set[str]) -> str:
    candidate, suffix = base, 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _absorb_unclaimed(chapters: list[dict], batch_shas: list[str], by_sha: dict[str, CommitStat]) -> None:
    """Give every commit of the batch a home: unclaimed ones join the chapter closest in
    time. Without this a chapter's "3 commits" could silently omit real work."""
    claimed = {sha for chapter in chapters for sha in chapter["shas"]}
    for sha in batch_shas:
        if sha in claimed:
            continue
        when = by_sha[sha].timestamp
        nearest = min(
            chapters,
            key=lambda chapter: min(abs(when - by_sha[other].timestamp) for other in chapter["shas"]),
        )
        nearest["shas"].append(sha)


def _finalize_chapter(chapter: dict, by_sha: dict[str, CommitStat], sha_paths: dict[str, set[str]]) -> None:
    times = [by_sha[sha].timestamp for sha in chapter["shas"] if sha in by_sha]
    chapter["from"] = min(times) if times else 0
    chapter["to"] = max(times) if times else 0
    chapter["when"] = _day(chapter["from"])
    chapter["stats"] = _chapter_stats(chapter["shas"], by_sha, sha_paths)
    chapter["commits"] = _commit_rows(chapter["shas"], by_sha)


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
                "title": headline.subject[:110] or f"{len(episode.stats)} commits",
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


def build_key(root: Path, branch: str) -> str:
    return f"{Path(root)}|{branch or 'HEAD'}"


def _run_json(choice, prompt: str, timeout: int) -> dict:
    """One agent call, serialized against the learn page's calls (a shared lock, so a
    laptop never runs two backend CLIs at once) and against nothing else."""
    if not learn_page._AGENT_LOCK.acquire(timeout=_LOCK_WAIT_SECONDS):
        raise StoryError("The learning agent is busy; try again in a moment.")
    try:
        return learn_page._run_agent_json(choice, _STORY_SYSTEM, prompt, timeout)
    except learn_page.LearnAgentError as exc:
        raise StoryError(str(exc)) from exc
    finally:
        learn_page._AGENT_LOCK.release()


def _select_episodes(episodes: list[Episode], story: dict | None, mode: str) -> tuple[list[Episode], str]:
    """Which episodes this build covers, and where their chapters go.

    * ``rewrite`` - the newest episodes, up to the call budget; the old story is replaced.
    * ``extend`` - only what happened after the stored story's newest covered commit.
    * ``earlier`` - the next slice of history BEFORE the stored story's oldest one.
    """
    if story is None or mode == "rewrite":
        return episodes[-(_MAX_BATCHES * _BATCH_EPISODES) :], "replace"
    covered = set(story.get("covered_shas") or [])
    if mode == "earlier":
        older = [ep for ep in episodes if not (set(ep.shas) & covered)]
        oldest_covered = min((chapter.get("from", 0) for chapter in story.get("chapters") or []), default=0)
        older = [ep for ep in older if ep.end <= oldest_covered or not oldest_covered]
        return older[-(_MAX_BATCHES * _BATCH_EPISODES) :], "prepend"
    fresh = [ep for ep in episodes if not set(ep.shas) <= covered]
    fresh = [ep for ep in fresh if ep.end >= max((chapter.get("to", 0) for chapter in story.get("chapters") or []), default=0)]
    return fresh[: _MAX_BATCHES * _BATCH_EPISODES], "append"


def build_story(
    root: Path,
    stats: list[CommitStat],
    sha_paths: dict[str, set[str]],
    *,
    branch: str = "",
    tone: str = DEFAULT_TONE,
    mode: str = "extend",
    repo_name: str = "",
    key: str = "",
    stop: threading.Event | None = None,
) -> dict[str, Any]:
    """Generate (or extend) the story for ``branch`` and persist it. Blocking: this is what
    the background worker runs. Chapters are saved as each batch lands, so the page shows
    the story appearing rather than a spinner that ends in everything at once."""
    store = StoryStore(root)
    story_key = branch or "HEAD"
    existing = store.get(story_key)
    tone = tone if tone in STORY_TONES else DEFAULT_TONE
    episodes = segment_episodes(stats, sha_paths)
    by_sha: dict[str, CommitStat] = {}
    for stat in story_stats(stats):
        by_sha[stat.sha] = stat
        by_sha[stat.short] = stat
    selected, placement = _select_episodes(episodes, existing, mode)
    if not selected:
        raise StoryError("There is nothing new to tell: the story already covers this branch.")

    batches = _batches(selected)
    truncated_batches = max(0, len(batches) - _MAX_BATCHES)
    batches = batches[-_MAX_BATCHES :] if placement == "prepend" else batches[:_MAX_BATCHES]
    choice = learn_page.resolve_learning_backend(Path(root))
    if key:
        _set_progress(key, phase="reading the commits", done=0, total=len(batches) + 1, chapters=0)

    kept = list(existing.get("chapters") or []) if (existing and placement != "replace") else []
    used_ids = {str(chapter.get("id") or "") for chapter in kept}
    produced: list[dict] = []
    context = ""
    if placement == "append" and kept:
        context = "\n".join(f"- {chapter.get('title', '')}: {chapter.get('summary', '')}" for chapter in kept[-4:])

    for number, batch in enumerate(batches, start=1):
        if stop is not None and stop.is_set():
            raise StoryError("cancelled")
        if key:
            _set_progress(key, phase=f"writing chapters ({number} of {len(batches)})", done=number - 1)
        raw = _run_json(choice, _batch_prompt(batch, tone, context), _CHAPTER_TIMEOUT_SECONDS)
        chapters = _normalize_chapters(raw.get("chapters"), batch, by_sha, sha_paths, used_ids)
        if not chapters:
            raise StoryError("The backend returned no usable chapters; try again.")
        produced.extend(chapters)
        context = "\n".join(f"- {chapter['title']}: {chapter['summary']}" for chapter in produced[-4:])
        if key:
            _set_progress(key, done=number, chapters=len(kept) + len(produced))
        # Persist as we go: a build interrupted after batch 3 leaves 3 real chapters, not
        # nothing, and the page renders them the moment they exist.
        story = _assemble(
            kept, produced, placement, story_key, tone, existing, by_sha, choice, truncated_batches, partial=True
        )
        store.put(story_key, story)

    if key:
        _set_progress(key, phase="finding the shape of it", done=len(batches))
    story = _assemble(
        kept, produced, placement, story_key, tone, existing, by_sha, choice, truncated_batches, partial=False
    )
    try:
        arc = _run_json(choice, _arc_prompt(story["chapters"], tone, repo_name or story_key), _ARC_TIMEOUT_SECONDS)
    except StoryError:
        arc = {}  # the chapters are the story; a missing arc must never lose them
    _apply_arc(story, arc, existing)
    store.put(story_key, story)
    if key:
        _set_progress(key, phase="done", done=len(batches) + 1, chapters=len(story["chapters"]))
    return story


def _assemble(
    kept: list[dict],
    produced: list[dict],
    placement: str,
    story_key: str,
    tone: str,
    existing: dict | None,
    by_sha: dict[str, CommitStat],
    choice,
    truncated_batches: int,
    *,
    partial: bool,
) -> dict[str, Any]:
    if placement == "prepend":
        chapters = [*produced, *kept]
    elif placement == "append":
        chapters = [*kept, *produced]
    else:
        chapters = list(produced)
    chapters.sort(key=lambda chapter: (chapter.get("from", 0), chapter.get("to", 0)))
    covered_shas = sorted({sha for chapter in chapters for sha in chapter.get("shas", [])})
    story: dict[str, Any] = {
        "branch": story_key,
        "tone": tone,
        "chapters": chapters,
        "covered_shas": covered_shas,
        "covered": len(covered_shas),
        "built_at": int(time.time()),
        "partial": partial,
        "engine": {"backend": choice.backend_name, "model": choice.model or ""},
        # More history exists than one build may spend calls on: the page offers to keep
        # going backwards instead of pretending this is the whole story.
        "more_earlier": bool(truncated_batches) if placement != "append" else bool(existing and existing.get("more_earlier")),
    }
    for field_name in ("title", "tagline", "arc", "acts"):
        if existing and existing.get(field_name):
            story[field_name] = existing[field_name]
    return story


def _apply_arc(story: dict[str, Any], arc: dict, existing: dict | None) -> None:
    chapters = story["chapters"]
    title = _text(arc.get("title"), 120)
    if title:
        story["title"] = title
    tagline = _text(arc.get("tagline"), 240)
    if tagline:
        story["tagline"] = tagline
    arc_text = _text(arc.get("arc"), 1200)
    if arc_text:
        story["arc"] = arc_text
    acts = []
    used: set[str] = set()
    for index, item in enumerate(arc.get("acts") if isinstance(arc.get("acts"), list) else []):
        if not isinstance(item, dict):
            continue
        act_title = _text(item.get("title"), 90)
        try:
            start = int(item.get("start") or 0)
        except (TypeError, ValueError):
            continue
        if not act_title or not 1 <= start <= len(chapters):
            continue
        acts.append(
            {
                "id": _unique_id(_slug(act_title, f"act-{index}"), used),
                "title": act_title,
                "blurb": _text(item.get("blurb"), 300),
                "start_id": chapters[start - 1].get("id", ""),
            }
        )
    if acts:
        acts[0]["start_id"] = chapters[0].get("id", "")  # the story always opens in act one
        story["acts"] = acts
    elif existing and existing.get("acts"):
        story["acts"] = existing["acts"]
    story["partial"] = False


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
) -> dict[str, Any]:
    """Kick off a build on a background thread and return immediately.

    A story is several agent calls and minutes of work: holding an HTTP request open for
    that is how you get a page that looks broken. The page polls ``/story/state`` instead,
    which reports progress and the chapters written so far."""
    key = build_key(root, branch)
    with _BUILDS_LOCK:
        if any(record["thread"].is_alive() for record in _BUILDS.values()):
            return {"busy": True, "error": "A story is already being written; let that one finish first."}
        stop = threading.Event()
        progress = {
            "phase": "starting",
            "done": 0,
            "total": 1,
            "chapters": 0,
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
                    key=key,
                    stop=stop,
                )
                _set_progress(key, running=False, phase="done")
            except StoryError as exc:
                _set_progress(key, running=False, phase="stopped", error=str(exc))
            except Exception as exc:  # never lose the reason in a background thread
                _set_progress(key, running=False, phase="stopped", error=f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=work, name="agitrack-story", daemon=True)
        record["thread"] = thread
        thread.start()
    return {"building": True, "progress": _progress(key)}


def cancel_build(root: Path, branch: str = "") -> dict[str, Any]:
    """Ask the running build to stop after the call in flight. Chapters already written
    stay: they are real story, and re-running continues from them."""
    with _BUILDS_LOCK:
        record = _BUILDS.get(build_key(root, branch))
        if record and record["thread"].is_alive():
            record["stop"].set()
            record["progress"]["phase"] = "stopping after the current chapter"
            return {"cancelling": True}
    return {"cancelling": False}


def build_progress(root: Path, branch: str = "") -> dict[str, Any] | None:
    """The current (or last) build's progress for this target, or None if it never ran."""
    return _progress(build_key(root, branch))


# --------------------------------------------------------------------------- page data


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
    covered = set(story.get("covered_shas") or []) if story else set()
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
            "uncovered": sum(1 for stat in kept if stat.sha not in covered),
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
            if mode not in ("extend", "rewrite", "earlier"):
                mode = "extend"
            return start_build(
                root,
                stats,
                sha_paths,
                branch=branch,
                tone=str(body.get("tone") or DEFAULT_TONE),
                mode=mode,
                repo_name=repo_name,
            )
        if path == "/story/cancel":
            return cancel_build(root, branch)
        if path == "/story/forget":
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
    return (
        _STORY_TEMPLATE.replace("__BACKTRACE_BANNER__", banner_html)
        .replace("__REPO_NAME__", _escape(repo_name))
        .replace("__REPO__", _escape(repo_path))
        .replace("__PREBOOT_CSS__", PREBOOT_CSS)
        .replace("__PREBOOT_HTML__", PREBOOT_HTML.replace("the aGiTrack dashboard", "the storyline"))
        .replace("__FONT_LINKS__", FONT_LINKS)
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
        "chapter writes itself.</div>"
    )


_STORY_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>story · __REPO_NAME__ · aGiTrack</title>
__PREBOOT_CSS__
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>📖</text></svg>">
__FONT_LINKS__
<style>
:root{--ink:#070b09;--panel:#0c120e;--panel2:#101813;--line:#1d2a21;--fg:#cfe7d8;--fg-dim:#7e998a;
  --phosphor:#3dffa0;--phosphor-dim:#1f7a52;--accent:#67b8d6;--warn:#ffb454;--bad:#ff6b6b;
  --chipbg:#101813;--amber:#ffb454;--amber-dim:#8a5e2a;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;--display:"VT323",var(--mono)}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
html,body{margin:0;padding:0;background:var(--ink);color:var(--fg);font:14px/1.55 var(--mono);
  overflow-x:hidden}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
[hidden]{display:none !important}

/* Same drifting glow as the learn page, so the three pages feel like one product. */
.ambient{position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.5;
  background:
    radial-gradient(620px 420px at 12% 6%, rgba(61,255,160,.10), transparent 60%),
    radial-gradient(720px 480px at 88% 26%, rgba(103,184,214,.08), transparent 60%),
    radial-gradient(560px 440px at 50% 96%, rgba(255,180,84,.06), transparent 60%);
  animation:drift 36s ease-in-out infinite alternate}
@keyframes drift{from{transform:translate3d(0,0,0) scale(1)}to{transform:translate3d(-30px,20px,0) scale(1.06)}}

.wrap{max-width:1000px;margin:0 auto;padding:22px 20px 80px}
header{display:flex;align-items:baseline;justify-content:space-between;gap:14px;flex-wrap:wrap;
  border-bottom:1px dashed var(--line);padding-bottom:14px;margin-bottom:18px}
.brand{font-family:var(--display);font-weight:400;font-size:38px;line-height:.9;color:var(--phosphor);
  letter-spacing:1.5px;text-shadow:0 0 12px rgba(61,255,160,.5),0 0 44px rgba(61,255,160,.2)}
.brand .a{color:var(--amber);text-shadow:0 0 12px rgba(255,180,84,.5),0 0 44px rgba(255,180,84,.2)}
.brand .sub{font-family:var(--display);font-size:.5em;color:var(--fg-dim);letter-spacing:3px;text-shadow:none}
.meta{color:var(--fg-dim);font-size:12.5px} .meta b{color:var(--fg)}
.navlinks{font-size:12.5px;display:flex;gap:14px;flex-wrap:wrap}
select,input[type=text]{background:var(--panel2);border:1px solid var(--line);color:var(--fg);
  font:inherit;font-size:12.5px;padding:4px 7px;border-radius:4px}

.rise{animation:rise .5s ease both}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion: reduce){
  html{scroll-behavior:auto}
  .rise,.ambient,.spin,.ch,.dot,.confetti span{animation:none !important}
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
.hint{color:var(--fg-dim);font-size:12px;margin-top:8px;line-height:1.5}
.gobtn{font-family:var(--display);font-size:23px;letter-spacing:.5px;background:transparent;
  border:1px solid var(--phosphor);color:var(--phosphor);padding:6px 20px;border-radius:6px;cursor:pointer;
  transition:background .15s,color .15s}
.gobtn:hover{background:var(--phosphor);color:var(--ink)}
.gobtn[disabled]{opacity:.4;cursor:not-allowed}

/* ---------------------------------------------------------------- build progress */
.buildbar{position:sticky;top:0;z-index:30;background:var(--panel);border:1px solid var(--phosphor-dim);
  border-radius:8px;padding:11px 14px;margin:14px 0;display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  box-shadow:0 10px 30px rgba(0,0,0,.5)}
.buildbar .phase{color:var(--phosphor);font-size:13px;flex:1;min-width:200px}
.buildbar .sub{color:var(--fg-dim);font-size:12px}
.spin{width:14px;height:14px;border:2px solid var(--phosphor-dim);border-top-color:var(--phosphor);
  border-radius:50%;animation:spin .8s linear infinite;flex:none}
@keyframes spin{to{transform:rotate(360deg)}}
.pbar{width:100%;height:4px;background:var(--panel2);border-radius:3px;overflow:hidden}
.pbar span{display:block;height:100%;background:var(--phosphor);width:0;transition:width .4s ease}
#flash .notice{border:1px solid var(--amber);color:var(--amber);background:var(--panel);padding:10px 13px;
  border-radius:6px;margin:10px 0;font-size:12.5px;cursor:pointer}
#flash .error{border:1px solid var(--bad);color:var(--bad);background:var(--panel);padding:10px 13px;
  border-radius:6px;margin:10px 0;font-size:12.5px;cursor:pointer}

/* ---------------------------------------------------------------- the toolbar */
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:22px 0 6px}
.toolbar .grow{flex:1}
.kbd{font-size:11.5px;color:var(--fg-dim)}
.kbd b{color:var(--fg);background:var(--panel2);border:1px solid var(--line);border-radius:3px;
  padding:1px 5px;font-weight:400}

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
.more{margin-top:14px;border-top:1px dashed var(--line);padding-top:12px;cursor:default}
.md{font-size:13px;line-height:1.65;color:var(--fg)}
.md p{margin:0 0 10px} .md h3,.md h4{font-size:13px;color:var(--phosphor);margin:14px 0 6px}
.md ul,.md ol{margin:0 0 10px;padding-left:20px} .md li{margin:3px 0}
.md code{background:var(--panel2);border:1px solid var(--line);border-radius:3px;padding:0 4px;font-size:12.5px}
.md pre{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:10px;overflow-x:auto}
.md pre code{border:none;background:none;padding:0}

/* the chain of thought: the developer's own words, quoted from the trace */
.thoughts{margin:16px 0 0}
.thoughts h4{font-size:11.5px;letter-spacing:1.5px;text-transform:uppercase;color:var(--amber);
  margin:0 0 9px;font-weight:600}
.th{border-left:2px solid var(--amber-dim);padding:2px 0 2px 12px;margin:0 0 12px}
.th .q{color:var(--fg);font-size:13px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
.th .q::before{content:"\201C"} .th .q::after{content:"\201D"}
.th .note{color:var(--fg-dim);font-size:12.5px;margin-top:5px}
.th .thmeta{color:var(--phosphor-dim);font-size:11px;margin-top:4px;letter-spacing:.5px}
.caret{display:inline-block;width:7px;background:var(--amber);animation:blink 1s steps(2) infinite;
  margin-left:1px;vertical-align:-1px;height:14px}
@keyframes blink{50%{opacity:0}}

.facts{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 0}
.fact{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:4px 10px;font-size:11.5px;
  color:var(--fg-dim)}
.fact b{color:var(--fg)}
.paths{margin:10px 0 0;display:flex;gap:6px;flex-wrap:wrap}
.paths span{font-size:11.5px;color:var(--fg-dim);background:var(--panel2);border:1px solid var(--line);
  border-radius:4px;padding:2px 7px;word-break:break-all}

.commits{margin:16px 0 0}
.commits h4{font-size:11.5px;letter-spacing:1.5px;text-transform:uppercase;color:var(--phosphor);
  margin:0 0 8px;font-weight:600}
.cmt{border:1px solid var(--line);border-radius:6px;margin:0 0 7px;background:var(--panel2)}
.cmt .chead{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;padding:7px 11px;cursor:pointer;font-size:12.5px}
.cmt .chead:hover{background:#0f1a14}
.cmt .sha{color:var(--phosphor);font-size:12px}
.cmt .subj{flex:1;min-width:180px;color:var(--fg);word-break:break-word}
.cmt .num{color:var(--fg-dim);font-size:11.5px}
.cmt .add{color:var(--phosphor)} .cmt .rem{color:var(--bad)}
.cmt .cbody{border-top:1px solid var(--line);padding:9px 11px}
.diffbox{margin:0;font-size:11.5px;line-height:1.45;overflow-x:auto;white-space:pre}
.diffbox .dl{display:block}
.dfile{color:var(--accent)} .dhunk{color:var(--warn)} .dmeta2{color:var(--fg-dim)}
.dadd{color:var(--phosphor)} .ddel{color:var(--bad)}
.diffempty{color:var(--fg-dim);font-size:12.5px}

/* ---------------------------------------------------------------- the outline */
.outline .row2{display:flex;gap:12px;align-items:baseline;padding:8px 0;border-bottom:1px dashed var(--line);
  flex-wrap:wrap}
.outline .when{color:var(--phosphor-dim);font-size:11.5px;min-width:88px}
.outline .what{flex:1;min-width:220px;color:var(--fg);font-size:12.5px;word-break:break-word}
.outline .num{color:var(--fg-dim);font-size:11.5px}
.outline .said{color:var(--fg-dim);font-size:12px;font-style:italic;margin-top:3px;width:100%}

/* ---------------------------------------------------------------- cinema mode */
body.cinema .studio,body.cinema .toolbar .nocinema{opacity:.25}
.cinebar{position:fixed;left:50%;transform:translateX(-50%);bottom:16px;z-index:70;display:flex;gap:10px;
  align-items:center;background:var(--panel);border:1px solid var(--phosphor-dim);border-radius:999px;
  padding:8px 16px;box-shadow:0 14px 44px rgba(0,0,0,.7);font-size:12.5px}
.cinebar b{color:var(--phosphor)}

.confetti{position:fixed;inset:0;pointer-events:none;z-index:90}
.confetti span{position:absolute;top:-24px;animation:fall 3.2s linear forwards}
@keyframes fall{to{transform:translateY(104vh) rotate(360deg);opacity:0}}

/* The frozen top strips (backtrace notice, static-demo notice), matching the learn page. */
.btbanner{position:sticky;top:0;z-index:60;background:#1a1206;border-bottom:1px solid var(--amber-dim);
  color:var(--amber);font-size:12.5px;padding:8px 14px;line-height:1.5}
.btbanner code{background:#241a09;border:1px solid var(--amber-dim);border-radius:3px;padding:0 5px;color:var(--amber)}
.btbanner a{color:var(--phosphor);text-decoration:none;border-bottom:1px solid var(--phosphor-dim)}
.btbanner a:hover{color:var(--ink);background:var(--phosphor);text-decoration:none}
.updatebanner{background:#1a1206;border-bottom:1px solid var(--amber-dim);color:var(--amber);
  font-size:12.5px;padding:8px 14px}

footer{margin-top:40px;padding-top:14px;border-top:1px dashed var(--line);color:var(--fg-dim);font-size:12px}
footer code{color:var(--fg)}

@media (max-width:640px){
  .wrap{padding:16px 14px 70px}
  .hero h1{font-size:34px}
  .timeline,.act{padding-left:32px}
  .timeline::before{left:9px}
  .ch .dot{left:-32px;width:22px;height:22px;font-size:11px}
  .counter{min-width:calc(50% - 5px);flex:1}
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
    <p class="tagline" id="story-tagline">every commit is a sentence; this is the paragraph they make.</p>
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
    <div class="row" id="actions">
      <button class="gobtn" id="write">&#10024; write the story</button>
      <button class="btn" id="extend" hidden>&#10133; add the new chapters</button>
      <button class="btn" id="earlier" hidden>&#8617; go further back</button>
      <button class="btn" id="rewrite" hidden>&#8635; tell it again</button>
      <button class="btn small" id="forget" hidden>forget this story</button>
    </div>
    <div class="hint" id="studiohint"></div>
  </div>

  <div class="buildbar" id="buildbar" hidden>
    <span class="spin"></span>
    <div style="flex:1;min-width:220px">
      <div class="phase" id="build-phase"></div>
      <div class="pbar"><span id="build-bar"></span></div>
    </div>
    <span class="sub" id="build-sub"></span>
    <button class="btn small" id="build-cancel">stop</button>
  </div>
  <div id="flash"></div>

  <div class="toolbar" id="toolbar" hidden>
    <button class="btn" id="play">&#9654; play the story</button>
    <button class="btn small nocinema" id="expand-all">expand all</button>
    <button class="btn small nocinema" id="collapse-all">collapse all</button>
    <span class="grow"></span>
    <span class="kbd nocinema"><b>j</b>/<b>k</b> move &nbsp; <b>enter</b> open &nbsp; <b>esc</b> close</span>
  </div>

  <div id="timeline"></div>

  <div class="outline" id="outlinewrap" hidden>
    <h2 class="section" id="outlinehead">the shape of it</h2>
    <div class="hint" id="outlinehint"></div>
    <div id="outlinelist"></div>
  </div>

  <footer id="enginenote"></footer>
</div>

<script>
"use strict";
// The document is here and styled: drop the pre-boot overlay that covered its transfer.
{ const pb = document.getElementById("preboot"); if (pb) pb.remove(); }
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const state = {
  data: null, story: null, tone: "playful", branch: "", open: new Set(), told: new Set(),
  poll: null, cinema: false, cineTimer: null, cursor: -1, building: false
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
function post(path, body){
  return fetch(path, {method:"POST", headers:{"Content-Type":"application/json"},
                      body: JSON.stringify(body||{}), cache:"no-store"})
    .then(r => { if(!r.ok) throw new Error("server error " + r.status); return r.json(); });
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
async function load(){
  const url = "story/state" + (state.branch ? "?branch=" + encodeURIComponent(state.branch) : "");
  const r = await fetch(url, {cache:"no-store"});
  if (!r.ok) throw new Error("server error " + r.status);
  state.data = await r.json();
  state.story = state.data.story;
  if (!state.branch && state.data.branch) state.branch = state.data.branch;
  if (state.story && state.story.tone) state.tone = state.story.tone;
  render();
}

function render(){
  const d = state.data; if (!d) return;
  renderBranches();
  renderHero();
  renderStudio();
  renderTimeline();
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
  const tag = (s && s.tagline) || (s ? "" : "every commit is a sentence; this is the paragraph they make.");
  $("story-tagline").textContent = tag;
  $("story-tagline").hidden = !tag;
  const arc = s && s.arc;
  $("story-arc").textContent = arc || "";
  $("story-arc").hidden = !arc;
  const days = m.first && m.last ? Math.max(1, Math.round((m.last - m.first) / 86400)) : 0;
  const chapters = s && s.chapters ? s.chapters.length : 0;
  const cells = [
    [chapters, chapters === 1 ? "chapter" : "chapters"],
    [m.commits || 0, "commits"],
    [m.turns || 0, "agent turns"],
    [days, days === 1 ? "day" : "days"],
  ];
  $("counters").innerHTML = cells.map(c => '<div class="counter"><b data-n="' + c[0] + '">0</b><span>' + esc(c[1]) + "</span></div>").join("");
  for (const b of $("counters").querySelectorAll("b")) countUp(b, Number(b.dataset.n));
}

function renderStudio(){
  const d = state.data, s = state.story, m = d.meta || {};
  const has = !!(s && s.chapters && s.chapters.length);
  const engineBroken = !!(d.engine && d.engine.error);
  $("write").hidden = has;
  $("extend").hidden = !has || !m.uncovered;
  $("earlier").hidden = !(has && s.more_earlier);
  $("rewrite").hidden = !has;
  $("forget").hidden = !has;
  if (m.uncovered && has) $("extend").innerHTML = "&#10133; add the " + m.uncovered + " new commit" + (m.uncovered === 1 ? "" : "s");
  for (const id of ["write","extend","earlier","rewrite"]) $(id).disabled = state.building || engineBroken;
  let hint;
  if (engineBroken)
    hint = "No coding agent backend is configured here, so nobody can write the story yet. Run an aGiTrack session in this repo, or set learning_backend in .agitrack/config.json. The outline below is built from the commits themselves and needs no agent.";
  else if (!has)
    hint = "Your coding agent reads the commits and the prompts behind them, then writes the story of this " +
           (d.backtrace ? "reconstructed history" : "branch") + ": chapters you can open, with the moments that turned it. " +
           "It takes a few minutes and a handful of agent calls, and the result is stored in .agitrack/story.json, so you only pay for it once.";
  else
    hint = "Chapters are stored in .agitrack/story.json and kept as new commits land. " +
           (m.uncovered ? m.uncovered + " commit" + (m.uncovered === 1 ? " has" : "s have") + " landed since this story was written." : "The story covers everything on this branch.");
  $("studiohint").textContent = hint;
}

function actMap(){
  const s = state.story, map = {};
  ((s && s.acts) || []).forEach((a, i) => { if (a.start_id) map[a.start_id] = Object.assign({n: i + 1}, a); });
  return map;
}

function renderTimeline(){
  const s = state.story, host = $("timeline");
  if (!s || !s.chapters || !s.chapters.length) { host.innerHTML = ""; $("toolbar").hidden = true; return; }
  $("toolbar").hidden = false;
  const acts = actMap();
  let html = "";
  let inTimeline = false;
  s.chapters.forEach((c, i) => {
    const act = acts[c.id];
    if (act) {
      if (inTimeline) { html += "</div>"; inTimeline = false; }
      html += '<div class="act"><div class="actno">act ' + act.n + '</div><h2>' + esc(act.title) + "</h2>" +
              (act.blurb ? "<p>" + esc(act.blurb) + "</p>" : "") + "</div>";
    }
    if (!inTimeline) { html += '<div class="timeline">'; inTimeline = true; }
    html += chapterHtml(c, i);
  });
  if (inTimeline) html += "</div>";
  host.innerHTML = html;
  for (const id of state.open) {
    const el = document.getElementById("ch-" + id);
    if (el) { el.classList.add("open"); fillChapter(el, false); }
  }
}

function chapterHtml(c, i){
  const st = c.stats || {};
  const bits = [];
  if (st.commits) bits.push(st.commits + " commit" + (st.commits === 1 ? "" : "s"));
  if (st.ins || st.del) bits.push('<span class="add">+' + num(st.ins) + '</span>/<span class="rem">-' + num(st.del) + "</span>");
  if (st.tokens) bits.push(num(st.tokens) + " tokens");
  return '<article class="ch" id="ch-' + esc(c.id) + '" data-id="' + esc(c.id) + '" data-i="' + i + '"' +
      ' style="animation-delay:' + Math.min(i * 40, 400) + 'ms">' +
    '<div class="dot">' + esc(c.emoji || "✦") + "</div>" +
    '<div class="chbody">' +
      '<div class="chhead"><span>' + esc(c.when || "") + "</span>" +
        '<span class="kick k-' + esc(String(c.kicker || "chapter").replace(/\s+/g, "-")) + '">' + esc(c.kicker || "chapter") + "</span>" +
        '<span>' + bits.join(" &nbsp;·&nbsp; ") + "</span></div>" +
      "<h3>" + esc(c.title) + "</h3>" +
      '<p class="sum">' + esc(c.summary || "") + "</p>" +
      '<div class="more" hidden></div>' +
      '<div class="open-hint">read the chapter &darr;</div>' +
    "</div></article>";
}

// The chapter body is built on first open: several hundred chapters' worth of detail
// would otherwise be parsed into the DOM for a page most readers skim.
function fillChapter(el, animate){
  const c = chapterById(el.dataset.id); if (!c) return;
  const box = el.querySelector(".more");
  box.hidden = false;
  el.querySelector(".open-hint").innerHTML = "close &uarr;";
  if (box.dataset.filled === "1") return;
  box.dataset.filled = "1";
  const st = c.stats || {};
  let html = '<div class="detail md">' + md(c.detail || c.summary || "") + "</div>";
  if (c.thoughts && c.thoughts.length) {
    html += '<div class="thoughts"><h4>&#129504; chain of thought</h4>' + c.thoughts.map((t, i) =>
      '<div class="th" data-i="' + i + '">' +
        '<div class="q" data-full="' + esc(t.quote || "") + '">' + esc(t.quote || "") + "</div>" +
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
    html += '<div class="commits"><h4>&#128220; the commits behind it</h4>' + c.commits.map(r =>
      '<div class="cmt" data-sha="' + esc(r.sha) + '">' +
        '<div class="chead"><span class="sha">' + esc(r.short) + "</span>" +
          '<span class="subj">' + esc(r.subject) + "</span>" +
          '<span class="num"><span class="add">+' + num(r.ins) + '</span>/<span class="rem">-' + num(r["del"]) + "</span></span>" +
          '<span class="num">show diff &#9662;</span></div>' +
      "</div>").join("") + "</div>";
  }
  box.innerHTML = html;
  if (animate && !REDUCED) typeThoughts(box);
}

// The developer's own words, typed out. Only on the first open of a chapter, and only for
// the first quote: this is a flourish, not a wait.
function typeThoughts(box){
  const q = box.querySelector(".th .q"); if (!q) return;
  const full = q.dataset.full || ""; if (full.length > 400) return;
  q.textContent = "";
  const caret = document.createElement("span"); caret.className = "caret";
  q.parentNode.insertBefore(caret, q.nextSibling);
  let i = 0;
  const tick = setInterval(() => {
    q.textContent = full.slice(0, i += Math.max(1, Math.round(full.length / 90)));
    if (i >= full.length) { clearInterval(tick); q.textContent = full; caret.remove(); }
  }, 16);
}

function chapterById(id){
  const s = state.story;
  return ((s && s.chapters) || []).find(c => c.id === id) || null;
}

function toggleChapter(el, force){
  const id = el.dataset.id;
  const wantOpen = force === undefined ? !state.open.has(id) : force;
  if (wantOpen) {
    state.open.add(id);
    el.classList.add("open");
    fillChapter(el, !state.told.has(id));
    state.told.add(id);
    if (history.replaceState) history.replaceState(null, "", "#ch-" + id);
  } else {
    state.open.delete(id);
    el.classList.remove("open");
    const box = el.querySelector(".more");
    if (box) box.hidden = true;
    el.querySelector(".open-hint").innerHTML = "read the chapter &darr;";
  }
}

// ------------------------------------------------------------------ diffs
const _diffCache = {};
async function toggleDiff(row){
  const sha = row.dataset.sha;
  const open = row.querySelector(".cbody");
  const label = row.querySelector(".chead .num:last-child");
  if (open) { open.remove(); if (label) label.innerHTML = "show diff &#9662;"; return; }
  if (label) label.innerHTML = "hide diff &#9652;";
  const body = document.createElement("div");
  body.className = "cbody";
  body.innerHTML = '<div class="diffempty">loading the diff…</div>';
  row.appendChild(body);
  if (_diffCache[sha]) { body.innerHTML = _diffCache[sha]; return; }
  try {
    const r = await fetch("diff?sha=" + encodeURIComponent(sha), {cache:"no-store"});
    const d = await r.json();
    const html = d.error ? '<div class="diffempty">' + esc(d.error) + "</div>" : diffHtml(d.diff, d.truncated);
    _diffCache[sha] = html;
    body.innerHTML = html;
  } catch (e) {
    body.innerHTML = '<div class="diffempty">could not load this diff (server unreachable)</div>';
  }
}
function diffHtml(text, truncated){
  const t = (text || "").trim();
  if (!t) return '<div class="diffempty">' + (state.data && state.data.backtrace
    ? "No file changes were recovered for this turn. A backtrace reads the agent's transcript, which records file-editing tool calls but not every way code can change (shell commands, formatters, generated files), so their diffs cannot be recovered after the fact."
    : "no changes to show for this commit") + "</div>";
  if (/Binary files .* differ/.test(t) && !/^@@/m.test(t))
    return '<div class="diffempty">binary file, no text diff to show</div>';
  const rows = t.replace(/\r\n/g, "\n").split("\n").map(raw => {
    let cls = "dl";
    if (/^(diff --git |index |new file|deleted file|similarity |rename |old mode|new mode)/.test(raw)) cls = "dl dfile";
    else if (raw.startsWith("@@")) cls = "dl dhunk";
    else if (raw.startsWith("+++") || raw.startsWith("---")) cls = "dl dmeta2";
    else if (raw.startsWith("+")) cls = "dl dadd";
    else if (raw.startsWith("-")) cls = "dl ddel";
    return '<span class="' + cls + '">' + (esc(raw) || "&nbsp;") + "</span>";
  });
  return '<pre class="diffbox">' + rows.join("") + "</pre>" +
         (truncated ? '<div class="diffempty">…diff truncated (very large diff)</div>' : "");
}

// ------------------------------------------------------------------ the outline
function renderOutline(){
  const d = state.data, rows = d.outline || [];
  const has = !!(state.story && state.story.chapters && state.story.chapters.length);
  $("outlinewrap").hidden = !rows.length;
  $("outlinehead").textContent = has ? "the raw material" : "the shape of it, before anyone tells it";
  $("outlinehint").textContent = has
    ? "Every sitting of work on this branch, grouped by when it happened. This is what the storyteller reads."
    : "aGiTrack grouped the commits into sittings of work. Press the button above and your agent turns these into chapters, with the moments that turned them.";
  $("outlinelist").innerHTML = rows.map(r =>
    '<div class="row2"><span class="when">' + esc(r.when) + "</span>" +
      '<span class="what">' + esc(r.title) + "</span>" +
      '<span class="num">' + r.commits + " commit" + (r.commits === 1 ? "" : "s") +
        (r.ins || r["del"] ? " &nbsp;+" + num(r.ins) + "/-" + num(r["del"]) : "") + "</span>" +
      (r.prompt ? '<div class="said">&#8220;' + esc(r.prompt) + "&#8221;</div>" : "") +
    "</div>").join("");
}

function renderEngine(){
  const e = (state.data && state.data.engine) || {};
  $("enginenote").innerHTML = e.error
    ? esc(e.error)
    : "chapters written by <code>" + esc(e.backend || "?") + "</code> · " + esc(e.model || "backend default model") +
      " · stored in <code>.agitrack/story.json</code>";
}

// ------------------------------------------------------------------ building
function renderBuild(p){
  const running = !!(p && p.running);
  state.building = running;
  $("buildbar").hidden = !running;
  if (p && p.error && !running) fail(p.error === "cancelled" ? "Stopped. The chapters already written are kept." : p.error);
  if (!running) { renderStudio(); return; }
  $("build-phase").textContent = p.phase || "working";
  const pct = p.total ? Math.round(100 * Math.min(1, p.done / p.total)) : 5;
  $("build-bar").style.width = Math.max(5, pct) + "%";
  $("build-sub").textContent = (p.chapters || 0) + " chapter" + (p.chapters === 1 ? "" : "s") + " so far";
  renderStudio();
}

async function build(mode){
  if (state.building) return;
  flash("");
  state.building = true; renderStudio();
  try {
    const r = await post("story/build", {branch: state.branch, tone: state.tone, mode: mode});
    if (r.error || r.busy) { state.building = false; fail(r.error || "busy"); renderStudio(); return; }
    renderBuild(r.progress);
    startPolling();
  } catch (e) { state.building = false; fail(e.message); renderStudio(); }
}

function startPolling(){
  stopPolling();
  state.poll = setInterval(async () => {
    try {
      const before = state.story ? (state.story.chapters || []).length : 0;
      await load();
      const after = state.story ? (state.story.chapters || []).length : 0;
      const p = state.data.building;
      if (after > before && after) {
        // A chapter landed while we watched: bring it into view, gently.
        const last = state.story.chapters[after - 1];
        const el = last && document.getElementById("ch-" + last.id);
        if (el && !state.cinema) el.scrollIntoView({block: "center", behavior: REDUCED ? "auto" : "smooth"});
      }
      if (!p || !p.running) { stopPolling(); if (after) celebrate(); }
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

// ------------------------------------------------------------------ cinema mode
function chapterEls(){ return Array.from(document.querySelectorAll(".ch")); }

function goTo(i, {open = true} = {}){
  const els = chapterEls(); if (!els.length) return;
  state.cursor = Math.max(0, Math.min(els.length - 1, i));
  const el = els[state.cursor];
  if (open) toggleChapter(el, true);
  el.classList.add("cue");
  setTimeout(() => el.classList.remove("cue"), 1200);
  el.scrollIntoView({block: "start", behavior: REDUCED ? "auto" : "smooth"});
}

function playStory(){
  if (state.cinema) return stopCinema();
  const els = chapterEls(); if (!els.length) return;
  state.cinema = true;
  document.body.classList.add("cinema");
  $("play").innerHTML = "&#9632; stop";
  for (const el of els) toggleChapter(el, false);
  const bar = document.createElement("div");
  bar.className = "cinebar";
  bar.id = "cinebar";
  bar.innerHTML = '<span>story mode</span><b id="cine-pos"></b><button class="btn small" id="cine-prev">&larr;</button>' +
                  '<button class="btn small" id="cine-next">&rarr;</button><button class="btn small" id="cine-stop">stop</button>';
  document.body.appendChild(bar);
  $("cine-prev").onclick = () => cineStep(-1);
  $("cine-next").onclick = () => cineStep(1);
  $("cine-stop").onclick = stopCinema;
  state.cursor = -1;
  cineStep(1);
}

function cineStep(delta){
  const els = chapterEls();
  const next = state.cursor + delta;
  if (next < 0 || next >= els.length) return stopCinema();
  if (state.cursor >= 0 && els[state.cursor]) toggleChapter(els[state.cursor], false);
  goTo(next);
  const pos = $("cine-pos");
  if (pos) pos.textContent = (state.cursor + 1) + " / " + els.length;
  clearTimeout(state.cineTimer);
  state.cineTimer = setTimeout(() => cineStep(1), 11000);
}

function stopCinema(){
  state.cinema = false;
  clearTimeout(state.cineTimer);
  document.body.classList.remove("cinema");
  $("play").innerHTML = "&#9654; play the story";
  const bar = $("cinebar"); if (bar) bar.remove();
}

function openFromHash(){
  const m = /^#ch-(.+)$/.exec(location.hash || "");
  if (!m) return;
  const el = document.getElementById("ch-" + m[1]);
  if (!el) return;
  toggleChapter(el, true);
  state.cursor = chapterEls().indexOf(el);
  setTimeout(() => el.scrollIntoView({block: "start", behavior: "auto"}), 30);
}

// ------------------------------------------------------------------ wiring
$("timeline").addEventListener("click", e => {
  const head = e.target.closest(".cmt .chead");
  if (head) { toggleDiff(head.parentElement); return; }
  if (e.target.closest(".more") && !e.target.closest(".open-hint")) return;  // reading, not toggling
  const ch = e.target.closest(".ch");
  if (!ch) return;
  toggleChapter(ch);
  state.cursor = chapterEls().indexOf(ch);
});
$("tone-chips").addEventListener("click", e => {
  const chip = e.target.closest(".chip"); if (!chip) return;
  state.tone = chip.dataset.v;
  for (const c of $("tone-chips").querySelectorAll(".chip")) c.classList.toggle("sel", c === chip);
});
$("write").addEventListener("click", () => build("rewrite"));
$("extend").addEventListener("click", () => build("extend"));
$("earlier").addEventListener("click", () => build("earlier"));
$("rewrite").addEventListener("click", () => build("rewrite"));
$("forget").addEventListener("click", async () => {
  try { await post("story/forget", {branch: state.branch}); state.open.clear(); await load(); notice("Forgotten. The commits are untouched; the telling is gone."); }
  catch (e) { fail(e.message); }
});
$("build-cancel").addEventListener("click", async () => {
  try { await post("story/cancel", {branch: state.branch}); } catch (e) {}
});
$("play").addEventListener("click", playStory);
$("expand-all").addEventListener("click", () => { for (const el of chapterEls()) toggleChapter(el, true); });
$("collapse-all").addEventListener("click", () => { for (const el of chapterEls()) toggleChapter(el, false); });
$("f-branch").addEventListener("change", () => {
  state.branch = $("f-branch").value; state.open.clear();
  load().catch(e => fail(e.message));
});
$("backlink").addEventListener("click", e => {
  // Returning to the dashboard through history restores it from the back/forward cache
  // instead of recomputing its whole commit history.
  try {
    if (document.referrer && new URL(document.referrer).origin === location.origin && history.length > 1) {
      e.preventDefault(); history.back();
    }
  } catch (err) {}
});
document.addEventListener("keydown", e => {
  if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
  if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); goTo(state.cursor + 1, {open: false}); }
  else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); goTo(Math.max(0, state.cursor - 1), {open: false}); }
  else if (e.key === "Enter") { const el = chapterEls()[state.cursor]; if (el) { e.preventDefault(); toggleChapter(el); } }
  else if (e.key === "Escape") { if (state.cinema) stopCinema(); else for (const el of chapterEls()) toggleChapter(el, false); }
  else if (e.key === " " && state.cinema) { e.preventDefault(); stopCinema(); }
});
window.addEventListener("hashchange", openFromHash);

load().catch(e => fail("could not load the story: " + e.message));
// A build started from another tab (or still running from before this page loaded) keeps
// the page live without anyone pressing anything.
setTimeout(() => { if (state.data && state.data.building && state.data.building.running) startPolling(); }, 100);
</script>
</body>
</html>
"""

