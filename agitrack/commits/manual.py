"""Manual-commit latent tracking, extracted so any driver — the interactive proxy or the
headless background tracker — can record agent turns as hidden latent commits and fold them
into the user's own commit.

In manual-commit mode the agent edits the current branch directly (no worktree) and each turn
is recorded as a hidden "latent" commit on ``refs/agitrack/manual/<session_id>`` instead of
landing on the branch. HEAD never moves. When the user commits (via aGiTrack or an external
``git commit``), a ``prepare-commit-msg`` hook folds the pending turns' trace + metadata into
that ONE commit and a ``post-commit`` hook resets the latent ref. When a custom
``core.hooksPath`` makes the hooks impossible, a poll-and-cover fallback detects the commit and
adds a metadata-only cover commit instead.

This class owns exactly that machinery over a pair of GitRepo handles (the working ``repo`` and
the ``base_repo`` whose ``.agitrack/`` holds the durable hook files), plus the small amount of
mutable poll state it needs. It performs no I/O with the user; callers supply a ``debug`` sink.
"""

from __future__ import annotations

from collections.abc import Callable

from agitrack.commits.message import apply_summary_to_message, build_manual_squash_trailer
from agitrack.config import AgitrackState
from agitrack.git import GitRepo
from agitrack.git import hooks as git_hooks


