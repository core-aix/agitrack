"""Edge cases chosen for one property: each fails SILENTLY.

Nothing here is exotic. They are the situations where aGiTrack loses the user's work, or
records it wrongly, without printing an error — so the user finds out later, from a history
that is missing a turn or attributes it to the wrong model. Loud failures are already
well covered; these are the quiet ones.

Grouped by what breaks: transcript parsing under real-world file damage, token accounting,
encoding, and the terminal input state machine.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from agitrack.backends.base import TokenUsage
from agitrack.git import GitRepo
from agitrack.transcripts.claude import export_session_at


# --- transcript parsing under damage ----------------------------------------
#
# A transcript is APPENDED TO by the backend while aGiTrack reads it. Reading mid-write is
# normal, not exceptional, so a torn line must cost at most that line.


def _write_session(tmp_path, rows, *, trailing=""):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n" + trailing, encoding="utf-8")
    return path


def _turn_rows(prompt, response, *, uid):
    return [
        {
            "type": "user",
            "uuid": f"u-{uid}",
            "timestamp": "2026-01-01T00:00:00.000Z",
            "message": {"role": "user", "content": prompt},
        },
        {
            "type": "assistant",
            "uuid": f"a-{uid}",
            "timestamp": "2026-01-01T00:00:01.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-5",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": response}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]


def test_a_half_written_trailing_line_does_not_lose_the_completed_turns(tmp_path):
    # The common case: aGiTrack reads while the backend is mid-append. The turns already on
    # disk must survive — dropping them means the commit silently omits the user's work.
    path = _write_session(tmp_path, _turn_rows("do the thing", "did it", uid=1), trailing='{"type":"assis')

    session = export_session_at(path)

    assert [t.user_prompt for t in session.turns] == ["do the thing"]
    assert session.turns[0].final_response == "did it"


def test_a_corrupt_line_in_the_middle_does_not_discard_what_follows(tmp_path):
    # A torn line must cost that line, not the rest of the file. Truncating at the first bad
    # row would silently drop every later turn.
    rows = _turn_rows("first", "one", uid=1)
    path = tmp_path / "session.jsonl"
    lines = [json.dumps(row) for row in rows]
    lines.append("{not json at all")
    lines.extend(json.dumps(row) for row in _turn_rows("second", "two", uid=2))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    session = export_session_at(path)

    assert [t.user_prompt for t in session.turns] == ["first", "second"]


def test_an_empty_transcript_is_not_an_error(tmp_path):
    # A session file exists from the moment the backend starts, before any turn. Treating that
    # as a parse failure would make every fresh session look broken.
    path = tmp_path / "session.jsonl"
    path.write_text("", encoding="utf-8")

    session = export_session_at(path)

    assert session is None or session.turns == []


def test_a_transcript_of_only_blank_lines_yields_no_turns(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text("\n\n   \n\n", encoding="utf-8")

    session = export_session_at(path)

    assert session is None or session.turns == []


def test_a_missing_transcript_returns_nothing_rather_than_raising(tmp_path):
    # Polled from the reactor for a session id that may name nothing yet.
    assert export_session_at(tmp_path / "nope.jsonl") is None


# --- token accounting -------------------------------------------------------
#
# Token counts ride on every commit and feed the dashboard's cost reporting. They fail
# silently by construction: a wrong number looks exactly like a right one.


def test_token_usage_sums_every_field_when_added():
    # TokenUsage.add is how a turn accumulates across many assistant messages. A field missed
    # here is a field that reads zero on every commit forever.
    total = TokenUsage()
    for _ in range(3):
        total.add(
            TokenUsage(
                total=1,
                input=2,
                output=3,
                reasoning=4,
                cache_read=5,
                cache_write=6,
                subagent_input=7,
                subagent_output=8,
                subagent_reasoning=9,
                subagent_cache_read=10,
                subagent_cache_write=11,
            )
        )

    assert total.input == 6 and total.output == 9 and total.reasoning == 12
    assert total.cache_read == 15 and total.cache_write == 18
    assert total.subagent_input == 21 and total.subagent_output == 24
    assert total.subagent_cache_read == 30 and total.subagent_cache_write == 33


def test_adding_usage_takes_the_latest_context_rather_than_summing_it():
    # `context` is a LEVEL (how full the window is), not a quantity to accumulate. Summing it
    # would report a context far larger than the model's window.
    total = TokenUsage(context=1000)
    total.add(TokenUsage(context=1500))
    assert total.context == 1500

    # …and a report that omits it must not clear what we already knew.
    total.add(TokenUsage(context=None))
    assert total.context == 1500


def test_a_turns_tokens_are_counted_once_per_message(tmp_path):
    # Claude repeats a message's `usage` across streamed rows; counting it per row inflates
    # every turn. The parser dedupes by message id — pin it.
    rows = [
        {
            "type": "user",
            "uuid": "u-1",
            "timestamp": "2026-01-01T00:00:00.000Z",
            "message": {"role": "user", "content": "go"},
        },
        {
            "type": "assistant",
            "uuid": "a-1",
            "timestamp": "2026-01-01T00:00:01.000Z",
            "message": {
                "id": "msg-1",
                "role": "assistant",
                "model": "claude-opus-5",
                "stop_reason": "tool_use",
                "content": [{"type": "text", "text": "working"}],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        },
        {
            "type": "assistant",
            "uuid": "a-2",
            "timestamp": "2026-01-01T00:00:02.000Z",
            "message": {
                "id": "msg-1",
                "role": "assistant",
                "model": "claude-opus-5",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        },
    ]
    session = export_session_at(_write_session(tmp_path, rows))

    assert session.turns[0].tokens.output == 20, "the same message's usage was counted twice"


def test_a_synthetic_model_marker_never_becomes_the_turns_model(tmp_path):
    # Claude stamps non-LLM messages with "<synthetic>". Letting that win means the commit —
    # and the dashboard's by-model breakdown — records a model that does not exist.
    rows = [
        {
            "type": "user",
            "uuid": "u-1",
            "timestamp": "2026-01-01T00:00:00.000Z",
            "message": {"role": "user", "content": "go"},
        },
        {
            "type": "assistant",
            "uuid": "a-1",
            "timestamp": "2026-01-01T00:00:01.000Z",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "claude-opus-5",
                "stop_reason": "tool_use",
                "content": [{"type": "text", "text": "x"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
        {
            "type": "assistant",
            "uuid": "a-2",
            "timestamp": "2026-01-01T00:00:02.000Z",
            "message": {
                "id": "m2",
                "role": "assistant",
                "model": "<synthetic>",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "y"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    ]
    session = export_session_at(_write_session(tmp_path, rows))

    assert session.turns[0].model == "claude-opus-5"


# --- encoding ---------------------------------------------------------------
#
# The prompt is user text that reaches git as a commit message. Anything that cannot be
# encoded there fails the commit, and the turn's work is left uncommitted.


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return GitRepo.discover(tmp_path)


@pytest.mark.parametrize(
    "message,label",
    [
        ("emoji 🎉 and em—dash", "non-ascii"),
        ("mixed 中文 and العربية", "multi-script"),
        ("null-ish \x0b vertical tab", "control character"),
        ("a" * 5000, "very long subject"),
    ],
)
def test_a_commit_message_survives_awkward_text(tmp_path, message, label):
    # Real prompts contain all of these. A commit that fails to encode leaves the turn's work
    # on disk and uncommitted, which is the failure mode aGiTrack exists to prevent.
    repo = _repo(tmp_path)
    (tmp_path / "f.txt").write_text("x\n")
    repo.stage_paths(["f.txt"])

    repo.commit(message)

    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8"
    ).stdout
    assert message.split("\n")[0][:50] in log, f"{label} did not survive the round trip"


def test_a_prompt_containing_a_lone_surrogate_does_not_crash_the_parser(tmp_path):
    # Surrogates reach a transcript from pasted or mis-decoded input and cannot be encoded to
    # UTF-8. The parser reads with errors="replace"; this pins that it never raises.
    path = tmp_path / "session.jsonl"
    row = {
        "type": "user",
        "uuid": "u-1",
        "timestamp": "2026-01-01T00:00:00.000Z",
        "message": {"role": "user", "content": "before"},
    }
    path.write_bytes(json.dumps(row).encode("utf-8") + b"\n" + b'{"broken": "\xed\xa0\x80"}\n')

    session = export_session_at(path)  # must not raise

    assert session is None or [t.user_prompt for t in session.turns] in ([], ["before"])


# --- terminal input ---------------------------------------------------------
#
# The stdin state machine sees bytes in whatever chunks the kernel delivers. A rule that
# holds for a whole sequence but not for one split across two reads is a real-world bug.

pytestmark_posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal input model")


def _proxy_input():
    from agitrack.proxy.runner import ProxyInput

    return ProxyInput()


def test_bracketed_paste_markers_are_recognised_when_split_across_reads():
    # The kernel splits stdin at arbitrary byte boundaries, so the paste delimiters routinely
    # arrive across two reads. If they are only recognised whole, aGiTrack never learns it is
    # inside a paste — and then treats pasted content as KEYS.
    from agitrack.proxy.runner import PASTE_START, PASTE_END

    proxy = _proxy_input()
    for byte in PASTE_START:  # one byte per call: the worst case
        proxy.feed(bytes([byte]))
    assert proxy.in_paste is True

    for byte in PASTE_END:
        proxy.feed(bytes([byte]))
    assert proxy.in_paste is False


def test_pasted_content_reaches_the_backend_byte_for_byte():
    # Inside a paste every byte is content the user copied. It must be forwarded unchanged —
    # delimiters included — so the backend sees exactly what was pasted.
    from agitrack.proxy.runner import PASTE_START, PASTE_END

    proxy = _proxy_input()
    payload = PASTE_START + b"exit\n\x03 rm -rf /\r" + PASTE_END
    forwarded, _, command, should_exit = proxy.feed(payload)

    assert b"".join(forwarded) == payload
    # …and none of it was interpreted: a pasted newline must not RUN anything, and a pasted
    # \x03 must not begin tearing the session down.
    assert command is None
    assert should_exit is False


def test_a_paste_split_mid_stream_still_forwards_everything():
    # The realistic shape: markers and content arrive in whatever chunks the kernel chose.
    from agitrack.proxy.runner import PASTE_START, PASTE_END

    proxy = _proxy_input()
    chunks = [PASTE_START[:3], PASTE_START[3:] + b"line one\n", b"line two\n", PASTE_END[:2], PASTE_END[2:]]
    seen = b""
    for chunk in chunks:
        forwarded, _, command, should_exit = proxy.feed(chunk)
        seen += b"".join(forwarded)
        assert command is None and should_exit is False

    assert seen == b"".join(chunks)
    assert proxy.in_paste is False


def test_signal_control_bytes_are_never_forwarded_to_the_backend():
    # Ctrl-\ (SIGQUIT) would kill the backend outright on a cooked pty, losing the
    # conversation, and means nothing to a TUI.
    proxy = _proxy_input()
    forwarded, _, _, _ = proxy.feed(b"\x1c")
    assert b"\x1c" not in b"".join(forwarded)


def test_ordinary_bytes_pass_through_untouched_when_not_capturing():
    # The other side of the contract: everything that is NOT special must reach the backend
    # byte for byte, or the agent sees corrupted input.
    proxy = _proxy_input()
    payload = b"select * from t where x > 1;\r"
    forwarded, _, _, _ = proxy.feed(payload)
    assert b"".join(forwarded) == payload
