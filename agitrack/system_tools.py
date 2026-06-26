"""Opt-in auto-install of the system prerequisites aGiTrack needs: git and the GitHub CLI.

aGiTrack manages commits with **git** (required) and uses **gh** for the dashboard's
committer identities and session sharing (optional). On a fresh machine — notably the
Windows MSI install, which ships no Python and no toolchain — these can be missing. When the
user opts in at a prompt, aGiTrack installs them with the platform's package manager: winget
on Windows, Homebrew on macOS (or the Xcode command-line tools for git), and the detected
distro manager (with sudo) on Linux. The freshly-installed tool's directory is added to THIS
process's PATH so it's usable immediately, since the installers only update the PATH that new
shells inherit.

Pure/dependency-injected (`run`/`which`/`output_fn`) so it is fully unit-testable on any OS.
Never raises.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any, Callable

# Per-tool install identifiers per package manager. winget needs no sudo (it elevates via
# UAC only if required); brew is user-scoped; the Linux distro managers need sudo.
_TOOLS: dict[str, dict[str, str]] = {
    "git": {
        "label": "git",
        "winget": "Git.Git",
        "brew": "git",
        "apt": "git",
        "dnf": "git",
        "pacman": "git",
        "zypper": "git",
    },
    "gh": {
        "label": "GitHub CLI (gh)",
        "winget": "GitHub.cli",
        "brew": "gh",
        "apt": "gh",
        "dnf": "gh",
        "pacman": "github-cli",
        "zypper": "gh",
    },
}

# Linux package managers, in detection order, with the command to install a package.
_LINUX_MANAGERS: list[tuple[str, str, Callable[[str], list[str]]]] = [
    ("apt", "apt-get", lambda pkg: ["sudo", "apt-get", "install", "-y", pkg]),
    ("dnf", "dnf", lambda pkg: ["sudo", "dnf", "install", "-y", pkg]),
    ("pacman", "pacman", lambda pkg: ["sudo", "pacman", "-S", "--noconfirm", pkg]),
    ("zypper", "zypper", lambda pkg: ["sudo", "zypper", "--non-interactive", "install", pkg]),
]


def _install_command(name: str, info: dict, which: Callable[[str], str | None]):
    """The (description, command) that installs `name` on this OS, or None when no supported
    package manager is available."""
    if os.name == "nt":
        winget = which("winget")
        if winget and info.get("winget"):
            return (
                f"winget install {info['winget']}",
                [
                    winget,
                    "install",
                    "-e",
                    "--id",
                    info["winget"],
                    "--silent",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ],
            )
        return None
    if sys.platform == "darwin":
        brew = which("brew")
        if brew and info.get("brew"):
            return (f"brew install {info['brew']}", [brew, "install", info["brew"]])
        if name == "git":  # git also ships with the Xcode command-line tools
            return ("xcode-select --install", ["xcode-select", "--install"])
        return None
    for key, tool, builder in _LINUX_MANAGERS:  # Linux: first detected distro manager
        if which(tool) and info.get(key):
            return (" ".join(builder(info[key])), builder(info[key]))
    return None


def can_install_tool(name: str, *, which: Callable[[str], str | None] = shutil.which) -> bool:
    """Whether aGiTrack can install `name` automatically on this machine (a supported package
    manager is present). Used to decide whether to even offer the install."""
    info = _TOOLS.get(name)
    return info is not None and _install_command(name, info, which) is not None


def _tool_bin_candidates(name: str) -> list[str]:
    """Directories the freshly-installed tool may live in, to add to PATH so it resolves
    without reopening the terminal (mainly needed on Windows after winget)."""
    if os.name == "nt":
        by_tool: dict[str, list[tuple[str, ...]]] = {"git": [("Git", "cmd"), ("Git", "bin")], "gh": [("GitHub CLI",)]}
        subdirs = by_tool.get(name, [])
        bases = [
            os.environ.get("ProgramW6432"),
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            r"C:\Program Files",
        ]
        return [os.path.join(base, *parts) for base in bases if base for parts in subdirs]
    return ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]


def _add_dirs_to_path(dirs: list[str]) -> None:
    existing = os.environ.get("PATH", "")
    parts = existing.split(os.pathsep) if existing else []
    additions = [d for d in dirs if d and os.path.isdir(d) and d not in parts]
    if additions:
        os.environ["PATH"] = os.pathsep.join([*additions, *parts])


def ensure_powershell_execution_policy(output_fn: Callable[[str], None] = print) -> None:
    """On Windows, make sure the per-user PowerShell execution policy allows the signed/local
    scripts that npm installs as ``.ps1`` shims (``npm.ps1``, ``claude.ps1``). Sets the
    CurrentUser policy to ``RemoteSigned`` when it's currently more restrictive (the default
    ``Restricted`` blocks them with "running scripts is disabled on this system").

    aGiTrack itself launches backends via the ``.cmd`` shim through ``cmd.exe`` and does NOT
    need this; it's a courtesy so the user's own ``npm``/``claude`` work in PowerShell too.
    No-op off Windows; never raises."""
    if os.name != "nt":
        return
    try:
        import winreg  # type: ignore[import-not-found,unused-ignore]  # Windows-only stdlib
    except ImportError:
        return
    reg: Any = winreg  # Any so mypy on Linux doesn't flag the win32-only registry members
    key_path = r"Software\Microsoft\PowerShell\1\ShellIds\Microsoft.PowerShell"
    acceptable = {"remotesigned", "unrestricted", "bypass"}
    try:
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, key_path, 0, reg.KEY_READ | reg.KEY_SET_VALUE) as key:
            try:
                current, _ = reg.QueryValueEx(key, "ExecutionPolicy")
            except OSError:
                current = ""
            if str(current).strip().lower() in acceptable:
                return
            reg.SetValueEx(key, "ExecutionPolicy", 0, reg.REG_SZ, "RemoteSigned")
        output_fn("Set the PowerShell execution policy (current user) to RemoteSigned so npm/claude scripts run.\n")
    except OSError:
        return


def install_system_tool(
    name: str,
    *,
    output_fn: Callable[[str], None] = print,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> bool:
    """Install the system tool `name` ("git" or "gh") with the platform package manager.
    Returns True only when it's actually runnable afterwards. Never raises."""
    info = _TOOLS.get(name)
    if info is None:
        return False
    plan = _install_command(name, info, which)
    if plan is None:
        output_fn(f"Could not install {info['label']} automatically (no supported package manager found).\n")
        return False
    description, command = plan
    output_fn(f"\nInstalling {info['label']} — {description}\n")
    try:
        result = run(command, timeout=900)
    except (OSError, subprocess.SubprocessError) as error:
        output_fn(f"  install failed: {error}\n")
        return False
    if getattr(result, "returncode", 1) != 0:
        output_fn(f"  {info['label']} install did not complete successfully.\n")
        return False
    _add_dirs_to_path(_tool_bin_candidates(name))
    if which(name) is not None:
        output_fn(f"{info['label']} installed.\n")
        return True
    output_fn(f"{info['label']} was installed but isn't on PATH yet; open a new terminal.\n")
    return False
