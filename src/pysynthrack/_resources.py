"""Resource path helpers for both source-tree and PyInstaller-frozen runs.

PyInstaller --onefile self-extracts to ``sys._MEIPASS`` at startup; bundled
data ends up under that directory.  In source mode the project root holds the
``examples/`` directory three levels above this file.

Keep this module *tiny* and dependency-free so the audio-callback panic path
or early import errors can use it without dragging the rest of the package in.
"""
from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["is_frozen", "resource_root", "examples_dir"]


def is_frozen() -> bool:
    """``True`` when running from a PyInstaller bundle (one-file or one-dir)."""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """Where bundled data lives.

    Frozen: ``sys._MEIPASS`` (the temp extraction directory in one-file mode,
    the install dir in one-dir mode).

    Source: the project root — i.e. the directory that contains ``examples/``
    and ``README.md``.  We resolve it relative to this file rather than the
    cwd so the CLI keeps working from any directory.
    """
    if is_frozen():
        # ``_MEIPASS`` is set by the PyInstaller bootloader.
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass is not None:
            return Path(meipass)
        # Fallback for one-dir builds where ``_MEIPASS`` is sometimes absent;
        # the executable directory holds the bundled data.
        return Path(sys.executable).resolve().parent
    # Source layout: src/pysynthrack/_resources.py → project root is two
    # ``parent`` calls (up out of ``pysynthrack/``) plus one more out of
    # ``src/``.
    return Path(__file__).resolve().parent.parent.parent


def examples_dir() -> Path:
    """Directory containing the bundled example ``.json`` patches."""
    return resource_root() / "examples"
