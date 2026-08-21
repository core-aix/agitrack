"""Tests for recovering file edits from an agent's SHELL tool calls.

An agent edits through the shell as much as through its editing tools — Claude Code's auto
mode explicitly instructs it to — and until :mod:`agitrack.transcripts.shell_edits` existed the
backtrace could not see any of it. Measured against this repository's own tracked commits, one
session reconstructed 31.5% of the lines the commits recorded; with these idioms read it
reconstructs ~99% per commit.

The recovery must be exact or absent, never approximate: ``file_state`` is the running
reconstruction every later diff is taken against, so a wrong recovery corrupts everything that
follows. Roughly half of these tests are therefore about what is DECLINED.
"""

from __future__ import annotations

import json

from agitrack.transcripts.shell_edits import edits_from_shell

REPO = "/repo"


def _run(command, state=None, cwd=REPO):
    state = {} if state is None else state
    return edits_from_shell(state, command, cwd=cwd), state


def _lines(edits):
    return sum(e.insertions for e in edits), sum(e.deletions for e in edits)


# --------------------------------------------------------------------------- heredocs


def test_cat_heredoc_writes_the_whole_file():
    edits, state = _run("cat > notes.md <<'EOF'\nalpha\nbeta\nEOF\n")
    assert [e.path for e in edits] == ["/repo/notes.md"]
    assert _lines(edits) == (2, 0)
    assert state["/repo/notes.md"] == "alpha\nbeta\n"
    assert "new file mode" in edits[0].patch  # a file first seen is an addition, not a rewrite


def test_heredoc_after_the_delimiter_still_finds_its_target():
    # `cat <<'EOF' > path` is as common as `cat > path <<'EOF'`, and reading only the first
    # ordering silently recovered nothing from the other.
    edits, state = _run("cat <<'EOF' > notes.md\nalpha\nEOF\n")
    assert state["/repo/notes.md"] == "alpha\n"
    assert _lines(edits) == (1, 0)


def test_append_heredoc_adds_to_what_the_session_already_wrote():
    _, state = _run("cat > a.py <<'EOF'\none\nEOF\n")
    edits, state = _run("cat >> a.py <<'EOF'\ntwo\nEOF\n", state)
    assert state["/repo/a.py"] == "one\ntwo\n"
    assert _lines(edits) == (1, 0)  # the incremental change, not the whole file again


def test_tee_is_read_like_a_redirect_including_append():
    _, state = _run("cat <<'EOF' | tee a.txt\none\nEOF\n")
    assert state["/repo/a.txt"] == "one\n"
    _, state = _run("cat <<'EOF' | tee -a a.txt\ntwo\nEOF\n", state)
    assert state["/repo/a.txt"] == "one\ntwo\n"


def test_an_unterminated_heredoc_does_not_leak_its_body_into_the_command_stream():
    # A truncated transcript ends mid-body. Resyncing would read the body's own lines as shell
    # commands — and a body is usually a script full of things that look like one.
    edits, _ = _run("cat > a.txt <<'EOF'\nrm -rf b.txt\necho hi > c.txt\n")
    assert [e.path for e in edits] == ["/repo/a.txt"]


def test_a_heredoc_feeding_a_command_that_writes_nothing_is_ignored():
    edits, state = _run("grep -c foo <<'EOF'\nfoo\nEOF\n")
    assert edits == [] and state == {}


# --------------------------------------------------------------------------- inline Python


def test_inline_python_replace_is_read_as_an_edit():
    _, state = _run("cat > m.py <<'EOF'\nold line\nkeep\nEOF\n")
    edits, state = _run(
        "python3 - <<'PY'\nfrom pathlib import Path\np = Path(\"m.py\")\nt = p.read_text()\n"
        't = t.replace("old line", "new line")\np.write_text(t)\nPY\n',
        state,
    )
    assert state["/repo/m.py"] == "new line\nkeep\n"
    assert _lines(edits) == (1, 1)


