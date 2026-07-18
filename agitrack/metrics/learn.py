"""The dashboard's interactive learning page (``/learn``).

Where the efficiency insights point at habits (a single deterministic pass, no LLM),
the learn page goes one step deeper: it hands the same filtered interaction traces to
the coding-agent BACKEND and asks it to coach the person behind them. The backend

* reads a compact digest of the traces (prompts, rework hotspots, efficiency insights,
  repo shape) for the selected trace source (your own sessions, a teammate's, or the
  whole team) and date slice,
* assesses what the learner already knows and where the traces show knowledge gaps,
* proposes a handful of small lessons sized to how much time the learner has and how
  fresh they feel right now (the learner never has to know what to ask for), and
* on request writes the full lesson: general coding knowledge that makes agent-driven
  work more effective, knowledge about THIS codebase, external links, a quick check,
  and a hands-on exercise whose attempt the mentor reviews.

Progress is per USER (GitHub id, via :func:`agitrack.sessions.identity.github_login`)
and tracked automatically (opened, completed, time spent, quiz results, exercise
attempts) in ``.agitrack/learning.json`` (git-ignored, local). Optionally the progress
log syncs to git the same way shared sessions do: a history-free orphan ref
(``refs/agitrack/learning-progress``, layout ``<repo-fingerprint>/<github-id>/
progress.json``) pushed best-effort with a lease, so teammates' dashboards can see it.
Off by default; a toggle on the page turns it on.

Backend/model selection: the ``learning_backend`` / ``learning_model`` config keys
(repo ``.agitrack/config.json`` overrides ``~/.agitrack/config.json``) win when set;
otherwise the latest session's backend and model from ``state.json``. The page's
"coach engine" panel edits the repo-scope keys in place. All agent calls are ``bare``
one-shots run from a scratch directory outside any repository (the same isolation the
summarizer uses, so learn sessions can never be adopted or resumed as the user's
coding conversation) and work identically on Claude and OpenCode.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agitrack.backends.base import AgentBackend
from agitrack.env import getenv_compat
from agitrack.git import GitRepo
from agitrack.metrics.collect import CommitStat
from agitrack.summaries.model_select import compatible_summarization_model

# The turn kinds that represent agent work (mirrors the dashboard's AI_KINDS).
_AI_KINDS = {"agent", "covered", "agent-merge"}

# Bare-run bounds. A lesson is a much larger completion than a summary, so these override
# the backends' 90s summarizer cap (see ``timeout_seconds`` in backends/base.py).
_SUGGEST_TIMEOUT_SECONDS = 240
_LESSON_TIMEOUT_SECONDS = 360
_CHAT_TIMEOUT_SECONDS = 180

# One learning agent call at a time: each call spawns a full backend CLI process, and the
# dashboard server is threaded, so an eager double-click would otherwise run two.
_AGENT_LOCK = threading.Lock()
# Store writes are read-modify-write; serialize them across request threads.
_STORE_LOCK = threading.Lock()

# Keep the digest comfortably inside a bare prompt.
_DIGEST_CHAR_LIMIT = 9000

# Below this many tracked agent turns in the selected slice there is nothing meaningful to
# personalize from: the check-in then explains how to get a trace (backtrace, or running
# sessions through aGiTrack) and offers starter topics instead of calling the agent.
_MIN_TRACE_TURNS = 3

# The starter topics offered when the selection has (almost) no captured trace. They flow
# through the normal lesson pipeline: tapping one still generates a personal, full lesson
# (the digest simply carries little beyond the repo shape and README).
_STARTER_SUGGESTIONS = [
    {
        "id": "agitrack-first-session",
        "title": "Your first tracked session with aGiTrack",
        "minutes": 10,
        "kind": "coding",
        "gap_id": "",
        "why": "No captured trace here yet, so this is the natural place to start.",
        "teaser": "Run one agent turn through aGiTrack and see it land as a commit with its full story.",
    },
    {
        "id": "agitrack-pick-backend",
        "title": "Picking and setting up a coding agent backend",
        "minutes": 5,
        "kind": "coding",
        "gap_id": "",
        "why": "aGiTrack drives a coding agent backend for you; choosing one is step zero.",
        "teaser": "Know which supported agent fits you and have it running in minutes.",
    },
    {
        "id": "agitrack-backtrace",
        "title": "Reconstruct your past agent work with backtrace",
        "minutes": 10,
        "kind": "coding",
        "gap_id": "",
        "why": "If this code was written with a supported agent outside aGiTrack, its history can be recovered.",
        "teaser": "Turn the sessions already on your machine into a dashboard, and optionally real commits.",
    },
    {
        "id": "driving-agents-well",
        "title": "Habits that make coding agents work better",
        "minutes": 15,
        "kind": "coding",
        "gap_id": "",
        "why": "Good prompting and verification habits pay off from the very first session.",
        "teaser": "Smaller asks, clear checks, fewer correction loops.",
    },
]


def _no_trace_message(turns: int) -> str:
    found = f"only {turns} aGiTrack-tracked agent turn(s)" if turns else "no aGiTrack-tracked agent turns"
    return (
        f"I found {found} in this selection, not enough to personalize lessons from. "
        "If this code was written with a supported agent (Claude Code or OpenCode) outside aGiTrack, "
        "run 'agitrack --backtrace' to reconstruct that history from the transcripts on your machine, "
        "preferably 'agitrack --backtrace commit' so it becomes part of a branch I can read. "
        "Otherwise, simply launch your next coding session with 'agitrack': every turn is captured "
        "automatically and lessons will personalize as the trace grows. Until then, here are some "
        "starter topics:"
    )


# The progress-sync ref: history-free orphan commits, one progress.json per user,
# scoped by repo fingerprint — the same shape as refs/agitrack/shared-sessions.
PROGRESS_REF = "refs/agitrack/learning-progress"
_SYNC_THROTTLE_SECONDS = 60.0
_PROGRESS_FETCH_TTL = 300.0
_sync_at: dict[str, float] = {}
_progress_fetch_at: dict[str, float] = {}

# The learner's GitHub id is resolved via `gh api user` (a network call); cache it.
_identity_cache: dict[str, tuple[float, str]] = {}
_IDENTITY_TTL = 3600.0


class LearnAgentError(RuntimeError):
    """The learning backend call failed or returned unusable output."""


def learning_scratch_dir() -> Path:
    """A stable directory, outside any repository, for learning-page backend calls.

    Same reasoning as the summarizer's scratch dir (issues #8/#56): headless backend
    calls record a real session keyed by their working directory, and running them in
    the repo would make the lesson conversation adoptable/resumable as the user's own.
    """
    config_dir = getenv_compat("CONFIG_DIR")
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".agitrack"
    path = base / "learning"
    path.mkdir(parents=True, exist_ok=True)
    return path


def learner_id(root: Path, repo: GitRepo | None) -> str:
    """The current user's identity for progress keying: their GitHub login (slugged),
    falling back to the git user.name (when ``repo`` is a git repo; the backtrace view
    can serve a plain directory, where only the gh login is available). Cached;
    ``gh api user`` is a network call."""
    key = str(root)
    now = time.monotonic()
    hit = _identity_cache.get(key)
    if hit and now - hit[0] < _IDENTITY_TTL:
        return hit[1]
    from agitrack.sessions.identity import github_login

    gid = github_login(repo)
    _identity_cache[key] = (now, gid)
    return gid


@dataclass
class LearningBackendChoice:
    """The backend+model the learn page will generate content with, and where each came
    from (``config`` when pinned via learning_backend/learning_model, else ``session``)."""

    backend_name: str
    model: str | None
    backend_source: str
    model_source: str

    def build(self) -> AgentBackend:
        from agitrack.backends.claude import ClaudeBackend
        from agitrack.backends.opencode import OpenCodeBackend
        from agitrack.config.settings import GlobalConfig

        config = GlobalConfig()
        launch = config.backend_command(self.backend_name)
        backend_class = OpenCodeBackend if self.backend_name == "opencode" else ClaudeBackend
        return backend_class(learning_scratch_dir(), launch_command=launch)


def resolve_learning_backend(repo_root: Path) -> LearningBackendChoice:
    """Which backend/model generates learning content for this repo.

    Precedence: the ``learning_backend`` / ``learning_model`` config keys (repo overlay
    over global) win; otherwise the LATEST SESSION's backend and model recorded in
    ``.agitrack/state.json``; otherwise the configured ``default_backend``. A model id
    that belongs to the other backend's format is dropped (the backend then uses its own
    default), exactly like the summarizer's model handling. Raises
    :class:`LearnAgentError` when no backend can be determined at all.
    """
    from agitrack.config.settings import GlobalConfig
    from agitrack.config.state import AgitrackState

    config = GlobalConfig()
    config.load_repo_overlay(repo_root)
    state = AgitrackState(repo_root)

    known = {"claude", "opencode"}
    backend_name, backend_source = config.learning_backend, "config"
    if backend_name not in known:
        backend_name, backend_source = state.data.get("backend"), "session"
    if backend_name not in known:
        backend_name, backend_source = config.default_backend, "session"
    if backend_name not in known:
        raise LearnAgentError(
            "No coding agent backend is configured for learning content. Run an aGiTrack "
            "session in this repo first, or set learning_backend in .agitrack/config.json."
        )

    model, model_source = config.learning_model, "config"
    if not model:
        model, model_source = state.model, "session"
    model = compatible_summarization_model(backend_name, model)
    return LearningBackendChoice(
        backend_name=str(backend_name), model=model, backend_source=backend_source, model_source=model_source
    )


# --------------------------------------------------------------------------- store


class LearnStore:
    """``.agitrack/learning.json``: per-user profiles (assessment, gaps, suggestions,
    lessons, progress) plus the sync toggle. Local plumbing next to state.json; atomic
    writes so an interrupted request can't truncate the file."""

    def __init__(self, repo_root: Path) -> None:
        self.path = Path(repo_root) / ".agitrack" / "learning.json"
        self.root = Path(repo_root)

    def load(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"profiles": {}}
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
            return {"profiles": {}}
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Best-effort: keep .agitrack/ git-ignored even in a repo that never ran aGiTrack
        # (the learn page may be the first thing to write here, e.g. under --backtrace).
        # A no-op outside a git repo.
        try:
            from agitrack.config.state import AgitrackState

            AgitrackState(self.root).ensure_local_ignore()
        except Exception:
            pass
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.path)

    @staticmethod
    def profile(data: dict[str, Any], gid: str) -> dict[str, Any]:
        """Get-or-create the profile for user ``gid``."""
        profiles = data.setdefault("profiles", {})
        profile = profiles.get(gid)
        if not isinstance(profile, dict):
            profile = {"assessment": "", "gaps": [], "suggestions": [], "lessons": []}
            profiles[gid] = profile
        profile.setdefault("assessment", "")
        for field in ("gaps", "suggestions", "lessons"):
            if not isinstance(profile.get(field), list):
                profile[field] = []
        return profile

    def update(self, gid: str, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Read-modify-write the user's profile under the store lock; returns it."""
        with _STORE_LOCK:
            data = self.load()
            profile = self.profile(data, gid)
            mutate(profile)
            self.save(data)
            return profile


def _find_by_id(items: list, item_id: str) -> dict[str, Any] | None:
    for item in items:
        if isinstance(item, dict) and item.get("id") == item_id:
            return item
    return None


# --------------------------------------------------------------------------- digest


def _clean_prompt(text: str) -> str:
    # Strip synthetic background-task markers (they are not something the user typed)
    # and collapse whitespace; mirrors the insights module's handling.
    text = text.replace("(background task completed)", " ")
    return re.sub(r"\s+", " ", text).strip()


def build_trace_digest(
    stats: list[CommitStat],
    insights: list[dict],
    file_rows: list[dict],
    repo_root: Path,
    profile: dict[str, Any],
    *,
    limit: int = _DIGEST_CHAR_LIMIT,
) -> str:
    """A compact plain-text digest of the filtered interaction traces, the evidence the
    learning agent reasons over. Everything in it is already on the dashboard: commit
    stats, the per-file change index, and the efficiency-insight cards. Capped so it
    always fits a bare prompt."""
    parts: list[str] = []
    turns = [stat for stat in stats if stat.kind in _AI_KINDS]
    if turns:
        first = min((s.timestamp for s in turns if s.timestamp), default=0)
        last = max((s.timestamp for s in turns if s.timestamp), default=0)
        span = ""
        if first and last:
            fmt = "%Y-%m-%d"
            span = (
                f" between {time.strftime(fmt, time.localtime(first))} and {time.strftime(fmt, time.localtime(last))}"
            )
        parts.append(f"WINDOW: {len(turns)} agent turns{span}.")

    prompts: list[str] = []
    seen: set[str] = set()
    for stat in reversed(turns):  # newest first
        for raw in [*(stat.user_prompts or []), stat.prompt or ""]:
            text = _clean_prompt(raw)
            if not text or text in seen:
                continue
            seen.add(text)
            prompts.append(text[:180])
            if len(prompts) >= 30:
                break
        if len(prompts) >= 30:
            break
    if prompts:
        parts.append("RECENT USER PROMPTS TO THE AGENT (newest first):\n- " + "\n- ".join(prompts))

    if insights:
        lines = []
        for card in insights[:6]:
            line = f"{card.get('title', '')}: {card.get('summary', '')}"
            if card.get("suggestion"):
                line += f" (suggested: {card['suggestion']})"
            lines.append(line.strip())
        parts.append("EFFICIENCY INSIGHTS (computed from the same traces):\n- " + "\n- ".join(lines))

    rows = sorted(
        (row for row in file_rows if isinstance(row, dict) and row.get("path")),
        key=lambda row: -int(row.get("changes") or 0),
    )[:14]
    if rows:
        lines = [
            f"{row['path']} ({row.get('changes', 0)} changes, +{row.get('insertions', 0)}/-{row.get('deletions', 0)})"
            for row in rows
        ]
        parts.append("MOST-CHANGED FILES:\n- " + "\n- ".join(lines))

    exts: dict[str, int] = {}
    tops: dict[str, int] = {}
    for row in file_rows:
        path = str(row.get("path") or "")
        if not path:
            continue
        suffix = Path(path).suffix
        if suffix:
            exts[suffix] = exts.get(suffix, 0) + 1
        head = path.split("/", 1)[0]
        tops[head] = tops.get(head, 0) + 1
    if exts:
        shape = ", ".join(f"{ext} x{count}" for ext, count in sorted(exts.items(), key=lambda kv: -kv[1])[:8])
        dirs = ", ".join(name for name, _ in sorted(tops.items(), key=lambda kv: -kv[1])[:8])
        parts.append(f"REPO SHAPE: file types {shape}; top-level areas: {dirs}.")

    for name in ("README.md", "README.rst", "README"):
        readme = Path(repo_root) / name
        if readme.is_file():
            try:
                head = readme.read_text(encoding="utf-8", errors="replace")[:1500].strip()
            except OSError:
                break
            if head:
                parts.append(f"README (start):\n{head}")
            break

    lessons = [lesson for lesson in profile.get("lessons", []) if lesson.get("title")]
    done = [lesson for lesson in lessons if lesson.get("status") == "completed"]
    in_progress = [lesson for lesson in lessons if lesson.get("status") != "completed"]
    if done:
        titles = ", ".join(str(lesson.get("title", "")) for lesson in done[-10:])
        parts.append(f"ALREADY LEARNED (completed lessons; never repeat these, build on them): {titles}.")
    if in_progress:
        titles = ", ".join(str(lesson.get("title", "")) for lesson in in_progress[-10:])
        parts.append(f"ALREADY IN PROGRESS (lessons started; do not repeat these either): {titles}.")
    addressed = [gap for gap in profile.get("gaps", []) if gap.get("status") == "addressed"]
    if addressed:
        parts.append("GAPS ALREADY ADDRESSED: " + ", ".join(str(gap.get("title", "")) for gap in addressed[-10:]) + ".")

    digest = "\n\n".join(parts)
    return digest[:limit]


# --------------------------------------------------------------------------- agent calls


def _extract_json(text: str) -> dict | None:
    """The first JSON object in ``text``. Tolerates code fences and prose around it
    (models occasionally wrap the JSON despite instructions)."""
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", cleaned):
        try:
            data, _end = decoder.raw_decode(cleaned, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


_MENTOR_PERSONA = (
    "You are a friendly, encouraging coding mentor built into aGiTrack, a tool that records "
    "how people work with coding agents. You coach the developer using evidence from their "
    "actual agent sessions. Be concrete, warm, and brief; never condescending. Use simple, "
    "everyday words and short sentences; when a technical term is unavoidable, explain it in "
    "plain language the first time you use it. Always prefer a concrete example over an "
    "abstract explanation. Do not use em-dashes anywhere in your output."
)


def _run_agent(choice: LearningBackendChoice, system_prompt: str, user_prompt: str, timeout: int) -> str:
    backend = choice.build()
    result = backend.run(
        user_prompt,
        model=choice.model,
        session_id=None,
        bare=True,
        system_prompt=system_prompt,
        timeout_seconds=timeout,
    )
    text = (result.final_response or "").strip()
    if result.exit_code != 0:
        raise LearnAgentError(
            f"The {choice.backend_name} backend exited with code {result.exit_code}."
            + (f" Output: {text[:200]}" if text else "")
        )
    if not text:
        raise LearnAgentError(f"The {choice.backend_name} backend returned no output.")
    return text


def _run_agent_json(choice: LearningBackendChoice, system_prompt: str, user_prompt: str, timeout: int) -> dict:
    text = _run_agent(choice, system_prompt, user_prompt, timeout)
    data = _extract_json(text)
    if data is None:
        raise LearnAgentError(f"The {choice.backend_name} backend did not return valid JSON: {text[:200]}")
    return data


_SUGGEST_SYSTEM = (
    _MENTOR_PERSONA
    + " You must reply with ONE JSON object and nothing else: no prose before or after it, no code fences."
)


def _suggest_prompt(digest: str, minutes: int, mood: str, note: str, gid: str, source: str) -> str:
    if source:
        traces = f"the sessions of '{source}'"
        traces += (
            " (the learner themself)" if source == gid else f" (a teammate the learner '{gid}' wants to learn from)"
        )
    else:
        traces = f"the whole team's sessions (the learner is '{gid}')"
    mood_hint = {
        "fresh": "They feel fresh and focused: a hands-on lesson with a small exercise is welcome.",
        "okay": "They feel okay: keep lessons practical and medium-weight.",
        "tired": "They feel tired: suggest light, skimmable lessons with quick wins, nothing demanding.",
    }.get(mood, "Assume medium focus.")
    extra = f'\nThe learner added: "{note[:300]}". Weigh this heavily when picking suggestions.' if note else ""
    return f"""Below is a digest of real coding-agent interaction traces from {traces}.

Your tasks:
1. Assess the learner's current coding knowledge from how the traces drive the agent (prompt style, corrections, what gets delegated vs fixed by hand).
2. Identify knowledge gaps the traces show. Two kinds: "coding" (general skills that would make them work with coding agents more effectively, e.g. git, testing, debugging, prompt habits) and "codebase" (understanding of THIS repository so they can spot issues and fix simple things themselves).
3. Propose exactly 3 or 4 small lesson suggestions the learner can do RIGHT NOW in at most {minutes} minutes each. {mood_hint}{extra}
Each suggestion must be grounded in the traces: its "why" cites the concrete evidence (a prompt pattern, a rework hotspot, an insight). Never repeat or closely rephrase anything listed under ALREADY LEARNED or ALREADY IN PROGRESS in the digest: every suggestion must teach something meaningfully new, at most building on top of those.

Reply with ONE JSON object, exactly this shape:
{{"assessment": "2-3 warm sentences on their current level and strengths",
 "gaps": [{{"id": "kebab-case-id", "title": "short title", "detail": "1-2 sentences", "kind": "coding" or "codebase", "evidence": "what in the traces shows this"}}],
 "suggestions": [{{"id": "kebab-case-id", "title": "inviting lesson title", "minutes": <=int {minutes}, "kind": "coding" or "codebase", "gap_id": "id of the gap it addresses", "why": "evidence-based reason", "teaser": "one inviting sentence on what they will be able to do afterwards"}}]}}

TRACE DIGEST:
{digest}"""


def _lesson_prompt(suggestion: dict, digest: str, mood: str) -> str:
    minutes = int(suggestion.get("minutes") or 15)
    words = {5: 350, 15: 700, 30: 1200}.get(minutes, min(1200, max(300, minutes * 45)))
    return f"""Write the full lesson for this suggestion, for the learner described by the trace digest below.

SUGGESTION: {json.dumps(suggestion, ensure_ascii=False)}

The lesson is shown in a web page that walks the learner through it ONE STEP AT A TIME. The page is
the entire learning environment: everything must be readable and doable inside it. NEVER tell the
learner to run a command, open a terminal or editor, or leave the page. When code, a diff, a config,
or a command's output is worth studying, include it directly in a step as a fenced code block.

Requirements:
- "steps": 3 to 7 small steps, ONE idea each, 40-150 words per step (total sized for about {minutes} minutes, {words} words or fewer). Mood: {mood or "okay"}. The first step says why this matters to THIS learner; where relevant reference their ACTUAL files/areas named in the digest; the final step is a "Try this next time" takeaway they can apply in their very next agent session.
- "links": 1-3 external resources for going deeper. Only real, stable, well-known URLs (official documentation, canonical references). If unsure a URL is real, leave it out.
- "quiz": 1-3 multiple-choice questions checking the key points. 3-4 choices each, "answer" is the 0-based index of the correct choice, "explain" says why in one sentence.
- "exercise": ONE exercise completable ENTIRELY in the page: include all the material to work with inside "task" (a code snippet, diff, or scenario, in fenced blocks), and ask for a short typed answer: predict what happens, spot the problem, write the fix, or write the prompt they would give their agent. Never require executing anything. "hint" helps if they get stuck.
- Plain language above all: simple everyday words, short sentences, one concrete example beats a paragraph of theory. Define any technical term in plain words the first time it appears. Write like you are explaining to a smart colleague who is new to this exact topic, never like documentation.
- Friendly and effortless to read. No em-dashes.

Reply with ONE JSON object, exactly this shape (no prose around it, no code fences):
{{"title": "lesson title", "minutes": {minutes},
 "steps": [{{"title": "short step title", "content_md": "the step in Markdown"}}],
 "links": [{{"title": "…", "url": "https://…", "note": "why it is worth opening"}}],
 "quiz": [{{"question": "…", "choices": ["…", "…", "…"], "answer": 0, "explain": "…"}}],
 "exercise": {{"task": "…", "hint": "…"}}}}

TRACE DIGEST:
{digest}"""


def _chat_prompt(lesson: dict, history: list[dict], message: str) -> str:
    convo = "\n".join(
        f"{'Learner' if turn.get('role') == 'user' else 'Mentor'}: {str(turn.get('text', ''))[:600]}"
        for turn in history[-8:]
    )
    convo_block = f"\n\nCONVERSATION SO FAR:\n{convo}" if convo else ""
    content = str(lesson.get("content_md", ""))[:4000]
    return (
        f"The learner just read this lesson and has a follow-up question. Answer it directly, "
        f"concretely and briefly (a few short paragraphs at most), in plain Markdown. No JSON.\n\n"
        f"LESSON '{lesson.get('title', '')}':\n{content}{convo_block}\n\nLearner's question: {message}"
    )


def _exercise_prompt(lesson: dict, notes: str) -> str:
    exercise = lesson.get("exercise") or {}
    content = str(lesson.get("content_md", ""))[:2500]
    return f"""The learner attempted the hands-on exercise from the lesson '{lesson.get("title", "")}'.

EXERCISE: {exercise.get("task", "")}

LESSON (context):
{content}

WHAT THE LEARNER REPORTS DOING / THEIR RESULT:
{notes[:2000]}

Judge whether the typed answer shows they achieved the exercise's goal. Be generous with honest effort but do not pass an attempt that missed the point. Anything you suggest trying next must be doable right here in the page (rethinking, retyping an answer) or in their next agent session; never tell them to run commands or open a terminal. No em-dashes. Reply with ONE JSON object, nothing else:
{{"passed": true or false, "feedback": "2-4 encouraging, concrete sentences; if not passed, say exactly what to reconsider"}}"""


# ------------------------------------------------------------------- normalization


def _slug(value: object, fallback: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return text[:60] or fallback


_TITLE_STOPWORDS = {"the", "a", "an", "your", "with", "for", "to", "of", "and", "in", "on", "how", "what", "why"}


def _title_tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if token not in _TITLE_STOPWORDS}


def _is_near_duplicate(title: str, recent_titles: list[str]) -> bool:
    """Whether a suggested title is (nearly) the same topic as a recent lesson: high word
    overlap after dropping stopwords. The last line of defense against the model
    re-suggesting something already learned despite the prompt's instruction."""
    tokens = _title_tokens(title)
    if not tokens:
        return False
    for other in recent_titles:
        other_tokens = _title_tokens(other)
        if not other_tokens:
            continue
        if len(tokens & other_tokens) / min(len(tokens), len(other_tokens)) >= 0.7:
            return True
    return False


def _norm_gaps(raw: object) -> list[dict]:
    gaps = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        gaps.append(
            {
                "id": _slug(item.get("id") or item.get("title"), f"gap-{index}"),
                "title": str(item.get("title") or "")[:120],
                "detail": str(item.get("detail") or "")[:400],
                "kind": "codebase" if item.get("kind") == "codebase" else "coding",
                "evidence": str(item.get("evidence") or "")[:400],
                "status": "open",
            }
        )
    return gaps[:6]


def _norm_suggestions(raw: object, minutes: int) -> list[dict]:
    suggestions = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        want = item.get("minutes")
        estimate = want if isinstance(want, int) and 0 < want <= 240 else minutes
        suggestions.append(
            {
                "id": _slug(item.get("id") or item.get("title"), f"idea-{index}"),
                "title": str(item.get("title") or "")[:140],
                "minutes": min(estimate, minutes) if minutes else estimate,
                "kind": "codebase" if item.get("kind") == "codebase" else "coding",
                "gap_id": _slug(item.get("gap_id"), "") if item.get("gap_id") else "",
                "why": str(item.get("why") or "")[:400],
                "teaser": str(item.get("teaser") or "")[:300],
            }
        )
    return suggestions[:4]


def _norm_lesson(raw: dict, suggestion: dict) -> dict:
    links = []
    raw_links = raw.get("links")
    for item in raw_links if isinstance(raw_links, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if not url.startswith(("http://", "https://")):
            continue
        links.append(
            {"title": str(item.get("title") or url)[:140], "url": url[:500], "note": str(item.get("note") or "")[:200]}
        )
    quiz = []
    raw_quiz = raw.get("quiz")
    for item in raw_quiz if isinstance(raw_quiz, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("choices"), list):
            continue
        choices = [str(choice)[:200] for choice in item["choices"][:4] if str(choice).strip()]
        answer = item.get("answer")
        if len(choices) < 2 or not isinstance(answer, int) or not 0 <= answer < len(choices):
            continue
        quiz.append(
            {
                "question": str(item.get("question") or "")[:300],
                "choices": choices,
                "answer": answer,
                "explain": str(item.get("explain") or "")[:300],
            }
        )
    raw_exercise = raw.get("exercise")
    if not isinstance(raw_exercise, dict):
        raw_exercise = {}
    task = str(raw_exercise.get("task") or "").strip()
    exercise = (
        {"task": task[:4000], "hint": str(raw_exercise.get("hint") or "")[:1000], "status": "open", "attempts": []}
        if task
        else None
    )
    # The lesson is a sequence of small steps the page walks through one at a time. A model
    # that returned a single content_md blob instead (or an older stored lesson) still works:
    # it becomes one step here, and the page splits legacy blobs on ### headings client-side.
    steps = []
    raw_steps = raw.get("steps")
    for item in raw_steps if isinstance(raw_steps, list) else []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content_md") or "").strip()
        if not content:
            continue
        steps.append({"title": str(item.get("title") or "")[:120], "content_md": content[:6000]})
    steps = steps[:8]
    content_md = str(raw.get("content_md") or "")
    if not steps and content_md.strip():
        steps = [{"title": "", "content_md": content_md}]
    if not content_md.strip():
        # Joined view of the steps: the chat/exercise prompts feed this to the mentor as context.
        content_md = "\n\n".join(
            (f"### {step['title']}\n{step['content_md']}" if step["title"] else step["content_md"]) for step in steps
        )
    minutes = raw.get("minutes")
    return {
        "title": str(raw.get("title") or suggestion.get("title") or "Lesson")[:140],
        "minutes": minutes if isinstance(minutes, int) and 0 < minutes <= 240 else suggestion.get("minutes", 15),
        "kind": suggestion.get("kind", "coding"),
        "gap_id": suggestion.get("gap_id", ""),
        "steps": steps,
        "content_md": content_md,
        "links": links[:3],
        "quiz": quiz[:3],
        "exercise": exercise,
    }


# ----------------------------------------------------------------- progress sync


def sync_enabled(repo_root: Path) -> bool:
    with _STORE_LOCK:
        return bool(LearnStore(repo_root).load().get("sync_enabled"))


def _record_sync_result(repo_root: Path, ok: bool, error: str) -> None:
    store = LearnStore(repo_root)
    with _STORE_LOCK:
        data = store.load()
        data["last_sync"] = {"ok": ok, "error": error[:300], "at": int(time.time())}
        store.save(data)


def sync_progress_now(repo: GitRepo, gid: str) -> tuple[bool, str]:
    """Write the user's progress profile to the sync ref and push it (best-effort).

    Mirrors the shared-session store's shape: the ref holds one parent-less snapshot
    commit (no history to grow), each user owns ``<fingerprint>/<gid>/progress.json``,
    and the push is guarded with a lease, retried once after a re-fetch on a lost race.
    The local learning.json is always the source of truth for OUR entry, so force-syncing
    the local ref from the remote can never lose our progress: the entry is rebuilt from
    the store on every sync."""
    store = LearnStore(repo.repo)
    with _STORE_LOCK:
        data = store.load()
        profile = LearnStore.profile(data, gid)
        payload = json.dumps({"gid": gid, "updated": int(time.time()), "profile": profile}, indent=2, sort_keys=True)
    fingerprint = repo.root_commit() or "no-root"
    path = f"{fingerprint}/{gid}/progress.json"
    error = ""
    if repo.remote_exists():
        repo.fetch_ref(f"+{PROGRESS_REF}:{PROGRESS_REF}", timeout=20)
    for _attempt in range(2):
        old = repo.ref_sha(PROGRESS_REF)
        entries = dict(repo.read_tree_paths(PROGRESS_REF))
        entries[path] = repo.write_blob(payload)
        tree = repo.write_tree_from(entries)
        sha = repo.commit_tree_orphan(tree, f"agitrack: learning progress {gid}")
        repo.update_ref(PROGRESS_REF, sha)
        if not repo.remote_exists():
            return True, ""
        lease = f"{PROGRESS_REF}:{old}" if old else None
        ok, error = repo.push_ref(f"{PROGRESS_REF}:{PROGRESS_REF}", force_with_lease=lease, timeout=30)
        if ok:
            return True, ""
        from agitrack.sessions.store import _is_stale_lease

        if not _is_stale_lease(error):
            break
        repo.fetch_ref(f"+{PROGRESS_REF}:{PROGRESS_REF}", timeout=20)
    return False, error.strip()


def maybe_sync(root: Path, repo: GitRepo | None) -> None:
    """Kick a background progress sync after a milestone (suggestions, a new lesson,
    completion, an exercise attempt) when the user has sync enabled. Throttled so a
    burst of progress flushes becomes one push; never blocks the request. A no-op
    without a git repo (backtrace over a plain directory)."""
    if repo is None or not sync_enabled(root):
        return
    key = str(root)
    now = time.monotonic()
    if now - _sync_at.get(key, 0.0) < _SYNC_THROTTLE_SECONDS:
        return
    _sync_at[key] = now
    gid = learner_id(root, repo)

    def worker() -> None:
        try:
            ok, error = sync_progress_now(repo, gid)
            _record_sync_result(root, ok, error)
        except Exception as exc:
            _record_sync_result(root, False, str(exc))

    threading.Thread(target=worker, daemon=True, name="agit-learn-sync").start()


def _fetch_progress_throttled(repo: GitRepo) -> None:
    """Best-effort background fetch of teammates' synced progress, at most once per TTL.
    Safe to force-update the local ref: our own entry is rebuilt from learning.json on
    every sync, so a remote overwrite can't lose local progress."""
    if not repo.remote_exists():
        return
    key = str(repo.repo)
    now = time.monotonic()
    if now - _progress_fetch_at.get(key, 0.0) < _PROGRESS_FETCH_TTL:
        return
    _progress_fetch_at[key] = now

    def worker() -> None:
        try:
            repo.fetch_ref(f"+{PROGRESS_REF}:{PROGRESS_REF}", timeout=20)
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True, name="agit-learn-fetch").start()


# Roots where a restore has already been attempted this process: a genuinely-new user
# with nothing on the ref shouldn't pay a network fetch on every page load.
_restore_checked: set[str] = set()


def _profile_is_empty(profile: dict[str, Any]) -> bool:
    return not (
        profile.get("assessment") or profile.get("gaps") or profile.get("suggestions") or profile.get("lessons")
    )


def restore_progress_from_ref(root: Path, repo: GitRepo, gid: str) -> bool:
    """Pull the user's synced progress back onto THIS machine.

    The sync ref is how progress travels: on a machine (or fresh clone) where the local
    ``learning.json`` has nothing for this user, fetch ``refs/agitrack/learning-progress``
    from origin and import the user's own entry. Called by :func:`learn_state` when the
    local profile is empty, so progress simply follows the user with no import step; a
    successful restore also re-enables sync (they had it on wherever the entry came from).
    Never overwrites a non-empty local profile: local, newer-by-construction state wins.
    """
    if repo.remote_exists():
        repo.fetch_ref(f"+{PROGRESS_REF}:{PROGRESS_REF}", timeout=15)
    fingerprint = repo.root_commit() or "no-root"
    raw = repo.read_ref_blob(PROGRESS_REF, f"{fingerprint}/{gid}/progress.json")
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return False
    profile = parsed.get("profile") if isinstance(parsed, dict) else None
    if not isinstance(profile, dict) or _profile_is_empty(profile):
        return False
    store = LearnStore(root)
    with _STORE_LOCK:
        data = store.load()
        current = LearnStore.profile(data, gid)
        if not _profile_is_empty(current):
            return False  # something landed locally in the meantime; keep it
        data["profiles"][gid] = profile
        data["sync_enabled"] = True
        store.save(data)
    return True


def synced_users(repo: GitRepo) -> list[dict]:
    """Who has synced learning progress for THIS repo (from the local ref), newest first."""
    fingerprint = repo.root_commit() or "no-root"
    prefix = f"{fingerprint}/"
    users = []
    for path in repo.read_tree_paths(PROGRESS_REF):
        if not path.startswith(prefix) or not path.endswith("/progress.json"):
            continue
        gid = path[len(prefix) :].split("/", 1)[0]
        raw = repo.read_ref_blob(PROGRESS_REF, path)
        updated = 0
        try:
            parsed = json.loads(raw) if raw else {}
            updated = int(parsed.get("updated") or 0)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        users.append({"gid": gid, "updated": updated})
    return sorted(users, key=lambda user: -user["updated"])


def set_sync(root: Path, repo: GitRepo | None, enabled: bool) -> dict[str, Any]:
    """Flip the progress-sync toggle. Enabling syncs immediately (synchronously, so the
    page can report the outcome); disabling just stops future pushes. Refused without a
    git repo: the sync ref has nowhere to live."""
    if repo is None:
        return {"error": "Progress sync needs a git repository; this directory isn't one."}
    store = LearnStore(root)
    with _STORE_LOCK:
        data = store.load()
        data["sync_enabled"] = bool(enabled)
        store.save(data)
    if enabled:
        try:
            ok, error = sync_progress_now(repo, learner_id(root, repo))
        except Exception as exc:
            ok, error = False, str(exc)
        _record_sync_result(root, ok, error)
    return {"sync": sync_info(root, repo)}


def sync_info(root: Path, repo: GitRepo | None) -> dict[str, Any]:
    if repo is None:
        return {"available": False, "enabled": False, "last": None, "users": []}
    store = LearnStore(root)
    with _STORE_LOCK:
        data = store.load()
    _fetch_progress_throttled(repo)
    return {
        "available": True,
        "enabled": bool(data.get("sync_enabled")),
        "last": data.get("last_sync"),
        "users": synced_users(repo),
    }


# ------------------------------------------------------------------------ public API


def model_options(backend_name: str) -> dict[str, Any]:
    """Models the learn page's engine picker can offer for ``backend_name`` (queried from
    the backend CLI, same as the summarizer's model menu). Empty when unqueryable; the
    picker then falls back to "auto"."""
    if backend_name not in {"claude", "opencode"}:
        return {"backend": backend_name, "models": []}
    from agitrack.summaries.model_select import list_available_models

    return {"backend": backend_name, "models": list_available_models(backend_name)}


def set_learning_config(repo_root: Path, *, backend: str, model: str) -> dict[str, Any]:
    """Persist the learn page's engine choice into the REPO config overlay
    (``.agitrack/config.json``): the same ``learning_backend`` / ``learning_model`` keys a
    user can set by hand, so the page and the config file are two views of one setting.
    Empty values unset the key (back to "auto": the latest session's backend/model)."""
    backend, model = backend.strip(), model.strip()
    if backend not in {"", "claude", "opencode"}:
        return {"error": f"Unknown backend '{backend}'."}
    from agitrack.config.settings import GlobalConfig

    config = GlobalConfig()
    config.load_repo_overlay(Path(repo_root))
    if backend:
        config.repo_data["learning_backend"] = backend
    else:
        config.repo_data.pop("learning_backend", None)
    if model:
        config.repo_data["learning_model"] = model
    else:
        config.repo_data.pop("learning_model", None)
    config.save_repo()
    return {"backend_info": describe_learning_backend(Path(repo_root))}


def describe_learning_backend(repo_root: Path) -> dict[str, Any]:
    """Backend/model info for the page footer: which backend generates content, which
    model, and where each choice came from. An error string when unresolvable."""
    try:
        choice = resolve_learning_backend(Path(repo_root))
    except LearnAgentError as exc:
        return {"error": str(exc)}
    return {
        "backend": choice.backend_name,
        "model": choice.model,
        "backend_source": choice.backend_source,
        "model_source": choice.model_source,
    }


def learn_state(root: Path, repo: GitRepo | None) -> dict[str, Any]:
    """Everything the page needs to paint instantly, with no agent call: who the learner
    is (GitHub id), their persisted profile, the engine info, and the sync status. On a
    machine where this user has no local progress yet, their synced progress (if any) is
    restored from the git ref first, so progress follows them across machines."""
    gid = learner_id(root, repo)
    store = LearnStore(root)
    with _STORE_LOCK:
        data = store.load()
        profile = LearnStore.profile(data, gid)
    restored = False
    if repo is not None and _profile_is_empty(profile) and str(root) not in _restore_checked:
        _restore_checked.add(str(root))
        try:
            restored = restore_progress_from_ref(root, repo, gid)
        except Exception:
            restored = False
        if restored:
            with _STORE_LOCK:
                profile = LearnStore.profile(store.load(), gid)
    return {
        "me": gid,
        "profile": profile,
        "restored": restored,
        "backend_info": describe_learning_backend(root),
        "sync": sync_info(root, repo),
    }


def suggest(
    root: Path,
    repo: GitRepo | None,
    stats: list[CommitStat],
    insights: list[dict],
    file_rows: list[dict],
    *,
    source: str,
    minutes: int,
    mood: str,
    note: str = "",
    period_days: int = 0,
    branch: str = "",
) -> dict[str, Any]:
    """One backend call: assess the learner, identify gaps, and propose lessons sized to
    ``minutes``/``mood``. ``source`` selects WHOSE traces feed the analysis (a committer
    label, or '' for the whole team); the progress profile is always the current user's.
    Persists the result and returns the refreshed profile.

    With (almost) no tracked turns in the slice there is nothing to personalize from, so
    instead of calling the agent this stores STARTER topics plus a notice explaining how
    to get a trace (--backtrace for history written outside aGiTrack, or simply running
    sessions through aGiTrack). The starter topics use the normal lesson pipeline."""
    if not _AGENT_LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        gid = learner_id(root, repo)
        ai_turns = sum(1 for stat in stats if stat.kind in _AI_KINDS)
        if ai_turns < _MIN_TRACE_TURNS:
            notice = _no_trace_message(ai_turns)

            def apply_starters(profile: dict[str, Any]) -> None:
                profile["suggestions"] = [dict(item) for item in _STARTER_SUGGESTIONS]
                profile["trace_notice"] = notice
                profile["suggested_at"] = int(time.time())
                profile["suggest_context"] = {
                    "minutes": minutes,
                    "mood": mood,
                    "note": note[:300],
                    "source": source,
                    "days": period_days,
                    "branch": branch,
                }

            profile = LearnStore(root).update(gid, apply_starters)
            return {"profile": profile, "no_trace": True}
        store = LearnStore(root)
        with _STORE_LOCK:
            profile = LearnStore.profile(store.load(), gid)
        digest = build_trace_digest(stats, insights, file_rows, root, profile)
        choice = resolve_learning_backend(root)
        minutes = minutes if minutes in (5, 15, 30) else 15
        raw = _run_agent_json(
            choice,
            _SUGGEST_SYSTEM,
            _suggest_prompt(digest, minutes, mood, note, gid, source),
            _SUGGEST_TIMEOUT_SECONDS,
        )
        assessment = str(raw.get("assessment") or "")[:800]
        gaps = _norm_gaps(raw.get("gaps"))
        suggestions = _norm_suggestions(raw.get("suggestions"), minutes)
        if not suggestions:
            raise LearnAgentError("The backend returned no usable suggestions; try again.")
        # Belt and braces on top of the prompt's no-repeat instruction: drop any pick that
        # is (nearly) the same topic as a recent lesson. If the model somehow duplicated
        # EVERYTHING, keep its output rather than showing nothing.
        recent_titles = [str(lesson.get("title") or "") for lesson in profile.get("lessons", [])][-15:]
        fresh = [item for item in suggestions if not _is_near_duplicate(item["title"], recent_titles)]
        if fresh:
            suggestions = fresh

        def apply(profile: dict[str, Any]) -> None:
            if assessment:
                profile["assessment"] = assessment
            # Merge gaps by id: a re-identified gap keeps its "addressed" status only if
            # the agent no longer flags it; when it still shows in the evidence it reopens.
            known = {str(gap.get("id") or ""): gap for gap in profile["gaps"] if isinstance(gap, dict)}
            profile["gaps"] = [*gaps, *[gap for gap_id, gap in known.items() if _find_by_id(gaps, gap_id) is None]][:12]
            profile["suggestions"] = suggestions
            profile.pop("trace_notice", None)  # a real trace personalized these picks
            profile["suggested_at"] = int(time.time())
            # The check-in that produced these picks; the page restores it into the
            # controls so the selections shown always match the content shown.
            profile["suggest_context"] = {
                "minutes": minutes,
                "mood": mood,
                "note": note[:300],
                "source": source,
                "days": period_days,
                "branch": branch,
            }

        profile = store.update(gid, apply)
        maybe_sync(root, repo)
        return {"profile": profile}
    except LearnAgentError as exc:
        return {"error": str(exc)}
    finally:
        _AGENT_LOCK.release()


def make_lesson(
    root: Path,
    repo: GitRepo | None,
    stats: list[CommitStat],
    insights: list[dict],
    file_rows: list[dict],
    *,
    suggestion_id: str,
) -> dict[str, Any]:
    """Generate the full lesson for one stored suggestion; persist and return it."""
    if not _AGENT_LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        gid = learner_id(root, repo)
        store = LearnStore(root)
        with _STORE_LOCK:
            profile = LearnStore.profile(store.load(), gid)
        suggestion = _find_by_id(profile["suggestions"], suggestion_id)
        if suggestion is None:
            return {"error": "That suggestion is no longer stored; ask for fresh suggestions."}
        digest = build_trace_digest(stats, insights, file_rows, root, profile)
        choice = resolve_learning_backend(root)
        mood = str((profile.get("suggest_context") or {}).get("mood") or "")
        raw = _run_agent_json(
            choice, _SUGGEST_SYSTEM, _lesson_prompt(suggestion, digest, mood), _LESSON_TIMEOUT_SECONDS
        )
        lesson = _norm_lesson(raw, suggestion)
        if not lesson["content_md"].strip():
            raise LearnAgentError("The backend returned an empty lesson; try again.")
        now = int(time.time())
        lesson.update(
            {
                "id": f"{suggestion_id}-{now}",
                "suggestion_id": suggestion_id,  # lets the page pair cards with their lessons
                "status": "started",
                "created_at": now,
                "seconds_spent": 0,
                "quiz_correct": None,
                "quiz_total": None,
                "chat": [],
            }
        )

        def apply(profile: dict[str, Any]) -> None:
            profile["lessons"].append(lesson)

        store.update(gid, apply)
        maybe_sync(root, repo)
        return {"lesson": lesson}
    except LearnAgentError as exc:
        return {"error": str(exc)}
    finally:
        _AGENT_LOCK.release()


def delete_lesson(root: Path, repo: GitRepo | None, *, lesson_id: str) -> dict[str, Any]:
    """Remove one lesson (and its chat/exercise/quiz record) from the progress history.
    Gaps are left as they are: a gap the lesson closed stays closed, since the learning
    happened even if the record is no longer wanted. Synced like any other milestone."""
    store = LearnStore(root)
    gid = learner_id(root, repo)
    found = {"ok": False}

    def apply(profile: dict[str, Any]) -> None:
        lessons = profile.get("lessons", [])
        kept = [lesson for lesson in lessons if lesson.get("id") != lesson_id]
        found["ok"] = len(kept) != len(lessons)
        profile["lessons"] = kept

    profile = store.update(gid, apply)
    if not found["ok"]:
        return {"error": "Unknown lesson."}
    maybe_sync(root, repo)
    return {"profile": profile}


def reset_suggestions(root: Path, repo: GitRepo | None) -> dict[str, Any]:
    """Clear the stored suggestions (and the check-in context that produced them), so the
    next check-in starts clean: for when the repo has moved on or the filters changed and
    the stored picks feel stale. Progress (lessons, gaps, assessment) is kept; nothing an
    agent produced for COMPLETED work is lost."""
    gid = learner_id(root, repo)

    def apply(profile: dict[str, Any]) -> None:
        profile["suggestions"] = []
        profile.pop("suggested_at", None)
        profile.pop("suggest_context", None)

    profile = LearnStore(root).update(gid, apply)
    return {"profile": profile}


def lesson_chat(root: Path, repo: GitRepo | None, *, lesson_id: str, message: str) -> dict[str, Any]:
    """A follow-up question about a lesson. Stateless on the backend side: the lesson and
    recent conversation ride in the prompt, so it never depends on scratch-session state."""
    message = message.strip()
    if not message:
        return {"error": "Empty question."}
    if not _AGENT_LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        gid = learner_id(root, repo)
        store = LearnStore(root)
        with _STORE_LOCK:
            profile = LearnStore.profile(store.load(), gid)
        lesson = _find_by_id(profile["lessons"], lesson_id)
        if lesson is None:
            return {"error": "Unknown lesson."}
        choice = resolve_learning_backend(root)
        reply = _run_agent(
            choice, _MENTOR_PERSONA, _chat_prompt(lesson, lesson.get("chat") or [], message), _CHAT_TIMEOUT_SECONDS
        )

        def apply(profile: dict[str, Any]) -> None:
            stored = _find_by_id(profile["lessons"], lesson_id)
            if stored is not None:
                chat = stored.setdefault("chat", [])
                chat.extend([{"role": "user", "text": message[:2000]}, {"role": "mentor", "text": reply[:8000]}])
                del chat[:-20]  # keep the tail; old exchanges age out

        store.update(gid, apply)
        return {"reply": reply}
    except LearnAgentError as exc:
        return {"error": str(exc)}
    finally:
        _AGENT_LOCK.release()


def exercise_check(root: Path, repo: GitRepo | None, *, lesson_id: str, notes: str) -> dict[str, Any]:
    """The learner tried the lesson's hands-on exercise and reports what happened; the
    mentor reviews it. The attempt (notes, feedback, pass/fail) is logged in the
    progress record, and a pass marks the exercise done."""
    notes = notes.strip()
    if not notes:
        return {"error": "Type your answer first."}
    if not _AGENT_LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        gid = learner_id(root, repo)
        store = LearnStore(root)
        with _STORE_LOCK:
            profile = LearnStore.profile(store.load(), gid)
        lesson = _find_by_id(profile["lessons"], lesson_id)
        if lesson is None:
            return {"error": "Unknown lesson."}
        if not lesson.get("exercise"):
            return {"error": "This lesson has no exercise."}
        choice = resolve_learning_backend(root)
        raw = _run_agent_json(choice, _SUGGEST_SYSTEM, _exercise_prompt(lesson, notes), _CHAT_TIMEOUT_SECONDS)
        passed = bool(raw.get("passed"))
        feedback = str(raw.get("feedback") or "").strip()[:1500]
        if not feedback:
            raise LearnAgentError("The backend returned no feedback; try again.")

        def apply(profile: dict[str, Any]) -> None:
            stored = _find_by_id(profile["lessons"], lesson_id)
            exercise = stored.get("exercise") if stored else None
            if isinstance(exercise, dict):
                attempts = exercise.setdefault("attempts", [])
                attempts.append({"notes": notes[:2000], "feedback": feedback, "passed": passed, "at": int(time.time())})
                del attempts[:-10]
                if passed:
                    exercise["status"] = "done"

        store.update(gid, apply)
        maybe_sync(root, repo)
        return {"passed": passed, "feedback": feedback}
    except LearnAgentError as exc:
        return {"error": str(exc)}
    finally:
        _AGENT_LOCK.release()


def record_progress(
    root: Path,
    repo: GitRepo | None,
    *,
    lesson_id: str,
    status: str = "",
    seconds: int = 0,
    quiz_correct: int | None = None,
    quiz_total: int | None = None,
    exercise_status: str = "",
) -> dict[str, Any]:
    """Automatic progress tracking: time spent accumulates, completion closes the linked
    gap, quiz results and exercise outcomes are stored. Cheap and agent-free; the page
    calls it in the background (periodic flushes, quiz checks, the Done button)."""
    gid = learner_id(root, repo)
    store = LearnStore(root)
    found = {"ok": False}

    def apply(profile: dict[str, Any]) -> None:
        lesson = _find_by_id(profile["lessons"], lesson_id)
        if lesson is None:
            return
        found["ok"] = True
        if seconds > 0:
            lesson["seconds_spent"] = int(lesson.get("seconds_spent") or 0) + min(int(seconds), 7200)
        if isinstance(quiz_correct, int) and isinstance(quiz_total, int) and quiz_total > 0:
            lesson["quiz_correct"], lesson["quiz_total"] = quiz_correct, quiz_total
        # "open" reopens a previously skipped exercise (the page's "give it a try after all").
        if exercise_status in ("done", "skipped", "open") and isinstance(lesson.get("exercise"), dict):
            lesson["exercise"]["status"] = exercise_status
        if status in ("started", "completed", "dismissed"):
            lesson["status"] = status
            if status == "completed":
                lesson["completed_at"] = int(time.time())
                gap = _find_by_id(profile["gaps"], str(lesson.get("gap_id") or ""))
                if gap is not None:
                    gap["status"] = "addressed"

    profile = store.update(gid, apply)
    if not found["ok"]:
        return {"error": "Unknown lesson."}
    if status == "completed" or exercise_status:
        maybe_sync(root, repo)
    return {"profile": profile}


# ---------------------------------------------------------------- shared dispatch


def _body_int(body: dict, key: str) -> int:
    value = body.get(key)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def handle_learn_post(
    path: str,
    body: dict,
    *,
    root: Path,
    repo: GitRepo | None,
    view: Callable[[str, int, int, str], tuple[list[CommitStat], list[dict], list[dict]]],
) -> dict | None:
    """The POST dispatcher both dashboard servers (live and backtrace) share.

    ``view(source, frm, to, branch)`` returns the filter-scoped ``(stats, insights,
    file_rows)`` the learning agent's digest is built from — exactly the slice that
    server's dashboard would show. ``source`` selects WHOSE traces feed the analysis, and
    ``branch`` which git ref they are read from (the trace lives in commits, so it is
    branch-dependent; the live server validates it, backtrace ignores it). The identity
    progress is logged under is resolved server-side (GitHub id), never trusted from the
    client. Returns None for an unknown path (the caller 404s); agent failures come back
    as ``{"error": …}`` so the page shows them in place rather than a blank 500."""
    source = str(body.get("source") or "")
    frm, to = _body_int(body, "from"), _body_int(body, "to")
    branch = str(body.get("branch") or "")
    try:
        if path == "/learn/suggest":
            stats, insights, file_rows = view(source, frm, to, branch)
            return suggest(
                root,
                repo,
                stats,
                insights,
                file_rows,
                source=source,
                minutes=_body_int(body, "minutes"),
                mood=str(body.get("mood") or ""),
                note=str(body.get("note") or ""),
                period_days=_body_int(body, "days"),
                branch=branch,
            )
        if path == "/learn/lesson":
            stats, insights, file_rows = view(source, frm, to, branch)
            return make_lesson(
                root, repo, stats, insights, file_rows, suggestion_id=str(body.get("suggestion_id") or "")
            )
        if path == "/learn/chat":
            return lesson_chat(
                root, repo, lesson_id=str(body.get("lesson_id") or ""), message=str(body.get("message") or "")
            )
        if path == "/learn/exercise":
            return exercise_check(
                root, repo, lesson_id=str(body.get("lesson_id") or ""), notes=str(body.get("notes") or "")
            )
        if path == "/learn/progress":
            quiz_correct, quiz_total = body.get("quiz_correct"), body.get("quiz_total")
            return record_progress(
                root,
                repo,
                lesson_id=str(body.get("lesson_id") or ""),
                status=str(body.get("status") or ""),
                seconds=_body_int(body, "seconds"),
                quiz_correct=quiz_correct if isinstance(quiz_correct, int) else None,
                quiz_total=quiz_total if isinstance(quiz_total, int) else None,
                exercise_status=str(body.get("exercise_status") or ""),
            )
        if path == "/learn/reset":
            return reset_suggestions(root, repo)
        if path == "/learn/delete":
            return delete_lesson(root, repo, lesson_id=str(body.get("lesson_id") or ""))
        if path == "/learn/sync":
            return set_sync(root, repo, bool(body.get("enabled")))
        if path == "/learn/config":
            return set_learning_config(root, backend=str(body.get("backend") or ""), model=str(body.get("model") or ""))
    except Exception as exc:  # surface as an in-page error, never a blank 500
        return {"error": f"{type(exc).__name__}: {exc}"}
    return None


# ------------------------------------------------------------------------- the page


def learn_html(root: Path, *, banner_html: str = "") -> str:
    """The /learn page chrome. Data (profile, committers, backend info) is fetched from
    ``/learn/state`` after paint, so this stays instant like the dashboard shell.

    ``banner_html`` fills the frozen top-strip slot; the backtrace server passes its
    "contents are based on a reconstruction" notice here (same pattern as the dashboard's
    backtrace banner). Empty on the live server. Substituted FIRST so page content could
    never smuggle the placeholder in."""
    from agitrack.metrics.collect import _abbreviate_home
    from agitrack.metrics.web import _escape

    repo_path = _abbreviate_home(str(root))
    repo_name = repo_path.rstrip("/").rsplit("/", 1)[-1] or repo_path
    return (
        _LEARN_TEMPLATE.replace("__BACKTRACE_BANNER__", banner_html)
        .replace("__REPO_NAME__", _escape(repo_name))
        .replace("__REPO__", _escape(repo_path))
    )


def learn_backtrace_banner(directory: str) -> str:
    """The learn page's frozen backtrace notice: everything below is coached from a
    RECONSTRUCTION of past local sessions, not aGiTrack's live tracking."""
    from agitrack.metrics.web import _escape

    return (
        '<div class="btbanner">&#9194; BACKTRACE. This learning view is built from a reconstruction of past '
        f"coding-agent sessions in {_escape(directory)}: the coach's suggestions and lessons below are based on "
        "that backtraced history, not aGiTrack's live repo tracking.</div>"
    )


_LEARN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>learn · __REPO_NAME__ · aGiTrack</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🎓</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=VT323&family=IBM+Plex+Mono:ital,wght@0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">
<style>
:root{--ink:#0b0f0e;--panel:#111716;--panel2:#0e1413;--line:#233029;--fg:#cfe3d8;--fg-dim:#7d947f;
  --phosphor:#7fd77f;--phosphor-dim:#3f6b45;--accent:#6bbcee;--warn:#e0b653;--bad:#e07a6a;
  --chipbg:#16201d;--amber:#ffb454;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;--display:"VT323",var(--mono)}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--ink);color:var(--fg);
  font:14px/1.55 var(--mono)}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}

/* A slow ambient glow drifting behind everything: calm, not busy. */
.ambient{position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.5;
  background:
    radial-gradient(600px 420px at 18% 8%, rgba(127,215,127,.09), transparent 60%),
    radial-gradient(700px 480px at 85% 30%, rgba(107,188,238,.07), transparent 60%),
    radial-gradient(520px 420px at 45% 95%, rgba(224,182,83,.05), transparent 60%);
  animation:drift 36s ease-in-out infinite alternate}
@keyframes drift{from{transform:translate3d(0,0,0) scale(1)}to{transform:translate3d(-30px,20px,0) scale(1.06)}}

.wrap{max-width:1080px;margin:0 auto;padding:22px 20px 60px}
header{display:flex;align-items:baseline;justify-content:space-between;gap:14px;flex-wrap:wrap;
  border-bottom:1px dashed var(--line);padding-bottom:14px;margin-bottom:18px}
.brand{font-family:var(--display);font-weight:400;font-size:38px;line-height:.9;color:var(--phosphor);
  letter-spacing:1.5px;text-shadow:0 0 12px rgba(127,215,127,.5),0 0 44px rgba(127,215,127,.2)}
.brand .a{color:var(--amber);text-shadow:0 0 12px rgba(255,180,84,.5),0 0 44px rgba(255,180,84,.2)}
.brand .sub{font-family:var(--display);font-size:.5em;color:var(--fg-dim);letter-spacing:3px;text-shadow:none}
.meta{color:var(--fg-dim);font-size:12.5px} .meta b{color:var(--fg)}
.backlink{font-size:12.5px}

/* Sections and cards float in softly. */
.rise{animation:rise .5s ease both}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion: reduce){
  .rise,.card,.card.busy,.mascot,.ambient,.spin,.confetti span{animation:none !important}
  .bubble.typing .tdot{animation:none !important;opacity:.6}
}

