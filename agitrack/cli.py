from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from agitrack.backends.setup import select_default_backend, select_default_summarizer_model
from agitrack.backends.proxy_agents import available_backends
from agitrack.git import GitError, GitRepo, RepoLock, already_running_message
from agitrack.config import GlobalConfig, settings
from agitrack.shell import AgitrackShell

try:
    # The proxy drives the agent through a (Con)PTY. Imported at module level so tests and
    # the launch path reference ``cli.ProxyRunner`` directly, but tolerant of a platform
    # where the proxy's platform layer can't load yet — the headless paths (json mode,
    # dashboard, --version) don't need it, and proxy mode reports it cleanly below.
    from agitrack.proxy import ProxyRunner
except ImportError:  # pragma: no cover - only when the proxy platform layer is unavailable
    ProxyRunner = None  # type: ignore[assignment,misc]

_BACKEND_COMMANDS = {
    "claude": "claude",
    "opencode": "opencode",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Interactive agent + git commit orchestration.",
        add_help=False,
    )
    parser.add_argument(
        "-h",
        "--help",
        action="store_true",
        help="show this help message and exit",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the aGiTrack version and exit",
    )
    parser.add_argument("--repo", default=".", help="target Git repository path")
    parser.add_argument("--verbose", action="store_true", help="show aGiTrack diagnostic messages")
    parser.add_argument("--mode", choices=["proxy", "json"], default="proxy", help="interactive mode")
    parser.add_argument(
        "--prompt",
        action="append",
        dest="prompts",
        metavar="TEXT",
        help="run this prompt non-interactively (implies --mode json) and exit; "
        "repeatable, prompts run in order. Lines starting with ':' are aGiTrack "
        "commands, e.g. --prompt ':status'",
    )
    parser.add_argument(
        "-d",
        "--dashboard",
        nargs="?",
        const="html",
        choices=["text", "html", "stop", "status"],
        default=None,
        help="show repository metrics computed from aGiTrack commit metadata "
        "(coverage, AI / human / non-tracked line changes, tokens, per-backend/"
        "model/committer breakdowns, loop detection). Bare or `html` starts a "
        "filterable, auto-refreshing dashboard as a background daemon on localhost, "
        "opens it in the browser, and returns to the shell; the daemon stops when "
        "this terminal closes or via `-d stop`. `status` reports it; `text` prints a "
        "one-shot report and exits",
    )
    parser.add_argument(
        "--backend",
        choices=available_backends(),
        default=None,
        help="agent backend to use; also saved as the global default",
    )
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="start a fresh backend conversation instead of resuming the last one",
    )
    parser.add_argument(
        "--no-worktree",
        action="store_true",
        help="run the agent against the current branch instead of an isolated worktree "
        "(edits are visible live; no isolation/integration; unsafe with concurrent sessions)",
    )
    parser.add_argument(
        "--no-commit-guidance",
        action="store_true",
        help="do not tell the coding agent that aGiTrack handles commits; by default aGiTrack "
        "appends a note to the agent's system prompt (where the backend supports it) so the "
        "agent does not create its own git commits unless you explicitly ask",
    )
    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help="do not confine the agent's writes to its session worktree; by default aGiTrack "
        "sandboxes the backend so it can only write inside its worktree (plus .git). Also "
        "settable via 'sandbox' in config.",
    )
    parser.add_argument(
        "--allowed-edit-paths",
        default=None,
        metavar="PATH[:PATH...]",
        help="extra paths the sandbox lets the agent write to, beyond its worktree — "
        "multiple paths separated by '%s' (like PATH). Also settable via "
        "allowed_edit_paths in config." % os.pathsep,
    )
    parser.add_argument(
        "--backend-command",
        default=None,
        metavar="COMMAND",
        help="custom command used to launch the backend agent, replacing the backend "
        "executable so a wrapper can sit beneath aGiTrack — e.g. "
        "--backend-command 'somewrapper claude'. Split like a shell command; it must "
        "ultimately exec the chosen backend (aGiTrack's own sandbox wrapper still goes "
        "on top). Also settable via backend_command in config (a string, or an object "
        "keyed by backend name).",
    )
    parser.add_argument(
        "--delay-merge",
        action="store_true",
        help="don't merge a turn's committed changes into the base branch automatically; "
        "instead leave them in the session's working directory for you to review/edit, then "
        "merge on your confirmation via the session menu. Off by default.",
    )
    parser.add_argument(
        "--json-events",
        action="store_true",
        help="in --mode json, emit one machine-readable JSON line per turn event "
        "(the agent's response, the commit produced, errors) — used by the VSCode "
        "chat extension and other programmatic drivers",
    )
    parser.add_argument(
        "--ui-bridge",
        action="store_true",
        help="in --mode json, run a long-lived JSON-RPC session over stdin/stdout where "
        "interactive questions (menus, confirmations, text input) are asked of the driver "
        "instead of a terminal — used by the VSCode extension (see editors/vscode)",
    )
    parser.add_argument(
        "--full-agent-messages",
        action="store_true",
        help="record every user-facing message the agent sends during a turn in the "
        "commit's interaction trace, not just the final reply (tool calls and file edits "
        "are still excluded); also settable per-repo via full_agent_messages in config",
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help="finalize work left by a session that exited abruptly (e.g. the VSCode window "
        "was closed mid-turn): for each session worktree, commit a finished turn's "
        "uncommitted changes and merge it into the base branch (skipping the merge on a "
        "conflict). Runs headlessly and no-ops if a live aGiTrack holds the repo lock. Used "
        "by the VSCode extension on close; also runnable manually.",
    )
    parser.add_argument(
        "--skip-privacy-ack",
        action="store_true",
        # Suppress the one-time privacy warning/acknowledgment. Set automatically
        # when aGiTrack re-execs itself after an in-app (menu) update — the user already
        # acknowledged it this session — and not meant for manual use.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--dashboard-serve",
        action="store_true",
        # Internal: run the metrics dashboard HTTP server in the foreground (this
        # process). `agitrack -d` and the TUI's Ctrl-G dashboard spawn aGiTrack with this
        # flag to host the dashboard in a separate, lifecycle-bound child process (#110).
        # Not meant for manual use.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--dashboard-owner-pid",
        type=int,
        default=None,
        # Internal: pid the --dashboard-serve child watches (the launching shell for
        # `agitrack -d`, the TUI for the Ctrl-G dashboard). The child shuts itself down
        # when that pid dies, so the dashboard never outlives whatever launched it —
        # even on SIGKILL, which leaves no chance to stop us first.
        help=argparse.SUPPRESS,
    )
    parser.epilog = (
        "Unrecognized arguments are forwarded verbatim to the backend CLI "
        "(claude / opencode), e.g. `agitrack --backend opencode --port 12345`. Use "
        "`--` to forward arguments that aGiTrack also defines or a bare prompt, e.g. "
        '`agitrack -- --verbose "fix the bug"`. To see the backend CLI\'s own help, run '
        "`agitrack -- --help` (or invoke the backend directly)."
    )
    # parse_known_args so backend-specific flags pass through instead of erroring.
    args, backend_args = parser.parse_known_args(argv)
    # argparse leaves a single leading "--" separator in the remainder; drop it.
    if backend_args and backend_args[0] == "--":
        backend_args = backend_args[1:]

    # First run: ask the user to choose a default backend before launching.
    config = GlobalConfig()

    # Handle help request before any other processing. Show ONLY aGiTrack's own options —
    # not the backend's help (that is available via `agitrack -- --help`, handled below).
    if args.help:
        parser.print_help()
        return 0

    # Print the version and exit. Kept simple and side-effect-free (no repo
    # discovery, no privacy prompt) so tools — e.g. the VSCode extension checking
    # whether the installed CLI has self-updated past it — can read it cheaply.
    if args.version:
        from agitrack import __version__

        print(__version__)
        return 0

    if args.dashboard_serve:
        # Internal entry point: the detached dashboard child process. `agitrack -d`
        # (and the TUI's Ctrl-G dashboard) spawn this to run the read-only HTTP server
        # out-of-process (#110); it shuts down when its owner pid dies. No privacy
        # prompt / update check (read-only).
        try:
            serve_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError) as error:
            print(error)
            return 1
        from agitrack.metrics.daemon import EMAIL_LOGINS_ENV, run_dashboard_daemon

        email_logins: dict[str, str] = {}
        raw_logins = os.environ.get(EMAIL_LOGINS_ENV)
        if raw_logins:
            try:
                parsed = json.loads(raw_logins)
                if isinstance(parsed, dict):
                    email_logins = {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError:
                pass
        return run_dashboard_daemon(serve_repo, owner_pid=args.dashboard_owner_pid, email_logins=email_logins)

    if args.dashboard:
        # Read-only: nothing is logged or committed, so no privacy
        # acknowledgment and no repo initialization offer.
        try:
            dashboard_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError) as error:
            # OSError: --repo points at a directory that does not exist.
            print(error)
            return 1
        if args.dashboard == "text":
            from agitrack.metrics import render_dashboard

            print(render_dashboard(dashboard_repo))
            return 0
        if args.dashboard == "stop":
            from agitrack.metrics import stop_dashboard_daemon

            return stop_dashboard_daemon(dashboard_repo)
        if args.dashboard == "status":
            from agitrack.metrics import dashboard_daemon_status

            return dashboard_daemon_status(dashboard_repo)
        # Bare `-d` / `-d html`: start the live dashboard as a background daemon owned
        # by the launching shell, so the terminal is freed and the daemon dies when
        # that shell/terminal closes (#110).
        from agitrack.metrics import start_dashboard_daemon

        return start_dashboard_daemon(dashboard_repo, owner_pid=os.getppid())

    if args.recover:
        # Headless finalization of work left by a session that exited abruptly.
        # It only commits/merges already-produced changes (never starts new agent
        # work), so — like --dashboard — no privacy prompt and no update check. It
        # takes the repo lock itself and no-ops if a live aGiTrack holds it.
        try:
            recover_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError) as error:
            print(error)
            return 1
        from agitrack.config.migrate import migrate_repo_state
        from agitrack.recovery import RecoveryService

        migrate_repo_state(recover_repo)
        print(RecoveryService(recover_repo, config).recover().summary())
        return 0

    # If backend is asked for help, run it directly without TUI.
    if backend_args and any(arg in ("--help", "-h") for arg in backend_args):
        backend = args.backend or config.default_backend
        if not backend:
            print("Error: No backend selected. Use --backend to specify one.")
            return 1
        backend_cmd = _BACKEND_COMMANDS.get(backend)
        if not backend_cmd:
            print(f"Error: Unknown backend '{backend}'.")
            return 1
        launch, launch_error = _resolve_backend_command(args.backend_command, config, backend)
        if launch_error:
            print(launch_error)
            return 1
        # Launch under the configured wrapper if any (so `agitrack -- --help` shows the
        # backend's help exactly as it runs); otherwise verify the bare binary is on PATH.
        head = launch or [backend_cmd]
        if not launch and not shutil.which(backend_cmd):
            print(f"Error: Backend '{backend}' not found on PATH.")
            return 1
        result = subprocess.run(head + backend_args, check=False)
        return result.returncode

    scripted = bool(args.prompts)
    if scripted:
        args.mode = "json"  # --prompt drives the non-interactive shell (#53)
    if args.ui_bridge:
        args.mode = "json"  # the bridge is a json-mode transport (the VSCode extension)

    # Give immediate feedback that aGiTrack is launching — the interactive TUI takes a
    # few seconds to come up (update check, backend startup) and otherwise the terminal
    # looks frozen. Printed for interactive proxy mode only, so it never pollutes the
    # machine-readable json/bridge output or the cheap --version/--dashboard paths. Shown
    # however aGiTrack was started (terminal or VSCode), then replaced by the TUI frame.
    if args.mode == "proxy":
        print("aGiTrack is starting...", flush=True)

    # Offer a self-update before launching anything. Skipped for scripted/non-TTY
    # runs (no way to answer) and when the user turned update checks off. If the
    # user accepts, aGiTrack updates and re-execs immediately — no sessions are
    # running yet at startup, so there is nothing to finalize first.
    if not scripted and sys.stdin.isatty() and sys.stdout.isatty():
        _check_for_update_at_startup(config)

    if (
        args.backend is None
        and not config.has_default_backend()
        and not scripted
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    ):
        chosen_backend = select_default_backend(config)
        # First run also picks the default summarizer model, saved to the global config.
        select_default_summarizer_model(config, chosen_backend)

    if backend_args:
        _warn_reserved_passthrough(args.backend or config.default_backend, backend_args)

    try:
        repo = _discover_or_init(Path(args.repo).expanduser())
    except OSError as error:
        # --repo points at a directory that does not exist / can't be read.
        print(error)
        return 1
    if repo is None:
        return 1

    # Migrate a pre-rename ``.agit/`` state dir (and its worktrees) to ``.agitrack/``
    # before anything reads state, so existing sessions survive the upgrade.
    from agitrack.config.migrate import migrate_repo_state

    migrate_repo_state(repo)

    # Now that we know the repo, layer its local settings (.agitrack/config.json) over
    # the global config, then resolve the effective settings. A CLI flag always wins;
    # otherwise the (repo-overlaid) config value applies. getattr keeps a config written
    # before these keys existed (or a partial stub) working with the defaults.
    getattr(config, "load_repo_overlay", lambda _root: None)(repo.repo)
    use_worktrees = False if args.no_worktree else config.use_worktrees
    commit_guidance = False if args.no_commit_guidance else getattr(config, "commit_guidance", True)
    sandbox_enabled = False if args.no_sandbox else getattr(config, "sandbox", True)
    if args.allowed_edit_paths is not None:
        allowed_edit_paths = [p for p in args.allowed_edit_paths.split(os.pathsep) if p.strip()]
    else:
        allowed_edit_paths = getattr(config, "allowed_edit_paths", [])
    # Resolve the backend this run will use: an explicit --backend wins, else the
    # (repo-overlaid) configured default. There is no hardcoded fallback — if neither
    # exists we must not silently pick an agent. The interactive first-run prompt above
    # fills in a default when possible; reaching here without one means a non-interactive
    # run (scripted / no TTY) with nothing configured, so fail clearly instead.
    effective_backend = args.backend or config.default_backend
    if not effective_backend:
        print(
            "No coding agent backend is configured. Run aGiTrack in an interactive "
            "terminal to choose a default, or pass --backend <claude|opencode>."
        )
        return 1
    # Resolve the backend launch wrapper (--backend-command, else config) for the backend
    # this run will use. Validate the flag here so a malformed value fails fast and clearly.
    backend_command, backend_command_error = _resolve_backend_command(args.backend_command, config, effective_backend)
    if backend_command_error:
        print(backend_command_error)
        return 1
    if not _confirm_backend_command_mismatch(effective_backend, backend_command, scripted=scripted):
        print("aGiTrack not started.")
        return 1

    # Take the single-writer lock up front — BEFORE the privacy prompt — and hold it
    # for the whole session. Besides refusing a second instance immediately, this
    # makes the lock (carrying our PID) present from the very start, so a session
    # still sitting at this privacy prompt is already "locked". The VSCode extension
    # reads this lock to tell a starting/running session apart from a dead shell;
    # holding it from the start is what lets the aG button reliably focus the existing
    # terminal instead of opening a second one. (It was a read-only probe before, so
    # no lock was held during startup and the extension couldn't yet see the session.)
    management_lock = RepoLock(repo.repo / ".agitrack" / "lock")
    if not management_lock.acquire():
        print(already_running_message(management_lock.owner_pid()))
        return 1

    if not _acknowledge_privacy_warning(scripted=scripted, skip=args.skip_privacy_ack):
        management_lock.release()
        return 1

    try:
        if args.mode == "json":
            management_lock.release()  # json/scripted mode runs via AgitrackShell, which takes its own lock
            AgitrackShell(
                repo,
                verbose=args.verbose,
                backend=args.backend,
                new_session=args.new_session,
                backend_args=backend_args,
                backend_command=backend_command,
                prompts=args.prompts,
                commit_guidance=commit_guidance,
                json_events=args.json_events,
                ui_bridge=args.ui_bridge,
            ).run()
        else:
            # Before the TUI takes over the terminal, check the GitHub CLI and let the
            # user install/log in or continue without it (the TUI would otherwise leave
            # no shell prompt to act on the gh warning).
            proceed, gh_handled = _check_gh_availability(repo, scripted=scripted)
            if not proceed:
                return 1
            # Warn about a menu key the host likely intercepts (e.g. VS Code's Ctrl-G) and
            # let the user test/replace it now — the only chance before the TUI takes over.
            if not _verify_menu_key(config, scripted=scripted):
                return 1
            if ProxyRunner is None:  # pragma: no cover - platform without proxy support
                print("The interactive aGiTrack TUI is not available on this platform yet.")
                return 1
            return ProxyRunner(
                repo,
                verbose=args.verbose,
                backend=args.backend,
                new_session=args.new_session,
                use_worktrees=use_worktrees,
                backend_args=backend_args,
                backend_command=backend_command,
                commit_guidance=commit_guidance,
                full_agent_messages=args.full_agent_messages,
                delay_merge=args.delay_merge,
                sandbox=sandbox_enabled,
                allowed_edit_paths=allowed_edit_paths,
                gh_prechecked=gh_handled,
                _lock=management_lock,
            ).run()
    except (GitError, RuntimeError) as error:
        print(error)
        return 1
    finally:
        management_lock.release()  # idempotent: run()/json mode already released on their own paths
    return 0


