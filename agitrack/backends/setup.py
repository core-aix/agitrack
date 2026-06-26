from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from agitrack.backends.proxy_agents import available_backends, make_proxy_agent
from agitrack.proc import resolve_subprocess_command

# Per-backend facts used to build a single install hint that covers macOS, Linux, AND
# Windows — so whatever OS a user is on, they see a command that works. ``unix`` is the
# native installer for macOS/Linux; ``npm`` is the cross-platform fallback (needs Node.js).
_BACKEND_INSTALL = {
    "claude": {
        "label": "Claude Code",
        "url": "https://docs.claude.com/en/docs/claude-code",
        "unix": "curl -fsSL https://claude.ai/install.sh | bash",
        "npm": "@anthropic-ai/claude-code",
    },
    "opencode": {
        "label": "OpenCode",
        "url": "https://opencode.ai",
        "unix": "curl -fsSL https://opencode.ai/install | bash",
        "npm": "opencode-ai",
    },
}


class BackendUnavailable(RuntimeError):
    """Raised when the selected backend's CLI is not installed and no installed
    alternative was chosen."""


def _executable(name: str) -> str:
    return make_proxy_agent(name).spawn_command(Path("."), session_id=None, resume=False)[0]


def backend_installed(name: str) -> bool:
    return shutil.which(_executable(name)) is not None


def install_hint(name: str) -> str:
    """A cross-platform (macOS / Linux / Windows) install hint for a missing backend CLI.

    Each part sits on its own block (a blank line between them) so the options are easy to
    tell apart when printed to the user."""
    info = _BACKEND_INSTALL.get(name)
    if info is None:
        return f"Install the '{name}' CLI and make sure it is on your PATH."
    return "\n\n".join(
        [
            f"Install {info['label']} ({info['url']}):",
            f"  macOS / Linux:  {info['unix']}",
            f"  any OS (with Node.js):  npm install -g {info['npm']}",
            "  No Node.js? Install it first — macOS: brew install node · "
            "Linux: your package manager · Windows: winget install OpenJS.NodeJS",
            "  Then open a NEW terminal so the updated PATH is picked up.",
        ]
    )


# --- automatic installation -------------------------------------------------------------
#
# When a chosen backend isn't installed, aGiTrack can install it for the user (they opt in
# at the prompt). It installs ONLY the one backend the user picked, and works on macOS,
# Linux, and Windows: the backend's official install script on POSIX (self-contained, no
# Node needed), npm everywhere, and a winget Node bootstrap on Windows where npm is absent.
# The freshly-installed CLI's directory is added to THIS process's PATH so it's runnable at
# once — the OS installers update the registry/profile PATH, which only new shells inherit.


