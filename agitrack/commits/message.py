from __future__ import annotations

import platform
import re
from datetime import datetime, timezone
from textwrap import wrap

from agitrack import __version__


def _system_info() -> str:
    """Host OS and version for the commit metadata. Tool availability differs across
    macOS / Linux / Windows, so recording the platform helps interpret a session
    (and any tool-specific behaviour) after the fact."""
    system = platform.system()
    info = ""
    try:
        if system == "Darwin":
            info = f"macOS {platform.mac_ver()[0]}"
        elif system == "Windows":
            info = f"Windows {platform.release()}"
        elif system == "Linux":
            release = platform.freedesktop_os_release()
            version = release.get("VERSION_ID") or release.get("VERSION", "")
            info = f"{release.get('NAME', 'Linux')} {version}"
    except (OSError, AttributeError, KeyError):
        info = ""
    if not info.strip():
        info = f"{system} {platform.release()}"
    # Keep the metadata line within the 72-char commit-body width even for an
    # unusually long distro string.
    return info.strip()[:60] or "unknown"


DEFAULT_SUBJECT = "No subject provided"
# GitHub truncates a commit's subject (its first line) at 72 characters in the
# commit list, PR commits, blame, etc. — anything longer is ellipsized. Size the
# whole subject line (prefix included) to that limit so it's never cut off.
MAX_SUBJECT_WIDTH = 72
MAX_BODY_WIDTH = 72
# Subject tag for agent commits: every commit aGiTrack creates from the agent's
# work, including the cover commits placed on top of backend-made commits to
# carry their trace/metadata (issues #35/#58).
AGITRACK_SUBJECT_PREFIX = "<aGiTrack> "
SECRET_MASK = "[REDACTED]"
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|passwd|authorization)\b(\s*[:=]\s*)([^\s,;]+)"
)
# High-confidence secret token shapes — the set GitHub's secret-scanning push protection
# blocks. Redaction must cover at least these, or a transcript carrying one is refused by
# origin ("push declined") instead of sharing. Each has a distinctive prefix, so the
# false-positive risk is low. (Generic ``name = value`` secrets are caught separately by
# SECRET_ASSIGNMENT_RE above.)
SECRET_TOKEN_RES = [
    # OpenAI / Anthropic and other "sk-" API keys (covers sk-proj-…, sk-ant-…, incl. the
    # sk-ant-oat…/sk-ant-ort… OAuth access/refresh tokens Claude login stores).
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    # Claude Code / Anthropic OAuth login "reply key": after approving in the browser, the login
    # page shows an authorization code and state joined by '#' (``code#state``) to paste back
    # into the terminal — so it lands in the transcript as a pasted user message and, from there,
    # in the commit trace. One-time use, but a secret that never belongs in a commit or a shared
    # transcript. Two long URL-safe tokens joined by '#' is a distinctive shape; the lookarounds
    # keep it from firing inside a longer run, so ordinary trace text — a bare SHA, a UUID, a URL
    # with a short ``#fragment`` — is left untouched.
    re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{20,}#[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    # GitHub personal-access / OAuth / user / server / refresh tokens.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    # GitHub fine-grained personal-access token.
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    # GitLab personal-access / pipeline-trigger tokens.
    re.compile(r"\bgl(?:pat|ptt|rt|soat)-[A-Za-z0-9_-]{16,}\b"),
    # Slack bot / user / app / refresh / config tokens.
    re.compile(r"\bxox[baprse]-[A-Za-z0-9-]{10,}\b"),
    # AWS access key id (long-term AKIA + temporary/role/user ASIA, AROA, AIDA, …).
    re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|A3T[A-Z0-9])[0-9A-Z]{16}\b"),
    # Google API key and OAuth access token.
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"),
    # Stripe live / restricted secret keys.
    re.compile(r"\b[sr]k_live_[0-9A-Za-z]{16,}\b"),
    # npm automation token.
    re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    # PyPI / Test-PyPI upload token.
    re.compile(r"\bpypi-[A-Za-z0-9_-]{16,}\b"),
    # SendGrid API key.
    re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    # Twilio API key SID.
    re.compile(r"\bSK[0-9a-fA-F]{32}\b"),
    # Doppler service / personal token.
    re.compile(r"\bdop_v1_[A-Za-z0-9]{32,}\b"),
    # PEM private-key block (RSA/EC/OpenSSH/PGP/…); in a JSONL transcript the whole block
    # sits on one physical line, so grab it end-to-end when the END marker is present too.
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----(?:[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----)?"),
]
MOUSE_REPORT_RE = re.compile(r"(?:\x1b)?\[<\d+;\d+;\d+[Mm]")
# Full ANSI/terminal escape sequences (CSI/OSC/DCS and lone two-byte escapes).
ANSI_SEQUENCE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[P-_][^\x1b]*\x1b\\"
    r"|\x1b[@-Z\\-_]"
)
# Control characters that should never appear in a commit message, keeping tab,
# newline, and carriage return intact.
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# An ATX Markdown heading: 1–6 leading '#' followed by whitespace (matches the
# dashboard's own md() parser, so a line is a heading here iff it renders as one
# there). Used to nest a trace message's own headings under its role heading.
ATX_HEADING_RE = re.compile(r"^(#{1,6})(\s.*)$")
# A fenced code block delimiter; a leading '#' inside such a fence is a comment,
# not a heading, so heading-nesting must skip fenced regions.
CODE_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# The role heading for each trace turn ("## User" / "## Agent") is level 2, so a
# message's own headings are nested one level deeper, starting at level 3.
TRACE_ROLE_HEADING_LEVEL = 2


