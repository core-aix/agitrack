// aGiTrack VSCode extension.
//
// Runs the full interactive aGiTrack experience inside VSCode with no terminal.
// aGiTrack is launched once per workspace as a long-lived child process driven over
// a bidirectional JSON-RPC bridge (`agitrack --mode json --ui-bridge`):
//
//   * prompts and ':' commands are written to the child's stdin as JSON lines;
//   * the child streams back `response` / `commit` / `notice` / `error` events; and
//   * when aGiTrack needs an answer (stage which files? pick a backend? commit
//     message?) it emits an `ask`, which this extension renders as a native VSCode
//     QuickPick / InputBox / modal — and writes the user's choice back.
//
// The agent conversation happens in the native Chat view (`@agitrack`); the other
// actions (status, stage, user commit, switch backend, new session, summarizer)
// are Command Palette commands. Every turn aGiTrack auto-commits with full
// provenance, so each interaction leaves a tracked commit behind.

import * as vscode from "vscode";
import { ChildProcessWithoutNullStreams, spawn } from "child_process";
import * as readline from "readline";

let manager: BridgeManager;

export function activate(context: vscode.ExtensionContext): void {
  manager = new BridgeManager();
  context.subscriptions.push(manager);

  const participant = vscode.chat.createChatParticipant("agitrack.chat", chatHandler);
  participant.iconPath = new vscode.ThemeIcon("git-commit");
  context.subscriptions.push(participant);

  const register = (id: string, run: () => Promise<void> | void) =>
    context.subscriptions.push(vscode.commands.registerCommand(id, run));

  register("agitrack.status", () => manager.runCommand(":status"));
  register("agitrack.stage", () => manager.runCommand(":stage"));
  register("agitrack.userCommit", () => manager.runCommand(":user-commit"));
  register("agitrack.unstaged", () => manager.runCommand(":unstaged"));
  register("agitrack.switchBackend", () => manager.runCommand(":agent-backend"));
  register("agitrack.newSession", () => manager.runCommand(":new-session"));
  register("agitrack.summarizer", () => summarizerCommand());
  register("agitrack.restart", () => manager.restartActive());
}

export function deactivate(): void {
  manager?.dispose();
}

// --- Chat participant ----------------------------------------------------------

async function chatHandler(
  request: vscode.ChatRequest,
  _context: vscode.ChatContext,
  stream: vscode.ChatResponseStream,
  token: vscode.CancellationToken,
): Promise<void> {
  const folder = workspaceFolder();
  if (!folder) {
    stream.markdown("Open a folder or repository first — aGiTrack tracks a git repo.");
    return;
  }
  const text = request.prompt.trim();
  if (!text) {
    stream.markdown("Type a prompt for the coding agent, or run an `aGiTrack:` command from the Command Palette.");
    return;
  }
  try {
    const bridge = await manager.bridgeFor(folder);
    stream.progress("Running the agent through aGiTrack…");
    await bridge.runTurn({ type: "prompt", text }, stream, token);
  } catch (err) {
    stream.markdown(`\n\n**aGiTrack error:** ${errorText(err)}`);
  }
}

async function summarizerCommand(): Promise<void> {
  const pick = await vscode.window.showQuickPick(
    [
      { label: "Status", value: "status" },
      { label: "Turn on", value: "on" },
      { label: "Turn off", value: "off" },
      { label: "Set model…", value: "model" },
    ],
    { title: "aGiTrack summarizer", placeHolder: "Manage commit summarization" },
  );
  if (pick) {
    await manager.runCommand(`:summarizer ${pick.value}`);
  }
}

// --- Bridge manager: one child process per workspace folder --------------------

class BridgeManager implements vscode.Disposable {
  private readonly bridges = new Map<string, AgitrackBridge>();

  async bridgeFor(folder: vscode.WorkspaceFolder): Promise<AgitrackBridge> {
    const key = folder.uri.fsPath;
    let bridge = this.bridges.get(key);
    if (!bridge || !bridge.alive) {
      bridge = new AgitrackBridge(folder);
      this.bridges.set(key, bridge);
      await bridge.start();
    }
    return bridge;
  }

