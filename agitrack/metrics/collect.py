"""Collect per-commit statistics from git history and aGiTrack metadata (#54).

Every aGiTrack commit carries a ``# aGiTrack Metadata`` block (``commit_type``,
``backend``, ``model``, ``tokens_since_last_commit_*``, ``covered_commits``,
…) in its message, so the whole dashboard can be computed from ``git log``
alone — no extra state files, and it works on any clone of the repository.

Commit classification:

``agent``
    A commit aGiTrack created from the agent's work (``commit_type: agent``),
    including the merge-shaped cover commits placed on top of backend-made
    commits (#58).
``covered``
    A commit the backend made itself (``git commit`` run by the agent); it has
    no metadata of its own but is listed in some aGiTrack commit's
    ``covered_commits`` line. AI work.
``agent-merge``
    An integration merge whose conflicts an agent resolved.
``user``
    A user commit created through aGiTrack (``commit_type: user``). Non-tracked
    lines: even a user-made commit may contain lines an agent produced, so it is
    not claimed as human work.
``agitrack-ops``
    aGiTrack's own integration plumbing — the auto-generated merge commits it makes
    to bring base into a session branch (or back). Tracked, but its merges carry
    no diff, so it contributes no lines.
``untracked``
    No aGiTrack metadata and not covered — made outside aGiTrack. Non-tracked lines.

Line provenance is reported two ways only: aGiTrack-tracked AI (agent + covered +
agent-merge) versus non-tracked (user + untracked); there is no "human" bucket.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from agitrack.commits import METADATA_HEADER
from agitrack.git import GitRepo

_NUMSTAT_RE = re.compile(r"^(\d+|-)\t(\d+|-)\t")
_TOKEN_KEY_PREFIX = "tokens_since_last_commit_"
_RECORD_SEP = "\x00"
_FIELD_SEP = "\x01"

# aGiTrack turn branches are named `agitrack/<backend>/<session>/t<n>`; an auto-merge
# subject naming one is aGiTrack's own integration plumbing. The legacy `agit/`
# prefix is matched too so commits made before the rename still classify correctly.
_AGITRACK_BRANCH_RE = re.compile(r"agit(?:rack)?/[^/\s']+/[^/\s']+/t\d+")

# Commit markers written before the aGiT → aGiTrack rename. Historical commits carry
# the old metadata header and subject prefixes; normalising a body to the new markers
# up front lets the rest of the parser key off the current constants only.
_LEGACY_METADATA_HEADER = "# aGiT Metadata"
_LEGACY_SUBJECT_REPLACEMENTS = (("<aGiT-merge> ", "<aGiTrack-merge> "), ("<aGiT> ", "<aGiTrack> "))


def _normalize_legacy_markers(body: str) -> str:
    """Rewrite pre-rename commit markers (`# aGiT Metadata`, `<aGiT> `/`<aGiT-merge> `
    subjects) to their aGiTrack equivalents so the parser recognises old commits."""
    if _LEGACY_METADATA_HEADER in body:
        body = body.replace(_LEGACY_METADATA_HEADER, METADATA_HEADER)
    for old, new in _LEGACY_SUBJECT_REPLACEMENTS:
        if old in body:
            body = body.replace(old, new)
    return body


# A GitHub no-reply address (optionally `ID+`-prefixed) carries the user's
# login verbatim — the only GitHub identity that lives in `git log` itself.
_NOREPLY_RE = re.compile(
    r"^(?:\d+\+)?([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)@users\.noreply\.github\.com$",
    re.IGNORECASE,
)

# A `Co-Authored-By: Name <email>` trailer (git's standard multi-author credit;
# GitHub squash-merges emit one per PR contributor).
_CO_AUTHOR_RE = re.compile(r"(?im)^\s*co-authored-by:\s*(.*?)\s*<([^>]+)>\s*$")


def _is_non_human_committer(name: str, email: str) -> bool:
    """Whether a commit identity (primary author or a co-author trailer) is the AI
    assistant or a bot rather than a person — those aren't committers and are kept
    out of the committer list/filter and the per-committer breakdown. Covers the
    Claude credit (``noreply@anthropic.com``) and bot accounts, whose login carries
    a ``[bot]`` suffix in the name and the no-reply email (e.g.
    ``github-actions[bot]`` / ``41898282+github-actions[bot]@users.noreply.github.com``)."""
    name, email = name.lower(), email.lower()
    return email.endswith("noreply@anthropic.com") or "[bot]" in name or "[bot]" in email


def _parse_co_authors(body: str) -> list[tuple[str, str]]:
    """Human co-authors from a commit message's ``Co-Authored-By:`` trailers, as
    ``(name, lowercased email)`` pairs, de-duplicated and order-preserving."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for match in _CO_AUTHOR_RE.finditer(body):
        name = match.group(1).strip()
        email = match.group(2).strip().lower()
        if _is_non_human_committer(name, email):
            continue
        key = (name, email)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


