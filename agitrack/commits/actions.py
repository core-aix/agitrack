from __future__ import annotations

from typing import Protocol

from agitrack.commits.message import build_user_commit_message
from agitrack.git import GitRepo
from agitrack.transcripts.opencode import SessionTurn
from agitrack.config import AgitrackState


# ---------------------------------------------------------------------------
# Background-task file attribution
#
# A task the agent backgrounded (an experiment, a Monitor'd job) keeps writing files
# AFTER its turn was committed — most visibly under --no-worktree, where it writes the
# very tree the user works in. There is no OS-level way to ask "which process changed
# this file?", but the session's own history says it: paths that appear in agent
# commits whose recorded prompts are ONLY the synthetic background labels were written
# by background work, and further changes to them (or new files next to them) are
# presumed to be the background job still running — NOT the user's own edits. The
# user-commit flows use this to stop asking the user to commit a background job's
# output as their own work; those files are left for the agent's next commit instead.

_BACKGROUND_COMMIT_SCAN = 80  # recent commits examined
_BACKGROUND_COMMIT_TAKE = 12  # background-authored commits actually diffed
_background_paths_cache: dict[tuple[str, str], tuple[set[str], set[str]]] = {}


def _background_only_message(body: str) -> bool:
    """Whether a commit's recorded ``## User`` entries are ONLY background labels."""
    from agitrack.transcripts.claude import BACKGROUND_PROMPT_LABELS

    prompts: list[str] = []
    in_user = False
    for line in body.splitlines():
        if line.startswith("## User"):
            in_user = True
            continue
        if line.startswith("#"):
            in_user = False
            continue
        if in_user and line.strip():
            prompts.append(line.strip())
    return bool(prompts) and all(prompt in BACKGROUND_PROMPT_LABELS for prompt in prompts)


def background_authored_sets(repo: GitRepo) -> tuple[set[str], set[str]]:
    """``(paths, ancestor_dirs)`` written by background-task turns in recent history.

    Cached per (repo, HEAD): history behind HEAD never changes. Best-effort — an
    unreadable history yields empty sets, which simply keeps today's behaviour."""
    try:
        head = repo.ref_sha("HEAD") or ""
        key = (str(repo.repo), head)
        hit = _background_paths_cache.get(key)
        if hit is not None:
            return hit
        log = repo._run(["git", "log", "-n", str(_BACKGROUND_COMMIT_SCAN), "--format=%H%x01%B%x00"], check=False).stdout
        paths: set[str] = set()
        taken = 0
        for record in log.split("\x00"):
            if "\x01" not in record or taken >= _BACKGROUND_COMMIT_TAKE:
                continue
            sha, _, body = record.partition("\x01")
            if not _background_only_message(body):
                continue
            taken += 1
            names = repo._run(
                ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", sha.strip()], check=False
            ).stdout
            paths.update(line.strip() for line in names.splitlines() if line.strip())
        dirs: set[str] = set()
        for path in paths:
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            while parent:
                dirs.add(parent)
                parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
        result = (paths, dirs)
        _background_paths_cache.clear()  # one entry per repo tip is plenty
        _background_paths_cache[key] = result
        return result
    except Exception:
        return (set(), set())


def is_background_authored(path: str, sets: tuple[set[str], set[str]]) -> bool:
    """Whether ``path`` matches a background-authored file, or lives under a directory
    background work has written to (covers new files the job keeps creating there)."""
    paths, dirs = sets
    if path in paths:
        return True
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    while parent:
        if parent in dirs:
            return True
        parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
    return False


def split_background_paths(repo: GitRepo, paths: list[str]) -> tuple[list[str], list[str]]:
    """Partition ``paths`` into ``(user, background)`` using the session history."""
    sets = background_authored_sets(repo)
    if not sets[0]:
        return list(paths), []
    user: list[str] = []
    background: list[str] = []
    for path in paths:
        (background if is_background_authored(path, sets) else user).append(path)
    return user, background


def unstage_background_authored(repo: GitRepo) -> list[str]:
    """Drop background-authored files from the index (a user commit must not claim a
    background job's output as the user's own work); returns what was unstaged. The
    files stay modified in the tree, to be captured by the agent's next commit."""
    try:
        staged = repo.staged_paths()
    except Exception:
        return []
    _user, background = split_background_paths(repo, staged)
    if background:
        repo.unstage_paths(background)
    return background


class InteractiveUI(Protocol):
    """The editor-facing question surface (satisfied by shell.bridge.BridgeUI).

    When an AgitrackActions is given one of these, interactive prompts are asked of
    the driver (the VSCode extension) as menus/popups instead of reading a TTY.
    """

    def select(self, message: str, options: list[str], *, detail: str | None = ...) -> str | None: ...
    def multiselect(self, message: str, options: list[str], *, detail: str | None = ...) -> list[str]: ...
    def text(self, message: str, *, default: str = ...) -> str | None: ...
    def confirm(self, message: str) -> bool: ...
    def info(self, message: str, *, level: str = ...) -> None: ...


