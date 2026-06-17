"""`agit --dashboard` (#54): repo metrics computed from aGiTrack commit metadata.

The collector reads everything from `git log` alone, so the tests build a real
repository whose history contains every commit kind the classifier knows:
untracked (plain git), user (commit_type: user), agent (commit_type: agent,
with token metadata), backend-made commits covered by a merge-shaped cover
commit (#58), and an agent-merge.
"""

from pathlib import Path

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

import agitrack.metrics as metrics
from agitrack import cli
from agitrack.commits import build_agent_commit_message, build_agent_merge_message, build_user_commit_message
from agitrack.git import GitRepo
from agitrack.metrics import build_dashboard, build_server, dashboard_data, render_dashboard, render_html
from agitrack.metrics.collect import CommitStat, resolve_committers


def _write_lines(repo: GitRepo, name: str, count: int) -> None:
    (repo.repo / name).write_text("".join(f"line {i}\n" for i in range(count)), encoding="utf-8")
    repo.stage_paths([name])


def _agent_message(prompt: str, *, tokens: dict | None = None, covered: list[str] | None = None) -> str:
    return build_agent_commit_message(
        latest_prompt=prompt,
        trace=[{"role": "user", "content": prompt}, {"role": "agent", "content": "done"}],
        backend="claude",
        backend_session_id="ses-1",
        agitrack_session_id="agit-1",
        model="claude-opus-4-8",
        token_usage=tokens,
        covered_commits=covered,
    )


_TOKENS = {
    "context": 100,
    "total": 50,
    "input": 1000,
    "output": 50,
    "reasoning": 0,
    "cache_read": 0,
    "cache_write": 0,
}


def _demo_repo(tmp_path: Path) -> GitRepo:
    repo = GitRepo.init(tmp_path)  # seed commit: untracked

    _write_lines(repo, "human.txt", 10)
    repo.commit("plain human commit")  # untracked

    _write_lines(repo, "user.txt", 4)
    repo.commit(build_user_commit_message(message="save my edits", agitrack_session_id="agit-1"))  # user

    _write_lines(repo, "agent.txt", 20)
    repo.commit(_agent_message("add the feature", tokens=_TOKENS))  # agent, 1050 in / 50 out

    # Backend-made commit covered by a merge-shaped cover commit (#58).
    turn_start = repo.rev_parse("HEAD")
    _write_lines(repo, "backend.txt", 8)
    repo.commit("backend made this itself")
    backend_sha = repo.rev_parse("HEAD")
    repo.cover_commit(
        _agent_message("refactor the parser", tokens=_TOKENS, covered=[repo.short_sha(backend_sha)]),
        first_parent=turn_start,
        second_parent=backend_sha,
    )

    repo.commit_message("HEAD")  # sanity: readable
    repo._run(
        ["git", "commit", "--allow-empty", "-F", "-"],
        input_text=build_agent_merge_message(
            session_name="s1",
            base_branch="main",
            source_branch="agit/claude/s1/t1",
            agitrack_session_id="agit-1",
            backend="claude",
        ),
    )  # agent-merge
    return repo


def test_agitrack_integration_merge_is_classified_as_ops_not_untracked(tmp_path):
    repo = GitRepo.init(tmp_path)
    # aGiTrack's own auto-merge bringing base into a session turn branch.
    repo._run(
        ["git", "commit", "--allow-empty", "-m", "Merge branch 'dev' into agit/claude/session-1/t2"],
    )

    dash = build_dashboard(repo)
    ops = next(s for s in dash.stats if s.subject.startswith("Merge branch"))
    assert ops.kind == "agitrack-ops"  # an aGiTrack control, not stray untracked work
    assert dash.count("agitrack-ops") == 1
    assert ops.kind in ("agent", "covered", "agent-merge", "user", "agitrack-ops")
    # It counts toward aGiTrack coverage, and is not lumped into non-tracked lines.
    assert dash.nontracked_lines == (0, 0)


def test_dashboard_classifies_every_commit_kind(tmp_path):
    dash = build_dashboard(_demo_repo(tmp_path))

    kinds = {stat.kind for stat in dash.stats}
    assert kinds == {"untracked", "user", "agent", "covered", "agent-merge"}
    assert dash.count("agent") == 2  # the regular agent commit + the cover commit
    assert dash.count("covered") == 1  # the backend-made commit, identified via covered_commits
    assert dash.count("untracked") == 2  # seed + plain human commit
    assert dash.total_commits == 7
    assert dash.tracked_commits == 5
    assert abs(dash.coverage - 5 / 7) < 1e-9


def test_dashboard_splits_lines_into_tracked_ai_and_nontracked(tmp_path):
    dash = build_dashboard(_demo_repo(tmp_path))

    ai_ins, _ = dash.ai_lines
    nt_ins, _ = dash.nontracked_lines
    # aGiTrack-tracked AI: agent.txt (20) + backend.txt (8, via the covered commit);
    # the cover commit itself is a merge and contributes no numstat — no double
    # count.
    assert ai_ins == 28
    # Non-tracked: the user commit (user.txt, 4) + the plain commit (human.txt,
    # 10). We do not claim these as "human" — a user commit's lines may still be
    # AI-produced off the record; they are simply not tracked as AI.
    assert nt_ins == 14


def test_squash_parses_constituents_and_counts_their_tokens(tmp_path):
    # A squash / PR-merge message concatenates several original commits' metadata
    # blocks. The dashboard parses each one back out so their tokens and
    # model/backend usage are counted — they are not lost in the aggregate.
    repo = GitRepo.init(tmp_path)
    _write_lines(repo, "squashed.txt", 500)
    message = (
        "Stability improvements (#9)\n\n"
        "* did agent work\n\n"
        "# aGiTrack Metadata\ncommit_type: agent\nbackend: claude\nmodel: claude-opus-4-8\n"
        "tokens_since_last_commit_output: 1000\ntokens_since_last_commit_input: 500\n\n"
        "* my own edit\n\n"
        "# aGiTrack Metadata\ncommit_type: user\nagit_session_id: s1\n"
    )
    repo.commit(message)

    dash = build_dashboard(repo)
    squashed = next(s for s in dash.stats if s.subject.startswith("Stability"))
    # Two original commits recovered from the concatenated blocks.
    assert [c.kind for c in squashed.constituents] == ["agent", "user"]
    # The squash is tracked AI work (it contains agent work) and its single
    # combined diff counts as AI; the agent original's tokens are counted.
    assert squashed.kind == "agent"
    assert squashed.tokens["output"] == 1000
    assert dash.token_totals["output"] == 1000
    assert dash.ai_lines[0] == 500
    assert dash.nontracked_lines == (0, 0)
    # Usage is attributed to the original's model/backend, not lost.
    assert dash.by_model["claude-opus-4-8"]["output_tokens"] == 1000
    assert dash.by_model["claude-opus-4-8"]["commits"] == 1
    assert dash.by_backend["claude"]["commits"] == 1


