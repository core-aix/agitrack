"""Reconstruct file edits from a transcript's tool calls (for ``--backtrace``).

The per-backend transcript parsers deliberately drop tool-call arguments — the
interaction trace records only the conversation, never the file edits. The
backtrace feature needs those edits to show how a past conversation changed the
repository *without any git history*, so this module turns a tool call's
before/after text into a :class:`FileEdit`: added/removed line counts plus a
git-style unified diff the dashboard can colour like a real commit's diff.

The math is git-independent (pure :mod:`difflib`), so it works in a directory
that was never a repository.
"""

from __future__ import annotations

import difflib

from agitrack.transcripts.types import FileEdit


def make_edit(path: str, before: str, after: str, *, status: str = "modified") -> FileEdit | None:
    """A :class:`FileEdit` for a single file changing from ``before`` to ``after``.

    ``status`` is ``added`` (new file / whole-file write), ``deleted``, or the
    default ``modified``. Returns None when nothing actually changed (or there is
    no path), so a no-op tool call adds neither lines nor an empty diff entry.
    """
    path = (path or "").strip()
    if not path or before == after:
        return None
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    # difflib emits its own `--- `/`+++ ` header pair (the first two lines when there
    # is any change); drop them and write git-style headers ourselves so the diff view
    # colours the file/hunk lines the same way it does a real commit's patch.
    raw = list(difflib.unified_diff(before_lines, after_lines, lineterm=""))
    hunk_lines = raw[2:] if len(raw) >= 2 and raw[0].startswith("--- ") and raw[1].startswith("+++ ") else raw
    insertions = sum(1 for line in hunk_lines if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in hunk_lines if line.startswith("-") and not line.startswith("---"))
    if insertions == 0 and deletions == 0:
        return None
    header = [f"diff --git a/{path} b/{path}"]
    if status == "added":
        header.append("new file mode 100644")
        header.extend(["--- /dev/null", f"+++ b/{path}"])
    elif status == "deleted":
        header.append("deleted file mode 100644")
        header.extend([f"--- a/{path}", "+++ /dev/null"])
    else:
        header.extend([f"--- a/{path}", f"+++ b/{path}"])
    patch = "\n".join([*header, *hunk_lines]) + "\n"
    return FileEdit(path=path, insertions=insertions, deletions=deletions, patch=patch)


def tracked_edit(
    state: dict[str, str],
    path: str,
    *,
    write: str | None = None,
    subedits: "list[tuple[str, str]] | None" = None,
) -> FileEdit | None:
    """A :class:`FileEdit` for one tool call, diffed against the file's CURRENT tracked content in
    ``state`` (updated in place) rather than always against empty.

    This is what makes each turn's diff the INCREMENTAL change: an agent that rewrites a whole file
    with ``Write`` every turn would otherwise show the entire file as additions on every turn (the
    diffs appear to accumulate). Tracking the content means the second write diffs against the
    first, so only the real change shows.

    - ``write``: the whole new file content (a Write). Diffed against the tracked content (or empty
      for a file first seen), then recorded as the new content.
    - ``subedits``: ``(old, new)`` string replacements (an Edit / MultiEdit). Applied in order to
      the tracked content when it is known; otherwise the ``old→new`` snippets are diffed directly
      (the file predates the session, so its full content isn't recoverable).
    """
    path = (path or "").strip()
    if not path:
        return None
    if write is not None:
        before = state.get(path, "")
        status = "modified" if path in state else "added"
        state[path] = write
        return make_edit(path, before, write, status=status)
    subedits = subedits or []
    if path in state:
        before = state[path]
        after = before
        for old, new in subedits:
            if old and old in after:
                after = after.replace(old, new, 1)  # first occurrence, as the editors do
            elif not old:
                after = after + new  # inserting where there is no anchor
        state[path] = after
        return make_edit(path, before, after, status="modified")
    # File content isn't tracked (it existed before the session): fall back to the snippet diff.
    before = "".join(old for old, _ in subedits)
    after = "".join(new for _, new in subedits)
    return make_edit(path, before, after)


def combine_patches(edits: list[FileEdit]) -> str:
    """The concatenated git-style patch for a turn's edits (one file after another),
    for the dashboard's per-turn diff view. Empty when the turn changed nothing."""
    return "".join(edit.patch for edit in edits if edit.patch)


def total_lines(edits: list[FileEdit]) -> tuple[int, int]:
    """Summed (insertions, deletions) across a turn's file edits."""
    return (sum(edit.insertions for edit in edits), sum(edit.deletions for edit in edits))
