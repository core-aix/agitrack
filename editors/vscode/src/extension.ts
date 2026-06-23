// aGiTrack VSCode extension.
//
// A thin wrapper that lets you install aGiTrack as a VSCode plugin and launch the
// full aGiTrack terminal application (proxy mode) inside VSCode — without opening a
// terminal and typing `agitrack` yourself. Because it runs the real CLI in an
// integrated terminal, you get the complete aGiTrack experience (the backend's
// native TUI, the Ctrl-G command menu, sessions, sharing, worktrees, auto-commits —
// everything), just started from a VSCode command, the editor menu, or the status bar.
//
// The aGiTrack CLI is a Python package, so it can't be bundled in the extension; if
// it isn't installed, the extension offers to install it for you with pipx/pip.
//
// The extension is declared `extensionKind: ["workspace"]`, so in a Remote-SSH / WSL /
// Dev Container / Codespaces window it runs on the *remote* host — the same machine as
// the code. The terminal it opens, the `agitrack` it runs, and the on-demand install
// therefore all happen where the repository lives, which is exactly where aGiTrack must
// run. Locally (no remote) the workspace host is the local machine, so it just works.

import * as vscode from "vscode";
import { execFile, spawn } from "child_process";
import { readdirSync, readFileSync } from "fs";
import { homedir } from "os";
import { join } from "path";

import { dedupe, staticExeCandidates } from "./installPaths";
import { sessionLooksLive } from "./liveness";
import { isNativeWindows } from "./platform";

const TERMINAL_NAME = "aGiTrack";

// Held so module-level functions (startSession's one-time tip, deactivate's graceful
// shutdown) can reach globalState; set in activate().
let extensionContext: vscode.ExtensionContext | undefined;

// One aGiTrack terminal per workspace folder, keyed by folder path, so re-running
// the launch command focuses the existing session instead of starting a second
// instance (which aGiTrack would refuse with its repo lock).
const terminals = new Map<string, vscode.Terminal>();
// Dashboard terminals are tracked separately: the dashboard is read-only (no repo
// lock), so it can run alongside a session.
const dashboards = new Map<string, vscode.Terminal>();

// When we launched aGiTrack in each session terminal, so the reuse check gives a new
// session time to come up before judging it dead (see sessionStillRunning).
const launchedAt = new WeakMap<vscode.Terminal, number>();
const SESSION_STARTUP_GRACE_MS = 15_000;
// How long to let a VSCode-revived terminal settle (its shell reconnect, its child reattach)
// before probing whether aGiTrack is still running in it, so the reconcile verdict is accurate.
const RESTORE_SETTLE_MS = 1_500;

// Terminals we created, so the split-terminal handler never disposes our own
// terminals (it fires for every opened terminal, including ours).
const ourTerminals = new WeakSet<vscode.Terminal>();

// Folders whose session launch is in flight. startSession is async and has several
// awaits between deciding to launch and recording the terminal, so two rapid clicks of
// the aG button could both pass the "already running?" check and each create a terminal.
// A synchronous guard keyed by folder path closes that window: the second click bails.
const launching = new Set<string>();

export function activate(context: vscode.ExtensionContext): void {
  extensionContext = context;
  context.subscriptions.push(
    vscode.commands.registerCommand("agitrack.start", () => startSession()),
    vscode.commands.registerCommand("agitrack.startHere", (uri?: vscode.Uri) => startSession(uri)),
    vscode.commands.registerCommand("agitrack.restart", () => restartSession()),
    vscode.commands.registerCommand("agitrack.dashboard", (uri?: vscode.Uri) => openDashboard(uri)),
    vscode.commands.registerCommand("agitrack.install", () => installAgitrack()),
  );

  // Forget terminals the user closes so the next launch starts a fresh one. We do
  // NOT run recovery here: while the window is open, closing a session's terminal
  // delivers SIGHUP and aGiTrack finalizes itself (it has time — the host isn't
  // going away). Recovery is only a backstop for a whole-window close, run detached
  // from deactivate(); running it on every terminal close would grab the repo lock
  // and block an immediate relaunch.
  context.subscriptions.push(
    vscode.window.onDidCloseTerminal((closed) => {
      for (const map of [terminals, dashboards]) {
        for (const [key, terminal] of map) {
          if (terminal === closed) {
            map.delete(key);
          }
        }
      }
    }),
  );

  // Splitting an aGiTrack terminal (the editor's split button, or "Split Terminal")
  // opens a bare shell with no aGiTrack in it — confusing. Close such a split and say
  // why. Detection is precise: VSCode reports the terminal a split was created from in
  // creationOptions.location.parentTerminal, so this only ever fires for terminals split
  // off one WE created — never the user's own new terminals.
  context.subscriptions.push(
    vscode.window.onDidOpenTerminal((opened) => {
      if (ourTerminals.has(opened)) {
        return; // a terminal we launched — never dispose our own
      }
      const parent = splitParentOf(opened);
      if (parent && isAgitrackTerminal(parent)) {
        opened.dispose();
        void vscode.window.showInformationMessage(
          "aGiTrack runs one session per repository, so splitting its terminal just opens an " +
            "empty shell — that split was closed. Use the existing aGiTrack terminal (or the " +
            "aGiTrack button to start a session in another folder).",
        );
        return;
      }
      // A non-split terminal we don't own that's named like an aGiTrack session — almost
      // always one VSCode revived from a previous window. Reconcile it once it settles (see
      // reconcileRestoredTerminal): adopt it if aGiTrack is still running, close it if not.
      if (!parent && isRestoredSessionTerminal(opened)) {
        setTimeout(() => void reconcileRestoredTerminal(opened), RESTORE_SETTLE_MS);
      }
    }),
  );

  // VSCode revives persisted terminals from the previous window asynchronously after
  // activation. Reconcile any already present (adopt a still-running aGiTrack so the button
  // focuses it; close a leftover bare shell), then honour openOnStartup — by which point an
  // adopted session is known and gets focused rather than duplicated.
  void reconcileRestoredTerminals(RESTORE_SETTLE_MS).then(() => {
    if (vscode.workspace.getConfiguration("agitrack").get<boolean>("openOnStartup")) {
      void startSession();
    }
  });

  void bootstrap(context);
}