def test_same_commit_in_multiple_squashes_counts_tokens_once(tmp_path):
    # An original commit can be rolled into more than one squash (a branch is
    # squash-merged, then that result squashed again). Its metadata block — and so
    # its tokens — is copied into every squash; the dashboard must count it once.
    repo = GitRepo.init(tmp_path)

    def block(started_at: str, output: int) -> str:
        return (
            "# aGiTrack Metadata\ncommit_type: agent\nbackend: claude\nmodel: claude-opus-4-8\n"
            f"agent_started_at: {started_at}\n"
            f"tokens_since_last_commit_output: {output}\ntokens_since_last_commit_input: 1\n"
        )

    shared = block("2026-06-01T00:00:00Z", 1000)

    # Two genuine squashes (each concatenates >1 metadata block) that both contain
    # the shared turn — the byte-identical block is what marks it as one commit.
    _write_lines(repo, "first.txt", 100)
    repo.commit(
        "First squash (#1)\n\n* shared agent work\n\n"
        + shared
        + "\n* only in first\n\n"
        + block("2026-06-02T00:00:00Z", 20)
    )
    _write_lines(repo, "second.txt", 200)
    repo.commit(
        "Second squash (#2)\n\n* shared agent work\n\n"
        + shared
        + "\n* only in second\n\n"
        + block("2026-06-03T00:00:00Z", 7)
    )

    dash = build_dashboard(repo)
    first = next(s for s in dash.stats if s.subject.startswith("First"))
    second = next(s for s in dash.stats if s.subject.startswith("Second"))

    # The older squash keeps the shared turn; the newer one drops the repeat and is
    # left with only its own distinct turn.
    assert first.tokens["output"] == 1020
    assert [c.tokens.get("output") for c in second.constituents] == [7]
    assert second.tokens["output"] == 7
    # Counted once across the repo (1000 + 20 + 7), not 2027, in both totals and rollup.
    assert dash.token_totals["output"] == 1027
    assert dash.by_model["claude-opus-4-8"]["output_tokens"] == 1027


def test_dashboard_sums_tokens_and_efficiency(tmp_path):
    dash = build_dashboard(_demo_repo(tmp_path))

    totals = dash.token_totals
    assert totals["input"] == 2000  # two agent commits, 1000 input tokens each
    assert totals["output"] == 100
    assert dash.lines_per_1k_output_tokens == (28 + 0) / 100 * 1000


def test_dashboard_groups_by_backend_model_and_author(tmp_path):
    dash = build_dashboard(_demo_repo(tmp_path))

    backend = dash.by_backend["claude"]
    assert backend["commits"] == 2
    # The cover commit's bucket includes the covered backend commit's lines.
    assert backend["insertions"] == 28
    assert dash.by_model["claude-opus-4-8"]["commits"] == 2
    (author_stats,) = dash.by_author.values()
    assert author_stats["commits"] == 7
    assert author_stats["agitrack_commits"] == 5
    # The agent/covered lines this committer git-authored are reported as
    # AI-driven (28); their user + plain commits are non-tracked (4 + 10), never
    # claimed as their own human lines.
    assert author_stats["ai_insertions"] == 28
    assert author_stats["nontracked_insertions"] == 14


def test_render_dashboard_contains_all_sections(tmp_path):
    text = render_dashboard(_demo_repo(tmp_path))

    for heading in (
        "aGiTrack Dashboard",
        "Coverage",
        "Code changes",
        "Tokens",
        "By backend",
        "By model",
        "By committer",
    ):
        assert heading in text
    assert "aGiTrack-tracked commits: 5/7" in text
    assert "claude-opus-4-8" in text
    # Lines are split only two ways now — tracked AI vs non-tracked — with no
    # "human" category, since a user commit's lines may still be AI-produced.
    assert "aGiTrack-tracked AI:" in text
    assert "Non-tracked:" in text
    assert "Human (" not in text and "human (own code)" not in text


def test_text_dashboard_notes_input_includes_cache_write_only_when_nonzero(tmp_path):
    # When cache-creation happened, the text dashboard spells out that input folds in
    # cache-write tokens (which diverges from provider billing); otherwise it stays quiet.
    cached = GitRepo.init(tmp_path / "cached")
    _write_lines(cached, "a.txt", 5)
    cached.commit(_agent_message("do it", tokens={**_TOKENS, "cache_write": 800}))
    text = render_dashboard(cached)
    assert "cache write: 800" in text
    assert "note: input includes cache-creation (cache write) tokens" in text

    plain = GitRepo.init(tmp_path / "plain")
    _write_lines(plain, "b.txt", 5)
    plain.commit(_agent_message("do it", tokens=_TOKENS))  # cache_write == 0
    plain_text = render_dashboard(plain)
    assert "note: input includes cache-creation" not in plain_text


