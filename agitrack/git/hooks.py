"""Base-repo ``pre-commit`` guard: stop an aGiTrack worktree-mode agent from committing
straight into the base repo.

The guard is **scoped by an environment marker** so it only ever affects the agent aGiTrack
spawned in worktree mode:

* aGiTrack sets ``AGITRACK_WORKTREE_GUARD=1`` in the agent child's environment — and ONLY
  there, ONLY in worktree mode. Every ``git`` the agent runs inherits it.
* The hook is a no-op unless that variable is present. So the user's own commits, commits from
  an agent run *outside* aGiTrack, and commits from a ``--no-worktree`` agent (none of which
  carry the marker) are never blocked — they commit freely.
* Commits inside a *linked worktree* (the agent's sandbox) are always allowed; only commits in
  the base/main working tree are rejected.

Because the marker gates everything, a hook left behind by a crash is harmless: with no marker
in the environment it simply exits 0 for everyone. A pre-existing project ``pre-commit`` hook is
preserved (moved aside and chained), and restored on removal.

The hook is a POSIX ``sh`` script; Git for Windows runs hooks through its bundled ``sh``, so the
same script works on Windows too.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

# Set by aGiTrack on the agent child (worktree mode only); read by the hook.
ENV_GUARD = "AGITRACK_WORKTREE_GUARD"

_MARKER = "# AGITRACK-BASE-COMMIT-GUARD"
_REFTX_MARKER = "# AGITRACK-BASE-REFTX-GUARD"
_ORIG_SUFFIX = ".agitrack-orig"

_HOOK_SCRIPT = f"""#!/bin/sh
{_MARKER}
# Installed by aGiTrack. Blocks an aGiTrack worktree-mode agent from committing into the base
# repo; a harmless no-op for everyone else (the marker below is set ONLY on that agent's
# process). Remove aGiTrack's worktree sessions and this hook stops doing anything.
if [ -n "${{{ENV_GUARD}}}" ]; then
  case "$(git rev-parse --absolute-git-dir 2>/dev/null)" in
    */worktrees/*)
      : ;;  # inside a linked worktree (the agent's sandbox) -> allowed
    *)
      # Deliberately do NOT name git's hook-bypass flag in this message: it is only ever shown to
      # the AGENT (the user is never blocked — they carry no marker), so naming the bypass would
      # just hand the agent a way around the guard.
      echo "aGiTrack: this is a worktree session — commit inside your worktree, not the base repo." >&2
      echo "aGiTrack auto-commits and merges your worktree changes for you." >&2
      exit 1 ;;
  esac
fi
# Chain to any project pre-commit hook aGiTrack moved aside.
_agitrack_orig="$0{_ORIG_SUFFIX}"
if [ -x "$_agitrack_orig" ]; then
  exec "$_agitrack_orig" "$@"
fi
exit 0
"""

# The SAME guard at an enforcement point `--no-verify` cannot reach.
#
# `git commit --no-verify` skips `pre-commit` (and `commit-msg`) — that is git's documented
# behaviour and no hook can change it. So the guard above is advisory: an agent that passes the
# flag walks straight past it, its commit lands on the user's base branch, and nothing ever
# reconciles it (in worktree mode `_uncovered_backend_commits` only scans the session's managed
# turn branch, so a base-branch commit is invisible to it forever).
#
# `reference-transaction` is not skipped by `--no-verify` — verified against real git — and a
# non-zero exit on the `prepared` phase aborts the whole ref update, so the commit never lands.
# It is therefore the backstop, and `pre-commit` stays as the FRIENDLY first line: it fails
# earlier, with a better message, and works on git < 2.28 where this hook does not exist.
#
# Scope is deliberately narrow, because this hook fires on EVERY ref update — fetch, checkout,
# tag, branch, reset — and over-reaching here would break the agent's git entirely rather than
# just blocking a commit. Measured against real git, each operation touches:
#
#     commit         refs/heads/<branch>   old = the previous commit   (a MOVE)
#     branch create  refs/heads/<name>     old = 0000…                 (a CREATE)
#     checkout       HEAD only
#     tag            refs/tags/<name>
#     fetch          refs/remotes/…
#
# So the rule is exactly "a branch in the base repo MOVED": ``refs/heads/*`` with a non-zero old
# value. Checkout, tag and fetch are untouched. Branch CREATION is deliberately allowed too — it
# moves no history, and `pre-commit` already covers the (rare) first-commit-on-a-new-branch case
# for anyone not bypassing it.
_REFTX_HOOK_SCRIPT = f"""#!/bin/sh
{_REFTX_MARKER}
# Installed by aGiTrack. Backstop for the pre-commit guard, which git's hook-bypass flag
# skips (deliberately not named here — the agent can read this file); a
# harmless no-op for everyone else (the marker below is set ONLY on the agent's process).
# Stdin is "<old> <new> <ref>" per line and is consumed here, so it is replayed to any chained
# project hook below.
_agitrack_refs=$(cat)
if [ "$1" = "prepared" ] && [ -n "${{{ENV_GUARD}}}" ]; then
  case "$(git rev-parse --absolute-git-dir 2>/dev/null)" in
    */worktrees/*)
      : ;;  # inside a linked worktree (the agent's sandbox) -> allowed
    *)
      # Only a branch that MOVED: creations (old all-zeros), HEAD, tags and remote refs pass.
      if printf '%s\\n' "$_agitrack_refs" | awk '$3 ~ /^refs\\/heads\\// && $1 !~ /^0+$/ {{ found = 1 }} END {{ exit !found }}'; then
        # As with the pre-commit guard, never name git's bypass flag: this is only ever shown
        # to the AGENT, and there is no bypass for this hook anyway.
        echo "aGiTrack: this is a worktree session — commit inside your worktree, not the base repo." >&2
        echo "aGiTrack auto-commits and merges your worktree changes for you." >&2
        exit 1
      fi ;;
  esac
fi
# Chain to any project reference-transaction hook aGiTrack moved aside, replaying its stdin.
_agitrack_orig="$0{_ORIG_SUFFIX}"
if [ -x "$_agitrack_orig" ]; then
  printf '%s\\n' "$_agitrack_refs" | "$_agitrack_orig" "$@"
  exit $?
fi
exit 0
"""


# ---------------------------------------------------------------------------
# Manual-commit-mode hooks (opt-in, --manual-commits): fold the pending agent
# interaction trace/metadata into the user's own commit so a single commit stays
# fully tracked, then reset the latent ref. Both are pure ``sh`` and dependency-
# free — they read files aGiTrack pre-renders under ``<repo>/.agitrack/`` (the
# trailer text and the latent ref name), so no python is spawned on the hot path.
# They follow the same safe pattern as the guard: unique marker, chain-preserving
# ``.agitrack-orig`` backup, and (installed) only in manual mode.

_MANUAL_MSG_MARKER = "# AGITRACK-MANUAL-COMMIT-MSG"
_MANUAL_DONE_MARKER = "# AGITRACK-MANUAL-COMMIT-DONE"

_PENDING_TRAILER_REL = ".agitrack/manual-pending-trailer"
_MANUAL_REF_REL = ".agitrack/manual-ref"
_MANUAL_SIGNAL_REL = ".agitrack/manual-commit-signal"

# The metadata header both manual hooks grep for. `prepare-commit-msg` uses it to stay idempotent
# (never fold twice); `post-commit` uses it to confirm the fold actually happened before it clears
# the pending chain. They MUST agree — see the note in _POST_COMMIT_SCRIPT.
_METADATA_HEADER_LINE = "# aGiTrack Metadata"

_PREPARE_COMMIT_MSG_SCRIPT = f"""#!/bin/sh
{_MANUAL_MSG_MARKER}
# Installed by aGiTrack manual-commit mode. Appends the pending agent interaction
# trace/metadata to this commit's message so the single commit is fully tracked.
# A no-op when the pre-rendered trailer is absent/empty (i.e. not manual mode).
_agitrack_chain() {{
  _orig="$0{_ORIG_SUFFIX}"
  [ -x "$_orig" ] && exec "$_orig" "$@"
  exit 0
}}
# Skip merges, squashes and amends: only a fresh normal/message/template commit
# should carry the trailer (an amend would double it).
case "$2" in
  merge|squash|commit) _agitrack_chain "$@" ;;
esac
_root="$(git rev-parse --show-toplevel 2>/dev/null)" || _root="."
_trailer="$_root/{_PENDING_TRAILER_REL}"
# Idempotent: never append twice (the trailer carries its own metadata header).
if [ -s "$_trailer" ] && ! grep -q '^{_METADATA_HEADER_LINE}$' "$1"; then
  printf '\n' >> "$1"
  cat "$_trailer" >> "$1"
fi
_agitrack_chain "$@"
"""

_POST_COMMIT_SCRIPT = f"""#!/bin/sh
{_MANUAL_DONE_MARKER}
# Installed by aGiTrack manual-commit mode. After a commit folds in the pending
# agent turns, advance EVERY latent ref that was folded to the new commit (pending
# turns are now 0), clear the pre-rendered trailer, and signal aGiTrack to re-render it.
# One ref per line: a commit folds the pending turns of every session that ran in this
# working tree (a new session / backend switch moves the ref), so each must be advanced
# or its turns would be folded again into the NEXT commit.
#
# Every exit path goes through `_agitrack_chain`, exactly as prepare-commit-msg does. The early
# `exit 0` below fires on every commit WITHOUT aGiTrack metadata — i.e. every ordinary human
# commit — and it used to return before the chain block at the bottom, so a project's own
# post-commit hook (a CI trigger, a notification, a ctags rebuild) went silently dead from
# `agitrack -b` onward, and `-b stop` did not bring it back; only `--remove-hooks` did.
_agitrack_chain() {{
  _orig="$0{_ORIG_SUFFIX}"
  [ -x "$_orig" ] && exec "$_orig" "$@"
  exit 0
}}
_root="$(git rev-parse --show-toplevel 2>/dev/null)" || _root="."
_reffile="$_root/{_MANUAL_REF_REL}"
_agitrack_cr="$(printf '\\r')"
# Only advance the refs if the fold ACTUALLY happened. `prepare-commit-msg` deliberately skips
# folding for an amend/squash/merge-template commit (source `commit`/`squash`/`merge`, see its
# case statement), and this hook used to reset regardless — so `git commit --amend --no-edit`
# with a turn still pending cleared the chain without the trailer ever landing anywhere. The
# turn's trace and tokens were gone, silently and permanently. The two hooks have to reach the
# SAME decision, and the commit's own message is the only honest evidence of what was folded.
if ! git log -1 --format=%B 2>/dev/null | grep -q '^{_METADATA_HEADER_LINE}$'; then
  _agitrack_chain "$@"
fi
if [ -f "$_reffile" ]; then
  while IFS= read -r _ref || [ -n "$_ref" ]; do
    # Strip a trailing CR: a file written by an older aGiTrack on Windows has CRLF endings, and
    # `read -r` keeps the CR, which makes the ref name invalid and silently skips the reset.
    _ref="${{_ref%$_agitrack_cr}}"
    [ -n "$_ref" ] && git update-ref "$_ref" HEAD 2>/dev/null || true
  done < "$_reffile"
fi
: > "$_root/{_PENDING_TRAILER_REL}" 2>/dev/null || true
touch "$_root/{_MANUAL_SIGNAL_REL}" 2>/dev/null || true
# Chain to any project post-commit hook aGiTrack moved aside.
_agitrack_chain "$@"
"""


# ---------------------------------------------------------------------------
# Persistent auto-track pre-commit hook (feature: remind / auto-start on commit).
#
# Unlike the worktree guard and the manual-commit hooks (installed/removed per run), this hook
# is PERSISTENT: it stays after aGiTrack exits so a `git commit` made when aGiTrack isn't running
# still gets its AI work tracked. On commit it invokes ``agitrack --precommit-sync`` (with the
# python + repo baked in at install time, so it needs nothing on PATH), which — only when the AI
# actually made changes — records the pending turns and renders the fold trailer so the trace and
# metadata land in THIS commit, and (unless ``autotrack_hook`` is off) auto-starts the background
# tracker for future commits. Best-effort and non-blocking: it never fails a commit, adds no
# footprint to a purely human commit, and is a no-op inside a linked worktree (aGiTrack drives
# those itself). It chains any pre-existing pre-commit hook, and coexists with the worktree guard
# via the same ``.agitrack-orig`` backup/restore the guard already uses.

_AUTOTRACK_MARKER = "# AGITRACK-AUTOTRACK-PRECOMMIT"
# Stamped into the hook so a later aGiTrack can tell whether the installed hook's SCHEMA is current
# and, if not, replace it (see ``install_autotrack_precommit_hook``). The prefix is fixed; the
# version string follows on the same line.
_AUTOTRACK_VERSION_MARKER = "# AGITRACK-AUTOTRACK-VERSION"


def _sh_quote(arg: str) -> str:
    """POSIX single-quote a token for the hook script (also valid under Git-for-Windows' sh)."""
    return "'" + arg.replace("'", "'\\''") + "'"


def _autotrack_precommit_script(invoke: list[str], repo_root: str, version: str) -> str:
    # The baked invocation runs THIS aGiTrack (a stable interpreter path + `-m agitrack`, or the
    # frozen exe). After a self-update it resolves the NEW code automatically (the interpreter/exe
    # path is stable). A PATH fallback (`agitrack …`) covers the rare case the baked path moved, so
    # the hook always calls the CURRENT aGiTrack, never a stale one.
    baked = " ".join(_sh_quote(part) for part in invoke)
    root = _sh_quote(repo_root)
    return f"""#!/bin/sh
{_AUTOTRACK_MARKER}
{_AUTOTRACK_VERSION_MARKER} {version}
# Installed by aGiTrack. On `git commit`, if aGiTrack is not already tracking this repo, record
# any pending AI turns and fold their interaction trace + token metadata into THIS commit (only
# when the AI made changes since the last commit), and auto-start the background tracker for the
# turns that follow. Best-effort and non-blocking: it never fails the commit.
case "$(git rev-parse --absolute-git-dir 2>/dev/null)" in
  */worktrees/*)
    : ;;  # inside a session worktree -> aGiTrack already handles it
  *)
    # `>/dev/null` drops any stray stdout but KEEPS stderr, so aGiTrack's user-facing messages
    # (auto-started …, reminders, an available update) show in the `git commit` output.
    {baked} --precommit-sync --repo {root} >/dev/null \\
      || agitrack --precommit-sync --repo {root} >/dev/null \\
      || true ;;
esac
# Chain to any pre-commit hook aGiTrack moved aside (a project hook, or the worktree guard).
_agitrack_orig="$0{_ORIG_SUFFIX}"
if [ -x "$_agitrack_orig" ]; then
  exec "$_agitrack_orig" "$@"
fi
exit 0
"""


def is_autotrack_hook(path: Path) -> bool:
    """Whether *path* is the aGiTrack persistent auto-track pre-commit hook."""
    return _hook_has_marker(path, _AUTOTRACK_MARKER)


def autotrack_hook_version(path: Path) -> str | None:
    """The aGiTrack version stamped into the installed auto-track ``pre-commit`` hook at *path*, or
    ``None`` when *path* isn't our hook or predates version stamping (an older schema with no line)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if _AUTOTRACK_MARKER not in text:
        return None
    match = re.search(rf"^{re.escape(_AUTOTRACK_VERSION_MARKER)}\s+(\S+)", text, re.M)
    return match.group(1) if match else None


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse a dotted version into an int tuple for ordering; non-numeric parts sort as 0 so a
    comparison never raises on an odd version string."""
    parts: list[int] = []
    for chunk in version.split("."):
        match = re.match(r"\d+", chunk)
        parts.append(int(match.group()) if match else 0)
    return tuple(parts)


def install_autotrack_precommit_hook(
    hooks_dir: Path,
    *,
    invoke: list[str],
    repo_root: str,
    version: str | None = None,
    debug: Callable[[str], None] | None = None,
) -> bool:
    """Install the persistent auto-track ``pre-commit`` hook (idempotent). ``invoke`` is the argv
    prefix that runs THIS aGiTrack (``agitrack.proc.agitrack_invocation()`` — frozen-aware) and is
    baked in with a PATH fallback so the hook always calls the CURRENT aGiTrack after a self-update.
    ``version`` is stamped into the hook so a later launch can detect a schema change.

    Whenever the installed hook's stamped version is OLDER than ``version`` (or unstamped — an older
    schema), the previously installed aGiTrack hook is fully removed first (restoring any hook it
    chained) and then re-installed fresh, so a changed hook schema is replaced cleanly rather than
    left half-migrated. A pre-existing non-aGiTrack hook is chained via ``pre-commit.agitrack-orig``.
    Re-run on every aGiTrack launch, so the baked path is refreshed if the install location changes."""
    if version is None:
        from agitrack import __version__

        version = __version__
    hook = hooks_dir / "pre-commit"
    if is_autotrack_hook(hook):
        installed = autotrack_hook_version(hook)
        if installed is None or _version_tuple(installed) < _version_tuple(version):
            if debug:
                debug(f"autotrack hook schema outdated ({installed} < {version}); replacing")
            _remove_hook(hooks_dir, "pre-commit", _AUTOTRACK_MARKER, debug=debug)
    return _install_hook(
        hooks_dir, "pre-commit", _autotrack_precommit_script(invoke, repo_root, version), _AUTOTRACK_MARKER, debug=debug
    )


def remove_autotrack_precommit_hook(hooks_dir: Path, *, debug: Callable[[str], None] | None = None) -> None:
    """Remove the persistent auto-track pre-commit hook and restore any chained original. No-op
    unless the current ``pre-commit`` is ours."""
    _remove_hook(hooks_dir, "pre-commit", _AUTOTRACK_MARKER, debug=debug)


def _make_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        pass


def _hook_has_marker(path: Path, marker: str) -> bool:
    try:
        return marker in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def is_ours(path: Path) -> bool:
    """Whether *path* is the aGiTrack-installed pre-commit guard (carries our marker)."""
    return _hook_has_marker(path, _MARKER)


def _install_hook(
    hooks_dir: Path, name: str, script: str, marker: str, *, debug: Callable[[str], None] | None = None
) -> bool:
    """Install ``<hooks_dir>/<name>`` (idempotent). A pre-existing non-aGiTrack hook of
    the same name is moved to ``<name>.agitrack-orig`` and chained from ours. Returns
    True on success."""
    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook = hooks_dir / name
        orig = hooks_dir / (name + _ORIG_SUFFIX)
        if hook.exists() and not _hook_has_marker(hook, marker):
            # Preserve the user's hook (only back up once; don't clobber an existing backup).
            if not orig.exists():
                hook.rename(orig)
                _make_executable(orig)
        # LF endings, always: these are POSIX `sh` scripts. Path.write_text uses newline=None,
        # which on Windows would give the shebang a trailing CR — Git-for-Windows' bundled sh
        # tolerates that, but nothing else does (a plain `sh` reports "cannot exec: /bin/sh\r").
        with open(hook, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        _make_executable(hook)
        return True
    except OSError as error:
        if debug:
            debug(f"install {name} hook failed: {error!r}")
        return False


def _remove_hook(hooks_dir: Path, name: str, marker: str, *, debug: Callable[[str], None] | None = None) -> None:
    """Remove ``<hooks_dir>/<name>`` and restore any chained original. No-op unless the
    current hook is ours (we never touch a hook we didn't install)."""
    try:
        hook = hooks_dir / name
        if not hook.exists() or not _hook_has_marker(hook, marker):
            return
        orig = hooks_dir / (name + _ORIG_SUFFIX)
        hook.unlink()
        if orig.exists():
            orig.rename(hook)  # restore the project's original hook
            _make_executable(hook)
    except OSError as error:
        if debug:
            debug(f"remove {name} hook failed: {error!r}")


def install_base_commit_guard(hooks_dir: Path, *, debug: Callable[[str], None] | None = None) -> bool:
    """Install the guard (idempotent), as BOTH ``pre-commit`` and ``reference-transaction``.
    Pre-existing non-aGiTrack hooks are moved to ``<name>.agitrack-orig`` and chained from ours.

    Two hooks because one of them is bypassable: ``--no-verify`` skips ``pre-commit``, so alone
    it is advisory. ``reference-transaction`` is not skipped and aborts the ref update itself, so
    the commit cannot land. ``pre-commit`` is kept because it fails earlier, with a friendlier
    message, and exists on every git version.

    Returns True when the ``pre-commit`` guard installed — the reference-transaction hook is
    best-effort (git < 2.28 simply never calls it), so its absence must not report failure and
    make callers think the session is unguarded.
    """
    ok = _install_hook(hooks_dir, "pre-commit", _HOOK_SCRIPT, _MARKER, debug=debug)
    _install_hook(hooks_dir, "reference-transaction", _REFTX_HOOK_SCRIPT, _REFTX_MARKER, debug=debug)
    return ok


def remove_base_commit_guard(hooks_dir: Path, *, debug: Callable[[str], None] | None = None) -> None:
    """Remove both guard hooks and restore any chained originals. No-op for either hook that
    isn't ours (we never touch a hook we didn't install)."""
    _remove_hook(hooks_dir, "pre-commit", _MARKER, debug=debug)
    _remove_hook(hooks_dir, "reference-transaction", _REFTX_MARKER, debug=debug)


def install_manual_commit_hooks(hooks_dir: Path, *, debug: Callable[[str], None] | None = None) -> bool:
    """Install the manual-commit-mode ``prepare-commit-msg`` (fold the pending trailer)
    and ``post-commit`` (reset the latent ref) hooks. Idempotent; pre-existing project
    hooks are chained. Returns True only if BOTH installed."""
    ok_msg = _install_hook(hooks_dir, "prepare-commit-msg", _PREPARE_COMMIT_MSG_SCRIPT, _MANUAL_MSG_MARKER, debug=debug)
    ok_done = _install_hook(hooks_dir, "post-commit", _POST_COMMIT_SCRIPT, _MANUAL_DONE_MARKER, debug=debug)
    return ok_msg and ok_done


def remove_manual_commit_hooks(hooks_dir: Path, *, debug: Callable[[str], None] | None = None) -> None:
    """Remove the manual-commit-mode hooks and restore any chained originals."""
    _remove_hook(hooks_dir, "prepare-commit-msg", _MANUAL_MSG_MARKER, debug=debug)
    _remove_hook(hooks_dir, "post-commit", _MANUAL_DONE_MARKER, debug=debug)


def remove_all_installed_hooks(hooks_dir: Path, *, debug: Callable[[str], None] | None = None) -> list[str]:
    """Remove EVERY aGiTrack-installed git hook (the persistent auto-track ``pre-commit``, the
    worktree base-commit guard, and the manual-commit ``prepare-commit-msg`` / ``post-commit`` fold
    hooks), restoring any hooks they chained. Returns the hook names that were ours and got removed.
    Used by ``agitrack --remove-hooks`` so a user can fully opt out of commit-time tracking."""
    removed: list[str] = []
    # pre-commit is listed twice (auto-track, then the guard) so a chained guard restored by the
    # first removal is itself removed by the second — _remove_hook is a no-op unless the current
    # hook carries that exact marker.
    for name, marker in (
        ("pre-commit", _AUTOTRACK_MARKER),
        ("pre-commit", _MARKER),
        # The guard is installed as BOTH pre-commit and reference-transaction (see
        # install_base_commit_guard); omitting the second one meant the documented full opt-out
        # left an aGiTrack hook running on EVERY ref update (commit, branch, fetch, checkout,
        # reset) forever, and left the user's own chained hook shadowed in .agitrack-orig —
        # while `--remove-hooks` printed "Any chained project hooks were restored."
        ("reference-transaction", _REFTX_MARKER),
        ("prepare-commit-msg", _MANUAL_MSG_MARKER),
        ("post-commit", _MANUAL_DONE_MARKER),
    ):
        hook = hooks_dir / name
        if hook.exists() and _hook_has_marker(hook, marker):
            _remove_hook(hooks_dir, name, marker, debug=debug)
            if name not in removed:
                removed.append(name)
    return removed
