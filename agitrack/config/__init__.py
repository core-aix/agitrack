"""Persistence and settings: repository-local state (``.agitrack/state.json``) and
user-wide settings (``~/.agitrack/config.json``)."""

from agitrack.config.settings import DEFAULT_TIMINGS, GlobalConfig
from agitrack.config.state import AgitrackState

__all__ = ["AgitrackState", "GlobalConfig", "DEFAULT_TIMINGS"]
