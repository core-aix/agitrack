"""Drive the REAL aGiTrack TUI over a PTY and assert what it actually did to git.

Why this exists
---------------
The mocked suite proves aGiTrack's logic. It cannot prove that a MODE works end to end with
a real backend CLI in front of a real terminal — that the startup questions get answered, the
typed prompt reaches the agent, the turn completes, and a commit with the right metadata lands
on the right ref. Every attempt to check that by hand died the same way: the harness typed into
a modal it could not see, believed the turn had run, and "verified" nothing.

The rule that makes it reliable
-------------------------------
**Never conclude a turn ran by reading the screen. Poll git.** aGiTrack commits every turn, so
a turn is done when a new commit exists on the ref being watched — and if none appears, the
scenario fails loudly instead of passing on a repaint that looked plausible. Screen text is
used only to answer questions and to locate menus.

Everything else here is a scar
------------------------------
* ``TIOCSWINSZ`` on the SLAVE fd before spawning, or the child renders a 0-row screen.
* Strip every ``CLAUDE*`` env var, or a nested Claude Code misbehaves.
* Strip CSI **and** OSC **and** charset (``ESC ( B``) escapes when matching.
* A startup question is only live in the bytes that arrived since the last answer. Matching a
  scrollback window answers the same question twice — the second "n" landed on the secrets
  warning, aGiTrack exited, and every later keystroke was ``EIO`` on a closed pty.
* The reply to a keypress usually CONTAINS the next question, so the "since the last answer"
  buffer must become that reply rather than being cleared.
* The agent screen can be up while a modal still covers the composer (worktree mode asks about
  copying the environment right after the status bar paints). Wait for a modal-free moment.
* The name popup does not handle Ctrl-U — clear its suggestion with backspaces.
* Codex asks a per-directory trust question; Claude asks a per-folder trust question. Both are
  pre-answered in their config files here.
* A backend can stop mid-turn to ask permission for a write; accept it, or the turn "hangs".
* Quit with SIGTERM to the process group.

Usage
-----
    python devtools/live_tui.py --workdir /tmp/agitrack-live               # every scenario
    python devtools/live_tui.py --workdir /tmp/agitrack-live no_worktree manual
    python devtools/live_tui.py --workdir /tmp/agitrack-live --backend codex --model gpt-5.4-mini

It runs REAL agent turns and costs real tokens; it is not part of the test suite and CI never
runs it. Default model is a cheap one on purpose.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\x1b\([AB0]")

AGITRACK = os.environ.get("AGITRACK_BIN", "agitrack")
WORKDIR = Path(os.environ.get("AGITRACK_LIVE_WORKDIR", "/tmp/agitrack-live"))
BACKEND = "codex"
MODEL: str | None = "gpt-5.4-mini"

RESULTS: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)


def clean(raw: bytes) -> str:
    text = ANSI.sub(b"", raw).decode("utf-8", "replace").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip())


def git(repo: Path, *args: str, check_rc: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), text=True, capture_output=True)
    if check_rc and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {repo}: {proc.stderr}")
    return proc.stdout.strip()


def commit_count(repo: Path, ref: str = "HEAD") -> int:
    out = subprocess.run(["git", "rev-list", "--count", ref], cwd=str(repo), text=True, capture_output=True)
    return int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else 0


def head_message(repo: Path, ref: str = "HEAD") -> str:
    return git(repo, "log", "-1", "--format=%B", ref)


def state_of(path: Path) -> dict:
    return json.loads((Path(path) / ".agitrack" / "state.json").read_text(encoding="utf-8"))


def manual_refs(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/agitrack/manual"],
        cwd=str(repo),
        text=True,
        capture_output=True,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


# --- trusting a throwaway directory -------------------------------------------------------
# Both CLIs refuse to work in an unseen directory until the user answers a trust question. It
# is not part of what we are testing, and it blocks every keystroke until answered.


def trust_codex(repo: Path) -> None:
    cfg = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    entry = f'[projects."{repo}"]'
    if cfg.exists() and entry in cfg.read_text(encoding="utf-8"):
        return
    cfg.parent.mkdir(parents=True, exist_ok=True)
    with cfg.open("a", encoding="utf-8") as handle:
        handle.write(f'\n{entry}\ntrust_level = "trusted"\n')


def trust_claude(repo: Path) -> None:
    path = Path.home() / ".claude.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return
    entry = data.setdefault("projects", {}).setdefault(str(repo), {})
    if entry.get("hasTrustDialogAccepted") and entry.get("hasCompletedProjectOnboarding"):
        return
    entry["hasTrustDialogAccepted"] = True
    entry["hasCompletedProjectOnboarding"] = True
    entry.setdefault("allowedTools", [])
    entry.setdefault("history", [])
    tmp = path.with_suffix(".live-harness.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def make_repo(name: str, files: dict[str, str] | None = None) -> Path:
    repo = WORKDIR / name
    subprocess.run(["rm", "-rf", str(repo)], check=True)
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "live-harness@example.com")
    git(repo, "config", "user.name", "Live Harness")
    git(repo, "config", "commit.gpgsign", "false")
    for rel, body in (files or {"README.md": "seed\n"}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "initial")
    trust_codex(repo)
    trust_claude(repo)
    return repo


def config_home() -> Path:
    """An aGiTrack config dir of our own, so a live run never edits the developer's settings."""
    home = WORKDIR / "agit-home"
    home.mkdir(parents=True, exist_ok=True)
    config = home / "config.json"
    if not config.exists():
        config.write_text(
            json.dumps({"default_backend": BACKEND, "check_for_updates": False, "use_worktrees": True}),
            encoding="utf-8",
        )
    return home


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env.update({"TERM": "xterm-256color", "COLUMNS": "150", "LINES": "45"})
    env["AGITRACK_CONFIG_DIR"] = str(config_home())
    env.update(extra or {})
    return env


