/** Deciding whether a tracked terminal still has a *running* aGiTrack session.
 *
 * The aG button reuses an existing session terminal instead of starting a second one.
 * But aGiTrack only closes its terminal on a clean exit (`… && exit`); a non-zero exit
 * leaves the shell open, so the terminal lingers in the tracking map. Reusing it then
 * just re-focuses a dead shell — the aG button appears to "do nothing." So map presence
 * isn't enough; we confirm a process is actually there.
 *
 * aGiTrack takes the repo lock at the very start now — before the privacy prompt — so a
 * session holds it from launch (lockAlive) and that is the primary, reliable signal. There
 * is still a brief window between opening the terminal and aGiTrack acquiring the lock (the
 * shell's own startup), so we also accept a freshly-launched session (graceMs) or one whose
 * shell already has the aGiTrack child. This combines the available signals; pure and
 * parameterised so the policy is unit-testable without VSCode or a real process.
 */

export interface LivenessSignals {
  /** The terminal's own shell process has exited (VSCode set `Terminal.exitStatus`). */
  shellExited: boolean;
  /** Milliseconds since we launched aGiTrack in this terminal. */
  msSinceLaunch: number;
  /** A live process holds the repo lock (`.agitrack/lock` PID is alive). */
  lockAlive: boolean;
  /** The terminal's shell currently has a child process (aGiTrack still running —
   * including before it has acquired the lock, e.g. at the privacy prompt). */
  shellHasChild: boolean;
  /** Grace window after launch during which we always assume the session is coming up
   * (the shell may still be running its own startup before aGiTrack even starts). */
  graceMs: number;
}

export function sessionLooksLive(signals: LivenessSignals): boolean {
  if (signals.shellExited) {
    return false; // the terminal itself is gone
  }
  if (signals.msSinceLaunch < signals.graceMs) {
    return true; // just launched — give it time to come up before judging it dead
  }
  // Past the grace window: alive only if something is actually running there.
  return signals.lockAlive || signals.shellHasChild;
}
