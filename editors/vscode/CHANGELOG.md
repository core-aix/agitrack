# Changelog

The extension version always tracks the aGiTrack CLI version — they ship in lockstep
(`scripts/sync_vscode_version.py`, enforced in CI), so the extension's version number
matches the `agitrack` release it launches.

## Current

Launch the full aGiTrack terminal application from VSCode — no terminal typing.

- An **aGiTrack menu** at the top-right of the editor toolbar (brand-icon dropdown)
  with **Start Session**, **Restart Session**, and **Open Dashboard**. Also available
  as a status-bar button, Command Palette commands, and an Explorer folder context menu.
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
- Re-launching focuses the existing session (aGiTrack allows one per repository);
  **aGiTrack: Restart Session** stops and restarts it.
- aGiTrack's own self-update works normally in the integrated terminal; if the CLI
  updates past the installed extension, the extension prompts you to update it to
  the matching version.
- Settings: `agitrack.path`, `agitrack.backend`, `agitrack.args`,
  `agitrack.openOnStartup`.

## 0.0.1

- Initial chat participant that shelled out to `agitrack --mode json`.
