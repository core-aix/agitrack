/** Whether this is a native-Windows host (as opposed to the Linux side of a WSL /
 * Remote-SSH / Dev Container / Codespaces window, which reports a POSIX platform).
 *
 * aGiTrack runs natively on Windows as of #118, so this is no longer a "can't run" gate —
 * it selects the Windows code paths (PowerShell process queries, the shutdown sentinel +
 * taskkill fallback, `.exe`/Scripts install locations, PowerShell launch syntax) from the
 * POSIX ones. Parameterised on the platform string (defaulting to the current process) so
 * it can be unit-tested without spoofing `process.platform`. */
export function isNativeWindows(platform: NodeJS.Platform = process.platform): boolean {
  return platform === "win32";
}
