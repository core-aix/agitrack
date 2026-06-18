import assert from "node:assert/strict";
import { test } from "node:test";

import { isNativeWindows } from "./platform";

// aGiTrack is POSIX-only, so the extension blocks (and points users to WSL) on a native
// Windows host. The check must be true ONLY for win32, and false on the Linux side that a
// Windows + WSL / Remote-SSH / Dev Container window actually runs on — otherwise it would
// wrongly block the supported remote path.

test("isNativeWindows is true only on native Windows (win32)", () => {
  assert.equal(isNativeWindows("win32"), true);
});

test("isNativeWindows is false on POSIX platforms (incl. the Linux side of WSL/remote)", () => {
  for (const platform of ["linux", "darwin", "freebsd", "openbsd", "sunos", "aix"] as NodeJS.Platform[]) {
    assert.equal(isNativeWindows(platform), false, `expected ${platform} to be supported`);
  }
});

test("isNativeWindows defaults to the current process platform", () => {
  assert.equal(isNativeWindows(), process.platform === "win32");
});
