# aGiTrack Requirements

aGiTrack stands for agent + git tracking. It is a Python library and interactive CLI that combines coding-agent command line tools with automatic Git commits so agentic code changes are easier to trace.

## Goals

- Provide a common interactive interface for coding-agent backends and Git commit automation.
- Support OpenCode and Claude (Claude Code) as interchangeable backends; every aGiTrack feature works the same regardless of the selected backend.
- Keep the user experience similar to the selected backend CLI, while adding aGiTrack commands for agent switching, user commits, staging, status, and configuration.
- Make agentic code changes traceable by automatically committing code changes made after agent prompts.
- Support running aGiTrack from any folder against a target working repository.

## Commit Types

- Agent commits use the `<aGiTrack>` tag.
- User-triggered commits use the user-provided subject without an aGiTrack subject tag.
- Before an agent acts, if user changes already exist, aGiTrack creates a separate user commit first.
- A commit is created only when code has changed and staged changes exist.

## Commit Messages

- Agent commit subjects start with `<aGiTrack>` followed immediately by the latest user query for readability.
- User commit subjects use the user-provided commit message.
- User commit messages are required; blank user commit messages are rejected.
- Agent commit bodies include the full interaction trace since the last code-changing commit.
- The interaction trace includes full user prompts and final agent responses.
- Do not include thinking tokens or intermediate responses.
- Bare slash commands (`/compact`, `/comp`, `/model`, `/clear`, …) are backend/TUI directives, not prompts, and must be kept out of the interaction trace. The transcript parser already drops them; the proxy's own submit-time prompt capture (`CommitEngine.record_user_prompt`) must apply the same exclusion (`_is_slash_command`), or a typed `/compact` leaks into the commit as a stray `## User` / `/comp` entry — which is also redundant with the compaction lead-in note the trace already carries.
- Commit subjects and bodies must not contain terminal escape sequences or control characters; strip arrow-key/escape residue both where the prompt is captured and when building the message.
- Commit bodies include metadata such as backend, session name, backend session ID, aGiTrack session ID, model, commit type, and timestamps.
- Agent commit metadata includes the current context token count and token usage accumulated since the last code-changing commit.
- Token counts must be exact for every category (input, output, cache read/write, reasoning, and their sub-agent equivalents): count each backend message's reported usage exactly once — not more, not less. Claude splits one assistant response across several transcript rows that repeat the same usage (de-duplicate by message id), and sub-agents are recorded separately (own transcript files for Claude, child sessions for OpenCode) and must be fully included. The token counts are a headline metric, so changes to transcript parsing must be verified against real backend data, not only mocks.
- A turn's `input` is *fresh* input = uncached input **+ cache-creation (`cache_write`) tokens**, because cache-creation tokens are input the model processed once on the way into the cache. This is deliberately different from the provider's billing model (Anthropic prices cache writes, cache reads, and uncached input as three separate line items at different rates); aGiTrack folds cache-creation into `input` so the number answers "how much fresh context did this turn process?" rather than mirroring the price sheet. Keep the raw breakdown recoverable: still record `cache_write` (newly cached) and `cache_read` (replayed from cache, never folded into `input` — it was already counted when first written) on their own lines. The summarizer's cost obeys the same rule (`summary_tokens_input` includes its cache-creation; `summary_tokens_cache_read` is separate). The same fact is documented for users in the README token-accounting section — keep the two in sync.
- The summarizer must read **only** its instruction prompt plus the interaction trace (the same `## User` / `## Agent` text the commit carries) and nothing else. Run the summarizer backend in `bare` mode — a plain text completion with no tools, no project/user memory or MCP servers, and the agent's default system prompt replaced with a minimal one — so the summary isn't charged thousands of input tokens of system prompt and tool schemas it never uses (a real Claude call dropped from ~18,000 input tokens to a few hundred). `bare` is plumbed through `AgentBackend.run(..., bare=...)`; the main coding agent (and shell mode) must NOT use it, since it needs its tools. Keep this scoped to the summarizer.
- The summarizer is a mechanical text-reduction task, so its `bare` run also drops model thinking/reasoning to the floor (it gains nothing from it and it only adds latency and tokens). Each backend uses its own lever: Claude sets `MAX_THINKING_TOKENS=0` in the child env (a true off on the Anthropic API; a caller-set value still wins); OpenCode passes `--variant minimal` (the lowest reasoning effort its CLI exposes — it has no full "off"). Both are scoped to the `bare` path only and applied best-effort (a backend/provider that ignores the lever just runs as before); coding sessions keep their default thinking.
- Record reasoning/thinking token counts in commit metadata only when the backend session record reports them; otherwise omit the reasoning line. Do not add explanatory token notes to the metadata.
- Record the conversation's reasoning effort / thinking level as a `reasoning_effort:` metadata line, **only when the transcript reveals it** — neither backend records a numeric budget, so this is a coarse, best-effort signal, never asserted as "off". It lives per-turn on `SessionTurn.reasoning_effort`: Claude sets `"on"` when a turn carries a `thinking` content block (the sole transcript signal that extended thinking was active); OpenCode prefers a named effort/variant from the assistant `info` when the export carries one (`_REASONING_EFFORT_KEYS`) and otherwise falls back to `"on"` when the turn spent reasoning tokens. The commit takes the most recent turn that recorded a level (a later `None` does not erase it), so the metadata reflects the level in effect at the end of the span; the line is omitted entirely when no turn revealed one. This is the *coding* conversation's level — distinct from the summarizer, which is forced to its floor on the `bare` path.
- Track events that reshape the token accounting in both the interaction trace (as a lead-in note) and the metadata: context compactions (Claude's `isCompactSummary` rows, OpenCode's `summary`/`compaction` assistant messages — `context_compactions: N`, attributed to the turn whose context the compaction shrank) and a session being forked or copied from another conversation (`forked_from:` / `copied_from:` with the source and, for a copy, the contributor). A compaction resets the context and a fork/copy inherits another conversation's context and tokens, so these must be recorded to keep the token counts interpretable. The fork/copy origin is a one-shot, surfaced by the new session's first agent commit and then cleared.
- Proxy mode must baseline continued OpenCode sessions on startup so old turns do not inflate token usage for the next commit.