def test_one_script_patching_two_files_recovers_both():
    # The shape that motivated the statement-ordered analysis: agents routinely change the code
    # and the note about it in a single script, and a flat scan cannot tell whose replacement is
    # whose — so it used to decline both.
    _, state = _run("cat > code.py <<'EOF'\naaa\nEOF\ncat > NOTES.md <<'EOF'\nbbb\nEOF\n")
    edits, state = _run(
        "python3 - <<'PY'\nfrom pathlib import Path\n"
        'p = Path("code.py")\nt = p.read_text()\nt = t.replace("aaa", "AAA")\np.write_text(t)\n'
        'd = Path("NOTES.md")\nu = d.read_text()\nu = u.replace("bbb", "BBB")\nd.write_text(u)\nPY\n',
        state,
    )
    assert sorted(e.path for e in edits) == ["/repo/NOTES.md", "/repo/code.py"]
    assert state["/repo/code.py"] == "AAA\n" and state["/repo/NOTES.md"] == "BBB\n"


def test_concatenated_replacement_is_folded():
    # `t.replace(anchor, new + anchor)` — inserting before an anchor — is the single most common
    # way an agent adds a section, and reading only bare literals missed every one of them.
    _, state = _run("cat > n.md <<'EOF'\n## Anchor\nEOF\n")
    edits, state = _run(
        "python3 - <<'PY'\nfrom pathlib import Path\np = Path(\"n.md\")\nt = p.read_text()\n"
        'anchor = "## Anchor"\nnew = "## Added\\n"\np.write_text(t.replace(anchor, new + anchor))\nPY\n',
        state,
    )
    assert state["/repo/n.md"] == "## Added\n## Anchor\n"
    assert _lines(edits) == (1, 0)


def test_write_text_of_a_literal_is_a_whole_file_write():
    edits, state = _run('python3 - <<\'PY\'\nfrom pathlib import Path\nPath("x.txt").write_text("a\\nb\\n")\nPY\n')
    assert state["/repo/x.txt"] == "a\nb\n" and _lines(edits) == (2, 0)


def test_python_dash_c_is_read_like_a_heredoc():
    edits, state = _run('python3 -c \'from pathlib import Path; Path("y.txt").write_text("hi\\n")\'')
    assert state["/repo/y.txt"] == "hi\n" and _lines(edits) == (1, 0)


def test_a_computed_replacement_is_declined_rather_than_guessed():
    _, state = _run("cat > m.py <<'EOF'\naaa\nEOF\n")
    edits, state = _run(
        "python3 - <<'PY'\nfrom pathlib import Path\nimport sys\np = Path(\"m.py\")\n"
        't = p.read_text()\nt = t.replace("aaa", sys.argv[1])\np.write_text(t)\nPY\n',
        state,
    )
    assert edits == []
    assert state["/repo/m.py"] == "aaa\n"  # the baseline is left as it was, never half-applied


def test_an_unmodelled_transform_declines_the_whole_script():
    # A `re.sub` this cannot reproduce means the file on disk is not what the recovered
    # replacements would produce — so applying the others would seed a wrong baseline.
    _, state = _run("cat > m.py <<'EOF'\naaa\nEOF\n")
    edits, state = _run(
        "python3 - <<'PY'\nimport re\nfrom pathlib import Path\np = Path(\"m.py\")\n"
        't = p.read_text()\nt = t.replace("aaa", "bbb")\nt = re.sub(r"x+", "y", t)\np.write_text(t)\nPY\n',
        state,
    )
    assert edits == [] and state["/repo/m.py"] == "aaa\n"


def test_a_write_whose_content_came_from_elsewhere_is_declined():
    _, state = _run("cat > m.py <<'EOF'\naaa\nEOF\n")
    edits, state = _run(
        "python3 - <<'PY'\nfrom pathlib import Path\np = Path(\"m.py\")\nt = compute()\np.write_text(t)\nPY\n",
        state,
    )
    assert edits == [] and state["/repo/m.py"] == "aaa\n"


def test_a_syntactically_broken_script_recovers_nothing_and_does_not_raise():
    edits, state = _run("python3 - <<'PY'\nthis is not python(((\nPY\n")
    assert edits == [] and state == {}


