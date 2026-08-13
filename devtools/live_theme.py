#!/usr/bin/env python3
"""Drive the REAL aGiTrack TUI over a PTY and assert the COLOURS it actually emitted.

Why this is separate from ``live_tui.py``
-----------------------------------------
``live_tui`` proves a MODE works by polling git, and it STRIPS every escape sequence to match
screen text. The background behind the agent is the exact opposite question: the whole subject
is which background colour lands in the cells the backend never painted, and in aGiTrack's own
chrome. Stripping the escapes throws away the evidence, so this harness keeps every byte,
re-runs it through the same pyte screen aGiTrack itself uses, and asserts on cell colours and
on the SGR sequences in the stream. It reuses ``live_tui``'s pty plumbing (``Tui``, boot,
``check``).

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
    python devtools/live_theme.py --workdir /tmp/agitrack-theme --backend codex profiles overrides

Only ``stable`` runs an agent turn (it has to: its whole subject is a screen whose CONTENT
changes). ``profiles``, ``mismatch`` and ``overrides`` assert on a booted TUI and cost no tokens.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyte  # noqa: E402

import live_tui as lt  # noqa: E402
from agitrack.proxy.renderer import (  # noqa: E402
    _CANVAS_DARK,
    _CANVAS_LIGHT,
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
    """A private aGiTrack config dir, so ``agent_background`` is this scenario's alone."""
    home = lt.WORKDIR / f"home-{tag}"
    home.mkdir(parents=True, exist_ok=True)
    data = {"default_backend": lt.BACKEND, "check_for_updates": False, "use_worktrees": False}
    data.update(settings)
    (home / "config.json").write_text(json.dumps(data), encoding="utf-8")
    return home