# Section header that marks a commit as carrying aGiTrack metadata. Detection of
# backend-made commits (issue #35) checks message bodies for this exact text,
# so keep the builders and the detector on one definition.
METADATA_HEADER = "# aGiTrack Metadata"


def build_agent_commit_message(
    *,
    latest_prompt: str,
    trace: list[dict],
    backend: str,
    backend_session_id: str | None,
    agitrack_session_id: str,
    model: str | None,
    reasoning_effort: str | None = None,
    conversation_anchor: str | None = None,
    token_usage: dict[str, int | None] | None = None,
    trace_turn_limit: int = 5,
    session_name: str | None = None,
    summary: str | None = None,
    summary_metadata: list[str] | None = None,
    covered_commits: list[str] | None = None,
    started_at: int | None = None,
    ended_at: int | None = None,
    compactions: int = 0,
    origin_event: dict | None = None,
) -> str:
    if summary:
        # The summary leads (issue #8): its first line becomes the subject, the
        # rest of it is the first paragraph of the body (no # Summary section).
        # The prompts are not duplicated into the message — the interaction trace
        # below already carries them verbatim.
        lines = _summary_lead_lines(summary)
    else:
        subject_prompt, full_subject = _subject_parts(
            _mask_secrets(latest_prompt), width=MAX_SUBJECT_WIDTH - len(AGITRACK_SUBJECT_PREFIX)
        )
        lines = [f"{AGITRACK_SUBJECT_PREFIX}{subject_prompt}"]
        if full_subject:
            # The truncated subject flows straight into its full text with no blank
            # line between them, so the extended subject reads as one continued line.
            lines.extend(_body_lines(full_subject))
    lines.append("")
    lines.extend(
        _trace_and_metadata_lines(
            trace=trace,
            backend=backend,
            backend_session_id=backend_session_id,
            agitrack_session_id=agitrack_session_id,
            model=model,
            reasoning_effort=reasoning_effort,
            conversation_anchor=conversation_anchor,
            token_usage=token_usage,
            trace_turn_limit=trace_turn_limit,
            session_name=session_name,
            summary_metadata=summary_metadata,
            covered_commits=covered_commits,
            started_at=started_at,
            ended_at=ended_at,
            compactions=compactions,
            origin_event=origin_event,
        )
    )
    return "\n".join(lines).rstrip() + "\n"


