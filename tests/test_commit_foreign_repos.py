"""Other projects' names stay out of this repository's commit messages.

aGiTrack copies real conversation into git history, and a session is rarely about one project
only. Absolute paths were already masked; the bare NAME of the project next door was not, and
that is the form conversation actually uses. These cover both halves of the bargain: a
distinctive name goes, and an ordinary word stays — because a message redacted into
unreadability protects nothing anybody was going to read.
"""

from __future__ import annotations

import json
from pathlib import Path

from agitrack import repos as repo_registry
from agitrack.commits.foreign import FOREIGN_REPO_MASK, foreign_repo_names, is_distinctive, redact_foreign_repos
from agitrack.commits.message import build_agent_commit_message, build_pending_trailer


def _known(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
        repo_registry.remember(path)


# --- what counts as a name worth redacting -----------------------------------------------------


def test_a_distinctive_name_is_redacted_and_an_ordinary_word_is_not(tmp_path):
    # The whole feature is this trade. "acme-billing" in a commit message is a leak; "paper" is
    # a word the sentence needed, and blanking every occurrence would cost the reader more than
    # the leak ever did.
    _known(tmp_path / "here", tmp_path / "acme-billing", tmp_path / "paper")

    redacted = redact_foreign_repos("ported from acme-billing, wrote paper/main.tex", tmp_path / "here")

    assert redacted == f"ported from {FOREIGN_REPO_MASK}, wrote paper/main.tex"


def test_a_name_too_short_to_be_distinctive_is_left_alone(tmp_path):
    # "lv", "t4", "ml": two- and three-letter directory names collide with prose, identifiers and
    # abbreviations constantly, and firing on them costs more than it saves.
    _known(tmp_path / "here", tmp_path / "lv")

    assert redact_foreign_repos("the lv value is fine", tmp_path / "here") == "the lv value is fine"


def test_a_purely_numeric_name_is_left_alone(tmp_path):
    _known(tmp_path / "here", tmp_path / "2026")

    assert redact_foreign_repos("due in 2026", tmp_path / "here") == "due in 2026"


def test_the_generic_test_covers_the_words_people_actually_name_directories(tmp_path):
    for name in ("paper", "docs", "tmp", "scratch", "notes", "research", "src"):
        assert not is_distinctive(name), name
    for name in ("acme-billing", "quill-editor", "agitrack", "edgegen-2026"):
        assert is_distinctive(name), name


def test_ordinary_english_is_never_distinctive_even_when_a_repo_is_named_it(tmp_path):
    """A live run redacted the word "here" out of "whether it exists here", because a scratch
    repo happened to be called that. Ordinary English has to lose to the sentence."""
    for name in ("here", "there", "this", "when", "some", "first", "true"):
        assert not is_distinctive(name), name


def test_a_composed_name_is_distinctive_whatever_its_parts_are(tmp_path):
    # "test-app" is a name somebody composed; it does not turn up in a sentence by accident, so
    # it never has to answer to the word lists.
    for name in ("test-app", "my_paper", "paper2024", "myProj"):
        assert is_distinctive(name), name


def test_the_hosts_word_list_widens_the_net_when_there_is_one(tmp_path, monkeypatch):
    """Hand-listing English is not this file's job. Where the host has ``/usr/share/dict/words``
    a repo called "parser" or "cargo" is recognised as a word first and a project second; where
    it does not (Windows, a minimal container), the curated list is the floor and the behaviour
    is merely narrower, never wrong."""
    from agitrack.commits import foreign

    monkeypatch.setattr(foreign, "_dictionary", frozenset({"parser"}))
    assert not foreign.is_distinctive("parser")

    monkeypatch.setattr(foreign, "_dictionary", frozenset())
    assert foreign.is_distinctive("parser")  # no word list: narrower, and still not wrong


# --- whose names are foreign --------------------------------------------------------------------


def test_this_repositorys_own_name_is_never_redacted(tmp_path):
    # The failure that would make the feature worse than useless: a repo blanking its own name
    # out of its own history.
    _known(tmp_path / "quill-editor")

    text = "quill-editor now retries"
    assert redact_foreign_repos(text, tmp_path / "quill-editor") == text


def test_a_session_worktree_is_not_another_project(tmp_path):
    # A worktree lives at <repo>/.agitrack/worktrees/<name> and gets its own registry entry, so
    # without the containment check a session would redact the name of the very tree it runs in.
    repo = tmp_path / "quill-editor"
    worktree = repo / ".agitrack" / "worktrees" / "serenade"
    _known(repo, worktree)

    assert foreign_repo_names(repo) == []
    assert foreign_repo_names(worktree) == []


def test_a_directory_the_repository_lives_in_is_not_another_project(tmp_path):
    # ~/Code registered as a repo must not blank "Code" out of every project inside it.
    parent = tmp_path / "workspace"
    _known(parent, parent / "quill-editor")

    assert foreign_repo_names(parent / "quill-editor") == []


def test_a_stopped_repo_and_a_deleted_one_are_still_redacted(tmp_path):
    # `agitrack stop` and `rm -rf` both mean the user is done with the project — neither means
    # its name may now be published. The conversation that mentioned it is still in the
    # transcript this commit is built from.
    _known(tmp_path / "here", tmp_path / "acme-billing", tmp_path / "quill-editor")
    repo_registry.set_served(tmp_path / "acme-billing", False)
    (tmp_path / "quill-editor").rmdir()

    # Longest first, then alphabetically — a stable order, so the alternation is stable too.
    assert foreign_repo_names(tmp_path / "here") == ["acme-billing", "quill-editor"]


def test_the_longest_name_wins_when_one_is_a_prefix_of_another(tmp_path):
    # Matched in the other order, "acme" would fire first and leave "-billing" behind.
    _known(tmp_path / "here", tmp_path / "acme", tmp_path / "acme-billing")

    assert redact_foreign_repos("see acme-billing", tmp_path / "here") == f"see {FOREIGN_REPO_MASK}"


# --- how a name is matched -----------------------------------------------------------------------


def test_a_path_into_another_project_goes_whole(tmp_path):
    # Masking only the first segment would leave the file it points at in plain sight, which is
    # the more interesting half of "acme-billing/src/auth.ts".
    _known(tmp_path / "here", tmp_path / "acme-billing")

    redacted = redact_foreign_repos("same bug as acme-billing/src/auth.ts here", tmp_path / "here")

    assert redacted == f"same bug as {FOREIGN_REPO_MASK} here"


def test_sentence_punctuation_survives_the_redaction(tmp_path):
    _known(tmp_path / "here", tmp_path / "acme-billing")

    redacted = redact_foreign_repos("ported from acme-billing. Next: acme-billing/x.py.", tmp_path / "here")

    assert redacted == f"ported from {FOREIGN_REPO_MASK}. Next: {FOREIGN_REPO_MASK}."


def test_a_name_inside_a_longer_word_is_not_touched(tmp_path):
    # "acme-billing-notes" is a different name; redacting a substring of it would corrupt a word
    # nobody asked to have removed.
    _known(tmp_path / "here", tmp_path / "acme-billing")

    text = "the my-acme-billing-notes file and acme-billingify"
    assert redact_foreign_repos(text, tmp_path / "here") == text


def test_the_match_ignores_case(tmp_path):
    _known(tmp_path / "here", tmp_path / "acme-billing")

    assert redact_foreign_repos("see Acme-Billing", tmp_path / "here") == f"see {FOREIGN_REPO_MASK}"


def test_a_remote_url_naming_another_project_is_redacted_too(tmp_path):
    # The name is the leak wherever it appears, and a github URL is one of the likelier places a
    # conversation names another project.
    _known(tmp_path / "here", tmp_path / "acme-billing")

    redacted = redact_foreign_repos("https://github.com/them/acme-billing", tmp_path / "here")

    assert "acme-billing" not in redacted


# --- when nothing is redacted --------------------------------------------------------------------


def test_a_caller_that_does_not_say_which_repo_it_is_redacts_nothing(tmp_path):
    # Fail OPEN. Without a repo root there is no way to tell a foreign name from this project's
    # own, and guessing would blank out the repo's own name.
    _known(tmp_path / "here", tmp_path / "acme-billing")

    assert redact_foreign_repos("ported from acme-billing", None) == "ported from acme-billing"


def test_an_unreadable_registry_is_not_what_loses_a_commit_message(tmp_path, monkeypatch):
    _known(tmp_path / "here", tmp_path / "acme-billing")
    monkeypatch.setattr(repo_registry, "list_repos", lambda **_: (_ for _ in ()).throw(OSError("no registry")))

    assert redact_foreign_repos("ported from acme-billing", tmp_path / "here") == "ported from acme-billing"


def test_a_repo_can_turn_the_redaction_off(tmp_path):
    # For a project whose sessions legitimately talk about its siblings — a tool developed
    # alongside the thing it is used on — the redaction removes context the reader needs.
    here = tmp_path / "here"
    _known(here, tmp_path / "acme-billing")
    (here / ".agitrack").mkdir(parents=True, exist_ok=True)
    (here / ".agitrack" / "config.json").write_text(json.dumps({"redact_other_repos": False}), encoding="utf-8")

    assert redact_foreign_repos("ported from acme-billing", here) == "ported from acme-billing"


# --- the whole commit message ---------------------------------------------------------------------


def _message(tmp_path, **kwargs):
    return build_agent_commit_message(
        latest_prompt="port the retry fix from acme-billing",
        trace=[
            {"role": "user", "content": "port the retry fix from acme-billing"},
            {"role": "agent", "content": "Done — copied acme-billing/lib/retry.py, left paper/main.tex alone."},
        ],
        backend="claude",
        backend_session_id="s1",
        agitrack_session_id="a1",
        model="opus",
        repo_root=tmp_path / "here",
        **kwargs,
    )


def test_the_subject_the_summary_and_the_trace_are_all_covered(tmp_path):
    """The three places a project name reaches git history, and they are three separate code
    paths inside the builder — so all three are asserted rather than one standing for the rest."""
    _known(tmp_path / "here", tmp_path / "acme-billing", tmp_path / "paper")

    message = _message(tmp_path, summary="Port the acme-billing retry fix\n\nMirrors acme-billing/lib/retry.py.")

    assert "acme-billing" not in message
    subject, body = message.split("\n", 1)
    assert FOREIGN_REPO_MASK in subject  # the summary-led subject
    assert FOREIGN_REPO_MASK in body.split("# Interaction Trace")[0]  # the summary's lead paragraph
    assert FOREIGN_REPO_MASK in body.split("# Interaction Trace")[1]  # the trace itself
    assert "paper/main.tex" in message  # ...and the ordinary word is still there


def test_a_name_broken_across_a_wrapped_line_is_still_redacted(tmp_path):
    """The bug a live run found. The body is hard-wrapped to 72 columns, and ``textwrap`` breaks
    a long token at its hyphens — so a real commit came out carrying ``acme-\nbilling/lib/retry.py``,
    which no pattern working on the finished message can see. Redaction has to run on the text as
    the agent wrote it, before the wrap."""
    _known(tmp_path / "here", tmp_path / "acme-billing")
    long_line = (
        "The agent declined, reporting that neither acme-billing/lib/retry.py nor paper/main.tex "
        "exists in the repository, so there was no basis for the comparison it was asked to make."
    )

    message = build_agent_commit_message(
        latest_prompt="port the fix",
        trace=[{"role": "agent", "content": long_line}],
        backend="claude",
        backend_session_id="s1",
        agitrack_session_id="a1",
        model="opus",
        repo_root=tmp_path / "here",
    )

    assert "\n" in message.split("# Interaction Trace")[1]  # it really is wrapped
    assert "acme-billing" not in message and "acme-\nbilling" not in message
    assert FOREIGN_REPO_MASK in message


def test_a_prompt_led_subject_is_covered_when_there_is_no_summary(tmp_path):
    _known(tmp_path / "here", tmp_path / "acme-billing")

    message = _message(tmp_path)

    assert "acme-billing" not in message
    assert message.splitlines()[0].endswith(FOREIGN_REPO_MASK)


def test_the_manual_mode_fold_trailer_is_covered(tmp_path):
    """`-m` mode's trace reaches git through a different door: it is appended to the USER's own
    commit by the prepare-commit-msg hook, from a trailer rendered separately."""
    _known(tmp_path / "here", tmp_path / "acme-billing")
    latent = _message(tmp_path)

    trailer = build_pending_trailer(agitrack_session_id="a1", latent_bodies=[latent], repo_root=tmp_path / "here")

    assert "acme-billing" not in trailer
    assert FOREIGN_REPO_MASK in trailer


def test_the_in_flight_trailer_is_covered_too(tmp_path):
    # The other manual-mode door: the agent ran `git commit` itself mid-turn, so there is no
    # completed turn to fold and the running prompt is attributed on its own.
    _known(tmp_path / "here", tmp_path / "acme-billing")

    trailer = build_pending_trailer(
        agitrack_session_id="a1",
        latent_bodies=[],
        in_flight={
            "backend": "claude",
            "backend_session_id": "s1",
            "model": "opus",
            "prompt": "port the retry fix from acme-billing",
        },
        repo_root=tmp_path / "here",
    )

    assert "acme-billing" not in trailer
    assert FOREIGN_REPO_MASK in trailer


def test_a_reconstructed_backtrace_commit_is_redacted_but_the_dashboard_view_is_not(tmp_path):
    """`--backtrace commit` writes REAL commits onto a branch made to be pushed, and it builds
    them from months of transcripts at once — so it carries every project the user mentioned
    across all of it. The backtrace DASHBOARD renders the same turns into a page on localhost
    that is written nowhere; redacting there would only take information away from the one person
    already entitled to it."""
    from agitrack.backends.base import TokenUsage
    from agitrack.metrics.backtrace_commit import _TurnRec, _annotation

    _known(tmp_path / "here", tmp_path / "acme-billing")
    turn = _TurnRec(
        ended_at=2,
        started_at=1,
        files={"x.py"},
        backend="claude",
        model="opus",
        tokens=TokenUsage(),
        user_prompt="port the retry fix from acme-billing",
        final_response="Done, mirroring acme-billing/lib/retry.py.",
    )

    annotated = _annotation([turn], str(tmp_path / "here"))
    assert "acme-billing" not in annotated and FOREIGN_REPO_MASK in annotated

    # The same builder with no repo named — the dashboard's call — leaves the text alone.
    assert "acme-billing" in _annotation([turn])
