# MSI self-update

> Design doc for issue #124. Tracks the work to make the
> PyInstaller-bundled aGiTrack MSI install update itself in place.
> Implementation has not started yet — this file captures the agreed plan
> so the eventual PRs have a single, reviewable specification to point at.

## Problem

`installer/agitrack.wxs` ships a **PyInstaller one-folder bundle**
(`agitrack.exe` + `_internal/`) as a perMachine MSI. The existing
self-updater in `agitrack/update/updater.py` has no concept of this
shape: it resolves every install to `KIND_SOURCE` (git checkout),
`METHOD_PIP`, `METHOD_PIPX`, or `METHOD_HOMEBREW`, and a frozen
`agitrack.exe` matches none of them.

A bundled install's `Updater` therefore resolves to
`KIND_PACKAGE` → `METHOD_PIP` (the default), and `_apply_package()`
runs `python -m pip install --upgrade agitrack` against the PyInstaller
bootloader. The bootloader has no `pip` module, so the call falls
through to PATH `pip3`/`pip`, which either (a) upgrades an unrelated
site-packages and reports false success, or (b) prints the
`pip / pipx / brew / --break-system-packages` enumeration that does
not apply. Either way, the bundle is never replaced and the user is
stuck on the old version.

The user has to manually re-download the new MSI from the GitHub
release page. Early-phase aGiTrack ships patches and version bumps
frequently, so this gap is real.

## Design

Extend `_install_method()` and the package-apply ladder with a new
`METHOD_MSI` branch. The MSI path:

1. Detects itself via `getattr(sys, "frozen", False)` plus a Win32
   registry read of `HKLM\Software\aGiTrack\InstallDir` (the
   `perMachine` install root written by the WiX fragment).
2. Checks for updates against the **GitHub Releases API**
   (`/repos/{owner}/{repo}/releases/latest`), filtering for the
   `agitrack-*-windows-x64.msi` asset. The version is parsed from the
   asset filename. The repo is discovered via
   `git config --get remote.origin.url` (with `AGITRACK_GH_REPO` as
   an override) — the same trick VS Code uses.
3. Downloads the new MSI to `%TEMP%\agitrack-<ver>-windows-x64.msi`
   with a 600-second total timeout, a 200 MB size cap, and the
   GitHub-provided digest verified.
4. Hands the install off via a tiny Windows-specific bootstrapper
   (`installer/agitrack-update.cmd`, committed, harvested into the
   MSI). The running TUI **must exit first** because the MSI replaces
   the very `agitrack.exe` that is running. The handoff is:

   - TUI finalises every session (existing teardown path).
   - TUI exits and the runner calls
     `ShellExecuteExW(... lpVerb="runas", lpFile="cmd.exe",
      lpParameters='/c "<INSTALLDIR>\agitrack-update.cmd" "<downloaded msi>" "%LOCALAPPDATA%\aGiTrack\last-args.txt"')`.
   - UAC prompts; user clicks Yes.
   - The elevated bootstrapper runs
     `msiexec /i <msi> /passive /norestart REINSTALL=ALL REINSTALLMODE=vomus`,
     then re-launches `agitrack.exe <original argv>`.

The bootstrapper pattern is the only one that works for perMachine
MSI installs (VS Code, Slack, GitHub Desktop all use it for the same
reason). `<MajorUpgrade>` already in the WiX source prevents
downgrades, so the version check the updater does *before* invoking
msiexec is purely an optimisation to avoid a UAC dialog for a no-op.

## Why this shape

- **GitHub Releases API** — the release CI already attaches the MSI
  there. No new infra, no new account, no new secret. A 5-minute
  in-process ETag cache keeps us under the 60 req/h anonymous rate
  limit even with the periodic check firing.
- **`runas` self-elevation** — the only way to get a UAC prompt from
  an un-elevated process that is also the only Windows-native way
  that doesn't require a separate bootstrapper process to be running
  as admin from the start.
- **Bootstrapper in the install dir** — the TUI is the very process
  that is about to be replaced; we cannot hand off from inside it.
  The bootstrapper lives in `Program Files\aGiTrack\agitrack-update.cmd`
  so it is updated by the same MSI that updates the rest of the
  install.
