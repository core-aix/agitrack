"""The storyline page (`agitrack/metrics/story.py`): the repo's history told as chapters.

Real temp git repos throughout, so segmentation, the store, the incremental extend and both
servers' endpoints run for real. The only thing faked is the backend agent (a queue of
canned JSON replies), which is also how these tests pin the honesty rules: a commit id the
model invents is dropped, a quote is always read back from the real commit, and no commit
of a batch may fall out of the story.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agitrack.backends.base import AgentResult, TokenUsage
from agitrack.commits import build_agent_commit_message
from agitrack.git import GitRepo
from agitrack.metrics import build_server
from agitrack.metrics import learn, story
from agitrack.metrics.collect import CommitStat, build_dashboard

DAY = 86400


# --------------------------------------------------------------------------- helpers


class _QueuedBackend:
    """Stands in for the real backend: hands out the next canned reply per call."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def run(
        self, prompt, *, model, session_id, bare=False, system_prompt=None, commit_guidance=True, timeout_seconds=None
    ):
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else "{}"
        if isinstance(reply, Exception):
            raise reply
        return AgentResult(
            backend="claude",
            session_id=None,
            model=model,
            final_response=reply,
            exit_code=0,
            tokens=TokenUsage(),
        )


def _fake_agent(monkeypatch, replies):
    fake = _QueuedBackend(replies)
    monkeypatch.setattr(learn.LearningBackendChoice, "build", lambda self: fake)
    monkeypatch.setattr(
        learn,
        "resolve_learning_backend",
        lambda root: learn.LearningBackendChoice(
            backend_name="claude", model="claude-opus-5", backend_source="config", model_source="config"
        ),
    )
    return fake


def _stat(sha_char: str, prompt: str, ts: int, *, kind: str = "agent", subject: str = "") -> CommitStat:
    return CommitStat(
        sha=sha_char * 40,
        author="tester",
        email="t@t",
        subject=subject or f"<aGiTrack> {prompt[:40]}",
        kind=kind,
        timestamp=ts,
        prompt=prompt,
        user_prompts=[prompt],
        tokens={"input": 1000, "output": 200},
        insertions=30,
        deletions=5,
    )


def _chapter_reply(chapters) -> str:
    return json.dumps({"chapters": chapters})


_ARC = json.dumps(
    {
        "title": "How the parser learned to read",
        "tagline": "one repo, several minds, a lot of retries",
        "arc": "It started as a script and ended as a tool.",
        "acts": [{"title": "the beginning", "blurb": "first light", "start": 1}],
    }
)


def _repo_with_history(tmp_path: Path, *, prompts, gap_days=1, start=1_750_000_000) -> GitRepo:
    """A real repo whose commits carry aGiTrack agent metadata and interaction traces,
    spaced far enough apart that each becomes its own episode."""
    repo = GitRepo.init(tmp_path)
    # GitRepo.init's seed commit is stamped NOW, which would sit after this fabricated
    # timeline and make "what landed since" meaningless; date it just before the story.
    seed = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start - DAY))
    repo._run(
        # --amend keeps the original author date, so GIT_AUTHOR_DATE alone would not move it.
        ["git", "commit", "-q", "--amend", "--no-edit", "--allow-empty", "--date", seed],
        env={"GIT_AUTHOR_DATE": seed, "GIT_COMMITTER_DATE": seed},
    )
    for i, prompt in enumerate(prompts):
        (tmp_path / f"f{i}.py").write_text("\n".join(f"line {n}" for n in range(10 + i)), encoding="utf-8")
        repo.stage_paths([f"f{i}.py"])
        when = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start + i * gap_days * DAY))
        message = build_agent_commit_message(
            latest_prompt=prompt,
            trace=[{"role": "user", "content": prompt}, {"role": "agent", "content": "done"}],
            backend="claude",
            backend_session_id="ses-1",
            agitrack_session_id="agit-1",
            model="claude-opus-5",
            token_usage={"input": 900, "output": 120},
        )
        repo._run(
            ["git", "commit", "-q", "-F", "-"],
            input_text=message,
            env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
        )
    return repo