def cli(repo: Path, *args: str, timeout: float = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [AGITRACK, *args], cwd=str(repo), env=child_env(), text=True, capture_output=True, timeout=timeout
    )


class Tui:
    """A real aGiTrack TUI (or a bare backend CLI) driven over a PTY."""

    _ENTER_PROMPTS = (
        "to acknowledge and continue",  # the secrets warning — on EVERY launch
        "Press Enter to continue",
        "(press Enter",
    )
    _MODAL_PROMPTS = ("Up/Down selects. Enter confirms.", "Copy the full environment")
    # Accepting this one replaces the run with a dashboard. It only appears once the directory
    # HAS agent sessions, i.e. on the second run against a reused path — a harness that always
    # starts from a fresh directory never meets it.
    _DECLINE_PROMPTS = ("Open the backtrace view now?",)
    # Markers that the BACKEND's OWN composer is drawn — deliberately none of aGiTrack's own
    # chrome. "aGiTrack is starting..." prints before the child exists, and the status bar is
    # painted as soon as the child is spawned, well before it can accept a keystroke: typing
    # then leaves the prompt in a composer that is repainted from scratch a moment later, and
    # the text is lost with no error anywhere — the turn simply never starts.
    READY_MARKERS = (
        "OpenAI Codex (v",  # codex splash
        'Try "',  # codex / claude composer placeholder
        "for shortcuts",  # claude composer footer
        "to interrupt",
        "/help for help",  # opencode
    )
    # A backend's own "may I write this file?" dialog; Enter takes the permissive default.
    PERMISSION_PROMPTS = ("Do you want to", "1. Yes", "Allow this", "y/n")

    def __init__(self, repo: Path, command: list[str], *, log: Path | None = None):
        self.repo = Path(repo)
        self.log = log or WORKDIR / f"{self.repo.name}.pty.log"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self._logf = self.log.open("wb")
        self.master, slave = pty.openpty()
        # BEFORE the spawn, and on the SLAVE fd: sizing the master (or the child) instead
        # leaves the child rendering into a 0-row screen with nothing visible ever.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 150, 0, 0))
        self.proc = subprocess.Popen(
            command,
            cwd=str(self.repo),
            env=child_env(),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            preexec_fn=os.setsid,
        )
        os.close(slave)
        self.buffer = ""

    # --- io ---------------------------------------------------------------
    def read(self, quiet: float = 1.5, cap: float = 40.0) -> str:
        raw, deadline, hard = b"", time.time() + quiet, time.time() + cap
        while time.time() < deadline and time.time() < hard:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                break
            if not chunk:
                break
            raw += chunk
            deadline = time.time() + quiet
        if raw:
            self._logf.write(raw)
            self._logf.flush()
        text = clean(raw)
        self.buffer += text
        return text

    def press(self, data: bytes, label: str = "", quiet: float = 1.5, cap: float = 40.0) -> str:
        if self.proc.poll() is not None:
            raise AssertionError(f"the child exited (rc={self.proc.returncode}); tail:\n{self.buffer[-2000:]}")
        try:
            os.write(self.master, data)
        except OSError as error:  # say WHY, not "Errno 5"
            raise AssertionError(f"the pty is gone ({error}); tail:\n{self.buffer[-2000:]}") from error
        text = self.read(quiet, cap)
        if label:
            print(f"--- {label} ---\n{text[-1200:] or '(no repaint)'}", flush=True)
        return text

    def kill(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(self.proc.pid), sig)
                self.proc.wait(30 if sig == signal.SIGTERM else 10)
                break
            except Exception:
                continue
        try:
            self._logf.close()
        except OSError:
            pass

    # --- startup ----------------------------------------------------------
    @classmethod
    def agent_ready(cls, text: str) -> bool:
        return any(marker in text[-8000:] for marker in cls.READY_MARKERS)

    def boot(self, session_name: str | None = None, timeout: float = 120.0) -> str:
        seen, pending = "", ""
        end = time.time() + timeout
        ready_since: float | None = None

        def answer(keys: bytes, label: str, quiet: float) -> None:
            # `pending` becomes the REPLY, not "": the reply to a keypress usually contains the
            # next question (declining the backtrace offer paints the secrets warning at once),
            # and clearing it dropped that question where nothing would look again.
            nonlocal pending, ready_since, seen
            ready_since = None
            reply = self.press(keys, label, quiet)
            seen += reply
            pending = reply

        while time.time() < end:
            fresh = self.read(0.8, cap=8.0)
            seen += fresh
            pending += fresh
            window = pending[-6000:]
            if any(marker in window for marker in self._DECLINE_PROMPTS):
                answer(b"n\r", "decline startup offer", 2.5)
            elif any(marker in window for marker in self._MODAL_PROMPTS):
                answer(b"\r", "accept modal default", 2.5)
            elif "Name this session" in window:
                keys = (b"\x7f" * 60 + session_name.encode() + b"\r") if session_name else b"\r"
                answer(keys, "name the session", 3.0)
            elif any(marker in window for marker in self._ENTER_PROMPTS):
                answer(b"\r", "acknowledge notice", 2.0)
            elif self.agent_ready(seen):
                # Settle: a modal can still be raised a beat after the status bar appears, and
                # typing into it silently loses the prompt.
                if ready_since is None:
                    ready_since = time.time()
                elif time.time() - ready_since >= 4.0:
                    return seen
            elif self.proc.poll() is not None:
                raise AssertionError(f"aGiTrack exited during boot (rc={self.proc.returncode}); {seen[-3000:]}")
        raise AssertionError(f"boot timed out; tail:\n{seen[-3000:]}")

    # --- turns ------------------------------------------------------------
    def type_prompt(self, text: str, attempts: int = 8) -> None:
        probe = text[:18]
        for attempt in range(attempts):
            screen = self.press(text.encode(), f"type prompt (attempt {attempt + 1})", 2.0)
            if probe in screen or probe in self.buffer[-4000:]:
                return
            tail = self.buffer[-4000:]
            if any(marker in tail for marker in self._MODAL_PROMPTS) or "Name this session" in tail:
                self.press(b"\r", "dismiss the modal blocking the composer", 2.5)
                continue
            self.press(b"\x15", "", 0.8)
            self.press(b"\x7f" * len(text), "", 0.8)
        raise AssertionError(f"the prompt never echoed; tail:\n{self.buffer[-2000:]}")

    def run_turn(self, text: str, *, timeout: float = 300.0, ref: str = "HEAD", repo: Path | None = None) -> int:
        """Submit *text* and wait until aGiTrack records a NEW commit on *ref*. Returns how many.

        Asserting on git rather than on the screen is the whole point: a repaint can look like
        a finished turn when nothing ran at all.
        """
        watch = Path(repo or self.repo)
        before = commit_count(watch, ref)
        self.type_prompt(text)
        self.press(b"\r", "submit", 3.0)
        end = time.time() + timeout
        while time.time() < end:
            fresh = self.read(0.5, cap=3.0)
            if any(marker in fresh for marker in self.PERMISSION_PROMPTS):
                self.press(b"\r", "approve the backend's permission request", 2.0)
                continue
            now = commit_count(watch, ref)
            if now > before:
                self.read(2.0, cap=6.0)  # let the TUI settle after the commit
                return now - before
            if self.proc.poll() is not None:
                raise AssertionError(f"aGiTrack exited during the turn (rc={self.proc.returncode})")
        raise AssertionError(f"no new commit on {ref} in {watch} within {timeout}s; tail:\n{self.buffer[-3000:]}")

    # --- menus ------------------------------------------------------------
    def palette(self, command: str, quiet: float = 4.0) -> str:
        self.press(b"\x07", "Ctrl-G", 2.0)
        screen = self.press(command.encode() + b"\r", f"palette: {command}", quiet)
        assert "Unknown aGiTrack command" not in screen, screen[-400:]
        return screen

    def switch_backend(self, backend: str, order: list[str], quiet: float = 8.0) -> str:
        """The picker is an ARROW list that always opens on the FIRST entry, whichever backend
        is running — so move from index 0, never from the current backend's position."""
        screen = self.palette("agent-backend")
        for _ in range(order.index(backend)):
            screen = self.press(b"\x1b[B", f"move toward {backend}", 1.0)
        assert f"> {backend}" in screen, f"the picker marker is not on {backend}:\n{screen[-800:]}"
        return self.press(b"\r", f"choose {backend}", quiet)

    def answer_name(self, name: str, quiet: float = 8.0) -> str:
        # The popup does NOT handle Ctrl-U; without the backspaces the typed name is APPENDED
        # to the pre-filled suggestion ("jubilee" + "beta").
        return self.press(b"\x7f" * 60 + name.encode() + b"\r", f"name it {name!r}", quiet)