export function deactivate(): Thenable<void> {
  // VSCode awaits this promise during shutdown (window close, reload, extension
  // disable), so we use it to give every running aGiTrack a chance to exit
  // gracefully — finalize and commit the in-flight turn — instead of being
  // hard-killed when the pty is torn down. We send aGiTrack SIGTERM (its handler
  // finalizes pending work, then exits) and wait for the process to actually
  // disappear, up to a generous budget; then, as a backstop, kick off a DETACHED
  // `agitrack --recover` that outlives this extension host and finishes any work
  // the graceful exit couldn't (it no-ops if aGiTrack already exited cleanly).
  return shutdownSessionsGracefully(60_000);
}

/** Signal each running aGiTrack session to exit, wait (up to `timeoutMs` each) for it
 * to finish finalizing, then spawn a detached recovery as a backstop. Dashboards are
 * read-only and need no graceful exit, so they are torn down with the window. */
async function shutdownSessionsGracefully(timeoutMs: number): Promise<void> {
  await Promise.all(
    [...terminals.keys()].map(async (folderPath) => {
      await signalAndWait(folderPath, timeoutMs);
      runRecovery(folderPath, { detached: true });
    }),
  );
}

/** Run `agitrack --recover` for a workspace folder to finalize work an abrupt close
 * left behind — commit a finished turn and merge it (a no-op if a live session still
 * holds the repo lock). Detached + unref'd on window close so it outlives the extension
 * host; foreground otherwise. Best-effort: failures (e.g. CLI not installed) are ignored. */
function runRecovery(folderPath: string, opts: { detached: boolean }): void {
  try {
    const child = spawn(configuredExe(), ["--repo", folderPath, "--recover"], {
      detached: opts.detached,
      stdio: "ignore",
    });
    child.on("error", () => undefined); // not installed / not runnable — nothing to do
    if (opts.detached) {
      child.unref();
    }
  } catch {
    // ignore — recovery is best-effort
  }
}

async function signalAndWait(folderPath: string, timeoutMs: number): Promise<void> {
  // aGiTrack runs as a child of the shell, so the terminal's processId is the shell,
  // not aGiTrack. Read aGiTrack's own PID from the repo lock file it writes while
  // running (.agitrack/lock). A clean exit truncates that file, so an empty/missing
  // pid means there's nothing to signal — it already finished.
  const pid = readAgitrackPid(folderPath);
  if (!pid) {
    return;
  }
  try {
    process.kill(pid, "SIGTERM"); // aGiTrack's SIGTERM handler finalizes the turn, then exits
  } catch {
    return; // already gone
  }
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!isAlive(pid)) {
      return; // exited cleanly
    }
    await delay(150);
  }
}

/** aGiTrack's PID from the repo lock file it holds while running, or undefined when no
 * session is running there (file missing, or truncated to empty by a clean exit). */
function readAgitrackPid(folderPath: string): number | undefined {
  try {
    const info = JSON.parse(readFileSync(join(folderPath, ".agitrack", "lock"), "utf8"));
    return typeof info?.pid === "number" ? info.pid : undefined;
  } catch {
    return undefined; // no lock / empty / unreadable
  }
}

/** The terminal a newly-opened terminal was split from, if any. VSCode records it in
 * creationOptions.location as a TerminalSplitLocationOptions ({ parentTerminal }); for a
 * non-split terminal the location is a plain enum/editor-location with no parent. */
function splitParentOf(terminal: vscode.Terminal): vscode.Terminal | undefined {
  const location = (terminal.creationOptions as vscode.TerminalOptions | undefined)?.location;
  if (location && typeof location === "object" && "parentTerminal" in location) {
    return location.parentTerminal;
  }
  return undefined;
}

/** True if `terminal` is an aGiTrack session or dashboard terminal we created. */
function isAgitrackTerminal(terminal: vscode.Terminal): boolean {
  for (const map of [terminals, dashboards]) {
    for (const tracked of map.values()) {
      if (tracked === terminal) {
        return true;
      }
    }
  }
  return false;
}

/** True while `pid` exists; signal 0 only probes (it sends nothing) and throws once
 * the process is gone. */
function isAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/** Explain that aGiTrack needs WSL on Windows, with a link to set it up. Returns true if
 * we're on native Windows (caller should stop), false otherwise. */
