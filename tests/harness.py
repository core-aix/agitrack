"""Composition harness: drive the REAL ``ProxyRunner.run()`` and reactor in a test.

Why this exists
---------------
``tests/proxy_helpers.make_runner`` builds a runner with ``ProxyRunner.for_testing()`` and
the test then calls ONE method on it. That is the right tool for testing a method and a
structurally impossible one for testing a *sequence*: ``run()`` is 250 lines of ordering
(gates → status files → resume staging → screen → spawn → watchers → hooks → reactor) and
``_loop`` is five phases that must hand off to each other correctly. Neither is exercised
by calling their callees directly, so the whole composition layer was invisible to CI —
1,825 uncovered lines in ``runner.py``, concentrated exactly there.

The bug class this catches is real and has already shipped: a fix applied to
``_new_session``'s resume path but not to the ``run()`` → ``_spawn()`` startup path. Every
unit test stayed green because each individual method was correct; only the *sequence* was
wrong. ``test_startup_stages_the_backend_resume_before_spawning`` is the guard.

How it works
------------
Real repo, real git, real ``ProxyRunner.__init__``, real ``run()``. Only the three
platform boundaries are faked, and they are faked at the SAME seam production uses — the
``agitrack.proxy.platform`` factories — so the runner cannot tell the difference:

* :class:`FakeChildProcess` for ``make_child_process`` — a real ``os.pipe()`` pair, so the
  reactor's ``select``/``os.read`` path is the production one. Records the spawn argv.
* :class:`FakeHostTerminal` for ``make_host_terminal`` — a real pipe for stdin, so
  keystrokes are delivered exactly as a terminal delivers them; records mode transitions.
* The real ``make_waker`` is kept (a self-pipe works fine headless).

Both fakes satisfy the ``runtime_checkable`` Protocols in ``proxy/platform/base.py``, and
:func:`test_fakes_satisfy_the_platform_protocols` asserts it — so a contract change breaks
the harness loudly instead of letting it drift into testing a shape production abandoned.

Nothing here sleeps on wall-clock time. The reactor is bounded by iteration count
(``stop_after``), not by a timeout, so these tests are deterministic and fast.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from agitrack.git import GitRepo


# --- real git repo ---------------------------------------------------------


def init_repo(path: Path) -> GitRepo:
    """A real git repo with one commit — the baseline every startup test needs."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


# --- platform fakes --------------------------------------------------------


class FakeChildProcess:
    """A backend child that never execs anything, over a real pipe.

    ``master_fd`` is the read end of a real ``os.pipe()``, so ``select.select`` and
    ``os.read`` behave exactly as they do against a PTY master. :meth:`emit` is the test's
    way to play the backend: whatever it writes is what the reactor drains.
    """

    def __init__(self, command: list[str], cwd: str, extra_env: dict[str, str] | None = None) -> None:
        self.command = list(command)
        self.cwd = cwd
        self.extra_env = dict(extra_env or {})
        self._read_fd, self._write_fd = os.pipe()
        self.master_fd: int | None = self._read_fd
        self.child_pid: int | None = 4242  # a pid-shaped value; nothing signals it
        self.written = bytearray()  # everything the runner sent toward the backend
        self.resizes: list[tuple[int, int]] = []
        self.interrupted = False
        self.terminated = False
        self._exit_code: int | None = None
        self._closed = False

    # -- test-facing ------------------------------------------------------
    def emit(self, data: bytes) -> None:
        """Play backend output. The reactor sees it on the next select."""
        if not self._closed:
            os.write(self._write_fd, data)

    def close_output(self) -> None:
        """EOF on the child's PTY — how the reactor learns the backend is gone."""
        if not self._closed:
            self._closed = True
            os.close(self._write_fd)

    def exit(self, code: int = 0) -> None:
        self._exit_code = code
        self.close_output()

    # -- ChildProcess contract -------------------------------------------
    def read_fileno(self) -> int | None:
        return self.master_fd

    def drain(self) -> bytes | None:
        # Mirrors BackendProcess.drain's contract exactly, including the distinction that
        # matters most: b"" means "nothing right now", None means "the child is GONE".
        # Getting that backwards fakes a backend exit, so it is worth mirroring precisely.
        if self.master_fd is None:
            return None
        os.set_blocking(self.master_fd, False)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = os.read(self.master_fd, 65536)
            except BlockingIOError:
                break
            except OSError:
                return b"".join(chunks) if chunks else None
            if not chunk:  # real EOF
                return b"".join(chunks) if chunks else None
            chunks.append(chunk)
        return b"".join(chunks)

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    def flush_input(self) -> bool:
        return False

    def pending_input(self) -> int:
        return 0

    def resize(self, rows: int, cols: int) -> None:
        self.resizes.append((rows, cols))

    def interrupt(self) -> None:
        self.interrupted = True

    def terminate(self) -> None:
        self.terminated = True
        self.exit(0)

    def cleanup(self) -> None:
        self.teardown()

    def teardown(self) -> None:
        self.close_output()
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

    def signal_exit(self) -> None:
        self.terminated = True

    def poll(self) -> int | None:
        return self._exit_code