def _view_of(repo: GitRepo):
    from agitrack.metrics.files import git_browser
    from agitrack.metrics.insights import context_from_browser

    dash = build_dashboard(repo)
    browser = git_browser(repo, dash.stats, "HEAD")
    _files, sha_paths = context_from_browser(browser, dash.stats)
    return dash.stats, sha_paths


# --------------------------------------------------------------------------- segmentation


def test_episodes_break_where_the_work_actually_paused():
    base = 1_750_000_000
    stats = [
        _stat("a", "start the parser", base),
        _stat("b", "keep going", base + 600),  # same sitting
        _stat("c", "next day's idea", base + 2 * DAY),  # a real pause
    ]
    episodes = story.segment_episodes(stats)
    assert [len(e.stats) for e in episodes] == [2, 1]
    assert episodes[0].insertions == 60 and episodes[0].tokens == 2400


def test_one_marathon_sitting_still_becomes_several_beats():
    base = 1_750_000_000
    stats = [_stat(chr(ord("a") + i), f"step {i}", base + i * 60) for i in range(20)]
    episodes = story.segment_episodes(stats)
    assert len(episodes) == 2  # capped at _EPISODE_MAX_COMMITS, not one 20-commit blob
    assert all(len(e.stats) <= story._EPISODE_MAX_COMMITS for e in episodes)


def test_housekeeping_commits_are_not_story():
    base = 1_750_000_000
    stats = [_stat("a", "real work", base), _stat("b", "", base + 60, kind="agitrack-ops")]
    assert [s.short for s in story.story_stats(stats)] == ["a" * 7]


def test_the_outline_needs_no_agent_at_all():
    base = 1_750_000_000
    rows = story.outline([_stat("a", "first thing", base), _stat("b", "later thing", base + 3 * DAY)])
    assert [r["title"] for r in rows][0].startswith("<aGiTrack> later thing")  # newest first
    assert rows[0]["prompt"] == "later thing"  # the developer's own words, unedited
    assert rows[0]["commits"] == 1


def test_eras_are_split_by_us_not_by_the_model():
    """Letting the agent choose the groupings AND name them put 26 of 44 chapters in the last
    act, titled after its first three ("Windows Finally Gets Invited In" for a month that was
    mostly other things). Boundaries are ours; naming is all the agent is asked for."""
    chapters = [
        {"id": f"c{i}", "when": "2026-06-%02d" % (i + 1), "from": 1_000 + i * 100, "to": 1_000 + i * 100 + 50}
        for i in range(44)
    ]
    slices = story.act_slices(chapters)
    assert len(slices) == 5
    assert slices[0][0] == 0 and slices[-1][1] == len(chapters) - 1  # every chapter is in an era
    for (start, end), (next_start, _) in zip(slices, slices[1:]):
        assert next_start == end + 1  # contiguous, no gaps and no overlaps
    sizes = [end - start + 1 for start, end in slices]
    assert max(sizes) - min(sizes) <= 5  # and no era swallows the story
    assert story.act_slices(chapters[:3]) == [(0, 2)]  # a tiny story is one era
    assert story.act_slices([]) == []


def test_an_era_boundary_prefers_a_real_pause_in_the_work():
    chapters = []
    for i in range(12):
        start = 1_000 + i * 100 + (5_000 if i >= 7 else 0)  # a long gap before chapter 7
        chapters.append({"id": f"c{i}", "when": "d", "from": start, "to": start + 50})
    slices = story.act_slices(chapters, target=2)
    assert slices[1][0] == 7  # the era starts where the work actually restarted


