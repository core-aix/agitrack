"""Summarizer sessions must never be adopted as the resume session (#56, #8).

A headless summarizer call (``claude -p`` / ``opencode run``) records a real
backend session keyed by its working directory. When the summarizer ran inside
the session worktree, the summary conversation became the worktree's newest
non-empty session — the parse worker and the exit-time adoption then resumed
the *summary* session instead of the user's conversation (entering it on
restart / starting what looked like a brand-new session). These tests pin the
fix: every summarizer backend is constructed with the scratch directory, never
the worktree or the repo.
"""

from agitrack.config import AgitrackState
from agitrack.git import GitRepo
from agitrack.summaries import summary_scratch_dir

from proxy_helpers import make_runner


def test_summary_scratch_dir_is_under_config_dir_and_outside_repos(tmp_path, monkeypatch):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-config"))

    scratch = summary_scratch_dir()

    assert scratch == tmp_path / "agit-config" / "summarizer"
    assert scratch.is_dir()  # created so the backend can chdir into it


def test_proxy_summarizer_backend_never_runs_in_the_worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-config"))
    repo = GitRepo.init(tmp_path / "worktree")
    runner = make_runner(repo=repo, state=AgitrackState(tmp_path / "worktree"))
    runner.global_config = None
    runner.state.backend = "claude"

    summarizer = runner._make_summarizer()

    assert summarizer is not None
    assert summarizer.backend.repo == summary_scratch_dir()
    assert summarizer.backend.repo != repo.repo  # the leak: cwd = worktree


def test_shell_summarizer_backend_never_runs_in_the_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-config"))
    from agitrack.shell.runner import AgitrackShell

    repo = GitRepo.init(tmp_path / "repo")
    shell = AgitrackShell(repo, backend="claude")

    backend = shell._summarizer_backend()

    assert backend.repo == summary_scratch_dir()
    assert backend.repo != repo.repo


def test_an_opencode_summarizer_uses_the_SESSION_model_not_opencodes_global_default(tmp_path, monkeypatch):
    # Found live: EVERY OpenCode commit summary failed, silently. The summarizer runs outside
    # the repo (above), so the project's `opencode.json` model pin cannot apply — and with no
    # --model OpenCode falls back to its GLOBAL default, which on a machine that pins models per
    # project is one the user may have no access to (measured: exit 1, a 403 from the provider).
    # Commit subjects stayed raw prompts where Claude sessions got real summaries.
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-config"))
    repo = GitRepo.init(tmp_path / "wt-oc")
    runner = make_runner(repo=repo, state=AgitrackState(tmp_path / "wt-oc"))
    runner.global_config = None
    runner.state.backend = "opencode"
    runner.state.model = "opencode/deepseek-v4-flash-free"  # what the session is actually running

    summarizer = runner._make_summarizer()

    assert summarizer.model == "opencode/deepseek-v4-flash-free"


def test_a_claude_summarizer_still_lets_the_cli_pick_its_own_default(tmp_path, monkeypatch):
    # Deliberately asymmetric: the Claude CLI's own default is the right choice there, and the
    # recorded session model can carry a variant suffix (`claude-opus-5[1m]`) that --model rejects.
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-config"))
    repo = GitRepo.init(tmp_path / "wt-cl")
    runner = make_runner(repo=repo, state=AgitrackState(tmp_path / "wt-cl"))
    runner.global_config = None
    runner.state.backend = "claude"
    runner.state.model = "claude-opus-5[1m]"

    assert runner._make_summarizer().model is None


def test_a_configured_summarization_model_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-config"))
    repo = GitRepo.init(tmp_path / "wt-cfg")
    runner = make_runner(repo=repo, state=AgitrackState(tmp_path / "wt-cfg"))
    runner.global_config = None
    runner.state.backend = "opencode"
    runner.state.model = "opencode/deepseek-v4-flash-free"
    runner.state.summarization_model = "anthropic/claude-haiku-4-5"

    assert runner._make_summarizer().model == "anthropic/claude-haiku-4-5"
