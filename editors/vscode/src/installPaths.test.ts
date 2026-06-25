import assert from "node:assert/strict";
import { test } from "node:test";

import { dedupe, staticExeCandidates, windowsExeCandidates } from "./installPaths";

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

// Windows-specific tests
test("windowsExeCandidates includes pip --user Scripts dir", () => {
  // Simulate Windows home without relying on real APPDATA env vars.
  const home = "C:\\Users\\testuser";
  // Temporarily stub environment variables so the function uses our values.
  const origAppdata = process.env.APPDATA;
  const origLocalappdata = process.env.LOCALAPPDATA;
  process.env.APPDATA = "C:\\Users\\testuser\\AppData\\Roaming";
  process.env.LOCALAPPDATA = "C:\\Users\\testuser\\AppData\\Local";
  try {
    const candidates = windowsExeCandidates(home, ["Python312", "Python311"]);
    // pip --user Scripts dir
    assert.ok(
      candidates.includes("C:\\Users\\testuser\\AppData\\Roaming\\Python\\Scripts\\agitrack.exe"),
      "missing pip --user Scripts",
    );
    // pipx venv
    assert.ok(
      candidates.includes(
        "C:\\Users\\testuser\\AppData\\Local\\pipx\\venvs\\agitrack\\Scripts\\agitrack.exe",
      ),
      "missing pipx venv",
    );
    // Per-version installs
    assert.ok(
      candidates.includes(
        "C:\\Users\\testuser\\AppData\\Local\\Programs\\Python\\Python312\\Scripts\\agitrack.exe",
      ),
      "missing Python312 Scripts",
    );
    assert.ok(
      candidates.includes(
        "C:\\Users\\testuser\\AppData\\Local\\Programs\\Python\\Python311\\Scripts\\agitrack.exe",
      ),
      "missing Python311 Scripts",
    );
    // Every candidate ends with agitrack.exe
    for (const c of candidates) {
      assert.ok(c.endsWith("agitrack.exe"), `unexpected exe name: ${c}`);
    }
  } finally {
    process.env.APPDATA = origAppdata;
    process.env.LOCALAPPDATA = origLocalappdata;
  }
});

test("staticExeCandidates on win32 delegates to windowsExeCandidates (.exe suffix)", () => {
  const candidates = staticExeCandidates("C:\\Users\\u", "win32", []);
  for (const c of candidates) {
    assert.ok(c.endsWith("agitrack.exe"), `expected .exe suffix: ${c}`);
  }
});

test("dedupe preserves first-seen order so the authoritative candidate stays first", () => {
  const input = ["agitrack", "/opt/homebrew/bin/agitrack", "agitrack", "/usr/local/bin/agitrack"];
  assert.deepEqual(dedupe(input), ["agitrack", "/opt/homebrew/bin/agitrack", "/usr/local/bin/agitrack"]);
});
