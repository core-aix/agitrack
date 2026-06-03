from __future__ import annotations

import argparse
from pathlib import Path

from agit.git import GitError, GitRepo
from agit.shell import AgitShell


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interactive agent + git commit orchestration.")
    parser.add_argument("--repo", default=".", help="target Git repository path")
    parser.add_argument("--verbose", action="store_true", help="show aGiT diagnostic messages")
    args = parser.parse_args(argv)

    try:
        repo = GitRepo.discover(Path(args.repo).expanduser())
        AgitShell(repo, verbose=args.verbose).run()
    except GitError as error:
        print(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
