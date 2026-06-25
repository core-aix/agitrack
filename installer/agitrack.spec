# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for agitrack on Windows.
#
# Build:
#   pip install pyinstaller
#   pyinstaller installer/agitrack.spec
#
# Output: dist/agitrack/agitrack.exe  (plus supporting files)
#
# Notes:
#   - We exclude POSIX-only stdlib modules (pty, termios, tty, fcntl) to keep
#     the bundle clean; they are never imported on Windows thanks to the
#     sys.platform guards we added in the windows-support branch.
#   - pywinpty ships as a compiled wheel; PyInstaller picks it up automatically
#     via collect_dynamic_libs.
#   - watchdog on Windows uses the ReadDirectoryChangesW backend which is pure
#     Python on Windows, so no native libs need special handling there.

from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs

hidden = [
    # Force all agitrack subpackages in — some are imported conditionally
    # (e.g. the metrics server, the shell backend) and PyInstaller's static
    # analyser would otherwise miss them.
    *collect_submodules("agitrack"),
    # prompt_toolkit uses lazy plugin loading; collect the whole package so the
    # shell UI (agitrack.shell.ui) finds completion/application at runtime.
    *collect_submodules("prompt_toolkit"),
    # pyte is fully static but small; include it whole.
    *collect_submodules("pyte"),
    # watchdog selects its OS backend by string at runtime.
    *collect_submodules("watchdog"),
]

# pywinpty's .pyd / .dll lives next to the Python binding; pull it in.
binaries = collect_dynamic_libs("winpty")

a = Analysis(
    [r"..\agitrack\__main__.py"],
    pathex=[r".."],
    binaries=binaries,
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # POSIX-only stdlib — never available on Windows anyway, but excluding
        # explicitly prevents PyInstaller from warning about missing imports.
        "pty",
        "termios",
        "tty",
        "fcntl",
        "readline",
        # Dev/test deps — not needed at runtime.
        "pytest",
        "ruff",
        "mypy",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="agitrack",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,   # UPX can trigger false-positive AV detections
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="agitrack",
)
