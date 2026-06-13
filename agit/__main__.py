"""Allow ``python -m agit`` to run the CLI. Used by the self-updater to re-exec
aGiT after an in-place update, independent of the installed console script."""

from agit.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