def apply_summary_to_message(
    message: str,
    summary: str,
    *,
    summary_metadata: list[str] | None = None,
) -> str:
    """Rewrite an existing agent commit message so the summary leads (#8).

    The summary's first line becomes the subject and the rest of the summary
    becomes the first paragraph of the body (no ``# Summary`` section). The
    original prompt-led subject is dropped, not preserved in a section — the
    ``# Interaction Trace`` below already carries the prompts verbatim, so a
    separate ``# Prompts`` block only duplicated them. ``summary_metadata`` lines
    are added to the metadata section. Idempotent: a message that already carries
    a summary (marked by its ``summary_model:`` metadata) is returned unchanged,
    so a redundant amend can never happen.
    """
    if not summary.strip() or "\nsummary_model:" in message:
        return message
    lines = message.splitlines()
    try:
        subject_end = lines.index("")
    except ValueError:
        subject_end = len(lines)
    rest = lines[subject_end + 1 :]

    new_lines = _summary_lead_lines(summary)
    new_lines.append("")
    new_lines.extend(rest)
    if summary_metadata:
        new_lines = _insert_before_version_line(new_lines, summary_metadata)
    return "\n".join(new_lines).rstrip() + "\n"


def summary_metadata_lines(
    *, model: str | None, tokens_input: int = 0, tokens_output: int = 0, tokens_cache_read: int = 0
) -> list[str]:
    """Metadata recording what the summarization itself cost (issue #8).

    ``tokens_input`` is fresh input (uncached input + cache-creation), matching the main
    commit's token accounting; ``tokens_cache_read`` reports cache hits separately and is
    emitted only when non-zero."""
    lines = [f"summary_model: {model or 'unknown'}"]
    if tokens_input > 0:
        lines.append(f"summary_tokens_input: {tokens_input}")
    if tokens_cache_read > 0:
        lines.append(f"summary_tokens_cache_read: {tokens_cache_read}")
    if tokens_output > 0:
        lines.append(f"summary_tokens_output: {tokens_output}")
    return lines


def _is_summary_header(line: str) -> bool:
    """A line that is only the word "summary" plus non-letters — e.g. ``Summary``,
    ``Summary:``, ``## Summary``, ``**Summary**``. Models sometimes emit such a
    header as the first line; left alone it becomes a useless commit subject."""
    return re.sub(r"[^a-zA-Z]", "", line).lower() == "summary"


def _summary_lead_lines(summary: str) -> list[str]:
    """Subject + leading body for a summarized message.

    Mirrors the prompt-led layout: the summary's first line is the subject
    (a truncated subject flows straight into its full text, no blank line),
    and the rest of the summary follows as the first paragraph of the body —
    there is no separate ``# Summary`` section. A leading bare "Summary" header
    line is skipped so the subject is never just the word "summary".
    """
    text_lines = _mask_secrets(summary).strip().splitlines()
    first_index = next(
        (i for i, line in enumerate(text_lines) if line.strip() and not _is_summary_header(line)),
        None,
    )
    first_line = text_lines[first_index] if first_index is not None else DEFAULT_SUBJECT
    remainder = text_lines[first_index + 1 :] if first_index is not None else []
    while remainder and not remainder[0].strip():
        remainder.pop(0)

    subject, full = _subject_parts(first_line, width=MAX_SUBJECT_WIDTH - len(AGITRACK_SUBJECT_PREFIX))
    lines = [f"{AGITRACK_SUBJECT_PREFIX}{subject}"]
    if full:
        lines.extend(_body_lines(full))
    if remainder:
        lines.append("")
        lines.extend(_body_lines("\n".join(remainder).rstrip()))
    return lines


def _insert_before_version_line(lines: list[str], extra: list[str]) -> list[str]:
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("agitrack_version:"):
            return lines[:index] + list(extra) + lines[index:]
    return lines + list(extra)


def render_interaction_trace(trace: list[dict], trace_turn_limit: int) -> str:
    """The interaction-trace body exactly as it is appended to an aGiTrack commit:
    role headings plus masked, heading-nested content (same as the commit's
    ``# Interaction Trace`` section, without the header). This is the *sole* input
    given to the summarizer, so the summary reflects the committed trace and
    nothing else — no diff, no out-of-band context."""
    lines: list[str] = []
    for item in _limit_trace_turns(trace, trace_turn_limit):
        role = item.get("role", "").strip().lower()
        content = _nest_headings_under_role(_mask_secrets(item.get("content", "")))
        label = "User" if role == "user" else "Agent"
        lines.extend([f"## {label}", "", *_body_lines(content), ""])
    return "\n".join(lines).strip()