h2.section{font-size:13px;letter-spacing:1.5px;text-transform:uppercase;color:var(--phosphor);
  margin:36px 0 12px;font-weight:600}
.sechead{display:flex;align-items:baseline;justify-content:space-between;gap:12px}
.sechead h2.section{margin-bottom:10px}
.btn.small{padding:5px 12px;font-size:12px}
.panel{background:var(--panel);border:1px solid var(--line);padding:16px 18px;border-radius:8px}
.checkin .lead{margin:0 0 12px;color:var(--fg);font-size:15px;display:flex;align-items:center;gap:10px}
.checkin .lead .hi{color:var(--phosphor)}
.mascot{display:inline-block;font-size:22px;animation:bob 3.2s ease-in-out infinite;transform-origin:50% 90%}
@keyframes bob{0%,100%{transform:translateY(0) rotate(-3deg)}50%{transform:translateY(-4px) rotate(3deg)}}
.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:10px 0}
.row label{color:var(--fg-dim);font-size:12px;min-width:86px}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{background:var(--chipbg);border:1px solid var(--line);color:var(--fg);padding:7px 14px;
  cursor:pointer;font:inherit;font-size:13px;border-radius:999px;transition:transform .12s,border-color .12s}
.chip:hover{border-color:var(--phosphor-dim);transform:translateY(-1px)}
.chip.sel{border-color:var(--phosphor);color:var(--phosphor);background:#14241a}
select,input[type=text],textarea{background:var(--panel2);border:1px solid var(--line);color:var(--fg);
  font:inherit;font-size:13px;padding:6px 8px;border-radius:4px}
input[type=text]{flex:1;min-width:200px}
input[type=text]::placeholder,textarea::placeholder{color:var(--fg-dim)}
textarea{width:100%;min-height:74px;resize:vertical}
.gobtn{display:block;width:100%;margin-top:14px;padding:13px;font:inherit;font-size:15px;cursor:pointer;
  background:linear-gradient(180deg,#173123,#122619);border:1px solid var(--phosphor-dim);color:var(--phosphor);
  border-radius:8px;letter-spacing:.3px;transition:transform .12s,border-color .12s,box-shadow .12s}
.gobtn:hover{border-color:var(--phosphor);transform:translateY(-1px);box-shadow:0 4px 18px rgba(127,215,127,.12)}
.gobtn:disabled{opacity:.55;cursor:default;transform:none;box-shadow:none}
.hint{color:var(--fg-dim);font-size:12px;margin-top:8px}
.thinking{display:flex;align-items:center;gap:10px;color:var(--fg-dim);font-size:13px;padding:14px 4px}
.thinking .grow{font-size:18px;animation:bob 2.4s ease-in-out infinite}
/* The full-screen processing overlay: while the agent writes suggestions or a lesson,
   the whole page dims and this card is unmissable, whatever was scrolled into view. */
.overlay{position:fixed;inset:0;z-index:60;display:flex;align-items:center;justify-content:center;
  background:rgba(4,7,5,.74);backdrop-filter:blur(3px);animation:fadein .25s ease both}
@keyframes fadein{from{opacity:0}to{opacity:1}}
.ov-card{background:var(--panel);border:1px solid var(--phosphor-dim);border-radius:12px;
  padding:32px 40px;max-width:460px;margin:0 20px;text-align:center;
  box-shadow:0 18px 60px rgba(0,0,0,.55);animation:rise .3s ease both}
.ov-card .mascot{font-size:44px}
.ov-title{font-size:16px;color:var(--phosphor);margin:12px 0 4px}
.ov-msg{font-size:13px;color:var(--fg-dim);min-height:20px}
.ov-bar{height:4px;border-radius:2px;background:var(--panel2);overflow:hidden;margin:16px 0 12px}
.ov-bar span{display:block;height:100%;width:38%;border-radius:2px;background:var(--phosphor);
  will-change:transform}
.ov-hint{font-size:11.5px;color:var(--fg-dim)}
.spin{width:13px;height:13px;border:2px solid var(--phosphor-dim);border-top-color:var(--phosphor);
  border-radius:50%;display:inline-block;animation:spin .8s linear infinite;flex:none}
@keyframes spin{to{transform:rotate(360deg)}}
.assess{color:var(--fg);font-size:13.5px;border-left:3px solid var(--phosphor-dim);padding:8px 12px;
  margin:0 0 14px;background:var(--panel2);border-radius:0 6px 6px 0}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);padding:14px 16px;cursor:pointer;border-radius:8px;
  transition:border-color .15s,transform .15s,box-shadow .15s;animation:rise .5s ease both}
