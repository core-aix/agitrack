from __future__ import annotations

import copy
import json
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agitrack.fileio import atomic_write_text, merge_json_for_save
from agitrack.proc import UTF8_TEXT, console_isolation_kwargs, fs_path

# The git-resolved .git/info/exclude path per repo root — invariant for a run, so resolve it once
# (see AgitrackState._exclude_path) instead of spawning `git rev-parse` on every save/ensure.
_EXCLUDE_PATH_CACHE: dict[Path, Path] = {}

# What aGiTrack adds to the repo's `.git/info/exclude` — every path IT writes inside the user's
# working tree, so none of them ever appears in `git status` as the user's change.
#
# `.claude/settings.local.json` is on this list because aGiTrack WRITES it (the Stop /
# SessionStart hooks that make auto-start work). Only `.agitrack/` was ever excluded, so a new
# user saw `?? .claude/` the moment they ran `agitrack -b` — and in a submodule that surfaced in
# the parent as ` M vendor/sub`. It went unnoticed for so long because the developers' own
# machines happened to carry a personal `**/.claude/settings.local.json` in
# ~/.config/git/ignore, which aGiTrack did not create and no new user has.
#
# An exclude entry has no effect on a file the repo already TRACKS, which is the correct
# behaviour here: a team that deliberately commits that file keeps deciding for themselves.
# What protects THEM is `add_tracked()`'s scaffolding filter (git/repo.py).
# The other two backends' session-start hooks are here for the same reason, and they matter more
# now that every INSTALLED backend gets one (see backends/agent_hooks.py): a repo can pick up
# aGiTrack files for an agent its owner never opens. `.opencode/plugin/agitrack-autostart.js` is
# aGiTrack's alone and lives in the user's SOURCE TREE rather than in a dot-config the tools
# hide — without the exclude it shows up as `?? .opencode/` and gets swept into the user's own
# `git add .` (seen live: aGiTrack's plugin committed as part of someone's first commit).
# `.codex/hooks.json` is a file aGiTrack MERGES into rather than owns, so excluding it is the one
# entry here with a cost: a team that hand-writes Codex project hooks and has not committed them
# yet stops seeing that file in `git status`. It is still the right trade — an exclude never
# affects a TRACKED file, so a team that does commit it is untouched, while every other repo is
# spared an untracked file it did not ask for and did not create.
_EXCLUDE_LINES = (
    ".agitrack/",
    ".claude/settings.local.json",
    ".codex/hooks.json",
    ".opencode/plugin/agitrack-autostart.js",
)


