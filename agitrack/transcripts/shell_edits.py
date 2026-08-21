"""Recover file edits an agent made through its SHELL tool, from the command text alone.

:mod:`agitrack.transcripts.edits` reconstructs the edits an agent made with its *editing*
tools (Claude's ``Edit``/``Write``, OpenCode's ``edit``/``write``, Codex's ``apply_patch``).
That is only part of the story. Agents edit through the shell constantly — a ``cat`` heredoc
to write a new file, an inline Python script to patch an existing one, ``sed -i`` for a
one-liner — and Claude Code's auto mode explicitly *instructs* it to. A session measured on
this repository ran 405 shell calls against 42 editing-tool calls, and the backtrace
reconstructed 31.5% of the lines the tracked commits recorded: two thirds of the work was
invisible, so ``--backtrace`` showed a fraction of the real change and ``--backtrace commit``
had far too few files to attribute commits by.

The content is not lost, though. A shell tool call records the *whole command string*, and
the mutating commands agents actually write are shapes whose new content is right there in
the text: a heredoc carries the file verbatim, and an inline ``read_text``/``replace``/
``write_text`` script carries the same ``(old, new)`` pair an ``Edit`` call would. Adding the
four idioms below took the same session from 31.5% to ~95% of the tracked line count.

**Nothing here executes anything.** Every recovery is a parse of recorded text: a heredoc
body is read as-is, an inline Python script is read with :mod:`ast` (never ``exec``), and a
``sed`` script is applied with :mod:`re`. Replaying a recorded shell command would be the only
way to recover the rest (``uv lock``, formatters, arbitrary computation), and running commands
off a transcript to draw a dashboard is not a trade this makes.

**Conservative on purpose.** ``file_state`` is a running reconstruction of each file's
content, and every later diff is taken against it, so a *wrong* recovery is worse than no
recovery: it corrupts the baseline for every edit that follows. Anything this module cannot
model exactly — a script that computes its output, a BRE-only ``sed`` pattern, an unresolved
``$VAR`` in a path — is skipped whole rather than approximated.
"""

from __future__ import annotations

import ast
import posixpath
import re
import shlex
import warnings

from agitrack import paths
from agitrack.transcripts.edits import make_edit, tracked_edit
from agitrack.transcripts.types import FileEdit

# A heredoc introducer: `<<EOF`, `<<-EOF`, `<< 'EOF'`, `<<"EOF"`. The body always begins on
# the NEXT line and ends at a line holding only the delimiter, which is what lets this module
# scan line by line and still skip heredoc bodies whole.
_HEREDOC = re.compile(r"<<-?\s*(?P<quote>['\"]?)(?P<delim>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)")