  /** Run a ':' command against the active workspace, surfacing results as popups. */
  async runCommand(text: string): Promise<void> {
    const folder = workspaceFolder();
    if (!folder) {
      void vscode.window.showWarningMessage("aGiTrack: open a folder or repository first.");
      return;
    }
    try {
      const bridge = await this.bridgeFor(folder);
      await bridge.runTurn({ type: "command", text });
    } catch (err) {
      void vscode.window.showErrorMessage(`aGiTrack: ${errorText(err)}`);
    }
  }

  async restartActive(): Promise<void> {
    const folder = workspaceFolder();
    if (!folder) {
      return;
    }
    this.bridges.get(folder.uri.fsPath)?.dispose();
    this.bridges.delete(folder.uri.fsPath);
    await this.bridgeFor(folder);
    void vscode.window.showInformationMessage("aGiTrack restarted.");
  }

  dispose(): void {
    for (const bridge of this.bridges.values()) {
      bridge.dispose();
    }
    this.bridges.clear();
  }
}

// --- A single aGiTrack child process and its JSON-RPC conversation --------------

interface OutgoingRequest {
  type: "prompt" | "command";
  text: string;
}

interface BridgeEvent {
  type: string;
  [key: string]: unknown;
}

class AgitrackBridge {
  private proc: ChildProcessWithoutNullStreams | undefined;
  private ready = false;
  private session = "";
  private backend = "";
  alive = false;

  // Only one turn runs at a time; later requests queue behind the current one.
  private queue: Promise<void> = Promise.resolve();
  // Resolver for the turn currently in flight, fired on `turn-complete`/`bye`.
  private turnDone: (() => void) | undefined;
  // The chat stream to render into while a chat turn is active (undefined for
  // command turns, whose output is shown as popups instead).
  private activeStream: vscode.ChatResponseStream | undefined;

  constructor(private readonly folder: vscode.WorkspaceFolder) {}

  start(): Promise<void> {
    const config = vscode.workspace.getConfiguration("agitrack");
    const exe = config.get<string>("path") || "agitrack";
    const backend = config.get<string>("backend") || "";

    const args = ["--repo", this.folder.uri.fsPath, "--mode", "json", "--ui-bridge", "--skip-privacy-ack"];
    if (backend) {
      args.push("--backend", backend);
    }

    return new Promise<void>((resolve, reject) => {
      let proc: ChildProcessWithoutNullStreams;
      try {
        proc = spawn(exe, args, { cwd: this.folder.uri.fsPath });
      } catch (err) {
        reject(new Error(`failed to launch aGiTrack (\`${exe}\`): ${errorText(err)}`));
        return;
      }
      this.proc = proc;
      this.alive = true;

      let stderr = "";
      proc.stderr.on("data", (chunk: Buffer) => {
        stderr += chunk.toString();
      });
      proc.on("error", (err: Error) => {
        this.alive = false;
        reject(new Error(`failed to launch aGiTrack (\`${exe}\`): ${err.message}. Set \`agitrack.path\` if it isn't on PATH.`));
      });
      proc.on("close", (code: number | null) => {
        this.alive = false;
        this.ready = false;
        this.finishTurn();
        if (!this.everReady && code !== 0) {
          reject(new Error(`aGiTrack exited (code ${code ?? "?"}).${stderr ? ` ${stderr.trim()}` : ""}`));
        }
      });

      const rl = readline.createInterface({ input: proc.stdout });
      rl.on("line", (line: string) => this.onLine(line, resolve));
    });
  }

  private everReady = false;

  private onLine(line: string, onReady: () => void): void {
    const trimmed = line.trim();
    if (!trimmed.startsWith("{")) {
      return; // ignore any human-readable lines; only JSON events are consumed
    }
    let event: BridgeEvent;
    try {
      event = JSON.parse(trimmed) as BridgeEvent;
    } catch {
      return;
    }
    void this.handleEvent(event, onReady);
  }

