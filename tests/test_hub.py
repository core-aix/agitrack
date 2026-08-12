"""One dashboard, one port, every repository: the registry, the routing, and which view opens.

These cover the pieces that make a single dashboard able to stand in for what used to be a daemon
per repository per view: a user-wide list of repositories, path-based mounts over the existing
scopes, and the rule that decides whether a repository opens on aGiTrack's own tracking or on the
reconstruction of its past agent sessions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agitrack import repos as repo_registry
from agitrack.git import GitRepo
from agitrack.metrics import hub


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    """Keep every test out of the developer's real ~/.agitrack/repos.json."""
    monkeypatch.setenv("AGITRACK_CONFIG_DIR", str(tmp_path / "config"))


def _repo(tmp_path: Path, name: str) -> GitRepo:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    repo = GitRepo.init(root)
    (root / "f.txt").write_text("x", encoding="utf-8")
    repo.stage_paths(["f.txt"])
    repo.commit("seed")
    return repo


# --- the repository registry --------------------------------------------------------------------


def test_a_slug_is_derived_from_the_path_not_handed_out(tmp_path):
    # Every process must compute the same URL for the same repository without asking the hub,
    # which is what makes a printed URL and a served one agree.
    first = repo_registry.slug_for(tmp_path / "alpha")
    assert first == repo_registry.slug_for(tmp_path / "alpha")
    assert first != repo_registry.slug_for(tmp_path / "beta")
    assert first.startswith("alpha-")  # readable half, so a URL is recognisable at a glance


def test_two_repos_with_the_same_name_get_different_slugs(tmp_path):
    (tmp_path / "one" / "src").mkdir(parents=True)
    (tmp_path / "two" / "src").mkdir(parents=True)
    assert repo_registry.slug_for(tmp_path / "one" / "src") != repo_registry.slug_for(tmp_path / "two" / "src")


def test_remembering_a_repo_lists_it_most_recent_first(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    repo_registry.remember(tmp_path / "a")
    repo_registry.remember(tmp_path / "b")

    listed = [entry.name for entry in repo_registry.list_repos()]
    assert listed[0] == "b" and set(listed) == {"a", "b"}


def test_a_repo_whose_directory_is_gone_is_not_offered(tmp_path):
    (tmp_path / "gone").mkdir()
    repo_registry.remember(tmp_path / "gone")
    (tmp_path / "gone").rmdir()

    # Offering a repository the dashboard cannot open is worse than not offering it.
    assert repo_registry.list_repos() == []


def test_stopping_a_repo_keeps_its_state_but_drops_it_from_the_switcher(tmp_path):
    (tmp_path / "proj").mkdir()
    repo_registry.remember(tmp_path / "proj")
    repo_registry.mark_tracked_seen(tmp_path / "proj")

    repo_registry.set_served(tmp_path / "proj", False)

    assert repo_registry.list_repos() == []
    kept = repo_registry.entry_for(tmp_path / "proj")
    # The entry survives, so the once-only view switch does not happen a second time later.
    assert kept is not None and kept.tracked_seen is True
    repo_registry.remember(tmp_path / "proj")  # working on it again puts it back
    assert [entry.name for entry in repo_registry.list_repos()] == ["proj"]


def test_keys_written_by_a_newer_agitrack_survive_a_save(tmp_path):
    # The file is shared by every version installed, so an unknown key is carried through rather
    # than dropped on the next write.
    (tmp_path / "proj").mkdir()
    repo_registry.remember(tmp_path / "proj")
    path = repo_registry.registry_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["repos"][0]["something_from_the_future"] = 7
    path.write_text(json.dumps(data), encoding="utf-8")

    repo_registry.set_view(tmp_path / "proj", repo_registry.BACKTRACE)

    reread = json.loads(path.read_text(encoding="utf-8"))
    assert reread["repos"][0]["something_from_the_future"] == 7
    assert reread["repos"][0]["view"] == "backtrace"


# --- path routing --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/r/proj-abc/", ("active", "proj-abc", "/")),
        ("/b/proj-abc/", ("backtrace", "proj-abc", "/")),
        ("/r/proj-abc/learn/state", ("active", "proj-abc", "/learn/state")),
        ("/b/proj-abc/story", ("backtrace", "proj-abc", "/story")),
    ],
)
def test_a_mount_splits_into_view_slug_and_the_scope_subpath(path, expected):
    # The subpath always starts with "/", so a scope's routing table is identical whether it is
    # mounted in the hub or serving at the root of its own daemon.
    assert hub.split_mount(path) == expected


@pytest.mark.parametrize("path", ["/", "/repos", "/x/proj-abc/", "/r//"])
def test_a_path_that_is_not_a_mount_is_not_treated_as_one(path):
    assert hub.split_mount(path) is None