async function blockOnNativeWindows(): Promise<boolean> {
  if (!isNativeWindows()) {
    return false;
  }
  const choice = await vscode.window.showWarningMessage(
    "aGiTrack needs a POSIX environment and can't run on native Windows. Open your project " +
      "in WSL (or a Dev Container / Remote-SSH) and start aGiTrack there — this extension then " +
      "runs on the Linux side automatically.",
    "Set up WSL",
  );
  if (choice === "Set up WSL") {
    void vscode.env.openExternal(vscode.Uri.parse("https://code.visualstudio.com/docs/remote/wsl"));
  }
  return true;
}

/** First-run housekeeping: if the CLI is present, check version parity; if it's
 * missing, offer to install it so the extension is usable out of the box. */
async function bootstrap(context: vscode.ExtensionContext): Promise<void> {
  if (isNativeWindows()) {
    return; // can't run here — stay silent on activation; we explain when they try to start
  }
  const exe = configuredExe();
  if (await runnable(exe)) {
    await checkVersionParity(context, exe);
    return;
  }
  // Not installed — offer to install (non-modal so opening a workspace isn't intrusive).
  const choice = await vscode.window.showInformationMessage(
    "aGiTrack CLI isn't installed. Install it now so this extension can run it?",
    "Install aGiTrack",
    "Not now",
  );
  if (choice === "Install aGiTrack") {
    await installAgitrack();
  }
}

/** Launch (or focus) aGiTrack in a terminal for the chosen workspace folder. */
async function startSession(targetUri?: vscode.Uri): Promise<void> {
  if (await blockOnNativeWindows()) {
    return;
  }
  const folder = await pickFolder(targetUri);
  if (!folder) {
    void vscode.window.showWarningMessage("aGiTrack: open a folder or repository first.");
    return;
  }

  const key = folder.uri.fsPath;
  // Reserve the folder synchronously before any await, so a second rapid click can't slip
  // past the checks below and open a duplicate terminal (a focus/no-op is the right
  // response to "it's already starting").
  if (launching.has(key)) {
    terminals.get(key)?.show();
    return;
  }
  launching.add(key);
  try {
    const existing = terminals.get(key);
    if (existing) {
      if (await sessionStillRunning(existing, key)) {
        existing.show();
        return; // a RUNNING aGiTrack is here — bring it forward, never restart it.
      }
      // Tracked terminal, but aGiTrack is no longer running in it (e.g. a non-zero exit
      // left the shell open). Discard the dead terminal and start a fresh session, so the
      // aG button doesn't just keep re-focusing a lingering shell.
      terminals.delete(key);
      existing.dispose();
    }

    const exe = await ensureCliAvailable();
    if (!exe) {
      return; // not installed and not installed-on-demand
    }

    await ensureCloseConfirmation();
    void maybeShowGracefulExitTip();
    // Disable VSCode's Python venv activation BEFORE creating the terminal, so the Python
    // extension doesn't send a process-killing Ctrl-C + `source activate` into aGiTrack.
    await suppressPythonEnvActivationForLaunch();

    // Run aGiTrack inside the shell, but only AFTER the shell has finished its own startup
    // — including any commands VSCode injects automatically (the Python extension's venv
    // activation, conda init, shell integration). Those must run in the shell first;
    // otherwise they get typed into aGiTrack's stdin (e.g. `source .venv/bin/activate`
    // landing in the agent, or a stray newline auto-answering the privacy prompt).
    // spawnAgitrackTerminal sequences the launch via shell integration to guarantee this.
    //
    // `&& exit` closes the terminal ONLY when aGiTrack exits successfully (status 0, e.g.
    // Ctrl-G → exit). On a non-zero/error exit the `&& exit` is skipped, so the shell
    // stays open with aGiTrack's error message still on screen for the user to read.
    // aGiTrack runs as a child of the shell (not `exec`-ed), so it's a real child process
    // VSCode can see — which also makes the close-confirmation prompt fire consistently.
    const terminal = spawnAgitrackTerminal({
      name: terminals.size === 0 ? TERMINAL_NAME : `${TERMINAL_NAME} (${folder.name})`,
      cwd: folder.uri.fsPath,
      icon: "git-commit",
      command: `${launchCommand(exe)} && exit`,
    });
    launchedAt.set(terminal, Date.now());
    terminals.set(key, terminal);
    terminal.show();
  } finally {
    launching.delete(key);
  }
}

/** Whether `terminal` still has a RUNNING aGiTrack session (so the aG button focuses it
 * instead of starting a second one). Map presence alone isn't enough: a non-zero aGiTrack
 * exit leaves the shell open. We confirm via the combined signals in `sessionLooksLive`.
 * The lock is taken only after the startup privacy prompt, so a session waiting at that
 * prompt holds no lock yet but its shell still has the aGiTrack child — hence the child
 * check covers it. */
async function sessionStillRunning(terminal: vscode.Terminal, folderPath: string): Promise<boolean> {
  const pid = readAgitrackPid(folderPath);
  return sessionLooksLive({
    shellExited: terminal.exitStatus !== undefined,
    msSinceLaunch: Date.now() - (launchedAt.get(terminal) ?? 0),
    lockAlive: pid !== undefined && isAlive(pid),
    shellHasChild: await shellHasChild(terminal),
    graceMs: SESSION_STARTUP_GRACE_MS,
  });
}

