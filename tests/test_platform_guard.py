"""Windows platform support.

aGiTrack now supports native Windows (ConPTY via pywinpty). This module verifies that
the package imports cleanly on all platforms and that the old Windows block is gone.
"""

from __future__ import annotations

import agitrack


def test_package_imports_without_blocking():
    # The package should import without raising SystemExit on any platform,
    # including native Windows (os.name == "nt").
    assert hasattr(agitrack, "__version__")


def test_version_is_present():
    # Sanity check that version resolution still works.
    assert isinstance(agitrack.__version__, str)
    assert agitrack.__version__  # non-empty
