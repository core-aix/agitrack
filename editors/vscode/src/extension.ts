// aGiTrack VSCode extension.
//
// A thin wrapper that lets you install aGiTrack as a VSCode plugin and launch the
// full aGiTrack terminal application (proxy mode) inside VSCode — without opening a
// terminal and typing `agitrack` yourself. Because it runs the real CLI in an
// integrated terminal, you get the complete aGiTrack experience (the backend's
// native TUI, the Ctrl-G command menu, sessions, sharing, worktrees, auto-commits —
// everything), just started from a VSCode command or the status-bar button.

import * as vscode from "vscode";
import { execFile } from "child_process";

const TERMINAL_NAME = "aGiTrack";

// One aGiTrack terminal per workspace folder, keyed by folder path, so re-running
// the launch command focuses the existing session instead of starting a second
// instance (which aGiTrack would refuse with its repo lock).
const terminals = new Map<string, vscode.Terminal>();

export function activate(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("agitrack.start", () => startSession()),
    vscode.commands.registerCommand("agitrack.startHere", (uri?: vscode.Uri) => startSession(uri)),
    vscode.commands.registerCommand("agitrack.restart", () => restartSession()),
  );

  // Forget terminals the user closes so the next launch starts a fresh one.
  context.subscriptions.push(
    vscode.window.onDidCloseTerminal((closed) => {
      for (const [key, terminal] of terminals) {
        if (terminal === closed) {
          terminals.delete(key);
        }
      }
    }),
  );

  // A status-bar button so launching aGiTrack is one click, no Command Palette needed.
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 0);
  status.text = "$(git-commit) aGiTrack";
  status.tooltip = "Start an aGiTrack session in this workspace";
  status.command = "agitrack.start";
  status.show();
  context.subscriptions.push(status);

  if (vscode.workspace.getConfiguration("agitrack").get<boolean>("openOnStartup")) {
    void startSession();
  }

  // aGiTrack auto-updates itself (you're prompted in the terminal at startup or via
  // Ctrl-G). The extension and the CLI ship in lockstep, so if the CLI has updated
  // past the installed extension, nudge the user to update the extension to match.
  void checkVersionParity(context);
}

export function deactivate(): void {
  // Terminals are disposed by VSCode with the window; aGiTrack releases its repo
  // lock on exit, so there is nothing to clean up here.
}

/** Launch (or focus) aGiTrack in an integrated terminal for the chosen workspace folder. */
async function startSession(targetUri?: vscode.Uri): Promise<void> {
  const folder = await pickFolder(targetUri);
  if (!folder) {
    void vscode.window.showWarningMessage("aGiTrack: open a folder or repository first.");
    return;
  }

  const key = folder.uri.fsPath;
  const existing = terminals.get(key);
  if (existing) {
    existing.show();
    return; // aGiTrack is already running here — just bring it forward.
  }

  const terminal = createTerminal(folder);
  terminals.set(key, terminal);
  terminal.show();
  terminal.sendText(launchCommand());
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

function createTerminal(folder: vscode.WorkspaceFolder): vscode.Terminal {
  return vscode.window.createTerminal({
    name: terminals.size === 0 ? TERMINAL_NAME : `${TERMINAL_NAME} (${folder.name})`,
    cwd: folder.uri.fsPath,
    iconPath: new vscode.ThemeIcon("git-commit"),
  });
}

/** Build the `agitrack …` command line from the user's settings. */
function launchCommand(): string {
  const config = vscode.workspace.getConfiguration("agitrack");
  const exe = config.get<string>("path") || "agitrack";
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

/** Quote a shell argument only when it contains characters that need it. */
function quote(value: string): string {
  return /[^\w./:-]/.test(value) ? `'${value.replace(/'/g, "'\\''")}'` : value;
}

/** Warn when the installed CLI has self-updated past this extension (they ship in
 * lockstep). Best-effort: if the CLI can't be run (e.g. not on the extension host's
 * PATH), stay silent — launching uses the integrated terminal's shell PATH instead. */
async function checkVersionParity(context: vscode.ExtensionContext): Promise<void> {
  const extensionVersion = String(context.extension.packageJSON.version ?? "");
  const exe = vscode.workspace.getConfiguration("agitrack").get<string>("path") || "agitrack";
  let cliVersion: string;
  try {
    cliVersion = await agitrackVersion(exe);
  } catch {
    return; // CLI not found / not runnable here — don't raise a false alarm
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

/** Read `agitrack --version` (cheap, side-effect-free). */
function agitrackVersion(exe: string): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(exe, ["--version"], { timeout: 5000 }, (err, stdout) => {
      if (err) {
        reject(err);
      } else {
        resolve(stdout.trim());
      }
    });
  });
}

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
