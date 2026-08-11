import json
import os
import subprocess
from pathlib import Path

import agitrack.shell.runner as shell_mod
from agitrack.backends.base import AgentResult, TokenUsage
from agitrack.backends.setup import BackendUnavailable
from agitrack.commits import AgitrackActions
from agitrack.git import GitRepo
from agitrack.git import RepoLock
from agitrack.shell import AgitrackShell
from agitrack.config import AgitrackState


def test_declined_untracked_files_do_not_count_as_pre_agent_changes(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    (tmp_path / "declined.txt").write_text("declined", encoding="utf-8")
    repo = GitRepo.discover(tmp_path)
    state = AgitrackState(repo.repo)
    state.add_declined(["declined.txt"])

    shell = AgitrackShell(repo)

    assert shell.actions.has_pre_agent_user_changes() is False


def test_new_promptable_untracked_files_count_as_pre_agent_changes(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    (tmp_path / "new.txt").write_text("new", encoding="utf-8")

    shell = AgitrackShell(GitRepo.discover(tmp_path))

    assert shell.actions.has_pre_agent_user_changes() is True


def test_second_instance_is_refused(tmp_path, monkeypatch, capsys):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    repo = GitRepo.discover(tmp_path)
    shell = AgitrackShell(repo, backend="claude")
    monkeypatch.setattr(shell_mod, "ensure_installed_backend", lambda *a, **k: shell.state.backend)
    # Another live aGiTrack already holds this repo's management lock.
    holder = RepoLock(repo.repo / ".agitrack" / "lock")
    assert holder.acquire() is True
    # The prompt loop must never start when the repo is already taken.
    shell.prompt = type("P", (), {"prompt": lambda self: (_ for _ in ()).throw(AssertionError("should not prompt"))})()

    shell.run()

    out = capsys.readouterr().out
    assert "already running" in out
    assert str(os.getpid()) in out  # names the holding process's PID
    holder.release()


# --- scripted prompts: `agit --prompt ...` (#53) ------------------------------


class FakeBackend:
    """Headless backend stand-in: every prompt writes one file and succeeds."""

    name = "claude"
    runs: list[str] = []

    def __init__(self, repo, *, verbose=False, backend_args=None, launch_command=None):
        self.repo = Path(repo)
        self.launch_command = list(launch_command or [])

    def run(self, prompt, *, model, session_id, bare=False, system_prompt=None, commit_guidance=True):
        FakeBackend.runs.append(prompt)
        (self.repo / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        return AgentResult(
            backend=self.name,
            session_id="ses-1",
            model="m",
            final_response="created hello.py",
            exit_code=0,
            tokens=TokenUsage(),
        )


def _no_input(monkeypatch):
    monkeypatch.setattr(
        "builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("scripted mode must not prompt"))
    )


def _scripted_shell(tmp_path, monkeypatch, prompts):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-home"))
    FakeBackend.runs = []
    monkeypatch.setitem(shell_mod.BACKENDS, "claude", FakeBackend)
    monkeypatch.setattr(shell_mod, "ensure_installed_backend", lambda name, *a, **k: name)
    _no_input(monkeypatch)
    repo = GitRepo.init(tmp_path / "demo")
    shell = AgitrackShell(repo, backend="claude", prompts=prompts)
    # Disable summarization for scripted tests via the durable GLOBAL config (the source
    # of truth), so commits stay prompt-led rather than going through the summarizer.
    shell.global_config.summarization_enabled = False
    return shell, repo


def test_scripted_prompts_commit_without_any_input(tmp_path, monkeypatch, capsys):
    shell, repo = _scripted_shell(tmp_path, monkeypatch, ["write hello.py", ":status"])

    shell.run()

    out = capsys.readouterr().out
    # The new file was auto-staged (no interactive review) and committed.
    assert "Staged untracked files: hello.py" in out
    assert "Created <aGiTrack> commit." in out
    log = subprocess.run(
        ["git", "-C", str(repo.repo), "log", "-1", "--format=%B"], capture_output=True, text=True
    ).stdout
    assert log.startswith("<aGiTrack> write hello.py")
    assert FakeBackend.runs == ["write hello.py"]  # ':status' is aGiTrack's, not the agent's


def test_json_events_emit_response_and_commit(tmp_path, monkeypatch, capsys):
    # --json-events makes the headless shell emit one machine-readable JSON line per
    # turn event, which the VSCode chat extension parses.
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-home"))
    FakeBackend.runs = []
    monkeypatch.setitem(shell_mod.BACKENDS, "claude", FakeBackend)
    monkeypatch.setattr(shell_mod, "ensure_installed_backend", lambda name, *a, **k: name)
    _no_input(monkeypatch)
    repo = GitRepo.init(tmp_path / "demo")
    shell = AgitrackShell(repo, backend="claude", prompts=["write hello.py"], json_events=True)
    shell.global_config.summarization_enabled = False

    shell.run()

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    by_type = {e["type"]: e for e in events}
    assert by_type["response"]["text"] == "created hello.py"
    assert by_type["response"]["session"] == "ses-1"
    assert by_type["commit"]["sha"]  # a commit sha was recorded


def test_json_events_off_by_default_keeps_plain_output(tmp_path, monkeypatch, capsys):
    shell, _ = _scripted_shell(tmp_path, monkeypatch, ["write hello.py"])
    shell.run()
    out = capsys.readouterr().out
    # No JSON event lines when the flag is off.
    assert not any(line.startswith('{"type"') for line in out.splitlines())


def test_scripted_exit_command_stops_the_script(tmp_path, monkeypatch):
    shell, _ = _scripted_shell(tmp_path, monkeypatch, [":exit", "never reaches the agent"])

    shell.run()

    assert FakeBackend.runs == []


def test_scripted_run_releases_the_repo_lock(tmp_path, monkeypatch):
    shell, repo = _scripted_shell(tmp_path, monkeypatch, ["write hello.py"])

    shell.run()

    assert RepoLock(repo.repo / ".agitrack" / "lock").acquire() is True


# --- non-interactive actions fall back to safe defaults (#53) -----------------


def test_non_interactive_untracked_review_stages_everything(tmp_path, monkeypatch):
    repo = GitRepo.init(tmp_path)
    (tmp_path / "new.txt").write_text("x", encoding="utf-8")
    _no_input(monkeypatch)

    actions = AgitrackActions(repo, AgitrackState(repo.repo), interactive=False)
    actions.review_untracked(include_declined=False)

    assert repo.has_staged_changes()


def test_non_interactive_user_commit_uses_default_message(tmp_path, monkeypatch):
    repo = GitRepo.init(tmp_path)
    (tmp_path / "new.txt").write_text("x", encoding="utf-8")
    _no_input(monkeypatch)

    actions = AgitrackActions(repo, AgitrackState(repo.repo), interactive=False)
    assert actions.create_user_commit() is True

    log = subprocess.run(
        ["git", "-C", str(repo.repo), "log", "-1", "--format=%B"], capture_output=True, text=True
    ).stdout
    assert log.startswith("Save user changes")


# --- switching backends must not carry the old backend's model over ------------------------


def test_switching_backends_drops_the_previous_backends_model(tmp_path, monkeypatch):
    # `state.model` is the model the LAST turn ran under. Carried across a `--backend` switch it
    # made the new backend's commits claim the old backend's model — a live run produced
    # `backend: codex, model: claude-haiku-4-5-20251001`, and the dashboard grouped the Codex
    # commit under a Claude model. It is also what aGiTrack re-pins on the command line, so the
    # switched-to CLI is asked for a model id it does not have.
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-home"))
    monkeypatch.setattr(shell_mod, "ensure_installed_backend", lambda name, *a, **k: name)
    repo = GitRepo.init(tmp_path / "demo")
    state = AgitrackState(repo.repo)
    state.backend = "claude"
    state.model = "claude-haiku-4-5-20251001"

    shell = AgitrackShell(repo, backend="codex")

    assert shell.state.backend == "codex"
    assert shell.state.model is None


def test_switching_back_restores_the_model_that_backend_last_ran(tmp_path, monkeypatch):
    # Dropping is the fallback, not the goal: when aGiTrack recorded what this backend last ran
    # under, switching back must restore it rather than forget it.
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-home"))
    monkeypatch.setattr(shell_mod, "ensure_installed_backend", lambda name, *a, **k: name)
    repo = GitRepo.init(tmp_path / "demo")
    state = AgitrackState(repo.repo)
    state.backend = "claude"
    state.model = "claude-haiku-4-5-20251001"
    state.remember_session("codex", session_id="019f-abc", worktree="", model="gpt-5.4-mini")

    shell = AgitrackShell(repo, backend="codex")

    assert shell.state.model == "gpt-5.4-mini"


# --- exit codes: a scripted run must be able to tell "did not start" from "ran" -------------


def test_a_missing_backend_exits_non_zero(tmp_path, monkeypatch, capsys):
    # `agitrack --json --prompt ...` is the scripted entry point. With the backend CLI absent it
    # printed an install hint and exited 0 — indistinguishable, to the calling script, from a
    # turn that ran and committed.
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-home"))

    def _unavailable(*_a, **_k):
        raise BackendUnavailable("Backend 'codex' is not installed.")

    monkeypatch.setattr(shell_mod, "ensure_installed_backend", _unavailable)
    repo = GitRepo.init(tmp_path / "demo")

    assert AgitrackShell(repo, backend="codex", prompts=["do a thing"]).run() == 1
    assert "not installed" in capsys.readouterr().out


def test_a_repo_already_being_tracked_exits_non_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-home"))
    monkeypatch.setattr(shell_mod, "ensure_installed_backend", lambda name, *a, **k: name)
    repo = GitRepo.init(tmp_path / "demo")
    holder = RepoLock(repo.repo / ".agitrack" / "lock")
    assert holder.acquire() is True
    try:
        assert AgitrackShell(repo, backend="claude", prompts=["do a thing"]).run() == 1
    finally:
        holder.release()


def test_a_scripted_run_that_worked_exits_zero(tmp_path, monkeypatch):
    shell, _repo = _scripted_shell(tmp_path, monkeypatch, ["write hello.py"])

    assert shell.run() == 0


# --- a refused run must leave the owner's state alone --------------------------------------
# AgitrackShell prepared its startup state (backend switch, --new-session, the conversation
# pointer and the committed watermark) in __init__ — BEFORE run() takes the repo lock. On a
# repo already owned by a background tracker or a live TUI, `agitrack --json --prompt …`
# therefore rewrote that process's state.json and minted a fresh agitrack session id, printed
# "already running", and exited: a lost update from a run that never started.


def test_a_refused_shell_run_does_not_touch_the_owners_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-home"))
    monkeypatch.setattr(shell_mod, "ensure_installed_backend", lambda name, *a, **k: name)
    repo = GitRepo.init(tmp_path / "demo")
    owner = AgitrackState(repo.repo)
    owner.backend = "claude"
    owner.backend_session_id = "live-conversation"
    owner.last_backend_message_id = "msg-42"
    owner.name_session("live-conversation", "aurora")
    before = (repo.repo / ".agitrack" / "state.json").read_text(encoding="utf-8")

    holder = RepoLock(repo.repo / ".agitrack" / "lock")
    assert holder.acquire() is True
    try:
        assert AgitrackShell(repo, backend="codex", new_session=True, prompts=["do a thing"]).run() == 1
    finally:
        holder.release()

    assert (repo.repo / ".agitrack" / "state.json").read_text(encoding="utf-8") == before
    reloaded = AgitrackState(repo.repo)
    assert reloaded.backend == "claude"
    assert reloaded.backend_session_id == "live-conversation"
    assert reloaded.last_backend_message_id == "msg-42"
    assert reloaded.session_name_for("live-conversation") == "aurora"


def test_a_refused_shell_run_does_not_change_the_global_default_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-home"))
    monkeypatch.setattr(shell_mod, "ensure_installed_backend", lambda name, *a, **k: name)
    repo = GitRepo.init(tmp_path / "demo")
    AgitrackState(repo.repo).backend = "claude"
    from agitrack.config import GlobalConfig

    GlobalConfig().default_backend = "claude"

    holder = RepoLock(repo.repo / ".agitrack" / "lock")
    assert holder.acquire() is True
    try:
        assert AgitrackShell(repo, backend="codex", prompts=["do a thing"]).run() == 1
    finally:
        holder.release()

    assert GlobalConfig().default_backend == "claude"


def test_a_shell_run_that_starts_does_persist_the_switch(tmp_path, monkeypatch):
    # The other half of the contract: once the lock IS held, everything __init__ decided
    # must land on disk exactly as before.
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-home"))
    monkeypatch.setattr(shell_mod, "ensure_installed_backend", lambda name, *a, **k: name)
    repo = GitRepo.init(tmp_path / "demo")
    AgitrackState(repo.repo).backend = "claude"

    assert AgitrackShell(repo, backend="codex", prompts=[]).run() == 0

    from agitrack.config import GlobalConfig

    assert AgitrackState(repo.repo).backend == "codex"
    assert GlobalConfig().default_backend == "codex"


def test_json_events_stdout_carries_only_json(tmp_path, monkeypatch, capsys):
    """C38: the interactive prompt writes "> " with NO newline, so under --json-events the next
    event came out glued to it as `> {"type": "response", …}` — the one event carrying the
    agent's answer, and the only unparseable line (json-ok=1, non-json=7). codex and opencode
    escaped it only because their streamed reply happened to terminate the line first. The
    privacy banner, the prompt echo, "Staged untracked files:" and "aGiTrack is summarizing…"
    were interleaved into the same stream."""
    import json

    from agitrack.shell.runner import AgitrackShell

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    repo = GitRepo.discover(tmp_path)
    shell = AgitrackShell(repo, json_events=True, prompts=["hello"])
    shell._emit({"type": "response", "text": "hi"})
    shell._say("a human-readable notice")
    print("> echoed prompt", file=shell._human)

    captured = capsys.readouterr()
    for line in captured.out.splitlines():
        if line.strip():
            json.loads(line)  # every stdout line must parse
    assert "a human-readable notice" in captured.err
    assert "> echoed prompt" in captured.err


def test_without_json_events_human_output_stays_on_stdout(tmp_path, capsys):
    """The default shell must be unchanged: prose on stdout, exactly as before."""
    from agitrack.shell.runner import AgitrackShell

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE)
    repo = GitRepo.discover(tmp_path)
    shell = AgitrackShell(repo)
    shell._say("a human-readable notice")

    captured = capsys.readouterr()
    assert "a human-readable notice" in captured.out
    assert captured.err == ""


def test_a_prompt_run_prints_the_agents_reply(tmp_path, monkeypatch, capsys):
    """C37: `_emit` is a no-op without --json-events, so a plain `agitrack --prompt "…"` showed
    the privacy banner and the echoed prompt and then NOTHING — the agent's answer appeared
    nowhere on screen (it went only into state.json and the commit trace). On claude that meant
    a turn whose entire content was "I need permission to create the file" looked like a silent
    success."""
    shell, _repo = _scripted_shell(tmp_path, monkeypatch, ["do the thing"])
    shell.run()

    assert "created hello.py" in capsys.readouterr().out


def test_a_turn_that_changed_nothing_says_so_without_verbose(tmp_path, monkeypatch, capsys):
    """ "No code changes" used to print only under --verbose, so a declined permission, a refusal
    and a real success were indistinguishable from the outside — all silence, all exit 0."""

    class NoOpBackend(FakeBackend):
        def run(self, prompt, *, model, session_id, bare=False, system_prompt=None, commit_guidance=True):
            return AgentResult(
                backend=self.name,
                session_id="ses-1",
                model="m",
                final_response="I need permission to create the file.",
                exit_code=0,
                tokens=TokenUsage(),
            )

    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-home"))
    monkeypatch.setitem(shell_mod.BACKENDS, "claude", NoOpBackend)
    monkeypatch.setattr(shell_mod, "ensure_installed_backend", lambda name, *a, **k: name)
    _no_input(monkeypatch)
    repo = GitRepo.init(tmp_path / "demo")
    AgitrackShell(repo, backend="claude", prompts=["do the thing"]).run()

    out = capsys.readouterr().out
    assert "No code changes were made" in out
    assert "I need permission to create the file." in out


def test_claude_headless_gets_a_permission_mode_for_coding_runs(tmp_path):
    """Out of the box `claude -p` has no terminal to approve a Write/Edit on and no permission
    flag was ever passed, so every edit auto-declined: the turn spent real tokens, produced
    nothing, and exited 0 through an invisible `no_changes`."""
    from agitrack.backends.claude import ClaudeBackend

    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = '{"result": "ok", "session_id": "s"}'
        stderr = ""

    backend = ClaudeBackend(tmp_path)
    import agitrack.backends.claude as claude_mod

    original = claude_mod.subprocess.run

    def _spy(command, **kwargs):
        captured["command"] = command
        return _Proc()

    claude_mod.subprocess.run = _spy
    try:
        backend.run("do it", model=None, session_id=None)
        coding = list(captured["command"])
        backend.run("summarize", model=None, session_id=None, bare=True, system_prompt="be brief")
        summarizing = list(captured["command"])
    finally:
        claude_mod.subprocess.run = original

    assert "--permission-mode" in coding and "acceptEdits" in coding
    # The summarizer must never be allowed to touch files.
    assert "--permission-mode" not in summarizing


def test_a_user_supplied_permission_mode_wins(tmp_path):
    from agitrack.backends.claude import ClaudeBackend
    import agitrack.backends.claude as claude_mod

    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = '{"result": "ok", "session_id": "s"}'
        stderr = ""

    backend = ClaudeBackend(tmp_path, backend_args=["--permission-mode", "plan"])
    original = claude_mod.subprocess.run
    claude_mod.subprocess.run = lambda command, **kw: (captured.update(command=command), _Proc())[1]
    try:
        backend.run("do it", model=None, session_id=None)
    finally:
        claude_mod.subprocess.run = original

    assert captured["command"].count("--permission-mode") == 1
    assert "acceptEdits" not in captured["command"]
