"""``agitrack --backtrace commit``: rewrite a repository's history onto a NEW branch so that the
commits which correspond to AI-made changes carry the aGiTrack metadata + interaction trace they
would have had if the sessions had been run with aGiTrack — reconstructed from local transcripts.

It answers "I built this project with a coding agent but WITHOUT aGiTrack; can I still get the
tracked history?" — yes: this replays every existing commit onto a new branch (same trees, same
authors/dates, same structure), and for each commit whose files an AI turn produced, appends the
turn's ``# Interaction Trace`` and ``# aGiTrack Metadata`` (backend, model, tokens, timings). User
commits with no AI correspondence are copied verbatim.

Because it rewrites history (every commit gets a new SHA), it is destructive-adjacent: it only
runs on a NEW branch, requires a clean working tree, and never touches the original branch — the
user force-replaces the old branch themselves, after reviewing, if they want to.

AI-vs-user attribution: a commit is "AI" when its changed files overlap the files an agent turn
edited (matched to the earliest commit at/after the turn's time that contains those files). A
commit no turn explains is a user commit.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from agitrack.backends.base import TokenUsage
from agitrack.commits import METADATA_HEADER
from agitrack.commits.message import _trace_and_metadata_lines
from agitrack.metrics import backtrace as bt
from agitrack.metrics import files as filesmod
from agitrack.metrics.collect import _abbreviate_home


@dataclass
class _TurnRec:
    """The bits of one agent turn that matter for reconstructing a commit."""

    ended_at: int
    started_at: int | None
    files: set[str]
    backend: str
    model: str | None
    tokens: TokenUsage
    user_prompt: str
    final_response: str
    queued_followups: list[str] = field(default_factory=list)
    session_id: str = ""
    reasoning_effort: str | None = None


def backtrace_commit(directory: Path, new_branch: str, *, assume_yes: bool = False, _input=input) -> int:
    """Entry point for ``agitrack --backtrace commit --branch <new_branch>``.

    Returns a process exit code. Prints all user-facing guidance itself (git-init hint, dirty-tree
    hint, the warning + confirmation, the progress bar, and the force-replace instructions)."""
    from agitrack.git import GitError, GitRepo

    directory = directory.expanduser().resolve()

    # 1) Must be a git repository — a plain directory can't hold reconstructed commits.
    try:
        repo = GitRepo.discover(directory)
    except (GitError, OSError):
        print(
            f"{_abbreviate_home(str(directory))} is not a git repository.\n"
            "`--backtrace commit` writes real commits, so the directory must be a repo first. "
            "Initialize one and commit your current files, then re-run:\n"
            "    git init\n"
            "    git add -A && git commit -m 'initial snapshot'\n"
            "    agitrack --backtrace commit --branch <new-branch>"
        )
        return 1
    root = repo.repo

    # 2) A new branch name is required (this never writes to the current branch).
    new_branch = (new_branch or "").strip()
    if not new_branch:
        print(
            "Give the name of a NEW branch to create the reconstructed history on:\n"
            "    agitrack --backtrace commit --branch <new-branch>\n"
            "The reconstruction rewrites history, so it is placed on its own branch and your current "
            "branch is left untouched."
        )
        return 1
    if _branch_exists(repo, new_branch):
        print(f"Branch '{new_branch}' already exists. Choose a new branch name that does not exist yet.")
        return 1

    # 3) The working tree must be clean — a rewrite that carried uncommitted edits would be
    #    unreconstructable and unsafe. Instruct the user to commit or ignore everything first.
    dirty = repo._run(["git", "status", "--porcelain"], check=False).stdout.strip()
    if dirty:
        print(
            "Your working tree has uncommitted changes:\n\n"
            + "\n".join("    " + line for line in dirty.splitlines())
            + "\n\n`--backtrace commit` rewrites history and must start from a CLEAN tree. Commit the "
            "changes you want to keep, or add them to .gitignore (then `git rm --cached` anything "
            "already tracked), until `git status` is clean, and re-run."
        )
        return 1

    # 4) Reconstruct the agent turns (with the files each changed) from local transcripts.
    turns = [t for t in _gather_turns(root) if t.files]
    if not turns:
        print(
            f"No AI-made file changes were found in local Claude/OpenCode transcripts for "
            f"{_abbreviate_home(str(root))}.\nThere is nothing to reconstruct — this command annotates "
            "commits whose changes an agent produced."
        )
        return 0

    # 5) The commits to replay (oldest first), and which of them are AI-made.
    commits = _commits_oldest_first(repo)
    if not commits:
        print("This repository has no commits yet — nothing to reconstruct.")
        return 0
    changed = filesmod._numstat_by_commit(repo, "HEAD", set(commits))
    ai_map = _match_turns_to_commits(repo, commits, changed, turns)

    # 6) Warn (history rewrite, new SHAs, force needed) and confirm.
    print(
        f"Reconstructing tracked history for {_abbreviate_home(str(root))}:\n"
        f"  • {len(commits)} commit(s) will be replayed onto a new branch '{new_branch}'.\n"
        f"  • {len(ai_map)} of them will gain aGiTrack metadata (backend, model, tokens, and the "
        f"user↔agent trace) from {len(turns)} reconstructed agent turn(s).\n"
        f"  • {len(commits) - len(ai_map)} will be kept verbatim as user commits.\n\n"
        "This REWRITES history: every commit gets a new hash, so the new branch is NOT a "
        "fast-forward of your current branch. Your current branch is left untouched."
    )
    if not assume_yes:
        answer = _input(f"Create branch '{new_branch}' with the reconstructed history? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted — no changes made.")
            return 0

    # 7) Replay onto the new branch, with a progress bar.
    original_branch = repo.current_branch()
    new_tip = _replay(repo, commits, ai_map, new_branch)
    if not new_tip:
        print("Reconstruction failed — no commits were written. Your repository is unchanged.")
        return 1
    repo._run(["git", "branch", new_branch, new_tip], check=False)
    switched = repo._run(["git", "switch", new_branch], check=False)
    on_branch = switched.returncode == 0

    _print_completion(new_branch, original_branch, on_branch, len(commits), len(ai_map))
    return 0


# ---------------------------------------------------------------------------
# Reconstruction helpers
# ---------------------------------------------------------------------------


def _gather_turns(root: Path) -> list[_TurnRec]:
    """Every agent turn recorded for the repository, with its edited files relativized to the
    repo root (so they compare directly to git's repo-relative paths)."""
    out: list[_TurnRec] = []
    for source in bt._discover(root):
        try:
            exported = source.export()
        except Exception:
            exported = None
        if exported is None:
            continue
        bases = bt._relativize_bases(root, source.base_dir)
        for turn in exported.turns:
            edits = [bt._relativize(edit, bases) for edit in turn.edits]
            files = {edit.path for edit in edits if edit.path}
            if not files:
                continue
            out.append(
                _TurnRec(
                    ended_at=int(turn.ended_at or turn.started_at or exported.updated or 0),
                    started_at=turn.started_at,
                    files=files,
                    backend=source.backend,
                    model=turn.model,
                    tokens=turn.tokens,
                    user_prompt=turn.user_prompt,
                    final_response=turn.final_response,
                    queued_followups=list(turn.queued_followups),
                    session_id=exported.session_id,
                    reasoning_effort=turn.reasoning_effort,
                )
            )
    return out


def _commits_oldest_first(repo) -> list[str]:
    """Every commit reachable from HEAD, parents before children (topological, oldest first)."""
    out = repo._run(["git", "log", "--topo-order", "--reverse", "--format=%H", "HEAD", "--"], check=False).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _match_turns_to_commits(
    repo, commits: list[str], changed: dict, turns: list[_TurnRec]
) -> dict[str, list[_TurnRec]]:
    """Attribute each turn to the EARLIEST commit at/after the turn's time whose changed files
    overlap the turn's files — that commit is where the agent's change actually landed. Returns
    ``sha -> [turns]`` for the commits that are thereby AI-made."""
    times = {
        sha: int(repo._run(["git", "log", "-1", "--format=%at", sha], check=False).stdout.strip() or 0)
        for sha in commits
    }
    commit_files = {sha: {path for (path, _ins, _del) in changed.get(sha, [])} for sha in commits}
    ai_map: dict[str, list[_TurnRec]] = {}
    for turn in sorted(turns, key=lambda t: t.ended_at):
        for sha in commits:  # oldest first
            if times[sha] + 1 >= turn.ended_at and (turn.files & commit_files[sha]):
                ai_map.setdefault(sha, []).append(turn)
                break
    return ai_map


def _annotation(turns: list[_TurnRec]) -> str:
    """The ``# Interaction Trace`` + ``# aGiTrack Metadata`` block to append to a commit, built from
    the turns that produced it — rendered in the exact format a real aGiTrack commit uses."""
    ordered = sorted(turns, key=lambda t: t.ended_at)
    trace: list[dict] = []
    tokens = TokenUsage()
    backends: Counter[str] = Counter()
    models: Counter[str] = Counter()
    effort: str | None = None
    session_ids: set[str] = set()
    starts: list[int] = []
    ends: list[int] = []
    for turn in ordered:
        if turn.user_prompt.strip():
            trace.append({"role": "user", "content": turn.user_prompt})
        for followup in turn.queued_followups:
            if followup.strip():
                trace.append({"role": "user", "content": followup})
        if turn.final_response.strip():
            trace.append({"role": "agent", "content": turn.final_response})
        tokens.add(turn.tokens)
        backends[turn.backend] += 1
        if turn.model:
            models[turn.model] += 1
        effort = effort or turn.reasoning_effort
        session_ids.add(turn.session_id)
        if turn.started_at:
            starts.append(turn.started_at)
        if turn.ended_at:
            ends.append(turn.ended_at)
    backend = backends.most_common(1)[0][0] if backends else "unknown"
    model = models.most_common(1)[0][0] if models else None
    backend_session_id = session_ids.pop() if len(session_ids) == 1 else "multiple"
    lines = _trace_and_metadata_lines(
        trace=trace,
        backend=backend,
        backend_session_id=backend_session_id,
        # These sessions weren't run under aGiTrack; mark the reconstruction honestly rather than
        # inventing a session identity/name.
        agitrack_session_id="reconstructed",
        session_name="backtrace",
        model=model,
        reasoning_effort=effort,
        token_usage=tokens.to_dict(),
        trace_turn_limit=len(trace) + 1,
        covered_commits=None,
        started_at=min(starts) if starts else None,
        ended_at=max(ends) if ends else None,
    )
    return "\n".join(lines)


def _replay(repo, commits: list[str], ai_map: dict[str, list[_TurnRec]], new_branch: str) -> str:
    """Recreate every commit with ``git commit-tree`` — same tree, same author/committer identity
    and dates, parents remapped to the rewritten history — appending the aGiTrack annotation to AI
    commits. Returns the new tip SHA (or "" on failure). Shows a progress bar."""
    old_to_new: dict[str, str] = {}
    total = len(commits)
    print(f"Reconstructing {total} commit(s) onto '{new_branch}' …")
    for index, sha in enumerate(commits, start=1):
        tree = repo._run(["git", "rev-parse", f"{sha}^{{tree}}"], check=False).stdout.strip()
        if not tree:
            _progress(index, total)
            continue
        parent_line = repo._run(["git", "rev-list", "--parents", "-n", "1", sha], check=False).stdout.split()
        parents = parent_line[1:] if len(parent_line) > 1 else []
        new_parents = [old_to_new[p] for p in parents if p in old_to_new]

        message = repo._run(["git", "log", "-1", "--format=%B", sha], check=False).stdout.rstrip("\n")
        turns = ai_map.get(sha)
        # Don't double-annotate a commit that already carries aGiTrack metadata (e.g. a repo that
        # used aGiTrack for part of its life).
        if turns and METADATA_HEADER not in message:
            message = message.rstrip() + "\n\n" + _annotation(turns) + "\n"

        args = ["git", "commit-tree", tree]
        for parent in new_parents:
            args += ["-p", parent]
        new_sha = repo._run(args, input_text=message, env=_identity_env(repo, sha), check=False).stdout.strip()
        if not new_sha:
            print("\n  ! failed to write a commit; aborting reconstruction.")
            return ""
        old_to_new[sha] = new_sha
        _progress(index, total)
    print()  # end the progress line
    return old_to_new.get(commits[-1], "")


def _identity_env(repo, sha: str) -> dict[str, str]:
    """The author/committer identity + dates of ``sha``, as git env vars, so the replayed commit
    preserves who made it and when."""
    raw = repo._run(
        ["git", "log", "-1", "--format=%an%x1f%ae%x1f%aI%x1f%cn%x1f%ce%x1f%cI", sha], check=False
    ).stdout.strip()
    parts = raw.split("\x1f")
    if len(parts) != 6:
        return {}
    an, ae, ad, cn, ce, cd = parts
    return {
        "GIT_AUTHOR_NAME": an,
        "GIT_AUTHOR_EMAIL": ae,
        "GIT_AUTHOR_DATE": ad,
        "GIT_COMMITTER_NAME": cn,
        "GIT_COMMITTER_EMAIL": ce,
        "GIT_COMMITTER_DATE": cd,
    }


def _branch_exists(repo, name: str) -> bool:
    result = repo._run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def _progress(done: int, total: int) -> None:
    """A single-line textual progress bar (repositories with long histories can take a while)."""
    width = 32
    filled = int(width * done / total) if total else width
    bar = "█" * filled + "░" * (width - filled)
    pct = int(100 * done / total) if total else 100
    print(f"\r  [{bar}] {pct:3d}%  {done}/{total} commits", end="", flush=True)


def _print_completion(new_branch: str, original_branch: str, on_branch: bool, n_commits: int, n_ai: int) -> None:
    here = (
        f"You are now on '{new_branch}'." if on_branch else f"Created '{new_branch}' (couldn't switch automatically)."
    )
    old = original_branch or "your previous branch"
    print(
        f"\nDone. Reconstructed {n_commits} commit(s) on '{new_branch}' "
        f"({n_ai} annotated with aGiTrack metadata). {here}\n"
        f"\nReview the result before doing anything irreversible:\n"
        f"    git switch {new_branch}\n"
        f"    agitrack --dashboard          # see the reconstructed AI attribution\n"
        f"    git log --stat                # spot-check the commits and their messages\n"
        f"\nThis branch is a REWRITE of '{old}', so it does NOT merge cleanly (every commit has a new "
        f"hash). If, after reviewing, you want '{new_branch}' to REPLACE '{old}':\n"
        f"    git branch -f {old} {new_branch}       # move the old branch to the reconstructed tip\n"
        f"    git switch {old}\n"
        f"    # if it was already pushed, others must re-clone/reset — force-push only if you are sure:\n"
        f"    git push --force-with-lease origin {old}\n"
        f"Keep '{new_branch}' (or a backup of '{old}') until you have confirmed everything looks right."
    )
