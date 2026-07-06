# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys


def windows_ssl_binaries():
    if not sys.platform.startswith("win"):
        return []
    roots = [
        Path(sys.base_prefix),
        Path(sys.prefix),
        Path(sys.executable).resolve().parent,
    ]
    binaries = []
    seen = set()
    patterns = [
        "DLLs/_ssl.pyd",
        "DLLs/_hashlib.pyd",
        "DLLs/libssl-*.dll",
        "DLLs/libcrypto-*.dll",
        "libssl-*.dll",
        "libcrypto-*.dll",
    ]
    for root in roots:
        for pattern in patterns:
            for path in root.glob(pattern):
                resolved = path.resolve()
                if resolved in seen or not resolved.exists():
                    continue
                seen.add(resolved)
                binaries.append((str(resolved), "."))
    return binaries

a = Analysis(
    ['launcher_gui.py'],
    pathex=[],
    binaries=windows_ssl_binaries(),
    datas=[
        ('background_for_launcher.jpg', '.'),
        ('resources', 'resources'),
    ],
    hiddenimports=['ssl', '_ssl', '_hashlib'],
    hookspath=['hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='BebraLandLauncher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='resources/gml/Images/logo.ico',
    version='build/launcher_version_info.txt' if sys.platform.startswith('win') else None,
)
