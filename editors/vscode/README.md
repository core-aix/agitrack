# aGiTrack for VSCode

Install [aGiTrack](https://github.com/core-aix/agitrack) as a VSCode plugin and launch
it **inside VSCode** — no opening a terminal and typing `agitrack` yourself.

**To start aGiTrack, do either:**

- Click the **aG button** — the brand icon at the **top-right of the editor toolbar** — or
- open the **Command Palette** (<kbd>Ctrl/Cmd</kbd>+<kbd>Shift</kbd>+<kbd>P</kbd>) and run
  **aGiTrack: Start aGiTrack**.

That's it — a session opens in a VSCode terminal.

This extension is a thin **launcher**: it runs the real aGiTrack CLI in a VSCode
integrated terminal, so you get the **complete aGiTrack experience** — the coding
agent's native interface (Claude Code / OpenCode), the `Ctrl-G` command menu, sessions,
sharing, worktrees, and auto-commits with full provenance on every turn. Everything
aGiTrack does in a terminal, started from VSCode.

## Requirements

- **Python 3.10+** to install the aGiTrack CLI via pipx/pip — **or**, on Windows, nothing
  at all: the extension can install the self-contained MSI (which bundles its own Python).
- A backend installed (Claude Code or OpenCode), the same as using aGiTrack in a terminal.
- The workspace is a git repository.

The aGiTrack CLI itself does **not** need to be installed first — if it's missing, the
extension offers to install it for you (via `pipx`, falling back to `pip --user`). On
Windows, when neither pipx nor pip is available, it falls back to downloading the
standalone **MSI** from the latest GitHub release and running it (you'll see a Windows
elevation prompt) — so the extension works even on a machine with no Python. The extension
also discovers an MSI you installed by hand under `C:\Program Files\aGiTrack`. If you
already have the CLI elsewhere, point `agitrack.path` at it.

## Usage

Start a session in any of these ways:

- Click the **aG button** (the brand icon at the top-right of the editor toolbar),
- run **aGiTrack: Start aGiTrack** from the Command Palette, or
- right-click a folder in the Explorer → **aGiTrack: Start aGiTrack Here**.

A session opens **beside the editor** (a split to the right) by default and starts. From
there it's the normal aGiTrack app: type to the agent, press `Ctrl-G` for aGiTrack's
command menu (sessions, sharing, summarizer, **dashboard**, commits, update, …), and
every turn is auto-committed. Prefer the bottom panel? Set `agitrack.terminalLocation`
to `panel`.

Running it again focuses the existing session (aGiTrack only allows one per repository).
**aGiTrack: Restart aGiTrack** stops it and starts fresh.

**Closing the session terminal** prompts you to confirm, and aGiTrack then **exits
gracefully** — finalizing and committing the latest turn rather than dropping it. The
prompt is VSCode's own "terminate the running process?"; since VSCode has no
per-terminal close hook, the extension makes sure it appears by raising
`terminal.integrated.confirmOnKill` to `always` when your current setting wouldn't
prompt for the aGiTrack terminal. Turn this off with `agitrack.confirmTerminalClose:
false`.

> The session terminal sets `CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL=1` so the Claude Code
> backend doesn't try to auto-install its own VSCode companion extension here (that
> attempt fails in this context and shows a confusing error). If you want Claude's IDE
> features, install the *Claude Code* extension from the Marketplace yourself.

### Dashboard

**aGiTrack: Open Dashboard** (Command Palette) — or the `dashboard` item in the
`Ctrl-G` menu inside a session — runs aGiTrack's metrics dashboard. It serves a
filterable, auto-refreshing report and opens it in your browser; read-only, runs
alongside a session, Ctrl-C in its terminal to stop.

When the workspace is **remote** (Remote-SSH / WSL / container / Codespaces) or you're
in an SSH/Mosh session, the dashboard never tries to open a browser on the remote host
(which would be headless). VSCode automatically forwards the port so the printed URL
opens on **your** machine; over plain SSH, forward it yourself (e.g.
`ssh -L 8765:127.0.0.1:8765 <host>`). An explicit `$BROWSER` is always honored.

## Settings

| Setting | Default | Description |
| --- | --- | --- |
| `agitrack.path` | `agitrack` | Path to the aGiTrack executable. |
| `agitrack.backend` | (aGiTrack default) | `claude` or `opencode`. |
| `agitrack.args` | `[]` | Extra CLI arguments (e.g. `["--no-worktree"]`). |
| `agitrack.openOnStartup` | `false` | Start a session automatically when a workspace opens. |
| `agitrack.terminalLocation` | `beside` | Where the terminal opens: `beside` (right of the editor), `editor` (new editor tab), or `panel` (bottom). |

## Remote development

The extension is a **workspace** extension, so in a **Remote-SSH**, **WSL**, **Dev
Container**, or **Codespaces** window it runs on the *remote* host — the same machine
as your code. That means:

- the aGiTrack terminal opens on the remote (where the repo lives and the agent must run);
- `agitrack` is found on the remote's `PATH` (set `agitrack.path` in your **Remote**
  settings if needed); and
- if aGiTrack isn't installed on the remote, the extension installs it **there** (the
  pipx/pip install runs on the remote host, next to the code).

When you install the extension in a remote window, VSCode installs it on the remote
automatically. Locally (no remote), the same extension just runs on your machine.

## Windows

aGiTrack is POSIX-only (it uses pty/termios/fcntl), so it does not run on **native
Windows**. On Windows, open your project in **WSL** (or a Dev Container / Remote-SSH) and
run aGiTrack there: because this is a workspace extension, it then runs on the Linux side
and works exactly as on macOS/Linux. In a native Windows window the extension detects this
and points you to [Remote-WSL](https://code.visualstudio.com/docs/remote/wsl) instead of
launching something that can't run.

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
npm test             # unit tests (node:test); covers the native-Windows platform guard
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