# An output redirect, capturing append vs truncate. The lookbehind rejects `2>`, `>>` seen as
# a second `>`, and `&>`; the character class stops at anything that ends a word or a command.
_REDIRECT = re.compile(r"(?<![0-9<>&])(?P<op>>>|>)\s*(?P<path>[^\s;&|<>()]+)")
_TEE = re.compile(r"\btee\b(?P<flags>(?:\s+-{1,2}[A-Za-z-]+)*)\s+(?P<path>[^\s;&|<>()]+)")
# Python is as often invoked by PATH as by name — `./.venv/bin/python - <<PY` is the same
# script as `python3 - <<PY`, and requiring a bare word missed every one of them (one measured
# session ran 60+ that way, and its whole reconstruction was the poorer for it).
_PYTHON = re.compile(r"(?:^|[\s;&|(])(?:uv\s+run\s+)?[\w./~+-]*python[23]?(?:\.\d+)?\b")
# `echo`/`printf` need not start the line — `mkdir -p x && echo hi > x/f` is ordinary — but the
# arguments must stop at the next command separator, or the following command is read as payload.
_ECHO = re.compile(r"(?:^|[\s;&|(])(?P<cmd>echo|printf)\s+(?P<args>[^\n;&|]*)")
_CD = re.compile(r"(?:^|[\s;&|(])cd\s+(?P<target>[^\s;&|()]+)")
# `SP=/tmp/work` then `cd $SP` — an agent naming a directory once and reusing it. The value is
# right there in the command, so the paths that use it are knowable; treating every `$` as
# unresolvable declined whole sessions that happened to work this way.
_ASSIGN = re.compile(r"(?:^|[\s;&|(])(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>'[^']*'|\"[^\"$`]*\"|[^\s;&|()'\"$`]*)")
_VAR = re.compile(r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|\$(?P<bare>[A-Za-z_][A-Za-z0-9_]*)")
_MOVE = re.compile(r"(?:^|[\s;&|(])(?P<cmd>mv|cp|rm)(?P<args>(?:\s+[^\s;&|()]+)+)")

# Shell metacharacters that make a word unresolvable from the text alone. A path holding one
# names a file only the running shell knew ($SCRATCH/x.py, $(mktemp), *.py), and guessing
# would seed the state under a path that no later edit matches.
_UNRESOLVABLE = re.compile(r"[$`*?\[\]]")

# String methods that transform content in ways this module does not model. A script using one
# would have its OTHER replacements applied to a baseline that never sees this one, so the whole
# script is skipped instead (see the module docstring on why a partial apply is the bad option).
_UNMODELLED_CALLS = frozenset({"sub", "subn", "format", "join", "insert", "pop", "sort", "extend"})


def edits_from_shell(state: dict[str, str], command: str, *, cwd: str = "") -> list[FileEdit]:
    """The file edits recoverable from one shell tool call's ``command`` text.

    ``state`` is the session's running ``path -> content`` reconstruction (mutated in place,
    exactly as :func:`agitrack.transcripts.edits.tracked_edit` does), so an edit here diffs
    against what earlier tool calls left behind and a later editing-tool call diffs against
    what this one wrote. ``cwd`` is the directory the command ran in, used to resolve relative
    paths to the SAME absolute form the editing tools record — without it a file written by
    ``cat > tests/x.py`` and then patched by ``Edit`` would occupy two unrelated state slots
    and be counted twice.

    Never raises: an unparseable command yields no edits, because a backtrace that dies on one
    odd shell line is worse than one that under-reports it.
    """
    try:
        return list(_recover(state, str(command or ""), cwd))
    except Exception:  # noqa: BLE001 - a transcript is untrusted input; degrade, never fail
        return []


def _recover(state: dict[str, str], command: str, cwd: str):
    text = command if command.endswith("\n") else command + "\n"
    lines = text.split("\n")
    index = 0
    # The command's OWN working directory, which `cd` moves and the transcript does not record.
    # Without following it, `cd /elsewhere && cat > notes.md` was resolved against the session's
    # directory and recorded as a repo-root file that has never existed — the reconstruction
    # invented paths, and scratch work done outside the repo was counted as changes to it.
    here = cwd
    # Shell variables the command assigns to itself, so `SP=/tmp/w` … `cd $SP` resolves.
    variables: dict[str, str] = {}
    while index < len(lines):
        line = lines[index]
        index += 1
        _collect_assignments(line, variables)
        here, line_here = _apply_cd(line, here, variables)
        heredoc = _HEREDOC.search(line)
        if heredoc:
            body, index = _heredoc_body(lines, index, heredoc.group("delim"))
            yield from _from_heredoc(state, line, body, line_here, variables)
            continue
        yield from _from_line(state, line, line_here, variables)


def _collect_assignments(line: str, variables: dict[str, str]) -> None:
    """Record ``NAME=value`` assignments on this line, for later ``$NAME`` expansion.

    Only values the text fully determines — a command substitution or a nested unknown
    ``$OTHER`` is not recorded, so the variable stays unresolvable rather than half-known.
    """
    for match in _ASSIGN.finditer(line):
        value = _expand(_norm_word(match.group("value")), variables)
        if value and not _UNRESOLVABLE.search(value):
            variables[match.group("name")] = value


def _expand(word: str, variables: dict[str, str]) -> str:
    """``word`` with every ``$NAME`` / ``${NAME}`` this command assigned substituted in.
    Unknown names are left as-is, so the caller's ``_UNRESOLVABLE`` check still declines them."""
    if "$" not in word:
        return word
    return _VAR.sub(lambda m: variables.get(m.group("braced") or m.group("bare") or "", m.group(0)), word)


def _apply_cd(line: str, here: str, variables: dict[str, str] | None = None) -> tuple[str, str]:
    """``(directory for the lines that follow, directory for THIS line)`` after any ``cd`` on it.

    Applied before the line's own writes, since ``cd sub && cat > f`` writes into ``sub``. The
    two differ for a SUBSHELL: ``(cd pkg && …)`` moves nothing for the caller, and letting it
    persist made a later line's ``pkg/mod.py`` resolve to ``pkg/pkg/mod.py`` — a path that has
    never existed. Inventing paths is the failure mode this whole function exists to remove, so
    a parenthesised ``cd`` is deliberately scoped to its own line.

    A target the text cannot pin down (``cd $VAR``, ``cd -``, a bare ``cd``) yields ``""``,
    which makes every later relative path unresolvable and therefore skipped. Guessing the old
    directory would keep attributing writes to files that were never touched.
    """
    line_here = here
    for match in _CD.finditer(line):
        subshell = match.group(0).startswith("(")
        target = _expand(_norm_word(match.group("target")), variables or {})
        if not target or _UNRESOLVABLE.search(target) or target == "-":
            return ("" if not subshell else here), ""
        if paths.is_absolute(target):
            moved = paths.slash(target).rstrip("/")
        elif not line_here:
            return ("" if not subshell else here), ""
        else:
            moved = posixpath.normpath(paths.slash(line_here).rstrip("/") + "/" + paths.slash(target))
        line_here = moved
        if not subshell:
            here = moved
    return here, line_here


def _heredoc_body(lines: list[str], start: int, delim: str) -> tuple[str, int]:
    """The heredoc body beginning at ``lines[start]``, and the index just past its terminator.

    An unterminated heredoc (a truncated transcript, a delimiter that never appears) consumes
    the rest of the command rather than resyncing: resyncing would read the body's own text as
    shell commands, and a body is very often a Python script full of things that look like one.
    """
    body: list[str] = []
    while start < len(lines):
        line = lines[start]
        start += 1
        if line.strip() == delim:
            break
        body.append(line)
    return ("\n".join(body) + "\n" if body else ""), start


def _from_heredoc(state: dict[str, str], line: str, body: str, cwd: str, variables: dict[str, str]):
    """Edits from a heredoc, dispatched on what the line feeds the body to."""
    if _PYTHON.search(line):
        yield from _from_python(state, body, cwd, variables)
        return
    target, append = _write_target(line)
    if not target:
        return
    path = _resolve(target, cwd, variables)
    if not path:
        return
    content = state.get(path, "") + body if append else body
    edit = tracked_edit(state, path, write=content)
    if edit is not None:
        yield edit


def _write_target(line: str) -> tuple[str, bool]:
    """``(path, append)`` for the file a heredoc line writes to, or ``("", False)``.

    Both orderings occur in the wild — ``cat > f <<'EOF'`` and ``cat <<'EOF' > f`` — as does
    ``cat <<'EOF' | tee -a f``, so the redirect is looked for across the whole line and the
    LAST one wins (a pipeline's final redirect is the one that reaches disk).
    """
    redirects = list(_REDIRECT.finditer(line))
    if redirects:
        last = redirects[-1]
        return last.group("path"), last.group("op") == ">>"
    tee = _TEE.search(line)
    if tee:
        return tee.group("path"), "-a" in tee.group("flags") or "--append" in tee.group("flags")
    return "", False


def _from_line(state: dict[str, str], line: str, cwd: str, variables: dict[str, str]):
    """Edits from a single (non-heredoc) command line."""
    for edit in _from_python_c(state, line, cwd, variables):
        yield edit
    for edit in _from_sed(state, line, cwd, variables):
        yield edit
    for edit in _from_echo(state, line, cwd, variables):
        yield edit
    for edit in _from_move(state, line, cwd, variables):
        yield edit


# --------------------------------------------------------------------------- inline Python


def _from_python_c(state: dict[str, str], line: str, cwd: str, variables: dict[str, str]):
    """Edits from ``python -c '<source>'`` — the same recovery as a Python heredoc."""
    if not _PYTHON.search(line):
        return
    try:
        words = shlex.split(line, comments=False)
    except ValueError:
        return
    for position, word in enumerate(words):
        if word == "-c" and position + 1 < len(words):
            yield from _from_python(state, words[position + 1], cwd, variables)
            return


def _from_python(state: dict[str, str], source: str, cwd: str, variables: dict[str, str]) -> list[FileEdit]:
    """The edits an inline Python script makes, read statically — never executed.

    The shape this recovers is the one agents actually write, and it is the shell spelling of
    an ``Edit`` tool call::

        p = Path("mod.py")
        t = p.read_text()
        t = t.replace(OLD, NEW)
        p.write_text(t)

    so it hands the same ``(old, new)`` pairs to ``tracked_edit`` that ``Edit`` does, and gets
    the same incremental diff; ``write_text`` of a literal is a whole-file write instead.

    The analysis walks STATEMENTS in order and tracks which variable holds which file's
    content, rather than collecting every ``.replace()`` in the script and hoping there is one
    target. One script very often patches two files at once — a code change and the note about
    it — and a flat scan cannot say which replacement belongs to which, so it had to decline
    them all. Anything the text does not determine (a computed replacement, an unmodelled
    transform, a write whose content came from somewhere unseen) makes that ONE file
    unrecoverable and is skipped; the other file in the same script is still recovered.
    """
    with warnings.catch_warnings():
        # Recorded scripts carry things like "\$" that today's Python only warns about; the
        # warning is about the transcript's code, not aGiTrack's, and must not reach stderr.
        warnings.simplefilter("ignore")
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return []

    constants: dict[str, str] = {}
    path_vars: dict[str, str] = {}  # name -> path, from `p = Path("x")`
    text_vars: dict[str, str] = {}  # name -> path whose content the variable holds
    pending: dict[str, list[tuple[str, str]]] = {}  # path -> replacements applied but not written
    # The writes the script performs, in order: ``("write", path, content)`` for a whole-file
    # write and ``("subedits", path, pairs)`` for a patch. Collected first and applied after the
    # walk, so a file the script later turns out to write unreadably is dropped whole.
    operations: list[tuple[str, str, object]] = []
    refused: set[str] = set()

    def literal(node: ast.expr | None) -> str | None:
        """``node``'s string value when the text alone determines it, else None.

        Concatenation is folded because the shape agents write is
        ``t.replace(anchor, new + anchor)`` — a BinOp, not a bare literal — and declining it
        left every one of those edits unrecovered.
        """
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return constants.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = literal(node.left), literal(node.right)
            return None if left is None or right is None else left + right
        return None

    def target_of(node: ast.expr | None) -> str | None:
        """The file a ``Path``-valued expression names: ``Path("x")`` or a name bound to one."""
        direct = _path_literal(node)
        if direct is not None:
            return direct
        return path_vars.get(node.id) if isinstance(node, ast.Name) else None

    def replace_chain(node: ast.expr) -> tuple[str | None, list[tuple[str, str]]]:
        """``(root variable, replacements)`` for a ``t.replace(a, b).replace(c, d)`` chain.

        The root is what the chain reads FROM, so the caller can tell which file's content is
        being transformed. An unresolvable argument returns no root: the chain's result would
        not be what landed on disk, so nothing about it can be trusted.
        """
        pairs: list[tuple[str, str]] = []
        while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "replace":
            if len(node.args) < 2:
                return None, []
            old, new = literal(node.args[0]), literal(node.args[1])
            if old is None or new is None:
                return None, []
            pairs.insert(0, (old, new))
            node = node.func.value
        return (node.id if isinstance(node, ast.Name) else None), pairs

    def reads_file(node: ast.expr) -> str | None:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "read_text":
            return target_of(node.func.value)
        return None

    for node in ast.walk(tree):
        function = node.func if isinstance(node, ast.Call) else None
        name = function.attr if isinstance(function, ast.Attribute) else ""
        if name in _UNMODELLED_CALLS:
            return []  # the script transforms text in a way this cannot reproduce

    for statement in tree.body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            bound = statement.targets[0].id
            value = statement.value
            # The value is classified BEFORE the name is rebound, because the overwhelmingly
            # common statement is `t = t.replace(old, new)` — the variable reads from itself.
            # Clearing `t` first made its own chain look rooted in an unknown variable, and the
            # whole script was declined.
            path_literal = _path_literal(value)
            if path_literal is not None:
                path_vars[bound] = path_literal
                text_vars.pop(bound, None)
                continue
            read = reads_file(value)
            if read is not None:
                text_vars[bound] = read
                continue
            root, pairs = replace_chain(value)
            if root is not None and root in text_vars and pairs:
                path = text_vars[root]
                pending.setdefault(path, []).extend(pairs)
                text_vars[bound] = path
                continue
            text_vars.pop(bound, None)  # rebound to something unmodelled: no longer a file's text
            folded = literal(value)
            if folded is not None:
                constants[bound] = folded
            continue
        call = statement.value if isinstance(statement, ast.Expr) else None
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "write_text"):
            continue
        written_to = target_of(call.func.value)
        if written_to is None:
            continue
        path = written_to
        argument = call.args[0] if call.args else None
        if argument is None:
            refused.add(path)
            continue
        written = literal(argument)
        if written is not None:
            pending.pop(path, None)
            operations.append(("write", path, written))
            continue
        root, pairs = replace_chain(argument)
        if root is None or text_vars.get(root) != path:
            refused.add(path)
            continue
        collected = pending.pop(path, []) + pairs
        if collected:
            operations.append(("subedits", path, collected))

    out: list[FileEdit] = []
    for kind, raw_path, payload in operations:
        if raw_path in refused:
            continue
        resolved = _resolve(raw_path, cwd, variables)
        if not resolved:
            continue
        if kind == "write":
            edit = tracked_edit(state, resolved, write=str(payload))
        else:
            edit = tracked_edit(state, resolved, subedits=list(payload))  # type: ignore[call-overload]
        if edit is not None:
            out.append(edit)
    return out