## Staging Behavior

- When starting a coding agent, if the backend CLI supports appending to its system prompt (Claude's `--append-system-prompt`; OpenCode's TUI has no such flag), append a note that the session runs inside aGiTrack, which auto-commits each turn, so the agent must not create git commits itself unless the user explicitly asks. Apply this to coding sessions only (interactive proxy spawn and the shell-mode `run`), NOT to the summarizer's bare run. It is on by default but can be disabled per run with `--no-commit-guidance` (or `commit_guidance: false` in the global config), threaded as `commit_guidance` through the runners → `spawn_command`/`run`.
- Use `git add -u` by default for tracked modifications and deletions.
- When new untracked files are present, ask whether they should be staged.
- If the user declines staging untracked files, remember those files in repository-local state and do not ask about them again automatically.
- Inform the user when intentionally unstaged files exist.
- Provide an interactive CLI command to review and stage intentionally unstaged files.

## Repository-Local State

- Store state in `.agitrack/state.json` in the target repository.
- Ignore `.agitrack/` by default.
- State includes the aGiTrack session ID, selected backend, selected model, backend session ID, per-backend session IDs, declined untracked files, and pending interaction trace.
- Optional repository-local config lives in `.agitrack/config.json`; `trace_turn_limit` defaults to `5` and controls the maximum recent user turns included in an agent commit body.

## MVP Interface