@dataclass
class CommitStat:
    """One commit's contribution to the dashboard."""

    sha: str
    author: str
    email: str
    subject: str
    kind: str  # agent | covered | agent-merge | user | agitrack-ops | untracked
    timestamp: int = 0  # commit author date, epoch seconds (every commit has one)
    started_at: str = ""  # AI conversation start, ISO-8601 UTC (agent commits)
    ended_at: str = ""  # AI conversation end, ISO-8601 UTC (agent commits)
    backend: str | None = None
    model: str | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    insertions: int = 0
    deletions: int = 0
    covered_commits: list[str] = field(default_factory=list)
    # Human co-authors from `Co-Authored-By:` trailers (name, lowercased email),
    # so a commit credited to several people is filterable under each of them (#54).
    # The AI assistant and bot accounts are excluded — they aren't committers.
    co_authors: list[tuple[str, str]] = field(default_factory=list)
    prompt: str = ""  # the turn's prompt text (parsed from the trace)
    user_prompts: list[str] = field(default_factory=list)  # trace ## User entries
    metadata_block: str = ""  # raw `# aGiTrack Metadata` text, for duplicate detection
    message: str = ""  # the full commit message (shown when a log entry is opened)
    # For a squash / PR-merge that concatenated several commits' metadata blocks:
    # the original commits, each parsed from its block, so their tokens and
    # model/backend usage are counted and the squash is expandable in the UI.
    constituents: list[CommitStat] = field(default_factory=list)
    # A manual-commit-mode latent turn: recorded on refs/agitrack/manual/* and not yet
    # folded into a real branch commit. Shown in the dashboard as an in-progress turn so
    # the user sees the session's work before they commit (#manual-commits).
    pending: bool = False

    @property
    def short(self) -> str:
        return self.sha[:7]

    @property
    def lines(self) -> int:
        return self.insertions + self.deletions


# Placeholders the backend/model commit metadata uses for "no real value". The
# writer records ``model: unknown`` when a turn's model couldn't be determined
# (commits/message.py), and the Claude transcript can carry ``<synthetic>`` as the
# "model" of synthetic (non-LLM) assistant messages — compaction notices, interrupt
# markers — which name no real model. Read back as None so a turn with no genuine
# backend/model is simply omitted from the by-backend / by-model breakdowns instead
# of forming an "unknown"/"<synthetic>" bucket. (The transcript parser now also avoids
# recording ``<synthetic>`` going forward; this keeps already-committed history clean.)
_METADATA_PLACEHOLDERS = {"unknown", "<synthetic>"}


def _real_metadata_label(value: str | None) -> str | None:
    """A backend/model metadata value, or None when it is a "no real value" placeholder."""
    if value is None:
        return None
    return None if value.strip().lower() in _METADATA_PLACEHOLDERS else value


# Hierarchy for the dashboard's token panel. Each base category's headline number is the
# main-agent count PLUS its sub-agent share; the sub-agent amount (and, for input, the
# cache-write amount) is a SUBSET of that headline, shown indented as "of which". This
# mirrors the metadata convention (commits/message.py): ``input`` already folds in
# cache-write (fresh input processed once into the cache), and sub-agent usage is recorded
# in its own ``subagent_*`` counters rather than added to the main ones. Each entry is
# ``(base_key, subagent_key, label, [(subset_label, (keys_to_sum, …)), …])``; a subset's
# value is the sum of its keys (so "cache write" totals main + sub-agent cache-write).
_TOKEN_CATEGORIES: list[tuple[str, str, str, list[tuple[str, tuple[str, ...]]]]] = [
    (
        "input",
        "subagent_input",
        "input",
        [
            ("cache write", ("cache_write", "subagent_cache_write")),
            ("sub-agents", ("subagent_input",)),
        ],
    ),
    ("output", "subagent_output", "output", [("sub-agents", ("subagent_output",))]),
    # reasoning sits with output (both are generated tokens): OpenCode reports it as its
    # own bucket; Claude folds it into output, so this row is simply absent for Claude.
    ("reasoning", "subagent_reasoning", "reasoning", [("sub-agents", ("subagent_reasoning",))]),
    ("cache_read", "subagent_cache_read", "cache read", [("sub-agents", ("subagent_cache_read",))]),
]


