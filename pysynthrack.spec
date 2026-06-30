# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — windowed (GUI) one-file build.

Run from the project root:

    pyinstaller pysynthrack.spec --noconfirm

Output: dist/PySynthRack.exe (Windows) / dist/PySynthRack (Linux/macOS).

Hidden-import / collection notes:
  * ``mido.backends.rtmidi`` is imported by string at runtime, so PyInstaller's
    static analyser can't see it.  Same for the other mido backends, but
    rtmidi is the only one we ship-depend on.
  * ``collect_submodules('pysynthrack')`` pulls in every module class even
    though some are referenced indirectly via the type registry.
  * ``collect_all('sounddevice')`` is the safe-and-loud option:  grabs the
    portaudio DLL, the package data and any submodules.  Skimping here is
    the #1 cause of "silent exit on launch" in windowed builds.
  * ``collect_all('dearpygui')`` likewise — DPG bundles a native ``.pyd``
    that's easy to miss with the lighter hooks.
  * ``pyo`` is intentionally excluded — it's an optional backend with
    heavy native deps.  Drop the exclude if you want it bundled.

Debugging a silent-exit build:
    pyinstaller pysynthrack.spec --noconfirm --debug=imports
    .\\dist\\PySynthRack.exe   (errors will surface in the console launcher
                                 if you used pysynthrack-cli.spec instead)
"""
from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

block_cipher = None

# ---------------------------------------------------------------------------
# Hidden imports
# ---------------------------------------------------------------------------
hiddenimports = []
hiddenimports += collect_submodules("pysynthrack")
hiddenimports += [
    "mido.backends.rtmidi",
    "pkg_resources.py2_warn",   # mido transitively touches this in some envs
]

# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------
datas = [
    # Bundle the example patches read-only.  ``examples_dir()`` in
    # ``pysynthrack._resources`` resolves this at runtime via ``sys._MEIPASS``.
    ("examples", "examples"),
]

# ---------------------------------------------------------------------------
# Binaries
# ---------------------------------------------------------------------------
binaries = []

# Pull sounddevice in completely (data + binaries + submodules) — the
# portaudio DLL must travel with the exe or audio init silently aborts.
sd_datas, sd_bins, sd_hidden = collect_all("sounddevice")
datas += sd_datas
binaries += sd_bins
hiddenimports += sd_hidden

# DearPyGui ships a native .pyd; collect_all is the safest hook.
dpg_datas, dpg_bins, dpg_hidden = collect_all("dearpygui")
datas += dpg_datas
binaries += dpg_bins
hiddenimports += dpg_hidden

# Belt-and-braces for rtmidi, which has a native .pyd of its own.
try:
    rt_datas, rt_bins, rt_hidden = collect_all("rtmidi")
    datas += rt_datas
    binaries += rt_bins
    hiddenimports += rt_hidden
except Exception:
    # rtmidi not installed — fine, MIDI just won't be available.
    pass

# ffmpeg binary bundled by imageio-ffmpeg (the [media] extra) — lets the
# packaged exe decode mp3/flac/ogg and the audio track of video files
# without a system ffmpeg install. Skipped cleanly if the extra isn't
# installed, so the default build is byte-for-byte unchanged.
try:
    iio_datas, iio_bins, iio_hidden = collect_all("imageio_ffmpeg")
    datas += iio_datas
    binaries += iio_bins
    hiddenimports += iio_hidden
except Exception:
    # imageio-ffmpeg not installed — fine, media decode falls back to a
    # system ffmpeg (or WAV-only).
    pass

# ---------------------------------------------------------------------------
# Analysis / build
# ---------------------------------------------------------------------------
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
    name="PySynthRack",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                  # GUI — no terminal window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
