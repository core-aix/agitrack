"""``agitrack redact``: take interaction traces back OUT of commit messages.

A commit message is the one place aGiTrack writes a conversation down, and it is published the
moment the branch is pushed. People paste things into an agent they would never put in a commit:
a password typed into the wrong window, a message meant for someone else, a customer's details.
Once aGiTrack has folded that into history, deleting it from the transcript does nothing — so
this command rewrites the affected commits with the trace replaced by a short note.

What is selected
    ``--commit <rev>`` (repeatable) removes every turn's trace from that commit. ``--since`` /
    ``--until`` remove the trace of every TURN whose recorded conversation span
    (``agent_started_at`` .. ``agent_ended_at``) overlaps the window; a turn with no span is
    placed by its commit's date. A manual-mode commit folds several turns into one message, and
    only the turns inside the window are touched there.

What changes
    Only commit MESSAGES. Every rewritten commit keeps its tree, author, committer and dates, so
    no file in any checkout changes and the working tree, index and HEAD stay as they are. The
    commits after a rewritten one get new ids too (their parents changed), which is what any
    history rewrite costs. For a redacted turn the trace becomes a one-line note, and an
    aGiTrack-written subject/summary — which was produced FROM that trace and routinely quotes the
    prompt verbatim — becomes ``<aGiTrack> (interaction trace removed)`` unless
    ``--keep-summary``. The metadata block (tokens, model, timestamps) stays, gaining a
    ``trace_removed:`` line, so the dashboard's numbers do not change. A user's own subject is
    never touched: they wrote it.

What else it reaches
    Every local branch and every pending latent turn (``refs/agitrack/manual/*``) containing a
    rewritten commit; aGiTrack's notes on those commits (the stored summary is dropped for a
    redacted commit); the tracker's coverage watermark; the not-yet-committed trace in
    ``state.json``. A window is also REMEMBERED (``.agitrack/redactions.json``), so a turn from
    it that has not been committed yet never reaches a commit message later.

What it cannot reach, and says so
    A remote that already has the commits (force-push, and the old text stays in the remote's
    history, pull requests and other people's clones until then), tags, and the local reflog
    (``--purge`` expires it and prunes the old objects).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agitrack.commits.message import AGITRACK_SUBJECT_PREFIX, TRACE_HEADER
from agitrack.fileio import atomic_write_text, read_json_object
from agitrack.git import GitRepo, RepoLock
from agitrack.proc import console_isolation_kwargs

# The metadata line a redacted block gains. Its presence is also how a second run recognises a
# turn that is already redacted.
REMOVED_KEY = "trace_removed"
REMOVED_SUBJECT = f"{AGITRACK_SUBJECT_PREFIX}(interaction trace removed)"
_REDACTIONS_NAME = "redactions.json"
_SUMMARY_NOTES = "refs/notes/agitrack/commit-summary"


# ---------------------------------------------------------------------------
# Time windows
# ---------------------------------------------------------------------------


def parse_when(text: str, *, end: bool = False) -> float:
    """Epoch seconds for a ``--since`` / ``--until`` value.

    Accepts an ISO date or date-time (``2026-10-06``, ``2026-10-06 14:30``,
    ``2026-10-06T14:30:00Z``), read in LOCAL time unless it names a zone, plus ``now`` and a
    relative ``<n>m`` / ``<n>h`` / ``<n>d`` ago. A bare DATE given as the end of a window means
    the whole of that day, which is what someone typing ``--until 2026-10-06`` means."""
    value = text.strip()
    if not value:
        raise ValueError("empty time")
    if value.lower() == "now":
        return time.time()
    relative = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([mhd])(?:\s+ago)?", value.lower())
    if relative:
        amount, unit = float(relative.group(1)), relative.group(2)
        return time.time() - amount * {"m": 60, "h": 3600, "d": 86400}[unit]
    date_only = re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"cannot read {text!r} as a time; use e.g. 2026-10-06, '2026-10-06 14:30', or 3h") from error
    if date_only and end:
        parsed = parsed + timedelta(days=1)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # naive means the user's own clock
    return parsed.timestamp()


def _iso_to_epoch(value: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _redactions_path(repo_root: Path) -> Path:
    from agitrack.tracking_gap import base_root

    return base_root(repo_root) / ".agitrack" / _REDACTIONS_NAME


def redacted_windows(repo_root: Path) -> list[tuple[float, float]]:
    """The windows the user removed traces for. A turn that BEGAN inside one never reaches a
    commit message, even if it is committed after the redaction ran."""
    try:
        record = read_json_object(_redactions_path(repo_root))
    except Exception:
        return []
    windows = []
    for item in record.get("windows") or []:
        try:
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end >= start:
            windows.append((start, end))
    return windows


def remember_window(repo_root: Path, start: float, end: float) -> None:
    path = _redactions_path(repo_root)
    record = {}
    try:
        record = read_json_object(path)
    except Exception:
        record = {}
    windows = list(record.get("windows") or [])
    windows.append({"start": start, "end": end, "recorded_at": time.time()})
    record["windows"] = windows
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(record, indent=2) + "\n")


def in_windows(stamp: float | None, windows: list[tuple[float, float]]) -> bool:
    if stamp is None or not windows:
        return False
    stamp = stamp / 1000 if stamp > 1e11 else stamp  # tolerate millisecond stamps
    return any(start <= stamp <= end for start, end in windows)


# ---------------------------------------------------------------------------
# Rewriting one message
# ---------------------------------------------------------------------------


@dataclass
class _Block:
    lead_start: int  # first line of this turn's own lead (its <aGiTrack> subject/summary)
    trace: int | None  # the "# Interaction Trace" line, or None when the block has no trace
    header: int  # the "# aGiTrack Metadata" line
    end: int  # first line after the block's key: value lines
    meta: dict[str, str] = field(default_factory=dict)


def _blocks(lines: list[str]) -> list[_Block]:
    """The turns recorded in a message, split on its REAL metadata headers (fence-aware, the same
    boundaries the dashboard reads; a block quoted inside a trace is not a turn)."""
    from agitrack.metrics.collect import _is_metadata_kv, metadata_header_lines

    blocks: list[_Block] = []
    previous_end = 0
    for header in metadata_header_lines(lines):
        if header < previous_end:
            continue
        end = len(lines)
        for index in range(header + 1, len(lines)):
            if not _is_metadata_kv(lines[index]):
                end = index
                break
        meta: dict[str, str] = {}
        for line in lines[header + 1 : end]:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
        trace = None
        in_fence = False
        for index in range(previous_end, header):
            stripped = lines[index].strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
                continue
            if not in_fence and stripped == TRACE_HEADER:
                trace = index
                break
        blocks.append(_Block(previous_end, trace, header, end, meta))
        previous_end = end
    return blocks


def _span(meta: dict[str, str]) -> tuple[float, float] | None:
    start = _iso_to_epoch(meta.get("agent_started_at", ""))
    end = _iso_to_epoch(meta.get("agent_ended_at", ""))
    if start is None and end is None:
        return None
    return (start if start is not None else end, end if end is not None else start)  # type: ignore[return-value]


def _is_agitrack_lead(lines: list[str]) -> int | None:
    """Index (within *lines*) of an aGiTrack-written subject line leading this turn, or None.
    GitHub's squash writes each commit as a ``* subject`` bullet, which counts too."""
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        bare = stripped[2:] if stripped.startswith("* ") else stripped
        return index if bare.startswith(AGITRACK_SUBJECT_PREFIX.strip()) else None
    return None


