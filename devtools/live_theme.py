#!/usr/bin/env python3
"""Drive the REAL aGiTrack TUI over a PTY and assert the COLOURS it actually emitted.

Why this is separate from ``live_tui.py``
-----------------------------------------
``live_tui`` proves a MODE works by polling git, and it STRIPS every escape sequence to match
screen text. Agent-theme adaptation is the exact opposite question: the whole feature is which
background colour lands in the cells the backend never painted, and in aGiTrack's own chrome.
Stripping the escapes throws away the evidence, so this harness keeps every byte, re-runs it
through the same pyte screen aGiTrack itself uses, and asserts on cell colours and on the SGR
sequences in the stream. It reuses ``live_tui``'s pty plumbing (``Tui``, boot, ``check``).

The rule from ``live_tui`` still governs: never conclude something happened by reading the
screen loosely. "The status bar looked dark" is not a result; ``bg == '1c1c1c'`` for 6750 of
6750 cells is.

Staging a terminal, and staging an agent
----------------------------------------
The HOST terminal's theme is staged by answering the OSC 10/11/4 queries aGiTrack sends at
startup (``live_tui.host_terminal_reply``) — the developer's real terminal is never involved.
The AGENT's theme is staged per backend, and the backends differ in what they even offer:

* claude   — ``theme`` in ``settings.json`` (an isolated ``CLAUDE_CONFIG_DIR`` per run, so a
             live ``/theme`` switch cannot touch the developer's own settings), and ``/theme``
             switches it inside a running session.
* opencode — no light/dark SETTING: every opencode theme is adaptive (nord and gruvbox both
             render their light variant against a reported white background, checked on the
             bare CLI), so the mode follows the terminal. It can be switched at RUNTIME from
             its command palette ("Switch between light and dark theme mode").
* codex    — has NO light/dark control at all. Its palette is derived from the background the
             terminal reports (verified: the same build paints ``f4f4f4`` panels against a
             reported white background and ``373737`` against a dark one), and its ``/themes``
             command picks a SYNTAX-highlighting theme, not a light/dark UI mode.

For the two that follow the terminal, the only way agent and terminal can disagree is a forced
``agent_background`` — which is also what makes the OSC relay observable end to end: under
``agent_background=dark`` in a white terminal the backend itself must paint its dark panels,
because that is the colour aGiTrack reported to it.

Usage
-----
    python devtools/live_theme.py --workdir /tmp/agitrack-theme --backend claude
    python devtools/live_theme.py --workdir /tmp/agitrack-theme --backend codex mismatch startup

No agent turn is ever run here: a booted TUI already paints everything the inference reads, so
these scenarios cost no tokens.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyte  # noqa: E402

import live_tui as lt  # noqa: E402
from agitrack.proxy.renderer import (  # noqa: E402
    _CANVAS_DARK,
    _CANVAS_LIGHT,
    ScreenRenderer,
    _BackgroundColorEraseScreen,
)
from agitrack.proxy.runner import _PYTE_HOSTILE_CSI_RE  # noqa: E402

ROWS, COLS = 45, 150
check = lt.check

# The canvas colours, and the exact "back to normal" sequence aGiTrack emits with each one in
# front of a truecolor terminal (``reset_sgr`` = reset + the canvas fg + the canvas bg). These
# are asserted against the renderer's own constants below, so a change there fails loudly here
# instead of silently making every assertion vacuous.
CANVAS = {"dark": _CANVAS_DARK, "light": _CANVAS_LIGHT}
assert CANVAS["dark"] == ("1c1c1c", "d0d0d0") and CANVAS["light"] == ("ffffff", "1c1c1c")
CANVAS_RESET = {
    "dark": b"\x1b[0;38;2;208;208;208;48;2;28;28;28m",
    "light": b"\x1b[0;38;2;28;28;28;48;2;255;255;255m",
}
PLAIN_RESET = b"\x1b[0m"


# ---------------------------------------------------------------------------
# Reading colours back off the wire
# ---------------------------------------------------------------------------


def emulate(raw: bytes, rows: int = ROWS, cols: int = COLS):
    """Replay what aGiTrack wrote to the terminal through the SAME screen model it uses, so a
    cell's colour here is the colour a real terminal would show."""
    screen = _BackgroundColorEraseScreen(cols, rows, history=5000, ratio=0.5)
    pyte.ByteStream(screen).feed(_PYTE_HOSTILE_CSI_RE.sub(b"", bytes(raw)))
    return screen


