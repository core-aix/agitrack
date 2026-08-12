import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

// The extension host is a GUI process: it has no console of its own. On Windows every console
// application it starts therefore gets a console WINDOW unless the spawn says `windowsHide`,
// so each `--version` probe, each `git remote -v`, each recovery run puts a black box on the
// user's desktop for a fraction of a second. Reported live as "terminals flashing over time".
//
// Node's documented default for `windowsHide` is false, so this is not something to leave to
// the runtime. The Python side has the same rule enforced by tests/test_console_isolation.py;
// this is its counterpart for the extension, and it reads the SOURCE for the same reason:
// nothing about a spawn that works correctly reveals the window it opened.
test("every child process the extension starts hides its console window on Windows", () => {
  const source = readFileSync(join(__dirname, "..", "src", "extension.ts"), "utf8");
  const offenders: string[] = [];
  const call = /\b(spawn|execFile|exec)\s*\(/g;
  let match: RegExpExecArray | null;
  while ((match = call.exec(source)) !== null) {
    // The options object is the tail of the call; take a generous window and look for the flag.
    const tail = source.slice(match.index, match.index + 400);
    const end = tail.indexOf(");");
    const text = end === -1 ? tail : tail.slice(0, end);
    if (!text.includes("windowsHide")) {
      const line = source.slice(0, match.index).split("\n").length;
      offenders.push(`extension.ts:${line} ${match[1]}(...)`);
    }
  }
  assert.deepEqual(offenders, [], `these spawns can flash a console window on Windows:\n  ${offenders.join("\n  ")}`);
});