def redact_message(
    message: str,
    *,
    window: tuple[float, float] | None,
    committed_at: float | None,
    keep_summary: bool = False,
    today: str | None = None,
) -> tuple[str, int]:
    """*message* with the selected turns' traces removed, and how many turns were redacted.

    ``window`` None selects every turn in the message (``--commit``); otherwise a turn is
    selected when its span overlaps the window, or — with no span recorded — when the commit
    itself falls inside it. A turn already redacted is left alone, so the command is idempotent.
    """
    lines = message.split("\n")
    blocks = _blocks(lines)
    today = today or datetime.now().astimezone().date().isoformat()
    out: list[str] = []
    cursor = 0
    redacted = 0
    for block in blocks:
        if block.trace is None or REMOVED_KEY in block.meta:
            continue
        if window is not None:
            span = _span(block.meta)
            if span is None:
                if committed_at is None or not (window[0] <= committed_at <= window[1]):
                    continue
            elif span[1] < window[0] or span[0] > window[1]:
                continue
        lead = lines[block.lead_start : block.trace]
        out.extend(lines[cursor : block.lead_start])
        subject_at = _is_agitrack_lead(lead)
        if subject_at is not None and not keep_summary:
            # The subject and summary were written FROM the trace (the subject is often the
            # prompt itself), so they go with it.
            bullet = "* " if lead[subject_at].lstrip().startswith("* ") else ""
            out.extend(lead[:subject_at])
            out.extend([f"{bullet}{REMOVED_SUBJECT}", ""])
        else:
            out.extend(lead)
        out.extend(
            [
                TRACE_HEADER,
                "",
                f"> The interaction trace of this turn was removed with `agitrack redact` on {today}.",
                "",
            ]
        )
        out.extend(lines[block.header : block.end])
        out.append(f"{REMOVED_KEY}: {today}")
        cursor = block.end
        redacted += 1
    if not redacted:
        return message, 0
    out.extend(lines[cursor:])
    return "\n".join(out), redacted