def backgrounds(screen, rows: int = ROWS, cols: int = COLS) -> Counter:
    counts: Counter = Counter()
    for row in range(rows):
        line = screen.buffer[row]
        for col in range(cols):
            counts[line[col].bg] += 1
    return counts


def foregrounds(screen, rows: int = ROWS, cols: int = COLS) -> Counter:
    counts: Counter = Counter()
    for row in range(rows):
        line = screen.buffer[row]
        for col in range(cols):
            if (line[col].data or " ").strip():
                counts[line[col].fg] += 1
    return counts


def row_text(screen, row: int, cols: int = COLS) -> str:
    return "".join((screen.buffer[row][col].data or " ") for col in range(cols)).rstrip()


def rows_matching(screen, needle: str, rows: int = ROWS) -> list[int]:
    return [row for row in range(rows) if needle in row_text(screen, row)]


def box_columns(screen, row: int) -> list[int]:
    """The columns strictly inside a popup's border on *row* (empty when there is no box)."""
    edges = [col for col in range(COLS) if (screen.buffer[row][col].data or " ") in "┃┏┓┗┛━"]
    return list(range(edges[0] + 1, edges[-1])) if len(edges) >= 2 else []


def describe(screen) -> str:
    counts = backgrounds(screen)
    return ", ".join(f"{colour}×{count}" for colour, count in counts.most_common(4))


def status_backgrounds(screen) -> Counter:
    """The backgrounds of the status bar — aGiTrack's OWN bottom row."""
    return Counter(screen.buffer[ROWS - 1][col].bg for col in range(COLS))


def canvas_verdict(screen) -> str:
    """What the canvas is — 'dark', 'light' or 'none' — read off aGiTrack's own chrome.

    The status bar is drawn by aGiTrack on every frame and resets with ``reset_sgr``, so its
    background IS the canvas (and the terminal's ``default`` when there is none). Reading THAT
    rather than the whole screen matters for a backend that paints every cell itself: opencode
    fills the entire screen with its own theme colour, so the screen histogram says what the
    AGENT painted, not what aGiTrack did with the cells the agent left alone."""
    colour, _ = status_backgrounds(screen).most_common(1)[0]
    if colour == "default":
        return "none"
    for scheme, (background, _) in CANVAS.items():
        if colour == background:
            return scheme
    return f"other({colour})"


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def theme_home(tag: str, **settings) -> Path:
    """A private aGiTrack config dir, so ``agent_background`` and the remembered per-backend
    scheme are this scenario's alone."""
    home = lt.WORKDIR / f"home-{tag}"
    home.mkdir(parents=True, exist_ok=True)
    data = {"default_backend": lt.BACKEND, "check_for_updates": False, "use_worktrees": False}
    data.update(settings)
    (home / "config.json").write_text(json.dumps(data), encoding="utf-8")
    return home


def home_config(home: Path) -> dict:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


def claude_home(tag: str, theme: str | None) -> Path:
    """An isolated CLAUDE_CONFIG_DIR carrying the developer's login but this scenario's theme.

    Claude persists a ``/theme`` choice, so pointing it at the real ``~/.claude`` would let a
    live switch scenario rewrite the developer's own setting."""
    home = lt.WORKDIR / f"claude-{tag}"
    home.mkdir(parents=True, exist_ok=True)
    source = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
    identity = {
        key: source[key]
        for key in ("oauthAccount", "userID", "hasCompletedOnboarding", "lastOnboardingVersion",
                    "installMethod", "autoUpdates", "firstStartTime")
        if key in source
    }
    identity["projects"] = {}
    (home / ".claude.json").write_text(json.dumps(identity), encoding="utf-8")
    (home / "settings.json").write_text(json.dumps({"theme": theme or "auto"}), encoding="utf-8")
    return home