/** True if the terminal's shell process currently has any child — i.e. aGiTrack (run as
 * `agitrack && exit`) is still executing in it. An idle interactive shell at a prompt
 * (the state after a non-zero exit) has no children. POSIX-only, like aGiTrack itself. */
async function shellHasChild(terminal: vscode.Terminal): Promise<boolean> {
  const shellPid = await terminal.processId;
  if (!shellPid) {
    return false;
  }
  try {
    const out = await execCapture("pgrep", ["-P", String(shellPid)], 2_000);
    return out.trim().length > 0;
  } catch {
    return false; // pgrep exits non-zero when the shell has no children
  }
}

/** Reconcile aGiTrack SESSION terminals VSCode revived from a previous window (those we
 * didn't create this run): adopt one that still has aGiTrack running — a reload that kept
 * its pty — so the aG button focuses it, and close one whose aGiTrack is gone — a restart
 * left a bare shell — so it isn't mistaken for a live session and the user can start fresh.
 * Dashboards (read-only) are left alone. `delayMs` lets revival settle before probing. */
async function reconcileRestoredTerminals(delayMs = 0): Promise<void> {
  if (delayMs > 0) {
    await delay(delayMs);
  }
  await Promise.all(vscode.window.terminals.map((terminal) => reconcileRestoredTerminal(terminal)));
}

async function reconcileRestoredTerminal(terminal: vscode.Terminal): Promise<void> {
  if (ourTerminals.has(terminal) || !isRestoredSessionTerminal(terminal)) {
    return; // created this run, or not one of our session terminals (by name)
  }
  const key = folderKeyForTerminal(terminal);
  const pid = key ? readAgitrackPid(key) : undefined;
  const shellPid = await terminal.processId;
  // aGiTrack is LIVE IN THIS TERMINAL only if the lock-holder is alive AND a descendant of
  // this terminal's own shell. The repo lock alone is NOT enough: it is folder-global, so it
  // can be held by the detached `agitrack --recover` we spawn on window close, or matched by a
  // stale/reused PID — neither of which means aGiTrack is running in this revived shell. That
  // false positive is exactly what left an idle leftover terminal around.
  const lockHeld = pid !== undefined && isAlive(pid);
  const liveHere = lockHeld && shellPid !== undefined && (await isUnderShell(pid as number, shellPid));
  if (liveHere) {
    if (key && !terminals.has(key)) {
      terminals.set(key, terminal);
      launchedAt.set(terminal, Date.now());
      ourTerminals.add(terminal);
    }
    return; // a live aGiTrack is running in this terminal — adopt it so the aG button focuses it
  }
  // A leftover aGiTrack terminal with no session in it: VSCode was closed/reloaded without
  // exiting aGiTrack (a clean Ctrl-G → exit closes its terminal, so it is never restored). The
  // revived tab is just a dead shell showing old scrollback — close it.
  terminal.dispose();
  // Relaunch only when the repo lock is actually free, so the session resumes in a fresh
  // terminal. If the lock is still held elsewhere (a recovery finalizing the last turn), don't
  // relaunch — it would be refused as "already running"; the user can press aG once it frees.
  if (!lockHeld) {
    const folder = key ? vscode.workspace.workspaceFolders?.find((f) => f.uri.fsPath === key) : undefined;
    if (folder) {
      void startSession(folder.uri);
    }
  }
}

/** Parent PID of `pid`, or undefined if it can't be read (process gone / `ps` unavailable). */
async function parentPid(pid: number): Promise<number | undefined> {
  try {
    const ppid = parseInt((await execCapture("ps", ["-o", "ppid=", "-p", String(pid)], 2_000)).trim(), 10);
    return Number.isFinite(ppid) ? ppid : undefined;
  } catch {
    return undefined;
  }
}

/** Whether `pid` is `shellPid` or a descendant of it (walking the parent chain, bounded). Tells
 * an aGiTrack actually running in THIS terminal apart from one whose folder-global lock is held
 * by an unrelated process (e.g. the detached `agitrack --recover` spawned on window close). */
async function isUnderShell(pid: number, shellPid: number): Promise<boolean> {
  let cur: number | undefined = pid;
  for (let depth = 0; depth < 12 && cur !== undefined && cur > 1; depth++) {
    if (cur === shellPid) {
      return true;
    }
    cur = await parentPid(cur);
  }
  return cur === shellPid;
}

/** Whether `terminal`'s name marks it as an aGiTrack SESSION terminal (not a dashboard) —
 * e.g. one revived across a window reopen. */
function isRestoredSessionTerminal(terminal: vscode.Terminal): boolean {
  const name = terminal.name;
  if (name.startsWith(`${TERMINAL_NAME} Dashboard`)) {
    return false; // dashboards are read-only; never auto-close them
  }
  return name === TERMINAL_NAME || name.startsWith(`${TERMINAL_NAME} (`);
}

/** Best-effort map from a session terminal's name back to its workspace folder path: the
 * un-suffixed "aGiTrack" is the first folder; "aGiTrack (name)" matches by folder name. */
function folderKeyForTerminal(terminal: vscode.Terminal): string | undefined {
  const folders = vscode.workspace.workspaceFolders ?? [];
  if (terminal.name === TERMINAL_NAME) {
    return folders[0]?.uri.fsPath;
  }
  const prefix = `${TERMINAL_NAME} (`;
  if (terminal.name.startsWith(prefix) && terminal.name.endsWith(")")) {
    const folderName = terminal.name.slice(prefix.length, -1);
    return folders.find((folder) => folder.name === folderName)?.uri.fsPath;
  }
  return undefined;
}

