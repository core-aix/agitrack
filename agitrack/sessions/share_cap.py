"""Bound a shared session transcript to a size limit while keeping it RESUMABLE.

Git hosts reject very large files — GitHub blocks any pushed file over 100 MiB — so a
long-running session whose transcript has grown past that limit can't be shared at all. We
keep the most RECENT turns as a contiguous tail and drop the older middle.

Crucially, the kept tail must begin at a *resume boundary* — a point a backend can reconstruct
the conversation from (for Claude, a real user prompt or a compaction summary; never mid-turn).
Empirically, keeping a disconnected "head" (the original opening) breaks Claude's resume
entirely — it reconstructs no prior context — so we deliberately do NOT keep one; the system
prompt is re-applied at runtime and a compaction summary recaps earlier context.
"""

from __future__ import annotations

from collections.abc import Sequence

# Storing files much larger than ~20 MB in git is poor practice (slow clones/fetches, bloated
# packs) well before GitHub's 100 MiB hard block — so cap the shared transcript here. This is
# user-configurable, but never past HARD_MAX_SHARED_BYTES.
DEFAULT_MAX_SHARED_BYTES = 20 * 1024 * 1024
# Absolute ceiling, enforced regardless of config: Git hosts reject pushes of files over
# 100 MiB, so a shared transcript may never be configured (or trimmed) above it.
HARD_MAX_SHARED_BYTES = 100 * 1024 * 1024


def select_kept_indices(
    sizes: Sequence[int],
    boundary: Sequence[bool],
    max_bytes: int,
    *,
    sep_bytes: int = 1,
) -> list[int] | None:
    """Pick which trailing items (lines for Claude, messages for OpenCode) to KEEP so the
    joined result fits ``max_bytes`` — the most recent items, as a contiguous tail anchored to
    BEGIN at a resume boundary (``boundary[i]`` True) so the trimmed conversation starts cleanly.

    ``sizes[i]`` is item *i*'s serialized byte length; ``sep_bytes`` is the per-join overhead
    between items (1 for ``"\\n"`` / ``","``). Returns the sorted indices to keep, or ``None``
    when nothing needs dropping (the caller should then return its input unchanged).
    """
    n = len(sizes)
    if n == 0:
        return None
    full = sum(sizes) + sep_bytes * (n - 1)
    if full <= max_bytes:
        return None

    # The most recent whole items that fit the budget.
    start = n
    acc = 0
    for i in range(n - 1, -1, -1):
        add = sizes[i] + (sep_bytes if start != n else 0)
        if acc + add > max_bytes:
            break
        acc += add
        start = i
    if start >= n:  # budget too small even for the final item — keep at least it
        start = n - 1

    # Anchor the tail to start at a resume boundary so it's reconstructible, not mid-turn.
    anchored = next((i for i in range(start, n) if boundary[i]), None)
    if anchored is not None:
        start = anchored  # earliest boundary that fits → most recent context, clean start
    else:
        # The most recent turn alone exceeds the budget, so no boundary fits. Beginning
        # mid-turn isn't resumable; fall back to the latest boundary BEFORE the window (keeps a
        # bit more than the soft budget, still bounded by the hard cap) so the start stays clean.
        earlier = [i for i in range(start) if boundary[i]]
        if earlier:
            start = earlier[-1]

    return list(range(start, n))
