"""One answer to "which aGiTrack is this?", for every place that reports it to a person.

The release version alone cannot answer it on a source checkout: every commit between two
releases carries the same one, so ``0.6.13`` does not distinguish the code that shipped from the
code someone is running fifty commits later. A checkout therefore reports its commit too.

The point of this module is that there is exactly ONE such string. It was computed in three
places — ``agitrack --version``, the dashboard's ``/state`` poll and the version embedded in the
page — and they disagreed: the CLI named the commit while the dashboard showed the bare release
version, so the same install answered the same question two different ways depending on where it
was asked. The dashboard's copy also drives its self-reload, which means a disagreement is not
cosmetic: the page compares this string across polls to decide whether the daemon serving it has
been replaced.

Machine-read fields deliberately do NOT use this: the ``agitrack_version:`` commit-metadata
trailer, the daemon registry's ``version``, and the autotrack hook's stamp are parsed or compared
for equality, and a commit suffix would break that.
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def source_suffix() -> str:
    """``" (source <sha>[-dirty])"`` when aGiTrack runs from a git checkout, else ``""``.

    Resolved ONCE per process. It identifies the code this process loaded, which cannot change
    underneath it — a daemon that finds a new commit restarts, and the successor computes its
    own — while the dashboard asks on every poll, and shelling out to git several times a second
    to re-derive a constant is waste.

    Runs git through :class:`~agitrack.git.GitRepo`, never a bare ``subprocess.run``: that is what
    carries the repo-wide invariants (UTF-8 decoding rather than the platform code page, console
    isolation so a Windows spawn opens no window), and hand-rolling the two calls broke both.

    Side-effect-free by contract: it looks only at aGiTrack's OWN install directory and never
    discovers or touches the user's repository. Any failure degrades to no suffix — reporting a
    version must not be something that can fail.
    """
    try:
        from agitrack.git import GitRepo
        from agitrack.update.updater import detect_source_repo

        root = detect_source_repo()
        if root is None:
            return ""
        source = GitRepo(root)
        rev = source.short_sha("HEAD")
        if not rev:
            return ""
        dirty = "-dirty" if source.status_short().strip() else ""
        return f" (source {rev}{dirty})"
    except Exception:
        return ""


def version_line() -> str:
    """What ``agitrack --version`` prints, and what every other version indicator shows.

    The release version stays FIRST and unadorned, so a prefix read
    (``--version | cut -d' ' -f1``) still yields exactly what it always did. ``__version__`` is
    read fresh each call rather than cached with the suffix, so a test that patches it is
    answered honestly."""
    from agitrack import __version__

    return f"{__version__}{source_suffix()}"