class AgitrackState:
    def __init__(self, repo: Path, *, default_backend: str | None = None) -> None:
        self.repo = repo
        self.path = repo / ".agitrack" / "state.json"
        self.config_path = repo / ".agitrack" / "config.json"
        self._default_backend = default_backend
        self.data = self._load()
        self.config = self._load_config()
        # What this instance believes was on disk when it loaded. save() writes only the
        # difference against it, so a concurrent writer's keys are not dropped — see
        # fileio.merge_json_for_save.
        self._baseline = copy.deepcopy(self.data)
        self._config_baseline = copy.deepcopy(self.config)
        self._saves_suspended = 0

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default()
        try:
            # utf-8-SIG: a BOM (a Windows editor, `Out-File -Encoding utf8`) otherwise made
            # json.load raise and the file was quarantined as corrupt. Identical to utf-8
            # when there is no BOM.
            with self.path.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            # A truncated or invalid state file must not brick startup. Keep the
            # corrupt file aside for debugging and start from defaults.
            self._quarantine_corrupt()
            return self._default()
        if not isinstance(data, dict):
            self._quarantine_corrupt()
            return self._default()
        # Pre-rename state files keyed the session id as ``agit_session_id``; carry
        # the existing value over so a session isn't given a fresh id (which would
        # orphan its worktree/branches) after upgrading to aGiTrack.
        if "agitrack_session_id" not in data and "agit_session_id" in data:
            data["agitrack_session_id"] = data.pop("agit_session_id")
        default = self._default()
        default.update(data)
        return default

    def _quarantine_corrupt(self) -> None:
        try:
            self.path.replace(self.path.with_name(self.path.name + ".bak"))
        except OSError:
            pass

    def _default(self) -> dict[str, Any]:
        return {
            "agitrack_session_id": f"agitrack-{uuid.uuid4()}",
            "backend": self._default_backend,
            "model": None,
            "backend_session_id": None,
            "backend_session_repo": None,
            "backend_session_ids": {},
            "backend_sessions": {},
            "session_names": {},
            "last_backend_message_id": None,
            "declined_untracked_files": [],
            "pending_trace": [],
            "pending_token_usage": {
                "context": None,
                "total": 0,
                "input": 0,
                "output": 0,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
                "subagent_input": 0,
                "subagent_output": 0,
                "subagent_reasoning": 0,
                "subagent_cache_read": 0,
                "subagent_cache_write": 0,
            },
            "session_summary": None,
            "session_summary_commit": None,
        }

    def _default_config(self) -> dict[str, Any]:
        return {
            "trace_turn_limit": 5,
            "summarization_model": None,
            "summarization_enabled": True,
            "full_agent_messages": False,
        }

    def _load_config(self) -> dict[str, Any]:
        default = self._default_config()
        if not self.config_path.exists():
            return default
        try:
            with self.config_path.open("r", encoding="utf-8-sig") as handle:  # tolerate a BOM
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return default  # user-edited file; don't crash, just use defaults
        default.update(data if isinstance(data, dict) else {})
        return default

    @contextmanager
    def suspend_saves(self) -> Iterator[None]:
        """Keep mutations in memory instead of writing them, for a block that must not touch
        the file yet. Used where state is prepared BEFORE the repo lock is held: writing there
        rewrites a live tracker's state file even when the run is then refused, and the
        tracker's own next save reverts it — a lost update in both directions."""
        self._saves_suspended += 1
        try:
            yield
        finally:
            self._saves_suspended -= 1

    def save(self) -> None:
        if self._saves_suspended:
            return
        self._ensure_repo_local_ignore()
        # Atomic write (unique tmp, see fileio): save() runs on every property setter, so
        # an in-place rewrite interrupted by a crash/SIGKILL/full disk would leave exactly
        # the truncated file that bricks the next startup — and a FIXED tmp name crashed
        # whenever a second aGiTrack process (dashboard, export, tracker) saved this same
        # repo's state concurrently.
        #
        # Merge against the file rather than overwriting it: atomicity is not isolation, and
        # a whole-document write silently discards whatever another live instance (a second
        # AgitrackState in this process, or another aGiTrack process on this repo) wrote in
        # the meantime. See fileio.merge_json_for_save.
        merged = merge_json_for_save(self.path, self.data, self._baseline)
        atomic_write_text(self.path, json.dumps(merged, indent=2, sort_keys=True) + "\n")
        # Adopt the merged document (in place, so anything holding ``state.data`` keeps
        # seeing the live dict) and re-baseline: this instance is now in sync with disk.
        self.data.clear()
        self.data.update(merged)
        self._baseline = copy.deepcopy(merged)

    def ensure_local_ignore(self) -> None:
        """Make sure everything aGiTrack writes into the repo is git-ignored (idempotent). Call
        this before writing any ``.agitrack/`` file (the manual trailer/ref, handshake, …) so
        aGiTrack's internal state can never leak into a ``git add -A`` / user commit — ``save()``
        also does it, but state isn't always saved before those files are written (e.g. an idle
        daemon)."""
        self._ensure_repo_local_ignore()

    def add_local_ignore(self, line: str) -> None:
        """Add one extra pattern to this repo's ``.git/info/exclude`` (idempotent).

        For paths aGiTrack writes whose location the USER chose, so they cannot live in the
        fixed ``_EXCLUDE_LINES`` list — today that is the ``--log-file`` event log."""
        self._ensure_repo_local_ignore(extra=(line,))

    def _ensure_repo_local_ignore(self, *, extra: tuple[str, ...] = ()) -> None:
        wanted = (*_EXCLUDE_LINES, *extra)
        exclude = self._exclude_path()
        if exclude is None:
            return
        if not exclude.exists():
            # Repos created without the default template have no info/exclude;
            # create it (only in an actual git repo) so aGiTrack's files stay unignored
            # nowhere. The worktree case resolves to the shared git dir via git.
            if not (self.repo / ".git").exists():
                return
            try:
                exclude.parent.mkdir(parents=True, exist_ok=True)
                exclude.write_text("".join(f"{line}\n" for line in wanted), encoding="utf-8")
            except OSError:
                pass
            return
        content = exclude.read_text(encoding="utf-8")
        present = set(content.splitlines())
        missing = [line for line in wanted if line not in present]
        if not missing:
            return
        with exclude.open("a", encoding="utf-8") as handle:
            if content and not content.endswith("\n"):
                handle.write("\n")
            for line in missing:
                handle.write(f"{line}\n")

    def _exclude_path(self) -> Path | None:
        # The info/exclude location is invariant for a repo, but ensure_local_ignore() /
        # save() ask for it constantly (every state save, every daemon poll). Resolving it via
        # git each time spawns a `git rev-parse` subprocess — dozens during the startup burst
        # (a chunk of the slow start + the terminal "git" title flicker). Cache it per repo so
        # the git call happens once; the (cheap) file read/append in _ensure_repo_local_ignore
        # still runs each time, so an externally emptied exclude file is still re-populated.
        cached = _EXCLUDE_PATH_CACHE.get(self.repo)
        if cached is not None:
            return cached
        resolved = self._resolve_exclude_path()
        if resolved is not None:
            _EXCLUDE_PATH_CACHE[self.repo] = resolved
        return resolved

    def _resolve_exclude_path(self) -> Path | None:
        # Resolve the info/exclude path via git so it works inside a worktree,
        # where ``.git`` is a file pointing at the shared git dir rather than a
        # directory. Fall back to the conventional location when git is not
        # available (e.g. tests with a fabricated .git/info).
        fallback = self.repo / ".git" / "info" / "exclude"
        try:
            process = subprocess.run(
                ["git", "rev-parse", "--git-path", "info/exclude"],
                cwd=self.repo,
                **UTF8_TEXT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                # Keep git off a console on Windows. This runs on EVERY state save (and the
                # background daemon saves/ensures the ignore each poll cycle); the detached,
                # console-less daemon would otherwise allocate — and flash — a new console
                # window per call, once every few seconds. See proc.py.
                **console_isolation_kwargs(),
            )
        except OSError:
            return fallback
        if process.returncode != 0:
            return fallback
        path = fs_path(process.stdout.strip())  # git printed UTF-8; see proc.fs_path
        return path if path.is_absolute() else self.repo / path

    @property
    def session_id(self) -> str:
        return str(self.data["agitrack_session_id"])

    def new_agitrack_session_id(self) -> str:
        self.data["agitrack_session_id"] = f"agitrack-{uuid.uuid4()}"
        self.save()
        return self.session_id

    @property
    def backend(self) -> str:
        # Honour the configured default (not a hardcoded backend) when the record
        # has no backend yet, or when the stored value is no longer a known
        # backend, so a missing/stale entry never silently launches the wrong
        # agent (and make_proxy_agent never receives an invalid name). With no
        # stored value AND no configured default this RAISES rather than silently
        # falling back to some hardcoded agent — the caller is expected to have
        # resolved a backend (prompt/error) before reaching a spawn path.
        from agitrack.backends.proxy_agents import available_backends

        stored = self.data.get("backend")
        if stored and stored in available_backends():
            return str(stored)
        if self._default_backend:
            return self._default_backend
        raise RuntimeError(
            "No coding agent backend is configured for this session. Run aGiTrack in an "
            "interactive terminal to choose a default, or pass --backend <" + "|".join(available_backends()) + ">."
        )

    @backend.setter
    def backend(self, value: str) -> None:
        self.data["backend"] = value
        self.save()

    @property
    def model(self) -> str | None:
        value = self.data.get("model")
        return str(value) if value else None

    @model.setter
    def model(self, value: str | None) -> None:
        self.data["model"] = value
        self.save()

    @property
    def backend_session_id(self) -> str | None:
        value = self.data.get("backend_session_id")
        return str(value) if value else None

    @backend_session_id.setter
    def backend_session_id(self, value: str | None) -> None:
        self.data["backend_session_id"] = value
        self.data["backend_session_repo"] = str(self.repo) if value else None
        self.save()

    def backend_session_matches_repo(self) -> bool:
        return self.data.get("backend_session_repo") == str(self.repo)

    def remember_backend_session(self) -> None:
        """Record the current backend's session id so it can be restored when
        the user switches back to this backend."""
        sessions = dict(self.data.get("backend_session_ids") or {})
        if self.backend_session_id:
            sessions[self.backend] = self.backend_session_id
        else:
            sessions.pop(self.backend, None)
        self.data["backend_session_ids"] = sessions
        self.save()

    def stored_backend_session(self, backend: str) -> str | None:
        value = (self.data.get("backend_session_ids") or {}).get(backend)
        return str(value) if value else None

    def remember_session(
        self,
        backend: str,
        *,
        session_id: str | None,
        worktree: str,
        message_id: str | None = None,
        model: str | None = None,
    ) -> None:
        """Record a backend's most recent conversation (its id and the worktree it
        ran in) so it can be resumed after its worktree is removed on exit."""
        sessions = dict(self.data.get("backend_sessions") or {})
        if session_id:
            sessions[backend] = {"id": session_id, "worktree": worktree, "message_id": message_id, "model": model}
        else:
            sessions.pop(backend, None)
        self.data["backend_sessions"] = sessions
        self.save()

    def recall_session(self, backend: str) -> dict | None:
        record = (self.data.get("backend_sessions") or {}).get(backend)
        return dict(record) if isinstance(record, dict) else None

    def session_name_for(self, session_id: str | None) -> str | None:
        """The user-given name for a backend conversation, if one was set."""
        if not session_id:
            return None
        value = (self.data.get("session_names") or {}).get(str(session_id))
        return str(value) if value else None

    @property
    def pending_session_name(self) -> str | None:
        """A name chosen for a session whose backend conversation did not exist yet."""
        value = self.data.get("pending_session_name")
        return str(value) if value else None

    def remember_pending_session_name(self, name: str | None) -> None:
        """Persist a session's name the MOMENT it is chosen, before there is a backend conversation
        id to key it against (:meth:`name_session`). A brand-new conversation only gets its id once
        the backend creates it, so without this a non-graceful exit — kill -9, a closed terminal —
        loses the name the user just typed. Cleared once the name is linked to a real id."""
        if name:
            self.data["pending_session_name"] = name
        else:
            self.data.pop("pending_session_name", None)
        self.save()

    def name_session(self, session_id: str | None, name: str | None) -> None:
        """Record (or clear) the user-given name for a backend conversation, and
        stamp when it was last named so a session with no transcript of its own
        (e.g. a no-commit session surfaced for resume) still has a real date to
        show instead of the Unix epoch."""
        if not session_id:
            return
        names = dict(self.data.get("session_names") or {})
        stamps = dict(self.data.get("session_named_at") or {})
        if name:
            names[str(session_id)] = name
            stamps[str(session_id)] = time.time()
        else:
            names.pop(str(session_id), None)
            stamps.pop(str(session_id), None)
        self.data["session_names"] = names
        self.data["session_named_at"] = stamps
        self.save()

    def session_named_at(self, session_id: str | None) -> float:
        """Epoch when ``session_id`` was last named (0.0 if unknown)."""
        if not session_id:
            return 0.0
        value = (self.data.get("session_named_at") or {}).get(str(session_id))
        return float(value) if isinstance(value, (int, float)) else 0.0

    @property
    def last_backend_message_id(self) -> str | None:
        value = self.data.get("last_backend_message_id")
        return str(value) if value else None

    @last_backend_message_id.setter
    def last_backend_message_id(self, value: str | None) -> None:
        self.data["last_backend_message_id"] = value
        self.save()

    # --- per-conversation commit watermark ---------------------------------
    # The single ``last_backend_message_id`` above is the high-water assistant message
    # id already committed. That is correct while a session stays on ONE backend
    # conversation, but it double-counts when the user switches conversations inside the
    # backend and later switches back: the watermark still holds the OTHER conversation's
    # id, which isn't found in this one, so ``turns_after`` replays (and re-counts) every
    # already-committed turn. Keying the watermark per conversation fixes that — each
    # conversation remembers its own high-water mark, so switching is exact. The legacy
    # single value is kept in sync for backward compatibility and continuity across upgrade.

    def backend_message_id_for(self, session_id: str | None) -> str | None:
        """The committed high-water assistant message id to use when parsing *session_id*.

        A conversation reads its OWN remembered mark from the per-conversation map, and
        ``None`` when it has never committed — every one of its turns is then new. Only that
        makes switching between conversations exact: switching back to a prior one no longer
        replays and re-counts its committed turns.

        **A conversation must never inherit ANOTHER conversation's mark.** The map used to be
        consulted only for a conversation OTHER than the tracked one, so a brand-new
        conversation — ``backend_session_id`` already reassigned to it, nothing of its own
        committed yet — read the global ``last_backend_message_id`` still holding the PREVIOUS
        conversation's id. That id matches no turn boundary in the new conversation, and
        ``backend_message_marked_at_for`` (rightly per-conversation) returns None for it, so
        ``turns_after`` fell through to its last-resort ``turns[-1:]`` branch and silently
        discarded every earlier turn — prompt, tokens and trace never reaching any commit.
        Seen live: commit bccd5332 in this repo covers the second of two turns and the first
        was never recorded anywhere (2026-08-15).

        Two things still hold, which is why the global is not simply ignored:

        * **An explicit reset still clears the watermark.** Several paths do
          ``state.last_backend_message_id = None`` to mean "start tracking from scratch"; a
          falsy global therefore short-circuits to None before the map is ever read.
        * **Legacy state keeps working.** ``set_backend_message_id`` always writes the global
          and the map entry together, so a global that appears NOWHERE in the map predates the
          map (an upgrade) and is the tracked conversation's own mark — it is honoured. A
          global that IS in the map belongs to whichever conversation recorded it, and is
          never lent to a different one."""
        if not session_id:
            return self.last_backend_message_id
        ids = self.data.get("backend_message_ids") or {}
        if session_id == self.backend_session_id:
            current = self.last_backend_message_id
            if not current:
                return None  # an existing "recompute from scratch" reset still governs
            own = ids.get(str(session_id))
            if own:
                return str(own)
            if current in {str(known) for known in ids.values() if known}:
                return None  # the global is ANOTHER conversation's mark; this one has none
            return current  # pre-map legacy state: the global is this conversation's own mark
        value = ids.get(str(session_id))
        return str(value) if value else None

    def set_backend_message_id(
        self, session_id: str | None, message_id: str | None, *, marked_at: float | None = None
    ) -> None:
        """Advance the committed watermark for *session_id*: the primary single value (so all
        existing readers/resetters keep working) and the per-conversation map entry, together
        in one save. ``marked_at`` (the watermark turn's end time) backs turns_after's
        safe fallback when a compaction later reshapes turn boundaries and the id stops
        matching — without it, a lost mark once re-exported a whole 20-day session."""
        self.data["last_backend_message_id"] = message_id
        ids = dict(self.data.get("backend_message_ids") or {})
        marks = dict(self.data.get("backend_message_marked_at") or {})
        if session_id:
            if message_id:
                ids[str(session_id)] = message_id
                if marked_at:
                    marks[str(session_id)] = marked_at
            else:
                ids.pop(str(session_id), None)
                marks.pop(str(session_id), None)
        self.data["backend_message_ids"] = ids
        self.data["backend_message_marked_at"] = marks
        self.save()

    def backend_message_marked_at_for(self, session_id: str | None) -> float | None:
        marks = self.data.get("backend_message_marked_at") or {}
        value = marks.get(str(session_id)) if session_id else None
        return float(value) if isinstance(value, (int, float)) else None

    def declined_untracked(self) -> list[str]:
        return list(self.data.get("declined_untracked_files") or [])

    def add_declined(self, paths: list[str]) -> None:
        current = set(self.declined_untracked())
        current.update(paths)
        self.data["declined_untracked_files"] = sorted(current)
        self.save()

    def remove_declined(self, paths: list[str]) -> None:
        remove = set(paths)
        self.data["declined_untracked_files"] = [path for path in self.declined_untracked() if path not in remove]
        self.save()

    def keep_declined(self, paths: list[str]) -> None:
        keep = set(paths)
        self.data["declined_untracked_files"] = [path for path in self.declined_untracked() if path in keep]
        self.save()

    # --- shared-session auto-update opt-in (issue #55) ---------------------
    # The backend session ids the user has asked aGiTrack to keep shared (re-redact
    # and re-push as the conversation grows). Per-repo, opt-in, off by default.

    def auto_share_session_ids(self) -> list[str]:
        return list(self.data.get("auto_share_sessions") or [])

    def auto_share_enabled(self, session_id: str | None) -> bool:
        return bool(session_id) and session_id in self.auto_share_session_ids()

    def set_auto_share(self, session_id: str, enabled: bool) -> None:
        current = set(self.auto_share_session_ids())
        if enabled:
            current.add(session_id)
        else:
            current.discard(session_id)
        self.data["auto_share_sessions"] = sorted(current)
        self.save()

    # --- shared-session id lineage (#55) -----------------------------------
    # The backend can mint a new session id when a conversation is resumed
    # (Claude forks on `--resume`). A session shared or auto-shared under its old
    # id must still be recognised as shared after that drift, otherwise the
    # marker and auto-update silently disappear on the next run. We record, for a
    # drifted live id, the previous id it forked from, so callers can walk back to
    # the original (shared) id.

    def shared_session_aliases(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in (self.data.get("shared_session_aliases") or {}).items()}

    def add_shared_session_alias(self, new_id: str | None, previous_id: str | None) -> None:
        if not new_id or not previous_id or new_id == previous_id:
            return
        aliases = self.shared_session_aliases()
        aliases[str(new_id)] = str(previous_id)
        self.data["shared_session_aliases"] = aliases
        self.save()

    def session_lineage(self, session_id: str | None) -> list[str]:
        """The id plus every ancestor id it forked from across resume drift."""
        if not session_id:
            return []
        aliases = self.shared_session_aliases()
        chain = [str(session_id)]
        seen = {str(session_id)}
        cur = str(session_id)
        while cur in aliases:
            parent = aliases[cur]
            if parent in seen:
                break  # defensive: never loop on a corrupt chain
            chain.append(parent)
            seen.add(parent)
            cur = parent
        return chain

    # --- shared-session lineage origin (#55) -------------------------------
    # The identity a session was first shared under: its origin OWNER (the first
    # sharer's github id), origin NAME, and the accumulating set of CONTRIBUTORS
    # (every github id that has shared it). Tracked per backend session id and
    # carried across resume id-drift so re-sharing a session imported from another
    # machine updates the SAME shared entry — keyed by (owner, name), not the local
    # sharer — and merges the sharer into the contributor set. This is what keeps
    # one logical session as ONE entry whose display is `<id1>+<id2>/<name>` instead
    # of spawning a fresh `<sharer>/<name>` on every machine it round-trips through.

    def shared_origin(self, session_id: str | None) -> dict | None:
        """The lineage origin record ``{owner, name, contributors}`` a session was
        first shared under, or None. Falls back to the legacy name-only record."""
        if not session_id:
            return None
        rec = (self.data.get("shared_origins") or {}).get(str(session_id))
        if isinstance(rec, dict) and rec.get("name"):
            return {
                "owner": str(rec.get("owner") or ""),
                "name": str(rec["name"]),
                "contributors": [str(c) for c in (rec.get("contributors") or [])],
            }
        name = (self.data.get("shared_origin_names") or {}).get(str(session_id))
        if name:  # older state recorded only the name
            return {"owner": "", "name": str(name), "contributors": []}
        return None

    def set_shared_origin(
        self, session_id: str | None, *, owner: str | None, name: str | None, contributors: list[str] | None = None
    ) -> None:
        if not session_id:
            return
        origins = dict(self.data.get("shared_origins") or {})
        legacy = dict(self.data.get("shared_origin_names") or {})
        if name:
            origins[str(session_id)] = {
                "owner": str(owner or ""),
                "name": str(name),
                "contributors": sorted({str(c) for c in (contributors or []) if c}),
            }
            legacy[str(session_id)] = str(name)  # keep the legacy map in sync
        else:
            origins.pop(str(session_id), None)
            legacy.pop(str(session_id), None)
        self.data["shared_origins"] = origins
        self.data["shared_origin_names"] = legacy
        self.save()

    def shared_origin_name(self, session_id: str | None) -> str | None:
        rec = self.shared_origin(session_id)
        return rec["name"] if rec else None

    def set_shared_origin_name(self, session_id: str | None, name: str | None) -> None:
        # Back-compat shim: record just the name (no owner/contributors). New callers
        # should use set_shared_origin to capture the full lineage identity.
        self.set_shared_origin(session_id, owner=None, name=name)

    # --- session origin event (fork / copy) --------------------------------
    # A one-shot record that THIS session was started by forking or copying another
    # conversation, set when the fork/copy happens and surfaced by the next agent
    # commit, then cleared. A forked/copied session resumes a transcript that already
    # carries prior turns (and the original's token usage), so noting the lineage in
    # the commit makes the inherited context — and the token counts that ride on it —
    # interpretable. ``kind`` is "fork" (same user, new lineage) or "copy" (a peer's
    # shared session brought in here).

    def session_origin_event(self) -> dict | None:
        rec = self.data.get("session_origin_event")
        return dict(rec) if isinstance(rec, dict) else None

    def set_session_origin_event(
        self,
        *,
        kind: str,
        source: str | None,
        collaborator: str | None = None,
        source_name: str | None = None,
    ) -> None:
        self.data["session_origin_event"] = {
            "kind": kind,
            "source": str(source or ""),
            "collaborator": str(collaborator or ""),
            "source_name": str(source_name or ""),
            "at": int(time.time()),
        }
        self.save()

    def clear_session_origin_event(self) -> None:
        if self.data.pop("session_origin_event", None) is not None:
            self.save()

    def pending_trace(self) -> list[dict]:
        return list(self.data.get("pending_trace") or [])

    @property
    def trace_turn_limit(self) -> int:
        value = self.config.get("trace_turn_limit", 5)
        return value if isinstance(value, int) and value > 0 else 5

    @property
    def full_agent_messages(self) -> bool:
        # When on, the interaction trace records every user-facing message the agent
        # sent during a turn (each as its own "## Agent" block), not just the final
        # one. Off by default — the latest message is usually the substantive reply,
        # and intermediate progress notes add length. Tool calls / file edits are
        # never included either way.
        value = self.config.get("full_agent_messages")
        return bool(value)

    @full_agent_messages.setter
    def full_agent_messages(self, value: bool) -> None:
        self.config["full_agent_messages"] = bool(value)
        self._save_config()

    @property
    def merge_branch(self) -> str | None:
        # The branch this session's worktree integrates ("merges") into. Persisted
        # per worktree so aGiTrack can verify it never merges a DIFFERENT branch into
        # this worktree (cross-branch contamination), independent of which session is
        # active when a sync runs.
        value = self.config.get("merge_branch")
        return str(value) if value else None

    @merge_branch.setter
    def merge_branch(self, value: str | None) -> None:
        self.config["merge_branch"] = value
        self._save_config()

    @property
    def copy_full_env(self) -> bool:
        # Whether this worktree was created with the FULL base environment copied in
        # (untracked + git-ignored files), as opposed to only the tracked files git checks
        # out. Persisted per worktree so a later reuse knows whether to keep the environment
        # in sync with the base, rather than re-asking or syncing a tracked-only worktree.
        return bool(self.config.get("copy_full_env", False))

    @copy_full_env.setter
    def copy_full_env(self, value: bool) -> None:
        self.config["copy_full_env"] = bool(value)
        self._save_config()

    @property
    def summarization_model(self) -> str | None:
        value = self.config.get("summarization_model")
        return str(value) if value else None

    @summarization_model.setter
    def summarization_model(self, value: str | None) -> None:
        self.config["summarization_model"] = value
        self._save_config()

    @property
    def summarization_enabled(self) -> bool:
        value = self.config.get("summarization_enabled")
        return True if value is None else bool(value)

    @summarization_enabled.setter
    def summarization_enabled(self, value: bool) -> None:
        self.config["summarization_enabled"] = bool(value)
        self._save_config()

    def _save_config(self) -> None:
        # ``<repo>/.agitrack/config.json`` is written by TWO classes with disjoint key sets —
        # this one and GlobalConfig.save_repo (the repo settings overlay, which the dashboard
        # daemon also writes) — so an unmerged whole-file write here drops every setting the
        # other one owns. Merge, exactly as save() does.
        merged = merge_json_for_save(self.config_path, self.config, self._config_baseline)
        atomic_write_text(self.config_path, json.dumps(merged, indent=2, sort_keys=True) + "\n")
        self.config.clear()
        self.config.update(merged)
        self._config_baseline = copy.deepcopy(merged)

    def append_trace(self, role: str, content: str, *, starts_turn: bool = True) -> None:
        """Add one entry to the pending trace.

        ``starts_turn=False`` marks an entry that CONTINUES the turn before it rather than
        opening a new one — a message the user queued while the agent was working. It reads as
        its own `## User` block (it was sent after the agent had already spoken), but it is not a
        turn, and the trace limiter must not count it as one. Absent on entries written by older
        installs, which is why the default preserves the previous meaning."""
        trace = self.pending_trace()
        item: dict[str, object] = {"role": role, "content": content}
        if not starts_turn:
            item["starts_turn"] = False
        trace.append(item)
        self.data["pending_trace"] = trace
        self.save()

    def clear_trace(self) -> None:
        self.data["pending_trace"] = []
        self.data["pending_token_usage"] = self._default()["pending_token_usage"]
        self.save()

    # --- partial-turn token accounting (see CommitEngine._add_turn_usage) -----------
    # A turn force-captured while still running is re-exported INCLUSIVELY once it
    # finishes (the watermark sat on its user id). This records what its first commit
    # already counted, so the re-commit adds only the delta instead of the whole,
    # now-larger turn again — the mechanism behind the 13-26x token inflation seen on
    # days with restarts mid-turn (2026-07-25).

    def partial_turn_usage(self) -> dict | None:
        record = self.data.get("partial_turn_tokens")
        return record if isinstance(record, dict) else None

    def set_partial_turn_usage(self, session_id: str | None, user_id: str, usage: dict) -> None:
        self.data["partial_turn_tokens"] = {"session_id": session_id, "user_id": user_id, "usage": usage}
        self.save()

    def clear_partial_turn_usage(self) -> None:
        if self.data.pop("partial_turn_tokens", None) is not None:
            self.save()

    def pending_token_usage(self) -> dict[str, int | None]:
        usage = dict(self._default()["pending_token_usage"])
        usage.update(self.data.get("pending_token_usage") or {})
        return usage

    def add_token_usage(self, usage) -> None:
        current = self.pending_token_usage()
        if usage.context is not None:
            current["context"] = usage.context
        for key in (
            "total",
            "input",
            "output",
            "reasoning",
            "cache_read",
            "cache_write",
            "subagent_input",
            "subagent_output",
            "subagent_reasoning",
            "subagent_cache_read",
            "subagent_cache_write",
        ):
            current[key] = int(current.get(key) or 0) + int(getattr(usage, key, 0) or 0)
        self.data["pending_token_usage"] = current
        self.save()

    @property
    def session_summary(self) -> str | None:
        value = self.data.get("session_summary")
        return str(value) if value else None

    @session_summary.setter
    def session_summary(self, value: str | None) -> None:
        self.data["session_summary"] = value
        self.save()

    @property
    def session_summary_commit(self) -> str | None:
        value = self.data.get("session_summary_commit")
        return str(value) if value else None

    @session_summary_commit.setter
    def session_summary_commit(self, value: str | None) -> None:
        self.data["session_summary_commit"] = value
        self.save()