.card:nth-child(2){animation-delay:.08s}.card:nth-child(3){animation-delay:.16s}.card:nth-child(4){animation-delay:.24s}
.card:hover{border-color:var(--phosphor);transform:translateY(-2px);box-shadow:0 6px 22px rgba(0,0,0,.35)}
.card.busy{border-color:var(--phosphor);animation:busypulse 1.2s ease-in-out infinite}
.card.busy .start{color:var(--phosphor)}
@keyframes busypulse{50%{box-shadow:0 0 0 4px rgba(127,215,127,.18)}}
.card h3{margin:0 0 6px;font-size:14.5px;color:var(--fg);font-weight:600}
.card .why{color:var(--fg-dim);font-size:12.5px;margin:6px 0}
.card .teaser{color:var(--fg);font-size:12.5px;margin:6px 0 8px}
.badges{display:flex;gap:6px;flex-wrap:wrap}
.badge{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--fg-dim)}
.badge.kind-coding{border-color:#3d5a75;color:var(--accent)}
.badge.kind-codebase{border-color:var(--phosphor-dim);color:var(--phosphor)}
.badge.min{color:var(--warn);border-color:#5c4d28}
.badge.done{color:var(--phosphor);border-color:var(--phosphor-dim)}
.card .start{margin-top:8px;font-size:12.5px;color:var(--accent)}
.lesson h1{font-size:19px;margin:4px 0 2px;color:var(--fg)}
.lesson .lmeta{color:var(--fg-dim);font-size:12px;margin-bottom:14px}
/* The step-by-step walk: one small idea on screen at a time, dots showing where you are. */
.step-head{display:flex;align-items:center;justify-content:space-between;margin:6px 0 2px}
.step-count{font-size:11.5px;color:var(--fg-dim);letter-spacing:1.2px;text-transform:uppercase}
.step-dots .sdot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--line);
  margin-left:7px;transition:background .25s,transform .25s}
