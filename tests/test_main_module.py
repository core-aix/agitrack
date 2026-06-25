"""Tests for agitrack/__main__.py.

Covers both the module-level import and the __main__ guard
(the `if __name__ == "__main__": raise SystemExit(main())` path).
"""

from __future__ import annotations

import importlib
import runpy
from unittest.mock import patch

import pytest


def test_main_module_import_succeeds():
    """Importing agitrack.__main__ must not raise."""
    mod = importlib.import_module("agitrack.__main__")
    assert hasattr(mod, "main")


def test_main_module_as_entrypoint_exits_zero():
    """Running as __main__ with main() returning 0 raises SystemExit(0)."""
    with patch("agitrack.cli.main", return_value=0):
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("agitrack", run_name="__main__", alter_sys=True)
    assert exc_info.value.code == 0


def test_main_module_propagates_nonzero_exit_code():
    """Running as __main__ with main() returning 2 raises SystemExit(2)."""
    with patch("agitrack.cli.main", return_value=2):
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("agitrack", run_name="__main__", alter_sys=True)
    assert exc_info.value.code == 2