def test_pr_merge_commit_does_not_double_count_the_cover_turn(tmp_path):
    """A GitHub-style merge that inherits the cover commit's message (and so its
    metadata block) must not be counted as a second turn (#58 regression)."""
    repo = GitRepo.init(tmp_path)

    _write_lines(repo, "agent.txt", 20)
    cover_message = _agent_message("add the feature", tokens=_TOKENS)
    repo.commit(cover_message)
    cover_sha = repo.rev_parse("HEAD")

    base = build_dashboard(repo)

    # Integrate the branch with a real merge commit whose message is a verbatim
    # copy of the cover commit's — exactly what GitHub does on PR merge.
    repo._run(["git", "checkout", "-q", "-b", "base", "HEAD~1"])
    repo._run(
        ["git", "merge", "--no-ff", "-m", cover_message, cover_sha],
    )

    merged = build_dashboard(repo)

    assert merged.token_totals == base.token_totals
    assert merged.count("agent") == base.count("agent")
    assert merged.by_backend["claude"]["output_tokens"] == base.by_backend["claude"]["output_tokens"]


# --- committer identity merging ------------------------------------------------


def _person(name: str, email: str, kind: str = "agent") -> CommitStat:
    return CommitStat(sha=name + email, author=name, email=email, subject="", kind=kind)


def test_resolve_committers_merges_name_variants_sharing_an_email():
    stats = [
        _person("Pat Example", "pat@example.com"),
        _person("Pat Example", "pat@example.com"),
        _person("Patricia Example", "pat@example.com"),
    ]
    labels = resolve_committers(stats)
    # One identity, labelled with the most frequent name.
    assert set(labels.values()) == {"Pat Example"}


def test_resolve_committers_bridges_personal_and_noreply_via_login():
    stats = [
        _person("dev", "octodev@example.com"),
        _person("Dev Example", "octodev@users.noreply.github.com"),
    ]
    labels = resolve_committers(stats)
    # Same GitHub login across a personal email and a no-reply address (the
    # personal email's local-part matches the login) → one identity, labelled
    # with the login plus the person's first name from their git author name.
    assert set(labels.values()) == {"octodev (Dev)"}


def test_resolve_committers_merges_same_name_across_two_emails():
    stats = [
        _person("Sam Sample", "sam@sample.test"),
        _person("Sam Sample", "sam.sample@example.com"),
    ]
    # No login signal, but the identical name yields the same label, so the two
    # collapse to one committer downstream.
    assert set(resolve_committers(stats).values()) == {"Sam Sample"}


def test_resolve_committers_keeps_distinct_people_apart():
    stats = [
        _person("Alice Example", "alice@example.com"),
        _person("Bob Example", "bob@example.com"),
    ]
    assert len(set(resolve_committers(stats).values())) == 2


def test_resolve_committers_uses_github_logins_when_provided():
    a = _person("Pat", "pat@personal.test")
    b = _person("Patricia Example", "pat.example@work.test")
    # gh maps both commits to the same login despite unrelated names/emails; the
    # label is the login with the first name (preferring the "First Last" variant).
    labels = resolve_committers([a, b], {a.sha: "patexample", b.sha: "patexample"})
    assert set(labels.values()) == {"patexample (Patricia)"}


def test_committer_label_omits_first_name_when_it_just_repeats_the_login():
    # When the only git name IS the login (no separate human first name), the label
    # is the bare login — no redundant "octocat (octocat)".
    a = _person("octocat", "octocat@personal.test")
    labels = resolve_committers([a], {a.sha: "octocat"})
    assert set(labels.values()) == {"octocat"}


def test_committer_label_without_a_login_is_just_the_name():
    # No GitHub login resolvable → fall back to the most frequent git name, with no
    # parenthetical (the name is already the primary identity).
    labels = resolve_committers([_person("Sam Sample", "sam@sample.test")])
    assert set(labels.values()) == {"Sam Sample"}


# --- co-author trailers (multiple committers per commit, #54) ------------------


def test_parse_co_authors_extracts_humans_and_drops_ai_and_bots():
    from agitrack.metrics.collect import _parse_co_authors

    body = (
        "Add a feature\n\n"
        "Co-authored-by: Alice Example <alice@example.com>\n"
        "Co-Authored-By: Bob Example <bob@users.noreply.github.com>\n"
        "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>\n"
        "Co-authored-by: github-actions[bot] <github-actions[bot]@users.noreply.github.com>\n"
        "Co-authored-by: Alice Example <alice@example.com>\n"  # duplicate
    )
    assert _parse_co_authors(body) == [
        ("Alice Example", "alice@example.com"),
        ("Bob Example", "bob@users.noreply.github.com"),
    ]


def _co_authored_dashboard(subject: str):
    from agitrack.metrics.collect import Dashboard

    stat = CommitStat(
        sha="s1",
        author="Alex Doe",
        email="alex@example.com",
        subject=subject,
        kind="agent",
        timestamp=1_700_000_000,
        co_authors=[("Robin Roe", "robin@example.com")],
    )
    return Dashboard(repo="r", branch="main", stats=[stat]), stat


def test_co_authored_commit_is_filterable_under_every_committer():
    from agitrack.metrics import dashboard_data

    dash, stat = _co_authored_dashboard("Pair feature")
    # Both the primary author and the co-author are committers of this one commit.
    assert set(dash.committers_of(stat)) == {"Alex Doe", "Robin Roe"}
    data = dashboard_data(dash)
    # Both surface as filter options, and the commit lists both committers.
    assert {"Alex Doe", "Robin Roe"} <= set(data["committers"])
    entry = next(c for c in data["commits"] if c["subject"] == "Pair feature")
    assert set(entry["committers"]) == {"Alex Doe", "Robin Roe"}


def test_bot_and_ai_primary_authors_are_not_committers():
    from agitrack.metrics.collect import Dashboard
    from agitrack.metrics.web import _options

    bot = CommitStat(
        sha="b1",
        author="github-actions[bot]",
        email="41898282+github-actions[bot]@users.noreply.github.com",
        subject="Automated release",
        kind="untracked",
    )
    human = CommitStat(sha="h1", author="Alex Doe", email="alex@example.com", subject="Real work", kind="agent")
    dash = Dashboard(repo="r", branch="main", stats=[bot, human])
    # The bot is the primary author but is not a committer: empty here, absent
    # from the filter options and the per-committer breakdown.
    assert dash.committers_of(bot) == []
    assert dash.committers_of(human) == ["Alex Doe"]
    assert _options(dash)["committers"] == ["Alex Doe"]
    assert "github-actions[bot]" not in dash.by_author


