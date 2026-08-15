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


def test_a_repo_with_a_live_tracker_is_offered_even_if_nothing_remembered_it(tmp_path, monkeypatch):
    # THE BUG: `repos.remember` was reachable only through `ensure_hub_for`, so a tracker that
    # opened no dashboard — the autotrack hook, a scripted or non-TTY start, or
    # `open_dashboard_on_start` off — never got an entry and its repo was missing from the
    # switcher for as long as it ran (seen live: a tracker running for hours on a repo the
    # dropdown did not list). A repository aGiTrack is demonstrably tracking is always offered.
    _repo(tmp_path, "headless")
    monkeypatch.setattr("agitrack.daemons.running_repos", lambda **_: [str(tmp_path / "headless")])
    router = hub.HubRouter()

    payload = json.loads(router.get("/repos", {}).body)

    assert [row["name"] for row in payload["repos"]] == ["headless"]
    # ...and it was REMEMBERED as it was added, so the repair happens once and the repo then
    # behaves like any other — it outlives its tracker, exactly like a `-d`-opened repo.
    remembered = repo_registry.entry_for(tmp_path / "headless")
    assert remembered is not None and remembered.served is True
    monkeypatch.setattr("agitrack.daemons.running_repos", lambda **_: [])
    assert [entry.name for entry in repo_registry.list_repos()] == ["headless"]


def test_a_stopped_repo_is_not_resurrected_by_a_surviving_daemon(tmp_path, monkeypatch):
    # `agitrack stop` clearing `served` is an explicit decision, and a switcher that keeps
    # offering a project the user just stopped is ignoring them. Only a repo with NO entry is
    # adopted from the daemon registry, so the outcome does not depend on whether the stop
    # managed to kill the daemon before it cleared the flag.
    _repo(tmp_path, "stopped")
    repo_registry.remember(tmp_path / "stopped")
    repo_registry.set_served(tmp_path / "stopped", False)
    monkeypatch.setattr("agitrack.daemons.running_repos", lambda **_: [str(tmp_path / "stopped")])
    router = hub.HubRouter()

    payload = json.loads(router.get("/repos", {}).body)

    assert payload["repos"] == []
    assert repo_registry.entry_for(tmp_path / "stopped").served is False


def test_the_switcher_still_draws_when_the_daemon_registry_cannot_be_read(tmp_path, monkeypatch):
    # The union is an enhancement, never a precondition: a daemon registry that raises must not
    # cost the user the repositories that ARE remembered.
    _repo(tmp_path, "proj")
    repo_registry.remember(tmp_path / "proj")

    def boom(**_):
        raise OSError("registry unreadable")

    monkeypatch.setattr("agitrack.daemons.running_repos", boom)
    router = hub.HubRouter()

    payload = json.loads(router.get("/repos", {}).body)

    assert [row["name"] for row in payload["repos"]] == ["proj"]


def test_a_live_daemon_for_a_vanished_directory_is_not_offered(tmp_path, monkeypatch):
    # The list exists to be switched between; offering a repo the dashboard cannot open is worse
    # than not offering it (the same rule `list_repos` applies to remembered entries).
    monkeypatch.setattr("agitrack.daemons.running_repos", lambda **_: [str(tmp_path / "gone")])
    router = hub.HubRouter()

    payload = json.loads(router.get("/repos", {}).body)

    assert payload["repos"] == []


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


# --- every way into the dashboard has an answer ------------------------------------------------


def test_a_missing_directory_names_the_path_and_stops(tmp_path, capsys):
    from agitrack import cli

    assert cli.main(["-d", "--repo", str(tmp_path / "nope")]) == 1
    out = capsys.readouterr().out
    assert "does not exist" in out and "nope" in out


def test_a_file_instead_of_a_directory_says_which_it_is(tmp_path, capsys):
    from agitrack import cli

    target = tmp_path / "a-file.txt"
    target.write_text("x", encoding="utf-8")

    assert cli.main(["-d", "--repo", str(target)]) == 1
    out = capsys.readouterr().out
    assert "is a file, not a project directory" in out


