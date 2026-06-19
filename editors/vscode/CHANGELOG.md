# Changelog

The extension version always tracks the aGiTrack CLI version — they ship in lockstep
(`scripts/sync_vscode_version.py`, enforced in CI), so the extension's version number
matches the `agitrack` release it launches.

## Current

Launch the full aGiTrack terminal application from VSCode — no terminal typing.

- A brand-icon **aGiTrack button** at the top-right of the editor toolbar that starts
  a session in one click (**aGiTrack: Start aGiTrack**). Restart and Open Dashboard are
  Command Palette commands; Start is also on the Explorer folder context menu.
- **Splitting** an aGiTrack terminal (the editor split button / "Split Terminal") used to
  open a confusing empty shell with no aGiTrack in it. Such a split is now detected (via
  the terminal it was split from) and closed automatically, with a one-line explanation —
  the user's own terminals are never touched.
- **Python venv activation no longer kills aGiTrack.** VSCode's Python extension activates
  a venv by sending `Ctrl-C` then `source .../activate` into the terminal — and that Ctrl-C
  would interrupt and kill aGiTrack at launch (you'd see the privacy prompt get `^C`'d and
  "aGiTrack not started", followed by the venv activating). The extension now disables
  `python.terminal.activateEnvironment` for the brief launch window and restores it after,
  so nothing is injected into aGiTrack. The agent runs with its own interpreter, so the
  shell venv is never needed. Opt out with `agitrack.suppressTerminalEnvActivation: false`.
- Beyond that, after shell integration is ready the launch waits for any remaining startup
  command (conda/direnv, etc.) to finish before starting aGiTrack, and aGiTrack drains any
  pending terminal input right before the sensitive-information prompt as a backstop.
- Exiting aGiTrack (e.g. the `Ctrl-G` → exit menu) **closes the terminal** automatically —
  but **only on a clean exit** (status 0). If aGiTrack quits with an error the terminal
  stays open so its message is readable. (`agitrack … && exit`.)
- Re-launching never restarts a running session: the editor button **focuses the existing
  aGiTrack terminal** instead of starting a second one.
- Closing the window/terminal exits aGiTrack **gracefully**, finalizing the latest turn
  instead of stranding it. On shutdown the extension signals aGiTrack and waits (up to
  60s) for it to finish; it also raises `terminal.integrated.confirmOnKill` to `always`
  when your setting wouldn't otherwise prompt for the aGiTrack terminal (opt out with
  `agitrack.confirmTerminalClose: false`). Because VSCode bounds how long it waits on
  shutdown, a dialog points you at the reliable path — **`Ctrl-G` → exit** inside
  aGiTrack — which has no time pressure. It re-appears once after every extension update
  or reinstall (keyed on the installed version), so the safe-exit habit is re-surfaced
  with each new release rather than shown only on first install.
- **Abrupt-close recovery backstop:** if a whole-window close kills a session before it can
  finalize, deactivate runs a **detached** `agitrack --recover` (it outlives the extension
  host) to finish the job — committing a *finished* turn's changes and merging them (an
  aborted/in-flight turn is left untouched; merges are skipped on conflict). No-ops if
  aGiTrack is still running, so it never races a live session. (Closing just the terminal
  while the window stays open needs no backstop: aGiTrack receives SIGHUP and finalizes
  itself.)
- The dashboard is **remote-aware**: on a remote/SSH/Mosh host it no longer tries to
  open a (headless) remote browser — it relies on port forwarding so the URL opens on
  your local machine; `$BROWSER` is honored when set.
- The Ctrl-G dashboard labels the session's fresh, unpushed commits with the current
  user's GitHub ID (which `gh` can't resolve until they're on the remote).
- **Start aGiTrack** opens aGiTrack in a VSCode terminal for the workspace — the full
  experience: the agent's native interface, the `Ctrl-G` command menu, sessions,
  sharing, worktrees, and per-turn auto-commits.
- **Open Dashboard** launches aGiTrack's metrics dashboard (`agitrack -d`) in its own
  terminal; read-only, runs alongside a session.
- **Auto-installs the aGiTrack CLI** when it's missing (via `pipx`, falling back to
  `pip --user`), so the extension works on a machine that's never had aGiTrack.
- **Remote-aware:** runs as a workspace extension, so on Remote-SSH / WSL / Dev
  Containers / Codespaces it launches and (if needed) installs aGiTrack on the remote
  host, where the code lives.
- **Windows:** aGiTrack is POSIX-only, so on Windows you run it inside **WSL** (or a Dev
  Container / Remote-SSH) — the workspace extension then runs on the Linux side and works
  as on macOS/Linux. In a native Windows window the extension detects this and points you
  to Remote-WSL instead of launching a doomed terminal or offering an install that can't run.
- Opens **beside the editor** (split to the right) by default; configurable via
  `agitrack.terminalLocation` (`beside` / `editor` / `panel`).
- Uses the aGiTrack brand mark (the website favicon) as its icon.
- Sets `CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL=1` on the session terminal so the Claude Code
  backend no longer errors trying to auto-install its VSCode companion extension.
- The editor-toolbar button is the only launch button (the status-bar button was removed).
- Re-launching focuses the existing session (aGiTrack allows one per repository);
  **aGiTrack: Restart aGiTrack** stops and restarts it.
- aGiTrack's own self-update works normally in the integrated terminal; if the CLI
  updates past the installed extension, the extension prompts you to update it to
  the matching version.
- Settings: `agitrack.path`, `agitrack.backend`, `agitrack.args`,
  `agitrack.openOnStartup`, `agitrack.terminalLocation`, `agitrack.confirmTerminalClose`,
  `agitrack.suppressTerminalEnvActivation`.

## 0.0.1

- Initial chat participant that shelled out to `agitrack --mode json`.
