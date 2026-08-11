from agitrack.proxy.session_names import SESSION_WORDS, random_session_name


def test_wordlist_is_clean_and_sane():
    # All lowercase single words, unique, and a healthy variety so collisions are rare.
    assert len(SESSION_WORDS) >= 200
    assert len(set(SESSION_WORDS)) == len(SESSION_WORDS)  # no duplicates
    for w in SESSION_WORDS:
        assert w.isalpha() and w.islower() and " " not in w


def test_random_session_name_avoids_taken():
    # Deterministic pick (choice = first available): the first word is normally chosen,
    # but is skipped when already taken.
    first = SESSION_WORDS[0]
    assert random_session_name(set(), choice=lambda seq: seq[0]) == first
    picked = random_session_name({first}, choice=lambda seq: seq[0])
    assert picked == SESSION_WORDS[1] and picked != first


def test_random_session_name_suffixes_when_everything_taken():
    # The (vanishingly unlikely) all-taken case still returns a unique name.
    taken = set(SESSION_WORDS)
    name = random_session_name(taken, choice=lambda seq: seq[0])
    assert name == f"{SESSION_WORDS[0]}-2"
    assert name not in taken


def _runner_with_state(tmp_path, *, name):
    """A ProxyRunner stub with just enough surface for `_persist_session_name`."""
    import subprocess

    from agitrack.config import AgitrackState
    from agitrack.proxy.runner import ProxyRunner

    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    runner = ProxyRunner.__new__(ProxyRunner)
    # `name` is a per-session field the runner delegates to its active Session.
    from agitrack.proxy.session import Session

    runner.__dict__["_active_session"] = Session()
    runner.name = name
    state = AgitrackState(root)
    runner._root_state = lambda: state
    runner._debug = lambda message: None
    return runner, state


def test_resuming_a_past_conversation_does_not_steal_its_name(tmp_path):
    """C16: `_persist_session_name` wrote self.name — the name of the session doing the
    RESUMING — onto whatever conversation was now active, so resuming renamed it:
    alpha → hill → gamma → delta. The list then showed two unrelated conversations both called
    "hill", and the original name was unrecoverable."""
    runner, state = _runner_with_state(tmp_path, name="alpha")
    runner._persist_session_name("conv-1")
    assert state.session_name_for("conv-1") == "alpha"

    # A different live session ("hill") now resumes conv-1.
    runner.name = "hill"
    runner._persist_session_name("conv-1")

    assert state.session_name_for("conv-1") == "alpha"


def test_a_conversation_with_no_name_yet_still_gets_one(tmp_path):
    """The fill-only rule must not break the case the method exists for: binding a name to a
    conversation id the moment the backend creates it."""
    runner, state = _runner_with_state(tmp_path, name="maple")

    runner._persist_session_name("conv-new")

    assert state.session_name_for("conv-new") == "maple"


def test_an_explicit_rename_does_replace_the_stored_name(tmp_path):
    """A rename is a deliberate act with its own command, and it is the ONLY caller allowed to
    overwrite."""
    runner, state = _runner_with_state(tmp_path, name="alpha")
    runner._persist_session_name("conv-1")

    runner.name = "renamed"
    runner._persist_session_name("conv-1", overwrite=True)

    assert state.session_name_for("conv-1") == "renamed"