def trust(repo: Path, home: Path | None) -> None:
    # Only the backend actually being driven: both trust files are shared, developer-owned
    # state, and two backends' runs must not append to the same file at the same time.
    if lt.BACKEND == "codex":
        lt.trust_codex(repo)
    if home is None:
        # No claude in this run — and the developer's real ~/.claude.json is deliberately not
        # touched, so two backends' scenarios can run side by side without racing on it.
        return
    path = home / ".claude.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("projects", {})[str(repo)] = {
        "hasTrustDialogAccepted": True,
        "hasCompletedProjectOnboarding": True,
        "allowedTools": [],
        "history": [],
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def themed_from_config(backend: str) -> bool:
    """Whether the backend's light/dark scheme can be SET independently of the terminal.

    Only claude can (``theme`` in settings.json). codex has no light/dark setting at all, and
    opencode's themes are adaptive — every one of them (nord, gruvbox, …) renders its light
    variant when the terminal reports a light background, verified against the bare CLI. For
    those two the only way to make agent and terminal disagree is a forced
    ``agent_background``, which is also the setting whose OSC relay makes them disagree
    deliberately."""
    return backend == "claude"


def switchable_in_agent(backend: str) -> bool:
    """Whether the RUNNING agent can be switched between light and dark from inside it."""
    return backend in {"claude", "opencode"}