def _npm_command(which: Callable[[str], str | None]) -> str | None:
    """The npm executable to use, or None. Falls back to Node's well-known Windows install
    dir so a just-installed Node is found before the shell PATH is refreshed."""
    found = which("npm")
    if found:
        return found
    if os.name == "nt":
        for base in (os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles"), r"C:\Program Files"):
            if base:
                candidate = os.path.join(base, "nodejs", "npm.cmd")
                if os.path.isfile(candidate):
                    return candidate
    return None


def _install_node_with_winget(
    output_fn: Callable[[str], None],
    run: Callable[..., subprocess.CompletedProcess],
    which: Callable[[str], str | None],
) -> str | None:
    """Best-effort Node.js install via winget (Windows only, when npm is missing). Returns
    the npm command afterwards, or None when winget is unavailable or the install fails."""
    if os.name != "nt":
        return None
    winget = which("winget")
    if not winget:
        return None
    output_fn("Node.js (needed to install the agent CLI) was not found; installing it with winget…\n")
    try:
        run(
            resolve_subprocess_command(
                [
                    winget,
                    "install",
                    "-e",
                    "--id",
                    "OpenJS.NodeJS",
                    "--silent",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ]
            ),
            timeout=900,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _npm_command(which)


def _npm_global_bin(npm: str, run: Callable[..., subprocess.CompletedProcess]) -> str | None:
    """The directory npm puts global CLI shims in (`npm prefix -g`, plus `/bin` on POSIX)."""
    try:
        result = run(resolve_subprocess_command([npm, "prefix", "-g"]), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    prefix = (getattr(result, "stdout", "") or "").strip()
    if not prefix:
        return None
    return prefix if os.name == "nt" else os.path.join(prefix, "bin")


def _candidate_bin_dirs(npm: str | None, run: Callable[..., subprocess.CompletedProcess]) -> list[str]:
    """Directories a freshly-installed backend CLI may live in, to add to PATH so it
    resolves without restarting aGiTrack."""
    home = os.path.expanduser("~")
    dirs: list[str] = []
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            dirs.append(os.path.join(appdata, "npm"))  # npm global prefix (claude.cmd lands here)
        for base in (os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles"), r"C:\Program Files"):
            if base:
                dirs.append(os.path.join(base, "nodejs"))
    else:
        dirs += [
            os.path.join(home, ".local", "bin"),  # claude's official installer target
            os.path.join(home, ".opencode", "bin"),  # opencode's installer target
            os.path.join(home, "bin"),
            "/usr/local/bin",
            "/opt/homebrew/bin",
        ]
    if npm:
        global_bin = _npm_global_bin(npm, run)
        if global_bin:
            dirs.append(global_bin)
    return dirs


def _add_dirs_to_path(dirs: list[str]) -> None:
    existing = os.environ.get("PATH", "")
    parts = existing.split(os.pathsep) if existing else []
    additions = [d for d in dirs if d and os.path.isdir(d) and d not in parts]
    if additions:
        os.environ["PATH"] = os.pathsep.join([*additions, *parts])


def _install_plan(name: str, info: dict, npm: str | None, which: Callable[[str], str | None]):
    """Ordered (description, command) install attempts for the current OS. POSIX prefers the
    backend's self-contained official installer (no Node needed); npm is the cross-platform
    fallback, and the only route on Windows."""
    plan: list[tuple[str, list[str]]] = []
    if os.name != "nt" and which("bash") and which("curl"):
        plan.append((info["unix"], ["bash", "-lc", info["unix"]]))
    if npm:
        plan.append((f"npm install -g {info['npm']}", resolve_subprocess_command([npm, "install", "-g", info["npm"]])))
    return plan


def install_backend(
    name: str,
    *,
    output_fn: Callable[[str], None] = print,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> bool:
    """Install the single backend CLI `name` automatically (the user opted in at the prompt).

    Cross-platform: the backend's official install script on macOS/Linux, npm everywhere,
    with a winget Node bootstrap on Windows when npm is absent. On success the installed
    CLI's directory is added to this process's PATH so it runs immediately — no restart.
    Returns True only when the backend is actually runnable afterwards. Never raises."""
    info = _BACKEND_INSTALL.get(name)
    if info is None:
        output_fn(install_hint(name))
        return False
    npm = _npm_command(which)
    plan = _install_plan(name, info, npm, which)
    if not plan:
        # Nothing to run with (e.g. Windows without Node) — try to bootstrap npm via winget.
        npm = _install_node_with_winget(output_fn, run, which)
        plan = _install_plan(name, info, npm, which)
    if not plan:
        output_fn(f"Could not install {info['label']} automatically.\n")
        output_fn(install_hint(name))
        return False
    for description, command in plan:
        output_fn(f"\nInstalling {info['label']} — {description}\n")
        try:
            result = run(command, timeout=900)
        except (OSError, subprocess.SubprocessError) as error:
            output_fn(f"  that attempt failed: {error}\n")
            continue
        if getattr(result, "returncode", 1) != 0:
            output_fn("  that attempt did not complete successfully.\n")
            continue
        _add_dirs_to_path(_candidate_bin_dirs(npm, run))
        if backend_installed(name):
            output_fn(f"\n{info['label']} installed.\n")
            return True
    output_fn(f"\n{info['label']} could not be made runnable automatically.\n")
    output_fn(install_hint(name))
    return False


def select_default_backend(
    config,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    install_fn: Callable[..., bool] = install_backend,
) -> str:
    """First-run backend setup. Shows every agent backend with its install status, then
    asks whether to install any of the UNINSTALLED ones — a single one (by number) or all
    of them ('all'). Already-installed backends are just shown as installed, never offered
    for (re)install, and skipping is always allowed. Saves and returns the default backend
    (the first installed one, or the first listed when none is installed yet — the launch
    gate then offers to install that one before the agent starts)."""
    names = available_backends()
    while True:
        installed = [name for name in names if backend_installed(name)]
        uninstalled = [name for name in names if name not in installed]
        output_fn("Agent backends:")
        for index, name in enumerate(names, start=1):
            output_fn(f"  {index}. {name} ({'installed' if name in installed else 'not installed'})")
        if not uninstalled:
            break  # everything installed — nothing to offer
        choices = "its number to install it" + (
            ", 'all' to install every uninstalled one" if len(uninstalled) > 1 else ""
        )
        lead = "Install any of the uninstalled backends?" if installed else "No backend is installed yet."
        answer = input_fn(f"\n{lead} Enter {choices}, or press Enter to skip: ").strip().lower()
        if not answer:
            break
        if answer in {"all", "a"}:
            for name in uninstalled:
                install_fn(name, output_fn=output_fn)
            continue  # re-show with refreshed statuses
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            name = names[int(answer) - 1]
            if name in installed:
                output_fn(f"{name} is already installed.\n")
            else:
                install_fn(name, output_fn=output_fn)
            continue
        output_fn("Please enter a valid number, 'all', or press Enter to skip.\n")
    installed = [name for name in names if backend_installed(name)]
    default = installed[0] if installed else names[0]
    config.default_backend = default
    return default


def select_default_summarizer_model(
    config,
    backend_name: str,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> None:
    """First-run prompt (after the backend is chosen): pick the model aGiTrack uses
    to summarize each commit, saved as the global default. For Claude the smallest
    (Haiku) tier is the recommended default since summarization is a cheap task.
    Silently leaves the default unchanged when the backend's models can't be listed."""
    from agitrack.summaries.model_select import list_available_models, smallest_model

    models = list_available_models(backend_name)
    if not models:
        return
    smallest = smallest_model(backend_name, models)
    ordered = [smallest, *(m for m in models if m != smallest)] if smallest else list(models)
    output_fn("\nChoose the model aGiTrack uses to summarize each commit (a cheap task):")
    for index, model in enumerate(ordered, start=1):
        tag = "  (smallest — recommended)" if model == smallest else ""
        output_fn(f"  {index}. {model}{tag}")
    same_index = len(ordered) + 1
    output_fn(f"  {same_index}. Same as the agent's session model")
    raw = input_fn(f"Enter a number [1-{same_index}] (default 1): ").strip()
    choice = raw or "1"
    if not choice.isdigit() or not 1 <= int(choice) <= same_index:
        config.summarization_model = ordered[0]  # invalid input → the recommended default
        return
    picked = int(choice)
    config.summarization_model = None if picked == same_index else ordered[picked - 1]


def ensure_installed_backend(
    name: str,
    config,
    *,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    install_fn: Callable[..., bool] = install_backend,
) -> str:
    """Make sure the backend that is about to run is installed. If not, offer to install it
    automatically, switch to an installed backend (saving the new default), or see manual
    instructions. Returns the backend to use; raises BackendUnavailable otherwise."""
    if backend_installed(name):
        return name
    if not interactive:
        raise BackendUnavailable(f"Backend '{name}' is not installed.\n{install_hint(name)}")

    names = available_backends()
    label = _label(name)
    while True:
        if backend_installed(name):
            return name
        installed = [other for other in names if backend_installed(other)]
        output_fn(f"\nThe selected backend '{name}' is not installed.\n")
        prompt = f"Press Enter to install {label} now"
        if installed:
            prompt += f", type a backend to switch to ({', '.join(installed)})"
        prompt += ", 'm' for manual instructions, or 'q' to quit: "
        answer = input_fn(prompt).strip().lower()
        if answer in {"q", "quit"}:
            raise BackendUnavailable(f"Backend '{name}' is not installed.")
        if answer in installed:
            config.default_backend = answer
            return answer
        if answer in {"m", "manual"}:
            output_fn("\n" + install_hint(name))
            input_fn("\nPress Enter after installing to retry: ")
            continue
        if install_fn(name, output_fn=output_fn):
            return name
        # Install didn't take — loop and offer the choices again.


def _label(name: str) -> str:
    info = _BACKEND_INSTALL.get(name)
    return info["label"] if info else name