def _path_literal(node: ast.expr | None) -> str | None:
    """``Path("x")`` / ``open("x", ...)`` -> ``"x"``. None for anything computed."""
    if not (isinstance(node, ast.Call) and node.args):
        return None
    function = node.func
    name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
    if name not in ("Path", "open"):
        return None
    first = node.args[0]
    return first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None


# --------------------------------------------------------------------------- sed -i


def _from_sed(state: dict[str, str], line: str, cwd: str, variables: dict[str, str]):
    """Edits from an in-place ``sed``, applied to the tracked content with :mod:`re`.

    Only files ALREADY tracked are touched: ``sed`` edits a file that exists, and without its
    prior content there is nothing to substitute into. Only ``s///`` is modelled, and only when
    its pattern means the same thing to Python's ``re`` as it does to ``sed`` — see
    :func:`_sed_substitute`.
    """
    for arguments in _invocations(line, "sed"):
        scripts, files, extended = _sed_parts(arguments)
        if not scripts or not files:
            continue
        for name in files:
            path = _resolve(name, cwd, variables)
            if not path or path not in state:
                continue
            before = state[path]
            after = before
            for script in scripts:
                applied = _sed_substitute(script, after, extended=extended)
                if applied is None:
                    after = None  # type: ignore[assignment]
                    break
                after = applied
            if after is None or after == before:
                continue
            state[path] = after
            edit = make_edit(path, before, after)
            if edit is not None:
                yield edit


