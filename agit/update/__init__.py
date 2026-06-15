"""aGiT self-update: detect the installation shape and update in place."""

from agit.update.updater import (
    KIND_PACKAGE,
    KIND_SOURCE,
    KIND_UNKNOWN,
    STARTUP_NET_TIMEOUT,
    UpdateStatus,
    Updater,
    detect_source_repo,
    restart_agit,
)

__all__ = [
    "KIND_PACKAGE",
    "KIND_SOURCE",
    "KIND_UNKNOWN",
    "STARTUP_NET_TIMEOUT",
    "UpdateStatus",
    "Updater",
    "detect_source_repo",
    "restart_agit",
]
