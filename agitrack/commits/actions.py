from __future__ import annotations

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