def test_the_agent_only_names_the_eras(tmp_path, monkeypatch):
    repo = _repo_with_history(tmp_path, prompts=[f"step {i}" for i in range(8)])
    stats, sha_paths = _view_of(repo)
    ordered = [stat.short for stat in story.story_stats(stats)]
    _fake_agent(
        monkeypatch,
        [
            _chapter_reply(
                [
                    {"id": f"c{i}", "title": f"Chapter {i}", "summary": "s", "shas": [sha]}
                    for i, sha in enumerate(ordered)
                ]
            ),
            json.dumps(
                {
                    "title": "A title",
                    "tagline": "A tagline",
                    "arc": "It happened.",
                    # named by number; the model never says where an era starts
                    "eras": [
                        {"n": 1, "title": "The beginning", "blurb": "b"},
                        {"n": 2, "title": "The rest", "blurb": "b"},
                    ],
                }
            ),
        ],
    )
    built = story.build_story(tmp_path, stats, sha_paths, branch="main", mode="rewrite")
    acts = built["acts"]
    assert [act["title"] for act in acts] == ["The beginning", "The rest"]
    assert acts[0]["start_id"] == built["chapters"][0]["id"]  # the story always opens in act one
    assert acts[-1]["end_id"] == built["chapters"][-1]["id"]  # ...and the last era runs to the end
    assert sum(act["chapters"] for act in acts) == len(built["chapters"])  # every chapter is covered
    # An era the model forgot to name still gets a label rather than an empty heading.
    assert all(act["title"] for act in acts)


# --------------------------------------------------------------------------- building


def test_build_writes_chapters_with_real_commits_and_quoted_asks(tmp_path, monkeypatch):
    repo = _repo_with_history(tmp_path, prompts=["make the parser stream", "now make it fast"])
    stats, sha_paths = _view_of(repo)
    shas = [stat.short for stat in story.story_stats(stats)]
    fake = _fake_agent(
        monkeypatch,
        [
            _chapter_reply(
                [
                    {
                        "id": "streaming",
                        "title": "The parser learns to stream",
                        "kicker": "turning point",
                        "emoji": "🌊",
                        "summary": "Reading the file in one gulp had to go.",
                        "detail": "It started with **memory**.\n\nThen it got fast.",
                        # shas[0] is the repo's seed commit; the first real turn is shas[1].
                        "thoughts": [{"sha": shas[1], "note": "They wanted it incremental from the start."}],
                        "shas": shas,
                    }
                ]
            ),
            _ARC,
        ],
    )

    built = story.build_story(tmp_path, stats, sha_paths, branch="main", tone="plain", mode="rewrite")

    assert built["title"] == "How the parser learned to read"
    assert built["tagline"] and built["arc"]
    chapter = built["chapters"][0]
    assert chapter["title"] == "The parser learns to stream"
    assert chapter["kicker"] == "turning point"
    assert [row["short"] for row in chapter["commits"]] == shas  # oldest first inside a chapter
    assert chapter["stats"]["commits"] == len(shas) and chapter["stats"]["turns"] == 2
    assert chapter["stats"]["files"]  # which files the chapter touched
    # The quoted ask is the developer's OWN prompt, read back from the commit; only
    # the note comes from the model.
    assert chapter["thoughts"][0]["quote"] == "make the parser stream"
    assert chapter["thoughts"][0]["note"].startswith("They wanted it")
    # ...and it is persisted, so re-opening the page costs nothing.
    assert story.StoryStore(tmp_path).get("main")["chapters"][0]["id"] == "streaming"
    assert len(fake.prompts) == 2  # one chapter call, one arc call
    assert "asked: " in fake.prompts[0]  # the digest carries the prompts, not just subjects


