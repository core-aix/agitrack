import io
import json

from pathlib import Path

import agitrack.shell.runner as shell_mod
from agitrack.backends.base import AgentResult, TokenUsage
from agitrack.git import GitRepo
from agitrack.shell import AgitrackShell
from agitrack.shell.bridge import BridgeServer, BridgeUI


# --- transport: BridgeServer routes stdin lines to the right queue --------------


def test_bridge_server_routes_requests_and_answers_and_synthesizes_exit():
    inp = io.StringIO(
        '{"type":"prompt","text":"hi"}\n'
        '{"type":"answer","id":"ask-1","value":true}\n'
        '{"type":"command","text":":status"}\n'
    )
    server = BridgeServer(out=io.StringIO(), inp=inp)
    server.start()

    assert server.next_request()["text"] == "hi"  # prompt -> request queue
    assert server.wait_answer("ask-1") is True  # answer -> answer queue
    assert server.next_request()["text"] == ":status"  # command -> request queue
    # Closed stdin becomes a synthesized exit so the main loop always unblocks.
    assert server.next_request()["type"] == "exit"


def test_bridge_server_ignores_malformed_lines():
    inp = io.StringIO('not json\n{"type":"prompt","text":"ok"}\n')
    server = BridgeServer(out=io.StringIO(), inp=inp)
    server.start()
    assert server.next_request()["text"] == "ok"


# --- BridgeUI: each question is an `ask` and blocks for its matching answer ------


def _ui_with_answers(*answers: dict) -> tuple[BridgeUI, io.StringIO]:
    out = io.StringIO()
    server = BridgeServer(out=out, inp=io.StringIO())
    for answer in answers:
        server._answers.put({"type": "answer", **answer})
    return BridgeUI(server), out


def _events(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_select_emits_ask_and_returns_chosen_label():
    ui, out = _ui_with_answers({"id": "ask-1", "value": "Stage all"})
    assert ui.select("Stage?", ["Stage all", "Skip"], detail="files") == "Stage all"
    ask = _events(out)[0]
    assert ask["type"] == "ask" and ask["kind"] == "select"
    assert ask["options"] == ["Stage all", "Skip"]
    assert ask["detail"] == "files"


def test_multiselect_returns_list_and_filters_non_strings():
    ui, _ = _ui_with_answers({"id": "ask-1", "value": ["a.py", "b.py", 7]})
    assert ui.multiselect("Pick", ["a.py", "b.py", "c.py"]) == ["a.py", "b.py"]


def test_text_returns_none_when_cancelled():
    ui, _ = _ui_with_answers({"id": "ask-1", "value": None})
    assert ui.text("Message?") is None


def test_confirm_is_true_only_on_explicit_true():
    ui_yes, _ = _ui_with_answers({"id": "ask-1", "value": True})
    ui_no, _ = _ui_with_answers({"id": "ask-1", "value": "yes"})
    assert ui_yes.confirm("Sure?") is True
    assert ui_no.confirm("Sure?") is False


def test_wait_answer_skips_stale_answers():
    ui, _ = _ui_with_answers(
        {"id": "ask-stale", "value": "ignored"},
        {"id": "ask-1", "value": "Skip"},
    )
    assert ui.select("Stage?", ["Stage all", "Skip"]) == "Skip"


# --- end to end: the shell's bridge loop drives a turn and asks the editor -------


class FakeBackend:
    name = "claude"

    def __init__(self, repo, *, verbose=False, backend_args=None):
        self.repo = Path(repo)

    def run(self, prompt, *, model, session_id, bare=False, system_prompt=None, commit_guidance=True):
        (self.repo / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        return AgentResult(
            backend=self.name,
            session_id="ses-1",
            model="m",
            final_response="created hello.py",
            exit_code=0,
            tokens=TokenUsage(),
        )


def _bridge_shell(tmp_path, monkeypatch, stdin_lines):
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "agit-home"))
    monkeypatch.setitem(shell_mod.BACKENDS, "claude", FakeBackend)
    monkeypatch.setattr(shell_mod, "ensure_installed_backend", lambda name, *a, **k: name)
    repo = GitRepo.init(tmp_path / "demo")
    shell = AgitrackShell(repo, backend="claude", ui_bridge=True)
    shell.global_config.summarization_enabled = False
    # Swap in test-controlled stdio so the bridge reads our script and we can read events.
    out = io.StringIO()
    shell._bridge = BridgeServer(out=out, inp=io.StringIO("".join(stdin_lines)))
    shell.ui = BridgeUI(shell._bridge)
    shell.actions.ui = shell.ui
    return shell, repo, out


