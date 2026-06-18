# Changelog

The extension version always tracks the aGiTrack CLI version — they ship in lockstep
(`scripts/sync_vscode_version.py`, enforced in CI), so the extension's version number
matches the `agitrack` release it launches.

## Current

Launch the full aGiTrack terminal application from VSCode — no terminal typing.

- **aGiTrack: Start Session** (status-bar button, Command Palette, or Explorer
  folder context menu) opens aGiTrack in a VSCode integrated terminal for the
  workspace. You get the complete experience: the agent's native interface, the
  `Ctrl-G` command menu, sessions, sharing, worktrees, and per-turn auto-commits.
- Re-launching focuses the existing session (aGiTrack allows one per repository);
  **aGiTrack: Restart Session** stops and restarts it.
- aGiTrack's own self-update works normally in the integrated terminal; if the CLI
  updates past the installed extension, the extension prompts you to update it to
  the matching version.
- Settings: `agitrack.path`, `agitrack.backend`, `agitrack.args`,
  `agitrack.openOnStartup`.

## 0.0.1

- Initial chat participant that shelled out to `agitrack --mode json`.