def test_filter_stats_matches_any_committer():
    from agitrack.metrics.web import _filter_stats, _options

    dash, _ = _co_authored_dashboard("Shared work")
    # Filtering on the co-author (not the primary git author) still returns it...
    for who in ("Robin Roe", "Alex Doe"):
        matched = _filter_stats(dash, author=who, backend="", model="", frm=0, to=0)
        assert [s.subject for s in matched] == ["Shared work"], who
    # ...and both names appear as selectable committer options.
    assert {"Alex Doe", "Robin Roe"} <= set(_options(dash)["committers"])


def test_resolve_logins_falls_back_to_empty_without_gh(monkeypatch, tmp_path):
    from agitrack.metrics import github

    github._reset_cache_for_tests()
    monkeypatch.setattr(github.shutil, "which", lambda _name: None)  # gh not installed
    repo = GitRepo.init(tmp_path)
    # No gh → empty mapping (never raises), so callers fall back to the heuristic.
    assert github.resolve_logins(repo) == {}


def test_gh_status_reports_missing_unauth_and_ok(monkeypatch):
    from agitrack.metrics import github

    # Not installed.
    monkeypatch.setattr(github.shutil, "which", lambda _name: None)
    assert github.gh_status() == "missing"
    assert github.gh_available() is False

    # Installed but the auth check fails (not logged in).
    monkeypatch.setattr(github.shutil, "which", lambda _name: "/usr/bin/gh")

    class _Fail:
        returncode = 1

    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: _Fail())
    assert github.gh_status() == "unauthenticated"
    assert github.gh_available() is False

    # Installed and authenticated.
    class _Ok:
        returncode = 0

    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: _Ok())
    assert github.gh_status() == "ok"
    assert github.gh_available() is True


def test_gh_status_unauthenticated_when_check_errors(monkeypatch):
    from agitrack.metrics import github

    monkeypatch.setattr(github.shutil, "which", lambda _name: "/usr/bin/gh")

    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(github.subprocess, "run", boom)
    assert github.gh_status() == "unauthenticated"


def test_resolve_logins_parses_gh_tsv_and_caches(monkeypatch, tmp_path):
    from agitrack.metrics import github

    repo = GitRepo.init(tmp_path)  # before patching subprocess (git uses it too)
    github._reset_cache_for_tests()
    monkeypatch.setattr(github.shutil, "which", lambda _name: "/usr/bin/gh")
    calls = {"n": 0}

    class _Result:
        returncode = 0
        stdout = "sha1\toctocat\nsha2\thubber\n"

    def fake_run(*args, **kwargs):
        calls["n"] += 1
        return _Result()

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    assert github.resolve_logins(repo) == {"sha1": "octocat", "sha2": "hubber"}
    github.resolve_logins(repo)  # cached: no second subprocess call
    assert calls["n"] == 1


def test_cached_logins_non_blocking_then_populates_in_background(monkeypatch, tmp_path):
    # The live dashboard's hot path must not block on the (paginated, networked) gh
    # crawl: a cold call returns {} at once and refreshes in the background; the
    # resolved logins are then served from the cache.
    import time

    from agitrack.metrics import github

    repo = GitRepo.init(tmp_path)  # before patching subprocess (git uses it too)
    github._reset_cache_for_tests()
    monkeypatch.setattr(github.shutil, "which", lambda _name: "/usr/bin/gh")
    ran = threading.Event()

    class _Result:
        returncode = 0
        stdout = "sha1\toctocat\n"

    def fake_run(*args, **kwargs):
        ran.set()
        return _Result()

    monkeypatch.setattr(github.subprocess, "run", fake_run)

    assert github.cached_logins(repo) == {}  # cold: returns immediately, no blocking crawl
    assert ran.wait(timeout=5)  # the crawl ran on a background thread
    deadline = time.monotonic() + 5
    while github.cached_logins(repo) == {} and time.monotonic() < deadline:
        time.sleep(0.01)
    assert github.cached_logins(repo) == {"sha1": "octocat"}  # served from the warmed cache


def test_cached_logins_serves_warm_cache_without_spawning(monkeypatch, tmp_path):
    import time

    from agitrack.metrics import github

    repo = GitRepo.init(tmp_path)
    github._reset_cache_for_tests()
    github._CACHE[str(repo.repo)] = (time.monotonic(), {"sha1": "octocat"})  # fresh entry
    calls = {"n": 0}
    monkeypatch.setattr(github.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))

    assert github.cached_logins(repo) == {"sha1": "octocat"}
    assert calls["n"] == 0  # warm cache: no background crawl spawned


# --- commit-log sort ----------------------------------------------------------


def test_log_page_sorts_by_lines_tokens_or_newest(tmp_path):
    from agitrack.metrics.web import log_page

    dash = build_dashboard(_demo_repo(tmp_path))

    # Default: newest first (descending commit timestamp).
    dates = [e["ts"] for e in log_page(dash)["entries"]]
    assert dates == sorted(dates, reverse=True)

    # By lines changed (insertions + deletions), most first.
    totals = [e["ins"] + e["del"] for e in log_page(dash, sort="lines")["entries"]]
    assert totals == sorted(totals, reverse=True)

    # By output tokens, most first.
    outputs = [e["tokens"].get("output", 0) for e in log_page(dash, sort="tokens")["entries"]]
    assert outputs == sorted(outputs, reverse=True)

    # Every sort returns the same commits, only reordered.
    assert {e["short"] for e in log_page(dash, sort="lines")["entries"]} == {
        e["short"] for e in log_page(dash)["entries"]
    }


def test_log_page_unknown_sort_falls_back_to_newest_first(tmp_path):
    from agitrack.metrics.web import log_page

    dash = build_dashboard(_demo_repo(tmp_path))
    bogus = [e["ts"] for e in log_page(dash, sort="whatever")["entries"]]
    assert bogus == [e["ts"] for e in log_page(dash, sort="date")["entries"]]


# --- HTML dashboard (filterable web view) --------------------------------------


def _embedded_data(html: str) -> dict:
    match = re.search(r'id="agitrack-data">(.*?)</script>', html, re.S)
    assert match is not None
    return json.loads(match.group(1))