def agitrack_tui(repo: Path, args: list[str], name: str) -> Tui:
    handle = Tui(repo, [AGITRACK, *args])
    handle.boot(name)
    return handle


def bare_backend(repo: Path) -> Tui:
    """The backend CLI with NO aGiTrack in front of it — what background (-b) mode watches."""
    command = [BACKEND] + (["--model", MODEL] if MODEL else [])
    handle = Tui(repo, command, log=WORKDIR / f"{repo.name}.backend.log")
    end = time.time() + 90
    seen = ""
    while time.time() < end:
        seen += handle.read(0.8, cap=8.0)
        if handle.agent_ready(seen):
            break
    return handle


def backend_args() -> list[str]:
    return ["--backend", BACKEND] + (["--model", MODEL] if MODEL else [])


# ------------------------------------------------------------------ scenarios


def s_no_worktree() -> None:
    """--no-worktree: the turn's latent commits fold into a real commit on the current branch."""
    repo = make_repo("live_nw")
    handle = agitrack_tui(repo, ["--no-worktree", *backend_args()], "nw-session")
    try:
        added = handle.run_turn("Create a file named alpha.txt whose only content is the word alpha. Then stop.")
        check("no-worktree: a real commit lands on the branch", added == 1, f"(+{added})")
        check("no-worktree: the commit carries the backend's metadata", f"backend: {BACKEND}" in head_message(repo))
        check("no-worktree: the file is on the branch", (repo / "alpha.txt").exists())
        check("no-worktree: the session name is recorded", "nw-session" in state_of(repo).get("session_names", {}).values())
    finally:
        handle.kill()


