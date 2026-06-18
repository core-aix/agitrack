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
import { readFileSync } from "fs";
import { homedir } from "os";
import { join } from "path";

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

// Folders whose session terminal is being disposed for a user-initiated restart,
// not a real close — recovery is skipped so it doesn't race the relaunch for the
// repo lock (a held lock would make the new instance think aGiTrack is running).
const restartingFolders = new Set<string>();
// True once deactivate() begins (window close / reload): the close handler then
// skips its own recovery because deactivate() runs it detached so it survives.
let deactivating = false;

export function activate(context: vscode.ExtensionContext): void {
  extensionContext = context;
  context.subscriptions.push(
    vscode.commands.registerCommand("agitrack.start", () => startSession()),
    vscode.commands.registerCommand("agitrack.startHere", (uri?: vscode.Uri) => startSession(uri)),
    vscode.commands.registerCommand("agitrack.restart", () => restartSession()),
    vscode.commands.registerCommand("agitrack.dashboard", (uri?: vscode.Uri) => openDashboard(uri)),
    vscode.commands.registerCommand("agitrack.install", () => installAgitrack()),
  );

  // Forget terminals the user closes so the next launch starts a fresh one — and
  // for a session, finalize anything an abrupt close left behind (the Extension
  // Host outlives the terminal, so it can run recovery with no time pressure).
  context.subscriptions.push(
    vscode.window.onDidCloseTerminal((closed) => {
      for (const [key, terminal] of dashboards) {
        if (terminal === closed) {
          dashboards.delete(key);
        }
      }
      for (const [key, terminal] of terminals) {
        if (terminal !== closed) {
          continue;
        }
        terminals.delete(key);
        if (restartingFolders.delete(key)) {
          continue; // a relaunch is coming; don't grab the lock out from under it
        }
        if (deactivating) {
          continue; // window closing — deactivate() runs recovery detached instead
        }
        runRecovery(key, { detached: false });
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
      const parent = splitParentOf(opened);
      if (!parent || !isAgitrackTerminal(parent)) {
        return;
      }
      opened.dispose();
      void vscode.window.showInformationMessage(
        "aGiTrack runs one session per repository, so splitting its terminal just opens an " +
          "empty shell — that split was closed. Use the existing aGiTrack terminal (or the " +
          "aGiTrack button to start a session in another folder).",
      );
    }),
  );

  if (vscode.workspace.getConfiguration("agitrack").get<boolean>("openOnStartup")) {
    void startSession();
  }

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
  deactivating = true;
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

/** Find a surviving aGiTrack terminal for `folder` to reattach to when our map lost it
 * (e.g. after an extension reload). Gated on a live repo-lock PID so we only reattach
 * when a session is genuinely running in that folder — never to an unrelated terminal. */
function findAgitrackTerminal(folder: vscode.WorkspaceFolder): vscode.Terminal | undefined {
  const pid = readAgitrackPid(folder.uri.fsPath);
  if (!pid || !isAlive(pid)) {
    return undefined; // no live session here — let a fresh one start
  }
  const named = `${TERMINAL_NAME} (${folder.name})`;
  return (
    vscode.window.terminals.find((t) => t.name === named) ??
    vscode.window.terminals.find((t) => t.name === TERMINAL_NAME)
  );
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

/** First-run housekeeping: if the CLI is present, check version parity; if it's
 * missing, offer to install it so the extension is usable out of the box. */
async function bootstrap(context: vscode.ExtensionContext): Promise<void> {
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
  const folder = await pickFolder(targetUri);
  if (!folder) {
    void vscode.window.showWarningMessage("aGiTrack: open a folder or repository first.");
    return;
  }

  const key = folder.uri.fsPath;
  // Never start a second session in a folder that already has one — just bring the
  // existing terminal forward, so a still-running job is never interrupted/restarted.
  // Reattach to a surviving terminal even if our map lost it (e.g. an extension reload
  // cleared the map but VSCode kept the terminal), and confirm a session is actually
  // running there via the repo lock before reusing it.
  const existing = terminals.get(key) ?? findAgitrackTerminal(folder);
  if (existing) {
    terminals.set(key, existing);
    existing.show();
    return;
  }

  const exe = await ensureCliAvailable();
  if (!exe) {
    return; // not installed and not installed-on-demand
  }

  await ensureCloseConfirmation();
  void maybeShowGracefulExitTip();

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
  terminals.set(key, terminal);
  terminal.show();
}

/** Once per install, tell the user the reliable way to exit aGiTrack — Ctrl-G → exit —
 * so the in-flight turn is finalized. Closing the window also attempts a graceful exit
 * (see deactivate()), but VSCode bounds how long it waits, so this is the sure path. */
async function maybeShowGracefulExitTip(): Promise<void> {
  const KEY = "agitrack.gracefulExitTipShown";
  const state = extensionContext?.globalState;
  if (!state || state.get<boolean>(KEY)) {
    return;
  }
  await state.update(KEY, true); // remember first, so an immediate close won't re-show it
  void vscode.window.showInformationMessage(
    "To exit aGiTrack and make sure your latest turn is committed and merged, use the " +
      "Ctrl-G menu → exit inside aGiTrack. Closing the terminal or window still tries to " +
      "exit cleanly, but Ctrl-G → exit is the reliable way.",
    { modal: true },
    "Got it",
  );
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
  // Mark this a restart so the close handler doesn't fire recovery and race the
  // relaunch for the repo lock.
  restartingFolders.add(folder.uri.fsPath);
  terminals.get(folder.uri.fsPath)?.dispose();
  terminals.delete(folder.uri.fsPath);
  // Give aGiTrack a moment to release its repo lock before relaunching.
  await new Promise((resolve) => setTimeout(resolve, 300));
  await startSession(folder.uri);
}

/** Open aGiTrack's metrics dashboard for the chosen folder (read-only; `agitrack -d`
 * serves it on localhost and opens the browser, Ctrl-C in the terminal to stop). */
async function openDashboard(targetUri?: vscode.Uri): Promise<void> {
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
      void vscode.window.showErrorMessage(
        "aGiTrack installed but its executable couldn't be located. Set `agitrack.path` to it manually.",
      );
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

/** Locate the agitrack executable produced by an install. */
async function resolveInstalledExe(plan: InstallPlan): Promise<string | undefined> {
  const candidates: string[] = ["agitrack", join(homedir(), ".local", "bin", "agitrack")];
  if (plan.userBaseFrom) {
    try {
      const base = (await execCapture(plan.userBaseFrom, ["-m", "site", "--user-base"], 5_000)).trim();
      if (base) {
        candidates.push(join(base, "bin", "agitrack"));
      }
    } catch {
      // ignore — fall back to the other candidates
    }
  }
  for (const candidate of candidates) {
    if (await runnable(candidate)) {
      return candidate;
    }
  }
  return undefined;
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
