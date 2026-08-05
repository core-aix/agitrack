"""Skills a turn used must reach the commit metadata — on both backends.

``subagents`` and ``mcp_tools`` were showing up in real commits while ``skills`` never did, and
the reason was different on each backend:

* **Claude** — a skill invoked as ``/<skill>`` HOT-LOADS. Claude Code injects the skill's own
  text as a meta user row and never calls the ``Skill`` tool, so the capability collector (which
  reads ``tool_use`` blocks) saw nothing at all. Only a model-initiated ``Skill`` call was ever
  recorded, and typing the slash command is how a user invokes one.
* **OpenCode** — the collector never populated ``skills`` on the grounds that OpenCode had no
  skills concept. It does (1.18+): a ``skill`` tool call whose input names the skill. Once
  populated it was still dropped on the floor — ``_build_turn`` computed the list and never
  passed it to the ``SessionTurn``, so the field stayed empty on this backend either way.

Recording only the INVOKING turn then turned out to be wrong in the first place: a skill is
standing instructions, not a one-shot action, so it keeps shaping the session until the context
holding it is discarded. Worse, the commonest invocation — a bare ``/<skill>`` — edits nothing,
so that lone credited turn produced no commit and the skill reached no commit at all. Skills are
now carried on a session roster, seeded into each turn and cleared on compaction or ``/clear``.

The shapes here are taken from real transcripts on disk, not invented: Claude's injected body
opens with ``Base directory for this skill: <dir>``, and OpenCode's part is
``{"tool": "skill", "state": {"input": {"name": ...}, "metadata": {"name": ..., "dir": ...}}}``.
"""

from __future__ import annotations

import json

from agitrack.transcripts.claude import export_session_at
from agitrack.transcripts.opencode import _collect_capabilities

SKILL_BODY = (
    "Base directory for this skill: /Users/dev/.claude/skills/academic-writing-style\n"
    "\n# Academic writing style\n\nStanding preferences for academic writing.\n"
)


def _rows(*rows):
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def _user(uuid, content, **extra):
    return {"type": "user", "uuid": uuid, "message": {"role": "user", "content": content}, **extra}


def _assistant(uuid, text, parent):
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "message": {"role": "assistant", "id": "msg_1", "content": [{"type": "text", "text": text}]},
    }


# ------------------------------------------------------------------ Claude


def test_a_skill_invoked_as_a_slash_command_is_recorded(tmp_path):
    # The reported gap: the user types `/academic-writing-style`, the skill hot-loads, and the
    # commit's provenance said nothing about it.
    path = tmp_path / "s.jsonl"
    path.write_text(
        _rows(
            _user("u1", "<command-message>x</command-message>\n<command-name>/academic-writing-style</command-name>"),
            _user("u2", [{"type": "text", "text": SKILL_BODY}], isMeta=True),
            _assistant("a1", "Done.", "u2"),
        )
    )

    session = export_session_at(path)

    assert [t.skills for t in session.turns] == [["academic-writing-style"]]


def test_a_skill_slash_command_with_arguments_lands_on_the_same_turn(tmp_path):
    # With arguments the command row is itself an instruction, so it opens the turn and the
    # injected body arrives AFTER it. The skill still belongs to that turn, not to a new one.
    path = tmp_path / "s.jsonl"
    path.write_text(
        _rows(
            _user(
                "u1",
                "<command-message>x</command-message>\n<command-name>/academic-writing-style</command-name>\n"
                "<command-args>tighten the abstract and fix the captions</command-args>",
            ),
            _user("u2", [{"type": "text", "text": SKILL_BODY}], isMeta=True),
            _assistant("a1", "Done.", "u2"),
        )
    )

    session = export_session_at(path)

    assert len(session.turns) == 1  # one turn, not one for the command and one for the body
    assert session.turns[0].skills == ["academic-writing-style"]
    assert "tighten the abstract" in session.turns[0].user_prompt


def test_a_hot_loaded_skill_is_not_attributed_to_the_previous_turn(tmp_path):
    # The body row arrives while the PREVIOUS turn is still the current one, so a naive
    # "attach to whatever is open" would bill the skill to work that never used it.
    path = tmp_path / "s.jsonl"
    path.write_text(
        _rows(
            _user("u0", "first, unrelated request"),
            _assistant("a0", "ok", "u0"),
            _user("u1", "<command-message>x</command-message>\n<command-name>/academic-writing-style</command-name>"),
            _user("u2", [{"type": "text", "text": SKILL_BODY}], isMeta=True),
            _assistant("a1", "Done.", "u2"),
        )
    )

    session = export_session_at(path)

    assert [t.skills for t in session.turns] == [[], ["academic-writing-style"]]