# ---------------------------------------------------------------------------
# Rewriting history
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    targets: dict[str, str]  # sha -> new message
    turns: int
    refs: list[str]  # refs whose history will be rewritten
    tags: list[str] = field(default_factory=list)  # tags still holding an original (not rewritten)
    remotes: list[str] = field(default_factory=list)  # remote branches that already have one
    window: tuple[float, float] | None = None


def _git(repo: GitRepo, *args: str, input_text: str | None = None, check: bool = True) -> str:
    return repo._run(["git", *args], input_text=input_text, check=check).stdout


def _candidate_refs(repo: GitRepo) -> list[str]:
    out = _git(repo, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/agitrack/manual")
    return [line for line in out.splitlines() if line]


def plan_redaction(
    repo: GitRepo,
    *,
    commits: list[str] | None = None,
    window: tuple[float, float] | None = None,
    keep_summary: bool = False,
) -> Plan:
    refs = _candidate_refs(repo)
    targets: dict[str, str] = {}
    turns = 0
    if commits:
        for rev in commits:
            sha = _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False).strip()
            if not sha:
                raise ValueError(f"not a commit in this repository: {rev}")
            message = _raw_message(repo, sha)
            new, count = redact_message(message, window=None, committed_at=None, keep_summary=keep_summary)
            if count:
                targets[sha] = new
                turns += count
    if window is not None and refs:
        log = _git(repo, "log", "--format=%H%x00%ct%x01", *refs)
        for record in log.split("\x01"):
            record = record.strip()
            if not record:
                continue
            sha, _, stamp = record.partition("\x00")
            if sha in targets:
                continue
            message = _raw_message(repo, sha)
            if "# Interaction Trace" not in message:
                continue
            new, count = redact_message(
                message, window=window, committed_at=float(stamp or 0), keep_summary=keep_summary
            )
            if count:
                targets[sha] = new
                turns += count
    plan = Plan(targets=targets, turns=turns, refs=[], window=window)
    if not targets:
        return plan
    plan.refs = [ref for ref in refs if _contains_any(repo, ref, targets)]
    for kind, pattern in (("tags", "refs/tags"), ("remotes", "refs/remotes")):
        held: set[str] = set()
        for sha in list(targets)[:200]:
            out = _git(repo, "for-each-ref", "--format=%(refname:short)", f"--contains={sha}", pattern, check=False)
            held.update(line for line in out.splitlines() if line and not line.endswith("/HEAD"))
        setattr(plan, kind, sorted(held))
    return plan


def _contains_any(repo: GitRepo, ref: str, targets: dict[str, str]) -> bool:
    out = _git(repo, "rev-list", ref, check=False)
    reachable = set(out.split())
    return any(sha in reachable for sha in targets)


def _cat_commit(repo: GitRepo, sha: str) -> str:
    """A commit object's exact text. Read as BYTES: the text pipe would translate line endings
    on Windows, and the object is written back byte for byte (see :func:`_write_commit`)."""
    raw = subprocess.run(
        ["git", "-C", str(repo.repo), "cat-file", "commit", sha],
        capture_output=True,
        check=True,
        **console_isolation_kwargs(),
    ).stdout
    return raw.decode("utf-8", errors="surrogateescape")


