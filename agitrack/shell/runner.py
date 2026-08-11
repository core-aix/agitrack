from __future__ import annotations

import json
import sys

from agitrack.commits import AgitrackActions
from agitrack.backends.setup import BackendUnavailable, backend_installed, ensure_installed_backend, install_hint
from agitrack.backends import headless_backends
from agitrack.git import GitRepo
from agitrack.config import GlobalConfig
from agitrack.git import RepoLock, already_running_message
from agitrack.config import AgitrackState
from agitrack.shell.ui import AgitrackPrompt, PromptState


AGITRACK_PREFIX = ":"

# The headless adapters, from the single registry in agitrack.backends — so a newly registered
# backend is drivable from shell mode without editing this module. Kept as a module-level dict
# because tests monkeypatch it to substitute fakes.
BACKENDS = headless_backends()


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
        log_file: str | None = None,
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
        # WHERE HUMAN TEXT GOES. With --json-events (and no bridge), stdout is a machine stream:
        # one JSON object per line, nothing else. It was not — the privacy banner, the `> `
        # prompt echo, "Staged untracked files:" and "aGiTrack is summarizing…" were all
        # interleaved into it, and the `> ` marker (written with no newline) glued itself onto
        # the very event carrying the agent's answer, making it unparseable. Prose goes to
        # stderr in that mode, where a driver can still show or ignore it.
        self._human = sys.stderr if (json_events and not ui_bridge) else sys.stdout
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
        # An explicit --backend seeds the state's default so a brand-new repo (no stored
        # backend) resolves to it rather than to the configured default, which may be unset
        # (there is no hardcoded fallback).
        self.state = AgitrackState(repo.repo, default_backend=backend or self.global_config.default_backend)
        # NOTHING BELOW MAY REACH DISK YET. The repo lock is only taken in run(), and this
        # constructor runs even when the run is about to be refused because another aGiTrack
        # (a background tracker, a live TUI) already holds the repo. Writing here rewrote that
        # process's state.json — a new agitrack session id, a different conversation pointer,
        # a cleared watermark — and its own long-lived copy then reverted ours a poll later:
        # a lost update in both directions from a run that never started. Kept in memory and
        # flushed by run()'s save(), after the lock is held.
        self._switch_to_backend: str | None = None
        with self.state.suspend_saves():
            if backend and backend in BACKENDS and backend != self.state.backend:
                self._switch_to_backend = backend
                self.state.remember_backend_session()
                self.state.backend = backend
                self.state.backend_session_id = self.state.stored_backend_session(backend)
                self.state.last_backend_message_id = None
                # A MODEL BELONGS TO ITS BACKEND. `state.model` is the model the last turn ran
                # under; carried across a switch it made the new backend's commits claim the old
                # backend's model ("backend: codex, model: claude-haiku-4-5-…"), which the
                # dashboard then groups under that model. Worse, it is also what aGiTrack
                # re-pins on the command line, so the switched-to CLI is asked to run a model id
                # it does not have. Restore whatever this backend last ran under, or nothing —
                # the backend reports its own.
                self.state.model = (self.state.recall_session(backend) or {}).get("model")
            if new_session:
                self.state.backend_session_id = None
                self.state.last_backend_message_id = None
                self.state.new_agitrack_session_id()
        self.verbose = verbose
        self.prompt = AgitrackPrompt(self._prompt_state, human_stream=self._human)
        self.actions = AgitrackActions(repo, self.state, verbose=verbose, interactive=self.interactive, ui=self.ui)
        self.management_lock = RepoLock(repo.repo / ".agitrack" / "lock")
        # --log-file used to be parsed, passed to the background tracker and the proxy runner,
        # and then simply DROPPED here: this constructor took no log_file argument at all, so
        # `--prompt`/`--json` runs — including ones that made real commits — created no log file
        # anywhere, no warning, exit 0, while `--help` said verbatim "Works in every mode".
        from agitrack.events import EventLog, exclude_log_file, resolve_log_path

        self.events = EventLog(resolve_log_path(log_file, repo.repo))
        exclude_log_file(repo.repo, self.events.path)

    def run(self) -> int:
        """Run the shell, returning a PROCESS EXIT CODE (0 = ran, 1 = could not start).

        The code matters because this is the scripted entry point (`agitrack --json --prompt`):
        a run that never started — no backend installed, another aGiTrack holding the repo —
        exited 0 and looked to any calling script exactly like a successful turn.
        """
        try:
            resolved = ensure_installed_backend(self.state.backend, self.global_config, interactive=self.interactive)
        except BackendUnavailable as error:
            print(error, file=self._human)
            if self._bridge is not None:
                self._bridge.emit({"type": "error", "message": str(error)})
                self._bridge.emit({"type": "bye"})
            return 1
        if not self.management_lock.acquire():
            message = already_running_message(
                self.management_lock.owner_pid(), repo_root=getattr(self.repo, "repo", None)
            )
            print(message, file=self._human)
            if self._bridge is not None:
                self._bridge.emit({"type": "error", "message": message})
                self._bridge.emit({"type": "bye"})
            return 1
        # Only now — with the lock held, so no other aGiTrack owns this repo's state — do the
        # startup decisions made in __init__ become durable (see the note there).
        if resolved != self.state.backend:
            self.state.backend = resolved
        if self._switch_to_backend:
            self.global_config.default_backend = self._switch_to_backend
        self.state.save()
        if self.verbose:
            print(f"aGiTrack session {self.state.session_id}", file=self._human)
            print(f"Repository: {self.repo.repo}", file=self._human)
            print(f"Backend: {self.state.backend}", file=self._human)
            print("Type :help for aGiTrack commands. Backend / commands are passed through.", file=self._human)
        try:
            if self._bridge is not None:
                self._run_bridge()
                return 0
            if self.prompts is not None:
                self._run_scripted(self.prompts)
                return 0
            while True:
                try:
                    text = self.prompt.prompt().strip()
                except (EOFError, KeyboardInterrupt):
                    print(file=self._human)
                    return 0
                if not text:
                    continue
                if text.startswith(AGITRACK_PREFIX):
                    if self._handle_command(text):
                        return 0
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
            print(f"> {text}", file=self._human)
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
            print(self.repo.status_short() or "Working tree clean", file=self._human)
        elif command == ":agent-backend":
            agent = arg.strip()
            if agent not in BACKENDS:
                print(
                    f"Unknown backend: {agent or '(none)'}. Available: {', '.join(sorted(BACKENDS))}", file=self._human
                )
            elif not backend_installed(agent):
                print(f"'{agent}' is not installed.", file=self._human)
                print(install_hint(agent), file=self._human)
            else:
                self.state.remember_backend_session()
                self.state.backend = agent
                self.global_config.default_backend = agent
                self.state.backend_session_id = self.state.stored_backend_session(agent)
                self.state.last_backend_message_id = None
                print(f"Backend set to {agent}", file=self._human)
        elif command == ":user-commit":
            self.actions.create_user_commit()
        elif command == ":unstaged":
            declined = self.state.declined_untracked()
            if declined:
                print("Intentionally unstaged files:", file=self._human)
                for path in declined:
                    print(f"  {path}", file=self._human)
            else:
                print("No intentionally unstaged files.", file=self._human)
        elif command == ":stage":
            self.actions.review_untracked(include_declined=True)
        elif command == ":summarizer":
            self._handle_summarizer_command(arg.strip())
        else:
            print(f"Unknown command: {command}", file=self._human)
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
                print(f"Current summarizer model: {current}", file=self._human)
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
            print(message, file=self._human, flush=True)

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
                print("User changes detected before agent runs.", file=self._human)
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
            failure = f"Backend exited with code {result.exit_code}"
            message = result.final_response or failure
            self.state.append_trace("agent", message)
            self.state.add_token_usage(result.tokens)
            self._emit({"type": "error", "message": message, "exit_code": result.exit_code})
            # ALWAYS say something. This used to print only under --verbose, so a failed turn in
            # the default json/scripted mode was completely invisible: the prompt echoed, nothing
            # came back, no commit was made, and the process still exited 0. Whatever the backend
            # said about the failure (a quota message, a rejected model id) is the useful part, so
            # lead with it and let the exit code follow.
            lead = "" if message == failure else f"{message}\n"
            self._say(f"{lead}{failure}; no automatic agent commit was made.", level="warn")
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
        # PRINT THE REPLY. `_emit` is a no-op without --json-events, so a plain
        # `agitrack --prompt "…"` showed the privacy banner and the echoed prompt and then
        # nothing at all — the agent's answer appeared nowhere on screen (it went only into
        # state.json and the commit trace). On claude that meant a turn whose entire content was
        # "I need permission to create the file" looked like a silent success.
        if result.final_response and self.ui is None and not self._json_events:
            print(result.final_response, file=self._human)
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
                print("aGiTrack is summarizing the changes before committing...", file=self._human)
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
                        print(f"Summarization failed: {error}", file=self._human)

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
            self.events.emit(
                "commit", sha=(commit_sha or "")[:12], type="agent", backend=result.backend, subject=commit_summary
            )

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
                        print(f"Session summary update failed: {error}", file=self._human)

            print("Created <aGiTrack> commit.", file=self._human)
            self._emit({"type": "commit", "sha": commit_sha, "session": self.state.session_id})
        else:
            self._emit({"type": "no_changes"})
            # ALWAYS say it, not only under --verbose. A turn that produced no file change is
            # exactly the case a scripted caller must be able to see: it is indistinguishable
            # from a successful one otherwise, and it is what a declined permission, a refusal,
            # or a pure question all look like from the outside. The exit code stays 0: the turn
            # genuinely ran and the agent genuinely answered — "changed nothing" is a legitimate
            # outcome for a question, and the `no_changes` event plus this line are what make it
            # visible. A turn that FAILED still returns non-zero, above.
            print("No code changes were made; the interaction trace remains pending.", file=self._human)

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
            print("aGiTrack: Capturing session summary before compaction...", file=self._human)
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
                print("aGiTrack: Session summary captured.", file=self._human)
        except Exception as error:
            if self.verbose:
                print(f"aGiTrack: Pre-compaction summary failed: {error}", file=self._human)

    def _print_help(self) -> None:
        print("Commands:", file=self._human)
        print("  :help              show this help", file=self._human)
        print("  :status            show git status", file=self._human)
        print("  :user-commit       create a user commit", file=self._human)
        print("  :stage             review and stage untracked files", file=self._human)
        print("  :unstaged          show intentionally unstaged files", file=self._human)
        print(f"  :agent-backend <{'|'.join(BACKENDS)}> select the agent backend", file=self._human)
        print("  :summarizer [on|off|model|status]", file=self._human)
        print("                     manage summarization (on/off, set model, show status)", file=self._human)
        print("  :exit              exit", file=self._human)
        print("Backend / commands are not reserved by aGiTrack and are sent to the backend.", file=self._human)

    def _prompt_state(self) -> PromptState:
        existing = [path for path in self.state.declined_untracked() if (self.repo.repo / path).exists()]
        return PromptState(
            repo=self.repo.repo,
            backend=self.state.backend,
            model=self.state.model,
            declined_count=len(existing),
            verbose=self.verbose,
        )
