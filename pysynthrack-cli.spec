# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — console (CLI) one-file build.

Same bundling as ``pysynthrack.spec`` but with ``console=True``.  Output
goes to a terminal so a silent failure shows up as a real Python
traceback.  This is the debugging build.

    pyinstaller pysynthrack-cli.spec --noconfirm
    .\\dist\\PySynthRack-cli.exe --cli --seconds 2
    .\\dist\\PySynthRack-cli.exe                # launches the GUI but keeps a console
"""
from PyInstaller.utils.hooks import (
    collect_all,
    collect_submodules,
)

block_cipher = None

hiddenimports = []
hiddenimports += collect_submodules("pysynthrack")
hiddenimports += [
    "mido.backends.rtmidi",
    "pkg_resources.py2_warn",
]

datas = [("examples", "examples")]
binaries = []

sd_datas, sd_bins, sd_hidden = collect_all("sounddevice")
datas += sd_datas; binaries += sd_bins; hiddenimports += sd_hidden

dpg_datas, dpg_bins, dpg_hidden = collect_all("dearpygui")
datas += dpg_datas; binaries += dpg_bins; hiddenimports += dpg_hidden

try:
    rt_datas, rt_bins, rt_hidden = collect_all("rtmidi")
    datas += rt_datas; binaries += rt_bins; hiddenimports += rt_hidden
except Exception:
    pass

a = Analysis(
    ["packaging/entry.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pyo"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="PySynthRack-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