- `agitrack` starts proxy mode in the current repository, launching the native OpenCode TUI through a pseudo-terminal and rendering it through an internal terminal screen with an aGiTrack status line.
- `agitrack --repo PATH` starts the interactive CLI for another repository.
- `agitrack --mode json` uses the structured JSON prompt-loop fallback.
- `agitrack --verbose` shows aGiTrack diagnostic messages; normal mode should avoid debug/status chatter.
- Plain text input is sent to the active agent backend.
- In proxy mode, all printable input is forwarded to the backend; aGiTrack controls are opened with `Ctrl-G` only. `:` is not an aGiTrack command trigger in proxy mode and is forwarded to the backend like any other character.
- Proxy mode command palette previews aGiTrack commands; Up/Down selects, Tab completes, and Enter runs the selected command.
- In JSON mode, aGiTrack commands use `:` instead of `/` so OpenCode-native slash controls are not intercepted.
- The interactive UI should show status information and contextual command hints for both `:` aGiTrack controls and `/` OpenCode-native controls.
- Intentionally unstaged-file notices should live in the status bar, not in the main transcript.
- Proxy mode renders the backend screen itself, so it must reproduce each cell's colors and attributes (bold/italic/underline/reverse) exactly as the backend emitted them.
- Proxy mode must re-emit colors in the same encoding/depth the backend used, chosen from the shared terminal color support (truecolor stays 24-bit; 256-color stays a palette index so the host terminal's own palette renders it; named ANSI stays named). Upconverting 256-color output to truecolor breaks terminals without truecolor support (e.g. Apple Terminal) and shifts colors on terminals with customized palettes.
- Proxy mode must answer the terminal capability queries the backend makes (foreground/background via OSC 10/11, palette via OSC 4, cursor position, device attributes) using the host terminal's real responses, so the backend detects the same theme it would in a native session. Without this the backend cannot match the host terminal's light/dark theme and colors render wrong.
- Proxy mode commands after `Ctrl-G` (bare names, no `:`), in this order: `session`, `agent-backend`, `git-status`, `git-stage`, `git-unstaged`, `git-user-commit`, `exit`. Order matters because the palette selects by typed prefix.
- Only `session` starts with `s`, so pressing `s`+Enter jumps straight to the session picker. Git-specific commands are grouped under a `git-` prefix.
- `session` opens an interactive menu of the live concurrent sessions to switch between them, start a new one (in its own worktree), or stop one. Each entry shows whether the session is `running` (a turn is in flight / it produced output recently) or `idle`. Typed forms: `session new`, `session <n>` (switch to the n-th).
- `agent-backend` selects the agent backend (`opencode` or `claude`); with no argument it shows a picker. Switching relaunches the backend TUI, restores that backend's previous session for the repo if known, and updates the saved global default.
- `git-status` shows Git status; `git-stage` reviews/stages untracked files; `git-unstaged` shows intentionally unstaged files; `git-user-commit` creates a user commit.
- `exit` exits (with confirmation; finalizes pending commits first).

## OpenCode Backend

- Use the OpenCode CLI, initially through `opencode run --format json`.
- Proxy mode uses the native OpenCode TUI and recovers metadata through `opencode session list --format json` and `opencode export`.
- Parse the final response, backend session ID, and model when available.
- Preserve only the final response in commit messages.

## Claude Backend

- Use the Claude Code CLI: proxy mode launches the native `claude` TUI; JSON mode uses `claude -p <prompt> --output-format json`.
- Proxy mode starts a fresh session with an explicit `claude --session-id <uuid>` so aGiTrack knows which transcript to read, and continues an existing session with `claude --resume <id>`.
- Recover metadata by reading the session transcript JSONL under `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` (override the base directory with `CLAUDE_CONFIG_DIR`); the project directory name is the absolute working directory with every non-alphanumeric character replaced by a dash.
- Parse turns into user prompts and final assistant text responses; exclude thinking blocks, tool calls, tool results, sidechain (subagent) messages, and slash-command artifacts.
- Map Claude token usage (input/output/cache-read/cache-creation) onto the shared token model; reasoning/thinking tokens are not reported separately.

## Session Tracking

- aGiTrack tracks exactly one backend session per repository and pins to the session it launched, rather than chasing whichever session is globally newest.
- For backends that accept an explicit session id (Claude), aGiTrack pins at launch. For backends that assign their own id (OpenCode), aGiTrack snapshots existing session ids before launch and adopts the newly created one on the first parse, then stays pinned to it.
- The `session` command (proxy) lets the user start a new session, switch the tracked session to another existing one, or sync tracking to the most recently active session (useful after starting a new session inside the backend TUI). Switching or starting a new session relaunches the backend TUI and re-baselines so existing history is not re-committed.
- Session detection, listing, and switching must work identically for both OpenCode and Claude.

## Concurrency and Locking

- Only one aGiTrack process may auto-commit/merge in a given working tree at a time. A process acquires a single-writer lock at `<tree>/.agitrack/lock` (PID-based, with stale-owner reclaim). A second aGiTrack process on the same repo runs **read-only**: it renders the backend TUI but makes no commits, and shows a banner that another aGiTrack process is managing the repo. (Implemented in `agitrack/git/lock.py`, wired into both proxy and JSON modes.)
- Quitting a managing (non-read-only) proxy instance asks for confirmation before exiting.
- Concurrent sessions are isolated with git worktrees so changes are never attributed to the wrong session; see Concurrent Sessions below.

## Concurrent Sessions (worktrees + auto-integration)

This is the design aGiTrack targets for running several sessions at once. Foundations (`agitrack/git/lock.py`, `agitrack/git/worktree.py`, and the worktree/branch/merge helpers in `agitrack/git/repo.py`) are implemented and unit-tested; the multiplexer wiring in `agitrack/proxy/runner.py` is the remaining integration.

- A single aGiTrack process multiplexes several live sessions; one is displayed, the others keep running and integrating in the background.
- The main working tree / base branch is mutated only by the serialized merge coordinator; every session runs in its own worktree under `.agitrack/worktrees/<name>`.
- A session creates a transient turn branch (`agitrack/<name>/t<n>`) when it receives a prompt; on the turn's final agent message the branch is integrated into the base and deleted.
- Integration is serialized and completion-ordered. It runs inside the owning session's worktree (merge base into the turn branch) so the agent can resolve conflicts in place; aGiTrack auto-prompts the agent with the conflicting commits' context and pauses for the user if it cannot resolve.
- A `session` view shows running/idle/merging status and can stop sessions; a `base` command switches the base branch after stopping sessions and draining pending integrations; on restart aGiTrack offers recovery for stale `agitrack/*` branches and worktrees.

## Backend Selection and Global Config

- The selected backend is stored per repository in `.agitrack/state.json`.
- A user-wide config at `~/.agitrack/config.json` (override the directory with `AGITRACK_CONFIG_DIR`) stores `default_backend`, used when a repository has no backend recorded yet.
- On the first run (no global default yet and no `--backend`), prompt the user to choose the default backend. List backends in alphabetical order and show whether each is installed.
- Check that the backend's CLI is installed (on `PATH`) before launching it. If it is not, show install instructions and let the user install it or choose a different (installed) backend; backend switching is likewise blocked for backends that are not installed.
- `agitrack --backend <claude|opencode>` selects the backend for a run and saves it as the new global default.
- Switching backends saves the current backend's session id and restores the target backend's last session id for the repository, so each backend keeps its own conversation.

## Self-Update

- aGiTrack can update itself in place. It detects how it was installed: **source-linked** (importable from a git checkout of its own source — the editable `pip install -e .`) or **package** (installed as a wheel). Implemented in `agitrack/update/`.
- Source-linked: compare the current branch against its upstream remote branch (after a `git fetch`); an update is available when the local branch is behind. Package: compare the installed version against the latest published one (`pip index versions`, run through the resolved pip invocation; reading the index is safe even on an externally-managed Python).
- Check for updates at startup and periodically while running (every `timings.update_check_seconds`, default 300s). The periodic check runs on a worker thread so the terminal never blocks on network I/O.
- When an update is available, prompt the user — at startup (before launching) and during a session (a status-bar notice plus the `update` command in the `Ctrl-G` menu).
- Never surface an update prompt while a merge / conflict resolution is in progress in any session (active or a background session integrating its turn); the notice is held until the merge is done, and an accepted update is likewise deferred.
- If the user accepts during a session, apply the update only once **every session has finished and all commits are integrated** (no agent in flight, no pending parse/merge/summary, no running background session), then re-exec aGiTrack (`python -m agit`) so the new code loads.
- Source updates are fast-forward only and abort (with a message) when the checkout is dirty or has diverged from upstream, so an automatic update never clobbers local development.
- Package updates are deliberately **package-manager-independent** wherever possible: the primary path upgrades with the *running interpreter's own* pip (`<python> -m pip install --upgrade`), which upgrades a plain `pip` install, a venv, a `--user` install, and a pipx venv identically — no need to detect or shell out to pipx/brew/apt. It falls back to a `pip3`/`pip` on `PATH` only when that interpreter has no `pip` module. The single case pip can't handle is an externally-managed (PEP 668) Python — Homebrew's or a distro's — where pip refuses by design; there aGiTrack defers to the owning system manager (`brew upgrade`, the only system manager that ships aGiTrack — distro managers don't carry it), and when no automatic path works it reports a **full enumeration** of every manual route (pip, pipx, Homebrew, and `--break-system-packages`). Never auto-add `--break-system-packages` (it can corrupt a system/Homebrew Python). Install-method detection keys off where the running code physically lives (resolved package path, `sys.prefix` backstop), checking the pipx-venv marker before the Homebrew prefix.
- Update checks are on by default; the user can turn them off via the "stop checking" choice or `check_for_updates: false` in `~/.agitrack/config.json`. Scripted/non-TTY runs never prompt.
- A failed update must never take aGiTrack down or block work. `Updater.apply()` never raises — any failure (subprocess timeout, OS error, pip/git crash, PEP 668 refusal) becomes an `error` status carrying manual-update instructions (`Updater.manual_update_instructions()`), and aGiTrack keeps running the current version. On a failure aGiTrack records the target version in `pending_manual_update` (global config): the next startup shows a **single** manual-update reminder (and clears it once aGiTrack is actually at/above that version), while the regular in-session update notice is suppressed for that version so the user isn't nagged repeatedly. The explicit Ctrl-G `update` command can still retry.

## Testing

- Make it regular practice to **create temporary git repositories** to test aGiTrack functionality end-to-end against real git, rather than relying only on mocked inputs. Where a feature depends on backend behavior (transcripts, sessions, sub-agents, token usage), also run the actual backend CLIs (`claude`, `opencode`) in a temp repo to produce real data and verify against it. This is the established practice for branch/worktree concurrency, sub-agent token accounting, and token-count precision.
- Compute expected results independently from the real data and assert **exact** equality — especially for token counts, which must be precise (not more, not less).
- Capture the verified scenarios as permanent tests under `tests/` so the behavior stays locked in, and clean up any temporary repositories afterwards.
- `./scripts/check.sh` (ruff check, ruff format, mypy-vs-baseline, pytest + coverage) must pass before a change is considered done.