# Flags aGiTrack injects itself to manage session tracking; forwarding a duplicate
# can fight aGiTrack's own session handling. We warn but still forward — aGiTrack never
# silently swallows the user's intent.
_RESERVED_PASSTHROUGH = {
    "claude": {"--session-id", "--resume", "-r", "--continue", "-c"},
    "opencode": {"--session", "-s", "--continue", "-c"},
}


def _resolve_backend_command(
    flag_value: str | None, config: GlobalConfig, backend: str
) -> tuple[list[str], str | None]:
    """Resolve the command that launches the backend, replacing its executable with a
    user wrapper. The ``--backend-command`` flag (a shell-split string) wins; otherwise
    the per-backend ``backend_command`` config value applies. Returns ``(tokens, error)``
    — ``tokens`` empty means "launch the backend directly"; a non-None ``error`` is a
    user-facing message for a malformed flag (so the caller can stop)."""
    if flag_value is None:
        getter = getattr(config, "backend_command", None)
        return (list(getter(backend)) if callable(getter) else [], None)
    import shlex

    try:
        # posix=False on Windows so backslashes in paths (e.g. C:\tools\wrapper.exe) are
        # kept literally rather than treated as shell escapes.
        tokens = shlex.split(flag_value, posix=(os.name != "nt"))
    except ValueError as error:
        return ([], f"Invalid --backend-command {flag_value!r}: {error}")
    if not tokens:
        return ([], "Invalid --backend-command: the command is empty.")
    return (tokens, None)