# --------------------------------------------------------------------------- sed -i


def test_bsd_style_in_place_sed_with_an_empty_backup_suffix():
    # macOS requires the suffix and agents pass `''`. Reading that as the script — or as a
    # filename — recovered nothing from every session recorded on a Mac.
    _, state = _run("cat > a.txt <<'EOF'\nalpha\nEOF\n")
    edits, state = _run("sed -i '' 's/alpha/beta/' a.txt", state)
    assert state["/repo/a.txt"] == "beta\n" and _lines(edits) == (1, 1)


def test_gnu_style_in_place_sed_and_the_global_flag():
    _, state = _run("cat > a.txt <<'EOF'\nx x x\nEOF\n")
    _, state = _run("sed -i 's/x/y/g' a.txt", state)
    assert state["/repo/a.txt"] == "y y y\n"


def test_sed_without_in_place_changes_nothing():
    _, state = _run("cat > a.txt <<'EOF'\nalpha\nEOF\n")
    edits, state = _run("sed 's/alpha/beta/' a.txt", state)
    assert edits == [] and state["/repo/a.txt"] == "alpha\n"


def test_a_basic_regex_only_pattern_is_declined():
    # BRE's `\(` is a GROUP to sed and a LITERAL PAREN to Python's re, so applying it would
    # substitute the wrong text silently. `-E` makes the same pattern mean what re means.
    _, state = _run("cat > a.txt <<'EOF'\nfoo123\nEOF\n")
    edits, state = _run(r"sed -i '' 's/\(foo\)[0-9]*/\1/' a.txt", state)
    assert edits == [] and state["/repo/a.txt"] == "foo123\n"
    _, state = _run(r"sed -i '' -E 's/(foo)[0-9]*/\1/' a.txt", state)
    assert state["/repo/a.txt"] == "foo\n"


def test_sed_on_a_file_the_session_never_wrote_is_skipped():
    # There is no prior content to substitute into, and inventing one would be a wrong baseline.
    edits, state = _run("sed -i '' 's/a/b/' untouched.py")
    assert edits == [] and state == {}


def test_the_word_sed_inside_another_command_is_not_a_sed_call():
    _, state = _run("cat > a.txt <<'EOF'\nalpha\nEOF\n")
    edits, state = _run("grep -c 'sed -i' a.txt", state)
    assert edits == [] and state["/repo/a.txt"] == "alpha\n"


# --------------------------------------------------------------------------- echo / printf


def test_echo_and_printf_redirects():
    edits, state = _run("echo hello > a.txt")
    assert state["/repo/a.txt"] == "hello\n" and _lines(edits) == (1, 0)
    _, state = _run("printf 'x\\ny\\n' > b.txt", state)
    assert state["/repo/b.txt"] == "x\ny\n"
    _, state = _run("echo more >> a.txt", state)
    assert state["/repo/a.txt"] == "hello\nmore\n"


def test_printf_with_format_specifiers_is_declined():
    # The specifiers consume the remaining arguments; the bytes written are not in the text.
    edits, state = _run("printf '%s\\n' \"$NAME\" > a.txt")
    assert edits == [] and state == {}


def test_a_redirect_from_an_arbitrary_command_is_not_a_recoverable_write():
    edits, state = _run("uv run pytest -q > results.txt")
    assert edits == [] and state == {}


# --------------------------------------------------------------------------- mv / cp / rm


def test_rm_of_a_tracked_file_records_its_deletion():
    _, state = _run("cat > tmp.py <<'EOF'\none\ntwo\nEOF\n")
    edits, state = _run("rm -f tmp.py", state)
    assert _lines(edits) == (0, 2) and "/repo/tmp.py" not in state
    assert "deleted file mode" in edits[0].patch


