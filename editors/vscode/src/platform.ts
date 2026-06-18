/** aGiTrack is POSIX-only (it imports pty/termios/fcntl at load), so it cannot run on a
 * native Windows host. The extension runs on the workspace machine (`extensionKind:
 * ["workspace"]`), so a Windows + WSL / Remote-SSH / Dev Container / Codespaces window runs
 * this code on the Linux side (`platform !== "win32"`) and works exactly as on macOS/Linux;
 * only a LOCAL Windows window is `"win32"` here — the one case aGiTrack can't support.
 *
 * Parameterised on the platform string (defaulting to the current process) so it can be
 * unit-tested without spoofing `process.platform`. */
export function isNativeWindows(platform: NodeJS.Platform = process.platform): boolean {
  return platform === "win32";
}
