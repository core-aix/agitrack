from __future__ import annotations

import sys

from typing import Protocol

from agitrack.commits.message import build_user_commit_message
from agitrack.git import GitRepo
from agitrack.transcripts.opencode import SessionTurn
from agitrack.config import AgitrackState


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


# The console spelling of "Esc" at the user-commit prompt. A word rather than an empty line:
# an empty answer must keep re-prompting, since a stray Enter should never be read as a decision
# to leave the user's work uncommitted.
_SKIP_WORD = "skip"
# Typed at a prompt that cannot be skipped, this ABANDONS the run instead — so the user is never
# trapped in a question with no answer. See create_user_commit.
_QUIT_WORD = "quit"


class UserCommitAborted(Exception):
    """The user asked to abandon the run at the mandatory pre-agent commit prompt.

    Worktree mode requires this commit, and the prompt had NO way out at all: `skip` was not
    recognised (it became the commit MESSAGE — a commit literally named `skip` landed on main),
    an empty Enter re-asked forever, and Ctrl-C was swallowed; one live run spent 150 s and six
    Ctrl-C without escaping. A required question still has to have an exit."""


class AgitrackActions:
    def __init__(
        self,
        repo: GitRepo,
        state: AgitrackState,
        *,
        verbose: bool = False,
        interactive: bool = True,
        ui: InteractiveUI | None = None,
        human_stream=None,
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
        # WHERE PROSE GOES. Under --json-events stdout carries one JSON object per line and
        # nothing else, so these notices ("Staged untracked files: …", "Created user commit.")
        # have to follow the shell's own prose stream rather than being printed straight to
        # stdout — one of them landing there is enough to break a driver's json.loads(line).
        self._out = human_stream if human_stream is not None else sys.stdout

    def _say(self, *args, **kwargs) -> None:
        """print() onto this run's prose stream (stdout normally, stderr under --json-events)."""
        kwargs.setdefault("file", self._out)
        print(*args, **kwargs)

    def _staged_paths(self) -> list[str]:
        """The staged files this commit would contain, for showing before the message
        prompt. Best-effort: a listing failure must never block committing."""
        try:
            output = self.repo._run(["git", "diff", "--cached", "--name-only"], check=False).stdout
        except Exception:
            return []
        return [line.strip() for line in output.splitlines() if line.strip()]

    def create_user_commit(self, *, allow_skip: bool = True) -> bool:
        """Offer to commit the user's own uncommitted changes. True when one was made.

        ``allow_skip`` says whether declining is an acceptable outcome. It is not in worktree
        mode: the session's worktree is checked out from HEAD, so anything left uncommitted
        simply is not in the tree the agent works in — the user would be editing one copy while
        the agent reads another. Under ``--no-worktree`` the agent works in this very tree, so
        uncommitted changes are still right there and declining costs nothing.

        Declining NEVER leaves the index touched. This method stages on the user's behalf in
        order to show what a commit would contain; if they then back out, the index is restored
        to what it was. Leaving it staged was actively harmful during a merge conflict, where
        `git status` files staged paths under "changes to be committed" and a half-resolved file
        stops standing out as one still needing work.
        """
        staged_before = set(self.repo.staged_paths())

        def restore_index() -> None:
            try:
                self.repo.unstage([path for path in self.repo.staged_paths() if path not in staged_before])
            except Exception:
                pass  # best-effort: never turn a declined commit into a failure

        self.repo.add_tracked()
        self.review_untracked(include_declined=False)
        if not self.repo.has_staged_changes():
            if self.verbose:
                self._say("No staged user changes to commit.")
            restore_index()
            return False
        # Show WHAT is about to be committed before asking for a message: the answer is a
        # decision about these files, and at startup they may be edits the user forgot they
        # had (or made in another window) — naming a commit blind is how unintended content
        # gets in. The TUI popup already lists them; this is the console path.
        staged = self._staged_paths()
        listing = "\n".join(f"  {path}" for path in staged)
        skip_hint = (
            "Esc to continue without committing"
            if allow_skip
            else "a commit is required here: the agent's worktree is checked out from HEAD, so "
            "uncommitted changes would not be in it. Run with --no-worktree to work in this tree "
            f"instead, or type '{_QUIT_WORD}' to stop aGiTrack"
        )
        if self.ui is not None:
            message = ""
            cancelled = False
            while not message.strip():
                # Cancelling (Esc) returns None — continue without committing.
                # Folded into the message rather than passed as a separate field: a UI
                # implementation only has to render text() to show the file list.
                question = f"User commit message ({skip_hint}):"
                if staged:
                    question = f"Committing {len(staged)} file(s):\n{listing}\n\n{question}"
                entered = self.ui.text(question)
                if entered is None:
                    if not allow_skip:
                        # Cancelling twice is not indecision — it is someone with no other way
                        # out. Honour the second one rather than looping forever.
                        if cancelled:
                            restore_index()
                            raise UserCommitAborted
                        cancelled = True
                        self.ui.info(f"Cannot skip — {skip_hint}. Cancel again to stop aGiTrack.", level="warn")
                        continue
                    self.ui.info("Continuing without committing.", level="warn")
                    restore_index()
                    return False
                message = entered
                if message.strip().lower() in (_SKIP_WORD, _QUIT_WORD) and not allow_skip:
                    if message.strip().lower() == _QUIT_WORD:
                        restore_index()
                        raise UserCommitAborted
                    self.ui.info(f"Cannot skip — {skip_hint}.", level="warn")
                    message = ""
                    continue
                if not message.strip():
                    self.ui.info("User commit message is required.", level="warn")
        else:
            message = "" if self.interactive else "Save user changes"
            if self.interactive and staged:
                self._say(f"Committing {len(staged)} file(s) to {self.repo.repo}:")
                self._say(listing)
            # An explicit word, not an empty line, is the way out. Empty deliberately re-prompts
            # (a stray Enter must never be read as "don't commit my work"), so without a
            # sentinel this loop had no exit at all — which is how a --no-worktree start became
            # impossible with a dirty tree.
            prompt = f"User commit message (or type '{_SKIP_WORD}' to continue without committing): "
            if not allow_skip:
                prompt = f"User commit message (or '{_QUIT_WORD}' to stop aGiTrack): "
            while not message.strip():
                try:
                    message = input(prompt)
                except (EOFError, KeyboardInterrupt):
                    self._say()  # no usable stdin, or interrupted
                    restore_index()
                    if not allow_skip:
                        # Ctrl-C at a question that cannot be answered any other way means
                        # "get me out", not "commit nothing and carry on into a worktree that
                        # is missing my work". It was previously swallowed into `return False`.
                        raise UserCommitAborted
                    return False
                typed = message.strip().lower()
                if typed == _SKIP_WORD:
                    if allow_skip:
                        self._say("Continuing without committing.")
                        restore_index()
                        return False
                    # NEVER commit the sentinel. Typing `skip` where skipping is impossible
                    # produced a commit literally named `skip` on the user's branch — the same
                    # word means "back out" one mode over, so people reach for it here too.
                    self._say(f"Cannot skip — {skip_hint}.")
                    message = ""
                    continue
                if typed == _QUIT_WORD and not allow_skip:
                    restore_index()
                    raise UserCommitAborted
                if not message.strip():
                    self._say(
                        "User commit message is required."
                        if allow_skip
                        else f"User commit message is required — {skip_hint}."
                    )
        self.repo.commit(
            build_user_commit_message(
                message=message, agitrack_session_id=self.state.session_id, repo_root=self.repo.repo
            )
        )
        self.state.clear_trace()
        self._say("Created user commit.")
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
                self._say("Created <aGiTrack> commit.")

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
            self._say("Staged untracked files: " + ", ".join(candidates))
            return

        self._say("Untracked files:")
        for index, path in enumerate(candidates, start=1):
            self._say(f"  {index}. {path}")
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
        return self.repo.has_tracked_changes() or bool(promptable_untracked)

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
