"""Self-updater for aGiT.

aGiT can update itself in place. Two installation shapes are supported:

* **source-linked** — aGiT is importable from a git checkout of its own source
  (the documented ``pip install -e .`` editable install). Updates merge the
  upstream branch into the checkout: a clean checkout fast-forwards, and a
  checkout carrying the user's own commits (aGiT runs on session worktree
  branches that accumulate commits) gets a normal merge. Only a genuine content
  conflict, or an uncommitted (dirty) tree, blocks the update — with a message
  instead of leaving the running source half-merged.
* **package** — aGiT was installed as a wheel (e.g. ``pip install agit``).
  Updates run ``pip install --upgrade`` and the latest available version is
  discovered with ``pip index versions``.

The checking logic here is intentionally pure/blocking; callers (the CLI at
startup, the proxy runner's idle loop) run :meth:`Updater.check` in a background
thread so the terminal never stalls on a network ``git fetch``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

import agit

# The PyPI distribution name. The import package and the installed command are
# both ``agit``; the plain ``agit`` name on PyPI belongs to an unrelated project,
# so aGiT is published as ``agit-ai`` (see pyproject.toml / scripts/publish.sh).
DIST_NAME = "agit-ai"

# How aGiT was installed, as reported by ``UpdateStatus.kind`` / ``Updater.kind``.
KIND_SOURCE = "source"
KIND_PACKAGE = "package"
KIND_UNKNOWN = "unknown"

# A short network timeout so a startup check never hangs the terminal. The
# periodic in-session check runs on a worker thread, but keep it bounded anyway.
_NET_TIMEOUT = 20
# The startup check blocks launch, so bound it much tighter: an offline user waits
# at most this long before aGiT starts anyway.
STARTUP_NET_TIMEOUT = 6

# Sentinel so callers can inject ``source_repo=None`` to mean "no source-linked
# install" (force the package path), distinct from "not provided -> auto-detect".
_DETECT = object()


@dataclass
class UpdateStatus:
    """Result of an update check.

    ``available`` is the only field callers must consult to decide whether to
    offer an update; the rest drive the user-facing message.
    """

    kind: str  # KIND_SOURCE | KIND_PACKAGE | KIND_UNKNOWN
    available: bool = False
    current: str = ""  # current revision (short sha) or version
    latest: str = ""  # target revision (short sha) or version
    behind: int = 0  # commits behind upstream (source installs only)
    message: str = ""  # human-readable summary for the UI
    error: str | None = None  # set when the check could not complete
    restart_only: bool = False  # code is already current on disk; only a restart is needed

    @property
    def ok(self) -> bool:
        return self.error is None


def _git(args: list[str], cwd: Path, *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    # GIT_TERMINAL_PROMPT=0: a network git call (fetch) must never block on a
    # credential prompt — offline/auth failures should fail fast, not hang launch.
    # A timeout bounds the rest; on expiry the process is killed and we report it as
    # a plain non-zero result so callers degrade to "couldn't check" gracefully.
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["git", *args], returncode=124, stdout="", stderr="timed out")


def detect_source_repo() -> Path | None:
    """Return the top level of aGiT's own source checkout when the running code
    is imported from one (an editable / source-linked install), else ``None``.

    We trust an install as source-linked only when the directory containing the
    ``agit`` package both is a git work tree *and* carries aGiT's own
    ``pyproject.toml``. We key off the console-script entry point
    (``agit.cli:main``) rather than the distribution ``name`` so the check keeps
    working if the published name changes. That avoids mistaking, say, a wheel
    that happens to sit under some unrelated repository for the real source.
    """
    try:
        module_file = Path(agit.__file__).resolve()
    except (AttributeError, TypeError):
        return None
    root = module_file.parent.parent  # .../<repo>/agit/__init__.py -> <repo>
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return None
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    if "agit.cli:main" not in text:
        return None
    result = _git(["rev-parse", "--show-toplevel"], root)
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


class Updater:
    """Detects how aGiT is installed and performs in-place updates."""

    def __init__(self, *, source_repo: Path | None | object = _DETECT) -> None:
        # source_repo is injectable for tests; production auto-detects it. Passing
        # an explicit None forces the package path (no source checkout).
        self._source_repo: Path | None = (
            detect_source_repo() if source_repo is _DETECT else cast("Path | None", source_repo)
        )
        # The source HEAD the running process was loaded from. Snapshotted HERE, at
        # construction (≈ process start), not on the first successful check: a check
        # gated on a network fetch could miss a purely-local update — offline, or the
        # checkout advancing before the first check — and leave the running process
        # unaware it is stale. Reading HEAD needs no network and is independent of the
        # upstream/fetch outcome. After the checkout moves on under a still-running
        # process (a self-update, a manual pull, or, in self-development, the source
        # advancing), this lets us see the running code is older than disk and prompt
        # a restart instead of reporting "up to date" off the checkout alone.
        self._running_rev: str | None = None
        if self._source_repo is not None:
            head = _git(["rev-parse", "HEAD"], self._source_repo)
            if head.returncode == 0:
                self._running_rev = head.stdout.strip() or None

    @property
    def kind(self) -> str:
        if self._source_repo is not None:
            return KIND_SOURCE
        return KIND_PACKAGE

    @property
    def source_repo(self) -> Path | None:
        return self._source_repo

    # --- checking --------------------------------------------------------

    def check(self, *, fetch: bool = True, timeout: int = _NET_TIMEOUT) -> UpdateStatus:
        """Check whether a newer aGiT is available. Blocking (network); run on a
        worker thread from interactive contexts. ``timeout`` bounds each network
        call — pass a short value (``STARTUP_NET_TIMEOUT``) for the launch-time
        check so an offline user isn't made to wait."""
        if self.kind == KIND_SOURCE:
            return self._check_source(fetch=fetch, timeout=timeout)
        return self._check_package(timeout=timeout)

    def _upstream_ref(self, repo: Path) -> str | None:
        # The current branch's configured upstream (e.g. "origin/main").
        result = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo)
        if result.returncode != 0:
            return None
        ref = result.stdout.strip()
        return ref or None

    def _remote_target(self, repo: Path) -> str | None:
        # The remote-tracking ref to compare/merge against. Prefer the current
        # branch's configured upstream; when the branch has none — aGiT commonly
        # runs on a session worktree branch (``agit/...``) that tracks nothing —
        # fall back to the default branch of ``origin`` so the source still
        # follows upstream aGiT releases. Returns None only when neither exists.
        upstream = self._upstream_ref(repo)
        if upstream is not None:
            return upstream
        head = _git(["rev-parse", "--abbrev-ref", "origin/HEAD"], repo)
        if head.returncode == 0 and head.stdout.strip():
            return head.stdout.strip()
        for candidate in ("origin/main", "origin/master"):
            if _git(["rev-parse", "--verify", "--quiet", candidate], repo).returncode == 0:
                return candidate
        return None

    def _count(self, repo: Path, rev_range: str) -> int:
        # Number of commits in ``rev_range`` (e.g. "HEAD..origin/main"); 0 on error.
        result = _git(["rev-list", "--count", rev_range], repo)
        if result.returncode != 0:
            return 0
        return int(result.stdout.strip() or "0")

    def _check_source(self, *, fetch: bool, timeout: int = _NET_TIMEOUT) -> UpdateStatus:
        repo = self._source_repo
        assert repo is not None
        status = UpdateStatus(kind=KIND_SOURCE)
        # Three commit hashes drive the decision (per the source-install update
        # contract): the RUNNING process's rev, the LOCAL checkout's HEAD, and the
        # REMOTE target's tip. If either the local disk or the remote carries code
        # the running process lacks, an update is available.
        head = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        if self._running_rev is None:  # construction snapshot failed: fall back to now
            self._running_rev = head or None
        running = self._running_rev
        head_short = _git(["rev-parse", "--short", "HEAD"], repo).stdout.strip()
        status.current = head_short

        # The remote side (needs the network). A missing target or a failed fetch is
        # remembered, not fatal — local staleness below is reported either way.
        target = self._remote_target(repo)
        fetch_error: str | None = None
        remote_head: str | None = None
        if target is not None:
            remote = target.split("/", 1)[0]
            if fetch:
                fetched = _git(["fetch", "--quiet", remote], repo, timeout=timeout)
                if fetched.returncode != 0:
                    fetch_error = (fetched.stderr.strip() or "git fetch failed").splitlines()[-1]
            if fetch_error is None:
                resolved = _git(["rev-parse", "--verify", "--quiet", target], repo)
                remote_head = resolved.stdout.strip() or None

        # Remote ahead of the local checkout: upstream commits the checkout lacks —
        # a real update (fetch + merge) is needed.
        remote_ahead = self._count(repo, f"HEAD..{remote_head}") if remote_head else 0
        # Local checkout ahead of the running process: the disk already carries code
        # this process hasn't loaded (a prior self-update, a manual pull, or a session
        # integration) — only a restart is needed. Detected with NO network.
        local_ahead = 0
        if running and head and running != head:
            local_ahead = self._count(repo, f"{running}..HEAD") or 1  # differ ⇒ at least restart

        if remote_ahead > 0 and remote_head is not None:
            status.available = True
            status.behind = remote_ahead
            status.latest = remote_head[:7]  # short sha of the remote target's tip
            commits = "commit" if remote_ahead == 1 else "commits"
            status.message = (
                f"aGiT update available: {remote_ahead} new {commits} on {target} ({status.current} → {status.latest})."
            )
            return status
        if local_ahead > 0:
            # The checkout is already updated but this process still runs the old code.
            # Detected with no network, so an offline local update still prompts a
            # restart. Takes precedence over a fetch error — the user can act on it now.
            assert running is not None
            status.available = True
            status.restart_only = True
            status.current = running[:7]
            status.latest = head_short  # restarting loads the HEAD on disk
            status.message = (
                f"aGiT was updated on disk but the running copy is older "
                f"({status.current} → {status.latest}); restart to load it."
            )
            return status
        # Nothing newer locally or on the running side. If the remote couldn't be
        # checked, say why; otherwise we are genuinely current.
        if target is None:
            status.error = "no upstream branch is configured for the aGiT source checkout"
        elif fetch_error is not None:
            status.error = fetch_error
        else:
            status.message = "aGiT is up to date."
        return status

    def _check_package(self, *, timeout: int = _NET_TIMEOUT) -> UpdateStatus:
        status = UpdateStatus(kind=KIND_PACKAGE)
        installed = self._installed_version()
        status.current = installed
        latest = self._latest_package_version(timeout=timeout)
        if latest is None:
            status.error = "could not determine the latest published aGiT version"
            return status
        status.latest = latest
        running = self._running_version()
        index_newer = _version_tuple(latest) > _version_tuple(installed)
        running_stale = _version_tuple(installed) > _version_tuple(running)
        if index_newer:
            status.available = True
            status.message = f"aGiT update available: {installed} → {latest}."
        elif running_stale:
            # The package on disk was already upgraded (e.g. `pip install -U`) but
            # this process is still running the old version.
            status.available = True
            status.restart_only = True
            status.current = running
            status.message = f"aGiT {installed} is installed but the running copy is {running}; restart to load it."
        else:
            status.message = "aGiT is up to date."
        return status

    def _installed_version(self) -> str:
        try:
            from importlib import metadata

            return metadata.version(DIST_NAME)
        except Exception:
            return getattr(agit, "__version__", "0")

    def _running_version(self) -> str:
        # The version this process actually imported at startup. ``agit.__version__``
        # is read once at import, so after an in-place upgrade it still reflects the
        # OLD version while :meth:`_installed_version` reads the new one from disk.
        return getattr(agit, "__version__", "0")

    def _latest_package_version(self, *, timeout: int = _NET_TIMEOUT) -> str | None:
        # `pip index versions` is the most portable way to ask the configured
        # index without a hard dependency on a PyPI JSON client. It is marked
        # experimental but degrades gracefully: any failure (including a network
        # timeout) returns None and the caller reports "could not determine the
        # latest version".
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "index", "versions", DIST_NAME],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return None
        if result.returncode != 0:
            return None
        prefix = f"{DIST_NAME.lower()} ("
        for line in result.stdout.splitlines():
            line = line.strip()
            # e.g. "agit-ai (1.2.3)" or "LATEST: 1.2.3"
            if line.upper().startswith("LATEST:"):
                return line.split(":", 1)[1].strip() or None
            if line.lower().startswith(prefix) and line.endswith(")"):
                return line[line.index("(") + 1 : -1].strip() or None
        return None

    # --- applying --------------------------------------------------------

    def apply(self) -> UpdateStatus:
        """Install the available update in place. Blocking. Returns a status
        whose ``available`` is False on success and ``error`` set on failure."""
        if self.kind == KIND_SOURCE:
            return self._apply_source()
        return self._apply_package()

    def _apply_source(self) -> UpdateStatus:
        repo = self._source_repo
        assert repo is not None
        status = UpdateStatus(kind=KIND_SOURCE)
        # Never update over local development: a dirty work tree means the user
        # has uncommitted changes a fast-forward would not touch but which make
        # an automatic update surprising. Bail with a clear message instead.
        dirty = _git(["status", "--porcelain"], repo)
        if dirty.returncode != 0:
            status.error = dirty.stderr.strip() or "could not inspect the source checkout"
            return status
        if dirty.stdout.strip():
            status.error = (
                "the aGiT source checkout has uncommitted changes; "
                "update skipped (commit or stash them, or update manually)"
            )
            return status
        target = self._remote_target(repo)
        if target is None:
            status.error = "no upstream branch is configured for the aGiT source checkout"
            return status
        # Refresh the target ref so we merge the very latest commit, even if the
        # periodic check ran a while ago.
        remote = target.split("/", 1)[0]
        fetched = _git(["fetch", "--quiet", remote], repo, timeout=_NET_TIMEOUT)
        if fetched.returncode != 0:
            status.error = (fetched.stderr.strip() or "git fetch failed").splitlines()[-1]
            return status
        # Merge the upstream code into the checkout: a clean checkout fast-forwards;
        # a diverged one (the user's own commits — aGiT's session branches accumulate
        # them) gets a normal merge. Only a genuine content CONFLICT blocks the update.
        # On conflict we abort so the running source is left clean (no conflict
        # markers, local work intact) rather than half-merged.
        merged = _git(["merge", "--no-edit", target], repo, timeout=_NET_TIMEOUT)
        if merged.returncode != 0:
            _git(["merge", "--abort"], repo)
            status.error = (
                "automatic update is impossible due to merge conflicts between the "
                f"local aGiT source and {target}; resolve them manually, then update"
            )
            return status
        status.current = _git(["rev-parse", "--short", "HEAD"], repo).stdout.strip()
        status.latest = status.current
        status.message = f"Updated aGiT source checkout to {status.current}."
        return status

    def _apply_package(self) -> UpdateStatus:
        status = UpdateStatus(kind=KIND_PACKAGE)
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", DIST_NAME],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=600,
        )
        if result.returncode != 0:
            status.error = (result.stderr.strip() or result.stdout.strip() or "pip upgrade failed").splitlines()[-1]
            return status
        status.current = self._installed_version()
        status.latest = status.current
        status.message = f"Updated aGiT package to {status.current}."
        return status


def _version_tuple(version: str) -> tuple:
    # Lenient numeric-prefix comparison: "1.2.3" -> (1, 2, 3). Non-numeric
    # trailers (e.g. "1.2.3rc1") compare by their leading integer only, which is
    # good enough to answer "is the index version higher than mine?".
    parts: list[int] = []
    for chunk in version.strip().split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def restart_agit(extra_args: Sequence[str] = ()) -> NoReturn:
    """Re-exec aGiT in place so the freshly updated code is loaded.

    Uses ``python -m agit`` with the original CLI arguments so the entry point
    survives a package upgrade that may have rewritten the console script. This
    does not return on success.

    ``extra_args`` are appended to the original argv (de-duplicated) — used to
    carry ``--skip-privacy-ack`` through a menu-triggered restart so the
    already-acknowledged privacy warning isn't shown again.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    args = list(sys.argv[1:])
    for arg in extra_args:
        if arg not in args:
            args.append(arg)
    os.execv(sys.executable, [sys.executable, "-m", "agit", *args])
