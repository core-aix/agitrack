import json
import types
from pathlib import Path

import pytest

from agitrack.backends.claude import ClaudeBackend
from agitrack.backends.proxy_agents import available_backends, make_proxy_agent
from agitrack.config import GlobalConfig
from agitrack.config import AgitrackState


def test_available_backends_includes_opencode_and_claude():
    assert set(available_backends()) == {"opencode", "claude"}


def test_opencode_proxy_agent_spawn_command():
    agent = make_proxy_agent("opencode")
    assert agent.name == "opencode"
    assert agent.new_session_id() is None
    assert agent.spawn_command(Path("/repo"), session_id=None, resume=False) == ["opencode", "/repo"]
    assert agent.spawn_command(Path("/repo"), session_id="s1", resume=True) == ["opencode", "--session", "s1", "/repo"]


def test_claude_proxy_agent_spawn_command_uses_session_id_and_resume():
    from agitrack.backends.proxy_agents import AGENT_SYSTEM_NOTE

    agent = make_proxy_agent("claude")
    assert agent.name == "claude"
    # Claude picks an explicit session id so aGiTrack knows which transcript to read.
    assert len(agent.new_session_id()) == 36
    # The coding agent is told aGiTrack auto-commits (Claude supports --append-system-prompt),
    # so it doesn't self-commit; the note follows the session/resume flags.
    note = ["--append-system-prompt", AGENT_SYSTEM_NOTE]
    assert agent.spawn_command(Path("/repo"), session_id="u1", resume=False) == ["claude", "--session-id", "u1", *note]
    assert agent.spawn_command(Path("/repo"), session_id="u1", resume=True) == ["claude", "--resume", "u1", *note]
    assert agent.spawn_command(Path("/repo"), session_id=None, resume=False) == ["claude", *note]
    assert "aGiTrack" in AGENT_SYSTEM_NOTE and "git commit" in AGENT_SYSTEM_NOTE
    # In the default worktree model the note also explains that aGiTrack runs in a worktree
    # under .agitrack/ and auto-merges it into the current branch (so the agent leaves those
    # alone); spawn_command defaults to that variant.
    assert ".agitrack/" in AGENT_SYSTEM_NOTE and "worktree" in AGENT_SYSTEM_NOTE
    # Under --no-worktree the worktree clause is dropped (the agent edits the branch directly),
    # but the no-self-commit guidance stays.
    from agitrack.backends.proxy_agents import agent_system_note

    no_wt = agent_system_note(use_worktrees=False)
    assert ".agitrack/" not in no_wt and "worktree" not in no_wt
    assert "git commit" in no_wt
    cmd = agent.spawn_command(Path("/repo"), session_id="u1", resume=False, use_worktrees=False)
    assert cmd == ["claude", "--session-id", "u1", "--append-system-prompt", no_wt]
    # commit_guidance=False (--no-commit-guidance) omits the note entirely.
    assert agent.spawn_command(Path("/repo"), session_id=None, resume=False, commit_guidance=False) == ["claude"]
    assert "--append-system-prompt" not in agent.spawn_command(
        Path("/repo"), session_id="u1", resume=True, commit_guidance=False
    )


def test_opencode_proxy_agent_spawn_command_has_no_system_prompt_append():
    # OpenCode's interactive TUI exposes no flag to append to the system prompt, so the
    # note is not added there ("if there is this option" — there isn't for OpenCode).
    agent = make_proxy_agent("opencode")
    cmd = agent.spawn_command(Path("/repo"), session_id="s1", resume=True)
    assert "--append-system-prompt" not in cmd
    assert cmd == ["opencode", "--session", "s1", "/repo"]


def test_spawn_command_executable_replaces_backend_binary():
    # A custom launch command (e.g. a wrapper) replaces the backend executable head,
    # while the backend's own flags are still appended after it.
    claude = make_proxy_agent("claude")
    cmd = claude.spawn_command(
        Path("/repo"), session_id="u1", resume=True, commit_guidance=False, executable=["somewrapper", "claude"]
    )
    assert cmd == ["somewrapper", "claude", "--resume", "u1"]
    # No session id, no resume: just the wrapper head.
    assert claude.spawn_command(
        Path("/repo"), session_id=None, resume=False, commit_guidance=False, executable=["somewrapper", "claude"]
    ) == ["somewrapper", "claude"]

    opencode = make_proxy_agent("opencode")
    assert opencode.spawn_command(Path("/repo"), session_id="s1", resume=True, executable=["w", "opencode"]) == [
        "w",
        "opencode",
        "--session",
        "s1",
        "/repo",
    ]
    # executable=None keeps the default binary head.
    assert opencode.spawn_command(Path("/repo"), session_id=None, resume=False, executable=None) == [
        "opencode",
        "/repo",
    ]