/** Once per installed version, tell the user the only reliable way to exit aGiTrack —
 * Ctrl-G → exit — so the in-flight turn is committed and merged. We key the "seen" flag on
 * the extension version, so the tip re-appears after every update or reinstall (a fresh
 * version bump is a good moment to re-surface the safe-exit habit). Closing the terminal or
 * window can leave the latest turn unfinalized, so we deliberately do NOT present that as a
 * graceful alternative — the user is directed to exit through the menu only. */
async function maybeShowGracefulExitTip(): Promise<void> {
  const KEY = "agitrack.gracefulExitTipShownVersion";
  const state = extensionContext?.globalState;
  if (!state) {
    return;
  }
  const version = String(extensionContext?.extension.packageJSON.version ?? "");
  if (state.get<string>(KEY) === version) {
    return; // already shown for this installed version
  }
  await state.update(KEY, version); // remember first, so an immediate close won't re-show it
  void vscode.window.showInformationMessage(
    "To exit aGiTrack, always use the Ctrl-G menu → exit inside aGiTrack — that is what " +
      "commits and merges your latest turn. Closing the terminal or window may leave your " +
      "most recent work unfinalized, so exit through the menu rather than closing the terminal.",
    { modal: true },
    "Got it",
  );
}

// Track an in-flight env-activation suppression so overlapping launches don't clobber
// the saved prior value or restore it early.
let envSuppressionActive = false;
let envSuppressionPriorGlobal: boolean | undefined;

/** Stop VSCode's Python extension from activating a venv/conda env in the aGiTrack
 * terminal. It does so by sending `Ctrl-C` then `source .../activate`, and that Ctrl-C
 * kills aGiTrack at launch (the agent uses its own interpreter, so the shell venv is
 * never needed). We turn `python.terminal.activateEnvironment` off for the brief launch
 * window, then restore the prior value so other terminals are unaffected. Best-effort and
 * gated on `agitrack.suppressTerminalEnvActivation`. */
async function suppressPythonEnvActivationForLaunch(): Promise<void> {
  if (!vscode.workspace.getConfiguration("agitrack").get<boolean>("suppressTerminalEnvActivation", true)) {
    return;
  }
  if (!vscode.extensions.getExtension("ms-python.python")) {
    return; // no Python extension installed — nothing activates the env
  }
  const py = vscode.workspace.getConfiguration("python.terminal");
  if (py.get<boolean>("activateEnvironment", true) === false) {
    return; // already disabled — nothing to do
  }
  if (envSuppressionActive) {
    return; // a prior launch already disabled it; its pending restore will re-enable
  }
  envSuppressionActive = true;
  envSuppressionPriorGlobal = py.inspect<boolean>("activateEnvironment")?.globalValue;
  await py.update("activateEnvironment", false, vscode.ConfigurationTarget.Global);
  // Restore once the Python extension's activation window for our terminal has passed,
  // so terminals opened later still get the user's normal behaviour.
  setTimeout(() => {
    void vscode.workspace
      .getConfiguration("python.terminal")
      .update("activateEnvironment", envSuppressionPriorGlobal, vscode.ConfigurationTarget.Global)
      .then(
        () => {
          envSuppressionActive = false;
        },
        () => {
          envSuppressionActive = false;
        },
      );
  }, 10_000);
}

/** Make sure closing the aGiTrack terminal is confirmed (so aGiTrack can exit
 * gracefully and finalize the latest turn) rather than killed outright. VSCode gives
 * no per-terminal close hook, so we lean on `terminal.integrated.confirmOnKill`: if its
 * current value wouldn't prompt for the terminal we open, raise it to `always`. */
async function ensureCloseConfirmation(): Promise<void> {
  const cfg = vscode.workspace.getConfiguration("agitrack");
  if (!cfg.get<boolean>("confirmTerminalClose", true)) {
    return;
  }
  const termCfg = vscode.workspace.getConfiguration("terminal.integrated");
  const current = termCfg.get<string>("confirmOnKill") || "editor";
  const inEditor = (cfg.get<string>("terminalLocation") || "beside") !== "panel";
  // Editor-area terminals prompt on "editor" or "always"; panel terminals only on
  // "always". If ours wouldn't prompt, restore confirmation by raising it to "always".
  const willPrompt = current === "always" || (inEditor && current === "editor");
  if (!willPrompt) {
    await termCfg.update("confirmOnKill", "always", vscode.ConfigurationTarget.Global);
  }
}

/** Stop the workspace's aGiTrack terminal (if any) and start a fresh one. */
async function restartSession(): Promise<void> {
  const folder = await pickFolder();
  if (!folder) {
    return;
  }
  terminals.get(folder.uri.fsPath)?.dispose();
  terminals.delete(folder.uri.fsPath);
  // Give aGiTrack a moment to release its repo lock before relaunching.
  await new Promise((resolve) => setTimeout(resolve, 300));
  await startSession(folder.uri);
}

/** Open aGiTrack's metrics dashboard for the chosen folder (read-only; `agitrack -d`
 * serves it on localhost and opens the browser, Ctrl-C in the terminal to stop). */