@dataclass
class Dashboard:
    repo: str
    branch: str  # the ref this dashboard was built for (the current branch by default)
    stats: list[CommitStat]  # oldest first
    sha_logins: dict[str, str] = field(default_factory=dict)  # commit SHA → GitHub login (best-effort)
    # Lowercased author email → GitHub login. A supplemental hint for commits `gh` can't
    # map because they aren't on the remote yet (e.g. fresh, unpushed session commits):
    # aGiTrack knows the current user's login, so their local commits still get an ID.
    email_logins: dict[str, str] = field(default_factory=dict)
    commit_base: str = ""  # GitHub commit URL prefix, or "" when no GitHub remote
    branches: list[str] = field(default_factory=list)  # every local branch, for the per-branch view selector

    # --- derived aggregates -------------------------------------------------

    @property
    def total_commits(self) -> int:
        return len(self.stats)

    def count(self, *kinds: str) -> int:
        return sum(1 for stat in self.stats if stat.kind in kinds)

    @property
    def tracked_commits(self) -> int:
        return self.count("agent", "covered", "agent-merge", "user", "agitrack-ops")

    @property
    def coverage(self) -> float:
        return self.tracked_commits / self.total_commits if self.total_commits else 0.0

    def lines_changed(self, *kinds: str) -> tuple[int, int]:
        ins = sum(stat.insertions for stat in self.stats if stat.kind in kinds)
        dels = sum(stat.deletions for stat in self.stats if stat.kind in kinds)
        return ins, dels

    @property
    def ai_lines(self) -> tuple[int, int]:
        # aGiTrack-tracked AI work: agent commits and the backend-made commits an
        # aGiTrack cover commit accounts for (#58). Agent-resolved merges are NOT
        # counted here — a merge's lines can't be cleanly attributed to the AI vs the
        # person who ran it, so they're left out of the line totals (the commits
        # still count toward coverage and the by-kind tally).
        return self.lines_changed("agent", "covered")

    @property
    def nontracked_lines(self) -> tuple[int, int]:
        # Everything aGiTrack did not track as AI — user commits and plain commits
        # alike. We do NOT call these "human": even a user-made commit can
        # contain lines an agent produced off the record, so the only honest
        # claim is that aGiTrack did not track them as AI.
        return self.lines_changed("user", "untracked")

    @property
    def token_totals(self) -> dict[str, int]:
        totals: dict[str, int] = defaultdict(int)
        for stat in self.stats:
            for key, value in stat.tokens.items():
                totals[key] += value
        return dict(totals)

    @property
    def token_breakdown(self) -> dict:
        """The token totals arranged as a hierarchy for the dashboard: each base category's
        headline is main-agent + sub-agent, with the sub-agent (and, for input, cache-write)
        amount as an indented "of which" subset. Summarizer usage — aGiTrack's own commit
        summary calls, not the agent's — is reported separately. JSON-serializable so the
        web dashboard can render the same structure the text one does (see _TOKEN_CATEGORIES)."""
        totals = self.token_totals

        def s(*keys: str) -> int:
            return sum(totals.get(key, 0) for key in keys)

        categories = []
        for base, subagent, label, subsets in _TOKEN_CATEGORIES:
            total = s(base, subagent)
            if total <= 0:
                continue
            children = [{"label": clabel, "value": value} for clabel, keys in subsets if (value := s(*keys)) > 0]
            categories.append({"label": label, "total": total, "subsets": children})
        summarizer = {
            key: totals.get(name, 0)
            for key, name in (
                ("input", "summary_input"),
                ("output", "summary_output"),
                ("cache_read", "summary_cache_read"),
            )
        }
        return {"categories": categories, "summarizer": {k: v for k, v in summarizer.items() if v > 0}}

    @property
    def lines_per_1k_output_tokens(self) -> float | None:
        output = self.token_totals.get("output", 0)
        if not output:
            return None
        ins, dels = self.ai_lines
        return (ins + dels) / output * 1000

    def group_by(self, label_fn) -> dict[str, dict[str, int]]:
        """Aggregate agent turns by a label (backend, model). A turn's lines
        are its own plus those of the backend-made commits it covers — the
        cover commit (#58) carries the tokens while the covered commits carry
        the diff, and both belong to the same backend/model."""
        by_sha = {stat.sha: stat for stat in self.stats}
        covered_lines: dict[str, tuple[int, int]] = {}
        for stat in self.stats:
            ins, dels = stat.insertions, stat.deletions
            for short in stat.covered_commits:
                covered = next((s for s in self.stats if s.sha.startswith(short)), None)
                if covered is not None and covered.sha != stat.sha:
                    ins += covered.insertions
                    dels += covered.deletions
            covered_lines[stat.sha] = (ins, dels)

        groups: dict[str, dict[str, int]] = {}
        for stat in self.stats:
            # A squash counts each original commit it contains under that
            # original's own model/backend (so the usage is split correctly),
            # while its single combined diff is attributed to the dominant one.
            if stat.constituents:
                ai_parts = [p for p in stat.constituents if p.kind in ("agent", "covered", "agent-merge")]
                for part in ai_parts:
                    label = label_fn(part)
                    if not label:
                        continue  # unknown backend/model (placeholder → None) — omit, don't bucket
                    bucket = groups.setdefault(label, defaultdict(int))
                    bucket["commits"] += 1
                    bucket["output_tokens"] += part.tokens.get("output", 0)
                    bucket["input_tokens"] += part.tokens.get("input", 0)
                dominant = max(ai_parts, key=lambda p: p.tokens.get("output", 0), default=None)
                dominant_label = label_fn(dominant) if dominant is not None else None
                if dominant_label:
                    bucket = groups.setdefault(dominant_label, defaultdict(int))
                    bucket["insertions"] += stat.insertions
                    bucket["deletions"] += stat.deletions
                continue
            if stat.kind != "agent":
                continue
            label = label_fn(by_sha[stat.sha])
            if not label:
                continue  # unknown backend/model (placeholder → None) — omit, don't bucket
            bucket = groups.setdefault(label, defaultdict(int))
            bucket["commits"] += 1
            ins, dels = covered_lines[stat.sha]
            bucket["insertions"] += ins
            bucket["deletions"] += dels
            bucket["output_tokens"] += stat.tokens.get("output", 0)
            bucket["input_tokens"] += stat.tokens.get("input", 0)
        return {label: dict(bucket) for label, bucket in groups.items()}

    @property
    def by_backend(self) -> dict[str, dict[str, int]]:
        return self.group_by(lambda stat: stat.backend)

    @property
    def by_model(self) -> dict[str, dict[str, int]]:
        return self.group_by(lambda stat: stat.model)

    @cached_property
    def committer_labels(self) -> dict[tuple[str, str], str]:
        """Map each ``(author name, email)`` to a merged committer label, so
        name variants of one person collapse to a single identity. Cached: it is
        read once per commit during serialization."""
        return resolve_committers(self.stats, self.sha_logins, self.email_logins)

    def label_of(self, stat: CommitStat) -> str:
        return self.committer_labels.get((stat.author or "", (stat.email or "").strip().lower())) or "unknown"

    def committers_of(self, stat: CommitStat) -> list[str]:
        """Every *human* merged committer label credited on a commit — its primary
        author first, then any co-authors — de-duplicated. A commit shows up when
        filtering on any of these (#54). The AI assistant and bot accounts (e.g.
        ``github-actions[bot]``) are excluded even when they are the primary
        author, so they never appear as committers; such a commit may have none."""
        labels = self.committer_labels
        out: list[str] = []
        if not _is_non_human_committer(stat.author or "", stat.email or ""):
            out.append(self.label_of(stat))
        for name, email in stat.co_authors:
            label = labels.get((name or "", (email or "").strip().lower()))
            if label and label not in out:
                out.append(label)
        return out

    @property
    def by_author(self) -> dict[str, dict[str, int]]:
        """Per committer, lines split into aGiTrack-tracked AI and non-tracked.
        Agent commits are git-authored by whoever ran aGiTrack but written by the
        model, so they count as that person's AI-driven lines; their user and
        plain commits are non-tracked (not claimed as human)."""
        labels = self.committer_labels
        groups: dict[str, dict[str, int]] = {}
        for stat in self.stats:
            # Bot/AI authors (e.g. github-actions[bot]) aren't committers — keep
            # them out of the per-committer breakdown, as for the filter list.
            if _is_non_human_committer(stat.author or "", stat.email or ""):
                continue
            label = labels.get((stat.author or "", (stat.email or "").strip().lower())) or "unknown"
            bucket = groups.setdefault(label, defaultdict(int))
            bucket["commits"] += 1
            if stat.kind != "untracked":
                bucket["agitrack_commits"] += 1
            if stat.kind in ("agent", "covered", "agent-merge"):
                bucket["ai_insertions"] += stat.insertions
                bucket["ai_deletions"] += stat.deletions
            elif stat.kind == "agitrack-ops":
                pass  # operational merges carry no diff; only the commit counts
            else:
                bucket["nontracked_insertions"] += stat.insertions
                bucket["nontracked_deletions"] += stat.deletions
        return {label: dict(bucket) for label, bucket in groups.items()}


