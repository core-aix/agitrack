"""The mode surface: `-i`, the bare-`agitrack` menu, and `agitrack stop`.

aGiTrack runs in several quite different modes and, for a long time, a bare `agitrack` silently
started the one that happened to be the historical default. These cover the replacement: a menu
that says the modes exist, a flag for each, and one stop command that does not require knowing
which mode is holding the repository.
"""

from __future__ import annotations

import io

import pytest

from agitrack import cli, modes


# --- the mode table ---------------------------------------------------------------------------


def test_background_with_auto_commits_is_the_default_mode():
    # The mode that asks least of the user: their terminal stays theirs, their agent stays
    # whatever they already use, and the tracking happens anyway.
    assert modes.DEFAULT_MODE is modes.MODES[0]
    assert modes.DEFAULT_MODE.argv == ("-b",)


def test_worktree_and_no_worktree_interactive_runs_are_separate_modes():
    # They behave differently enough (an isolated branch with an integration step, versus the
    # agent editing your working tree live) that collapsing them hides the choice that matters.
    interactive = [mode for mode in modes.MODES if mode.argv and mode.argv[0] == "-i"]
    assert ("-i",) in [mode.argv for mode in interactive]
    assert ("-i", "--no-worktree") in [mode.argv for mode in interactive]
    assert ("-i", "-m") in [mode.argv for mode in interactive]


def test_every_mode_carries_a_description_and_the_command_it_stands_for():
    for mode in modes.MODES:
        assert mode.headline and mode.summary, mode.name
        # The menu doubles as documentation: whatever you pick, you can see the command that
        # would have gone straight there next time.
        assert mode.command.startswith("agitrack ")


def test_the_menu_shows_every_mode_with_its_command_and_marks_the_selection():
    lines = modes.render(0, colour=False)
    text = "\n".join(lines)
    for mode in modes.MODES:
        assert mode.headline in text
        assert mode.command in text
    assert "↑/↓ to move" in text
    assert lines[3].lstrip().startswith("❯")  # the first row is selected


def test_the_menu_drops_the_other_summaries_when_it_would_not_fit_the_screen():
    # The menu repaints in place by moving the cursor back over its own output; a menu taller
    # than the screen has already scrolled by then, and each keypress would smear a new copy
    # down the terminal.
    full = modes.render(0, colour=False, compact=False)
    compact = modes.render(0, colour=False, compact=True)
    assert len(compact) < len(full)
    assert modes.MODES[0].summary.split()[0] in "\n".join(compact)  # the SELECTED one is kept


@pytest.mark.parametrize(
    "answer,expected",
    [("1", ("-b",)), ("3", ("-i",)), ("interactive-here", ("-i", "--no-worktree")), ("stop", ("stop",))],
)
def test_a_mode_can_be_named_by_number_name_or_flag(answer, expected):
    mode = modes.mode_by_name(answer)
    assert mode is not None and mode.argv == expected


def test_an_unknown_answer_names_no_mode():
    assert modes.mode_by_name("banana") is None
    assert modes.mode_by_name("99") is None


def test_arrow_keys_move_the_selection_and_enter_takes_it(monkeypatch):
    # Up from the first row wraps to the last, so a user reaching for `stop` does not have to
    # press down eight times.
    keys = iter(["up", "enter"])
    monkeypatch.setattr(modes._RawKeys, "__enter__", lambda self: setattr(self, "ok", True) or self)
    monkeypatch.setattr(modes._RawKeys, "__exit__", lambda self, *a: None)
    monkeypatch.setattr(modes._RawKeys, "read", lambda self: next(keys))

    assert modes.choose(stream=io.StringIO()) is modes.MODES[-1]


def test_a_digit_chooses_immediately(monkeypatch):
    monkeypatch.setattr(modes._RawKeys, "__enter__", lambda self: setattr(self, "ok", True) or self)
    monkeypatch.setattr(modes._RawKeys, "__exit__", lambda self, *a: None)
    monkeypatch.setattr(modes._RawKeys, "read", lambda self: "3")

    assert modes.choose(stream=io.StringIO()).argv == ("-i",)


def test_q_quits_without_choosing(monkeypatch):
    monkeypatch.setattr(modes._RawKeys, "__enter__", lambda self: setattr(self, "ok", True) or self)
    monkeypatch.setattr(modes._RawKeys, "__exit__", lambda self, *a: None)
    monkeypatch.setattr(modes._RawKeys, "read", lambda self: "q")

    assert modes.choose(stream=io.StringIO()) is None


