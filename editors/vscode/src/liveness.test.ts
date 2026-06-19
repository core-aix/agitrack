import assert from "node:assert/strict";
import { test } from "node:test";

import { sessionLooksLive } from "./liveness";

const GRACE = 15_000;
const base = { shellExited: false, msSinceLaunch: GRACE + 1, lockAlive: false, shellHasChild: false, graceMs: GRACE };

// The aG button reuses a terminal only when aGiTrack is actually running in it; a non-zero
// exit leaves the shell open, and that lingering shell must NOT be treated as live.

test("a closed terminal is never live", () => {
  assert.equal(sessionLooksLive({ ...base, shellExited: true, lockAlive: true, shellHasChild: true }), false);
});

test("a just-launched session is live during the startup grace (lock not taken yet)", () => {
  assert.equal(sessionLooksLive({ ...base, msSinceLaunch: 1_000 }), true);
});

test("a running session holding the repo lock is live", () => {
  assert.equal(sessionLooksLive({ ...base, lockAlive: true }), true);
});

test("a session at the pre-lock privacy prompt is live via its shell child", () => {
  // No lock yet, past the grace window, but the shell still has the aGiTrack child.
  assert.equal(sessionLooksLive({ ...base, lockAlive: false, shellHasChild: true }), true);
});

test("an exited session leaving the shell open is NOT live (the bug)", () => {
  // Past grace, no lock, no child → just a lingering shell after a non-zero exit.
  assert.equal(sessionLooksLive(base), false);
});