# ---------------------------------------------------------------------------
# Committer identity: collapse name variants of the same person (#54)
# ---------------------------------------------------------------------------


def _login_of(email: str) -> str | None:
    match = _NOREPLY_RE.match(email.strip())
    return match.group(1).lower() if match else None


class _Union:
    """Tiny union-find over hashable nodes."""

    def __init__(self) -> None:
        self._parent: dict[object, object] = {}

    def find(self, node: object) -> object:
        self._parent.setdefault(node, node)
        root = node
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[node] != root:
            self._parent[node], node = root, self._parent[node]
        return root

    def union(self, a: object, b: object) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


def resolve_committers(
    stats: list[CommitStat],
    sha_logins: dict[str, str] | None = None,
    email_logins: dict[str, str] | None = None,
) -> dict[tuple[str, str], str]:
    """Map every ``(author name, email)`` pair to a single committer label.

    ``sha_logins`` (commit SHA → GitHub login, from :mod:`agitrack.metrics.github`)
    is the authoritative GitHub identity when available — every commit GitHub
    knows is merged under its real login. Without it (``gh`` missing, offline, no
    GitHub remote) we fall back to ``git log`` alone: identities merge when they
    share an email, a GitHub login parsed from a no-reply address, or an email
    local-part that equals a known login (linking ``user@host`` to
    ``user@users.noreply.github.com``). The label prefers the GitHub login;
    otherwise it is the person's most frequent name. Two people who never share a
    login or email but share a name cannot be told apart here and may merge."""
    sha_logins = sha_logins or {}
    email_logins = email_logins or {}
    # Flatten every contribution — each commit's primary author plus its human
    # co-authors — as (name, lowercased email, login-hint). Only the PRIMARY
    # author can borrow the commit's GitHub login (``sha_logins`` is keyed by sha
    # and maps to the author, not a co-author); a co-author resolves to a login
    # only through a no-reply address. This is what lets a co-authored commit be
    # filtered under each contributor (#54).
    contributions: list[tuple[str, str, str | None]] = []
    for stat in stats:
        email = (stat.email or "").strip().lower()
        primary_login = sha_logins.get(stat.sha) or _login_of(email) or email_logins.get(email)
        contributions.append((stat.author or "", email, primary_login))
        for name, co_email in stat.co_authors:
            ce = (co_email or "").strip().lower()
            contributions.append((name or "", ce, _login_of(ce) or email_logins.get(ce)))

    union = _Union()
    logins: set[str] = set()
    for name, email, login in contributions:
        node = ("pair", name, email)
        union.find(node)
        if email:
            union.union(node, ("email", email))
        if login:
            union.union(node, ("login", login.lower()))
            logins.add(login.lower())
    # Bridge a plain email to a login when its local-part is that login.
    for _name, email, _login in contributions:
        local = email.split("@", 1)[0]
        if email and local in logins:
            union.union(("email", email), ("login", local))

    root_logins: dict[object, set[str]] = defaultdict(set)
    root_names: dict[object, Counter[str]] = defaultdict(Counter)
    for name, email, login in contributions:
        root = union.find(("pair", name, email))
        if name:
            root_names[root][name] += 1
        if login:
            root_logins[root].add(login.lower())

    labels: dict[tuple[str, str], str] = {}
    for name, email, _login in contributions:
        root = union.find(("pair", name, email))
        if root_logins[root]:
            # GitHub login is the primary identity; append the person's first name
            # (from their git author name) when we have one, e.g. "octocat (Mona)".
            login = sorted(root_logins[root])[0]
            first = _first_name(root_names[root], login)
            label = f"{login} ({first})" if first else login
        elif root_names[root]:
            label = root_names[root].most_common(1)[0][0]
        else:
            label = email or "unknown"
        labels[(name, email)] = label
    return labels


