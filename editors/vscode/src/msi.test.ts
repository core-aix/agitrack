import assert from "node:assert/strict";
import { test } from "node:test";

import {
  GITHUB_REPO_DEFAULT,
  githubRepo,
  latestReleasePageUrl,
  msiInstallCandidates,
  pickMsiAsset,
  programFilesDirs,
  releasesApiUrl,
} from "./msi";

// The extension falls back to the standalone MSI on Windows when no pipx/pip is present.
// These pin the pure logic: which release asset is the installer, where it lands, and the
// repo/URLs we reach for it — all unit-testable without the network or a real Windows host.

test("githubRepo defaults to core-aix/agitrack and honours AGITRACK_GH_REPO", () => {
  assert.equal(githubRepo({}), GITHUB_REPO_DEFAULT);
  assert.equal(githubRepo({ AGITRACK_GH_REPO: "me/fork" }), "me/fork");
  assert.equal(githubRepo({ AGITRACK_GH_REPO: "  " }), GITHUB_REPO_DEFAULT); // blank → default
});

test("releasesApiUrl / latestReleasePageUrl build the expected GitHub URLs", () => {
  assert.equal(releasesApiUrl("core-aix/agitrack"), "https://api.github.com/repos/core-aix/agitrack/releases/latest");
  assert.equal(latestReleasePageUrl("core-aix/agitrack"), "https://github.com/core-aix/agitrack/releases/latest");
});

test("pickMsiAsset selects the windows-x64 .msi and returns its download URL", () => {
  const assets = [
    { name: "agitrack-0.1.10.tar.gz", browser_download_url: "https://x/tar" },
    { name: "agitrack-0.1.10-windows-x64.msi", browser_download_url: "https://x/msi" },
  ];
  assert.deepEqual(pickMsiAsset(assets), {
    name: "agitrack-0.1.10-windows-x64.msi",
    url: "https://x/msi",
  });
});

test("pickMsiAsset is case-insensitive on the asset name", () => {
  const assets = [{ name: "AgiTrack-1.2.3-WINDOWS-X64.MSI", browser_download_url: "https://x/msi" }];
  assert.equal(pickMsiAsset(assets)?.url, "https://x/msi");
});

test("pickMsiAsset returns undefined when no MSI asset is present", () => {
  assert.equal(pickMsiAsset([{ name: "agitrack-1.0.0-macos.dmg", browser_download_url: "https://x/dmg" }]), undefined);
  assert.equal(pickMsiAsset([]), undefined);
});

test("pickMsiAsset is defensive about malformed asset shapes", () => {
  // Non-array, and entries missing a string name / url, must not throw — just be skipped.
  assert.equal(pickMsiAsset(undefined), undefined);
  assert.equal(pickMsiAsset({ not: "an array" }), undefined);
  assert.equal(pickMsiAsset([{ name: "agitrack-1-windows-x64.msi" }, null, 7]), undefined); // no url
});

test("programFilesDirs prefers ProgramW6432, includes (x86), dedupes, and falls back", () => {
  const dirs = programFilesDirs({
    ProgramW6432: "C:\\Program Files",
    ProgramFiles: "C:\\Program Files",
    "ProgramFiles(x86)": "C:\\Program Files (x86)",
  });
  assert.deepEqual(dirs, ["C:\\Program Files", "C:\\Program Files (x86)"]); // duplicate 64-bit dir collapsed
  // No env (e.g. a POSIX test host): a conventional default keeps the candidate list usable.
  assert.deepEqual(programFilesDirs({}), ["C:\\Program Files"]);
});

test("msiInstallCandidates points at <Program Files>\\aGiTrack\\agitrack.exe", () => {
  const candidates = msiInstallCandidates({
    ProgramFiles: "C:\\Program Files",
    "ProgramFiles(x86)": "C:\\Program Files (x86)",
  });
  assert.ok(candidates.includes("C:\\Program Files\\aGiTrack\\agitrack.exe"));
  assert.ok(candidates.includes("C:\\Program Files (x86)\\aGiTrack\\agitrack.exe"));
  assert.ok(candidates.every((c) => c.endsWith("\\aGiTrack\\agitrack.exe")));
});
