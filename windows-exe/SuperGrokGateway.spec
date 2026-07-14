# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path(SPECPATH)
web_root = project_root / "src" / "supergrok_openai" / "web"
web_data = [
    (str(path), "supergrok_openai/web")
    for path in web_root.iterdir()
    if path.suffix in {".html", ".css", ".js"}
]

analysis = Analysis(
    [str(project_root / "portable_main.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=web_data,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["argon2", "pytest", "ruff"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="SuperGrokGateway",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(project_root / "version_info.txt"),
)