def test_mv_follows_the_content_so_later_edits_diff_against_it():
    _, state = _run("cat > old.py <<'EOF'\none\nEOF\n")
    _, state = _run("mv old.py new.py", state)
    assert state["/repo/new.py"] == "one\n" and "/repo/old.py" not in state
    edits, state = _run("sed -i '' 's/one/two/' new.py", state)
    assert _lines(edits) == (1, 1)  # not the whole file re-counted as new


def test_cp_keeps_the_source():
    _, state = _run("cat > a.py <<'EOF'\none\nEOF\n")
    _, state = _run("cp a.py b.py", state)
    assert state["/repo/a.py"] == "one\n" and state["/repo/b.py"] == "one\n"


def test_rm_of_a_file_the_session_never_wrote_records_nothing():
    edits, state = _run("rm -rf build/")
    assert edits == [] and state == {}


# --------------------------------------------------------------------------- paths


def test_relative_paths_resolve_to_the_same_key_the_editing_tools_use():
    # The whole point: a file written by `cat > tests/x.py` and later patched by the Edit tool
    # (which records an absolute path) must be ONE entry, or its lines are counted twice.
    _, state = _run("cat > tests/x.py <<'EOF'\none\nEOF\n")
    assert list(state) == ["/repo/tests/x.py"]


def test_an_absolute_path_is_kept_as_it_is():
    _, state = _run("cat > /repo/pkg/x.py <<'EOF'\none\nEOF\n")
    assert list(state) == ["/repo/pkg/x.py"]


def test_a_path_the_text_cannot_pin_down_is_skipped():
    for command in (
        "cat > $SCRATCH/x.py <<'EOF'\none\nEOF\n",
        "cat > ~/notes.md <<'EOF'\none\nEOF\n",
        "cat > /dev/null <<'EOF'\none\nEOF\n",
        "cat > ../outside.py <<'EOF'\none\nEOF\n",
    ):
        edits, state = _run(command)
        assert edits == [] and state == {}, command


def test_a_relative_path_with_no_recorded_cwd_is_skipped():
    # Storing it relative would put the same file in the state twice, under two keys.
    edits, state = _run("cat > x.py <<'EOF'\none\nEOF\n", cwd="")
    assert edits == [] and state == {}


def test_cd_moves_where_relative_writes_land():
    # Without following `cd`, this was resolved against the session's directory and recorded as
    # /repo/notes.md — a repo-root file that never existed. The reconstruction invented paths.
    edits, state = _run("cd sub && cat > notes.md <<'EOF'\nalpha\nEOF\n")
    assert list(state) == ["/repo/sub/notes.md"]
    assert edits[0].path == "/repo/sub/notes.md"


def test_cd_to_an_absolute_path_outside_the_directory_is_followed_not_folded_in():
    # Scratch work done elsewhere must keep its real path, so the backtrace's own
    # outside-the-directory filter can drop it — rather than being counted as a repo change.
    _, state = _run("cd /elsewhere/tmp && cat > notes.md <<'EOF'\nalpha\nEOF\n")
    assert list(state) == ["/elsewhere/tmp/notes.md"]


def test_cd_upwards_is_normalised():
    _, state = _run("cd pkg/sub && cd ../other && echo hi > f.txt")
    assert list(state) == ["/repo/pkg/other/f.txt"]


def test_a_cd_the_text_cannot_resolve_makes_later_relative_writes_unresolvable():
    # `cd $SCRATCH` lands somewhere only the running shell knew. Resolving the write against
    # the OLD directory would attribute it to a file that was never touched.
    for command in ("cd $SCRATCH && cat > x.py <<'EOF'\none\nEOF\n", "cd - && echo hi > x.py"):
        edits, state = _run(command)
        assert edits == [] and state == {}, command


def test_a_subshell_cd_does_not_move_the_lines_after_it():
    # `(cd pkg && …)` moves nothing for the caller. Letting it persist resolved a later line's
    # `pkg/mod.py` to `pkg/pkg/mod.py` — a path that has never existed.
    _, state = _run("(cd pkg && echo one > a.txt)\necho two > pkg/b.txt\n")
    assert sorted(state) == ["/repo/pkg/a.txt", "/repo/pkg/b.txt"]