def _first_name(names: Counter[str], login: str) -> str | None:
    """A committer's first name for display next to their GitHub login, taken from
    their git author name. Prefers a "First Last" style name when one exists (so
    ``Rana Waqas`` beats a bare ``waqas``); returns ``None`` when there is nothing
    more informative than the login itself."""
    if not names:
        return None
    # max() key: a name containing a space wins over one without; ties break on
    # frequency. So a real full name is preferred over a one-word handle.
    best = max(names, key=lambda n: (" " in n.strip(), names[n]))
    parts = best.strip().split()
    first = parts[0] if parts else ""
    if not first or first.lower() == login.lower():
        return None
    return first


def collect_commit_stats(repo: GitRepo, ref: str = "HEAD") -> list[CommitStat]:
    """Parse ``git log`` into :class:`CommitStat` records, oldest first."""
    # %x00/%x01 are git's own escapes: a literal NUL is not representable in
    # an argv string, but git happily PRINTS one as a record separator.
    log = repo._run(
        # The trailing ``--`` separates the revision from pathspecs: a ref whose name also
        # exists as a path (e.g. a ``dev`` branch alongside a tracked ``dev/`` directory) is
        # otherwise "ambiguous argument 'dev': both revision and filename" and git aborts with
        # NO output — the dashboard then showed an empty commit log for that branch.
        ["git", "log", "--format=%H%x01%an%x01%ae%x01%at%x01%B%x00", ref, "--"],
        check=False,
    ).stdout
    stats: list[CommitStat] = []
    for record in log.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record.strip():
            continue
        sha, _, rest = record.partition(_FIELD_SEP)
        author, _, rest = rest.partition(_FIELD_SEP)
        email, _, rest = rest.partition(_FIELD_SEP)
        committed_at, _, body = rest.partition(_FIELD_SEP)
        stats.append(_parse_commit(sha.strip(), author, email, committed_at.strip(), body))

    _neutralize_inherited_metadata(repo, ref, stats)

    _apply_numstat(repo, ref, {stat.sha: stat for stat in stats})

    # Backend-made commits have no metadata of their own; they are AI work if
    # some aGiTrack commit's covered_commits names them (#35/#58).
    covered_prefixes = [short for stat in stats for short in stat.covered_commits]
    for stat in stats:
        if stat.kind == "untracked" and any(stat.sha.startswith(prefix) for prefix in covered_prefixes):
            stat.kind = "covered"

    stats.reverse()  # oldest first
    _dedupe_squash_constituents(stats)
    return stats


def collect_manual_pending(repo: GitRepo) -> list[CommitStat]:
    """Manual-commit-mode latent turns not yet folded into a branch commit: the commits
    on ``refs/agitrack/manual/*`` that HEAD does not contain, parsed as :class:`CommitStat`
    and marked ``pending``. Oldest first, de-duplicated across refs. Line counts are fetched
    per commit (blobless-clone safe). Empty unless a manual-commit session is active."""
    try:
        refs = repo._run(
            ["git", "for-each-ref", "--format=%(refname)", "refs/agitrack/manual/"],
            check=False,
        ).stdout.split()
    except Exception:
        return []
    pending: list[CommitStat] = []
    seen: set[str] = set()
    for ref in refs:
        log = repo._run(
            ["git", "log", "--format=%H%x01%an%x01%ae%x01%at%x01%B%x00", f"HEAD..{ref}", "--"],
            check=False,
        ).stdout
        for record in log.split(_RECORD_SEP):
            record = record.strip("\n")
            if not record.strip():
                continue
            sha, _, rest = record.partition(_FIELD_SEP)
            author, _, rest = rest.partition(_FIELD_SEP)
            email, _, rest = rest.partition(_FIELD_SEP)
            committed_at, _, body = rest.partition(_FIELD_SEP)
            sha = sha.strip()
            if not sha or sha in seen:
                continue
            seen.add(sha)
            stat = _parse_commit(sha, author, email, committed_at.strip(), body)
            stat.pending = True
            pending.append(stat)
    if pending:
        apply_numstat_for(repo, [stat.sha for stat in pending], {stat.sha: stat for stat in pending})
    pending.sort(key=lambda stat: stat.timestamp)  # oldest first, matching branch stats
    return pending


def _dedupe_squash_constituents(stats: list[CommitStat]) -> None:
    """Count each original commit's tokens once, even when it lands in several squashes.

    Tokens are recorded per original commit, inside that commit's metadata block.
    Squashing copies the block verbatim, so if the same commit is rolled into more
    than one squash (e.g. a branch is squash-merged, then that result is squashed
    again), its block — and its tokens — appears in every such squash. Walking
    oldest first, the first squash to contain a commit keeps it; later squashes drop
    the repeat and have their token totals recomputed from the constituents that
    remain. Repeats *within* a single squash are left intact: two byte-identical
    blocks squashed together are genuine back-to-back turns, not one commit seen
    twice, so a squash's own constituents are marked seen only after it is filtered."""
    seen: set[str] = set()
    for stat in stats:
        if not stat.constituents:
            continue
        kept = [p for p in stat.constituents if not (p.metadata_block and p.metadata_block in seen)]
        seen.update(p.metadata_block for p in stat.constituents if p.metadata_block)
        if len(kept) != len(stat.constituents):
            stat.constituents = kept
            stat.tokens = _sum_tokens(kept)


