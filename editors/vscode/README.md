# aGiTrack for VSCode

Chat with a coding agent (Claude Code / OpenCode) from VSCode's **Chat** window, with
[aGiTrack](https://github.com/core-aix/agitrack) auto-committing every turn so each
exchange leaves a tracked, provenance-rich commit behind.

This extension is a thin **Chat participant**: it shells out to the `agitrack` CLI in
its headless `--mode json --json-events` mode and renders the streamed events in chat.
aGiTrack itself does all the work (session tracking, backend orchestration, commits).

## Requirements

- aGiTrack installed and on your `PATH` (`pipx install agitrack`, or set `agitrack.path`).
- A backend installed (Claude Code or OpenCode), the same as using aGiTrack in a terminal.
- The workspace is a git repository.

## Usage

Open the Chat view and address the participant:

```
@agitrack add a healthcheck endpoint and a test for it
```

Each turn spawns `agitrack --repo <workspace> --mode json --json-events --prompt "<text>"`.
aGiTrack resumes the same conversation across turns automatically (it persists the
backend session), so you can keep chatting. When a turn changes files, aGiTrack commits
them and the chat shows the short commit SHA.

## Settings

| Setting | Default | Description |
| --- | --- | --- |
| `agitrack.path` | `agitrack` | Path to the aGiTrack executable. |
| `agitrack.backend` | (aGiTrack default) | `claude` or `opencode`. |

## Event protocol

The extension consumes the line-delimited JSON that `agitrack --mode json --json-events`
prints, one object per turn event:

```json
{"type": "response", "text": "...", "session": "...", "model": "..."}
{"type": "commit",   "sha": "abc1234", "session": "..."}
{"type": "no_changes"}
{"type": "error",    "message": "...", "exit_code": 1}
```

Non-JSON lines (aGiTrack's human-readable output) are ignored.

## Developing

This extension is **not** built by aGiTrack's Python CI. Build and run it manually:

```bash
cd editors/vscode
npm install
npm run compile      # type-checks and emits out/extension.js
```

Then press **F5** in VSCode ("Run Extension") to launch an Extension Development Host,
open a git repo in it, and chat with `@agitrack`.