def s_worktree() -> None:
    """Default worktree mode: the turn commits in the worktree and integrates into the base."""
    repo = make_repo("live_wt")
    handle = agitrack_tui(repo, backend_args(), "wt-session")
    try:
        added = handle.run_turn("Create a file named beta.txt whose only content is the word beta. Then stop.", timeout=360)
        check("worktree: the turn reaches the BASE branch", added >= 1, f"(+{added})")
        check("worktree: the file is integrated into the base repo", (repo / "beta.txt").exists())
        check("worktree: the base commit carries the backend's metadata", f"backend: {BACKEND}" in head_message(repo))
    finally:
        handle.kill()


def s_delay_merge() -> None:
    """--delay-merge: the turn commits on the session branch and must NOT reach the base."""
    repo = make_repo("live_dm")
    handle = agitrack_tui(repo, [*backend_args(), "--delay-merge"], "dm-session")
    try:
        added = 0
        try:
            added = handle.run_turn("Create a file named gamma.txt whose only content is gamma. Then stop.", timeout=90)
        except AssertionError:
            pass  # expected: the base must not advance
        check("delay-merge: the base branch does NOT advance", added == 0)
        branches = git(repo, "branch", "--list", "agitrack/*", "--format=%(refname:short)").splitlines()
        moved = [b for b in branches if commit_count(repo, b) > 1]
        check("delay-merge: the turn IS committed on the session branch", bool(moved), f"{branches}")
        if moved:
            check("delay-merge: that commit carries the backend's metadata", f"backend: {BACKEND}" in head_message(repo, moved[0]))
    finally:
        handle.kill()


