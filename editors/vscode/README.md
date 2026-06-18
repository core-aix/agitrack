# aGiTrack for VSCode

Install [aGiTrack](https://github.com/core-aix/agitrack) as a VSCode plugin and launch
it **inside VSCode with one click** — no opening a terminal and typing `agitrack`
yourself.

This extension is a thin **launcher**: it runs the real aGiTrack CLI in a VSCode
integrated terminal, so you get the **complete aGiTrack experience** — the coding
agent's native interface (Claude Code / OpenCode), the `Ctrl-G` command menu, sessions,
sharing, worktrees, and auto-commits with full provenance on every turn. Everything
aGiTrack does in a terminal, started from VSCode.

## Requirements

- aGiTrack installed and on your `PATH` (`pipx install agitrack`), or set `agitrack.path`.
- A backend installed (Claude Code or OpenCode), the same as using aGiTrack in a terminal.
- The workspace is a git repository.

## Usage

- Click the **`$(git-commit) aGiTrack`** button in the status bar, or
- run **aGiTrack: Start Session** from the Command Palette, or
- right-click a folder in the Explorer → **aGiTrack: Start Session Here**.

A terminal opens in your workspace and aGiTrack starts. From there it's the normal
aGiTrack app: type to the agent, press `Ctrl-G` for aGiTrack's command menu (sessions,
sharing, summarizer, commits, update, …), and every turn is auto-committed.

Running the command again focuses the existing session (aGiTrack only allows one per
repository). Use **aGiTrack: Restart Session** to stop it and start fresh.

## Settings

| Setting | Default | Description |
| --- | --- | --- |
| `agitrack.path` | `agitrack` | Path to the aGiTrack executable. |
| `agitrack.backend` | (aGiTrack default) | `claude` or `opencode`. |
| `agitrack.args` | `[]` | Extra CLI arguments (e.g. `["--no-worktree"]`). |
| `agitrack.openOnStartup` | `false` | Start a session automatically when a workspace opens. |

## Updates

Because the extension runs the real aGiTrack CLI in a terminal, aGiTrack's own
**self-update works here exactly as in a standalone terminal** — you're offered the
update at startup, or any time via the `Ctrl-G` → *update* menu, and aGiTrack restarts
itself in place.

The extension and the CLI ship in **lockstep** (the extension's version always equals
the `agitrack` release it launches). The extension is published to the Marketplace at
that matching version, and VSCode auto-updates it like any extension. If the CLI ever
runs ahead of the installed extension (e.g. the CLI self-updated and the new extension
hasn't been pulled yet), the extension detects the mismatch on startup and prompts you
to update it.

## Develop

```bash
npm install
npm run compile      # or: npm run watch
```

Press <kbd>F5</kbd> ("Run Extension") to launch an Extension Development Host with the
extension loaded, then use the status-bar button or Command Palette.

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