def _neutralize_inherited_metadata(repo: GitRepo, ref: str, stats: list[CommitStat]) -> None:
    """Strip the aGiTrack metadata from commits that merely inherit a parent's block.

    When a session branch is integrated with a real merge commit (e.g. a GitHub
    PR merged with "Create a merge commit"), GitHub copies the PR title and body
    into the merge commit message. The cover commit (#58) at the branch tip is
    that PR's source, so the merge ends up carrying a byte-identical copy of the
    cover's metadata — same tokens, same trace. Counting both would double every
    figure for that turn (tokens, lines-via-covered_commits).

    The fingerprint is a *merge* commit (>1 parent) that shares an identical
    metadata block with one of its parents: that parent is the cover commit it
    merges, and the block was copied wholesale. Rather than drop the merge (which
    would make the dashboard show fewer commits than GitHub does), we keep it in
    the log but wipe the inherited metadata in place — tokens, covered_commits,
    trace, backend/model — and reclassify it as the plain merge it really is, so
    the turn's figures are still counted only once (on the cover parent).
    Restricting to merges is what separates this from a genuine run of repeated
    turns, which is linear — each such commit has a single parent, so an identical
    neighbouring block is a real (repeated) turn, not a copy. Cover commits and
    integration merges are themselves merge-shaped but never share a parent's
    block, so they are untouched."""
    block_by_sha = {stat.sha: stat.metadata_block for stat in stats if stat.metadata_block}
    if not block_by_sha:
        return
    parents = _parents(repo, ref)
    for stat in stats:
        inherited = (
            stat.metadata_block
            and len(parents.get(stat.sha, ())) > 1
            and any(block_by_sha.get(parent) == stat.metadata_block for parent in parents[stat.sha])
        )
        if not inherited:
            continue
        # Keep the commit (so the count matches GitHub) but scrub every field that
        # the copied block populated, so nothing about this turn is counted twice.
        stat.kind = _neutralized_merge_kind(stat.subject)
        stat.tokens = {}
        stat.covered_commits = []
        stat.constituents = []
        stat.started_at = stat.ended_at = ""
        stat.backend = stat.model = None
        stat.prompt = ""
        stat.user_prompts = []
        stat.metadata_block = ""


def _neutralized_merge_kind(subject: str) -> str:
    """The kind a merge should carry once its inherited aGiTrack metadata is stripped:
    aGiTrack's own integration plumbing if it names a session branch, else untracked —
    mirroring the metadata-less classification in :func:`_parse_commit`."""
    if subject.startswith("Merge ") and _AGITRACK_BRANCH_RE.search(subject):
        return "agitrack-ops"
    return "untracked"


def _parents(repo: GitRepo, ref: str) -> dict[str, list[str]]:
    # ``--`` disambiguates a ref that collides with a path name (see collect_commit_stats).
    output = repo._run(["git", "log", "--format=%H %P", ref, "--"], check=False).stdout
    parents: dict[str, list[str]] = {}
    for line in output.splitlines():
        sha, _, rest = line.strip().partition(" ")
        if sha:
            parents[sha] = rest.split()
    return parents


def _abbreviate_home(path: str) -> str:
    """Replace a leading home-directory prefix with ``~`` so the dashboard (and
    its public screenshot) never leaks an absolute home path."""
    try:
        home = Path.home()
        rel = Path(path).relative_to(home)
    except (ValueError, RuntimeError, OSError):
        return path
    return "~" if str(rel) == "." else f"~/{rel.as_posix()}"


