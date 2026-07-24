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
from agitrack.proc import console_isolation_kwargs
from agitrack.config import GlobalConfig, settings
from agitrack.shell import AgitrackShell

try:
    # The proxy drives the agent through a (Con)PTY. Imported at module level so tests and
    # the launch path reference ``cli.ProxyRunner`` directly, but tolerant of a platform
    # where the proxy's platform layer can't load yet — the headless paths (json mode,
    # dashboard, --version) don't need it, and proxy mode reports it cleanly below.
    from agitrack.proxy import BackgroundRunner, ProxyRunner
except ImportError:  # pragma: no cover - only when the proxy platform layer is unavailable
    ProxyRunner = None  # type: ignore[assignment,misc]
    BackgroundRunner = None  # type: ignore[assignment,misc]

_BACKEND_COMMANDS = {
    "claude": "claude",
    "opencode": "opencode",
}


def _git_install_hint() -> str:
    """Shown when ``git`` isn't on PATH. aGiTrack manages your commits with git, so it can't
    run without it — a common state right after the VS Code extension installs the CLI but
    git itself isn't installed. Covers macOS, Linux, and Windows so any user sees a command
    that works; each part is its own block (blank line between) for legibility."""
    return "\n\n".join(
        [
            "git is not installed (or not on your PATH). aGiTrack manages your commits with "
            "git, so it can't run without it. Install it:",
            "  macOS:    brew install git    (or: xcode-select --install)",
            "  Linux:    use your package manager, e.g. sudo apt install git / sudo dnf install git",
            "  Windows:  winget install Git.Git    (or https://git-scm.com/download/win)",
            "Then open a NEW terminal so the updated PATH is picked up.",
        ]
    )


def _gh_install_hint() -> str:
    """Shown when the GitHub CLI (``gh``) isn't installed. gh is OPTIONAL — it gives the
    dashboard committer identities by GitHub username and powers session sharing — so this is
    informational. Covers macOS, Linux, and Windows, each part its own block for legibility."""
    return "\n\n".join(
        [
            "GitHub CLI (gh) isn't installed. aGiTrack uses it for the dashboard's committer "
            "identities and for session sharing; without it those features are limited (the "
            "dashboard groups authors by email instead). It's optional — you can continue "
            "without it. To install it:",
            "  macOS:    brew install gh",
            "  Linux:    sudo apt install gh    (or your package manager)",
            "  Windows:  winget install GitHub.cli",
            "Then run `gh auth login` and restart aGiTrack.",
        ]
    )


def _installed_via_msi() -> bool:
    """True for a frozen (PyInstaller) build — i.e. the Windows MSI bundle. There,
    prerequisite setup (backends, git, gh, git identity, gh login) is the MSI installer's
    job, so aGiTrack does NOT prompt for it at runtime. A pip/source install is not frozen
    and does its setup at first run, on every platform (including Windows)."""
    return bool(getattr(sys, "frozen", False))