def test_a_mount_url_always_carries_its_trailing_slash():
    # Load-bearing: every page fetches its data with RELATIVE urls, so "/r/<slug>" without the
    # slash would resolve them one level too high.
    assert hub.mount_path("proj-abc") == "/r/proj-abc/"
    assert hub.mount_path("proj-abc", "backtrace", "learn") == "/b/proj-abc/learn"


def test_a_mount_without_its_trailing_slash_redirects_rather_than_serving(tmp_path):
    _repo(tmp_path, "proj")
    entry = repo_registry.remember(tmp_path / "proj")
    router = hub.HubRouter()

    response = router.get(f"/r/{entry.slug}", {})

    assert response is not None and response.status == 302
    assert response.headers["Location"] == f"/r/{entry.slug}/"


def test_the_root_sends_you_to_the_repo_you_used_last(tmp_path):
    _repo(tmp_path, "older")
    _repo(tmp_path, "newer")
    repo_registry.remember(tmp_path / "older")
    entry = repo_registry.remember(tmp_path / "newer")
    router = hub.HubRouter()

    response = router.get("/", {})

    assert response.status == 302 and entry.slug in response.headers["Location"]


def test_the_root_explains_itself_when_no_repo_has_been_tracked():
    router = hub.HubRouter()

    response = router.get("/", {})

    assert response.status == 200
    body = response.body.decode("utf-8")
    assert "Nothing to show yet" in body and "agitrack -d" in body


def test_an_unknown_slug_is_a_404_that_says_what_happened(tmp_path):
    router = hub.HubRouter()

    response = router.get("/r/never-heard-of-it/", {})

    assert response.status == 404 and "agitrack stop" in response.body.decode("utf-8")


def test_the_repo_list_is_what_the_switcher_is_built_from(tmp_path):
    _repo(tmp_path, "proj")
    entry = repo_registry.remember(tmp_path / "proj")
    router = hub.HubRouter()

    payload = json.loads(router.get("/repos", {}).body)

    assert payload["repos"][0]["slug"] == entry.slug
    # Both views are offered per repo, so the switcher can keep the current one when it moves.
    assert payload["repos"][0]["active_url"] == f"/r/{entry.slug}/"
    assert payload["repos"][0]["backtrace_url"] == f"/b/{entry.slug}/"


def test_a_mounted_repo_serves_the_same_routes_it_would_serve_alone(tmp_path):
    _repo(tmp_path, "proj")
    entry = repo_registry.remember(tmp_path / "proj")
    router = hub.HubRouter()

    assert router.get(f"/r/{entry.slug}/", {}).content_type.startswith("text/html")
    data = json.loads(router.get(f"/r/{entry.slug}/data", {}).body)
    assert "tokens" in data["agg"]
    assert router.get(f"/r/{entry.slug}/learn", {}).content_type.startswith("text/html")
    assert router.get(f"/r/{entry.slug}/nonsense", {}) is None  # a 404 at the edge


# --- there is only ever one hub ------------------------------------------------------------


def test_only_one_hub_can_run_at_a_time(tmp_path, monkeypatch):
    """Two hubs is the failure this lock exists to stop, and it was not hypothetical.

    Two aGiTrack runs starting a second apart both saw no handshake, both spawned, and the port
    scan politely stepped the second around the first: two dashboards on 8765 and 8766, each
    serving a different subset of what the other had cached. The check-then-spawn window cannot be
    closed by checking harder; it needs a lock that both processes can see."""
    from agitrack.git import RepoLock

    incumbent = RepoLock(hub.lock_path())
    assert incumbent.acquire() is True
    try:
        bound: list[object] = []
        monkeypatch.setattr(
            "agitrack.metrics.server.bind_scanning",
            lambda *a, **k: bound.append(1) or pytest.fail("a second hub must never bind a port"),
        )

        # The rival stands down immediately and silently: the incumbent is already serving, so
        # there is nothing to report and nothing to retry.
        assert hub.run_hub_daemon() == 0
        assert bound == []
    finally:
        incumbent.release()


def test_the_lock_lives_next_to_the_handshake(tmp_path):
    # Both are user-wide, because the hub is: one per user, not one per repository.
    assert hub.lock_path().parent == hub.handshake_path().parent
    assert hub.lock_path() != hub.handshake_path()


# --- which view opens ------------------------------------------------------------------------


def test_a_repo_with_no_sessions_opens_on_the_live_dashboard(tmp_path, monkeypatch):
    _repo(tmp_path, "proj")
    repo_registry.remember(tmp_path / "proj")
    monkeypatch.setattr(hub, "_has_backtrace", lambda directory: False)
    monkeypatch.setattr(hub, "_has_tracked_tokens", lambda directory: False)

    assert hub.preferred_view(tmp_path / "proj") == repo_registry.ACTIVE


