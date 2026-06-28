from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any


class AgitrackState:
    def __init__(self, repo: Path, *, default_backend: str | None = None) -> None:
        self.repo = repo
        self.path = repo / ".agitrack" / "state.json"
        self.config_path = repo / ".agitrack" / "config.json"
        self._default_backend = default_backend
        self.data = self._load()
        self.config = self._load_config()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
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
            with self.config_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return default  # user-edited file; don't crash, just use defaults
        default.update(data if isinstance(data, dict) else {})
        return default

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_repo_local_ignore()
        # Atomic write: save() runs on every property setter, so an in-place
        # rewrite interrupted by a crash/SIGKILL/full disk would leave exactly
        # the truncated file that bricks the next startup.
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.path)

    def _ensure_repo_local_ignore(self) -> None:
        exclude = self._exclude_path()
        if exclude is None:
            return
        if not exclude.exists():
            # Repos created without the default template have no info/exclude;
            # create it (only in an actual git repo) so .agitrack/ stays unignored
            # nowhere. The worktree case resolves to the shared git dir via git.
            if not (self.repo / ".git").exists():
                return
            try:
                exclude.parent.mkdir(parents=True, exist_ok=True)
                exclude.write_text(".agitrack/\n", encoding="utf-8")
            except OSError:
                pass
            return
        content = exclude.read_text(encoding="utf-8")
        if ".agitrack/" in content.splitlines():
            return
        with exclude.open("a", encoding="utf-8") as handle:
            if content and not content.endswith("\n"):
                handle.write("\n")
            handle.write(".agitrack/\n")

    def _exclude_path(self) -> Path | None:
        # Resolve the info/exclude path via git so it works inside a worktree,
        # where ``.git`` is a file pointing at the shared git dir rather than a
        # directory. Fall back to the conventional location when git is not
        # available (e.g. tests with a fabricated .git/info).
        fallback = self.repo / ".git" / "info" / "exclude"
        try:
            process = subprocess.run(
                ["git", "rev-parse", "--git-path", "info/exclude"],
                cwd=self.repo,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError:
            return fallback
        if process.returncode != 0:
            return fallback
        path = Path(process.stdout.strip())
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
            "interactive terminal to choose a default, or pass --backend <claude|opencode>."
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_name(self.config_path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self.config, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.config_path)

    def append_trace(self, role: str, content: str) -> None:
        trace = self.pending_trace()
        trace.append({"role": role, "content": content})
        self.data["pending_trace"] = trace
        self.save()

    def clear_trace(self) -> None:
        self.data["pending_trace"] = []
        self.data["pending_token_usage"] = self._default()["pending_token_usage"]
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
