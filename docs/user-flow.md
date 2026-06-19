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
  loop -->|Ctrl-G then exit| ex["Exit confirmation"]
  sig --> done(["aGiTrack exits"])
  ex --> done

  click launch "#2-startup-and-launch-gating"
  click pre "#4-before-forwarding-a-prompt-base-to-worktree"
  click turn "#5-the-agent-turn-auto-commit-and-integration"
  click copyback "#6-after-the-turn-copy-worktree-only-files-to-base"
  click menu "#7-ctrl-g-command-menu"
  click cancel "#5-the-agent-turn-auto-commit-and-integration"
  click upd "#9-self-update-flow"
  click ex "#10-exit-and-terminal-close"
```

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
  lock -->|Held by another aGiTrack process| ro[["Run READ-ONLY: render TUI, make no commits, show 'another process manages this repo' banner"]]
  ro --> spawn
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

  click updoffer "#9-self-update-flow"
  click spawn "#3-worktrees-vs-no-worktree"
```

---

## 3. Worktrees vs no-worktree

Where a session physically runs (`--no-worktree` turns worktrees off) — this decides whether the base-to-worktree and
worktree-to-base flows below apply at all.

```mermaid
flowchart TD
  s(["Start or switch a session"]) --> mode{"use_worktrees? Default true; off via --no-worktree or config"}
  mode -->|Worktrees on| wt["Open/create a worktree at .agitrack/worktrees/&lt;name&gt;; agent edits there, isolated"]
  wt --> wtnote[["Base repo is the user's tree; worktree is the agent's sandbox. Both base-to-worktree and worktree-to-base flows apply"]]
  mode -->|No-worktree| base["Session runs on the BASE tree, worktree=None, commits to the checked-out branch"]
  base --> warn[/"One-time caveat: parallel no-worktree sessions share the dir and may conflict"/]
  warn --> basenote[["No isolation: the per-turn commit captures whatever is in the tree. Copy-back and base-sync flows are no-ops"]]

  base --> resume{"Resuming a session first run in a worktree?"}
  resume -->|Yes| retarget[["Retarget the transcript's recorded cwd to the base dir so it doesn't reopen the gone worktree path"]]
```

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

  click go "#5-the-agent-turn-auto-commit-and-integration"
```

> The explicit base commit paths (this pre-prompt offer and the `git-user-commit`
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
  parse --> changed{"Code changed AND staged changes exist?"}
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
  click copy "#6-after-the-turn-copy-worktree-only-files-to-base"
```

---

## 6. After the turn: copy worktree-only files to base

Only for a worktree session. Catches files the agent left UNCOMMITTED or that are
git-ignored — they integrate into nothing, so the user working in the base dir would
never see them (`_offer_copy_unstaged_to_base`). Runs whether or not the turn committed.

```mermaid
flowchart TD
  start(["Turn finished, worktree session"]) --> gather[["List worktree files that won't merge: uncommitted or git-ignored. Skip .agitrack/ and names starting with _ or ."]]
  gather --> fresh{"Any with NEW content since last offered? Fingerprint gate"}
  fresh -->|No| done(["Nothing to do"])
  fresh -->|Yes| offer[/"N file(s) won't be merged. Copy them into the base repo dir? • No, leave them in the worktree • Yes, copy to the base repo"/]

  offer -->|No, or cancel| remain[/"Notice: the files remain in the worktree, with the worktree path; they're deleted when aGiTrack exits or the session integrates"/]
  remain --> done

  offer -->|Yes, copy| conflictq{"Any would overwrite an existing base file?"}
  conflictq -->|No conflicts| copyall[["Copy every file into the base dir"]]
  conflictq -->|Yes| ow[/"N already exist in the base repo. Overwrite them? • No, keep the base versions • Yes, overwrite all • Let me confirm each one"/]

  ow -->|No, keep the base versions| skipc[["Skip the conflicting files; still copy the non-conflicting new ones"]]
  ow -->|Yes, overwrite all| copyall
  ow -->|Let me confirm each one| each[/"Per conflicting file: 'file' already exists. Overwrite it? • No, keep the base version • Yes, overwrite"/]
  each -->|No, keep| skip1[["Keep the base copy"]]
  each -->|Yes, overwrite| ov1[["Overwrite that file"]]
  skip1 --> tally
  ov1 --> tally
  skipc --> tally
  copyall --> tally[["Report copied count; anything not copied gets the 'files remain' notice"]]
  tally --> done
```

