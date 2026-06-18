# Changelog

The extension version always tracks the aGiTrack CLI version — they ship in lockstep
(`scripts/sync_vscode_version.py`, enforced in CI), so the extension's version number
matches the `agitrack` release it launches.

## Current

Launch the full aGiTrack terminal application from VSCode — no terminal typing.

- A brand-icon **aGiTrack button** at the top-right of the editor toolbar that starts
  a session in one click. Restart and Open Dashboard are Command Palette commands;
  Start is also on the Explorer folder context menu.
- aGiTrack starts only **after** the terminal's automatic startup has run — VSCode's
  venv/conda activation, shell integration, and any other commands it injects — by
  sequencing the launch through shell integration. This fixes those commands being typed
  into aGiTrack instead of the shell (e.g. `source .venv/bin/activate` landing in the
  agent, or a stray newline auto-acknowledging the sensitive-information prompt). aGiTrack
  also drains any pending terminal input right before that prompt as a backstop.
- Exiting aGiTrack (e.g. the `Ctrl-G` → exit menu) **closes the terminal** automatically:
  the session is launched with `exec`, so when aGiTrack exits the process is gone and the
  terminal closes.
- Closing the window/terminal exits aGiTrack **gracefully**, finalizing the latest turn
  instead of stranding it. On shutdown the extension signals aGiTrack and waits (up to
  60s) for it to finish; it also raises `terminal.integrated.confirmOnKill` to `always`
  when your setting wouldn't otherwise prompt for the aGiTrack terminal (opt out with
  `agitrack.confirmTerminalClose: false`). Because VSCode bounds how long it waits on
  shutdown, a one-time dialog points you at the reliable path — **`Ctrl-G` → exit** inside
  aGiTrack — which has no time pressure.
- The dashboard is **remote-aware**: on a remote/SSH/Mosh host it no longer tries to
  open a (headless) remote browser — it relies on port forwarding so the URL opens on
  your local machine; `$BROWSER` is honored when set.
- The Ctrl-G dashboard labels the session's fresh, unpushed commits with the current
  user's GitHub ID (which `gh` can't resolve until they're on the remote).
- **Start Session** opens aGiTrack in a VSCode terminal for the workspace — the full
  experience: the agent's native interface, the `Ctrl-G` command menu, sessions,
  sharing, worktrees, and per-turn auto-commits.
- **Open Dashboard** launches aGiTrack's metrics dashboard (`agitrack -d`) in its own
  terminal; read-only, runs alongside a session.
- **Auto-installs the aGiTrack CLI** when it's missing (via `pipx`, falling back to
  `pip --user`), so the extension works on a machine that's never had aGiTrack.
- **Remote-aware:** runs as a workspace extension, so on Remote-SSH / WSL / Dev
  Containers / Codespaces it launches and (if needed) installs aGiTrack on the remote
  host, where the code lives.
- Opens **beside the editor** (split to the right) by default; configurable via
  `agitrack.terminalLocation` (`beside` / `editor` / `panel`).
- Uses the aGiTrack brand mark (the website favicon) as its icon.
- Sets `CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL=1` on the session terminal so the Claude Code
  backend no longer errors trying to auto-install its VSCode companion extension.
- The editor-toolbar button is the only launch button (the status-bar button was removed).
- Re-launching focuses the existing session (aGiTrack allows one per repository);
  **aGiTrack: Restart Session** stops and restarts it.
- aGiTrack's own self-update works normally in the integrated terminal; if the CLI
  updates past the installed extension, the extension prompts you to update it to
  the matching version.
- Settings: `agitrack.path`, `agitrack.backend`, `agitrack.args`,
  `agitrack.openOnStartup`.

## 0.0.1

- Initial chat participant that shelled out to `agitrack --mode json`.