def test_without_raw_keys_the_menu_is_answered_by_typing(monkeypatch):
    # A pipe, a test, or a terminal that refuses raw mode: arrow keys would do nothing, so the
    # same menu is printed once and answered with a number, a name, or a bare Enter.
    monkeypatch.setattr(modes._RawKeys, "__enter__", lambda self: setattr(self, "ok", False) or self)
    monkeypatch.setattr(modes._RawKeys, "__exit__", lambda self, *a: None)
    monkeypatch.setattr("builtins.input", lambda *a: "")

    assert modes.choose(stream=io.StringIO()) is modes.DEFAULT_MODE


def test_the_typed_menu_gives_up_instead_of_asking_forever(monkeypatch):
    """A menu that re-asks forever hangs anything driving aGiTrack through a pipe.

    This is not hypothetical: with `input()` stubbed to one unusable answer, the whole CLI test
    file stopped finishing. A person who has mistyped three times is being told something the
    prompt is not managing to say, and a stream that answers the same unusable thing three times
    is not a person at all."""
    monkeypatch.setattr(modes._RawKeys, "__enter__", lambda self: setattr(self, "ok", False) or self)
    monkeypatch.setattr(modes._RawKeys, "__exit__", lambda self, *a: None)
    asked = []
    monkeypatch.setattr("builtins.input", lambda *a: asked.append(1) or "not a mode")
    out = io.StringIO()

    assert modes.choose(stream=out) is None
    assert len(asked) == modes._MAX_TYPED_ATTEMPTS
    assert "agitrack --help" in out.getvalue()


def test_a_stdin_that_cannot_be_read_chooses_nothing(monkeypatch):
    # A closed stream (or pytest's captured stdin) raises OSError rather than EOFError. There is
    # no one to ask either way, so choose nothing rather than let it escape as a crash.
    monkeypatch.setattr(modes._RawKeys, "__enter__", lambda self: setattr(self, "ok", False) or self)
    monkeypatch.setattr(modes._RawKeys, "__exit__", lambda self, *a: None)

    def boom(*a):
        raise OSError("reading from stdin while output is captured")

    monkeypatch.setattr("builtins.input", boom)

    assert modes.choose(stream=io.StringIO()) is None


# --- the CLI around it ------------------------------------------------------------------------