async function openDashboard(targetUri?: vscode.Uri): Promise<void> {
  if (await blockOnNativeWindows()) {
    return;
  }
  const folder = await pickFolder(targetUri);
  if (!folder) {
    void vscode.window.showWarningMessage("aGiTrack: open a folder or repository first.");
    return;
  }

  const key = folder.uri.fsPath;
  const existing = dashboards.get(key);
  if (existing) {
    existing.show();
    return; // a dashboard is already serving for this folder
  }

  const exe = await ensureCliAvailable();
  if (!exe) {
    return;
  }

  const terminal = spawnAgitrackTerminal({
    name: dashboards.size === 0 ? `${TERMINAL_NAME} Dashboard` : `${TERMINAL_NAME} Dashboard (${folder.name})`,
    cwd: folder.uri.fsPath,
    icon: "graph",
    command: `${quote(exe)} --repo ${quote(folder.uri.fsPath)} --dashboard`,
  });
  dashboards.set(key, terminal);
  terminal.show();
}

/** Create a shell terminal and run `command` in it once the shell is ready (so any
 * commands VSCode injects at startup run first). */
function spawnAgitrackTerminal(opts: { name: string; cwd: string; icon: string; command: string }): vscode.Terminal {
  const terminal = vscode.window.createTerminal({
    name: opts.name,
    cwd: opts.cwd,
    iconPath: new vscode.ThemeIcon(opts.icon),
    location: terminalLocation(),
    env: {
      // aGiTrack runs the agent inside its own terminal UI. When the backend is Claude
      // Code, it otherwise tries to auto-install its VSCode companion extension on
      // detecting VSCode — which fails in this context (e.g. no `code` on PATH) and
      // shows a confusing "failed to install the IDE extension" error. Skip that; IDE
      // *connection* still works if the user installs the Claude Code extension from the
      // Marketplace. Harmless for the OpenCode backend.
      CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL: "1",
    },
  });
  ourTerminals.add(terminal); // mark before onDidOpenTerminal fires, so we never self-dispose
  void runWhenShellReady(terminal, opts.command);
  return terminal;
}

/** Run `command` once the shell has finished initializing. VSCode injects its automatic
 * setup (venv/conda activation, shell integration) into a new terminal; sequencing the
 * launch through shell integration guarantees that setup runs in the shell FIRST, so it
 * never gets typed into aGiTrack. Falls back to a delayed sendText if shell integration
 * isn't available (it's then best-effort, with aGiTrack's own stdin-drain as a backstop).
 *
 * A progress notification is shown for the whole wait so the user knows the few-second
 * pause is expected. It is deliberately NOT echoed into the terminal — writing into a
 * not-yet-ready shell prints literal text before the prompt and garbles the startup. */
async function runWhenShellReady(terminal: vscode.Terminal, command: string): Promise<void> {
  await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: "aGiTrack is starting — preparing your terminal (a few seconds)…",
    },
    async () => {
      const integration = await waitForShellIntegration(terminal, 6000);
      if (integration) {
        // Let any startup command VSCode runs (conda/direnv, etc.) finish before aGiTrack,
        // so it isn't queued behind this long-running foreground process. Python venv
        // activation is handled separately (suppressed before launch), so this is light
        // insurance with a short grace window.
        await waitForShellSetupToSettle(terminal, 1200);
        // Keep the notification up until aGiTrack actually starts: executeCommand returns
        // immediately, so we wait for the command's first output chunk (aGiTrack drawing
        // its first frame / prompt). A timeout caps the wait so it can never hang forever
        // if a backend's raw-TUI output isn't captured by shell integration.
        const execution = integration.executeCommand(command);
        await waitForFirstOutput(execution, 15000);
      } else {
        // No shell integration: launch after a short delay, then hold the notification a
        // few seconds to cover the typical startup since we have no readiness signal.
        setTimeout(() => terminal.sendText(command), 1200);
        await delay(5000);
      }
    },
  );
}

/** After shell integration is ready, wait for VSCode's own startup command(s) — venv /
 * conda activation — to run and finish, so they execute BEFORE aGiTrack instead of being
 * queued behind it. VSCode is given a grace window of `graceMs` to *kick off* such a
 * command: if one starts we wait for it to end (and any back-to-back ones), and if none
 * starts within the window we proceed — activation was via environment variables (no
 * command to wait for), or there is no venv. */
function waitForShellSetupToSettle(terminal: vscode.Terminal, graceMs: number): Promise<void> {
  return new Promise((resolve) => {
    let active = 0;
    let graceTimer: ReturnType<typeof setTimeout>;
    const finish = () => {
      startSub.dispose();
      endSub.dispose();
      clearTimeout(graceTimer);
      resolve();
    };
    const startSub = vscode.window.onDidStartTerminalShellExecution((event) => {
      if (event.terminal === terminal) {
        active++;
        clearTimeout(graceTimer); // a startup command is running — wait for it to finish
      }
    });
    const endSub = vscode.window.onDidEndTerminalShellExecution((event) => {
      if (event.terminal === terminal) {
        active = Math.max(0, active - 1);
        if (active === 0) {
          finish(); // VSCode's setup command(s) have finished — safe to launch aGiTrack
        }
      }
    });
    graceTimer = setTimeout(() => {
      if (active === 0) {
        finish(); // nothing kicked off in the grace window — nothing to wait for
      }
    }, graceMs);
  });
}