def _trace_and_metadata_lines(
    *,
    trace: list[dict],
    backend: str,
    backend_session_id: str | None,
    agitrack_session_id: str,
    model: str | None,
    token_usage: dict[str, int | None] | None,
    trace_turn_limit: int,
    reasoning_effort: str | None = None,
    conversation_anchor: str | None = None,
    session_name: str | None,
    covered_commits: list[str] | None,
    summary_metadata: list[str] | None = None,
    started_at: int | None = None,
    ended_at: int | None = None,
    compactions: int = 0,
    origin_event: dict | None = None,
) -> list[str]:
    lines: list[str] = ["# Interaction Trace", ""]
    # Session-level events (a fork/copy this session began from, any context
    # compactions in these turns) lead the trace as a note, so the conversation log
    # itself shows when the context — and the token counts riding on it — changed.
    lines.extend(_session_event_note_lines(compactions=compactions, origin_event=origin_event))
    for item in _limit_trace_turns(trace, trace_turn_limit):
        role = item.get("role", "").strip().lower()
        # Nest the message's own headings under the "## User"/"## Agent" heading so
        # a message's "# Title" can't outrank the role it belongs to in the log.
        content = _nest_headings_under_role(_mask_secrets(item.get("content", "")))
        label = "User" if role == "user" else "Agent"
        lines.extend([f"## {label}", "", *_body_lines(content), ""])

    lines.extend(
        [
            METADATA_HEADER,
            "commit_type: agent",
            f"backend: {backend}",
            f"model: {model or 'unknown'}",
        ]
    )
    # The reasoning effort / thinking level the conversation ran at, only when the
    # backend transcript revealed it (see SessionTurn.reasoning_effort).
    if reasoning_effort:
        lines.append(f"reasoning_effort: {reasoning_effort}")
    lines.extend(
        [
            f"session_name: {session_name or 'unknown'}",
            f"agitrack_session_id: {agitrack_session_id}",
            f"backend_session_id: {backend_session_id or 'unknown'}",
        ]
    )
    # A pointer to the exact place in the backend conversation this commit
    # accounts for: the backend message id of the last turn it covers. Combined
    # with backend/backend_session_id it pinpoints where in the transcript to
    # resume reading, so a shared or locally-kept session can be backtracked to
    # the moment these changes were made. Recorded only when the transcript
    # exposes a message id for the turn.
    if conversation_anchor:
        lines.append(f"conversation_anchor: {conversation_anchor}")
    if covered_commits:
        # The backend-made commits this trace/metadata accounts for (#35).
        # Those commits are never rewritten, so the hashes stay valid (#58).
        lines.append(f"covered_commits: {' '.join(covered_commits)}")
    # When the AI-driven conversation began and ended (UTC, ISO-8601), so the
    # dashboard can report durations and filter by time.
    if started_at is not None:
        lines.append(f"agent_started_at: {_iso_utc(started_at)}")
    if ended_at is not None:
        lines.append(f"agent_ended_at: {_iso_utc(ended_at)}")
    lines.extend(_token_metadata_lines(token_usage))
    lines.extend(_session_event_metadata_lines(compactions=compactions, origin_event=origin_event))
    if summary_metadata:
        lines.extend(summary_metadata)
    lines.append(f"system: {_system_info()}")
    lines.append(f"agitrack_version: {__version__}")
    return lines


def _session_event_note_lines(*, compactions: int, origin_event: dict | None) -> list[str]:
    """Lead-in note for the interaction trace describing fork/copy lineage and any
    context compactions — the human-readable counterpart to the metadata lines."""
    notes: list[str] = []
    if origin_event:
        notes.append(_origin_event_sentence(origin_event))
    if isinstance(compactions, int) and compactions > 0:
        times = "once" if compactions == 1 else f"{compactions} times"
        notes.append(
            f"The conversation context was compacted {times} here — earlier history "
            "was summarized to fit the model's window, so the token counts below run "
            "against a reset (smaller) context."
        )
    lines: list[str] = []
    for note in notes:
        lines.extend(_note_block(_mask_secrets(note)))
        lines.append("")
    return lines