.step-dots .sdot.done{background:var(--phosphor-dim)}
.step-dots .sdot.on{background:var(--phosphor);transform:scale(1.25)}
.step-nav{display:flex;gap:10px;margin-top:16px;border-top:1px dashed var(--line);padding-top:14px}
.step-nav #step-next{margin-left:auto}
.lcontent.stepped{animation:rise .35s ease both}
.lcontent{font-size:14px}
.lcontent h3{color:var(--phosphor);font-size:14px;margin:20px 0 6px}
.lcontent h4{color:var(--fg);font-size:13.5px;margin:16px 0 6px}
.lcontent p{margin:8px 0}
.lcontent code{background:var(--panel2);border:1px solid var(--line);padding:1px 5px;font-size:12.5px;border-radius:3px}
.lcontent pre{background:var(--panel2);border:1px solid var(--line);padding:12px;overflow-x:auto;font-size:12.5px;border-radius:6px}
.lcontent pre code{background:none;border:none;padding:0}
.lcontent ul,.lcontent ol{margin:8px 0;padding-left:24px}
.lcontent li{margin:3px 0}
.subhead{color:var(--phosphor);font-size:13px;letter-spacing:1px;text-transform:uppercase;margin:0 0 14px}
.links{margin-top:40px;border-top:1px dashed var(--line);padding-top:26px}
.links .lk{margin:8px 0;font-size:13px}
.links .lk .note{color:var(--fg-dim);font-size:12px}
.quiz{margin-top:40px;border-top:1px dashed var(--line);padding-top:26px}
.qq{margin:12px 0}
.qq .qt{font-size:13.5px;margin-bottom:6px}
.qq label{display:block;padding:6px 10px;border:1px solid var(--line);margin:4px 0;cursor:pointer;font-size:13px;
  border-radius:5px;transition:border-color .12s}