def _maybe_install_tool(name: str, *, required: bool) -> bool:
    """Offer to auto-install a missing prerequisite (``git`` or ``gh``); return True once it
    is available. Only prompts on an interactive TTY where a supported package manager
    exists — otherwise returns False so the caller falls back to printing the manual hint.

    The MSI bundle is intentionally excluded: there, prerequisites are set up by the MSI
    installer, not by aGiTrack at runtime. A pip/source install still offers it (any OS)."""
    if _installed_via_msi():
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    from agitrack.system_tools import can_install_tool, install_system_tool

    if not can_install_tool(name):
        return False
    label = "git" if name == "git" else "the GitHub CLI (gh)"
    note = "" if required else " (optional)"
    try:
        answer = input(f"\n{label} isn't installed. Install it now{note}? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if answer in {"n", "no"}:
        return False
    return install_system_tool(name)


def _git_config_global(config_args: list[str]) -> str:
    """Run ``git config --global`` and return its stdout (empty on any failure)."""
    try:
        result = subprocess.run(
            ["git", "config", "--global", *config_args],
            text=True,
            capture_output=True,
            check=False,
            **console_isolation_kwargs(),  # keep git off a console on Windows (proc.py)
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _ensure_git_identity() -> None:
    """git refuses to commit without ``user.name`` and ``user.email`` ("Author identity
    unknown"), and aGiTrack commits every turn — so on a fresh machine, prompt for whichever
    is missing and set it globally. Interactive callers only; non-TTY callers should not
    reach here (they get no prompt and a polluted machine-readable stream is avoided)."""
    name = _git_config_global(["--get", "user.name"])
    email = _git_config_global(["--get", "user.email"])
    if name and email:
        return
    print("\ngit needs a name and email to record commits (aGiTrack commits your work each turn).")
    if not name:
        entered = input("  Name for git commits: ").strip()
        if entered:
            _git_config_global(["user.name", entered])
    if not email:
        entered = input("  Email for git commits: ").strip()
        if entered:
            _git_config_global(["user.email", entered])
    if not (_git_config_global(["--get", "user.name"]) and _git_config_global(["--get", "user.email"])):
        print("git identity is still incomplete; aGiTrack's commits may fail until name and email are set.")


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
        "-b",
        "--background",
        dest="background",
        nargs="?",
        const="run",
        choices=["run", "stop", "status"],
        default=None,
        help="background (headless) mode: run WITHOUT the interactive TUI, so you drive the "
        "coding agent from any UI you like (its native CLI, an IDE extension, …) while aGiTrack "
        "watches the local session transcript and performs all the tracking the TUI would — "
        "recording each turn, summarizing, and installing the commit hooks that fold the "
        "interaction trace and token metadata into your commits. ALWAYS runs without a worktree "
        "(implies --no-worktree). Uses AUTO commits by default (like interactive mode); add "
        "--manual-commits / -m for user-triggered commits. Bare `-b` (no argument) means `-b run`; "
        "`-b stop` / `-b status` stop or report the background tracker running on this repo. "
        "Also settable via 'background' in config.",
    )
    parser.add_argument(
        "--no-worktree",
        action="store_true",
        help="run the agent against the current branch instead of an isolated worktree "
        "(edits are visible live; no isolation/integration; unsafe with concurrent sessions). "
        "Background (-b) and manual-commit (-m) modes always imply this.",
    )
    parser.add_argument(
        "-m",
        "--manual-commits",
        dest="manual_commits",
        action="store_true",
        help="user-triggered commits. ALWAYS runs without a worktree (implies --no-worktree): the "
        "agent edits the current branch directly and each turn is recorded as a hidden 'latent' "
        "commit on a side ref instead of landing on the branch. When you commit (via the aGiTrack "
        "menu or an external `git commit`), the pending agent turns are folded into that one "
        "commit. Also settable via 'manual_commits' in config.",
    )
    parser.add_argument(
        "-d",
        "--dashboard",
        nargs="?",
        const="html",
        choices=["text", "html", "stop", "status", "export"],
        default=None,
        help="show repository metrics computed from aGiTrack commit metadata "
        "(coverage, AI / human / non-tracked line changes, tokens, per-backend/"
        "model/committer breakdowns, loop detection). Bare `-d` (no argument) means `-d html`: "
        "it starts a filterable, auto-refreshing dashboard as a background daemon on localhost, "
        "opens it in the browser, and returns to the shell; the daemon stops when "
        "this terminal closes or via `-d stop`. `status` reports it; `text` prints a "
        "one-shot report and exits; `export` writes a server-free static demo copy of the "
        "dashboard (see --export-dir) that any static web host can serve",
    )
    parser.add_argument(
        "--export-dir",
        default=None,
        help="where `-d export` writes the static demo site (default: .agitrack/demo-site "
        "inside the repo). The directory is replaced.",
    )
    parser.add_argument(
        "--backtrace",
        nargs="?",
        const="html",
        choices=["text", "html", "stop", "status", "commit"],
        default=None,
        help="reconstruct how PAST coding-agent conversations changed THIS directory, from local "
        "Claude/OpenCode transcripts alone — even if you have never used aGiTrack here, and even if "
        "the directory is not a git repo. It reads the sessions that ran in this directory (or a "
        "subdirectory), recovers each turn's file edits, and shows the same dashboard (tokens, "
        "models, lines changed, and the full user↔agent trace behind each change) marked clearly as "
        "a historical backtrace, not live repo status. Bare `--backtrace` (or `--backtrace html`) "
        "starts it as a background daemon on localhost, opens the browser, and returns to the shell "
        "(it stops when this terminal closes or via `--backtrace stop`); `status` reports it; "
        "`text` prints a one-shot report. `commit` REWRITES history onto a NEW branch (`--backtrace-branch`), "
        "annotating the commits that made AI changes with aGiTrack metadata — so a project built "
        "without aGiTrack still gets a tracked history (requires a clean working tree).",
    )
    parser.add_argument(
        "--backtrace-branch",
        default=None,
        help="the NEW branch to create for `--backtrace commit` (the reconstructed, history-"
        "rewritten commits are placed here; your current branch is left untouched).",
    )
    parser.add_argument(
        "-s",
        "--status",
        action="store_true",
        help="report whether aGiTrack is running for this repo and in which mode (interactive vs "
        "background, auto vs manual commit, worktree vs no-worktree), then exit.",
    )
    parser.add_argument(
        "--daemons",
        action="store_true",
        help="list every running aGiTrack daemon across ALL repositories — its function (repo "
        "dashboard, backtrace dashboard, or background mode), repo name, and PID — so you can stop a "
        "stray one by hand, then exit.",
    )
    # --- options without a short form, in rough order of how often they matter ---
    parser.add_argument("--repo", default=".", help="target Git repository path")
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
        "--auto-commit",
        dest="auto_commit",
        action="store_true",
        help="force automatic (aGiTrack-triggered) commits — the default in background mode, so "
        "this only matters to override a configured 'manual_commits': true. aGiTrack commits each "
        "agent turn itself and folds tracking into the agent's own commits via a prepare-commit-msg hook.",
    )
    parser.add_argument(
        "--delay-merge",
        action="store_true",
        help="don't merge a turn's committed changes into the base branch automatically; "
        "instead leave them in the session's working directory for you to review/edit, then "
        "merge on your confirmation via the session menu. Off by default.",
    )
    parser.add_argument(
        "--no-commit-guidance",
        action="store_true",
        help="do not tell the coding agent that aGiTrack handles commits; by default aGiTrack "
        "appends a note to the agent's system prompt (where the backend supports it) so the "
        "agent does not create its own git commits unless you explicitly ask",
    )
    parser.add_argument(
        "--full-agent-messages",
        action="store_true",
        help="record every user-facing message the agent sends during a turn in the "
        "commit's interaction trace, not just the final reply (tool calls and file edits "
        "are still excluded); also settable per-repo via full_agent_messages in config",
    )
    parser.add_argument(
        "--log-file",
        dest="log_file",
        default=None,
        metavar="PATH",
        help="append notable aGiTrack events (an AI change detected, a commit made, a merge "
        "integrated, an update available) to PATH — a plain-text log you can `tail -f`. Works in "
        "every mode, with or without -b. A relative path is resolved against the repo root. Also "
        "settable via 'log_file' in config.",
    )
    parser.add_argument(
        "--no-confine",
        "--no-sandbox",
        dest="no_sandbox",
        action="store_true",
        help="do not confine the agent's writes to its session worktree. By default aGiTrack "
        "confines the agent to its worktree (plus .git): on macOS/Linux via the OS sandbox "
        "(sandbox-exec/bubblewrap), and where no sandbox is available (e.g. Windows) via a git "
        "pre-commit guard that stops the agent from committing into the base repo. Also settable "
        "via 'sandbox' in config. (--no-sandbox is kept as an alias.)",
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
        "--remove-hooks",
        action="store_true",
        help="remove all aGiTrack-installed git hooks from the repo — the persistent auto-track "
        "pre-commit hook and the manual-commit prepare-commit-msg/post-commit fold hooks (and the "
        "worktree base-commit guard), restoring any hooks they chained. Use this to fully opt out "
        "of aGiTrack's commit-time tracking.",
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
    parser.add_argument("--verbose", action="store_true", help="show aGiTrack diagnostic messages")
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the aGiTrack version and exit",
    )
    # --- testing / programmatic-driver options (real interactive use never needs these) ---
    parser.add_argument(
        "--json",
        dest="json_mode",
        action="store_true",
        help="use the JSON prompt-loop instead of the interactive TUI: aGiTrack sends each typed "
        "line (or --prompt) to the backend non-interactively and captures the reply as a commit. "
        "Mainly for testing and programmatic drivers — normal interactive use does not need it.",
    )
    parser.add_argument(
        "--prompt",
        action="append",
        dest="prompts",
        metavar="TEXT",
        help="run this prompt non-interactively (implies --json) and exit; "
        "repeatable, prompts run in order. Lines starting with ':' are aGiTrack "
        "commands, e.g. --prompt ':status'",
    )
    parser.add_argument(
        "--json-events",
        action="store_true",
        help="with --json, emit one machine-readable JSON line per turn event "
        "(the agent's response, the commit produced, errors) — used by the VSCode "
        "chat extension and other programmatic drivers",
    )
    parser.add_argument(
        "--ui-bridge",
        action="store_true",
        help="with --json, run a long-lived JSON-RPC session over stdin/stdout where "
        "interactive questions (menus, confirmations, text input) are asked of the driver "
        "program instead of a terminal — for embedding aGiTrack behind an editor/GUI front-end",
    )
    parser.add_argument(
        "--mode",
        choices=["proxy", "json"],
        default="proxy",
        # Deprecated: `--mode` conflated too many things (interactive/background,
        # auto/manual, worktree/no-worktree are separate flags now). Kept as a hidden,
        # still-working alias for `--json` so existing scripts don't break: `--mode json`
        # == `--json`. New usage should prefer `--json`.
        help=argparse.SUPPRESS,
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
        "--backtrace-serve",
        action="store_true",
        # Internal: the detached `--backtrace` child. Bare `agitrack --backtrace` spawns aGiTrack
        # with this flag to host the reconstructed dashboard out-of-process, bound to the owner
        # pid via --dashboard-owner-pid. Not meant for manual use.
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
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=None,
        # Internal: port the --dashboard-serve child should bind. Used on RESTART so the
        # replacement daemon keeps the previous URL; falls back to an OS-assigned port when
        # taken. Not meant for manual use.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--precommit-sync",
        action="store_true",
        # Internal: entry point of the persistent auto-track pre-commit hook. Records any pending
        # AI turns and renders the fold trailer so the commit being made carries the trace, and
        # (unless autotrack_hook=off) auto-starts the background daemon. Best-effort, never fails
        # a commit. Not meant for manual use.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--background-serve",
        action="store_true",
        # Internal: run the headless background tracker loop in the foreground (this
        # process). `agitrack -b` spawns aGiTrack with this flag as a detached daemon so the
        # launching terminal is freed. Unlike the dashboard daemon it has NO owner-pid
        # watchdog — a tracker must outlive the terminal that started it (stop it with
        # `agitrack -b stop`). Not meant for manual use.
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

    if args.daemons:
        # Global, read-only listing of every running aGiTrack daemon — no repo/git needed.
        from agitrack.daemons import list_running
        from agitrack.metrics.collect import _abbreviate_home

        running = list_running()
        if not running:
            print("No aGiTrack daemons are currently running.")
            return 0
        print("aGiTrack daemons running:\n")
        print(f"  {'PID':>7}  {'FUNCTION':<20}  DIRECTORY")
        for info in running:
            location = _abbreviate_home(info.repo)
            url = f"   {info.url}" if info.url else ""
            print(f"  {info.pid:>7}  {info.function:<20}  {location}{url}")
        # The per-daemon stop commands act on the CURRENT directory's repo, so they must be run
        # from that directory (shown above) or given its path with --repo.
        print(
            "\nTo stop one:\n"
            "  • by PID:            kill <PID>\n"
            "  • or its own stop command, from that directory (or with --repo <path>):\n"
            "        repo dashboard       agitrack --repo <path> -d stop\n"
            "        backtrace dashboard  agitrack --repo <path> --backtrace stop\n"
            "        background mode      agitrack --repo <path> -b stop"
        )
        return 0

    # Backtrace works purely from local transcripts — no git repo AND no git binary needed —
    # so both the user command and its detached child are handled BEFORE the git check below.
    if args.backtrace_serve:
        # Internal entry point: the detached `--backtrace` child. Serves the reconstructed
        # dashboard out-of-process and shuts down when its owner pid dies.
        from agitrack.metrics.backtrace import run_backtrace_daemon

        return run_backtrace_daemon(
            Path(args.repo).expanduser().resolve(),
            owner_pid=args.dashboard_owner_pid,
            # A restart passes the previous port so the URL survives (see start_backtrace_daemon);
            # None means "start from the default and scan upward for the first free one".
            port=args.dashboard_port,
        )

    if args.backtrace:
        # Read-only reconstruction from local transcripts — no git, no privacy prompt, no repo
        # init: it works in ANY directory, including one that was never a repository. The target
        # is the directory itself (--repo, or the cwd), NOT a discovered repo root, so a
        # subdirectory backtraces its own sessions.
        directory = Path(args.repo).expanduser().resolve()
        if not directory.is_dir():
            print(f"{directory} is not a directory.")
            return 1
        if args.backtrace == "text":
            from agitrack.metrics.backtrace import render_backtrace_text

            print(render_backtrace_text(directory))
            return 0
        if args.backtrace == "stop":
            from agitrack.metrics.backtrace import stop_backtrace_daemon

            return stop_backtrace_daemon(directory)
        if args.backtrace == "status":
            from agitrack.metrics.backtrace import backtrace_daemon_status

            return backtrace_daemon_status(directory)
        if args.backtrace == "commit":
            # Reconstruct a TRACKED git history: rewrite commits onto a new branch, annotating the
            # AI-made ones with aGiTrack metadata. Requires a git repo + clean tree + a branch name.
            # Always interactive (it rewrites history) — there is no skip-confirmation flag.
            from agitrack.metrics.backtrace_commit import backtrace_commit

            return backtrace_commit(directory, args.backtrace_branch or "")
        from agitrack.metrics.backtrace import start_backtrace_daemon

        return start_backtrace_daemon(directory, owner_pid=os.getppid())

    # aGiTrack can't do anything without git (every path below discovers/commits to a repo).
    # Check once, up front, so a missing git gives a clear, actionable message instead of a
    # raw FileNotFoundError deep in repo discovery — common right after the VS Code extension
    # installs the CLI but git isn't on PATH. --version/--help above don't need git.
    if shutil.which("git") is None and not _maybe_install_tool("git", required=True):
        print(_git_install_hint())
        return 1

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
        if args.dashboard_port is not None:
            return run_dashboard_daemon(
                serve_repo,
                owner_pid=args.dashboard_owner_pid,
                email_logins=email_logins,
                port=args.dashboard_port,
            )
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
        if args.dashboard == "export":
            from agitrack.metrics.export import export_static_demo

            out_dir = (
                Path(args.export_dir).expanduser()
                if args.export_dir
                else dashboard_repo.repo / ".agitrack" / "demo-site"
            )
            export_static_demo(dashboard_repo, out_dir)
            print(f"Static demo dashboard written to {out_dir}")
            print("Serve the directory with any static web host (or open index.html directly).")
            return 0
        # Bare `-d` / `-d html`: start the live dashboard as a background daemon owned
        # by the launching shell, so the terminal is freed and the daemon dies when
        # that shell/terminal closes (#110).
        from agitrack.metrics import start_dashboard_daemon

        return start_dashboard_daemon(dashboard_repo, owner_pid=os.getppid())

    if args.background in ("stop", "status"):
        # `agitrack -b stop` / `-b status`: signal or report the background tracker running on
        # this repo. Read-only w.r.t. the agent — no privacy prompt, no repo init, no update check.
        try:
            bg_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError) as error:
            print(error)
            return 1
        from agitrack.proxy.background import background_status, stop_background

        return stop_background(bg_repo) if args.background == "stop" else background_status(bg_repo)

    if args.status:
        # `agitrack --status` / `-s`: report the running mode for this repo. Read-only — no privacy
        # prompt, no repo init, no update check.
        try:
            status_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError) as error:
            print(error)
            return 1
        from agitrack.proxy.background import repo_status

        return repo_status(status_repo)

    if args.remove_hooks:
        # Let the user fully opt out of aGiTrack's commit-time tracking by removing every hook it
        # installed (restoring any chained originals). Read-only w.r.t. the agent — no privacy
        # prompt, no repo init, no update check.
        try:
            rh_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError) as error:
            print(error)
            return 1
        from agitrack.git import hooks as git_hooks

        removed = git_hooks.remove_all_installed_hooks(rh_repo.hooks_dir())
        # Persist the opt-out so a later aGiTrack run doesn't silently reinstall the auto-track hook.
        try:
            rh_config = GlobalConfig()
            rh_config.load_repo_overlay(rh_repo.repo)
            rh_config.set("autotrack_hook", "off", scope="repo")
        except Exception:
            pass
        if removed:
            print(f"Removed aGiTrack git hook(s): {', '.join(removed)}. Any chained project hooks were restored.")
            print("Auto-start on commit is now off for this repo. Re-enable it in Ctrl-G → settings or `agitrack -b`.")
        else:
            print("No aGiTrack git hooks were installed in this repository. Auto-start on commit is now off.")
        return 0

    if args.precommit_sync:
        # Internal: the persistent auto-track pre-commit hook. Fast, best-effort, never fails a
        # commit — records any pending AI turns and renders the fold trailer for the commit being
        # made, then (unless autotrack_hook=off) auto-starts the background daemon.
        try:
            sync_repo = GitRepo.discover(Path(args.repo).expanduser())
        except (GitError, OSError):
            return 0  # not a repo / bad path ⇒ silently do nothing, never block the commit
        from agitrack.proxy.background import precommit_sync

        return precommit_sync(sync_repo)

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

    # `--json` is the documented flag for the JSON prompt-loop; `--mode json` is its hidden
    # deprecated alias. `--prompt` and `--ui-bridge` both drive that same non-interactive loop.
    if args.json_mode:
        args.mode = "json"
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

    # Make sure git can actually commit: without a global user.name/user.email every commit
    # fails with "Author identity unknown", and aGiTrack commits each turn. Prompt for any
    # missing value on an interactive launch; the MSI bundle defers this to the installer,
    # and scripted/json runs are left clean (those users have git configured).
    if args.mode == "proxy" and not _installed_via_msi() and sys.stdin.isatty() and sys.stdout.isatty():
        _ensure_git_identity()

    # Make the global config self-documenting: write any settings still missing from
    # ~/.agitrack/config.json with their built-in defaults, so a user opening the file
    # sees every available knob. Only fills gaps (never overwrites a user value) and only
    # writes when something was missing, so it is a no-op on every run after the first.
    # Placed after the cheap --version/--dashboard paths return, so those stay side-effect-free.
    getattr(config, "seed_defaults", lambda: False)()

    # Offer a self-update before launching anything. Skipped for scripted/non-TTY
    # runs (no way to answer) and when the user turned update checks off. If the
    # user accepts, aGiTrack updates and re-execs immediately — no sessions are
    # running yet at startup, so there is nothing to finalize first.
    if not scripted and sys.stdin.isatty() and sys.stdout.isatty():
        _check_for_update_at_startup(config)

    # First-run backend setup. Runs whenever no default backend is configured (and one wasn't
    # passed via --backend) so the user always chooses one before launch — NOT gated on a
    # backend being missing: with both already installed but no default saved, skipping this
    # used to drop straight to the "No coding agent backend is configured" error every launch.
    # Skipped for the MSI bundle (the installer handles it) and for scripted/non-TTY runs (no
    # way to answer). select_default_backend lists statuses, offers to install any missing
    # ones, asks which to use as the default, and explains how to change it later.
    if (
        args.backend is None
        and not config.has_default_backend()
        and not scripted
        and not _installed_via_msi()
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
    share_config_error = getattr(config, "share_config_error", lambda: None)()
    if share_config_error:
        print(f"Configuration error: {share_config_error}")
        return 1
    manual_commits = True if getattr(args, "manual_commits", False) else getattr(config, "manual_commits", False)
    # Background (headless) mode: aGiTrack tracks a user-driven native backend session instead
    # of running the interactive TUI. `-b`/`--background` (const "run") or the 'background' config
    # key enable it; `-b stop`/`status` were handled earlier and never reach here.
    background = (getattr(args, "background", None) == "run") or getattr(config, "background", False)
    if background:
        # Background mode ALWAYS runs without a worktree, and uses AUTO commits by default (like
        # interactive mode). --manual-commits / -m (or config manual_commits) opts into manual;
        # --auto-commit forces auto even over a configured manual_commits: true.
        if getattr(args, "auto_commit", False):
            manual_commits = False
    # Manual-commit mode edits the current branch directly and defers commits to the user, so
    # it necessarily runs without a worktree (there is no per-turn branch to integrate).
    use_worktrees = False if (args.no_worktree or manual_commits or background) else config.use_worktrees
    commit_guidance = False if args.no_commit_guidance else getattr(config, "commit_guidance", True)
    sandbox_enabled = False if args.no_sandbox else getattr(config, "sandbox", True)
    # Event-log path: a per-run --log-file wins over the configured log_file; None ⇒ no log.
    log_file_spec = args.log_file if args.log_file is not None else getattr(config, "log_file", None)
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

    # Take the single-writer lock up front — BEFORE any interactive startup prompt — and
    # hold it for the whole session. Besides refusing a second instance immediately, this
    # makes the lock (carrying our PID) present from the very start, so a session still
    # sitting at a startup prompt is already "locked". The VSCode extension reads this lock
    # to tell a starting/running session apart from a dead shell; holding it from the start
    # is what lets the aG button reliably focus the existing terminal instead of opening a
    # second one. (It was a read-only probe before, so no lock was held during startup and
    # the extension couldn't yet see the session.)
    management_lock = RepoLock(repo.repo / ".agitrack" / "lock")
    if not management_lock.acquire():
        owner_pid = management_lock.owner_pid()
        replaced = False
        if background:
            # `agitrack -b` over a live background tracker replaces it (like re-running
            # `-d`/`--backtrace`): stop the old daemon cleanly and take over — so a rerun
            # after an aGiTrack update always runs the new code. Anything else holding the
            # lock (an interactive session) still refuses below.
            from agitrack.proxy.background import _running_tracker_is_current, replace_running_tracker

            # But if that tracker is ALREADY the current version, there is no new code to load —
            # leave it running instead of tearing it down and respawning. This needless restart
            # churn is what made the daemon appear to "quit" on every unrelated aGiTrack invocation.
            if _running_tracker_is_current(repo, owner_pid=owner_pid):
                print(
                    f"aGiTrack background tracker already running (PID {owner_pid}, current version) — left in place."
                )
                return 0
            replaced = replace_running_tracker(repo, owner_pid=owner_pid) and management_lock.acquire()
        if not replaced:
            print(already_running_message(owner_pid))
            return 1

    try:
        if background:
            # Headless background tracker (issue #143): no TUI, no PTY takeover. aGiTrack watches
            # the user-driven backend session and tracks it. Show the privacy warning first (it
            # auto-proceeds without a TTY — so the interactive launcher below acknowledges it,
            # then hands the detached child `--skip-privacy-ack`).
            if not _acknowledge_privacy_warning(scripted=scripted, skip=args.skip_privacy_ack):
                return 1
            if BackgroundRunner is None:  # pragma: no cover - platform without proxy support
                print("Background mode is not available on this platform yet.")
                return 1
            if args.background_serve:
                # We ARE the detached daemon child: run the tracker loop in the foreground of
                # this (already-detached) process, holding the repo lock for our whole run.
                return BackgroundRunner(
                    repo,
                    verbose=args.verbose,
                    backend=args.backend,
                    new_session=args.new_session,
                    manual_commits=manual_commits,
                    backend_command=backend_command,
                    log_file=log_file_spec,
                    _lock=management_lock,
                ).run()
            # Explain the persistent pre-commit hook and let the user decide (once per repo)
            # whether to keep it after this tracker exits — before we spawn anything.
            _maybe_prompt_background_hook(config, scripted=scripted)
            # Launcher: spawn the tracker as a DETACHED daemon (like `agitrack -d`) so the
            # terminal is freed, then return to the shell. The child re-execs aGiTrack with
            # --background-serve and takes its own lock, so release ours first (the child owns
            # the single-writer lock for the daemon's lifetime; stop it with `agitrack -b stop`).
            management_lock.release()
            from agitrack.proxy.background import start_background_daemon

            child_args: list[str] = []
            if args.backend:
                child_args += ["--backend", args.backend]
            # Force the resolved commit mode explicitly so the child's own config can't flip it.
            child_args.append("--manual-commits" if manual_commits else "--auto-commit")
            if args.new_session:
                child_args.append("--new-session")
            if args.verbose:
                child_args.append("--verbose")
            if args.backend_command:
                child_args += ["--backend-command", args.backend_command]
            # Forward a per-run --log-file (a configured log_file the child reads itself).
            if args.log_file:
                child_args += ["--log-file", args.log_file]
            return start_background_daemon(repo, extra_args=child_args)
        if args.mode == "json":
            # json/scripted mode has no interactive pre-TUI configuration steps, so show the
            # privacy warning here (it auto-proceeds without a TTY) before the shell starts.
            if not _acknowledge_privacy_warning(scripted=scripted, skip=args.skip_privacy_ack):
                management_lock.release()
                return 1
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
                manual_commits=manual_commits,
                backend_args=backend_args,
                backend_command=backend_command,
                commit_guidance=commit_guidance,
                full_agent_messages=args.full_agent_messages,
                delay_merge=args.delay_merge,
                sandbox=sandbox_enabled,
                allowed_edit_paths=allowed_edit_paths,
                log_file=log_file_spec,
                gh_prechecked=gh_handled,
                skip_privacy_ack=args.skip_privacy_ack,
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
    # Windows MSI build: apply() only DOWNLOADED the installer (it replaces the running
    # agitrack.exe, so it can't install in place). Hand off to the elevated installer, which
    # installs after we exit and relaunches the updated build; quit so that can proceed. Without
    # this the startup path would just re-exec the current version and re-offer the update every
    # launch (an endless "update available" loop that never installs).
    if getattr(updater, "pending_msi_path", None):
        if updater.launch_msi_bootstrapper():
            print(f"{result.message} aGiTrack will reinstall and reopen automatically.")
            sys.exit(0)
        config.pending_manual_update = status.latest or status.current or "available"
        print(f"Could not start the aGiTrack installer. To update it, {updater.manual_update_instructions()}")
        return
    # Windows package installs can't replace the running agitrack.exe in place (the OS locks
    # it), so apply() defers the pip upgrade to a helper that runs after we exit. Spawn it and
    # quit so the upgrade can proceed; the helper relaunches aGiTrack itself when it's done.
    if getattr(updater, "pending_pip_upgrade", None):
        if updater.launch_pip_bootstrapper():
            print(f"{result.message} aGiTrack will reopen automatically once it completes.")
            sys.exit(0)
        # Couldn't start the helper — keep the current version and remind the user to update.
        config.pending_manual_update = status.latest or status.current or "available"
        print(f"Could not start the aGiTrack updater. To update it, {updater.manual_update_instructions()}")
        return
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
    scripted mode) it does nothing and returns ``(True, False)``. The MSI bundle also does
    nothing — gh setup/login there is the installer's job; a pip/source install still does it."""
    if _installed_via_msi() or scripted or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return (True, False)
    from agitrack.metrics.github import commit_url_base, gh_status

    status = gh_status()
    if status == "ok":
        return (True, False)  # installed and authenticated — nothing to do
    if not commit_url_base(repo):
        return (True, False)  # no GitHub remote — gh isn't needed here yet
    if status == "missing":
        # Offer to install gh automatically; if it lands, it still needs a login, so fall
        # through to the unauthenticated branch below (re-checking its real status).
        if _maybe_install_tool("gh", required=False):
            status = gh_status()
            if status == "ok":
                return (True, True)
    if status == "missing":
        # Leading blank line so the gh question is separated from the preceding startup output.
        print("\n" + _gh_install_hint())
        prompt = "\nPress Enter to continue without it (q to quit): "
    else:  # unauthenticated
        print(
            "\nGitHub CLI (gh) isn't signed in. aGiTrack uses it for the dashboard's committer "
            "identities and for session sharing; without it those features are limited.\n\n"
            "Sign in with `gh auth login` (or press 'l' below to run it now)."
        )
        prompt = "\nPress Enter to continue, type 'l' to log in now (q to quit): "
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


