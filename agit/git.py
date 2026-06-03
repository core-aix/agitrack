from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


class GitRepo:
    def __init__(self, repo: Path) -> None:
        self.repo = repo.resolve()
        self._run(["git", "rev-parse", "--show-toplevel"])

    @classmethod
    def discover(cls, path: Path) -> "GitRepo":
        process = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if process.returncode != 0:
            raise GitError(f"Not a Git repository: {path}")
        return cls(Path(process.stdout.strip()))

    def status_short(self) -> str:
        return self._run(["git", "status", "--short"]).stdout

    def has_changes(self) -> bool:
        return bool(self.status_short().strip())

    def add_tracked(self) -> None:
        self._run(["git", "add", "-u"])

    def stage_paths(self, paths: list[str]) -> None:
        if paths:
            self._run(["git", "add", "--", *paths])

    def untracked_files(self) -> list[str]:
        output = self._run(["git", "ls-files", "--others", "--exclude-standard"]).stdout
        return [line for line in output.splitlines() if line]

    def has_staged_changes(self) -> bool:
        process = self._run(["git", "diff", "--cached", "--quiet"], check=False)
        if process.returncode == 0:
            return False
        if process.returncode == 1:
            return True
        raise GitError(process.stderr.strip() or "Unable to inspect staged changes")

    def commit(self, message: str) -> None:
        self._run(["git", "commit", "-F", "-"], input_text=message)

    def _run(self, command: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        process = subprocess.run(
            command,
            cwd=self.repo,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip()
            raise GitError(f"Command failed: {' '.join(command)}\n{detail}")
        return process