def _origin_event_sentence(origin_event: dict) -> str:
    kind = origin_event.get("kind")
    source_name = origin_event.get("source_name") or origin_event.get("source") or "another session"
    collaborator = origin_event.get("collaborator")
    if kind == "copy":
        whose = f"{collaborator}'s shared session" if collaborator else "a shared session"
        return (
            f"This session was copied from {whose} '{source_name}'. It resumes that "
            "conversation, so its starting context — and the token usage inherited with "
            "it — originated there, not in this session."
        )
    return (
        f"This session was forked from '{source_name}'. It resumes a copy of that "
        "conversation, so its starting context and the token usage inherited with it "
        "came from the original session."
    )


def _note_block(text: str) -> list[str]:
    """Wrap *text* as a Markdown blockquote (``>`` on every line) within the body width.
    The note is plain prose (no code/indentation to preserve), so collapse whitespace and
    drop the wrap padding for clean, evenly-filled quote lines."""
    wrapped = wrap(" ".join(text.split()), width=MAX_BODY_WIDTH - 2) or [""]
    return [f"> {line}" for line in wrapped]


def _session_event_metadata_lines(*, compactions: int, origin_event: dict | None) -> list[str]:
    """Machine-readable metadata for the session events: a compaction count and the
    fork/copy lineage, so the dashboard can flag turns whose token counts span a
    context reset or were inherited from another conversation."""
    lines: list[str] = []
    if isinstance(compactions, int) and compactions > 0:
        lines.append(f"context_compactions: {compactions}")
    if origin_event:
        kind = origin_event.get("kind") or "fork"
        source = origin_event.get("source") or "unknown"
        source_name = origin_event.get("source_name")
        detail = f"{source} ({source_name})" if source_name else str(source)
        key = "copied_from" if kind == "copy" else "forked_from"
        lines.append(f"{key}: {detail}")
        collaborator = origin_event.get("collaborator")
        if kind == "copy" and collaborator:
            lines.append(f"copied_from_contributor: {collaborator}")
    return lines


AGENT_MERGE_SUBJECT_PREFIX = "<aGiTrack-merge> "


