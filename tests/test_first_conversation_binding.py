"""A fresh session must not be asked to name itself twice.

`agitrack --new-session` prompts for a name at startup and then spawns the backend with no
conversation to resume — so ``state.backend_session_id`` is None until the backend writes its
first transcript, which happens on the user's FIRST PROMPT. Until that moment the native-switch
watcher is tracking nothing, and the conversation appearing looked exactly like a switch made
inside the backend: aGiTrack popped "New conversation detected" and asked for a name again, for
a session the user had named seconds earlier.

That is not a switch — there is nothing to switch away from. The first conversation to appear
IS this session's own, and it must be adopted under the name already chosen.

The adoption has to be narrow, though. Under ``--no-worktree`` the session runs in the base
directory, which can hold conversations from long before this run; "newest with content" would
happily bind to one of those. So binding uses the since-launch snapshot (the same filter the
commit path uses) and additionally requires real content, since Claude mints empty transcripts
on picker actions.

Real git, both backends.
"""

from __future__ import annotations

import subprocess

import pytest

from agitrack.config import AgitrackState, GlobalConfig
from agitrack.git import GitRepo
from agitrack.transcripts.types import SessionRef
from proxy_helpers import make_runner

BACKENDS = ["claude", "opencode"]


def _init_repo(path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    return GitRepo.discover(path)


def _fresh_runner(tmp_path, backend_name, *, refs, pre_spawn):
    """A just-started `--new-session` runner: named at startup, bound to nothing yet.

    ``pre_spawn`` is the conversation snapshot taken at spawn time — what was already in the
    directory before this run. ``refs`` is what the directory holds now.
    """
    repo = _init_repo(tmp_path)
    state = AgitrackState(repo.repo, default_backend=backend_name)
    state.backend_session_id = None  # --new-session: _apply_new_session_if_requested cleared it

    runner = make_runner(repo=repo, state=state)
    runner.base_repo = repo
    # for_testing() bypasses __init__, which is the only place global_config is built; name
    # persistence opens the repo-root state through it, so a real one is needed here.
    runner.global_config = GlobalConfig(path=tmp_path / "global.json")
    runner.name = "candle"  # the name the user gave at the startup prompt
    runner.state.data["backend"] = backend_name
    runner._pre_spawn_sessions = pre_spawn

    class _Backend:
        name = backend_name

        def list_sessions(self, _repo):
            return refs

    runner.backend = _Backend()
    runner._session_watch_at = 0.0
    runner.agent_in_flight = False
    runner.running = True
    runner._initialize_session_baseline = lambda: None
    runner._render = lambda *a, **k: None

    asked: list[str] = []
    runner._prompt_session_name = lambda title, **k: asked.append(title) or "renamed-by-prompt"
    return runner, asked


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_the_first_prompt_does_not_re_ask_for_a_session_name(tmp_path, backend_name):
    # The reported bug: name the session at startup, type the first prompt, get asked again.
    runner, asked = _fresh_runner(
        tmp_path,
        backend_name,
        refs=[SessionRef(id="ours", updated=200.0, label="my first prompt")],
        pre_spawn={},  # empty worktree: nothing existed before this run
    )

    runner._service_native_session_switch()

    assert runner.state.backend_session_id == "ours"  # bound to the conversation that appeared
    assert runner.name == "candle"  # and kept the name given at startup
    assert asked == []  # the second question is never asked


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_binding_makes_the_startup_name_durable_under_the_new_id(tmp_path, backend_name):
    # The startup name was recorded as a PENDING name, because no conversation existed to key
    # it to. Binding is the moment that becomes a real link — without it a crash before the
    # first commit leaves the conversation nameless in the resume list.
    runner, _asked = _fresh_runner(
        tmp_path,
        backend_name,
        refs=[SessionRef(id="ours", updated=200.0, label="my first prompt")],
        pre_spawn={},
    )

    runner._service_native_session_switch()

    root = AgitrackState(runner.base_repo.repo, default_backend=backend_name)
    assert root.session_name_for("ours") == "candle"


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_an_untouched_pre_existing_conversation_is_never_adopted(tmp_path, backend_name):
    # --no-worktree runs in the base directory, which can hold conversations from earlier work.
    # Before the user's first prompt those are the only thing in the listing, and "newest with
    # content" would bind the fresh session to one of them — inheriting a stranger's history.
    stale = SessionRef(id="last-week", updated=100.0, label="unrelated old work")
    runner, asked = _fresh_runner(
        tmp_path,
        backend_name,
        refs=[stale],
        pre_spawn={"last-week": 100.0},  # present at launch and untouched since
    )

    runner._service_native_session_switch()

    assert runner.state.backend_session_id is None  # stays unbound until OUR conversation shows
    assert asked == []


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_our_conversation_is_picked_out_from_among_pre_existing_ones(tmp_path, backend_name):
    # The positive half of the same guard: with old conversations sitting alongside, the one
    # written since launch is the one to bind — even though the others are perfectly valid.
    runner, asked = _fresh_runner(
        tmp_path,
        backend_name,
        refs=[
            SessionRef(id="last-week", updated=100.0, label="unrelated old work"),
            SessionRef(id="ours", updated=300.0, label="my first prompt"),
        ],
        pre_spawn={"last-week": 100.0},
    )

    runner._service_native_session_switch()

    assert runner.state.backend_session_id == "ours"
    assert runner.name == "candle"
    assert asked == []


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_an_empty_transcript_does_not_bind_the_session(tmp_path, backend_name):
    # Claude mints a fresh EMPTY transcript on picker actions. Binding to one strands the
    # session on a blank conversation — the same blank-session trap the switch path guards.
    runner, asked = _fresh_runner(
        tmp_path,
        backend_name,
        refs=[
            SessionRef(id="ours", updated=300.0, label="my first prompt"),
            SessionRef(id="blank", updated=400.0, label=None),
        ],
        pre_spawn={},
    )

    runner._service_native_session_switch()

    assert runner.state.backend_session_id == "ours"  # not "blank", despite it being newer
    assert asked == []


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_a_real_switch_later_is_followed_under_the_session_name(tmp_path, backend_name):
    # The narrow guard must not swallow the genuine case. Once the session IS bound, a
    # conversation started with the backend's own /clear is a real switch and tracking follows
    # it — keeping this session's name rather than asking for another (see
    # test_a_native_switch_to_a_NEW_conversation_keeps_the_session_name for why the question
    # went away: it was a modal, and it stalled the commit fold behind it).
    runner, asked = _fresh_runner(
        tmp_path,
        backend_name,
        refs=[SessionRef(id="ours", updated=200.0, label="my first prompt")],
        pre_spawn={},
    )
    runner._service_native_session_switch()
    assert runner.state.backend_session_id == "ours"

    runner._session_watch_at = 0.0
    linked: list = []
    runner._persist_session_name = lambda sid: linked.append(sid)
    runner.backend.list_sessions = lambda _repo: [
        SessionRef(id="ours", updated=200.0, label="my first prompt"),
        SessionRef(id="after-clear", updated=500.0, label="a brand new topic"),
    ]

    runner._service_native_session_switch()

    assert runner.state.backend_session_id == "after-clear"  # the switch IS followed
    assert asked == []  # ...silently
    assert linked == ["after-clear"]  # and the new conversation carries the session's name


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_binding_to_a_conversation_WE_created_does_not_skip_its_first_turn(tmp_path, backend_name):
    # Found live via `agent-backend` (a session created mid-run): its first turn was never
    # committed. Binding computes a resume BASELINE — "everything already recorded predates us,
    # skip it" — which is right when continuing an existing conversation. But a conversation
    # that did not exist at launch contains nothing but OUR work, and for a backend whose
    # conversation only becomes visible once the first turn FINISHES (OpenCode's session store)
    # the newest complete turn IS that first turn. It was skipped: no commit, and the file it
    # wrote was later offered back as "intentionally unstaged or git-ignored" leftovers.
    runner, _asked = _fresh_runner(
        tmp_path,
        backend_name,
        refs=[SessionRef(id="ours", updated=200.0, label="my first prompt")],
        pre_spawn={},  # nothing existed before this run
    )
    # Stand in for the real baseline, which parks the watermark on the newest complete turn.
    runner._initialize_session_baseline = lambda: setattr(runner.state, "last_backend_message_id", "msg-first-turn")

    runner._service_native_session_switch()

    assert runner.state.backend_session_id == "ours"
    assert runner.state.last_backend_message_id is None  # nothing to skip: the turn is ours


@pytest.mark.parametrize("backend_name", BACKENDS)
def test_binding_to_a_PRE_EXISTING_conversation_keeps_its_baseline(tmp_path, backend_name):
    # The other side: a conversation that was already in the directory and has since been
    # written to is one the user resumed inside the backend. Its older turns are history and
    # must NOT be re-committed, so the computed baseline stands.
    runner, _asked = _fresh_runner(
        tmp_path,
        backend_name,
        refs=[SessionRef(id="old", updated=300.0, label="resumed conversation")],
        pre_spawn={"old": 100.0},  # existed at launch, written to since
    )
    runner._initialize_session_baseline = lambda: setattr(runner.state, "last_backend_message_id", "msg-older-turn")

    runner._service_native_session_switch()

    assert runner.state.backend_session_id == "old"
    assert runner.state.last_backend_message_id == "msg-older-turn"