def test_python_invoked_by_path_is_still_python():
    # `./.venv/bin/python - <<PY` is the same script as `python3 - <<PY`. Requiring a bare word
    # missed every one — one measured session ran 60+ that way and reconstructed almost none.
    _, state = _run("cat > m.py <<'EOF'\naaa\nEOF\n")
    edits, state = _run(
        "./.venv/bin/python - <<'PY'\nfrom pathlib import Path\np = Path(\"m.py\")\n"
        't = p.read_text()\nt = t.replace("aaa", "bbb")\np.write_text(t)\nPY\n',
        state,
    )
    assert state["/repo/m.py"] == "bbb\n" and _lines(edits) == (1, 1)


def test_a_variable_the_command_assigns_is_expanded():
    # `SP=/tmp/work` then `cd $SP` — naming a directory once and reusing it. The value is in the
    # command, so treating every `$` as unknowable declined whole sessions that worked this way.
    _, state = _run("SP=/tmp/work\ncd $SP && cat > notes.md <<'EOF'\nalpha\nEOF\n")
    assert list(state) == ["/tmp/work/notes.md"]
    _, state = _run("D=pkg/sub\necho hi > ${D}/f.txt")
    assert list(state) == ["/repo/pkg/sub/f.txt"]


def test_a_variable_the_command_never_assigns_stays_unresolvable():
    edits, state = _run("cd $SOMETHING_ELSE && cat > x.py <<'EOF'\none\nEOF\n")
    assert edits == [] and state == {}


def test_a_variable_assigned_from_a_substitution_is_not_guessed():
    edits, state = _run("SP=$(mktemp -d)\ncat > $SP/x.py <<'EOF'\none\nEOF\n")
    assert edits == [] and state == {}


def test_commands_apply_in_order_within_one_call():
    edits, state = _run("cat > a.txt <<'EOF'\nalpha\nEOF\nsed -i '' 's/alpha/beta/' a.txt\necho gamma >> a.txt\n")
    assert state["/repo/a.txt"] == "beta\ngamma\n"
    assert [e.insertions for e in edits] == [1, 1, 1]


def test_a_command_that_changes_nothing_yields_no_edit():
    _, state = _run("cat > a.txt <<'EOF'\nsame\nEOF\n")
    edits, state = _run("cat > a.txt <<'EOF'\nsame\nEOF\n", state)
    assert edits == []


# --------------------------------------------------------------------------- backend wiring
#
# Every backend has a shell tool and spells it differently, so each parser has to reach the
# recovery on its own. These prove the wiring; the live end-to-end checks are in
# tests/test_backtrace_shell_live.py.


def test_claude_reads_its_bash_tool_and_resolves_paths_against_the_recorded_cwd():
    from agitrack.transcripts import claude

    rows = [
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-07-02T09:00:00Z",
            "message": {"role": "user", "content": "go"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "cwd": "/r",
            "timestamp": "2026-07-02T09:00:01Z",
            "message": {
                "id": "m1",
                "role": "assistant",
                "stop_reason": "end_turn",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "cat > f.py <<'EOF'\ndef add():\n    pass\nEOF\n"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Edit",
                        # The SAME file, named absolutely as the editing tools always do. It must
                        # diff against what the heredoc wrote, not count the whole file again.
                        "input": {"file_path": "/r/f.py", "old_string": "    pass\n", "new_string": "    return 1\n"},
                    },
                ],
            },
        },
    ]
    turns = claude.parse_rows("s", rows, collect_edits=True).turns
    assert [e.path for e in turns[0].edits] == ["/r/f.py", "/r/f.py"]
    assert sum(e.insertions for e in turns[0].edits) == 3  # 2 written + 1 changed
    assert sum(e.deletions for e in turns[0].edits) == 1
    assert claude.parse_rows("s", rows).turns[0].edits == []  # opt-in, as before