def _sed_parts(arguments: list[str]) -> tuple[list[str], list[str], bool]:
    """``sed``'s arguments split into ``(scripts, files, extended_regex)``.

    In-place editing is spelled two ways and the difference is not cosmetic: GNU takes
    ``-i`` alone, BSD/macOS requires a backup suffix and agents pass an empty one
    (``sed -i '' 's/a/b/' f``). Reading that ``''`` as the script — or as a filename — is how a
    macOS-recorded session reconstructs nothing at all.
    """
    scripts: list[str] = []
    files: list[str] = []
    extended = False
    in_place = False
    expect_script = False
    index = 0
    while index < len(arguments):
        word = arguments[index]
        index += 1
        if expect_script:
            scripts.append(word)
            expect_script = False
            continue
        if word == "-e":
            expect_script = True
            continue
        if word.startswith("-") and word != "-":
            if "i" in word[1:] and not word.startswith("--"):
                in_place = True
            if "E" in word[1:] or "r" in word[1:] or word == "--regexp-extended":
                extended = True
            if word in ("-i", "--in-place") and index < len(arguments) and not arguments[index].strip():
                index += 1  # BSD's mandatory (here empty) backup suffix
            continue
        if not scripts and not files:
            scripts.append(word)  # the bare script, when no -e was given
            continue
        files.append(word)
    return (scripts if in_place else []), files, extended


