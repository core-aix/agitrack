"""Make a freshly-installed agent CLI reachable from the user's OTHER terminals.

aGiTrack installs a backend into a per-user bin directory (``~/.local/bin``,
``~/.opencode/bin``, npm's global prefix, ``%APPDATA%\\npm``). The official installers only
update the PATH that *future* shells inherit — and the ``curl | bash`` ones often just print
a note about it — while aGiTrack additionally prepends the directory to its OWN process PATH
so the agent is runnable immediately. The result, if nothing else is done, is a CLI that
works inside aGiTrack and reports "command not found" in every other terminal, leaving the
user to discover the right rc file themselves.

So after an install aGiTrack offers to write that directory into the user's shell profile
(POSIX) or their per-user PATH (Windows registry). Two rules shape everything here:

* **Never without consent.** This edits a file (or registry value) the user owns, so it is
  always offered and never done silently.
* **A failure must not pass unnoticed.** If the write fails, the manual instructions are
  shown and the user has to acknowledge them, so nobody discovers the missing PATH later.

Pure/dependency-injected (``input_fn``/``output_fn``/``environ``) so every branch is testable
on any OS. Never raises.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

from agitrack.console import ask
from agitrack.proc import INHERITED_PATH

# Every line aGiTrack writes carries this marker: it makes the edit obvious to a user reading
# their own rc file, and lets a later install recognise its own work instead of appending a
# duplicate export on every run.
MARKER = "# added by aGiTrack (agent CLI install)"


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def shell_profile_path(*, environ: Any = None, platform: str | None = None) -> Path:
    """The file a NEW interactive shell of the user's login shell reads.

    Chosen from ``$SHELL`` because that is the shell the user will actually open next:
    zsh reads ``~/.zshrc``, fish ``~/.config/fish/config.fish``, and bash reads
    ``~/.bash_profile`` on macOS (where terminals start login shells) but ``~/.bashrc`` on
    Linux. Anything unrecognised falls back to ``~/.profile``, which every POSIX shell reads.
    """
    env = os.environ if environ is None else environ
    system = sys.platform if platform is None else platform
    shell = os.path.basename((env.get("SHELL") or "").strip())
    home = _home()
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "fish":
        return home / ".config" / "fish" / "config.fish"
    if shell == "bash":
        return home / (".bash_profile" if system == "darwin" else ".bashrc")
    return home / ".profile"


def _home_relative(directory: str) -> str:
    """``$HOME``-relative form of *directory*, so the written line survives a moved home."""
    home = str(_home())
    if directory == home:
        return "$HOME"
    if directory.startswith(home + os.sep):
        return "$HOME/" + directory[len(home) + 1 :].replace(os.sep, "/")
    return directory


def profile_line(directory: str, *, profile: Path | None = None) -> str:
    """The line to append to the shell profile so *directory* is on PATH in new shells."""
    target = profile if profile is not None else shell_profile_path()
    if target.name == "config.fish":
        # fish has no `export`; fish_add_path is idempotent and prepends by default.
        return f'fish_add_path "{_home_relative(directory)}"'
    return f'export PATH="{_home_relative(directory)}:$PATH"'


def on_inherited_path(directory: str, *, inherited: str | None = None) -> bool:
    """Whether the shell that launched aGiTrack already had *directory* on its PATH — i.e.
    the user's other terminals find the command too, and there is nothing to fix."""
    path = INHERITED_PATH if inherited is None else inherited
    entries = [part for part in path.split(os.pathsep) if part]
    target = os.path.normcase(os.path.normpath(directory))
    return any(os.path.normcase(os.path.normpath(part)) == target for part in entries)


def already_in_profile(directory: str, *, profile: Path | None = None) -> bool:
    """Whether the shell profile already puts *directory* on PATH (aGiTrack's own line from
    an earlier run, or something the user wrote themselves)."""
    target = profile if profile is not None else shell_profile_path()
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return directory in text or _home_relative(directory) in text


def persist_path_entry(directory: str, *, profile: Path | None = None) -> tuple[bool, str]:
    """Put *directory* on the PATH of the user's future shells.

    POSIX appends a marked line to the shell profile; Windows appends to the per-user ``Path``
    in the registry (the value the OS hands to new processes), which is why neither needs
    admin rights. Returns ``(ok, detail)`` — *detail* names where it was written, or says why
    it could not be. Never raises: a failure is reported so the caller can fall back to
    telling the user what to do by hand."""
    if os.name == "nt":
        return _persist_windows(directory)
    target = profile if profile is not None else shell_profile_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix}\n{MARKER}\n{profile_line(directory, profile=target)}\n")
    except OSError as error:
        return (False, f"could not write {target}: {error}")
    return (True, str(target))