def s_manual_external() -> None:
    """-m: the turn is a latent commit on refs/agitrack/manual/<id>, folded by an EXTERNAL commit."""
    repo = make_repo("live_me")
    handle = agitrack_tui(repo, ["-m", *backend_args()], "manual-ext")
    try:
        before = commit_count(repo)
        try:
            handle.run_turn("Create a file named delta.txt whose only content is the word delta. Then stop.", timeout=90)
        except AssertionError:
            pass  # expected: manual mode must not advance the branch
        check("manual: the branch does NOT advance on an agent turn", commit_count(repo) == before)
        refs, end = [], time.time() + 120
        while time.time() < end and not refs:
            handle.read(0.5, cap=3.0)
            refs = manual_refs(repo)
        check("manual: a latent commit exists on refs/agitrack/manual/<id>", bool(refs), f"{refs}")
        check("manual: the agent's edit is visible in the working tree", (repo / "delta.txt").exists())
        if refs:
            check("manual: the latent commit carries the backend's metadata", f"backend: {BACKEND}" in head_message(repo, refs[0]))
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "user commit folding the agent turn")
        folded = head_message(repo)
        check("manual: the external git commit advances the branch", commit_count(repo) == before + 1)
        check("manual: the external commit folded the agent turn", f"backend: {BACKEND}" in folded)
    finally:
        handle.kill()


def s_manual_menu() -> None:
    """-m: the same fold, triggered from aGiTrack's own `git-commit` menu entry."""
    repo = make_repo("live_mm")
    handle = agitrack_tui(repo, ["-m", *backend_args()], "manual-menu")
    try:
        before = commit_count(repo)
        try:
            handle.run_turn("Create a file named epsilon.txt whose only content is epsilon. Then stop.", timeout=90)
        except AssertionError:
            pass
        refs, end = [], time.time() + 120
        while time.time() < end and not refs:
            handle.read(0.5, cap=3.0)
            refs = manual_refs(repo)
        check("manual-menu: a latent commit is recorded", bool(refs), f"{refs}")
        screen = handle.palette("git-commit", quiet=8.0)  # NOT "commit" — that is an unknown command
        # Two steps, and they need different keys. Enter on the untracked-files list picks the
        # highlighted "Stage all N file(s)"; the message field then REJECTS an empty submit
        # ("Commit message is required"), so pressing Enter again just re-asks forever — which is
        # exactly how this scenario failed the first time it was run for real.
        if "Untracked Files" in screen:
            screen = handle.press(b"\r", "stage the untracked file", 5.0)
        if "Commit message" in screen:
            screen = handle.press(b"folded by the aGiTrack menu\r", "type the commit message", 8.0)
        for _ in range(6):
            if commit_count(repo) > before:
                break
            handle.press(b"\r", "settle any follow-up prompt", 5.0)
        check("manual-menu: the menu commit advances the branch", commit_count(repo) > before)
        if commit_count(repo) > before:
            check("manual-menu: the menu commit folded the turn", f"backend: {BACKEND}" in head_message(repo))
    finally:
        handle.kill()


def s_background() -> None:
    """-b run / status / stop, with the agent driven from its OWN CLI."""
    repo = make_repo("live_bg")
    start = cli(repo, "-b", "run", "--backend", BACKEND)
    check("-b run: the tracker starts", start.returncode == 0, start.stdout[-200:])
    status = cli(repo, "-b", "status")
    check("-b status: reports a running tracker", "running" in (status.stdout + status.stderr).lower())
    agent = None
    try:
        agent = bare_backend(repo)
        before = commit_count(repo)
        agent.type_prompt("Create a file named zeta.txt whose only content is the word zeta. Then stop.")
        agent.press(b"\r", "submit to the backend CLI", 3.0)
        end, added = time.time() + 300, 0
        while time.time() < end and not added:
            agent.read(0.5, cap=3.0)
            added = commit_count(repo) - before
        check("-b: the tracker commits a turn the user drove from the backend CLI", added >= 1, f"(+{added})")
        if added:
            check("-b: that commit carries the backend's metadata", f"backend: {BACKEND}" in head_message(repo))
    finally:
        if agent:
            agent.kill()
        stop = cli(repo, "-b", "stop")
        check("-b stop: stops the tracker", stop.returncode == 0)
        after = cli(repo, "-b", "status")
        check("-b status: reports stopped afterwards", "no agitrack background tracker" in (after.stdout + after.stderr).lower())


