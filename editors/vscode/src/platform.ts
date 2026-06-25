/** Detect whether the extension is running on a native Windows host.
 *
 * The extension runs on the workspace machine (`extensionKind: ["workspace"]`),
 * so a Windows + WSL / Remote-SSH / Dev Container / Codespaces window runs this
 * code on the Linux side (`platform !== "win32"`) and works exactly as on
 * macOS/Linux.  Only a LOCAL Windows window is `"win32"` here.
 *
 * aGiTrack now supports native Windows (ConPTY via pywinpty), so this helper
 * is kept for feature-detection purposes rather than blocking.
 *
 * Parameterised on the platform string (defaulting to the current process) so it
 * can be unit-tested without spoofing `process.platform`. */
export function isNativeWindows(platform: NodeJS.Platform = process.platform): boolean {
  return platform === "win32";
}