def test_dashboard_data_serializes_every_commit_with_filters(tmp_path):
    data = dashboard_data(build_dashboard(_demo_repo(tmp_path)))

    assert len(data["commits"]) == 7
    assert data["branch"]
    assert data["head"]  # HEAD sha, so the live page can skip no-op re-renders
    # Filter option lists are derived from the (effective) commit fields.
    assert data["backends"] == ["claude"]
    assert data["models"] == ["claude-opus-4-8"]
    assert data["committers"]  # at least the one git author


def test_dashboard_data_covered_commit_inherits_effective_backend(tmp_path):
    data = dashboard_data(build_dashboard(_demo_repo(tmp_path)))

    covered = next(c for c in data["commits"] if c["kind"] == "covered")
    # The backend-made commit carries no metadata of its own, but inherits the
    # cover commit's backend/model so a per-backend filter buckets it correctly.
    assert covered["backend"] is None
    assert covered["eff_backend"] == "claude"
    assert covered["eff_model"] == "claude-opus-4-8"


def test_render_html_embeds_aggregates_and_first_log_page_only(tmp_path):
    html = render_html(_demo_repo(tmp_path))

    assert html.startswith("<!DOCTYPE html>")
    assert "aGiTrack dashboard" in html
    for control in ('id="f-author"', 'id="f-backend"', 'id="f-model"', 'id="f-period"', 'id="f-from"'):
        assert control in html
    # The page embeds server-computed aggregates plus only the FIRST page of the
    # commit log — never the whole history — so browser memory stays bounded no
    # matter how big the repo is. Further pages are fetched from the server.
    data = _embedded_data(html)
    assert "commits" not in data  # the full per-commit list is never embedded
    assert data["agg"]["total"] == 7
    assert data["log"]["total"] == 7
    assert data["log"]["offset"] == 0
    assert len(data["log"]["entries"]) == 7  # one small repo fits on the first page
    entry = data["log"]["entries"][-1]
    assert entry["ts"] > 0 and entry["message"] and "tokens" in entry


def test_render_html_has_unreachable_banner_and_clear_kind_labels(tmp_path):
    html = render_html(_demo_repo(tmp_path))
    # An error banner exists and is wired to show only when the live backend
    # can't be reached (never for a static file:// snapshot).
    assert 'id="neterror"' in html and "Can't reach the aGiTrack dashboard server" in html
    assert "function setOffline" in html and "const LIVE" in html
    assert "if(LIVE) setOffline(true)" in html
    # The commit-kind counts are labelled so "agent 30" reads as a commit count.
    assert "commits by kind:" in html
    # Bar-row labels carry a hover title so any ellipsized cell reveals its full
    # text instead of dead-ending at "…".
    assert 'class="name" title=' in html
    # Markdown heading levels in the expanded commit message render distinctly so
    # the role/section/nested-heading relationship is visible, not flattened.
    for level in ("h3.md-h", "h4.md-h", "h5.md-h", "h6.md-h"):
        assert level in html


def test_web_dashboard_truncates_long_commit_subjects(tmp_path):
    # The commit log caps a displayed subject at 120 chars with an ellipsis; the full
    # subject stays available (hover title + expanded message). Client-side rendering, so
    # assert the JS source carries the cap and applies it in renderLog.
    html = render_html(_demo_repo(tmp_path))
    assert "SUBJECT_MAX = 120" in html
    assert "const truncSubject" in html and "SUBJECT_MAX-1" in html  # ellipsis counts toward the cap
    assert "shown = truncSubject(subj)" in html  # applied to each log row's subject
    assert 'title="' in html  # full subject preserved on hover when truncated


def test_web_dashboard_shows_loading_indicator_on_filter_change(tmp_path):
    # A "loading…" spinner appears while a filter change re-fetches the data and is
    # cleared once the panels render (even on a fetch failure, via finally).
    html = render_html(_demo_repo(tmp_path))
    assert 'id="loading"' in html and 'class="spin"' in html  # the badge + spinner
    assert "@keyframes spin" in html  # the animation
    assert "function showLoading" in html
    # applyFilters shows it before fetching and clears it in a finally.
    assert "showLoading(true)" in html and "} finally { showLoading(false); }" in html


def test_web_dashboard_embeds_cache_write_input_note(tmp_path):
    # The web token panel explains aGiTrack's input convention; the note is client-side
    # gated to show only when cache-write tokens are non-zero.
    html = render_html(_demo_repo(tmp_path))
    assert "input includes cache-creation (cache&nbsp;write) tokens" in html
    assert "(tok.cache_write||0)+(tok.subagent_cache_write||0) > 0" in html  # the gate


def test_filter_bar_is_single_row_with_a_custom_range_popup(tmp_path):
    html = render_html(_demo_repo(tmp_path))
    # The sticky filter bar stays a single row (never wraps) when frozen.
    assert "flex-wrap:nowrap" in html
    # The redundant "scope" readout (it just echoed the committer filter) is gone.
    assert 'id="scope"' not in html
    # from/to are no longer standalone fields — they live in a custom-range popup
    # revealed by selecting "custom range…".
    assert 'id="daterange"' in html and 'id="dr-done"' in html
    assert 'id="f-from"' in html and 'id="f-to"' in html  # still present, inside the popup


def test_log_page_paginates_and_clamps(tmp_path):
    from agitrack.metrics.web import log_page

    dash = build_dashboard(_demo_repo(tmp_path))
    first = log_page(dash, offset=0, limit=3)
    assert first["total"] == 7 and len(first["entries"]) == 3 and first["offset"] == 0
    last = log_page(dash, offset=6, limit=3)
    assert len(last["entries"]) == 1  # only one commit left on the final page
    # Out-of-range / silly inputs are clamped, never raise.
    assert log_page(dash, offset=999, limit=3)["entries"] == []
    assert log_page(dash, offset=-5, limit=0)["offset"] == 0


def test_aggregates_payload_filters_server_side(tmp_path):
    from agitrack.metrics.web import aggregates_payload

    dash = build_dashboard(_demo_repo(tmp_path))
    full = aggregates_payload(dash)
    assert full["agg"]["total"] == 7
    assert "by_committer" in full["agg"] and "commits" not in full
    # A backend filter narrows the aggregates; options still list every backend.
    claude_only = aggregates_payload(dash, backend="claude")
    assert claude_only["agg"]["total"] <= full["agg"]["total"]
    assert full["options"]["backends"] == ["claude"]


