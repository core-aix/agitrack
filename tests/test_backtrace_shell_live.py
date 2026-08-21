"""Live proof that shell-made edits are recovered, on every backend that is installed.

Deselected by default; run with ``pytest -m live -k shell``.

The mocked tests in tests/test_shell_edits.py assert that each parser reaches
:mod:`agitrack.transcripts.shell_edits` given a transcript we wrote ourselves. That is exactly
the assumption a mock cannot check: whether the shape a REAL backend records is the shape the
parser reads. Claude spells it ``Bash``, OpenCode ``bash``, and Codex has three spellings of
its own (``exec_command``, the legacy ``shell``, and an ``exec`` sandbox whose input is
JavaScript) — a wrong guess about any of them silently reconstructs nothing, which looks
identical to a session that made no edits.

So each test drives the real CLI, tells it to change a file using ONLY its shell tool, and then
rebuilds the backtrace over the directory it worked in. The assertion is comparative on
purpose: the same reconstruction is built twice, once with the shell recovery neutralised, and
the recovered lines must appear only in the run that has it. That is what makes this a test of
the shell path rather than of whichever tool the model happened to reach for.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from agitrack.backends import headless_backends
from agitrack.backends.setup import _executable
from agitrack.metrics import backtrace as bt
from agitrack.transcripts import claude, codex, opencode

pytestmark = pytest.mark.live

_BACKENDS = headless_backends()

# The parser module each backend's transcript is read by — the one whose `edits_from_shell`
# has to be neutralised for the control run.
_PARSER = {"claude": claude, "codex": codex, "opencode": opencode}

# Not the cheapest tier the other live tests use: this prompt has three dependent steps, and a
# small model reliably does the first and stops — which fails as "the sed edit was not
# recovered" and reads like a bug in the recovery. None = the CLI's own default (OpenCode fronts
# arbitrary providers, so no id is valid everywhere).
_MODELS = {"claude": "claude-sonnet-5", "codex": "gpt-5.4-mini", "opencode": None}

_PROMPT = """Use ONLY your shell/terminal tool for every step. Do NOT use any file-editing tool
(no Edit, Write, MultiEdit, apply_patch, str_replace).

1. With a heredoc, create calc.py containing exactly:
def add(a, b):
    return a + b

def mul(a, b):
    return a * b

2. With `sed` in place, change `a * b` to `a * b * 1`.
3. With `echo`, append the line `# done` to calc.py.

Do ALL THREE steps. Before replying, run `cat calc.py` and check it shows all of
`def add(a, b):`, `a * b * 1` and `# done`; if any is missing, fix it and check again.

Then reply with only the word FINISHED."""


def _backend_or_skip(name, tmp_path):
    if shutil.which(_executable(name)) is None:
        pytest.skip(f"{name} is not installed on this machine")
    return _BACKENDS[name](tmp_path)


def _patches_for(directory, path="calc.py"):
    """Every reconstructed patch line the backtrace holds for ``path`` in ``directory``."""
    claude._LAST_EXPORT = None  # the exporter memoizes per file identity; each build must re-read
    view = bt.build_backtrace(directory)
    lines: list[str] = []
    for edits in view.file_edits.values():
        for edit in edits:
            if edit.path == path:
                lines.extend(edit.patch.splitlines())
    return lines


@pytest.mark.parametrize("backend_name", sorted(_BACKENDS))
def test_shell_made_edits_reach_the_backtrace(backend_name, tmp_path, monkeypatch):
    backend = _backend_or_skip(backend_name, tmp_path)

    result = backend.run(
        _PROMPT,
        model=_MODELS[backend_name],
        session_id=None,
        timeout_seconds=300,
    )
    assert result.exit_code == 0, f"{backend_name} run failed: {result!r}"

    # The backend really did the work through its shell — if this fails the run is the problem,
    # not the recovery, and the reconstruction assertions below would be meaningless.
    written = (tmp_path / "calc.py").read_text()
    assert "def add(a, b):" in written, f"{backend_name} never created calc.py: {written!r}"
    assert "a * b * 1" in written, f"{backend_name} did not apply the sed step: {written!r}"
    assert "# done" in written, f"{backend_name} did not append the echo line: {written!r}"

    recovered = _patches_for(tmp_path)
    assert recovered, f"{backend_name}: the backtrace reconstructed no change to calc.py at all"
    added = [line[1:] for line in recovered if line.startswith("+") and not line.startswith("+++")]
    assert "def add(a, b):" in added, f"{backend_name}: the heredoc's content was not recovered"
    assert any("a * b * 1" in line for line in added), f"{backend_name}: the sed edit was not recovered"
    assert "# done" in added, f"{backend_name}: the appended line was not recovered"

    # The control: with the shell recovery switched off, the SAME transcript must yield nothing
    # for this file. Without this the test would pass just as well if the model had quietly used
    # an editing tool, which is the one thing it is here to rule out.
    monkeypatch.setattr(_PARSER[backend_name], "edits_from_shell", lambda *args, **kwargs: [])
    assert not _patches_for(tmp_path), (
        f"{backend_name}: calc.py was reconstructed even with shell recovery disabled — "
        "the model used an editing tool, so this run proves nothing about the shell path"
    )


@pytest.mark.parametrize("backend_name", sorted(_BACKENDS))
def test_the_recorded_harness_version_matches_the_installed_cli(backend_name, tmp_path):
    """The version aGiTrack records must be the version that actually ran.

    A mocked transcript can only confirm we read the field we chose; it cannot catch the CLI
    renaming it, or reporting something other than what `--version` prints. Both would leave
    every commit carrying a wrong or missing harness version, silently — which is exactly the
    blind spot this field exists to remove.
    """
    backend = _backend_or_skip(backend_name, tmp_path)
    installed = _installed_version(backend_name)

    result = backend.run(
        "Reply with exactly the word: pineapple",
        model=_MODELS[backend_name],
        session_id=None,
        timeout_seconds=180,
    )
    assert result.exit_code == 0, f"{backend_name} run failed: {result!r}"

    claude._LAST_EXPORT = None
    view = bt.build_backtrace(tmp_path)
    recorded = {
        line.split(": ", 1)[1].strip()
        for stat in view.dashboard.stats
        for line in (getattr(stat, "message", "") or "").splitlines()
        if line.startswith("backend_version: ")
    }
    assert recorded, f"{backend_name}: no backend_version reached the metadata at all"
    assert installed in recorded, f"{backend_name}: recorded {recorded}, but `--version` says {installed}"


def _installed_version(backend_name):
    """The version number the backend's own `--version` prints.

    Each CLI decorates it differently — `2.1.238 (Claude Code)`, `codex-cli 0.147.0`, a bare
    `1.18.16` — so the comparison is on the dotted number, not the whole line.
    """
    executable = shutil.which(_executable(backend_name))
    output = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=60).stdout
    match = re.search(r"\d+\.\d+\.\d+\S*", output)
    assert match, f"{backend_name}: could not read a version out of {output!r}"
    return match.group(0)