.qq label:hover{border-color:var(--phosphor-dim)}
.qq label.right{border-color:var(--phosphor);color:var(--phosphor)}
.qq label.wrong{border-color:var(--bad);color:var(--bad)}
.qq .explain{color:var(--fg-dim);font-size:12.5px;margin:4px 0 0 10px;display:none}
.qq .explain.show{display:block}
.exercise{margin-top:40px;border-top:1px dashed var(--line);padding-top:26px}
.exercise .extask{font-size:13.5px;background:var(--panel2);border:1px solid var(--line);border-radius:6px;
  padding:10px 12px;margin-bottom:10px}
.exercise details{margin:8px 0;font-size:12.5px;color:var(--fg-dim)}
.exercise details summary{cursor:pointer}
.exercise .exfeed{margin-top:10px}
.btnrow{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap;align-items:center}
.btn{background:var(--chipbg);border:1px solid var(--line);color:var(--fg);padding:9px 16px;cursor:pointer;
  font:inherit;font-size:13px;border-radius:5px;transition:transform .12s,border-color .12s}
.btn:hover{border-color:var(--phosphor-dim);transform:translateY(-1px)}
.btn.primary{border-color:var(--phosphor-dim);color:var(--phosphor)}
.btn.primary:hover{border-color:var(--phosphor)}
.chat{margin-top:40px;border-top:1px dashed var(--line);padding-top:26px}
.bubble{max-width:85%;padding:8px 12px;margin:8px 0;font-size:13px;border:1px solid var(--line);border-radius:10px;
  animation:rise .35s ease both}