def test_a_commit_id_the_model_invents_is_dropped(tmp_path, monkeypatch):
    repo = _repo_with_history(tmp_path, prompts=["do the thing", "do the other thing"])
    stats, sha_paths = _view_of(repo)
    real = [stat.short for stat in story.story_stats(stats)]
    _fake_agent(
        monkeypatch,
        [
            _chapter_reply(
                [
                    {
                        "title": "A chapter",
                        "summary": "s",
                        "shas": [real[0], "deadbee", "0" * 40],
                        "thoughts": [{"sha": "deadbee", "note": "invented"}],
                    }
                ]
            ),
            _ARC,
        ],
    )
    built = story.build_story(tmp_path, stats, sha_paths, branch="main", mode="rewrite")
    chapter = built["chapters"][0]
    # The invented ids are gone; the second real commit is still in the story (folded in
    # below), and the fabricated "thought" fell back to a real prompt of this chapter.
    assert set(row["short"] for row in chapter["commits"]) == set(real)
    assert chapter["thoughts"][0]["quote"] in ("do the thing", "do the other thing")
    assert chapter["thoughts"][0]["sha"] in [stat.sha for stat in stats]


def test_no_commit_of_a_batch_falls_out_of_the_story(tmp_path, monkeypatch):
    repo = _repo_with_history(tmp_path, prompts=["one", "two", "three"])
    stats, sha_paths = _view_of(repo)
    real = [stat.short for stat in story.story_stats(stats)]
    _fake_agent(
        monkeypatch,
        [_chapter_reply([{"title": "Only about the first", "summary": "s", "shas": [real[0]]}]), _ARC],
    )
    built = story.build_story(tmp_path, stats, sha_paths, branch="main", mode="rewrite")
    told = {sha for chapter in built["chapters"] for sha in chapter["shas"]}
    assert told == set(story.story_stats(stats)[i].sha for i in range(len(real)))  # the ones the model
    assert built["covered"] == len(real)  # forgot were folded into the nearest chapter


def test_extending_tells_only_what_is_new(tmp_path, monkeypatch):
    repo = _repo_with_history(tmp_path, prompts=["first push", "second push"])
    stats, sha_paths = _view_of(repo)
    first = [stat.short for stat in story.story_stats(stats)]
    _fake_agent(
        monkeypatch,
        [_chapter_reply([{"id": "one", "title": "The first push", "summary": "s", "shas": first}]), _ARC],
    )
    story.build_story(tmp_path, stats, sha_paths, branch="main", mode="rewrite")

    # A new sitting of work lands days later.
    when = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(1_750_000_000 + 9 * DAY))
    (tmp_path / "new.py").write_text("fresh\n", encoding="utf-8")
    repo.stage_paths(["new.py"])
    repo._run(
        ["git", "commit", "-q", "-F", "-"],
        input_text=build_agent_commit_message(
            latest_prompt="add the new thing",
            trace=[{"role": "user", "content": "add the new thing"}],
            backend="claude",
            backend_session_id="ses-2",
            agitrack_session_id="agit-1",
            model="claude-opus-5",
            token_usage={"input": 10, "output": 5},
        ),
        env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
    )
    stats2, sha_paths2 = _view_of(repo)
    newest = story.story_stats(stats2)[-1].short
    fake = _fake_agent(
        monkeypatch,
        [_chapter_reply([{"id": "two", "title": "The new thing", "summary": "s", "shas": [newest]}]), _ARC],
    )
    built = story.build_story(tmp_path, stats2, sha_paths2, branch="main", mode="extend")

    assert [c["id"] for c in built["chapters"]] == ["one", "two"]  # kept, then continued
    assert newest not in [s for c in built["chapters"][:1] for s in c["shas"]]
    # The already-told commits are never sent to the agent a second time.
    assert first[0] not in fake.prompts[0]
    assert newest in fake.prompts[0]
    # ...and the earlier chapters ride along as context so the telling continues.
    assert "The first push" in fake.prompts[0]