def test_a_plain_directory_with_no_history_gets_the_two_commands_that_change_that(tmp_path, capsys):
    from agitrack import cli

    plain = tmp_path / "plain"
    plain.mkdir()
    monkey_free = cli  # the probe below is what decides; no sessions exist under tmp_path

    assert monkey_free.main(["-d", "--repo", str(plain)]) == 1
    out = capsys.readouterr().out
    assert "not a Git repository" in out
    assert "git init" in out and "agitrack" in out
    # The raw git sentence is not echoed back: it was the whole unhelpful answer before.
    assert "Not a Git repository:" not in out


def test_a_non_git_directory_with_sessions_shows_the_backtrace_instead(tmp_path, monkeypatch, capsys):
    # The reconstruction needs no git at all, so refusing here would withhold the one view that
    # can answer the question.
    from agitrack import cli

    plain = tmp_path / "explored"
    plain.mkdir()
    monkeypatch.setattr("agitrack.metrics.suggest.has_backtrace_history", lambda directory: True)
    opened: dict = {}
    monkeypatch.setattr(
        "agitrack.metrics.hub.open_dashboard",
        lambda directory, **kw: opened.update(directory=directory, **kw) or 0,
    )

    assert cli.main(["-d", "--repo", str(plain)]) == 0
    assert opened["view"] == repo_registry.BACKTRACE
    out = capsys.readouterr().out
    assert "showing the BACKTRACE view instead" in out
    assert "git init" in out  # ...and how to make it a tracked project


def test_backtrace_on_a_missing_directory_says_so(tmp_path, capsys):
    from agitrack import cli

    assert cli.main(["--backtrace", "--repo", str(tmp_path / "gone")]) == 1
    assert "does not exist" in capsys.readouterr().out


def test_a_dashboard_that_will_not_start_says_where_to_look(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hub, "ensure_hub_for", lambda directory, **kw: ("", "active"))

    assert hub.open_dashboard(tmp_path) == 1
    out = capsys.readouterr().out
    assert "did not start" in out
    assert "dashboard.log" in out  # the actual reason lives there
    assert "tracking does not depend on the dashboard" in out  # ...and nothing else is broken


def test_the_menu_calls_the_dashboard_entry_dashboard_only():
    from agitrack import modes

    entry = modes.mode_by_name("dashboard")
    # Every other mode opens the dashboard too, so an entry called just "dashboard" made the
    # look-only option read as the way to get one.
    assert "only" in entry.headline
    assert "without starting any tracking" in entry.summary


# --- switching repository picks that repo's view ------------------------------------------------


def test_switching_repository_lands_on_the_view_that_suits_it(tmp_path, monkeypatch):
    """Carrying the current view across is what lands you on another project's empty page."""
    _repo(tmp_path, "tracked")
    _repo(tmp_path, "untracked")
    repo_registry.remember(tmp_path / "tracked")
    other = repo_registry.remember(tmp_path / "untracked")
    # The one being switched TO has nothing tracked but sessions to reconstruct.
    monkeypatch.setattr(hub, "_has_tracked_tokens", lambda d: "untracked" not in str(d))
    monkeypatch.setattr(hub, "_has_backtrace", lambda d: True)
    router = hub.HubRouter()

    # The switcher navigates to /go/<slug>/, from wherever it currently is.
    response = router.get(hub.choose_path(other.slug), {})

    assert response.status == 302
    assert response.headers["Location"] == f"/b/{other.slug}/"


def test_switching_keeps_the_page_you_were_reading(tmp_path, monkeypatch):
    _repo(tmp_path, "proj")
    entry = repo_registry.remember(tmp_path / "proj")
    monkeypatch.setattr(hub, "_has_tracked_tokens", lambda d: True)
    router = hub.HubRouter()

    response = router.get(hub.choose_path(entry.slug, "learn"), {})

    assert response.headers["Location"] == f"/r/{entry.slug}/learn"


