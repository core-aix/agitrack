from __future__ import annotations

from agit.actions import AgitActions
from agit.backends.opencode import OpenCodeBackend
from agit.git import GitRepo
from agit.state import AgitState
from agit.ui import AgitPrompt, PromptState


AGIT_PREFIX = ":"


class AgitShell:
    def __init__(self, repo: GitRepo, *, verbose: bool = False) -> None:
        self.repo = repo
        self.state = AgitState(repo.repo)
        self.verbose = verbose
        self.prompt = AgitPrompt(self._prompt_state)
        self.actions = AgitActions(repo, self.state, verbose=verbose)

    def run(self) -> None:
        self.state.save()
        if self.verbose:
            print(f"aGiT session {self.state.session_id}")
            print(f"Repository: {self.repo.repo}")
            print("Type :help for aGiT commands. OpenCode / commands are passed through.")
        while True:
            try:
                text = self.prompt.prompt().strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not text:
                continue
            if text.startswith(AGIT_PREFIX):
                if self._handle_command(text):
                    return
            else:
                self._handle_agent_prompt(text)

    def _handle_command(self, text: str) -> bool:
        command, _, arg = text.partition(" ")
        if command in {":exit", ":quit"}:
            return True
        if command == ":help":
            self._print_help()
        elif command == ":status":
            print(self.repo.status_short() or "Working tree clean")
        elif command == ":agent":
            agent = arg.strip()
            if agent != "opencode":
                print("Only the opencode backend is available in the MVP.")
            else:
                self.state.backend = agent
                print("Backend set to opencode")
        elif command == ":user-commit":
            self.actions.create_user_commit()
        elif command == ":unstaged":
            declined = self.state.declined_untracked()
            if declined:
                print("Intentionally unstaged files:")
                for path in declined:
                    print(f"  {path}")
            else:
                print("No intentionally unstaged files.")
        elif command == ":stage":
            self.actions.review_untracked(include_declined=True)
        else:
            print(f"Unknown command: {command}")
        return False

    def _handle_agent_prompt(self, prompt: str) -> None:
        if self.actions.has_pre_agent_user_changes():
            print("User changes detected before agent runs.")
            self.actions.create_user_commit()

        backend = self._backend()
        self.state.append_trace("user", prompt)
        result = backend.run(prompt, model=self.state.model, session_id=self.state.backend_session_id)
        if result.session_id:
            self.state.backend_session_id = result.session_id
        if result.model and result.model != self.state.model:
            self.state.model = result.model
        if result.exit_code != 0:
            self.state.append_trace("agent", result.final_response or f"Backend exited with code {result.exit_code}")
            self.state.add_token_usage(result.tokens)
            if self.verbose:
                print(f"Backend exited with code {result.exit_code}; no automatic agent commit was made.")
            return

        self.state.append_trace("agent", result.final_response)
        self.state.add_token_usage(result.tokens)
        self.repo.add_tracked()
        self.actions.review_untracked(include_declined=False)
        if self.repo.has_staged_changes():
            from agit.commit_message import build_agent_commit_message

            self.repo.commit(
                build_agent_commit_message(
                    latest_prompt=prompt,
                    trace=self.state.pending_trace(),
                    backend=result.backend,
                    backend_session_id=self.state.backend_session_id,
                    agit_session_id=self.state.session_id,
                    model=self.state.model,
                    token_usage=self.state.pending_token_usage(),
                )
            )
            self.state.clear_trace()
            print("Created <agent> commit.")
        else:
            if self.verbose:
                print("No code changes detected; interaction trace remains pending.")

    def _backend(self) -> OpenCodeBackend:
        if self.state.backend != "opencode":
            raise RuntimeError(f"Unsupported backend: {self.state.backend}")
        return OpenCodeBackend(self.repo.repo, verbose=self.verbose)

    def _print_help(self) -> None:
        print("Commands:")
        print("  :help              show this help")
        print("  :status            show git status")
        print("  :user-commit       create a <user> commit")
        print("  :stage             review and stage untracked files")
        print("  :unstaged          show intentionally unstaged files")
        print("  :agent opencode    select the OpenCode backend")
        print("  :exit              exit")
        print("OpenCode / commands are not reserved by aGiT and are sent to the backend.")

    def _prompt_state(self) -> PromptState:
        existing = [path for path in self.state.declined_untracked() if (self.repo.repo / path).exists()]
        return PromptState(
            repo=self.repo.repo,
            backend=self.state.backend,
            model=self.state.model,
            declined_count=len(existing),
            verbose=self.verbose,
        )
