# aGiT

aGiT stands for agent + git. It is an interactive Python CLI that wraps coding-agent command line tools and Git so agentic code changes are committed with traceable metadata.

The MVP supports OpenCode as the first backend.

## Install

For local development:

```bash
python3 -m pip install -e .
```

This installs the `agit` command and the terminal UI dependency used for status bars and contextual command hints.

## Usage

Run in the current repository:

```bash
agit
```

By default, `agit` runs in proxy mode: it launches the real OpenCode TUI in a pseudo-terminal, renders it through an internal terminal screen, and reserves a bottom status line for aGiT. Press `Ctrl-G` to enter aGiT command mode.

Run against another repository:

```bash
agit --repo /path/to/repo
```

Show aGiT diagnostic messages:

```bash
agit --verbose
```

Use the structured JSON fallback mode:

```bash
agit --mode json
```

In proxy mode, press `Ctrl-G`, then type one of these aGiT commands:

```text
status            show git status
user-commit       create a user commit
stage             review and stage untracked files
unstaged          show intentionally unstaged files
agent-backend opencode select the OpenCode backend
exit              exit
```

The command palette previews available commands. Use Up/Down to select a command, Tab to complete it, and Enter to run it.

In JSON mode, plain text is sent to the active agent backend:

```text
> fix the parser bug
```

JSON mode aGiT commands use `:` so OpenCode-native `/` input is not intercepted:

```text
:help              show commands
:status            show git status
:user-commit       create a user commit
:stage             review and stage untracked files
:unstaged          show intentionally unstaged files
:agent-backend opencode select the OpenCode backend
:exit              exit
```

The current MVP invokes OpenCode through `opencode run --format json` for each prompt so aGiT can capture the final response and create traceable commits.

Proxy mode launches OpenCode's native TUI directly and uses `opencode export` to recover session metadata for automatic agent commits.

In JSON mode, aGiT shows a bottom status bar with the active backend, target repo, model, and unstaged-new-file count. Typing `:` shows aGiT command completions. Typing `/` shows common OpenCode command completions, and slash commands are forwarded to OpenCode rather than handled by aGiT.

## Commit Behavior

- Tracked modifications and deletions are staged with `git add -u`.
- New untracked files require confirmation before staging.
- Declined untracked files are remembered in repository-local `.agit/state.json`.
- Agent commits use the `<agent>` tag and include the full interaction trace since the last code-changing commit.
- Agent commit metadata includes context token count and generated token usage accumulated since the last code-changing commit.
- Proxy mode baselines the continued OpenCode session on startup so token metadata only includes turns after aGiT starts tracking new changes.
- Proxy mode preserves OpenCode's selected model in commit metadata when it can be read from exported session data.
- User commits use the user-provided subject and include aGiT metadata.
- Commits are created only when staged changes exist.

## Configuration

Repository-local configuration can be stored in `.agit/config.json`:

```json
{
  "trace_turn_limit": 5
}
```

`trace_turn_limit` controls the maximum number of recent user turns included in an agent commit body. The default is `5`.
