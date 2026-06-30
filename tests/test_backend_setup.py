import pytest

import agitrack.backends.setup as bs
from agitrack.backends.setup import BackendUnavailable, ensure_installed_backend, select_default_backend
from agitrack.backends.proxy_agents import available_backends
from agitrack.config import GlobalConfig


class FakeConfig:
    def __init__(self):
        self._backend = None
        self.saved = []
        self.summarization_model: str | None = None

    @property
    def default_backend(self):
        return self._backend

    @default_backend.setter
    def default_backend(self, value):
        self._backend = value
        self.saved.append(value)


def _inputs(*values):
    iterator = iter(values)
    return lambda _prompt: next(iterator)


def test_available_backends_is_alphabetical():
    assert available_backends() == ["claude", "opencode"]


def test_backend_installed_uses_executable_lookup(monkeypatch):
    # backend_installed resolves through which_executable (Windows-aware), not raw shutil.which.
    monkeypatch.setattr(bs, "which_executable", lambda exe: "/usr/bin/" + exe if exe == "claude" else None)
    assert bs.backend_installed("claude") is True
    assert bs.backend_installed("opencode") is False


def test_select_default_backend_all_installed_defaults_to_first(monkeypatch):
    # Both installed: nothing to install, but the user IS asked which to make the default;
    # a bare Enter (empty) takes the first (claude).
    monkeypatch.setattr(bs, "backend_installed", lambda name: True)
    config = FakeConfig()
    chosen = select_default_backend(config, input_fn=_inputs(""), output_fn=lambda _s: None)
    assert chosen == "claude"
    assert config.saved == ["claude"]


def test_select_default_backend_two_installed_user_picks_second(monkeypatch):
    # Both installed: the choose-default prompt lets the user pick #2 (opencode) as the default.
    monkeypatch.setattr(bs, "backend_installed", lambda name: True)
    config = FakeConfig()
    chosen = select_default_backend(config, input_fn=_inputs("2"), output_fn=lambda _s: None)
    assert chosen == "opencode"
    assert config.saved == ["opencode"]


def test_select_default_backend_single_installed_not_asked(monkeypatch):
    # Exactly one backend installed (opencode): after Enter skips the install offer for the
    # other, it's used as the default WITHOUT a separate choose-default prompt (nothing to
    # choose between).
    monkeypatch.setattr(bs, "backend_installed", lambda name: name == "opencode")
    config = FakeConfig()
    lines: list[str] = []
    chosen = select_default_backend(config, input_fn=_inputs(""), output_fn=lines.append)
    assert chosen == "opencode"
    assert not any("use by default" in line for line in lines)  # the choose prompt never appeared


def test_select_default_backend_explains_how_to_switch(monkeypatch):
    # After the default is chosen, the user is told how to change it later: per-run --backend
    # and the in-app settings menu (repo or global scope).
    monkeypatch.setattr(bs, "backend_installed", lambda name: True)
    lines: list[str] = []
    select_default_backend(FakeConfig(), input_fn=_inputs("1"), output_fn=lines.append)
    text = "\n".join(lines)
    assert "--backend" in text
    assert "Settings" in text and "scope" in text.lower()


def test_select_default_backend_skip_keeps_installed_default(monkeypatch):
    # claude installed, opencode not; user is asked but presses Enter to skip → default claude.
    monkeypatch.setattr(bs, "backend_installed", lambda name: name == "claude")
    config = FakeConfig()
    chosen = select_default_backend(
        config,
        input_fn=_inputs(""),  # skip installing opencode
        output_fn=lambda _s: None,
        install_fn=lambda name, output_fn: pytest.fail("skip must not install anything"),
    )
    assert chosen == "claude"


def test_select_default_backend_installs_chosen_uninstalled(monkeypatch):
    # claude installed, opencode not; user enters '2' to install opencode, then Enter.
    installs = []
    monkeypatch.setattr(bs, "backend_installed", lambda name: name == "claude" or name in installs)
    config = FakeConfig()
    chosen = select_default_backend(
        config,
        input_fn=_inputs("2", ""),
        output_fn=lambda _s: None,
        install_fn=lambda name, output_fn: installs.append(name) or True,
    )
    assert installs == ["opencode"]
    assert chosen == "claude"


def test_ensure_installed_backend_returns_installed_backend(monkeypatch):
    monkeypatch.setattr(bs, "backend_installed", lambda name: True)
    assert ensure_installed_backend("opencode", FakeConfig(), interactive=True) == "opencode"


def test_ensure_installed_backend_switches_to_installed_alternative(monkeypatch):
    monkeypatch.setattr(bs, "backend_installed", lambda name: name == "claude")
    config = FakeConfig()
    resolved = ensure_installed_backend(
        "opencode", config, interactive=True, input_fn=_inputs("claude"), output_fn=lambda _s: None
    )
    assert resolved == "claude"
    assert config.saved == ["claude"]


def test_ensure_installed_backend_non_interactive_raises(monkeypatch):
    monkeypatch.setattr(bs, "backend_installed", lambda name: False)
    with pytest.raises(BackendUnavailable):
        ensure_installed_backend("opencode", FakeConfig(), interactive=False)


def test_ensure_installed_backend_quit_raises(monkeypatch):
    monkeypatch.setattr(bs, "backend_installed", lambda name: False)
    with pytest.raises(BackendUnavailable):
        ensure_installed_backend(
            "opencode", FakeConfig(), interactive=True, input_fn=_inputs("q"), output_fn=lambda _s: None
        )


def test_global_config_has_default_backend(tmp_path):
    config = GlobalConfig(tmp_path / "config.json")
    assert config.has_default_backend() is False
    config.default_backend = "claude"
    assert GlobalConfig(tmp_path / "config.json").has_default_backend() is True


def test_select_default_summarizer_model_saves_recommended_smallest(monkeypatch):
    import agitrack.summaries.model_select as ms
    from agitrack.backends.setup import select_default_summarizer_model

    monkeypatch.setattr(
        ms, "list_available_models", lambda name: ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-8"]
    )
    config = FakeConfig()
    # Default (1) is the smallest (Haiku), saved as the global summarizer model.
    select_default_summarizer_model(config, "claude", input_fn=_inputs(""), output_fn=lambda _s: None)
    assert config.summarization_model == "claude-haiku-4-5-20251001"


def test_select_default_summarizer_model_same_as_session(monkeypatch):
    import agitrack.summaries.model_select as ms
    from agitrack.backends.setup import select_default_summarizer_model

    monkeypatch.setattr(ms, "list_available_models", lambda name: ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"])
    config = FakeConfig()
    # The last option ("same as session") clears the model → None.
    select_default_summarizer_model(config, "claude", input_fn=_inputs("3"), output_fn=lambda _s: None)
    assert config.summarization_model is None


def test_select_default_summarizer_model_noop_when_models_unlistable(monkeypatch):
    import agitrack.summaries.model_select as ms
    from agitrack.backends.setup import select_default_summarizer_model

    monkeypatch.setattr(ms, "list_available_models", lambda name: [])
    config = FakeConfig()
    config.summarization_model = "preset"
    select_default_summarizer_model(config, "opencode", input_fn=_inputs(), output_fn=lambda _s: None)
    assert config.summarization_model == "preset"  # left unchanged, no prompt shown
