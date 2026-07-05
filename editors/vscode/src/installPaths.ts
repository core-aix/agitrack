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

/** Map a list of console-script directories (e.g. the ones a Python reports via
 * `sysconfig`) to the `agitrack(.exe)` path inside each. Pure and testable; the caller
 * probes the results for a runnable executable. */
export function exeCandidatesFromScriptDirs(scriptDirs: string[], platform: NodeJS.Platform): string[] {
  const exe = exeName(platform);
  return scriptDirs.map((dir) => join(dir, exe));
}

/** Ordered absolute candidate paths for the `agitrack` executable, for the common
 * install locations on a host:
 *  - POSIX: `~/.local/bin` (pipx default; pip --user on many Linux setups), macOS
 *    framework Python user scripts `~/Library/Python/<X.Y>/bin` (one per version found),
 *    Homebrew bin dirs (`/opt/homebrew/bin`, `/usr/local/bin`)
 *  - Windows: `~/.local/bin` (pipx default) and the pip `--user` / per-user-Python Scripts
 *    dirs — which live under a **version subfolder** `Python<XY>\Scripts`, not directly
 *    under `%APPDATA%\Python\Scripts` (issue #140: guessing the version-less path never
 *    finds `agitrack.exe`). Pass the discovered `Python<XY>` folder names in
 *    `winPythonVersions`; the version-less dirs are kept only as a last-resort fallback.
 *
 * Pure and parameterised (no `process`/`fs` access) so it is unit-testable. */
export function staticExeCandidates(
  home: string,
  platform: NodeJS.Platform,
  libraryPythonVersions: string[] = [],
  winPythonVersions: string[] = [],
): string[] {
  const exe = exeName(platform);
  if (platform === "win32") {
    const roaming = join(home, "AppData", "Roaming", "Python"); // pip --user
    const programs = join(home, "AppData", "Local", "Programs", "Python"); // per-user Python
    const dirs = [join(home, ".local", "bin")]; // pipx default bin dir on Windows
    // The authoritative location: %APPDATA%\Python\Python<XY>\Scripts (and the per-user
    // Python equivalent). Both roots share the same Python<XY> version-folder naming.
    for (const version of winPythonVersions) {
      dirs.push(join(roaming, version, "Scripts"));
      dirs.push(join(programs, version, "Scripts"));
    }
    // Version-less fallbacks for older/other pip layouts.
    dirs.push(join(roaming, "Scripts"), join(programs, "Scripts"));
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