def build_dashboard(
    repo: GitRepo,
    ref: str = "HEAD",
    *,
    sha_logins: dict[str, str] | None = None,
    email_logins: dict[str, str] | None = None,
) -> Dashboard:
    from agitrack.metrics.github import commit_url_base

    stats = collect_commit_stats(repo, ref)
    # Manual-commit mode: surface this session's not-yet-committed latent turns as pending
    # entries (only when showing HEAD — the live working branch — not a historical ref).
    if ref == "HEAD":
        stats = stats + collect_manual_pending(repo)
    # The branch the dashboard *shows*: the explicit ref when one is requested,
    # otherwise whatever HEAD currently points at.
    branch = repo.current_branch() if ref == "HEAD" else ref
    # Every local branch feeds the per-branch view selector. Keep the shown branch
    # in the list (and first) even if it's detached/HEAD so it's always selectable.
    branches = repo.list_branches()
    if branch and branch != "HEAD":
        branches = [branch, *(b for b in branches if b != branch)]
    return Dashboard(
        repo=_abbreviate_home(str(repo.repo)),
        branch=branch,
        stats=stats,
        sha_logins=sha_logins or {},
        email_logins=email_logins or {},
        commit_base=commit_url_base(repo),
        branches=branches,
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_commit(sha: str, author: str, email: str, committed_at: str, body: str) -> CommitStat:
    body = _normalize_legacy_markers(body)
    subject = body.splitlines()[0] if body.splitlines() else ""
    timestamp = int(committed_at) if committed_at.isdigit() else 0
    co_authors = _parse_co_authors(body)
    # A clean aGiTrack turn carries exactly one metadata block. More than one means
    # the message is an aggregate — a squash or PR merge that concatenated
    # several commits' blocks (possibly across several rounds of squashing, which
    # git flattens). Parse each original commit so its tokens and model/backend
    # usage are still counted, and so the squash can be expanded in the UI.
    if body.count(METADATA_HEADER) > 1:
        constituents = _parse_constituents(body)
        return _build_squash(sha, author, email, subject, timestamp, body, constituents, co_authors)
    metadata = _parse_metadata(body)
    commit_type = metadata.get("commit_type")
    if commit_type == "agent":
        kind = "agent"
    elif commit_type == "agent-merge":
        kind = "agent-merge"
    elif commit_type == "user":
        kind = "user"
    elif METADATA_HEADER in body:
        kind = "agent"  # metadata without commit_type: treat as agent work
    elif subject.startswith("Merge ") and _AGITRACK_BRANCH_RE.search(subject):
        # aGiTrack's own integration plumbing: the auto-generated merge commits it
        # makes to bring base into a session turn branch (or back), e.g. "Merge
        # branch 'dev' into agit/claude/session-1/t2". These carry no metadata
        # but are aGiTrack operations, not stray human/untracked work, so they get
        # their own class (and merges contribute no numstat lines anyway).
        kind = "agitrack-ops"
    else:
        kind = "untracked"
    return CommitStat(
        sha=sha,
        author=author,
        email=email,
        subject=subject,
        kind=kind,
        timestamp=timestamp,
        started_at=metadata.get("agent_started_at", ""),
        ended_at=metadata.get("agent_ended_at", ""),
        backend=_real_metadata_label(metadata.get("backend")),
        model=_real_metadata_label(metadata.get("model")),
        tokens=_parse_tokens(metadata),
        covered_commits=(metadata.get("covered_commits") or "").split(),
        co_authors=co_authors,
        prompt=_extract_prompt(body, subject, kind),
        user_prompts=_extract_user_prompts(body),
        metadata_block=_metadata_block(body),
        message=body.strip(),
    )


def _parse_tokens(metadata: dict[str, str]) -> dict[str, int]:
    tokens: dict[str, int] = {}
    for key, value in metadata.items():
        if key.startswith(_TOKEN_KEY_PREFIX) and value.isdigit():
            tokens[key.removeprefix(_TOKEN_KEY_PREFIX)] = int(value)
        elif key in ("summary_tokens_input", "summary_tokens_output") and value.isdigit():
            tokens["summary_" + key.removeprefix("summary_tokens_")] = int(value)
    return tokens


def _kind_from_metadata(metadata: dict[str, str]) -> str:
    commit_type = metadata.get("commit_type")
    if commit_type in ("agent", "agent-merge", "user"):
        return commit_type
    return "agent"  # a metadata block without a commit_type is agent work


def _is_metadata_kv(line: str) -> bool:
    """A `key: value` metadata line (key has no spaces and isn't a `#` header)."""
    if line.startswith("#") or ":" not in line:
        return False
    key = line.split(":", 1)[0].strip()
    return bool(key) and " " not in key


def _parse_constituents(body: str) -> list[CommitStat]:
    """Split a squashed message into one :class:`CommitStat` per original commit.

    git concatenates the squashed commits' messages, so each ``# aGiTrack Metadata``
    block marks one original aGiTrack commit. A constituent spans from the end of the
    previous block to the end of its own block (its subject is the nearest ``*``
    bullet, the format GitHub's squash uses). Constituents carry the original's
    kind/backend/model/tokens/span but no line counts — the squash holds the one
    combined diff."""
    lines = body.splitlines()
    headers = [i for i, line in enumerate(lines) if line.strip() == METADATA_HEADER]
    constituents: list[CommitStat] = []
    prev_end = 0
    for header in headers:
        end = len(lines)
        for j in range(header + 1, len(lines)):
            if not _is_metadata_kv(lines[j]):
                end = j
                break
        segment = lines[prev_end:end]
        constituents.append(_constituent(segment))
        prev_end = end
    return constituents


def _constituent(segment_lines: list[str]) -> CommitStat:
    text = "\n".join(segment_lines).strip()
    metadata = _parse_metadata(text)
    subject = next(
        (line[2:].strip() for line in segment_lines if line.startswith("* ")),
        next((line for line in segment_lines if line.strip() and not line.startswith("#")), ""),
    )
    return CommitStat(
        sha="",
        author="",
        email="",
        subject=subject,
        kind=_kind_from_metadata(metadata),
        started_at=metadata.get("agent_started_at", ""),
        ended_at=metadata.get("agent_ended_at", ""),
        backend=_real_metadata_label(metadata.get("backend")),
        model=_real_metadata_label(metadata.get("model")),
        tokens=_parse_tokens(metadata),
        # The metadata block identifies the original commit this constituent was
        # parsed from: if the same commit is captured in more than one squash, the
        # block is byte-identical, which lets us count its tokens only once.
        metadata_block=_metadata_block(text),
        message=text,
    )


def _build_squash(
    sha: str,
    author: str,
    email: str,
    subject: str,
    timestamp: int,
    body: str,
    constituents: list[CommitStat],
    co_authors: list[tuple[str, str]] | None = None,
) -> CommitStat:
    """The parent stat for a squash: it keeps the combined diff and carries the
    summed tokens of its constituents, classified by what they contain (any AI
    constituent ⇒ aGiTrack-tracked AI). Its backend/model is the dominant agent
    constituent's, for line attribution and display."""
    tokens = _sum_tokens(constituents)
    ai_parts = [part for part in constituents if part.kind in ("agent", "covered", "agent-merge")]
    if ai_parts:
        kind = "agent"
    elif any(part.kind == "user" for part in constituents):
        kind = "user"
    else:
        kind = "untracked"
    dominant = max(ai_parts, key=lambda part: part.tokens.get("output", 0), default=None)
    return CommitStat(
        sha=sha,
        author=author,
        email=email,
        subject=subject,
        kind=kind,
        timestamp=timestamp,
        backend=dominant.backend if dominant else None,
        model=dominant.model if dominant else None,
        tokens=dict(tokens),
        co_authors=co_authors or [],
        message=body.strip(),
        constituents=constituents,
    )


def _sum_tokens(parts: list[CommitStat]) -> dict[str, int]:
    tokens: dict[str, int] = defaultdict(int)
    for part in parts:
        for key, value in part.tokens.items():
            tokens[key] += value
    return dict(tokens)


def _metadata_block(body: str) -> str:
    """The `# aGiTrack Metadata` section verbatim (it is always the last section).

    A GitHub PR merge commit copies its PR's title and body — and so the whole
    metadata block — into the merge commit message, byte for byte. Capturing the
    block lets :func:`collect_commit_stats` recognise such a merge as a duplicate
    of the cover commit it merges and avoid counting the turn's tokens twice."""
    lines = body.splitlines()
    try:
        start = lines.index(METADATA_HEADER)
    except ValueError:
        return ""
    return "\n".join(lines[start:]).strip()


def _parse_metadata(body: str) -> dict[str, str]:
    lines = body.splitlines()
    try:
        start = lines.index(METADATA_HEADER)
    except ValueError:
        return {}
    metadata: dict[str, str] = {}
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() and " " not in key.strip():
            metadata[key.strip()] = value.strip()
    return metadata


def _extract_prompt(body: str, subject: str, kind: str) -> str:
    if kind != "agent":
        return ""
    # The turn's prompts live in the interaction trace's "## User" sections. When a
    # summary leads the message (#8) the subject is the summary, not the prompt, so
    # the trace is the reliable source; fall back to the subject for a prompt-led
    # message with no trace.
    user_prompts = _extract_user_prompts(body)
    if user_prompts:
        return " ".join(user_prompts)
    return subject.removeprefix("<aGiTrack> ").strip()


def _extract_user_prompts(body: str) -> list[str]:
    if "# Interaction Trace" not in body:
        return []
    prompts: list[str] = []
    lines = body.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].strip() == "## User":
            index += 1
            entry: list[str] = []
            while index < len(lines) and not lines[index].startswith(("## ", "# ")):
                entry.append(lines[index].strip())
                index += 1
            text = " ".join(part for part in entry if part)
            if text:
                prompts.append(text)
        else:
            index += 1
    return prompts