- **`last-args.txt` in `%LOCALAPPDATA%`** — per-user, no UAC to
  write, survives reboots. The runner writes `sys.argv[1:]` once
  during startup (gated on `sys.frozen`); the bootstrapper reads it
  and re-launches with the same flags (`--repo`, `--backend`,
  `--no-worktree`, etc.).

## Reused mechanics (no change needed)

- `Updater.check()` and `Updater.apply()` are the single entry
  points the runner, the CLI startup prompt, and the Ctrl-G `update`
  command all use. Adding `METHOD_MSI` to `_install_method()` and a
  corresponding `_apply_msi()` makes every entry point get the new
  path for free.
- The startup `_check_for_update_at_startup` (`cli.py:596`) is
  already gated by `check_for_updates` config + TTY + non-scripted,
  and it already calls `Updater.check()`. Zero changes.
- The periodic in-session check
  (`runner._maybe_check_for_update`) already calls `Updater.check()`
  on a worker thread. Zero changes.
- `_ready_for_update()` already gates on "no turn in flight, no
  pending parse/merge/summary, no running background session". This
  matters for the MSI path too — replacing `agitrack.exe` mid-run
  would orphan the agent. Zero changes.
- `_finalize_pending_work()` already runs before the existing
  `restart_agitrack()`; the MSI branch reuses it.
- `_handle_exit_signal` already short-circuits while
  `self._update_applying` is set; the MSI branch reuses the same
  flag to keep SIGHUP from killing the bootstrapper mid-install.
- The single-writer lock at `<repo>/.agitrack/lock` is per-repo, not
  per-install — out of scope.
- The VSCode extension's `checkVersionParity` (`extension.ts:1180`)
  already works against the MSI's `agitrack.exe --version`. Zero
  changes.

## File-by-file

1. **`installer/agitrack-update.cmd`** (new, committed) — the
   bootstrapper. ~30 lines: `msiexec /i <msi> /passive /norestart
   REINSTALL=ALL REINSTALLMODE=vomus`, on success `agitrack.exe
   <args from last-args.txt>`, propagate exit code. One extra
   `echo` line documents the SmartScreen "More info → Run anyway"
   path so first-time users aren't stuck.
2. **`installer/agitrack.wxs`** — add a `ComponentGroup
   Id="UpdaterComponents"` referencing the harvested bootstrapper
   and reference it from `<Feature Id="Main">`.
3. **`agitrack/update/updater.py`** — add `METHOD_MSI` constant;
   extend `_install_method()` with a `sys.frozen` + registry check
   (gated on Windows; raises on POSIX is fine because the
   `getattr(sys, "frozen", False)` short-circuits); add
   `_check_msi()` that calls the GitHub API and parses the asset
   filename for the version; add `_apply_msi()` that downloads to
   `%TEMP%` and stashes the path on `self._pending_msi_path`;
   route the package check/apply to the MSI branch when applicable;
   extend `manual_update_instructions()` with the MSI route. Reuse
   the existing `pending_manual_update` global-config flag for the
   "user clicked No on UAC" failure case so the next startup shows a
   single reminder.
4. **`agitrack/proxy/runner.py`** — in `run()` very early, when
   `getattr(sys, "frozen", False)` is True, write `sys.argv[1:]` to
   `%LOCALAPPDATA%\aGiTrack\last-args.txt`. Extend
   `_apply_update_and_restart()` with a branch that calls a new
   `_launch_msi_bootstrapper()` instead of `restart_agitrack()`
   when an MSI update is pending; the bootstrapper handles the
   re-launch.
5. **`agitrack/proc.py`** — add `shell_execute_runas(command_line)
   -> int` wrapping `ShellExecuteExW` with `lpVerb="runas"`.
   Windows-only; raises `NotImplementedError` off-Windows.
6. **`tests/test_updater.py`** — eight new tests, all injectable
   (monkey-patched GitHub HTTP, monkey-patched registry reader):
   MSI detection (frozen + registry), non-MSI when not frozen,
   `_check_msi` newer/equal/api-error, `_apply_msi` downloads and
   stores path, `_apply_msi` failure surfaces manual instructions,
   `manual_update_instructions` mentions the MSI route.