def _confirm_backend_command_mismatch(backend: str, backend_command: list[str], *, scripted: bool) -> bool:
    """When the launch command clearly names a *different* known backend than the selected
    one (e.g. ``--backend claude --backend-command "wrap opencode"``), warn and require
    explicit confirmation before proceeding. aGiTrack tracks transcripts/sessions per the
    selected backend, so a wrapper that execs another backend silently breaks that tracking
    — the user must opt in. Returns True to proceed, False to abort.

    Only an unambiguous mismatch prompts — a known backend name appears in the command but
    the selected one does not. An opaque wrapper (no known backend named, e.g.
    ``mylauncher``) or a consistent command proceeds silently. Without a way to ask
    (scripted/non-interactive), the warning is printed and the run proceeds, since
    automation can't answer a prompt and must not hang on one."""
    if not backend_command:
        return True
    named = {os.path.basename(token) for token in backend_command}
    if backend in named:
        return True  # the command names the selected backend — consistent
    others = sorted(named & (set(available_backends()) - {backend}))
    if not others:
        return True  # opaque wrapper — don't guess which backend it runs
    print(
        f"Warning: --backend is '{backend}' but the launch command names "
        f"{', '.join(others)}. aGiTrack tracks sessions for '{backend}', so a wrapper "
        f"that runs a different backend will break session/transcript tracking. Pass "
        f"--backend {others[0]} (or set default_backend) if that's what you meant."
    )
    if scripted or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return True  # can't prompt here; proceed with the warning rather than hang automation
    # Drain any injected input first so a stray newline can't auto-confirm (same reason as
    # the privacy acknowledgment): this must be a deliberate keypress.
    _drain_terminal_input()
    try:
        answer = input(f"Proceed with backend '{backend}' anyway? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def _warn_reserved_passthrough(backend: str | None, backend_args: list[str]) -> None:
    reserved = _RESERVED_PASSTHROUGH.get(backend or "", set())
    hit = sorted({arg for arg in backend_args if arg in reserved})
    if hit:
        print(
            f"Warning: forwarding {', '.join(hit)} to {backend}; aGiTrack manages "
            "session selection itself, so this may interfere with its session tracking."
        )


def _check_for_update_at_startup(config: GlobalConfig) -> None:
    """At startup, check for a newer aGiTrack and, if one exists, prompt the user to
    install it now. Best-effort: any failure (no network, no upstream) is
    swallowed so it can never block launching aGiTrack."""
    # Skip when checks are off, or when the attribute is absent — a config that
    # doesn't carry the update preference (e.g. a test stub) has nothing to read
    # or persist, so there is no update flow to run.
    if not getattr(config, "check_for_updates", None):
        return
    try:
        from agitrack.update import STARTUP_NET_TIMEOUT, Updater, restart_agitrack

        updater = Updater()
        # Bound the launch-time check tightly so an offline / bad-connection user
        # isn't blocked from starting aGiTrack (the network call fails fast and we just
        # skip the offer below).
        status = updater.check(timeout=STARTUP_NET_TIMEOUT)
    except KeyboardInterrupt:
        # The check was slow (e.g. a sluggish `git fetch`) and the user pressed Ctrl-C.
        # Treat it as "skip the update check and get on with launching" — never dump a
        # traceback over a best-effort, optional check.
        print("Skipped the update check.")
        return
    except Exception:
        return
    # A prior automatic update may have failed (or the user chose not to retry). Clear
    # that reminder the moment aGiTrack is actually current; otherwise honour it below.
    pending = getattr(config, "pending_manual_update", None)
    if pending and status.ok and not status.available:
        config.pending_manual_update = None
        pending = None
    if not status.ok or not status.available:
        return
    if pending:
        # Don't re-run the interactive auto-update — it already failed. Show a single
        # startup reminder with how to update by hand; the user keeps running the
        # current version and can also retry via the Ctrl-G 'update' menu.
        print(f"\nReminder: {status.message}")
        print(
            f"The automatic update did not complete earlier. To update aGiTrack, {updater.manual_update_instructions()}"
        )
        return
    print(f"\n{status.message}")
    try:
        # Default (empty Enter) is to update — that's the recommended action, so
        # make it the path of least resistance and say so in the prompt.
        answer = input("Update aGiTrack now? [Y]es / [n]o / [never] ask again: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if answer in {"never", "no ask", "stop"}:
        config.check_for_updates = False
        print(
            "aGiTrack will no longer check for updates (re-enable with check_for_updates in ~/.agitrack/config.json)."
        )
        return
    if answer not in {"", "y", "yes"}:
        return
    print("Updating aGiTrack...")
    result = updater.apply()
    if not result.ok:
        # The automatic update failed. Keep aGiTrack running on the current version,
        # tell the user how to update by hand, and remember to remind (once) at the
        # next startup rather than nagging during the session.
        config.pending_manual_update = status.latest or status.current or "available"
        print(f"Update failed: {result.error}")
        print(f"aGiTrack will keep running the current version. To update it, {updater.manual_update_instructions()}")
        return
    config.pending_manual_update = None  # a successful update clears any prior reminder
    print(f"{result.message} Restarting aGiTrack...")
    restart_agitrack()  # does not return on success


# The privacy warning as one flowing paragraph; it is wrapped per terminal width at
# print time (see `_privacy_warning`) so it never overflows or is chopped mid-word on a
# narrow terminal.
_PRIVACY_WARNING_TEXT = (
    "WARNING: aGiTrack records the conversation in git commit messages — every "
    "message you enter in the chat can become part of the repository history. "
    "Do not enter passwords, API keys, or other sensitive information in the "
    "chat. (Keeping secrets out of prompts is good practice anyway.)"
)
# The width the warning is authored to wrap at on a normal/wide terminal; a narrower
# terminal wraps tighter than this so the text always fits.
_PRIVACY_WARNING_WIDTH = 73


def _privacy_warning(width: int | None = None) -> str:
    """The startup privacy warning, wrapped to fit the terminal. Wraps at the authored
    width on a normal/wide terminal, but re-wraps at the terminal's actual width when it is
    narrower, so the line breaks land in different places (and nothing overflows) on a small
    terminal. ``width`` defaults to the detected terminal width. Keeps the leading blank
    line the message has always had."""
    import textwrap

    if width is None:
        width = shutil.get_terminal_size().columns
    wrap_at = max(20, min(_PRIVACY_WARNING_WIDTH, width))
    return "\n" + textwrap.fill(_PRIVACY_WARNING_TEXT, width=wrap_at)


def _drain_terminal_input() -> None:
    """Discard any unread bytes in the controlling terminal's input queue (POSIX tty).

    A no-op when stdin isn't a real tty or termios is unavailable. Used before an
    interactive acknowledgment so input injected into the terminal by something other
    than the user (an editor's shell integration, a venv-activation hook) can't answer
    it. Best-effort: never raise."""
    try:
        import termios

        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


def _acknowledge_privacy_warning(*, scripted: bool = False, skip: bool = False) -> bool:
    """Show the privacy warning at startup; the user must acknowledge it to
    continue. Without a TTY there is no way to acknowledge, and a scripted run
    (``--prompt``) already has its input on the command line, so in both cases
    the warning is printed and aGiTrack proceeds — never block automation on an
    ``input()`` that cannot be answered.

    ``skip`` suppresses the warning entirely; aGiTrack sets it when re-exec'ing
    itself after an in-app (menu) update, where the user acknowledged the
    warning earlier this session and should not be prompted again."""
    if skip:
        return True
    print(_privacy_warning())
    if scripted or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return True
    # Discard anything already sitting in the terminal's input queue before reading, so a
    # stray newline can't auto-acknowledge this. Editors that host aGiTrack in a terminal
    # (e.g. the VSCode extension) — or their shell-integration / venv-activation hooks —
    # can inject a command whose trailing Enter would otherwise answer this prompt for the
    # user. The acknowledgment must be a deliberate keypress, so flush first.
    _drain_terminal_input()
    try:
        answer = input("Press Enter to acknowledge and continue (q to quit): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\naGiTrack not started.")
        return False
    if answer in {"q", "quit", "n", "no"}:
        print("aGiTrack not started.")
        return False
    return True


def _check_gh_availability(repo: GitRepo, *, scripted: bool = False) -> tuple[bool, bool]:
    """Before the TUI takes over the terminal, check the GitHub CLI (``gh``) and let the
    user act on it. aGiTrack uses ``gh`` for the dashboard's committer identities and for
    session sharing; once the full-screen TUI starts there is no shell prompt left to run
    ``gh auth login`` in, so we surface it here while stdin is still an ordinary terminal.

    Only prompts when ``gh`` is missing or not signed in **and** the repo has a GitHub
    remote (where ``gh`` actually matters) — a local-only / non-GitHub repo is never nagged.
    Offers to log in inline (``gh auth login`` runs right here) or continue without it.

    Returns ``(proceed, handled)``: ``proceed`` is False only when the user chose to quit;
    ``handled`` is True when the interactive prompt was shown, so the runner can skip its
    own in-TUI gh notice. Never blocks automation — without an interactive TTY (or in
    scripted mode) it does nothing and returns ``(True, False)``."""
    if scripted or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return (True, False)
    from agitrack.metrics.github import commit_url_base, gh_status

    status = gh_status()
    if status == "ok":
        return (True, False)  # installed and authenticated — nothing to do
    if not commit_url_base(repo):
        return (True, False)  # no GitHub remote — gh isn't needed here yet
    if status == "missing":
        print(
            "GitHub CLI (gh) isn't installed. aGiTrack uses it for the dashboard's committer\n"
            "identities and for session sharing; without it those features are limited.\n"
            "Install it from https://cli.github.com, then restart aGiTrack."
        )
        prompt = "Press Enter to continue without it (q to quit): "
    else:  # unauthenticated
        print(
            "GitHub CLI (gh) isn't signed in. aGiTrack uses it for the dashboard's committer\n"
            "identities and for session sharing; without it those features are limited."
        )
        prompt = "Press Enter to continue, type 'l' to log in now (q to quit): "
    # Drain injected input first so a stray newline can't auto-answer this (same reason as
    # the privacy acknowledgment) — the choice must be a deliberate keypress.
    _drain_terminal_input()
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\naGiTrack not started.")
        return (False, True)
    if answer in {"q", "quit"}:
        print("aGiTrack not started.")
        return (False, True)
    if status == "unauthenticated" and answer in {"l", "login", "log in"}:
        _run_gh_login()
    return (True, True)


def _run_gh_login() -> None:
    """Run ``gh auth login`` interactively in the current terminal (we are still in cooked
    mode, before the TUI). Best-effort: any failure is reported and aGiTrack continues."""
    try:
        subprocess.run(["gh", "auth", "login"], check=False)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"Could not run `gh auth login`: {error}")


def _verify_menu_key(config: GlobalConfig, *, scripted: bool = False) -> bool:
    """Before the TUI starts, warn when the configured menu key is likely intercepted by
    the host (e.g. VS Code binds Ctrl-G to "Go to Line"), and let the user test it, switch
    to another key, or keep it. The menu can't be opened from inside the TUI if its key
    doesn't reach aGiTrack, so this is the one chance to fix it while a shell prompt exists.

    Returns True to proceed, False to abort. A changed key is persisted to the global
    config (which the runner re-reads). Never blocks automation — without an interactive
    TTY, or in scripted mode, it does nothing and returns True."""
    if scripted or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return True
    key = config.menu_key
    conflict = settings.detect_menu_key_conflict(key, os.environ)
    if conflict is None:
        return True  # no known conflict — don't bother the user
    if config._raw("menu_key_acknowledged") == key:
        return True  # the user already resolved/confirmed this key in this environment
    label = settings.menu_key_label_for(key)
    print(f"\nHeads up: aGiTrack's menu key is {label}, but {conflict}")
    print("Once the TUI starts you can't open the aGiTrack menu if that key never arrives.")
    while True:
        try:
            answer = (
                input(f"[t] test {label} now   [c] choose a different key   [Enter] keep it   [q] quit: ")
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            print("\naGiTrack not started.")
            return False
        if answer in {"q", "quit"}:
            print("aGiTrack not started.")
            return False
        if answer in {"", "k", "keep"}:
            config.set("menu_key_acknowledged", key, scope="global")  # don't nag next launch
            return True
        if answer in {"t", "test"}:
            _run_menu_key_test(key)
            continue
        if answer in {"c", "change", "choose"}:
            chosen = _choose_menu_key(config, key)
            if chosen is None:
                continue  # backed out — re-show the options
            config.set("menu_key", chosen, scope="global")
            config.set("menu_key_acknowledged", chosen, scope="global")
            print(f"Menu key set to {settings.menu_key_label_for(chosen)}.")
            return True
        print("Please choose t, c, Enter, or q.")


def _choose_menu_key(config: GlobalConfig, current: str) -> str | None:
    """Prompt for a replacement menu key, offering known-good suggestions and an optional
    test. Returns the canonical key chosen, or None if the user backed out."""
    suggestions = settings.suggest_menu_keys(current, os.environ)
    if suggestions:
        print("Suggested: " + ", ".join(settings.menu_key_label_for(k) for k in suggestions))
    while True:
        try:
            raw = input("New menu key (e.g. ctrl-o or ctrl+shift+g; blank to go back): ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw:
            return None
        chosen = settings.normalize_menu_key(raw)
        if chosen is None:
            print("Not a valid menu key. Use ctrl-<letter> (not c/h/i/j/m) or ctrl+shift+<letter>.")
            continue
        if settings.detect_menu_key_conflict(chosen, os.environ):
            print(f"Note: {settings.menu_key_label_for(chosen)} may also be intercepted by the host.")
        if not _confirm_menu_key_by_test(chosen):
            continue  # the test failed and the user declined to use it anyway — pick another
        return chosen


def _confirm_menu_key_by_test(key: str) -> bool:
    """Offer to test *key*; if the test shows it doesn't reach aGiTrack, ask whether to use
    it anyway. Returns True to accept *key*, False to pick a different one."""
    label = settings.menu_key_label_for(key)
    try:
        if input(f"Test {label} now? [Y/n]: ").strip().lower() in {"n", "no"}:
            return True  # user skipped the test — accept the key as entered
    except (EOFError, KeyboardInterrupt):
        return True
    result = _run_menu_key_test(key)
    if result is not False:
        return True  # worked, or the test was cancelled/unavailable
    try:
        return input(f"{label} didn't reach aGiTrack. Use it anyway? [y/N]: ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def _run_menu_key_test(key: str) -> bool | None:
    """Prompt the user to press *key* and report whether it reached aGiTrack. Returns True
    (reached), False (swallowed by the host / timed out), or None (cancelled/unavailable)."""
    label = settings.menu_key_label_for(key)
    print(f"Press {label} now (you have a few seconds)…")
    result = _read_menu_key_press(settings.menu_key_bytes_for(key), shift=settings.menu_key_is_shift(key))
    if result is True:
        print(f"  ✓ {label} reached aGiTrack — it will open the menu inside the TUI.")
    elif result is False:
        print(f"  ✗ {label} did NOT reach aGiTrack — the host likely intercepts it; choose another key.")
    else:
        print(f"  (skipped the {label} test)")
    return result


def _read_menu_key_press(expected: bytes, *, shift: bool, timeout: float = 8.0) -> bool | None:
    """Put the terminal in raw mode and wait up to *timeout* for *expected* to arrive on
    stdin. True if it does (so it will open the menu in the TUI), False on timeout (the
    host swallowed it), None if the user pressed Ctrl-C or the terminal can't go raw.

    This is the authoritative check the issue asks for: a key intercepted by VS Code (or
    any host) never reaches stdin here, so the test fails exactly when the TUI would."""
    import select
    import termios
    import time
    import tty

    try:
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except (termios.error, ValueError, OSError):
        return None  # not a real tty (or redirected stdin) — can't test
    try:
        tty.setraw(fd)
        if shift:
            # Ask the terminal to report shifted control keys (kitty keyboard protocol).
            # If it doesn't support them, the sequence never arrives and the test fails —
            # which is correct, since the key wouldn't work in the TUI either.
            os.write(sys.stdout.fileno(), b"\x1b[>1u")
        buffer = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            readable, _, _ = select.select([fd], [], [], remaining)
            if not readable:
                return False
            chunk = os.read(fd, 64)
            if not chunk:
                return False
            buffer += chunk
            if b"\x03" in buffer:  # Ctrl-C cancels the test (never a valid menu key)
                return None
            if expected in buffer:
                return True
    finally:
        if shift:
            try:
                os.write(sys.stdout.fileno(), b"\x1b[<u")
            except OSError:
                pass
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except (termios.error, ValueError, OSError):
            pass


def _discover_or_init(path: Path) -> GitRepo | None:
    """Find the Git repository for ``path``, or offer to create one. aGiTrack cannot
    run outside a Git repository, so if the user declines (or we can't prompt),
    return None and let the caller stop."""
    try:
        repo = GitRepo.discover(path)
        # A user who ran `git init` themselves leaves an unborn HEAD (no commits),
        # which aGiTrack's worktree setup cannot use. Seed an initial commit so an
        # otherwise-empty repository starts cleanly.
        if repo.ensure_born():
            print(f"Seeded an initial commit in empty repository {repo.repo}")
        return repo
    except GitError:
        pass
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(f"Not a Git repository: {path}\naGiTrack requires a Git repository to run.")
        return None
    try:
        answer = input(f"{path} is not a Git repository. Initialize one here with `git init`? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer not in {"y", "yes"}:
        print("aGiTrack cannot run outside a Git repository. Exiting.")
        return None
    try:
        repo = GitRepo.init(path)
    except GitError as error:
        print(error)
        return None
    print(f"Initialized empty Git repository in {repo.repo}")
    return repo


if __name__ == "__main__":
    raise SystemExit(main())
