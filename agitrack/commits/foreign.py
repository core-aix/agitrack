"""Keep OTHER projects' names out of this repository's commit messages.

aGiTrack copies real conversation into git history — the prompt, the agent's reply, the LLM
summary — and a coding session is rarely about one project only. "I ported the retry fix from
acme-billing", "same bug as in client-portal/src/auth.ts", "check how quill-editor does it":
each of those sentences is ordinary, useful context in the conversation, and each of them
publishes the NAME of a private project into a repository that may be public, shared with a
different client, or simply read by people who have no business knowing what else is on the
machine. Absolute paths are already gone (:func:`agitrack.commits.message.mask_paths`), which
covers ``/Users/me/Code/acme-billing/...`` — but not the bare word ``acme-billing``, and the bare
word is what conversation actually uses.

So every repository aGiTrack knows about EXCEPT the one being committed to is treated as a name
to keep out. The registry (:mod:`agitrack.repos`) is the source: it is the user-wide list of
projects aGiTrack has worked in, which is exactly the set of names a session on this machine is
likely to mention.

Two properties this deliberately does NOT have:

* **It is not a security boundary.** A name can reach a commit in a form nothing can recognise —
  an abbreviation, a ticket number, a class name, prose describing the project without naming it.
  This is best-effort tidying of the obvious cases, and it is documented as such wherever the
  user meets it.
* **It does not redact ordinary words.** A project called ``paper``, ``docs`` or ``notes``
  shares its name with words the conversation uses for other reasons, and blanking every
  occurrence would gut the very text this exists to keep readable — the message would lose more
  meaning to the redaction than it ever leaked. Those names are left alone (see
  :data:`GENERIC_NAMES` and :func:`is_distinctive`), which is the deliberate hole in the net.
"""

from __future__ import annotations

import re
from pathlib import Path

# What a redacted name becomes. Says what was removed and why, unlike a row of asterisks: a
# reader who hits it in a commit message a year later can tell that a project name stood here and
# that aGiTrack took it out on purpose.
#
# An UNDERSCORE, not a hyphen. Commit bodies are hard-wrapped by ``textwrap``, which breaks a
# hyphenated word at its hyphen — a live run produced "[OTHER-\nREPO]" mid-paragraph, which is
# both ugly and no longer greppable. Nothing splits on an underscore, for the same reason
# "[REDACTED]" and "[PATH]" are single words.
FOREIGN_REPO_MASK = "[OTHER_REPO]"

# Names too ordinary to redact. Each is a word a conversation uses for its own sake many times
# per turn, so redacting it would damage far more text than it protects — and it would be
# self-defeating, since a message full of "[OTHER-REPO]" is a message nobody can read.
#
# Curated rather than derived: there is no dictionary to consult here (aGiTrack ships no word
# list and must work offline), and the population is small and predictable — the words people
# actually name a directory after. Erring toward "generic" is the safe direction: a name left in
# is a leak of one word the user already chose to have on their own machine, while a name wrongly
# taken out silently damages the commit history it was supposed to preserve.
# Grouped by what people name a directory after, and hand-wrapped: one word per line would
# bury the shape of the list in four hundred lines.
# fmt: off
GENERIC_NAMES = frozenset({
    # a scratch or working directory
    "temp", "tmp", "scratch", "sandbox", "playground", "test", "tests", "testing", "demo",
    "demos", "example", "examples", "sample", "samples", "draft", "drafts", "trial", "misc",
    "stuff", "junk", "old", "new", "backup", "backups", "archive", "archives", "copy",
    # a project, or a directory inside one
    "project", "projects", "work", "working", "repo", "repos", "code", "codes", "source",
    "src", "lib", "libs", "library", "app", "apps", "application", "web", "website", "site",
    "sites", "api", "apis", "server", "client", "core", "common", "shared", "main", "master",
    "build", "dist", "bin", "tools", "tool", "utils", "util", "scripts", "script", "config",
    "configs", "dotfiles", "setup", "infra", "infrastructure", "platform", "service",
    "services", "backend", "frontend", "ui", "cli", "sdk", "plugin", "plugins", "extension",
    "extensions", "package", "packages", "module", "modules", "template", "templates",
    # academic and writing work
    "paper", "papers", "notes", "note", "book", "books", "thesis", "dissertation", "slides",
    "talk", "talks", "poster", "posters", "review", "reviews", "research", "study", "studies",
    "experiment", "experiments", "results", "figures", "data", "dataset", "datasets",
    "analysis", "report", "reports", "docs", "doc", "documentation", "manual", "course",
    "courses", "class", "classes", "lecture", "lectures", "homework", "assignment",
    "exercise", "exercises", "tutorial", "tutorials", "blog", "posts", "writing",
    # a personal directory
    "home", "personal", "private", "public", "user", "users", "mine", "local", "desktop",
    "documents", "downloads", "resume", "portfolio", "profile", "todo", "ideas", "inbox",
    "vault", "wiki",
    # ordinary English a sentence uses for its own sake. Only 4+ letters need listing (shorter
    # names are never distinctive), and the list is the FLOOR — where the host has a word list,
    # `_dictionary_words` catches far more. A live run redacted the word "here" out of "whether
    # it exists here" because a scratch repo happened to be called that, which is what this
    # half of the list exists to stop.
    "here", "there", "this", "that", "these", "those", "then", "than", "them", "they", "their",
    "when", "where", "what", "which", "while", "with", "from", "into", "onto", "over", "under",
    "about", "after", "before", "again", "also", "only", "just", "more", "most", "much", "many",
    "some", "such", "same", "each", "every", "both", "will", "would", "could", "should", "have",
    "been", "does", "done", "make", "made", "take", "taken", "give", "given", "find", "found",
    "need", "want", "know", "like", "look", "come", "back", "next", "last", "first", "second",
    "other", "another", "thing", "things", "time", "times", "part", "parts", "line", "lines",
    "file", "files", "name", "names", "word", "words", "case", "cases", "list", "lists", "item",
    "items", "page", "pages", "view", "form", "mode", "type", "kind", "sort", "size", "rate",
    "step", "steps", "task", "tasks", "plan", "plans", "goal", "goals", "help", "fine", "good",
    "best", "well", "real", "true", "false", "null", "none", "still", "even", "ever", "never",
    "always", "your", "yours", "ours", "mine", "self", "here",
})
# fmt: on

