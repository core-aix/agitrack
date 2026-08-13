"""The UI-bridge command surface — the protocol the VSCode extension speaks.

``shell/runner.py`` is how aGiTrack runs with no terminal: ``--json``, ``--prompt`` and
``--ui-bridge`` all drive the same loop, and the bridge is the transport the **VSCode
extension** uses for every session it opens. It was the least-covered user-facing module in
the codebase (43%), with its dispatch spine — ``_bridge_command``, ``_bridge_switch_backend``,
``_handle_command``, ``_handle_summarizer_command`` — almost entirely untested, and
``FLOW_MATRIX.md`` had no section for it at all.

A regression here breaks every extension user at once, and does it silently: the editor just
stops getting the event it was waiting for. These tests pin the command dispatch and the
event frames each command emits, including the malformed and unknown inputs an editor can
legitimately send.
"""

from __future__ import annotations

import io
import json

import pytest

import agitrack.shell.runner as shell_mod
from agitrack.git import GitRepo
from agitrack.shell import AgitrackShell
from agitrack.shell.bridge import BridgeServer, BridgeUI


class _FakeBackend:
    name = "claude"

    def __init__(self, repo, *, verbose=False, backend_args=None, launch_command=None, **kwargs):
        self.repo = repo

    def run(self, *args, **kwargs):  # pragma: no cover - these tests drive commands, not turns
        raise AssertionError("no turn should run in these tests")


def _shell(tmp_path, monkeypatch, *, backend="claude"):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-home"))
    monkeypatch.setitem(shell_mod.BACKENDS, "claude", _FakeBackend)
    monkeypatch.setattr(shell_mod, "ensure_installed_backend", lambda name, *a, **k: name)
    repo = GitRepo.init(tmp_path / "demo")
    shell = AgitrackShell(repo, backend=backend, ui_bridge=True)
    out = io.StringIO()
    shell._bridge = BridgeServer(out=out, inp=io.StringIO())
    shell.ui = BridgeUI(shell._bridge)
    shell.actions.ui = shell.ui
    return shell, repo, out


def _events(out):
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _notices(out):
    return [event["message"] for event in _events(out) if event.get("type") == "notice"]


# --- dispatch ---------------------------------------------------------------


@pytest.mark.parametrize("command", [":exit", ":quit"])
def test_exit_commands_end_the_session(tmp_path, monkeypatch, command):
    # The editor closing a session must actually end the loop; anything else leaves an
    # orphaned aGiTrack process holding the repo lock.
    shell, _, _ = _shell(tmp_path, monkeypatch)
    assert shell._bridge_command(command) is True


def test_status_reports_a_clean_tree_as_a_notice(tmp_path, monkeypatch):
    # In bridge mode nothing may be printed to stdout as free text — stdout IS the protocol
    # channel, so a stray print corrupts the stream the editor is parsing.
    shell, _, out = _shell(tmp_path, monkeypatch)

    assert shell._bridge_command(":status") is False

    assert _notices(out) == ["Working tree clean"]


def test_unstaged_reports_when_there_is_nothing_intentionally_unstaged(tmp_path, monkeypatch):
    shell, _, out = _shell(tmp_path, monkeypatch)

    shell._bridge_command(":unstaged")

    assert _notices(out) == ["No intentionally unstaged files."]


def test_unstaged_lists_the_declined_files(tmp_path, monkeypatch):
    shell, _, out = _shell(tmp_path, monkeypatch)
    shell.state.add_declined(["secret.env", "scratch.txt"])

    shell._bridge_command(":unstaged")

    message = _notices(out)[0]
    assert "secret.env" in message and "scratch.txt" in message


def test_new_session_mints_an_id_and_announces_it(tmp_path, monkeypatch):
    # The editor keys its chat view on the session id, so a new session MUST be followed by a
    # `ready` frame carrying the new one — otherwise the view stays bound to the dead session.
    shell, _, out = _shell(tmp_path, monkeypatch)
    shell.state.backend_session_id = "old-backend-session"
    before = shell.state.session_id

    shell._bridge_command(":new-session")

    assert shell.state.session_id != before
    assert shell.state.backend_session_id is None
    ready = [event for event in _events(out) if event.get("type") == "ready"]
    assert ready and ready[-1]["session"] == shell.state.session_id


def test_an_unknown_command_warns_instead_of_failing(tmp_path, monkeypatch):
    # An editor on a newer/older aGiTrack will send commands this build doesn't know. That
    # must be a warning, not an exception that takes the session down.
    shell, _, out = _shell(tmp_path, monkeypatch)

    assert shell._bridge_command(":nonsense") is False

    events = [event for event in _events(out) if event.get("type") == "notice"]
    assert events and events[-1]["level"] == "warn"
    assert ":nonsense" in events[-1]["message"]


