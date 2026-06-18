# Changelog

## 0.1.0

Full interactive aGiTrack inside VSCode — no terminal required.

- aGiTrack now runs as a long-lived process per workspace, driven over a
  bidirectional JSON-RPC bridge (`agitrack --mode json --ui-bridge`).
- Interactive questions (stage which untracked files? pick a backend? commit
  message?) are rendered as native VSCode menus, input boxes, and modal
  confirmations instead of terminal prompts.
- Command Palette actions under **aGiTrack:** — Show Git Status, Review & Stage
  Untracked Files, Create User Commit, Show Intentionally Unstaged Files, Switch
  Agent Backend, Start New Session, Manage Summarizer, Restart aGiTrack.
- The `@agitrack` chat participant streams responses and shows the auto-commit
  for each turn.

## 0.0.1

- Initial chat participant that shells out to `agitrack --mode json --json-events`.