---

## 7. Ctrl-G command menu

`Ctrl-G` opens the command palette (type a prefix, Up/Down to select, Tab to complete,
Enter to run). Commands, in palette order:

```mermaid
flowchart TD
  g(["Ctrl-G"]) --> pal[/"Command palette"/]
  pal --> sessions["sessions"]
  pal --> backend["agent-backend"]
  pal --> summ["summarizer"]
  pal --> gunstaged["git-unstaged"]
  pal --> gcommit["git-user-commit"]
  pal --> dash["dashboard"]
  pal --> update["update"]
  pal --> exit["exit"]

  sessions --> smenu[/"Live sessions menu: each shows running or idle"/]
  smenu -->|Switch to one| sswitch[["Show that session, relaunch TUI, re-baseline so history isn't re-committed"]]
  smenu -->|sessions new| snew["Start a new session: own worktree, or shared base dir under --no-worktree"]
  smenu -->|Stop one| sstop[["Stop the session"]]
  smenu -->|Merge reviewed changes| smerge[/"Pick base branch, then integrate that session's changes"/]
  snew --> mode3["See Worktrees vs no-worktree"]

  backend -->|No arg, picker| bpick[/"Pick claude or opencode"/]
  bpick --> bswitch[["Save current backend's session, relaunch target backend, restore its last session, update global default"]]

  summ --> spick[/"Pick the summarizer model, current shown"/]

  gunstaged --> gu[["Show intentionally-unstaged files in the status bar"]]

  gcommit --> gcflow["git-user-commit flow"]

  dash --> dserve[["Serve the read-only metrics dashboard; open the local browser only if it lands on this machine, else print the URL"]]

  update --> uflow["aGiTrack self-update flow"]

  exit --> exflow["Exit confirmation"]

  click snew "#3-worktrees-vs-no-worktree"
  click gcflow "#8-git-user-commit"
  click uflow "#9-self-update-flow"
  click exflow "#10-exit-and-terminal-close"
  click mode3 "#3-worktrees-vs-no-worktree"
```

---

## 8. `git-user-commit`

Creates a user commit (no `<aGiTrack>` tag) from the user's own edits, from whichever
tree holds them — the base repo and/or this session's worktree.

```mermaid
flowchart TD
  start(["git-user-commit"]) --> where{"Which tree is dirty?"}
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
  apply -->|Package| pkg[["Upgrade via the running interpreter's pip; PEP 668 defers to brew or prints every manual route"]]
  src --> ok{"Succeeded?"}
  pkg --> ok
  ok -->|Yes| reexec[["Re-exec python -m agitrack so the new code loads"]]
  ok -->|No| failnotice[["Never crashes: record the target version, show a single manual-update reminder next startup; keep running current version"]]
  reexec --> done2(["Updated"])
  failnotice --> done2
```

> Distinct from the **backend agent** (Claude / OpenCode) updating itself: that runs
> inside the agent TUI, and the sandbox is built to keep the agent's own install dirs
> writable so it always works. See `agitrack/proxy/sandbox.py`.

---

## 10. Exit and terminal close

```mermaid
flowchart TD
  how{"How is aGiTrack ending?"}
  how -->|Ctrl-G then exit, managing instance| conf[/"Exit aGiTrack? • No, keep working • Yes, exit"/]
  conf -->|No| stay(["Keep working"])
  conf -->|Yes| busy{"Sessions still running, turns in flight?"}
  busy -->|Yes| term[/"Terminate them and exit? • No, keep working • Yes, terminate them and exit"/]
  busy -->|No| fin
  term -->|No| stay
  term -->|Yes| fin

  how -->|Terminal or window closed, SIGHUP/SIGTERM| sig[["_handle_exit_signal: best-effort finalize pending work, render suppressed"]]
  how -->|Read-only instance| roexit[["Torn down with the window; no finalize needed"]]

  fin[["Finalize: commit a just-completed turn, integrate committed work, stop the dashboard"]]
  sig --> fin
  fin --> bye(["aGiTrack exits"])
  roexit --> bye
```

---

### Cross-references

- Prose spec: [`AGENTS.md`](../AGENTS.md) — Staging Behavior, Concurrent Sessions, Self-Update, Concurrency and Locking.
- User-facing docs: [`README.md`](../README.md).
- Sandbox / confinement: `agitrack/proxy/sandbox.py`; per-turn commit and copy logic: `agitrack/proxy/runner.py`.
