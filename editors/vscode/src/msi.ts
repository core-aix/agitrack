/** Locating and installing aGiTrack from the standalone Windows MSI.
 *
 * The MSI (built by `.github/workflows/release.yml`, attached to every GitHub release) is a
 * complete, self-contained install: a PyInstaller bundle, so the target needs no
 * Python/pip at all. The extension falls back to it on Windows when neither pipx nor pip is
 * available (some machines have no Python), and uses it to discover an MSI a user installed
 * by hand. This module holds only the PURE logic — asset selection, install-path
 * derivation, URL construction — so it is unit-testable without the network, the
 * filesystem, or the `vscode` module; the download + `msiexec` plumbing lives in
 * extension.ts. */

import { win32 } from "path";

/** owner/repo the release MSI is published under. Overridable via the `AGITRACK_GH_REPO`
 * environment variable (useful for forks / pre-release testing). */
export const GITHUB_REPO_DEFAULT = "core-aix/agitrack";

export function githubRepo(env: NodeJS.ProcessEnv = {}): string {
  const override = (env.AGITRACK_GH_REPO || "").trim();
  return override || GITHUB_REPO_DEFAULT;
}

/** The GitHub REST endpoint for the most recent published release of `repo`. */
export function releasesApiUrl(repo: string): string {
  return `https://api.github.com/repos/${repo}/releases/latest`;
}

/** The human-facing releases page, opened when an automatic MSI install can't proceed. */
export function latestReleasePageUrl(repo: string): string {
  return `https://github.com/${repo}/releases/latest`;
}

export interface MsiAsset {
  name: string;
  url: string;
}

/** The release asset filename the build produces: `agitrack-<version>-windows-x64.msi`. */
const MSI_ASSET_RE = /^agitrack-.+-windows-x64\.msi$/i;

/** Pick the Windows x64 MSI asset out of a GitHub release `assets` array, or `undefined`
 * when the release carries none. Defensive about shape — the value comes straight off the
 * GitHub API JSON, so anything missing a string `name` / `browser_download_url` is skipped
 * rather than throwing. */
export function pickMsiAsset(assets: unknown): MsiAsset | undefined {
  if (!Array.isArray(assets)) {
    return undefined;
  }
  for (const asset of assets) {
    const name = asset?.name;
    const url = asset?.browser_download_url;
    if (typeof name === "string" && typeof url === "string" && MSI_ASSET_RE.test(name)) {
      return { name, url };
    }
  }
  return undefined;
}

/** The Program Files roots an MSI perMachine install could land under, newest-API-first.
 * `ProgramW6432` and `ProgramFiles` point at the 64-bit dir from a 64-bit process; the
 * `(x86)` dir is included so resolution still finds an install seen from a 32-bit context.
 * Falls back to the conventional path when the env vars are absent (e.g. on a non-Windows
 * unit-test host). Deduped, first-seen order preserved. */
export function programFilesDirs(env: NodeJS.ProcessEnv = {}): string[] {
  const dirs = [env.ProgramW6432, env.ProgramFiles, env["ProgramFiles(x86)"]].filter(
    (dir): dir is string => typeof dir === "string" && dir.length > 0,
  );
  if (dirs.length === 0) {
    dirs.push("C:\\Program Files");
  }
  return [...new Set(dirs)];
}

/** Absolute candidate paths for an MSI-installed `agitrack.exe` (the WiX `INSTALLFOLDER` is
 * `<Program Files>\aGiTrack`). Used both to resolve the exe right after an MSI install and
 * to discover an MSI the user installed by hand — a GUI-launched VSCode doesn't inherit the
 * shell PATH the MSI extends, so a bare `agitrack` lookup can miss it (issue #93). */
export function msiInstallCandidates(env: NodeJS.ProcessEnv = {}): string[] {
  return programFilesDirs(env).map((dir) => win32.join(dir, "aGiTrack", "agitrack.exe"));
}