def _write_commit(repo: GitRepo, text: str) -> str:
    """Store *text* as a commit object and return its id. Written as BYTES: through a text pipe
    Windows turns every ``\n`` into ``\r\n``, and git rejects the object ("badTreeSha1:
    invalid 'tree' line format") — which is exactly how Windows CI failed."""
    out = subprocess.run(
        ["git", "-C", str(repo.repo), "hash-object", "-t", "commit", "-w", "--stdin"],
        input=text.encode("utf-8", errors="surrogateescape"),
        capture_output=True,
        check=True,
        **console_isolation_kwargs(detach_stdin=False),
    ).stdout
    return out.decode("ascii").strip()


def _raw_message(repo: GitRepo, sha: str) -> str:
    raw = _cat_commit(repo, sha)
    return raw.split("\n\n", 1)[1] if "\n\n" in raw else ""


def _rewritten_object(raw: str, parents: list[str], message: str | None) -> str:
    """A commit object's text with new parents (and message), signatures dropped: a signature
    over the OLD content would no longer verify, and git would report it as bad rather than
    absent."""
    header, _, old_message = raw.partition("\n\n")
    kept: list[str] = []
    skipping = False
    parents_written = False
    for line in header.split("\n"):
        if skipping and line.startswith(" "):
            continue
        skipping = False
        if line.startswith("gpgsig ") or line.startswith("gpgsig-sha256 ") or line.startswith("mergetag "):
            skipping = True
            continue
        if line.startswith("parent "):
            if not parents_written:
                kept.extend(f"parent {parent}" for parent in parents)
                parents_written = True
            continue
        if line.startswith("author ") and not parents_written:
            kept.extend(f"parent {parent}" for parent in parents)
            parents_written = True
        kept.append(line)
    return "\n".join(kept) + "\n\n" + (old_message if message is None else message)


def apply_redaction(repo: GitRepo, plan: Plan, *, keep_summary: bool = False) -> dict[str, str]:
    """Rewrite every ref in the plan. Returns the old-sha -> new-sha mapping."""
    if not plan.targets or not plan.refs:
        return {}
    tips = {ref: _git(repo, "rev-parse", ref).strip() for ref in plan.refs}
    order = _git(repo, "rev-list", "--reverse", "--topo-order", "--parents", *tips.values())
    mapping: dict[str, str] = {}
    for line in order.splitlines():
        sha, *parents = line.split()
        new_parents = [mapping.get(parent, parent) for parent in parents]
        if sha not in plan.targets and new_parents == parents:
            continue
        raw = _cat_commit(repo, sha)
        obj = _rewritten_object(raw, new_parents, plan.targets.get(sha))
        mapping[sha] = _write_commit(repo, obj)
    for ref, old in tips.items():
        new = mapping.get(old)
        if new:
            _git(repo, "update-ref", "-m", "agitrack redact", ref, new, old)
    head = _git(repo, "rev-parse", "HEAD", check=False).strip()
    detached = not _git(repo, "symbolic-ref", "-q", "HEAD", check=False).strip()
    if detached and head in mapping:
        _git(repo, "update-ref", "--no-deref", "-m", "agitrack redact", "HEAD", mapping[head], head)
    _carry_notes(repo, mapping, plan, keep_summary=keep_summary)
    _remap_state(repo, mapping)
    return mapping


def _carry_notes(repo: GitRepo, mapping: dict[str, str], plan: Plan, *, keep_summary: bool) -> None:
    """Notes are keyed by commit id, so a rewritten commit would lose them. They follow the new
    id — except a redacted commit's stored SUMMARY, which was written from the removed trace."""
    notes_refs = [r for r in _git(repo, "for-each-ref", "--format=%(refname)", "refs/notes").splitlines() if r]
    for notes_ref in notes_refs:
        listed = _git(repo, "notes", "--ref", notes_ref, "list", check=False)
        annotated = {line.split()[1] for line in listed.splitlines() if len(line.split()) == 2}
        for old, new in mapping.items():
            if old not in annotated:
                continue
            drop = old in plan.targets and notes_ref == _SUMMARY_NOTES and not keep_summary
            if not drop:
                _git(repo, "notes", "--ref", notes_ref, "copy", "-f", old, new, check=False)
            if old in plan.targets:
                _git(repo, "notes", "--ref", notes_ref, "remove", "--ignore-missing", old, check=False)