# BRE spells grouping, alternation and repetition with BACKSLASHED metacharacters, which mean
# the exact opposite in Python: `\(` is a literal paren to `re` and a group to `sed`. Applying
# such a pattern would substitute the wrong text silently, so those scripts are declined.
_BRE_ONLY = re.compile(r"\\[(){}+?|]")


def _sed_substitute(script: str, content: str, *, extended: bool) -> str | None:
    """``content`` with one ``s/pattern/replacement/flags`` applied, or None if not modelled."""
    script = script.strip()
    if not script.startswith("s") or len(script) < 4:
        return None
    separator = script[1]
    if separator.isalnum() or separator in "\\\n":
        return None
    parts = _split_unescaped(script[2:], separator)
    if len(parts) < 2:
        return None
    pattern, replacement = parts[0], parts[1]
    flags_text = parts[2] if len(parts) > 2 else ""
    if not extended and _BRE_ONLY.search(pattern):
        return None
    if set(flags_text) - set("gi"):
        return None  # a numbered or printing flag: not modelled
    try:
        return re.sub(
            pattern,
            # sed's `&` is the whole match; a literal `&` reaches Python as an ordinary
            # character, so only the unescaped form is translated.
            re.sub(r"(?<!\\)&", r"\\g<0>", replacement),
            content,
            count=0 if "g" in flags_text else 1,
            flags=re.IGNORECASE if "i" in flags_text else 0,
        )
    except re.error:
        return None