# Below this, a name collides with ordinary text too easily to be worth redacting: two- and
# three-letter directory names ("lv", "t4", "wip", "ml") turn up inside prose, identifiers and
# abbreviations constantly, and a redaction that fires on them costs more than it saves. Chosen
# as the point where a name starts to look chosen rather than typed in a hurry.
MIN_DISTINCTIVE_LENGTH = 4

# Punctuation that ends the SENTENCE rather than the name or the path ("see acme-billing/x.py."
# keeps its full stop). Stripped off the match and re-appended after the mask — the same rule
# ``mask_paths`` applies, for the same reason.
_TRAILING_PUNCT = ".,;:!?)]}\"'"
# What may follow a foreign repo name and still be part of the same reference: the rest of a
# RELATIVE path inside it. `acme-billing/src/auth.ts` names a file in the other project, so
# masking only the first segment would leave the file it points at in plain sight.
_PATH_TAIL = r"(?:/[^\s<>\"'`|,;]*)?"


# Word lists the host may already have. Consulted as a much wider net than the curated list can
# be: a repo called "here", "queue", "parser" or "cargo" is an English word first and a project
# second, and hand-listing English is not a job this file should be doing. Absent on Windows and
# on minimal containers, which is why GENERIC_NAMES is the FLOOR rather than a supplement — the
# feature must behave sensibly with no word list at all.
_SYSTEM_WORD_LISTS = ("/usr/share/dict/words", "/usr/dict/words")
# Loaded at most once: read on the first question asked of it and kept. `None` means "not looked
# yet"; an empty set means "looked, found nothing", so a missing file is not re-opened per commit.
_dictionary: frozenset[str] | None = None


