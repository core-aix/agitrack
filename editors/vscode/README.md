# aGiTrack for VSCode

Run a coding agent (Claude Code / OpenCode) through
[aGiTrack](https://github.com/core-aix/agitrack) **inside VSCode, with no terminal**.
Chat with the agent in the native Chat view, answer aGiTrack's questions as native
menus and dialogs, and let aGiTrack auto-commit every turn with full provenance.

Unlike a plain terminal wrapper, this extension drives aGiTrack's **full interactive
experience** in the editor: when aGiTrack needs to ask something — *stage which
untracked files? pick a backend? what's the commit message?* — it pops up a VSCode
QuickPick, input box, or modal, and feeds your answer back.

## How it works

aGiTrack is launched once per workspace as a long-lived child process driven over a
bidirectional JSON-RPC bridge:

```
agitrack --repo <workspace> --mode json --ui-bridge --skip-privacy-ack
```

- Prompts and `:` commands are written to the child's **stdin** as JSON lines.
- aGiTrack streams back `response`, `commit`, `notice`, and `error` events on **stdout**.
- When aGiTrack needs input it emits an `ask` event; the extension shows the matching
  native UI (menu / multi-select / input box / confirm) and writes the answer back.

aGiTrack does all the real work — session tracking, backend orchestration, summaries,
and commits. The extension is the editor-side UI for it.

## Requirements

- aGiTrack installed and on your `PATH` (`pipx install agitrack`), or set `agitrack.path`.
- A backend installed (Claude Code or OpenCode), same as using aGiTrack in a terminal.
- The workspace is a git repository.

## Usage

### Chat

Open the Chat view and address the participant:

```
@agitrack add a healthcheck endpoint and a test for it
```

Responses stream into the chat. If aGiTrack asks whether to stage new files, a menu
appears; your reply continues the turn. When the turn changes files, the chat shows
the short commit SHA aGiTrack created.

### Commands

From the Command Palette (`aGiTrack:` prefix):

| Command | What it does |
| --- | --- |
| Show Git Status | Current working-tree status |
| Review & Stage Untracked Files | Menu to stage all / pick / skip untracked files |
| Create User Commit | Commit your own (non-agent) changes with a message |
| Show Intentionally Unstaged Files | Files you chose not to stage |
| Switch Agent Backend | Pick Claude Code or OpenCode |
| Start New Session | Begin a fresh agent conversation |
| Manage Summarizer | Turn commit summarization on/off, set its model |
| Restart aGiTrack | Restart the background aGiTrack process |

## Settings

| Setting | Default | Description |
| --- | --- | --- |
| `agitrack.path` | `agitrack` | Path to the aGiTrack executable. |
| `agitrack.backend` | (aGiTrack default) | `claude` or `opencode`. |

## Develop

```bash
npm install
npm run compile      # or: npm run watch
```

Press <kbd>F5</kbd> ("Run Extension") to launch an Extension Development Host with the
extension loaded. Open a git repo, open the Chat view, and talk to `@agitrack`.

## Package & publish

Packaging and publishing use [`@vscode/vsce`](https://github.com/microsoft/vscode-vsce)
(a dev dependency):

```bash
npm run package      # produces agitrack-vscode-<version>.vsix
```

Install the `.vsix` locally with **Extensions: Install from VSIX…**, or:

```bash
code --install-extension agitrack-vscode-<version>.vsix
```

To publish to the [Visual Studio Marketplace](https://marketplace.visualstudio.com/),
the maintainer needs a publisher and a Personal Access Token (PAT) — see
[the vsce publishing guide](https://code.visualstudio.com/api/working-with-extensions/publishing-extension):

```bash
vsce login core-aix      # one time, with the publisher's Azure DevOps PAT
npm run publish          # vsce publish
```

> The `publisher` field in `package.json` (`core-aix`) must match the Marketplace
> publisher that owns the PAT. Publishing cannot be done without that token.

## Protocol reference

Newline-delimited JSON in both directions.

**Editor → aGiTrack (stdin):**

| Message | Meaning |
| --- | --- |
| `{"type":"prompt","text":"…"}` | Run one agent turn |
| `{"type":"command","text":":status"}` | Run an aGiTrack `:` command |
| `{"type":"answer","id":"ask-3","value":…}` | Reply to an `ask` |
| `{"type":"exit"}` | Shut the session down |

**aGiTrack → editor (stdout):**

| Event | Meaning |
| --- | --- |
| `ready` | Session started (`session`, `backend`, `repo`) |
| `response` | Agent reply text |
| `commit` | A commit was created (`sha`) |
| `no_changes` | The turn changed no files |
| `notice` | Informational message (`level`: info/warn/error) |
| `error` | Something failed (`message`) |
| `ask` | Needs input (`kind`: select/multiselect/input/confirm) |
| `turn-complete` | The current prompt/command finished |
| `bye` | The session is exiting |