def _split_unescaped(text: str, separator: str) -> list[str]:
    """``text`` split on ``separator``, honouring backslash escapes."""
    parts: list[str] = []
    current: list[str] = []
    escaped = False
    for character in text:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            current.append(character)
            escaped = True
        elif character == separator:
            parts.append("".join(current))
            current = []
        else:
            current.append(character)
    parts.append("".join(current))
    return parts


# --------------------------------------------------------------------------- echo / printf


def _from_echo(state: dict[str, str], line: str, cwd: str, variables: dict[str, str]):
    """Edits from ``echo``/``printf`` redirected into a file — the shell's one-line write."""
    for match in _ECHO.finditer(line):
        redirect = _REDIRECT.search(match.group("args"))
        if not redirect:
            continue
        path = _resolve(redirect.group("path"), cwd, variables)
        if not path:
            continue
        payload = _echo_payload(match.group("cmd"), match.group("args")[: redirect.start()])
        if payload is None:
            continue
        content = state.get(path, "") + payload if redirect.group("op") == ">>" else payload
        edit = tracked_edit(state, path, write=content)
        if edit is not None:
            yield edit


def _echo_payload(command: str, arguments: str) -> str | None:
    """The bytes ``echo``/``printf`` would write, or None when they cannot be known."""
    try:
        words = shlex.split(arguments, comments=False)
    except ValueError:
        return None
    newline = True
    escapes = command == "printf"
    while words and words[0] in ("-n", "-e", "-E"):
        flag = words.pop(0)
        newline = newline and flag != "-n"
        escapes = escapes or flag == "-e"
    if not words:
        return None
    if command == "printf":
        text = words[0]
        if "%" in text:
            return None  # format specifiers consume the remaining arguments; not modelled
        newline = False
    else:
        text = " ".join(words)
    if escapes:
        text = text.replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")
    return text + "\n" if newline else text