def _persist_windows(directory: str) -> tuple[bool, str]:
    """Append *directory* to the per-user PATH in the registry (HKCU\\Environment).

    Deliberately NOT ``setx``: it truncates the value at 1024 characters, which silently
    destroys a long PATH. New processes pick the value up; already-open terminals do not,
    which is why the caller still says to open a new one."""
    try:
        import winreg  # type: ignore[import-not-found,unused-ignore]  # Windows-only stdlib
    except ImportError:  # pragma: no cover - only reachable off Windows
        return (False, "the Windows registry is unavailable")
    reg: Any = winreg  # Any so mypy on POSIX doesn't flag the win32-only members
    try:
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, "Environment", 0, reg.KEY_READ | reg.KEY_SET_VALUE) as key:
            try:
                current, value_type = reg.QueryValueEx(key, "Path")
            except OSError:
                current, value_type = "", reg.REG_EXPAND_SZ
            parts = [part for part in str(current).split(os.pathsep) if part]
            if any(
                os.path.normcase(os.path.normpath(part)) == os.path.normcase(os.path.normpath(directory))
                for part in parts
            ):
                return (True, "your user PATH (already listed)")
            updated = os.pathsep.join([*parts, directory])
            reg.SetValueEx(key, "Path", 0, value_type or reg.REG_EXPAND_SZ, updated)
    except OSError as error:
        return (False, f"could not update your user PATH: {error}")
    return (True, "your user PATH (HKCU\\Environment)")


def manual_path_instructions(directory: str, *, profile: Path | None = None) -> str:
    """What to run by hand when aGiTrack could not do it — one block per platform so the
    user sees a command that works wherever they are."""
    target = profile if profile is not None else shell_profile_path()
    return "\n\n".join(
        [
            f"To use the agent CLI outside aGiTrack, add this directory to your PATH:\n  {directory}",
            f"  macOS / Linux:  echo '{profile_line(directory, profile=target)}' >> {target}",
            f'  Windows (PowerShell):  setx PATH "$env:PATH;{directory}"',
            "Then open a NEW terminal so the updated PATH is picked up.",
        ]
    )


def offer_to_persist_path(
    directory: str,
    *,
    label: str,
    command: str,
    input_fn: Callable[[str], str] = ask,
    output_fn: Callable[[str], None] = print,
    persist_fn: Callable[..., tuple[bool, str]] = persist_path_entry,
    interactive: bool = True,
) -> bool:
    """Offer to put *directory* (where ``command`` was just installed) on the user's PATH.

    Returns True when future shells will find the command — because they already did, or
    because the entry was written. A user who declines gets the manual instructions; a write
    that FAILS gets them too and must be acknowledged, so the one outcome that leaves the
    command unusable outside aGiTrack cannot scroll past unread."""
    if not directory or on_inherited_path(directory):
        return True  # the user's shells already find it — nothing to offer
    profile = shell_profile_path()
    if already_in_profile(directory, profile=profile):
        # Written by an earlier run (or by the user): new shells are fine, this one just
        # hasn't re-read it. Say so rather than offering a duplicate line.
        output_fn(
            f"\n\n{label} is installed in {directory}, which {profile} already adds to your PATH.\n"
            "Open a NEW terminal (or re-source that file) to use it outside aGiTrack."
        )
        return True
    where = "your user PATH" if os.name == "nt" else str(profile)
    output_fn(
        f"\n\n{label} was installed to {directory}, which is not on your PATH — so `{command}` "
        f"works inside aGiTrack now, but not in your other terminals."
    )
    if not interactive:
        output_fn("\n" + manual_path_instructions(directory, profile=profile))
        return False
    try:
        answer = input_fn(f"\nAdd it to your PATH in {where}? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if answer in {"n", "no"}:
        output_fn("\nLeaving your PATH unchanged.\n")
        output_fn(manual_path_instructions(directory, profile=profile))
        return False
    ok, detail = persist_fn(directory, profile=profile) if os.name != "nt" else persist_fn(directory)
    if ok:
        output_fn(f"\nAdded to {detail}. Open a NEW terminal for `{command}` to work there.\n")
        return True
    # The one outcome the user MUST take away with them: aGiTrack could not do it, so the
    # command stays aGiTrack-only until they set PATH themselves. Say why, show how, and
    # require an acknowledgment rather than letting it scroll past.
    output_fn(f"\nCould not update your PATH automatically — {detail}.\n")
    output_fn(manual_path_instructions(directory, profile=profile))
    try:
        input_fn("\nPress Enter to acknowledge that you'll need to set PATH yourself: ")
    except (EOFError, KeyboardInterrupt):
        pass
    return False
