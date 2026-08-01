"""Capabilities beyond the backend's built-in toolset — MCP servers/tools, plugins, skills and
sub-agents — that a turn used, and how they reach a commit's `# aGiTrack Metadata` block.

A repo can extend the agent through MCP servers or plugins, and what the agent reached for shapes
the change as much as the model does, so the commit's provenance is incomplete without it.
"""

from __future__ import annotations

from agitrack.transcripts import capabilities


def test_claude_mcp_tool_names_split_into_server_and_tool():
    # Claude namespaces every MCP tool `mcp__<server>__<tool>`, so the split needs no config.
    servers, tools = capabilities.split_mcp_names(
        ["Bash", "mcp__lore__lucky_number", "mcp__lore__project_motto", "Edit"]
    )
    assert servers == ["lore"]
    assert tools == ["lore/lucky_number", "lore/project_motto"]


def test_server_or_tool_names_containing_an_underscore_survive_the_split():
    # The separator is a DOUBLE underscore, so single underscores inside either name are safe.
    servers, tools = capabilities.split_mcp_names(["mcp__my_server__do_a_thing"])
    assert servers == ["my_server"]
    assert tools == ["my_server/do_a_thing"]


def test_opencode_mcp_tools_are_resolved_against_the_configured_servers():
    # OpenCode names an MCP tool `<server>_<tool>`, which is indistinguishable by SHAPE from a
    # built-in like `apply_patch` — so the split is made against the servers actually configured.
    names = ["lore_lucky_number", "apply_patch", "glob", "read"]
    servers, tools = capabilities.split_mcp_names(names, servers=["lore"])
    assert servers == ["lore"]
    assert tools == ["lore/lucky_number"]


def test_opencode_names_are_never_guessed_without_configured_servers():
    # Guessing from the shape would invent a server name ("apply") in a commit's provenance —
    # worse than recording nothing. With no configured servers, nothing is claimed.
    assert capabilities.split_mcp_names(["lore_lucky_number", "apply_patch"]) == ([], [])


def test_longest_configured_server_name_wins():
    # `lore` and `lore_extra` can coexist; the more specific server must claim its own tool.
    servers, tools = capabilities.split_mcp_names(["lore_extra_do_thing"], servers=["lore", "lore_extra"])
    assert servers == ["lore_extra"]
    assert tools == ["lore_extra/do_thing"]


def test_plugins_are_lifted_from_namespaced_skill_names():
    # A plugin-supplied skill renders as "<plugin>:<skill>" — the ONLY place a plugin names itself
    # in a transcript. A bare (personal/project) skill contributes no plugin.
    assert capabilities.plugins_from_skills(["lorepack:pack-lore", "repo-lore"]) == ["lorepack"]
    assert capabilities.plugins_from_skills(["repo-lore"]) == []


def test_collect_reports_every_capability_kind_and_drops_builtins():
    used = capabilities.collect(
        tool_names=["Bash", "Edit", "mcp__lore__lucky_number"],
        skills=["lorepack:pack-lore", "repo-lore"],
        subagents=["Explore"],
    )
    assert used.mcp_servers == ["lore"] and used.mcp_tools == ["lore/lucky_number"]
    assert used.skills == ["lorepack:pack-lore", "repo-lore"]
    assert used.plugins == ["lorepack"]
    assert used.subagents == ["Explore"]


def test_collect_is_empty_for_a_turn_that_used_only_builtins():
    used = capabilities.collect(tool_names=["Bash", "Read", "Edit", "Grep"])
    assert (used.mcp_servers, used.mcp_tools, used.skills, used.subagents, used.plugins) == (
        [],
        [],
        [],
        [],
        [],
    )


class _Turn:
    def __init__(self, **kw):
        for name in capabilities.FIELDS:
            setattr(self, name, kw.get(name, []))


def test_merge_turns_unions_across_the_span_in_first_seen_order():
    # A union, not "latest wins": a server used only in the FIRST of three turns still shaped the
    # commit. First-seen order keeps re-rendering the same span byte-identical.
    merged = capabilities.merge_turns(
        [
            _Turn(mcp_servers=["lore"], mcp_tools=["lore/a"], skills=["s1"]),
            _Turn(mcp_servers=["github"], mcp_tools=["github/pr"]),
            _Turn(mcp_servers=["lore"], mcp_tools=["lore/a"], skills=["s1"]),  # duplicates collapse
        ]
    )
    assert merged["mcp_servers"] == ["lore", "github"]
    assert merged["mcp_tools"] == ["lore/a", "github/pr"]
    assert merged["skills"] == ["s1"]
    assert merged["subagents"] == []


