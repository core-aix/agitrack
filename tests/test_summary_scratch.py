"""Summarizer sessions must never be adopted as the resume session (#56, #8).

A headless summarizer call (``claude -p`` / ``opencode run`` / ``codex exec``)
records a real backend session keyed by its working directory. When the
summarizer ran inside the session worktree, the summary conversation became the
worktree's newest non-empty session — the parse worker and the exit-time
adoption then resumed the *summary* session instead of the user's conversation
(entering it on restart / starting what looked like a brand-new session). These
tests pin the fix: every summarizer backend is constructed with the scratch
directory, never the worktree or the repo.

Running outside the repo has a consequence of its own, so it is pinned here too:
a PROJECT-scoped model pin (``opencode.json``, ``.codex/config.toml``) cannot
apply in the scratch dir, so on every non-Claude backend the summarizer must be
given the SESSION's model rather than letting the CLI reach for its global
default — which on a machine that pins models per project is one the user may
have no access to at all.
"""

import pytest

from agitrack.config import AgitrackState
from agitrack.git import GitRepo
from agitrack.summaries import summary_scratch_dir

from proxy_helpers import make_runner

# Every backend aGiTrack can run, with a model id that backend's own CLI accepts. The scratch
# dir is backend-independent, so the "never in the worktree" rule is checked on all three.
BACKENDS = [("claude", "claude-opus-5[1m]"), ("codex", "gpt-5.4-mini"), ("opencode", "opencode/deepseek-v4-flash-free")]
# The backends whose model pin is PROJECT-scoped, so the scratch dir loses it (see above).
# Codex was added to this rule at the source but had no test: only `..._on_opencode` variants
# existed, so the second backend with the same exposure was riding on the first one's coverage.
PROJECT_PINNED = [pair for pair in BACKENDS if pair[0] != "claude"]


def test_summary_scratch_dir_is_under_config_dir_and_outside_repos(tmp_path, monkeypatch):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-config"))

    scratch = summary_scratch_dir()

    assert scratch == tmp_path / "agit-config" / "summarizer"
    assert scratch.is_dir()  # created so the backend can chdir into it


@pytest.mark.parametrize("backend", [name for name, _model in BACKENDS])
def test_proxy_summarizer_backend_never_runs_in_the_worktree(tmp_path, monkeypatch, backend):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-config"))
    repo = GitRepo.init(tmp_path / "worktree")
    runner = make_runner(repo=repo, state=AgitrackState(tmp_path / "worktree"))
    runner.global_config = None
    runner.state.backend = backend

    summarizer = runner._make_summarizer()

    assert summarizer is not None
    assert summarizer.backend.name == backend  # the SESSION's backend summarizes, never a default
    assert summarizer.backend.repo == summary_scratch_dir()
    assert summarizer.backend.repo != repo.repo  # the leak: cwd = worktree


@pytest.mark.parametrize("backend", [name for name, _model in BACKENDS])
def test_shell_summarizer_backend_never_runs_in_the_repo(tmp_path, monkeypatch, backend):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-config"))
    from agitrack.shell.runner import AgitrackShell

    repo = GitRepo.init(tmp_path / "repo")
    shell = AgitrackShell(repo, backend=backend)

    summarizer_backend = shell._summarizer_backend()

    assert summarizer_backend.name == backend
    assert summarizer_backend.repo == summary_scratch_dir()
    assert summarizer_backend.repo != repo.repo


@pytest.mark.parametrize("backend,session_model", PROJECT_PINNED)
def test_a_project_pinned_backend_summarizes_with_the_SESSION_model(tmp_path, monkeypatch, backend, session_model):
    # Found live on OpenCode: EVERY commit summary failed, silently. The summarizer runs outside
    # the repo (above), so the project's `opencode.json` model pin cannot apply — and with no
    # --model OpenCode falls back to its GLOBAL default, which on a machine that pins models per
    # project is one the user may have no access to (measured: exit 1, a 403 from the provider).
    # Commit subjects stayed raw prompts where Claude sessions got real summaries. Codex has the
    # same project-scoped pin (`.codex/config.toml`) and so exactly the same exposure.
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-config"))
    repo = GitRepo.init(tmp_path / f"wt-{backend}")
    runner = make_runner(repo=repo, state=AgitrackState(tmp_path / f"wt-{backend}"))
    runner.global_config = None
    runner.state.backend = backend
    runner.state.model = session_model  # what the session is actually running

    summarizer = runner._make_summarizer()

    assert summarizer.model == session_model


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


