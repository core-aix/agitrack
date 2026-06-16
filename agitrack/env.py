"""Environment-variable access with pre-rename fallback.

aGiTrack reads its configuration from ``AGITRACK_*`` variables but still honours
the legacy ``AGIT_*`` names (used before the aGiT → aGiTrack rename) so existing
shells, scripts, and CI configs keep working without changes."""

from __future__ import annotations

import os


def getenv_compat(suffix: str, default: str | None = None) -> str | None:
    """Return ``AGITRACK_<suffix>``, falling back to the legacy ``AGIT_<suffix>``.

    The new name wins when both are set; ``default`` is returned when neither is."""
    value = os.environ.get(f"AGITRACK_{suffix}")
    if value is not None:
        return value
    return os.environ.get(f"AGIT_{suffix}", default)