def test_codex_reads_exec_command_and_the_javascript_exec_sandbox():
    from agitrack.transcripts import codex

    def call(name, payload_extra):
        return {"type": "response_item", "payload": {"type": "function_call", "name": name, **payload_extra}}

    rows = [
        {"type": "session_meta", "payload": {"cwd": "/r", "id": "c1"}},
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
        },
        call("exec_command", {"arguments": '{"cmd":"cat > f.py <<\'EOF\'\\nalpha\\nEOF\\n","workdir":"/r"}'}),
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "input": 'const r = await tools.exec_command({cmd:"sed -i \'\' s/alpha/beta/ f.py","workdir":"/r"});',
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
        },
    ]
    turns = codex.parse_exported_session(rows, collect_edits=True).turns
    # Codex coalesces a turn's edits per file (`merge_edits_by_path`), so the heredoc's write
    # and the `sed` that followed it arrive as ONE entry carrying both.
    edits = [e for turn in turns for e in turn.edits]
    assert [e.path for e in edits] == ["/r/f.py"]
    assert (edits[0].insertions, edits[0].deletions) == (2, 1)
    assert not any(turn.edits for turn in codex.parse_exported_session(rows).turns)


def test_opencode_reads_its_bash_tool_against_the_session_directory():
    from agitrack.transcripts import opencode

    data = {
        "info": {"id": "ses_1", "directory": "/r", "time": {"updated": 1_700_000_000_000}},
        "messages": [
            {
                "info": {"id": "u1", "role": "user", "time": {"created": 1_700_000_000_000}},
                "parts": [{"type": "text", "text": "go"}],
            },
            {
                "info": {
                    "id": "a1",
                    "role": "assistant",
                    "time": {"created": 1_700_000_001_000},
                    "finish": "stop",
                    "model": {"providerID": "anthropic", "modelID": "claude"},
                },
                "parts": [
                    {
                        "type": "tool",
                        "tool": "bash",
                        "state": {"input": {"command": "cat > f.py <<'EOF'\nalpha\nbeta\nEOF\n"}},
                    },
                    {
                        "type": "tool",
                        "tool": "edit",
                        "state": {"input": {"filePath": "/r/f.py", "oldString": "alpha\n", "newString": "ALPHA\n"}},
                    },
                    {"type": "text", "text": "done", "metadata": {"phase": "final_answer"}},
                ],
            },
        ],
    }
    edits = opencode.parse_exported_session(data, collect_edits=True).turns[0].edits
    assert {e.path for e in edits} == {"/r/f.py"}
    assert (sum(e.insertions for e in edits), sum(e.deletions for e in edits)) == (3, 1)
    assert opencode.parse_exported_session(data).turns[0].edits == []


# --------------------------------------------------------------------------- sub-agent edits
#
# A sub-agent's transcript is not a conversation the user can resume, so it appears in no
# session listing and nothing else reads it. Its tokens were already recovered; its EDITS were
# not — so every file a delegated agent wrote was missing from the backtrace, and a turn that
# fanned all its work out looked like a turn that changed nothing. The work belongs to the turn
# that delegated it, which is where its tokens already go.


def _claude_session_with_subagent(tmp_path, sub_rows):
    session = tmp_path / "s.jsonl"
    rows = [
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-07-02T09:00:00Z",
            "message": {"role": "user", "content": "go"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "cwd": "/r",
            "timestamp": "2026-07-02T09:00:01Z",
            "message": {
                "id": "m1",
                "role": "assistant",
                "stop_reason": "end_turn",
                "model": "claude-opus-4-8",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [
                    {"type": "tool_use", "id": "task-1", "name": "Task", "input": {"subagent_type": "general-purpose"}}
                ],
            },
        },
    ]
    session.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    subdir = tmp_path / "s" / "subagents"
    subdir.mkdir(parents=True)
    (subdir / "agent-x.jsonl").write_text("\n".join(json.dumps(row) for row in sub_rows) + "\n")
    (subdir / "agent-x.meta.json").write_text(json.dumps({"toolUseId": "task-1"}))
    return session