def test_aggregates_payload_includes_per_period_timeseries(tmp_path):
    from agitrack.metrics.web import aggregates_payload

    dash = build_dashboard(_demo_repo(tmp_path))
    ts = aggregates_payload(dash)["timeseries"]
    # Every series is the same length as the bucket-start axis; default = per day.
    n = len(ts["t"])
    assert n >= 1 and ts["granularity"] == "day"
    for key in ("commits", "ai_lines", "output_tokens", "input_tokens"):
        assert len(ts[key]) == n
    # Per-bucket activity: the buckets SUM to the filtered totals (not cumulative).
    assert sum(ts["commits"]) == 7  # all seven demo commits
    # The two agent turns carry _TOKENS (1000 in / 50 out) each.
    assert sum(ts["output_tokens"]) == 100
    assert sum(ts["input_tokens"]) == 2000


def test_timeseries_granularity_buckets_by_calendar_period():
    from agitrack.metrics.web import _timeseries

    def stat(day, *, out=0):
        return CommitStat(
            sha=f"s{day}",
            author="a",
            email="e",
            subject="s",
            kind="agent",
            timestamp=int(__import__("calendar").timegm((2026, 1, day, 12, 0, 0))),
            tokens={"output": out},
        )

    # Three commits: Jan 1, Jan 2, Jan 2 again.
    stats = [stat(1, out=5), stat(2, out=3), stat(2, out=4)]
    day = _timeseries(stats, granularity="day")
    assert day["granularity"] == "day"
    # Two day buckets, contiguous; per-bucket commit counts are 1 then 2.
    assert day["commits"] == [1, 2]
    assert day["output_tokens"] == [5, 7]
    # Month granularity collapses all three into one January bucket.
    month = _timeseries(stats, granularity="month")
    assert month["commits"] == [3] and month["output_tokens"] == [12]
    # An unknown granularity falls back to the default (day).
    assert _timeseries(stats, granularity="bogus")["granularity"] == "day"


def test_timeseries_fills_empty_periods_with_zero():
    from agitrack.metrics.web import _timeseries

    cal = __import__("calendar")

    def stat(day):
        return CommitStat(
            sha=f"s{day}",
            author="a",
            email="e",
            subject="s",
            kind="user",
            timestamp=int(cal.timegm((2026, 1, day, 0, 0, 0))),
        )

    # Commits on Jan 1 and Jan 4 — the two quiet days between read as zeros.
    ts = _timeseries([stat(1), stat(4)], granularity="day")
    assert ts["commits"] == [1, 0, 0, 1]


def test_dashboard_repo_path_abbreviates_home(tmp_path, monkeypatch):
    # A repo under the home directory is shown with ``~`` so the public dashboard
    # screenshot never leaks an absolute home path. Build the repo before patching
    # HOME so git still finds the real global identity for the seed commits.
    home = tmp_path / "home"
    repo_dir = home / "projects" / "demo"
    repo_dir.parent.mkdir(parents=True)
    repo = _demo_repo(repo_dir)
    monkeypatch.setenv("HOME", str(home))

    dash = build_dashboard(repo)
    assert dash.repo == "~/projects/demo"
    assert str(home) not in render_html(repo)


def test_dashboard_repo_path_outside_home_is_unchanged(tmp_path, monkeypatch):
    repo = _demo_repo(tmp_path / "work")
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    dash = build_dashboard(repo)
    assert dash.repo == str(repo.repo)


def test_aggregates_payload_reports_full_history_span(tmp_path):
    from agitrack.metrics.web import aggregates_payload

    dash = build_dashboard(_demo_repo(tmp_path))
    span = aggregates_payload(dash)["span"]
    times = [s.timestamp for s in dash.stats if s.timestamp]
    # The from/to date inputs use this to show the real date range, not a blank.
    assert span == {"from": min(times), "to": max(times)}
    # The span is the FULL history, so a committer filter does not shrink it.
    assert aggregates_payload(dash, backend="claude")["span"] == span


def test_dashboard_lists_shared_sessions(tmp_path):
    from agitrack.metrics.web import render_html, shared_sessions_for
    from agitrack.sessions import SharedSessionStore

    repo = _demo_repo(tmp_path)
    SharedSessionStore(repo).publish(
        github_id="alice",
        name="fix-parser",
        transcript="conversation",
        manifest={
            "github_id": "alice",
            "name": "fix-parser",
            "session_id": "s1",
            "model": "claude-opus-4-8",
            "backend": "claude",
            "updated": 123,
        },
    )
    listed = shared_sessions_for(repo)
    assert [f"{s['label']}/{s['name']}" for s in listed] == ["alice/fix-parser"]
    assert listed[0]["owner"] == "alice"
    assert listed[0]["model"] == "claude-opus-4-8"
    # The panel + render hook are wired, and the first paint embeds the list.
    html = render_html(repo)
    assert 'id="shared"' in html and "function renderShared" in html
    assert _embedded_data(html)["shared_sessions"][0]["name"] == "fix-parser"


def test_shared_sessions_for_is_safe_without_sharing(tmp_path):
    from agitrack.metrics.web import shared_sessions_for

    assert shared_sessions_for(_demo_repo(tmp_path)) == []  # nothing shared ⇒ empty, no error


def test_shared_sessions_survive_a_fetch_failure(tmp_path, monkeypatch):
    # A transient fetch error (e.g. racing a concurrent auto-share push) must NOT
    # blank the dashboard's list — fall back to the local ref. Regression for the
    # "shared session disappeared from the dashboard" report.
    from agitrack.metrics.web import shared_sessions_for
    from agitrack.sessions import SharedSessionStore

    repo = _demo_repo(tmp_path)
    SharedSessionStore(repo).publish(
        github_id="alice",
        name="s",
        transcript="t",
        manifest={"github_id": "alice", "name": "s", "session_id": "x", "updated": 1},
    )

    def boom(self):
        raise RuntimeError("network hiccup mid-push")

    monkeypatch.setattr(SharedSessionStore, "fetch_throttled", boom)
    assert [s["name"] for s in shared_sessions_for(repo)] == ["s"]  # still shown from the local ref