def _maybe_prompt_background_hook(config: GlobalConfig, *, scripted: bool) -> None:
    """When starting `agitrack -b`, explain the persistent auto-track pre-commit hook and let the
    user decide whether to enable it. When enabled (the default), a `git commit` made while aGiTrack
    isn't running folds the AI trace into that commit AND auto-starts the tracker (in the same commit
    mode as the last run) for the turns that follow. Sets the repo-scoped ``autotrack_hook``
    ("auto"/"off"). Never blocks automation (no-TTY / scripted → default on)."""
    if scripted or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    try:
        # Skip only when auto-start is ALREADY enabled for this repo (an explicit repo-scoped
        # "auto"). Re-ask whenever it's off — including after `agitrack --remove-hooks`, which sets
        # it off — so the user can turn it back on; and ask on the first run (default is a global,
        # not repo-scoped, "auto").
        if config.autotrack_hook == "auto" and config.source("autotrack_hook") == "repo":
            return
    except Exception:
        return
    print(
        "\naGiTrack installs a persistent git pre-commit hook in this repo. When you `git commit`\n"
        "later and aGiTrack isn't running, it records that commit's AI work into the commit AND\n"
        "auto-starts background tracking (in the same auto/manual commit mode as your last run) for\n"
        "the turns that follow — so tracking survives you closing the terminal or a reboot. Your\n"
        "commit stays your own; a purely human commit (no AI work) is left untouched. Disable it\n"
        "anytime with `agitrack --remove-hooks`."
    )
    try:
        answer = input("Enable this auto-start hook? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if answer.startswith("n"):
        config.set("autotrack_hook", "off", scope="repo")
        print("\naGiTrack: auto-start hook off — tracking runs only while `agitrack -b` is up.")
    else:
        config.set("autotrack_hook", "auto", scope="repo")
        print("\naGiTrack: auto-start hook enabled. Disable it anytime with `agitrack --remove-hooks`.")


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
    if os.name == "nt":  # native Windows has no termios/tty — read the console via msvcrt
        return _read_menu_key_press_windows(expected, shift=shift, timeout=timeout)
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


def _read_menu_key_press_windows(expected: bytes, *, shift: bool, timeout: float) -> bool | None:
    """Native-Windows port of :func:`_read_menu_key_press` (#118).

    The Windows console hands control keys (Ctrl-G = ``0x07``) straight through
    ``msvcrt.getch`` with no echo or line buffering, so a key the host (VS Code) intercepts
    never arrives here — exactly as it wouldn't reach the TUI. Returns True if *expected*
    arrives, False on timeout, None on Ctrl-C / no console. The kitty-protocol shifted-key
    reporting the POSIX path enables isn't available on the Windows console, so a shift-based
    menu key simply times out here — which is correct, since it wouldn't work in the TUI."""
    import time

    try:
        import msvcrt
    except ImportError:  # pragma: no cover - msvcrt is always present on Windows
        return None
    # Bind the console readers once; the ignores cover mypy on POSIX, where it (correctly)
    # sees no win32 attributes on the ``msvcrt`` stub. The dispatcher only reaches here on
    # Windows; the tests substitute a fake msvcrt so this stays exercised on the POSIX gate.
    kbhit = msvcrt.kbhit  # type: ignore[attr-defined]
    getch = msvcrt.getch  # type: ignore[attr-defined]
    buffer = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not kbhit():
            time.sleep(0.02)
            continue
        char = getch()
        if char in (b"\x00", b"\xe0"):
            # A function/arrow key: a lead byte followed by a scancode. Consume the scancode
            # so it isn't mis-read as a separate keypress; it's never a valid menu key here.
            if kbhit():
                getch()
            continue
        buffer += char
        if b"\x03" in buffer:  # Ctrl-C cancels the test (never a valid menu key)
            return None
        if expected in buffer:
            return True
    return False


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