def test_extending_with_nothing_new_says_so(tmp_path, monkeypatch):
    repo = _repo_with_history(tmp_path, prompts=["only thing"])
    stats, sha_paths = _view_of(repo)
    shas = [stat.short for stat in story.story_stats(stats)]
    _fake_agent(monkeypatch, [_chapter_reply([{"title": "All of it", "summary": "s", "shas": shas}]), _ARC])
    story.build_story(tmp_path, stats, sha_paths, branch="main", mode="rewrite")
    with pytest.raises(story.StoryError, match="nothing new"):
        story.build_story(tmp_path, stats, sha_paths, branch="main", mode="extend")
    # ...and the page is told so straight away, instead of watching a spinner that fails.
    answer = story.start_build(tmp_path, stats, sha_paths, branch="main", mode="extend")
    assert "nothing new to tell" in answer["error"] and "building" not in answer
    assert story.build_progress(tmp_path, "main") is None  # no thread was ever started


def test_an_unusable_reply_is_retried_then_told_plainly(tmp_path, monkeypatch):
    # Two batches: the first lands, the second comes back as prose twice. A stretch the model
    # cannot tell must not cost the chapters around it, nor leave a hole in the timeline.
    prompts = [f"step {i}" for i in range(14)]
    repo = _repo_with_history(tmp_path, prompts=prompts, gap_days=1)
    stats, sha_paths = _view_of(repo)
    ordered = [stat.short for stat in story.story_stats(stats)]
    monkeypatch.setattr(story, "_BATCH_EPISODES", 8)
    fake = _fake_agent(
        monkeypatch,
        [
            _chapter_reply([{"id": "part-one", "title": "Part one", "summary": "s", "shas": ordered[:8]}]),
            "here is a nice essay instead",
            "still an essay",
            _ARC,
        ],
    )
    built = story.build_story(tmp_path, stats, sha_paths, branch="main", mode="rewrite")

    assert built["chapters"][0]["id"] == "part-one"
    auto = [chapter for chapter in built["chapters"] if chapter.get("auto")]
    assert auto and auto[0]["shas"]  # the rest is told from the commits themselves
    assert "storyteller could not be reached" in auto[0]["summary"]
    assert _RETRY_NUDGE_MARK in fake.prompts[2]  # ...after being told exactly what went wrong
    told = {sha for chapter in built["chapters"] for sha in chapter["shas"]}
    assert told == {stat.sha for stat in story.story_stats(stats)}  # no hole in the timeline


_RETRY_NUDGE_MARK = "your previous answer could not be used"


def test_a_backend_that_keeps_failing_stops_but_keeps_what_landed(tmp_path, monkeypatch):
    prompts = [f"step {i}" for i in range(14)]
    repo = _repo_with_history(tmp_path, prompts=prompts, gap_days=1)
    stats, sha_paths = _view_of(repo)
    ordered = [stat.short for stat in story.story_stats(stats)]
    monkeypatch.setattr(story, "_BATCH_EPISODES", 8)
    _fake_agent(
        monkeypatch,
        [
            _chapter_reply([{"id": "part-one", "title": "Part one", "summary": "s", "shas": ordered[:8]}]),
            learn.LearnAgentError("the claude backend exited with code 1"),
            learn.LearnAgentError("the claude backend exited with code 1"),
        ],
    )
    with pytest.raises(story.StoryError, match="exited with code 1"):
        story.build_story(tmp_path, stats, sha_paths, branch="main", mode="rewrite")
    stored = story.StoryStore(tmp_path).get("main")
    assert [c["id"] for c in stored["chapters"]] == ["part-one"]
    assert stored["partial"] is True  # and the page can say the telling is unfinished


def test_the_arc_call_failing_never_loses_the_chapters(tmp_path, monkeypatch):
    repo = _repo_with_history(tmp_path, prompts=["a thing"])
    stats, sha_paths = _view_of(repo)
    shas = [stat.short for stat in story.story_stats(stats)]
    _fake_agent(
        monkeypatch,
        [_chapter_reply([{"id": "c1", "title": "A thing happened", "summary": "s", "shas": shas}]), "nonsense"],
    )
    built = story.build_story(tmp_path, stats, sha_paths, branch="main", mode="rewrite")
    assert [c["id"] for c in built["chapters"]] == ["c1"]
    assert built["partial"] is False
    assert "title" not in built or built["title"]  # no title is fine; a broken one is not


