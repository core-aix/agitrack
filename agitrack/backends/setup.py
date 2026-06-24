from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from agitrack.backends.proxy_agents import available_backends, make_proxy_agent

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
    """A cross-platform (macOS / Linux / Windows) install hint for a missing backend CLI."""
    info = _BACKEND_INSTALL.get(name)
    if info is None:
        return f"Install the '{name}' CLI and make sure it is on your PATH."
    return (
        f"Install {info['label']}: {info['url']}\n"
        f"  macOS / Linux:  {info['unix']}\n"
        f"  any OS (Node):  npm install -g {info['npm']}\n"
        "  (no Node? install it — macOS: brew install node · Linux: your package manager · "
        "Windows: winget install OpenJS.NodeJS)\n"
        "  Then open a NEW terminal so the updated PATH is picked up."
    )


def select_default_backend(
    config,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> str:
    """First-run prompt: let the user pick the default backend (listed
    alphabetically), checking installation and offering to install or choose
    another. Saves and returns the chosen backend."""
    names = available_backends()
    while True:
        output_fn("Welcome to aGiTrack! Choose your default agent backend:")
        for index, name in enumerate(names, start=1):
            status = "installed" if backend_installed(name) else "not installed"
            output_fn(f"  {index}. {name} ({status})")
        raw = input_fn(f"Enter a number [1-{len(names)}] (default 1): ").strip()
        choice = raw or "1"
        if not choice.isdigit() or not 1 <= int(choice) <= len(names):
            output_fn("Please enter a valid number.")
            continue
        name = names[int(choice) - 1]
        if backend_installed(name) or _wait_for_install(name, input_fn=input_fn, output_fn=output_fn):
            config.default_backend = name
            return name
        # User asked to choose a different backend: show the menu again.


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
) -> str:
    """Make sure the backend that is about to run is installed. If not, prompt
    the user to install it or switch to an installed backend (saving the new
    default). Returns the backend to use; raises BackendUnavailable otherwise."""
    if backend_installed(name):
        return name
    if not interactive:
        raise BackendUnavailable(f"Backend '{name}' is not installed.\n{install_hint(name)}")

    names = available_backends()
    while True:
        if backend_installed(name):
            return name
        output_fn(f"\nThe selected backend '{name}' is not installed.")
        output_fn(install_hint(name))
        installed = [other for other in names if backend_installed(other)]
        prompt = "Press Enter after installing to retry"
        if installed:
            prompt += f", type a backend to switch to ({', '.join(installed)})"
        prompt += ", or 'q' to quit: "
        answer = input_fn(prompt).strip().lower()
        if answer in {"q", "quit"}:
            raise BackendUnavailable(f"Backend '{name}' is not installed.")
        if answer in installed:
            config.default_backend = answer
            return answer
        # Otherwise loop and re-check whether `name` is now installed.


def _wait_for_install(
    name: str,
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> bool:
    """Return True once `name` is installed, or False if the user wants to
    choose a different backend."""
    while True:
        output_fn(f"\n'{name}' is not installed.")
        output_fn(install_hint(name))
        answer = (
            input_fn("Press Enter after installing to continue, or type 'b' to choose a different backend: ")
            .strip()
            .lower()
        )
        if answer in {"b", "back", "c", "choose"}:
            return False
        if backend_installed(name):
            return True
        output_fn(f"'{name}' was still not found on your PATH.")
