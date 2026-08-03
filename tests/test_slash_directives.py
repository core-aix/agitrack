"""Slash commands that carry a user INSTRUCTION must reach the interaction trace.

Claude Code records a typed slash command as a synthetic user row of ``<command-name>`` /
``<command-args>`` artifacts rather than a normal prompt, so the parser rightly drops it as a
prompt. But some commands' arguments ARE the request — ``/goal <what to achieve>``,
``/loop <what to repeat>``, a skill invoked with instructions — and the agent then works on
them for the rest of the session. Those were being dropped entirely: no ``isMeta`` expansion
row exists for them, so the invocation was remembered, never consumed, and silently
discarded. A commit produced by a paragraph-long ``/goal`` recorded no prompt at all.

The rule is deliberately BEHAVIOURAL rather than a list of command names — Claude Code keeps
adding commands and users define their own skills, so any fixed list is out of date the moment
it ships:

    a slash command that carries arguments AND is followed by the agent doing work
    is a user instruction.

The tests below pin both halves, and the exclusions that fall out of them.
"""

from __future__ import annotations

import json

from agitrack.transcripts.claude import export_session_at


def _row(**kwargs):
    row = {"uuid": kwargs.pop("uuid", "u-1"), "timestamp": kwargs.pop("timestamp", "2026-01-01T00:00:00.000Z")}
    row.update(kwargs)
    return row


def _user(text, **kwargs):
    return _row(type="user", message={"role": "user", "content": text}, **kwargs)


def _assistant(text="Working on it.", *, stop_reason="end_turn", **kwargs):
    return _row(
        type="assistant",
        message={
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
        **kwargs,
    )


def _command(name, args=""):
    body = f"<command-name>{name}</command-name>\n<command-message>x</command-message>\n<command-args>{args}</command-args>"
    return _user(body)


def _session(tmp_path, rows):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return export_session_at(path)


# --- the rule ---------------------------------------------------------------


def test_a_command_with_args_followed_by_work_is_recorded_with_its_instruction(tmp_path):
    # The core case. The args are what the user asked for, so they must be the prompt — a bare
    # "/goal" would say something was asked without saying what.
    session = _session(
        tmp_path,
        [_command("/goal", "Make the paper publishable and write an experiment plan."), _assistant()],
    )

    assert len(session.turns) == 1
    assert session.turns[0].user_prompt == "/goal Make the paper publishable and write an experiment plan."


def test_the_rule_is_not_a_list_of_known_command_names(tmp_path):
    # The point of being behavioural: a command nobody has heard of — a future built-in, or a
    # user-defined skill — is captured on exactly the same evidence, with no code change.
    session = _session(
        tmp_path,
        [_command("/some-brand-new-command", "Refactor the parser and add tests."), _assistant()],
    )

    assert len(session.turns) == 1
    assert session.turns[0].user_prompt == "/some-brand-new-command Refactor the parser and add tests."


def test_multiline_arguments_are_preserved(tmp_path):
    # /goal and /loop arguments are routinely paragraphs; truncating at the first line would
    # drop most of the requirement.
    instruction = "First do A.\nThen do B.\nFinally verify C."
    session = _session(tmp_path, [_command("/goal", instruction), _assistant()])

    assert session.turns[0].user_prompt == f"/goal {instruction}"


def test_a_command_with_no_arguments_is_not_a_prompt(tmp_path):
    # Half the rule: no args, no instruction. `/clear` and friends must not open turns.
    session = _session(tmp_path, [_command("/clear"), _assistant()])

    assert [t.user_prompt for t in session.turns] == []


def test_a_command_the_agent_never_answers_is_not_a_prompt(tmp_path):
    # The other half: `/model sonnet` carries args but is handled locally and gets no assistant
    # response. Recording it would put configuration noise in every later commit's trace.
    session = _session(tmp_path, [_command("/model", "sonnet"), _user("Now refactor the parser."), _assistant()])

    assert [t.user_prompt for t in session.turns] == ["Now refactor the parser."]


def test_a_real_prompt_supersedes_an_unanswered_directive(tmp_path):
    # A directive the agent never acted on must not attach itself to the NEXT prompt's work.
    session = _session(
        tmp_path,
        [_command("/status", "verbose"), _user("Fix the failing test."), _assistant()],
    )

    assert [t.user_prompt for t in session.turns] == ["Fix the failing test."]


def test_a_directive_can_open_the_very_first_turn_of_a_conversation(tmp_path):
    # A session that STARTS with /goal has no open turn for the work to attach to. Before this
    # widened the assistant branch's guard, that work was dropped on the floor.
    session = _session(tmp_path, [_command("/goal", "Ship the release."), _assistant("On it.")])

    assert len(session.turns) == 1
    assert session.turns[0].user_prompt == "/goal Ship the release."


def test_a_directive_mid_conversation_opens_its_own_turn(tmp_path):
    # It must not be folded into the previous, already-committed turn — that would overwrite
    # that turn's assistant id and break the commit watermark.
    session = _session(
        tmp_path,
        [
            _user("Add a helper.", uuid="u-1"),
            _assistant("Added.", uuid="a-1"),
            _command("/goal", "Now make every test pass."),
            _assistant("Running the suite.", uuid="a-2"),
        ],
    )

    assert [t.user_prompt for t in session.turns] == ["Add a helper.", "/goal Now make every test pass."]


def test_an_expanding_command_keeps_its_expansion_behaviour(tmp_path):
    # /init injects its body as an isMeta row and has no args. That path predates this change
    # and must be untouched: the turn still opens, labelled with the command name.
    session = _session(
        tmp_path,
        [
            _command("/init"),
            _row(
                type="user",
                isMeta=True,
                message={"role": "user", "content": "Analyze this codebase and write CLAUDE.md."},
            ),
            _assistant(),
        ],
    )

    assert [t.user_prompt for t in session.turns] == ["/init"]


def test_an_expanding_command_with_args_prefers_the_instruction(tmp_path):
    # When BOTH signals exist, the args win: same turn either way, but the args say what was
    # actually asked. This is the shape /goal has in a real transcript — Claude Code follows the
    # invocation with a local-stdout row and then an isMeta row describing the hook it installed.
    session = _session(
        tmp_path,
        [
            _command("/goal", "Make all the tests pass."),
            _user("<local-command-stdout>Goal set: Make all the tests pass.</local-command-stdout>"),
            _row(type="user", isMeta=True, message={"role": "user", "content": "A Stop hook is now active."}),
            _assistant(),
        ],
    )

    assert [t.user_prompt for t in session.turns] == ["/goal Make all the tests pass."]


def test_compact_is_still_never_a_turn(tmp_path):
    # /compact drives a compaction and never receives a reply; an unanswered "/compact" turn
    # would ride every later commit's trace. Its existing exclusion must survive.
    session = _session(tmp_path, [_user("/compact focus on the parser"), _assistant()])

    assert [t.user_prompt for t in session.turns] == []


def test_directive_turns_carry_their_tokens_and_model(tmp_path):
    # A directive turn is a real turn: it must be billed and attributed like any other, or the
    # per-turn accounting the commits carry is wrong.
    session = _session(tmp_path, [_command("/goal", "Ship it."), _assistant("Done.")])

    turn = session.turns[0]
    assert turn.tokens.output == 5
    assert turn.tokens.input == 10
    assert session.model == "claude-opus-5"
    assert turn.final_response == "Done."