def _apply_numstat(repo: GitRepo, ref: str, by_sha: dict[str, CommitStat]) -> None:
    # Line counts for the WHOLE history, computed from the LOCAL blobs only
    # (allow_lazy_fetch=False). On a blobless partial clone (`git clone --filter=blob:none`)
    # diffing every commit would otherwise lazily fetch every historical blob from the
    # promisor remote — tens of seconds per dashboard poll, and the interrupted fetches
    # litter `.git/objects/pack` with `tmp_pack_*` files — so the dashboard appears to hang
    # with no commits. Counting from local blobs keeps the poll instant; the commits the user
    # is actually viewing get their exact counts via apply_numstat_for (which fetches just
    # that page). Merge commits (cover commits #58, integration merges) report no numstat by
    # default, so a turn's lines are counted exactly once — on the commits that introduced them.
    # ``--`` disambiguates a ref that collides with a path name (see collect_commit_stats).
    output = repo._run(
        ["git", "log", "--numstat", "--format=%x01%H", ref, "--"], check=False, allow_lazy_fetch=False
    ).stdout
    _accumulate_numstat(output, by_sha)


def apply_numstat_for(repo: GitRepo, shas: list[str], by_sha: dict[str, CommitStat]) -> None:
    """Recompute insertions/deletions for SPECIFIC commits, fetching only those commits'
    blobs (allow_lazy_fetch stays on). This is how the dashboard gets exact line counts for
    the log page it is about to show without pulling the rest of a blobless clone's history:
    only what is displayed is fetched. A commit whose blobs still can't be reached (offline,
    or an aGiTrack-only ref the remote doesn't have) keeps the local-blob count it already
    had, rather than being zeroed."""
    targets = {sha for sha in shas if sha and sha in by_sha}
    if not targets:
        return
    # `--no-walk` shows exactly the named commits (each diffed against its parent for numstat)
    # without traversing ancestry, so the fetch is bounded to this page.
    output = repo._run(
        ["git", "log", "--no-walk=unsorted", "--numstat", "--format=%x01%H", *sorted(targets), "--"],
        check=False,
    ).stdout
    fresh: dict[str, CommitStat] = {
        sha: CommitStat(sha=sha, author="", email="", subject="", kind="") for sha in targets
    }
    seen = _accumulate_numstat(output, fresh)
    for sha in seen:
        by_sha[sha].insertions = fresh[sha].insertions
        by_sha[sha].deletions = fresh[sha].deletions


def _accumulate_numstat(output: str, by_sha: dict[str, CommitStat]) -> set[str]:
    """Parse ``git log --numstat`` output, adding each commit's line counts onto its
    :class:`CommitStat`. Returns the set of SHAs that had at least one numstat row (so a
    caller can tell a genuinely-counted commit from one git emitted no diff for)."""
    current: CommitStat | None = None
    current_sha = ""
    seen: set[str] = set()
    for line in output.splitlines():
        if line.startswith(_FIELD_SEP):
            current_sha = line[1:].strip()
            current = by_sha.get(current_sha)
            continue
        match = _NUMSTAT_RE.match(line)
        if match and current is not None:
            seen.add(current_sha)
            if match.group(1) != "-":
                current.insertions += int(match.group(1))
            if match.group(2) != "-":
                current.deletions += int(match.group(2))
    return seen
