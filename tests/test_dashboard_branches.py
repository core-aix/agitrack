"""A branch that has just been PULLED must show up in the dashboard, with its people on it.

Three separate things had to be true for that to work, and none of them were:

* a fetched branch exists only as a remote-tracking ref until someone checks it out, and the
  selector listed local branches only, so it was invisible however long you waited;
* the built dashboard is cached against the SHOWN branch's tip, which a branch arriving does not
  move — so even a local branch stayed out of the selector until the current branch happened to
  commit;
* GitHub identities were resolved from ``repos/{owner}/{repo}/commits``, which lists the DEFAULT
  BRANCH and nothing else, so every contributor whose commits were only on the new branch fell
  back to their raw git name. The same person then carried two labels depending on the branch
  you were looking at, and filtering by their GitHub ID dropped their branch-only commits.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from agitrack.git import GitRepo
from agitrack.metrics import github
from agitrack.metrics.collect import build_dashboard, viewable_branches


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def _only_fake_gh(monkeypatch, fake):
    """Stand in for the ``gh`` crawl and NOTHING else.

    ``github.subprocess`` is the stdlib module, so patching ``.run`` on it patches it for every
    caller in the process — including ``GitRepo._run``. A test that did that silently fed fake
    output to git as well, and the ref lookups these tests depend on came back as nonsense."""
    real_run = subprocess.run
    monkeypatch.setattr(github.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        github.subprocess,
        "run",
        lambda command, **kwargs: (
            fake(command, **kwargs) if command and command[0] == "gh" else real_run(command, **kwargs)
        ),
    )


def _origin_with_a_branch(tmp_path: Path) -> tuple[GitRepo, Path]:
    """A clone whose ``origin`` has a branch the clone has fetched but never checked out —
    exactly the on-disk shape of "someone just pulled a new branch"."""
    origin = tmp_path / "origin"
    subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
    _git(origin, "config", "user.email", "alice@example.com")
    _git(origin, "config", "user.name", "Alice Example")
    (origin / "a.txt").write_text("one\n", encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "commit", "-qm", "Alice adds a.txt")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)

    # ...and only now does the branch appear on the remote, so the clone has to FETCH it.
    _git(origin, "checkout", "-qb", "feature-x")
    _git(origin, "config", "user.email", "bob@example.com")
    _git(origin, "config", "user.name", "Bob Builder")
    (origin / "b.txt").write_text("bob\n", encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "commit", "-qm", "Bob adds b.txt")
    _git(origin, "checkout", "-q", "main")  # leave the default branch checked out on the origin
    _git(clone, "fetch", "-q", "origin")
    return GitRepo(clone), origin


# --- the branch selector ----------------------------------------------------


def test_a_fetched_branch_is_offered_even_though_it_was_never_checked_out(tmp_path):
    repo, _origin = _origin_with_a_branch(tmp_path)
    assert repo.list_branches() == ["main"]  # nothing local to show

    assert viewable_branches(repo) == ["main", "origin/feature-x"]


def test_a_remote_branch_that_has_a_local_copy_is_not_listed_twice(tmp_path):
    repo, _origin = _origin_with_a_branch(tmp_path)
    _git(repo.repo, "branch", "feature-x", "origin/feature-x")

    # The local branch is the same history and is what the rest of aGiTrack works with.
    assert sorted(viewable_branches(repo)) == ["feature-x", "main"]


def test_origin_head_is_not_offered_as_a_branch(tmp_path):
    # `origin/HEAD` is a symbolic alias for the default branch; listing it offers one branch
    # twice under two names.
    repo, _origin = _origin_with_a_branch(tmp_path)
    _git(repo.repo, "remote", "set-head", "origin", "main")

    assert "origin/HEAD" not in viewable_branches(repo)
    assert "origin/HEAD" not in repo.list_remote_branches()


def test_a_repo_with_no_remote_still_lists_its_local_branches(tmp_path):
    repo = GitRepo.init(tmp_path / "solo")
    _git(repo.repo, "config", "user.email", "t@t")
    _git(repo.repo, "config", "user.name", "t")
    (repo.repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo.repo, "add", ".")
    _git(repo.repo, "commit", "-qm", "init")

    assert viewable_branches(repo) == repo.list_branches()


def test_the_dashboard_can_be_built_for_a_branch_only_the_remote_has(tmp_path):
    # Offering it in the selector is only half of it: the ref has to be readable too.
    repo, _origin = _origin_with_a_branch(tmp_path)

    dash = build_dashboard(repo, "origin/feature-x")

    assert dash.branch == "origin/feature-x"
    assert [stat.subject for stat in dash.stats] == ["Alice adds a.txt", "Bob adds b.txt"]


# --- the selector must not be served from a stale cache ---------------------


def test_a_branch_appearing_invalidates_the_cached_dashboard(tmp_path):
    """The bug: the fingerprint was the SHOWN branch's tip, and a branch arriving does not move
    it — so the selector kept serving the branch list from before the pull."""
    from agitrack.metrics.server import RepoScope

    repo, _origin = _origin_with_a_branch(tmp_path)
    live = RepoScope(repo)
    before = live._ref_state("HEAD")

    _git(repo.repo, "branch", "feature-x", "origin/feature-x")  # HEAD does not move

    assert live._ref_state("HEAD") != before


def test_a_branch_merely_moving_does_not_churn_the_cache(tmp_path):
    """The other half: the fingerprint holds branch NAMES, not tips. A branch that only moved
    changes nothing the selector shows, and rebuilding the whole history for it is pure churn."""
    from agitrack.metrics.server import RepoScope

    repo, _origin = _origin_with_a_branch(tmp_path)
    _git(repo.repo, "branch", "side", "HEAD")
    live = RepoScope(repo)
    before = live._ref_state("HEAD")

    _git(repo.repo, "fetch", "-q", "origin", "feature-x:side", "--force")  # `side` moves

    assert live._ref_state("HEAD") == before


def test_the_selector_only_ever_offers_a_branch_the_validator_accepts(tmp_path):
    # Offering a branch `_ref` then refuses silently falls back to HEAD, which reads as the
    # selector being broken. One list backs both, so they cannot drift apart.
    from agitrack.metrics.server import RepoScope

    repo, _origin = _origin_with_a_branch(tmp_path)
    live = RepoScope(repo)

    for branch in viewable_branches(repo):
        assert live._ref(branch) == branch
    assert live._ref("no-such-branch") == "HEAD"
    assert live._ref("") == "HEAD"


# --- GitHub identities on the new branch ------------------------------------


def test_logins_are_resolved_for_the_branch_being_viewed_not_just_the_default(tmp_path, monkeypatch):
    """`repos/{owner}/{repo}/commits` lists the DEFAULT BRANCH's commits. A contributor whose
    work is only on the pulled branch got no login from it at all."""
    repo, _origin = _origin_with_a_branch(tmp_path)
    github._reset_cache_for_tests()
    tip = repo.rev_parse("origin/feature-x")
    main_tip = repo.rev_parse("main")
    asked: list[str] = []

    def fake_gh(command, **kwargs):
        endpoint = next(arg for arg in command if arg.startswith("repos/"))
        asked.append(endpoint)
        # The real API answers with the named ref's commits; the default branch has no `sha=`.
        sha = tip if f"sha={tip}" in endpoint else main_tip
        return subprocess.CompletedProcess(command, 0, stdout=f"{sha}\tsomebody\n", stderr="")

    _only_fake_gh(monkeypatch, fake_gh)

    logins = github.resolve_logins(repo, ref="origin/feature-x")

    assert any(f"sha={tip}" in endpoint for endpoint in asked), "the viewed branch was never crawled"
    assert any("sha=" not in endpoint for endpoint in asked), "the default branch is still crawled"
    # Both maps are merged, so a commit on either branch keeps its identity.
    assert logins[tip] == "somebody"
    assert logins[repo.rev_parse("main")] == "somebody"


def test_the_default_branch_crawl_is_reused_across_refs(tmp_path, monkeypatch):
    # Per (repo, ref) caching: viewing a second branch must not re-crawl the default branch.
    repo, _origin = _origin_with_a_branch(tmp_path)
    github._reset_cache_for_tests()
    calls = {"n": 0}

    def fake_gh(command, **kwargs):
        calls["n"] += 1
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    _only_fake_gh(monkeypatch, fake_gh)

    github.resolve_logins(repo)  # default branch only
    assert calls["n"] == 1
    github.resolve_logins(repo, ref="origin/feature-x")  # + this branch
    assert calls["n"] == 2
    github.resolve_logins(repo, ref="origin/feature-x")  # both cached now
    assert calls["n"] == 2


def test_an_unpushed_ref_falls_back_instead_of_failing(tmp_path, monkeypatch):
    # A local-only branch is not on GitHub: the crawl 422s, and the committer keeps the name
    # the email heuristic gives them rather than the page erroring.
    repo, _origin = _origin_with_a_branch(tmp_path)
    github._reset_cache_for_tests()
    _only_fake_gh(
        monkeypatch,
        lambda command, **kwargs: subprocess.CompletedProcess(command, 22, stdout="", stderr="HTTP 422"),
    )

    assert github.resolve_logins(repo, ref="main") == {}


def test_cached_logins_never_blocks_the_poll_on_the_new_branchs_crawl(tmp_path, monkeypatch):
    # The hot path stays non-blocking for the ref crawl too: cold means {} now and resolved
    # on a later poll, never a networked wait inside a request.
    repo, _origin = _origin_with_a_branch(tmp_path)
    github._reset_cache_for_tests()
    tip = repo.rev_parse("origin/feature-x")
    github._CACHE[(str(repo.repo), "")] = (time.monotonic(), {"a": "alice"})
    github._CACHE[(str(repo.repo), tip)] = (time.monotonic(), {"b": "bob"})
    _only_fake_gh(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(AssertionError("crawled in-request")))

    assert github.cached_logins(repo, "origin/feature-x") == {"a": "alice", "b": "bob"}
