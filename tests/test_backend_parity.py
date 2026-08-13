"""Structural parity between the backends aGiTrack ships.

The project rule is that every feature works on EVERY registered backend (Claude Code,
Codex and OpenCode today; whatever the registry holds tomorrow). Nothing enforced that:
``ProxyAgent`` is a ``Protocol``, so a method present on one agent and missing on another is
not a type error, and the runner reaches most of them through ``getattr(..., None)`` — which
turns "this backend can't do that" into a SILENT degradation rather than a failure. Two real
drifts were live when this file was written:

* ``retarget_working_dir`` — implemented on both agents and called by the runner, but not
  declared on the Protocol at all;
* ``session_last_activity`` / the turn-end liveness signal — implemented only on Claude, so
  OpenCode ran the whole session with the runner's PTY-only fallback (see
  ``test_turn_end_detection.py`` for what that costs the user).

These tests are deliberately structural: they compare every agent against the contract rather
than against each other's current behaviour, and they enumerate ``available_backends()`` — so a
newly registered backend gets the same checks for free (Codex did).
"""

from __future__ import annotations

import inspect

import pytest

from agitrack.backends.proxy_agents import ProxyAgent, available_backends, make_proxy_agent

# Every backend, by name — so a new one is covered the moment it is registered.
BACKENDS = available_backends()

# The Protocol's own machinery, not part of the backend contract.
_PROTOCOL_INTERNALS = {"__init__", "__subclasshook__", "__class_getitem__", "__protocol_attrs__"}

# The turn-end liveness trio, which is genuinely ALTERNATIVE rather than optional-by-laziness:
# a backend supplies its signal either as a file to stat (`session_transcript_path`) or
# directly (`session_activity_mtime`), and answering both would be redundant — Claude's path is
# resolved once and cached, so routing it through the direct form would re-scan the project
# dirs on every reactor tick and make the cheap check expensive. Requiring all three of every
# agent would therefore force a WORSE implementation. So they are exempt from the
# "implement everything" rule, and `test_every_backend_offers_a_turn_end_liveness_signal`
# enforces the property that actually matters: at least one of them must be answered.
_ALTERNATIVE_LIVENESS = {"session_transcript_path", "session_activity_mtime", "session_last_activity"}


def _contract_methods() -> list[str]:
    return sorted(
        name
        for name, value in vars(ProxyAgent).items()
        if callable(value) and not name.startswith("_") and name not in _PROTOCOL_INTERNALS
    )


