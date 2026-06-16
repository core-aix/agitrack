"""Allow ``python -m agitrack`` to run the CLI. Used by the self-updater to re-exec
aGiTrack after an in-place update, independent of the installed console script."""

from agitrack.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
