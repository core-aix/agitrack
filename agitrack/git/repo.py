from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from agitrack.proc import console_isolation_kwargs


class GitError(RuntimeError):
    pass


class GitRepo:
    def __init__(self, repo: Path) -> None:
        self.repo = repo.resolve()
        self._run(["git", "rev-parse", "--show-toplevel"])

    @classmethod
    def discover(cls, path: Path) -> "GitRepo":
        process = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if process.returncode != 0:
            raise GitError(f"Not a Git repository: {path}")
        return cls(Path(process.stdout.strip()))

    @classmethod
    def init(cls, path: Path) -> "GitRepo":
        """Initialize a new Git repository at ``path`` and seed an empty initial
        commit. aGiTrack runs every session in a worktree, which requires a valid HEAD;
        a fresh `git init` leaves an unborn branch, so the seed commit makes the
        repo usable immediately. Any existing files are committed afterwards by
        aGiTrack's normal pre-agent user-commit flow."""
        path = path.expanduser()
        path.mkdir(parents=True, exist_ok=True)
        process = subprocess.run(
            ["git", "init"],
            cwd=path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if process.returncode != 0:
            raise GitError(f"git init failed in {path}:\n{process.stderr.strip()}")
        repo = cls.discover(path)
        repo.ensure_born()
        return repo

    def has_commits(self) -> bool:
        """True once the repository has at least one commit (a born HEAD)."""
        return self._run(["git", "rev-parse", "--verify", "--quiet", "HEAD"], check=False).returncode == 0

    def ensure_born(self) -> bool:
        """Make sure HEAD points at a commit. A freshly `git init`-ed repository
        has an unborn branch with no commits, which aGiTrack cannot run on (every
        session is a worktree, and a worktree needs a valid HEAD). Seed an empty
        initial commit so the repo is usable; any pre-existing files are left
        untracked for aGiTrack's normal pre-agent user-commit flow. Returns True if a
        seed commit was created, False if HEAD was already born."""
        if self.has_commits():
            return False
        self._run(["git", "commit", "--allow-empty", "-m", "Initial commit"])
        return True

    def status_short(self) -> str:
        return self._run(["git", "status", "--short"]).stdout

    def status_short_ignored(self) -> str:
        """Porcelain short status that also lists git-ignored entries (``!! path``).

        Used to find worktree files that no commit will ever carry into the base
        directory — not just untracked/unstaged tracked edits, but also ignored
        build output or local data the agent may have created. A wholly-ignored
        directory is reported once as ``dir/`` rather than file-by-file."""
        return self._run(["git", "status", "--short", "--ignored"]).stdout

    def status(self) -> str:
        # Full (long-format) `git status`, for the user-facing status command.
        return self._run(["git", "status"]).stdout

    def has_changes(self) -> bool:
        return bool(self.status_short().strip())

    def has_tracked_changes(self) -> bool:
        return self._diff_has_changes(["git", "diff", "--quiet"]) or self.has_staged_changes()

    def diff_head(self) -> str:
        # Content of all tracked changes (staged + unstaged) relative to HEAD.
        # Used as a fingerprint: `status --short` alone cannot tell new edits to
        # an already-modified file apart from the state a user declined before.
        return self._run(["git", "diff", "HEAD"], check=False).stdout

    def add_tracked(self) -> None:
        self._run(["git", "add", "-u"])

    def discard_all_changes(self) -> None:
        """Reset the working tree to HEAD, discarding every uncommitted change:
        staged and unstaged tracked edits (``reset --hard``) plus untracked files
        (``clean -fd``). ``clean`` is run without ``-x``, so git-ignored paths —
        aGiTrack's own ``.agitrack/`` among them — are preserved. Destructive and
        unrecoverable; callers must confirm with the user first."""
        self._run(["git", "reset", "--hard", "HEAD"])
        self._run(["git", "clean", "-fd"])

    def stage_paths(self, paths: list[str]) -> None:
        if paths:
            self._run(["git", "add", "--", *paths])

    def untracked_files(self) -> list[str]:
        output = self._run(["git", "ls-files", "--others", "--exclude-standard"]).stdout
        return [line for line in output.splitlines() if line and not line.startswith(".agitrack/")]

    def has_staged_changes(self) -> bool:
        return self._diff_has_changes(["git", "diff", "--cached", "--quiet"])

    def _diff_has_changes(self, command: list[str]) -> bool:
        process = self._run(command, check=False)
        if process.returncode == 0:
            return False
        if process.returncode == 1:
            return True
        raise GitError(process.stderr.strip() or "Unable to inspect changes")

    def commit(self, message: str) -> str:
        self._run(["git", "commit", "-F", "-"], input_text=message)
        return self.short_sha("HEAD")

    def amend_commit(self, message: str) -> str:
        """Rewrite HEAD's message (tree untouched); returns the new short SHA."""
        self._run(["git", "commit", "--amend", "-F", "-"], input_text=message)
        return self.short_sha("HEAD")

    def cover_commit(self, message: str, *, first_parent: str, second_parent: str, include_staged: bool = False) -> str:
        """Create a merge-shaped *cover* commit with parents ``(first_parent,
        second_parent)`` — the same shape as a GitHub PR merge commit. Used to
        attach aGiTrack's message on top of backend-made commits without amending
        them, since an amend changes their hashes and breaks references already
        published elsewhere (#58). The checked-out branch (or detached HEAD)
        moves to the new commit.

        By default the tree is ``second_parent``'s, so the cover is a pure
        metadata commit (working tree untouched). With ``include_staged`` the
        tree is the current index instead, folding any extra staged changes (e.g.
        files aGiTrack staged on top of the backend's commits) into the cover — so the
        cover's first-parent diff shows ALL the covered commits' changes plus the
        staged ones as one unit, instead of a plain commit that shows only the
        extra delta and hides the covered changes behind its single parent."""
        tree = (
            self._run(["git", "write-tree"]).stdout.strip()
            if include_staged
            else self.rev_parse(f"{second_parent}^{{tree}}")
        )
        sha = self._run(
            ["git", "commit-tree", tree, "-p", first_parent, "-p", second_parent],
            input_text=message,
        ).stdout.strip()
        self._run(["git", "reset", "--soft", sha])
        return self.short_sha(sha)

    def parents(self, ref: str = "HEAD") -> list[str]:
        output = self._run(["git", "rev-list", "--parents", "-1", ref]).stdout.split()
        return output[1:]

    def short_sha(self, ref: str = "HEAD") -> str:
        return self._run(["git", "rev-parse", "--short", ref]).stdout.strip()

    def commit_message(self, ref: str = "HEAD") -> str:
        return self._run(["git", "log", "-1", "--format=%B", ref], check=False).stdout

    def diff_range(self, base: str, head: str) -> str:
        return self._run(["git", "diff", f"{base}..{head}"], check=False).stdout

    # --- branches / worktrees / merges (used by concurrent-session support) ---

    def current_branch(self) -> str:
        # Returns the branch name, or "HEAD" when detached.
        return self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    def rev_parse(self, ref: str) -> str:
        return self._run(["git", "rev-parse", ref]).stdout.strip()

    def branch_exists(self, name: str) -> bool:
        return self._run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], check=False).returncode == 0

    def create_branch(self, name: str, base: str) -> None:
        self._run(["git", "branch", name, base])

    def delete_branch(self, name: str, *, force: bool = False) -> None:
        self._run(["git", "branch", "-D" if force else "-d", name])

    def list_branches(self, prefix: str = "") -> list[str]:
        output = self._run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"]).stdout
        names = [line for line in output.splitlines() if line]
        return [name for name in names if name.startswith(prefix)] if prefix else names

    def switch(self, branch: str, *, create: bool = False, base: str | None = None) -> None:
        command = ["git", "switch"]
        if create:
            # `-c`, never `-C`: create must not silently reset an existing
            # branch — a leftover turn branch can still hold unintegrated
            # commits. Callers pick a free name (or handle the GitError).
            command.append("-c")
        command.append(branch)
        if create and base:
            command.append(base)
        self._run(command)

    def switch_detach(self, ref: str) -> None:
        # Detach HEAD at ``ref`` (keeping any working-tree changes), leaving no
        # branch checked out — so a now-empty turn branch can be deleted.
        self._run(["git", "switch", "--detach", ref])

    def is_detached(self) -> bool:
        return self.current_branch() == "HEAD"

    def worktree_add_detached(self, path: str, *, base: str) -> None:
        # Create a worktree detached at ``base`` with no branch of its own; a turn
        # branch is created lazily on the first commit (see _ensure_turn_branch).
        self._run(["git", "worktree", "add", "--detach", path, base])

    def worktree_move(self, old_path: str, new_path: str) -> None:
        # Move a worktree's directory and update git's admin record. The worktree
        # must not be in use (no process with its cwd there) or git refuses.
        self._run(["git", "worktree", "move", old_path, new_path])

    def worktree_remove(self, path: str, *, force: bool = False) -> None:
        command = ["git", "worktree", "remove"]
        if force:
            command.append("--force")
        command.append(path)
        self._run(command)

    def worktree_prune(self) -> None:
        # Drop administrative entries for worktrees whose directories are gone.
        self._run(["git", "worktree", "prune"], check=False)

    def repair_worktrees(self, *paths: str) -> None:
        # Re-link worktree administrative files after the worktrees' directories
        # moved (e.g. the .agit → .agitrack state-dir migration). A moved worktree
        # must be named explicitly — ``git worktree repair`` with no args can't find
        # a worktree whose directory it no longer knows about, so pass the NEW paths.
        # Best-effort; never raises.
        self._run(["git", "worktree", "repair", *paths], check=False)

    def worktree_list(self) -> list[dict[str, str]]:
        output = self._run(["git", "worktree", "list", "--porcelain"]).stdout
        worktrees: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in output.splitlines():
            if not line.strip():
                if current:
                    worktrees.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                current["path"] = value
            elif key == "HEAD":
                current["head"] = value
            elif key == "branch":
                current["branch"] = value.removeprefix("refs/heads/")
            elif key == "detached":
                current["branch"] = ""
        if current:
            worktrees.append(current)
        return worktrees

    def merge(self, ref: str) -> bool:
        """Merge ``ref`` into the current branch. Returns True on a clean merge,
        False if there are conflicts (the merge is left in progress for
        resolution). Raises GitError on any other failure."""
        process = self._run(["git", "merge", "--no-edit", ref], check=False)
        if process.returncode == 0:
            return True
        if self.unmerged_paths():
            return False
        raise GitError(process.stderr.strip() or process.stdout.strip() or "merge failed")

    def merge_ff_only(self, ref: str) -> None:
        self._run(["git", "merge", "--ff-only", ref])

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """True if ``ancestor`` is reachable from ``descendant`` — i.e. moving a
        branch from ``ancestor`` to ``descendant`` would be a fast-forward."""
        return (
            self._run(
                ["git", "merge-base", "--is-ancestor", ancestor, descendant],
                check=False,
            ).returncode
            == 0
        )

    def fast_forward_branch(self, branch: str, target: str) -> None:
        """Fast-forward ``branch`` (which need NOT be checked out) to ``target``.

        Refuses unless ``target`` is a descendant of ``branch``, so this can only
        ever advance the branch along its own history — never a force-move that
        could drop commits. Lets aGiTrack integrate into the base branch even when the
        user has `git checkout`ed a different branch in the directory."""
        if not self.is_ancestor(branch, target):
            raise GitError(f"'{target}' is not a fast-forward of '{branch}'")
        # -f updates the ref in place; git itself still refuses if `branch` is
        # checked out in any worktree, so this only runs for a non-checked-out base.
        self._run(["git", "branch", "-f", branch, target])

    def merge_abort(self) -> None:
        self._run(["git", "merge", "--abort"], check=False)

    def unmerged_paths(self) -> list[str]:
        output = self._run(["git", "diff", "--name-only", "--diff-filter=U"], check=False).stdout
        return [line for line in output.splitlines() if line]

    def merge_in_progress(self) -> bool:
        return self._run(["git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD"], check=False).returncode == 0

    def has_conflict_markers(self) -> bool:
        # `git diff --check` reports leftover conflict markers, but only in the
        # worktree vs the index — once `add_all()` stages the files it sees
        # nothing. `--cached` checks the staged content against HEAD, so markers
        # are still caught right before they would be committed.
        for command in (["git", "diff", "--check"], ["git", "diff", "--cached", "--check"]):
            output = self._run(command, check=False).stdout
            if "conflict marker" in output.lower():
                return True
        return False

    def add_all(self) -> None:
        self._run(["git", "add", "-A"])

    def log_range(self, base: str, head: str, *, paths: list[str] | None = None) -> str:
        command = ["git", "log", "--no-color", "--format=%h %s", f"{base}..{head}"]
        if paths:
            command.extend(["--", *paths])
        return self._run(command, check=False).stdout.strip()

    def log_shas(self, base: str, head: str) -> list[str]:
        """Full SHAs of commits in ``base..head``, oldest first."""
        output = self._run(["git", "log", "--format=%H", "--reverse", f"{base}..{head}"], check=False).stdout
        return [line for line in output.split() if line]

    def notes_add(self, commit: str, message: str, *, namespace: str = "agitrack") -> None:
        self._run(["git", "notes", "--ref", namespace, "add", "-f", "-m", message, commit])

    def notes_show(self, commit: str, *, namespace: str = "agitrack") -> str | None:
        result = self._run(["git", "notes", "--ref", namespace, "show", commit], check=False)
        return result.stdout if result.returncode == 0 else None

    def notes_list(self, *, namespace: str = "agitrack") -> list[tuple[str, str]]:
        output = self._run(["git", "notes", "--ref", namespace, "list"], check=False).stdout
        if not output.strip():
            return []
        entries = []
        for line in output.splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                commit_sha = parts[1]
                note = self.notes_show(commit_sha, namespace=namespace)
                first_line = (note or "").strip().split("\n")[0] if note else ""
                entries.append((commit_sha, first_line))
        return entries

    # --- low-level object/ref plumbing (shared-session storage, issue #55) ------
    # These build and move a custom ref (refs/agit/shared-sessions) entirely in
    # the object database, never touching the working tree or the real index.

    def ref_exists(self, ref: str) -> bool:
        return self._run(["git", "rev-parse", "--verify", "--quiet", ref], check=False).returncode == 0

    def ref_sha(self, ref: str) -> str | None:
        result = self._run(["git", "rev-parse", "--verify", "--quiet", ref], check=False)
        return result.stdout.strip() or None if result.returncode == 0 else None

    def write_blob(self, content: str) -> str:
        """Write *content* as a blob into the object db; returns its SHA."""
        return self._run(["git", "hash-object", "-w", "--stdin"], input_text=content).stdout.strip()

    def read_tree_paths(self, ref: str) -> dict[str, str]:
        """Map ``path -> blob SHA`` for every file reachable from ``ref`` (a tree
        or commit). Empty when the ref doesn't exist."""
        if not self.ref_exists(ref):
            return {}
        output = self._run(["git", "ls-tree", "-r", "-z", ref], check=False).stdout
        entries: dict[str, str] = {}
        for record in output.split("\0"):
            if not record:
                continue
            meta, _, path = record.partition("\t")
            parts = meta.split()
            if len(parts) >= 3 and parts[1] == "blob":
                entries[path] = parts[2]
        return entries

    def read_ref_blob(self, ref: str, path: str) -> str | None:
        """Contents of ``path`` within ``ref``'s tree, or None if absent."""
        result = self._run(["git", "cat-file", "-p", f"{ref}:{path}"], check=False)
        return result.stdout if result.returncode == 0 else None

    def write_tree_from(self, entries: dict[str, str]) -> str:
        """Build a tree containing exactly ``entries`` (``path -> blob SHA``) using
        a throwaway index, so the real index and working tree are untouched.
        Returns the tree SHA (the empty tree when ``entries`` is empty)."""
        with tempfile.TemporaryDirectory() as tmp:
            index = os.path.join(tmp, "index")
            env = {"GIT_INDEX_FILE": index}
            for path, blob in entries.items():
                self._run(
                    ["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"],
                    env=env,
                )
            return self._run(["git", "write-tree"], env=env).stdout.strip()

    def commit_tree_orphan(self, tree: str, message: str) -> str:
        """Commit ``tree`` with NO parents — a standalone, history-free snapshot.
        Rewriting a ref to such commits keeps only the latest copy (old objects
        become unreferenced and are GC'd)."""
        return self._run(["git", "commit-tree", tree, "-F", "-"], input_text=message).stdout.strip()

    def update_ref(self, ref: str, sha: str) -> None:
        self._run(["git", "update-ref", ref, sha])

    def delete_ref(self, ref: str) -> None:
        self._run(["git", "update-ref", "-d", ref], check=False)

    def remote_exists(self, name: str = "origin") -> bool:
        return name in self._run(["git", "remote"], check=False).stdout.split()

    def fetch_ref(
        self,
        refspec: str,
        *,
        remote: str = "origin",
        filter_blobs: str | None = None,
        refetch: bool = False,
        timeout: float | None = None,
        cancel: "threading.Event | None" = None,
    ) -> bool:
        """Fetch a single refspec (e.g. ``+refs/agit/x:refs/agit/x``). Returns
        True on success; False on any failure (offline, no such ref yet, …).

        With ``filter_blobs`` (e.g. ``blob:limit=16k``) the fetch skips large blobs
        — used to pull a shared-session ref's small manifests for listing without
        downloading every transcript; the transcripts are fetched on demand. The
        one-off partial fetch's persisted filter is then dropped so the user's
        normal ``git fetch`` stays full.

        With ``refetch`` git re-downloads every object reachable from the ref as a
        fresh clone would (ignoring what's already local) — used to backfill blobs a
        prior partial fetch omitted, since a plain ref fetch won't (the ref is already
        at the tip, so it transfers nothing).

        ``cancel`` (a ``threading.Event``) stops the fetch the moment it is set —
        the git subprocess is killed, not merely abandoned — so a user who cancels
        (or exits) truly stops the network work rather than leaving it running."""
        cmd = ["git", "fetch"]
        if refetch:
            cmd.append("--refetch")
        if filter_blobs:
            cmd.append(f"--filter={filter_blobs}")
        cmd += [remote, refspec]
        # Never block on an interactive credential prompt — these ref syncs run in
        # the background (and on the exit path), where a prompt would hang with no
        # way to answer. Cached creds / credential helpers still work. The timeout
        # bounds a stalled fetch; cancel kills it immediately on user request.
        ok = self._run_bounded(cmd, env={"GIT_TERMINAL_PROMPT": "0"}, timeout=timeout, cancel=cancel) == 0
        if filter_blobs and ok:
            # Don't turn the user's remote into a permanently-filtered clone.
            self._run(["git", "config", "--unset", f"remote.{remote}.partialclonefilter"], check=False)
        return ok

    def resolve_blob_oid(self, ref: str, path: str) -> str | None:
        """The blob id at ``ref:path``, read from the (present) tree — so it resolves even when
        the blob CONTENT is a partial-clone placeholder not yet fetched. None if absent."""
        result = self._run(["git", "rev-parse", f"{ref}:{path}"], check=False)
        oid = result.stdout.strip()
        return oid if result.returncode == 0 and oid else None

    def has_object_local(self, oid: str) -> bool:
        """Whether ``oid`` is present in the LOCAL object store, without triggering a
        partial-clone lazy fetch (``GIT_NO_LAZY_FETCH`` keeps it offline). Lets a caller decide
        whether a blob still needs downloading without paying a network round-trip when it
        doesn't. (On git < 2.36 the env is ignored and a missing promised object may lazy-fetch
        here instead — harmless: it just gets fetched a step earlier.)"""
        result = self._run(["git", "cat-file", "-e", oid], check=False, env={"GIT_NO_LAZY_FETCH": "1"})
        return result.returncode == 0

    def fetch_object(
        self,
        oid: str,
        *,
        remote: str = "origin",
        timeout: float | None = None,
        cancel: "threading.Event | None" = None,
    ) -> bool:
        """Fetch a single object by id — used to backfill a transcript blob a partial-clone
        listing fetch omitted (a plain ref fetch won't, as the ref is already at the tip).
        Returns True on success. Bounded + non-interactive like ``fetch_ref``; fails (False)
        on a remote that disallows fetching by object id, so the caller can fall back."""
        cmd = ["git", "fetch", remote, oid]
        return self._run_bounded(cmd, env={"GIT_TERMINAL_PROMPT": "0"}, timeout=timeout, cancel=cancel) == 0

    def _run_bounded(
        self,
        command: list[str],
        *,
        timeout: float | None = None,
        cancel: "threading.Event | None" = None,
        env: dict[str, str] | None = None,
    ) -> int:
        """Run *command* as a subprocess that can be stopped early — when *cancel*
        (a ``threading.Event``-like object with ``is_set()``) is set, or *timeout*
        elapses. The process is terminated (then killed) so the network work really
        stops. Returns the exit code, or 124 when cancelled/timed out."""
        # Discard output: callers only use the exit code, and piping a long fetch's
        # progress (stderr) without reading it would fill the pipe buffer and wedge
        # the process — the opposite of "bounded".
        process = subprocess.Popen(
            command,
            cwd=self.repo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, **env} if env else None,
        )
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            try:
                process.wait(timeout=0.1)
                return process.returncode
            except subprocess.TimeoutExpired:
                pass
            stop = (cancel is not None and cancel.is_set()) or (deadline is not None and time.monotonic() > deadline)
            if stop:
                self._terminate_process(process)
                return 124

    @staticmethod
    def _terminate_process(process: "subprocess.Popen") -> None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def push_ref(
        self,
        refspec: str,
        *,
        remote: str = "origin",
        force_with_lease: str | None = None,
        timeout: float | None = None,
        cancel: "threading.Event | None" = None,
    ) -> tuple[bool, str]:
        """Push a refspec. Returns ``(ok, stderr)`` — stderr lets the caller spot a
        non-fast-forward/stale rejection and retry after re-fetching.

        ``timeout`` bounds a stalled push and ``cancel`` (a ``threading.Event``) stops
        it the instant it is set — the git subprocess is killed, not abandoned — so a
        user who cancels a manual share truly stops the upload. When neither is given
        the push is a plain blocking call (the background/exit paths)."""
        command = ["git", "push"]
        if force_with_lease is not None:
            command.append(f"--force-with-lease={force_with_lease}" if force_with_lease else "--force-with-lease")
        command += [remote, refspec]
        # GIT_TERMINAL_PROMPT=0: fail fast on a missing credential rather than
        # blocking on a prompt no one can answer (e.g. the synchronous exit-path
        # share). Cached creds / credential helpers are unaffected.
        env = {"GIT_TERMINAL_PROMPT": "0"}
        if timeout is None and cancel is None:
            result = self._run(command, check=False, env=env)
            return result.returncode == 0, result.stderr
        code, stderr = self._run_bounded_io(command, env=env, timeout=timeout, cancel=cancel)
        return code == 0, stderr

    def _run_bounded_io(
        self,
        command: list[str],
        *,
        timeout: float | None = None,
        cancel: "threading.Event | None" = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Like :meth:`_run_bounded` but captures stderr for the caller — used for a
        cancellable ``git push``, whose output is small (a few status lines), so the
        pipe can't fill and wedge the process the way an unread fetch progress stream
        would. Returns ``(exit_code, stderr)``; ``(124, partial-stderr)`` when
        cancelled or timed out. Retrying ``communicate`` after a ``TimeoutExpired``
        does not lose output (documented behaviour), so the poll loop is safe."""
        process = subprocess.Popen(
            command,
            cwd=self.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **env} if env else None,
        )
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            try:
                _out, err = process.communicate(timeout=0.1)
                return process.returncode, err or ""
            except subprocess.TimeoutExpired:
                pass
            stop = (cancel is not None and cancel.is_set()) or (deadline is not None and time.monotonic() > deadline)
            if stop:
                self._terminate_process(process)
                try:
                    _out, err = process.communicate(timeout=2)
                except Exception:
                    err = ""
                return 124, err or ""

    def unreachable_commits(self) -> list[str]:
        """SHAs of commits reachable from no ref (and no reflog) — dangling objects
        git's auto-gc would eventually drop. Used to sweep stale shared-session
        snapshots; the caller filters to genuine sessions before deleting anything."""
        result = self._run(
            ["git", "fsck", "--unreachable", "--no-reflogs", "--connectivity-only", "--no-progress"],
            check=False,
        )
        commits = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0] == "unreachable" and parts[1] == "commit":
                commits.append(parts[2])
        return commits

    def delete_orphaned_objects(self, old_sha: str | None) -> int:
        """Immediately delete the loose objects reachable from ``old_sha`` but from
        no current ref — the previous shared-session snapshot's commit/tree/blobs
        after the ref was rewritten off it. Targeted (it only ever removes objects
        exclusive to ``old_sha``, never anything another ref reaches), so it's safe
        to run alongside aGiTrack's other git writes and doesn't wait for git's auto-gc.
        Returns the count removed. Best-effort; never raises.

        Uses an explicit object-set difference: ``git rev-list --objects A --not B``
        does NOT reliably drop trees/blobs A shares with B, which would delete
        objects the current ref still needs — so we diff the two object sets here."""
        if not old_sha:
            return 0
        old = self._run(["git", "rev-list", "--objects", old_sha], check=False)
        if old.returncode != 0:
            return 0
        old_shas = {line.split(" ", 1)[0] for line in old.stdout.splitlines() if line}
        if not old_shas:
            return 0
        kept = self._run(["git", "rev-list", "--objects", "--all"], check=False)
        kept_shas = {line.split(" ", 1)[0] for line in kept.stdout.splitlines() if line}
        orphaned = old_shas - kept_shas  # in old's snapshot, reachable from no ref
        raw = self._run(["git", "rev-parse", "--git-path", "objects"], check=False).stdout.strip()
        if not raw:
            return 0
        objects = Path(raw) if os.path.isabs(raw) else (self.repo / raw)
        removed = 0
        for sha in orphaned:
            if len(sha) < 4 or any(ch not in "0123456789abcdef" for ch in sha):
                continue
            loose = objects / sha[:2] / sha[2:]
            try:
                loose.unlink()  # packed objects have no loose file (no-op via OSError)
                removed += 1
            except OSError:
                pass
        return removed

    def root_commit(self) -> str | None:
        """The repo's first (root) commit SHA — a clone-stable repo fingerprint.
        None for an unborn repo. Picks the earliest if history has several roots."""
        output = self._run(["git", "rev-list", "--max-parents=0", "HEAD"], check=False).stdout.split()
        return output[-1] if output else None

    def _run(
        self,
        command: list[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        # A timeout bounds a network git call (fetch/push over bad internet): on
        # expiry subprocess.run kills the process and raises, which we surface as a
        # non-zero result so the caller treats it as a plain failure (e.g. offline).
        # Keep git off the host console on Windows: a child that inherits our console can leave
        # it out of raw mode (input then echoes as escape codes). When we feed git via input=,
        # subprocess already pipes its stdin, so only detach stdin when we don't. (See proc.py.)
        isolation = console_isolation_kwargs(detach_stdin=input_text is None)
        if timeout is not None:
            try:
                return subprocess.run(
                    command,
                    cwd=self.repo,
                    input=input_text,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    env={**os.environ, **env} if env else None,
                    timeout=timeout,
                    **isolation,
                )
            except subprocess.TimeoutExpired:
                return subprocess.CompletedProcess(command, returncode=124, stdout="", stderr="timed out")
        process = subprocess.run(
            command,
            cwd=self.repo,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env={**os.environ, **env} if env else None,
            **isolation,
        )
        if check and process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip()
            raise GitError(f"Command failed: {' '.join(command)}\n{detail}")
        return process
