from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from agitrack.backends.setup import select_default_backend, select_default_summarizer_model
from agitrack.backends.proxy_agents import available_backends
from agitrack.git import GitError, GitRepo, RepoLock, already_running_message
from agitrack.config import GlobalConfig
from agitrack.proxy import ProxyRunner
from agitrack.shell import AgitrackShell

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
        choices=["text", "html"],
        default=None,
        help="show repository metrics computed from aGiTrack commit metadata "
        "(coverage, AI / human / non-tracked line changes, tokens, per-backend/"
        "model/committer breakdowns, loop detection). Bare or `html` serves a "
        "filterable, auto-refreshing dashboard on localhost and opens it in the "
        "browser (Ctrl-C to stop); `text` prints a one-shot report and exits",
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

    if args.dashboard:
        # Read-only: nothing is logged or committed, so no privacy
        # acknowledgment and no repo initialization offer.
        try:
            dashboard_repo = GitRepo.discover(Path(args.repo).expanduser())
            if args.dashboard == "text":
                from agitrack.metrics import render_dashboard

                print(render_dashboard(dashboard_repo))
                return 0
            from agitrack.metrics import serve_dashboard

            return serve_dashboard(dashboard_repo)
        except (GitError, OSError) as error:
            # OSError: --repo points at a directory that does not exist.
            print(error)
            return 1

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
        if not shutil.which(backend_cmd):
            print(f"Error: Backend '{backend}' not found on PATH.")
            return 1
        result = subprocess.run([backend_cmd] + backend_args, check=False)
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

    # Worktrees on unless the config opts out or --no-worktree is passed (flag wins).
    use_worktrees = False if args.no_worktree else config.use_worktrees
    # The agent commit-guidance note is on unless the config opts out or
    # --no-commit-guidance is passed (flag wins). getattr keeps a config written before
    # this key existed (or a partial stub) defaulting to on.
    commit_guidance = False if args.no_commit_guidance else getattr(config, "commit_guidance", True)

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

    # Refuse a second instance on this repo up front — BEFORE the privacy prompt —
    # so the user isn't asked to acknowledge anything only to be turned away. The
    # authoritative lock is still taken inside run(); this is just an early, friendly
    # check (a brief probe-then-acquire race only delays the same refusal, never
    # lets two instances start).
    owner = RepoLock(repo.repo / ".agitrack" / "lock").probe_owner()
    if owner is not None:
        print(already_running_message(owner))
        return 1

    if not _acknowledge_privacy_warning(scripted=scripted, skip=args.skip_privacy_ack):
        return 1

    try:
        if args.mode == "json":
            AgitrackShell(
                repo,
                verbose=args.verbose,
                backend=args.backend,
                new_session=args.new_session,
                backend_args=backend_args,
                prompts=args.prompts,
                commit_guidance=commit_guidance,
                json_events=args.json_events,
                ui_bridge=args.ui_bridge,
            ).run()
        else:
            return ProxyRunner(
                repo,
                verbose=args.verbose,
                backend=args.backend,
                new_session=args.new_session,
                use_worktrees=use_worktrees,
                backend_args=backend_args,
                commit_guidance=commit_guidance,
                full_agent_messages=args.full_agent_messages,
                delay_merge=args.delay_merge,
            ).run()
    except (GitError, RuntimeError) as error:
        print(error)
        return 1
    return 0


# Flags aGiTrack injects itself to manage session tracking; forwarding a duplicate
# can fight aGiTrack's own session handling. We warn but still forward — aGiTrack never
# silently swallows the user's intent.
_RESERVED_PASSTHROUGH = {
    "claude": {"--session-id", "--resume", "-r", "--continue", "-c"},
    "opencode": {"--session", "-s", "--continue", "-c"},
}


def _warn_reserved_passthrough(backend: str, backend_args: list[str]) -> None:
    reserved = _RESERVED_PASSTHROUGH.get(backend, set())
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


PRIVACY_WARNING = (
    "\nWARNING: aGiTrack records the conversation in git commit messages — every\n"
    "message you enter in the chat can become part of the repository history.\n"
    "Do not enter passwords, API keys, or other sensitive information in the\n"
    "chat. (Keeping secrets out of prompts is good practice anyway.)"
)


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
    print(PRIVACY_WARNING)
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
