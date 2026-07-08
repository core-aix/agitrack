<!--
  KEEP THIS DIAGRAM IN SYNC WITH THE ACTUAL USER FLOW.
  Whenever the user flow changes — a new prompt, a changed option label, a new
  decision based on file/commit status, a new Ctrl-G command, a changed exit or
  update path — update the matching graph below in the same change. AGENTS.md
  records this as a standing requirement.
-->

# aGiTrack User Flow

A complete map of what aGiTrack shows the user, **when**, and what each choice does —
including which file/commit status triggers which prompt and where every nested option
leads. Read it top to bottom: start at [Lifecycle](#1-lifecycle-overview), then drill
into whichever phase you care about.

> This document is the source of truth for the interactive flow. If you change the flow,
> change the diagram (see the note at the top of the file).

**Legend**

```mermaid
flowchart LR
  A(["Start / end"]) --- B["Action aGiTrack takes"]
  B --- C{"Decision on state"}
  C --- D[/"User is prompted"/]
  D --- E[["Background / automatic"]]
```

- `([ ])` start/end · `[ ]` an action · `{ }` a decision on repo/commit/session state ·
  `[/ /]` a prompt shown to the user · `[[ ]]` background/automatic work, no prompt.
- Edge labels are the user's answer, or the condition that selects that branch.

---

## 1. Lifecycle overview

```mermaid
flowchart TD
  start(["Run agitrack"]) --> launch["Startup and launch gating"]
  launch --> loop{"What does the user do?"}

  loop -->|Types text| pre["Before forwarding the prompt: commit/stage base and worktree"]
  pre --> turn["Agent runs the turn"]
  turn --> commit[["Auto-commit the turn onto the turn branch"]]
  commit --> integrate[["Integrate turn branch into base"]]
  integrate --> copyback["Offer to copy worktree-only files into the base repo"]
  copyback --> loop

  loop -->|Ctrl-G| menu["Ctrl-G command menu"]
  menu --> loop

  loop -->|Esc during a turn| cancel["Handle the cancelled turn"]
  cancel --> loop

  loop -->|Update available| upd["aGiTrack self-update notice"]
  upd --> loop

  loop -->|Terminal or window closed| sig["Signal exit: finalize pending work"]
  loop -->|"Ctrl-G then 'exit aGiTrack'"| ex["Exit confirmation"]
  sig --> done(["aGiTrack exits"])
  ex --> done

```

**Jump to:** [Startup and launch gating](#2-startup-and-launch-gating) ·
[Worktrees vs no-worktree](#3-worktrees-vs-no-worktree) ·
[Manual-commit mode](#3a-manual-commit-mode---manual-commits---m) ·
[Before forwarding a prompt](#4-before-forwarding-a-prompt-base-to-worktree) ·
[The agent turn](#5-the-agent-turn-auto-commit-and-integration) ·
[Copy worktree-only files](#6-after-the-turn-copy-worktree-only-files-to-base) ·
[Ctrl-G command menu](#7-ctrl-g-command-menu) ·
[Self-update flow](#9-self-update-flow) ·
[Exit and terminal close](#10-exit-and-terminal-close)

> Under **`--manual-commits` / `-m`** the auto-commit → integrate steps above are replaced: each turn
> is recorded as a hidden latent commit and the branch is only ever written by **your** commit, which
> folds the pending turns in. See [Manual-commit mode](#3a-manual-commit-mode---manual-commits---m).

---

## 2. Startup and launch gating

Everything that must resolve before the backend TUI appears.

```mermaid
flowchart TD
  start(["Run agitrack"]) --> priv{"Privacy notice acknowledged before?"}
  priv -->|No| privask[/"Show privacy notice, ask to acknowledge"/]
  privask -->|Declines| quit(["Exit"])
  privask -->|Acknowledges| lock
  priv -->|Yes, or --skip-privacy-ack| lock

  lock{"Acquire single-writer repo lock .agitrack/lock?"}
  lock -->|Held by another aGiTrack process| busy[["Refuse to start: name the holding process (PID) and tell the user to stop it first"]]
  busy --> quit
  lock -->|Acquired, or stale owner reclaimed| backend

  backend{"Selected backend CLI installed on PATH?"}
  backend -->|No| binstall[/"Show install instructions: install now, or switch to an installed backend"/]
  binstall -->|Installs and retries| backend
  binstall -->|Picks another backend| backend
  binstall -->|Neither available| quit
  backend -->|Yes| firstrun

  firstrun{"First ever run? No global default backend and no --backend"}
  firstrun -->|Yes| pick[/"Choose default backend, alphabetical, shows installed state"/]
  pick --> updchk
  firstrun -->|No| updchk

  updchk[["Startup update check, TTY-gated, short network timeout"]] --> updq{"aGiTrack update available and checks enabled?"}
  updq -->|Yes| updoffer[/"Offer to update now, see Self-update"/]
  updq -->|No, non-TTY, or disabled| sess
  updoffer --> sess

  sess["Pick the session to show: resume the repo's pinned session, or start fresh"] --> spawn["Spawn backend TUI in its worktree under the sandbox"] --> ready(["Ready for input"])

```

**Jump to:** [Self-update flow](#9-self-update-flow) ·
[Worktrees vs no-worktree](#3-worktrees-vs-no-worktree)

---

## 3. Worktrees vs no-worktree

Where a session physically runs (`--no-worktree` turns worktrees off) — this decides whether the base-to-worktree and
worktree-to-base flows below apply at all. `--manual-commits` / `-m` is a **commit-timing variant of no-worktree**: it
**always runs without a worktree** (it implies `--no-worktree`) and additionally defers every commit to the user (see [Manual-commit mode](#3a-manual-commit-mode---manual-commits---m)).

```mermaid
flowchart TD
  s(["Start or switch a session"]) --> mode{"use_worktrees? Default true; off via --no-worktree/-m or config"}
  mode -->|Worktrees on| wt["Open/create a worktree at .agitrack/worktrees/&lt;name&gt;; agent edits there, isolated"]
  wt --> wtnote[["Base repo is the user's tree; worktree is the agent's sandbox. Both base-to-worktree and worktree-to-base flows apply"]]
  mode -->|No-worktree| base["Session runs on the BASE tree, worktree=None, commits to the checked-out branch"]
  base --> mc{"Manual commits? -m / manual_commits"}
  mc -->|No: auto-commit each turn| warn[/"One-time caveat: parallel no-worktree sessions share the dir and may conflict"/]
  warn --> basenote[["No isolation: the per-turn commit captures whatever is in the tree. Copy-back and base-sync flows are no-ops"]]
  mc -->|Yes: user-triggered commits| mannote[["Turns are recorded as HIDDEN latent commits; the branch is untouched until YOU commit. See Manual-commit mode"]]

  base --> resume{"Resuming a session first run in a worktree?"}
  resume -->|Yes| retarget[["Retarget the transcript's recorded cwd to the base dir so it doesn't reopen the gone worktree path"]]
```

**Jump to:** [Manual-commit mode](#3a-manual-commit-mode---manual-commits---m)

---

## 3a. Manual-commit mode (`--manual-commits` / `-m`)

Makes agent-assisted coding feel like normal git: **you** decide when to commit. It **always runs
without a worktree** (it implies `--no-worktree`, so the agent edits the current branch directly),
but aGiTrack does **not** create a commit each turn. Instead every turn is recorded as a hidden **latent** commit on a side ref
(`refs/agitrack/manual/<session>`) that your branch never shows, while `HEAD` stays put — so your
history stays clean while you work, and no interaction is lost.

When **you** commit — through `Ctrl-G → git-commit` **or** an ordinary `git commit` on the command
line / in your editor — aGiTrack folds every pending latent turn's interaction trace and metadata
into that **one** commit, alongside your own edits. A managed `prepare-commit-msg` hook does the
folding and a `post-commit` hook resets the latent ref; both are installed only for the manual-mode
session and removed on exit. So you always get a single, self-contained commit carrying your changes
**and** the full agent tracking — whether or not the commit went through aGiTrack's menu, with no
separate cover commit and no `<aGiTrack>` commits polluting the branch. Enable it for every run with
`"manual_commits": true` in config.

```mermaid
flowchart TD
  s(["Manual-commit mode: -m / manual_commits (implies --no-worktree)"]) --> turn["Agent runs a turn, editing the current branch directly"]
  turn --> changed{"Working tree changed since the last latent turn?"}
  changed -->|No| loopb(["Back to the input loop — nothing recorded"])
  changed -->|Yes| latent[["Record a HIDDEN latent commit on refs/agitrack/manual/&lt;session&gt;: snapshot the working tree, same &lt;aGiTrack&gt; trace + metadata as a normal turn commit. HEAD does NOT move; your git index is untouched"]]
  latent --> dash[["Dashboard shows it live as a 'pending' turn until you commit"]]
  dash --> loopb

  loopb --> uc{"You commit? Ctrl-G → git-commit, OR an external git commit"}
  uc -->|Hook installed| fold[["prepare-commit-msg folds the pending turns' trace + metadata into YOUR message → ONE commit with your changes AND all agent tracking; post-commit resets the latent ref to the new commit"]]
  uc -->|"core.hooksPath set (aGiTrack can't install the hook)"| cover[["Fallback: a HEAD poll detects the commit → aGiTrack adds a cover commit carrying the pending tracking (its tree = the new commit's, so it adds no diff) → reset the ref"]]
  fold --> clean(["Branch now has ONE clean commit; 0 pending turns"])
  cover --> clean
```

> The single folded commit carries a `commit_type: user` block plus one block per agent turn, so the
> dashboard parses it as a squash — summing the turns' tokens and classifying it as agent-tracked
> work. A commit made with **no** pending turns is still a plain, session-attributed user commit.
> Recovery: if a prior run left the latent ref already contained in `HEAD` (an interrupted commit),
> the next start resets it so those turns are never re-folded.

**Jump to:** [`git-commit`](#8-git-commit) · [The agent turn](#5-the-agent-turn-auto-commit-and-integration)

---

## 3b. Background mode (`--background` / `-b`)

Runs aGiTrack **without the interactive TUI** so you can drive the coding agent from **any** UI (the
**Claude desktop app**, an IDE extension, its native CLI, a chat window) while aGiTrack tracks that
session in the background — the interactive-UI-agnostic tracker of
[issue #143](https://github.com/core-aix/agitrack/issues/143). Especially handy if you'd rather stay
in a GUI than a terminal. aGiTrack does **not** launch or relay the agent; it watches the agent's
on-disk session transcript for the repo (**only** this repo's — Claude by its cwd-encoded transcript
dir, OpenCode by each session's recorded directory), records each completed turn, summarizes it, and
installs the fold hooks. It **always runs without a worktree** (implies `--no-worktree`), with either
commit style (auto — default — or `--manual-commits`).

**It runs as a DETACHED daemon, like `agitrack -d`.** `agitrack -b` starts the tracker in the
background and **returns to your shell immediately**; unlike the dashboard daemon it has **no
owner-terminal watchdog**, so it keeps tracking after the terminal closes — stop it with
`agitrack -b stop`. `agitrack -b status` reports whether one is running; **`agitrack --status` / `-s`**
reports the mode of whatever is tracking the repo (interactive vs background, auto/manual,
worktree/no-worktree, or not running) and whether **auto-start on commit** is enabled. The daemon
logs activity to `<repo>/.agitrack/background.log`, appends notable events to a user `--log-file`,
and reminds you (never auto-installs) when an aGiTrack update is available. Auto commits are **clean
agent commits** (subject = the LLM summary, one metadata block); the daemon waits briefly for the
summary before folding since it never amends HEAD. Only one aGiTrack runs per repo (the shared repo
lock refuses a second start).

**Never forget to start it — auto-start on commit.** aGiTrack installs a PERSISTENT `pre-commit`
hook (surviving the daemon exiting). When you `git commit` while no tracker is running and the AI
actually changed code, the hook folds that AI work's trace into your (own, manual) commit AND
**auto-starts** the daemon for the turns that follow — in the **same commit mode as your last run** —
printing an explicit "started automatically … stop with `agitrack -b stop`; disable with
`agitrack --remove-hooks`" message. A purely human commit is left untouched. The first `agitrack -b`
per repo asks whether to enable this (default on; repo-scoped `autotrack_hook`); `agitrack --remove-hooks`
turns it off. The hook calls the CURRENT aGiTrack even after a self-update (frozen-aware invocation +
PATH fallback).

**Keeping the hook's schema current.** Every background start (when `autotrack_hook` isn't `off`)
re-installs this hook and **stamps the running aGiTrack version** into it (a
`# AGITRACK-AUTOTRACK-VERSION <version>` line). Before writing, it compares the version stamped in the
already-installed hook against the running one: if the installed hook is **older** (or predates version
stamping and carries no stamp), aGiTrack **removes the previously installed hook** — restoring any
project hook it had chained — and then **installs the current version fresh**, so a changed hook schema
is never left half-migrated. A same-or-newer stamp just refreshes the baked invocation in place. This
runs on the background startup path (and equally when a no-worktree interactive session installs the same
persistent hook).

```mermaid
flowchart TD
  s(["Background mode: -b / background (implies --no-worktree); manual (default) or --auto-commit"]) --> hookq{"Auto-start enabled? autotrack_hook != off (and no custom core.hooksPath)"}
  hookq -->|No| watch
  hookq -->|Yes| stamp{"Installed pre-commit hook's stamped aGiTrack version vs the running one"}
  stamp -->|Older / unstamped schema| replace[["Remove the previously installed aGiTrack hook (restore any chained project hook), then install the current version fresh"]]
  stamp -->|Same or newer| refresh[["Refresh the baked invocation in place; keep the stamped version"]]
  replace --> watch
  refresh --> watch
  watch["Poll the newest backend session transcript in the repo dir (follows in-backend session switches)"]
  watch --> turn{"A newly completed turn?"}
  turn -->|No| watch
  turn -->|Yes| latent[["Record a HIDDEN latent commit on refs/agitrack/manual/&lt;session&gt; + summarize it (git note). HEAD does NOT move"]]
  latent --> mode{"Commit style?"}
  mode -->|Manual| user(["Wait for YOUR commit → prepare-commit-msg folds the pending turns in (cover fallback)"])
  mode -->|Auto| auto{"Working tree still has the agent's uncommitted work?"}
  auto -->|"Yes (agent didn't commit)"| aGit[["aGiTrack commits it itself, folding the pending turns' trace + metadata → ONE tracked commit; reset the ref"]]
  auto -->|"No (agent committed its own work)"| hook[["The prepare-commit-msg hook already folded the tracking into the agent's commit; cover is only the fallback"]]
  user --> watch
  aGit --> watch
  hook --> watch
```

The per-conversation commit watermark makes this exact even when you switch conversations inside the
backend (a `/resume` or a new conversation, tested on both backends): aGiTrack follows the newest
session and counts each conversation's turns exactly once, and sub-agent tokens are folded into the
launching turn. `agitrack -b stop` records any final turn (folding it in auto mode) and removes its
per-run fold hooks (the persistent auto-track `pre-commit` hook stays, so a later commit still tracks).

**Jump to:** [Manual-commit mode](#3a-manual-commit-mode---manual-commits---m) · [The agent turn](#5-the-agent-turn-auto-commit-and-integration)

---

## 4. Before forwarding a prompt (base to worktree)

Runs every time the user submits text, **before** the backend sees it
(`_pre_agent_commit_if_needed`). The point: capture the user's own edits as a user
commit and make sure the agent starts from them. Driven by where uncommitted work lives.

```mermaid
flowchart TD
  submit(["User submits a prompt"]) --> wtq{"This session's WORKTREE dirty?"}
  wtq -->|Yes| wtc[/"Prompt for a user commit message, blank is rejected"/]
  wtc --> wtcommit[["Commit the worktree onto its turn branch: git add -u for tracked; ask about untracked"]]
  wtq -->|No| baseq
  wtcommit --> baseq

  baseq{"BASE repo has user edits? Any tracked change OR any new untracked file, excluding .agitrack/"}
  baseq -->|Tracked changes| basecommit[/"Prompt for a user commit message, commit onto the base branch"/]
  baseq -->|New untracked file| stage[/"Stage all N new file(s)? y/N, lists the files"/]
  baseq -->|Nothing pending| fwd

  stage -->|y| basecommit
  stage -->|N, leave unstaged| decl[["Remember this base tree fingerprint so the same state isn't re-prompted; re-offered once the tree changes"]]
  decl --> fwd

  basecommit --> sync[["_sync_idle_worktrees_to_base: merge / re-point the new base commit into the worktree(s)"]]
  sync --> fwd["Forward the prompt to the backend"]
  fwd --> go(["Agent turn begins"])
```

**Jump to:** [The agent turn](#5-the-agent-turn-auto-commit-and-integration)

> The explicit base commit paths (this pre-prompt offer and the `git-commit`
> command) re-offer **every** untracked file (`include_declined=True`), so a previously
> declined file can always be staged here. The automatic worktree capture keeps the
> agent's own untracked decline sticky.

---

## 5. The agent turn: auto-commit and integration

```mermaid
flowchart TD
  go(["Prompt forwarded"]) --> run[["Agent works in the worktree, writes confined by the sandbox"]]
  run --> endq{"How did the turn end?"}

  endq -->|Final agent message| parse[["Parse the turn: prompts, final reply, exact token usage"]]
  parse --> mmode{"Manual-commit mode? (-m)"}
  mmode -->|Yes| mlatent[["Record the turn as a HIDDEN latent commit on refs/agitrack/manual/&lt;session&gt; (HEAD frozen; no integration). See Manual-commit mode"]]
  mlatent --> back
  mmode -->|No| changed{"Code changed AND staged changes exist?"}
  changed -->|Yes| acommit[["Create the &lt;aGiTrack&gt; turn commit: subject = tag + latest query; body = interaction trace + metadata"]]
  changed -->|No| copy
  acommit --> delay{"--delay-merge set?"}
  delay -->|No| integ[["Integrate the turn branch into base, serialized and completion-ordered; agent resolves conflicts in its worktree"]]
  delay -->|Yes| hold[/"Notice: changes held for review, names the worktree path. Merge only on explicit confirm via the session menu"/]
  integ --> conflict{"Merge conflict aGiTrack can't auto-resolve?"}
  conflict -->|Yes| cprompt[/"Auto-prompt the agent with the conflicting commits, pause for the user"/]
  conflict -->|No| copy
  hold --> copy
  cprompt --> copy

  endq -->|User pressed Esc with uncommitted edits| cancel[/"Keep them, commit with your next turn / Commit the changes now / Discard the changes"/]
  cancel -->|Keep| keep[["Remember 'keep' for this turn id so it isn't re-asked"]]
  cancel -->|Commit now| acommit
  cancel -->|Discard| dconf[/"Discard ALL uncommitted changes from the interrupted turn? Cannot be undone"/]
  dconf -->|Confirms| discard[["Discard all changes; advance the parse watermark"]]
  dconf -->|Cancels| keep
  keep --> copy
  discard --> copy

  copy["Offer to copy worktree-only files to base"] --> back(["Back to the input loop"])
```

> **The whole status → commit → integrate pipeline above runs on a dedicated git worker
> thread, never the main one** — so a `git status`/commit/merge can never block your typing,
> even right after an edit. Any dialog it needs (the conflict prompt, the copy/keep/discard
> offers) is handed to the main thread to present and the answer passed back; the worker only
> ever touches the **foreground** session. Background sessions are committed/integrated on the
> main thread (rare, throttled, and never the session you're typing in).

**Jump to:** [Copy worktree-only files to base](#6-after-the-turn-copy-worktree-only-files-to-base)

---

## 6. After the turn: copy worktree-only files to base

Only for a worktree session. Catches files the agent left UNCOMMITTED or that are
git-ignored — they integrate into nothing, so the user working in the base dir would
never see them (`_offer_copy_unstaged_to_base`). It runs for the **active** session only
(a background session is never interrupted mid-run); its files are caught instead when you
**switch to it** or on **aGiTrack exit**, just before the worktree is deleted.

First, though, aGiTrack offers to **commit** any of the user's own uncommitted edits in
the worktree (`_offer_user_commit_for_worktree_edits`) — those belong in git, not just
copied. So when both a user edit and copy-able leftovers exist, **both prompts appear**: a
commit prompt for the edits, then the copy prompt for the leftovers.

```mermaid
flowchart TD
  start(["Trigger: active session idle after a turn (committed or not), OR switched to this session, OR aGiTrack exiting"]) --> wtq{"Worktree session? (no-op under --no-worktree)"}
  wtq -->|No| done(["Nothing to do"])
  wtq -->|Yes| useredit{"User's OWN uncommitted edits in the worktree? (tracked changes / new non-declined files)"}
  useredit -->|Yes| ucommit[/"Uncommitted changes in this worktree — commit them? (the normal user-commit prompt; see git-commit). Then continue to the copy offer"/]
  useredit -->|No| gather
  ucommit --> gather[["List worktree files that won't merge: intentionally unstaged or git-ignored (new agent files are auto-staged + committed). Skip .agitrack/ and names starting with _ or ."]]
  gather --> any{"Any candidate files left to copy?"}
  any -->|No| done
  any -->|Yes| muted{"This whole SET already declined, AND no genuinely new file? (applies in EVERY context, incl. exit)"}
  muted -->|Yes| done
  muted -->|No: first ask, or a NEW path re-opens the whole set| ctx{"Context?"}

  ctx -->|Exiting| offerx[/"N file(s) will be DELETED when this worktree is removed on exit. Copy them into the base repo first? Esc cancels the exit so you can handle them yourself. (Files listed vertically under 'File(s):', PgUp/PgDn scrolls) • No, discard them with the worktree • Yes, copy to the base repo"/]
  ctx -->|Turn or switch| offer[/"N file(s) won't be merged. Copy them into the base repo? (listed vertically under 'File(s):', scrollable) Note: declining won't re-ask until the fileset changes or you switch sessions. • No, leave them in the worktree • Yes, copy to the base repo"/]

  offer -->|No| mute[["Mute this whole set of paths (re-opened only by a new file / switch / restart); notice names the worktree path"]]
  offerx -->|No, discard| disc[["Files are discarded with the worktree"]]
  offerx -->|Esc| abortx(["Abort the exit: keep the worktree + files; aGiTrack stays running so you can handle them"])
  mute --> done
  disc --> done

  offer -->|Yes| conflictq{"Any would overwrite an existing base file?"}
  offerx -->|Yes| conflictq
  conflictq -->|No conflicts| copyall[["Copy every file into the base dir"]]
  conflictq -->|Yes| ow[/"N already exist in the base repo. Overwrite them? • No, keep the base versions • Yes, overwrite all • Let me confirm each one"/]

  ow -->|No, keep the base versions| skipc[["Skip the conflicting files; still copy the non-conflicting new ones"]]
  ow -->|Yes, overwrite all| copyall
  ow -->|Let me confirm each one| each[/"Per conflicting file: overwrite it? • No, keep the base version • Yes, overwrite"/]
  each --> tally
  skipc --> tally
  copyall --> tally[["Report copied count; anything not copied gets the 'files remain' notice"]]
  tally --> done
```

**Jump to:** [`git-commit`](#8-git-commit)

> A file already accepted or left in place isn't re-offered until its content changes
> (fingerprint). Declining mutes the whole current **set of paths** — aGiTrack won't ask
> again while only those files keep changing; a genuinely new path re-opens the whole set
> (ask about all again). The mute clears on session switch and aGiTrack restart. The
> **exit** offer ignores the mute (the files are about to be deleted) and warns as much.

---

## 7. Ctrl-G command menu

`Ctrl-G` opens the command palette (type a prefix, Up/Down to select, Tab to complete,
Enter to run). Commands, in palette order:

> **One rule across every menu: Esc goes up exactly one level.** The **command palette is
> the parent of every command menu**, so Esc on a command menu (the sessions list, the
> settings list, the summarizer menu…) returns you **to the palette** — not to the agent.
> Esc on the palette returns to the agent. Esc in a sub-menu returns to the menu that opened
> it (e.g. Esc in *Manage <one shared session>* → the shared-sessions list → the sessions
> menu → the palette → the agent — one step per Esc). The only thing that unwinds further is
> a choice that moves you into a **different session** (switch / new / resume): that drops
> straight to the agent, since there is no level to come back to.
>
> Navigation is **silent and instant**: backing out shows no "closed/cancelled" message and
> never flashes the bare backend screen between levels. Internally each menu is one loop
> returning just `UP` (Esc/back → caller re-shows itself) or `DONE` (a session transition →
> unwind to the agent); a child menu the user backs out of simply re-shows its parent. So Esc
> unwinds the call stack one frame at a time and the on-screen hierarchy mirrors the code's.

```mermaid
flowchart TD
  g(["Ctrl-G"]) --> pal[/"Command palette"/]
  pal --> sessions["sessions"]
  pal --> backend["agent-backend"]
  pal --> summ["summarizer"]
  pal --> settings2["settings"]
  pal --> gunstaged["git-unstaged"]
  pal --> gcommit["git-commit"]
  pal --> dash["dashboard"]
  pal --> update["update"]
  pal --> exit["exit aGiTrack"]

  settings2 --> setmenu["Settings menu"]

  sessions --> smenu[/"Sessions menu (live sessions show running/idle; dormant worktrees and shared markers listed too)"/]
  smenu -->|Switch to a live session| sswitch[["Show it, relaunch TUI, re-baseline so history isn't re-committed; then offer to copy its worktree-only files"]]
  smenu -->|Resume an idle/dormant worktree| sresume[["Reopen that session in its worktree; continue the backend conversation if still recorded"]]
  smenu -->|Resolve an unmerged dormant worktree| sresolve[/"Integrate its pending commits, or discard the worktree (confirmed)"/]
  smenu -->|+ New session| snew["Start a new session: own worktree, or shared base dir under --no-worktree"]
  smenu -->|✎ Rename a session| srename[["Rename = fork: clears the shared lineage, so a later share creates a NEW shared entry"]]
  smenu -->|⤳ Change a session's merge branch| smb[/"Pick the branch this idle session integrates into (flushes its pending work into the old branch first)"/]
  smenu -->|↻ Resume a past conversation| spast[/"Pick from past conversations of this repo, newest first"/]
  smenu -->|⇪ Share this session with collaborators| sshare["Session sharing"]
  smenu -->|⇩ Resume a shared session| srshare["Session sharing"]
  smenu -->|⚙ Manage shared sessions| smanage["Session sharing"]
  smenu -->|✓ Integrate this session's commits / Merge reviewed changes| smerge[/"Integrate this session's committed work into its base branch (pick the branch under --delay-merge)"/]
  smenu -->|- Stop a session| sstop[["Stop the session"]]
  snew --> mode3["See Worktrees vs no-worktree"]
  sswitch --> scopy["See After the turn: copy worktree-only files"]

  backend -->|No arg, picker| bpick[/"Pick claude or opencode"/]
  bpick --> bswitch[["Save current backend's session, relaunch target backend, restore its last session, update global default"]]

  summ --> smm[/"Summarizer menu: Toggle (ON/OFF) / Set model"/]
  smm -->|Toggle| stog[["Flip on/off; menu re-shows"]]
  smm -->|Set model| spick[/"Pick the summarizer model (current shown); Esc → back to the Summarizer menu"/]
  spick --> smm

  gunstaged --> gu[["Show intentionally-unstaged files in the status bar"]]

  gcommit --> gcflow["git-commit flow"]

  dash --> dserve[["Serve the read-only metrics dashboard; open the local browser only if it lands on this machine, else print the URL"]]

  update --> uflow["aGiTrack self-update flow"]

  exit --> exflow["Exit confirmation"]
```

**Jump to:** [Worktrees vs no-worktree](#3-worktrees-vs-no-worktree) ·
[Copy worktree-only files](#6-after-the-turn-copy-worktree-only-files-to-base) ·
[`git-commit`](#8-git-commit) ·
[Self-update flow](#9-self-update-flow) ·
[Exit and terminal close](#10-exit-and-terminal-close) ·
[Session sharing](#11-session-sharing) ·
[Settings menu](#12-settings-menu)

---

## 8. `git-commit`

Creates a user commit (no `<aGiTrack>` tag) from the user's own edits, from whichever
tree holds them — the base repo and/or this session's worktree.

> **Under `--manual-commits` / `-m` this command does more.** Manual mode always runs
> **no-worktree** (it implies `--no-worktree`), so there is only the base tree — and this same
> `git-commit` is the one command used for **both** a plain user commit and a commit that includes
> the agent's tracked work. It stages your changes, then folds every pending latent turn's trace +
> metadata into the message so the result is a **single** commit carrying your edits *and* the full
> agent tracking; the latent ref is then reset. (An external `git commit` you run yourself gets the
> same folding via the `prepare-commit-msg` hook.) See
> [Manual-commit mode](#3a-manual-commit-mode---manual-commits---m).

```mermaid
flowchart TD
  start(["git-commit"]) --> where{"Which tree is dirty?"}
  where -->|Worktree| wmsg[/"Ask for a commit message, blank rejected"/]
  where -->|Base repo| bmsg[/"Ask for a commit message, blank rejected"/]
  where -->|Both| wmsg
  where -->|Neither| none[/"No changes to commit"/]

  wmsg --> wuntracked{"Untracked files present?"}
  wuntracked -->|Yes| wstage[/"Stage all N new file(s)? y/N, re-offers every untracked file"/]
  wuntracked -->|No| wdo
  wstage --> wdo[["git add -u plus staged untracked, commit onto the turn branch"]]
  wdo --> bmsg

  bmsg --> buntracked{"Untracked files present?"}
  buntracked -->|Yes| bstage[/"Stage all N new file(s)? y/N"/]
  buntracked -->|No| bdo
  bstage --> bdo[["Commit onto the base branch"]]
  bdo --> bsync[["Sync the base commit into the worktree(s)"]]
  bsync --> done(["Done"])
  none --> done
```

---

## 9. Self-update flow

aGiTrack updating **itself** (not the backend agent). Checked at startup and every
`update_check_seconds` (default 300s) on a worker thread.

```mermaid
flowchart TD
  chk[["Check: source-linked vs package install"]] --> avail{"Update available?"}
  avail -->|No| idle(["Nothing to do"])
  avail -->|Yes| merging{"A merge or conflict resolution in progress in ANY session?"}
  merging -->|Yes| holdn[["Hold the notice until the merge finishes"]]
  holdn --> avail
  merging -->|No| where{"When?"}

  where -->|At startup, TTY| soffer[/"Offer to update before launching"/]
  where -->|During a session| inoffer[/"Status-bar notice plus Ctrl-G update command"/]

  soffer -->|Stop checking| off[["Disable update checks"]]
  soffer -->|Decline| idle
  soffer -->|Accept| apply
  inoffer -->|Accept| wait[["Defer until every session is finished and all commits are integrated: no agent in flight, no pending parse/merge/summary/background session"]]
  wait --> apply

  apply{"Apply the update"}
  apply -->|Source-linked| src[["Fast-forward only; abort with a message if the checkout is dirty or diverged"]]
  apply -->|Package| pkg[["Upgrade via the running interpreter's pip (PEP 668 defers to brew or prints every manual route). Runs DETACHED in its own session so a terminal-close SIGHUP mid-upgrade can't kill pip between uninstall and reinstall"]]
  src --> ok{"Succeeded?"}
  pkg --> ok
  ok -->|Yes| reexec[["Re-exec python -m agitrack so the new code loads"]]
  ok -->|No| failnotice[["Never crashes: record the target version, show a single manual-update reminder next startup; keep running current version"]]
  reexec --> done2(["Updated"])
  failnotice --> done2
```

> **An interrupted upgrade never leaves aGiTrack uninstalled.** `pip install --upgrade`
> removes the old version before writing the new, so the upgrade runs in its own session
> (`start_new_session`) and a SIGHUP/SIGTERM that arrives while it applies is **ignored**
> (the apply + restart finish) — closing the terminal or quitting VS Code mid-upgrade can no
> longer strand the package half-removed.

> Distinct from the **backend agent** (Claude / OpenCode) updating itself: that runs
> inside the agent TUI, and the sandbox is built to keep the agent's own install dirs
> writable so it always works. See `agitrack/proxy/sandbox.py`.

---

## 10. Exit and terminal close

Exiting **always asks first** — a deliberate safety net — regardless of whether anything is
pending. Pressing **Esc** at any exit prompt **cancels the exit**: aGiTrack keeps running and
tells you what was *not* done, so you can handle it yourself (commits already made this exit
are kept; nothing is deleted). Only an explicit "Yes"/"No" choice proceeds.

```mermaid
flowchart TD
  how{"How is aGiTrack ending?"}
  how -->|"Ctrl-C, or Ctrl-G then 'exit aGiTrack'"| conf[/"Exit aGiTrack? • No, keep working • Yes, exit (or press Ctrl-C again) — Esc cancels"/]
  conf -->|No / Esc| stay(["Exit cancelled — keep working (a message says nothing was shut down)"])
  conf -->|Yes| busy{"Background sessions still running?"}
  busy -->|Yes| term[/"Terminate them and exit? • No, keep working • Yes, terminate them and exit (Esc cancels)"/]
  busy -->|No| fin
  term -->|No / Esc| stay
  term -->|Yes| fin

  how -->|"Terminal or window closed, SIGHUP/SIGTERM (incl. system restart)"| sig[["_handle_exit_signal: note whether work was in progress, then best-effort finalize, render suppressed (non-interactive)"]]

  fin[["Finalize each session: commit a just-completed turn, integrate committed work. ALWAYS shows a notice the moment exit begins (teardown can take seconds) — a specific 'Finishing up before exit — …' naming the work when known, else a generic 'Finalizing things before exiting…'"]]
  fin --> copy2["Per session, before deleting its worktree: offer to copy its leftover files (see Copy)"]
  copy2 --> esc{"Esc on that copy offer?"}
  esc -->|Yes| abort(["Exit cancelled — worktree + files kept; message tells you to copy them then exit again"])
  esc -->|No| rm[["Remove the (fully-integrated) worktree(s), stop the dashboard"]]
  sig --> fin
  rm --> pend{"Forced close (SIGHUP/SIGTERM) that interrupted work, on a macOS desktop?"}
  pend -->|"No (chose to exit, clean close, or no GUI)"| bye(["aGiTrack exits"])
  pend -->|Yes| ask[/"Out-of-terminal dialog: Reopen aGiTrack • Quit aGiTrack (auto-quits after 25s, so a restart never hangs)"/]
  ask -->|Quit / timeout| bye
  ask -->|Reopen| again[["Release the lock, then open a new window running aGiTrack in the repo (last session auto-resumes)"]]
  again --> bye
```

**Jump to:** [Copy worktree-only files to base](#6-after-the-turn-copy-worktree-only-files-to-base)

---

## 11. Session sharing

Sharing pushes a session's **redacted** backend transcript to `origin` on a custom ref
(`refs/agitrack/shared-sessions`), keyed by repo + your GitHub id + a name, so collaborators
on the same repo can resume your conversation. Opt-in, with consent on every share — the
first prompt spells out exactly what is uploaded (`_share_session`,
`_resume_shared_session_menu`, `_manage_shared_sessions_menu`). Only backends with a portable
transcript (Claude) support it.

### Share this session

```mermaid
flowchart TD
  s(["⇪ Share this session"]) --> sup{"Backend supports sharing AND a resumable session exists?"}
  sup -->|No| nope[/"Not supported / nothing to share yet — explain why"/]
  sup -->|Yes| consent{"Consent — shown every share: the transcript may include file contents, command output, and secrets"}
  consent -->|No, cancel| cancel(["Cancelled"])
  consent -->|Yes, share it| redact[["Export + REDACT the transcript, build the manifest, record the lineage origin: owner + name + contributors"]]
  redact --> push[["Push to origin in the BACKGROUND so the terminal never freezes; the result lands as a notice"]]
  push --> behind{"Shared copy already has NEWER turns than this machine?"}
  behind -->|Yes| skip[["Refuse to rewind it: tell the user to resume the shared version first, then share again"]]
  behind -->|No| okp[["Shared (or saved locally if there is no remote). A diverged collaborator's turns are union-merged in, never lost"]]
  redact --> autoq{"Already auto-shared?"}
  autoq -->|No| autop[/"Keep this shared session up to date automatically? • Yes, keep it updated • No, I'll re-share manually"/]
  autop -->|Yes| auton[["Auto-update ON: every new turn (and exit) re-pushes the latest"]]
```

### Resume a shared session

```mermaid
flowchart TD
  r(["⇩ Resume a shared session"]) --> fetch[["Fetch shared sessions from origin (cancellable)"]]
  fetch --> anyr{"Any found for this repo?"}
  anyr -->|No| noner(["None found"])
  anyr -->|Yes| pickr[/"Pick one (newest first; shows model + age)"/]
  pickr --> origin[["Record its lineage origin, so a later re-share updates the SAME entry and adds you to the contributors"]]
  origin --> wherer{"Is this conversation already open locally?"}
  wherer -->|Running here, same id| livr[/"Update this session to the shared version / Keep both (copy to a new session) / Stay as it is — guards against replacing newer local work with an older shared copy"/]
  wherer -->|Open under the same shared lineage, different backend id| linr[/"Continue my existing session / Fetch the shared version as a separate copy"/]
  wherer -->|Not open here| namer[/"Name the local session (defaults to the share name)"/]
  livr --> bgr[["Fetch transcript + import on a worker thread; the resume completes on the main loop so the UI never freezes"]]
  linr --> bgr
  namer --> bgr
```

### Manage / unshare

```mermaid
flowchart TD
  mg(["⚙ Manage shared sessions"]) --> mine{"You've shared any in this repo?"}
  mine -->|No| nonem(["Nothing to manage"])
  mine -->|Yes| pickm[/"Pick one (shows age, auto-update state, and a 'local has newer turns' hint)"/]
  pickm --> act{"Manage this shared session"}
  act -->|↻ Update now| upd[["Re-push the latest transcript in the background; folds you into the contributor set"]]
  act -->|Toggle auto-update| tog[["Turn auto-update on (pushes once immediately) or off"]]
  act -->|✗ Unshare| uconf{"Remove from origin for everyone? Can't be undone."}
  uconf -->|Yes, unshare| undo[["Delete the shared ref entry (background)"]]
  uconf -->|No, keep it| keepm(["Kept"])
```

> Renaming a session **forks** it (`_fork_lineage_on_rename`): the shared lineage origin is
> cleared, so sharing the renamed session creates a NEW `<you>/<new-name>` shared entry rather
> than updating the one it came from. The whole feature is opt-in; nothing is uploaded without
> an explicit "Yes" each time.

---

## 12. Settings menu

`Ctrl-G → settings` opens an editor for **all** config options, each labelled in plain
language and showing its current effective value and source (`· repo` / `· global`, or
nothing for a built-in default). The menu is a small **form**: edits are collected as
**pending** changes — each one picks its own scope, **This repository**
(`<repo>/.agitrack/config.json`) or **Global** (`~/.agitrack/config.json`) — and is
written only when you **save on the way out**. A pending row shows its new value as
`· UNSAVED → repo/global`. Precedence: repo-local wins over global wins over the built-in
default (`GlobalConfig` overlay).

**Esc goes up one level**, everywhere ([§7](#7-ctrl-g-command-menu) describes the same rule
for every menu): Esc at a value editor → back to the list; Esc at the scope prompt → back to
the value editor; Esc on the list → close. **Closing with unsaved changes asks whether to
save them** — *Yes, save them / No, discard them / ← Keep editing* — so nothing is written
silently and nothing is lost without a prompt.

```mermaid
flowchart TD
  s(["Ctrl-G → settings"]) --> list[/"Settings list — each: label, value, source (or '· UNSAVED → scope' for a pending edit). Plus 'Timings (advanced)…' and '← Close (save N change(s))'"/]
  list -->|"← Close / Esc, no pending edits"| close(["Settings closed."])
  list -->|"← Close / Esc, with pending edits"| savep[/"You have N unsaved change(s). Save them? • Yes, save them • No, discard them • ← Keep editing"/]
  savep -->|Yes| writeall[["Write every pending edit to its chosen scope (repo overlay / global file). Restart-only settings note: won't take effect until YOU restart aGiTrack — it never restarts on its own"]]
  savep -->|No| discard[["Drop all pending edits — nothing written"]]
  savep -->|← Keep editing| list
  writeall --> close
  discard --> close

  list -->|Pick 'Timings…'| tlist[/"Timings list (seconds), pending shown as '· UNSAVED → scope' + '← Back'"/]
  list -->|Pick a setting| edit{"Editor depends on the setting's kind"}

  edit -->|on/off setting e.g. sandbox| eb[/"Turn ON / Turn OFF / ← Back"/]
  edit -->|choice setting e.g. backend| ec[/"Pick a value / ← Back"/]
  edit -->|path-list setting e.g. allowed edit paths| ep[/"Type paths separated by the PATH separator (blank = none) / ← Back"/]
  edit -->|text setting e.g. model, menu key| et[/"Type a value (blank = unset) / ← Back"/]

  eb -->|← Back / Esc → up one level| list
  ec -->|← Back / Esc| list
  ep -->|← Back / Esc| list
  et -->|← Back / Esc| list
  eb --> scope
  ec --> scope
  ep --> scope
  et --> scope

  scope[/"Apply 'setting' to: • This repository only • Global — all repositories • ← Back"/]
  scope -->|"← Back / Esc → up one level (re-edit the value)"| edit
  scope -->|This repository| pend[["Record a PENDING edit (value + scope) — not written yet"]]
  scope -->|Global| pend
  pend --> list

  tlist -->|← Back / Esc → up one level| list
  tlist -->|Pick a timing| tval[/"Type new seconds (> 0) / ← Back"/]
  tval -->|"← Back / Esc"| tlist
  tval -->|valid| tscope[/"Apply timing to: repo / global / ← Back"/]
  tscope -->|repo or global| tpend[["Record a PENDING timing edit (saved with the rest on close)"]]
  tpend --> tlist
  tscope -->|"← Back / Esc → re-edit the value"| tval
```

> **Sandbox & allowed edit paths.** By default the backend agent is confined (`sandbox`)
> so it can only write inside its session worktree (plus `.git`). `allowed_edit_paths`
> lists extra directories/files it may write to (e.g. a shared data dir). Both are settable
> here, in either config file, or per run on the command line: `--no-sandbox` and
> `--allowed-edit-paths <path>[:<path>…]` (`:`-separated like `PATH`; a CLI flag wins over
> config). On macOS the carve-out covers not-yet-created paths; under Linux bubblewrap a
> path under the read-only base must already exist to become writable.

---

## 13. Backtrace (`--backtrace`) — reconstruct a history you didn't track

Backtrace is **read-only reconstruction from local transcripts** — it works with no prior aGiTrack
use and even in a directory that was never a git repo. It answers "show me (and optionally commit)
what my past coding-agent sessions did here."

```mermaid
flowchart TD
  bt["agitrack --backtrace [text|html|stop|status|commit]"] --> disc[["Discover every Claude/OpenCode session whose recorded cwd is this directory (or a subdirectory)"]]
  disc --> exp[["Export each session; recover every turn's file edits from the tool calls (Edit/Write/MultiEdit)"]]
  exp --> map[["Map each turn → a virtual commit: model, tokens, timings, per-file diff, and the user↔agent trace (final response only, exactly as a real aGiTrack commit)"]]
  map --> mode{mode}
  mode -->|text| txt[["Print a one-shot report"]]
  mode -->|html / bare| serve[["Serve the same dashboard as a background daemon (frozen 'BACKTRACE — not live' banner); dies with the terminal or via --backtrace stop"]]
  mode -->|commit| commit
  subgraph commit["--backtrace commit --branch NEW"]
    g1{git repo?} -->|no| gi[["Instruct: git init + commit, then re-run"]]
    g1 -->|yes| g2{clean tree?}
    g2 -->|no| gd[["Instruct: commit or .gitignore pending files until git status is clean"]]
    g2 -->|yes| g3{branch name given & unused?}
    g3 -->|no| gb[["Instruct: pass --branch <new-branch>"]]
    g3 -->|yes| cls[["Classify each existing commit AI-vs-user by file overlap with the turns"]]
    cls --> warn[["Warn: this REWRITES history (new hashes, not a fast-forward); confirm (unless --yes)"]]
    warn --> replay[["Replay every commit onto NEW branch with git commit-tree — same tree/author/date/parents; append trace+metadata to AI commits, keep user commits verbatim (progress bar)"]]
    replay --> sw[["Switch to NEW branch; print review + force-replace instructions"]]
  end
```

- **View** (`text` / `html` / bare): nothing is written and nothing is uploaded. The served view
  is built once and cached (re-exporting per poll would be far too slow). Committer chrome and the
  commit hash are hidden — a reconstructed turn has no committer and no real commit — and only the
  agent's **final response** is shown, matching what a real aGiTrack commit records.
- **`stop` / `status`**: manage the background daemon (per-directory handshake in a temp dir, so it
  works in non-git folders and never collides with the `-d` dashboard daemon).
- **`commit`**: the only writing path. It is deliberately gated — git repo required, clean tree
  required, a new branch required — and it never touches the current branch. AI-vs-user attribution
  is by file overlap: a commit whose changed files an agent turn edited is AI (annotated); a commit
  no turn explains is a user commit (kept verbatim). Because it rewrites history, aGiTrack prints
  the exact `git branch -f` / `git push --force-with-lease` steps and tells the user to review the
  new branch first.

Implementation: `agitrack/metrics/backtrace.py` (view + daemon), `agitrack/metrics/backtrace_commit.py`
(the `commit` replay), `agitrack/metrics/files.py` (file browser), `agitrack/transcripts/` (edit
recovery).

---

### Cross-references

- Prose spec: [`AGENTS.md`](../AGENTS.md) — Staging Behavior, Concurrent Sessions, Session Sharing, Self-Update, Concurrency and Locking.
- User-facing docs: [`README.md`](../README.md) — including [Sharing sessions](../README.md#sharing-sessions).
- Sandbox / confinement: `agitrack/proxy/sandbox.py`; per-turn commit and copy logic: `agitrack/proxy/runner.py`; sharing store: `agitrack/sessions/store.py`.