def test_an_ordinary_slash_command_records_no_skill(tmp_path):
    # /model, /clear and friends also inject expansions. Only the skill marker counts, or every
    # built-in command would be reported as a skill.
    path = tmp_path / "s.jsonl"
    path.write_text(
        _rows(
            _user("u1", "<command-message>x</command-message>\n<command-name>/init</command-name>"),
            _user("u2", [{"type": "text", "text": "Analyze this codebase and create a CLAUDE.md."}], isMeta=True),
            _assistant("a1", "Done.", "u2"),
        )
    )

    session = export_session_at(path)

    assert [t.skills for t in session.turns] == [[]]


def test_a_plugin_skill_still_reports_its_plugin(tmp_path):
    # A plugin-supplied skill lives under the plugin's directory; the plugin field is derived
    # from the "<plugin>:<skill>" spelling, so a bare directory name yields no plugin — but the
    # skill itself must still be recorded.
    path = tmp_path / "s.jsonl"
    body = "Base directory for this skill: /Users/dev/.claude/plugins/lorepack/skills/pack-lore\n\n# Pack lore\n"
    path.write_text(
        _rows(
            _user("u1", "<command-message>x</command-message>\n<command-name>/pack-lore</command-name>"),
            _user("u2", [{"type": "text", "text": body}], isMeta=True),
            _assistant("a1", "Done.", "u2"),
        )
    )

    session = export_session_at(path)

    assert session.turns[0].skills == ["pack-lore"]