def test_the_repo_list_offers_the_view_choosing_url(tmp_path):
    _repo(tmp_path, "proj")
    entry = repo_registry.remember(tmp_path / "proj")
    router = hub.HubRouter()

    row = json.loads(router.get("/repos", {}).body)["repos"][0]

    assert row["go_url"] == f"/go/{entry.slug}/"


def test_switching_to_a_repo_that_is_gone_says_so(tmp_path):
    router = hub.HubRouter()

    response = router.get(hub.choose_path("never-heard-of-it"), {})

    assert response.status == 404


# --- is aGiTrack actually running here? --------------------------------------------------------


def test_the_dashboard_reports_the_repos_running_mode(tmp_path, monkeypatch):
    from agitrack.metrics.server import RepoScope

    repo = _repo(tmp_path, "proj")
    monkeypatch.setattr("agitrack.proxy.background._live_background_pid", lambda r: 4242, raising=False)
    monkeypatch.setattr(
        "agitrack.proxy.background._read_handshake",
        lambda r: {"mode": "auto commits", "backend": "claude"},
        raising=False,
    )

    state = json.loads(RepoScope(repo).get("/state", {}).body)

    assert state["running"] is True and state["kind"] == "background"
    assert "background" in state["label"] and "auto commits" in state["label"]
    assert "4242" in state["detail"]


def test_a_repo_nothing_is_tracking_says_so_and_how_to_start(tmp_path, monkeypatch):
    from agitrack.metrics.server import RepoScope

    repo = _repo(tmp_path, "idle")
    monkeypatch.setattr("agitrack.proxy.background._live_background_pid", lambda r: None, raising=False)
    monkeypatch.setattr("agitrack.proxy.background._read_proxy_status", lambda r: None, raising=False)

    state = json.loads(RepoScope(repo).get("/state", {}).body)

    assert state["running"] is False and state["label"] == "not tracking"
    # A dashboard that only says "off" leaves the reader with nowhere to go.
    assert "agitrack" in state["detail"]


def test_an_unreadable_repo_reports_not_tracking_rather_than_breaking_the_page(tmp_path):
    from agitrack.proxy.background import running_mode

    broken = type("R", (), {"repo": tmp_path / "gone"})()

    assert running_mode(broken)["running"] is False


def test_the_backtrace_of_a_non_repository_says_it_cannot_be_tracked(tmp_path):
    from agitrack.metrics.backtrace import BacktraceScope

    scope = object.__new__(BacktraceScope)
    scope.learn_repo = None
    state = json.loads(BacktraceScope.get(scope, "/state", {}).body)

    assert state["running"] is False and state["label"] == "not a repository"
    assert "git init" in state["detail"]