def test_the_first_build_writes_headlines_only_and_a_chapter_is_written_when_opened(tmp_path, monkeypatch):
    """Generation is lazy: covering a whole history must not cost a body for every chapter
    nobody has read yet. The outline pass writes headlines; opening a chapter writes it out,
    once, and stores it."""
    repo = _repo_with_history(tmp_path, prompts=["teach it to stream", "now make it fast"])
    stats, sha_paths = _view_of(repo)
    shas = [stat.short for stat in story.story_stats(stats)]
    outline_fake = _fake_agent(
        monkeypatch,
        [_chapter_reply([{"id": "streaming", "title": "It learns to stream", "summary": "s", "shas": shas}]), _ARC],
    )
    built = story.build_story(tmp_path, stats, sha_paths, branch="main", mode="rewrite")

    chapter = built["chapters"][0]
    assert chapter["title"] and chapter["summary"]  # the headline is there...
    assert chapter["detail"] == "" and chapter["thoughts"] == []  # ...and nothing was paid for below it
    assert len(outline_fake.prompts) == 2  # one outline call for the whole history, one arc call
    assert "Headlines only." in outline_fake.prompts[0]

    expand_fake = _fake_agent(
        monkeypatch,
        [
            json.dumps(
                {
                    "detail": "Reading the whole file was the problem.",
                    "thoughts": [{"sha": shas[1], "note": "They wanted it incremental."}],
                }
            )
        ],
    )
    answer = story.expand_chapter(tmp_path, stats, sha_paths, branch="main", chapter_id="streaming")
    assert answer["chapter"]["detail"].startswith("Reading the whole file")
    assert answer["chapter"]["thoughts"][0]["quote"] == "teach it to stream"  # the real prompt, again
    assert len(expand_fake.prompts) == 1
    assert "It learns to stream" in expand_fake.prompts[0]  # the headline rides along, unchanged

    # Persisted, so re-opening it later costs nothing at all.
    stored = story.StoryStore(tmp_path).get("main")["chapters"][0]
    assert stored["detail"] and stored["thoughts"]
    again = story.expand_chapter(tmp_path, stats, sha_paths, branch="main", chapter_id="streaming")
    assert again["chapter"]["detail"] == stored["detail"]
    assert len(expand_fake.prompts) == 1  # no second call


def test_opening_a_chapter_that_is_no_longer_in_the_story_says_so(tmp_path, monkeypatch):
    repo = _repo_with_history(tmp_path, prompts=["only thing"])
    stats, sha_paths = _view_of(repo)
    assert "no story" in story.expand_chapter(tmp_path, stats, sha_paths, branch="main", chapter_id="x")["error"]
    shas = [stat.short for stat in story.story_stats(stats)]
    _fake_agent(monkeypatch, [_chapter_reply([{"id": "c1", "title": "It", "summary": "s", "shas": shas}]), _ARC])
    story.build_story(tmp_path, stats, sha_paths, branch="main", mode="rewrite")
    answer = story.expand_chapter(tmp_path, stats, sha_paths, branch="main", chapter_id="gone")
    assert "no longer part of the story" in answer["error"]


def test_the_chapter_route_is_wired(tmp_path, monkeypatch):
    repo = _repo_with_history(tmp_path, prompts=["one"])
    stats, sha_paths = _view_of(repo)
    answer = story.handle_story_post(
        "/story/chapter", {"branch": "main", "id": "nope"}, root=tmp_path, view=lambda b: (stats, sha_paths)
    )
    assert answer and "error" in answer


# --------------------------------------------------------------------------- background


