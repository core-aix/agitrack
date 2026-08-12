"""No aGiTrack process may put a console window on a Windows user's desktop.

THE SYMPTOM THIS EXISTS FOR: "why do I see new terminals appearing for under a second, over
time?" — reported while a background tracker was running. aGiTrack's daemons are DETACHED, so
they have no console of their own; when a console-less Windows process spawns a console
application (git, pip, powershell) without ``CREATE_NO_WINDOW``, Windows allocates a console
window for the child. A once-a-minute update check therefore flashed a window once a minute,
forever, and nothing in the suite could see it: every one of those calls works perfectly.

``agitrack/proc.py::console_isolation_kwargs`` is the fix, and this test is the enforcement —
a new ``subprocess.run`` that forgets it fails here rather than on someone's desktop.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "agitrack"

# The wrappers that make a spawn safe. `detach_kwargs` is the daemon-launching sibling: it
# carries CREATE_NO_WINDOW too (plus DETACHED_PROCESS), so a spawn using it is equally safe.
SAFE_KWARGS = {"console_isolation_kwargs", "detach_kwargs"}

# Spawns that legitimately opt out, each with the reason it cannot flash a window.
ALLOWED = {
    # The backend CLI in interactive proxy mode: aGiTrack IS a console app there, and the
    # agent inherits that console deliberately — it is the terminal the user is looking at.
    ("cli.py", "head + backend_args"),
    # `gh auth login` is an interactive login the user is answering right now.
    ("cli.py", "['gh', 'auth', 'login']"),
    # POSIX-only paths: osascript is macOS, bwrap is Linux. Neither can reach Windows.
    ("host_prompt.py", "['osascript', '-e', script]"),
    ("sandbox.py", "[bwrap, '--dev-bind', '/', '/', '--', true_bin]"),
    # The re-exec of aGiTrack itself at the end of a self-update: it REPLACES this process
    # (the parent is exiting), so there is no daemon left to own a stray console.
    ("updater.py", "cmd"),
}


def _spawn_calls():
    """Every ``subprocess.run``/``Popen``/``call`` in the package, with its argv and whether it
    passes console isolation."""
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("run", "Popen", "call", "check_call", "check_output"):
                continue
            if getattr(node.func.value, "id", "") != "subprocess":
                continue
            isolated = False
            for keyword in node.keywords:
                if keyword.arg is None and isinstance(keyword.value, ast.Call):
                    inner = keyword.value.func
                    if getattr(inner, "id", getattr(inner, "attr", "")) in SAFE_KWARGS:
                        isolated = True
                # A spawn that builds the flags itself (proc.py's own helpers, and any call
                # threading a prepared kwargs dict) is doing the same job by hand.
                elif keyword.arg in ("creationflags", "startupinfo"):
                    isolated = True
                elif keyword.arg is None and isinstance(keyword.value, ast.Name):
                    isolated = True
            argv = ast.unparse(node.args[0]) if node.args else "?"
            yield path.name, node.lineno, argv, isolated


def test_every_spawn_keeps_its_console_window_off_the_desktop():
    offenders = [
        f"{name}:{line}  {argv}"
        for name, line, argv, isolated in _spawn_calls()
        if not isolated and (name, argv) not in ALLOWED
    ]
    assert not offenders, (
        "these subprocess spawns are missing console_isolation_kwargs(), so a console-less "
        "aGiTrack daemon spawning them flashes a window on Windows:\n  " + "\n  ".join(offenders)
    )


def test_the_audit_can_actually_see_a_bad_spawn():
    # An allowlist plus a parser is exactly the shape of test that silently stops checking
    # anything. Prove the detector still detects.
    tree = ast.parse("import subprocess\nsubprocess.run(['git', 'status'])\n")
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call))
    assert not any(keyword.arg in ("creationflags", "startupinfo") for keyword in call.keywords)


@pytest.mark.parametrize("name", sorted({entry[0] for entry in ALLOWED}))
def test_the_allowlist_has_no_stale_entries(name):
    # A file that no longer spawns anything should lose its exemption, or the next spawn added
    # to it inherits a waiver nobody meant to give.
    assert any(spawn[0] == name for spawn in _spawn_calls()), f"{name} no longer spawns anything"
