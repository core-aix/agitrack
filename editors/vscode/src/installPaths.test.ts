import assert from "node:assert/strict";
import { test } from "node:test";

import { dedupe, exeName, staticExeCandidates } from "./installPaths";

// Issue #93: a GUI-launched VSCode lacks the shell PATH, so a freshly-installed `agitrack`
// must be found by absolute path. These pin the fallback locations we probe.

test("staticExeCandidates always includes ~/.local/bin (pipx default / pip --user)", () => {
  for (const platform of ["darwin", "linux"] as NodeJS.Platform[]) {
    const candidates = staticExeCandidates("/home/u", platform);
    assert.ok(candidates.includes("/home/u/.local/bin/agitrack"), `missing on ${platform}`);
  }
});

test("staticExeCandidates adds macOS framework-Python and Homebrew dirs on darwin", () => {
  const candidates = staticExeCandidates("/Users/u", "darwin", ["3.12", "3.11"]);
  // One entry per discovered ~/Library/Python/<X.Y>/bin, in the order given.
  assert.ok(candidates.includes("/Users/u/Library/Python/3.12/bin/agitrack"));
  assert.ok(candidates.includes("/Users/u/Library/Python/3.11/bin/agitrack"));
  // Both Homebrew prefixes (Apple Silicon + Intel).
  assert.ok(candidates.includes("/opt/homebrew/bin/agitrack"));
  assert.ok(candidates.includes("/usr/local/bin/agitrack"));
});

test("staticExeCandidates does not add macOS-only dirs off darwin", () => {
  const candidates = staticExeCandidates("/home/u", "linux", ["3.12"]);
  assert.ok(!candidates.some((c) => c.includes("Library/Python")));
  assert.ok(!candidates.some((c) => c.startsWith("/opt/homebrew")));
});

test("staticExeCandidates ends every candidate with the agitrack executable", () => {
  for (const candidate of staticExeCandidates("/Users/u", "darwin", ["3.12"])) {
    assert.ok(candidate.endsWith("/agitrack"), candidate);
  }
});

test("exeName is agitrack.exe on Windows, agitrack elsewhere (#118)", () => {
  assert.equal(exeName("win32"), "agitrack.exe");
  assert.equal(exeName("linux"), "agitrack");
  assert.equal(exeName("darwin"), "agitrack");
});

test("staticExeCandidates targets agitrack.exe in Windows Scripts/pipx dirs (#118)", () => {
  const candidates = staticExeCandidates("/home/u", "win32");
  assert.ok(
    candidates.every((c) => c.endsWith("agitrack.exe")),
    `every candidate should end with agitrack.exe: ${JSON.stringify(candidates)}`,
  );
  assert.ok(candidates.some((c) => c.includes(".local")), "includes the pipx ~/.local/bin dir");
  assert.ok(candidates.some((c) => c.includes("Scripts")), "includes a pip --user Scripts dir");
  // No POSIX-only dirs leak onto Windows.
  assert.ok(!candidates.some((c) => c.startsWith("/usr/local") || c.startsWith("/opt/homebrew")));
});

test("dedupe preserves first-seen order so the authoritative candidate stays first", () => {
  const input = ["agitrack", "/opt/homebrew/bin/agitrack", "agitrack", "/usr/local/bin/agitrack"];
  assert.deepEqual(dedupe(input), ["agitrack", "/opt/homebrew/bin/agitrack", "/usr/local/bin/agitrack"]);
});