def test_a_build_runs_in_the_background_and_reports_progress(tmp_path, monkeypatch):
    repo = _repo_with_history(tmp_path, prompts=["one", "two"])
    stats, sha_paths = _view_of(repo)
    shas = [stat.short for stat in story.story_stats(stats)]
    _fake_agent(monkeypatch, [_chapter_reply([{"title": "It", "summary": "s", "shas": shas}]), _ARC])

    started = story.start_build(tmp_path, stats, sha_paths, branch="main", mode="rewrite")
    assert started["building"] is True
    deadline = time.time() + 10
    while time.time() < deadline:
        progress = story.build_progress(tmp_path, "main")
        if progress and not progress["running"]:
            break
        time.sleep(0.05)
    progress = story.build_progress(tmp_path, "main")
    assert progress and progress["running"] is False and not progress["error"]
    assert story.StoryStore(tmp_path).get("main")["chapters"]


def test_only_one_story_is_written_at_a_time(tmp_path, monkeypatch):
    repo = _repo_with_history(tmp_path, prompts=["one"])
    stats, sha_paths = _view_of(repo)
    release = threading.Event()

    class _Blocking(_QueuedBackend):
        def run(self, prompt, **kwargs):
            release.wait(5)
            return super().run(prompt, **kwargs)

    blocking = _Blocking([_chapter_reply([{"title": "x", "summary": "s", "shas": ["deadbee"]}]), _ARC])
    monkeypatch.setattr(learn.LearningBackendChoice, "build", lambda self: blocking)
    monkeypatch.setattr(
        learn,
        "resolve_learning_backend",
        lambda root: learn.LearningBackendChoice(
            backend_name="claude", model="m", backend_source="config", model_source="config"
        ),
    )
    try:
        story.start_build(tmp_path, stats, sha_paths, branch="main", mode="rewrite")
        second = story.start_build(tmp_path, stats, sha_paths, branch="other", mode="rewrite")
        assert second["busy"] is True and second["error"]
    finally:
        release.set()
        time.sleep(0.2)


# --------------------------------------------------------------------------- page state


def test_state_offers_the_outline_and_says_what_is_uncovered(tmp_path, monkeypatch):
    repo = _repo_with_history(tmp_path, prompts=["one", "two"])
    stats, sha_paths = _view_of(repo)
    state = story.story_state(tmp_path, stats, sha_paths, branch="main", branches=["main"], repo_name="demo")
    assert state["story"] is None
    assert len(state["outline"]) == 3  # two agent turns plus the repo's seed commit
    assert state["meta"]["commits"] == 3 and state["meta"]["uncovered"] == 3
    assert state["meta"]["turns"] == 2  # ...of which two are agent turns

    shas = [stat.short for stat in story.story_stats(stats)]
    _fake_agent(monkeypatch, [_chapter_reply([{"title": "It", "summary": "s", "shas": shas}]), _ARC])
    story.build_story(tmp_path, stats, sha_paths, branch="main", mode="rewrite")
    state = story.story_state(tmp_path, stats, sha_paths, branch="main", branches=["main"], repo_name="demo")
    assert state["meta"]["uncovered"] == 0
    assert state["story"]["chapters"]


def test_forgetting_a_story_leaves_the_commits_alone(tmp_path, monkeypatch):
    repo = _repo_with_history(tmp_path, prompts=["one"])
    stats, sha_paths = _view_of(repo)
    shas = [stat.short for stat in story.story_stats(stats)]
    _fake_agent(monkeypatch, [_chapter_reply([{"title": "It", "summary": "s", "shas": shas}]), _ARC])
    story.build_story(tmp_path, stats, sha_paths, branch="main", mode="rewrite")

    answer = story.handle_story_post(
        "/story/forget", {"branch": "main"}, root=tmp_path, view=lambda branch: (stats, sha_paths)
    )

    assert answer == {"forgotten": True}
    assert story.StoryStore(tmp_path).get("main") is None
    assert build_dashboard(repo).total_commits == 2  # history untouched (seed + the turn)


def test_an_unknown_story_path_is_not_handled(tmp_path):
    assert story.handle_story_post("/story/nope", {}, root=tmp_path, view=lambda b: ([], {})) is None


