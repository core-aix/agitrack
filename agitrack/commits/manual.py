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

import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from agitrack.commits.message import apply_summary_to_message, build_manual_squash_trailer, build_pending_trailer
from agitrack.config import AgitrackState
from agitrack.git import GitRepo
from agitrack.git import hooks as git_hooks
from agitrack.fileio import ensure_state_dir


def write_lf(path: Path, text: str) -> None:
    """Write *text* with LF endings on every platform.

    These files are read by the POSIX ``sh`` commit hooks: ``manual-ref`` a line at a time, and
    ``manual-pending-trailer`` straight into the commit message. ``Path.write_text`` uses
    ``newline=None``, which on Windows rewrites every ``\n`` as ``\r\n`` — the hook then reads a
    ref name with a trailing CR and ``git update-ref`` rejects it, so the latent refs are never
    advanced and the turns fold a SECOND time into the next commit (caught by Windows CI).

    ATOMIC (temp file + rename), because the reader is a separate process whose timing we do not
    control: the ``sh`` hooks read these files during a ``git commit`` that can land at any moment,
    including mid-write. An in-place rewrite can be read half-finished — and while an EMPTY read is
    harmlessly skipped by the hook's ``[ -s ... ]`` guard (the turn is merely lost), a partial but
    non-empty one would fold a truncated metadata block straight into the user's permanent commit
    message. Same guarantee ``state.save()`` already relies on.
    """
    path = Path(path)
    ensure_state_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        # Only reachable with the tmp still present when something above raised; on success
        # os.replace already consumed it.
        try:
            os.unlink(tmp)
        except OSError:
            pass


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
        # Poll/fallback state: last HEAD we saw, whether the fold hooks are installed, and the
        # last post-commit signal mtime we reacted to.
        self.last_head: str | None = None
        self.hooks_installed = False
        self._signal_mtime: float | None = None
        # Latent chains `setup()` discarded, for the driver to report to the user.
        self.dropped_chains: list[str] = []

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
        # …and drop chains from sessions that are gone whose turns some commit already holds.
        # Startup is the natural moment: any ref other than ours belongs to a session that is no
        # longer running. See `prune_abandoned_refs` for the rule.
        try:
            # Recorded rather than only logged: the driver surfaces it, because AI attribution
            # going away must never be something the user can only discover with --verbose.
            self.dropped_chains = prune_abandoned_refs(self.repo, self.ref(), self.pending_refs(), debug=self._debug)
        except Exception as error:
            self._debug(f"abandoned-ref prune failed: {error!r}")
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
                body = apply_summary_to_message(body, summary, repo_root=self.repo.repo)
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
            if not self.repo.worktree_differs_from_head():
                return None
        except Exception as error:
            self._debug(f"in-flight tree check failed: {error!r}")
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
            ensure_state_dir(agit_dir)
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
                repo_root=self.repo.repo,
            )
            write_lf(agit_dir / "manual-pending-trailer", trailer)
        except Exception as error:
            self._debug(f"manual trailer render failed: {error!r}")

    # --- recording turns ----------------------------------------------------

    def gate(self) -> bool:
        """Commit gate for a latent turn: always open. Every turn the conversation's watermark
        hands over is recorded, whether or not it changed a file, because a turn is a MESSAGE in
        the conversation log, not a snapshot of the tree. (It used to be gated on "the working
        tree changed since the last record", which cost a full snapshot per turn and dropped the
        question asked after an edit and the prompt that led to a commit.)"""
        return True

    def record(self, message: str) -> str | None:
        """Record a turn as a hidden latent commit on this session's ref: ONE ``commit-tree``
        reusing its parent's tree, and one ref update. HEAD, the user's index and the working
        tree are untouched, and nothing is read from the working tree: recording a message costs
        the same however large the repository is. Returns the short sha."""
        tip = self.repo.ref_sha(self.ref())
        if tip is not None and not self.repo.has_object_local(tip):
            # The ref names a commit this repo no longer has — `git gc --prune` can collect a
            # latent commit, which is unreachable from any branch by design. Re-anchor at HEAD:
            # the chain restarts, and only the already-lost turns are lost.
            self._debug(f"latent tip {tip} is missing from the object store; re-anchoring at HEAD")
            tip = None
        parent = tip or self.repo.rev_parse("HEAD")
        sha = self.repo.commit_tree(self.repo.rev_parse(f"{parent}^{{tree}}"), parents=[parent], message=message)
        self.repo.update_ref(self.ref(), sha)
        self.render_trailer()
        return self.repo.short_sha(sha)

    # --- reconciliation with the user's own commits -------------------------

    def reset_stale_ref(self) -> bool:
        """Reset the latent ref to HEAD once a commit holds its turns (the tip is an ANCESTOR of
        HEAD), so they are never folded again. Nothing else makes a recorded turn stale: a turn is
        a message in the conversation, kept until a commit carries it, whatever became of its
        edits. Never merges — the ref is only ever reset. Returns True when it reset the ref."""
        try:
            head = self.repo.rev_parse("HEAD")
            tip = self.repo.ref_sha(self.ref())
            if tip and tip != head and self.repo.is_ancestor(tip, head):
                self.repo.update_ref(self.ref(), head)
                return True
        except Exception as error:
            self._debug(f"manual ref reset failed: {error!r}")
        return False

    def mark_folded(self) -> None:
        """Every pending turn is now in HEAD (aGiTrack folded them into a commit itself): move
        EVERY latent ref onto HEAD, as the post-commit hook does. Needed whenever that hook may not
        have run (a custom ``core.hooksPath``): a record is kept until a commit holds it, and a
        fold commit does not descend from the records it folded, so without this they would fold
        into the next commit a second time."""
        try:
            head = self.repo.rev_parse("HEAD")
        except Exception as error:
            self._debug(f"mark folded failed: {error!r}")
            return
        for ref in dict.fromkeys([*self.pending_refs(), self.ref()]):
            try:
                self.repo.update_ref(ref, head)
            except Exception as error:
                self._debug(f"mark folded failed for {ref}: {error!r}")
        self.last_head = head

    def discard_pending(self) -> int:
        """Drop every pending latent turn, on every session's ref, without folding it anywhere.

        The user's answer to "these turns from an earlier run never reached a commit; keep them?"
        (issue #296). The refs move onto HEAD exactly as a fold would leave them, so nothing
        reads them as pending again, and the trailer is re-rendered so a commit made before the
        next poll cannot fold the discarded text from the old file. Returns how many turns were
        dropped."""
        count = self.pending_count()
        if count:
            self.mark_folded()
        self.render_trailer()
        return count

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
            agitrack_session_id=self.state.session_id, latent_bodies=bodies, repo_root=self.repo.repo
        )
        try:
            head_tree = self.repo.rev_parse("HEAD^{tree}")
            self.repo.cover_commit(message, first_parent=head, second_parent=tip, tree=head_tree)
            self.mark_folded()
        except Exception as error:
            self._debug(f"manual cover reconcile failed: {error!r}")
        self.render_trailer()


def prune_abandoned_refs(
    repo: GitRepo,
    own_ref: str,
    refs: list[str],
    *,
    debug: Callable[[str], None] | None = None,
) -> list[str]:
    """Reset the latent refs of sessions that are gone once a commit holds their turns.

    A session's turns are messages in the conversation: they are kept, and fold into the next
    commit, until some commit carries them, whatever became of their edits. So the only chain
    this resets is one whose tip HEAD already contains. The caller's own ref is left to
    ``reset_stale_ref``. Returns the refs that were reset, for logging."""
    log = debug or (lambda _message: None)
    changed: list[str] = []
    try:
        head = repo.rev_parse("HEAD")
    except Exception as error:
        log(f"abandoned-ref prune skipped: {error!r}")
        return changed
    for ref in refs:
        if ref == own_ref:
            continue
        try:
            tip = repo.ref_sha(ref)
            if tip and tip != head and repo.is_ancestor(tip, head):
                repo.update_ref(ref, head)
                changed.append(ref)
        except Exception as error:
            log(f"abandoned-ref prune failed for {ref}: {error!r}")
    return changed
