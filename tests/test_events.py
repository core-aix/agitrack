"""The user-facing event log (``--log-file`` / ``log_file`` config): append-only, greppable,
best-effort. Works in every mode; here we lock the primitive (path resolution + line format)."""

from __future__ import annotations

from pathlib import Path

from agitrack.events import EventLog, resolve_log_path


def test_resolve_log_path_none_relative_absolute_and_tilde(tmp_path):
    assert resolve_log_path(None, tmp_path) is None
    assert resolve_log_path("", tmp_path) is None
    assert resolve_log_path("   ", tmp_path) is None
    # A relative path resolves against the repo root, so the same config value is stable
    # regardless of the shell's cwd.
    assert resolve_log_path("events.log", tmp_path) == tmp_path / "events.log"
    assert resolve_log_path("logs/a.log", tmp_path) == tmp_path / "logs" / "a.log"
    # An absolute path is taken as it stands. Built from the filesystem's own anchor, because
    # "/abs/x.log" is NOT absolute on Windows (no drive) and is resolved against the repo root
    # there, which is the correct Windows reading of a rooted path rather than a bug.
    absolute = Path(tmp_path.anchor) / "abs" / "x.log"
    assert resolve_log_path(str(absolute), tmp_path) == absolute
    assert resolve_log_path("~/x.log", tmp_path) == Path.home() / "x.log"


def test_event_log_disabled_is_a_noop():
    # A None path makes emit a no-op — callers never branch on whether logging is on.
    EventLog(None).emit("commit", sha="abc")  # must not raise


def test_event_log_format_quotes_and_drops_none(tmp_path):
    log = tmp_path / "events.log"
    el = EventLog(log)
    el.emit("commit", sha="deadbeef1234", type="agent", subject="Add validation to parse()")
    el.emit("ai-change-detected", backend="claude", session=None)  # None fields dropped
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    # <iso-timestamp> event k=v … ; whitespace/quotes force quoting; a value with none stays bare.
    assert lines[0].split(" ", 1)[1] == 'commit sha=deadbeef1234 type=agent subject="Add validation to parse()"'
    assert lines[1].endswith("ai-change-detected backend=claude")  # session=None omitted


def test_event_log_flattens_newlines(tmp_path):
    log = tmp_path / "events.log"
    EventLog(log).emit("note", text="line one\nline two")
    assert len(log.read_text().splitlines()) == 1  # one event is always one line


def test_event_log_survives_unwritable_path(tmp_path):
    # Best-effort: an unwritable path (a directory where the parent is a file) is swallowed.
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    EventLog(blocker / "sub" / "events.log").emit("commit", sha="abc")  # must not raise


def _repo(tmp_path):
    import subprocess

    from agitrack.git import GitRepo

    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "a.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return GitRepo.discover(root)


def test_an_in_repo_log_file_is_git_ignored(tmp_path):
    """The default place to put the log is the repo root (a relative `--log-file events.log`
    resolves there) and nothing excluded it. In the TUI that left a permanent `?? events.log`;
    under -b the tracker's own `git add` swept the file into the AGENT'S commit, so aGiTrack's
    telemetry about a turn ended up inside the turn, attributed to the AI, in permanent
    history."""
    import subprocess

    from agitrack.events import EventLog, exclude_log_file, resolve_log_path

    repo = _repo(tmp_path)
    path = resolve_log_path("events.log", repo.repo)
    exclude_log_file(repo.repo, path)
    EventLog(path).emit("daemon-start", backend="claude")

    porcelain = subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", "status", "-uall", "--porcelain"],
        cwd=repo.repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    assert porcelain == ""
    assert path.read_text(encoding="utf-8").strip().endswith("backend=claude")


def test_a_log_file_outside_the_repo_is_left_alone(tmp_path):
    from agitrack.events import exclude_log_file

    repo = _repo(tmp_path)
    exclude_log_file(repo.repo, tmp_path / "elsewhere.log")
    exclude = repo.repo / ".git" / "info" / "exclude"
    assert not exclude.exists() or "elsewhere.log" not in exclude.read_text(encoding="utf-8")


def test_the_one_shot_shell_honours_log_file(tmp_path, monkeypatch):
    """`--log-file` was parsed, passed to the background tracker and the proxy runner, then
    DROPPED: AgitrackShell took no log_file argument at all, so `--prompt`/`--json` runs — even
    ones that made real commits — created no log file anywhere, no warning, exit 0, while
    `--help` said verbatim "Works in every mode"."""
    from agitrack.shell.runner import AgitrackShell

    repo = _repo(tmp_path)
    shell = AgitrackShell(repo, log_file="events.log")

    assert shell.events.enabled
    assert shell.events.path == repo.repo / "events.log"