def s_background_manual() -> None:
    """-b -m: background tracking with manual commits — latent ref, folded by a user commit."""
    repo = make_repo("live_bm")
    start = cli(repo, "-b", "run", "-m", "--backend", BACKEND)
    check("-b -m: the tracker starts", start.returncode == 0)
    agent = None
    try:
        agent = bare_backend(repo)
        before = commit_count(repo)
        agent.type_prompt("Create a file named eta.txt whose only content is the word eta. Then stop.")
        agent.press(b"\r", "submit to the backend CLI", 3.0)
        refs, end = [], time.time() + 300
        while time.time() < end and not refs:
            agent.read(0.5, cap=3.0)
            refs = manual_refs(repo)
        check("-b -m: a latent commit is recorded on the manual ref", bool(refs), f"{refs}")
        check("-b -m: the branch does NOT advance", commit_count(repo) == before)
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "user commit folding the background turn")
        check("-b -m: the user commit folds the agent turn", f"backend: {BACKEND}" in head_message(repo))
    finally:
        if agent:
            agent.kill()
        cli(repo, "-b", "stop")


def s_no_confine() -> None:
    """--no-confine: the sandbox is off; the turn still runs and commits."""
    repo = make_repo("live_nc")
    handle = agitrack_tui(repo, ["--no-worktree", "--no-confine", *backend_args()], "nc-session")
    try:
        added = handle.run_turn("Create a file named theta.txt whose only content is theta. Then stop.")
        check("--no-confine: the turn commits", added == 1, f"(+{added})")
        check("--no-confine: the commit carries the backend's metadata", f"backend: {BACKEND}" in head_message(repo))
    finally:
        handle.kill()


def s_allowed_edit_paths() -> None:
    """--allowed-edit-paths EXTENDS the sandbox's writable set beyond the session worktree.

    It is not a restriction on the repo — an earlier version of this scenario asserted the
    opposite and "failed" on correct behaviour.
    """
    repo = make_repo("live_ae")
    outside = WORKDIR / "live_ae_outside"
    subprocess.run(["rm", "-rf", str(outside)], check=True)
    outside.mkdir(parents=True)
    handle = agitrack_tui(repo, [*backend_args(), "--allowed-edit-paths", str(outside)], "ae-session")
    try:
        target = outside / "iota.txt"
        try:
            handle.run_turn(
                f"Create the file {target} (absolute path, outside this directory) whose only content "
                "is the word iota. Then stop.",
                timeout=300,
            )
        except AssertionError:
            pass  # a write outside the worktree produces no commit; the file is the evidence
        check("--allowed-edit-paths: an allow-listed path OUTSIDE the worktree is writable", target.exists())
    finally:
        handle.kill()


