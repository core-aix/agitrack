import assert from "node:assert/strict";
import { test } from "node:test";

import { isNativeWindows } from "./platform";

// aGiTrack now supports native Windows (ConPTY via pywinpty), so isNativeWindows is kept
// for feature-detection but no longer blocks operation.

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
