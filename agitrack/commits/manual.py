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

from agitrack.commits.message import apply_summary_to_message, build_manual_squash_trailer, build_pending_trailer
from agitrack.config import AgitrackState
from agitrack.git import GitRepo
from agitrack.git import hooks as git_hooks


def write_lf(path, text: str) -> None:
    """Write *text* with LF endings on every platform.

    These files are read by the POSIX ``sh`` commit hooks: ``manual-ref`` a line at a time, and
    ``manual-pending-trailer`` straight into the commit message. ``Path.write_text`` uses
    ``newline=None``, which on Windows rewrites every ``\n`` as ``\r\n`` — the hook then reads a
    ref name with a trailing CR and ``git update-ref`` rejects it, so the latent refs are never
    advanced and the turns fold a SECOND time into the next commit (caught by Windows CI).
    """
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class ManualCommitTracker:
    def __init__(
        self,
        repo: GitRepo,
        base_repo: GitRepo,
        state: AgitrackState,
        *,
        debug: Callable[[str], None] | None = None,
        in_flight_fn: Callable[[], dict | None] | None = None,
    ) -> None:
        self.repo = repo
        self.base_repo = base_repo
        self.state = state
        self._debug = debug or (lambda _m: None)
        # Supplies the currently-running turn's facts (or None) so a commit the AGENT makes
        # mid-turn still carries attribution — see :meth:`render_trailer`.
        self._in_flight_fn = in_flight_fn
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

    def pending_refs(self) -> list[str]:
        """EVERY latent ref that still holds turns HEAD does not contain — not just this
        session's.

        A session's turns live on ``refs/agitrack/manual/<agitrack_session_id>``, and that id
        changes when the user starts a new session (Ctrl-G → + New session mints a fresh one) or
        when a backend switch opens one. Folding only ``self.ref()`` therefore DROPPED every turn
        recorded before the switch: the trace between the previous commit and the new one had a
        hole in it, silently. Manual mode is always no-worktree, so all of those sessions edited
        the SAME working tree and the user's commit captures all of their work — the trace must
        cover all of them too. (The dashboard's pending view has always enumerated the refs this
        way; the fold is what lagged behind.)
        """
        try:
            refs = self.repo.list_refs("refs/agitrack/manual/")
        except Exception as error:
            self._debug(f"manual ref enumeration failed: {error!r}")
            refs = []
        current = self.ref()
        if current not in refs:
            refs.append(current)
        return [ref for ref in refs if self.repo.ref_sha(ref)]

    def _pending_shas(self) -> list[str]:
        """The latent commits awaiting a fold across every session's ref, oldest first.

        Ordered by commit time rather than per-ref, so turns from two sessions interleave in the
        order they actually happened; deduplicated because a reset ref can leave two names on one
        chain."""
        seen: set[str] = set()
        stamped: list[tuple[int, str]] = []
        for ref in self.pending_refs():
            try:
                # Commits reachable from NO branch — the ones that are genuinely still latent.
                # `HEAD..ref` is not enough: after a fold the ref points at a real branch commit,
                # so switching to another branch made that (already-folded) work look pending and
                # it folded a second time — duplicating the trace and double-counting its tokens.
                shas = self.repo.unlanded_commits(ref)
            except Exception as error:
                self._debug(f"manual pending walk failed for {ref}: {error!r}")
                continue
            for sha in shas:
                if sha in seen:
                    continue
                seen.add(sha)
                stamped.append((self.repo.commit_timestamp(sha) or 0, sha))
        stamped.sort(key=lambda item: item[0])
        return [sha for _stamp, sha in stamped]

    def pending_count(self) -> int:
        """How many latent turns are recorded but not yet folded into a commit, across every
        session's ref (cheap: no message reads)."""
        return len(self._pending_shas())

    def pending_bodies(self) -> list[str]:
        """Commit-message bodies of the pending latent turns (oldest first): the commits on
        EVERY session's latent ref that HEAD does not yet contain (see :meth:`pending_refs` — a
        session or backend switch moves the ref, and those turns must still be folded). Each body already carries the turn's full
        metadata + interaction trace; the LLM summary (a git note on the latent commit) is folded
        into the body here when it has arrived, so the user's commit gets the summarized message."""
        shas = self._pending_shas()
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

    def in_flight_attribution(self) -> dict | None:
        """The running turn's facts for the trailer, or None when nothing should be attributed.

        Two conditions, both required: the driver reports a turn actually in progress, AND the
        working tree differs from the latent tip so the agent has real changes in this commit.
        The tree check is what keeps the "no AI work ⇒ no footprint" promise — without it, a
        human's own commit made while an agent happened to be thinking would be stamped as
        agent work."""
        if self._in_flight_fn is None:
            return None
        try:
            facts = self._in_flight_fn()
        except Exception as error:
            self._debug(f"in-flight lookup failed: {error!r}")
            return None
        if not facts:
            return None
        try:
            if not self._tree_differs_from_tip(self.repo.snapshot_worktree_tree()):
                return None
        except Exception as error:
            self._debug(f"in-flight snapshot failed: {error!r}")
            return None
        return facts

    def render_trailer(self) -> None:
        """(Re)render ``.agitrack/manual-pending-trailer`` from the durable latent ref, and the
        ``.agitrack/manual-ref`` name file the post-commit hook reads. When pending turns exist the
        trailer carries the ``commit_type: user`` block plus each turn's full trace/metadata; when
        none are pending but the agent is mid-turn with changes in the tree, it carries the
        in-flight attribution block instead; otherwise it is empty, so a purely human commit (no
        AI work) is left untouched."""
        try:
            agit_dir = self.agit_dir()
            agit_dir.mkdir(parents=True, exist_ok=True)
            # EVERY ref whose turns this commit will fold, one per line — the post-commit hook
            # advances each. Writing only the current session's ref left an earlier session's
            # already-folded turns pending, so the next commit folded them a second time.
            refs = self.pending_refs()
            if self.ref() not in refs:
                refs.append(self.ref())
            write_lf(agit_dir / "manual-ref", "\n".join(refs) + "\n")
            trailer = build_pending_trailer(
                agitrack_session_id=self.state.session_id,
                latent_bodies=self.pending_bodies(),
                in_flight=self.in_flight_attribution(),
            )
            write_lf(agit_dir / "manual-pending-trailer", trailer)
        except Exception as error:
            self._debug(f"manual trailer render failed: {error!r}")

    # --- recording turns ----------------------------------------------------

    def _tree_differs_from_tip(self, tree: str) -> bool:
        """Whether *tree* differs from the latent tip's tree (or HEAD's when the chain is
        empty) — i.e. whether there is uncommitted agent work to account for."""
        tip = self.repo.ref_sha(self.ref())
        try:
            base_tree = self.repo.rev_parse(f"{tip or 'HEAD'}^{{tree}}")
        except Exception:
            base_tree = None
        return tree != base_tree

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
        return self._tree_differs_from_tip(self._pending_tree)

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
