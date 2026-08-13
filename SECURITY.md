# Security policy

aGiTrack wraps a coding agent's session and writes what happened into your git history. That
means it handles two things worth protecting: the transcript of your agent sessions (which can
contain anything you or the agent typed, including secrets) and your repository itself. This
document says how to report a problem with either, and which behaviours are deliberate rather
than bugs.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately through GitHub security advisories:

**https://github.com/core-aix/agitrack/security/advisories/new**

If you cannot use that form, open a public issue containing only "security report, please make
contact" and no detail, and a maintainer will arrange a private channel.

A useful report includes:

- your aGiTrack version (`agitrack --version`), OS, and which backend you were on
  (Claude Code, Codex or OpenCode),
- what an attacker gains, and what access they need to start,
- the smallest reproduction you can manage. A failing test in the style of `tests/` is the
  single most useful thing you can attach,
- any sanitized log or trace. Please redact your own secrets before sending: if the report is
  about a secret leaking, describe the SHAPE of the token rather than pasting a live one.

This is a small project, so these are honest targets rather than a contractual SLA: an
acknowledgement within 3 working days, an assessment within 7, and a fix released as soon as it
is ready and verified. You will be credited in the advisory unless you would rather not be.
Please give us a chance to ship a fix before publishing, and tell us if you have a disclosure
deadline so we can plan around it.

## Supported versions

Only the most recent release is supported. Every merge into `main` cuts a release automatically
(version bump, PyPI, the VS Code Marketplace, a GitHub Release), so the latest version is never
far from `main`.

| Version | Supported |
| --- | --- |
| Latest release (currently 0.6.x) | Yes |
| Anything older | No: please upgrade before reporting |

Fixes land as a new patch release. There are no backports to older lines.

## What aGiTrack touches

Knowing where the trust boundaries sit will tell you whether what you found is a vulnerability.

**It runs locally, on your machine, as you.** There is no aGiTrack service and no telemetry.
The only outbound connections are the GitHub API (to resolve contributor logins and avatars for
the dashboard), the updater (to check for and download new releases), and the Google Fonts
stylesheet referenced by the generated web pages. Your transcripts and diffs are not sent
anywhere by aGiTrack.

**It writes transcripts into commit messages.** Commit messages and interaction traces carry
the agent's turns, so `agitrack/commits/message.py` masks secrets before anything is committed:
generic `name = value` assignments (`api_key`, `token`, `password`, and similar) plus the
high-confidence token shapes that GitHub secret-scanning blocks (`sk-`, `ghp_`, `github_pat_`,
AWS key ids, Slack, Stripe, PyPI, PEM private-key blocks, and more). Absolute filesystem paths
are replaced with `[PATH]` so a pushed commit does not leak your home directory or account
name. **Masking is a security control, and a way around it is a vulnerability.** See "In scope"
below.

**The dashboard is unauthenticated, by design.** `agitrack --dashboard` serves your commits and
diffs over plain HTTP with no login. On your own machine it binds loopback. In a remote shell
(SSH or Mosh) loopback would be useless, so it binds all interfaces instead, prints a warning
saying so, and tells you how to opt out. Anyone who can route to that host and past its firewall
can read the repository it is serving. To keep it loopback-only everywhere and tunnel in
yourself:

```bash
export AGITRACK_DASHBOARD_HOST=127.0.0.1
ssh -N -L 8765:localhost:8765 you@remote-host   # then open http://localhost:8765/
```

**Exports embed real history.** `agitrack -d export` writes a self-contained copy of the
dashboard, with commit messages, diffs and author identities baked into the files, meant for
static hosting. Read what you are about to publish before you publish it.

**The agent's own sandbox is the agent's.** aGiTrack launches Claude Code, Codex or OpenCode and
records what they do. It does not add a sandbox, weaken one, or decide which tool calls those
agents are allowed to make. Permission and sandbox behaviour belongs to the backend you chose,
and its sandboxes do not nest inside another one.

## In scope

Report these:

- a secret shape that survives masking and reaches a commit message, an interaction trace, the
  dashboard or an export,
- absolute-path masking that can be bypassed, leaking a home directory or account name,
- reading files outside the repository through the dashboard HTTP server (path traversal), or
  any endpoint that mutates state (it is meant to be read-only),
- injection of attacker-controlled repository content into a page as script, given that commit
  messages, branch names and file paths all come from the repo and are rendered into HTML,
- a crafted branch name, ref, path or filename that turns into command execution through the git
  or backend subprocess layer,
- destructive git behaviour: losing committed work, corrupting refs, or escaping the
  single-writer lock into a concurrent write,
- privilege or path problems in the installers (the MSI, the pipx and Homebrew paths) or in the
  installed hooks, including anything writable by a less privileged user that later runs with
  more,
- the updater fetching or executing a release without validating what it downloaded.

## Out of scope

These are known and deliberate, so a report of them will be closed with a pointer here:

- the dashboard being reachable from your network after you started it in an SSH session. It is
  documented, it warns at startup, and `AGITRACK_DASHBOARD_HOST=127.0.0.1` turns it off,
- the dashboard having no authentication at all,
- anything that assumes the attacker already has your local user account, your shell, or write
  access to the repository. At that point they do not need aGiTrack,
- vulnerabilities in Claude Code, Codex or OpenCode themselves. Please report those to their
  own projects,
- vulnerabilities in the coding agent's own decisions: aGiTrack records what the agent did, it
  does not vet it,
- advisories against dev-only dependencies that ship in nothing, notably `editors/vscode/node_modules`,
- missing security headers, a missing `SameSite` attribute, or similar hardening findings on the
  local dashboard with no demonstrated impact,
- output from an automated scanner with no working reproduction against a released version.

## If you have already leaked a secret

Rotate it first: that is the only step that actually helps. Then, because aGiTrack puts
transcripts in commits, know that rewriting history does not reliably erase a secret that has
been pushed to a hosted remote, since the old objects can stay reachable by SHA until the host
garbage-collects them. Rotate, then clean up.

If the leak got there because masking missed the token shape, that is a vulnerability in
aGiTrack. Please report it through the advisory link above so the pattern gets added.
