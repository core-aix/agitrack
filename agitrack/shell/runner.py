from __future__ import annotations

import json
import sys

from agitrack.commits import AgitrackActions
from agitrack.backends.setup import BackendUnavailable, backend_installed, ensure_installed_backend, install_hint
from agitrack.backends.claude import ClaudeBackend
from agitrack.backends.opencode import OpenCodeBackend
from agitrack.git import GitRepo
from agitrack.config import GlobalConfig
from agitrack.git import RepoLock, already_running_message
from agitrack.config import AgitrackState
from agitrack.shell.ui import AgitrackPrompt, PromptState


AGITRACK_PREFIX = ":"

BACKENDS = {
    OpenCodeBackend.name: OpenCodeBackend,
    ClaudeBackend.name: ClaudeBackend,
}


class AgitrackShell:
    def __init__(
        self,
        repo: GitRepo,
        *,
        verbose: bool = False,
        backend: str | None = None,
        new_session: bool = False,
        backend_args: list[str] | None = None,
        backend_command: list[str] | None = None,
        prompts: list[str] | None = None,
        commit_guidance: bool = True,
        json_events: bool = False,
        ui_bridge: bool = False,
    ) -> None:
        self.repo = repo
        self.backend_args = list(backend_args or [])  # forwarded to the backend CLI (#32)
        # Per-run override (from --backend-command) for the command that launches the
        # backend, replacing its executable so the agent runs under a user wrapper. Empty
        # ⇒ the per-backend config value (GlobalConfig.backend_command) applies.
        self._backend_command = list(backend_command or [])
        # The VSCode extension runs aGiTrack as a long-lived child with no terminal and
        # drives it over a bidirectional JSON-RPC bridge (see agitrack/shell/bridge.py):
        # prompts/commands arrive on stdin, events go out on stdout, and interactive
        # questions (menus, confirms, text) are asked of the editor instead of a TTY.
        self._ui_bridge = ui_bridge
        from agitrack.shell.bridge import BridgeServer, BridgeUI

        self._bridge: BridgeServer | None = BridgeServer() if ui_bridge else None
        self.ui: BridgeUI | None = BridgeUI(self._bridge) if self._bridge is not None else None
        # When True, emit one machine-readable JSON line per turn event (the agent's
        # response, the commit it produced, errors) so a programmatic driver — the
        # VSCode chat extension (see editors/vscode) — can render the conversation.
        # Bridge mode always emits them; otherwise it follows the --json-events flag.
        self._json_events = json_events or ui_bridge
        # Tell the coding agent that aGiTrack auto-commits so it doesn't self-commit
        # (--no-commit-guidance turns it off). Appended where the backend supports it.
        self._commit_guidance = commit_guidance
        # Scripted mode (#53): run these prompts in order, then exit. No
        # question can be answered in a scripted or piped run, so everything
        # that would ask one falls back to a safe non-interactive default.
        self.prompts = list(prompts) if prompts is not None else None
        # Bridge mode is interactive even though stdin isn't a TTY — questions are
        # answered by the editor over the bridge rather than by a terminal user.
        self.interactive = self._ui_bridge or (self.prompts is None and sys.stdin.isatty())
        self.global_config = GlobalConfig()
        self.state = AgitrackState(repo.repo, default_backend=self.global_config.default_backend)
        if backend and backend in BACKENDS and backend != self.state.backend:
            self.state.remember_backend_session()
            self.state.backend = backend
            self.global_config.default_backend = backend
            self.state.backend_session_id = self.state.stored_backend_session(backend)
            self.state.last_backend_message_id = None
        if new_session:
            self.state.backend_session_id = None
            self.state.last_backend_message_id = None
            self.state.new_agitrack_session_id()
        self.verbose = verbose
        self.prompt = AgitrackPrompt(self._prompt_state)
        self.actions = AgitrackActions(repo, self.state, verbose=verbose, interactive=self.interactive, ui=self.ui)
        self.management_lock = RepoLock(repo.repo / ".agitrack" / "lock")

    def run(self) -> None:
        try:
            resolved = ensure_installed_backend(self.state.backend, self.global_config, interactive=self.interactive)
        except BackendUnavailable as error:
            print(error)
            if self._bridge is not None:
                self._bridge.emit({"type": "error", "message": str(error)})
                self._bridge.emit({"type": "bye"})
            return
        if resolved != self.state.backend:
            self.state.backend = resolved
        if not self.management_lock.acquire():
            message = already_running_message(self.management_lock.owner_pid())
            print(message)
            if self._bridge is not None:
                self._bridge.emit({"type": "error", "message": message})
                self._bridge.emit({"type": "bye"})
            return
        self.state.save()
        if self.verbose:
            print(f"aGiTrack session {self.state.session_id}")
            print(f"Repository: {self.repo.repo}")
            print(f"Backend: {self.state.backend}")
            print("Type :help for aGiTrack commands. Backend / commands are passed through.")
        try:
            if self._bridge is not None:
                self._run_bridge()
                return
            if self.prompts is not None:
                self._run_scripted(self.prompts)
                return
            while True:
                try:
                    text = self.prompt.prompt().strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return
                if not text:
                    continue
                if text.startswith(AGITRACK_PREFIX):
                    if self._handle_command(text):
                        return
                else:
                    self._handle_agent_prompt(text)
        finally:
            self.management_lock.release()

    def _run_scripted(self, prompts: list[str]) -> None:
        """`agitrack --prompt ...` (#53): run the prompts in order, then exit.
        ':' commands work exactly as at the interactive prompt; each prompt is
        echoed so the output reads like a session transcript."""
        for text in prompts:
            text = text.strip()
            if not text:
                continue
            print(f"> {text}")
            if text.startswith(AGITRACK_PREFIX):
                if self._handle_command(text):
                    return
            else:
                self._handle_agent_prompt(text)

    def _run_bridge(self) -> None:
        """Long-lived JSON-RPC loop for the VSCode extension. Reads prompt/command
        requests from stdin, runs each turn (interactive questions are asked of the
        editor via ``self.ui``), and frames every turn with a ``turn-complete`` so
        the editor knows when to re-enable input. Exits on an ``exit`` request or
        when stdin closes."""
        assert self._bridge is not None
        bridge = self._bridge
        bridge.start()
        bridge.emit(
            {
                "type": "ready",
                "session": self.state.session_id,
                "backend": self.state.backend,
                "repo": str(self.repo.repo),
                "model": self.state.model,
            }
        )
        while True:
            request = bridge.next_request()
            kind = request.get("type")
            if kind == "exit":
                break
            try:
                text = (request.get("text") or "").strip()
                if not text:
                    pass
                elif kind == "command":
                    if self._bridge_command(text):
                        break
                elif kind == "prompt":
                    if text.startswith(AGITRACK_PREFIX):
                        if self._bridge_command(text):
                            break
                    else:
                        self._handle_agent_prompt(text)
            except Exception as error:  # never let one turn kill the session
                bridge.emit({"type": "error", "message": str(error)})
            bridge.emit({"type": "turn-complete"})
        bridge.emit({"type": "bye"})

    def _bridge_command(self, text: str) -> bool:
        """Handle a ':' command in bridge mode, emitting results as editor notices
        instead of printing to a terminal. Returns True to end the session."""
        assert self._bridge is not None and self.ui is not None
        command, _, arg = text.partition(" ")
        if command in {":exit", ":quit"}:
            return True
        if command == ":status":
            self.ui.info(self.repo.status_short() or "Working tree clean")
        elif command == ":user-commit":
            self.actions.create_user_commit()
        elif command == ":stage":
            self.actions.review_untracked(include_declined=True)
        elif command == ":unstaged":
            declined = self.state.declined_untracked()
            if declined:
                self.ui.info("Intentionally unstaged files:\n" + "\n".join(f"  {p}" for p in declined))
            else:
                self.ui.info("No intentionally unstaged files.")
        elif command == ":new-session":
            self.state.remember_backend_session()
            self.state.backend_session_id = None
            self.state.last_backend_message_id = None
            self.state.new_agitrack_session_id()
            self.state.save()
            self.ui.info(f"Started a new session: {self.state.session_id}")
            self._bridge.emit({"type": "ready", "session": self.state.session_id, "backend": self.state.backend})
        elif command == ":agent-backend":
            self._bridge_switch_backend(arg.strip())
        elif command == ":summarizer":
            self._handle_summarizer_command(arg.strip())
        else:
            self.ui.info(f"Unknown command: {command}", level="warn")
        return False

    def _bridge_switch_backend(self, agent: str) -> None:
        assert self.ui is not None
        if not agent:
            agent = self.ui.select("Choose a backend", sorted(BACKENDS)) or ""
        if agent not in BACKENDS:
            self.ui.info(
                f"Unknown backend: {agent or '(none)'}. Available: {', '.join(sorted(BACKENDS))}", level="warn"
            )
            return
        if not backend_installed(agent):
            self.ui.info(f"'{agent}' is not installed. {install_hint(agent)}", level="warn")
            return
        self.state.remember_backend_session()
        self.state.backend = agent
        self.global_config.default_backend = agent
        self.state.backend_session_id = self.state.stored_backend_session(agent)
        self.state.last_backend_message_id = None
        self.ui.info(f"Backend set to {agent}")
        self._bridge.emit({"type": "ready", "session": self.state.session_id, "backend": agent})  # type: ignore[union-attr]

    def _handle_command(self, text: str) -> bool:
        command, _, arg = text.partition(" ")
        if command in {":exit", ":quit"}:
            return True
        if command == ":help":
            self._print_help()
        elif command == ":status":
            print(self.repo.status_short() or "Working tree clean")
        elif command == ":agent-backend":
            agent = arg.strip()
            if agent not in BACKENDS:
                print(f"Unknown backend: {agent or '(none)'}. Available: {', '.join(sorted(BACKENDS))}")
            elif not backend_installed(agent):
                print(f"'{agent}' is not installed.")
                print(install_hint(agent))
            else:
                self.state.remember_backend_session()
                self.state.backend = agent
                self.global_config.default_backend = agent
                self.state.backend_session_id = self.state.stored_backend_session(agent)
                self.state.last_backend_message_id = None
                print(f"Backend set to {agent}")
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
        elif command == ":summarizer":
            self._handle_summarizer_command(arg.strip())
        else:
            print(f"Unknown command: {command}")
        return False

    def _handle_summarizer_command(self, arg: str) -> None:
        sub = arg.lower()
        if sub in ("on", "off"):
            enabled = sub == "on"
            # Persist globally so the toggle survives restarts (the per-session worktree
            # state is transient and reset to "on" each launch); keep the session in sync.
            if self.global_config is not None:
                self.global_config.summarization_enabled = enabled
            self.state.summarization_enabled = enabled
            self._say(f"Summarizer {'enabled' if enabled else 'disabled'}.")
        elif sub == "model":
            current = self.state.summarization_model or self.global_config.summarization_model or "(same as session)"
            if self.ui is not None:
                entered = self.ui.text(f"Summarizer model (current: {current}; empty to clear):")
                if entered is None:
                    return  # cancelled — leave the model unchanged
                new_model = entered.strip()
            else:
                print(f"Current summarizer model: {current}")
                new_model = input("Enter model (empty to clear): ").strip()
            # Persist globally (survives restarts and applies across the repo); clear the
            # per-session override so the global value takes effect.
            self.global_config.summarization_model = new_model or None
            self.state.summarization_model = None
            self._say(f"Summarizer model set to: {self.global_config.summarization_model or '(same as session)'}")
        elif sub == "" or sub == "status":
            enabled = self._summarization_enabled()
            model = self.state.summarization_model or self.global_config.summarization_model or "(same as session)"
            self._say(f"Summarizer: {'ON' if enabled else 'OFF'}\nModel: {model}")
        else:
            self._say(f"Unknown summarizer command: {arg}\nUsage: :summarizer [on|off|model|status]", level="warn")

    def _say(self, message: str, *, level: str = "info") -> None:
        """Emit a user-facing message: an editor notice in bridge mode, otherwise a
        plain print. Terminal output is unchanged when no bridge is attached."""
        if self.ui is not None:
            self.ui.info(message, level=level)
        else:
            print(message)

    def _summarization_enabled(self) -> bool:
        # The GLOBAL config is the durable source of truth (survives restarts), so it wins
        # over the per-session worktree state, which always defaults to "on" on a fresh
        # worktree and would otherwise shadow a persisted "off".
        gc_enabled = getattr(self.global_config, "summarization_enabled", None)
        if gc_enabled is not None:
            return bool(gc_enabled)
        state_enabled = getattr(self.state, "summarization_enabled", None)
        return True if state_enabled is None else bool(state_enabled)

    def _emit(self, event: dict) -> None:
        """Emit one machine-readable JSON event line for programmatic drivers (the
        VSCode chat extension). In bridge mode it goes through the bridge (one
        lock-serialized stdout channel); with plain ``--json-events`` it is printed.
        A no-op otherwise, so the human-readable shell output is untouched."""
        if self._bridge is not None:
            self._bridge.emit(event)
        elif self._json_events:
            print(json.dumps(event), flush=True)

    def _handle_agent_prompt(self, prompt: str) -> None:
        if prompt.startswith("/compact"):
            self._handle_pre_compaction()

        if self.actions.has_pre_agent_user_changes():
            if self.ui is not None:
                self.ui.info("User changes detected before the agent runs.")
            else:
                print("User changes detected before agent runs.")
            self.actions.create_user_commit()

        backend = self._backend()
        self.state.append_trace("user", prompt)
        result = backend.run(
            prompt,
            model=self.state.model,
            session_id=self.state.backend_session_id,
            commit_guidance=self._commit_guidance,
        )
        if result.session_id:
            self.state.backend_session_id = result.session_id
        if result.model and result.model != self.state.model:
            self.state.model = result.model
        if result.exit_code != 0:
            self.state.append_trace("agent", result.final_response or f"Backend exited with code {result.exit_code}")
            self.state.add_token_usage(result.tokens)
            self._emit(
                {
                    "type": "error",
                    "message": result.final_response or f"Backend exited with code {result.exit_code}",
                    "exit_code": result.exit_code,
                }
            )
            if self.verbose:
                print(f"Backend exited with code {result.exit_code}; no automatic agent commit was made.")
            return

        self.state.append_trace("agent", result.final_response)
        self.state.add_token_usage(result.tokens)
        self._emit(
            {
                "type": "response",
                "text": result.final_response,
                "session": self.state.backend_session_id,
                "model": self.state.model,
            }
        )
        self.repo.add_tracked()
        self.actions.review_untracked(include_declined=False)
        if self.repo.has_staged_changes():
            from agitrack.commits import build_agent_commit_message, render_interaction_trace, summary_metadata_lines
            from agitrack.summaries import Summarizer

            # The summary is built from ONLY the interaction trace appended to the
            # commit (the same text the commit carries), and nothing else — so
            # render it now, before clear_trace below.
            trace_text = render_interaction_trace(self.state.pending_trace(), self.state.trace_turn_limit)
            commit_summary = None
            summary_metadata = None
            summarizer_model = self.state.summarization_model or self.global_config.summarization_model
            if self._summarization_enabled():
                # Shell mode is synchronous per prompt, so summarizing inline is
                # fine — but say so, since the LLM call can take a while.
                print("aGiTrack is summarizing the changes before committing...")
                if self.ui is not None:
                    self.ui.info("Summarizing the changes before committing…")
                try:
                    summarizer = Summarizer(self._summarizer_backend(), model=summarizer_model)
                    commit_summary = summarizer.summarize_commit(trace=trace_text)
                    summary_metadata = summary_metadata_lines(
                        model=summarizer.model or self.state.model,
                        tokens_input=summarizer.tokens_input,
                        tokens_output=summarizer.tokens_output,
                        tokens_cache_read=summarizer.tokens_cache_read,
                    )
                except Exception as error:
                    if self.verbose:
                        print(f"Summarization failed: {error}")

            origin_event = self.state.session_origin_event()
            commit_sha = self.repo.commit(
                build_agent_commit_message(
                    latest_prompt=prompt,
                    trace=self.state.pending_trace(),
                    backend=result.backend,
                    backend_session_id=self.state.backend_session_id,
                    agitrack_session_id=self.state.session_id,
                    model=self.state.model,
                    reasoning_effort="on" if result.tokens.reasoning > 0 else None,
                    token_usage=self.state.pending_token_usage(),
                    trace_turn_limit=self.state.trace_turn_limit,
                    summary=commit_summary,
                    summary_metadata=summary_metadata,
                    origin_event=origin_event,
                )
            )
            if origin_event is not None:
                self.state.clear_session_origin_event()  # one-shot: surfaced once, then cleared
            self.state.clear_trace()

            if commit_summary and commit_sha:
                try:
                    self.repo.notes_add(commit_sha, commit_summary, namespace="agitrack/commit-summary")
                    new_session_summary = summarizer.update_session_summary(
                        current_summary=self.state.session_summary,
                        trace=trace_text,
                        commit_summary=commit_summary,
                    )
                    self.state.session_summary = new_session_summary
                    self.state.session_summary_commit = commit_sha
                    self.repo.notes_add(commit_sha, new_session_summary, namespace="agitrack/session-summary")
                except Exception as error:
                    if self.verbose:
                        print(f"Session summary update failed: {error}")

            print("Created <aGiTrack> commit.")
            self._emit({"type": "commit", "sha": commit_sha, "session": self.state.session_id})
        else:
            self._emit({"type": "no_changes"})
            if self.verbose:
                print("No code changes detected; interaction trace remains pending.")

    def _launch_command(self) -> list[str]:
        # Command that launches the current backend, replacing its executable with a user
        # wrapper. A per-run --backend-command override wins; otherwise the per-backend
        # config value applies. Empty ⇒ launch the backend binary directly.
        if self._backend_command:
            return list(self._backend_command)
        getter = getattr(self.global_config, "backend_command", None)
        return list(getter(self.state.backend)) if callable(getter) else []

    def _backend(self):
        backend_class = BACKENDS.get(self.state.backend)
        if backend_class is None:
            raise RuntimeError(f"Unsupported backend: {self.state.backend}")
        return backend_class(
            self.repo.repo,
            verbose=self.verbose,
            backend_args=self.backend_args,
            launch_command=self._launch_command() or None,
        )

    def _summarizer_backend(self):
        # Summarizer calls run from a scratch cwd, never the repo: a headless
        # run records a real backend session keyed by its working directory,
        # which would otherwise pollute the repo's session list and get picked
        # up as "the previous session" on resume (issues #8/#56).
        from agitrack.summaries import summary_scratch_dir

        backend_class = BACKENDS.get(self.state.backend)
        if backend_class is None:
            raise RuntimeError(f"Unsupported backend: {self.state.backend}")
        return backend_class(summary_scratch_dir(), verbose=self.verbose, launch_command=self._launch_command() or None)

    def _handle_pre_compaction(self) -> None:
        if self.verbose:
            print("aGiTrack: Capturing session summary before compaction...")
        try:
            from agitrack.summaries import Summarizer

            model = self.state.summarization_model or self.global_config.summarization_model
            summarizer = Summarizer(self._summarizer_backend(), model=model)
            session_id = self.state.backend_session_id
            if not session_id:
                return
            from agitrack.backends.proxy_agents import make_proxy_agent

            proxy_agent = make_proxy_agent(self.state.backend)
            exported = proxy_agent.export_session(self.repo.repo, session_id)
            if not exported or not exported.turns:
                return
            summary = summarizer.summarize_pre_compaction(
                exported_session=exported,
                current_summary=self.state.session_summary,
            )
            self.state.session_summary = summary
            head_sha = self.repo.rev_parse("HEAD")
            if head_sha:
                self.state.session_summary_commit = head_sha
                self.repo.notes_add(head_sha, summary, namespace="agitrack/session-summary")
            if self.verbose:
                print("aGiTrack: Session summary captured.")
        except Exception as error:
            if self.verbose:
                print(f"aGiTrack: Pre-compaction summary failed: {error}")

    def _print_help(self) -> None:
        print("Commands:")
        print("  :help              show this help")
        print("  :status            show git status")
        print("  :user-commit       create a user commit")
        print("  :stage             review and stage untracked files")
        print("  :unstaged          show intentionally unstaged files")
        print(f"  :agent-backend <{'|'.join(BACKENDS)}> select the agent backend")
        print("  :summarizer [on|off|model|status]")
        print("                     manage summarization (on/off, set model, show status)")
        print("  :exit              exit")
        print("Backend / commands are not reserved by aGiTrack and are sent to the backend.")

    def _prompt_state(self) -> PromptState:
        existing = [path for path in self.state.declined_untracked() if (self.repo.repo / path).exists()]
        return PromptState(
            repo=self.repo.repo,
            backend=self.state.backend,
            model=self.state.model,
            declined_count=len(existing),
            verbose=self.verbose,
        )