7. **`tests/test_windows_platform.py`** — one new
   `skipif sys.platform != "win32"` test for the real
   `ShellExecuteExW` round-trip.
8. **`AGENTS.md`** Self-Update section — append a paragraph on the
   MSI path, the GitHub Releases source, the `runas` handoff, the
   bootstrapper, and the SmartScreen note.
9. **`README.md`** Self-Update section — one-sentence addendum.

## Edge cases

- **UAC refused** — `ShellExecuteExW` returns
  `SE_ERR_ACCESSDENIED`; bootstrapper never runs. Surface a
  one-liner, set `pending_manual_update` so the next startup
  reminds the user, suppress the in-session notice for that
  version.
- **GitHub API rate-limited** — 60 req/h anonymous, well above
  our 1/5-minute cadence; 5-minute ETag cache amortises; on 403
  report "could not check" and continue, same as today.
- **SmartScreen on unsigned MSI** — documented; bootstrapper log
  line tells the user to click "More info → Run anyway".
- **Download corruption** — `msiexec` refuses, Windows Installer
  rolls back, bootstrapper exits non-zero, `pending_manual_update`
  set.
- **Two aGiTrack instances both updating** — `<MajorUpgrade>`
  prevents two concurrent msiexec installs; first to win the UAC
  dialog wins. Same behaviour as VS Code.
- **Update interrupted mid-install** — msiexec is transactional,
  rollback is automatic; SIGHUP during download only loses the
  temp file. The "interrupted update must never leave aGiTrack
  uninstalled" rule from `AGENTS.md` continues to hold.
- **Updating a non-MSI PyInstaller bundle** — `_install_method()`
  only returns `METHOD_MSI` when *both* `sys.frozen` is True *and*
  the registry install-dir key exists. Portable PyInstaller zips
  fall through to `METHOD_PIP` (a separate non-goal).
- **Source checkout on Windows** — `sys.frozen` is False in a
  source checkout, so it falls through to the source path.
  No regression.

## Out of scope

- **Code-signing the MSI** (needs a `signtool` step and a cert
  secret in the release CI). Bootstrapper log line covers the
  unsigned SmartScreen path; a follow-up PR can wire
  `signtool sign /fd sha256 /tr http://timestamp.digicert.com
  /td sha256 /f <cert> /p <pass> <msi>` into the release workflow
  gated by a `WINDOWS_CODESIGN_PFX` repo secret.
- **Delta / MSP patches** — ~25 MB MSI is acceptable; updates are
  rare; a future `msp` could ship a 200 KB diff if the cadence
  changes.
- **Stable vs beta channels** — only `v*` tags ship MSIs; if a
  `v0.2.0-beta1` tag is ever added, a 2-line `if "beta" in
  release.tag_name: skip` filter is the fix.
- **A new auto-update preference UI** — the existing
  `check_for_updates: false` config flag works for all install
  methods.

## Verification

- Unit tests in `test_updater.py` and `test_windows_platform.py`
  (POSIX runs the GitHub HTTP / registry / `Updater` logic via
  monkey-patching; one gated Windows test for the real
  `ShellExecuteExW` round-trip).
- Integration test (Windows CI, `windows-latest` job): build the
  MSI in one job, install it via `msiexec /quiet`, run
  `agitrack --version`, mock the GitHub release to advertise a
  newer version, run `agitrack update` non-interactively, assert
  exit-code-zero and the new `--version`. UAC is bypassed in CI
  runners that have admin tokens.
- Manual smoke: install the MSI locally, observe startup update
  prompt, accept, see UAC, accept, observe new version, verify
  `last-args.txt` is honoured (`--backend opencode --no-worktree`
  survives the update).

## References

- Issue #124 — msi vs. pip installation for Windows.
- `AGENTS.md` Self-Update section — the standing contract this
  plan extends.
- `installer/agitrack.wxs` — the WiX v4 source the bootstrapper
  plugs into.
- `agitrack/update/updater.py` — the updater the `METHOD_MSI`
  branch is added to.
- `.github/workflows/release.yml` — the CI that already attaches
  the MSI to the GitHub release; no change needed there.