def test_headless_backends_launch_command_replaces_binary():
    # The headless backends (shell mode / summarizer) build their command head from
    # launch_command when given, falling back to the bare binary otherwise.
    from agitrack.backends.opencode import OpenCodeBackend

    claude = ClaudeBackend("/repo", launch_command=["somewrapper", "claude"])
    assert claude.launch_command == ["somewrapper", "claude"]
    oc = OpenCodeBackend("/repo")
    assert oc.launch_command == []


def test_backend_command_config_string_and_dict(tmp_path):
    path = tmp_path / "config.json"
    # A bare string applies to whichever backend is launched (single-backend setup).
    path.write_text(json.dumps({"backend_command": "somewrapper claude"}))
    config = GlobalConfig(path)
    assert config.backend_command("claude") == ["somewrapper", "claude"]
    assert config.backend_command("opencode") == ["somewrapper", "claude"]

    # An object keyed by backend name wraps each backend differently; an unlisted
    # backend gets no wrapper.
    path.write_text(json.dumps({"backend_command": {"claude": "wrap claude", "opencode": "wrap oc -x"}}))
    config = GlobalConfig(path)
    assert config.backend_command("claude") == ["wrap", "claude"]
    assert config.backend_command("opencode") == ["wrap", "oc", "-x"]
    assert config.backend_command("other") == []

    # Unset / invalid (unbalanced quote) ⇒ launch the binary directly, never raise.
    assert GlobalConfig(tmp_path / "absent.json").backend_command("claude") == []
    path.write_text(json.dumps({"backend_command": 'wrap "unbalanced'}))
    assert GlobalConfig(path).backend_command("claude") == []


def test_make_proxy_agent_raises_on_unknown_backend():
    # An unknown/stale backend name must surface loudly, not silently launch
    # OpenCode (which contradicts the configured default).
    with pytest.raises(ValueError) as excinfo:
        make_proxy_agent("nonsense")
    assert "nonsense" in str(excinfo.value)


def test_global_config_default_backend_persists(tmp_path):
    path = tmp_path / "config.json"
    config = GlobalConfig(path)
    assert config.default_backend == "opencode"
    config.default_backend = "claude"
    assert GlobalConfig(path).default_backend == "claude"
    assert json.loads(path.read_text())["default_backend"] == "claude"


def test_timings_default_when_unset(tmp_path):
    from agitrack.config import DEFAULT_TIMINGS

    config = GlobalConfig(tmp_path / "config.json")
    assert config.timings == DEFAULT_TIMINGS
    # A fresh copy, not the module-level dict (so callers can't mutate the defaults).
    assert config.timings is not DEFAULT_TIMINGS


def test_timings_override_subset_from_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"timings": {"base_poll_seconds": 30}}))
    timings = GlobalConfig(path).timings
    assert timings["base_poll_seconds"] == 30.0  # overridden, coerced to float
    assert timings["background_poll_seconds"] == 2.0  # untouched key keeps its default


def test_timings_ignore_invalid_values(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "timings": {
                    "base_poll_seconds": -5,
                    "child_idle_seconds": "soon",
                    "file_stable_seconds": True,
                    "parse_cooldown_seconds": 0,
                }
            }
        )
    )
    timings = GlobalConfig(path).timings
    # Non-positive, wrong-type, and bool values all fall back to the defaults.
    assert timings["base_poll_seconds"] == 3.0
    assert timings["child_idle_seconds"] == 4.0
    assert timings["file_stable_seconds"] == 8.0
    assert timings["parse_cooldown_seconds"] == 10.0


