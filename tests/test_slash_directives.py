"""Slash commands that carry a user INSTRUCTION must reach the interaction trace.

Claude Code records a typed slash command as a synthetic user row of ``<command-name>`` /
``<command-args>`` artifacts rather than a normal prompt, so the parser rightly drops it as a
prompt. But some commands' arguments ARE the request — ``/goal <what to achieve>``,
``/loop <what to repeat>``, a skill invoked with instructions — and the agent then works on
them for the rest of the session. Those were being dropped entirely: no ``isMeta`` expansion
row exists for them, so the invocation was remembered, never consumed, and silently
discarded. A commit produced by a paragraph-long ``/goal`` recorded no prompt at all.

The rule is decided from the ARGUMENTS rather than a list of command names — Claude Code keeps
adding commands and users define their own skills, so any fixed list is out of date the moment
it ships:

    a slash command whose arguments are PROSE (more than one word) carries a user
    instruction, and belongs in the trace exactly like a typed prompt.

Crucially it does NOT also require the agent to have replied. A `/goal` typed while the agent is
mid-tool-call, or as the last row of a transcript, has no reply to wait for yet — and those are
the ordinary ways these commands get used (steering work already in progress). Requiring a reply
dropped exactly those.

Something must still exclude configuration, because an unanswered turn is not free: it stays
incomplete and defers commits (the same reason `/compact` is excluded). One bare token —
`/model sonnet`, `/goal clear` — is a parameter or a control word, not a request. Prose is.
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


def test_single_token_arguments_are_configuration_not_an_instruction(tmp_path):
    # `/model sonnet` carries args, but "sonnet" is a parameter — nobody asked the agent to do
    # anything. Recording it would open a turn that never completes, and an incomplete turn
    # defers commits (the same reason /compact is excluded).
    session = _session(tmp_path, [_command("/model", "sonnet"), _user("Now refactor the parser."), _assistant()])

    assert [t.user_prompt for t in session.turns] == ["Now refactor the parser."]


def test_a_control_word_argument_is_not_an_instruction(tmp_path):
    # `/goal clear` turns the goal OFF. Same command as the instruction case, and still not a
    # request — which is why the test is on the arguments, not on the command name.
    session = _session(tmp_path, [_command("/goal", "clear"), _user("Carry on."), _assistant()])

    assert [t.user_prompt for t in session.turns] == ["Carry on."]


def test_an_instruction_is_recorded_even_when_no_reply_has_arrived_yet(tmp_path):
    # The case that requiring a reply used to drop: the user types /goal and the transcript ends
    # there (the agent has not answered yet). The instruction is still the user's request, so it
    # must be in the trace — as a turn in flight, which completes when the reply lands.
    session = _session(tmp_path, [_command("/goal", "Make every test pass.")])

    assert [t.user_prompt for t in session.turns] == ["/goal Make every test pass."]
    assert session.turns[0].complete is False


def test_an_instruction_typed_mid_turn_is_recorded(tmp_path):
    # How these commands are most often used: steering the agent while it is already working, so
    # the current turn is mid-tool-call. Requiring a reply lost this one silently.
    session = _session(
        tmp_path,
        [
            _user("Start the refactor.", uuid="u-1"),
            _assistant("Working.", stop_reason="tool_use", uuid="a-1"),
            _command("/goal", "Also make sure every test passes."),
            _assistant("Understood.", uuid="a-2"),
        ],
    )

    assert [t.user_prompt for t in session.turns] == [
        "Start the refactor.",
        "/goal Also make sure every test passes.",
    ]


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


def test_an_instruction_at_the_start_of_a_turn_is_recorded(tmp_path):
    """The other ordinary way these are typed: the agent is IDLE and the user opens the next
    turn with `/goal …` rather than a plain prompt.

    Uses the real row shape — Claude Code follows the invocation with a `<local-command-stdout>`
    row and an `isMeta` row describing the hook it installed, and only then does the agent work.
    Neither of those may swallow the instruction or add a second turn for the same request.
    """
    session = _session(
        tmp_path,
        [
            _user("Add a helper.", uuid="u-1"),
            _assistant("Added.", uuid="a-1"),
            _command("/goal", "Now keep the whole suite green."),
            _user("<local-command-stdout>Goal set: Now keep the whole suite green.</local-command-stdout>"),
            _row(type="user", isMeta=True, message={"role": "user", "content": "A Stop hook is now active."}),
            _assistant("Running the suite.", uuid="a-2"),
        ],
    )

    assert [t.user_prompt for t in session.turns] == [
        "Add a helper.",
        "/goal Now keep the whole suite green.",
    ]
    assert session.turns[-1].final_response == "Running the suite."


def test_an_instruction_opening_a_conversation_uses_the_real_row_shape(tmp_path):
    # Same, but as the very first thing in the conversation — no previous turn to attach to.
    session = _session(
        tmp_path,
        [
            _command("/loop", "Re-run the failing test until it passes."),
            _user("<local-command-stdout>Loop armed.</local-command-stdout>"),
            _row(type="user", isMeta=True, message={"role": "user", "content": "A loop is now active."}),
            _assistant("Starting."),
        ],
    )

    assert [t.user_prompt for t in session.turns] == ["/loop Re-run the failing test until it passes."]
