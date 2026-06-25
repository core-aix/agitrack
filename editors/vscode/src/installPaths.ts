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

/** The executable name, platform-aware: `.exe` suffix on Windows. */
const EXE = process.platform === "win32" ? "agitrack.exe" : "agitrack";

/** Ordered absolute candidate paths for the `agitrack` executable on the current host.
 *
 * POSIX (Linux / macOS):
 *  - `~/.local/bin` (pipx default; pip --user on many Linux setups)
 *  - macOS framework Python user scripts: `~/Library/Python/<X.Y>/bin`
 *  - Homebrew bin dirs (`/opt/homebrew/bin` on Apple Silicon, `/usr/local/bin`)
 *
 * Windows:
 *  - `%APPDATA%\Python\Scripts` (pip --user on Windows)
 *  - `%LOCALAPPDATA%\Programs\Python\PythonXY\Scripts` (per-version user installs)
 *  - `%USERPROFILE%\.local\bin` (pipx default when configured that way)
 *  - `%APPDATA%\Local\pipx\venvs\agitrack\Scripts` (pipx venv on Windows)
 *  - `%APPDATA%\Python\PythonXY\Scripts` (alternate pip --user layout)
 *
 * Pure and parameterised (no `process`/`fs` access) so it is unit-testable. */
export function staticExeCandidates(
  home: string,
  platform: NodeJS.Platform,
  libraryPythonVersions: string[] = [],
): string[] {
  if (platform === "win32") {
    return windowsExeCandidates(home, libraryPythonVersions);
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
  return dirs.map((dir) => join(dir, EXE));
}

/** Windows-specific candidate paths for the agitrack executable.
 *
 * Covers the three most common install locations on Windows:
 *  1. `%APPDATA%\Python\Scripts` — `pip install --user` default
 *  2. `%LOCALAPPDATA%\Programs\Python\PythonXY\Scripts` — per-version installs
 *  3. pipx venv at `%LOCALAPPDATA%\pipx\venvs\agitrack\Scripts`
 *  4. `%USERPROFILE%\.local\bin` — pipx with custom PIPX_BIN_DIR
 *
 * `windowsPythonVersions` should list the `XY` version strings found under
 * `%LOCALAPPDATA%\Programs\Python\` (e.g. `["Python312", "Python311"]`). */
export function windowsExeCandidates(home: string, windowsPythonVersions: string[] = []): string[] {
  const appdata = process.env.APPDATA || join(home, "AppData", "Roaming");
  const localappdata = process.env.LOCALAPPDATA || join(home, "AppData", "Local");

  const dirs: string[] = [
    // pip --user (the most common case)
    join(appdata, "Python", "Scripts"),
    // pipx venv
    join(localappdata, "pipx", "venvs", "agitrack", "Scripts"),
    // ~/.local/bin (pipx with PIPX_BIN_DIR set, or Git Bash convention)
    join(home, ".local", "bin"),
  ];

  // Per-version installs: %LOCALAPPDATA%\Programs\Python\PythonXY\Scripts
  for (const version of windowsPythonVersions) {
    dirs.push(join(localappdata, "Programs", "Python", version, "Scripts"));
    // Also cover %APPDATA%\Python\PythonXY\Scripts (alternate layout)
    dirs.push(join(appdata, "Python", version, "Scripts"));
  }

  return dirs.map((dir) => join(dir, EXE));
}

/** De-duplicate while preserving first-seen order (so the most authoritative candidate,
 * listed first, is tried first and not dropped). */
export function dedupe(values: string[]): string[] {
  return [...new Set(values)];
}
