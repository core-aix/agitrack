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


def test_backend_installed_uses_path_lookup(monkeypatch):
    monkeypatch.setattr(bs.shutil, "which", lambda exe: "/usr/bin/" + exe if exe == "claude" else None)
    assert bs.backend_installed("claude") is True
    assert bs.backend_installed("opencode") is False


def test_select_default_backend_picks_installed_choice(monkeypatch):
    monkeypatch.setattr(bs, "backend_installed", lambda name: True)
    config = FakeConfig()
    # Menu is alphabetical, so option 2 is opencode.
    chosen = select_default_backend(config, input_fn=_inputs("2"), output_fn=lambda _s: None)
    assert chosen == "opencode"
    assert config.saved == ["opencode"]


def test_select_default_backend_defaults_to_first_on_empty_input(monkeypatch):
    monkeypatch.setattr(bs, "backend_installed", lambda name: True)
    config = FakeConfig()
    chosen = select_default_backend(config, input_fn=_inputs(""), output_fn=lambda _s: None)
    assert chosen == "claude"


def test_select_default_backend_offers_install_or_choose_another(monkeypatch):
    # opencode is missing; user picks it, backs out, then picks the installed claude.
    monkeypatch.setattr(bs, "backend_installed", lambda name: name == "claude")
    config = FakeConfig()
    chosen = select_default_backend(config, input_fn=_inputs("2", "b", "1"), output_fn=lambda _s: None)
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
