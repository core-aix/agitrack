"""Best-effort redaction of a session transcript before it leaves the machine.

Reuses the exact secret patterns aGiTrack already applies to commit messages
(:mod:`agitrack.commits.message`), so "what counts as a secret" has one definition,
and additionally masks the contributor's home-directory path (which carries their
username). This is best-effort — a transcript can still contain file contents and
command output — so the share flow also asks for explicit consent.
"""

from __future__ import annotations

import re

from agitrack.commits.message import SECRET_ASSIGNMENT_RE, SECRET_MASK, SECRET_TOKEN_RES

# /Users/<name> or /home/<name> → mask just the username segment, keeping the
# rest of the path intact (and JSON-string boundaries: stop at / " or whitespace).
_HOME_PATH_RE = re.compile(r"(/(?:Users|home)/)[^/\"\s\\]+")


def redact_text(text: str) -> str:
    value = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{SECRET_MASK}", text)
    for pattern in SECRET_TOKEN_RES:
        value = pattern.sub(SECRET_MASK, value)
    return _HOME_PATH_RE.sub(r"\1user", value)


def redact_transcript(text: str) -> str:
    """Redact a backend transcript (e.g. a Claude ``.jsonl``) line by line, so a
    match never spans rows and the JSON-per-line structure is preserved."""
    return "\n".join(redact_text(line) for line in text.split("\n"))