def test_timeseries_respects_filters(tmp_path):
    from agitrack.metrics.web import aggregates_payload

    dash = build_dashboard(_demo_repo(tmp_path))
    full = aggregates_payload(dash)["timeseries"]
    # A future-only window filters every commit out — an empty, still-valid series.
    empty = aggregates_payload(dash, frm=4102444800)["timeseries"]  # year 2100
    assert empty["t"] == [] and empty["commits"] == []
    assert sum(full["commits"]) == 7


def test_render_html_wires_the_activity_chart(tmp_path):
    html = render_html(_demo_repo(tmp_path))
    # The plot section, canvas, legend, granularity selector, and toggle/redraw
    # wiring are present, and the initial payload embeds the series so the first
    # paint needs no fetch.
    assert "activity over time" in html
    assert 'id="ts-canvas"' in html and 'id="ts-legend"' in html
    assert 'id="ts-gran"' in html and "function renderChart" in html and "const tsOn" in html
    # Mouse zoom/pan over the loaded buckets is kept (wheel zoom, drag pan,
    # dblclick reset), with a visible hint to scroll.
    assert "function onChartWheel" in html and 'addEventListener("wheel"' in html
    assert "function tsBounds" in html and 'addEventListener("dblclick"' in html
    assert "scroll to zoom" in html
    # The redundant per-plot SHOW range dropdown was removed — the filter's "range"
    # (period) is the single range selector.
    assert 'id="ts-look"' not in html and "function applyLookback" not in html
    data = _embedded_data(html)
    assert "timeseries" in data and sum(data["timeseries"]["commits"]) == 7


def test_dashboard_data_serializes_squash_constituents_for_expansion(tmp_path):
    repo = GitRepo.init(tmp_path)
    _write_lines(repo, "s.txt", 30)
    repo.commit(
        "Squashed PR (#12)\n\n"
        "* first turn\n\n"
        "# aGiTrack Metadata\ncommit_type: agent\nbackend: claude\nmodel: claude-opus-4-8\n"
        "tokens_since_last_commit_output: 200\n\n"
        "* second turn\n\n"
        "# aGiTrack Metadata\ncommit_type: agent\nbackend: opencode\nmodel: qwen\n"
        "tokens_since_last_commit_output: 50\n"
    )

    data = dashboard_data(build_dashboard(repo))
    squash = next(c for c in data["commits"] if c["subject"].startswith("Squashed"))
    # The original commits ride along so the log entry can expand into them.
    assert len(squash["parts"]) == 2
    assert [p["model"] for p in squash["parts"]] == ["claude-opus-4-8", "qwen"]
    assert squash["parts"][0]["tokens"]["output"] == 200
    assert squash["parts"][0]["message"]  # full text for the nested view


def test_dashboard_data_links_commits_to_github_when_remote_present(tmp_path):
    dash = build_dashboard(_demo_repo(tmp_path))
    dash.commit_base = "https://github.com/core-aix/agit/commit/"  # as a GitHub remote would yield
    data = dashboard_data(dash)
    assert all(c["url"].startswith("https://github.com/core-aix/agit/commit/") for c in data["commits"])


def test_dashboard_data_omits_links_without_github_remote(tmp_path):
    data = dashboard_data(build_dashboard(_demo_repo(tmp_path)))  # local repo, no remote
    assert all(c["url"] == "" for c in data["commits"])


# --- live localhost server -----------------------------------------------------