def test_a_model_invoked_skill_tool_is_still_recorded(tmp_path):
    # The path that already worked must keep working: the model calling the Skill tool itself.
    path = tmp_path / "s.jsonl"
    path.write_text(
        _rows(
            _user("u1", "use the deep-research skill"),
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "message": {
                    "role": "assistant",
                    "id": "msg_1",
                    "content": [{"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "deep-research"}}],
                },
            },
            _assistant("a2", "Done.", "a1"),
        )
    )

    session = export_session_at(path)

    assert session.turns[0].skills == ["deep-research"]


def test_a_loaded_skill_stays_in_effect_for_the_turns_that_follow(tmp_path):
    # The gap that made the whole feature look broken. A bare `/<skill>` loads standing
    # instructions and edits nothing, so its own turn never produces a commit — crediting only
    # that turn meant the skill reached NO commit at all, even though every later turn in the
    # session was written under its rules.
    path = tmp_path / "s.jsonl"
    path.write_text(
        _rows(
            _user("u1", "<command-message>x</command-message>\n<command-name>/academic-writing-style</command-name>"),
            _user("u2", [{"type": "text", "text": SKILL_BODY}], isMeta=True),
            _assistant("a1", "Loaded.", "u2"),
            _user("u3", "tighten the abstract"),
            _assistant("a2", "Done.", "u3"),
            _user("u4", "now fix the captions"),
            _assistant("a3", "Done.", "u4"),
        )
    )

    session = export_session_at(path)

    assert [t.skills for t in session.turns] == [["academic-writing-style"]] * 3


def test_a_compaction_ends_a_skills_run(tmp_path):
    # Compaction replaces the conversation with a summary, so the skill's verbatim instructions
    # are no longer in context. Past that point the transcript cannot show the skill in play, and
    # a skill that is no longer used must not keep appearing.
    path = tmp_path / "s.jsonl"
    path.write_text(
        _rows(
            _user("u1", "<command-message>x</command-message>\n<command-name>/academic-writing-style</command-name>"),
            _user("u2", [{"type": "text", "text": SKILL_BODY}], isMeta=True),
            _assistant("a1", "Loaded.", "u2"),
            _user("u3", "tighten the abstract"),
            _assistant("a2", "Done.", "u3"),
            _user("u4", "<summary>...</summary>", isCompactSummary=True),
            _user("u5", "now do something unrelated"),
            _assistant("a3", "Done.", "u5"),
        )
    )

    session = export_session_at(path)

    assert [t.skills for t in session.turns] == [["academic-writing-style"], ["academic-writing-style"], []]


def test_clear_ends_a_skills_run(tmp_path):
    # /clear wipes the context outright — the strongest possible "no longer used" signal.
    path = tmp_path / "s.jsonl"
    path.write_text(
        _rows(
            _user("u1", "<command-message>x</command-message>\n<command-name>/academic-writing-style</command-name>"),
            _user("u2", [{"type": "text", "text": SKILL_BODY}], isMeta=True),
            _assistant("a1", "Loaded.", "u2"),
            _user("u3", "<command-message>x</command-message>\n<command-name>/clear</command-name>"),
            _user("u4", "something unrelated"),
            _assistant("a2", "Done.", "u4"),
        )
    )

    session = export_session_at(path)

    assert [t.skills for t in session.turns] == [["academic-writing-style"], []]


def test_a_skill_tool_call_also_stays_in_effect(tmp_path):
    # The model invoking `Skill` loads instructions into the SAME context a `/<skill>` does, so
    # it lasts just as long — the invocation route must not change how long the skill counts for.
    path = tmp_path / "s.jsonl"
    path.write_text(
        _rows(
            _user("u1", "use the deep-research skill"),
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "message": {
                    "role": "assistant",
                    "id": "msg_1",
                    "content": [{"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "deep-research"}}],
                },
            },
            _assistant("a2", "Done.", "a1"),
            _user("u2", "keep going"),
            _assistant("a3", "Done.", "u2"),
        )
    )

    session = export_session_at(path)

    assert [t.skills for t in session.turns] == [["deep-research"], ["deep-research"]]


# ------------------------------------------------------------------ OpenCode


def _oc_user(message_id, text):
    return {"info": {"id": message_id, "role": "user"}, "parts": [{"type": "text", "text": text}]}


def _oc_assistant(message_id, parts):
    return {"info": {"id": message_id, "role": "assistant", "finish": "stop"}, "parts": parts}


_OC_SKILL_PART = {
    "type": "tool",
    "tool": "skill",
    "callID": "c1",
    "state": {"status": "completed", "input": {"name": "repo-lore"}},
}


def _oc_session(*messages):
    return {"info": {"id": "ses_1"}, "messages": list(messages)}


def test_opencode_puts_the_loaded_skill_on_the_turn():
    # `skills` was collected but never passed to the SessionTurn, so on OpenCode the field was
    # empty in every commit — the per-part collector below passed while the session parse dropped
    # the result. Asserted end-to-end so a collector-only test cannot mask it again.
    from agitrack.transcripts.opencode import parse_exported_session

    session = parse_exported_session(
        _oc_session(_oc_user("m1", "load it"), _oc_assistant("m2", [_OC_SKILL_PART, {"type": "text", "text": "ok"}]))
    )

    assert session.turns[0].skills == ["repo-lore"]


def test_opencode_skill_stays_in_effect_for_the_turns_that_follow():
    from agitrack.transcripts.opencode import parse_exported_session

    session = parse_exported_session(
        _oc_session(
            _oc_user("m1", "load it"),
            _oc_assistant("m2", [_OC_SKILL_PART, {"type": "text", "text": "ok"}]),
            _oc_user("m3", "now use it"),
            _oc_assistant("m4", [{"type": "text", "text": "done"}]),
        )
    )

    assert [t.skills for t in session.turns] == [["repo-lore"], ["repo-lore"]]


def test_opencode_compaction_ends_a_skills_run():
    # The turn the compaction happened IN still ran with the skill loaded; the ones after it did
    # not. Same rule as Claude, so a commit's provenance does not depend on the backend.
    from agitrack.transcripts.opencode import parse_exported_session

    session = parse_exported_session(
        _oc_session(
            _oc_user("m1", "load it"),
            _oc_assistant("m2", [_OC_SKILL_PART, {"type": "text", "text": "ok"}]),
            _oc_user("m3", "a long piece of work"),
            {"info": {"id": "m4", "role": "assistant", "summary": True}, "parts": []},
            _oc_assistant("m5", [{"type": "text", "text": "done"}]),
            _oc_user("m6", "something unrelated"),
            _oc_assistant("m7", [{"type": "text", "text": "done"}]),
        )
    )

    assert [t.skills for t in session.turns] == [["repo-lore"], ["repo-lore"], []]


def test_opencode_records_the_skill_a_skill_tool_call_loaded():
    part = {
        "type": "tool",
        "tool": "skill",
        "state": {
            "status": "completed",
            "input": {"name": "repo-lore"},
            "metadata": {"name": "repo-lore", "dir": "/repo/.claude/skills/repo-lore"},
        },
    }
    tool_names: list[str] = []
    subagents: list[str] = []
    skills: list[str] = []

    _collect_capabilities([part], tool_names, subagents, skills)

    assert skills == ["repo-lore"]
    assert tool_names == ["skill"]
    assert subagents == []


def test_opencode_reads_the_skill_name_before_the_call_completes():
    # A turn parsed MID-FLIGHT has the input but no metadata yet; the skill is already known.
    part = {"type": "tool", "tool": "skill", "state": {"status": "running", "input": {"name": "repo-lore"}}}
    skills: list[str] = []

    _collect_capabilities([part], [], [], skills)

    assert skills == ["repo-lore"]


def test_opencode_subagents_are_unaffected_by_the_skill_branch():
    part = {"type": "tool", "tool": "task", "state": {"input": {"subagent_type": "explorer"}}}
    subagents: list[str] = []
    skills: list[str] = []

    _collect_capabilities([part], [], subagents, skills)

    assert subagents == ["explorer"]
    assert skills == []
