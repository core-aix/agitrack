"""Bound a shared session transcript to a size limit without losing the parts that must
survive: the session's *opening* (system prompt / setup that persists for the whole session)
and its most *recent* turns.

Git hosts reject very large files — GitHub blocks any pushed file over 100 MiB — so a
long-running session whose transcript has grown past that limit can't be shared at all. We
trim the MIDDLE instead: keep a HEAD (the original opening) and the most recent TAIL, and
prefer to start the tail at a *compaction boundary* (the summary there recaps everything
before it, so the dropped middle costs no needed context). That is exactly the "delete very
old conversation / resume from a compaction" strategy, applied to both backends' formats via
the format-agnostic index selection below.
"""

from __future__ import annotations

from collections.abc import Sequence

# Storing files much larger than ~20 MB in git is poor practice (slow clones/fetches, bloated
# packs) well before GitHub's 100 MiB hard block — so cap the shared transcript here. Both this
# and the head budget below are user-configurable, but never past HARD_MAX_SHARED_BYTES.
DEFAULT_MAX_SHARED_BYTES = 20 * 1024 * 1024
# How much of the original opening (system prompt / initial context) to preserve verbatim —
# kept small so the recent tail gets the majority of the budget (the opening is usually tiny).
DEFAULT_HEAD_BYTES = 4 * 1024 * 1024
# Absolute ceiling, enforced regardless of config: Git hosts reject pushes of files over
# 100 MiB, so a shared transcript may never be configured (or trimmed) above it.
HARD_MAX_SHARED_BYTES = 100 * 1024 * 1024


def select_kept_indices(
    sizes: Sequence[int],
    compaction: Sequence[bool],
    max_bytes: int,
    *,
    sep_bytes: int = 1,
    head_bytes: int = DEFAULT_HEAD_BYTES,
) -> list[int] | None:
    """Pick which transcript items (lines for Claude, messages for OpenCode) to KEEP so the
    joined result fits ``max_bytes``, preserving a head of the opening and a recent tail.

    ``sizes[i]`` is item *i*'s serialized byte length; ``compaction[i]`` marks a clean resume
    boundary; ``sep_bytes`` is the per-join overhead between items (1 for ``"\\n"`` / ``","``).
    Returns the sorted indices to keep, or ``None`` when nothing needs dropping (the caller
    should then return its input unchanged).
    """
    n = len(sizes)
    if n == 0:
        return None
    full = sum(sizes) + sep_bytes * (n - 1)
    if full <= max_bytes:
        return None

    # HEAD — leading whole items up to the head budget (never more than half the total budget,
    # so the recent tail always gets the larger share).
    head_budget = min(max_bytes // 2, head_bytes)
    head_end = 0  # exclusive
    used = 0
    for i in range(n):
        add = sizes[i] + (sep_bytes if head_end else 0)
        if used + add > head_budget:
            break
        used += add
        head_end = i + 1

    # TAIL — the most recent whole items that fit the remaining budget, never reaching into the
    # head region.
    remaining = max_bytes - used - (sep_bytes if head_end else 0)
    start = n
    acc = 0
    for i in range(n - 1, head_end - 1, -1):
        add = sizes[i] + (sep_bytes if start != n else 0)
        if acc + add > remaining:
            break
        acc += add
        start = i
    if start >= n:  # budget too small even for the final item — keep at least it
        start = n - 1

    # Anchor the tail at the first compaction boundary at/after its start: the summary there
    # recaps the dropped middle, so the shared session resumes coherently.
    for i in range(start, n):
        if compaction[i]:
            start = i
            break

    return sorted(set(range(head_end)) | set(range(start, n)))