def test_a_repo_with_sessions_but_nothing_tracked_opens_on_the_backtrace(tmp_path, monkeypatch):
    # An empty live dashboard is the worst possible answer when the history is sitting in the
    # backends' own transcripts.
    _repo(tmp_path, "proj")
    repo_registry.remember(tmp_path / "proj")
    monkeypatch.setattr(hub, "_has_backtrace", lambda directory: True)
    monkeypatch.setattr(hub, "_has_tracked_tokens", lambda directory: False)

    assert hub.preferred_view(tmp_path / "proj") == repo_registry.BACKTRACE


def test_the_switch_to_the_live_dashboard_happens_once_and_then_never_again(tmp_path, monkeypatch):
    _repo(tmp_path, "proj")
    directory = tmp_path / "proj"
    repo_registry.remember(directory)
    monkeypatch.setattr(hub, "_has_backtrace", lambda d: True)
    monkeypatch.setattr(hub, "_has_tracked_tokens", lambda d: False)
    assert hub.preferred_view(directory) == repo_registry.BACKTRACE
    repo_registry.set_view(directory, repo_registry.BACKTRACE)

    # The first tracked commit carrying token counts flips it, unasked...
    monkeypatch.setattr(hub, "_has_tracked_tokens", lambda d: True)
    assert hub.preferred_view(directory) == repo_registry.ACTIVE
    assert repo_registry.entry_for(directory).tracked_seen is True

    # ...and after that the user's own choice stands, however many times it is asked.
    repo_registry.set_view(directory, repo_registry.BACKTRACE)
    assert hub.preferred_view(directory) == repo_registry.BACKTRACE
    assert hub.preferred_view(directory) == repo_registry.BACKTRACE


def test_opening_a_view_remembers_it(tmp_path):
    _repo(tmp_path, "proj")
    entry = repo_registry.remember(tmp_path / "proj")
    router = hub.HubRouter()

    router.get(f"/b/{entry.slug}/", {})

    assert repo_registry.entry_for(tmp_path / "proj").view == repo_registry.BACKTRACE


# --- what the live dashboard says about the other one ------------------------------------------


def test_an_empty_repo_says_there_is_nothing_to_show(tmp_path, monkeypatch):
    from agitrack.metrics.pending import empty_state

    monkeypatch.setattr("agitrack.metrics.pending._has_sessions", lambda directory: False)

    state = empty_state(tmp_path, commits=0, tracked=False)

    assert state["kind"] == "nothing" and "nothing to show" in state["text"]


def test_a_repo_with_only_manual_commits_says_it_has_no_agent_history(tmp_path, monkeypatch):
    from agitrack.metrics.pending import empty_state

    monkeypatch.setattr("agitrack.metrics.pending._has_sessions", lambda directory: False)

    state = empty_state(tmp_path, commits=4, tracked=False)

    # The commits below are the repository's real content, so they stay; what is missing is the
    # agent attribution, and that is what the message is about.
    assert state["kind"] == "no-sessions" and "No traceable coding-agent sessions" in state["text"]


def test_a_tracked_repo_gets_no_empty_state(tmp_path):
    from agitrack.metrics.pending import empty_state

    assert empty_state(tmp_path, commits=10, tracked=True) == {}


def test_sessions_with_no_tracked_commit_are_reported_as_work_worth_committing(tmp_path, monkeypatch):
    from agitrack.metrics import pending

    monkeypatch.setattr(
        pending, "_discover", lambda directory: [], raising=False
    )  # replaced below; keeps the import honest
    monkeypatch.setattr(
        "agitrack.metrics.backtrace._discover",
        lambda directory: [type("S", (), {"ref_id": "ses-1"})(), type("S", (), {"ref_id": "ses-2"})()],
    )
    monkeypatch.setattr("agitrack.metrics.backtrace._committed_anchors", lambda directory: {"ses-1": {"m1"}})

    work = pending.pending_work(tmp_path)

    assert work.sessions == 1 and not work.exact
    assert "1 past agent session" in pending.notice_text(work)


def test_a_built_reconstruction_gives_an_exact_turn_count(tmp_path):
    from agitrack.metrics import pending

    def stat(tracked, session):
        return type("Stat", (), {"tracked": tracked, "backend_session_id": session})()

    view = type(
        "View",
        (),
        {"dashboard": type("D", (), {"stats": [stat(True, "a"), stat(False, "a"), stat(False, "b")]})()},
    )()

    work = pending.pending_work(tmp_path, view)

    assert work.exact and work.turns == 2 and work.sessions == 2
    assert "2 agent turns across 2 conversations" in pending.notice_text(work)


def test_nothing_pending_says_nothing(tmp_path):
    from agitrack.metrics import pending

    assert pending.notice_text(pending.PendingWork()) == ""