/** Resolve when the shell execution produces its first output, or after `timeoutMs`. */
async function waitForFirstOutput(execution: vscode.TerminalShellExecution, timeoutMs: number): Promise<void> {
  await Promise.race([
    (async () => {
      for await (const _chunk of execution.read()) {
        return; // first chunk means aGiTrack has started producing output
      }
    })(),
    delay(timeoutMs),
  ]);
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function waitForShellIntegration(
  terminal: vscode.Terminal,
  timeoutMs: number,
): Promise<vscode.TerminalShellIntegration | undefined> {
  if (terminal.shellIntegration) {
    return Promise.resolve(terminal.shellIntegration);
  }
  return new Promise((resolve) => {
    const sub = vscode.window.onDidChangeTerminalShellIntegration((event) => {
      if (event.terminal === terminal) {
        clearTimeout(timer);
        sub.dispose();
        resolve(terminal.shellIntegration);
      }
    });
    const timer = setTimeout(() => {
      sub.dispose();
      resolve(terminal.shellIntegration);
    }, timeoutMs);
  });
}

/** Build the `agitrack …` shell command from the user's settings. */
function launchCommand(exe: string): string {
  const config = vscode.workspace.getConfiguration("agitrack");
  const backend = config.get<string>("backend") || "";
  const extra = config.get<string[]>("args") || [];
  const parts = [quote(exe)];
  if (backend) {
    parts.push("--backend", backend);
  }
  for (const arg of extra) {
    parts.push(quote(arg));
  }
  return parts.join(" ");
}

/** Where aGiTrack's terminal opens. Default `beside`: in the editor area, split to
 * the right of the current editor (rather than in the bottom panel). */
function terminalLocation(): vscode.TerminalOptions["location"] {
  switch (vscode.workspace.getConfiguration("agitrack").get<string>("terminalLocation")) {
    case "panel":
      return vscode.TerminalLocation.Panel;
    case "editor":
      return vscode.TerminalLocation.Editor;
    case "beside":
    default:
      return { viewColumn: vscode.ViewColumn.Beside };
  }
}

/** Quote a shell argument only when it contains characters that need it (used for the
 * install-in-terminal fallback, which runs in a shell). */
function quote(value: string): string {
  return /[^\w./:-]/.test(value) ? `'${value.replace(/'/g, "'\\''")}'` : value;
}

// --- making sure the CLI is installed ------------------------------------------

function configuredExe(): string {
  return vscode.workspace.getConfiguration("agitrack").get<string>("path") || "agitrack";
}

/** Return a runnable aGiTrack executable, installing it on demand when missing. */
async function ensureCliAvailable(): Promise<string | undefined> {
  const exe = configuredExe();
  if (await runnable(exe)) {
    return exe;
  }
  const choice = await vscode.window.showInformationMessage(
    "aGiTrack isn't installed. Install it now? (Requires Python 3.10+.)",
    { modal: true },
    "Install aGiTrack",
  );
  if (choice !== "Install aGiTrack") {
    return undefined;
  }
  return installAgitrack();
}

interface InstallPlan {
  cmd: string;
  args: string[];
  label: string;
  /** When installing with pip, the python to query for the --user scripts dir. */
  userBaseFrom?: string;
}

/** Install the aGiTrack CLI with the best available Python tool, then resolve the
 * executable it produced. Returns the path to use, or undefined on failure. */
async function installAgitrack(): Promise<string | undefined> {
  if (await blockOnNativeWindows()) {
    return undefined; // POSIX-only — installing on native Windows would never run
  }
  const plan = await planInstaller();
  if (!plan) {
    const pick = await vscode.window.showErrorMessage(
      "aGiTrack needs Python 3.10+ with pipx or pip. Install Python, then try again.",
      "Open python.org",
    );
    if (pick) {
      void vscode.env.openExternal(vscode.Uri.parse("https://www.python.org/downloads/"));
    }
    return undefined;
  }

  try {
    const exe = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Installing aGiTrack with ${plan.label}…` },
      async () => {
        await execCapture(plan.cmd, plan.args, 300_000);
        return resolveInstalledExe(plan);
      },
    );
    if (!exe) {
      const pick = await vscode.window.showErrorMessage(
        "aGiTrack installed, but its executable wasn't on any known path. Run `which agitrack` " +
          "(or `pipx list`) in a terminal, then paste that path into the `agitrack.path` setting.",
        "Open Setting",
      );
      if (pick) {
        void vscode.commands.executeCommand("workbench.action.openSettings", "agitrack.path");
      }
      return undefined;
    }
    // Persist a resolved absolute path so later launches work even if the install
    // dir (e.g. ~/.local/bin) isn't yet on the shell PATH.
    if (exe !== "agitrack") {
      await vscode.workspace
        .getConfiguration("agitrack")
        .update("path", exe, vscode.ConfigurationTarget.Global);
    }
    void vscode.window.showInformationMessage("aGiTrack installed.");
    return exe;
  } catch (err) {
    const pick = await vscode.window.showErrorMessage(
      `Installing aGiTrack failed: ${errorText(err)}`,
      "Install in Terminal",
    );
    if (pick) {
      const terminal = vscode.window.createTerminal({ name: "Install aGiTrack" });
      ourTerminals.add(terminal);
      terminal.show();
      terminal.sendText([plan.cmd, ...plan.args].map(quote).join(" "));
    }
    return undefined;
  }
}

/** Choose how to install: pipx (preferred for CLI tools), else pip --user. */
async function planInstaller(): Promise<InstallPlan | undefined> {
  if (await runnable("pipx")) {
    return { cmd: "pipx", args: ["install", "agitrack"], label: "pipx" };
  }
  const py = await firstPython();
  if (py && (await hasPip(py))) {
    return {
      cmd: py,
      args: ["-m", "pip", "install", "--user", "agitrack"],
      label: `pip (${py})`,
      userBaseFrom: py,
    };
  }
  return undefined;
}

/** Locate the agitrack executable produced by an install (issue #93). A GUI-launched
 * VSCode (Finder/Dock) doesn't inherit the shell PATH, so a bare `agitrack` won't resolve
 * even after a successful install. We first ask the package manager exactly where it put
 * the executable (authoritative, PATH-independent), then fall back to the well-known
 * install locations for the host. */
async function resolveInstalledExe(plan: InstallPlan): Promise<string | undefined> {
  const candidates: string[] = ["agitrack"];
  if (plan.cmd === "pipx") {
    // pipx knows its own app-bin directory — the most reliable answer.
    try {
      const binDir = (await execCapture("pipx", ["environment", "--value", "PIPX_BIN_DIR"], 5_000)).trim();
      if (binDir) {
        candidates.push(join(binDir, "agitrack"));
      }
    } catch {
      // fall through to the static candidates
    }
  }
  if (plan.userBaseFrom) {
    // pip --user puts console scripts in <user-base>/bin (covers macOS framework
    // Python's ~/Library/Python/X.Y/bin too).
    try {
      const base = (await execCapture(plan.userBaseFrom, ["-m", "site", "--user-base"], 5_000)).trim();
      if (base) {
        candidates.push(join(base, "bin", "agitrack"));
      }
    } catch {
      // ignore — fall back to the static candidates
    }
  }
  candidates.push(...staticExeCandidates(homedir(), process.platform, macLibraryPythonVersions()));
  for (const candidate of dedupe(candidates)) {
    if (await runnable(candidate)) {
      return candidate;
    }
  }
  return undefined;
}

/** Version subdirectories under ~/Library/Python (e.g. "3.12"), where macOS framework
 * Python keeps user console scripts. Empty off macOS or when the directory is absent. */
function macLibraryPythonVersions(): string[] {
  if (process.platform !== "darwin") {
    return [];
  }
  try {
    return readdirSync(join(homedir(), "Library", "Python"));
  } catch {
    return [];
  }
}

async function firstPython(): Promise<string | undefined> {
  for (const py of ["python3", "python"]) {
    if (await runnable(py)) {
      return py;
    }
  }
  return undefined;
}

async function hasPip(py: string): Promise<boolean> {
  try {
    await execCapture(py, ["-m", "pip", "--version"], 5_000);
    return true;
  } catch {
    return false;
  }
}

/** True if `<exe> --version` runs successfully. */
async function runnable(exe: string): Promise<boolean> {
  try {
    await execCapture(exe, ["--version"], 5_000);
    return true;
  } catch {
    return false;
  }
}

function execCapture(cmd: string, args: string[], timeout: number): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { timeout }, (err, stdout, stderr) => {
      if (err) {
        reject(new Error(((stderr || "") + (err.message || "")).trim() || "command failed"));
      } else {
        resolve(stdout);
      }
    });
  });
}

// --- version parity -------------------------------------------------------------

/** Warn when the installed CLI has self-updated past this extension (they ship in
 * lockstep). Best-effort; silent if the CLI can't be run here. */
async function checkVersionParity(context: vscode.ExtensionContext, exe: string): Promise<void> {
  const extensionVersion = String(context.extension.packageJSON.version ?? "");
  let cliVersion: string;
  try {
    cliVersion = (await execCapture(exe, ["--version"], 5_000)).trim();
  } catch {
    return;
  }
  if (!cliVersion || !extensionVersion || cliVersion === extensionVersion) {
    return;
  }
  const choice = await vscode.window.showInformationMessage(
    `aGiTrack CLI is v${cliVersion} but the aGiTrack extension is v${extensionVersion}. ` +
      "They ship in lockstep — update the extension to match.",
    "Check for Extension Updates",
  );
  if (choice) {
    void vscode.commands.executeCommand("workbench.extensions.action.checkForUpdates");
  }
}

// --- helpers --------------------------------------------------------------------

/** Resolve which workspace folder to run in: the invoking resource, the active
 * editor's folder, the only folder, or a picker when the choice is ambiguous. */
async function pickFolder(targetUri?: vscode.Uri): Promise<vscode.WorkspaceFolder | undefined> {
  if (targetUri) {
    const folder = vscode.workspace.getWorkspaceFolder(targetUri);
    if (folder) {
      return folder;
    }
  }
  const active = vscode.window.activeTextEditor?.document.uri;
  if (active) {
    const folder = vscode.workspace.getWorkspaceFolder(active);
    if (folder) {
      return folder;
    }
  }
  const folders = vscode.workspace.workspaceFolders ?? [];
  if (folders.length <= 1) {
    return folders[0];
  }
  return vscode.window.showWorkspaceFolderPick({ placeHolder: "Start aGiTrack in which folder?" });
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