  private async handleEvent(event: BridgeEvent, onReady: () => void): Promise<void> {
    switch (event.type) {
      case "ready":
        this.ready = true;
        this.session = str(event.session) || this.session;
        this.backend = str(event.backend) || this.backend;
        if (!this.everReady) {
          this.everReady = true;
          onReady();
        }
        break;
      case "response":
        this.toStream(str(event.text));
        break;
      case "commit":
        this.toStream(`\n\n_aGiTrack committed this turn: \`${str(event.sha)}\`_`);
        break;
      case "no_changes":
        this.toStream("\n\n_No file changes this turn._");
        break;
      case "notice":
        this.notice(str(event.level) || "info", str(event.message));
        break;
      case "error":
        if (this.activeStream) {
          this.activeStream.markdown(`\n\n**aGiTrack error:** ${str(event.message)}`);
        } else {
          void vscode.window.showErrorMessage(`aGiTrack: ${str(event.message)}`);
        }
        break;
      case "ask":
        await this.answer(event);
        break;
      case "turn-complete":
        this.finishTurn();
        break;
      case "bye":
        this.alive = false;
        this.finishTurn();
        break;
      default:
        break;
    }
  }

  /** Run one prompt/command turn to completion (resolves on `turn-complete`). */
  runTurn(
    request: OutgoingRequest,
    stream?: vscode.ChatResponseStream,
    token?: vscode.CancellationToken,
  ): Promise<void> {
    const task = () =>
      new Promise<void>((resolve) => {
        if (!this.alive || !this.proc) {
          stream?.markdown("aGiTrack is not running.");
          resolve();
          return;
        }
        this.activeStream = stream;
        this.turnDone = () => {
          this.activeStream = undefined;
          this.turnDone = undefined;
          resolve();
        };
        token?.onCancellationRequested(() => this.finishTurn());
        this.send(request);
      });
    // Serialize turns: each waits for the previous to finish.
    this.queue = this.queue.then(task, task);
    return this.queue;
  }

  private finishTurn(): void {
    this.turnDone?.();
  }

  private send(message: object): void {
    this.proc?.stdin.write(JSON.stringify(message) + "\n");
  }

  private toStream(text: string): void {
    if (text && this.activeStream) {
      this.activeStream.markdown(text + "\n");
    }
  }

  private notice(level: string, message: string): void {
    if (!message) {
      return;
    }
    // During a chat turn, fold the notice into the conversation; otherwise pop it up.
    if (this.activeStream && level === "info") {
      this.activeStream.markdown(`\n_${message}_\n`);
      return;
    }
    if (level === "error") {
      void vscode.window.showErrorMessage(`aGiTrack: ${message}`);
    } else if (level === "warn") {
      void vscode.window.showWarningMessage(`aGiTrack: ${message}`);
    } else {
      void vscode.window.showInformationMessage(`aGiTrack: ${message}`);
    }
  }

  /** Render an `ask` as native VSCode UI and write the chosen value back. */
  private async answer(event: BridgeEvent): Promise<void> {
    const id = str(event.id);
    const message = str(event.message);
    const detail = str(event.detail);
    const options = Array.isArray(event.options) ? (event.options as unknown[]).map(str) : [];
    let value: unknown = null;

    switch (str(event.kind)) {
      case "select": {
        const choice = await vscode.window.showQuickPick(options, {
          title: message,
          placeHolder: detail || message,
          ignoreFocusOut: true,
        });
        value = choice ?? null;
        break;
      }
      case "multiselect": {
        const choices = await vscode.window.showQuickPick(options, {
          title: message,
          placeHolder: detail || message,
          canPickMany: true,
          ignoreFocusOut: true,
        });
        value = choices ?? [];
        break;
      }
      case "input": {
        const entered = await vscode.window.showInputBox({
          title: message,
          prompt: message,
          value: str(event.default),
          ignoreFocusOut: true,
        });
        value = entered === undefined ? null : entered;
        break;
      }
      case "confirm": {
        const choice = await vscode.window.showWarningMessage(message, { modal: true }, "Yes", "No");
        value = choice === "Yes";
        break;
      }
      default:
        value = null;
    }
    this.send({ type: "answer", id, value });
  }

  dispose(): void {
    if (this.proc && this.alive) {
      try {
        this.send({ type: "exit" });
      } catch {
        // ignore — we kill below anyway
      }
      this.proc.kill();
    }
    this.alive = false;
    this.proc = undefined;
  }
}

// --- helpers -------------------------------------------------------------------

function workspaceFolder(): vscode.WorkspaceFolder | undefined {
  const active = vscode.window.activeTextEditor?.document.uri;
  if (active) {
    const folder = vscode.workspace.getWorkspaceFolder(active);
    if (folder) {
      return folder;
    }
  }
  return vscode.workspace.workspaceFolders?.[0];
}

function str(value: unknown): string {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