def _dictionary_words() -> frozenset[str]:
    """The host's word list, lowercased, or an empty set when it has none.

    Only words at least :data:`MIN_DISTINCTIVE_LENGTH` long are kept: a shorter name is never
    distinctive anyway, and dropping them keeps a 235,000-line file down to what can actually
    change an answer."""
    global _dictionary
    if _dictionary is not None:
        return _dictionary
    words: set[str] = set()
    for candidate in _SYSTEM_WORD_LISTS:
        try:
            with open(candidate, encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    word = line.strip().lower()
                    if len(word) >= MIN_DISTINCTIVE_LENGTH and word.isalpha():
                        words.add(word)
        except OSError:
            continue
        if words:
            break
    _dictionary = frozenset(words)
    return _dictionary


def is_distinctive(name: str) -> bool:
    """Whether ``name`` is specific enough to redact rather than an ordinary word.

    The whole feature turns on this question, and it is asked in the direction that protects the
    TEXT: anything that could plausibly be a word the conversation used for its own sake is left
    in. See :data:`GENERIC_NAMES` for why that hole is deliberate.

    A COMPOUND name — one carrying a hyphen, underscore, dot, digit or internal capital — is
    distinctive whatever its parts are: "test-app" and "my_paper" are names somebody composed,
    and neither turns up in a sentence by accident. Only a bare single word has to answer to the
    word lists."""
    lowered = name.strip().lower()
    if len(lowered) < MIN_DISTINCTIVE_LENGTH:
        return False
    if lowered.isdigit():
        return False  # "2026", "01": a date or an index in every other sentence
    if any(character in lowered for character in "-_.") or any(character.isdigit() for character in lowered):
        return True
    if name.strip()[1:] != name.strip()[1:].lower():  # camelCase / PascalCase after the first letter
        return True
    return lowered not in GENERIC_NAMES and lowered not in _dictionary_words()


def _resolve(path: str | Path) -> Path:
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return Path(path).expanduser()


def _is_related(candidate: Path, current: Path) -> bool:
    """Whether ``candidate`` is the current repository, inside it, or contains it.

    All three are "not another project": a session WORKTREE lives at
    ``<repo>/.agitrack/worktrees/<name>`` and gets its own registry entry, a nested repository is
    part of the tree being committed, and an enclosing directory is where this project lives. A
    name from any of them must never be redacted out of the repo's own commits."""
    if candidate == current:
        return True
    return candidate in current.parents or current in candidate.parents


def foreign_repo_names(repo_root: str | Path) -> list[str]:
    """The distinctive names of every OTHER repository aGiTrack knows about, longest first.

    Longest first so that, where one name is a prefix of another, the more specific one is
    matched: with ``acme`` and ``acme-billing`` both known, an alternation tried in the other
    order would leave ``-billing`` behind.

    Reads the registry EVERY time rather than caching it. This runs once per commit message —
    not once per line — so one small JSON read is not worth a cache that would go stale the
    moment the user opened a new project in another terminal."""
    current = _resolve(repo_root)
    try:
        from agitrack import repos as repo_registry

        # Neither filter applies here. A repo the user STOPPED is still a project whose name does
        # not belong in another one's history, and a repo whose directory has since been deleted
        # is if anything more sensitive, not less — the conversation that mentioned it is still
        # in the transcript this commit is built from.
        entries = repo_registry.list_repos(existing_only=False, served_only=False)
    except Exception:
        return []  # no registry, no redaction: this must never be what fails a commit
    # The current repository's own path components can never be redacted: with a repo at
    # ~/Code/acme and another registered at ~/Code, "Code" would otherwise be blanked out of the
    # very repository it names.
    own = {part.lower() for part in current.parts}
    names: set[str] = set()
    for entry in entries:
        try:
            path = _resolve(entry.path)
        except Exception:
            continue
        if _is_related(path, current):
            continue
        name = (entry.name or path.name).strip()
        if not name or name.lower() in own or not is_distinctive(name):
            continue
        names.add(name)
    return sorted(names, key=lambda name: (-len(name), name.lower()))


def _pattern(names: list[str]) -> re.Pattern[str] | None:
    if not names:
        return None
    alternation = "|".join(re.escape(name) for name in names)
    # The lookbehind keeps the name from matching inside a longer word ("acme-billing" must not
    # fire on "my-acme-billing-notes"); "/" is deliberately NOT in it, so a name reached through a
    # relative path or a URL — `../acme-billing/x`, `github.com/them/acme-billing` — is still
    # caught. The lookahead allows a following "." so a name at the end of a sentence, and a file
    # named after the project ("acme-billing.md"), are both matched.
    return re.compile(rf"(?<![\w-])(?:{alternation})(?![\w-]){_PATH_TAIL}", re.IGNORECASE)


def _enabled(repo_root: str | Path) -> bool:
    """Whether this repository wants the redaction (``redact_other_repos``, on by default).

    Read per call rather than once, and with the repo's own overlay loaded: the setting is one
    a user reaches for exactly when a commit has just come out over-redacted, and having to
    restart the tracker for it to take effect would be the wrong answer to that."""
    from agitrack.config import GlobalConfig

    config = GlobalConfig()
    config.load_repo_overlay(Path(repo_root))
    return config.redact_other_repos


def redact_foreign_repos(text: str, repo_root: str | Path | None) -> str:
    """Replace mentions of OTHER repositories in ``text`` with :data:`FOREIGN_REPO_MASK`.

    ``repo_root`` is the repository being committed to; ``None`` means "no idea which repo this
    is", and then nothing is redacted. Fail-open is the only safe default here: a caller that has
    not said which project it is committing to cannot be told which names are foreign, and
    guessing would blank out the repo's own name."""
    if not text or repo_root is None:
        return text
    try:
        if not _enabled(repo_root):
            return text
        pattern = _pattern(foreign_repo_names(repo_root))
    except Exception:
        return text  # redaction is best-effort; never let it be what loses a commit message
    if pattern is None:
        return text

    def replace(match: re.Match[str]) -> str:
        hit = match.group(0)
        trailing = ""
        while hit and hit[-1] in _TRAILING_PUNCT:
            trailing = hit[-1] + trailing
            hit = hit[:-1]
        return FOREIGN_REPO_MASK + trailing if hit else match.group(0)

    return pattern.sub(replace, text)