.bubble.user{margin-left:auto;background:#14222b;border-color:#2a4356;border-bottom-right-radius:3px}
.bubble.mentor{background:var(--panel2);border-bottom-left-radius:3px}
.chatrow{display:flex;gap:8px;margin-top:10px}
.chatrow input{flex:1}
/* The mentor-is-thinking bubble: three softly pulsing dots where the reply will appear. */
.bubble.typing{display:inline-flex;align-items:center;gap:5px;padding:12px 16px}
.bubble.typing .tdot{width:7px;height:7px;border-radius:50%;background:var(--fg-dim);
  animation:tblink 1.2s ease-in-out infinite}
.bubble.typing .tdot:nth-child(2){animation-delay:.2s}
.bubble.typing .tdot:nth-child(3){animation-delay:.4s}
@keyframes tblink{0%,60%,100%{opacity:.25;transform:translateY(0)}30%{opacity:1;transform:translateY(-3px)}}
.progress .pstats{display:flex;gap:22px;flex-wrap:wrap;margin-bottom:10px}
/* Stat tooltips are drawn by CSS (::after on hover): reliable and instant, unlike the
   native title bubble, which proved flaky over these elements. */
.pstat{cursor:help;position:relative}
.pstat b{color:var(--phosphor);font-size:17px}
.pstat span{color:var(--fg-dim);font-size:12px;display:block;border-bottom:1px dotted var(--line)}
.pstat::after{content:attr(data-tip);position:absolute;left:0;top:100%;margin-top:8px;z-index:20;
  width:240px;background:var(--panel2);border:1px solid var(--phosphor-dim);border-radius:6px;
  padding:8px 11px;font-size:11.5px;color:var(--fg);line-height:1.5;
  opacity:0;visibility:hidden;pointer-events:none;transition:opacity .15s;
  box-shadow:0 8px 24px rgba(0,0,0,.45)}
.pstat:hover::after{opacity:1;visibility:visible}
.ppager{display:flex;align-items:center;justify-content:center;gap:14px;padding:10px 0 2px;
  border-top:1px solid var(--line)}
.plist .pl{display:flex;align-items:baseline;gap:10px;padding:7px 4px;border-top:1px solid var(--line);
  font-size:13px;cursor:pointer}
.plist .pl:hover .plt{color:var(--accent)}
.plist .st{flex:none;width:16px}
.plist .plt{flex:1}
.plist .pmeta{color:var(--fg-dim);font-size:11.5px;flex:none;display:flex;gap:6px;align-items:baseline}
.plist .pldel{flex:none;background:none;border:none;color:var(--fg-dim);font:inherit;font-size:12px;
  cursor:pointer;padding:2px 6px;border-radius:4px;opacity:0;visibility:hidden;transition:color .15s}
.plist .pl:hover .pldel,.plist .pldel:focus-visible{opacity:1;visibility:visible}
.plist .pldel:hover{color:var(--bad)}
.plist .pldel.armed{opacity:1;color:var(--bad);border:1px solid var(--bad)}
.error{border:1px solid var(--bad);color:var(--bad);padding:10px 14px;font-size:13px;margin:10px 0;border-radius:6px}
.notice{border:1px solid var(--warn);color:var(--warn);padding:10px 14px;font-size:13px;margin:10px 0;border-radius:6px}
.engine{margin-top:26px;border:1px solid var(--line);background:var(--panel);border-radius:8px}
.engine summary{cursor:pointer;padding:10px 16px;color:var(--fg-dim);font-size:12.5px;list-style:none}
.engine summary::before{content:"\2699\FE0F  "}
.engine summary:hover{color:var(--fg)}
.engine .ebody{padding:4px 16px 14px}
.engine .esaved{color:var(--phosphor)}
/* Emoji confetti on a completed lesson: a brief, gentle celebration. */
.confetti{position:fixed;inset:0;pointer-events:none;overflow:hidden;z-index:50}
.confetti span{position:absolute;top:-30px;font-size:20px;animation:fall 2.6s ease-in forwards}
@keyframes fall{to{transform:translateY(105vh) rotate(340deg);opacity:.1}}
footer{margin-top:46px;padding-top:18px;border-top:1px dashed var(--line);color:var(--fg-dim);font-size:12px}
/* The backtrace notice: a frozen top strip, amber like the dashboard's, always visible. */
.btbanner{position:sticky;top:0;z-index:55;margin:0;padding:10px 18px;background:var(--panel);
  border-bottom:2px solid #5c4d28;color:var(--warn);font-size:12.5px;line-height:1.5;
  text-align:center;box-shadow:0 4px 16px rgba(0,0,0,.55)}
footer code{color:var(--fg)}
[hidden]{display:none !important}
@media (max-width:600px){.cards{grid-template-columns:1fr}.bubble{max-width:100%}}
</style>
</head>
<body>
__BACKTRACE_BANNER__
<div class="ambient"></div>
<div class="wrap">
  <header class="rise">
    <div class="brand"><span class="a">a</span>GiTrack<span class="sub">&nbsp;learn</span></div>
    <div class="meta"><span>repo</span> <b>__REPO__</b> <span id="me-meta"></span></div>
    <a class="backlink" id="backlink" href="./">&larr; dashboard</a>
  </header>

  <div class="panel checkin rise" id="checkin">
    <p class="lead"><span class="mascot">&#127793;</span><span><span class="hi" id="hello">Hi!</span> I read your agent sessions and can teach you something genuinely useful. Just tell me how you're doing right now:</span></p>
    <div class="notice" id="trace-notice" hidden></div>
    <div class="row"><label>based on interactions from</label>
      <select id="f-source"><option value="">entire team</option></select>
      <span id="branch-wrap"><label style="min-width:auto">branch</label>
      <select id="f-branch"></select></span>
      <label style="min-width:auto">period</label>
      <select id="f-period">
        <option value="">all time</option>
        <option value="30" selected>last 30 days</option>
        <option value="7">last 7 days</option>
      </select>
    </div>
    <div class="row"><label>time I have</label>
      <div class="chips" id="time-chips">
        <button class="chip" data-v="5">&#9749; 5 min</button>
        <button class="chip" data-v="15">&#9200; 15 min</button>
        <button class="chip" data-v="30">&#129504; 30 min</button>
      </div>
    </div>
    <div class="row"><label>feeling</label>
      <div class="chips" id="mood-chips">
        <button class="chip" data-v="fresh">&#128267; fresh</button>
        <button class="chip" data-v="okay">&#128578; okay</button>
        <button class="chip" data-v="tired">&#129715; tired</button>
      </div>
    </div>
    <div class="row"><label>optional</label>
      <input type="text" id="f-note" placeholder="anything specific on your mind? (leave empty and I'll pick)" maxlength="300">
    </div>
    <button class="gobtn" id="go">&#10024; find me something worth learning</button>
    <div class="hint">I'll look at the recent sessions, spot what would help you most, and size it to your time. Your progress is saved automatically. Press the button again any time (after new commits, or with different filters) for fresh picks.</div>
  </div>

  <div id="agent-wait" class="thinking" hidden><span class="spin"></span><span class="grow" id="wait-icon">&#127793;</span><span id="wait-msg"></span></div>

  <div class="overlay" id="overlay" hidden>
    <div class="ov-card">
      <span class="mascot" id="ov-icon">&#128221;</span>
      <div class="ov-title" id="ov-title"></div>
      <div class="ov-msg" id="ov-msg"></div>
      <div class="ov-bar"><span></span></div>
      <div class="ov-hint">your coach is reading the traces and writing just for you; this usually takes under a minute</div>
    </div>
  </div>
  <div id="flash"></div>

  <div id="suggestwrap" hidden>
    <div class="sechead"><h2 class="section">picked for you</h2>
      <button class="btn small" id="reset-suggest" title="Clear these picks and check in again, e.g. after new commits or different filters">&#8635; start over</button></div>
    <div class="assess" id="assess" hidden></div>
    <div class="cards" id="suggestions"></div>
  </div>

  <div id="lessonwrap" hidden>
    <div class="panel lesson rise">
      <button class="btn" id="lesson-back">&larr; back</button>
      <h1 id="lesson-title"></h1>
      <div class="lmeta" id="lesson-meta"></div>
      <div class="step-head" id="step-head" hidden>
        <span class="step-count" id="step-count"></span>
        <span class="step-dots" id="step-dots"></span>
      </div>
      <div class="lcontent" id="lesson-content"></div>
      <div class="step-nav" id="step-nav" hidden>
        <button class="btn" id="step-prev">&larr; back a step</button>
        <button class="btn primary" id="step-next">got it, next &rarr;</button>
      </div>
      <div class="links" id="lesson-links"></div>
      <div class="quiz" id="lesson-quiz" hidden>
        <h3 class="subhead">&#128161; quick check</h3>
        <div id="quiz-qs"></div>
        <div class="btnrow"><button class="btn" id="quiz-check">check my answers</button>
          <span class="hint" id="quiz-result"></span></div>
      </div>
      <div class="exercise" id="lesson-ex" hidden>
        <h3 class="subhead">&#128296; try it yourself</h3>
        <div id="ex-work">
          <div class="extask" id="ex-task"></div>
          <details id="ex-hint-wrap"><summary>need a hint?</summary><div id="ex-hint"></div></details>
          <textarea id="ex-notes" placeholder="type your answer here; everything you need is in the task above"></textarea>
          <div class="btnrow">
            <button class="btn primary" id="ex-check">ask my mentor to review</button>
            <button class="btn" id="ex-skip">skip for now</button>
            <span class="hint" id="ex-status"></span>
          </div>
        </div>
        <div class="hint" id="ex-skipped" hidden>skipped for now, no pressure.
          <button class="btn small" id="ex-resume">give it a try after all</button></div>
        <div class="exfeed" id="ex-feedback"></div>
      </div>
      <div class="btnrow">
        <button class="btn primary" id="lesson-done">&#10003; got it, done</button>
      </div>
      <div class="chat">
        <h3 class="subhead">&#128172; ask a follow-up</h3>
        <div id="chatlog"></div>
        <div class="chatrow">
          <input type="text" id="chat-input" placeholder="anything unclear? ask me" maxlength="2000">
          <button class="btn" id="chat-send">send</button>
        </div>
      </div>
    </div>
  </div>

  <div id="progresswrap" hidden>
    <h2 class="section">your progress</h2>
    <div class="panel progress">
      <div class="pstats" id="pstats"></div>
      <div class="plist" id="plist"></div>
    </div>
  </div>

  <details class="engine" id="engine">
    <summary>coach engine &amp; progress sync</summary>
    <div class="ebody">
      <div class="row"><label>backend</label>
        <select id="e-backend">
          <option value="">auto (latest session)</option>
          <option value="claude">claude</option>
          <option value="opencode">opencode</option>
        </select>
        <label style="min-width:auto">model</label>
        <select id="e-model"><option value="">auto (latest session)</option></select>
        <button class="btn" id="e-save">save</button>
        <span class="hint" id="e-msg"></span>
      </div>
      <div class="hint">Saved to <code>.agitrack/config.json</code> in this repo as <code>learning_backend</code> / <code>learning_model</code>. You can also edit it there, or set it globally in <code>~/.agitrack/config.json</code>.</div>
      <div class="row"><label>progress sync</label>
        <button class="chip" id="sync-toggle">off</button>
        <span class="hint" id="sync-msg" style="margin-top:0"></span>
      </div>
      <div class="hint">When on, your progress log is published to git (<code>refs/agitrack/learning-progress</code>, one entry per GitHub user) the same way shared sessions are, so teammates can see it. Off by default; progress always stays tracked locally either way.</div>
    </div>
  </details>

  <footer id="backendnote"></footer>
</div>

<script>
"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

const state = { me: "", source: "", branch: "", branches: [], minutes: 0, mood: "", profile: null, lesson: null,
                sync: null, openedAt: 0, flushedS: 0, waitTimer: null,
                steps: [], step: 0, endReached: false, progressPage: 0, barAnim: null };

function period() {
  const days = $("f-period").value;
  if (!days) return {from: 0, to: 0};
  return {from: Math.floor(Date.now()/1000) - Number(days)*86400, to: 0};
}

async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
                               body: JSON.stringify(body), cache: "no-store"});
  if (!r.ok) throw new Error("server error " + r.status);
  return r.json();
}

function flash(html) { $("flash").innerHTML = html; }
function clearFlash() { flash(""); }

// ------- friendly waiting states (an agent call takes a little while) -------
// The big generation calls (suggestions, a lesson) take over the whole screen with a
// dimmed overlay + progress bar, so there is never any doubt something is happening,
// wherever the page is scrolled. Conversational calls (chat, exercise review) instead
// show a typing bubble right where the reply will appear (see showTyping).
const WAIT = {
  suggest: {icon: "\u{1F331}", overlay: true, title: "finding something worth learning…",
            msgs: ["reading your recent sessions…", "spotting patterns in how you work…",
            "checking what you already learned…", "picking something worth your time…"]},
  lesson:  {icon: "\u{1F4DD}", overlay: true, title: "writing your lesson…",
            msgs: ["reading the traces behind this topic…", "tailoring the examples to your repo…",
            "building the steps, quiz and exercise…", "adding links worth opening…", "almost there…"]}
};
function startBarAnimation() {
  const bar = document.querySelector("#overlay .ov-bar span");
  if (!bar || !bar.animate) return;
  if (state.barAnim) state.barAnim.cancel();
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    bar.style.width = "100%";
    state.barAnim = bar.animate([{opacity: .25}, {opacity: .85}, {opacity: .25}],
                                {duration: 2000, iterations: Infinity});
  } else {
    bar.style.width = "38%";
    state.barAnim = bar.animate([{transform: "translateX(-110%)"}, {transform: "translateX(290%)"}],
                                {duration: 1600, iterations: Infinity, easing: "ease-in-out"});
  }
}

function startWait(kind) {
  let i = 0;
  const w = WAIT[kind];
  if (w.overlay) {
    $("overlay").hidden = false;
    $("ov-icon").textContent = w.icon;
    $("ov-title").textContent = w.title;
    $("ov-msg").textContent = w.msgs[0];
    startBarAnimation();
  } else {
    $("agent-wait").hidden = false;
    $("wait-icon").textContent = w.icon;
    $("wait-msg").textContent = w.msgs[0];
  }
  const target = w.overlay ? "ov-msg" : "wait-msg";
  state.waitTimer = setInterval(() => {
    i = (i + 1) % w.msgs.length;
    $(target).textContent = w.msgs[i];
  }, 3500);
}
function stopWait() {
  clearInterval(state.waitTimer);
  if (state.barAnim) { state.barAnim.cancel(); state.barAnim = null; }
  $("agent-wait").hidden = true;
  $("overlay").hidden = true;
}

// Gentle emoji confetti for finished lessons and passed exercises.
function celebrate() {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const box = document.createElement("div");
  box.className = "confetti";
  const icons = ["\u{1F389}", "✨", "\u{1F331}", "\u{1F4A1}", "\u{1F393}"];
  for (let i = 0; i < 18; i++) {
    const s = document.createElement("span");
    s.textContent = icons[i % icons.length];
    s.style.left = (4 + Math.random()*92) + "vw";
    s.style.animationDelay = (Math.random()*0.7) + "s";
    s.style.fontSize = (14 + Math.random()*12) + "px";
    box.appendChild(s);
  }
  document.body.appendChild(box);
  setTimeout(() => box.remove(), 3600);
}