def test_state_uses_default_backend_and_remembers_sessions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    state = AgitrackState(repo, default_backend="claude")
    assert state.backend == "claude"

    state.backend_session_id = "claude-session"
    state.remember_backend_session()
    state.backend = "opencode"
    state.backend_session_id = "opencode-session"
    state.remember_backend_session()

    assert state.stored_backend_session("claude") == "claude-session"
    assert state.stored_backend_session("opencode") == "opencode-session"
    # Survives a reload from disk.
    reloaded = AgitrackState(repo, default_backend="claude")
    assert reloaded.stored_backend_session("claude") == "claude-session"


def test_claude_backend_parses_json_result():
    backend = ClaudeBackend(Path("."))
    output = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "the answer",
            "session_id": "sess-xyz",
            "modelUsage": {"claude-opus-4-8": {"inputTokens": 1}},
            "usage": {
                "input_tokens": 12,
                "output_tokens": 34,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 5,
            },
        }
    )
    response, session_id, model, tokens = backend._parse_output(output)
    assert response == "the answer"
    assert session_id == "sess-xyz"
    assert model == "claude-opus-4-8"
    assert tokens.output == 34
    assert tokens.context == 12 + 100 + 5
    assert tokens.cache_read == 100


def test_claude_backend_bare_run_strips_tools_memory_and_system_prompt(monkeypatch, tmp_path):
    # A bare run (the summarizer) must add the flags that drop Claude Code's tool schemas,
    # MCP servers, project/user memory, and the large default system prompt — so the only
    # input is the caller's prompt. A normal run must NOT add them.
    import subprocess

    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return types.SimpleNamespace(
            stdout=json.dumps({"type": "result", "result": "ok", "session_id": "s"}), stderr="", returncode=0
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = ClaudeBackend(tmp_path)

    backend.run(
        "summarize this", model="claude-haiku-4-5-20251001", session_id=None, bare=True, system_prompt="BE A SUMMARIZER"
    )
    cmd = captured["command"]
    assert "--tools" in cmd and cmd[cmd.index("--tools") + 1] == ""  # all tools disabled
    assert "--strict-mcp-config" in cmd  # no MCP servers
    assert "--setting-sources" in cmd and cmd[cmd.index("--setting-sources") + 1] == ""  # no CLAUDE.md/skills
    # The caller's system prompt is used (the summarizer's instruction), replacing the
    # default agent system prompt.
    assert cmd[cmd.index("--system-prompt") + 1] == "BE A SUMMARIZER"
    # The summarizer (bare) is NOT told aGiTrack auto-commits — it isn't a coding run.
    assert "--append-system-prompt" not in cmd

    backend.run("do real work", model=None, session_id=None)  # bare defaults to False
    normal = captured["command"]
    assert "--tools" not in normal and "--system-prompt" not in normal and "--strict-mcp-config" not in normal
    # A coding run (not bare) IS told aGiTrack auto-commits, so it doesn't self-commit.
    # Shell mode runs on the repo directly (no worktree), so the worktree clause is omitted.
    from agitrack.backends.proxy_agents import agent_system_note

    assert normal[normal.index("--append-system-prompt") + 1] == agent_system_note(use_worktrees=False)

    # ...but commit_guidance=False (--no-commit-guidance) omits it on a coding run too.
    backend.run("do real work", model=None, session_id=None, commit_guidance=False)
    assert "--append-system-prompt" not in captured["command"]


def test_claude_backend_tolerates_leading_logs():
    backend = ClaudeBackend(Path("."))
    output = "starting up\n" + json.dumps({"type": "result", "result": "hi", "session_id": "s"})
    response, session_id, _model, _tokens = backend._parse_output(output)
    assert response == "hi"
    assert session_id == "s"


def test_claude_backend_picks_main_model_from_multi_model_usage():
    # Issue #24: modelUsage can contain several models for one invocation (e.g.
    # a Haiku sub-agent alongside the main model). The recorded model — which
    # also becomes --model on later runs — must be the main conversation model
    # (most output), not whichever dict key happens to come first.
    backend = ClaudeBackend(Path("."))
    output = json.dumps(
        {
            "type": "result",
            "result": "done",
            "session_id": "sess-1",
            "modelUsage": {
                "claude-haiku-4-5-20251001": {"inputTokens": 900, "outputTokens": 80},
                "claude-opus-4-8": {"inputTokens": 5000, "outputTokens": 2200},
            },
        }
    )
    _response, _session_id, model, _tokens = backend._parse_output(output)
    assert model == "claude-opus-4-8"

    # And it is not just "the larger usage dict last": reverse the ordering.
    output = json.dumps(
        {
            "type": "result",
            "result": "done",
            "session_id": "sess-1",
            "modelUsage": {
                "claude-opus-4-8": {"inputTokens": 5000, "outputTokens": 2200},
                "claude-haiku-4-5-20251001": {"inputTokens": 900, "outputTokens": 80},
            },
        }
    )
    _response, _session_id, model, _tokens = backend._parse_output(output)
    assert model == "claude-opus-4-8"


def test_claude_backend_prefers_explicit_top_level_model():
    backend = ClaudeBackend(Path("."))
    output = json.dumps(
        {
            "type": "result",
            "result": "done",
            "session_id": "sess-1",
            "model": "claude-opus-4-8",
            "modelUsage": {"claude-haiku-4-5-20251001": {"outputTokens": 999}},
        }
    )
    _response, _session_id, model, _tokens = backend._parse_output(output)
    assert model == "claude-opus-4-8"


def test_claude_backend_model_falls_back_to_total_volume():
    # No output recorded (e.g. cached/aborted turn): fall back to overall
    # token volume rather than dict order.
    backend = ClaudeBackend(Path("."))
    output = json.dumps(
        {
            "type": "result",
            "result": "done",
            "session_id": "sess-1",
            "modelUsage": {
                "claude-haiku-4-5-20251001": {"inputTokens": 10},
                "claude-opus-4-8": {"inputTokens": 800},
            },
        }
    )
    _response, _session_id, model, _tokens = backend._parse_output(output)
    assert model == "claude-opus-4-8"


def test_menu_key_defaults_and_validation(tmp_path):
    from agitrack.config import GlobalConfig

    config = GlobalConfig(tmp_path / "config.json")
    assert config.menu_key == "ctrl-g"
    assert config.menu_key_byte == b"\x07"
    assert config.menu_key_label == "Ctrl-G"
    assert config.is_shift_modified is False

    # A configured key is normalized and converted to its control byte.
    config.data["menu_key"] = "Ctrl+P"
    assert config.menu_key == "ctrl-p"
    assert config.menu_key_byte == b"\x10"
    assert config.menu_key_label == "Ctrl-P"
    assert config.is_shift_modified is False

    # Conflicting or invalid values fall back to the default, so a config
    # typo can never lock the user out of the menu.
    for bad in ("ctrl-c", "ctrl-m", "ctrl-i", "ctrl-j", "ctrl-h", "shift-g", "g", 7, None):
        config.data["menu_key"] = bad
        assert config.menu_key == "ctrl-g"


def test_menu_key_shift_modified(tmp_path):
    from agitrack.config import GlobalConfig

    config = GlobalConfig(tmp_path / "config.json")

    # Test ctrl+shift+g format
    config.data["menu_key"] = "ctrl+shift+g"
    assert config.menu_key == "ctrl+shift+g"
    assert config.is_shift_modified is True
    assert config.menu_key_byte == b""  # Empty for shift-modified keys
    assert config.menu_key_sequence == b"\x1b[103;6u"  # CSI 103 ; 6 u (g=103, modifiers=6)
    assert config.menu_key_label == "Ctrl+Shift-G"

    # Test normalization
    config.data["menu_key"] = "Ctrl+Shift+P"
    assert config.menu_key == "ctrl+shift+p"
    assert config.is_shift_modified is True
    assert config.menu_key_sequence == b"\x1b[112;6u"  # p=112
    assert config.menu_key_label == "Ctrl+Shift-P"

    # Test that plain ctrl-<letter> still works
    config.data["menu_key"] = "ctrl-y"
    assert config.menu_key == "ctrl-y"
    assert config.is_shift_modified is False
    assert config.menu_key_byte == b"\x19"
    assert config.menu_key_sequence == b"\x19"  # Same as byte for plain keys
    assert config.menu_key_label == "Ctrl-Y"


# --- use_worktrees config (#9) ----------------------------------------------


def test_use_worktrees_defaults_true(tmp_path, monkeypatch):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path))
    assert GlobalConfig().use_worktrees is True


def test_use_worktrees_config_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text('{"use_worktrees": false}')
    assert GlobalConfig().use_worktrees is False