def test_merge_turns_handles_no_turns():
    assert capabilities.merge_turns([]) == {name: [] for name in capabilities.FIELDS}


# --- parsers: the real transcript shapes both backends write ----------------------------------


def _claude_rows():
    """The row shapes a real Claude session writes for an MCP call, a skill and a sub-agent
    (captured from a live run against a local MCP server + a local plugin)."""
    return [
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-08-01T00:00:00.000Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "use the lore server"}]},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2026-08-01T00:00:05.000Z",
            "message": {
                "id": "msg-1",
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "mcp__lore__lucky_number", "input": {}},
                    {"type": "tool_use", "id": "t2", "name": "Skill", "input": {"skill": "lorepack:pack-lore"}},
                    {
                        "type": "tool_use",
                        "id": "t3",
                        "name": "Agent",
                        "input": {"description": "find it", "prompt": "…", "subagent_type": "Explore"},
                    },
                    {"type": "tool_use", "id": "t4", "name": "Edit", "input": {}},
                    {"type": "text", "text": "done"},
                ],
            },
        },
    ]


def test_claude_parser_records_mcp_skill_plugin_and_subagent():
    from agitrack.transcripts.claude import parse_rows

    turn = parse_rows("s1", _claude_rows()).turns[0]

    assert turn.mcp_servers == ["lore"] and turn.mcp_tools == ["lore/lucky_number"]
    assert turn.skills == ["lorepack:pack-lore"] and turn.plugins == ["lorepack"]
    assert turn.subagents == ["Explore"]


def test_claude_parser_leaves_capabilities_empty_for_a_builtin_only_turn():
    rows = _claude_rows()
    rows[1]["message"]["content"] = [
        {"type": "tool_use", "id": "t", "name": "Edit", "input": {}},
        {"type": "text", "text": "done"},
    ]

    from agitrack.transcripts.claude import parse_rows

    turn = parse_rows("s1", rows).turns[0]
    assert (turn.mcp_servers, turn.mcp_tools, turn.skills, turn.plugins, turn.subagents) == ([], [], [], [], [])


def _opencode_export():
    """OpenCode's `{info, messages}` export shape, with an MCP tool part (`<server>_<tool>`)
    alongside a built-in whose name also contains an underscore."""
    return {
        "info": {"id": "ses_1"},
        "messages": [
            {"info": {"id": "m1", "role": "user"}, "parts": [{"type": "text", "text": "use the lore server"}]},
            {
                "info": {"id": "m2", "role": "assistant", "finish": "stop"},
                "parts": [
                    {"type": "tool", "tool": "lore_lucky_number", "callID": "c1"},
                    {"type": "tool", "tool": "apply_patch", "callID": "c2"},
                    {"type": "tool", "tool": "task", "callID": "c3", "state": {"input": {"subagent_type": "explorer"}}},
                    {"type": "text", "text": "done"},
                ],
            },
        ],
    }


def test_opencode_parser_records_mcp_and_subagents_against_configured_servers():
    from agitrack.transcripts.opencode import parse_exported_session

    turn = parse_exported_session(_opencode_export(), mcp_servers=frozenset({"lore"})).turns[0]

    assert turn.mcp_servers == ["lore"] and turn.mcp_tools == ["lore/lucky_number"]
    assert turn.subagents == ["explorer"]
    assert turn.skills == []  # OpenCode has no skills concept


def test_opencode_parser_claims_nothing_when_no_mcp_server_is_configured():
    # Without the configured names, `lore_lucky_number` is indistinguishable from `apply_patch`.
    from agitrack.transcripts.opencode import parse_exported_session

    turn = parse_exported_session(_opencode_export()).turns[0]
    assert turn.mcp_servers == [] and turn.mcp_tools == []
    assert turn.subagents == ["explorer"]  # the sub-agent is unambiguous, so it is still recorded


def test_opencode_configured_mcp_servers_reads_the_project_config(tmp_path):
    from agitrack.transcripts.opencode import configured_mcp_servers

    (tmp_path / "opencode.json").write_text(
        '{"mcp": {"lore": {"type": "local", "command": ["x"]}, "other": {}}}', encoding="utf-8"
    )
    assert configured_mcp_servers(tmp_path) >= {"lore", "other"}


def test_opencode_configured_mcp_servers_survives_a_broken_config(tmp_path):
    # A malformed config must never raise mid-parse; recording nothing is the safe outcome.
    (tmp_path / "opencode.json").write_text("{not json", encoding="utf-8")
    from agitrack.transcripts.opencode import configured_mcp_servers

    assert isinstance(configured_mcp_servers(tmp_path), frozenset)


