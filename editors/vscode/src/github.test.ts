import assert from "node:assert/strict";
import { test } from "node:test";

import { hasGithubRemoteUrl, shouldPromptGithubSignIn } from "./github";

// `gh` only matters when the repo is actually on GitHub, so the prompt is gated on a
// GitHub remote. The match is host-based (ssh or https) and case-insensitive.
test("hasGithubRemoteUrl detects ssh and https GitHub remotes", () => {
  assert.equal(hasGithubRemoteUrl("origin\tgit@github.com:owner/repo.git (fetch)"), true);
  assert.equal(hasGithubRemoteUrl("origin\thttps://github.com/owner/repo.git (push)"), true);
  assert.equal(hasGithubRemoteUrl("origin\thttps://GitHub.com/Owner/Repo (fetch)"), true);
});

test("hasGithubRemoteUrl is false for non-GitHub or absent remotes", () => {
  assert.equal(hasGithubRemoteUrl("origin\tgit@gitlab.com:owner/repo.git (fetch)"), false);
  assert.equal(hasGithubRemoteUrl(""), false);
});

// The prompt fires ONLY for the actionable case: gh installed but unauthenticated, on a
// GitHub repo, not suppressed, not already shown this activation.
test("shouldPromptGithubSignIn fires when unauthenticated on a GitHub repo", () => {
  assert.equal(
    shouldPromptGithubSignIn({
      status: "unauthenticated",
      hasGithubRemote: true,
      suppressed: false,
      alreadyPrompted: false,
    }),
    true,
  );
});

test("shouldPromptGithubSignIn stays silent when gh is ok or missing", () => {
  for (const status of ["ok", "missing"] as const) {
    assert.equal(
      shouldPromptGithubSignIn({ status, hasGithubRemote: true, suppressed: false, alreadyPrompted: false }),
      false,
      `expected no prompt for status=${status}`,
    );
  }
});

test("shouldPromptGithubSignIn stays silent without a GitHub remote", () => {
  assert.equal(
    shouldPromptGithubSignIn({
      status: "unauthenticated",
      hasGithubRemote: false,
      suppressed: false,
      alreadyPrompted: false,
    }),
    false,
  );
});

test("shouldPromptGithubSignIn respects dismissal and once-per-activation gating", () => {
  const base = { status: "unauthenticated", hasGithubRemote: true } as const;
  assert.equal(shouldPromptGithubSignIn({ ...base, suppressed: true, alreadyPrompted: false }), false);
  assert.equal(shouldPromptGithubSignIn({ ...base, suppressed: false, alreadyPrompted: true }), false);
});