@pytest.mark.parametrize(
    "backend,session_model,configured",
    [
        ("opencode", "opencode/deepseek-v4-flash-free", "anthropic/claude-haiku-4-5"),
        ("codex", "gpt-5.4-mini", "o5-nano"),
    ],
)
def test_a_configured_summarization_model_still_wins(tmp_path, monkeypatch, backend, session_model, configured):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-config"))
    repo = GitRepo.init(tmp_path / f"wt-cfg-{backend}")
    runner = make_runner(repo=repo, state=AgitrackState(tmp_path / f"wt-cfg-{backend}"))
    runner.global_config = None
    runner.state.backend = backend
    runner.state.model = session_model
    runner.state.summarization_model = configured

    assert runner._make_summarizer().model == configured


def test_a_claude_shaped_summarization_model_is_dropped_on_codex_for_the_session_model(tmp_path, monkeypatch):
    # `summarization_model` is one GLOBAL setting but a model id belongs to a provider, and
    # Codex spells its ids bare exactly as the Claude CLI does. A user who configured
    # "claude-haiku-4-5" while on Claude and then switched a session to Codex had that id handed
    # to `codex -m`, which rejects it — EVERY summary failed after the switch. The incompatible
    # id must be dropped AND the session's own model used in its place, not merely dropped
    # (which would leave the scratch dir's missing project pin exposed all over again).
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-config"))
    repo = GitRepo.init(tmp_path / "wt-cx-mixed")
    runner = make_runner(repo=repo, state=AgitrackState(tmp_path / "wt-cx-mixed"))
    runner.global_config = None
    runner.state.backend = "codex"
    runner.state.model = "gpt-5.4-mini"
    runner.state.summarization_model = "claude-haiku-4-5"

    assert runner._make_summarizer().model == "gpt-5.4-mini"


@pytest.mark.parametrize("backend,session_model", PROJECT_PINNED)
def test_the_DAEMONS_summarizer_also_uses_the_session_model(tmp_path, monkeypatch, backend, session_model):
    # The proxy and the background daemon each build their own summarizer. Fixing only the proxy
    # left every daemon-made OpenCode commit with a raw-prompt subject: the live `-b` run logged
    # "summary failed … UnusableSummaryError('summarizer backend exited with 1: ')" for each turn.
    # Two copies, one bug — so both are pinned, on every backend that has the exposure.
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-config"))
    from agitrack.proxy.background import BackgroundRunner

    repo = GitRepo.init(tmp_path / f"bg-{backend}")
    (tmp_path / f"bg-{backend}" / "seed.txt").write_text("seed\n", encoding="utf-8")
    repo.stage_paths(["seed.txt"])
    repo.commit("seed")
    monkeypatch.setattr("agitrack.proxy.background.make_proxy_agent", lambda _name: object())
    runner = BackgroundRunner(repo, backend=backend)
    runner.state.model = session_model

    assert runner._make_summarizer().model == session_model


def test_the_DAEMONS_summarizer_still_lets_claude_choose(tmp_path, monkeypatch):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-config"))
    from agitrack.proxy.background import BackgroundRunner

    repo = GitRepo.init(tmp_path / "bg-cl")
    (tmp_path / "bg-cl" / "seed.txt").write_text("seed\n", encoding="utf-8")
    repo.stage_paths(["seed.txt"])
    repo.commit("seed")
    monkeypatch.setattr("agitrack.proxy.background.make_proxy_agent", lambda _name: object())
    runner = BackgroundRunner(repo, backend="claude")
    runner.state.model = "claude-opus-5[1m]"

    assert runner._make_summarizer().model is None
