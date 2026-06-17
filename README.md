# aGiTrack

aGiTrack stands for agent + git tracking. It is an interactive Python CLI that wraps coding-agent command line tools and Git so agentic code changes are committed with traceable metadata.

aGiTrack supports OpenCode and Claude (Claude Code) as interchangeable backends. Every aGiTrack feature works the same regardless of the selected backend.


## Requirements

aGiTrack runs on **macOS and Linux**. It is a POSIX terminal application (it drives the terminal through `pty`/`termios`), so it does not run on native Windows; on Windows, use it inside **WSL** (Windows Subsystem for Linux), which is a Linux environment. aGiTrack works across common terminal emulators (e.g. iTerm2, Apple Terminal, Alacritty, kitty, GNOME Terminal, Konsole, tmux, the VS Code integrated terminal, and Windows Terminal driving WSL), degrading gracefully on terminals that lack features such as the alternate screen or the kitty keyboard protocol.

aGiTrack needs **git** and at least one backend CLI — [Claude Code](https://docs.claude.com/en/docs/claude-code) or [OpenCode](https://opencode.ai) — on your `PATH`. The dashboard's committer view additionally uses the **GitHub CLI (`gh`)** to resolve commit authors to their GitHub logins: install it from [cli.github.com](https://cli.github.com) and run `gh auth login`. `gh` is optional — without it the dashboard still works and falls back to merging committer identities by email and no-reply login.

## Install

```bash
pip install agitrack
```

This installs the `agitrack` command and the terminal UI dependency used for status bars and contextual command hints. The PyPI distribution, the importable package, and the command are all named `agitrack` (the legacy `agit` command name still works as an alias). Once installed, aGiTrack keeps itself up to date — see [Self-update](#self-update).

For local development, install from a checkout instead:

```bash
python3 -m pip install -e .
```


## Usage

Run in the current repository:

```bash
agitrack
```

By default, `agitrack` runs in proxy mode: it launches the real backend TUI (OpenCode or Claude) in a pseudo-terminal, renders it through an internal terminal screen, and reserves a bottom status line for aGiTrack showing the session (and the branch it merges into — shown **bold** when that branch differs from the one checked out in the repo directory), the backend, the summarizer state, and the repository the agent is working on (the base repository path, home-abbreviated and elided from the left when space is tight). Press `Ctrl-G` to enter aGiTrack command mode (configurable via `menu_key` in `~/.agitrack/config.json` — see Configuration).

Run against another repository:

```bash
agitrack --repo /path/to/repo
```

Choose the backend (also saved as the global default for future runs):

```bash
agitrack --backend claude
agitrack --backend opencode
```

By default aGiTrack resumes the previous conversation for the repository. Start a fresh one instead with:

```bash
agitrack --new-session
```

Run without a worktree (the agent edits the current branch directly, so changes are visible live as it works):

```bash
agitrack --no-worktree
```

This is for single-session use: there's no isolation or auto-integration, and concurrent sessions are unsafe in this mode (starting a new session is blocked). Set `"use_worktrees": false` in `~/.agitrack/config.json` to make it the default; `--no-worktree` always wins.



On the first run, aGiTrack asks which backend should be the default (listed alphabetically, with each backend's install status). If the chosen backend's CLI is not installed, aGiTrack shows install instructions and lets you install it or pick a different one. The choice is saved in `~/.agitrack/config.json` (`default_backend`) and reused for future runs. You can also switch backends mid-session with the `agent-backend` command below.

In proxy mode (default), press `Ctrl-G`, then type one of these aGiTrack commands:

```text
session                   switch / start (own worktree) / stop a live session
agent-backend             switch backend (opencode|claude); shows a picker
summarizer                toggle summarization on/off, set model, show status
git-unstaged              show intentionally unstaged files
git-user-commit           create a user commit
update                    check for / install an aGiTrack self-update
exit                      exit (with confirmation)
```

aGiTrack tracks one session per repository and stays pinned to the session it launched (so it does not drift to other sessions you open). Use the `session` command (`Ctrl-G`, then `session`) to start a new session, switch the tracked session to another existing one, or sync tracking to the most recently active session — for example after starting a new conversation inside the backend's own TUI. This works the same for all backends.

Only `session` starts with `s`, so `Ctrl-G` then `s` + Enter jumps straight to the session picker. The session menu marks each session `running` or `idle`. Git-specific commands share a `git-` prefix.



## Dashboard

`agitrack --dashboard` (or `-d`) opens a **live, auto-refreshing web dashboard** of your repository — who and what wrote the code — served on `localhost` and opened in your browser. Every number is computed from commit metadata alone, so it's identical on every clone; nothing is sent anywhere.

```bash
agitrack --dashboard        # serve on localhost and open the browser (Ctrl-C to stop)
agitrack -d text            # one-shot plain-text report instead (pipe it, paste it into an issue)
```

![The aGiTrack dashboard](https://raw.githubusercontent.com/core-aix/agitrack/main/docs/images/dashboard-v4.png)

- **aGiTrack-tracked AI vs non-tracked lines** — what the agents wrote (tracked by aGiTrack) versus everything else; it never claims a human wrote what the model did.
- **Filter live** — narrow the whole dashboard to one committer (merged to their GitHub ID), a backend, a model, or a time range.
- **Tokens, efficiency, and loop detection**, plus a commit log you can click to read the full message and jump to the commit on GitHub.

See [Repository dashboard](#repository-dashboard) below for the full breakdown.


## Sharing sessions

You can share a full agent conversation with collaborators through the repo's git remote, and resume each other's sessions. It's **opt-in** — nothing is ever uploaded until you explicitly share a session. Both backends are supported: Claude shares its per-session transcript, and OpenCode shares its session via the built-in `opencode export`/`import`.

From the `session` menu (`Ctrl-G` → `session`) — where each session is also marked **⇪ shared** or **⇪ auto-share** if you've shared it:

- **Share this session** — publishes the current conversation to the remote. The first time, aGiTrack asks you to confirm; a transcript can contain file contents, command output, and secrets, so **review what's in the session before sharing** (aGiTrack also applies best-effort secret masking, but that is *not* a guarantee — don't rely on it). You can choose to keep the shared copy **updated automatically**, or re-share manually.
- **Resume a shared session** — lists teammates' sessions as `<github-id>/<name>` and continues one in a fresh session worktree.
- **Manage shared sessions** — see what *you've* shared (with "up to date" vs "local has newer turns"), push the latest turns, toggle auto-update, or unshare.

Shared sessions also appear in the [dashboard](#dashboard) under **shared sessions**. The storage model and the guarantees behind these actions are described under [Session tracking](#session-tracking).


## How It Works

### Backends

aGiTrack drives an external coding agent rather than calling a model directly. OpenCode and Claude (Claude Code) are interchangeable backends, and every aGiTrack feature behaves the same regardless of which is selected. The per-repository backend is recorded in state; the user-wide default lives in `~/.agitrack/config.json`.

aGiTrack recovers what to commit from the backend's own session record: `opencode export` for OpenCode, and the session transcript under `~/.claude/projects/` for Claude. In JSON mode it instead invokes the backend non-interactively per prompt and captures the final response.

### Session tracking

aGiTrack tracks exactly one backend session per repository and stays pinned to the session it launched, so it does not drift to other conversations you open in the backend. On startup it baselines the tracked session, so token metadata and the interaction trace only cover turns that happen after aGiTrack starts watching — resuming an old conversation does not re-commit its history.

Use the `session` command to start a new session, switch the tracked session to another existing one, or sync tracking to the most recently active session (for example after starting a fresh conversation inside the backend's own TUI). The session menu marks each session `running` or `idle`. A new session is given a friendly random word as its default name (e.g. `maple`, `harbor`) — easier to remember than `session-1` and, since sessions are shared as `<github-id(s)>/<name>`, far less likely to clash with a collaborator's; you can accept it or type your own at the prompt, and rename later.

When you start a new session you can make it either a **blank session** (a fresh conversation) or a **fork of the current session** — a copy of the current conversation that runs independently. A fork is installed under a brand-new backend id (so it never clashes with the original) and defaults to the current session's merge branch, so you can branch a conversation to try an alternative direction while the original keeps going.

**Sharing a session across machines and collaborators** (the [Sharing sessions](#sharing-sessions) actions above) builds on the same per-session transcript, with this storage model and these guarantees. **Only sessions you explicitly share are tracked here** — a session you haven't shared stays purely local: it is never uploaded, listed, or tracked in the shared store, and nothing about it leaves your machine.

- **Only the latest copy is kept — git history never grows.** Shared sessions live on a dedicated custom ref `refs/agitrack/shared-sessions`, stored as a *single, parent-less (orphan) commit* built with `git commit-tree` and no parent. Every update (manual or auto) **rewrites** that ref to a brand-new orphan commit holding only the current snapshot. So no matter how many times a session is updated, the ref is always one commit deep — it never accumulates history or bloats the repo, and **unsharing removes the session completely** (there is no older commit anywhere that still holds it). This deliberately avoids a normal commit chain, whose whole point is to *retain* every past version — exactly what we don't want for a privacy-sensitive transcript.
- **Updates still transmit only the diff (git deltifies at the pack level, not via history).** Skipping commit history does *not* mean re-uploading the whole transcript each time. Git delta-compresses by **content similarity within a packfile**, and a push builds a *thin pack* that deltas new objects against objects the remote **already has** — independent of whether the commits share any ancestry. So an orphan-commit rewrite still pushes only the turns that changed. The one requirement is that the *previous* version still exist locally as a delta base at push time, so aGiTrack **defers reclaiming it until after the push** (rather than deleting it first, which would have forced a full re-upload every share — costly for an append-only transcript that is re-shared or auto-shared as it grows). Right after the push succeeds, the now-unreferenced previous objects are reclaimed, keeping local storage bounded to just the latest snapshot. Concurrent updates are made safe with `git push --force-with-lease` (each contributor edits only their own subtree), retrying after a sync if the lease is stale.
- **One session is one entry, named by its contributor set.** A shared session is displayed as `<id1>+<id2>/<name>` — the github ids of everyone who has shared it, sorted (so order never matters), before its name. It is stored once, keyed by its lineage origin (the first sharer's id + name), not by whoever last pushed. So when you resume a teammate's `alice/fix-parser` and share your continuation, it becomes `alice+you/fix-parser` — the **same** entry, now co-owned — rather than a second `you/fix-parser`. Moving a session back and forth between your own machines likewise keeps updating that one entry instead of spawning new names.
- **Resuming continues the same conversation locally.** Picking a shared session downloads its transcript into your local backend store (Claude's project dir, or via `opencode import`) and continues it in a fresh worktree — using whichever backend recorded it, regardless of which one you're currently on. Your turns are appended to your local copy; sharing again updates that one entry (above), guarded by newest-wins below. Choosing **Keep both** instead deliberately *forks* a separate copy under a new id — a new, independent lineage published as `<you>/<name>` of its own.
- **Auto-update rides your commits.** When a session is set to auto-update, aGiTrack re-pushes the latest turns **at commit time** (in the background, only when the content changed) rather than on a busy timer — so it won't hammer the remote. The opt-in is remembered across aGiTrack runs.
- **Newest wins — a behind machine can't rewind the shared copy.** Transcripts only grow (turns are appended), so aGiTrack compares conversation length on both ends. Sharing from a machine whose copy is *behind* the shared one is refused (it would erase newer turns), and resuming never replaces a local copy with an *older* shared one — it tells you the local one is newer and keeps it by default. This keeps the same session consistent as you move between computers, instead of a stale machine (or its auto-update) dragging everyone back to an earlier state.
- **No manual git needed.** The Resume menu and the dashboard sync the shared ref for you. (A plain `git clone`/`git fetch` does *not* pull custom refs, so teammates rely on those menus; to inspect by hand: `git ls-remote origin 'refs/agitrack/*'`.) Because it's a custom ref, not a branch or tag, **it won't appear on GitHub's web UI**.
- **Scoped and bounded.** Sessions are namespaced by the repo's root commit, so only *this* repo's sessions are ever uploaded or listed; each contributor keeps their most-recent few, with older ones auto-pruned.

### Worktrees and branches

To let sessions run without stepping on each other or on your working tree, each aGiTrack session runs in its own git worktree under `.agitrack/worktrees/<name>`, created *detached* at its merge branch — a session has no branch of its own. Work within a session is committed on per-turn branches named `agitrack/<backend>/<name>/t<turn>`, created lazily on the first commit of each turn; once a turn is integrated its branch is deleted and the worktree is detached at the merge branch again. All aGiTrack-managed branches live under the `agitrack/` prefix so they are easy to recognize for cleanup and never collide with your own branches.

A session's branch is only ever advanced by **integration**: aGiTrack merges a session's pending commits back into its merge branch rather than committing onto it directly. A single-writer lock ensures only one aGiTrack process auto-commits or integrates at a time, so concurrent sessions stay consistent.

#### Per-session merge branches

Each session has its **own** merge destination, independent of the other sessions and of the branch checked out in your repo directory — so you can run concurrent sessions that land on different branches (e.g. one on `main`, one on `feature-x`) without ever switching your working directory.

- **Choosing a branch for a new session.** When you start a new session (`Ctrl-G → + New session`), aGiTrack asks which branch its changes should merge into. The default is the branch checked out in your repo directory ("the base branch"); pick another to point that session elsewhere. aGiTrack does **not** check that branch out in your directory — it advances the branch's ref directly via fast-forward, so your working tree stays put.
- **Status bar emphasis.** The status line shows `session → branch`. When a session merges into a branch *different* from the one checked out in your repo directory, that branch name is shown in **bold**, so it's obvious at a glance that the changes are landing somewhere other than your current directory. The emphasis updates automatically when you check out a different branch in the directory.
- **Staying in sync.** Sessions keep merging into their own branches by default, even after you check out a different branch in the repo directory. aGiTrack only *asks* — and only when the active session's merge branch differs from the directory's:
  - When you **`git checkout` a different branch in the repo directory**, it offers three choices: do nothing (the default — every session keeps its own branch), switch only the current session to the directory's branch, or switch **all idle** sessions to it. Only idle sessions are re-pointed — any session running a turn keeps the branch it started on, and is reported so you can re-point it once it's idle.
  - When you **switch sessions** and the one you land on diverges, it offers two: keep that session's branch (default) or switch it to the directory's branch.

  Switching a session re-points it (flushing its pending work into the old branch first). The session list (`Ctrl-G → session`) shows each session's merge branch as `name → branch`.
- **Startup keeps each session's previous merge branch.** aGiTrack persists every session's merge branch across restarts. On startup (and when resuming a dormant session) it would otherwise assume the directory's current branch — so if a session was assigned a *different* branch in the previous run, aGiTrack keeps that prior assignment and asks you to **confirm the change before it takes effect**, telling you how to re-point any session (`Ctrl-G → session → ⤳ Change a session's merge branch`).
- **Changing a session's merge branch.** `Ctrl-G → session → ⤳ Change a session's merge branch` re-points **any** session's merge destination. The session keeps running and only that session is affected (its pending work is flushed into its old branch first).
- **A running session's branch can't change mid-run.** A session's merge branch can only be changed while it is **idle** — never during an in-flight turn, whose work would otherwise split across branches. If you `git checkout` a different branch in the directory while a session is mid-turn, aGiTrack warns that *this run* still merges into its current branch, and re-asks **after that run's changes have merged into the original branch** (so the just-finished work lands where it was always headed before you're asked where future work should go). A cancelled run leaves nothing to merge, so the dialog is free to appear.
- **Sessions on different branches are never merged across each other.** Each worktree records its own merge branch and aGiTrack only ever merges *that* branch into it; if anything would merge a different branch into a worktree, it is refused with a warning. (This guards against the class of bug where one session's work leaks onto another session's branch.)

### Integration and startup recovery

When a session's commits are merged into its merge branch and the merge has conflicts, the agent backend resolves them, and the resolution is recorded as an `<aGiTrack-merge>` commit listing the base commits it was resolved against.

On startup, aGiTrack reconciles worktrees left behind by previous runs: it integrates any pending commits into the merge branch and then deletes the worktree. Worktrees that cannot be integrated cleanly (a conflict, or uncommitted changes) are kept so no work is lost. The backend conversation itself persists (keyed by the worktree path) and stays resumable.

### Commit message format

aGiTrack commit messages use a consistent Markdown-style structure. The first line is the subject (prefixed with `<aGiTrack>` for agent commits — including the cover commits placed on top of backend-made commits — `<aGiTrack-merge>` for agent-resolved merges, or left plain for user commits). The subject is the **first sentence** of the prompt/summary (up to the first sentence-ending period); the rest flows onto the next line. aGiTrack does not truncate it with an ellipsis — a long subject is left intact and Git shortens its display where needed. When summarization is enabled the summary leads the message: its first line is the subject and the rest is the first paragraph of the body. The rest of the body is organized into `#` sections — `# Prompts` (when a summary takes the subject), `# Interaction Trace`, `# aGiTrack Metadata` — with `## User` / `## Agent` subsections inside the interaction trace. Commits are written with `git commit -F -` (no editor), so the `#` lines are preserved rather than stripped as git comments. Secrets and terminal escape sequences are masked out of subjects and trace bodies before committing.

Because the conversation is recorded in commit messages, aGiTrack shows a privacy warning at startup — never enter passwords, API keys, or other sensitive information in the chat — which must be acknowledged to continue (skipped when there is no terminal to acknowledge from).

### Summarization

When summarization is enabled (the default), aGiTrack runs a second LLM stream alongside the coding session to preserve design context that would otherwise be lost to session compaction or terse commit subjects:

- **Commit summaries** — each agent commit gets an LLM-written summary of what changed and why. The summary leads the commit message: its first line becomes the subject and the rest follows as the first paragraph of the body (the prompts that used to head the message move to `# Prompts`); it is also stored as a git note in the `agitrack/commit-summary` namespace.
- **Session summaries** — a rolling narrative of the session (goals, architectural decisions, design evolution) is updated on every commit and attached as a git note in the `agitrack/session-summary` namespace.
- **Pre-compaction capture** — when you run `/compact` in the backend, aGiTrack first exports the full session transcript and folds it into the session summary, so compaction does not lose the conversation's context.

Because summaries are git notes, they travel with the repository and can be read independently of commit messages:

```bash
git notes --ref agitrack/commit-summary show <commit>
git notes --ref agitrack/session-summary show <commit>
```

Summarization never blocks the session: commits are created immediately with a prompt-based subject, the summary is computed on a background worker (the status line shows "aGiTrack is summarizing commit ..."), and the commit message is then amended in place. The amend only happens while it is safe — the commit is still the latest, unintegrated, and nothing new is staged; integration waits for the summary up to `summary_wait_seconds` and then proceeds, in which case the summary is recorded in git notes only. The metadata records the summarization cost next to the session's own usage (`summary_model`, `summary_tokens_input`, `summary_tokens_output`).

The status bar shows whether summarization is active (`sum:on` / `sum:off`). Use the `summarizer` command (`Ctrl-G`, then `summarizer`, or `:summarizer` in JSON mode) to toggle it (`summarizer on|off`), set the summarization model (`summarizer model`), or show the current status; changes persist to the repository-local `.agitrack/config.json` (see Configuration).


### Commit Behavior

- Tracked modifications and deletions are staged with `git add -u`.
- New untracked files require confirmation before staging.
- Declined untracked files are remembered in repository-local `.agitrack/state.json`.
- Agent commits use the `<aGiTrack>` tag and include the full interaction trace since the last code-changing commit.
- Agent commit metadata includes context token count and generated token usage accumulated since the last code-changing commit.
  - Token figures are read directly from the backend's session transcript (each assistant message's reported usage) and broken out by category: `input`, `output`, `cache_read`, `cache_write`, and (when the backend reports it) `reasoning`. For Claude, the recorded output count already includes extended-thinking and tool-call tokens. Sub-agent/sidechain turns are counted separately under the matching `subagent_*` categories rather than dropped. Each category is recorded only when the backend reports a non-zero value, so backends that omit a field (e.g. OpenCode does not expose sub-agent usage) simply have no line for it.
  - The categories are **non-overlapping**: `output` counts only the main agent's generated tokens, and `subagent_output` counts only sub-agent generated tokens — neither includes the other, so nothing is double-counted. For a grand total of generated tokens, sum the matching pairs yourself (e.g. `output + subagent_output`, and `reasoning + subagent_reasoning` for OpenCode). The input side counts every token exactly once: `input` is all *fresh* input processed since the last commit — the uncached remainder plus the cache-creation tokens (so a first run's input reflects the full context instead of looking near zero next to the cache) — with `cache_write` kept as the "of which was written to the cache" breakdown. `cache_read` stays separate because those tokens were already counted as input when first processed; they are replayed from the cache and billed at a different rate.
  - The figures should still be treated as a lower bound: any consumption the backend does not record in the transcript (e.g. internal compaction, retried requests, or usage a provider omits) is not captured, so actual tokens consumed may be higher than reported.
- Proxy mode baselines the continued backend session on startup so token metadata only includes turns after aGiTrack starts tracking new changes.
- Proxy mode preserves the backend's selected model in commit metadata when it can be read from session data.
- User commits use the user-provided subject and include aGiTrack metadata.
- Commits are created only when staged changes exist.
- If the backend commits on its own (e.g. the agent runs `git commit` itself, or a hook does), aGiTrack never rewrites those commits — their hashes stay exactly what the agent may already have reported in PR or issue comments. Instead, once the turn finishes, aGiTrack adds a *cover commit* on top carrying the interaction trace and metadata: a merge-shaped commit in the GitHub PR merge style, whose tree is the backend head's tree and whose parents are the turn's start and the backend's head, so `git log --first-parent` reads turn-by-turn while the backend's own commits remain reachable via the second parent. The `covered_commits` metadata line records the hashes of the backend-made commits the cover accounts for; when aGiTrack also has uncommitted changes to commit, its own (regular) commit carries that line instead.

### Repository dashboard

`agitrack --dashboard` (or `-d`) opens a live web dashboard of repository metrics computed entirely from the aGiTrack metadata in commit messages — no extra state, so the numbers are identical on every clone. It is served on `localhost`, opens in your browser, and **auto-refreshes** (the page polls the server, which recomputes from `git log` on each request), so you can watch metrics update as an agent commits. Press Ctrl-C to stop.

- **Coverage**: how many commits are aGiTrack-tracked (agent commits, backend-made commits covered by an aGiTrack cover commit, agent-resolved merges, user commits, and aGiTrack's own integration merges) versus non-tracked.
- **Code changes**: lines added/removed split two ways — **aGiTrack-tracked AI** (agent commits + the backend-made commits an aGiTrack cover commit accounts for + agent-resolved merges) and **non-tracked** (everything else: user commits, plain commits with no aGiTrack metadata, and squash/PR-merge commits whose message concatenates several metadata blocks and so can't be cleanly attributed). There is deliberately no "human" category — even a user-made commit can contain lines an agent produced off the record, so the only honest claim is whether aGiTrack tracked the lines as AI. Cover commits are merges and contribute no line counts of their own, so a turn's lines are never double-counted.
- **aGiTrack-ops**: the integration merge commits aGiTrack makes itself (e.g. bringing base into a session branch) are shown as their own class, not lumped into non-tracked. They carry no diff, so they add no lines.
- **Squashed commits**: when several aGiTrack commits are squashed into one (a squash- or PR-merge concatenates their metadata blocks — git flattens this even across multiple rounds of squashing), the dashboard parses every original back out, so their tokens and per-model/backend usage are still counted instead of lost in the aggregate. In the web commit log such a commit is tagged `⧉ N squashed` and expands on click into its original commits, each itself expandable to its full message.
- **Tokens**: totals per category (input, output, reasoning, cache read/write, sub-agents, summarizer) and an efficiency ratio — AI-changed lines per 1k output tokens.
- **Breakdowns** by backend, by model (a cover commit's bucket includes the lines of the backend-made commits it covers), and by committer. Committer identities are merged to **GitHub IDs** via the `gh` CLI when available (every commit GitHub knows is keyed by its real login); without `gh` it falls back to merging by email and no-reply login. Each committer's lines are split into the aGiTrack-tracked AI they drove versus non-tracked.
- **Possible loops**: runs of three or more consecutive turns with near-identical prompts (or the same prompt repeated within one turn's trace), with the output tokens they consumed — a sign the conversation is going in circles.

The web page (styled like the [project page](https://github.com/core-aix/agitrack/tree/main/docs)) lets you **filter live** — narrow the whole dashboard to one committer or view the entire team, slice by backend or model, or restrict to a **time range** (presets or a custom from/to). The server recomputes the metrics for each filter, and the **commit log is paginated** (fetched a page at a time), so the browser never holds the whole history — memory stays bounded no matter how big the repo is. Each log line shows per-line token metrics; clicking a line opens the full commit message **rendered as Markdown** with a link to the commit on GitHub, and a squash expands into its original commits (each itself expandable). Agent commits also record when the AI-driven conversation started and ended (`agent_started_at` / `agent_ended_at` in the metadata block).

`agitrack --dashboard text` (or `-d text`) prints the same metrics as a one-shot plain-text report instead of serving — handy for piping or pasting into an issue.

The dashboard is read-only in either form: it never commits, never prompts, and skips the privacy acknowledgment.

### Self-update

aGiTrack keeps itself current. On startup, and then about every five minutes while you work, it checks whether a newer aGiTrack is available:

- **Source-linked install** (the editable `pip install -e .` from a git checkout): it compares three commit hashes — the one the **running** process loaded, the checkout's **local** `HEAD`, and the **remote** target's tip — and offers an update whenever either the local disk or the remote carries newer code. The remote target is the current branch's upstream, or, when the branch tracks nothing (aGiTrack runs on session worktree branches), `origin`'s default branch. When you pick `update` from the menu it runs a **fresh** check on the spot, so a teammate's push or a just-pulled local update is reflected immediately rather than waiting for the next periodic check.
- **Package install** (a wheel from a package index): it compares the installed version with the latest published one.

If an update exists, aGiTrack prompts you at startup and shows a notice during a session (run the `update` command from the `Ctrl-G` menu to act on it). When you accept, aGiTrack waits until **every session has finished and all commits are integrated**, installs the update, then restarts itself automatically. It never interrupts a merge: while any session is resolving a merge/conflict, the notice is held back and an accepted update is deferred until the merge is done. A source update **merges** the upstream branch into the checkout — a clean checkout fast-forwards and a diverged one (your own commits, or aGiTrack's session integrations) gets a normal merge — so it pulls in new code without discarding local work; if the checkout has uncommitted changes it's skipped with a message, and if the merge hits a genuine conflict aGiTrack aborts it (leaving the source clean) and tells you automatic update isn't possible until you resolve it. When only the running process is behind the on-disk code, no download happens — aGiTrack just restarts to load it. Choose "Stop checking for updates" — or set `"check_for_updates": false` in `~/.agitrack/config.json` — to turn the checks off; tune the cadence with `timings.update_check_seconds`.


Proxy mode launches the backend's native TUI directly and recovers session metadata for automatic agent commits — via `opencode export` for OpenCode, or by reading the session transcript under `~/.claude/projects/` for Claude.

## Advanced Usage

Show aGiTrack diagnostic messages:

```bash
agitrack --verbose
```

Use the structured JSON fallback mode:

```bash
agitrack --mode json
```

JSON mode invokes the backend non-interactively for each prompt (`opencode run --format json` or `claude -p --output-format json`) so aGiTrack can capture the final response and create traceable commits.

In JSON mode, plain text is sent to the active agent backend:

```text
> fix the parser bug
```

JSON mode aGiTrack commands use `:` so backend-native `/` input is not intercepted:

```text
:help                      show commands
:status                    show git status
:user-commit               create a user commit
:stage                     review and stage untracked files
:unstaged                  show intentionally unstaged files
:agent-backend <backend>   switch backend (opencode|claude)
:exit                      exit
```

In JSON mode, aGiTrack shows a bottom status bar with the active backend, target repo, model, and unstaged-new-file count. Typing `:` shows aGiTrack command completions. Typing `/` shows common backend command completions, and slash commands are forwarded to the backend rather than handled by aGiTrack.

### Scripted runs and the demo

`--prompt` runs JSON mode fully scripted: each prompt is sent to the backend in order (lines starting with `:` are aGiTrack commands), every turn that changes files becomes a commit, and aGiTrack exits when the prompts are done.

```bash
agitrack --repo path/to/repo --backend claude \
  --prompt "add input validation to parse()" \
  --prompt ":status" \
  --permission-mode acceptEdits
```

Scripted runs never block on a question: the privacy warning is printed without waiting for acknowledgment, and new untracked files are staged automatically (with a notice) instead of being reviewed interactively. The same non-interactive defaults apply when prompts are piped to `agitrack --mode json` on stdin. Note that headless Claude needs permission to edit files — forward `--permission-mode acceptEdits` (or your preferred permission flags) through aGiTrack as shown above; OpenCode's `run` mode edits by default.

`scripts/demo.sh` is a self-contained showcase built on this: it creates a fresh repository in a temporary directory, has the agent write a small program and its tests through aGiTrack, and leaves the repository behind so you can inspect the `<aGiTrack>` commit history or continue interactively.

```bash
scripts/demo.sh                      # drive the demo with claude
scripts/demo.sh --backend opencode   # ... or with opencode
scripts/demo.sh --model haiku --dir /tmp/agitrack-demo
```

### Forwarding arguments to the backend

aGiTrack does not reduce the backend's own functionality: any argument it doesn't recognize is forwarded verbatim to the backend CLI (`claude` / `opencode`).

```bash
agitrack --backend opencode --port 12345      # --port 12345 goes to opencode
```

Use `--` to forward an argument that aGiTrack also defines (e.g. `--verbose`), or to pass a bare prompt:

```bash
agitrack -- --verbose "fix the bug"           # everything after -- goes to the backend
```

aGiTrack's own flags (`--repo`, `--verbose`, `--mode`, `--backend`, `--new-session`, `--no-worktree`) bind to aGiTrack when they appear before `--`. Note that aGiTrack manages session selection itself, so forwarding session flags (`--resume`, `--session-id`, `--session`, `--continue`) may interfere with its session tracking — it warns when you do.

Help follows the same model: `agitrack --help` (or `-h`) prints aGiTrack's own options followed by the active backend's help, so one command documents both layers. To run only the backend's help, forward it explicitly: `agitrack -- --help`.



## Configuration

Repository-local configuration can be stored in `.agitrack/config.json`:

```json
{
  "trace_turn_limit": 5,
  "summarization_enabled": true,
  "summarization_model": null
}
```

`trace_turn_limit` controls the maximum number of recent user turns included in an agent commit body. The default is `5`.

`summarization_enabled` (default `true`) toggles the LLM summarization stream (see Summarization above). `summarization_model` sets the model the summarizer asks the backend to use; leave it unset (`null`) to use the backend's default model. Both keys can also be set user-wide in `~/.agitrack/config.json`; the repository-local value wins, and the `summarizer` command writes its changes here.

User-wide settings live in `~/.agitrack/config.json` (override the directory with `AGITRACK_CONFIG_DIR`):

```json
{
  "default_backend": "opencode",
  "menu_key": "ctrl-g",
  "sandbox": true,
  "use_worktrees": true,
  "timings": {
    "base_poll_seconds": 3.0
  }
}
```

`default_backend` (`opencode` or `claude`) is used for repositories that have no backend recorded yet. It is updated whenever you pass `--backend` or switch backends with `agent-backend`.

`sandbox` (default `true`) confines the agent's writes to its own session worktree (via `sandbox-exec` on macOS), keeping the base repository and sibling worktrees read-only to the agent. Set it to `false` to disable confinement; when sandboxing is unavailable, aGiTrack instead warns when the base repository is edited while a session runs.

`use_worktrees` (default `true`) controls whether sessions run in isolated worktrees. Set it to `false` to run the agent directly on the current branch by default — the same behavior as `--no-worktree` (which always wins over the config). See the `--no-worktree` notes under Usage for the trade-offs.

`menu_key` sets the key that opens aGiTrack's command menu in proxy mode. The default is `ctrl-g`; any `ctrl-<letter>` works except keys the terminal or aGiTrack already uses (`ctrl-c` exit flow, `ctrl-h` Backspace, `ctrl-i` Tab, `ctrl-j`/`ctrl-m` Enter). An invalid value falls back to `ctrl-g`, so a typo can never lock you out of the menu. The status line and aGiTrack's messages show whichever key is configured.

`timings` tunes aGiTrack's polling and debounce intervals (all in seconds). Specify only the keys you want to change; anything omitted — or set to a non-positive / non-numeric value — keeps its default:

| Key | Default | What it controls |
| --- | --- | --- |
| `base_poll_seconds` | `3.0` | How often the base branch is re-checked for commits made outside aGiTrack (so worktrees pick them up). |
| `background_poll_seconds` | `2.0` | How often an idle background session is serviced (committed / integrated). |
| `file_stable_seconds` | `8.0` | Quiet period after a file change before an auto-commit. |
| `child_idle_seconds` | `4.0` | No backend output for this long counts as idle. |
| `parse_cooldown_seconds` | `10.0` | Minimum gap between agent-turn parses. |
| `base_edit_check_seconds` | `3.0` | How often aGiTrack warns about edits to the base repo when the sandbox is unavailable. |
| `cwd_check_seconds` | `3.0` | How often aGiTrack checks for the Claude resume-cwd drift bug. |
| `base_drift_check_seconds` | `2.0` | How often aGiTrack checks whether the base repo was switched to another branch outside aGiTrack. |
| `summary_wait_seconds` | `45.0` | How long integration waits for a background commit summary before proceeding without it. |


## Contributing

Install dependencies and the optional git hooks:

```bash
uv sync --group dev
make install-hooks
```

This installs two hooks:

- **commit** — `ruff` (lint + format) and basic file hygiene, so commits stay fast.
- **push** — the full CI-equivalent gate (`ruff`, `mypy` vs the baseline, tests + coverage), so a push that would break CI fails locally first.

Run the same full gate by hand at any time:

```bash
make check        # or: ./scripts/check.sh
```

This is the definition of "done" for a change — it mirrors CI exactly (`.github/workflows/ci.yml`).