# --- the commit message ------------------------------------------------------------------------


def test_metadata_block_reports_every_capability_kind():
    from agitrack.commits.message import build_agent_commit_message

    message = build_agent_commit_message(
        latest_prompt="add a constant",
        trace=[{"role": "user", "content": "add a constant"}],
        backend="claude",
        backend_session_id="s1",
        agitrack_session_id="a1",
        model="claude-opus-5",
        capabilities={
            "mcp_servers": ["lore"],
            "mcp_tools": ["lore/lucky_number", "lore/project_motto"],
            "plugins": ["lorepack"],
            "skills": ["lorepack:pack-lore"],
            "subagents": ["Explore"],
        },
    )
    block = message.split("# aGiTrack Metadata", 1)[1]
    assert "mcp_servers: lore" in block
    assert "mcp_tools: lore/lucky_number lore/project_motto" in block
    assert "plugins: lorepack" in block
    assert "skills: lorepack:pack-lore" in block
    assert "subagents: Explore" in block


def test_metadata_block_omits_capability_lines_for_an_ordinary_commit():
    # A turn that used only built-ins must leave the metadata block exactly as it was before this
    # feature — no empty `mcp_servers:` lines cluttering every commit.
    from agitrack.commits.message import build_agent_commit_message

    message = build_agent_commit_message(
        latest_prompt="add a constant",
        trace=[{"role": "user", "content": "add a constant"}],
        backend="claude",
        backend_session_id="s1",
        agitrack_session_id="a1",
        model="claude-opus-5",
        capabilities={name: [] for name in capabilities.FIELDS},
    )
    for name in capabilities.FIELDS:
        assert f"{name}:" not in message


def test_a_long_capability_list_is_truncated_with_a_count():
    # A commit trailer must stay readable: a turn that fanned out over dozens of tools should not
    # bury the rest of the metadata, but the field must still report the true breadth.
    from agitrack.commits.message import build_agent_commit_message

    message = build_agent_commit_message(
        latest_prompt="p",
        trace=[{"role": "user", "content": "p"}],
        backend="claude",
        backend_session_id="s1",
        agitrack_session_id="a1",
        model="m",
        capabilities={"mcp_tools": [f"srv/tool{i}" for i in range(20)]},
    )
    line = next(ln for ln in message.splitlines() if ln.startswith("mcp_tools:"))
    assert "srv/tool0" in line and "+8 more" in line
    assert "srv/tool19" not in line


# --- every mode reaches the same metadata -------------------------------------------------------


def test_commit_engine_records_capabilities_in_every_mode(tmp_path):
    # The proxy (interactive) and latent (background / manual-commit) paths share one metadata
    # builder, so the same capabilities must land whichever mode produced the commit. Pinned for
    # both branches of `commit_turns` — `-b`, `-m` and the TUI must not diverge.
    from agitrack.backends.base import TokenUsage
    from agitrack.config import AgitrackState
    from agitrack.proxy.commit_engine import CommitEngine
    from agitrack.transcripts.types import SessionTurn

    class _Repo:
        def __init__(self):
            self.message = None

        def add_tracked(self):
            pass

        def has_staged_changes(self):
            return True

        def commit(self, message):
            self.message = message
            return "dead1234"

        def untracked_files(self):
            return []

        def stage_paths(self, paths):
            pass

    def _turn():
        return SessionTurn(
            "u",
            "a",
            "do it",
            "done",
            TokenUsage(total=1, output=1),
            "m",
            mcp_servers=["lore"],
            mcp_tools=["lore/lucky_number"],
            skills=["lorepack:pack-lore"],
            plugins=["lorepack"],
            subagents=["Explore"],
        )

    for accumulate_only_on_commit in (False, True):  # proxy path, then the latent/manual path
        repo = _Repo()
        engine = CommitEngine(repo, AgitrackState(tmp_path / f"m{accumulate_only_on_commit}"))
        engine.commit_turns(
            turns=[_turn()],
            backend="claude",
            backend_session_id="s1",
            model="m",
            stage_untracked_fn=lambda _repo, _state: None,
            accumulate_trace_only_on_commit=accumulate_only_on_commit,
        )
        assert repo.message is not None, accumulate_only_on_commit
        for expected in (
            "mcp_servers: lore",
            "mcp_tools: lore/lucky_number",
            "plugins: lorepack",
            "skills: lorepack:pack-lore",
            "subagents: Explore",
        ):
            assert expected in repo.message, (accumulate_only_on_commit, expected)