def s_backend_command() -> None:
    """--backend-command: the backend is launched through a user wrapper."""
    repo = make_repo("live_bc")
    wrapper, marker = WORKDIR / "wrapper.sh", WORKDIR / "wrapper.marker"
    marker.unlink(missing_ok=True)
    wrapper.write_text(f'#!/bin/sh\necho "$@" >> "{marker}"\nexec {BACKEND} "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    handle = agitrack_tui(repo, ["--no-worktree", "--backend-command", str(wrapper), *backend_args()], "bc-session")
    try:
        added = handle.run_turn("Create a file named lambda.txt whose only content is lambda. Then stop.")
        check("--backend-command: the wrapper launched the backend", marker.exists())
        check("--backend-command: the turn commits", added == 1, f"(+{added})")
    finally:
        handle.kill()


def _switch(handle: Tui, repo: Path, target: str, *, expect_prompt: bool, name: str | None, label: str,
            expect_name: str | None = None, worktrees: bool = False) -> str:
    order = sorted({BACKEND, "claude", "codex", "opencode"})
    screen = handle.switch_backend(target, order)
    asked = f"New {target} session" in screen or "Name this session" in screen
    check(f"{label}: switching to {target} {'ASKS' if expect_prompt else 'does NOT ask'} for a name",
          asked == expect_prompt, f"asked={asked}")
    if expect_prompt and asked and name:
        screen = handle.answer_name(name)
        check(f"{label}: the typed name reaches the status bar", f"| {name} " in screen)
        # Durable BEFORE the first turn, by the mechanism each mode uses: without a worktree
        # there is no conversation id yet so the name is held as `pending_session_name`; with
        # worktrees the name IS the worktree directory, which exists from creation.
        if worktrees:
            check(f"{label}: the name is durable at once (its worktree exists)",
                  (repo / ".agitrack" / "worktrees" / name).is_dir())
        else:
            pending = state_of(repo).get("pending_session_name")
            check(f"{label}: the name is durable at once (pending_session_name)", pending == name, f"{pending!r}")
    if expect_name:
        check(f"{label}: switching back restores {expect_name!r}", f"| {expect_name} " in screen)
    return screen


def _switch_scenario(repo_name: str, args: list[str], *, worktrees: bool, first: str, second: str) -> None:
    other = "claude" if BACKEND != "claude" else "codex"
    repo = make_repo(repo_name)
    handle = agitrack_tui(repo, args, first)
    label = "wt " if worktrees else ""
    try:
        handle.run_turn("Create a file named mu.txt whose only content is the word mu. Then stop.", timeout=360)
        names = state_of(repo).get("session_names", {})
        check(f"{label}switch: the first conversation is named before the switch", first in names.values(), f"{names}")

        _switch(handle, repo, other, expect_prompt=True, name=second, label=f"{label}switch fresh", worktrees=worktrees)
        # Best effort: this turn only exists to give the new conversation content so its name
        # binds to a real id. It must not cost us the switch-BACK checks, which are the point.
        try:
            handle.run_turn("Create a file named nu.txt whose only content is the word nu. Then stop.", timeout=240)
        except AssertionError as error:
            check(f"{label}switch: the second backend ran a turn", False, str(error)[:160])
        names = state_of(repo).get("session_names", {})
        check(f"{label}switch: the ORIGINAL name survived the switch", first in names.values(), f"{names}")

        _switch(handle, repo, BACKEND, expect_prompt=False, name=None, label=f"{label}switch back",
                expect_name=first, worktrees=worktrees)
        names = state_of(repo).get("session_names", {})
        check(f"{label}switch back: every name is still recorded", first in names.values(), f"{names}")
    finally:
        handle.kill()


def s_switch_no_worktree() -> None:
    """Backend switching without a worktree: a switch that starts a FRESH conversation asks for
    a name; a switch BACK to a stored conversation must not, and must restore its name.

    This is also the live proof of the state-clobber fix: without a worktree the session state
    and the repo-root state are ONE file, and the name written during the switch used to be
    erased by the session's very next property setter.
    """
    _switch_scenario("live_sw", ["--no-worktree", *backend_args()], worktrees=False,
                     first="codex-one", second="claude-two")


def s_switch_worktree() -> None:
    """The same two switch paths in the default worktree mode."""
    _switch_scenario("live_sww", backend_args(), worktrees=True, first="wt-first", second="wt-second")


def scripted_turn(repo: Path, backend: str, prompt: str, model: str | None = None) -> str | None:
    """One real turn through the NON-INTERACTIVE shell (`--json --prompt`): a real backend call
    and a real commit, with no TUI typing at all. Returns the backend conversation id."""
    args = [AGITRACK, "--no-worktree", "--backend", backend, "--json", "--prompt", prompt]
    if model:
        args += ["--model", model]
    proc = subprocess.run(args, cwd=str(repo), env=child_env(), text=True, capture_output=True, timeout=600)
    print(f"[{backend} scripted turn] rc={proc.returncode}\n{proc.stdout[-400:]}", flush=True)
    return state_of(repo).get("backend_session_id")


def s_switch_seeded() -> None:
    """BOTH switch-naming paths, with the fragile part removed — the most reliable of these.

    Each backend's conversation is created by ``--json --prompt`` instead of by typing into a
    composer, so what is being tested (does a switch ask for a name? does switching back restore
    one?) no longer depends on whether a typed prompt landed. It is also the live proof of the
    state-clobber fix: without a worktree the session state and the repo-root state are ONE
    file, so a name recorded for one backend used to be erased by the next backend's writes.
    """
    repo = make_repo("live_sb")
    first = scripted_turn(repo, BACKEND, "Create a file named mu.txt whose only content is mu. Then stop.", MODEL)
    check("seed: the first backend's turn committed", commit_count(repo) == 2, str(first))
    check("seed: its commit carries the backend's metadata", f"backend: {BACKEND}" in head_message(repo))

    # Name that conversation as the TUI would, then hand the repo to a DIFFERENT backend:
    # switching at startup files the first backend's conversation under `backend_session_ids`.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agitrack.config import AgitrackState

    AgitrackState(repo).name_session(first, "first-one")
    other = "claude" if BACKEND != "claude" else "codex"
    scripted_turn(repo, other, "Reply with exactly the word: ready. Do not use any tools.")
    check("seed: the first conversation is remembered for a switch back",
          state_of(repo).get("backend_session_ids", {}).get(BACKEND) == first)
    check("seed: its NAME survived the other backend's turns (clobber fix)",
          state_of(repo).get("session_names", {}).get(first) == "first-one",
          f"{state_of(repo).get('session_names')}")

    handle = Tui(repo, [AGITRACK, "--no-worktree", "--backend", other])
    order = sorted({BACKEND, other, "claude", "codex", "opencode"})
    try:
        handle.boot(None)
        screen = handle.switch_backend(BACKEND, order)
        check("switch BACK to a stored conversation does NOT ask for a name", f"New {BACKEND} session" not in screen)
        check("switch BACK restores that conversation's name", "| first-one " in screen, screen[-300:])
        check("switch BACK resumes the stored conversation id", state_of(repo).get("backend_session_id") == first)

        third = next(name for name in order if name not in (BACKEND, other))
        screen = handle.switch_backend(third, order, quiet=6.0)
        asked = f"New {third} session" in screen
        check("switch to a backend with NO stored conversation ASKS for a name", asked, screen[-300:])
        if asked:
            screen = handle.answer_name("third-one")
            check("the typed name reaches the status bar", "| third-one " in screen, screen[-300:])
            check("the name is durable immediately (pending_session_name)",
                  state_of(repo).get("pending_session_name") == "third-one")
            check("naming the new session did NOT erase the old names (clobber fix)",
                  state_of(repo).get("session_names", {}).get(first) == "first-one",
                  f"{state_of(repo).get('session_names')}")
    finally:
        handle.kill()


SCENARIOS = {
    "no_worktree": s_no_worktree,
    "worktree": s_worktree,
    "delay_merge": s_delay_merge,
    "manual": s_manual_external,
    "manual_menu": s_manual_menu,
    "background": s_background,
    "background_manual": s_background_manual,
    "no_confine": s_no_confine,
    "allowed_edit_paths": s_allowed_edit_paths,
    "backend_command": s_backend_command,
    "switch_no_worktree": s_switch_no_worktree,
    "switch_worktree": s_switch_worktree,
    "switch_seeded": s_switch_seeded,
}


def main(argv: list[str] | None = None) -> int:
    global WORKDIR, BACKEND, MODEL, AGITRACK
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenarios", nargs="*", choices=[*SCENARIOS, []], help="which scenarios to run (default: all)")
    parser.add_argument("--workdir", default=str(WORKDIR), help="where throwaway repos and pty logs go")
    parser.add_argument("--backend", default=BACKEND)
    parser.add_argument("--model", default=MODEL, help="'' for the CLI's own default")
    parser.add_argument("--agitrack", default=AGITRACK, help="the aGiTrack executable to drive")
    args = parser.parse_args(argv)

    WORKDIR = Path(args.workdir)
    WORKDIR.mkdir(parents=True, exist_ok=True)
    BACKEND, MODEL, AGITRACK = args.backend, (args.model or None), args.agitrack

    for key in args.scenarios or list(SCENARIOS):
        print("\n" + "#" * 30, key, "#" * 30, flush=True)
        try:
            SCENARIOS[key]()
        except Exception as error:  # one broken scenario must not hide the rest
            import traceback

            traceback.print_exc()
            check(f"{key}: the scenario crashed", False, str(error)[:300])

    print("\n" + "=" * 70)
    for name, verdict, detail in RESULTS:
        print(f"{verdict:4}  {name}  {detail}")
    failures = sum(1 for _, verdict, _ in RESULTS if verdict == "FAIL")
    print("=" * 70)
    print(f"FAILURES: {failures} of {len(RESULTS)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
