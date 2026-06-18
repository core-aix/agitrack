// aGiTrack VSCode extension: a Chat participant (`@agitrack`) that drives the
// aGiTrack CLI in its headless `--mode json --json-events` mode and renders the
// conversation in VSCode's native Chat window. aGiTrack auto-commits every turn, so
// each chat turn leaves a tracked commit behind.
//
// The participant spawns one `agitrack ... --prompt <text>` process per turn.
// aGiTrack persists the backend session between runs, so a fresh process resumes the
// same conversation — no long-lived daemon is needed.

import * as vscode from "vscode";
import { spawn } from "child_process";
import * as readline from "readline";

export function activate(context: vscode.ExtensionContext): void {
  const participant = vscode.chat.createChatParticipant("agitrack.chat", handler);
  participant.iconPath = new vscode.ThemeIcon("git-commit");
  context.subscriptions.push(participant);
}

export function deactivate(): void {
  // Nothing to clean up: each turn's process is short-lived and tied to the request.
}

async function handler(
  request: vscode.ChatRequest,
  _context: vscode.ChatContext,
  stream: vscode.ChatResponseStream,
  token: vscode.CancellationToken,
): Promise<void> {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    stream.markdown("Open a folder or repository first — aGiTrack tracks a git repo.");
    return;
  }
  if (!request.prompt.trim()) {
    stream.markdown("Type a prompt for the coding agent.");
    return;
  }

  const config = vscode.workspace.getConfiguration("agitrack");
  const exe = config.get<string>("path") || "agitrack";
  const backend = config.get<string>("backend") || "";

  const args = [
    "--repo",
    folder.uri.fsPath,
    "--mode",
    "json",
    "--json-events",
    "--skip-privacy-ack",
  ];
  if (backend) {
    args.push("--backend", backend);
  }
  args.push("--prompt", request.prompt);

  stream.progress("Running the agent through aGiTrack…");
  await runAgitrack(exe, args, folder.uri.fsPath, stream, token);
}

function runAgitrack(
  exe: string,
  args: string[],
  cwd: string,
  stream: vscode.ChatResponseStream,
  token: vscode.CancellationToken,
): Promise<void> {
  return new Promise((resolve) => {
    const proc = spawn(exe, args, { cwd });
    token.onCancellationRequested(() => proc.kill());

    const rl = readline.createInterface({ input: proc.stdout });
    let sawResponse = false;
    rl.on("line", (line: string) => {
      const trimmed = line.trim();
      if (!trimmed.startsWith("{")) {
        return; // ignore aGiTrack's human-readable lines; only JSON events are consumed
      }
      let event: AgitrackEvent;
      try {
        event = JSON.parse(trimmed) as AgitrackEvent;
      } catch {
        return;
      }
      switch (event.type) {
        case "response":
          if (event.text) {
            sawResponse = true;
            stream.markdown(event.text + "\n");
          }
          break;
        case "commit":
          stream.markdown(`\n\n_aGiTrack committed this turn: \`${event.sha}\`_`);
          break;
        case "no_changes":
          stream.markdown("\n\n_No file changes this turn._");
          break;
        case "error":
          stream.markdown(`\n\n**aGiTrack error:** ${event.message ?? "unknown error"}`);
          break;
        default:
          break;
      }
    });

    let stderr = "";
    proc.stderr.on("data", (chunk: Buffer) => {
      stderr += chunk.toString();
    });
    proc.on("error", (err: Error) => {
      stream.markdown(`Failed to launch aGiTrack (\`${exe}\`): ${err.message}\n\nSet \`agitrack.path\` in settings if it isn't on PATH.`);
      resolve();
    });
    proc.on("close", (code: number | null) => {
      if (!sawResponse && code !== 0) {
        stream.markdown(`\n\naGiTrack exited with code ${code ?? "?"}.${stderr ? ` ${stderr.trim()}` : ""}`);
      }
      resolve();
    });
  });
}

interface AgitrackEvent {
  type: "response" | "commit" | "no_changes" | "error";
  text?: string;
  sha?: string;
  session?: string;
  model?: string;
  message?: string;
  exit_code?: number;
}