class AgitrackActions:
    def __init__(
        self,
        repo: GitRepo,
        state: AgitrackState,
        *,
        verbose: bool = False,
        interactive: bool = True,
        ui: InteractiveUI | None = None,
    ) -> None:
        self.repo = repo
        self.state = state
        self.verbose = verbose
        # Scripted runs (`agit --prompt`, piped stdin) cannot answer questions;
        # every input() below then falls back to a safe default instead (#53).
        self.interactive = interactive
        # When set (the VSCode bridge), interactive questions are routed to the
        # editor as menus/popups instead of the terminal. A BridgeUI-shaped object
        # exposing select/multiselect/text/confirm/info; None keeps terminal I/O.
        self.ui = ui

    def create_user_commit(self) -> bool:
        self.repo.add_tracked()
        self.review_untracked(include_declined=False)
        if not self.repo.has_staged_changes():
            if self.verbose:
                print("No staged user changes to commit.")
            return False
        if self.ui is not None:
            message = ""
            while not message.strip():
                # Cancelling (Esc) returns None — continue without committing.
                entered = self.ui.text("User commit message (Esc to continue without committing):")
                if entered is None:
                    self.ui.info("Continuing without committing.", level="warn")
                    return False
                message = entered
                if not message.strip():
                    self.ui.info("User commit message is required.", level="warn")
        else:
            message = "" if self.interactive else "Save user changes"
            while not message.strip():
                message = input("User commit message: ")
                if not message.strip():
                    print("User commit message is required.")
        self.repo.commit(build_user_commit_message(message=message, agitrack_session_id=self.state.session_id))
        self.state.clear_trace()
        print("Created user commit.")
        return True

    def create_agent_commit_from_turns(
        self,
        *,
        turns: list[SessionTurn],
        backend: str,
        backend_session_id: str | None,
        model: str | None,
        quiet: bool = False,
    ) -> bool:
        """Delegate to CommitEngine so proxy mode and actions/shell share one pipeline.

        The interactive difference (input()-based untracked review vs popup) is
        injected as ``stage_untracked_fn``.  Token and trace accounting follows
        the same d041d10 semantics as the proxy path: accumulated only once the
        commit actually happens.
        """

        def stage_untracked_fn(repo, state):
            self.review_untracked(include_declined=False)

        def on_commit_fn(sha, _trace, _is_cover):
            if not quiet:
                print("Created <aGiTrack> commit.")

        # Imported lazily: agitrack.proxy's package __init__ imports runner, which
        # imports this module — a top-level import here is circular and breaks
        # any process importing agitrack.commits.actions/agitrack.shell before agitrack.proxy.
        from agitrack.proxy.commit_engine import CommitEngine

        return CommitEngine(self.repo, self.state).commit_turns(
            turns=turns,
            backend=backend,
            backend_session_id=backend_session_id,
            model=model,
            stage_untracked_fn=stage_untracked_fn,
            on_commit_fn=on_commit_fn,
            accumulate_trace_only_on_commit=True,
        )

    def review_untracked(self, *, include_declined: bool) -> None:
        untracked = self.repo.untracked_entries()
        declined = set(self.state.declined_untracked())
        candidates = untracked if include_declined else [path for path in untracked if path not in declined]
        if not candidates:
            return

        if self.ui is not None:
            self._review_untracked_via_ui(candidates)
            return

        if not self.interactive:
            # No way to ask: stage everything new so the commit captures the
            # agent's work instead of silently dropping it.
            self.repo.stage_paths(candidates)
            self.state.remove_declined(candidates)
            print("Staged untracked files: " + ", ".join(candidates))
            return

        print("Untracked files:")
        for index, path in enumerate(candidates, start=1):
            print(f"  {index}. {path}")
        answer = input("Stage untracked files? [y/N/select]: ").strip().lower()
        if answer in {"y", "yes"}:
            self.repo.stage_paths(candidates)
            self.state.remove_declined(candidates)
            return
        if answer in {"s", "select"}:
            selected = self._select_paths(candidates)
            if selected:
                self.repo.stage_paths(selected)
                self.state.remove_declined(selected)
            declined_now = [path for path in candidates if path not in selected]
            if declined_now:
                self.state.add_declined(declined_now)
            return
        self.state.add_declined(candidates)

    def has_pre_agent_user_changes(self) -> bool:
        declined = set(self.state.declined_untracked())
        untracked = self.repo.untracked_entries()
        self.state.keep_declined(untracked)
        promptable_untracked = [path for path in untracked if path not in declined]
        # Changes attributable to the session's own BACKGROUND tasks (a monitored job
        # still writing results between commits) are not the user's edits: they must not
        # raise the "commit your changes?" dialog. They stay in the tree for the agent's
        # next commit, which claims background work anyway.
        tracked_changed = self.repo.changed_tracked_paths() if self.repo.has_tracked_changes() else []
        user_tracked, _bg = split_background_paths(self.repo, tracked_changed)
        user_untracked, _bg2 = split_background_paths(self.repo, promptable_untracked)
        return bool(user_tracked) or bool(user_untracked)

    def _review_untracked_via_ui(self, candidates: list[str]) -> None:
        """Untracked-file review routed through the editor (VSCode bridge): a
        menu to stage all / pick / skip, then a multi-select when picking. Files
        left unstaged are recorded as declined so they aren't re-offered."""
        assert self.ui is not None
        detail = "Untracked files:\n" + "\n".join(f"  {path}" for path in candidates)
        choice = self.ui.select(
            "Stage untracked files?",
            ["Stage all", "Select files…", "Skip"],
            detail=detail,
        )
        if choice == "Stage all":
            self.repo.stage_paths(candidates)
            self.state.remove_declined(candidates)
            self.ui.info("Staged untracked files: " + ", ".join(candidates))
            return
        if choice == "Select files…":
            selected = self.ui.multiselect("Select files to stage", candidates)
            if selected:
                self.repo.stage_paths(selected)
                self.state.remove_declined(selected)
            declined_now = [path for path in candidates if path not in selected]
            if declined_now:
                self.state.add_declined(declined_now)
            return
        # Skip (or dismissed): leave everything unstaged and remember the choice.
        self.state.add_declined(candidates)

    def _select_paths(self, candidates: list[str]) -> list[str]:
        raw = input("Enter numbers to stage, separated by spaces: ").strip()
        selected: list[str] = []
        for item in raw.split():
            if item.isdigit() and 1 <= int(item) <= len(candidates):
                selected.append(candidates[int(item) - 1])
        return selected