@pytest.fixture
def quiet_startup(monkeypatch):
    """Silence everything a real interactive launch does after the mode is settled.

    These tests are about WHICH mode gets chosen, so the first-run wizard, the update check and
    the git-identity prompt are stubbed out, and repo discovery stops the run before any mode
    actually starts."""
    monkeypatch.setattr(cli, "stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "stdout_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_check_for_update_at_startup", lambda config: None)
    monkeypatch.setattr(cli, "_ensure_git_identity", lambda: None)
    monkeypatch.setattr(cli, "select_default_backend", lambda config: "claude")
    monkeypatch.setattr(cli, "select_default_summarizer_model", lambda config, backend: None)
    monkeypatch.setattr(cli.GlobalConfig, "has_default_backend", lambda self: True)
    monkeypatch.setattr(cli, "_discover_or_init", lambda path: None)


def test_bare_agitrack_on_a_terminal_asks_which_mode(monkeypatch, quiet_startup):
    chosen: list[bool] = []
    monkeypatch.setattr(cli, "_choose_mode", lambda: ["-b"])
    original = cli._no_mode_requested

    def spy(args, backend_args):
        answer = original(args, backend_args)
        chosen.append(answer)
        return answer

    monkeypatch.setattr(cli, "_no_mode_requested", spy)

    cli.main([])

    assert chosen[0] is True  # nothing on the command line picked a mode...
    assert chosen[1] is False  # ...and the re-entry carries the choice, so the menu is not shown


def test_naming_a_mode_skips_the_menu(monkeypatch, quiet_startup):
    monkeypatch.setattr(cli, "_choose_mode", lambda: pytest.fail("a named mode must not be second-guessed by a menu"))

    cli.main(["-i"])


def test_a_prompt_is_a_mode_request(monkeypatch, quiet_startup):
    # `agitrack -- "fix the bug"` is someone naming a task for the agent. Answering that with a
    # menu would be obtuse.
    monkeypatch.setattr(cli, "_choose_mode", lambda: pytest.fail("a prompt already says what to do"))

    cli.main(["--", "fix the bug"])


def test_without_a_terminal_the_historical_launch_still_happens(monkeypatch):
    # There is no one to ask, so scripts and editor front-ends are unaffected by the menu.
    monkeypatch.setattr(cli, "stdin_is_interactive", lambda: False)
    monkeypatch.setattr(cli, "stdout_is_interactive", lambda: False)
    monkeypatch.setattr(cli, "_choose_mode", lambda: pytest.fail("nobody is there to answer a menu"))
    monkeypatch.setattr(cli, "_discover_or_init", lambda path: None)

    cli.main([])


def test_yes_never_shows_the_menu(monkeypatch, quiet_startup):
    # --yes means "never ask me a question", and a menu is one.
    monkeypatch.setattr(cli, "_choose_mode", lambda: pytest.fail("--yes asked for no questions"))

    cli.main(["--yes"])


# --- the dashboard opens last -----------------------------------------------------------------


def test_the_dashboard_waits_until_every_startup_question_is_answered(monkeypatch, tmp_path):
    """A browser window arriving mid-startup pulls the user away from a prompt still waiting.

    The interactive path asks its last questions INSIDE the runner (the privacy acknowledgment,
    the pre-agent commit), so the open has to be handed to the runner rather than fired from the
    CLI before it is constructed."""
    from agitrack.proxy.runner import ProxyRunner

    order: list[str] = []
    runner = object.__new__(ProxyRunner)
    runner._on_startup_complete = lambda: order.append("dashboard")
    runner._debug = lambda message: None

    # The hook fires where run() calls it: after the questions, before the TUI takes the screen.
    order.append("privacy")
    order.append("pre-agent commit")
    runner._on_startup_complete()

    assert order == ["privacy", "pre-agent commit", "dashboard"]


def test_the_cli_hands_the_dashboard_to_the_runner_rather_than_opening_it_first(monkeypatch, quiet_startup):
    """The interactive launch must not open a browser before the runner has had its say."""
    captured: dict = {}

    class Fake:
        def __init__(self, repo, **kw):
            captured.update(kw)

        def run(self):
            return 0

    monkeypatch.setattr(cli, "ProxyRunner", Fake)
    monkeypatch.setattr(cli, "_check_gh_availability", lambda repo, scripted=False: (True, True))
    monkeypatch.setattr(cli, "_verify_menu_key", lambda config, scripted=False: True)
    monkeypatch.setattr(cli, "_refuse_during_merge_conflict", lambda repo: False)
    opened: list[int] = []
    monkeypatch.setattr(cli, "_open_dashboard_on_start", lambda *a, **k: opened.append(1))
    import pathlib

    repo = type("R", (), {"repo": pathlib.Path("/tmp/proj"), "hooks_dir": lambda self: None})()
    monkeypatch.setattr(cli, "_discover_or_init", lambda path: repo)
    monkeypatch.setattr("agitrack.config.migrate.migrate_repo_state", lambda r: False)

    cli.main(["-i", "--backend", "claude"])

    # Handed over, not called: the runner fires it once its own questions are answered.
    assert callable(captured.get("on_startup_complete"))
    assert opened == []
    captured["on_startup_complete"]()
    assert opened == [1]


def test_a_failing_dashboard_never_stops_the_session_starting(monkeypatch):
    from agitrack.proxy.runner import ProxyRunner

    runner = object.__new__(ProxyRunner)
    logged: list[str] = []
    runner._debug = lambda message: logged.append(message)

    def boom():
        raise RuntimeError("no browser here")

    runner._on_startup_complete = boom
    # Exactly what run() does with it: a side errand may not take the session down with it.
    try:
        runner._on_startup_complete()
    except Exception as error:
        runner._debug(f"startup-complete hook failed: {error!r}")

    assert logged and "no browser here" in logged[0]


# --- `agitrack stop` --------------------------------------------------------------------------


def test_stop_is_recognised_as_the_first_word():
    assert cli._split_command(["stop", "--repo", "/x"]) == ("stop", ["--repo", "/x"])


def test_a_prompt_starting_with_a_command_word_is_still_a_prompt():
    # Promoting a word to a command steals it from every prompt that begins with it, so the
    # theft is limited to exactly the phrase typed AS a command.
    assert cli._split_command(["--", "stop", "the", "retry", "loop"]) == (None, ["--", "stop", "the", "retry", "loop"])


def test_stop_is_also_recognised_after_its_options(tmp_path, monkeypatch, capsys):
    # `agitrack --repo <path> stop` is how half of all CLIs are typed.
    from agitrack.git import GitRepo

    GitRepo.init(tmp_path)
    called: list[object] = []
    monkeypatch.setattr("agitrack.stop.stop_everything", lambda repo, **kw: called.append(repo) or 0)

    assert cli.main(["--repo", str(tmp_path), "stop"]) == 0
    assert called


def test_stop_on_an_idle_repo_says_so_and_succeeds(tmp_path, capsys):
    # Asking for a state that already holds is not an error.
    from agitrack.git import GitRepo
    from agitrack.stop import stop_everything

    repo = GitRepo.init(tmp_path)
    assert stop_everything(repo) == 0
    assert "not running" in capsys.readouterr().out