def _serve(repo: GitRepo):
    server = build_server(repo, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return server, thread, base


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode("utf-8")


def test_dashboard_server_serves_aggregates_and_paginated_log(tmp_path):
    repo = _demo_repo(tmp_path)
    server, thread, base = _serve(repo)
    try:
        html = _get(base + "/")
        assert "<!DOCTYPE html>" in html and 'id="f-author"' in html

        # /data returns aggregates only — never the full commit list.
        data = json.loads(_get(base + "/data"))
        assert "commits" not in data and data["agg"]["total"] == 7
        first_head = data["head"]

        # /log paginates the commit log without loading everything at once.
        page1 = json.loads(_get(base + "/log?limit=3&offset=0"))
        page2 = json.loads(_get(base + "/log?limit=3&offset=3"))
        assert page1["total"] == 7 and len(page1["entries"]) == 3
        assert page2["offset"] == 3
        assert {e["short"] for e in page1["entries"]}.isdisjoint(e["short"] for e in page2["entries"])

        # Both are recomputed each request, so a new commit shows up live.
        _write_lines(repo, "live.txt", 3)
        repo.commit(_agent_message("add a live change", tokens=_TOKENS))
        refreshed = json.loads(_get(base + "/data"))
        assert refreshed["head"] != first_head and refreshed["agg"]["total"] == 8

        # A filter narrows the aggregates server-side (committer names can carry
        # spaces, e.g. CI's "aGiTrack CI", so the query must be URL-encoded).
        query = urllib.parse.urlencode({"author": data["options"]["committers"][0]})
        only_me = json.loads(_get(base + "/data?" + query))
        assert only_me["agg"]["total"] <= refreshed["agg"]["total"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _add_feature_branch(repo: GitRepo) -> str:
    """Branch off the current tip and add one extra agent commit on ``feature``,
    leaving the original branch checked out. Returns the original branch name."""
    main = repo.current_branch()
    repo.create_branch("feature", main)
    repo.switch("feature")
    _write_lines(repo, "feat.txt", 5)
    repo.commit(_agent_message("feature work", tokens=_TOKENS))
    repo.switch(main)
    return main


def test_build_dashboard_for_explicit_branch_reports_that_branch(tmp_path):
    repo = _demo_repo(tmp_path)  # 7 commits on the default branch
    main = _add_feature_branch(repo)

    dash = build_dashboard(repo, "feature")
    assert dash.branch == "feature"
    assert dash.total_commits == 8  # the default branch's 7 + the feature commit
    # The selector lists every branch, with the viewed one first.
    assert dash.branches[0] == "feature"
    assert main in dash.branches

    # The default (HEAD) view still reports the current branch and its history.
    head = build_dashboard(repo)
    assert head.branch == main and head.total_commits == 7
    assert set(head.branches) == {main, "feature"}


def test_dashboard_server_switches_branches_for_per_branch_views(tmp_path):
    repo = _demo_repo(tmp_path)
    main = _add_feature_branch(repo)
    server, thread, base = _serve(repo)
    try:
        # The default view is the checked-out branch; the picker offers every branch.
        data = json.loads(_get(base + "/data"))
        assert data["branch"] == main and data["agg"]["total"] == 7
        assert set(data["options"]["branches"]) == {main, "feature"}

        # ?branch=<name> re-scopes both the aggregates and the commit log to that ref.
        feat = json.loads(_get(base + "/data?branch=feature"))
        assert feat["branch"] == "feature" and feat["agg"]["total"] == 8
        flog = json.loads(_get(base + "/log?branch=feature"))
        assert any("feature work" in e["subject"] for e in flog["entries"])

        # An unknown or injected ref never reaches git — it falls back to HEAD.
        bogus = json.loads(_get(base + "/data?branch=" + urllib.parse.quote("main; rm -rf /")))
        assert bogus["branch"] == main and bogus["agg"]["total"] == 7
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_server_index_embeds_shared_sessions(tmp_path):
    # Regression: the live index page must embed shared sessions on first paint,
    # not only fill them in on the first /data poll (the bug where the dashboard
    # showed no shared sessions until a refresh — and never for a file:// snapshot).
    from agitrack.sessions import SharedSessionStore

    repo = _demo_repo(tmp_path)
    SharedSessionStore(repo).publish(
        github_id="alice",
        name="s1",
        transcript="t",
        manifest={"github_id": "alice", "name": "s1", "session_id": "x", "updated": 1},
    )
    server, thread, base = _serve(repo)
    try:
        html = _get(base + "/")
        embedded = json.loads(re.search(r'id="agitrack-data">(.*?)</script>', html, re.S).group(1))
        assert [s["name"] for s in embedded["shared_sessions"]] == ["s1"]
        served = json.loads(_get(base + "/data"))
        assert [s["name"] for s in served["shared_sessions"]] == ["s1"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_server_404s_unknown_paths(tmp_path):
    server, thread, base = _serve(_demo_repo(tmp_path))
    try:
        try:
            _get(base + "/nope")
            raise AssertionError("expected HTTP 404")
        except urllib.error.HTTPError as error:
            assert error.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_server_is_threaded_and_swallows_client_disconnects(tmp_path, capsys):
    import http.server

    server = build_server(GitRepo.init(tmp_path), port=0)
    try:
        # Threaded so one slow request (e.g. the first gh lookup) never blocks
        # the live page's polling.
        assert isinstance(server, http.server.ThreadingHTTPServer)
        # A client vanishing mid-response (a superseded poll, a closed tab) must
        # not spew a BrokenPipeError traceback into aGiTrack's console.
        try:
            raise BrokenPipeError(32, "Broken pipe")
        except BrokenPipeError:
            server.handle_error(object(), ("127.0.0.1", 0))
        assert "Traceback" not in capsys.readouterr().err
    finally:
        server.server_close()


# --- CLI -----------------------------------------------------------------------


def test_cli_dashboard_html_is_default_and_serves_on_localhost(tmp_path, monkeypatch):
    _demo_repo(tmp_path)
    served: dict[str, GitRepo] = {}

    def fake_serve(repo, **kwargs):
        served["repo"] = repo
        return 0

    monkeypatch.setattr(metrics, "serve_dashboard", fake_serve)

    # Bare --dashboard now means html, which serves on localhost.
    rc = cli.main(["--dashboard", "--repo", str(tmp_path)])

    assert rc == 0
    assert served["repo"].repo == GitRepo.discover(tmp_path).repo


def test_cli_dashboard_shorthand_d_serves_like_dashboard(tmp_path, monkeypatch):
    _demo_repo(tmp_path)
    served: dict[str, GitRepo] = {}

    def fake_serve(repo, **kwargs):
        served["repo"] = repo
        return 0

    monkeypatch.setattr(metrics, "serve_dashboard", fake_serve)

    # `-d` is shorthand for `--dashboard`; bare form defaults to html (serve).
    assert cli.main(["-d", "--repo", str(tmp_path)]) == 0
    assert served["repo"].repo == GitRepo.discover(tmp_path).repo


def test_cli_dashboard_shorthand_d_accepts_text(tmp_path, capsys, monkeypatch):
    _demo_repo(tmp_path)
    monkeypatch.setattr(
        metrics, "serve_dashboard", lambda *a, **k: (_ for _ in ()).throw(AssertionError("text must not serve"))
    )

    assert cli.main(["-d", "text", "--repo", str(tmp_path)]) == 0
    assert "aGiTrack Dashboard" in capsys.readouterr().out


def test_cli_dashboard_text_prints_report_and_exits(tmp_path, capsys, monkeypatch):
    _demo_repo(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("dashboard must not prompt")))
    # The text path must never start a server.
    monkeypatch.setattr(
        metrics, "serve_dashboard", lambda *a, **k: (_ for _ in ()).throw(AssertionError("text must not serve"))
    )

    rc = cli.main(["--dashboard", "text", "--repo", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "aGiTrack Dashboard" in out
    assert "aGiTrack-tracked commits" in out


def test_cli_dashboard_outside_repo_fails_cleanly(tmp_path, capsys, monkeypatch):
    (tmp_path / "plain").mkdir()
    monkeypatch.setattr(
        metrics, "serve_dashboard", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not serve"))
    )

    rc = cli.main(["--dashboard", "--repo", str(tmp_path / "plain")])

    assert rc == 1
    assert "Not a Git repository" in capsys.readouterr().out


def test_cli_dashboard_missing_directory_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(
        metrics, "serve_dashboard", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not serve"))
    )
    assert cli.main(["--dashboard", "--repo", str(tmp_path / "nowhere")]) == 1