// ---------------------------------------------------------------- markdown (tiny)
function md(src) {
  src = String(src || "");
  const blocks = [];
  src = src.replace(/```([\s\S]*?)```/g, (_, code) => {
    blocks.push("<pre><code>" + esc(code.replace(/^\w*\n/, "")) + "</code></pre>");
    return "\x00" + (blocks.length - 1) + "\x00";
  });
  let h = esc(src);
  h = h.replace(/^#{4,6}\s+(.+)$/gm, "<h4>$1</h4>")
       .replace(/^###\s+(.+)$/gm, "<h3>$1</h3>")
       .replace(/^##\s+(.+)$/gm, "<h3>$1</h3>")
       .replace(/^#\s+(.+)$/gm, "<h3>$1</h3>")
       .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
       .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>")
       .replace(/`([^`\n]+)`/g, "<code>$1</code>")
       .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
                '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  const lines = h.split("\n");
  const out = [];
  let list = null, para = [];
  const endPara = () => { if (para.length) { out.push("<p>" + para.join(" ") + "</p>"); para = []; } };
  const endList = () => { if (list) { out.push("</" + list + ">"); list = null; } };
  for (const line of lines) {
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

// ------------------------------------------------------------------ rendering
function kindBadge(kind) {
  return kind === "codebase"
    ? '<span class="badge kind-codebase">&#128193; this codebase</span>'
    : '<span class="badge kind-coding">&#128295; coding skill</span>';
}

// The newest lesson generated from a given suggestion (matched by the stored
// suggestion_id, falling back to the id prefix older lessons embed).
function lessonForSuggestion(sid) {
  const lessons = (state.profile && state.profile.lessons) || [];
  for (let i = lessons.length - 1; i >= 0; i--) {
    const l = lessons[i];
    if (l.suggestion_id === sid || String(l.id || "").replace(/-\d+$/, "") === sid) return l;
  }
  return null;
}

function renderSuggestions() {
  const p = state.profile;
  const list = (p && p.suggestions) || [];
  // A pick whose lesson is finished has served its purpose: it leaves the list
  // (it lives on under "your progress"). One with an unfinished lesson stays,
  // but continues that lesson instead of generating a second copy.
  const open = list.filter(s => {
    const l = lessonForSuggestion(s.id);
    return !(l && l.status === "completed");
  });
  $("suggestwrap").hidden = !list.length && !(p && p.assessment);
  const notice = p && p.trace_notice;
  $("assess").hidden = !(p && p.assessment) && !notice;
  if (notice) $("assess").textContent = notice;
  else if (p && p.assessment) $("assess").textContent = p.assessment;
  if (list.length && !open.length) {
    $("suggestions").innerHTML =
      '<div class="hint">&#127881; you finished everything I picked. Check in above and I\'ll find you something new.</div>';
    return;
  }
  $("suggestions").innerHTML = open.map(s => {
    const started = lessonForSuggestion(s.id);
    return `
    <div class="card" data-id="${esc(s.id)}">
      <div class="badges">${kindBadge(s.kind)}<span class="badge min">&#9200; ~${esc(s.minutes)} min</span></div>
      <h3>${esc(s.title)}</h3>
      ${s.teaser ? `<div class="teaser">${esc(s.teaser)}</div>` : ""}
      ${s.why ? `<div class="why">why now: ${esc(s.why)}</div>` : ""}
      <div class="start">${started ? "continue &rarr;" : "start &rarr;"}</div>
    </div>`;
  }).join("");
  for (const card of $("suggestions").querySelectorAll(".card"))
    card.addEventListener("click", () => {
      const existing = lessonForSuggestion(card.dataset.id);
      if (existing) openLesson(existing);
      else openSuggestion(card.dataset.id);
    });
}

const PROGRESS_PAGE_SIZE = 8;

function renderProgress() {
  const p = state.profile;
  const lessons = (p && p.lessons) || [];
  $("progresswrap").hidden = !lessons.length;
  if (!lessons.length) return;
  const done = lessons.filter(l => l.status === "completed");
  const quizzes = lessons.filter(l => l.quiz_total > 0).length;
  const exDone = lessons.filter(l => l.exercise && l.exercise.status === "done").length;
  $("pstats").innerHTML = `
    <div class="pstat" data-tip="Lessons you finished with 'got it, done'."><b>${done.length}</b><span>lessons done</span></div>
    <div class="pstat" data-tip="Lessons whose quick-check quiz you answered."><b>${quizzes}</b><span>quizzes taken</span></div>
    <div class="pstat" data-tip="Hands-on exercises where your typed answer passed the mentor's review. Quizzes count separately, and skipped exercises don't count."><b>${exDone}</b><span>exercises done</span></div>`;
  // Newest first, split into pages so a long learning history stays scannable.
  const items = lessons.slice().reverse();
  const pages = Math.max(1, Math.ceil(items.length / PROGRESS_PAGE_SIZE));
  state.progressPage = Math.min(state.progressPage || 0, pages - 1);
  const start = state.progressPage * PROGRESS_PAGE_SIZE;
  const rows = items.slice(start, start + PROGRESS_PAGE_SIZE).map(l => `
    <div class="pl" data-id="${esc(l.id)}">
      <span class="st">${l.status === "completed" ? "&#10003;" : "&#9679;"}</span>
      <span class="plt">${esc(l.title)}</span>
      <span class="pmeta">${kindBadge(l.kind)}${l.quiz_total ? `<span class="badge done">quiz ${l.quiz_correct}/${l.quiz_total}</span>` : ""}${l.exercise && l.exercise.status === "done" ? '<span class="badge done">&#128296; exercise</span>' : ""}</span>
      <button class="pldel" title="Remove this lesson from your history">&#10005;</button>
    </div>`).join("");
  const pager = pages > 1 ? `
    <div class="ppager">
      <button class="btn small" id="pp-prev" ${state.progressPage === 0 ? "disabled" : ""}>&larr; newer</button>
      <span class="hint" style="margin-top:0">page ${state.progressPage + 1} of ${pages}</span>
      <button class="btn small" id="pp-next" ${state.progressPage >= pages - 1 ? "disabled" : ""}>older &rarr;</button>
    </div>` : "";
  $("plist").innerHTML = rows + pager;
  for (const row of $("plist").querySelectorAll(".pl")) {
    row.addEventListener("click", () => {
      const lesson = lessons.find(l => l.id === row.dataset.id);
      if (lesson) openLesson(lesson);
    });
    // Two-step delete so a stray click can't erase history: the first click arms the
    // button ("sure?"), the second within a few seconds deletes.
    const del = row.querySelector(".pldel");
    // Leaving the row hides AND disarms the button, so it never lingers.
    row.addEventListener("mouseleave", () => {
      del.classList.remove("armed");
      del.innerHTML = "&#10005;";
    });
    del.addEventListener("click", async e => {
      e.stopPropagation();
      if (!del.classList.contains("armed")) {
        del.classList.add("armed");
        del.textContent = "sure?";
        setTimeout(() => { del.classList.remove("armed"); del.innerHTML = "&#10005;"; }, 4000);
        return;
      }
      try {
        const r = await post("learn/delete", {lesson_id: row.dataset.id});
        if (r.error) { flash(`<div class="error">${esc(r.error)}</div>`); return; }
        state.profile = r.profile;
        renderProgress();
        renderSuggestions(); // a deleted completed lesson may resurface its pick
      } catch (err) { flash(`<div class="error">${esc(err.message)}</div>`); }
    });
  }
  if (pages > 1) {
    $("pp-prev").addEventListener("click", () => { state.progressPage--; renderProgress(); });
    $("pp-next").addEventListener("click", () => { state.progressPage++; renderProgress(); });
  }
}

function renderBackendNote(info) {
  if (!info) return;
  if (info.error) { $("backendnote").innerHTML = "&#9888; " + esc(info.error); return; }
  const model = info.model ? esc(info.model) : "backend default model";
  const src = info.backend_source === "config" ? "pinned in config" : "from your latest session";
  $("backendnote").innerHTML =
    `lessons are generated by <b>${esc(info.backend)}</b> &middot; ${model} (${src}). ` +
    `Adjust it in the "coach engine" panel above, or set <code>learning_backend</code> / ` +
    `<code>learning_model</code> in <code>.agitrack/config.json</code>.`;
  if ($("e-backend").dataset.userTouched !== "1") {
    $("e-backend").value = info.backend_source === "config" ? info.backend : "";
    loadModels(info);
  }
}

function renderSync() {
  const s = state.sync;
  if (!s) return;
  if (s.available === false) {
    $("sync-toggle").textContent = "unavailable";
    $("sync-toggle").disabled = true;
    $("sync-msg").textContent = "needs a git repository; progress still stays tracked locally";
    return;
  }
  $("sync-toggle").textContent = s.enabled ? "on" : "off";
  $("sync-toggle").classList.toggle("sel", !!s.enabled);
  let msg = "";
  if (s.enabled && s.last) msg = s.last.ok ? "synced &#10003;" : ("last sync failed: " + esc(s.last.error || "unknown"));
  const others = (s.users || []).filter(u => u.gid !== state.me);
  if (others.length) msg += (msg ? " &middot; " : "") + "also syncing: " + others.map(u => esc(u.gid)).join(", ");
  $("sync-msg").innerHTML = msg;
}

// ------------------------------------------------------------------ lesson view
// The steps to walk through. New lessons carry a steps[] array; an older stored lesson
// (one Markdown blob) is split on its ### headings so it gets the same guided treatment.
function lessonSteps(lesson) {
  if (Array.isArray(lesson.steps) && lesson.steps.length) return lesson.steps;
  const src = String(lesson.content_md || "");
  const steps = [];
  let current = {title: "", content_md: ""};
  for (const line of src.split("\n")) {
    const m = /^###\s+(.*)/.exec(line);
    if (m) {
      if (current.title || current.content_md.trim()) steps.push(current);
      current = {title: m[1].trim(), content_md: ""};
    } else current.content_md += line + "\n";
  }
  if (current.title || current.content_md.trim()) steps.push(current);
  return steps.length ? steps : [{title: "", content_md: src}];
}

function openLesson(lesson) {
  flushTime();
  state.lesson = lesson;
  state.openedAt = Date.now();
  state.flushedS = 0;
  state.steps = lessonSteps(lesson);
  // A finished lesson reopens fully unfolded; a fresh one starts the guided walk.
  state.endReached = lesson.status === "completed";
  state.step = state.endReached ? state.steps.length - 1 : 0;
  $("checkin").hidden = true;
  $("suggestwrap").hidden = true;
  $("progresswrap").hidden = true;
  $("lessonwrap").hidden = false;
  clearFlash();
  $("lesson-title").textContent = lesson.title;
  $("lesson-meta").innerHTML = `${kindBadge(lesson.kind)} <span class="badge min">&#9200; ~${esc(lesson.minutes)} min</span> <span class="badge">${state.steps.length > 1 ? state.steps.length + " small steps" : ""}</span>`;
  $("lesson-links").innerHTML = (lesson.links || []).length
    ? '<h3 class="subhead">&#128279; go deeper</h3>' +
      lesson.links.map(k => `<div class="lk"><a href="${esc(k.url)}" target="_blank" rel="noopener noreferrer">${esc(k.title)}</a>${k.note ? ` <span class="note">${esc(k.note)}</span>` : ""}</div>`).join("")
    : "";
  renderQuiz(lesson);
  renderExercise(lesson);
  renderChat(lesson);
  renderStep();
  window.scrollTo(0, 0);
  if (lesson.status !== "completed")
    post("learn/progress", {lesson_id: lesson.id, status: "started"}).catch(() => {});
}

// One step on screen at a time; the quiz, exercise, links and Done button unlock when
// the learner reaches the final step, so the page itself guides the order of things.
function renderStep() {
  const steps = state.steps;
  const i = state.step;
  const many = steps.length > 1;
  $("step-head").hidden = !many;
  $("step-nav").hidden = !many;
  if (many) {
    $("step-count").textContent = `step ${i + 1} of ${steps.length}`;
    $("step-dots").innerHTML = steps.map((_, d) =>
      `<span class="sdot ${d === i ? "on" : (d < i ? "done" : "")}"></span>`).join("");
    $("step-prev").disabled = i === 0;
    $("step-next").hidden = i >= steps.length - 1;
  }
  const step = steps[i];
  const content = $("lesson-content");
  content.classList.remove("stepped");
  void content.offsetWidth; // restart the entry animation per step
  content.classList.add("stepped");
  content.innerHTML = (step.title ? `<h3>${esc(step.title)}</h3>` : "") + md(step.content_md);
  if (i >= steps.length - 1) state.endReached = true;
  updateEndSections();
}

function updateEndSections() {
  const lesson = state.lesson;
  const show = state.endReached;
  $("lesson-links").hidden = !show || !(lesson.links || []).length;
  $("lesson-quiz").hidden = !show || !(lesson.quiz || []).length;
  $("lesson-ex").hidden = !show || !lesson.exercise;
  $("lesson-done").hidden = !show || lesson.status === "completed";
}

function stepBy(delta) {
  const next = state.step + delta;
  if (next < 0 || next >= state.steps.length) return;
  state.step = next;
  // Deliberately no scrolling: the next/back buttons stay under the cursor and the new
  // step fades in place, so stepping through feels stable rather than jumpy.
  renderStep();
}

function renderQuiz(lesson) {
  const quiz = lesson.quiz || [];
  $("lesson-quiz").hidden = !quiz.length;
  $("quiz-result").textContent = "";
  $("quiz-qs").innerHTML = quiz.map((q, qi) => `
    <div class="qq" data-qi="${qi}">
      <div class="qt">${qi + 1}. ${esc(q.question)}</div>
      ${q.choices.map((c, ci) => `<label><input type="radio" name="q${qi}" value="${ci}"> ${esc(c)}</label>`).join("")}
      <div class="explain">${esc(q.explain)}</div>
    </div>`).join("");
}

function checkQuiz() {
  const lesson = state.lesson;
  if (!lesson) return;
  let correct = 0;
  const quiz = lesson.quiz || [];
  quiz.forEach((q, qi) => {
    const box = document.querySelector(`.qq[data-qi="${qi}"]`);
    const picked = box.querySelector("input:checked");
    box.querySelectorAll("label").forEach((lab, ci) => {
      lab.classList.remove("right", "wrong");
      if (ci === q.answer) lab.classList.add("right");
      else if (picked && Number(picked.value) === ci) lab.classList.add("wrong");
    });
    box.querySelector(".explain").classList.add("show");
    if (picked && Number(picked.value) === q.answer) correct++;
  });
  $("quiz-result").textContent = correct === quiz.length
    ? "all correct, nicely done!" : `${correct}/${quiz.length} correct. Peek at the notes above.`;
  if (correct === quiz.length && quiz.length) celebrate();
  post("learn/progress", {lesson_id: lesson.id, quiz_correct: correct, quiz_total: quiz.length}).catch(() => {});
}

function renderExercise(lesson) {
  const ex = lesson.exercise;
  $("lesson-ex").hidden = !ex;
  if (!ex) return;
  // Skipping visibly folds the exercise away to a one-line note (with a way back),
  // instead of leaving the full task and buttons sitting there as if nothing happened.
  const skipped = ex.status === "skipped";
  $("ex-work").hidden = skipped;
  $("ex-skipped").hidden = !skipped;
  if (skipped) return;
  // Markdown-render the task and hint: the exercise material (code to study, a diff, a
  // scenario) lives right here in the page, so fenced blocks must display properly.
  $("ex-task").innerHTML = md(ex.task);
  $("ex-hint-wrap").hidden = !ex.hint;
  $("ex-hint").innerHTML = md(ex.hint || "");
  $("ex-notes").value = "";
  $("ex-status").textContent = ex.status === "done" ? "done ✓" : "";
  $("ex-feedback").innerHTML = (ex.attempts || []).map(a =>
    `<div class="bubble mentor">${a.passed ? "✅ " : "\u{1F4AD} "}${md(a.feedback)}</div>`).join("");
}

// A mentor "typing" bubble appended right where the reply will appear, so the thinking
// state is visible at the spot the user is looking, not in an indicator scrolled away.
function showTyping(host) {
  const bubble = document.createElement("div");
  bubble.className = "bubble mentor typing";
  bubble.setAttribute("aria-label", "the mentor is thinking");
  bubble.innerHTML = '<span class="tdot"></span><span class="tdot"></span><span class="tdot"></span>';
  host.appendChild(bubble);
  bubble.scrollIntoView({behavior: "smooth", block: "nearest"});
  return bubble;
}

async function checkExercise() {
  const lesson = state.lesson;
  const notes = $("ex-notes").value.trim();
  if (!lesson || !notes) { $("ex-status").textContent = "type your answer first"; return; }
  const btn = $("ex-check");
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = "reviewing…";
  const typing = showTyping($("ex-feedback"));
  try {
    const r = await post("learn/exercise", {lesson_id: lesson.id, notes});
    if (r.busy) { flash('<div class="notice">I\'m still busy with another request, give me a moment.</div>'); return; }
    if (r.error) { flash(`<div class="error">${esc(r.error)}</div>`); return; }
    clearFlash();
    lesson.exercise.attempts = lesson.exercise.attempts || [];
    lesson.exercise.attempts.push({passed: r.passed, feedback: r.feedback});
    if (r.passed) { lesson.exercise.status = "done"; celebrate(); }
    renderExercise(lesson);
  } catch (e) { flash(`<div class="error">${esc(e.message)}</div>`); }
  finally { typing.remove(); btn.disabled = false; btn.textContent = label; }
}

function skipExercise() {
  const lesson = state.lesson;
  if (!lesson || !lesson.exercise) return;
  lesson.exercise.status = "skipped";
  renderExercise(lesson);
  post("learn/progress", {lesson_id: lesson.id, exercise_status: "skipped"}).catch(() => {});
}

function resumeExercise() {
  const lesson = state.lesson;
  if (!lesson || !lesson.exercise) return;
  lesson.exercise.status = "open";
  renderExercise(lesson);
  post("learn/progress", {lesson_id: lesson.id, exercise_status: "open"}).catch(() => {});
}

function renderChat(lesson) {
  $("chatlog").innerHTML = (lesson.chat || []).map(t =>
    `<div class="bubble ${t.role === "user" ? "user" : "mentor"}">${t.role === "user" ? esc(t.text) : md(t.text)}</div>`).join("");
}

async function sendChat() {
  const lesson = state.lesson;
  const input = $("chat-input");
  const text = input.value.trim();
  if (!lesson || !text) return;
  input.value = "";
  input.disabled = true;
  $("chat-send").disabled = true;
  lesson.chat = lesson.chat || [];
  lesson.chat.push({role: "user", text});
  renderChat(lesson);
  const typing = showTyping($("chatlog"));
  try {
    const r = await post("learn/chat", {lesson_id: lesson.id, message: text});
    if (r.busy) { flash('<div class="notice">I\'m still busy with another request, give me a moment.</div>'); return; }
    if (r.error) { flash(`<div class="error">${esc(r.error)}</div>`); return; }
    clearFlash();
    lesson.chat.push({role: "mentor", text: r.reply});
    renderChat(lesson);
    const last = $("chatlog").lastElementChild;
    if (last) last.scrollIntoView({behavior: "smooth", block: "nearest"});
  } catch (e) { flash(`<div class="error">${esc(e.message)}</div>`); }
  finally {
    typing.remove(); // renderChat may already have replaced it; removing a detached node is fine
    input.disabled = false;
    $("chat-send").disabled = false;
    input.focus();
  }
}

function closeLesson() {
  flushTime();
  state.lesson = null;
  $("lessonwrap").hidden = true;
  $("checkin").hidden = false;
  refreshState();
}

async function finishLesson() {
  const lesson = state.lesson;
  if (!lesson) return;
  const seconds = unflushedSeconds();
  state.flushedS += seconds;
  try {
    await post("learn/progress", {lesson_id: lesson.id, status: "completed", seconds});
  } catch (e) {}
  lesson.status = "completed";
  $("lesson-done").hidden = true;
  celebrate();
  flash('<div class="notice">&#127881; nice, that one is done. It counts toward your progress.</div>');
  closeLesson();
}

// -------------------------------------------------- automatic time tracking
function unflushedSeconds() {
  if (!state.openedAt || document.visibilityState === "hidden") return 0;
  return Math.max(0, Math.round((Date.now() - state.openedAt) / 1000) - state.flushedS);
}
function flushTime(useBeacon) {
  const lesson = state.lesson;
  const seconds = unflushedSeconds();
  if (!lesson || seconds < 5) return;
  state.flushedS += seconds;
  const body = JSON.stringify({lesson_id: lesson.id, seconds});
  if (useBeacon && navigator.sendBeacon) navigator.sendBeacon("learn/progress", new Blob([body], {type: "application/json"}));
  else post("learn/progress", JSON.parse(body)).catch(() => {});
}
setInterval(() => flushTime(false), 30000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flushTime(true); });
window.addEventListener("pagehide", () => flushTime(true));

// ------------------------------------------------------------------ actions
async function suggest() {
  const btn = $("go");
  btn.disabled = true;
  clearFlash();
  startWait("suggest");
  try {
    const pr = period();
    const r = await post("learn/suggest", {source: state.source, branch: state.branch,
                                           from: pr.from, to: pr.to,
                                           minutes: state.minutes, mood: state.mood,
                                           note: $("f-note").value.trim(),
                                           days: Number($("f-period").value) || 0});
    if (r.busy) { flash('<div class="notice">I\'m already thinking about another request, give me a moment and try again.</div>'); return; }
    if (r.error) { flash(`<div class="error">${esc(r.error)}</div>`); return; }
    state.profile = r.profile;
    // The check-in served its purpose: clear it so the next visit starts fresh.
    $("f-note").value = "";
    state.minutes = 0;
    state.mood = "";
    setChips("time-chips", null);
    setChips("mood-chips", null);
    renderSuggestions();
    renderProgress();
    $("suggestwrap").scrollIntoView({behavior: "smooth"});
  } catch (e) { flash(`<div class="error">${esc(e.message)}</div>`); }
  finally { stopWait(); btn.disabled = false; }
}

async function openSuggestion(id) {
  clearFlash();
  // Mark the tapped card as in-progress too, so when the overlay lifts (or if the user
  // dismisses their eyes from it) the chosen topic is visibly the one being written.
  const card = document.querySelector(`#suggestions .card[data-id="${CSS.escape(id)}"]`);
  const start = card && card.querySelector(".start");
  if (card) card.classList.add("busy");
  if (start) start.innerHTML = "&#9997;&#65039; writing your lesson…";
  startWait("lesson");
  try {
    const pr = period();
    const r = await post("learn/lesson", {source: state.source, branch: state.branch,
                                          from: pr.from, to: pr.to, suggestion_id: id});
    if (r.busy) { flash('<div class="notice">I\'m still writing something else, one moment please.</div>'); return; }
    if (r.error) { flash(`<div class="error">${esc(r.error)}</div>`); return; }
    if (state.profile) state.profile.lessons = [...(state.profile.lessons || []), r.lesson];
    openLesson(r.lesson);
  } catch (e) { flash(`<div class="error">${esc(e.message)}</div>`); }
  finally {
    stopWait();
    if (card) card.classList.remove("busy");
    if (start) start.innerHTML = "start &rarr;";
  }
}

// Restore the check-in that produced the CURRENT picks into the controls, once per page
// load, so the time/mood/period/note shown always describe the "picked for you" content.
// The user's own later clicks are never overridden (the guard flips after first use).
function setChips(id, value) {
  for (const c of $(id).querySelectorAll(".chip")) c.classList.toggle("sel", c.dataset.v == value);
}
function applyCheckinContext() {
  const ctx = state.profile && state.profile.suggest_context;
  if (!ctx || state.ctxApplied) return;
  state.ctxApplied = true;
  // Time/mood/note deliberately do NOT restore: each check-in describes the moment,
  // so the page opens with a clean slate (only the data filters carry over).
  if (ctx.days !== undefined) $("f-period").value = ctx.days ? String(ctx.days) : "";
  if (ctx.source !== undefined) state.source = ctx.source;
  if (ctx.branch) state.branch = ctx.branch;
}

async function refreshState() {
  try {
    const r = await fetch("learn/state" + (state.branch ? `?branch=${encodeURIComponent(state.branch)}` : ""), {cache: "no-store"});
    const d = await r.json();
    state.profile = d.profile;
    state.me = d.me || "";
    state.sync = d.sync || null;
    if (d.restored) flash('<div class="notice">&#9729;&#65039; welcome back! I restored your learning progress from git, so you can pick up where you left off.</div>');
    if (state.me) {
      $("hello").textContent = "Hi " + state.me + "!";
      $("me-meta").innerHTML = " &nbsp;&middot;&nbsp; learner <b>" + esc(state.me) + "</b>";
    }
    applyCheckinContext();
    renderBackendNote(d.backend_info);
    renderSync();
    // Branch selector: the trace lives in commits, so it is branch-dependent. Hidden
    // when the server reports no refs (the backtrace reconstruction).
    state.branches = d.branches || [];
    $("branch-wrap").hidden = !state.branches.length;
    if (state.branches.length) {
      const bsel = $("f-branch");
      if (!state.branch) state.branch = d.branch || "";
      bsel.innerHTML = state.branches.map(b => `<option value="${esc(b)}">${esc(b)}</option>`).join("");
      bsel.value = state.branch;
      if (bsel.value !== state.branch) { bsel.value = d.branch || state.branches[0]; state.branch = bsel.value; }
    }
    // With (almost) no captured trace in this branch, say so up front and point at the
    // ways to get one; the check-in still works and offers starter topics.
    if (typeof d.trace_turns === "number" && d.trace_turns < 3) {
      $("trace-notice").innerHTML =
        `&#128269; ${d.trace_turns ? "only " + d.trace_turns : "no"} tracked agent turn${d.trace_turns === 1 ? "" : "s"} captured here yet, so I can't personalize lessons. ` +
        "If this code was written with Claude Code or OpenCode outside aGiTrack, run " +
        "<code>agitrack --backtrace</code> to reconstruct that history (ideally <code>agitrack --backtrace commit</code> " +
        "so it becomes part of the branch). Or simply start your next session with <code>agitrack</code> and the " +
        "trace builds itself. Either way, check in below and I'll offer some starter topics.";
      $("trace-notice").hidden = false;
    } else {
      $("trace-notice").hidden = true;
    }
    if (d.committers) {
      const sel = $("f-source");
      const current = state.source;
      sel.innerHTML = '<option value="">entire team</option>' +
        d.committers.map(c => `<option value="${esc(c)}">${esc(c)}${c === state.me ? " (me)" : ""}</option>`).join("");
      // Default to the learner's own sessions when they appear as a committer. A stale
      // stored source that no longer matches an option would leave the select BLANK
      // (selectedIndex -1), so fall back to "entire team" explicitly.
      sel.value = current || (d.committers.includes(state.me) ? state.me : "");
      if (sel.selectedIndex === -1) sel.selectedIndex = 0;
      state.source = sel.value;
    }
    renderSuggestions();
    renderProgress();
  } catch (e) {}
}

// ------------------------------------------- engine picker & progress sync
async function loadModels(info) {
  const backend = $("e-backend").value || (info && info.backend) || "";
  const sel = $("e-model");
  sel.innerHTML = '<option value="">auto (latest session)</option>';
  if (!backend) return;
  try {
    const r = await fetch(`learn/models?backend=${encodeURIComponent(backend)}`, {cache: "no-store"});
    const d = await r.json();
    for (const m of d.models || []) {
      const o = document.createElement("option");
      o.value = m; o.textContent = m;
      sel.appendChild(o);
    }
    if (info && info.model_source === "config" && info.model) sel.value = info.model;
  } catch (e) {}
}

async function saveEngine() {
  $("e-msg").textContent = "saving…";
  try {
    const r = await post("learn/config", {backend: $("e-backend").value, model: $("e-model").value});
    if (r.error) { $("e-msg").textContent = r.error; return; }
    $("e-msg").innerHTML = '<span class="esaved">saved &#10003;</span>';
    renderBackendNote(r.backend_info);
  } catch (e) { $("e-msg").textContent = e.message; }
}

async function toggleSync() {
  if (state.sync && state.sync.available === false) return;
  const want = !(state.sync && state.sync.enabled);
  $("sync-msg").textContent = want ? "syncing…" : "";
  try {
    const r = await post("learn/sync", {enabled: want});
    if (r.error) { $("sync-msg").textContent = r.error; return; }
    state.sync = r.sync;
    renderSync();
  } catch (e) { $("sync-msg").textContent = e.message; }
}

// ------------------------------------------------------------------ wiring
function wireChips(id, key) {
  $(id).addEventListener("click", e => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    for (const c of $(id).querySelectorAll(".chip")) c.classList.toggle("sel", c === chip);
    state[key] = key === "minutes" ? Number(chip.dataset.v) : chip.dataset.v;
  });
}
wireChips("time-chips", "minutes");
wireChips("mood-chips", "mood");
$("go").addEventListener("click", suggest);
$("reset-suggest").addEventListener("click", async () => {
  try {
    const r = await post("learn/reset", {});
    if (r.profile) {
      state.profile = r.profile;
      renderSuggestions();
      renderProgress();
      $("checkin").scrollIntoView({behavior: "smooth"});
      flash('<div class="notice">fresh start! check in above and I\'ll pick again from the latest sessions.</div>');
    }
  } catch (e) {}
});
// Going back to the dashboard via history restores it instantly from the browser's
// back/forward cache instead of a full reload (and its commit-history crunch).
$("backlink").addEventListener("click", e => {
  try {
    if (document.referrer && new URL(document.referrer).origin === location.origin && history.length > 1) {
      e.preventDefault();
      history.back();
    }
  } catch (err) {}
});
$("f-source").addEventListener("change", () => { state.source = $("f-source").value; });
$("f-branch").addEventListener("change", () => {
  state.branch = $("f-branch").value;
  refreshState(); // committer list and the trace notice are branch-dependent
});
$("lesson-back").addEventListener("click", closeLesson);
$("lesson-done").addEventListener("click", finishLesson);
$("step-prev").addEventListener("click", () => stepBy(-1));
$("step-next").addEventListener("click", () => stepBy(1));
$("quiz-check").addEventListener("click", checkQuiz);
$("ex-check").addEventListener("click", checkExercise);
$("ex-skip").addEventListener("click", skipExercise);
$("ex-resume").addEventListener("click", resumeExercise);
$("chat-send").addEventListener("click", sendChat);
$("chat-input").addEventListener("keydown", e => { if (e.key === "Enter") sendChat(); });
$("f-note").addEventListener("keydown", e => { if (e.key === "Enter") suggest(); });
$("e-backend").addEventListener("change", () => { $("e-backend").dataset.userTouched = "1"; loadModels(null); });
$("e-save").addEventListener("click", saveEngine);
$("sync-toggle").addEventListener("click", toggleSync);

refreshState();
</script>
</body>
</html>
"""