class ManualCommitTracker:
    def __init__(
        self,
        repo: GitRepo,
        base_repo: GitRepo,
        state: AgitrackState,
        *,
        debug: Callable[[str], None] | None = None,
    ) -> None:
        self.repo = repo
        self.base_repo = base_repo
        self.state = state
        self._debug = debug or (lambda _m: None)
        # Cached working-tree snapshot from gate(), reused by record() so it doesn't re-snapshot.
        self._pending_tree: str | None = None
        # Poll/fallback state: last HEAD we saw, whether the fold hooks are installed, and the
        # last post-commit signal mtime we reacted to.
        self.last_head: str | None = None
        self.hooks_installed = False
        self._signal_mtime: float | None = None

    # --- identity / paths ---------------------------------------------------

    def ref(self) -> str:
        """The hidden ref that chains this session's per-turn latent commits."""
        return f"refs/agitrack/manual/{self.state.session_id}"

    def agit_dir(self):
        return self.base_repo.repo / ".agitrack"

    # --- lifecycle ----------------------------------------------------------

    def setup(self) -> None:
        """Startup wiring: install the fold/reset hooks (unless a custom ``core.hooksPath``
        makes that impossible — then the poll+cover fallback runs instead), drop a stale latent
        chain left by a prior run, record the baseline HEAD, and render the initial trailer so
        even a first commit with no agent turns is attributed to the session."""
        self.hooks_installed = False
        try:
            if self.base_repo.core_hooks_path():
                self._debug("manual-commit hooks skipped: core.hooksPath is set (using poll+cover fallback)")
            else:
                self.hooks_installed = git_hooks.install_manual_commit_hooks(
                    self.base_repo.hooks_dir(), debug=self._debug
                )
        except Exception as error:
            self._debug(f"manual-commit hook install failed: {error!r}")
        self.reset_stale_ref()
        try:
            self.last_head = self.repo.rev_parse("HEAD")
        except Exception:
            self.last_head = None
        self.render_trailer()

    def teardown(self) -> None:
        try:
            if self.base_repo is not None and not self.base_repo.core_hooks_path():
                git_hooks.remove_manual_commit_hooks(self.base_repo.hooks_dir(), debug=self._debug)
        except Exception as error:
            self._debug(f"manual-commit hook removal failed: {error!r}")

    # --- pending turns ------------------------------------------------------

    def pending_count(self) -> int:
        """How many latent turns are recorded but not yet folded into a commit (cheap: no
        message reads)."""
        tip = self.repo.ref_sha(self.ref())
        if not tip:
            return 0
        try:
            return len(self.repo.log_shas("HEAD", tip))
        except Exception:
            return 0

    def pending_bodies(self) -> list[str]:
        """Commit-message bodies of the pending latent turns (oldest first): the commits on the
        latent ref that HEAD does not yet contain. Each body already carries the turn's full
        metadata + interaction trace; the LLM summary (a git note on the latent commit) is folded
        into the body here when it has arrived, so the user's commit gets the summarized message."""
        tip = self.repo.ref_sha(self.ref())
        if not tip:
            return []
        try:
            shas = self.repo.log_shas("HEAD", tip)  # HEAD..tip, oldest first
        except Exception as error:
            self._debug(f"manual pending walk failed: {error!r}")
            return []
        bodies: list[str] = []
        for sha in shas:
            body = self.repo.commit_message(sha)
            if not body:
                continue
            try:
                summary = self.repo.notes_show(sha, namespace="agitrack/commit-summary")
            except Exception:
                summary = None
            if summary and summary.strip():
                body = apply_summary_to_message(body, summary)
            bodies.append(body)
        return bodies

    def render_trailer(self) -> None:
        """(Re)render ``.agitrack/manual-pending-trailer`` from the durable latent ref, and the
        ``.agitrack/manual-ref`` name file the post-commit hook reads. When pending turns exist the
        trailer carries the ``commit_type: user`` block plus each turn's full trace/metadata; with
        NO pending turns it is empty, so a purely human commit (no AI work) is left untouched."""
        try:
            agit_dir = self.agit_dir()
            agit_dir.mkdir(parents=True, exist_ok=True)
            (agit_dir / "manual-ref").write_text(self.ref() + "\n", encoding="utf-8")
            trailer = build_manual_squash_trailer(
                agitrack_session_id=self.state.session_id,
                latent_bodies=self.pending_bodies(),
            )
            (agit_dir / "manual-pending-trailer").write_text(trailer, encoding="utf-8")
        except Exception as error:
            self._debug(f"manual trailer render failed: {error!r}")

    # --- recording turns ----------------------------------------------------

    def gate(self) -> bool:
        """Commit gate for a manual-mode turn: True when the working tree changed since the
        latent tip (or HEAD when the chain is empty). Caches the snapshot so :meth:`record`
        doesn't re-snapshot."""
        try:
            self._pending_tree = self.repo.snapshot_worktree_tree()
        except Exception as error:
            self._debug(f"manual snapshot failed: {error!r}")
            self._pending_tree = None
            return False
        tip = self.repo.ref_sha(self.ref())
        base_ref = tip or "HEAD"
        try:
            base_tree = self.repo.rev_parse(f"{base_ref}^{{tree}}")
        except Exception:
            base_tree = None
        return self._pending_tree != base_tree

    def record(self, message: str) -> str | None:
        """Record a manual-mode turn as a hidden latent commit: snapshot the working tree,
        commit-tree it onto the latent tip, and advance ONLY the latent ref — HEAD and the user's
        index are untouched. Returns the short sha, or None if the tree is unchanged."""
        tree = self._pending_tree
        self._pending_tree = None
        if tree is None:
            try:
                tree = self.repo.snapshot_worktree_tree()
            except Exception as error:
                self._debug(f"manual snapshot failed: {error!r}")
                return None
        tip = self.repo.ref_sha(self.ref())
        parent = tip or self.repo.rev_parse("HEAD")
        if tip is not None and tree == self.repo.rev_parse(f"{tip}^{{tree}}"):
            return None  # defensive: nothing new since the latent tip
        sha = self.repo.commit_tree(tree, parents=[parent], message=message)
        self.repo.update_ref(self.ref(), sha)
        self.render_trailer()
        return self.repo.short_sha(sha)

    # --- reconciliation with the user's own commits -------------------------

    def reset_stale_ref(self) -> bool:
        """Reset the latent ref to HEAD when its recorded turns are STALE, so they are never
        re-folded into an unrelated future commit. Turns are stale when the tip is an ANCESTOR of
        HEAD (already committed/folded) or the working tree is CLEAN (nothing left to fold). A
        DIRTY tree with a diverged tip means real uncommitted work remains, so the turns are kept.
        Never merges — the ref is only ever reset. Returns True when it reset the ref."""
        try:
            head = self.repo.rev_parse("HEAD")
            tip = self.repo.ref_sha(self.ref())
            if not tip:
                return False
            clean = self.repo.snapshot_worktree_tree() == self.repo.rev_parse("HEAD^{tree}")
            if clean or self.repo.is_ancestor(tip, head):
                self.repo.update_ref(self.ref(), head)
                return True
        except Exception as error:
            self._debug(f"manual ref reset failed: {error!r}")
        return False

    def service(self) -> None:
        """Per-loop upkeep. With the hooks installed, react to a commit (the post-commit signal
        fired, or HEAD simply moved) by dropping the now-stale latent chain and re-rendering the
        trailer. Without hooks (custom core.hooksPath), fall back to poll+cover."""
        if self.hooks_installed:
            signal_file = self.agit_dir() / "manual-commit-signal"
            try:
                mtime: float | None = signal_file.stat().st_mtime
            except OSError:
                mtime = None
            try:
                head = self.repo.rev_parse("HEAD")
            except Exception:
                return
            signalled = mtime is not None and mtime != self._signal_mtime
            moved = head != self.last_head
            if signalled or moved:
                self._signal_mtime = mtime
                self.last_head = head
                self.reset_stale_ref()
                self.render_trailer()
        else:
            self.reconcile_external_commit()

    def reconcile_external_commit(self) -> None:
        """Poll+cover FALLBACK for when the fold hook can't run (custom core.hooksPath): if HEAD
        moved since we last looked and pending latent turns exist, the user committed outside the
        hook — add a metadata-only cover commit carrying the pending tracking (its tree equals the
        new HEAD's, so it introduces no diff), then reset the latent ref."""
        if self.hooks_installed:
            return
        try:
            head = self.repo.rev_parse("HEAD")
        except Exception:
            return
        if self.last_head is None:
            self.last_head = head
            return
        if head == self.last_head:
            return
        self.last_head = head
        tip = self.repo.ref_sha(self.ref())
        bodies = self.pending_bodies()
        if not tip or not bodies:
            self.render_trailer()
            return
        message = "<aGiTrack> track agent turns\n\n" + build_manual_squash_trailer(
            agitrack_session_id=self.state.session_id, latent_bodies=bodies
        )
        try:
            head_tree = self.repo.rev_parse("HEAD^{tree}")
            self.repo.cover_commit(message, first_parent=head, second_parent=tip, tree=head_tree)
            self.last_head = self.repo.rev_parse("HEAD")
            self.repo.update_ref(self.ref(), self.repo.rev_parse("HEAD"))
        except Exception as error:
            self._debug(f"manual cover reconcile failed: {error!r}")
        self.render_trailer()
