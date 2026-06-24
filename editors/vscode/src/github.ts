/** GitHub-CLI (`gh`) sign-in helpers for the extension.
 *
 * aGiTrack uses `gh` for the dashboard's committer identities and for session sharing.
 * When `gh` is installed but not authenticated, aGiTrack's TUI says those features are
 * limited and tells the user to run `gh auth login` — but aGiTrack's full-screen TUI owns
 * the terminal, so there is no shell prompt to type that into, and a GUI-launched VSCode's
 * terminal child often can't read `gh`'s credentials anyway (e.g. the token lives in the
 * macOS Keychain, which that child can't reach), so the user is stuck (#…).
 *
 * The extension therefore offers a one-click "Sign in to GitHub" that opens a plain
 * terminal running `gh auth login`. This module holds only the PURE decision logic so it
 * is unit-testable without the `vscode` module; the terminal/notification plumbing lives in
 * extension.ts. */

/** The three states of the `gh` CLI from the extension's point of view, mirroring the
 * CLI's own `gh_status()`: installed-and-authenticated, not installed, or installed but
 * not logged in (or the auth check failed — e.g. it couldn't reach the Keychain). */
export type GhStatus = "ok" | "missing" | "unauthenticated";

/** Whether `git remote -v` output names a GitHub remote. `gh` only matters when the repo
 * actually lives on GitHub (committer identities, session sharing), so the sign-in prompt
 * is gated on this to avoid nagging on local-only or non-GitHub repos. */
export function hasGithubRemoteUrl(remoteOutput: string): boolean {
  return /github\.com/i.test(remoteOutput);
}

/** Decide whether to surface the GitHub sign-in prompt for a just-launched session.
 *
 * Only when `gh` is installed but UNAUTHENTICATED (the actionable case — `missing` needs an
 * install, `ok` needs nothing), the repo has a GitHub remote (so `gh` is relevant), the user
 * hasn't dismissed the prompt for good, and we haven't already prompted this activation (so a
 * relaunch in the same window doesn't re-nag). */
export function shouldPromptGithubSignIn(opts: {
  status: GhStatus;
  hasGithubRemote: boolean;
  suppressed: boolean;
  alreadyPrompted: boolean;
}): boolean {
  if (opts.suppressed || opts.alreadyPrompted) {
    return false;
  }
  if (opts.status !== "unauthenticated") {
    return false;
  }
  return opts.hasGithubRemote;
}
