"""Shared test fixtures for the agit test suite (#29, P7).

The ``make_runner`` factory lives in ``tests/proxy_helpers.py`` (importable
as ``proxy_helpers`` on the pytest sys.path); this file also re-exports it
for convenience.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Put the REPO ROOT on sys.path so `from tests.test_dashboard import ...` resolves.
# pytest only adds `tests/` itself (the first dir without an __init__.py), so several modules
# that share fixtures via `tests.<module>` imports were relying on the repo root arriving by
# accident — through whichever editable-install layout the environment happened to use. That
# is not a property of this repo, so reinstalling the package (a plain `uv sync`) silently
# broke collection of five test modules. Anchoring it here makes the suite independent of how
# aGiTrack is installed.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from proxy_helpers import make_runner as _make_runner  # noqa: E402,F401 – re-exported below


@pytest.fixture(scope="session", autouse=True)
def _keep_console_windows_off_the_desktop():
    """Windows: stop the SUITE's own subprocesses from flashing console windows.

    These tests are real-git by policy, so a full run spawns git a few thousand times. Under a
    console-less parent — a CI runner, or any harness that pipes the output — Windows gives
    every one of those children a console window of its own, and running the tests carpet-bombs
    the desktop with windows that appear and vanish. Product code avoids this with
    ``proc.console_isolation_kwargs()``; 200-odd direct spawns across the suite's own helpers
    cannot each be relied on to remember, so it is patched in once, here, at the one place they
    all pass through.

    This does NOT paper over a missing flag in PRODUCT code: that is enforced separately and
    statically by ``tests/test_console_isolation.py``, which reads the source rather than the
    runtime, so a product spawn that forgets still fails the suite.
    """
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name != "nt" or not no_window:
        yield
        return
    original_init = subprocess.Popen.__init__

    def _init(self, *args, **kwargs):
        # Only when the caller expressed no preference: a spawn that sets creationflags or
        # startupinfo (detach_kwargs, the console-sharing self-update re-exec) means it.
        if not kwargs.get("creationflags") and not kwargs.get("startupinfo"):
            kwargs["creationflags"] = no_window
        return original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _init
    try:
        yield
    finally:
        subprocess.Popen.__init__ = original_init


@pytest.fixture(autouse=True)
def _isolate_global_config(tmp_path_factory, monkeypatch):
    """Point aGiTrack's global config at an isolated empty dir for every test.

    Otherwise tests read the developer's real ``~/.agitrack/config.json`` — whose
    ``default_backend`` masks tests that should fail without one — so the suite
    passed locally but broke in CI (empty config, no default backend). Isolating it
    makes the suite behave the same on every machine and in CI. Tests that need a
    specific config still set ``AGITRACK_CONFIG_DIR`` themselves (that wins, as it
    runs after this fixture)."""
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path_factory.mktemp("agitrack-config")))


@pytest.fixture(autouse=True)
def _never_touch_real_daemons(monkeypatch):
    """No test may see (or signal!) the developer's real daemons.

    ``daemons.list_running`` combines the registry (isolated via AGITRACK_CONFIG_DIR
    above) with an OS PROCESS-TABLE SCAN — so a test that reaches ``restart_all()``
    (e.g. through ``restart_agitrack``) would SIGTERM the developer's live dashboards
    on every run, exactly as it silently did before this guard. Tests of the scan
    itself bind the real function at import time (see test_daemons.py).

    Belt and braces since the scan itself now stands down under a custom
    AGITRACK_CONFIG_DIR — which is the real fix, because this fixture only ever
    protected the SUITE. Users running under an isolated config dir (a CI job, one
    slot of a parallel test run) had no such guard, and `--daemons stop` reached
    across the whole machine for them."""
    from agitrack import daemons

    monkeypatch.setattr(daemons, "_scan_daemon_processes", lambda: [])


# PIDs of daemons tests started, collected as each test ends and killed ONCE when the session
# does. Collect-now-kill-later on purpose: killing during the run means signalling a process
# while another test may still be inside a call that waits on it, and doing that per-test hung a
# full run at 98% with a reader thread blocked forever on a pipe the dead daemon's children still
# held open. The leak this exists to fix is daemons surviving the whole RUN, so session end is
# both sufficient and the only safe moment.
_LEAKED_DAEMON_PIDS: set[int] = set()


def collect_registered_daemons(config_dir: str | os.PathLike[str]) -> list[int]:
    """Note (and de-register) every daemon recorded under ``config_dir``. Returns their pids.

    Tests that exercise `-b` spawn REAL detached daemons and nothing stopped them: a full suite
    run left 60 alive on Windows and several on macOS, still polling long-deleted
    `pytest-of-…/pytest-NNN` temp dirs, indefinitely. Scoped by config dir — which the autouse
    isolation fixture gives every test its own — so this can only ever see what the tests
    started, never the developer's own daemons.
    """
    import json

    # os.path, not pathlib: a test that stages `os.name = "nt"` turns Path into WindowsPath,
    # which cannot be instantiated here — and teardown must never be the thing that fails.
    registry = os.path.join(str(config_dir), "daemons")
    if not os.path.isdir(registry):
        return []
    found: list[int] = []
    for name in sorted(os.listdir(registry)):
        if not name.endswith(".json"):
            continue
        entry = os.path.join(registry, name)
        try:
            with open(entry, encoding="utf-8") as handle:
                pid = json.load(handle).get("pid")
        except (OSError, ValueError):
            pid = None
        if isinstance(pid, int) and pid != os.getpid():
            found.append(pid)
        try:
            os.unlink(entry)
        except OSError:
            pass
    return found


def reap_daemon_pids(pids) -> int:
    """SIGTERM then SIGKILL each pid, and reap it. Returns how many were still alive.

    The whole GROUP is signalled: aGiTrack detaches daemons with ``start_new_session=True``, so
    each is its own group leader, and killing only the leader leaves its children holding the
    write end of a pipe someone may be reading. Guarded so it can never reach the test runner's
    own group.

    Every pid is checked against the process table FIRST — the same proof-of-identity the
    product's own signalling paths require (``daemons._signal_targets``, the PID-reuse fix).
    A registry entry outlives the process that wrote it, and Windows reassigns PIDs briskly, so
    "the registry says this number was a daemon" is not evidence that it still is. Under xdist
    each worker runs this teardown when IT finishes, while its siblings are still working — so
    an unverified kill here reaches another worker, which is exactly what
    ``[gw0] node down: Not properly terminated`` at 97% was: the run died with no summary, and
    the coverage total for the whole session went with it."""
    import signal
    import time

    from agitrack import daemons

    verified = daemons._live_agitrack_pids()
    if verified is not None:
        pids = [pid for pid in pids if pid in verified]

    killed = 0
    for pid in sorted(set(pids)):
        for sig in (getattr(signal, "SIGTERM", 15), getattr(signal, "SIGKILL", 9)):
            try:
                group = os.getpgid(pid)
            except (OSError, AttributeError):
                group = None
            try:
                mine = os.getpgid(0)
            except (OSError, AttributeError):
                mine = None
            try:
                if group is not None and group != mine and hasattr(os, "killpg"):
                    os.killpg(group, sig)
                else:
                    os.kill(pid, sig)
            except OSError:
                break  # already gone
            killed += 1
            time.sleep(0.05)
            try:
                os.kill(pid, 0)
            except OSError:
                break
        # Reap the zombie if it was our child. POSIX only: `os.WNOHANG` does not exist on
        # Windows, and there are no zombies there — a terminated process is gone once its
        # handles close. Guarded with hasattr rather than caught, because AttributeError is
        # raised while building the ARGUMENTS, so the `except (OSError, ChildProcessError)`
        # below never saw it: every Windows run raised out of this loop on the FIRST pid,
        # leaving every daemon after it alive (the 60-alive-on-Windows leak this function
        # exists to fix), and taking the session-teardown fixture down with it — which is
        # what made whole runs end without a summary and their coverage totals swing by ten
        # points between identical runs.
        if hasattr(os, "WNOHANG"):
            try:
                os.waitpid(pid, os.WNOHANG)
            except (OSError, ChildProcessError):
                pass
    return killed


@pytest.fixture(autouse=True)
def _note_daemons_a_test_started():
    """Record (and de-register) any daemon this test leaves behind; the session kills them.

    Defined after the config-dir fixture so ``AGITRACK_CONFIG_DIR`` is still the test's own when
    this tears down."""
    yield
    from agitrack.env import getenv_compat

    config_dir = getenv_compat("CONFIG_DIR")
    if config_dir:
        _LEAKED_DAEMON_PIDS.update(collect_registered_daemons(config_dir))


@pytest.fixture(scope="session", autouse=True)
def _kill_leaked_daemons_at_the_end():
    """One sweep, after every test has finished. See ``_LEAKED_DAEMON_PIDS``."""
    yield
    reap_daemon_pids(_LEAKED_DAEMON_PIDS)


@pytest.fixture(autouse=True)
def _never_really_self_update(monkeypatch):
    """No test may install an aGiTrack update for real.

    ``attempt_self_update`` is a genuine side effect: on a source install it fetches and
    MERGES the checkout aGiTrack is running from. A test that started the daemon's watcher
    thread did exactly that in CI — the merge pulled the release commit into the job's own
    checkout mid-run, so ``pyproject.toml`` said 0.5.18 while the already-imported
    ``agitrack.__version__`` still said 0.5.17, and the version test failed. The watcher
    now opts in rather than defaulting on; this is the second line of defence, so no future
    caller can reach the real updater from a test. Tests of the self-updater itself stub
    ``Updater`` and call the module directly."""
    from agitrack.update import selfupdate

    monkeypatch.setattr(
        selfupdate, "attempt_self_update", lambda **kwargs: selfupdate.SelfUpdateRecord(), raising=False
    )


@pytest.fixture
def runner_factory():
    """Pytest fixture providing the make_runner factory."""
    return _make_runner


# Re-export so callers can do: from conftest import make_runner
make_runner = _make_runner
