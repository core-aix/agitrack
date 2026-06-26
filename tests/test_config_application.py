"""Every settings field written to the config file must be APPLIED to the running program.

Regression guard for: a value in the config (global or the repo-local overlay) silently not
taking effect — e.g. `use_worktrees: false` in the config yet aGiTrack still starting in a
worktree. Each test writes a real config file in a fresh dummy git repo and checks the value
reaches the actual runtime (the ProxyRunner cli.py builds, or the config the runner reads).

Two application mechanisms are covered:
  * cli.py reads the (repo-overlaid) config and passes it as a ProxyRunner constructor arg —
    use_worktrees, sandbox, commit_guidance, allowed_edit_paths;
  * the ProxyRunner reads its OWN GlobalConfig (with the repo overlay loaded) for the rest —
    default_backend, summarization_*, check_for_updates, menu_key, timings.
Both are exercised at GLOBAL scope and at REPO-overlay scope (repo overrides global).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import agitrack.cli as cli
from agitrack.git import GitRepo
from agitrack.proxy.runner import ProxyRunner


def _dummy_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "x"], cwd=repo, check=True, env={**env}, stdout=subprocess.PIPE
    )
    return repo


def _write_global(tmp_path: Path, monkeypatch, data: dict) -> Path:
    cfgdir = tmp_path / "cfg"
    cfgdir.mkdir()
    (cfgdir / "config.json").write_text(json.dumps({"default_backend": "claude", **data}))
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(cfgdir))
    return cfgdir


def _write_repo_overlay(repo: Path, data: dict) -> None:
    (repo / ".agitrack").mkdir(exist_ok=True)
    (repo / ".agitrack" / "config.json").write_text(json.dumps(data))


def _run_main_capturing_runner(monkeypatch, repo: Path) -> dict:
    """Call cli.main far enough to build the ProxyRunner, capturing its kwargs (the .run() loop
    is stubbed out). Returns the kwargs cli.py resolved from the config."""
    captured: dict = {}

    class _FakeRunner:
        def __init__(self, repo, **kw):
            captured.update(kw)
            captured["_repo"] = repo

        def run(self):
            return 0

    monkeypatch.setattr(cli, "ProxyRunner", _FakeRunner)
    cli.main(["--repo", str(repo), "--skip-privacy-ack"])
    return captured


# --- cli-passed fields: config file -> ProxyRunner constructor arg ----------------------------

CLI_FIELDS = [
    ("use_worktrees", False, "use_worktrees"),
    ("sandbox", False, "sandbox"),
    ("commit_guidance", False, "commit_guidance"),
    ("allowed_edit_paths", ["/data/shared", "/srv/x"], "allowed_edit_paths"),
]


@pytest.mark.parametrize("key,value,kwarg", CLI_FIELDS)
def test_global_config_field_is_applied_to_runner(tmp_path, monkeypatch, key, value, kwarg):
    _write_global(tmp_path, monkeypatch, {key: value})
    repo = _dummy_repo(tmp_path)
    captured = _run_main_capturing_runner(monkeypatch, repo)
    assert captured[kwarg] == value, f"global {key} not applied (runner got {captured.get(kwarg)!r})"


@pytest.mark.parametrize("key,value,kwarg", CLI_FIELDS)
def test_repo_overlay_field_is_applied_and_overrides_global(tmp_path, monkeypatch, key, value, kwarg):
    # Global holds the OPPOSITE/empty; the repo overlay must win and be applied.
    opposite = True if isinstance(value, bool) else []
    _write_global(tmp_path, monkeypatch, {key: opposite})
    repo = _dummy_repo(tmp_path)
    _write_repo_overlay(repo, {key: value})
    captured = _run_main_capturing_runner(monkeypatch, repo)
    assert captured[kwarg] == value, f"repo-overlay {key} not applied (runner got {captured.get(kwarg)!r})"


# --- runner-read fields: config file -> the GlobalConfig the running ProxyRunner reads ---------


def _runner_for(repo: Path) -> ProxyRunner:
    # Build the runner the way cli.py does for a no-worktree run (so no backend spawns); its
    # global_config is freshly loaded with the repo overlay, exactly as in a real launch.
    return ProxyRunner(GitRepo(repo), use_worktrees=False, backend=None)


def test_global_default_backend_is_applied(tmp_path, monkeypatch):
    _write_global(tmp_path, monkeypatch, {"default_backend": "opencode"})
    repo = _dummy_repo(tmp_path)
    runner = _runner_for(repo)
    assert runner.global_config.default_backend == "opencode"
    assert runner.backend.name == "opencode"  # the actual backend the runner will launch


def test_repo_overlay_default_backend_overrides_global(tmp_path, monkeypatch):
    _write_global(tmp_path, monkeypatch, {"default_backend": "claude"})
    repo = _dummy_repo(tmp_path)
    _write_repo_overlay(repo, {"default_backend": "opencode"})
    runner = _runner_for(repo)
    assert runner.backend.name == "opencode"


def test_over_limit_share_cap_is_refused_at_startup(tmp_path, monkeypatch, capsys):
    # A configured share cap above the 100 MiB hard limit is a config error: aGiTrack refuses
    # to start with a clear message rather than silently producing an unpushable file.
    from agitrack.sessions.share_cap import HARD_MAX_SHARED_BYTES

    _write_global(tmp_path, monkeypatch, {})
    repo = _dummy_repo(tmp_path)
    _write_repo_overlay(repo, {"share_max_transcript_bytes": HARD_MAX_SHARED_BYTES + 1})

    built = {"ran": False}

    class _FakeRunner:
        def __init__(self, repo, **kw):
            built["ran"] = True

        def run(self):
            return 0

    monkeypatch.setattr(cli, "ProxyRunner", _FakeRunner)
    rc = cli.main(["--repo", str(repo), "--skip-privacy-ack"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "Configuration error" in out and "share_max_transcript_bytes" in out
    assert built["ran"] is False  # refused BEFORE building/launching the session


def test_within_limit_share_cap_does_not_block_startup(tmp_path, monkeypatch):
    # A valid (sub-limit) configured cap must NOT be treated as an error.
    _write_global(tmp_path, monkeypatch, {})
    repo = _dummy_repo(tmp_path)
    _write_repo_overlay(repo, {"share_max_transcript_bytes": 50 * 1024 * 1024})
    captured = _run_main_capturing_runner(monkeypatch, repo)
    assert captured.get("_repo") is not None  # reached the runner build — no config error


RUNNER_READ_FIELDS = [
    ("summarization_enabled", False, lambda r: r._summarization_enabled()),
    ("summarization_model", "some-model-x", lambda r: r.global_config.summarization_model),
    ("check_for_updates", False, lambda r: r.global_config.check_for_updates),
    ("menu_key", "ctrl-t", lambda r: r.global_config.menu_key),
]


@pytest.mark.parametrize("key,value,read", RUNNER_READ_FIELDS)
def test_global_runner_read_field_is_applied(tmp_path, monkeypatch, key, value, read):
    _write_global(tmp_path, monkeypatch, {key: value})
    repo = _dummy_repo(tmp_path)
    runner = _runner_for(repo)
    assert read(runner) == value, f"global {key} not applied (got {read(runner)!r})"


@pytest.mark.parametrize("key,value,read", RUNNER_READ_FIELDS)
def test_repo_overlay_runner_read_field_is_applied(tmp_path, monkeypatch, key, value, read):
    opposite = True if isinstance(value, bool) else "GLOBAL-VALUE"
    _write_global(tmp_path, monkeypatch, {key: opposite})
    repo = _dummy_repo(tmp_path)
    _write_repo_overlay(repo, {key: value})
    runner = _runner_for(repo)
    assert read(runner) == value, f"repo-overlay {key} not applied (got {read(runner)!r})"


def test_timings_from_config_are_applied(tmp_path, monkeypatch):
    _write_global(tmp_path, monkeypatch, {"timings": {"file_stable_seconds": 12.0}})
    repo = _dummy_repo(tmp_path)
    runner = _runner_for(repo)
    assert runner.global_config.timings["file_stable_seconds"] == 12.0
    assert runner.FILE_STABLE_SECONDS == 12.0  # actually applied to the runtime constant