# --------------------------------------------------------------------------- mv / cp / rm


def _from_move(state: dict[str, str], line: str, cwd: str, variables: dict[str, str]):
    """Deletes and renames, for files the session itself is tracking.

    Bounded to tracked files on purpose. ``rm`` on a file whose content was never recovered
    has no diff to record, and a `mv` of one would only invent a new path holding unknown
    content — while a rename of a file the agent just wrote must be followed, or every later
    edit to the destination diffs against nothing and re-counts the whole file.
    """
    for match in _MOVE.finditer(line):
        command = match.group("cmd")
        try:
            words = [word for word in shlex.split(match.group("args"), comments=False) if not word.startswith("-")]
        except ValueError:
            continue
        if not words:
            continue
        if command == "rm":
            for name in words:
                path = _resolve(name, cwd, variables)
                if path and path in state:
                    edit = make_edit(path, state.pop(path), "", status="deleted")
                    if edit is not None:
                        yield edit
            continue
        if len(words) != 2:
            continue  # `mv a b c/` moves into a directory: the destination paths are guesses
        source, destination = (_resolve(words[0], cwd, variables), _resolve(words[1], cwd, variables))
        if not source or not destination or source not in state:
            continue
        content = state[source] if command == "cp" else state.pop(source)
        if command == "mv":
            removed = make_edit(source, content, "", status="deleted")
            if removed is not None:
                yield removed
        edit = tracked_edit(state, destination, write=content)
        if edit is not None:
            yield edit


# --------------------------------------------------------------------------- helpers


def _invocations(line: str, program: str) -> list[list[str]]:
    """Every ``program`` call on ``line``, as its argument words (the program name dropped).

    Split on the shell's command separators first, so ``grep -c sed f && sed -i 's/a/b/' g``
    yields only the real ``sed`` call and not the word that happened to be an argument.
    """
    out: list[list[str]] = []
    for segment in re.split(r"(?:&&|\|\||[;|])", line):
        try:
            words = shlex.split(segment, comments=False)
        except ValueError:
            continue
        while words and ("=" in words[0] or words[0] in ("env", "command", "sudo", "time")):
            words.pop(0)  # leading VAR=x / wrapper commands
        if words and (words[0] == program or words[0].endswith("/" + program)):
            out.append(words[1:])
    return out


def _norm_word(word: str) -> str:
    """A shell word with its surrounding quotes removed."""
    return word.strip().strip("'\"")


def _resolve(name: str, cwd: str, variables: dict[str, str] | None = None) -> str:
    """``name`` as the absolute path the editing tools would have recorded, or ``""``.

    Empty for anything the text cannot pin down: a ``$VAR``/glob/substitution, a device
    (``/dev/null``), or a relative path with no ``cwd`` to anchor it. Returning the relative
    path instead would put the same file in ``file_state`` twice — once as ``tests/x.py`` from
    the shell and once as ``/repo/tests/x.py`` from ``Edit`` — and count its lines twice.
    """
    name = _expand(_norm_word(name), variables or {})
    if not name or _UNRESOLVABLE.search(name):
        return ""
    if name.startswith("/dev/") or name.startswith("~"):
        return ""
    if paths.is_absolute(name):
        return paths.slash(name)
    if not cwd:
        return ""
    flat = paths.slash(name)
    while flat.startswith("./"):
        flat = flat[2:]
    if not flat or flat.startswith("../"):
        return ""  # above the session's directory: outside the repo the backtrace describes
    return paths.slash(cwd).rstrip("/") + "/" + flat