def _required_methods() -> list[str]:
    return [name for name in _contract_methods() if name not in _ALTERNATIVE_LIVENESS]


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_every_backend_implements_the_whole_proxy_agent_contract(backend_name):
    # The core parity check. A method the runner calls on "the backend" must exist on EVERY
    # backend, or the feature silently does nothing on the others.
    agent = make_proxy_agent(backend_name)
    missing = [name for name in _required_methods() if not callable(getattr(agent, name, None))]
    assert missing == [], f"{backend_name} is missing {missing}"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_every_backend_matches_the_contract_signatures(backend_name):
    # Same method name with different parameters is the subtler half of the same bug: the
    # runner passes `git_branch=` to one agent and gets a TypeError from the other, at
    # runtime, in the middle of a resume.
    agent = make_proxy_agent(backend_name)
    for name in _contract_methods():
        if not callable(getattr(type(agent), name, None)):
            continue  # an alternative-liveness method this backend legitimately doesn't answer
        expected = inspect.signature(getattr(ProxyAgent, name))
        actual = inspect.signature(getattr(agent, name))
        expected_params = [p for p in expected.parameters if p != "self"]
        actual_params = list(actual.parameters)
        assert actual_params == expected_params, f"{backend_name}.{name}{actual} != contract{expected}"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_every_backend_declares_its_identity_and_sharing_support(backend_name):
    agent = make_proxy_agent(backend_name)
    assert agent.name == backend_name
    assert isinstance(agent.supports_session_sharing, bool)


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_every_backend_offers_a_turn_end_liveness_signal(backend_name):
    """The one contract method that cannot be a no-op without hurting the user.

    ``_backend_idle_for`` decides "did the turn finish?" from the PTY *and* a signal that
    advances only on real backend work. A backend that answers neither
    ``session_transcript_path`` nor ``session_activity_mtime`` leaves the runner on the PTY
    alone, and the PTY is wrong in both directions — see ``test_turn_end_detection.py``.
    So: every backend must answer at least one of them.
    """
    # A Protocol's members are NOT inherited by the concrete classes (they never subclass it),
    # so "does this agent answer X?" is "is X defined on its own type?" — getattr, not
    # attribute access, which would raise for the one it doesn't implement.
    agent = make_proxy_agent(backend_name)
    has_path = getattr(type(agent), "session_transcript_path", None) is not None
    has_mtime = getattr(type(agent), "session_activity_mtime", None) is not None
    assert has_path or has_mtime, (
        f"{backend_name} overrides neither session_transcript_path nor session_activity_mtime, "
        "so the runner has no way to tell an in-progress turn from a quiet terminal"
    )


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_liveness_signals_are_safe_on_an_unknown_session(backend_name, tmp_path):
    # These are polled from the reactor for whatever id the session currently holds — which,
    # right after a switch or before the first turn, may name nothing at all. They must
    # answer None, not raise: an exception here propagates into the reactor.
    agent = make_proxy_agent(backend_name)
    for method in ("session_activity_mtime", "session_last_activity", "session_transcript_path"):
        fn = getattr(agent, method, None)
        if fn is None:
            continue  # see _ALTERNATIVE_LIVENESS
        assert fn("") is None
        assert fn("definitely-not-a-real-session-id") is None


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_new_import_id_is_distinct_per_call(backend_name):
    # "Keep both" re-imports a shared conversation alongside the local copy of the same id.
    # A backend that returned a constant would overwrite the very copy it means to preserve.
    agent = make_proxy_agent(backend_name)
    first, second = agent.new_import_id(), agent.new_import_id()
    if first is None:  # a backend that can't re-id an import must say so consistently
        assert second is None
    else:
        assert first != second


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_spawn_command_starts_with_the_backend_binary(backend_name, tmp_path):
    # The head of the command is what actually gets executed; an agent that dropped it would
    # launch the wrong program (or nothing).
    agent = make_proxy_agent(backend_name)
    command = agent.spawn_command(tmp_path, session_id=None, resume=False)
    assert command and command[0] == backend_name


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_spawn_command_honours_a_launch_wrapper(backend_name, tmp_path):
    # --backend-command lets a user run the agent under a wrapper. Every backend must honour
    # it, or the setting works on one agent and is silently ignored on the other.
    agent = make_proxy_agent(backend_name)
    command = agent.spawn_command(tmp_path, session_id=None, resume=False, executable=["wrap", backend_name])
    assert command[:2] == ["wrap", backend_name]


def test_unknown_backend_raises_rather_than_substituting_one():
    # A stale or mistyped backend name must surface. Silently launching a different agent
    # would resume the wrong conversation.
    with pytest.raises(ValueError) as excinfo:
        make_proxy_agent("nosuchbackend")
    assert "nosuchbackend" in str(excinfo.value)
    for name in BACKENDS:
        assert name in str(excinfo.value)  # the message lists the real choices


def test_retarget_working_dir_is_part_of_the_declared_contract():
    # The specific drift this file was written for: the runner calls this on every backend
    # from _stage_backend_resume, both agents implement it, and it was declared nowhere.
    assert "retarget_working_dir" in _required_methods()
    for name in BACKENDS:
        signature = inspect.signature(getattr(make_proxy_agent(name), "retarget_working_dir"))
        assert "git_branch" in signature.parameters