def stage(tag: str, agent_theme: str | None, host: str, *, background: str = "auto",
          remembered: dict | None = None) -> tuple[Path, Path, dict]:
    """A throwaway repo plus the environment that puts *agent_theme* in front of a *host*
    terminal. Returns (repo, aGiTrack config home, child env)."""
    repo = lt.make_repo(f"th_{tag}")
    settings: dict = {"agent_background": background}
    if remembered is not None:
        settings["agent_theme_seen"] = remembered
    home = theme_home(tag, **settings)
    env = {
        "AGITRACK_CONFIG_DIR": str(home),
        # Truecolor makes the evidence readable: the canvas arrives as 48;2;28;28;28 rather
        # than as a palette index that has to be decoded before it means anything.
        "COLORTERM": "truecolor",
        "AGITRACK_DEBUG_PROXY": "1",  # writes .agitrack/proxy-debug-*.log — the decision trail
    }
    claude_dir: Path | None = None
    if lt.BACKEND == "claude":
        claude_dir = claude_home(tag, agent_theme)
        env["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    trust(repo, claude_dir)
    return repo, home, env


def launch(repo: Path, env: dict, host: str, tag: str, *, args: list[str] | None = None) -> lt.Tui:
    command = [lt.AGITRACK, "--no-worktree", "--backend", lt.BACKEND]
    if lt.MODEL and lt.BACKEND != "opencode":
        command += ["--model", lt.MODEL]
    command += args or []
    return lt.Tui(repo, command, log=lt.WORKDIR / f"{tag}.pty.log", env=env, host_theme=host)


def debug_log(repo: Path) -> str:
    logs = sorted((repo / ".agitrack").glob("proxy-debug-*.log"))
    return logs[-1].read_text(encoding="utf-8", errors="replace") if logs else ""


def settle(handle: lt.Tui, seconds: float = 3.0) -> None:
    """Let the reactor tick with NOBODY typing — the state every 'while idle' claim is about."""
    end = time.time() + seconds
    while time.time() < end:
        handle.read(0.4, cap=1.0)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def s_mismatch() -> None:
    """The original complaint, both ways round: an agent themed against the terminal must not
    leave "a combination of dark and white backgrounds"."""
    for agent, host in (("dark", "light"), ("light", "dark")):
        tag = f"{lt.BACKEND[:2]}_{agent}_in_{host}"
        title = f"{lt.BACKEND}: {agent} agent in a {host} terminal"
        if themed_from_config(lt.BACKEND):
            repo, home, env = stage(tag, agent, host)
        else:
            # No theme of its own; forcing the background is what makes it paint against the
            # terminal, and the forced value is what aGiTrack reports to it over OSC 11.
            repo, home, env = stage(tag, None, host, background=agent)
        handle = launch(repo, env, host, tag)
        try:
            handle.boot(f"{tag}")
            settle(handle)
            screen = emulate(handle.raw)
            verdict = canvas_verdict(screen)
            check(f"{title}: the canvas is {agent}", verdict == agent, f"got {verdict}; {describe(screen)}")
            counts = backgrounds(screen)
            check(f"{title}: NO cell is left at the terminal's own background",
                  counts.get("default", 0) == 0, f"default×{counts.get('default', 0)}")
            check(f"{title}: the status bar sits on the agent's background",
                  set(status_backgrounds(screen)) == {CANVAS[agent][0]},
                  f"{sorted(status_backgrounds(screen))} | {row_text(screen, ROWS - 1)[:70]!r}")
            check(f"{title}: the canvas reset sequence is on the wire",
                  CANVAS_RESET[agent] in handle.raw, f"{CANVAS_RESET[agent]!r}")
            # The popup chrome is aGiTrack's own, drawn with reset_sgr — the piece the user saw
            # in the wrong colour.
            handle.press(b"\x07", "Ctrl-G", 2.0)
            settle(handle, 1.5)
            screen = emulate(handle.raw)
            palette = rows_matching(screen, "agent-backend")
            # Only the columns INSIDE the popup's own border: the rest of that row is still the
            # backend's screen, and a backend that paints every cell (opencode) has its own
            # colour there — which says nothing about aGiTrack's chrome.
            inside = box_columns(screen, palette[0]) if palette else []
            ok = bool(inside) and all(
                screen.buffer[row][col].bg == CANVAS[agent][0] for row in palette for col in inside
            )
            check(f"{title}: the command palette is drawn on the agent's background", ok,
                  f"rows={palette} cols={inside[:1]}..{inside[-1:]} "
                  f"{sorted({screen.buffer[palette[0]][col].bg for col in inside}) if inside else ''}")
            handle.press(b"\x1b", "close the palette", 1.5)
            if not themed_from_config(lt.BACKEND):
                # The forced background is relayed over OSC 11, so the BACKEND itself must have
                # themed to match — otherwise the canvas would be papering over a mismatch.
                painted = {colour for colour in backgrounds(screen) if colour not in {"default", CANVAS[agent][0]}}
                check(f"{title}: the backend itself painted for a {agent} background",
                      bool(painted) and all(_is_dark(colour) == (agent == "dark") for colour in painted),
                      f"backend fills {sorted(painted)}")
        finally:
            handle.kill()


def _is_dark(colour: str) -> bool:
    red, green, blue = (int(colour[at : at + 2], 16) for at in (0, 2, 4))
    return (0.299 * red + 0.587 * green + 0.114 * blue) < 128


def s_match() -> None:
    """Terminal and agent agree: NO canvas, so the output is what aGiTrack always produced."""
    for scheme in ("dark", "light"):
        tag = f"{lt.BACKEND[:2]}_match_{scheme}"
        title = f"{lt.BACKEND}: {scheme} agent in a {scheme} terminal"
        agent = scheme if themed_from_config(lt.BACKEND) else None
        repo, home, env = stage(tag, agent, scheme)
        handle = launch(repo, env, scheme, tag)
        try:
            handle.boot(tag)
            settle(handle)
            screen = emulate(handle.raw)
            verdict = canvas_verdict(screen)
            check(f"{title}: no canvas is applied", verdict == "none", f"got {verdict}; {describe(screen)}")
            check(f"{title}: aGiTrack's chrome keeps the terminal's own background",
                  set(status_backgrounds(screen)) == {"default"},
                  f"{sorted(status_backgrounds(screen))}")
            check(f"{title}: no canvas reset sequence is ever emitted",
                  CANVAS_RESET["dark"] not in handle.raw and CANVAS_RESET["light"] not in handle.raw)
            check(f"{title}: chrome resets with a plain SGR reset", PLAIN_RESET in handle.raw)
        finally:
            handle.kill()


def s_switch() -> None:
    """A theme changed INSIDE the agent, with aGiTrack idle, is followed within about a second.

    The DIRECTION is per backend, for a reason worth writing down: opencode dims the whole
    screen behind its command palette, and a dimmed light theme (``696969``) reads as dark, so
    a light→dark switch driven from that palette cannot be told apart from the dimming. Driving
    it dark→light instead makes the dimming harmless — it can only ever argue for the scheme
    that is already there — so the light canvas at the end is unambiguously the switch."""
    if not switchable_in_agent(lt.BACKEND):
        check(f"{lt.BACKEND}: in-agent light/dark switch", True,
              "SKIPPED - this backend has no light/dark theme of its own (see module docstring)")
        return
    start = "light" if lt.BACKEND == "claude" else "dark"
    target = "dark" if start == "light" else "light"
    tag = f"{lt.BACKEND[:2]}_switch"
    title = f"{lt.BACKEND}: in-agent switch to {target} while idle"
    repo, home, env = stage(tag, start, start)
    handle = launch(repo, env, start, tag)
    try:
        handle.boot(tag)
        settle(handle)
        screen = emulate(handle.raw)
        check(f"{title}: starts with no canvas (agent and terminal agree)",
              canvas_verdict(screen) == "none", describe(screen))
        mark = len(handle.raw)
        # Timed from the KEYSTROKE that switched the theme, not from when the harness stopped
        # reading afterwards — the reply to that keystroke is exactly where the canvas shows up.
        switched = switch_theme(handle, target)
        # Nobody types from here on: the canvas must be picked up by the reactor tick.
        deadline = time.time() + 10.0
        while time.time() < deadline and CANVAS_RESET[target] not in handle.raw[mark:]:
            handle.read(0.2, cap=0.4)
        found = handle.raw.find(CANVAS_RESET[target], mark)
        took = None
        if found >= 0:
            arrived = handle.when(found)
            took = None if arrived is None else arrived + handle.started - switched
        check(f"{title}: the canvas follows the agent", found >= 0,
              f"after {took:.2f}s" if took is not None else "never")
        if found >= 0:
            # This clock includes the AGENT's own repaint, which is the larger and more
            # variable part: claude re-themes in one frame (measured 2.1 s end to end), while
            # opencode regenerates its palette asynchronously and took 2.2 s once and 8.0 s
            # another time. aGiTrack's own share is bounded and small — the
            # CANVAS_VOTES_TO_SWITCH agreeing samples a change to an ESTABLISHED canvas needs,
            # CANVAS_SAMPLE_INTERVAL apart — and in the 8.0 s run its canvas landed 6 KB and
            # zero full repaints after the agent's first light frame. So the bound here is
            # generous on purpose: what must not happen is "never".
            check(f"{title}: followed, and not only after the user touched something",
                  took is not None and 0 <= took <= 12.0, f"{took:.2f}s")
            settle(handle)
            screen = emulate(handle.raw)
            check(f"{title}: aGiTrack's chrome ends up {target}", canvas_verdict(screen) == target,
                  describe(screen))
            check(f"{title}: and the agent really is painting {target} now",
                  backend_scheme(screen, target) is (target == "dark"),
                  f"backend painted for dark={backend_scheme(screen, target)}")
    finally:
        release_theme_lock(handle)
        handle.kill()


def release_theme_lock(handle: lt.Tui) -> None:
    """Put the agent back to following the terminal.

    opencode's runtime switch LOCKS the mode, and the lock is global and persistent: every
    later opencode session — this harness's and the developer's own — then ignores the
    reported background. Verified the hard way; a scenario that changes a machine-wide
    preference has to change it back."""
    if lt.BACKEND != "opencode" or handle.proc.poll() is not None:
        return
    try:
        handle.press(b"\x10", "opencode command palette (ctrl+p)", 2.0)
        handle.press(b"lock", "search for the theme-mode lock", 2.0)
        if rows_matching(emulate(handle.raw), "Unlock theme mode"):
            handle.press(b"\r", "unlock the theme mode", 2.5)
            check("opencode: the theme-mode lock the switch set is released again",
                  not rows_matching(emulate(handle.raw), "Unlock theme mode"))
        else:
            handle.press(b"\x1b", "close the palette", 1.0)
    except AssertionError as error:  # teardown must not mask the scenario's own result
        print(f"    could not release opencode's theme lock: {error}", flush=True)


def switch_theme(handle: lt.Tui, target: str) -> float:
    """Switch the running agent to its *target* scheme, the way a user would.

    Returns the monotonic time of the keystroke that did it, which is where the clock for
    "followed within about a second" starts."""
    if lt.BACKEND == "claude":
        # type_prompt retries until the composer echoes: a slash command typed into a composer
        # that repaints a beat later is simply lost, with nothing on screen to say so.
        handle.type_prompt("/theme")
        handle.press(b"\r", "open claude's /theme menu", 3.0)
        # The entries are NUMBERED ("2. Dark mode"), and the number is the shortcut. Pressing
        # it beats walking the list with arrows: the menu shares the screen with a composer
        # that has its own "❯", so "which row is selected" is not reliably readable.
        wanted = re.compile(rf"(\d+)\.\s+{target.capitalize()} mode\s*$")
        deadline = time.time() + 15
        while time.time() < deadline:
            screen = emulate(handle.raw)
            entry = next(
                (match for row in range(ROWS) if (match := wanted.search(row_text(screen, row)))),
                None,
            )
            if entry is None:
                handle.read(0.5, cap=1.0)
                continue
            stamp = time.monotonic()
            handle.press(entry.group(1).encode(), f"choose {entry.group(0).strip()!r}", 2.5)
            if wanted.search("\n".join(row_text(emulate(handle.raw), r) for r in range(ROWS))):
                stamp = time.monotonic()
                handle.press(b"\r", "confirm the theme choice", 2.5)
            return stamp
        raise AssertionError(f"never found '{target} mode' in claude's /theme menu:\n{handle.buffer[-2500:]}")
    if lt.BACKEND == "opencode":
        handle.press(b"\x10", "opencode command palette (ctrl+p)", 2.5)
        deadline = time.time() + 12
        while time.time() < deadline:
            if rows_matching(emulate(handle.raw), "Commands"):
                break
            handle.read(0.5, cap=1.0)
        else:
            raise AssertionError(f"opencode's command palette never opened:\n{handle.buffer[-1500:]}")
        # The entry is titled for what it WILL do ("Switch to light mode"), and the palette
        # matches on that title — "theme mode" finds only the lock command.
        handle.press(f"{target} mode".encode(), f"search for '{target} mode'", 2.5)
        hit = rows_matching(emulate(handle.raw), f"Switch to {target} mode")
        if not hit:
            raise AssertionError(f"'Switch to {target} mode' is not in the palette:\n{handle.buffer[-1500:]}")
        stamp = time.monotonic()
        handle.press(b"\r", f"switch opencode to {target} mode", 3.0)
        return stamp
    raise AssertionError(f"no in-agent theme switch known for {lt.BACKEND}")


def s_startup() -> None:
    """No flip a beat after launch, and no blank white/black gap while the backend starts.

    codex cannot disagree with the terminal on its own, so its mismatch — and therefore its
    startup case — is the forced ``agent_background``; there the canvas is known from the
    setting rather than remembered, which is asserted instead of the memory."""
    tag = f"{lt.BACKEND[:2]}_start"
    agent = "dark" if themed_from_config(lt.BACKEND) else None
    background = "auto" if agent else "dark"
    for run, remembered in (("cold", {}), ("warm", {lt.BACKEND: "dark"})):
        title = f"{lt.BACKEND}: {run} start, dark agent in a light terminal"
        repo, home, env = stage(f"{tag}_{run}", agent, "light", background=background,
                                remembered=remembered)
        handle = launch(repo, env, "light", f"{tag}_{run}")
        try:
            handle.boot(f"{tag}-{run}")
            settle(handle)
            alt = handle.raw.find(b"\x1b[?1049h")
            first = handle.raw.find(CANVAS_RESET["dark"])
            wrong = handle.raw.find(CANVAS_RESET["light"])
            check(f"{title}: the session ends up on the dark canvas", first >= 0)
            check(f"{title}: the WRONG canvas is never painted", wrong < 0,
                  f"light canvas at byte {wrong}")
            if first >= 0 and alt >= 0:
                from_alt = (handle.when(first) or 0) - (handle.when(alt) or 0)
                print(f"    [{run}] TUI took the screen at {handle.when(alt):.2f}s, "
                      f"dark canvas at {handle.when(first):.2f}s (Δ {from_alt:.2f}s)", flush=True)
                check(f"{title}: the canvas is up within a second of the TUI taking the screen",
                      from_alt <= 1.0, f"Δ {from_alt:.2f}s")
                if run == "warm" or background != "auto":
                    check(f"{title}: the canvas is up with the very first clear",
                          from_alt <= 0.05, f"Δ {from_alt:.2f}s")
                    # The alternate screen is filled with the canvas the moment it is cleared —
                    # otherwise a white (or black) flash shows until the backend paints. That
                    # exact pair of sequences is written by nothing else, and it must be the
                    # FIRST canvas on the wire or something painted the wrong colours first.
                    fill = handle.raw.find(CANVAS_RESET["dark"] + b"\x1b[2J\x1b[H")
                    check(f"{title}: the just-cleared screen is filled with the canvas",
                          fill >= 0 and fill == first, f"fill={fill} first canvas={first}")
            if background == "auto":
                check(f"{title}: the remembered scheme is recorded for this backend",
                      home_config(home).get("agent_theme_seen", {}).get(lt.BACKEND) == "dark",
                      str(home_config(home).get("agent_theme_seen")))
            else:
                # A forced background never consults or updates the memory: what the config
                # went in with is what it comes out with.
                check(f"{title}: a forced background leaves the remembered scheme alone",
                      home_config(home).get("agent_theme_seen", {}) == remembered,
                      f"{home_config(home).get('agent_theme_seen')} vs {remembered}")
        finally:
            handle.kill()


def s_memory() -> None:
    """The remembered scheme is per backend: one backend's must never be applied to another.

    Under ``auto`` every backend records what it paints — a themed one records its theme, and
    codex (which follows the terminal) records the terminal's. The OTHER backend's entry is
    seeded with the opposite value, so carrying it across would be unmistakable."""
    tag = f"{lt.BACKEND[:2]}_mem"
    other = "codex" if lt.BACKEND != "codex" else "claude"
    themed = themed_from_config(lt.BACKEND)
    agent = "dark" if themed else None
    mine, theirs = ("dark", "light") if themed else ("light", "dark")
    repo, home, env = stage(tag, agent, "light", remembered={other: theirs})
    handle = launch(repo, env, "light", tag)
    try:
        handle.boot(tag)
        settle(handle)
        seen = home_config(home).get("agent_theme_seen", {})
        check(f"{lt.BACKEND}: its own scheme is remembered under its own key",
              seen.get(lt.BACKEND) == mine, str(seen))
        check(f"{lt.BACKEND}: the other backend's remembered scheme is untouched",
              seen.get(other) == theirs, str(seen))
        check(f"{lt.BACKEND}: the other backend's scheme was NOT applied to this session",
              CANVAS_RESET[theirs] not in handle.raw)
    finally:
        handle.kill()


def backend_scheme(screen, canvas: str | None) -> bool | None:
    """What the BACKEND painted for, read with aGiTrack's OWN rule (``agent_theme_is_dark``)
    after undoing the canvas — every cell wearing exactly the canvas colours is one aGiTrack
    filled in, so it goes back to "default" before the vote. True = the backend is drawing for
    a dark background. This is how "the backend was told the forced colour" is checked without
    guessing at each CLI's palette: claude paints almost no backgrounds at all (14 accent
    cells), so a background histogram cannot answer it."""
    canvas_bg, canvas_fg = CANVAS[canvas] if canvas else (None, None)
    body = []
    for row in range(ROWS - 1):  # the last row is aGiTrack's status bar, not the backend's
        cells = {}
        for col in range(COLS):
            cell = screen.buffer[row][col]
            cells[col] = SimpleNamespace(
                fg="default" if cell.fg == canvas_fg else cell.fg,
                bg="default" if cell.bg == canvas_bg else cell.bg,
                data=cell.data,
            )
        body.append(cells)
    return ScreenRenderer.agent_theme_is_dark(ScreenRenderer(ROWS, COLS), body)


def s_overrides() -> None:
    """``agent_background``: dark / light force it, terminal opts out, auto adapts. The forced
    value is also what the backend is told over OSC 11, so a self-theming backend agrees.

    The agent is left on its own default here (claude's ``auto``; codex and opencode have no
    choice), so what it paints reports what it was TOLD — that is the end-to-end proof of the
    relay, and it works the same on all three."""
    for setting in ("dark", "light"):
        tag = f"{lt.BACKEND[:2]}_ov_{setting}"
        title = f"{lt.BACKEND}: agent_background={setting} in a light terminal"
        repo, home, env = stage(tag, None, "light", background=setting)
        handle = launch(repo, env, "light", tag)
        try:
            handle.boot(tag)
            settle(handle)
            screen = emulate(handle.raw)
            verdict = canvas_verdict(screen)
            check(f"{title}: canvas is {setting}", verdict == setting, f"got {verdict}; {describe(screen)}")
            painted = backend_scheme(screen, setting)
            check(f"{title}: the backend was told the forced colour (it painted to match)",
                  painted is (setting == "dark"), f"backend painted for dark={painted}; {describe(screen)}")
        finally:
            handle.kill()

    # Opting out needs a REAL disagreement to ignore, which only a backend with a theme of its
    # own can produce: the other two follow whatever aGiTrack reports, and under "terminal"
    # that is the truth.
    tag = f"{lt.BACKEND[:2]}_ov_terminal"
    title = f"{lt.BACKEND}: agent_background=terminal in a light terminal"
    if themed_from_config(lt.BACKEND):
        repo, home, env = stage(tag, "dark", "light", background="terminal")
        handle = launch(repo, env, "light", tag)
        try:
            handle.boot(tag)
            settle(handle)
            screen = emulate(handle.raw)
            check(f"{title}: a DARK agent is left alone — no canvas", canvas_verdict(screen) == "none",
                  f"got {canvas_verdict(screen)}; {describe(screen)}")
            check(f"{title}: the agent really was dark (so the opt-out had something to ignore)",
                  backend_scheme(screen, None) is True, str(backend_scheme(screen, None)))
            check(f"{title}: no canvas sequence is ever emitted",
                  CANVAS_RESET["dark"] not in handle.raw and CANVAS_RESET["light"] not in handle.raw)
        finally:
            handle.kill()
    else:
        check(f"{title}: the opt-out is exercised", True,
              "SKIPPED - this backend cannot disagree with the terminal on its own, and under "
              "'terminal' the relay is truthful, so there is no mismatch to ignore")

    # auto stays truthful: the backend must see the REAL terminal colour.
    tag = f"{lt.BACKEND[:2]}_ov_auto"
    title = f"{lt.BACKEND}: agent_background=auto relays the real terminal colour"
    repo, home, env = stage(tag, None, "light", background="auto")
    handle = launch(repo, env, "light", tag)
    try:
        handle.boot(tag)
        settle(handle)
        log = debug_log(repo)
        check(f"{title}: aGiTrack detected the staged light terminal",
              "bg=b'rgb:ffff/ffff/ffff'" in log, log[:200])
        screen = emulate(handle.raw)
        check(f"{title}: the backend painted for a light background",
              backend_scheme(screen, None) is False, str(backend_scheme(screen, None)))
        check(f"{title}: agent and terminal agree, so no canvas is applied",
              canvas_verdict(screen) == "none", f"{canvas_verdict(screen)}; {describe(screen)}")
    finally:
        handle.kill()


SCENARIOS = {
    "mismatch": s_mismatch,
    "match": s_match,
    "switch": s_switch,
    "startup": s_startup,
    "memory": s_memory,
    "overrides": s_overrides,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenarios", nargs="*", choices=[*SCENARIOS, []])
    parser.add_argument("--workdir", default=str(lt.WORKDIR))
    parser.add_argument("--backend", default=lt.BACKEND)
    parser.add_argument("--model", default=lt.MODEL, help="'' for the CLI's own default")
    parser.add_argument("--agitrack", default=lt.AGITRACK)
    args = parser.parse_args(argv)

    lt.WORKDIR = Path(args.workdir)
    lt.WORKDIR.mkdir(parents=True, exist_ok=True)
    lt.BACKEND, lt.MODEL, lt.AGITRACK = args.backend, (args.model or None), args.agitrack
    os.environ.setdefault("AGITRACK_LIVE_WORKDIR", str(lt.WORKDIR))

    for key in args.scenarios or list(SCENARIOS):
        print("\n" + "#" * 25, lt.BACKEND, key, "#" * 25, flush=True)
        try:
            SCENARIOS[key]()
        except Exception as error:
            import traceback

            traceback.print_exc()
            check(f"{lt.BACKEND} {key}: the scenario crashed", False, str(error)[:300])

    print("\n" + "=" * 70)
    for name, verdict, detail in lt.RESULTS:
        print(f"{verdict:4}  {name}  {detail}")
    failures = sum(1 for _, verdict, _ in lt.RESULTS if verdict == "FAIL")
    print("=" * 70)
    print(f"FAILURES: {failures} of {len(lt.RESULTS)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