class FakeHostTerminal:
    """The host terminal, over a real pipe instead of fd 0.

    ``stdin_fileno`` is a real pipe read end so the reactor's ``select`` on stdin is the
    production path; :meth:`type` is the test's keyboard. Mode transitions are recorded in
    :attr:`modes` so a startup test can assert the terminal was actually put into raw mode
    and restored — the two things that strand a user's shell when they regress.
    """

    def __init__(self, rows: int = 24, cols: int = 80) -> None:
        self._read_fd, self._write_fd = os.pipe()
        self.rows = rows
        self.cols = cols
        self.modes: list[str] = []
        self.stdout = bytearray()
        self.started = False
        self.stopped = False
        self._resize_pending = False

    # -- test-facing ------------------------------------------------------
    def type(self, data: bytes) -> None:
        """Deliver keystrokes to the reactor."""
        os.write(self._write_fd, data)

    def close(self) -> None:
        for fd in (self._write_fd, self._read_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    # -- HostTerminal contract -------------------------------------------
    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def set_raw(self) -> None:
        self.modes.append("raw")

    def set_cooked(self) -> None:
        self.modes.append("cooked")

    def restore_terminal(self) -> None:
        self.modes.append("restore")

    def disable_host_terminal_modes(self) -> None:
        self.modes.append("disable-modes")

    def enter_host_screen(self) -> None:
        self.modes.append("alt-screen")

    def detect_host_terminal(self, debug_fn=None) -> None:
        self.modes.append("detect")

    def pause_child_ui(self) -> None:
        self.modes.append("pause")

    def resume_child_ui(self, render_fn=None) -> None:
        self.modes.append("resume")

    def terminal_size(self) -> tuple[int, int]:
        return (self.rows, self.cols)

    def stdin_fileno(self) -> int:
        return self._read_fd

    def read_stdin(self, length: int) -> bytes:
        try:
            return os.read(self._read_fd, length)
        except OSError:
            return b""

    def write_stdout(self, data: bytes) -> None:
        self.stdout.extend(data)

    def flush_input(self) -> None:
        pass

    def consume_resize_pending(self) -> bool:
        pending, self._resize_pending = self._resize_pending, False
        return pending


# --- the harness ------------------------------------------------------------


class StartupHarness:
    """One launched ProxyRunner plus the fakes and the ordered record of what ran.

    :attr:`steps` is the point of the whole thing: an append-only log of the startup
    milestones, in the order ``run()`` actually reached them. Asserting on the ORDER is what
    catches an insertion at the wrong point, which is the failure mode unit tests cannot see.
    """

    def __init__(self, runner, host: FakeHostTerminal, steps: list[str], children: list[FakeChildProcess]) -> None:
        self.runner = runner
        self.host = host
        self.steps = steps
        self.children = children
        self.exit_code: int | None = None
        # Every keypress the scripted human gave a modal, in order — so a test can assert
        # "exactly one popup was raised" or "nothing asked the user anything".
        self.modal_answers: list[bytes] = []

    @property
    def child(self) -> FakeChildProcess:
        """The backend child spawned at startup."""
        assert self.children, "no backend child was spawned"
        return self.children[0]

    @property
    def spawn_command(self) -> list[str]:
        return self.child.command

    def index_of(self, step: str) -> int:
        assert step in self.steps, f"{step!r} never ran; steps were {self.steps}"
        return self.steps.index(step)

    def ran_before(self, first: str, second: str) -> bool:
        return self.index_of(first) < self.index_of(second)


def launch(
    tmp_path: Path,
    monkeypatch,
    *,
    repo: GitRepo | None = None,
    reactor_iterations: int = 0,
    script=None,
    on_spawn=None,
    answers: list[bytes] | None = None,
    **runner_kwargs,
):
    """Run the REAL ``ProxyRunner.run()`` against ``tmp_path`` and return a StartupHarness.

    ``reactor_iterations`` = 0 (the default) stubs ``_loop`` so the run stops at the reactor
    door: the whole startup sequence has executed for real and nothing is left running.
    A positive value instead drives that many real ``_loop`` iterations — all five phases,
    real ``select``, real drain — then stops the loop from inside the last phase, so the
    test is bounded by iterations rather than by a clock.

    ``script(harness)`` is invoked once, after the child is spawned and before the reactor
    runs, so a test can queue backend output or keystrokes for the loop to pick up.

    ``on_spawn(child)`` is invoked for EVERY spawned child, including the ones a relaunch
    creates — that is how a test scripts a backend that keeps dying (the crash-loop guard).

    ``answers`` is the scripted human: each modal popup consumes one entry, and anything
    unanswered gets Esc (every modal's documented default). This is required, not optional —
    a modal blocks the reactor until a key arrives, so an unanswered one hangs the test
    forever. It replaces only the KEYPRESS: ``_select_popup``, ``_run_modal`` and the modal
    state machines all still run for real.
    """
    from agitrack.proxy import runner as runner_module
    from agitrack.proxy.runner import ProxyRunner

    repo = repo if repo is not None else init_repo(tmp_path)

    steps: list[str] = []
    children: list[FakeChildProcess] = []
    host = FakeHostTerminal()

    def _make_child(command, cwd, extra_env=None):
        steps.append("spawn")
        child = FakeChildProcess(command, cwd, extra_env)
        children.append(child)
        if on_spawn is not None:
            on_spawn(child)
        return child

    monkeypatch.setattr(runner_module, "make_child_process", _make_child)
    monkeypatch.setattr(runner_module, "make_host_terminal", lambda _owner: host)
    # A TTY is a hard gate at the top of run(); nothing below it touches fd 0 directly
    # (everything goes through the host terminal), so asserting the gate is enough.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

    runner_kwargs.setdefault("backend", "claude")
    runner_kwargs.setdefault("skip_privacy_ack", True)  # the real ack path, told not to prompt
    runner_kwargs.setdefault("sandbox", False)  # no sandbox-exec wrapper around the fake child
    runner = ProxyRunner(repo, **runner_kwargs)

    # The backend binary is not installed in CI and we never exec it anyway; the gate's own
    # behaviour is covered by test_backend_setup.py.
    monkeypatch.setattr(runner, "_ensure_backend_available", lambda: True)

    # Collapse the reactor's select timeouts. These tests assert SEQUENCING, never timing, so
    # a 1s active / 30s idle block would only add dead wall-clock — and an iteration-bounded
    # loop would take a minute to prove something that is true immediately. Every background
    # sweep still self-throttles on its own interval, so shrinking the poll changes nothing
    # about what runs, only how long the reactor sits in select waiting for it.
    runner.ACTIVE_POLL_SECONDS = 0.01
    runner.IDLE_POLL_SECONDS = 0.01

    # Stand in for the human at the keyboard whenever a modal asks something. Only the
    # keypress is faked; the modal itself runs for real. Without this the first popup the
    # timers phase raises (e.g. the new-worktree "copy the full environment?" offer) blocks
    # the reactor on stdin forever — which is correct in production, where a user is sitting
    # there, and a deadlock in a test.
    queued = list(answers or [])
    modal_answers: list[bytes] = []

    def _scripted_keypress() -> bytes:
        key = queued.pop(0) if queued else b"\x1b"  # Esc = every modal's documented default
        modal_answers.append(key)
        return key

    monkeypatch.setattr(runner, "_popup_read_input", _scripted_keypress)

    # Record the startup milestones we want to assert ordering on, each wrapping the REAL
    # method so behaviour is unchanged — this is observation, not substitution.
    def _record(name: str, bound):
        def wrapper(*args, **kwargs):
            steps.append(name)
            return bound(*args, **kwargs)

        return wrapper

    for name, attr in (
        ("stage-resume", "_stage_backend_resume"),
        ("init-screen", "_init_screen"),
        ("file-watcher", "_start_file_watcher"),
        ("git-worker", "_start_git_worker"),
        ("reconcile", "_reconcile_sessions_on_startup"),
        ("base-guard", "_install_base_commit_guard"),
        ("manual-mode", "_setup_manual_commit_mode"),
        ("render", "_render"),
    ):
        monkeypatch.setattr(runner, attr, _record(name, getattr(runner, attr)))

    if reactor_iterations <= 0:
        monkeypatch.setattr(runner, "_loop", lambda: steps.append("loop") or 0)
    else:
        remaining = {"n": reactor_iterations}
        real_child_exit_phase = runner._reactor_child_exit_phase

        def _bounded_child_exit_phase():
            steps.append("loop-iteration")
            sentinel = real_child_exit_phase()
            remaining["n"] -= 1
            if remaining["n"] <= 0:
                runner.running = False  # leave the loop cleanly from its last phase
            return sentinel

        monkeypatch.setattr(runner, "_reactor_child_exit_phase", _bounded_child_exit_phase)

    harness = StartupHarness(runner, host, steps, children)
    harness.modal_answers = modal_answers

    if script is not None:
        # The script needs the spawned child, which only exists once _spawn has run — so
        # hook the render that immediately follows it rather than pre-seeding blindly.
        real_render = runner._render

        def _render_once(*args, **kwargs):
            if children and not getattr(_render_once, "fired", False):
                _render_once.fired = True  # type: ignore[attr-defined]
                script(harness)
            return real_render(*args, **kwargs)

        monkeypatch.setattr(runner, "_render", _record("render", _render_once))

    try:
        harness.exit_code = runner.run()
    finally:
        host.close()
        for child in children:
            child.teardown()
    return harness