def build_agent_merge_message(
    *,
    session_name: str | None,
    base_branch: str,
    source_branch: str,
    agitrack_session_id: str,
    backend: str,
    backend_session_id: str | None = None,
    conflicting_commits: str | None = None,
) -> str:
    """Commit message for a merge whose conflicts an agent resolved."""
    subject = f"{AGENT_MERGE_SUBJECT_PREFIX}integrate {session_name or source_branch} into {base_branch}"
    lines = [_subject_text(subject, width=MAX_SUBJECT_WIDTH), ""]
    if conflicting_commits and conflicting_commits.strip():
        lines.extend(["# Resolved Against Base Commits", ""])
        lines.extend(_body_lines(_mask_secrets(conflicting_commits)))
        lines.append("")
    lines.extend(
        [
            "# aGiTrack Metadata",
            "commit_type: agent-merge",
            f"backend: {backend}",
            f"session_name: {session_name or 'unknown'}",
            f"source_branch: {source_branch}",
            f"base_branch: {base_branch}",
            f"agitrack_session_id: {agitrack_session_id}",
            f"backend_session_id: {backend_session_id or 'unknown'}",
            f"system: {_system_info()}",
            f"agitrack_version: {__version__}",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def build_user_commit_message(
    *,
    message: str | None,
    agitrack_session_id: str,
) -> str:
    user_message = message.strip() if message else ""
    if not user_message:
        raise ValueError("User commit message is required")
    subject, full_subject = _subject_parts(_mask_secrets(user_message), width=MAX_SUBJECT_WIDTH)
    lines = [subject]
    if full_subject:
        # Extended subject continues directly under the subject line (no blank).
        lines.extend(_body_lines(full_subject))
    lines.append("")
    lines.extend(
        [
            "# aGiTrack Metadata",
            "commit_type: user",
            "backend: agit",
            f"agitrack_session_id: {agitrack_session_id}",
            f"system: {_system_info()}",
            f"agitrack_version: {__version__}",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def build_manual_squash_trailer(*, agitrack_session_id: str, latent_bodies: list[str]) -> str:
    """The tracking text a manual-commit-mode git hook appends to the user's own commit
    message, so the single commit carries the whole session's interaction record.

    It is a *squashed* message: a leading ``commit_type: user`` metadata block followed by the
    full message body of each pending latent turn in **chronological (oldest-first) order** — the
    same order a normal squash merge lists its commits, so a manual-mode commit reads like any
    other squash. Each latent body already carries its own ``# Interaction Trace`` +
    ``# aGiTrack Metadata``, so the concatenation has one metadata block per turn — exactly the
    aggregate shape the dashboard already parses into one constituent per turn (see
    ``agitrack.metrics.collect._parse_constituents``), which sums the tokens and classifies the
    commit as agent-tracked when any turn is present. No new ``commit_type`` is needed. (The
    dashboard *displays* the constituents newest-first, but the message itself stays chronological
    — the reorder is display-only, in ``agitrack.metrics.web``.)

    **No footprint on a non-AI commit.** When there are no pending agent turns — i.e. the commit
    holds only the user's own hand-written code, with no AI work to track — this returns the empty
    string, so the ``prepare-commit-msg`` hook appends nothing and the commit is left completely
    untouched. aGiTrack never attributes (or covers) a commit that contains no AI-written code.

    ``latent_bodies`` are the commit-message bodies read from the latent ref
    (``refs/agitrack/manual/<id>``), the durable source of truth, so the trailer is always
    reproducible after a restart. Returns a string ending in a single newline, or ``""`` when
    there are no pending turns."""
    # Chronological (oldest-first), like any squash merge; keep only real turn bodies.
    turn_blocks = [
        text for text in ((body or "").strip() for body in latent_bodies) if text and METADATA_HEADER in text
    ]
    if not turn_blocks:
        return ""  # no AI work in this commit ⇒ no trailer, no attribution, no footprint
    header = "\n".join(
        [
            METADATA_HEADER,
            "commit_type: user",
            "backend: agit",
            f"agitrack_session_id: {agitrack_session_id}",
            f"system: {_system_info()}",
            f"agitrack_version: {__version__}",
        ]
    )
    return "\n\n".join([header, *turn_blocks]).rstrip() + "\n"


def _subject_text(text: str, *, width: int) -> str:
    return _subject_parts(text, width=width)[0]


def _subject_parts(text: str, *, width: int) -> tuple[str, str | None]:
    # The subject is the first sentence: text up to the first period that ends one
    # (followed by whitespace or the end of the string). The remainder, if any, flows
    # onto the next line (the body). aGiTrack does NOT truncate with "…" — a long
    # subject is left intact and Git shortens its DISPLAY when needed. ``width`` is
    # accepted for caller compatibility but no longer used to truncate.
    one_line = " ".join(text.strip().split()) or DEFAULT_SUBJECT
    match = re.search(r"\.(?=\s|$)", one_line)
    if match is None:
        return one_line, None
    end = match.start() + 1  # include the period that ends the first sentence
    subject = one_line[:end].rstrip()
    remainder = one_line[end:].strip()
    return subject, remainder or None


def _body_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in str(text).splitlines() or [""]:
        lines.extend(wrap(raw_line, width=MAX_BODY_WIDTH, replace_whitespace=False, drop_whitespace=False) or [""])
    return lines


def _nest_headings_under_role(content: str) -> str:
    """Shift the Markdown headings inside one trace message so the shallowest one
    sits a single level below its ``## User`` / ``## Agent`` role heading (i.e. at
    level 3). The relative hierarchy is preserved — every heading moves by the same
    amount — so the rendered commit log nests the message's own sections under its
    role instead of letting a message ``#`` outrank the role it belongs to.

    Headings are only ever pushed deeper, never promoted; a message already nested
    at level 3+ is left as-is. Fenced code blocks are skipped, since a leading
    ``#`` there is a comment, not a heading. Levels are capped at the Markdown
    maximum of 6."""
    lines = content.splitlines()
    in_fence = False
    fence_marker = ""
    headings: list[tuple[int, int, str]] = []  # (line index, level, trailing text)
    for index, line in enumerate(lines):
        fence = CODE_FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, ""
            continue
        if in_fence:
            continue
        heading = ATX_HEADING_RE.match(line)
        if heading:
            headings.append((index, len(heading.group(1)), heading.group(2)))
    if not headings:
        return content
    shallowest = min(level for _, level, _ in headings)
    shift = max(0, (TRACE_ROLE_HEADING_LEVEL + 1) - shallowest)
    if not shift:
        return content
    for index, level, rest in headings:
        lines[index] = "#" * min(6, level + shift) + rest
    return "\n".join(lines)


def _limit_trace_turns(trace: list[dict], turn_limit: int) -> list[dict]:
    limit = turn_limit if isinstance(turn_limit, int) and turn_limit > 0 else 5
    user_indexes = [index for index, item in enumerate(trace) if str(item.get("role", "")).strip().lower() == "user"]
    if len(user_indexes) <= limit:
        return trace
    return trace[user_indexes[-limit] :]


def _mask_secrets(text: object) -> str:
    value = str(text or "")
    value = MOUSE_REPORT_RE.sub("", value)
    value = ANSI_SEQUENCE_RE.sub("", value)
    value = CONTROL_CHAR_RE.sub("", value)
    value = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{SECRET_MASK}", value)
    for pattern in SECRET_TOKEN_RES:
        value = pattern.sub(SECRET_MASK, value)
    return value


def _token_value(token_usage: dict[str, int | None] | None, key: str) -> int | str:
    if not token_usage:
        return "unknown"
    value = token_usage.get(key)
    return value if value is not None else "unknown"


def _append_positive(lines: list[str], key: str, value: object) -> None:
    """Record a token category only when the backend actually reports it."""
    amount = value if isinstance(value, int) else 0
    if amount > 0:
        lines.append(f"{key}: {amount}")


def _int_value(token_usage: dict[str, int | None], key: str) -> int:
    value = token_usage.get(key)
    return value if isinstance(value, int) else 0


def _iso_utc(epoch_seconds: int) -> str:
    """Epoch seconds → `YYYY-MM-DDTHH:MM:SSZ` in UTC (second precision)."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token_metadata_lines(token_usage: dict[str, int | None] | None) -> list[str]:
    lines = [f"context_tokens: {_token_value(token_usage, 'context')}"]
    if not token_usage:
        lines.append("tokens_since_last_commit_input: unknown")
        lines.append("tokens_since_last_commit_output: unknown")
        return lines
    # Main-line conversation consumption, broken out by category. Input and
    # output are always recorded; cache and reasoning only when non-zero so the
    # metadata stays compact for backends that do not report them.
    #
    # Backends report cache-creation tokens separately from input_tokens, but
    # they ARE fresh input — processed once and written to the cache. Counting
    # only the uncached remainder made a first run's input look near zero next
    # to its cache_write (issue #14), so the input line counts both; the
    # cache_write line below remains the "of which was written to the cache"
    # breakdown. Cache READS stay separate: those tokens were already counted
    # as input when first processed.
    lines.append(
        f"tokens_since_last_commit_input: {_int_value(token_usage, 'input') + _int_value(token_usage, 'cache_write')}"
    )
    _append_positive(lines, "tokens_since_last_commit_cache_read", token_usage.get("cache_read"))
    _append_positive(lines, "tokens_since_last_commit_cache_write", token_usage.get("cache_write"))
    lines.append(f"tokens_since_last_commit_output: {int(token_usage.get('output') or 0)}")
    _append_positive(lines, "tokens_since_last_commit_reasoning", token_usage.get("reasoning"))
    # Sub-agent / sidechain consumption, recorded separately (same input
    # accounting as the main line) and only when the backend exposes it.
    _append_positive(
        lines,
        "tokens_since_last_commit_subagent_input",
        _int_value(token_usage, "subagent_input") + _int_value(token_usage, "subagent_cache_write"),
    )
    _append_positive(lines, "tokens_since_last_commit_subagent_cache_read", token_usage.get("subagent_cache_read"))
    _append_positive(lines, "tokens_since_last_commit_subagent_cache_write", token_usage.get("subagent_cache_write"))
    _append_positive(lines, "tokens_since_last_commit_subagent_output", token_usage.get("subagent_output"))
    _append_positive(lines, "tokens_since_last_commit_subagent_reasoning", token_usage.get("subagent_reasoning"))
    return lines