def test_bridge_prompt_runs_turn_asks_to_stage_and_commits(tmp_path, monkeypatch):
    shell, repo, out = _bridge_shell(
        tmp_path,
        monkeypatch,
        [
            '{"type":"prompt","text":"write hello"}\n',
            '{"type":"answer","id":"ask-1","value":"Stage all"}\n',
            '{"type":"exit"}\n',
        ],
    )

    shell.run()

    events = _events(out)
    by_type: dict[str, dict] = {}
    for event in events:
        by_type.setdefault(event["type"], event)
    # The session announces itself, asks to stage the agent's new file, replies, commits.
    assert by_type["ready"]["backend"] == "claude"
    assert by_type["ask"]["kind"] == "select"
    assert by_type["response"]["text"] == "created hello.py"
    assert by_type["commit"]["sha"]
    assert by_type["turn-complete"]
    assert by_type["bye"]
    # hello.py was actually staged and committed (the "Stage all" answer took effect).
    assert "hello.py" not in repo.untracked_files()


def test_bridge_status_command_emits_notice(tmp_path, monkeypatch):
    shell, _, out = _bridge_shell(
        tmp_path,
        monkeypatch,
        ['{"type":"command","text":":status"}\n', '{"type":"exit"}\n'],
    )

    shell.run()

    notices = [event for event in _events(out) if event["type"] == "notice"]
    assert any("clean" in n["message"].lower() for n in notices)


# --- AgitrackActions routes interactive review through the injected UI ----------


class ScriptedUI:
    """A BridgeUI-shaped stand-in whose answers are pre-scripted, for unit tests."""

    def __init__(self, *, select=None, multiselect=None, text=None, confirm=False):
        self._select = select
        self._multiselect = multiselect or []
        self._text = text
        self.confirm_value = confirm
        self.infos: list[str] = []

    def select(self, message, options, *, detail=None):
        return self._select

    def multiselect(self, message, options, *, detail=None):
        return list(self._multiselect)

    def text(self, message, *, default=""):
        return self._text

    def confirm(self, message):
        return self.confirm_value

    def info(self, message, *, level="info"):
        self.infos.append(message)


def _repo_with_untracked(tmp_path):
    from agitrack.config import AgitrackState

    repo = GitRepo.init(tmp_path)
    (repo.repo / "a.py").write_text("a", encoding="utf-8")
    (repo.repo / "b.py").write_text("b", encoding="utf-8")
    return repo, AgitrackState(repo.repo)


def test_actions_review_stage_all_via_ui(tmp_path):
    from agitrack.commits import AgitrackActions

    repo, state = _repo_with_untracked(tmp_path)
    ui = ScriptedUI(select="Stage all")
    AgitrackActions(repo, state, ui=ui).review_untracked(include_declined=False)
    assert repo.has_staged_changes()
    assert set(state.declined_untracked()) == set()


def test_actions_review_select_subset_via_ui(tmp_path):
    from agitrack.commits import AgitrackActions

    repo, state = _repo_with_untracked(tmp_path)
    ui = ScriptedUI(select="Select files…", multiselect=["a.py"])
    AgitrackActions(repo, state, ui=ui).review_untracked(include_declined=False)
    # a.py staged; b.py left unstaged and remembered as declined (not re-offered).
    assert repo.has_staged_changes()
    assert "a.py" not in repo.untracked_files()
    assert state.declined_untracked() == ["b.py"]


def test_actions_review_skip_via_ui_declines_all(tmp_path):
    from agitrack.commits import AgitrackActions

    repo, state = _repo_with_untracked(tmp_path)
    ui = ScriptedUI(select="Skip")
    AgitrackActions(repo, state, ui=ui).review_untracked(include_declined=False)
    assert not repo.has_staged_changes()
    assert set(state.declined_untracked()) == {"a.py", "b.py"}


def test_actions_user_commit_cancel_via_ui(tmp_path):
    from agitrack.commits import AgitrackActions

    repo, state = _repo_with_untracked(tmp_path)
    ui = ScriptedUI(select="Stage all", text=None)  # cancel the commit-message box
    assert AgitrackActions(repo, state, ui=ui).create_user_commit() is False


def test_bridge_lock_conflict_reports_error(tmp_path, monkeypatch):
    shell, repo, out = _bridge_shell(tmp_path, monkeypatch, ['{"type":"exit"}\n'])
    # Another instance already holds the management lock.
    from agitrack.git import RepoLock

    holder = RepoLock(repo.repo / ".agitrack" / "lock")
    assert holder.acquire() is True

    shell.run()

    types = {event["type"] for event in _events(out)}
    assert "error" in types and "bye" in types
    holder.release()