def test_a_command_with_arguments_dispatches_on_the_verb_alone(tmp_path, monkeypatch):
    # Arguments are split off before dispatch; getting this wrong makes every parameterised
    # command ("`:summarizer off`") read as unknown.
    shell, _, out = _shell(tmp_path, monkeypatch)

    shell._bridge_command(":summarizer off")

    assert shell.state.summarization_enabled is False
    assert any("disabled" in message.lower() for message in _notices(out))


# --- backend switching ------------------------------------------------------


def test_switching_to_an_unknown_backend_warns_and_changes_nothing(tmp_path, monkeypatch):
    # Silently ignoring this would leave the editor showing a backend the session isn't using.
    shell, _, out = _shell(tmp_path, monkeypatch)
    before = shell.state.backend

    shell._bridge_switch_backend("nosuchagent")

    assert shell.state.backend == before
    warnings = [event for event in _events(out) if event.get("level") == "warn"]
    assert warnings and "nosuchagent" in warnings[-1]["message"]


def test_switching_to_an_uninstalled_backend_warns_with_an_install_hint(tmp_path, monkeypatch):
    # The user needs to know WHY the switch didn't happen and what to do about it.
    shell, _, out = _shell(tmp_path, monkeypatch)
    monkeypatch.setattr(shell_mod, "backend_installed", lambda name: False)

    shell._bridge_switch_backend("opencode")

    warnings = [event for event in _events(out) if event.get("level") == "warn"]
    assert warnings and "not installed" in warnings[-1]["message"]
    assert shell.state.backend != "opencode"


def test_switching_backend_remembers_the_outgoing_conversation(tmp_path, monkeypatch):
    # Each backend keeps its own conversation. Switching away must stash the current one so
    # switching BACK resumes it rather than starting from nothing.
    shell, _, out = _shell(tmp_path, monkeypatch)
    monkeypatch.setattr(shell_mod, "backend_installed", lambda name: True)
    shell.state.backend = "claude"
    shell.state.backend_session_id = "claude-conversation"

    shell._bridge_switch_backend("opencode")
    assert shell.state.backend == "opencode"

    shell._bridge_switch_backend("claude")
    assert shell.state.backend == "claude"
    assert shell.state.backend_session_id == "claude-conversation"


def test_switching_backend_announces_the_new_backend_to_the_editor(tmp_path, monkeypatch):
    shell, _, out = _shell(tmp_path, monkeypatch)
    monkeypatch.setattr(shell_mod, "backend_installed", lambda name: True)

    shell._bridge_switch_backend("opencode")

    ready = [event for event in _events(out) if event.get("type") == "ready"]
    assert ready and ready[-1]["backend"] == "opencode"


# --- summarizer -------------------------------------------------------------


@pytest.mark.parametrize("argument,expected", [("on", True), ("off", False), ("ON", True), ("Off", False)])
def test_summarizer_toggle_is_case_insensitive_and_persists_globally(tmp_path, monkeypatch, argument, expected):
    # Persisted to the GLOBAL config, not just the session: the per-session state is transient
    # and resets to "on" each launch, so a session-only toggle silently reverts.
    shell, _, _ = _shell(tmp_path, monkeypatch)

    shell._handle_summarizer_command(argument)

    assert shell.state.summarization_enabled is expected
    assert shell.global_config.summarization_enabled is expected


def test_an_unknown_summarizer_argument_does_not_change_the_setting(tmp_path, monkeypatch):
    shell, _, _ = _shell(tmp_path, monkeypatch)
    shell.state.summarization_enabled = True

    shell._handle_summarizer_command("maybe")

    assert shell.state.summarization_enabled is True


# --- transport robustness ---------------------------------------------------


def test_the_reader_survives_a_partial_or_malformed_frame(tmp_path):
    # Editors crash, pipes truncate. A half-written line must not poison the stream for the
    # frames that follow it.
    inp = io.StringIO('{"type":"prompt","text":"first"}\n{"type":"prom\n{"type":"prompt","text":"second"}\n')
    server = BridgeServer(out=io.StringIO(), inp=inp)
    server.start()

    assert server.next_request()["text"] == "first"
    assert server.next_request()["text"] == "second"


def test_a_frame_of_an_unknown_type_is_ignored_not_queued(tmp_path):
    # A newer editor may send frames this build has no concept of. They must be skipped, not
    # handed to the main loop as if they were requests.
    inp = io.StringIO('{"type":"telemetry","x":1}\n{"type":"prompt","text":"real"}\n')
    server = BridgeServer(out=io.StringIO(), inp=inp)
    server.start()

    assert server.next_request()["text"] == "real"


def test_closed_stdin_becomes_an_exit_so_the_loop_always_unblocks(tmp_path):
    # If the editor dies, aGiTrack must notice and shut down — not block forever on a dead
    # pipe holding the repo lock.
    server = BridgeServer(out=io.StringIO(), inp=io.StringIO(""))
    server.start()

    assert server.next_request()["type"] == "exit"