def test_claude_subagent_edits_land_on_the_turn_that_launched_it(tmp_path):
    from agitrack.transcripts import claude

    session = _claude_session_with_subagent(
        tmp_path,
        [
            {
                "type": "assistant",
                "uuid": "s1",
                "cwd": "/r",
                "isSidechain": True,
                "message": {
                    "id": "sm1",
                    "role": "assistant",
                    "content": [
                        # One through the shell and one through an editing tool: a sub-agent
                        # reaches for both, exactly as the agent that launched it does.
                        {
                            "type": "tool_use",
                            "id": "s-t1",
                            "name": "Bash",
                            "input": {"command": "cat > sub.py <<'EOF'\nalpha\nbeta\nEOF\n"},
                        },
                        {
                            "type": "tool_use",
                            "id": "s-t2",
                            "name": "Write",
                            "input": {"file_path": "/r/other.py", "content": "one\n"},
                        },
                    ],
                },
            }
        ],
    )
    turn = claude.export_session_at(session, collect_edits=True).turns[0]
    assert sorted(e.path for e in turn.edits) == ["/r/other.py", "/r/sub.py"]
    assert sum(e.insertions for e in turn.edits) == 3
    assert claude.export_session_at(session).turns[0].edits == []  # opt-in, and no extra file reads


def test_opencode_subagent_edits_land_on_the_turn_that_launched_it(monkeypatch, tmp_path):
    from agitrack.transcripts import opencode

    def task_part(parent, child):
        return {
            "type": "tool",
            "tool": "task",
            "state": {"metadata": {"sessionId": child, "parentSessionId": parent}},
        }

    child = {
        "info": {"id": "C", "directory": "/r"},
        "messages": [
            {
                "info": {"role": "assistant", "tokens": {"total": 5, "input": 1, "output": 5}},
                "parts": [
                    {
                        "type": "tool",
                        "tool": "bash",
                        "state": {"input": {"command": "cat > sub.py <<'EOF'\nalpha\nbeta\nEOF\n"}},
                    },
                ],
            }
        ],
    }
    monkeypatch.setattr(opencode, "_export_data", lambda repo, sid: {"C": child}.get(sid))
    parent = {
        "info": {"id": "P", "directory": "/r", "time": {"updated": 1_700_000_000_000}},
        "messages": [
            {
                "info": {"id": "u1", "role": "user", "time": {"created": 1_700_000_000_000}},
                "parts": [{"type": "text", "text": "go"}],
            },
            {
                "info": {
                    "id": "a1",
                    "role": "assistant",
                    "time": {"created": 1_700_000_001_000},
                    "finish": "stop",
                    "model": {"providerID": "anthropic", "modelID": "claude"},
                },
                "parts": [task_part("P", "C"), {"type": "text", "text": "done", "metadata": {"phase": "final_answer"}}],
            },
        ],
    }
    work = opencode._collect_subagent_work(tmp_path, "P", parent, collect_edits=True)
    turn = opencode.parse_exported_session(
        parent,
        subagent_tokens={c: u for c, (u, _e) in work.items()},
        subagent_edits={c: e for c, (_u, e) in work.items()},
        collect_edits=True,
    ).turns[0]
    assert [e.path for e in turn.edits] == ["/r/sub.py"]
    assert turn.edits[0].insertions == 2


def test_codex_subagent_shell_edits_are_collected(monkeypatch, tmp_path):
    from agitrack.transcripts import codex

    child_rows = [
        {"type": "session_meta", "payload": {"cwd": "/r", "id": "child"}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"cmd":"cat > sub.py <<\'EOF\'\\nalpha\\nEOF\\n"}',
            },
        },
    ]
    path = tmp_path / "child.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in child_rows) + "\n")
    monkeypatch.setattr(codex, "session_transcript_path", lambda sid: path if sid == "child" else None)
    monkeypatch.setattr(codex, "_spawned_thread_ids", lambda sid: [])

    work = codex._subagent_work("parent", extra_children=["child"], collect_edits=True)
    _usage, edits = work["child"]
    assert [e.path for e in edits] == ["/r/sub.py"]
    assert edits[0].insertions == 1
