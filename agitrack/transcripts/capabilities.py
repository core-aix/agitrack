"""Which capabilities BEYOND the backend's built-in toolset a turn used.

A repo can extend the agent with MCP servers, plugins, skills and sub-agents, and what the agent
had available shapes the change as much as the model does — a commit produced with an
``mcp__github__create_pr`` call has very different provenance from one made with a plain ``Edit``.
aGiTrack therefore records them alongside the model and token counts.

Deliberately what a turn **used**, not what was merely configured: usage is provable from the
transcript alone (so it is identical in every mode, including the background daemon, which only
ever reads the transcript), it needs no config discovery that could disagree with reality, and it
is what actually produced the commit. A configured-but-unused server says nothing about the change.

Both backends name MCP tools with the server embedded, just differently — Claude
``mcp__<server>__<tool>``, OpenCode ``<server>_<tool>`` / ``<server>.<tool>`` — so the split lives
here and each parser only has to hand over the raw tool names it saw.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Claude prefixes every MCP tool with the server name; the separator is a double underscore, so a
# server or tool whose own name contains one underscore is still parsed correctly.
_CLAUDE_MCP_PREFIX = "mcp__"
_CLAUDE_MCP_SEPARATOR = "__"


def parse_mcp_tool(name: str) -> tuple[str, str] | None:
    """``(server, tool)`` for an MCP tool call, or None when *name* is a built-in tool.

    Recognizes Claude's
    ``<server>.<tool>`` spellings. OpenCode's form is ambiguous with an ordinary underscored
    tool name, so it is only accepted for names the caller has already established are MCP
    (see :func:`collect`'s ``mcp_names``) — never guessed from the shape alone.
    """
    if not name or not name.startswith(_CLAUDE_MCP_PREFIX):
        return None
    rest = name[len(_CLAUDE_MCP_PREFIX) :]
    server, separator, tool = rest.partition(_CLAUDE_MCP_SEPARATOR)
    if not separator or not server or not tool:
        return None
    return server, tool


def _ordered_unique(values) -> list[str]:
    """Distinct values in first-seen order — stable output for a commit message (a set would
    reorder between runs and make otherwise-identical metadata churn)."""
    seen: dict[str, None] = {}
    for value in values:
        text = str(value or "").strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


def split_mcp_names(names, *, servers=()) -> tuple[list[str], list[str]]:
    """``(servers, "server/tool" entries)`` for the MCP calls among *names*.

    Claude's ``mcp__<server>__<tool>`` is self-describing and always recognized. OpenCode instead
    names an MCP tool ``<server>_<tool>`` (``lore_lucky_number``), which is INDISTINGUISHABLE by
    shape from a built-in like ``apply_patch`` — so it is matched only against *servers*, the MCP
    servers actually configured for the repo. Never guessed from the shape alone: a wrong guess
    would invent a server name in a commit's provenance, which is worse than recording nothing.
    """
    known = sorted((str(s).strip() for s in servers or () if str(s).strip()), key=len, reverse=True)
    found_servers: list[str] = []
    tools: list[str] = []
    for name in names:
        text = str(name or "").strip()
        if not text:
            continue
        parsed = parse_mcp_tool(text)
        if parsed is None:
            # Longest server name first, so a "lore" and a "lore_extra" server can coexist.
            for server in known:
                for separator in ("_", "."):
                    prefix = server + separator
                    if text.startswith(prefix) and len(text) > len(prefix):
                        parsed = (server, text[len(prefix) :])
                        break
                if parsed is not None:
                    break
        if parsed is None:
            continue
        server, tool = parsed
        found_servers.append(server)
        tools.append(f"{server}/{tool}" if tool else server)
    return _ordered_unique(found_servers), _ordered_unique(tools)


@dataclass
class Capabilities:
    """The extensions one turn used, in the shape :class:`SessionTurn` stores them."""

    mcp_servers: list[str] = field(default_factory=list)
    mcp_tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    subagents: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)


def collect(
    *,
    tool_names,
    skills=(),
    subagents=(),
    mcp_servers=(),
) -> Capabilities:
    """The capability lists for one turn, ready to attach to a :class:`SessionTurn`.

    ``tool_names`` is every tool the turn called (built-ins included — they are filtered here);
    ``skills`` and ``subagents`` are the skill names and sub-agent types the turn invoked.
    """
    servers, tools = split_mcp_names(tool_names, servers=mcp_servers)
    named_skills = _ordered_unique(skills)
    return Capabilities(
        mcp_servers=servers,
        mcp_tools=tools,
        skills=named_skills,
        subagents=_ordered_unique(subagents),
        plugins=plugins_from_skills(named_skills),
    )


# A capability a PLUGIN supplies is namespaced with the plugin that shipped it — Claude renders a
# plugin skill as "<plugin>:<skill>" ("lorepack:pack-lore"). That prefix is the only place the
# plugin's identity appears in the transcript, so it is lifted into its own field: "which plugins
# were in play" is the question a reader asks, and it should not require parsing skill names.
_PLUGIN_SEPARATOR = ":"


def plugins_from_skills(skills) -> list[str]:
    """Plugin names lifted from ``<plugin>:<skill>`` entries. A bare skill name (a personal or
    project skill, no plugin) contributes nothing."""
    return _ordered_unique(
        str(skill).split(_PLUGIN_SEPARATOR, 1)[0] for skill in skills or () if _PLUGIN_SEPARATOR in str(skill)
    )


# The metadata keys, in the order they appear in a commit's `# aGiTrack Metadata` block.
FIELDS = ("mcp_servers", "mcp_tools", "plugins", "skills", "subagents")


def merge_turns(turns) -> dict[str, list[str]]:
    """Union the capability lists across the turns one commit accounts for.

    A union (not "latest wins" like ``reasoning_effort``): each list is the SET of extensions that
    were in play across the span, and a server used only in the first of three turns still shaped
    the commit. First-seen order is preserved so re-rendering the same span yields byte-identical
    metadata.
    """
    merged: dict[str, list[str]] = {}
    for name in FIELDS:
        merged[name] = _ordered_unique(value for turn in turns or [] for value in (getattr(turn, name, None) or []))
    return merged
