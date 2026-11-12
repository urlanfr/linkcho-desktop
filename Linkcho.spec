# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ["PyQt6.QtMultimedia"]

for package_name in ("edge_tts", "mutagen", "wordfreq"):
    package_datas, package_binaries, package_hiddenimports = collect_all(
        package_name
    )
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "notebook"],
    noarchive=False,
    optimize=1,
)

# Qt 6 on modern Windows links against the OS-provided generic ICU forwarder
# (icuuc.dll). The Codex build host also has Poppler's private ICU 78 directory
# on PATH; PyInstaller may accidentally collect that incompatible DLL pair and
# make QtCore fail before the application starts. Never ship those host DLLs.
incompatible_host_icu = {"icuuc.dll", "icudt78.dll"}
a.binaries = [
    item
    for item in a.binaries
    if str(item[0]).lower() not in incompatible_host_icu
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Linkcho Desktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