def test_backtrace_with_no_sessions_explains_itself_in_the_terminal(tmp_path, monkeypatch, capsys):
    """Opening a browser onto an empty reconstruction says "nothing here" in the least useful
    place available."""
    from agitrack import cli

    monkeypatch.setattr("agitrack.metrics.suggest.has_backtrace_history", lambda directory: False)
    monkeypatch.setattr(
        hub, "open_dashboard", lambda *a, **k: pytest.fail("an empty reconstruction must not open a browser")
    )

    assert cli.main(["--backtrace", "--repo", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "No local coding-agent history found" in out
    # ...and the two things a reader can act on: why it might be empty, and what fills it.
    assert "different directory" in out
    assert "pick a mode" in out


def test_the_repo_list_carries_each_repos_tracking_state(tmp_path, monkeypatch):
    """A list of bare names cannot answer "which of my projects is actually being tracked?"."""
    _repo(tmp_path, "live")
    _repo(tmp_path, "idle")
    repo_registry.remember(tmp_path / "live")
    repo_registry.remember(tmp_path / "idle")
    monkeypatch.setattr(
        "agitrack.proxy.background.running_mode",
        lambda repo: (
            {"running": True, "kind": "background", "label": "tracking · background · auto commits", "detail": "d"}
            if repo.repo.name == "live"
            else {"running": False, "kind": "none", "label": "not tracking", "detail": "start it"}
        ),
    )
    router = hub.HubRouter()

    rows = {r["name"]: r for r in json.loads(router.get("/repos", {}).body)["repos"]}

    assert rows["live"]["running"] is True and rows["live"]["state"] == "background"
    assert rows["idle"]["running"] is False and rows["idle"]["state"] == "off"
    # The full sentence rides along for the row's tooltip.
    assert rows["idle"]["state_detail"] == "start it"


def test_a_repos_state_is_read_from_its_path_without_opening_a_repository(tmp_path):
    """Putting git on the path of drawing a dropdown would make the switcher the slowest control
    on the page, and some listed directories are not repositories at all."""
    from agitrack.proxy.background import running_mode_for

    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    state = running_mode_for(plain)

    assert state["running"] is False and state["label"] == "not tracking"


@pytest.mark.parametrize(
    "state,expected",
    [
        ({"running": False}, "off"),
        ({"running": True, "kind": "background", "label": "tracking · background · auto commits"}, "background"),
        (
            {"running": True, "kind": "background", "label": "tracking · background · manual commits"},
            "background · manual",
        ),
        ({"running": True, "kind": "interactive", "label": "tracking · interactive · auto commits"}, "interactive"),
        ({"running": True, "kind": "unknown", "label": "tracking"}, "tracking"),
    ],
)
def test_the_row_label_keeps_only_what_differs_between_repositories(state, expected):
    # "tracking · background · auto commits" reads well on its own line in the header and is far
    # too long next to twenty repository names.
    assert hub._short_state(state) == expected


# --- the header status must not lag ------------------------------------------------------------


def _run_background(tmp_path, monkeypatch, *, daemon_code=0):
    """Drive `agitrack -b` far enough to see the launcher's ordering, and record it."""
    from agitrack import cli

    _repo(tmp_path, "proj")
    order: list[str] = []
    monkeypatch.setattr(
        "agitrack.proxy.background.start_background_daemon",
        lambda repo, **kw: (order.append("tracker started"), daemon_code)[1],
    )
    monkeypatch.setattr(cli, "_open_dashboard_on_start", lambda *a, **k: order.append("dashboard opened"))
    monkeypatch.setattr(cli, "_maybe_prompt_background_hook", lambda *a, **k: None)
    cli.main(["-b", "--repo", str(tmp_path / "proj"), "--backend", "claude", "--yes"])
    return order


def test_background_mode_starts_the_tracker_before_opening_the_dashboard(tmp_path, monkeypatch):
    """Opening the browser first meant the page asked "is aGiTrack running here?" of a tracker
    that did not exist yet, was told no, and sat on that answer until a manual refresh."""
    assert _run_background(tmp_path, monkeypatch) == ["tracker started", "dashboard opened"]


def test_a_tracker_that_fails_to_start_does_not_open_a_browser(tmp_path, monkeypatch):
    # A failed start is news that belongs in the terminal, not behind a page saying "not tracking".
    assert _run_background(tmp_path, monkeypatch, daemon_code=1) == ["tracker started"]


def test_the_state_is_live_the_moment_the_launcher_returns(tmp_path, monkeypatch):
    """The property the ordering buys: by the time the dashboard is opened, the answer is real.

    `start_background_daemon` waits for the tracker's handshake, and that handshake is exactly
    what the header's status reads."""
    from agitrack.proxy.background import background_handshake_path, running_mode_for

    directory = tmp_path / "proj"
    (directory / ".agitrack").mkdir(parents=True)
    import json
    import os

    background_handshake_path(type("R", (), {"repo": directory})()).write_text(
        json.dumps({"pid": os.getpid(), "mode": "auto commits", "backend": "claude"}), encoding="utf-8"
    )

    state = running_mode_for(directory)

    assert state["running"] is True and "background" in state["label"]


# --- starting a tracking mode shows the tracking ------------------------------------------------


def test_starting_a_tracker_opens_the_tracked_view_even_if_the_backtrace_was_last_seen(tmp_path, monkeypatch):
    """`agitrack -b` is the user saying "record this from now on", and the reconstruction is
    emphatically not that: it is the inferred history of what happened BEFORE. A remembered
    preference for the backtrace was still winning, so starting a tracker on a fully tracked
    repository opened the one view that could not show the work about to be recorded."""
    _repo(tmp_path, "proj")
    directory = tmp_path / "proj"
    repo_registry.remember(directory)
    repo_registry.mark_tracked_seen(directory)
    repo_registry.set_view(directory, repo_registry.BACKTRACE)
    monkeypatch.setattr(hub, "_has_tracked_tokens", lambda d: True)
    monkeypatch.setattr(hub, "_has_backtrace", lambda d: True)

    # Just looking still honours the remembered choice...
    assert hub.preferred_view(directory) == repo_registry.BACKTRACE
    # ...but starting to track overrides it, and the choice is updated to match.
    assert hub.preferred_view(directory, starting_tracking=True) == repo_registry.ACTIVE
    assert repo_registry.entry_for(directory).view == repo_registry.ACTIVE


def test_starting_a_tracker_on_an_untracked_repo_still_shows_its_reconstruction(tmp_path, monkeypatch):
    # A first-ever run on an old project has nothing to show in the tracked view yet, and its
    # history is sitting in the transcripts. That rule is unchanged.
    _repo(tmp_path, "old")
    directory = tmp_path / "old"
    repo_registry.remember(directory)
    monkeypatch.setattr(hub, "_has_tracked_tokens", lambda d: False)
    monkeypatch.setattr(hub, "_has_backtrace", lambda d: True)

    assert hub.preferred_view(directory, starting_tracking=True) == repo_registry.BACKTRACE


def test_a_git_failure_never_reads_as_nothing_tracked(tmp_path):
    """A contended index.lock while the tracker commits is not an answer of "no history"."""
    from agitrack.metrics.suggest import has_tracked_tokens

    class Failing:
        repo = tmp_path

        def _run(self, *a, **k):
            return type("R", (), {"stdout": "", "returncode": 128})()

    # Answering "nothing tracked" here sends a fully tracked repository to the reconstruction.
    assert has_tracked_tokens(Failing()) is True


def test_gits_ordinary_no_match_is_still_an_answer(tmp_path):
    from agitrack.metrics.suggest import has_tracked_tokens

    class NoMatch:
        repo = tmp_path

        def _run(self, *a, **k):
            return type("R", (), {"stdout": "", "returncode": 1})()

    assert has_tracked_tokens(NoMatch()) is False


def test_a_directory_that_is_not_a_repository_has_nothing_tracked(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    assert hub._has_tracked_tokens(plain) is False


# --- one dashboard tab, not one per repository --------------------------------------------------


def test_an_open_dashboard_tab_is_steered_instead_of_opening_another():
    clients = hub._Clients()
    clients.ping("tab-1", "/r/one/", "")
    taken: list[str] = []

    def tab():
        # The page's next ping picks the navigation up.
        taken.append(clients.ping("tab-1", "/r/one/", ""))

    import threading

    threading.Timer(0.05, tab).start()
    assert clients.navigate("/r/two/") is not None
    assert taken == ["/r/two/"]


def test_a_story_or_learn_tab_is_left_where_the_reader_put_it():
    # Somewhere the reader chose to be, about a repository they chose. Steering it would take
    # that away, so those get a new tab instead.
    clients = hub._Clients()
    clients.ping("tab-story", "/r/one/story", "story")
    clients.ping("tab-learn", "/r/one/learn", "learn")

    assert clients.navigate("/r/two/", timeout=0.2) is None


def test_a_tab_that_has_gone_does_not_swallow_the_navigation():
    # Queueing into a tab closed a second ago would leave the user with no window at all.
    clients = hub._Clients()
    clients.ping("tab-1", "/r/one/", "")
    clients.close("tab-1")

    assert clients.navigate("/r/two/", timeout=0.2) is None


def test_a_tab_that_stopped_answering_is_given_up_on():
    clients = hub._Clients()
    clients.ping("tab-1", "/r/one/", "")  # registered, then never pings again

    assert clients.navigate("/r/two/", timeout=0.2) is None
    # ...and the abandoned instruction is dropped, so it cannot fire later out of context.
    assert clients.ping("tab-1", "/r/one/", "") == ""


def test_the_most_recently_active_dashboard_tab_is_the_one_steered():
    clients = hub._Clients()
    clients.ping("older", "/r/one/", "")
    clients.ping("newer", "/r/two/", "")
    taken: list[str] = []

    import threading

    threading.Timer(0.05, lambda: taken.append(clients.ping("newer", "/r/two/", ""))).start()
    assert clients.navigate("/r/three/") is not None
    assert taken == ["/r/three/"]


def test_the_right_tab_is_steered_even_when_the_clock_cannot_tell_them_apart():
    """`time.monotonic()` is ~15.6ms coarse on Windows, so two tabs pinging inside one tick get
    an identical timestamp. Ordering by that timestamp then fell through to comparing client IDS,
    steering the alphabetically-last tab instead of the one you were just looking at. CI caught
    it; a counter cannot tie."""
    clients = hub._Clients()
    clients.ping("zzz-opened-first", "/r/one/", "")
    clients.ping("aaa-opened-second", "/r/two/", "")
    # What a coarse clock produces, without freezing the real one out from under every other
    # thread in the process: two pings that are indistinguishable by time.
    for record in clients._seen.values():
        record["seen"] = 1000.0

    # The LAST tab to ping, whatever it happens to be called.
    assert clients._pick_dashboard() == "aaa-opened-second"


def test_liveness_still_comes_from_the_clock(monkeypatch):
    # Only the ORDERING moved to a counter. Whether a page is alive at all is still a question
    # about elapsed time, which coarse resolution cannot get wrong.
    clients = hub._Clients()
    clients.ping("tab-1", "/r/one/", "")
    monkeypatch.setattr(hub, "_CLIENT_TTL_SECONDS", -1.0)

    assert clients.snapshot() == []


def test_a_stale_tab_is_forgotten(monkeypatch):
    clients = hub._Clients()
    clients.ping("tab-1", "/r/one/", "")
    monkeypatch.setattr(hub, "_CLIENT_TTL_SECONDS", -1.0)

    assert clients.snapshot() == []


def test_the_hub_answers_the_pages_and_the_launcher(tmp_path):
    router = hub.HubRouter()

    assert json.loads(router.post("/clients", {"id": "t", "path": "/r/x/", "page": ""}).body) == {"navigate": ""}
    assert json.loads(router.get("/clients", {}).body)["clients"] == [{"page": "", "path": "/r/x/"}]
    # Nothing is listening for a story tab, so the launcher is told to open its own.
    assert json.loads(router.post("/navigate", {"url": "/r/y/"}).body)["navigated"] in (True, False)
    assert json.loads(router.post("/clients", {"id": "t", "closing": True}).body) == {"ok": True}
    assert json.loads(router.get("/clients", {}).body)["clients"] == []


# --- the steered tab is brought forward ---------------------------------------------------------


def test_the_steered_tab_reports_which_browser_it_is_in():
    """A page cannot raise itself: `window.focus()` is ignored without a user gesture in every
    current browser, by design. So the ask comes from aGiTrack's own process, and it needs to know
    WHICH browser to ask for on a machine with several open."""
    clients = hub._Clients()
    clients.ping("tab", "/r/one/", "", "firefox")
    import threading

    threading.Timer(0.05, lambda: clients.ping("tab", "/r/one/", "", "firefox")).start()

    assert clients.navigate("/r/two/") == "firefox"


def test_a_tab_that_did_not_say_which_browser_still_steers():
    # An older page, or one whose user agent says nothing useful. Steering it is still right;
    # only the raise is skipped.
    clients = hub._Clients()
    clients.ping("tab", "/r/one/", "")
    import threading

    threading.Timer(0.05, lambda: clients.ping("tab", "/r/one/", "")).start()

    assert clients.navigate("/r/two/") == ""


def test_nothing_steered_means_nothing_raised():
    clients = hub._Clients()

    assert clients.navigate("/r/two/", timeout=0.2) is None


def test_the_launcher_raises_the_browser_that_took_the_navigation(monkeypatch):
    raised: list[str] = []
    monkeypatch.setattr("agitrack.metrics.server.raise_browser_window", lambda family="": raised.append(family) or True)

    class FakeResponse:
        def read(self):
            return json.dumps({"navigated": True, "browser": "firefox"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResponse())

    assert hub._steer_open_tab({"url": "http://127.0.0.1:8765/"}, "/r/two/") is True
    assert raised == ["firefox"]


def test_no_window_is_raised_when_no_tab_was_steered(monkeypatch):
    monkeypatch.setattr(
        "agitrack.metrics.server.raise_browser_window",
        lambda family="": pytest.fail("nothing was steered, so there is no window to raise"),
    )

    class FakeResponse:
        def read(self):
            return json.dumps({"navigated": False, "browser": ""}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResponse())

    assert hub._steer_open_tab({"url": "http://127.0.0.1:8765/"}, "/r/two/") is False


def test_a_browser_that_is_not_running_is_never_launched(monkeypatch):
    """The point is to bring a window forward, not to start a browser nobody had open."""
    from agitrack.metrics import server

    monkeypatch.setattr(server, "_running_macos_apps", lambda: {"Finder", "Firefox"})
    launched: list[list[str]] = []
    monkeypatch.setattr(server.subprocess, "run", lambda cmd, **k: launched.append(cmd))

    assert server._raise_macos("chrome") is False
    assert launched == []  # no Chromium browser is running, so nothing was asked to open
    assert server._raise_macos("firefox") is True
    assert launched and launched[0][:2] == ["open", "-a"]


def test_raising_a_window_never_asks_macos_for_automation_permission(monkeypatch):
    """`System Events` would pop "Terminal wants to control System Events" on first use: a
    permission dialog nobody asked for, in service of a side errand."""
    from agitrack.metrics import server

    seen: list[list[str]] = []

    class Result:
        stdout = "/Applications/Firefox.app/Contents/MacOS/firefox\n"

    monkeypatch.setattr(server.subprocess, "run", lambda cmd, **k: seen.append(cmd) or Result())

    server._running_macos_apps()

    assert seen and seen[0][0] == "ps"
    assert not any("osascript" in part for cmd in seen for part in cmd)


def test_a_remote_or_headless_host_has_no_window_to_raise(monkeypatch):
    from agitrack.metrics import server

    monkeypatch.setattr(server, "browser_is_local", lambda: False)

    assert server.raise_browser_window("firefox") is False


def test_an_unknown_browser_raises_nothing_rather_than_guessing():
    """Trying each running browser in turn raised whichever came first in the list rather than
    the one holding the tab. With Safari showing the dashboard and Firefox merely running, it
    raised Firefox — and Firefox had no window open, so `open -a` made a blank one."""
    from agitrack.metrics import server

    assert server.raise_browser_window("") is False


def test_a_browser_family_is_read_off_the_request_when_the_page_does_not_say():
    """A tab loaded before the page learned to report its browser says nothing, and it is still
    the tab that has to be raised. The header is on every request either way."""
    from agitrack.metrics.server import browser_family_from_user_agent

    safari = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    )
    chrome = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

    assert browser_family_from_user_agent(safari) == "safari"
    # Order matters: Chrome's user agent also contains "Safari", and every Chromium browser
    # contains "Chrome".
    assert browser_family_from_user_agent(chrome) == "chrome"
    assert browser_family_from_user_agent(chrome + " Edg/120.0") == "edge"
    assert browser_family_from_user_agent("") == ""


def test_the_page_that_does_say_is_believed_over_the_header(tmp_path):
    # The body wins when it is there: the page knows things a user agent cannot say (Brave
    # reports itself as Chrome), and the header is only the gap-filler.
    router = hub.HubRouter()
    router.post("/clients", {"id": "t", "path": "/r/x/", "page": "", "browser": "firefox"})

    import threading

    threading.Timer(0.05, lambda: router.post("/clients", {"id": "t", "path": "/r/x/", "page": ""})).start()
    assert router.clients.navigate("/r/y/") == "firefox"