def _remap_state(repo: GitRepo, mapping: dict[str, str]) -> None:
    """aGiTrack's own records of commit ids, so a tracker resumes on the rewritten history."""
    agit = repo.repo / ".agitrack"
    watermark = agit / "tracked-head"
    try:
        old = watermark.read_text(encoding="utf-8").strip()
    except OSError:
        old = ""
    if old in mapping:
        atomic_write_text(watermark, mapping[old] + "\n")


def scrub_pending_trace(repo: GitRepo, window: tuple[float, float] | None) -> int:
    """Drop not-yet-committed trace entries from inside the window (``state.json``)."""
    if window is None:
        return 0
    path = repo.repo / ".agitrack" / "state.json"
    try:
        state = read_json_object(path)
    except Exception:
        return 0
    pending = state.get("pending_trace") or []
    kept = [item for item in pending if not in_windows(_stamp(item.get("at")), [window])]
    if len(kept) == len(pending):
        return 0
    state["pending_trace"] = kept
    atomic_write_text(path, json.dumps(state, indent=2) + "\n")
    return len(pending) - len(kept)


def _stamp(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def purge_old_objects(repo: GitRepo) -> None:
    """Expire the reflog and prune unreachable objects, so the original messages are gone from
    this clone too rather than one ``git reflog`` away."""
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "--prune=now", "--quiet")


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def run(
    repo: GitRepo,
    *,
    commits: list[str] | None,
    since: str | None,
    until: str | None,
    keep_summary: bool = False,
    purge: bool = False,
    assume_yes: bool = False,
    dry_run: bool = False,
) -> int:
    """``agitrack redact``. Returns a process exit code."""
    if not commits and since is None and until is None:
        print(
            "Say what to remove: `agitrack redact --commit <sha>` (repeatable), or a time window "
            "with `--since <time>` and/or `--until <time>` (e.g. --since '2026-10-06 14:00' --until 3h)."
        )
        return 2
    window = None
    if since is not None or until is not None:
        try:
            start = parse_when(since) if since is not None else 0.0
            end = parse_when(until, end=True) if until is not None else time.time()
        except ValueError as error:
            print(f"aGiTrack: {error}")
            return 2
        if end < start:
            print("aGiTrack: --until is before --since.")
            return 2
        window = (start, end)
    try:
        plan = plan_redaction(repo, commits=commits, window=window, keep_summary=keep_summary)
    except (ValueError, subprocess.CalledProcessError) as error:
        print(f"aGiTrack: {error}")
        return 1

    if not plan.targets:
        print("No recorded interaction trace matches; nothing in history to remove.")
        if window is not None and not dry_run:
            remember_window(repo.repo, *window)
            dropped = scrub_pending_trace(repo, window)
            print(
                "The window is remembered: turns from it that are not committed yet will never "
                "reach a commit message."
                + (f" Removed {dropped} pending trace entr{'y' if dropped == 1 else 'ies'}." if dropped else "")
            )
        return 0

    _describe(repo, plan)
    if dry_run:
        print("\nDry run: nothing was changed.")
        return 0
    if not assume_yes:
        if not sys.stdin.isatty():
            print("\nRe-run with --yes to rewrite history non-interactively.")
            return 1
        try:
            answer = input("\nRewrite these commit messages? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in {"y", "yes"}:
            print("Nothing was changed.")
            return 1

    with _exclusive(repo) as acquired:
        if not acquired:
            return 1
        # Planned again under the lock: a tracker stopped to take it records its final turn on
        # the way out, which can move a branch tip after the plan the user confirmed was made.
        plan = plan_redaction(repo, commits=commits, window=window, keep_summary=keep_summary)
        mapping = apply_redaction(repo, plan, keep_summary=keep_summary)
        if window is not None:
            remember_window(repo.repo, *window)
            scrub_pending_trace(repo, window)
        _rerender_pending_trailer(repo)
        # Reported before the lock is released (and a stopped tracker restarted), so the
        # outcome reads before the tracker's own start-up lines rather than after them.
        _report(repo, plan, mapping, purge=purge)
    return 0


def _report(repo: GitRepo, plan: Plan, mapping: dict[str, str], *, purge: bool) -> None:
    print(
        f"\nRemoved the interaction trace of {plan.turns} turn(s) across {len(plan.targets)} commit(s); "
        f"{len(mapping)} commit(s) rewritten."
    )
    if plan.remotes:
        print(
            "These commits are already on a remote. Force-push the rewritten branch(es) "
            "(e.g. `git push --force-with-lease`); until then, and in any clone or pull request "
            "that already has them, the original text is still there."
        )
    if plan.tags:
        print(f"Tags still point at the original commits: {', '.join(plan.tags)}.")
    if purge:
        purge_old_objects(repo)
        print("Expired the reflog and pruned the original commits from this clone.")
    else:
        print(
            "The original commits stay in this clone's reflog until git prunes them; "
            "`agitrack redact ... --purge` (or `git reflog expire --expire=now --all && "
            "git gc --prune=now`) removes them now."
        )


def _describe(repo: GitRepo, plan: Plan) -> None:
    # Never print the subjects: they are often the very text being removed.
    print(f"The interaction trace of {plan.turns} turn(s) will be removed from {len(plan.targets)} commit(s):")
    shown = sorted(plan.targets, key=lambda sha: _git(repo, "log", "-1", "--format=%ct", sha).strip() or "0")
    for sha in shown[:20]:
        when = _git(repo, "log", "-1", "--format=%ci", sha).strip()
        print(f"  {sha[:10]}  {when}")
    if len(shown) > 20:
        print(f"  ... and {len(shown) - 20} more")
    names = [ref.removeprefix("refs/heads/") for ref in plan.refs if ref.startswith("refs/heads/")]
    pending = [ref for ref in plan.refs if ref.startswith("refs/agitrack/manual/")]
    print(
        f"Branches rewritten: {', '.join(names) or '(none)'}"
        + (f"; pending turns: {len(pending)} ref(s)" if pending else "")
    )
    print("Files, the working tree and the index are not changed; only commit messages (and ids) are.")
    if plan.remotes:
        print(f"Already pushed to: {', '.join(plan.remotes)} (a force-push will be needed).")


class _exclusive:
    """Hold the repo's single-writer lock for the rewrite. A background tracker holding it is
    stopped for the duration and restarted in the same commit mode afterwards (it re-reads the
    rewritten history and the moved watermark on start); an interactive session is asked to
    quit first, since rewriting history under a live conversation would be a surprise."""

    def __init__(self, repo: GitRepo) -> None:
        self.repo = repo
        self.lock: RepoLock | None = None
        self.restart_args: list[str] | None = None

    def __enter__(self) -> bool:
        from agitrack.git.lock import already_running_message
        from agitrack.proxy import background

        lock = RepoLock(self.repo.repo / ".agitrack" / "lock")
        if not lock.acquire():
            info = background._read_handshake(self.repo)
            if background._live_background_pid(self.repo) is not None:
                manual = background.handshake_is_manual(info)
                if not background.replace_running_tracker(self.repo, owner_pid=None):
                    return False
                self.restart_args = ["--manual-commits" if manual else "--auto-commit"]
                # The stopped tracker may hold the OS lock for a moment after it exits (Windows).
                if not lock.acquire(retry_seconds=3.0):
                    print("aGiTrack: could not take the repository lock; nothing was changed.")
                    return False
            else:
                print(already_running_message(lock.owner_pid(), repo_root=self.repo.repo))
                print("Stop it, then run `agitrack redact` again. Nothing was changed.")
                return False
        self.lock = lock
        return True

    def __exit__(self, *exc) -> None:
        if self.lock is not None:
            self.lock.release()
        if self.restart_args is not None:
            from agitrack.proxy.background import start_background_daemon

            print("Restarting the background tracker.")
            start_background_daemon(self.repo, extra_args=self.restart_args)


def _rerender_pending_trailer(repo: GitRepo) -> None:
    """The fold trailer is a copy of the pending turns' bodies; rebuild it from the rewritten
    latent refs so the next commit folds the redacted text, not the cached original."""
    trailer = repo.repo / ".agitrack" / "manual-pending-trailer"
    if not trailer.exists():
        return
    try:
        from agitrack.commits import ManualCommitTracker
        from agitrack.config import AgitrackState

        ManualCommitTracker(repo, repo, AgitrackState(repo.repo)).render_trailer()
    except Exception:
        trailer.write_text("", encoding="utf-8")  # an empty trailer folds nothing rather than the original
