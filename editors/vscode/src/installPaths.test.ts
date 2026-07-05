import assert from "node:assert/strict";
import { test } from "node:test";

import { dedupe, exeCandidatesFromScriptDirs, exeName, staticExeCandidates } from "./installPaths";

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

test("staticExeCandidates ends every POSIX candidate with the agitrack executable", () => {
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

// `path.join` uses the HOST separator (these tests run on a POSIX CI host), so normalize
// to forward slashes before asserting — mirroring how the other Windows test avoids it.
const norm = (values: string[]): string[] => values.map((v) => v.replace(/\\/g, "/"));

test("staticExeCandidates targets the pip --user version subfolder on Windows (#140)", () => {
  // pip --user puts agitrack.exe in %APPDATA%\Python\Python<XY>\Scripts, NOT
  // %APPDATA%\Python\Scripts — the version-less guess is the #140 bug.
  const candidates = norm(staticExeCandidates("/home/u", "win32", [], ["Python314", "Python312"]));
  assert.ok(
    candidates.some((c) => c.includes("Roaming/Python/Python314/Scripts")),
    `expected a Python314 Scripts candidate: ${JSON.stringify(candidates)}`,
  );
  assert.ok(candidates.some((c) => c.includes("Roaming/Python/Python312/Scripts")));
  // Per-user Python installs share the Python<XY> naming under Programs\Python.
  assert.ok(candidates.some((c) => c.includes("Programs/Python/Python314/Scripts")));
  // The version-less path is kept only as a last-resort fallback, after the versioned ones.
  const versioned = candidates.findIndex((c) => c.includes("Python314/Scripts"));
  const versionless = candidates.findIndex((c) => c.endsWith("Roaming/Python/Scripts/agitrack.exe"));
  assert.ok(versioned >= 0 && versionless >= 0 && versioned < versionless, "versioned dir must precede the fallback");
});

test("exeCandidatesFromScriptDirs appends the platform exe to each dir", () => {
  assert.deepEqual(
    norm(exeCandidatesFromScriptDirs(["/home/u/AppData/Roaming/Python/Python314/Scripts"], "win32")),
    ["/home/u/AppData/Roaming/Python/Python314/Scripts/agitrack.exe"],
  );
  assert.deepEqual(norm(exeCandidatesFromScriptDirs(["/home/u/.local/bin"], "linux")), ["/home/u/.local/bin/agitrack"]);
  assert.deepEqual(exeCandidatesFromScriptDirs([], "darwin"), []);
});

test("dedupe preserves first-seen order so the authoritative candidate stays first", () => {
  const input = ["agitrack", "/opt/homebrew/bin/agitrack", "agitrack", "/usr/local/bin/agitrack"];
  assert.deepEqual(dedupe(input), ["agitrack", "/opt/homebrew/bin/agitrack", "/usr/local/bin/agitrack"]);
});
