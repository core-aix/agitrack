# Contributing to aGiTrack

Thanks for being here. aGiTrack wraps coding-agent CLIs with Git so that every agent turn
becomes a traceable commit, and it is itself built that way: most of this repository's history
was written by an agent running under aGiTrack. That shapes how contributions work, so this
document is worth ten minutes before your first change.

## Getting set up

```bash
git clone https://github.com/core-aix/agitrack
cd agitrack
uv sync --group dev      # everything, including the dev tools
make install-hooks       # optional but recommended (see below)
```

Python 3.10 or newer. [uv](https://docs.astral.sh/uv/) manages the environment; every command
below assumes it.

`make install-hooks` installs two git hooks:

- **commit**: `ruff` lint + format and basic file hygiene, so committing stays fast.
- **push**: the full CI-equivalent gate, so a push that would break CI fails locally first.

## The definition of done

One command, and it must exit 0:

```bash
make check        # or: ./scripts/check.sh
```

It runs, in CI's order: `ruff check`, `ruff format --check`, `mypy` against the committed
baseline (it fails only on NEW type errors), then the full test suite with coverage. Nothing is
finished until this passes. CI (`.github/workflows/ci.yml`) runs the same gate on Linux, macOS
and Windows, plus a native-Windows subset and an MSI build-and-install.

If you deliberately burn down type debt, regenerate the baseline:

```bash
uv run mypy | uv run mypy-baseline sync
```

## What a good change looks like

**All backends, really.** Every feature must work identically on Claude Code, Codex and OpenCode, and
"identically" means you ran it on both, not that the mocked unit tests pass. Their transcript
shapes, session ids, resume semantics and summary behaviour all differ, and the differences are
where the bugs are.

**Every platform.** macOS, Linux and native Windows are all supported. Windows is not a POSIX
variant with different slashes: `SO_REUSEADDR` means the opposite thing there, `os.replace` can
be refused while another handle is open, `os.kill(pid, 0)` does not exist, and a path that
starts with `C:\` answers "not absolute" to every POSIX-shaped test. Ask about path SHAPE
through `agitrack/paths.py` rather than with `startswith("/")`, and prefer a test that drives
the other platform's shapes on YOUR machine over one that only runs there.

**Tests that would have caught it.** A fix comes with a test that fails without it. Prefer a
test that pins the BEHAVIOUR a user would notice over one that pins an implementation detail;
name it after what must be true (`test_a_range_with_no_commits_in_it_is_refused_before_any_agent_call`),
not after the function it calls. Tests read top to bottom as documentation of the feature, and
the existing files are laid out that way on purpose.

**Comments that say WHY.** This codebase's comments carry the reason a thing is the way it is,
especially when the obvious implementation was tried and failed. If you fix something subtle,
leave behind what bit you: the next person (or agent) has no other way to know.

**No em-dashes** in anything aGiTrack ships or prints: the README, the docs, the website, the
CLI's own output. Use a colon, a comma, parentheses, or a full stop.

**Keep `AGENTS.md` current.** It is the working memory of this project: the invariants, the
traps, and why each exists. A change that alters behaviour described there updates it in the
same commit. Likewise `docs/user-flow.md` when the interactive flow changes.

## Layout

| Path | What lives there |
| --- | --- |
| `agitrack/proxy/` | The interactive TUI: the reactor, the terminal/ConPTY layer, the commit pipeline |
| `agitrack/backends/` | Claude, Codex and OpenCode, behind one interface |
| `agitrack/transcripts/` | Reading each backend's session files into turns and file edits |
| `agitrack/commits/` | Commit messages, the interaction trace, manual/latent commit modes |
| `agitrack/metrics/` | The dashboard, the learn page, the storyline, the backtrace, the static export |
| `agitrack/git/` | The repo wrapper, worktrees, hooks, the single-writer lock |
| `tests/` | One file per area; `scripts/check.sh` runs them all |
| `docs/` | The public website (GitHub Pages) and the user-flow diagrams |

The three web pages (dashboard, learn, storyline) are self-contained HTML documents that share
one design layer, `agitrack/metrics/ui.py`. Anything visual that belongs to more than one page
goes THERE, and `tests/test_web_ui.py` is the contract that keeps them from drifting apart.

## Proposing a change

1. Open an issue first for anything substantial. A design that lands in an issue costs
   everyone less than one that lands in a review.
2. Branch off `dev`. `main` is the release branch: every merge into it cuts a release
   automatically (version bump, PyPI, the VS Code Marketplace, a GitHub Release).
3. Keep the change and its tests and its docs in one coherent set of commits.
4. Run `make check`.
5. Open the PR against `dev` and say what you verified by hand, on which backends and which
   platforms. "Tested on Claude, Codex and OpenCode, macOS" is a useful sentence; its absence is a
   question the reviewer has to ask.

## Reporting a bug

The most useful report says what you expected, what happened, and how to get back there. Please
include your OS, your aGiTrack version (`agitrack --version`), which backend you were on, and,
if you can share it, the relevant part of `.agitrack/background.log` or a debug log. A crash in
the TUI writes a report under `.agitrack/`; attaching it saves a lot of guessing.

If you think you have found a security issue, please do not open a public issue. Report it
privately through GitHub's
[security advisories](https://github.com/core-aix/agitrack/security/advisories/new).
[SECURITY.md](SECURITY.md) says what counts as one, what is deliberate, and what to include.

## A note on agent-written contributions

Contributions written with a coding agent are welcome: that is what this tool is for. The bar
is exactly the same: you understand the change, you ran it, and you can explain why it is right.
Please do not send a diff you have not read.

## Licence

By contributing you agree that your work is licensed under the
[Apache 2.0 licence](LICENSE) that covers this project.
