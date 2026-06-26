/** Locating a freshly-installed `agitrack` console script.
 *
 * A VSCode launched from Finder/Dock (rather than a shell) doesn't inherit the user's
 * shell PATH, so a bare `agitrack` lookup fails even after a successful install — the
 * cause of issue #93. We therefore probe a set of absolute locations the installers are
 * known to use. The dynamic, authoritative answer (pipx's `PIPX_BIN_DIR`, pip's
 * `--user-base`) is queried in extension.ts; this module builds the static fallbacks and
 * is kept dependency-free so it can be unit-tested without a filesystem or VSCode.
 */

import { join } from "path";

/** The console-script filename: `agitrack.exe` on Windows, `agitrack` elsewhere. */
export function exeName(platform: NodeJS.Platform): string {
  return platform === "win32" ? "agitrack.exe" : "agitrack";
}

/** Ordered absolute candidate paths for the `agitrack` executable, for the common
 * install locations on a host:
 *  - POSIX: `~/.local/bin` (pipx default; pip --user on many Linux setups), macOS
 *    framework Python user scripts `~/Library/Python/<X.Y>/bin` (one per version found),
 *    Homebrew bin dirs (`/opt/homebrew/bin`, `/usr/local/bin`)
 *  - Windows: `~/.local/bin` (pipx default) and the pip `--user` Scripts dirs under
 *    `%APPDATA%\Python` and a per-user Python install — all holding `agitrack.exe`
 *
 * Pure and parameterised (no `process`/`fs` access) so it is unit-testable. */
export function staticExeCandidates(
  home: string,
  platform: NodeJS.Platform,
  libraryPythonVersions: string[] = [],
): string[] {
  const exe = exeName(platform);
  if (platform === "win32") {
    const dirs = [
      join(home, ".local", "bin"), // pipx default bin dir on Windows
      join(home, "AppData", "Roaming", "Python", "Scripts"), // pip --user (%APPDATA%\Python)
      join(home, "AppData", "Local", "Programs", "Python", "Scripts"), // per-user Python
    ];
    return dirs.map((dir) => join(dir, exe));
  }
  const dirs: string[] = [join(home, ".local", "bin")];
  if (platform === "darwin") {
    for (const version of libraryPythonVersions) {
      dirs.push(join(home, "Library", "Python", version, "bin"));
    }
    dirs.push("/opt/homebrew/bin", "/usr/local/bin");
  } else {
    dirs.push("/usr/local/bin");
  }
  return dirs.map((dir) => join(dir, exe));
}

/** De-duplicate while preserving first-seen order (so the most authoritative candidate,
 * listed first, is tried first and not dropped). */
export function dedupe(values: string[]): string[] {
  return [...new Set(values)];
}