def test_the_page_paints_without_any_story(tmp_path):
    html = story.story_html(tmp_path)
    assert "storyline" in html or "story" in html
    assert "story/state" in html  # it fetches its data after paint
    assert "__REPO__" not in html and "__PREBOOT_CSS__" not in html
    assert "what they asked for" in html
    # Phone support, like the dashboard's: a narrow-screen block that tightens the timeline
    # rail and stops the outline's numbers from being pushed off the side of the screen.
    assert "@media (max-width:640px)" in html
    # The numbers are drawn, not described: a bar for what a chapter moved, a dot per commit,
    # a sparkline per era, and a waiting state whenever content is not there yet.
    assert "function shapeHtml" in html and "function actMetaHtml" in html
    assert 'id="loading"' in html and 'id="skeleton"' in html
    # Cards appear as they are reached; they used to all animate at once on load.
    assert "IntersectionObserver" in html and "rootMargin" in html
    assert ".outline .num{flex-basis:100%}" in html
    # Nothing on the page may push the document wider than the screen.
    assert "overflow-x:hidden" in html
    assert ".diffbox{margin:0;font-size:11.5px;line-height:1.45;overflow-x:auto" in html


def test_the_storyteller_is_told_not_to_guess_anyone_s_gender():
    """The people in these commits are real and the material never states their pronouns:
    a story about someone's own work is the last place to guess (a first run did, and wrote
    'the developer ... himself')."""
    assert "never as 'he' or 'she'" in story._STORY_SYSTEM
    assert "'they'" in story._STORY_SYSTEM


def test_an_outline_title_is_cut_at_a_word_end(tmp_path):
    long_subject = "<aGiTrack> " + " ".join(["refactor"] * 40)
    rows = story.outline([_stat("a", "do it", 1_750_000_000, subject=long_subject)])
    assert rows[0]["title"].endswith("…") and len(rows[0]["title"]) <= 110
    assert not rows[0]["title"].endswith("refac…")  # never mid-word


def test_the_backtrace_banner_says_it_is_a_reconstruction():
    banner = story.story_backtrace_banner("/tmp/some/dir")
    assert "BACKTRACE" in banner and "reconstruction" in banner
    assert "agitrack --backtrace commit" in banner


# --------------------------------------------------------------------------- servers


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read().decode("utf-8")


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_the_live_dashboard_serves_the_storyline(tmp_path):
    repo = _repo_with_history(tmp_path, prompts=["one", "two"])
    server = build_server(repo, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = _get(base + "/story")
        assert "aGiTrack" in page and "story/state" in page
        state = json.loads(_get(base + "/story/state"))
        assert state["meta"]["commits"] == 3  # two agent turns plus the seed commit
        assert len(state["outline"]) == 3
        assert state["branches"]  # the live view can switch branches
        assert state["backtrace"] is False
        # And the dashboard itself points at it.
        assert 'id="storylink"' in _get(base + "/")
        assert _post(base + "/story/forget", {"branch": state["branch"]}) == {"forgotten": True}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_backtrace_dashboard_serves_its_own_storyline(monkeypatch, tmp_path):
    from tests.test_backtrace import _patch_discovery, _turn
    from agitrack.transcripts.types import ExportedSession
    from agitrack.transcripts.edits import make_edit
    import agitrack.metrics.backtrace as bt

    edit = make_edit("/repo/a.py", "", "x\ny\n", status="added")
    session = ExportedSession(
        session_id="c1",
        model="claude-opus-5",
        updated=2000,
        turns=[_turn("build the thing", edits=[edit])],
    )
    _patch_discovery(monkeypatch, claude_sessions={"c1": session})
    view = bt.build_backtrace(tmp_path)
    handler = bt._make_handler(view)
    import http.server

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = _get(base + "/story")
        assert "BACKTRACE" in page  # the frozen "this is a reconstruction" strip
        state = json.loads(_get(base + "/story/state"))
        assert state["backtrace"] is True
        assert state["branches"] == []  # no refs to switch between
        assert state["meta"]["commits"] == 1
        assert state["outline"][0]["prompt"] == "build the thing"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