def claude_home(tag: str, theme: str | None) -> Path:
    """An isolated CLAUDE_CONFIG_DIR carrying the developer's login but this scenario's theme.

    Claude persists a ``/theme`` choice, so pointing it at the real ``~/.claude`` would let a
    live switch scenario rewrite the developer's own setting."""
    home = lt.WORKDIR / f"claude-{tag}"
    home.mkdir(parents=True, exist_ok=True)
    source = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
    identity = {
        key: source[key]
        for key in (
            "oauthAccount",
            "userID",
            "hasCompletedOnboarding",
            "lastOnboardingVersion",
            "installMethod",
            "autoUpdates",
            "firstStartTime",
        )
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
    variant when the terminal reports a light background, verified against the bare CLI. Which
    is the point: those two follow whatever background aGiTrack reports, and aGiTrack reports
    the truth, so they match the terminal without anything having to adapt."""
    return backend == "claude"


def stage(tag: str, agent_theme: str | None = None, *, background: str = "terminal") -> tuple[Path, Path, dict]:
    """A throwaway repo plus the environment that puts *agent_theme* in front of the terminal
    ``launch`` stages. Returns (repo, aGiTrack config home, child env)."""
    repo = lt.make_repo(f"th_{tag}")
    home = theme_home(tag, agent_background=background)
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


def told_the_backend(log: str) -> list[str]:
    """Every DISTINCT background aGiTrack answered the backend's OSC 11 with, in order.

    THE OTHER HALF OF THE FEATURE, and the one the screen cannot show directly. A backend that
    themes itself asks what background it is drawing on; aGiTrack answers on the child's pty,
    where nothing else can see it, so `_answer_terminal_queries` logs what it sent. What the
    backend PAINTS is downstream evidence of the same thing (and is asserted separately), but
    this is the answer itself — and it is what makes a silent terminal work at all, since there
    the value comes from the platform rather than from any reply.
    """
    seen: list[str] = []
    for match in re.finditer(r"answered backend capability query with (b'[^']*'|b\"[^\"]*\")", log):
        for value in re.findall(r"\\x1b\]11;([^\\]*)\\x07", match.group(1)):
            if not seen or seen[-1] != value:
                seen.append(value)
    return seen


def _osc_value(colour: str) -> str:
    """A canvas hex colour in the ``rgb:rrrr/gggg/bbbb`` shape an OSC 11 answer carries."""
    return "rgb:" + "/".join(colour[at : at + 2] * 2 for at in (0, 2, 4))


def derived_background(log: str) -> str | None:
    """The background aGiTrack worked out from the PLATFORM (only when the terminal was silent)."""
    match = re.search(r"host background derived from the platform: b'([^']*)'", log)
    return match.group(1) if match else None


def settle(handle: lt.Tui, seconds: float = 3.0) -> None:
    """Let the reactor tick with NOBODY typing — the state every 'while idle' claim is about."""
    end = time.time() + seconds
    while time.time() < end:
        handle.read(0.4, cap=1.0)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def canvas_markers(raw: bytes, since: int = 0) -> list[tuple[int, str]]:
    """Every point in the byte stream where aGiTrack switched a canvas ON, in order.

    ``reset_sgr`` is emitted with EVERY painted region, so a canvas that is in force leaves its
    exact sequence all over the stream and one that is not leaves none at all. Those two
    sequences are written by nothing else, which makes this the direct measurement of the bug:
    a session that oscillated necessarily emits both, and the ORDER says when it flipped.
    """
    found: list[tuple[int, str]] = []
    for scheme, marker in CANVAS_RESET.items():
        at = raw.find(marker, since)
        while at >= 0:
            found.append((at, scheme))
            at = raw.find(marker, at + 1)
    return sorted(found)


def canvas_transitions(raw: bytes, since: int = 0) -> list[str]:
    """The sequence of DISTINCT canvases the session used, oldest first — ``["dark"]`` for a
    forced dark session, ``[]`` for one that kept the terminal's colours, and anything longer
    is a background the user watched change under them."""
    out: list[str] = []
    for _, scheme in canvas_markers(raw, since):
        if not out or out[-1] != scheme:
            out.append(scheme)
    return out


def backend_is_dark(screen, canvas: str | None) -> bool | None:
    """Whether the BACKEND painted for a dark background, by the backgrounds it filled.

    This lives in the harness, not in aGiTrack: reading a scheme off the screen is exactly the
    thing that oscillated, and it is only sound here because it is applied ONCE, to a settled
    screen, to check what the backend was TOLD — it never decides what anything is painted in.
    Cells wearing exactly the canvas colours are aGiTrack's own fill and are excluded.
    """
    canvas_bg = CANVAS[canvas][0] if canvas else None
    dark = light = 0
    for colour, count in backgrounds(screen).items():
        if colour in {"default", canvas_bg} or len(colour) != 6:
            continue
        if _is_dark(colour):
            dark += count
        else:
            light += count
    if dark + light < 40:  # too few filled cells to mean anything
        return None
    return dark > light


def _is_dark(colour: str) -> bool:
    red, green, blue = (int(colour[at : at + 2], 16) for at in (0, 2, 4))
    return (0.299 * red + 0.587 * green + 0.114 * blue) < 128


# The real Terminal.app profiles staged by live_tui.HOST_THEMES, plus the case that has no
# answer at all — which is what Terminal.app actually is, and the user's own setup.
PROFILES = ("basic", "novel", "silver-aerogel", "homebrew", "ocean", None)


def s_profiles() -> None:
    """Every terminal profile keeps its own colours, whatever the agent paints.

    `novel` (cream) and `silver-aerogel` (exactly 50% grey) are the ones that used to decide
    nothing reliably; `None` stages a terminal that answers no colour query at all, which is
    what macOS Terminal.app really does.
    """
    for profile in PROFILES:
        name = profile or "silent (answers nothing, like Terminal.app)"
        tag = f"{lt.BACKEND[:2]}_prof_{profile or 'silent'}"
        title = f"{lt.BACKEND}: {name} terminal"
        repo, home, env = stage(tag)
        handle = launch(repo, env, profile, tag)
        try:
            handle.boot(tag)
            settle(handle)
            screen = emulate(handle.raw)
            verdict = canvas_verdict(screen)
            check(
                f"{title}: no canvas — the terminal's own colours stand",
                verdict == "none",
                f"got {verdict}; {describe(screen)}",
            )
            check(
                f"{title}: no canvas sequence is EVER on the wire",
                canvas_transitions(handle.raw) == [],
                str(canvas_transitions(handle.raw)),
            )
            check(
                f"{title}: aGiTrack's own chrome keeps the terminal's background",
                set(status_backgrounds(screen)) == {"default"},
                str(sorted(status_backgrounds(screen))),
            )
            check(f"{title}: chrome resets with a plain SGR reset", PLAIN_RESET in handle.raw)
            # AND the backend was told the truth about it — the half the screen cannot show.
            log = debug_log(repo)
            told = told_the_backend(log)
            if profile:
                want = lt.HOST_THEMES[profile][0].decode()
                check(
                    f"{title}: the backend was told the terminal's REAL background",
                    told == [want],
                    f"told {told}, terminal is {want}",
                )
            else:
                # Nothing answered, so the value can only have come from the platform. On the
                # reporter's own setup (Apple Terminal) that is their profile's background; on
                # a machine where nothing is derivable, the honest answer is to say nothing.
                derived = derived_background(log)
                check(
                    f"{title}: a silent terminal is answered from the PLATFORM, or not at all",
                    told == ([derived] if derived else []),
                    f"told {told}, derived {derived}",
                )
        finally:
            handle.kill()


def s_mismatch() -> None:
    """An agent themed AGAINST the terminal is still not a reason to repaint the terminal.

    This is the case the old inference existed for, and the one the new rule deliberately
    declines to act on: the user picked a dark theme inside claude and a WHITE terminal
    profile outside it, so the two really do disagree — and aGiTrack still leaves the white
    terminal alone. Only claude can be staged this way; codex has no light/dark control and
    opencode's themes all follow the reported background (see the module docstring), so for
    those two "mismatch" is not a state that exists.
    """
    if not themed_from_config(lt.BACKEND):
        print(f"(skipped: {lt.BACKEND} has no light/dark theme of its own to disagree with)", flush=True)
        return
    for theme, profile in (("dark", "basic"), ("light", "homebrew")):
        tag = f"{lt.BACKEND[:2]}_mis_{theme}"
        title = f"{lt.BACKEND}: {theme}-themed agent in a {profile} terminal"
        repo, home, env = stage(tag, theme)
        handle = launch(repo, env, profile, tag)
        try:
            handle.boot(tag)
            settle(handle)
            screen = emulate(handle.raw)
            verdict = canvas_verdict(screen)
            check(
                f"{title}: the terminal's own colours stand anyway",
                verdict == "none",
                f"got {verdict}; {describe(screen)}",
            )
            check(
                f"{title}: no canvas sequence is EVER on the wire",
                canvas_transitions(handle.raw) == [],
                str(canvas_transitions(handle.raw)),
            )
            # That the mismatch was really staged is asserted from the BACKEND'S OWN config,
            # not from the screen: claude's opening screen paints ~15 coloured cells, far too
            # few to read a scheme off, and reading a scheme off a screen is the thing that
            # started all this. The screen's (usually inconclusive) verdict is reported as
            # detail so a run that does have enough colour still shows it.
            settings = json.loads((lt.WORKDIR / f"claude-{tag}" / "settings.json").read_text(encoding="utf-8"))
            check(
                f"{title}: the agent really was staged {theme}",
                settings.get("theme") == theme,
                f"claude settings.json theme={settings.get('theme')!r}; "
                f"screen says dark={backend_is_dark(screen, None)}; {describe(screen)}",
            )
        finally:
            handle.kill()


TURN_PROMPT = (
    "Reply with exactly three short paragraphs and nothing else. First a sentence of plain "
    "prose. Then a fenced python code block containing 'def f():' and 'return 1'. Then another "
    "sentence of plain prose. Do not use any tools."
)


def s_stable() -> None:
    """THE REGRESSION: a session long enough for the content to change must never repaint.

    The user's own diagnosis was that the background followed the CONTENT — "it shows dark if
    the view doesn't have a code snippet, and it switches back to bright when there is". So this
    runs a real turn that prints prose, then a fenced code block (whose own fill is what flipped
    the vote), then prose again, and asserts the canvas never moved across the whole session.
    Staged on `novel`, the profile the user reported flickering on, and on a silent terminal.
    """
    for profile in ("novel", None):
        name = profile or "silent"
        tag = f"{lt.BACKEND[:2]}_stable_{name}"
        title = f"{lt.BACKEND}: a full turn in a {name} terminal"
        repo, home, env = stage(tag)
        handle = launch(repo, env, profile, tag)
        try:
            handle.boot(tag)
            settle(handle)
            before = canvas_transitions(handle.raw)
            handle.type_prompt(TURN_PROMPT)
            handle.press(b"\r", "submit the turn", 4.0, cap=180.0)
            deadline = time.time() + 180.0
            while time.time() < deadline and "return 1" not in handle.buffer[-20000:]:
                handle.read(1.0, cap=20.0)
            check(f"{title}: the agent really printed a code block", "return 1" in handle.buffer, handle.buffer[-400:])
            settle(handle, 5.0)  # and keep watching after it falls quiet
            after = canvas_transitions(handle.raw)
            check(
                f"{title}: the background never changed, start to finish",
                after == [] and before == [],
                f"canvases used, in order: {after}",
            )
            screen = emulate(handle.raw)
            check(
                f"{title}: it ends on the terminal's own colours",
                canvas_verdict(screen) == "none",
                f"{canvas_verdict(screen)}; {describe(screen)}",
            )
        finally:
            handle.kill()


def s_overrides() -> None:
    """``agent_background``: dark/light force it for the WHOLE session; anything else keeps the
    terminal's. The forced value is also what the backend is told over OSC 11, so a self-theming
    backend paints to match rather than fighting the canvas."""
    for setting in ("dark", "light"):
        tag = f"{lt.BACKEND[:2]}_ov_{setting}"
        title = f"{lt.BACKEND}: agent_background={setting} in a light terminal"
        repo, home, env = stage(tag, background=setting)
        handle = launch(repo, env, "light", tag)
        try:
            handle.boot(tag)
            settle(handle)
            screen = emulate(handle.raw)
            verdict = canvas_verdict(screen)
            check(f"{title}: canvas is {setting}", verdict == setting, f"got {verdict}; {describe(screen)}")
            check(
                f"{title}: and it is the ONLY canvas the session ever used",
                canvas_transitions(handle.raw) == [setting],
                str(canvas_transitions(handle.raw)),
            )
            # The forced colour REPLACES the terminal's in the relay — asserted on the answer
            # itself, and then on what the backend did with it.
            told = told_the_backend(debug_log(repo))
            want = _osc_value(CANVAS[setting][0])
            check(
                f"{title}: the backend was told {setting}, NOT the terminal's real colour",
                told == [want],
                f"told {told}, wanted {want} (terminal is {lt.HOST_THEMES['light'][0].decode()})",
            )
            # ...and it acted on it. Stated as "never painted AGAINST it" rather than "painted
            # for it", because a booted-but-idle claude fills ~15 coloured cells — too few for
            # `backend_is_dark` to have an opinion at all. Asserting the opposite is absent
            # still catches the failure that matters (a backend fighting the canvas), and it
            # does not turn "this screen carries no evidence" into a false report of one.
            painted = backend_is_dark(screen, setting)
            check(
                f"{title}: and the backend never painted AGAINST it",
                painted is not (setting != "dark"),
                f"backend painted for dark={painted} (None = too few coloured cells to tell); {describe(screen)}",
            )
            # The canvas goes on with the very first clear: the setting IS the answer, so there
            # is nothing to wait for and no white/black flash before it.
            alt = handle.raw.find(b"\x1b[?1049h")
            fill = handle.raw.find(CANVAS_RESET[setting] + b"\x1b[2J\x1b[H")
            first = handle.raw.find(CANVAS_RESET[setting])
            check(
                f"{title}: the just-cleared screen is filled with the canvas",
                fill >= 0 and fill == first,
                f"fill={fill} first={first}",
            )
            if first >= 0 and alt >= 0:
                delta = (handle.when(first) or 0) - (handle.when(alt) or 0)
                check(f"{title}: in place the moment the TUI takes the screen", delta <= 0.05, f"delta {delta:.2f}s")
        finally:
            handle.kill()

    # And the default relays the truth, which is the whole reason nothing has to adapt.
    tag = f"{lt.BACKEND[:2]}_ov_terminal"
    title = f"{lt.BACKEND}: the default relays the real terminal colour"
    repo, home, env = stage(tag, background="terminal")
    handle = launch(repo, env, "light", tag)
    try:
        handle.boot(tag)
        settle(handle)
        log = debug_log(repo)
        check(f"{title}: aGiTrack detected the staged light terminal", "bg=b'rgb:ffff/ffff/ffff'" in log, log[:200])
        check(
            f"{title}: and relayed exactly that, unchanged, to the backend",
            told_the_backend(log) == ["rgb:ffff/ffff/ffff"],
            str(told_the_backend(log)),
        )
        screen = emulate(handle.raw)
        check(
            f"{title}: the backend never painted for a DARK one",
            backend_is_dark(screen, None) is not True,
            f"backend painted for dark={backend_is_dark(screen, None)} (None = too few coloured cells to tell)",
        )
        check(
            f"{title}: so no canvas is needed, and none is applied",
            canvas_verdict(screen) == "none",
            f"{canvas_verdict(screen)}; {describe(screen)}",
        )
    finally:
        handle.kill()


SCENARIOS = {
    "profiles": s_profiles,
    "mismatch": s_mismatch,
    "stable": s_stable,
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
