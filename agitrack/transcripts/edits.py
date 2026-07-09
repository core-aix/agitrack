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
import re

from agitrack.transcripts.types import FileEdit

# A line of a Read tool's output: the line number, a separator, then the line itself.
# Claude renders ``12\ttext``; OpenCode renders ``00012| text`` (one padding space).
_NUMBERED_LINE = re.compile(r"^\s*(\d+)(?:\t|\| ?)(.*)$")


def content_from_read_output(text: str) -> str | None:
    """The file content a Read tool's line-numbered output represents, or None when the output
    can't be trusted to be the file's WHOLE content.

    A backtrace has no filesystem history, so the only way to know what a file looked like before
    an agent rewrote it is the Read the agent did first. Reconstructing that content lets a later
    whole-file Write diff against it, instead of counting the entire file as new (see
    :func:`tracked_edit`).

    Strict on purpose: the numbering must run 1, 2, 3, … with no gaps and every line must be
    numbered. A partial read (``offset``/``limit``), a truncation notice, or any unnumbered line
    makes this return None, so a wrong baseline is never seeded."""
    if not text:
        return None
    lines = text.split("\n")
    if lines and lines[0].strip() == "<file>":  # OpenCode wraps its read output
        lines = lines[1:]
    while lines and lines[-1].strip() in ("", "</file>"):
        lines.pop()
    if not lines:
        return None
    out: list[str] = []
    expected = 1
    for line in lines:
        match = _NUMBERED_LINE.match(line)
        if not match or int(match.group(1)) != expected:
            return None
        expected += 1
        out.append(match.group(2))
    return "\n".join(out) + "\n"


def seed_file_state(state: dict[str, str], path: str, content: str | None) -> None:
    """Record a file's pre-existing content (from a Read) as the baseline for later edits, unless
    the session already tracks it — a Read after a Write must not clobber what we wrote."""
    path = (path or "").strip()
    if path and content is not None and path not in state:
        state[path] = content


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


def merge_edits_by_path(edits: list[FileEdit]) -> list[FileEdit]:
    """Coalesce a turn's edits so each file appears exactly once, keeping first-touch order.

    One turn commonly calls Edit/Write on the same file many times. Those are separate tool calls
    but a single change to that file, so they must be merged: otherwise the file's history lists the
    same turn once per tool call — many identical-looking rows (same sha, same conversation, same
    diff). Line counts add up and the patches concatenate, so totals and the diff view are
    unchanged."""
    merged: dict[str, FileEdit] = {}
    for edit in edits:
        previous = merged.get(edit.path)
        if previous is None:
            merged[edit.path] = edit
        else:
            merged[edit.path] = FileEdit(
                path=edit.path,
                insertions=previous.insertions + edit.insertions,
                deletions=previous.deletions + edit.deletions,
                patch=previous.patch + edit.patch,
            )
    return list(merged.values())


def combine_patches(edits: list[FileEdit]) -> str:
    """The concatenated git-style patch for a turn's edits (one file after another),
    for the dashboard's per-turn diff view. Empty when the turn changed nothing."""
    return "".join(edit.patch for edit in edits if edit.patch)


def total_lines(edits: list[FileEdit]) -> tuple[int, int]:
    """Summed (insertions, deletions) across a turn's file edits."""
    return (sum(edit.insertions for edit in edits), sum(edit.deletions for edit in edits))
